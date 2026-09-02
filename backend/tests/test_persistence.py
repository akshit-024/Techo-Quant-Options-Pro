from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread

from teco_quant.domain.enums import DataSource, SnapshotStatus
from teco_quant.domain.models import ManualOverride
from teco_quant.ingestion.validation import SnapshotValidator, ValidationReport
from teco_quant.persistence.sqlite import (
    SCHEMA_VERSION,
    ChangeOIProvenanceError,
    ContractIntegrityError,
    InstrumentMasterError,
    PersistenceError,
    SnapshotOrderingError,
    SQLiteRepository,
    UnsafeSchemaError,
    ValidationBindingError,
)
from teco_quant.serialization import canonical_json, content_hash
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG
from tests.helpers import NOW, valid_master, valid_master_records, valid_snapshot


def accepted_report(snapshot) -> ValidationReport:
    return ValidationReport(
        (),
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=content_hash(snapshot),
    )


def revised_snapshot(
    snapshot,
    *,
    snapshot_id: str,
    sequence: int,
    seconds: int,
    contract=None,
):
    timestamp = NOW + timedelta(seconds=seconds)
    return replace(
        snapshot,
        snapshot_id=snapshot_id,
        sequence=sequence,
        source_timestamp=timestamp,
        received_at=timestamp,
        contract=contract or snapshot.contract,
    )


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteRepository(":memory:")
        self.repository.record_instrument_master(valid_master(), valid_master_records())
        self.repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG)
        self.validator = SnapshotValidator(clock=lambda: NOW)

    def tearDown(self) -> None:
        self.repository.close()

    def test_accepted_snapshot_is_complete_and_promoted_atomically(self) -> None:
        snapshot = valid_snapshot()
        report = self.validator.validate(snapshot)

        status = self.repository.save_ingestion(snapshot, report)

        self.assertIs(status, SnapshotStatus.ACCEPTED)
        self.assertEqual(self.repository.ingestion_attempt_count(), 1)
        self.assertEqual(self.repository.accepted_snapshot_count(), 1)
        latest = self.repository.latest_accepted_snapshot(snapshot.contract.contract_key)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["snapshot_id"], snapshot.snapshot_id)
        with self.repository._lock:
            connection = self.repository._connection
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM instrument_master_records"
                ).fetchone()[0],
                valid_master().row_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM option_snapshots WHERE snapshot_id = ?",
                    (snapshot.snapshot_id,),
                ).fetchone()[0],
                len(snapshot.option_chain),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM validation_issues WHERE snapshot_id = ?",
                    (snapshot.snapshot_id,),
                ).fetchone()[0],
                len(report.issues),
            )
            persisted = connection.execute(
                "SELECT snapshot_json, snapshot_hash FROM ingestion_attempts"
            ).fetchone()
            self.assertTrue(persisted["snapshot_json"])
            self.assertEqual(persisted["snapshot_hash"], content_hash(snapshot))
        # Instrument master + strategy config + snapshot form one audit chain.
        self.assertEqual(self.repository.audit_event_count(), 3)

    def test_strategy_context_is_persisted_with_the_atomic_snapshot(self) -> None:
        snapshot = valid_snapshot()
        self.repository.save_ingestion(snapshot, self.validator.validate(snapshot))

        context = self.repository.snapshot_context(snapshot.snapshot_id)

        self.assertIsNotNone(context)
        self.assertEqual(context["operating_mode"], "PRO")
        self.assertEqual(context["trading_style"], "INTRADAY")
        self.assertEqual(context["account_capital"], "500000")
        self.assertEqual(context["risk_per_trade"], 0.01)
        self.assertEqual(context["event_risk_active"], 0)
        self.assertIsNone(context["price_action_confirmed"])

    def test_missing_master_batch_blocks_accepted_ingestion(self) -> None:
        repository = SQLiteRepository(":memory:")
        self.addCleanup(repository.close)
        repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG)
        snapshot = valid_snapshot()

        with self.assertRaises(InstrumentMasterError):
            repository.save_ingestion(snapshot, accepted_report(snapshot))

        self.assertEqual(repository.ingestion_attempt_count(), 0)
        self.assertEqual(repository.contract_revision_count(), 0)

    def test_rejected_unregistered_contract_is_still_durably_audited(self) -> None:
        repository = SQLiteRepository(":memory:")
        self.addCleanup(repository.close)
        snapshot = valid_snapshot()
        broken = replace(
            snapshot,
            snapshot_id="unregistered-rejected",
            option_chain=(replace(snapshot.option_chain[0], bid=None),)
            + snapshot.option_chain[1:],
            strategy_version="unpublished-strategy-version",
        )
        report = self.validator.validate(broken)
        self.assertFalse(report.accepted)

        status = repository.save_ingestion(broken, report)

        self.assertIs(status, SnapshotStatus.REJECTED)
        self.assertEqual(repository.ingestion_attempt_count(), 1)
        self.assertEqual(repository.accepted_snapshot_count(), 0)
        self.assertEqual(repository.contract_revision_count(), 0)
        self.assertEqual(repository.audit_event_count(), 1)

    def test_invalid_envelope_and_naive_contract_expiry_are_durably_rejected(self) -> None:
        snapshot = valid_snapshot()
        naive = NOW.replace(tzinfo=None)
        broken = replace(
            snapshot,
            snapshot_id="invalid-envelope-audit",
            sequence=-9,
            source_timestamp=naive,
            received_at=naive,
            contract=replace(snapshot.contract, option_expiry=naive),
        )

        report = self.validator.validate(broken)

        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("INVALID_SEQUENCE"))
        self.assertTrue(report.has_code("NAIVE_TIMESTAMP"))
        status = self.repository.save_ingestion(broken, report)
        self.assertIs(status, SnapshotStatus.REJECTED)
        with self.repository._lock:
            row = self.repository._connection.execute(
                """
                SELECT sequence, source_timestamp_valid, received_at_valid,
                       source_timestamp_us, received_at_us, recorded_at_us,
                       snapshot_json, report_json
                FROM ingestion_attempts WHERE snapshot_id = ?
                """,
                (broken.snapshot_id,),
            ).fetchone()
        self.assertEqual(row["sequence"], -9)
        self.assertEqual(row["source_timestamp_valid"], 0)
        self.assertEqual(row["received_at_valid"], 0)
        self.assertEqual(row["source_timestamp_us"], row["recorded_at_us"])
        self.assertEqual(row["received_at_us"], row["recorded_at_us"])
        self.assertEqual(row["snapshot_json"], canonical_json(broken))
        self.assertIn('"INVALID_SEQUENCE"', row["report_json"])
        self.assertEqual(self.repository.accepted_snapshot_count(), 0)

    def test_master_batch_is_complete_atomic_and_immutable(self) -> None:
        repository = SQLiteRepository(":memory:")
        self.addCleanup(repository.close)
        incomplete = replace(valid_master(), row_count=valid_master().row_count - 1)
        with self.assertRaises(InstrumentMasterError):
            repository.record_instrument_master(incomplete, valid_master_records())
        with repository._lock:
            self.assertEqual(
                repository._connection.execute(
                    "SELECT COUNT(*) FROM instrument_master_batches"
                ).fetchone()[0],
                0,
            )

        repository.record_instrument_master(valid_master(), valid_master_records())
        changed = list(valid_master_records())
        changed[-1] = replace(changed[-1], display_name="tampered display name")
        with self.assertRaises(InstrumentMasterError):
            repository.record_instrument_master(valid_master(), changed)
        with repository._lock:
            self.assertEqual(
                repository._connection.execute(
                    "SELECT COUNT(*) FROM instrument_master_records"
                ).fetchone()[0],
                valid_master().row_count,
            )

    def test_unchanged_master_payload_can_be_freshly_reattested(self) -> None:
        original = valid_master()
        fresh = replace(
            original,
            batch_id=f"{original.batch_id}:fresh-attestation",
            fetched_at=NOW,
        )

        self.repository.record_instrument_master(fresh, valid_master_records())

        with self.repository._lock:
            rows = self.repository._connection.execute(
                """
                SELECT batch_id, content_hash, fetched_at_us, provenance_json
                FROM instrument_master_batches
                WHERE content_hash = ? ORDER BY fetched_at_us
                """,
                (original.content_hash,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["batch_id"] for row in rows}, {original.batch_id, fresh.batch_id})
        self.assertNotEqual(rows[0]["provenance_json"], rows[1]["provenance_json"])

    def test_wrong_option_security_id_rolls_back_every_atomic_component(self) -> None:
        snapshot = valid_snapshot()
        wrong = replace(snapshot.option_chain[0], security_id="not-in-master")
        corrupt = replace(
            snapshot,
            snapshot_id="wrong-option-security-id",
            option_chain=(wrong,) + snapshot.option_chain[1:],
        )

        with self.assertRaises(ContractIntegrityError):
            self.repository.save_ingestion(corrupt, accepted_report(corrupt))

        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertEqual(self.repository.accepted_snapshot_count(), 0)
        self.assertEqual(self.repository.contract_revision_count(), 0)
        self.assertIsNone(self.repository.snapshot_context(corrupt.snapshot_id))
        self.assertEqual(self.repository.audit_event_count(), 2)

    def test_contract_content_change_creates_an_immutable_revision(self) -> None:
        first = valid_snapshot()
        self.repository.save_ingestion(first, accepted_report(first))
        changed_contract = replace(first.contract, futures=None)
        self.assertNotEqual(first.contract.contract_key, changed_contract.contract_key)
        second = revised_snapshot(
            first,
            snapshot_id="contract-revision-two",
            sequence=2,
            seconds=1,
            contract=changed_contract,
        )

        self.repository.save_ingestion(second, accepted_report(second))

        self.assertEqual(self.repository.contract_revision_count(), 2)
        self.assertEqual(
            self.repository.latest_accepted_snapshot(first.contract.contract_key)["snapshot_id"],
            first.snapshot_id,
        )
        self.assertEqual(
            self.repository.latest_accepted_snapshot(changed_contract.contract_key)["snapshot_id"],
            second.snapshot_id,
        )

    def test_sequence_and_source_time_must_both_advance_for_accepted_data(self) -> None:
        first = valid_snapshot()
        self.repository.save_ingestion(first, accepted_report(first))

        repeated_sequence = revised_snapshot(
            first,
            snapshot_id="same-sequence",
            sequence=1,
            seconds=1,
        )
        with self.assertRaises(SnapshotOrderingError):
            self.repository.save_ingestion(
                repeated_sequence, accepted_report(repeated_sequence)
            )

        repeated_time = revised_snapshot(
            first,
            snapshot_id="same-source-time",
            sequence=2,
            seconds=0,
        )
        with self.assertRaises(SnapshotOrderingError):
            self.repository.save_ingestion(repeated_time, accepted_report(repeated_time))

        valid_second = revised_snapshot(
            first,
            snapshot_id="valid-second",
            sequence=2,
            seconds=1,
        )
        self.repository.save_ingestion(valid_second, accepted_report(valid_second))
        self.assertEqual(self.repository.ingestion_attempt_count(), 2)

    def test_rejected_snapshot_does_not_advance_or_replace_accepted_head(self) -> None:
        good = valid_snapshot()
        self.repository.save_ingestion(good, self.validator.validate(good))
        broken_quote = replace(good.option_chain[0], bid=None)
        broken = replace(
            good,
            snapshot_id="broken-snapshot",
            sequence=2,
            option_chain=(broken_quote,) + good.option_chain[1:],
        )
        report = self.validator.validate(broken)

        status = self.repository.save_ingestion(broken, report)

        self.assertIs(status, SnapshotStatus.REJECTED)
        self.assertEqual(self.repository.ingestion_attempt_count(), 2)
        self.assertEqual(self.repository.accepted_snapshot_count(), 1)
        latest = self.repository.latest_accepted_snapshot(good.contract.contract_key)
        self.assertEqual(latest["snapshot_id"], good.snapshot_id)

        accepted_second = revised_snapshot(
            good,
            snapshot_id="accepted-after-rejection",
            sequence=2,
            seconds=1,
        )
        self.repository.save_ingestion(accepted_second, accepted_report(accepted_second))
        self.assertEqual(self.repository.accepted_snapshot_count(), 2)

    def test_validation_report_must_match_id_and_content_hash(self) -> None:
        snapshot = valid_snapshot()
        cases = (
            ValidationReport(()),
            ValidationReport(
                (), snapshot_id="another-id", snapshot_hash=content_hash(snapshot)
            ),
            ValidationReport(
                (), snapshot_id=snapshot.snapshot_id, snapshot_hash="0" * 64
            ),
        )
        for report in cases:
            with self.subTest(report=report), self.assertRaises(ValidationBindingError):
                self.repository.save_ingestion(snapshot, report)
        original_report = self.validator.validate(snapshot)
        mutated = replace(
            snapshot,
            option_chain=(replace(snapshot.option_chain[0], bid=Decimal(1)),)
            + snapshot.option_chain[1:],
        )
        with self.assertRaises(ValidationBindingError):
            self.repository.save_ingestion(mutated, original_report)
        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertEqual(self.repository.contract_revision_count(), 0)

    def test_mid_write_sql_failure_rolls_back_context_attempt_contract_and_audit(self) -> None:
        snapshot = valid_snapshot()
        duplicate = snapshot.option_chain[0]
        corrupt = replace(
            snapshot,
            snapshot_id="duplicate-option-leg",
            option_chain=(duplicate, duplicate) + snapshot.option_chain[2:],
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_ingestion(corrupt, accepted_report(corrupt))

        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertEqual(self.repository.accepted_snapshot_count(), 0)
        self.assertEqual(self.repository.contract_revision_count(), 0)
        self.assertIsNone(self.repository.snapshot_context(corrupt.snapshot_id))
        self.assertEqual(self.repository.audit_event_count(), 2)

    def test_latest_previous_option_snapshot_reconstructs_all_quote_fields(self) -> None:
        first = replace(valid_snapshot(), source=DataSource.DHAN_REST)
        self.repository.save_ingestion(first, accepted_report(first))
        next_quotes = tuple(
            replace(
                quote,
                open_interest=(quote.open_interest or 0) + 25,
                change_open_interest=25,
                change_oi_source_snapshot_id=first.snapshot_id,
                change_oi_interval_seconds=1.0,
                observed_at=NOW + timedelta(seconds=1),
            )
            for quote in first.option_chain
        )
        second = revised_snapshot(
            replace(first, option_chain=next_quotes),
            snapshot_id="quote-reconstruction-two",
            sequence=2,
            seconds=1,
        )
        self.repository.save_ingestion(second, accepted_report(second))

        latest = self.repository.latest_previous_option_snapshot(
            first.contract.contract_key,
            first.source,
        )
        previous = self.repository.latest_previous_option_snapshot(
            first.contract.contract_key,
            first.source,
            before_sequence=2,
        )

        self.assertIsNotNone(latest)
        self.assertEqual(latest.snapshot_id, second.snapshot_id)
        self.assertEqual(latest.option_chain, second.option_chain)
        self.assertIsNotNone(previous)
        self.assertEqual(previous.snapshot_id, first.snapshot_id)
        self.assertEqual(previous.option_chain, first.option_chain)

    def test_change_oi_provenance_must_reference_an_accepted_snapshot(self) -> None:
        snapshot = valid_snapshot()
        quote = replace(
            snapshot.option_chain[0],
            change_open_interest=5,
            change_oi_source_snapshot_id="missing-prior-snapshot",
            change_oi_interval_seconds=1.0,
        )
        corrupt = replace(
            snapshot,
            snapshot_id="bad-change-oi-provenance",
            option_chain=(quote,) + snapshot.option_chain[1:],
        )
        with self.assertRaises(ChangeOIProvenanceError):
            self.repository.save_ingestion(corrupt, accepted_report(corrupt))
        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertIsNone(self.repository.snapshot_context(corrupt.snapshot_id))

    def test_change_oi_delta_is_recomputed_at_acceptance_boundary(self) -> None:
        first = replace(valid_snapshot(), source=DataSource.DHAN_REST)
        self.repository.save_ingestion(first, accepted_report(first))
        current_time = NOW + timedelta(seconds=1)
        forged_quote = replace(
            first.option_chain[0],
            observed_at=current_time,
            open_interest=(first.option_chain[0].open_interest or 0) + 7,
            change_open_interest=999,
            change_oi_source_snapshot_id=first.snapshot_id,
            change_oi_interval_seconds=1.0,
        )
        second = replace(
            revised_snapshot(
                first,
                snapshot_id="forged-change-oi-delta",
                sequence=2,
                seconds=1,
            ),
            option_chain=(forged_quote,) + first.option_chain[1:],
        )

        with self.assertRaises(ChangeOIProvenanceError):
            self.repository.save_ingestion(second, accepted_report(second))
        self.assertEqual(self.repository.ingestion_attempt_count(), 1)
        self.assertEqual(self.repository.accepted_snapshot_count(), 1)

    def test_timestamps_are_utc_normalized_with_epoch_microseconds(self) -> None:
        india = timezone(timedelta(hours=5, minutes=30))
        same_instant = NOW.astimezone(india)
        snapshot = replace(
            valid_snapshot(),
            source_timestamp=same_instant,
            received_at=same_instant,
        )
        self.repository.save_ingestion(snapshot, accepted_report(snapshot))

        latest = self.repository.latest_accepted_snapshot(snapshot.contract.contract_key)
        expected_us = int(
            (NOW - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)
        )
        self.assertEqual(latest["source_timestamp_utc"], NOW.isoformat())
        self.assertEqual(latest["source_timestamp_us"], expected_us)
        self.assertEqual(latest["received_at_utc"], NOW.isoformat())
        self.assertEqual(latest["received_at_us"], expected_us)

    def test_offset_timestamps_are_ordered_by_instant_not_iso_text(self) -> None:
        first = valid_snapshot()
        self.repository.save_ingestion(first, accepted_report(first))
        india = timezone(timedelta(hours=5, minutes=30))
        second_instant = (NOW + timedelta(seconds=1)).astimezone(india)
        second = replace(
            first,
            snapshot_id="offset-second",
            sequence=2,
            source_timestamp=second_instant,
            received_at=second_instant,
        )
        self.repository.save_ingestion(second, accepted_report(second))

        same_as_second = replace(
            second,
            snapshot_id="same-instant-different-offset",
            sequence=3,
            source_timestamp=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(SnapshotOrderingError):
            self.repository.save_ingestion(
                same_as_second, accepted_report(same_as_second)
            )

        latest = self.repository.latest_accepted_snapshot(first.contract.contract_key)
        self.assertEqual(latest["snapshot_id"], second.snapshot_id)
        self.assertEqual(
            latest["source_timestamp_utc"],
            (NOW + timedelta(seconds=1)).isoformat(),
        )

    def test_database_constraints_reject_forged_empty_accepted_quote(self) -> None:
        snapshot = valid_snapshot()
        empty_bid = replace(snapshot.option_chain[0], bid=None)
        corrupt = replace(
            snapshot,
            snapshot_id="forged-empty-quote",
            option_chain=(empty_bid,) + snapshot.option_chain[1:],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_ingestion(corrupt, accepted_report(corrupt))
        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertEqual(self.repository.contract_revision_count(), 0)

    def test_zero_is_a_valid_manual_override(self) -> None:
        snapshot = valid_snapshot()
        self.repository.save_ingestion(snapshot, self.validator.validate(snapshot))
        override = ManualOverride(
            field_name="technicals.reference_volatility",
            imported_value=0.12,
            override_value=0,
            overridden_by="risk-manager",
            overridden_at=NOW,
            reason="QA zero-value semantics test",
        )

        override_id = self.repository.add_manual_override(
            base_snapshot_id=snapshot.snapshot_id, override=override
        )

        self.assertTrue(override_id)
        self.assertEqual(override.effective_value(NOW), 0)

    def test_manual_override_domain_and_repository_boundaries_are_strict(self) -> None:
        for changes in (
            {"field_name": "contract.lot_size"},
            {"overridden_by": " "},
            {"reason": ""},
            {"overridden_at": NOW.replace(tzinfo=None)},
            {"expires_at": NOW - timedelta(seconds=1)},
        ):
            values = {
                "field_name": "technicals.reference_volatility",
                "imported_value": 0.12,
                "override_value": 0.2,
                "overridden_by": "risk-manager",
                "overridden_at": NOW,
                "reason": "verified manual correction",
            }
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ManualOverride(**values)

        snapshot = valid_snapshot()
        self.repository.save_ingestion(snapshot, self.validator.validate(snapshot))
        forged = ManualOverride(
            field_name="technicals.reference_volatility",
            imported_value=0.99,
            override_value=0.2,
            overridden_by="risk-manager",
            overridden_at=NOW,
            reason="mismatched imported value",
        )
        with self.assertRaisesRegex(PersistenceError, "imported_value differs"):
            self.repository.add_manual_override(
                base_snapshot_id=snapshot.snapshot_id,
                override=forged,
            )

        bypassed = replace(forged, imported_value=0.12)
        object.__setattr__(bypassed, "reason", " ")
        with self.assertRaisesRegex(PersistenceError, "non-empty reason"):
            self.repository.add_manual_override(
                base_snapshot_id=snapshot.snapshot_id,
                override=bypassed,
            )

    def test_reads_use_the_same_shared_connection_lock(self) -> None:
        started = Event()
        finished = Event()
        errors: list[BaseException] = []

        def read_count() -> None:
            started.set()
            try:
                self.repository.ingestion_attempt_count()
            except (PersistenceError, sqlite3.Error) as error:  # pragma: no cover
                errors.append(error)
            finally:
                finished.set()

        self.repository._lock.acquire()
        try:
            worker = Thread(target=read_count)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
        finally:
            self.repository._lock.release()
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertFalse(errors)

    def test_failed_begin_releases_shared_lock(self) -> None:
        self.repository._connection.execute("BEGIN IMMEDIATE")
        acquired = Event()
        try:
            with self.assertRaises(sqlite3.OperationalError), self.repository._transaction():
                pass

            def acquire_lock() -> None:
                with self.repository._lock:
                    acquired.set()

            worker = Thread(target=acquire_lock)
            worker.start()
            self.assertTrue(acquired.wait(1), "failed BEGIN stranded the repository lock")
            worker.join(1)
        finally:
            self.repository._connection.execute("ROLLBACK")


class SQLiteSchemaSafetyTests(unittest.TestCase):
    def test_new_database_is_v3_and_can_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "teco.sqlite3"
            repository = SQLiteRepository(path)
            repository.close()
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                connection.close()

            reopened = SQLiteRepository(path)
            try:
                self.assertEqual(SCHEMA_VERSION, 3)
                self.assertEqual(reopened.ingestion_attempt_count(), 0)
            finally:
                reopened.close()

    def test_unversioned_v1_v2_and_newer_databases_are_refused(self) -> None:
        for version in (0, 1, 2, 4):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "unsafe.sqlite3"
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f"PRAGMA user_version = {version}")
                finally:
                    connection.close()
                with self.assertRaises(UnsafeSchemaError):
                    SQLiteRepository(path)

    def test_incomplete_database_claiming_v3_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete-v3.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version = 3")
            finally:
                connection.close()
            with self.assertRaises(UnsafeSchemaError):
                SQLiteRepository(path)


if __name__ == "__main__":
    unittest.main()
