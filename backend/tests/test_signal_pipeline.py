from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from teco_quant.domain.enums import DecisionState, OperatingMode, OptionType
from teco_quant.domain.models import PreviousOptionSnapshot
from teco_quant.execution.models import OrderSide
from teco_quant.ingestion.validation import ValidationReport
from teco_quant.serialization import content_hash
from teco_quant.signals import (
    EVIDENCE_VERSION,
    AutomatedSignalService,
    SignalPipeline,
    to_backtest_signal,
    to_execution_plan,
)
from tests.helpers import NOW, valid_snapshot


def accepted_report(snapshot) -> ValidationReport:
    return ValidationReport(
        (), snapshot_id=snapshot.snapshot_id, snapshot_hash=content_hash(snapshot)
    )


def directional_snapshots(*, mode: OperatingMode = OperatingMode.PRO):
    baseline = valid_snapshot()
    prior_time = NOW - timedelta(seconds=5)
    prior_quotes = tuple(
        replace(quote, observed_at=prior_time) for quote in baseline.option_chain
    )
    previous = PreviousOptionSnapshot(
        snapshot_id="prior-signal-snapshot",
        sequence=1,
        source=baseline.source,
        source_timestamp=prior_time,
        contract_key=baseline.contract.contract_key,
        option_chain=prior_quotes,
    )
    current_quotes = []
    for prior in prior_quotes:
        bullish = prior.option_type is OptionType.CALL
        multiplier = Decimal("1.02") if bullish else Decimal("0.98")
        ltp = prior.ltp * multiplier
        oi_change = 150 if bullish else -150
        current_quotes.append(
            replace(
                prior,
                bid=ltp - Decimal("0.50"),
                ask=ltp + Decimal("0.50"),
                ltp=ltp,
                open_interest=prior.open_interest + oi_change,
                change_open_interest=oi_change,
                change_oi_source_snapshot_id=previous.snapshot_id,
                change_oi_interval_seconds=5.0,
                greeks=replace(prior.greeks, theoretical_price=ltp),
                observed_at=NOW,
            )
        )
    context = replace(
        baseline.context,
        operating_mode=mode,
        signal_candle_high=Decimal(24790),
        signal_candle_low=Decimal(24700),
    )
    current = replace(
        baseline,
        sequence=2,
        source_timestamp=NOW,
        received_at=NOW,
        option_chain=tuple(current_quotes),
        context=context,
    )
    return current, previous


class SignalPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = SignalPipeline(
            estimated_round_trip_cost_per_lot=Decimal(58)
        )

    def test_bullish_evidence_produces_ranked_actionable_call_plan(self) -> None:
        snapshot, previous = directional_snapshots()

        result = self.pipeline.evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=NOW,
        )

        self.assertIs(result.decision, DecisionState.BUY_CALL)
        self.assertGreaterEqual(result.call_score, 75)
        self.assertGreater(result.call_score, result.put_score)
        self.assertEqual(result.ranked_strikes[0].option_type, OptionType.CALL)
        self.assertTrue(result.ranked_strikes[0].eligible)
        self.assertEqual(
            set(result.ranked_strikes[0].evidence.factors),
            {
                "trend",
                "premium",
                "open_interest",
                "liquidity",
                "greeks",
                "volatility",
                "risk_reward",
            },
        )
        plan = result.trade_plan
        self.assertIsNotNone(plan)
        self.assertTrue(plan.actionable)
        self.assertEqual(plan.evidence_version, EVIDENCE_VERSION)
        self.assertLess(plan.stop, plan.entry)
        self.assertTrue(all(target > plan.entry for target in plan.targets))
        self.assertGreater(plan.quantity, 0)
        self.assertLessEqual(plan.risk_per_lot * plan.lots, plan.maximum_risk)

    def test_cold_start_is_insufficient_and_never_creates_a_plan(self) -> None:
        snapshot, _ = directional_snapshots()

        result = self.pipeline.evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=None,
            now=NOW,
        )

        self.assertIs(result.decision, DecisionState.INSUFFICIENT_DATA)
        self.assertIsNone(result.trade_plan)
        self.assertTrue(
            all(
                "PREVIOUS_SNAPSHOT_REQUIRED" in ranked.rejection_reasons
                for ranked in result.ranked_strikes
            )
        )

    def test_quick_mode_is_advisory_even_with_strong_evidence(self) -> None:
        snapshot, previous = directional_snapshots(mode=OperatingMode.QUICK)

        result = self.pipeline.evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=NOW,
        )

        self.assertIs(result.decision, DecisionState.WAIT)
        self.assertEqual(result.reason, "QUICK_MODE_ONLY")
        self.assertIsNotNone(result.trade_plan)
        self.assertFalse(result.trade_plan.actionable)

    def test_wide_spreads_are_visible_but_not_selectable(self) -> None:
        snapshot, previous = directional_snapshots()
        widened = tuple(
            replace(quote, ask=quote.bid * Decimal("1.10"))
            if quote.option_type is OptionType.CALL
            else quote
            for quote in snapshot.option_chain
        )
        snapshot = replace(snapshot, option_chain=widened)

        result = self.pipeline.evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=NOW,
        )

        calls = [
            ranked
            for ranked in result.ranked_strikes
            if ranked.option_type is OptionType.CALL
        ]
        self.assertTrue(all(not ranked.eligible for ranked in calls))
        self.assertTrue(
            all("WIDE_SPREAD" in ranked.rejection_reasons for ranked in calls)
        )
        self.assertIs(result.decision, DecisionState.INSUFFICIENT_DATA)

    def test_report_binding_is_mandatory(self) -> None:
        snapshot, previous = directional_snapshots()
        wrong = ValidationReport(
            (), snapshot_id="another", snapshot_hash=content_hash(snapshot)
        )
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.pipeline.evaluate(
                snapshot, wrong, previous_snapshot=previous, now=NOW
            )

    def test_actionable_plan_adapts_to_execution_and_point_in_time_replay(self) -> None:
        snapshot, previous = directional_snapshots()
        result = self.pipeline.evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=NOW,
        )
        plan = result.trade_plan
        execution = to_execution_plan(plan, snapshot)
        replay = to_backtest_signal(plan, snapshot, target_index=1)

        self.assertIs(execution.side, OrderSide.BUY)
        self.assertEqual(execution.security_id, plan.security_id)
        self.assertEqual(execution.maximum_loss, plan.risk_per_lot * plan.lots)
        self.assertEqual(execution.data_time, snapshot.source_timestamp)
        self.assertEqual(replay.instrument_id, plan.security_id)
        self.assertEqual(replay.target_price, plan.targets[1])
        self.assertEqual(replay.requested_quantity, plan.quantity)
        self.assertEqual(replay.tags["snapshot_id"], snapshot.snapshot_id)

    def test_automated_service_uses_model_greeks_without_mutating_raw_snapshot(self) -> None:
        snapshot, previous = directional_snapshots()
        raw_greeks = snapshot.option_chain[0].greeks

        result = AutomatedSignalService().evaluate(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=NOW,
        )

        self.assertIs(result.decision, DecisionState.BUY_CALL)
        self.assertIsNotNone(result.trade_plan)
        self.assertIs(snapshot.option_chain[0].greeks, raw_greeks)


if __name__ == "__main__":
    unittest.main()
