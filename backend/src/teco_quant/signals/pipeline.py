"""One deterministic path from an accepted snapshot to a broker-independent trade plan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import isfinite, log, sqrt

from teco_quant.domain.enums import DecisionState, OptionType
from teco_quant.domain.models import (
    AtomicSnapshot,
    Greeks,
    OptionQuote,
    PreviousOptionSnapshot,
)
from teco_quant.ingestion.validation import ValidationReport
from teco_quant.serialization import content_hash
from teco_quant.strategy.spec import (
    DEFAULT_STRATEGY_CONFIG,
    DecisionInputs,
    PositionSizeResult,
    StrategyConfig,
    calculate_position_size,
    confirm_price_action,
    liquidity_score,
    resolve_decision,
    weighted_score,
)

from .models import EvidenceBreakdown, RankedStrike, SignalPipelineResult, TradePlan
from .policy import DEFAULT_EVIDENCE_POLICY, EvidencePolicy


@dataclass(frozen=True, slots=True)
class _Candidate:
    quote: OptionQuote
    score: float
    evidence: EvidenceBreakdown | None
    liquidity_score: float
    eligible: bool
    rejection_reasons: tuple[str, ...]
    risk_distance: Decimal | None


@dataclass(frozen=True, slots=True)
class _TradePlanDraft:
    signal_id: str
    snapshot_id: str
    contract_key: str
    strategy_version: str
    evidence_version: str
    generated_at: datetime
    symbol: str
    security_id: str
    option_type: OptionType
    strike: Decimal
    expiry: datetime
    score: float
    score_gap: float
    entry: Decimal
    stop: Decimal
    targets: tuple[Decimal, ...]
    lot_size: int
    lots: int
    quantity: int
    maximum_risk: Decimal
    risk_per_lot: Decimal
    premium_required: Decimal

    def finalize(self, decision: DecisionState) -> TradePlan:
        return TradePlan(
            signal_id=self.signal_id,
            snapshot_id=self.snapshot_id,
            contract_key=self.contract_key,
            strategy_version=self.strategy_version,
            evidence_version=self.evidence_version,
            generated_at=self.generated_at,
            decision=decision,
            actionable=decision in {DecisionState.BUY_CALL, DecisionState.BUY_PUT},
            symbol=self.symbol,
            security_id=self.security_id,
            option_type=self.option_type,
            strike=self.strike,
            expiry=self.expiry,
            score=self.score,
            score_gap=self.score_gap,
            entry=self.entry,
            stop=self.stop,
            targets=self.targets,
            lot_size=self.lot_size,
            lots=self.lots,
            quantity=self.quantity,
            maximum_risk=self.maximum_risk,
            risk_per_lot=self.risk_per_lot,
            premium_required=self.premium_required,
        )


class SignalPipeline:
    """Evaluate only validation-bound snapshots; never place or mutate broker orders."""

    def __init__(
        self,
        *,
        strategy: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
        evidence: EvidencePolicy = DEFAULT_EVIDENCE_POLICY,
        estimated_round_trip_cost_per_lot: Decimal = Decimal(0),
    ) -> None:
        if (
            not estimated_round_trip_cost_per_lot.is_finite()
            or estimated_round_trip_cost_per_lot < 0
        ):
            raise ValueError("estimated round-trip cost must be finite and non-negative")
        self._strategy = strategy
        self._evidence = evidence
        self._cost_per_lot = estimated_round_trip_cost_per_lot

    def evaluate(
        self,
        snapshot: AtomicSnapshot,
        report: ValidationReport,
        *,
        previous_snapshot: PreviousOptionSnapshot | None,
        modeled_greeks: Mapping[str, Greeks] | None = None,
        now: datetime | None = None,
    ) -> SignalPipelineResult:
        generated_at = now or datetime.now(UTC)
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("signal evaluation time must be timezone-aware")
        self._require_bound_report(snapshot, report)

        previous = self._previous_by_security(snapshot, previous_snapshot)
        if modeled_greeks is not None:
            expected_ids = {quote.security_id for quote in snapshot.option_chain}
            if set(modeled_greeks) != expected_ids:
                raise ValueError("modeled Greeks must cover the exact snapshot option chain")
        candidates = tuple(
            self._score_candidate(
                snapshot,
                replace_quote_greeks(quote, modeled_greeks)
                if modeled_greeks is not None
                else quote,
                previous.get(quote.security_id),
            )
            for quote in snapshot.option_chain
        )
        ranked = self._rank(snapshot, candidates)
        best_call = self._best(candidates, OptionType.CALL)
        best_put = self._best(candidates, OptionType.PUT)
        call_score = best_call.score if best_call is not None else 0.0
        put_score = best_put.score if best_put is not None else 0.0
        leading = best_call if call_score >= put_score else best_put

        pricing_underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
        price_confirmed = None
        if pricing_underlying is not None and leading is not None:
            price_confirmed = confirm_price_action(
                option_type=leading.quote.option_type,
                underlying_price=pricing_underlying,
                signal_candle_high=snapshot.context.signal_candle_high,
                signal_candle_low=snapshot.context.signal_candle_low,
            )

        position = None
        draft = None
        if leading is not None and leading.risk_distance is not None:
            draft, position = self._draft_plan(
                snapshot,
                leading,
                call_score=call_score,
                put_score=put_score,
                generated_at=generated_at,
            )

        age = (generated_at - snapshot.source_timestamp).total_seconds()
        data_complete = (
            report.accepted
            and best_call is not None
            and best_put is not None
            and all(candidate.evidence is not None for candidate in (best_call, best_put))
        )
        expiry_seconds = (snapshot.contract.option_expiry - generated_at).total_seconds()
        outcome = resolve_decision(
            DecisionInputs(
                data_complete=data_complete,
                data_stale=age < -self._strategy.future_clock_skew_seconds
                or age > self._strategy.live_max_age_seconds,
                expiry_valid=expiry_seconds > 0,
                extreme_expiry_risk=0 < expiry_seconds <= self._strategy.extreme_expiry_seconds,
                event_risk_active=snapshot.context.event_risk_active,
                liquid_strike_available=any(candidate.eligible for candidate in candidates),
                affordable=position is not None and position.affordable,
                call_score=call_score,
                put_score=put_score,
                price_action_confirmed=price_confirmed,
                operating_mode=snapshot.context.operating_mode,
            ),
            self._strategy,
        )

        plan = draft.finalize(outcome.state) if draft is not None else None
        warnings = tuple(issue.code for issue in report.warnings)
        return SignalPipelineResult(
            snapshot_id=snapshot.snapshot_id,
            generated_at=generated_at,
            decision=outcome.state,
            reason=outcome.reason.value,
            call_score=call_score,
            put_score=put_score,
            score_gap=outcome.score_gap,
            ranked_strikes=ranked,
            trade_plan=plan,
            warnings=warnings,
        )

    @staticmethod
    def _require_bound_report(snapshot: AtomicSnapshot, report: ValidationReport) -> None:
        if (
            report.snapshot_id != snapshot.snapshot_id
            or report.snapshot_hash != content_hash(snapshot)
        ):
            raise ValueError("validation report is not bound to this snapshot")

    def _previous_by_security(
        self,
        snapshot: AtomicSnapshot,
        previous: PreviousOptionSnapshot | None,
    ) -> Mapping[str, OptionQuote]:
        if previous is None:
            return {}
        if (
            previous.contract_key != snapshot.contract.contract_key
            or previous.source is not snapshot.source
            or previous.sequence >= snapshot.sequence
            or previous.source_timestamp >= snapshot.source_timestamp
        ):
            return {}
        interval = (snapshot.source_timestamp - previous.source_timestamp).total_seconds()
        if not 0 < interval <= self._strategy.live_max_age_seconds:
            return {}
        return {quote.security_id: quote for quote in previous.option_chain}

    def _score_candidate(
        self,
        snapshot: AtomicSnapshot,
        quote: OptionQuote,
        previous: OptionQuote | None,
    ) -> _Candidate:
        liquidity = liquidity_score(
            bid=quote.bid,
            ask=quote.ask,
            volume=quote.volume,
            open_interest=quote.open_interest,
            config=self._strategy,
        )
        reasons = list(liquidity.rejection_reasons)
        if quote.ask is None or quote.ltp is None or quote.implied_volatility is None:
            reasons.append("INCOMPLETE_OPTION_QUOTE")
        if previous is None or previous.ltp is None or previous.ltp <= 0:
            reasons.append("PREVIOUS_SNAPSHOT_REQUIRED")
        delta = quote.greeks.delta
        minimum_delta, maximum_delta = self._strategy.delta_range(
            snapshot.context.trading_style
        )
        if delta is None or not isfinite(delta):
            reasons.append("DELTA_UNAVAILABLE")
        elif not minimum_delta <= abs(delta) <= maximum_delta:
            reasons.append("DELTA_OUT_OF_RANGE")

        factors = self._factors(snapshot, quote, previous, liquidity.score)
        evidence = None
        score = 0.0
        risk_distance = self._risk_distance(snapshot, quote)
        if factors is not None:
            scored = weighted_score(factors, self._strategy)
            evidence = EvidenceBreakdown(
                factors=dict(factors), points=dict(scored.points), total=scored.total
            )
            score = scored.total
        eligible = not reasons and evidence is not None and liquidity.eligible
        return _Candidate(
            quote=quote,
            score=score,
            evidence=evidence,
            liquidity_score=liquidity.score,
            eligible=eligible,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            risk_distance=risk_distance,
        )

    def _factors(
        self,
        snapshot: AtomicSnapshot,
        quote: OptionQuote,
        previous: OptionQuote | None,
        liquidity_points: float,
    ) -> Mapping[str, float] | None:
        market = snapshot.market
        technicals = snapshot.technicals
        underlying = market.pricing_underlying(snapshot.contract.market_kind)
        ema_9 = technicals.ema_9
        ema_21 = technicals.ema_21
        wma_44 = technicals.wma_44
        previous_wma_44 = technicals.previous_wma_44
        atr_14 = technicals.atr_14
        ask = quote.ask
        ltp = quote.ltp
        if (
            underlying is None
            or ema_9 is None
            or ema_21 is None
            or wma_44 is None
            or previous_wma_44 is None
            or atr_14 is None
            or ask is None
            or ltp is None
        ):
            return None
        if previous is None:
            return None
        previous_ltp = previous.ltp
        implied_volatility = quote.implied_volatility
        reference_volatility = technicals.reference_volatility
        delta = quote.greeks.delta
        if (
            previous_ltp is None
            or previous_ltp <= 0
            or implied_volatility is None
            or reference_volatility is None
            or reference_volatility <= 0
            or delta is None
            or not isfinite(delta)
        ):
            return None

        bullish = quote.option_type is OptionType.CALL
        comparisons: tuple[bool, ...] = (
            underlying > ema_9,
            ema_9 > ema_21,
            ema_21 > wma_44,
            wma_44 > previous_wma_44,
        )
        if not bullish:
            comparisons = tuple(not value for value in comparisons)
        rsi = technicals.rsi_14
        rsi_supports = (
            rsi is not None
            and (50 <= rsi <= 75 if bullish else 25 <= rsi <= 50)
        )
        futures_supports = True
        if market.spot_price is not None and market.futures_price is not None:
            futures_supports = (
                market.futures_price >= market.spot_price
                if bullish
                else market.futures_price <= market.spot_price
            )
        trend = sum((*comparisons, rsi_supports, futures_supports)) / 6.0

        momentum = float((ltp - previous_ltp) / previous_ltp)
        premium = _clamp(
            0.5 + momentum / (2 * self._evidence.premium_momentum_saturation)
        )
        if quote.change_open_interest is None:
            return None
        if momentum > 0 and quote.change_open_interest > 0:
            open_interest = 1.0
        elif momentum > 0:
            open_interest = 0.70
        elif quote.change_open_interest > 0:
            open_interest = 0.25
        else:
            open_interest = 0.0

        greeks = self._greeks_factor(snapshot, quote)
        volatility = self._volatility_factor(
            implied_volatility, reference_volatility
        )
        risk_distance = self._risk_distance(snapshot, quote)
        expiry_years = max(
            0.0,
            (snapshot.contract.option_expiry - snapshot.source_timestamp).total_seconds()
            / (365.0 * 24 * 60 * 60),
        )
        expected_underlying_move = Decimal(0)
        if expiry_years > 0:
            expected_underlying_move = underlying * Decimal(
                str(reference_volatility * sqrt(expiry_years))
            )
        expected_premium_move = expected_underlying_move * Decimal(
            str(abs(delta))
        )
        reward_risk = 0.0
        if risk_distance is not None and risk_distance > 0:
            reward_risk = _clamp(
                float(expected_premium_move / risk_distance)
                / self._evidence.reward_risk_full_score
            )
        return {
            "trend": _clamp(trend),
            "premium": premium,
            "open_interest": open_interest,
            "liquidity": _clamp(liquidity_points / 100.0),
            "greeks": greeks,
            "volatility": volatility,
            "risk_reward": reward_risk,
        }

    def _greeks_factor(self, snapshot: AtomicSnapshot, quote: OptionQuote) -> float:
        delta = quote.greeks.delta
        assert delta is not None and quote.ltp is not None
        minimum, maximum = self._strategy.delta_range(snapshot.context.trading_style)
        midpoint = (minimum + maximum) / 2
        half_width = max((maximum - minimum) / 2, 1e-9)
        delta_fit = _clamp(1 - abs(abs(delta) - midpoint) / half_width)

        theoretical_fit = 0.5
        theoretical = quote.greeks.theoretical_price
        if theoretical is not None and theoretical.is_finite() and theoretical >= 0:
            error = float(abs(theoretical - quote.ltp) / quote.ltp)
            theoretical_fit = _clamp(
                1 - error / self._evidence.theoretical_error_ceiling
            )
        theta_fit = 0.5
        theta = quote.greeks.theta
        if theta is not None and isfinite(theta):
            drag = abs(theta) / float(quote.ltp)
            theta_fit = _clamp(1 - drag / self._evidence.daily_theta_drag_ceiling)
        shape = 1.0
        if quote.greeks.gamma is not None:
            shape *= float(isfinite(quote.greeks.gamma) and quote.greeks.gamma >= 0)
        if quote.greeks.vega is not None:
            shape *= float(isfinite(quote.greeks.vega) and quote.greeks.vega >= 0)
        return _clamp(
            0.45 * delta_fit + 0.25 * theoretical_fit + 0.20 * theta_fit + 0.10 * shape
        )

    def _volatility_factor(self, iv: float, reference: float) -> float:
        if not isfinite(iv) or iv <= 0 or not isfinite(reference) or reference <= 0:
            return 0.0
        ratio = iv / reference
        if ratio < self._evidence.volatility_ratio_floor:
            return _clamp(ratio / self._evidence.volatility_ratio_floor)
        if ratio > self._evidence.volatility_ratio_ceiling:
            return _clamp(self._evidence.volatility_ratio_ceiling / ratio)
        scale = max(
            abs(log(self._evidence.volatility_ratio_floor)),
            abs(log(self._evidence.volatility_ratio_ceiling)),
        )
        return _clamp(1 - abs(log(ratio)) / scale)

    def _risk_distance(
        self, snapshot: AtomicSnapshot, quote: OptionQuote
    ) -> Decimal | None:
        if quote.ask is None or quote.ask <= 0 or quote.greeks.delta is None:
            return None
        atr = snapshot.technicals.atr_14
        if atr is None or atr <= 0:
            return None
        premium_distance = quote.ask * Decimal(str(self._strategy.premium_stop_ratio))
        atr_distance = atr * Decimal(str(abs(quote.greeks.delta))) * Decimal(
            str(self._strategy.atr_stop_multiple)
        )
        distance = min(premium_distance, atr_distance)
        return max(snapshot.contract.tick_size, distance)

    def _draft_plan(
        self,
        snapshot: AtomicSnapshot,
        candidate: _Candidate,
        *,
        call_score: float,
        put_score: float,
        generated_at: datetime,
    ) -> tuple[_TradePlanDraft, PositionSizeResult]:
        quote = candidate.quote
        assert quote.ask is not None and candidate.risk_distance is not None
        tick = snapshot.contract.tick_size
        entry = _round_up(quote.ask, tick)
        raw_stop = max(tick, entry - candidate.risk_distance)
        stop = min(entry - tick, _round_up(raw_stop, tick))
        risk_distance = entry - stop
        targets = tuple(
            _round_down(entry + risk_distance * Decimal(str(multiple)), tick)
            for multiple in self._strategy.target_r_multiples
        )
        position = calculate_position_size(
            account_capital=snapshot.context.account_capital,
            risk_rate=snapshot.context.risk_per_trade,
            maximum_premium_allocation=snapshot.context.maximum_premium_allocation,
            entry=entry,
            stop=stop,
            lot_size=snapshot.contract.lot_size,
            estimated_round_trip_cost_per_lot=self._cost_per_lot,
            config=self._strategy,
        )
        identity = {
            "snapshot_id": snapshot.snapshot_id,
            "contract_key": snapshot.contract.contract_key,
            "security_id": quote.security_id,
            "side": quote.option_type.value,
            "strategy": snapshot.strategy_version,
            "evidence": self._evidence.version,
        }
        option_symbol = next(
            record.instrument.symbol
            for record in snapshot.contract.option_contracts
            if record.instrument.security_id == quote.security_id
        )
        draft = _TradePlanDraft(
            signal_id=f"signal:{content_hash(identity)}",
            snapshot_id=snapshot.snapshot_id,
            contract_key=snapshot.contract.contract_key,
            strategy_version=snapshot.strategy_version,
            evidence_version=self._evidence.version,
            generated_at=generated_at,
            symbol=option_symbol,
            security_id=quote.security_id,
            option_type=quote.option_type,
            strike=quote.strike,
            expiry=quote.expiry,
            score=candidate.score,
            score_gap=abs(call_score - put_score),
            entry=entry,
            stop=stop,
            targets=targets,
            lot_size=snapshot.contract.lot_size,
            lots=position.maximum_lots,
            quantity=position.quantity,
            maximum_risk=position.maximum_risk,
            risk_per_lot=position.risk_per_lot,
            premium_required=position.premium_per_lot * position.maximum_lots,
        )
        return draft, position

    def _rank(
        self, snapshot: AtomicSnapshot, candidates: tuple[_Candidate, ...]
    ) -> tuple[RankedStrike, ...]:
        underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                not candidate.eligible,
                -candidate.score,
                abs(candidate.quote.strike - underlying)
                if underlying is not None
                else Decimal(0),
                candidate.quote.strike,
                candidate.quote.option_type.value,
            ),
        )
        return tuple(
            RankedStrike(
                rank=index,
                security_id=candidate.quote.security_id,
                strike=candidate.quote.strike,
                option_type=candidate.quote.option_type,
                entry_ask=candidate.quote.ask,
                score=candidate.score,
                evidence=candidate.evidence,
                liquidity_score=candidate.liquidity_score,
                eligible=candidate.eligible,
                rejection_reasons=candidate.rejection_reasons,
            )
            for index, candidate in enumerate(ordered, start=1)
        )

    @staticmethod
    def _best(
        candidates: tuple[_Candidate, ...], option_type: OptionType
    ) -> _Candidate | None:
        eligible = [
            candidate
            for candidate in candidates
            if candidate.quote.option_type is option_type and candidate.eligible
        ]
        return max(eligible, key=lambda candidate: candidate.score, default=None)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _round_up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _round_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def replace_quote_greeks(
    quote: OptionQuote, modeled_greeks: Mapping[str, Greeks]
) -> OptionQuote:
    """Create an evaluation-only quote without mutating the accepted raw snapshot."""

    from dataclasses import replace

    return replace(quote, greeks=modeled_greeks[quote.security_id])
