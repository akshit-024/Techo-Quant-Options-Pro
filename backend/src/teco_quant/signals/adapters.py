"""Explicit adapters from a signal plan into replay and controlled execution."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from teco_quant.backtesting import BacktestSignal
from teco_quant.domain.enums import OptionType
from teco_quant.domain.models import AtomicSnapshot
from teco_quant.execution.models import OrderSide
from teco_quant.execution.models import TradePlan as ExecutionTradePlan
from teco_quant.strategy.spec import score_band

from .models import TradePlan


def to_execution_plan(
    plan: TradePlan,
    snapshot: AtomicSnapshot,
    *,
    validity: timedelta = timedelta(seconds=30),
    minimum_expiry_buffer: timedelta = timedelta(minutes=15),
) -> ExecutionTradePlan:
    """Convert only an actionable plan; the execution controller rechecks every gate."""

    _require_same_snapshot(plan, snapshot)
    if not plan.actionable:
        raise ValueError("only an actionable BUY plan can enter execution control")
    if validity <= timedelta(0) or minimum_expiry_buffer < timedelta(0):
        raise ValueError("execution validity and expiry buffer are invalid")
    return ExecutionTradePlan(
        signal_id=plan.signal_id,
        correlation_id=plan.signal_id,
        symbol=plan.symbol,
        security_id=plan.security_id,
        side=OrderSide.BUY,
        quantity=plan.quantity,
        limit_price=plan.entry,
        maximum_loss=plan.risk_per_lot * plan.lots,
        signal_time=plan.generated_at,
        data_time=snapshot.source_timestamp,
        valid_until=min(plan.expiry, plan.generated_at + validity),
        contract_expiry=plan.expiry,
        event_risk_active=snapshot.context.event_risk_active,
        expiry_risk_clear=(plan.expiry - plan.generated_at) > minimum_expiry_buffer,
        strategy_version=plan.strategy_version,
    )


def to_backtest_signal(
    plan: TradePlan,
    snapshot: AtomicSnapshot,
    *,
    target_index: int = 0,
    validity: timedelta = timedelta(minutes=5),
) -> BacktestSignal:
    """Map one selected target into the neutral, point-in-time replay contract."""

    _require_same_snapshot(plan, snapshot)
    if not plan.actionable:
        raise ValueError("only an actionable BUY plan can be replayed")
    if not 0 <= target_index < len(plan.targets):
        raise ValueError("target index is outside the generated trade plan")
    if validity <= timedelta(0):
        raise ValueError("backtest signal validity must be positive")
    pricing_underlying = snapshot.market.pricing_underlying(snapshot.contract.market_kind)
    if pricing_underlying is None:
        raise ValueError("pricing underlying is unavailable")
    selected_quote = next(
        (quote for quote in snapshot.option_chain if quote.security_id == plan.security_id),
        None,
    )
    if selected_quote is None:
        raise ValueError("trade-plan security is absent from its source snapshot")

    distance = plan.strike - pricing_underlying
    if abs(distance) <= snapshot.contract.strike_interval / Decimal(2):
        moneyness = "ATM"
    else:
        is_itm = (
            distance < 0
            if plan.option_type is OptionType.CALL
            else distance > 0
        )
        moneyness = "ITM" if is_itm else "OTM"
    remaining_days = (plan.expiry - plan.generated_at).total_seconds() / 86_400
    expiry_bucket = (
        "0-1D"
        if remaining_days <= 1
        else "2-7D"
        if remaining_days <= 7
        else "8-30D"
        if remaining_days <= 30
        else "31D+"
    )
    reference = snapshot.technicals.reference_volatility
    iv = selected_quote.implied_volatility
    if reference is None or reference <= 0 or iv is None:
        volatility_bucket = "UNKNOWN"
    else:
        ratio = iv / reference
        volatility_bucket = "LOW" if ratio < 0.8 else "NORMAL" if ratio <= 1.2 else "HIGH"
    ema_9 = snapshot.technicals.ema_9
    ema_21 = snapshot.technicals.ema_21
    if ema_9 is None or ema_21 is None:
        regime = "UNKNOWN"
    elif pricing_underlying > ema_9 > ema_21:
        regime = "BULLISH"
    elif pricing_underlying < ema_9 < ema_21:
        regime = "BEARISH"
    else:
        regime = "SIDEWAYS"
    return BacktestSignal(
        signal_id=plan.signal_id,
        instrument_id=plan.security_id,
        generated_at=plan.generated_at,
        option_side=plan.option_type.value,
        lots=plan.lots,
        lot_size=plan.lot_size,
        stop_price=plan.stop,
        target_price=plan.targets[target_index],
        contract_expiry=plan.expiry,
        score=plan.score,
        score_band=score_band(plan.score).value,
        market=snapshot.contract.market_kind.value,
        expiry_bucket=expiry_bucket,
        moneyness=moneyness,
        time_bucket=plan.generated_at.strftime("%H:00"),
        volatility_bucket=volatility_bucket,
        regime=regime,
        max_holding=timedelta(hours=snapshot.context.expected_holding_hours),
        valid_until=min(plan.expiry, plan.generated_at + validity),
        tags={
            "snapshot_id": snapshot.snapshot_id,
            "contract_key": snapshot.contract.contract_key,
            "evidence_version": plan.evidence_version,
        },
    )


def _require_same_snapshot(plan: TradePlan, snapshot: AtomicSnapshot) -> None:
    if (
        plan.snapshot_id != snapshot.snapshot_id
        or plan.contract_key != snapshot.contract.contract_key
        or plan.strategy_version != snapshot.strategy_version
    ):
        raise ValueError("trade plan is not bound to the supplied snapshot")

