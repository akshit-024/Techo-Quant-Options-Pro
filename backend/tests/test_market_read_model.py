from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Thread

from teco_quant.api.market_read_model import MarketReadModelStore
from teco_quant.brokers.dhan import DhanDepthLevel, DhanFeedPacket
from teco_quant.domain.enums import DataSource, DecisionState, ValidationSeverity
from teco_quant.ingestion.validation import ValidationIssue, ValidationReport
from teco_quant.serialization import content_hash
from teco_quant.signals.models import RankedStrike, SignalPipelineResult
from tests.helpers import NOW, valid_snapshot


def live_snapshot(*, sequence: int = 1, observed_at: datetime = NOW):
    baseline = valid_snapshot()
    quotes = tuple(
        replace(
            quote,
            observed_at=observed_at,
            change_open_interest=100,
            change_oi_source_snapshot_id="prior-snapshot",
            change_oi_interval_seconds=10.0,
        )
        for quote in baseline.option_chain
    )
    return replace(
        baseline,
        sequence=sequence,
        source=DataSource.DHAN_LIVE,
        source_timestamp=observed_at,
        received_at=observed_at,
        market=replace(baseline.market, observed_at=observed_at),
        technicals=replace(baseline.technicals, observed_at=observed_at),
        option_chain=quotes,
    )


def bound_report(snapshot, *issues: ValidationIssue) -> ValidationReport:
    return ValidationReport(
        issues=tuple(issues),
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=content_hash(snapshot),
    )


def analysis_for(
    snapshot, *, decision: DecisionState = DecisionState.WAIT
) -> SignalPipelineResult:
    ranked = tuple(
        RankedStrike(
            rank=index,
            security_id=quote.security_id,
            strike=quote.strike,
            option_type=quote.option_type,
            entry_ask=quote.ask,
            score=70.0,
            evidence=None,
            liquidity_score=95.0,
            eligible=True,
            rejection_reasons=(),
        )
        for index, quote in enumerate(snapshot.option_chain, start=1)
    )
    return SignalPipelineResult(
        snapshot_id=snapshot.snapshot_id,
        generated_at=snapshot.source_timestamp,
        decision=decision,
        reason="TEST_DECISION",
        call_score=72.0,
        put_score=68.0,
        score_gap=4.0,
        ranked_strikes=ranked,
        trade_plan=None,
    )


class MarketReadModelStoreTests(unittest.TestCase):
    def test_fresh_complete_dhan_workspace_is_live_and_coherent(self) -> None:
        snapshot = live_snapshot()
        store = MarketReadModelStore(clock=lambda: NOW + timedelta(seconds=1))

        self.assertTrue(store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot)))
        result = store.workspace("nifty", "nifty", snapshot.contract.option_expiry.date().isoformat())

        assert result is not None
        status = result["read_model"]
        self.assertIsInstance(status, dict)
        assert isinstance(status, dict)
        self.assertEqual(status["snapshot_id"], snapshot.snapshot_id)
        self.assertEqual(status["data_mode"], "LIVE")
        self.assertTrue(status["complete"])
        self.assertFalse(status["actionable"])
        self.assertEqual(result["selection"]["market_id"], "NIFTY")
        self.assertEqual(result["contract"]["contract_key"], snapshot.contract.contract_key)
        self.assertEqual(result["chain"]["leg_count"], 10)
        self.assertEqual(len(result["chain"]["strikes"]), 5)
        self.assertEqual(result["analytics"]["decision"], "WAIT")

    def test_fresh_snapshot_dynamically_becomes_stale_and_no_trade(self) -> None:
        snapshot = live_snapshot()
        store = MarketReadModelStore()
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))

        result = store.analytics(
            "NIFTY", "NIFTY", now=NOW + timedelta(seconds=31)
        )

        assert result is not None
        status = result["read_model"]
        assert isinstance(status, dict)
        self.assertEqual(status["data_mode"], "STALE")
        self.assertFalse(status["fresh"])
        self.assertFalse(status["actionable"])
        self.assertEqual(status["operational_decision"], "NO_TRADE")
        self.assertIn("STALE_MARKET_DATA", status["blockers"])

    def test_missing_analysis_and_rejected_report_fail_closed(self) -> None:
        snapshot = live_snapshot()
        issue = ValidationIssue(
            code="BAD_CHAIN",
            severity=ValidationSeverity.ERROR,
            path="option_chain",
            message="invalid chain",
        )
        store = MarketReadModelStore()

        store.publish(snapshot, bound_report(snapshot, issue))
        result = store.chain("NIFTY", "NIFTY", now=NOW + timedelta(seconds=1))

        assert result is not None
        status = result["read_model"]
        assert isinstance(status, dict)
        self.assertEqual(status["data_mode"], "INCOMPLETE")
        self.assertEqual(status["operational_decision"], "INSUFFICIENT_DATA")
        self.assertIn("BAD_CHAIN", status["blockers"])
        self.assertIn("ANALYTICS_UNAVAILABLE", status["blockers"])

    def test_non_live_sources_never_report_live(self) -> None:
        snapshot = valid_snapshot()
        snapshot = replace(
            snapshot,
            option_chain=tuple(
                replace(quote, change_open_interest=1) for quote in snapshot.option_chain
            ),
        )
        store = MarketReadModelStore()
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))

        result = store.contract("NIFTY", "NIFTY", now=NOW + timedelta(seconds=1))

        assert result is not None
        status = result["read_model"]
        assert isinstance(status, dict)
        self.assertEqual(status["data_mode"], "NON_LIVE")
        self.assertFalse(status["actionable"])
        self.assertEqual(status["operational_decision"], "NO_TRADE")

    def test_bound_report_and_monotonic_update_are_enforced(self) -> None:
        first = live_snapshot(sequence=2, observed_at=NOW + timedelta(seconds=2))
        delayed = live_snapshot(sequence=1, observed_at=NOW + timedelta(seconds=1))
        store = MarketReadModelStore()
        self.assertTrue(store.publish(first, bound_report(first), analysis_for(first)))
        self.assertFalse(
            store.publish(delayed, bound_report(delayed), analysis_for(delayed))
        )
        with self.assertRaisesRegex(ValueError, "not bound"):
            store.publish(delayed, bound_report(first), analysis_for(delayed))

    def test_market_catalog_is_deterministic_and_unknown_selection_is_none(self) -> None:
        snapshot = live_snapshot()
        store = MarketReadModelStore()
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))

        catalog = store.markets(now=NOW + timedelta(seconds=1))

        self.assertEqual(catalog["markets"][0]["market_id"], "NIFTY")
        self.assertEqual(catalog["markets"][0]["symbols"][0]["symbol"], "NIFTY")
        self.assertIsNone(store.workspace("NIFTY", "UNKNOWN"))

    def test_wait_for_revision_coalesces_and_close_wakes_waiter(self) -> None:
        snapshot = live_snapshot()
        store = MarketReadModelStore()
        observed: list[object] = []
        waiter = Thread(target=lambda: observed.append(store.wait_for_revision(0, 2.0)))
        waiter.start()
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))
        waiter.join(timeout=2)

        self.assertFalse(waiter.is_alive())
        event = observed[0]
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.revision, 1)
        self.assertEqual(event.event_type, "WORKSPACE")

        closed_observed: list[object] = []
        closed_waiter = Thread(
            target=lambda: closed_observed.append(store.wait_for_revision(1, 2.0))
        )
        closed_waiter.start()
        store.close()
        closed_waiter.join(timeout=2)
        self.assertEqual(closed_observed, [None])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            newer = live_snapshot(sequence=2, observed_at=NOW + timedelta(seconds=2))
            store.publish(newer, bound_report(newer), analysis_for(newer))

    def test_revision_wait_rejects_unbounded_or_invalid_requests(self) -> None:
        store = MarketReadModelStore()
        with self.assertRaises(ValueError):
            store.wait_for_revision(-1, 0)
        with self.assertRaises(ValueError):
            store.wait_for_revision(0, 31)
        self.assertIsNone(store.wait_for_revision(0, 0))

    def test_dhan_feed_callback_is_bounded_and_never_actionable(self) -> None:
        snapshot = live_snapshot()
        store = MarketReadModelStore(
            clock=lambda: NOW + timedelta(seconds=1), maximum_tick_instruments=1
        )
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))
        packet = DhanFeedPacket(
            response_code=8,
            message_length=162,
            exchange_segment_code=2,
            security_id="10000",
            fields={
                "last_price": 201.5,
                "last_trade_epoch": int(NOW.timestamp()),
                "volume": 2500,
                "open_interest": 5100,
            },
            depth=(
                DhanDepthLevel(
                    bid_quantity=100,
                    ask_quantity=125,
                    bid_orders=2,
                    ask_orders=3,
                    bid_price=201.0,
                    ask_price=202.0,
                ),
            ),
        )

        self.assertTrue(store.publish_feed_tick(packet))
        result = store.latest_feed_tick("10000")

        assert result is not None
        self.assertTrue(result["fresh"])
        self.assertTrue(result["complete_quote"])
        self.assertFalse(result["actionable"])
        self.assertEqual(result["best_bid"], 201.0)
        self.assertEqual(store.wait_for_revision(1, 0).event_type, "FEED_TICK")

        unknown = replace(packet, security_id="999999")
        self.assertFalse(store.publish_feed_tick(unknown))
        second_known = replace(packet, security_id="10001")
        self.assertFalse(store.publish_feed_tick(second_known))

    def test_primary_feed_ticks_require_valid_ltt_and_monotonic_receipt_time(self) -> None:
        snapshot = live_snapshot()
        check_time = NOW + timedelta(seconds=1)
        store = MarketReadModelStore(clock=lambda: check_time)
        store.publish(snapshot, bound_report(snapshot), analysis_for(snapshot))
        base = DhanFeedPacket(
            response_code=8,
            message_length=162,
            exchange_segment_code=2,
            security_id="10000",
            fields={
                "last_price": 201.5,
                "last_trade_epoch": int(NOW.timestamp()),
                "volume": 2500,
                "open_interest": 5100,
            },
        )

        missing = replace(base, fields={"last_price": 201.5})
        zero = replace(base, fields={**base.fields, "last_trade_epoch": 0})
        self.assertFalse(store.publish_feed_tick(missing, received_at=check_time))
        self.assertFalse(store.publish_feed_tick(zero, received_at=check_time))
        self.assertEqual(store.revision, 1)
        self.assertIsNone(store.latest_feed_tick("10000", now=check_time))

        stale_ltt = replace(
            base,
            fields={
                **base.fields,
                "last_trade_epoch": int((NOW - timedelta(seconds=31)).timestamp()),
            },
        )
        self.assertTrue(store.publish_feed_tick(stale_ltt, received_at=check_time))
        self.assertFalse(store.publish_feed_tick(base, received_at=check_time))

        next_receipt = check_time + timedelta(milliseconds=1)
        future_ltt = replace(
            base,
            fields={
                **base.fields,
                "last_trade_epoch": int((check_time + timedelta(seconds=3)).timestamp()),
            },
        )
        self.assertTrue(store.publish_feed_tick(future_ltt, received_at=next_receipt))
        self.assertEqual(store.revision, 3)

        result = store.latest_feed_tick("10000", now=next_receipt)
        assert result is not None
        self.assertEqual(result["observed_at"], next_receipt.isoformat())
        self.assertTrue(result["fresh"])
        self.assertFalse(result["actionable"])

