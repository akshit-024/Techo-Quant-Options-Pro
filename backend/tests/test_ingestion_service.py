from __future__ import annotations

import json
import unittest
from datetime import timedelta

from teco_quant.domain.enums import DataSource, OptionType, SnapshotStatus
from teco_quant.ingestion.service import SnapshotIngestionService, build_dhan_snapshot
from teco_quant.ingestion.validation import SnapshotValidator
from teco_quant.persistence.sqlite import SQLiteRepository
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG
from tests.helpers import NOW, valid_master, valid_master_records, valid_snapshot


def option_chain_payload(oi_increment: int = 25) -> dict:
    baseline = valid_snapshot()
    chain: dict[str, dict] = {}
    for quote in baseline.option_chain:
        strike_key = f"{quote.strike:.6f}"
        side_key = "ce" if quote.option_type is OptionType.CALL else "pe"
        chain.setdefault(strike_key, {})[side_key] = {
            "security_id": quote.security_id,
            "top_bid_price": str(quote.bid),
            "top_ask_price": str(quote.ask),
            "last_price": str(quote.ltp),
            "top_bid_quantity": quote.bid_quantity,
            "top_ask_quantity": quote.ask_quantity,
            "volume": quote.volume,
            "oi": (quote.open_interest or 0) + oi_increment,
            "previous_oi": quote.previous_open_interest,
            "previous_close_price": str(quote.previous_close),
            "implied_volatility": (quote.implied_volatility or 0) * 100,
            "greeks": {
                "delta": quote.greeks.delta,
                "gamma": quote.greeks.gamma,
                "theta": quote.greeks.theta,
                "vega": quote.greeks.vega,
            },
        }
    return {"status": "success", "data": {"last_price": 24800, "oc": chain}}


class IngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteRepository(":memory:")
        self.repository.record_instrument_master(valid_master(), valid_master_records())
        self.repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG)
        self.service = SnapshotIngestionService(
            validator=SnapshotValidator(
                clock=lambda: NOW,
                change_oi_reference_loader=self.repository.accepted_option_snapshot,
            ),
            repository=self.repository,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_dhan_payload_is_normalized_validated_and_promoted_atomically(self) -> None:
        baseline = valid_snapshot()
        prior_time = NOW - timedelta(seconds=5)
        seed = build_dhan_snapshot(
            option_chain_payload(oi_increment=0),
            sequence=1,
            source_timestamp=prior_time,
            received_at=prior_time,
            contract=baseline.contract,
            market=baseline.market,
            technicals=baseline.technicals,
            context=baseline.context,
        )
        seed_result = self.service.ingest(seed)
        self.assertIs(seed_result.status, SnapshotStatus.ACCEPTED)
        previous_snapshot = self.repository.latest_previous_option_snapshot(
            baseline.contract.contract_key,
            DataSource.DHAN_REST,
        )
        self.assertIsNotNone(previous_snapshot)
        snapshot = build_dhan_snapshot(
            option_chain_payload(),
            sequence=2,
            source_timestamp=NOW,
            received_at=NOW,
            contract=baseline.contract,
            market=baseline.market,
            technicals=baseline.technicals,
            context=baseline.context,
            previous_snapshot=previous_snapshot,
        )

        result = self.service.ingest(snapshot)

        self.assertIs(result.status, SnapshotStatus.ACCEPTED)
        self.assertTrue(result.report.accepted, result.report.issues)
        self.assertEqual(self.repository.ingestion_attempt_count(), 2)
        self.assertEqual(self.repository.accepted_snapshot_count(), 2)
        self.assertTrue(all(quote.change_open_interest == 25 for quote in snapshot.option_chain))
        self.assertTrue(
            all(
                quote.change_oi_source_snapshot_id == seed.snapshot_id
                for quote in snapshot.option_chain
            )
        )
        self.assertTrue(
            all(quote.change_oi_interval_seconds == 5.0 for quote in snapshot.option_chain)
        )
        self.assertEqual(len(snapshot.metadata["raw_payload_hash"]), 64)
        self.assertEqual(snapshot.metadata["normalizer_version"], "dhan-v2-2")
        row = self.repository._connection.execute(
            "SELECT metadata_json, snapshot_json FROM ingestion_attempts "
            "WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        indexed_metadata = json.loads(row["metadata_json"])
        replay_snapshot = json.loads(row["snapshot_json"])
        self.assertNotIn("raw_component_payloads", indexed_metadata)
        self.assertIn("raw_component_payloads", replay_snapshot["metadata"])

    def test_dhan_snapshot_cold_start_keeps_change_oi_unknown(self) -> None:
        baseline = valid_snapshot()
        snapshot = build_dhan_snapshot(
            option_chain_payload(),
            sequence=1,
            source_timestamp=NOW,
            received_at=NOW,
            contract=baseline.contract,
            market=baseline.market,
            technicals=baseline.technicals,
            context=baseline.context,
        )

        self.assertTrue(
            all(quote.change_open_interest is None for quote in snapshot.option_chain)
        )
        self.assertTrue(
            all(
                quote.change_oi_source_snapshot_id is None
                and quote.change_oi_interval_seconds is None
                for quote in snapshot.option_chain
            )
        )
        result = self.service.ingest(snapshot)
        self.assertIs(result.status, SnapshotStatus.ACCEPTED)
        self.assertTrue(result.report.has_code("CHANGE_OI_BASELINE_REQUIRED"))


if __name__ == "__main__":
    unittest.main()
