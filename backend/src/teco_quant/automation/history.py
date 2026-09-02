"""Restart-safe history for every signal evaluation, including WAIT and NO_TRADE."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC
from threading import RLock

from teco_quant.serialization import canonical_json, content_hash
from teco_quant.signals.models import SignalPipelineResult


class SignalHistoryRepository:
    def __init__(self, database: str | os.PathLike[str] = ":memory:") -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(
            os.fspath(database), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                generated_at_utc TEXT NOT NULL,
                generated_at_us INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL,
                call_score REAL NOT NULL,
                put_score REAL NOT NULL,
                score_gap REAL NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS signal_evaluations_latest
            ON signal_evaluations(generated_at_us DESC, evaluation_id DESC)
            """
        )

    def record(self, result: SignalPipelineResult) -> str:
        generated = result.generated_at
        if generated.tzinfo is None or generated.utcoffset() is None:
            raise ValueError("signal history requires a timezone-aware generated time")
        payload = canonical_json(result)
        evaluation_id = f"evaluation:{content_hash(result)}"
        utc = generated.astimezone(UTC)
        epoch_us = int(utc.timestamp() * 1_000_000)
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO signal_evaluations(
                    evaluation_id, snapshot_id, generated_at_utc, generated_at_us,
                    decision, reason, call_score, put_score, score_gap, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    result.snapshot_id,
                    utc.isoformat(),
                    epoch_us,
                    result.decision.value,
                    result.reason,
                    result.call_score,
                    result.put_score,
                    result.score_gap,
                    payload,
                ),
            )
        return evaluation_id

    def latest(self) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT evaluation_id, result_json
                FROM signal_evaluations
                ORDER BY generated_at_us DESC, evaluation_id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        decoded: object = json.loads(row["result_json"])
        if not isinstance(decoded, dict):
            raise TypeError("persisted signal result must be a JSON object")
        value: dict[str, object] = {}
        for key, item in decoded.items():
            if not isinstance(key, str):
                raise TypeError("persisted signal result keys must be strings")
            value[key] = item
        evaluation_id = row["evaluation_id"]
        if not isinstance(evaluation_id, str):
            raise TypeError("persisted evaluation ID must be a string")
        value["evaluation_id"] = evaluation_id
        return value

    def count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM signal_evaluations"
                ).fetchone()[0]
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
