"""Orchestrate model analytics and the signal pipeline without rewriting raw snapshots."""

from __future__ import annotations

from datetime import datetime

from teco_quant.analytics import analyze_option_chain
from teco_quant.domain.enums import PricingModel
from teco_quant.domain.models import AtomicSnapshot, Greeks, PreviousOptionSnapshot
from teco_quant.ingestion.validation import ValidationReport
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG, StrategyConfig

from .models import SignalPipelineResult
from .pipeline import SignalPipeline


class AutomatedSignalService:
    def __init__(
        self,
        *,
        pipeline: SignalPipeline | None = None,
        strategy: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
    ) -> None:
        self._strategy = strategy
        self._pipeline = pipeline or SignalPipeline(strategy=strategy)

    def evaluate(
        self,
        snapshot: AtomicSnapshot,
        report: ValidationReport,
        *,
        previous_snapshot: PreviousOptionSnapshot | None,
        now: datetime,
    ) -> SignalPipelineResult:
        contract = snapshot.contract
        if contract.pricing_model is PricingModel.BLACK_76:
            if contract.futures is None:
                raise ValueError("Black-76 evaluation requires the exact mapped future")
            pricing_security_id = contract.futures.instrument.security_id
        else:
            pricing_security_id = contract.underlying.security_id
        analyzed = analyze_option_chain(
            contract=contract,
            market=snapshot.market,
            quotes=snapshot.option_chain,
            as_of=now,
            pricing_security_id=pricing_security_id,
            risk_free_rate=self._strategy.risk_free_rate,
            dividend_yield=(
                0.0
                if contract.pricing_model is PricingModel.BLACK_76
                else self._strategy.dividend_yield
            ),
        )
        modeled = {
            leg.security_id: Greeks(
                delta=leg.analytics.delta,
                gamma=leg.analytics.gamma,
                theta=leg.analytics.theta_per_day,
                vega=leg.analytics.vega_per_vol_point,
                theoretical_price=leg.analytics.price,
            )
            for leg in analyzed
        }
        return self._pipeline.evaluate(
            snapshot,
            report,
            previous_snapshot=previous_snapshot,
            modeled_greeks=modeled,
            now=now,
        )
