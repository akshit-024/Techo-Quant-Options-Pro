"""Deterministic paper fills for repeatable development and tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256

from teco_quant.execution.models import (
    BrokerFill,
    BrokerOrder,
    ExecutablePlan,
    FillState,
    OrderSide,
    OrderState,
)


@dataclass(frozen=True, slots=True)
class PaperBrokerConfig:
    slippage_bps: Decimal = Decimal(0)
    partial_fill_ratio: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        if (
            not self.slippage_bps.is_finite()
            or self.slippage_bps < 0
            or self.slippage_bps >= Decimal(10000)
        ):
            raise ValueError("slippage_bps must be finite and within 0..9999")
        if (
            not self.partial_fill_ratio.is_finite()
            or not Decimal(0) <= self.partial_fill_ratio <= Decimal(1)
        ):
            raise ValueError("partial_fill_ratio must be within 0..1")


class PaperBroker:
    def __init__(self, config: PaperBrokerConfig | None = None) -> None:
        self.config = config or PaperBrokerConfig()

    def submit(self, plan: ExecutablePlan, *, now: datetime) -> tuple[BrokerOrder, tuple[BrokerFill, ...]]:
        digest = sha256(plan.correlation_id.encode("utf-8")).hexdigest()
        order_id = f"paper-order-{digest[:20]}"
        filled_quantity = int(
            (Decimal(plan.quantity) * self.config.partial_fill_ratio).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if self.config.partial_fill_ratio > 0 and filled_quantity == 0:
            filled_quantity = 1
        filled_quantity = min(plan.quantity, filled_quantity)
        multiplier = Decimal(1) + (
            self.config.slippage_bps / Decimal(10000)
            if plan.side is OrderSide.BUY
            else -self.config.slippage_bps / Decimal(10000)
        )
        fill_price = (plan.limit_price * multiplier).quantize(Decimal("0.0001"))
        if filled_quantity == plan.quantity:
            order_state = OrderState.FILLED
            fill_state = FillState.COMPLETE
        elif filled_quantity > 0:
            order_state = OrderState.PARTIALLY_FILLED
            fill_state = FillState.PARTIAL
        else:
            order_state = OrderState.ACKNOWLEDGED
            fill_state = FillState.PARTIAL
        fills: tuple[BrokerFill, ...] = ()
        if filled_quantity:
            fills = (
                BrokerFill(
                    fill_id=f"paper-fill-{digest[:20]}-1",
                    order_id=order_id,
                    quantity=filled_quantity,
                    price=fill_price,
                    state=fill_state,
                    filled_at=now,
                ),
            )
        order = BrokerOrder(
            order_id=order_id,
            correlation_id=plan.correlation_id,
            signal_id=plan.signal_id,
            symbol=plan.symbol,
            security_id=plan.security_id,
            side=plan.side,
            quantity=plan.quantity,
            filled_quantity=filled_quantity,
            limit_price=plan.limit_price,
            average_fill_price=fill_price if filled_quantity else None,
            state=order_state,
            submitted_at=now,
        )
        return order, fills
