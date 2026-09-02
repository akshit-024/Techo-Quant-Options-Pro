"""SQLite persistence for immutable market inputs and atomic snapshots.

Schema v3 is deliberately *fresh only*.  Earlier prototypes did not carry enough
identity and provenance information to migrate safely, so opening an unversioned,
v1, v2, newer, or structurally incomplete database raises :class:`UnsafeSchemaError`.
No order or execution state belongs in this Sprint 1 repository.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isfinite
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

from teco_quant.domain.enums import (
    DataSource,
    OptionType,
    SnapshotStatus,
)
from teco_quant.domain.models import (
    MANUAL_OVERRIDE_FIELDS,
    AtomicSnapshot,
    ContractSpec,
    Greeks,
    InstrumentMasterProvenance,
    InstrumentMasterRecord,
    ManualOverride,
    OptionQuote,
    PreviousOptionSnapshot,
)
from teco_quant.ingestion.validation import ValidationIssue, ValidationReport
from teco_quant.serialization import canonical_json, content_hash
from teco_quant.strategy.spec import StrategyConfig

SCHEMA_VERSION = 3
_SCHEMA_FINGERPRINT = "teco-quant-sqlite-v3-2026-08-21-d"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_CHANGE_OI_INTERVAL_SECONDS = 30.0


class PersistenceError(RuntimeError):
    """Base class for repository integrity failures."""


class UnsafeSchemaError(PersistenceError):
    """Raised when a database cannot be proven to be this complete v3 schema."""


class InstrumentMasterError(PersistenceError):
    """Raised when instrument-master provenance or records are incomplete/conflicting."""


class ContractIntegrityError(PersistenceError):
    """Raised when a contract cannot be tied exactly to immutable master records."""


class ValidationBindingError(PersistenceError):
    """Raised when a validation result was produced for another snapshot/content hash."""


class SnapshotOrderingError(PersistenceError):
    """Raised when sequence or provider time does not advance for a source stream."""


class ChangeOIProvenanceError(PersistenceError):
    """Raised when an accepted change-OI value cannot be exactly recomputed."""


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE repository_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE strategy_configs (
        version TEXT PRIMARY KEY,
        config_hash TEXT NOT NULL UNIQUE,
        config_json TEXT NOT NULL,
        published_at_utc TEXT NOT NULL,
        published_at_us INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE instrument_master_batches (
        batch_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        source_url TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        fetched_at_utc TEXT NOT NULL,
        fetched_at_us INTEGER NOT NULL,
        row_count INTEGER NOT NULL CHECK (row_count >= 0),
        provenance_json TEXT NOT NULL,
        UNIQUE(provider, content_hash, fetched_at_us, source_url, schema_version)
    )
    """,
    """
    CREATE TABLE instrument_master_records (
        batch_id TEXT NOT NULL,
        exchange TEXT NOT NULL,
        segment TEXT NOT NULL,
        security_id TEXT NOT NULL,
        canonical_key TEXT NOT NULL,
        symbol TEXT NOT NULL,
        display_name TEXT NOT NULL,
        instrument_type TEXT NOT NULL,
        underlying_security_id TEXT,
        expiry_utc TEXT,
        expiry_us INTEGER,
        strike TEXT,
        option_type TEXT,
        lot_size INTEGER,
        tick_size TEXT,
        record_json TEXT NOT NULL,
        PRIMARY KEY (batch_id, exchange, segment, security_id),
        UNIQUE (batch_id, canonical_key),
        UNIQUE (
            batch_id, exchange, segment, security_id,
            strike, option_type, expiry_utc, lot_size, tick_size
        ),
        FOREIGN KEY (batch_id)
            REFERENCES instrument_master_batches(batch_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE contract_revisions (
        contract_key TEXT PRIMARY KEY,
        contract_hash TEXT NOT NULL UNIQUE,
        contract_json TEXT NOT NULL,
        master_batch_id TEXT NOT NULL,
        underlying_exchange TEXT NOT NULL,
        underlying_segment TEXT NOT NULL,
        underlying_security_id TEXT NOT NULL,
        market_kind TEXT NOT NULL,
        pricing_model TEXT NOT NULL,
        option_expiry_utc TEXT NOT NULL,
        option_expiry_us INTEGER NOT NULL,
        lot_size INTEGER NOT NULL CHECK (lot_size > 0),
        strike_interval TEXT NOT NULL,
        tick_size TEXT NOT NULL,
        futures_exchange TEXT,
        futures_segment TEXT,
        futures_security_id TEXT,
        created_at_utc TEXT NOT NULL,
        created_at_us INTEGER NOT NULL,
        FOREIGN KEY (master_batch_id)
            REFERENCES instrument_master_batches(batch_id) ON DELETE RESTRICT,
        FOREIGN KEY (
            master_batch_id, underlying_exchange,
            underlying_segment, underlying_security_id
        ) REFERENCES instrument_master_records(
            batch_id, exchange, segment, security_id
        ) ON DELETE RESTRICT,
        FOREIGN KEY (
            master_batch_id, futures_exchange,
            futures_segment, futures_security_id
        ) REFERENCES instrument_master_records(
            batch_id, exchange, segment, security_id
        ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE contract_option_mappings (
        contract_key TEXT NOT NULL,
        master_batch_id TEXT NOT NULL,
        exchange TEXT NOT NULL,
        segment TEXT NOT NULL,
        security_id TEXT NOT NULL,
        strike TEXT NOT NULL,
        option_type TEXT NOT NULL,
        expiry_utc TEXT NOT NULL,
        lot_size INTEGER NOT NULL,
        tick_size TEXT NOT NULL,
        PRIMARY KEY (contract_key, strike, option_type),
        UNIQUE (contract_key, exchange, segment, security_id),
        UNIQUE (
            contract_key, strike, option_type,
            exchange, segment, security_id
        ),
        FOREIGN KEY (contract_key)
            REFERENCES contract_revisions(contract_key) ON DELETE RESTRICT,
        FOREIGN KEY (
            master_batch_id, exchange, segment, security_id,
            strike, option_type, expiry_utc, lot_size, tick_size
        ) REFERENCES instrument_master_records(
            batch_id, exchange, segment, security_id,
            strike, option_type, expiry_utc, lot_size, tick_size
        ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE source_order_heads (
        contract_key TEXT NOT NULL,
        source TEXT NOT NULL,
        last_sequence INTEGER NOT NULL,
        last_source_timestamp_utc TEXT NOT NULL,
        last_source_timestamp_us INTEGER NOT NULL,
        snapshot_id TEXT NOT NULL,
        PRIMARY KEY (contract_key, source),
        FOREIGN KEY (contract_key)
            REFERENCES contract_revisions(contract_key) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE ingestion_attempts (
        snapshot_id TEXT PRIMARY KEY,
        snapshot_hash TEXT NOT NULL UNIQUE,
        contract_key TEXT NOT NULL,
        source TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        source_timestamp_utc TEXT NOT NULL,
        source_timestamp_us INTEGER NOT NULL,
        source_timestamp_valid INTEGER NOT NULL CHECK (source_timestamp_valid IN (0, 1)),
        received_at_utc TEXT NOT NULL,
        received_at_us INTEGER NOT NULL,
        received_at_valid INTEGER NOT NULL CHECK (received_at_valid IN (0, 1)),
        strategy_version TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
        report_hash TEXT NOT NULL,
        report_json TEXT NOT NULL,
        recorded_at_utc TEXT NOT NULL,
        recorded_at_us INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE accepted_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        contract_key TEXT NOT NULL,
        source TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 0),
        source_timestamp_utc TEXT NOT NULL,
        source_timestamp_us INTEGER NOT NULL,
        UNIQUE (contract_key, source, sequence),
        UNIQUE (contract_key, source, source_timestamp_us),
        FOREIGN KEY (snapshot_id)
            REFERENCES ingestion_attempts(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY (contract_key)
            REFERENCES contract_revisions(contract_key) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE market_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        observed_at_utc TEXT NOT NULL,
        observed_at_us INTEGER NOT NULL,
        spot_price TEXT CHECK (spot_price IS NULL OR CAST(spot_price AS REAL) > 0),
        futures_price TEXT NOT NULL CHECK (CAST(futures_price AS REAL) > 0),
        previous_close TEXT NOT NULL CHECK (CAST(previous_close AS REAL) > 0),
        day_open TEXT NOT NULL CHECK (CAST(day_open AS REAL) > 0),
        day_high TEXT NOT NULL CHECK (CAST(day_high AS REAL) > 0),
        day_low TEXT NOT NULL CHECK (CAST(day_low AS REAL) > 0),
        vwap TEXT NOT NULL CHECK (CAST(vwap AS REAL) > 0),
        futures_open_interest INTEGER,
        CHECK (CAST(day_high AS REAL) >= CAST(day_low AS REAL)),
        FOREIGN KEY (snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE technical_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        observed_at_utc TEXT NOT NULL,
        observed_at_us INTEGER NOT NULL,
        ema_9 TEXT NOT NULL CHECK (CAST(ema_9 AS REAL) > 0),
        ema_21 TEXT NOT NULL CHECK (CAST(ema_21 AS REAL) > 0),
        wma_44 TEXT NOT NULL CHECK (CAST(wma_44 AS REAL) > 0),
        previous_wma_44 TEXT NOT NULL CHECK (CAST(previous_wma_44 AS REAL) > 0),
        rsi_14 REAL NOT NULL CHECK (rsi_14 >= 0 AND rsi_14 <= 100),
        atr_14 TEXT NOT NULL CHECK (CAST(atr_14 AS REAL) > 0),
        reference_volatility REAL NOT NULL CHECK (
            reference_volatility > 0 AND reference_volatility < 1.0e308
        ),
        timeframe TEXT NOT NULL CHECK (length(trim(timeframe)) > 0),
        completed_candle INTEGER NOT NULL CHECK (completed_candle = 1),
        FOREIGN KEY (snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE strategy_context_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        operating_mode TEXT NOT NULL CHECK (operating_mode IN ('QUICK', 'PRO')),
        trading_style TEXT NOT NULL CHECK (trading_style IN ('INTRADAY', 'POSITIONAL')),
        account_capital TEXT NOT NULL CHECK (CAST(account_capital AS REAL) > 0),
        risk_per_trade REAL NOT NULL CHECK (risk_per_trade > 0 AND risk_per_trade <= 0.02),
        maximum_premium_allocation REAL NOT NULL CHECK (
            maximum_premium_allocation > 0 AND maximum_premium_allocation <= 1
        ),
        event_risk_active INTEGER CHECK (event_risk_active IN (0, 1)),
        price_action_confirmed INTEGER CHECK (price_action_confirmed IN (0, 1)),
        signal_candle_high TEXT NOT NULL CHECK (CAST(signal_candle_high AS REAL) > 0),
        signal_candle_low TEXT NOT NULL CHECK (CAST(signal_candle_low AS REAL) > 0),
        expected_holding_hours REAL NOT NULL CHECK (
            expected_holding_hours > 0 AND expected_holding_hours < 1.0e308
        ),
        CHECK (CAST(signal_candle_high AS REAL) >= CAST(signal_candle_low AS REAL)),
        FOREIGN KEY (snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE option_snapshots (
        snapshot_id TEXT NOT NULL,
        contract_key TEXT NOT NULL,
        strike TEXT NOT NULL CHECK (CAST(strike AS REAL) > 0),
        option_type TEXT NOT NULL,
        exchange TEXT NOT NULL,
        segment TEXT NOT NULL,
        security_id TEXT NOT NULL,
        expiry_utc TEXT NOT NULL,
        expiry_us INTEGER NOT NULL,
        observed_at_utc TEXT NOT NULL,
        observed_at_us INTEGER NOT NULL,
        bid TEXT NOT NULL CHECK (CAST(bid AS REAL) > 0),
        ask TEXT NOT NULL CHECK (CAST(ask AS REAL) > 0),
        ltp TEXT NOT NULL CHECK (CAST(ltp AS REAL) > 0),
        bid_quantity INTEGER,
        ask_quantity INTEGER,
        volume INTEGER NOT NULL CHECK (volume >= 0),
        open_interest INTEGER NOT NULL CHECK (open_interest >= 0),
        previous_open_interest INTEGER,
        change_open_interest INTEGER,
        change_oi_source_snapshot_id TEXT,
        change_oi_interval_seconds REAL,
        implied_volatility REAL NOT NULL CHECK (
            implied_volatility > 0 AND implied_volatility < 1.0e308
        ),
        previous_close TEXT,
        delta REAL,
        gamma REAL,
        theta REAL,
        vega REAL,
        theoretical_price TEXT,
        PRIMARY KEY (snapshot_id, strike, option_type),
        UNIQUE (snapshot_id, security_id),
        CHECK (CAST(ask AS REAL) >= CAST(bid AS REAL)),
        CHECK (
            (change_open_interest IS NULL
                AND change_oi_source_snapshot_id IS NULL
                AND change_oi_interval_seconds IS NULL)
            OR
            (change_open_interest IS NOT NULL
                AND change_oi_source_snapshot_id IS NOT NULL
                AND change_oi_interval_seconds > 0
                AND change_oi_interval_seconds < 1.0e308)
        ),
        FOREIGN KEY (snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY (change_oi_source_snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY (
            contract_key, strike, option_type,
            exchange, segment, security_id
        ) REFERENCES contract_option_mappings(
            contract_key, strike, option_type,
            exchange, segment, security_id
        ) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE validation_issues (
        snapshot_id TEXT NOT NULL,
        issue_index INTEGER NOT NULL,
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        path TEXT NOT NULL,
        message TEXT NOT NULL,
        observed_json TEXT,
        expected_json TEXT,
        issue_json TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, issue_index),
        FOREIGN KEY (snapshot_id)
            REFERENCES ingestion_attempts(snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE manual_overrides (
        override_id TEXT PRIMARY KEY,
        base_snapshot_id TEXT NOT NULL,
        field_name TEXT NOT NULL CHECK (length(trim(field_name)) > 0),
        imported_value_json TEXT NOT NULL,
        override_value_json TEXT,
        overridden_by TEXT NOT NULL CHECK (length(trim(overridden_by)) > 0),
        overridden_at_utc TEXT NOT NULL,
        overridden_at_us INTEGER NOT NULL,
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        expires_at_utc TEXT,
        expires_at_us INTEGER,
        created_at_utc TEXT NOT NULL,
        created_at_us INTEGER NOT NULL,
        CHECK (
            (expires_at_utc IS NULL AND expires_at_us IS NULL)
            OR
            (expires_at_utc IS NOT NULL AND expires_at_us > overridden_at_us)
        ),
        FOREIGN KEY (base_snapshot_id)
            REFERENCES accepted_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE audit_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at_utc TEXT NOT NULL,
        occurred_at_us INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        previous_hash TEXT,
        event_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE INDEX accepted_snapshot_lookup
    ON accepted_snapshots(contract_key, source, source_timestamp_us DESC, sequence DESC)
    """,
    """
    CREATE INDEX option_snapshot_lookup
    ON option_snapshots(snapshot_id, strike, option_type)
    """,
)


_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "repository_metadata": frozenset(("key", "value")),
    "strategy_configs": frozenset(
        ("version", "config_hash", "config_json", "published_at_utc", "published_at_us")
    ),
    "instrument_master_batches": frozenset(
        (
            "batch_id",
            "provider",
            "source_url",
            "content_hash",
            "schema_version",
            "fetched_at_utc",
            "fetched_at_us",
            "row_count",
            "provenance_json",
        )
    ),
    "instrument_master_records": frozenset(
        (
            "batch_id",
            "exchange",
            "segment",
            "security_id",
            "canonical_key",
            "symbol",
            "display_name",
            "instrument_type",
            "underlying_security_id",
            "expiry_utc",
            "expiry_us",
            "strike",
            "option_type",
            "lot_size",
            "tick_size",
            "record_json",
        )
    ),
    "contract_revisions": frozenset(
        (
            "contract_key",
            "contract_hash",
            "contract_json",
            "master_batch_id",
            "underlying_exchange",
            "underlying_segment",
            "underlying_security_id",
            "market_kind",
            "pricing_model",
            "option_expiry_utc",
            "option_expiry_us",
            "lot_size",
            "strike_interval",
            "tick_size",
            "futures_exchange",
            "futures_segment",
            "futures_security_id",
            "created_at_utc",
            "created_at_us",
        )
    ),
    "contract_option_mappings": frozenset(
        (
            "contract_key",
            "master_batch_id",
            "exchange",
            "segment",
            "security_id",
            "strike",
            "option_type",
            "expiry_utc",
            "lot_size",
            "tick_size",
        )
    ),
    "source_order_heads": frozenset(
        (
            "contract_key",
            "source",
            "last_sequence",
            "last_source_timestamp_utc",
            "last_source_timestamp_us",
            "snapshot_id",
        )
    ),
    "ingestion_attempts": frozenset(
        (
            "snapshot_id",
            "snapshot_hash",
            "contract_key",
            "source",
            "sequence",
            "source_timestamp_utc",
            "source_timestamp_us",
            "source_timestamp_valid",
            "received_at_utc",
            "received_at_us",
            "received_at_valid",
            "strategy_version",
            "metadata_json",
            "snapshot_json",
            "status",
            "report_hash",
            "report_json",
            "recorded_at_utc",
            "recorded_at_us",
        )
    ),
    "accepted_snapshots": frozenset(
        (
            "snapshot_id",
            "contract_key",
            "source",
            "sequence",
            "source_timestamp_utc",
            "source_timestamp_us",
        )
    ),
    "market_snapshots": frozenset(
        (
            "snapshot_id",
            "observed_at_utc",
            "observed_at_us",
            "spot_price",
            "futures_price",
            "previous_close",
            "day_open",
            "day_high",
            "day_low",
            "vwap",
            "futures_open_interest",
        )
    ),
    "technical_snapshots": frozenset(
        (
            "snapshot_id",
            "observed_at_utc",
            "observed_at_us",
            "ema_9",
            "ema_21",
            "wma_44",
            "previous_wma_44",
            "rsi_14",
            "atr_14",
            "reference_volatility",
            "timeframe",
            "completed_candle",
        )
    ),
    "strategy_context_snapshots": frozenset(
        (
            "snapshot_id",
            "operating_mode",
            "trading_style",
            "account_capital",
            "risk_per_trade",
            "maximum_premium_allocation",
            "event_risk_active",
            "price_action_confirmed",
            "signal_candle_high",
            "signal_candle_low",
            "expected_holding_hours",
        )
    ),
    "option_snapshots": frozenset(
        (
            "snapshot_id",
            "contract_key",
            "strike",
            "option_type",
            "exchange",
            "segment",
            "security_id",
            "expiry_utc",
            "expiry_us",
            "observed_at_utc",
            "observed_at_us",
            "bid",
            "ask",
            "ltp",
            "bid_quantity",
            "ask_quantity",
            "volume",
            "open_interest",
            "previous_open_interest",
            "change_open_interest",
            "change_oi_source_snapshot_id",
            "change_oi_interval_seconds",
            "implied_volatility",
            "previous_close",
            "delta",
            "gamma",
            "theta",
            "vega",
            "theoretical_price",
        )
    ),
    "validation_issues": frozenset(
        (
            "snapshot_id",
            "issue_index",
            "code",
            "severity",
            "path",
            "message",
            "observed_json",
            "expected_json",
            "issue_json",
        )
    ),
    "manual_overrides": frozenset(
        (
            "override_id",
            "base_snapshot_id",
            "field_name",
            "imported_value_json",
            "override_value_json",
            "overridden_by",
            "overridden_at_utc",
            "overridden_at_us",
            "reason",
            "expires_at_utc",
            "expires_at_us",
            "created_at_utc",
            "created_at_us",
        )
    ),
    "audit_events": frozenset(
        (
            "event_id",
            "occurred_at_utc",
            "occurred_at_us",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "payload_json",
            "previous_hash",
            "event_hash",
        )
    ),
}


class _Transaction(AbstractContextManager[sqlite3.Connection]):
    """Serialize one explicit write transaction on the repository connection."""

    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self._connection = connection
        self._lock = lock
        self._entered = False

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            # A failed BEGIN must never strand the shared repository lock.
            self._lock.release()
            raise
        self._entered = True
        return self._connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc, traceback
        try:
            if exc_type is None:
                try:
                    self._connection.execute("COMMIT")
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise
            else:
                self._connection.execute("ROLLBACK")
        finally:
            if self._entered:
                self._entered = False
                self._lock.release()
        return False


class SQLiteRepository:
    """Thread-safe repository over one shared SQLite connection."""

    def __init__(self, database: str | os.PathLike[str]) -> None:
        self._database = os.fspath(database)
        self._lock = RLock()
        self._closed = False
        in_memory = self._database == ":memory:"
        existed = False if in_memory else Path(self._database).exists()
        self._connection = sqlite3.connect(
            self._database,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if existed:
                self._verify_existing_schema()
            if not in_memory:
                self._configure_file_journal()
            if not existed:
                self._initialize_fresh_schema()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def _transaction(self) -> _Transaction:
        self._ensure_open()
        return _Transaction(self._connection, self._lock)

    def _configure_file_journal(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise PersistenceError("SQLite refused the required WAL journal mode")
        self._connection.execute("PRAGMA synchronous = NORMAL")

    def _initialize_fresh_schema(self) -> None:
        with self._transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO repository_metadata(key, value) VALUES (?, ?)",
                ("schema_fingerprint", _SCHEMA_FINGERPRINT),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _verify_existing_schema(self) -> None:
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise UnsafeSchemaError(
                    "refusing unsafe SQLite schema: "
                    f"expected fresh-only v{SCHEMA_VERSION}, found v{version}"
                )
            metadata = self._connection.execute(
                "SELECT value FROM repository_metadata WHERE key = ?",
                ("schema_fingerprint",),
            ).fetchone() if self._table_exists("repository_metadata") else None
            if metadata is None or metadata["value"] != _SCHEMA_FINGERPRINT:
                raise UnsafeSchemaError("v3 schema fingerprint is missing or incompatible")
            for table, required in _REQUIRED_COLUMNS.items():
                if not self._table_exists(table):
                    raise UnsafeSchemaError(f"v3 schema is incomplete: missing table {table!r}")
                actual = {
                    row["name"]
                    for row in self._connection.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                }
                if actual != required:
                    raise UnsafeSchemaError(
                        f"v3 schema is incompatible: columns differ for {table!r}"
                    )
            integrity = self._connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise UnsafeSchemaError(f"SQLite integrity check failed: {integrity}")
            fk_errors = self._connection.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise UnsafeSchemaError("SQLite foreign-key check failed")

    def _table_exists(self, table: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    def publish_strategy_config(
        self,
        config: StrategyConfig,
        *,
        published_at: datetime | None = None,
    ) -> str:
        config_json = canonical_json(config)
        config_hash = content_hash(config)
        when = published_at or datetime.now(UTC)
        when_text, when_us = _timestamp(when, "published_at")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT config_hash, config_json FROM strategy_configs WHERE version = ?",
                (config.version,),
            ).fetchone()
            if existing is not None:
                if existing["config_hash"] != config_hash or existing["config_json"] != config_json:
                    raise PersistenceError(
                        f"strategy version {config.version!r} is immutable and already differs"
                    )
                return config_hash
            connection.execute(
                """
                INSERT INTO strategy_configs(
                    version, config_hash, config_json, published_at_utc, published_at_us
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (config.version, config_hash, config_json, when_text, when_us),
            )
            self._append_audit(
                connection,
                event_type="STRATEGY_CONFIG_PUBLISHED",
                aggregate_type="strategy_config",
                aggregate_id=config.version,
                payload={"config_hash": config_hash},
                occurred_at=when,
            )
        return config_hash

    def record_instrument_master(
        self,
        provenance: InstrumentMasterProvenance,
        records: Iterable[InstrumentMasterRecord],
    ) -> str:
        materialized = tuple(records)
        self._validate_master(provenance, materialized)
        provenance_json = canonical_json(provenance)
        record_jsons = {
            record.instrument.canonical_key: canonical_json(record) for record in materialized
        }
        fetched_text, fetched_us = _timestamp(provenance.fetched_at, "master.fetched_at")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT provenance_json FROM instrument_master_batches WHERE batch_id = ?",
                (provenance.batch_id,),
            ).fetchone()
            if existing is not None:
                persisted = {
                    row["canonical_key"]: row["record_json"]
                    for row in connection.execute(
                        """
                        SELECT canonical_key, record_json
                        FROM instrument_master_records
                        WHERE batch_id = ?
                        """,
                        (provenance.batch_id,),
                    ).fetchall()
                }
                if existing["provenance_json"] != provenance_json or persisted != record_jsons:
                    raise InstrumentMasterError(
                        f"instrument-master batch {provenance.batch_id!r} is immutable"
                    )
                return provenance.batch_id

            connection.execute(
                """
                INSERT INTO instrument_master_batches(
                    batch_id, provider, source_url, content_hash, schema_version,
                    fetched_at_utc, fetched_at_us, row_count, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provenance.batch_id,
                    provenance.provider,
                    provenance.source_url,
                    provenance.content_hash,
                    provenance.schema_version,
                    fetched_text,
                    fetched_us,
                    provenance.row_count,
                    provenance_json,
                ),
            )
            for record in materialized:
                expiry_text, expiry_us = _optional_timestamp(
                    record.expiry, "instrument_master_record.expiry"
                )
                connection.execute(
                    """
                    INSERT INTO instrument_master_records(
                        batch_id, exchange, segment, security_id, canonical_key,
                        symbol, display_name, instrument_type, underlying_security_id,
                        expiry_utc, expiry_us, strike, option_type, lot_size, tick_size,
                        record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provenance.batch_id,
                        record.instrument.exchange.value,
                        record.instrument.segment,
                        record.instrument.security_id,
                        record.instrument.canonical_key,
                        record.instrument.symbol,
                        record.display_name,
                        record.instrument_type,
                        record.underlying_security_id,
                        expiry_text,
                        expiry_us,
                        _decimal(record.strike),
                        record.option_type.value if record.option_type is not None else None,
                        record.lot_size,
                        _decimal(record.tick_size),
                        record_jsons[record.instrument.canonical_key],
                    ),
                )
            self._append_audit(
                connection,
                event_type="INSTRUMENT_MASTER_RECORDED",
                aggregate_type="instrument_master",
                aggregate_id=provenance.batch_id,
                payload={
                    "content_hash": provenance.content_hash,
                    "row_count": provenance.row_count,
                },
                occurred_at=provenance.fetched_at,
            )
        return provenance.batch_id

    def save_ingestion(
        self,
        snapshot: AtomicSnapshot,
        report: ValidationReport,
    ) -> SnapshotStatus:
        snapshot_hash = content_hash(snapshot)
        snapshot_json = canonical_json(snapshot)
        self._verify_report_binding(snapshot, snapshot_hash, report)
        report_payload = _report_payload(report)
        report_json = canonical_json(report_payload)
        report_hash = content_hash(report_payload)
        status = SnapshotStatus.ACCEPTED if report.accepted else SnapshotStatus.REJECTED
        recorded_at = datetime.now(UTC)
        recorded_text, recorded_us = _timestamp(recorded_at, "recorded_at")

        if status is SnapshotStatus.ACCEPTED:
            if snapshot.sequence < 0:
                raise PersistenceError("accepted snapshot sequence cannot be negative")
            source_text, source_us = _timestamp(
                snapshot.source_timestamp, "source_timestamp"
            )
            received_text, received_us = _timestamp(snapshot.received_at, "received_at")
            source_valid = received_valid = 1
        else:
            # Invalid provider/receipt timestamps are evidence, not a reason to lose
            # the rejected envelope. The canonical snapshot_json retains their exact
            # original representation; sortable audit columns use a safe receipt
            # instant and explicitly mark the substitution.
            safe_receipt = _safe_audit_instant(snapshot.received_at, recorded_at)
            received_text, received_us, received_valid = _audit_timestamp(
                snapshot.received_at, fallback=safe_receipt
            )
            source_text, source_us, source_valid = _audit_timestamp(
                snapshot.source_timestamp, fallback=safe_receipt
            )

        with self._transaction() as connection:
            if status is SnapshotStatus.ACCEPTED:
                self._ensure_strategy_exists(connection, snapshot.strategy_version)
                self._ensure_contract(connection, snapshot.contract, created_at=recorded_at)
                self._enforce_order(
                    connection,
                    contract_key=snapshot.contract.contract_key,
                    source=snapshot.source,
                    sequence=snapshot.sequence,
                    source_timestamp_us=source_us,
                )
                self._verify_change_oi_provenance(connection, snapshot)
            connection.execute(
                """
                INSERT INTO ingestion_attempts(
                    snapshot_id, snapshot_hash, contract_key, source, sequence,
                    source_timestamp_utc, source_timestamp_us, source_timestamp_valid,
                    received_at_utc, received_at_us, received_at_valid, strategy_version,
                    metadata_json, snapshot_json, status, report_hash, report_json,
                    recorded_at_utc, recorded_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot_hash,
                    snapshot.contract.contract_key,
                    snapshot.source.value,
                    snapshot.sequence,
                    source_text,
                    source_us,
                    source_valid,
                    received_text,
                    received_us,
                    received_valid,
                    snapshot.strategy_version,
                    canonical_json(_indexed_metadata(snapshot.metadata)),
                    snapshot_json,
                    status.value,
                    report_hash,
                    report_json,
                    recorded_text,
                    recorded_us,
                ),
            )
            self._insert_validation_issues(connection, snapshot.snapshot_id, report.issues)
            if status is SnapshotStatus.ACCEPTED:
                self._insert_accepted_components(
                    connection,
                    snapshot,
                    source_text=source_text,
                    source_us=source_us,
                )
            if status is SnapshotStatus.ACCEPTED:
                connection.execute(
                    """
                    INSERT INTO source_order_heads(
                        contract_key, source, last_sequence,
                        last_source_timestamp_utc, last_source_timestamp_us, snapshot_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(contract_key, source) DO UPDATE SET
                        last_sequence = excluded.last_sequence,
                        last_source_timestamp_utc = excluded.last_source_timestamp_utc,
                        last_source_timestamp_us = excluded.last_source_timestamp_us,
                        snapshot_id = excluded.snapshot_id
                    """,
                    (
                        snapshot.contract.contract_key,
                        snapshot.source.value,
                        snapshot.sequence,
                        source_text,
                        source_us,
                        snapshot.snapshot_id,
                    ),
                )
            self._append_audit(
                connection,
                event_type="SNAPSHOT_INGESTED",
                aggregate_type="snapshot",
                aggregate_id=snapshot.snapshot_id,
                payload={
                    "contract_key": snapshot.contract.contract_key,
                    "snapshot_hash": snapshot_hash,
                    "status": status.value,
                },
                occurred_at=recorded_at,
            )
        return status

    def _insert_accepted_components(
        self,
        connection: sqlite3.Connection,
        snapshot: AtomicSnapshot,
        *,
        source_text: str,
        source_us: int,
    ) -> None:
        contract_key = snapshot.contract.contract_key
        connection.execute(
            """
            INSERT INTO accepted_snapshots(
                snapshot_id, contract_key, source, sequence,
                source_timestamp_utc, source_timestamp_us
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                contract_key,
                snapshot.source.value,
                snapshot.sequence,
                source_text,
                source_us,
            ),
        )

        market = snapshot.market
        market_text, market_us = _timestamp(market.observed_at, "market.observed_at")
        connection.execute(
            """
            INSERT INTO market_snapshots(
                snapshot_id, observed_at_utc, observed_at_us,
                spot_price, futures_price, previous_close, day_open,
                day_high, day_low, vwap, futures_open_interest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                market_text,
                market_us,
                _decimal(market.spot_price),
                _decimal(market.futures_price),
                _decimal(market.previous_close),
                _decimal(market.day_open),
                _decimal(market.day_high),
                _decimal(market.day_low),
                _decimal(market.vwap),
                market.futures_open_interest,
            ),
        )

        technicals = snapshot.technicals
        technical_text, technical_us = _timestamp(
            technicals.observed_at, "technicals.observed_at"
        )
        connection.execute(
            """
            INSERT INTO technical_snapshots(
                snapshot_id, observed_at_utc, observed_at_us,
                ema_9, ema_21, wma_44, previous_wma_44, rsi_14,
                atr_14, reference_volatility, timeframe, completed_candle
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                technical_text,
                technical_us,
                _decimal(technicals.ema_9),
                _decimal(technicals.ema_21),
                _decimal(technicals.wma_44),
                _decimal(technicals.previous_wma_44),
                technicals.rsi_14,
                _decimal(technicals.atr_14),
                technicals.reference_volatility,
                technicals.timeframe,
                int(technicals.completed_candle),
            ),
        )

        context = snapshot.context
        connection.execute(
            """
            INSERT INTO strategy_context_snapshots(
                snapshot_id, operating_mode, trading_style, account_capital,
                risk_per_trade, maximum_premium_allocation, event_risk_active,
                price_action_confirmed, signal_candle_high, signal_candle_low,
                expected_holding_hours
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                context.operating_mode.value,
                context.trading_style.value,
                str(context.account_capital),
                context.risk_per_trade,
                context.maximum_premium_allocation,
                _optional_bool(context.event_risk_active),
                _optional_bool(context.price_action_confirmed),
                str(context.signal_candle_high),
                str(context.signal_candle_low),
                context.expected_holding_hours,
            ),
        )

        mappings = {
            record.instrument.security_id: record
            for record in snapshot.contract.option_contracts
        }
        for quote in snapshot.option_chain:
            mapped = mappings.get(quote.security_id)
            if mapped is None:
                raise ContractIntegrityError(
                    f"option security ID {quote.security_id!r} is not in the contract revision"
                )
            if mapped.strike != quote.strike or mapped.option_type is not quote.option_type:
                raise ContractIntegrityError(
                    f"option security ID {quote.security_id!r} does not map to "
                    f"{quote.strike}/{quote.option_type.value}"
                )
            expiry_text, expiry_us = _timestamp(quote.expiry, "option.expiry")
            observed_text, observed_us = _timestamp(quote.observed_at, "option.observed_at")
            connection.execute(
                """
                INSERT INTO option_snapshots(
                    snapshot_id, contract_key, strike, option_type,
                    exchange, segment, security_id,
                    expiry_utc, expiry_us, observed_at_utc, observed_at_us,
                    bid, ask, ltp, bid_quantity, ask_quantity, volume,
                    open_interest, previous_open_interest, change_open_interest,
                    change_oi_source_snapshot_id, change_oi_interval_seconds,
                    implied_volatility, previous_close, delta, gamma, theta, vega,
                    theoretical_price
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    snapshot.snapshot_id,
                    contract_key,
                    _decimal(quote.strike),
                    quote.option_type.value,
                    mapped.instrument.exchange.value,
                    mapped.instrument.segment,
                    quote.security_id,
                    expiry_text,
                    expiry_us,
                    observed_text,
                    observed_us,
                    _decimal(quote.bid),
                    _decimal(quote.ask),
                    _decimal(quote.ltp),
                    quote.bid_quantity,
                    quote.ask_quantity,
                    quote.volume,
                    quote.open_interest,
                    quote.previous_open_interest,
                    quote.change_open_interest,
                    quote.change_oi_source_snapshot_id,
                    quote.change_oi_interval_seconds,
                    quote.implied_volatility,
                    _decimal(quote.previous_close),
                    quote.greeks.delta,
                    quote.greeks.gamma,
                    quote.greeks.theta,
                    quote.greeks.vega,
                    _decimal(quote.greeks.theoretical_price),
                ),
            )

    def _ensure_contract(
        self,
        connection: sqlite3.Connection,
        contract: ContractSpec,
        *,
        created_at: datetime,
    ) -> None:
        contract_json = canonical_json(contract)
        contract_hash = content_hash(contract)
        existing = connection.execute(
            """
            SELECT contract_hash, contract_json
            FROM contract_revisions WHERE contract_key = ?
            """,
            (contract.contract_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing["contract_hash"] != contract_hash
                or existing["contract_json"] != contract_json
            ):
                raise ContractIntegrityError(
                    f"contract revision {contract.contract_key!r} is immutable"
                )
            return

        batch = connection.execute(
            """
            SELECT provenance_json, row_count
            FROM instrument_master_batches WHERE batch_id = ?
            """,
            (contract.master.batch_id,),
        ).fetchone()
        if batch is None:
            raise InstrumentMasterError(
                f"instrument-master batch {contract.master.batch_id!r} must be recorded first"
            )
        if batch["provenance_json"] != canonical_json(contract.master):
            raise InstrumentMasterError(
                "contract provenance differs from the persisted master batch"
            )
        actual_count = connection.execute(
            "SELECT COUNT(*) FROM instrument_master_records WHERE batch_id = ?",
            (contract.master.batch_id,),
        ).fetchone()[0]
        if actual_count != batch["row_count"]:
            raise InstrumentMasterError("instrument-master batch is incomplete")

        self._require_master_instrument(connection, contract.master.batch_id, contract.underlying)
        if contract.futures is not None:
            self._require_exact_master_record(
                connection, contract.master.batch_id, contract.futures
            )
        if not contract.option_contracts:
            raise ContractIntegrityError("contract revision contains no option mappings")
        seen_keys: set[tuple[Decimal | None, OptionType | None]] = set()
        seen_security_ids: set[str] = set()
        for record in contract.option_contracts:
            if record.strike is None or record.option_type is None or record.expiry is None:
                raise ContractIntegrityError("every option mapping needs strike, side, and expiry")
            key = (record.strike, record.option_type)
            if key in seen_keys or record.instrument.security_id in seen_security_ids:
                raise ContractIntegrityError("contract option mappings must be one-to-one")
            seen_keys.add(key)
            seen_security_ids.add(record.instrument.security_id)
            if record.expiry != contract.option_expiry:
                raise ContractIntegrityError("option mapping expiry differs from contract expiry")
            if record.lot_size != contract.lot_size or record.tick_size != contract.tick_size:
                raise ContractIntegrityError(
                    "option mapping lot/tick differs from contract revision"
                )
            self._require_exact_master_record(connection, contract.master.batch_id, record)

        expiry_text, expiry_us = _timestamp(contract.option_expiry, "contract.option_expiry")
        created_text, created_us = _timestamp(created_at, "contract.created_at")
        future = contract.futures.instrument if contract.futures is not None else None
        connection.execute(
            """
            INSERT INTO contract_revisions(
                contract_key, contract_hash, contract_json, master_batch_id,
                underlying_exchange, underlying_segment, underlying_security_id,
                market_kind, pricing_model, option_expiry_utc, option_expiry_us,
                lot_size, strike_interval, tick_size,
                futures_exchange, futures_segment, futures_security_id,
                created_at_utc, created_at_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract.contract_key,
                contract_hash,
                contract_json,
                contract.master.batch_id,
                contract.underlying.exchange.value,
                contract.underlying.segment,
                contract.underlying.security_id,
                contract.market_kind.value,
                contract.pricing_model.value,
                expiry_text,
                expiry_us,
                contract.lot_size,
                str(contract.strike_interval),
                str(contract.tick_size),
                future.exchange.value if future is not None else None,
                future.segment if future is not None else None,
                future.security_id if future is not None else None,
                created_text,
                created_us,
            ),
        )
        for record in contract.option_contracts:
            if record.expiry is None or record.option_type is None:
                raise ContractIntegrityError(
                    "contract option mappings require expiry and option type"
                )
            record_expiry, _ = _timestamp(record.expiry, "contract.option.expiry")
            connection.execute(
                """
                INSERT INTO contract_option_mappings(
                    contract_key, master_batch_id, exchange, segment, security_id,
                    strike, option_type, expiry_utc, lot_size, tick_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract.contract_key,
                    contract.master.batch_id,
                    record.instrument.exchange.value,
                    record.instrument.segment,
                    record.instrument.security_id,
                    _decimal(record.strike),
                    record.option_type.value,
                    record_expiry,
                    record.lot_size,
                    _decimal(record.tick_size),
                ),
            )

    def _require_master_instrument(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        instrument: Any,
    ) -> None:
        row = connection.execute(
            """
            SELECT symbol, canonical_key FROM instrument_master_records
            WHERE batch_id = ? AND exchange = ? AND segment = ? AND security_id = ?
            """,
            (
                batch_id,
                instrument.exchange.value,
                instrument.segment,
                instrument.security_id,
            ),
        ).fetchone()
        if (
            row is None
            or row["symbol"] != instrument.symbol
            or row["canonical_key"] != instrument.canonical_key
        ):
            raise ContractIntegrityError(
                f"instrument {instrument.canonical_key!r} is not an exact master record"
            )

    def _require_exact_master_record(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        record: InstrumentMasterRecord,
    ) -> None:
        row = connection.execute(
            """
            SELECT record_json FROM instrument_master_records
            WHERE batch_id = ? AND exchange = ? AND segment = ? AND security_id = ?
            """,
            (
                batch_id,
                record.instrument.exchange.value,
                record.instrument.segment,
                record.instrument.security_id,
            ),
        ).fetchone()
        if row is None or row["record_json"] != canonical_json(record):
            raise ContractIntegrityError(
                f"contract record {record.instrument.canonical_key!r} differs from master data"
            )

    def _enforce_order(
        self,
        connection: sqlite3.Connection,
        *,
        contract_key: str,
        source: DataSource,
        sequence: int,
        source_timestamp_us: int,
    ) -> None:
        head = connection.execute(
            """
            SELECT last_sequence, last_source_timestamp_us
            FROM source_order_heads WHERE contract_key = ? AND source = ?
            """,
            (contract_key, source.value),
        ).fetchone()
        if head is None:
            return
        if sequence <= head["last_sequence"]:
            raise SnapshotOrderingError(
                f"sequence must advance beyond {head['last_sequence']}, got {sequence}"
            )
        if source_timestamp_us <= head["last_source_timestamp_us"]:
            raise SnapshotOrderingError("source timestamp must advance monotonically")

    def _verify_change_oi_provenance(
        self,
        connection: sqlite3.Connection,
        snapshot: AtomicSnapshot,
    ) -> None:
        """Recompute every non-null change OI from its exact accepted prior leg."""

        current_source_text, current_source_us = _timestamp(
            snapshot.source_timestamp, "source_timestamp"
        )
        del current_source_text
        for quote in snapshot.option_chain:
            reference_id = quote.change_oi_source_snapshot_id
            declared_interval = quote.change_oi_interval_seconds
            if quote.change_open_interest is None:
                if reference_id is not None or declared_interval is not None:
                    raise ChangeOIProvenanceError(
                        "change-OI provenance cannot exist without a derived value"
                    )
                continue
            if snapshot.source is not DataSource.DHAN_REST:
                raise ChangeOIProvenanceError(
                    "change OI may only be accepted within the Dhan REST stream"
                )
            if (
                not reference_id
                or declared_interval is None
                or not isfinite(declared_interval)
                or not 0 < declared_interval <= _MAX_CHANGE_OI_INTERVAL_SECONDS
            ):
                raise ChangeOIProvenanceError(
                    "change OI requires a bounded interval and prior snapshot ID"
                )

            expiry_text, expiry_us = _timestamp(quote.expiry, "option.expiry")
            del expiry_text
            observed_text, observed_us = _timestamp(
                quote.observed_at, "option.observed_at"
            )
            del observed_text
            prior = connection.execute(
                """
                SELECT accepted.contract_key, accepted.source, accepted.sequence,
                       accepted.source_timestamp_us,
                       options.expiry_us, options.observed_at_us,
                       options.open_interest
                FROM accepted_snapshots AS accepted
                JOIN option_snapshots AS options
                  ON options.snapshot_id = accepted.snapshot_id
                WHERE accepted.snapshot_id = ?
                  AND options.security_id = ?
                  AND options.strike = ?
                  AND options.option_type = ?
                """,
                (
                    reference_id,
                    quote.security_id,
                    _decimal(quote.strike),
                    quote.option_type.value,
                ),
            ).fetchone()
            if prior is None:
                raise ChangeOIProvenanceError(
                    "referenced accepted snapshot lacks the exact prior security/strike/side"
                )
            if (
                prior["source"] != DataSource.DHAN_REST.value
                or prior["contract_key"] != snapshot.contract.contract_key
                or prior["sequence"] >= snapshot.sequence
                or prior["expiry_us"] != expiry_us
            ):
                raise ChangeOIProvenanceError(
                    "change-OI reference differs in source, contract, sequence, or expiry"
                )
            if observed_us != current_source_us:
                raise ChangeOIProvenanceError(
                    "current option observation time must equal the Dhan source time"
                )
            source_interval = (
                current_source_us - prior["source_timestamp_us"]
            ) / 1_000_000
            leg_interval = (observed_us - prior["observed_at_us"]) / 1_000_000
            if (
                prior["observed_at_us"] != prior["source_timestamp_us"]
                or source_interval <= 0
                or leg_interval != source_interval
                or abs(declared_interval - leg_interval) > 1e-9
            ):
                raise ChangeOIProvenanceError(
                    "declared change-OI interval differs from exact source/leg timestamps"
                )
            if (
                prior["open_interest"] is None
                or quote.open_interest is None
                or quote.change_open_interest
                != quote.open_interest - prior["open_interest"]
            ):
                raise ChangeOIProvenanceError(
                    "change OI does not equal current minus exact prior open interest"
                )

    def _ensure_strategy_exists(
        self, connection: sqlite3.Connection, strategy_version: str
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM strategy_configs WHERE version = ?", (strategy_version,)
        ).fetchone() is None:
            raise PersistenceError(
                f"strategy config {strategy_version!r} must be published before ingestion"
            )

    def _insert_validation_issues(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        issues: Sequence[ValidationIssue],
    ) -> None:
        for index, issue in enumerate(issues):
            connection.execute(
                """
                INSERT INTO validation_issues(
                    snapshot_id, issue_index, code, severity, path, message,
                    observed_json, expected_json, issue_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    index,
                    issue.code,
                    issue.severity.value,
                    issue.path,
                    issue.message,
                    _optional_json(issue.observed),
                    _optional_json(issue.expected),
                    canonical_json(issue),
                ),
            )

    def _verify_report_binding(
        self,
        snapshot: AtomicSnapshot,
        snapshot_hash: str,
        report: ValidationReport,
    ) -> None:
        report_snapshot_id = getattr(report, "snapshot_id", None)
        report_snapshot_hash = getattr(report, "snapshot_hash", None)
        if report_snapshot_id is None or report_snapshot_hash is None:
            raise ValidationBindingError(
                "ValidationReport must carry snapshot_id and snapshot_hash"
            )
        if report_snapshot_id != snapshot.snapshot_id:
            raise ValidationBindingError("ValidationReport snapshot_id does not match snapshot")
        if report_snapshot_hash != snapshot_hash:
            raise ValidationBindingError("ValidationReport snapshot_hash does not match content")

    def latest_accepted_snapshot(self, contract_key: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT attempts.*
                FROM ingestion_attempts AS attempts
                JOIN accepted_snapshots AS accepted USING(snapshot_id)
                WHERE accepted.contract_key = ?
                ORDER BY accepted.source_timestamp_us DESC, accepted.sequence DESC
                LIMIT 1
                """,
                (contract_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def snapshot_context(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM strategy_context_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def accepted_option_snapshot(
        self, snapshot_id: str
    ) -> PreviousOptionSnapshot | None:
        """Load one exact accepted snapshot for validator provenance checks."""

        with self._lock:
            self._ensure_open()
            header = self._connection.execute(
                """
                SELECT snapshot_id, sequence, source, source_timestamp_us, contract_key
                FROM accepted_snapshots WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            if header is None:
                return None
            rows = self._connection.execute(
                "SELECT * FROM option_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            quotes = tuple(
                sorted(
                    (_option_from_row(row) for row in rows),
                    key=lambda quote: (quote.strike, quote.option_type.value),
                )
            )
            return PreviousOptionSnapshot(
                snapshot_id=header["snapshot_id"],
                sequence=header["sequence"],
                source=DataSource(header["source"]),
                source_timestamp=_datetime_from_us(header["source_timestamp_us"]),
                contract_key=header["contract_key"],
                option_chain=quotes,
            )

    def latest_previous_option_snapshot(
        self,
        contract_key: str,
        source: DataSource,
        *,
        before_sequence: int | None = None,
        before_source_timestamp: datetime | None = None,
    ) -> PreviousOptionSnapshot | None:
        clauses = ["accepted.contract_key = ?", "accepted.source = ?"]
        parameters: list[Any] = [contract_key, source.value]
        if before_sequence is not None:
            clauses.append("accepted.sequence < ?")
            parameters.append(before_sequence)
        if before_source_timestamp is not None:
            _, before_us = _timestamp(before_source_timestamp, "before_source_timestamp")
            clauses.append("accepted.source_timestamp_us < ?")
            parameters.append(before_us)
        with self._lock:
            self._ensure_open()
            header = self._connection.execute(
                f"""
                SELECT accepted.snapshot_id, accepted.sequence, accepted.source,
                       accepted.source_timestamp_us, accepted.contract_key
                FROM accepted_snapshots AS accepted
                WHERE {' AND '.join(clauses)}
                ORDER BY accepted.source_timestamp_us DESC, accepted.sequence DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if header is None:
                return None
            rows = self._connection.execute(
                "SELECT * FROM option_snapshots WHERE snapshot_id = ?",
                (header["snapshot_id"],),
            ).fetchall()
            quotes = tuple(sorted((_option_from_row(row) for row in rows), key=lambda quote: (
                quote.strike, quote.option_type.value
            )))
            return PreviousOptionSnapshot(
                snapshot_id=header["snapshot_id"],
                sequence=header["sequence"],
                source=DataSource(header["source"]),
                source_timestamp=_datetime_from_us(header["source_timestamp_us"]),
                contract_key=header["contract_key"],
                option_chain=quotes,
            )

    def add_manual_override(
        self,
        *,
        base_snapshot_id: str,
        override: ManualOverride,
    ) -> str:
        self._validate_manual_override(override)
        assert override.overridden_at is not None
        overridden_text, overridden_us = _timestamp(
            override.overridden_at, "override.overridden_at"
        )
        expires_text, expires_us = _optional_timestamp(
            override.expires_at, "override.expires_at"
        )
        created_at = datetime.now(UTC)
        created_text, created_us = _timestamp(created_at, "override.created_at")
        override_id = str(uuid4())
        with self._transaction() as connection:
            base = connection.execute(
                """
                SELECT attempts.snapshot_json
                FROM accepted_snapshots AS accepted
                JOIN ingestion_attempts AS attempts USING(snapshot_id)
                WHERE accepted.snapshot_id = ?
                """,
                (base_snapshot_id,),
            ).fetchone()
            if base is None:
                raise PersistenceError("manual override requires an accepted base snapshot")
            try:
                imported_value = _json_path(
                    json.loads(base["snapshot_json"]), override.field_name
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PersistenceError(
                    "manual-override field does not resolve in the accepted base snapshot"
                ) from exc
            if canonical_json(override.imported_value) != canonical_json(imported_value):
                raise PersistenceError(
                    "manual override imported_value differs from the immutable base snapshot"
                )
            connection.execute(
                """
                INSERT INTO manual_overrides(
                    override_id, base_snapshot_id, field_name,
                    imported_value_json, override_value_json, overridden_by,
                    overridden_at_utc, overridden_at_us, reason,
                    expires_at_utc, expires_at_us, created_at_utc, created_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    override_id,
                    base_snapshot_id,
                    override.field_name,
                    canonical_json(override.imported_value),
                    _optional_json(override.override_value),
                    override.overridden_by,
                    overridden_text,
                    overridden_us,
                    override.reason,
                    expires_text,
                    expires_us,
                    created_text,
                    created_us,
                ),
            )
            self._append_audit(
                connection,
                event_type="MANUAL_OVERRIDE_RECORDED",
                aggregate_type="snapshot",
                aggregate_id=base_snapshot_id,
                payload={
                    "override_id": override_id,
                    "field_name": override.field_name,
                    "active": override.is_active,
                },
                occurred_at=created_at,
            )
        return override_id

    def _validate_manual_override(self, override: ManualOverride) -> None:
        """Repository boundary checks remain effective for forged domain objects."""

        if override.field_name not in MANUAL_OVERRIDE_FIELDS:
            raise PersistenceError("unsupported manual-override field")
        if not isinstance(override.overridden_by, str) or not override.overridden_by.strip():
            raise PersistenceError("manual override requires a non-empty actor")
        if not isinstance(override.reason, str) or not override.reason.strip():
            raise PersistenceError("manual override requires a non-empty reason")
        if override.overridden_at is None:
            raise PersistenceError("manual override requires an override timestamp")
        _timestamp(override.overridden_at, "override.overridden_at")
        if override.expires_at is not None:
            _timestamp(override.expires_at, "override.expires_at")
            if not override.is_active:
                raise PersistenceError("inactive manual override cannot expire")
            if override.expires_at <= override.overridden_at:
                raise PersistenceError(
                    "manual-override expiry must follow its override timestamp"
                )

    def ingestion_attempt_count(self) -> int:
        return self._count("ingestion_attempts")

    def accepted_snapshot_count(self) -> int:
        return self._count("accepted_snapshots")

    def audit_event_count(self) -> int:
        return self._count("audit_events")

    def contract_revision_count(self) -> int:
        return self._count("contract_revisions")

    def _count(self, table: str) -> int:
        if table not in {
            "ingestion_attempts",
            "accepted_snapshots",
            "audit_events",
            "contract_revisions",
        }:
            raise ValueError("unsupported count table")
        with self._lock:
            self._ensure_open()
            return int(self._connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> None:
        occurred_text, occurred_us = _timestamp(occurred_at, "audit.occurred_at")
        previous = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous is not None else None
        payload_json = canonical_json(payload)
        event_hash = content_hash(
            {
                "occurred_at_utc": occurred_text,
                "occurred_at_us": occurred_us,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload": payload,
                "previous_hash": previous_hash,
            }
        )
        connection.execute(
            """
            INSERT INTO audit_events(
                occurred_at_utc, occurred_at_us, event_type,
                aggregate_type, aggregate_id, payload_json,
                previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_text,
                occurred_us,
                event_type,
                aggregate_type,
                aggregate_id,
                payload_json,
                previous_hash,
                event_hash,
            ),
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise PersistenceError("repository is closed")

    def _validate_master(
        self,
        provenance: InstrumentMasterProvenance,
        records: Sequence[InstrumentMasterRecord],
    ) -> None:
        if not provenance.batch_id or not provenance.provider or not provenance.content_hash:
            raise InstrumentMasterError("master provenance identity fields cannot be empty")
        _timestamp(provenance.fetched_at, "master.fetched_at")
        if provenance.row_count != len(records):
            raise InstrumentMasterError(
                "master provenance row_count must equal the complete supplied record set"
            )
        identities = [record.instrument.canonical_key for record in records]
        if len(identities) != len(set(identities)):
            raise InstrumentMasterError("master batch contains duplicate instrument identities")
        for record in records:
            if not record.instrument.security_id or not record.instrument.segment:
                raise InstrumentMasterError("master record identity fields cannot be empty")
            _optional_timestamp(record.expiry, "instrument_master_record.expiry")


def _indexed_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep raw provider evidence once, in the canonical snapshot envelope.

    ``snapshot_json`` is the immutable replay record and already contains the complete
    metadata object.  The adjacent ``metadata_json`` column is an index copy; excluding
    the potentially large raw payload prevents every option chain being stored twice
    while preserving its content hash for lookup and integrity checks.
    """

    return {
        str(key): value
        for key, value in metadata.items()
        if key != "raw_component_payloads"
    }


def _timestamp(value: datetime, path: str) -> tuple[str, int]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceError(f"{path} must be timezone-aware")
    utc_value = value.astimezone(UTC)
    delta = utc_value - _EPOCH
    epoch_us = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return utc_value.isoformat(), epoch_us


def _safe_audit_instant(value: Any, fallback: datetime) -> datetime:
    if not isinstance(value, datetime):
        return fallback
    try:
        _timestamp(value, "audit.receipt_candidate")
    except (AttributeError, OverflowError, PersistenceError, TypeError, ValueError):
        return fallback
    return value


def _audit_timestamp(value: Any, *, fallback: datetime) -> tuple[str, int, int]:
    try:
        text, epoch_us = _timestamp(value, "audit.source_timestamp")
    except (AttributeError, OverflowError, PersistenceError, TypeError, ValueError):
        text, epoch_us = _timestamp(fallback, "audit.fallback_timestamp")
        return text, epoch_us, 0
    return text, epoch_us, 1


def _optional_timestamp(value: datetime | None, path: str) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    return _timestamp(value, path)


def _datetime_from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise PersistenceError("persisted decimals must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _optional_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _optional_json(value: Any | None) -> str | None:
    return None if value is None else canonical_json(value)


def _json_path(document: Any, field_name: str) -> Any:
    current = document
    for segment in field_name.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise KeyError(field_name)
        current = current[segment]
    return current


def _report_payload(report: ValidationReport) -> dict[str, Any]:
    return {
        "snapshot_id": getattr(report, "snapshot_id", None),
        "snapshot_hash": getattr(report, "snapshot_hash", None),
        "issues": tuple(report.issues),
    }


def _option_from_row(row: sqlite3.Row) -> OptionQuote:
    return OptionQuote(
        security_id=row["security_id"],
        strike=Decimal(row["strike"]),
        option_type=OptionType(row["option_type"]),
        expiry=_datetime_from_us(row["expiry_us"]),
        bid=_optional_decimal(row["bid"]),
        ask=_optional_decimal(row["ask"]),
        ltp=_optional_decimal(row["ltp"]),
        volume=row["volume"],
        open_interest=row["open_interest"],
        previous_open_interest=row["previous_open_interest"],
        change_open_interest=row["change_open_interest"],
        implied_volatility=row["implied_volatility"],
        greeks=Greeks(
            delta=row["delta"],
            gamma=row["gamma"],
            theta=row["theta"],
            vega=row["vega"],
            theoretical_price=_optional_decimal(row["theoretical_price"]),
        ),
        observed_at=_datetime_from_us(row["observed_at_us"]),
        bid_quantity=row["bid_quantity"],
        ask_quantity=row["ask_quantity"],
        previous_close=_optional_decimal(row["previous_close"]),
        change_oi_source_snapshot_id=row["change_oi_source_snapshot_id"],
        change_oi_interval_seconds=row["change_oi_interval_seconds"],
    )


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


__all__ = [
    "SCHEMA_VERSION",
    "ChangeOIProvenanceError",
    "ContractIntegrityError",
    "InstrumentMasterError",
    "PersistenceError",
    "SQLiteRepository",
    "SnapshotOrderingError",
    "UnsafeSchemaError",
    "ValidationBindingError",
    "canonical_json",
]
