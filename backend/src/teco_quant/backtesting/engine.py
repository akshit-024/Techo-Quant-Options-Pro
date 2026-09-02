"""Deterministic, in-memory replay engine for long option strategies."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

from teco_quant.backtesting.metrics import build_breakdowns, calculate_metrics
from teco_quant.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    BacktestSignal,
    CostBreakdown,
    ExitReason,
    JournalEntry,
    JournalEvent,
    OpenPositionSummary,
    OptionBar,
    ReplayFrame,
    Trade,
)

ZERO = Decimal(0)
BASIS_POINTS = Decimal(10000)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


@dataclass(slots=True)
class _OpenPosition:
    signal: BacktestSignal
    entry_time: datetime
    entry_price: Decimal
    entry_ask: Decimal
    quantity: int
    remaining_quantity: int
    entry_costs: CostBreakdown
    entry_slippage: Decimal
    exit_quantity: int = 0
    exit_notional: Decimal = ZERO
    exit_costs: CostBreakdown = field(default_factory=CostBreakdown)
    exit_slippage: Decimal = ZERO
    exit_reason: ExitReason | None = None
    last_exit_time: datetime | None = None
    last_exit_attempt: datetime | None = None


def _order_costs(
    *, price: Decimal, quantity: int, is_entry: bool, config: BacktestConfig
) -> CostBreakdown:
    notional = price * quantity
    costs = config.costs
    transaction = notional * costs.transaction_fee_rate
    regulatory = notional * costs.regulatory_fee_rate
    taxes = notional * (costs.entry_tax_rate if is_entry else costs.exit_tax_rate)
    brokerage = costs.brokerage_per_order
    gst = (brokerage + transaction + regulatory) * costs.gst_rate
    return CostBreakdown(
        brokerage=brokerage,
        taxes=taxes,
        transaction_fees=transaction,
        regulatory_fees=regulatory,
        gst=gst,
    )


def _entry_price(bar: OptionBar, config: BacktestConfig) -> tuple[Decimal, Decimal]:
    adverse = (
        bar.ask * config.slippage.entry_bps / BASIS_POINTS
        + config.slippage.fixed_entry_per_unit
    )
    return bar.ask + adverse, adverse


def _exit_price(bar: OptionBar, config: BacktestConfig) -> tuple[Decimal, Decimal]:
    adverse = (
        bar.bid * config.slippage.exit_bps / BASIS_POINTS
        + config.slippage.fixed_exit_per_unit
    )
    price = max(ZERO, bar.bid - adverse)
    return price, bar.bid - price


def _fillable_quantity(
    *,
    requested: int,
    visible: int | None,
    lot_size: int,
    config: BacktestConfig,
) -> int:
    partial = config.partial_fills
    if not partial.enabled:
        return requested
    if visible is None:
        return 0
    participating = int(
        (Decimal(visible) * partial.maximum_participation_rate).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return min(requested, participating // lot_size * lot_size)


def _exit_trigger(position: _OpenPosition, bar: OptionBar) -> ExitReason | None:
    # OHLC bars do not reveal event ordering.  A simultaneous stop/target hit is
    # therefore resolved stop-first, which is the conservative result for a long.
    if bar.low <= position.signal.stop_price:
        return ExitReason.STOP
    if bar.high >= position.signal.target_price:
        return ExitReason.TARGET
    if _utc(bar.timestamp) >= _utc(position.signal.contract_expiry):
        return ExitReason.EXPIRY
    if position.signal.max_holding is not None:
        deadline = position.entry_time + position.signal.max_holding
        if _utc(bar.timestamp) >= _utc(deadline):
            return ExitReason.TIME
    return None


class ReplayEngine:
    """Replay sorted frames without network, persistence, randomness, or wall-clock reads."""

    def __init__(self, config: BacktestConfig) -> None:
        self._config = config

    def run(self, frames: Sequence[ReplayFrame]) -> BacktestReport:
        ordered = tuple(frames)
        self._validate_sequence(ordered)
        started_at = ordered[0].timestamp
        ended_at = ordered[-1].timestamp
        pending: list[BacktestSignal] = []
        positions: dict[str, _OpenPosition] = {}
        trades: list[Trade] = []
        journal: list[JournalEntry] = []
        latest_bars: dict[str, OptionBar] = {}

        for frame in ordered:
            bars = {bar.instrument_id: bar for bar in frame.bars}
            latest_bars.update(bars)

            # Existing positions see this completed bar.  Entries made below do not,
            # because its OHLC path predates the fill decision at this timestamp.
            for instrument_id in sorted(positions):
                position = positions[instrument_id]
                bar = bars.get(instrument_id)
                if bar is None:
                    continue
                reason = position.exit_reason or _exit_trigger(position, bar)
                if reason is not None:
                    trade = self._execute_exit(position, bar, reason, journal)
                    if trade is not None:
                        trades.append(trade)
                        del positions[instrument_id]

            pending = self._execute_pending(
                pending=pending,
                frame=frame,
                bars=bars,
                positions=positions,
                journal=journal,
            )

            for signal in frame.signals:
                pending.append(signal)
                journal.append(
                    JournalEntry(
                        timestamp=frame.timestamp,
                        event=JournalEvent.SIGNAL,
                        signal_id=signal.signal_id,
                        instrument_id=signal.instrument_id,
                        message="signal queued for the first strictly later quote",
                    )
                )

        # Mark the final executable quote as an end-of-data liquidation.  When explicit
        # partial-fill simulation leaves insufficient depth, the residual is reported
        # openly rather than being silently filled with invented liquidity.
        for instrument_id in sorted(positions):
            position = positions[instrument_id]
            bar = latest_bars.get(instrument_id)
            if bar is None or _utc(bar.timestamp) < _utc(position.entry_time):
                continue
            if position.last_exit_attempt == bar.timestamp:
                continue
            reason = position.exit_reason or ExitReason.END_OF_DATA
            trade = self._execute_exit(position, bar, reason, journal)
            if trade is not None:
                trades.append(trade)
                del positions[instrument_id]

        for signal in pending:
            journal.append(
                JournalEntry(
                    timestamp=ended_at,
                    event=JournalEvent.UNFILLED,
                    signal_id=signal.signal_id,
                    instrument_id=signal.instrument_id,
                    message="no later executable quote before the replay ended",
                )
            )

        metrics = calculate_metrics(
            initial_capital=self._config.initial_capital,
            started_at=started_at,
            ended_at=ended_at,
            trades=trades,
        )
        open_summaries = tuple(
            OpenPositionSummary(
                signal_id=position.signal.signal_id,
                instrument_id=position.signal.instrument_id,
                remaining_quantity=position.remaining_quantity,
                entry_price=position.entry_price,
                last_timestamp=(position.last_exit_time or position.entry_time),
                pending_exit_reason=position.exit_reason,
            )
            for position in sorted(positions.values(), key=lambda item: item.signal.signal_id)
        )
        return BacktestReport(
            started_at=started_at,
            ended_at=ended_at,
            initial_capital=self._config.initial_capital,
            ending_capital=self._config.initial_capital + metrics.net_pnl,
            trades=tuple(trades),
            journal=tuple(journal),
            metrics=metrics,
            breakdowns=build_breakdowns(trades),
            open_positions=open_summaries,
        )

    @staticmethod
    def _validate_sequence(frames: tuple[ReplayFrame, ...]) -> None:
        if not frames:
            raise ValueError("at least one replay frame is required")
        previous: datetime | None = None
        signal_ids: set[str] = set()
        for frame in frames:
            instant = _utc(frame.timestamp)
            if previous is not None and instant <= previous:
                raise ValueError("replay frame timestamps must be strictly sorted and unique")
            previous = instant
            for signal in frame.signals:
                if signal.signal_id in signal_ids:
                    raise ValueError("signal IDs must be unique throughout a replay")
                signal_ids.add(signal.signal_id)

    def _execute_pending(
        self,
        *,
        pending: list[BacktestSignal],
        frame: ReplayFrame,
        bars: dict[str, OptionBar],
        positions: dict[str, _OpenPosition],
        journal: list[JournalEntry],
    ) -> list[BacktestSignal]:
        still_pending: list[BacktestSignal] = []
        for signal in pending:
            # This invariant is checked even if a caller mutates/circumvents dataclass
            # construction: a signal can never consume a bar from its own or prior time.
            if _utc(frame.timestamp) <= _utc(signal.generated_at):
                still_pending.append(signal)
                continue
            if _utc(frame.timestamp) >= _utc(signal.contract_expiry):
                self._skip(signal, frame.timestamp, "contract expired before entry", journal)
                continue
            if signal.valid_until is not None and _utc(frame.timestamp) > _utc(
                signal.valid_until
            ):
                self._skip(signal, frame.timestamp, "signal validity window elapsed", journal)
                continue
            bar = bars.get(signal.instrument_id)
            if bar is None:
                still_pending.append(signal)
                continue
            if signal.instrument_id in positions:
                self._skip(signal, frame.timestamp, "position already open", journal)
                continue
            quantity = _fillable_quantity(
                requested=signal.requested_quantity,
                visible=bar.ask_quantity,
                lot_size=signal.lot_size,
                config=self._config,
            )
            if quantity == 0:
                if self._config.partial_fills.enabled:
                    still_pending.append(signal)
                    journal.append(
                        JournalEntry(
                            timestamp=frame.timestamp,
                            event=JournalEvent.UNFILLED,
                            signal_id=signal.signal_id,
                            instrument_id=signal.instrument_id,
                            message="no whole lot within configured ask participation",
                        )
                    )
                    continue
                raise AssertionError("full-fill mode must return the requested quantity")
            price, adverse = _entry_price(bar, self._config)
            if not signal.stop_price < price < signal.target_price:
                self._skip(
                    signal,
                    frame.timestamp,
                    "executable entry is not strictly between stop and target",
                    journal,
                )
                continue
            entry_costs = _order_costs(
                price=price, quantity=quantity, is_entry=True, config=self._config
            )
            positions[signal.instrument_id] = _OpenPosition(
                signal=signal,
                entry_time=frame.timestamp,
                entry_price=price,
                entry_ask=bar.ask,
                quantity=quantity,
                remaining_quantity=quantity,
                entry_costs=entry_costs,
                entry_slippage=adverse * quantity,
            )
            fill_description = (
                "partial entry fill; unfilled remainder cancelled"
                if quantity < signal.requested_quantity
                else "entry filled"
            )
            journal.append(
                JournalEntry(
                    timestamp=frame.timestamp,
                    event=JournalEvent.ENTRY,
                    signal_id=signal.signal_id,
                    instrument_id=signal.instrument_id,
                    message=fill_description,
                    quantity=quantity,
                    price=price,
                )
            )
        return still_pending

    @staticmethod
    def _skip(
        signal: BacktestSignal,
        timestamp: datetime,
        message: str,
        journal: list[JournalEntry],
    ) -> None:
        journal.append(
            JournalEntry(
                timestamp=timestamp,
                event=JournalEvent.SKIPPED,
                signal_id=signal.signal_id,
                instrument_id=signal.instrument_id,
                message=message,
            )
        )

    def _execute_exit(
        self,
        position: _OpenPosition,
        bar: OptionBar,
        reason: ExitReason,
        journal: list[JournalEntry],
    ) -> Trade | None:
        actual_reason = position.exit_reason or reason
        position.exit_reason = actual_reason
        position.last_exit_attempt = bar.timestamp
        quantity = _fillable_quantity(
            requested=position.remaining_quantity,
            visible=bar.bid_quantity,
            lot_size=position.signal.lot_size,
            config=self._config,
        )
        if quantity == 0:
            journal.append(
                JournalEntry(
                    timestamp=bar.timestamp,
                    event=JournalEvent.UNFILLED,
                    signal_id=position.signal.signal_id,
                    instrument_id=position.signal.instrument_id,
                    message="exit pending: no whole lot within configured bid participation",
                )
            )
            return None

        price, adverse = _exit_price(bar, self._config)
        costs = _order_costs(
            price=price, quantity=quantity, is_entry=False, config=self._config
        )
        position.remaining_quantity -= quantity
        position.exit_quantity += quantity
        position.exit_notional += price * quantity
        position.exit_costs = position.exit_costs + costs
        position.exit_slippage += adverse * quantity
        position.last_exit_time = bar.timestamp
        journal.append(
            JournalEntry(
                timestamp=bar.timestamp,
                event=JournalEvent.EXIT_FILL,
                signal_id=position.signal.signal_id,
                instrument_id=position.signal.instrument_id,
                    message=f"{actual_reason.value} exit fill",
                quantity=quantity,
                price=price,
            )
        )
        if position.remaining_quantity > 0:
            return None

        exit_price = position.exit_notional / position.exit_quantity
        gross_pnl = (exit_price - position.entry_price) * position.quantity
        total_costs = position.entry_costs.total + position.exit_costs.total
        net_pnl = gross_pnl - total_costs
        initial_risk = (
            position.entry_price - position.signal.stop_price
        ) * position.quantity
        trade = Trade(
            signal_id=position.signal.signal_id,
            instrument_id=position.signal.instrument_id,
            option_side=position.signal.option_side,
            entry_time=position.entry_time,
            exit_time=position.last_exit_time or bar.timestamp,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            lots=Decimal(position.quantity) / position.signal.lot_size,
            stop_price=position.signal.stop_price,
            target_price=position.signal.target_price,
            exit_reason=actual_reason,
            entry_costs=position.entry_costs,
            exit_costs=position.exit_costs,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            initial_risk=initial_risk,
            r_multiple=net_pnl / initial_risk,
            slippage_cost=position.entry_slippage + position.exit_slippage,
            holding_seconds=(
                (position.last_exit_time or bar.timestamp) - position.entry_time
            ).total_seconds(),
            score=position.signal.score,
            score_band=position.signal.score_band,
            market=position.signal.market,
            expiry_bucket=position.signal.expiry_bucket,
            moneyness=position.signal.moneyness,
            time_bucket=position.signal.time_bucket,
            volatility_bucket=position.signal.volatility_bucket,
            regime=position.signal.regime,
        )
        journal.append(
            JournalEntry(
                timestamp=trade.exit_time,
                event=JournalEvent.EXIT,
                signal_id=trade.signal_id,
                instrument_id=trade.instrument_id,
                message=f"trade closed: {trade.exit_reason.value}",
                quantity=trade.quantity,
                price=trade.exit_price,
            )
        )
        return trade
