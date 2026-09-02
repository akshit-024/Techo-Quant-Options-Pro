"""Thread-safe, fail-closed market read models for read-only HTTP adapters.

The store retains canonical immutable domain objects rather than accepting arbitrary JSON.
Every response is rebuilt from one published :class:`AtomicSnapshot`, so a frontend cannot
observe a contract from one update and a chain or analysis from another.  Freshness is
evaluated at read time; publication never makes an old snapshot permanently look live.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from threading import Condition, RLock
from typing import Protocol

from teco_quant.brokers.dhan import DhanDepthLevel, DhanFeedPacket
from teco_quant.domain.enums import (
    DataSource,
    DecisionState,
    MarketKind,
    OptionType,
)
from teco_quant.domain.models import AtomicSnapshot, InstrumentMasterRecord, OptionQuote
from teco_quant.ingestion.validation import ValidationReport
from teco_quant.serialization import canonical_json, content_hash
from teco_quant.signals.models import RankedStrike, SignalPipelineResult
from teco_quant.strategy.spec import (
    DEFAULT_STRATEGY_CONFIG,
    StrategyConfig,
    expected_bounds,
    iv_expected_move,
    nearest_atm,
    synthetic_futures,
    time_to_expiry_years,
)

JsonObject = dict[str, object]
Clock = Callable[[], datetime]

_LIVE_SOURCES = frozenset((DataSource.DHAN_REST, DataSource.DHAN_LIVE))
_ACTIONABLE_DECISIONS = frozenset((DecisionState.BUY_CALL, DecisionState.BUY_PUT))
_MAX_SSE_WAIT_SECONDS = 30.0


class MarketWorkspaceReader(Protocol):
    """Small read-only contract intended for an HTTP adapter."""

    def markets(self, *, now: datetime | None = None) -> JsonObject: ...

    def contract(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None: ...

    def workspace(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None: ...

    def chain(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None: ...

    def analytics(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None: ...

    def latest_feed_tick(
        self, security_id: str, *, now: datetime | None = None
    ) -> JsonObject | None: ...

    def wait_for_revision(
        self, after: int, timeout: float = 15.0
    ) -> MarketReadModelEvent | None: ...

    @property
    def revision(self) -> int: ...


@dataclass(frozen=True, slots=True)
class MarketReadModelEvent:
    """One coalescing notification for an SSE or long-poll consumer.

    The store retains only this latest event.  A slow client detects skipped work from the
    revision number and reads the newest state instead of growing an unbounded per-client
    queue.
    """

    revision: int
    event_type: str
    occurred_at: datetime
    market_id: str | None = None
    symbol: str | None = None
    expiry: str | None = None
    snapshot_id: str | None = None
    security_id: str | None = None

    def as_payload(self) -> JsonObject:
        return {
            "revision": self.revision,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "market_id": self.market_id,
            "symbol": self.symbol,
            "expiry": self.expiry,
            "snapshot_id": self.snapshot_id,
            "security_id": self.security_id,
        }


@dataclass(frozen=True, slots=True)
class _SelectionKey:
    market_id: str
    symbol: str
    expiry: str


@dataclass(frozen=True, slots=True)
class _PublishedSnapshot:
    key: _SelectionKey
    snapshot: AtomicSnapshot
    report: ValidationReport
    analysis: SignalPipelineResult | None

    @property
    def ordering_key(self) -> tuple[datetime, int, datetime]:
        return (
            self.snapshot.source_timestamp.astimezone(UTC),
            self.snapshot.sequence,
            self.snapshot.received_at.astimezone(UTC),
        )


@dataclass(frozen=True, slots=True)
class _FeedTick:
    security_id: str
    exchange_segment_code: int
    response_code: int
    received_at: datetime
    observed_at: datetime
    fields: tuple[tuple[str, int | float], ...]
    depth: tuple[JsonObject, ...]
    revision: int


class MarketReadModelStore:
    """Publish and read coherent market workspaces without broker-side mutations.

    ``publish`` accepts validated canonical objects only.  An older update cannot replace a
    newer one for the same market/symbol/expiry, which protects readers from delayed network
    callbacks.  A rejected validation report may still be published for diagnostics, but its
    response is explicitly incomplete and can never become actionable.
    """

    def __init__(
        self,
        *,
        strategy: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
        clock: Clock | None = None,
        maximum_tick_instruments: int = 500,
    ) -> None:
        if (
            isinstance(maximum_tick_instruments, bool)
            or not isinstance(maximum_tick_instruments, int)
            or not 0 < maximum_tick_instruments <= 10_000
        ):
            raise ValueError("maximum tick instruments must be within 1..10000")
        self._strategy = strategy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._latest: dict[_SelectionKey, _PublishedSnapshot] = {}
        self._ticks: dict[str, _FeedTick] = {}
        self._maximum_tick_instruments = maximum_tick_instruments
        self._revision = 0
        self._latest_event: MarketReadModelEvent | None = None
        self._closed = False

    def publish(
        self,
        snapshot: AtomicSnapshot,
        report: ValidationReport,
        analysis: SignalPipelineResult | None = None,
    ) -> bool:
        """Atomically promote a validated update; return false for delayed/duplicate data."""

        if not isinstance(snapshot, AtomicSnapshot):
            raise TypeError("market read model requires an AtomicSnapshot")
        if not isinstance(report, ValidationReport):
            raise TypeError("market read model requires a ValidationReport")
        _require_aware(snapshot.source_timestamp, name="snapshot source timestamp")
        _require_aware(snapshot.received_at, name="snapshot received timestamp")
        if (
            report.snapshot_id != snapshot.snapshot_id
            or report.snapshot_hash != content_hash(snapshot)
        ):
            raise ValueError("validation report is not bound to the published snapshot")
        if analysis is not None:
            if not isinstance(analysis, SignalPipelineResult):
                raise TypeError("analysis must be a SignalPipelineResult")
            if analysis.snapshot_id != snapshot.snapshot_id:
                raise ValueError("analysis is not bound to the published snapshot")
            _require_aware(analysis.generated_at, name="analysis generation timestamp")
            if (
                analysis.trade_plan is not None
                and analysis.trade_plan.snapshot_id != snapshot.snapshot_id
            ):
                raise ValueError("analysis trade plan is not bound to the published snapshot")

        publication = _PublishedSnapshot(
            key=_selection_key(snapshot),
            snapshot=snapshot,
            report=report,
            analysis=analysis,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("market read model is closed")
            current = self._latest.get(publication.key)
            if current is not None and publication.ordering_key <= current.ordering_key:
                return False
            self._latest[publication.key] = publication
            self._revision += 1
            self._latest_event = MarketReadModelEvent(
                revision=self._revision,
                event_type="WORKSPACE",
                occurred_at=snapshot.received_at,
                market_id=publication.key.market_id,
                symbol=publication.key.symbol,
                expiry=publication.key.expiry,
                snapshot_id=snapshot.snapshot_id,
            )
            self._changed.notify_all()
        return True

    def publish_feed_tick(
        self,
        packet: DhanFeedPacket,
        *,
        received_at: datetime | None = None,
    ) -> bool:
        """Merge one decoded Dhan tick into a bounded, explicitly non-actionable cache.

        This method can be passed directly as a live-feed ``on_packet`` callback.  It accepts
        only security IDs already present in a published canonical contract, preventing an
        unexpected provider stream from growing memory.  Ticks never rewrite the atomic
        workspace or its analytics; a full normalization/validation cycle must do that.
        """

        if not isinstance(packet, DhanFeedPacket):
            raise TypeError("feed tick must be a decoded DhanFeedPacket")
        if packet.response_code in {7, 50}:
            return False
        if not packet.security_id or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
            for value in packet.fields.values()
        ):
            return False
        check_time = self._check_time(received_at)
        primary_price = packet.response_code in {2, 4, 8}
        observed_at = _feed_observed_at(packet) if primary_price else None
        if primary_price:
            if observed_at is None:
                return False
            observed_age = (check_time - observed_at).total_seconds()
            if not (
                -self._strategy.future_clock_skew_seconds
                <= observed_age
                <= self._strategy.live_max_age_seconds
            ):
                return False
        depth = tuple(_feed_depth_payload(level) for level in packet.depth)

        with self._lock:
            if self._closed:
                return False
            if packet.security_id not in self._known_security_ids_locked():
                return False
            current = self._ticks.get(packet.security_id)
            if primary_price:
                assert observed_at is not None
                if current is not None and observed_at <= current.observed_at:
                    return False
                selected_observed_at = observed_at
            else:
                # OI/previous-close packets don't contain a trade epoch.  They may enrich
                # an existing price tick but can neither create one nor refresh its market
                # observation time with local receipt time.
                if current is None:
                    return False
                selected_observed_at = current.observed_at
            if current is None and len(self._ticks) >= self._maximum_tick_instruments:
                return False
            merged = dict(current.fields) if current is not None else {}
            merged.update(packet.fields)
            selected_depth = depth or (() if current is None else current.depth)
            self._revision += 1
            self._ticks[packet.security_id] = _FeedTick(
                security_id=packet.security_id,
                exchange_segment_code=packet.exchange_segment_code,
                response_code=packet.response_code,
                received_at=check_time,
                observed_at=selected_observed_at,
                fields=tuple(sorted(merged.items())),
                depth=selected_depth,
                revision=self._revision,
            )
            self._latest_event = MarketReadModelEvent(
                revision=self._revision,
                event_type="FEED_TICK",
                occurred_at=check_time,
                security_id=packet.security_id,
            )
            self._changed.notify_all()
        return True

    def latest_feed_tick(
        self, security_id: str, *, now: datetime | None = None
    ) -> JsonObject | None:
        """Return the latest bounded feed tick, always marked non-actionable."""

        normalized = _required_security_id(security_id)
        check_time = self._check_time(now)
        with self._lock:
            tick = self._ticks.get(normalized)
        if tick is None:
            return None
        received_age = (check_time - tick.received_at).total_seconds()
        observed_age = (check_time - tick.observed_at).total_seconds()
        age = max(received_age, observed_age)
        fresh = (
            all(
                -self._strategy.future_clock_skew_seconds
                <= selected_age
                <= self._strategy.live_max_age_seconds
                for selected_age in (received_age, observed_age)
            )
        )
        fields = dict(tick.fields)
        best_bid = tick.depth[0].get("bid_price") if tick.depth else None
        best_ask = tick.depth[0].get("ask_price") if tick.depth else None
        return {
            "revision": tick.revision,
            "security_id": tick.security_id,
            "exchange_segment_code": tick.exchange_segment_code,
            "response_code": tick.response_code,
            "received_at": tick.received_at.isoformat(),
            "observed_at": tick.observed_at.isoformat(),
            "age_seconds": age,
            "received_age_seconds": received_age,
            "observed_age_seconds": observed_age,
            "fresh": fresh,
            "fields": fields,
            "depth": [dict(level) for level in tick.depth],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "complete_quote": bool(
                fresh
                and _positive_number(fields.get("last_price"))
                and _non_negative_number(fields.get("volume"))
                and _non_negative_number(fields.get("open_interest"))
                and _valid_top_of_book(best_bid, best_ask)
            ),
            "actionable": False,
            "blockers": ["ATOMIC_SNAPSHOT_REVALIDATION_REQUIRED"],
        }

    def wait_for_revision(
        self, after: int, timeout: float = 15.0
    ) -> MarketReadModelEvent | None:
        """Wait for a newer coalesced revision, a bounded timeout, or store shutdown."""

        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("revision must be a non-negative integer")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(float(timeout))
            or not 0 <= float(timeout) <= _MAX_SSE_WAIT_SECONDS
        ):
            raise ValueError(
                f"revision wait timeout must be within 0..{_MAX_SSE_WAIT_SECONDS:g} seconds"
            )
        with self._changed:
            self._changed.wait_for(
                lambda: self._closed or self._revision > after,
                timeout=float(timeout),
            )
            if self._closed or self._revision <= after:
                return None
            return self._latest_event

    def close(self) -> None:
        """Wake all waiters and reject subsequent snapshot publications."""

        with self._changed:
            if self._closed:
                return
            self._closed = True
            self._changed.notify_all()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def markets(self, *, now: datetime | None = None) -> JsonObject:
        check_time = self._check_time(now)
        with self._lock:
            publications = tuple(self._latest.values())

        grouped: dict[str, dict[str, list[_PublishedSnapshot]]] = {}
        for publication in publications:
            grouped.setdefault(publication.key.market_id, {}).setdefault(
                publication.key.symbol, []
            ).append(publication)

        markets: list[JsonObject] = []
        for market_id in sorted(grouped):
            symbols: list[JsonObject] = []
            for symbol in sorted(grouped[market_id]):
                choices = sorted(
                    grouped[market_id][symbol],
                    key=lambda item: (item.snapshot.contract.option_expiry, item.ordering_key),
                )
                latest = max(choices, key=lambda item: item.ordering_key)
                symbols.append(
                    {
                        "symbol": symbol,
                        "expiries": [item.key.expiry for item in choices],
                        "latest": self._read_status(latest, check_time),
                    }
                )
            markets.append({"market_id": market_id, "symbols": symbols})
        return {
            "generated_at": check_time.isoformat(),
            "markets": markets,
        }

    def contract(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None:
        selected = self._resolve(market_id, symbol, expiry)
        if selected is None:
            return None
        check_time = self._check_time(now)
        return {
            "read_model": self._read_status(selected, check_time),
            "selection": _selection_payload(selected),
            "contract": _contract_payload(selected.snapshot),
        }

    def workspace(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None:
        selected = self._resolve(market_id, symbol, expiry)
        if selected is None:
            return None
        check_time = self._check_time(now)
        snapshot = selected.snapshot
        return {
            "read_model": self._read_status(selected, check_time),
            "selection": _selection_payload(selected),
            "contract": _contract_payload(snapshot),
            "market": _json_object(snapshot.market),
            "technicals": _json_object(snapshot.technicals),
            "context": _json_object(snapshot.context),
            "chain": _chain_payload(selected),
            "analytics": _analytics_payload(selected),
            "validation": _validation_payload(selected.report),
        }

    def chain(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None:
        selected = self._resolve(market_id, symbol, expiry)
        if selected is None:
            return None
        check_time = self._check_time(now)
        return {
            "read_model": self._read_status(selected, check_time),
            "selection": _selection_payload(selected),
            "chain": _chain_payload(selected),
        }

    def analytics(
        self,
        market_id: str,
        symbol: str,
        expiry: str | None = None,
        *,
        now: datetime | None = None,
    ) -> JsonObject | None:
        selected = self._resolve(market_id, symbol, expiry)
        if selected is None:
            return None
        check_time = self._check_time(now)
        return {
            "read_model": self._read_status(selected, check_time),
            "selection": _selection_payload(selected),
            "analytics": _analytics_payload(selected),
        }

    def _resolve(
        self, market_id: str, symbol: str, expiry: str | None
    ) -> _PublishedSnapshot | None:
        normalized_market = _required_identifier(market_id, name="market_id")
        normalized_symbol = _required_identifier(symbol, name="symbol")
        with self._lock:
            candidates = tuple(
                publication
                for key, publication in self._latest.items()
                if key.market_id == normalized_market and key.symbol == normalized_symbol
            )
        if expiry is not None:
            normalized_expiry = _normalize_expiry_filter(expiry)
            candidates = tuple(
                item
                for item in candidates
                if item.key.expiry == normalized_expiry
                or item.snapshot.contract.option_expiry.date().isoformat()
                == normalized_expiry
            )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.ordering_key)

    def _check_time(self, value: datetime | None) -> datetime:
        selected = value or self._clock()
        return _require_aware(selected, name="read-model evaluation time")

    def _read_status(
        self, publication: _PublishedSnapshot, now: datetime
    ) -> JsonObject:
        snapshot = publication.snapshot
        static_blockers = _static_blockers(publication)
        dynamic_blockers, oldest_age, newest_age = _dynamic_blockers(
            snapshot,
            now,
            strategy=self._strategy,
        )
        source_blockers = (
            () if snapshot.source in _LIVE_SOURCES else ("NON_LIVE_SOURCE",)
        )
        blockers = tuple(
            dict.fromkeys((*static_blockers, *dynamic_blockers, *source_blockers))
        )
        incomplete = bool(static_blockers)
        stale = "STALE_MARKET_DATA" in dynamic_blockers
        future = "FUTURE_DATED_MARKET_DATA" in dynamic_blockers
        contract_master_stale = "INSTRUMENT_MASTER_STALE" in dynamic_blockers
        live_source = snapshot.source in _LIVE_SOURCES

        if not live_source:
            data_mode = "NON_LIVE"
        elif incomplete:
            data_mode = "INCOMPLETE"
        elif stale or future or contract_master_stale:
            data_mode = "STALE"
        else:
            data_mode = "LIVE"

        operational = live_source and not incomplete and not dynamic_blockers
        analysis = publication.analysis
        if incomplete:
            decision = DecisionState.INSUFFICIENT_DATA.value
        elif not operational:
            decision = DecisionState.NO_TRADE.value
        elif analysis is None:
            decision = DecisionState.INSUFFICIENT_DATA.value
        else:
            decision = analysis.decision.value
        actionable = bool(
            operational
            and analysis is not None
            and analysis.decision in _ACTIONABLE_DECISIONS
            and analysis.trade_plan is not None
            and analysis.trade_plan.actionable
        )

        return {
            "snapshot_id": snapshot.snapshot_id,
            "contract_key": snapshot.contract.contract_key,
            "sequence": snapshot.sequence,
            "source": snapshot.source.value,
            "captured_at": snapshot.source_timestamp.isoformat(),
            "received_at": snapshot.received_at.isoformat(),
            "data_mode": data_mode,
            "complete": not incomplete,
            "fresh": not stale and not future and not contract_master_stale,
            "actionable": actionable,
            "operational_decision": decision,
            "blockers": list(blockers),
            "warnings": [issue.code for issue in publication.report.warnings],
            "freshness": {
                "evaluated_at": now.isoformat(),
                "oldest_component_age_seconds": oldest_age,
                "newest_component_age_seconds": newest_age,
                "maximum_age_seconds": self._strategy.live_max_age_seconds,
                "future_clock_skew_seconds": self._strategy.future_clock_skew_seconds,
            },
        }

    def _known_security_ids_locked(self) -> set[str]:
        security_ids: set[str] = set()
        for publication in self._latest.values():
            contract = publication.snapshot.contract
            security_ids.add(contract.underlying.security_id)
            if contract.futures is not None:
                security_ids.add(contract.futures.instrument.security_id)
            security_ids.update(
                record.instrument.security_id for record in contract.option_contracts
            )
        return security_ids


def _selection_key(snapshot: AtomicSnapshot) -> _SelectionKey:
    symbol = _required_identifier(snapshot.contract.underlying.symbol, name="contract symbol")
    return _SelectionKey(
        market_id=_market_id(snapshot),
        symbol=symbol,
        expiry=snapshot.contract.option_expiry.isoformat(),
    )


def _market_id(snapshot: AtomicSnapshot) -> str:
    contract = snapshot.contract
    symbol = contract.underlying.symbol.strip().upper()
    compact_symbol = "".join(character for character in symbol if character.isalnum())
    if contract.market_kind is MarketKind.COMMODITY:
        return "MCX"
    if contract.market_kind is MarketKind.STOCK:
        return "STOCK_FNO"
    if compact_symbol in {"NIFTY", "NIFTY50"}:
        return "NIFTY"
    if compact_symbol in {"BANKNIFTY", "NIFTYBANK"}:
        return "BANKNIFTY"
    if compact_symbol in {"SENSEX", "SPBSESENSEX"}:
        return "SENSEX"
    return compact_symbol or "INDEX"


def _selection_payload(publication: _PublishedSnapshot) -> JsonObject:
    return {
        "market_id": publication.key.market_id,
        "symbol": publication.key.symbol,
        "expiry": publication.key.expiry,
    }


def _contract_payload(snapshot: AtomicSnapshot) -> JsonObject:
    contract = snapshot.contract
    return {
        "contract_key": contract.contract_key,
        "underlying": _json_object(contract.underlying),
        "market_kind": contract.market_kind.value,
        "pricing_model": contract.pricing_model.value,
        "option_expiry": contract.option_expiry.isoformat(),
        "lot_size": contract.lot_size,
        "strike_interval": str(contract.strike_interval),
        "tick_size": str(contract.tick_size),
        "master": _json_object(contract.master),
        "futures": _master_record_payload(contract.futures),
        "option_contracts": [
            _master_record_payload(record)
            for record in sorted(
                contract.option_contracts,
                key=lambda item: (
                    _decimal_sort_key(item.strike),
                    item.option_type.value if item.option_type is not None else "",
                ),
            )
        ],
    }


def _master_record_payload(record: InstrumentMasterRecord | None) -> JsonObject | None:
    return None if record is None else _json_object(record)


def _chain_payload(publication: _PublishedSnapshot) -> JsonObject:
    snapshot = publication.snapshot
    quotes_by_key = {quote.key: quote for quote in snapshot.option_chain}
    ranked = _ranking_by_security(publication.analysis)
    strikes = sorted(
        {
            quote.strike
            for quote in snapshot.option_chain
            if quote.strike.is_finite() and quote.strike > 0
        }
    )
    underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
    atm = nearest_atm(underlying, strikes) if underlying is not None and strikes else None

    rows: list[JsonObject] = []
    missing: list[JsonObject] = []
    for strike in strikes:
        call = quotes_by_key.get((strike, OptionType.CALL))
        put = quotes_by_key.get((strike, OptionType.PUT))
        if call is None:
            missing.append({"strike": str(strike), "option_type": OptionType.CALL.value})
        if put is None:
            missing.append({"strike": str(strike), "option_type": OptionType.PUT.value})
        rows.append(
            {
                "strike": str(strike),
                "moneyness": _moneyness(strike, atm, snapshot.contract.strike_interval),
                "call": _quote_payload(call, ranked),
                "put": _quote_payload(put, ranked),
            }
        )
    return {
        "atm_strike": None if atm is None else str(atm),
        "strike_interval": str(snapshot.contract.strike_interval),
        "leg_count": len(snapshot.option_chain),
        "strikes": rows,
        "missing_legs": missing,
    }


def _quote_payload(
    quote: OptionQuote | None, ranked: dict[str, RankedStrike]
) -> JsonObject | None:
    if quote is None:
        return None
    ranking = ranked.get(quote.security_id)
    value = _json_object(quote)
    value["spread"] = None if quote.spread is None else str(quote.spread)
    value["spread_ratio"] = quote.spread_ratio
    value["ranking"] = None if ranking is None else _json_object(ranking)
    return value


def _analytics_payload(publication: _PublishedSnapshot) -> JsonObject:
    snapshot = publication.snapshot
    signal = publication.analysis
    chain_metrics = _chain_analytics(snapshot)
    return {
        **chain_metrics,
        "trend": _trend_payload(snapshot),
        "call_score": None if signal is None else signal.call_score,
        "put_score": None if signal is None else signal.put_score,
        "score_gap": None if signal is None else signal.score_gap,
        "decision": (
            DecisionState.INSUFFICIENT_DATA.value
            if signal is None
            else signal.decision.value
        ),
        "decision_reason": "ANALYTICS_UNAVAILABLE" if signal is None else signal.reason,
        "ranked_strikes": (
            [] if signal is None else [_json_object(item) for item in signal.ranked_strikes]
        ),
        "trade_plan": (
            None if signal is None or signal.trade_plan is None else _json_object(signal.trade_plan)
        ),
        "generated_at": None if signal is None else signal.generated_at.isoformat(),
    }


def _chain_analytics(snapshot: AtomicSnapshot) -> JsonObject:
    calls = tuple(
        quote for quote in snapshot.option_chain if quote.option_type is OptionType.CALL
    )
    puts = tuple(
        quote for quote in snapshot.option_chain if quote.option_type is OptionType.PUT
    )
    strikes = sorted(
        {
            quote.strike
            for quote in snapshot.option_chain
            if quote.strike.is_finite() and quote.strike > 0
        }
    )
    underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
    atm = nearest_atm(underlying, strikes) if underlying is not None and strikes else None
    atm_call = next((quote for quote in calls if quote.strike == atm), None)
    atm_put = next((quote for quote in puts if quote.strike == atm), None)

    atm_ivs = tuple(
        quote.implied_volatility
        for quote in (atm_call, atm_put)
        if quote is not None and quote.implied_volatility is not None
    )
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
    move: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    if underlying is not None and atm_iv is not None:
        years = time_to_expiry_years(snapshot.source_timestamp, snapshot.contract.option_expiry)
        if years > 0 and atm_iv > 0:
            move = iv_expected_move(underlying, atm_iv, years)
            lower, upper = expected_bounds(underlying, move)

    synthetic: Decimal | None = None
    if (
        atm is not None
        and atm_call is not None
        and atm_put is not None
        and atm_call.ltp is not None
        and atm_put.ltp is not None
        and atm_call.ltp > 0
        and atm_put.ltp > 0
    ):
        synthetic = synthetic_futures(atm, atm_call.ltp, atm_put.ltp)

    call_oi = sum((quote.open_interest or 0 for quote in calls), 0)
    put_oi = sum((quote.open_interest or 0 for quote in puts), 0)
    call_change_oi = sum((quote.change_open_interest or 0 for quote in calls), 0)
    put_change_oi = sum((quote.change_open_interest or 0 for quote in puts), 0)
    support = _maximum_oi_strike(puts)
    resistance = _maximum_oi_strike(calls)
    return {
        "pricing_underlying": None if underlying is None else str(underlying),
        "expected_move": None if move is None else str(move),
        "expected_low": None if lower is None else str(lower),
        "expected_high": None if upper is None else str(upper),
        "synthetic_futures": None if synthetic is None else str(synthetic),
        "put_call_ratio": None if call_oi <= 0 else put_oi / call_oi,
        "change_oi_put_call_ratio": (
            None if call_change_oi == 0 else put_change_oi / call_change_oi
        ),
        "support": None if support is None else str(support),
        "resistance": None if resistance is None else str(resistance),
        "atm_iv_decimal": atm_iv,
    }


def _trend_payload(snapshot: AtomicSnapshot) -> JsonObject:
    market = snapshot.market
    technicals = snapshot.technicals
    underlying = market.pricing_underlying(snapshot.contract.market_kind)
    values = (
        underlying,
        technicals.ema_9,
        technicals.ema_21,
        technicals.wma_44,
        technicals.previous_wma_44,
    )
    if any(value is None for value in values):
        return {"direction": "INSUFFICIENT_DATA", "strength": 0.0}
    assert underlying is not None
    assert technicals.ema_9 is not None
    assert technicals.ema_21 is not None
    assert technicals.wma_44 is not None
    assert technicals.previous_wma_44 is not None
    bullish = sum(
        (
            underlying > technicals.ema_9,
            technicals.ema_9 > technicals.ema_21,
            technicals.ema_21 > technicals.wma_44,
            technicals.wma_44 > technicals.previous_wma_44,
        )
    )
    bearish = 4 - bullish
    if bullish == 4:
        direction = "BULLISH"
    elif bearish == 4:
        direction = "BEARISH"
    else:
        direction = "MIXED"
    return {"direction": direction, "strength": max(bullish, bearish) / 4 * 100.0}


def _maximum_oi_strike(quotes: tuple[OptionQuote, ...]) -> Decimal | None:
    eligible = tuple(quote for quote in quotes if quote.open_interest is not None)
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.open_interest or 0, -item.strike)).strike


def _ranking_by_security(
    analysis: SignalPipelineResult | None,
) -> dict[str, RankedStrike]:
    if analysis is None:
        return {}
    return {item.security_id: item for item in analysis.ranked_strikes}


def _validation_payload(report: ValidationReport) -> JsonObject:
    return {
        "accepted": report.accepted,
        "snapshot_id": report.snapshot_id,
        "snapshot_hash": report.snapshot_hash,
        "issues": [_json_object(issue) for issue in report.issues],
    }


def _static_blockers(publication: _PublishedSnapshot) -> tuple[str, ...]:
    snapshot = publication.snapshot
    blockers = [issue.code for issue in publication.report.errors]
    underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
    if underlying is None or not underlying.is_finite() or underlying <= 0:
        blockers.append("MARKET_PRICE_UNAVAILABLE")
    if not _five_strike_chain_complete(snapshot):
        blockers.append("OPTION_CHAIN_INCOMPLETE")
    if any(
        quote.change_open_interest is None
        for quote in snapshot.option_chain
    ):
        blockers.append("CHAIN_CHANGE_OI_UNAVAILABLE")
    analysis = publication.analysis
    if analysis is None:
        blockers.append("ANALYTICS_UNAVAILABLE")
    elif analysis.decision is DecisionState.INSUFFICIENT_DATA:
        blockers.append("ANALYTICS_INSUFFICIENT_DATA")
    elif not analysis.ranked_strikes:
        blockers.append("STRIKE_RANKING_UNAVAILABLE")
    return tuple(dict.fromkeys(blockers))


def _five_strike_chain_complete(snapshot: AtomicSnapshot) -> bool:
    underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
    interval = snapshot.contract.strike_interval
    if (
        underlying is None
        or not underlying.is_finite()
        or underlying <= 0
        or not interval.is_finite()
        or interval <= 0
    ):
        return False
    strikes = sorted(
        {
            quote.strike
            for quote in snapshot.option_chain
            if quote.strike.is_finite() and quote.strike > 0
        }
    )
    if len(strikes) < 5:
        return False
    atm = nearest_atm(underlying, strikes)
    required = {
        (atm + Decimal(offset) * interval, option_type)
        for offset in (-2, -1, 0, 1, 2)
        for option_type in (OptionType.CALL, OptionType.PUT)
    }
    available = {quote.key for quote in snapshot.option_chain}
    if not required.issubset(available):
        return False
    required_quotes = tuple(quote for quote in snapshot.option_chain if quote.key in required)
    return all(_quote_complete(quote) for quote in required_quotes)


def _quote_complete(quote: OptionQuote) -> bool:
    prices = (quote.bid, quote.ask, quote.ltp)
    return bool(
        all(value is not None and value.is_finite() and value > 0 for value in prices)
        and quote.ask is not None
        and quote.bid is not None
        and quote.ask >= quote.bid
        and quote.volume is not None
        and quote.volume >= 0
        and quote.open_interest is not None
        and quote.open_interest >= 0
        and quote.implied_volatility is not None
        and isfinite(quote.implied_volatility)
        and quote.implied_volatility > 0
    )


def _dynamic_blockers(
    snapshot: AtomicSnapshot,
    now: datetime,
    *,
    strategy: StrategyConfig,
) -> tuple[tuple[str, ...], float, float]:
    timestamps = (
        snapshot.source_timestamp,
        snapshot.market.observed_at,
        snapshot.technicals.observed_at,
        *(quote.observed_at for quote in snapshot.option_chain),
    )
    normalized = tuple(value.astimezone(UTC) for value in timestamps)
    ages = tuple((now.astimezone(UTC) - value).total_seconds() for value in normalized)
    oldest_age = max(ages) if ages else 0.0
    newest_age = min(ages) if ages else 0.0
    blockers: list[str] = []
    if any(age > strategy.live_max_age_seconds for age in ages):
        blockers.append("STALE_MARKET_DATA")
    if any(age < -strategy.future_clock_skew_seconds for age in ages):
        blockers.append("FUTURE_DATED_MARKET_DATA")
    if snapshot.contract.option_expiry <= now:
        blockers.append("CONTRACT_EXPIRED")
    master_age = (now - snapshot.contract.master.fetched_at).total_seconds()
    if master_age > strategy.instrument_master_max_age_seconds:
        blockers.append("INSTRUMENT_MASTER_STALE")
    return tuple(blockers), oldest_age, newest_age


def _moneyness(
    strike: Decimal, atm: Decimal | None, interval: Decimal
) -> str | None:
    if atm is None or interval <= 0:
        return None
    offset = (strike - atm) / interval
    if offset != offset.to_integral_value():
        return None
    integer = int(offset)
    if integer == 0:
        return "ATM"
    sign = "+" if integer > 0 else ""
    return f"ATM{sign}{integer}"


def _normalize_expiry_filter(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expiry must be a non-empty ISO-8601 timestamp or date")
    selected = value.strip()
    normalized = selected[:-1] + "+00:00" if selected.endswith("Z") else selected
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(f"{selected}T00:00:00").date().isoformat()
        except ValueError as exc:
            raise ValueError("expiry must be an ISO-8601 timestamp or date") from exc
    if "T" not in selected and " " not in selected:
        return parsed.date().isoformat()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expiry timestamp must include a timezone")
    return parsed.isoformat()


def _feed_observed_at(packet: DhanFeedPacket) -> datetime | None:
    epoch = packet.fields.get("last_trade_epoch")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, (int, float))
        or not isfinite(float(epoch))
        or epoch <= 0
    ):
        return None
    try:
        observed = datetime.fromtimestamp(float(epoch), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return observed


def _feed_depth_payload(level: DhanDepthLevel) -> JsonObject:
    values: tuple[int | float, ...] = (
        level.bid_quantity,
        level.ask_quantity,
        level.bid_orders,
        level.ask_orders,
        level.bid_price,
        level.ask_price,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or value < 0
        for value in values
    ):
        raise ValueError("feed depth contains an invalid numeric value")
    return {
        "bid_quantity": level.bid_quantity,
        "ask_quantity": level.ask_quantity,
        "bid_orders": level.bid_orders,
        "ask_orders": level.ask_orders,
        "bid_price": level.bid_price,
        "ask_price": level.ask_price,
    }


def _required_security_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("security_id is required")
    return value.strip()


def _positive_number(value: object) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
        and value > 0
    )


def _non_negative_number(value: object) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
        and value >= 0
    )


def _valid_top_of_book(bid: object, ask: object) -> bool:
    if not _positive_number(bid) or not _positive_number(ask):
        return False
    assert isinstance(bid, (int, float)) and not isinstance(bid, bool)
    assert isinstance(ask, (int, float)) and not isinstance(ask, bool)
    return ask >= bid


def _decimal_sort_key(value: Decimal | None) -> tuple[int, Decimal]:
    if value is None or not value.is_finite():
        return (1, Decimal(0))
    return (0, value)


def _required_identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip().upper()


def _require_aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _json_object(value: object) -> JsonObject:
    decoded = json.loads(canonical_json(value))
    if not isinstance(decoded, dict):
        raise TypeError("expected canonical serialization to produce an object")
    return {str(key): item for key, item in decoded.items()}
