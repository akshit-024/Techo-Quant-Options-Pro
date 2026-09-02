"""Restart-safe SQLite ledger isolated from Sprint 1 market-data persistence."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any

from teco_quant.execution.errors import ApprovalError, DuplicateSignalError, ExecutionError
from teco_quant.execution.models import (
    Approval,
    BrokerFill,
    BrokerOrder,
    ExecutablePlan,
    OrderSide,
    Position,
    PositionState,
    SignalExecutionState,
    TradePlan,
    plan_from_dict,
    plan_to_dict,
)

_FINGERPRINT = "teco-execution-ledger-v1-a"


class ExecutionLedger:
    def __init__(self, database: str | os.PathLike[str] = ":memory:") -> None:
        self.database = os.fspath(database)
        self._lock = RLock()
        existed = self.database != ":memory:" and Path(self.database).exists()
        self._connection = sqlite3.connect(
            self.database, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        if existed:
            self._verify_schema()
        else:
            self._create_schema()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _create_schema(self) -> None:
        statements = (
            "CREATE TABLE execution_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """
            CREATE TABLE signals(
                signal_id TEXT PRIMARY KEY,
                correlation_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'RECEIVED','BLOCKED','AWAITING_APPROVAL','SUBMITTED',
                    'PARTIALLY_FILLED','FILLED','UNCERTAIN'
                )),
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE approvals(
                signal_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
                reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
                approved_at TEXT NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(signal_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE orders(
                order_id TEXT PRIMARY KEY,
                correlation_id TEXT NOT NULL UNIQUE,
                signal_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                filled_quantity INTEGER NOT NULL CHECK(
                    filled_quantity >= 0 AND filled_quantity <= quantity
                ),
                limit_price TEXT NOT NULL CHECK(CAST(limit_price AS REAL) > 0),
                average_fill_price TEXT CHECK(
                    average_fill_price IS NULL OR CAST(average_fill_price AS REAL) > 0
                ),
                state TEXT NOT NULL CHECK(state IN (
                    'PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED',
                    'REJECTED','CANCELLED','UNKNOWN'
                )),
                submitted_at TEXT NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(signal_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE fills(
                fill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price TEXT NOT NULL CHECK(CAST(price AS REAL) > 0),
                state TEXT NOT NULL CHECK(state IN ('PARTIAL','COMPLETE')),
                filled_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE positions(
                position_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                security_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                average_entry_price TEXT NOT NULL CHECK(
                    CAST(average_entry_price AS REAL) > 0
                ),
                state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                closed_day TEXT,
                realized_pnl TEXT,
                FOREIGN KEY(signal_id) REFERENCES signals(signal_id) ON DELETE RESTRICT
            )
            """,
            """
            CREATE UNIQUE INDEX one_open_position_per_security
            ON positions(security_id) WHERE state = 'OPEN'
            """,
            """
            CREATE TABLE journal(
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT,
                entry_type TEXT NOT NULL,
                amount TEXT,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE execution_events(
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE kill_switch(
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                reason TEXT,
                actor TEXT,
                changed_at TEXT NOT NULL
            )
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO execution_metadata(key, value) VALUES('fingerprint', ?)",
                (_FINGERPRINT,),
            )
            connection.execute(
                """
                INSERT INTO kill_switch(singleton, active, changed_at)
                VALUES(1, 0, ?)
                """,
                (_timestamp(datetime.now(UTC)),),
            )

    def _verify_schema(self) -> None:
        try:
            row = self._connection.execute(
                "SELECT value FROM execution_metadata WHERE key='fingerprint'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ExecutionError("unsupported execution ledger schema") from exc
        if row is None or row["value"] != _FINGERPRINT:
            raise ExecutionError("unsupported execution ledger schema")
        if self._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExecutionError("execution ledger integrity check failed")

    def record_signal(
        self,
        plan: ExecutablePlan,
        state: SignalExecutionState,
        *,
        recorded_at: datetime,
    ) -> None:
        when = _timestamp(recorded_at)
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO signals(
                        signal_id, correlation_id, symbol, security_id,
                        plan_json, state, received_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.signal_id,
                        plan.correlation_id,
                        plan.symbol,
                        plan.security_id,
                        _json(plan_to_dict(plan)),
                        state.value,
                        when,
                        when,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateSignalError(
                    "signal_id or correlation_id has already been recorded"
                ) from exc
            self._event(
                connection,
                "SIGNAL_RECORDED",
                plan.signal_id,
                recorded_at,
                {"state": state.value},
            )

    def signal_exists(self, signal_id: str) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone() is not None

    def update_signal_state(
        self, signal_id: str, state: SignalExecutionState, *, now: datetime
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE signals SET state = ?, updated_at = ? WHERE signal_id = ?",
                (state.value, _timestamp(now), signal_id),
            )
            if cursor.rowcount != 1:
                raise ExecutionError("signal does not exist")
            self._event(
                connection,
                "SIGNAL_STATE_CHANGED",
                signal_id,
                now,
                {"state": state.value},
            )

    def plan(self, signal_id: str) -> TradePlan | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT plan_json FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return None if row is None else plan_from_dict(json.loads(row["plan_json"]))

    def signal_state(self, signal_id: str) -> SignalExecutionState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return None if row is None else SignalExecutionState(row["state"])

    def latest_signal(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM signals ORDER BY received_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def record_approval(self, approval: Approval) -> None:
        actor = approval.actor.strip()
        reason = approval.reason.strip()
        if not actor or not reason:
            raise ApprovalError("approval requires actor and reason")
        when = _timestamp(approval.approved_at)
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO approvals(signal_id, actor, reason, approved_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (approval.signal_id, actor, reason, when),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalError("signal is missing or already approved") from exc
            self._event(
                connection,
                "MANUAL_APPROVAL_RECORDED",
                approval.signal_id,
                approval.approved_at,
                {"actor": actor, "reason": reason},
            )

    def approval(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM approvals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def record_execution(
        self,
        order: BrokerOrder,
        fills: tuple[BrokerFill, ...],
        *,
        now: datetime,
    ) -> Position | None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO orders(
                    order_id, correlation_id, signal_id, symbol, security_id, side,
                    quantity, filled_quantity, limit_price, average_fill_price,
                    state, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.correlation_id,
                    order.signal_id,
                    order.symbol,
                    order.security_id,
                    order.side.value,
                    order.quantity,
                    order.filled_quantity,
                    _decimal(order.limit_price),
                    _optional_decimal(order.average_fill_price),
                    order.state.value,
                    _timestamp(order.submitted_at),
                ),
            )
            for fill in fills:
                connection.execute(
                    """
                    INSERT INTO fills(fill_id, order_id, quantity, price, state, filled_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.fill_id,
                        fill.order_id,
                        fill.quantity,
                        _decimal(fill.price),
                        fill.state.value,
                        _timestamp(fill.filled_at),
                    ),
                )
            position: Position | None = None
            if order.filled_quantity > 0 and order.average_fill_price is not None:
                position = Position(
                    position_id=f"position-{order.order_id}",
                    signal_id=order.signal_id,
                    symbol=order.symbol,
                    security_id=order.security_id,
                    side=order.side,
                    quantity=order.filled_quantity,
                    average_entry_price=order.average_fill_price,
                    state=PositionState.OPEN,
                    opened_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO positions(
                        position_id, signal_id, symbol, security_id, side, quantity,
                        average_entry_price, state, opened_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position.position_id,
                        position.signal_id,
                        position.symbol,
                        position.security_id,
                        position.side.value,
                        position.quantity,
                        _decimal(position.average_entry_price),
                        position.state.value,
                        _timestamp(position.opened_at),
                    ),
                )
            state = (
                SignalExecutionState.FILLED
                if order.filled_quantity == order.quantity
                else SignalExecutionState.PARTIALLY_FILLED
                if order.filled_quantity
                else SignalExecutionState.SUBMITTED
            )
            connection.execute(
                "UPDATE signals SET state = ?, updated_at = ? WHERE signal_id = ?",
                (state.value, _timestamp(now), order.signal_id),
            )
            connection.execute(
                """
                INSERT INTO journal(signal_id, entry_type, occurred_at, details_json)
                VALUES (?, 'ORDER_RECORDED', ?, ?)
                """,
                (
                    order.signal_id,
                    _timestamp(now),
                    _json({"order_id": order.order_id, "state": order.state.value}),
                ),
            )
            self._event(
                connection,
                "ORDER_RECORDED",
                order.order_id,
                now,
                {"signal_id": order.signal_id, "state": order.state.value},
            )
            return position

    def open_position_for_security(self, security_id: str) -> Position | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM positions WHERE security_id = ? AND state = 'OPEN'",
                (security_id,),
            ).fetchone()
        return None if row is None else _position(row)

    def positions(self, *, open_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM positions"
        parameters: tuple[Any, ...] = ()
        if open_only:
            sql += " WHERE state = ?"
            parameters = (PositionState.OPEN.value,)
        sql += " ORDER BY opened_at, position_id"
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, parameters).fetchall()]

    def close_position(
        self, position_id: str, *, exit_price: Decimal, closed_at: datetime
    ) -> Decimal:
        if not exit_price.is_finite() or exit_price <= 0:
            raise ExecutionError("exit price must be positive and finite")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM positions WHERE position_id = ? AND state = 'OPEN'",
                (position_id,),
            ).fetchone()
            if row is None:
                raise ExecutionError("open position does not exist")
            entry = Decimal(row["average_entry_price"])
            direction = Decimal(1) if row["side"] == OrderSide.BUY.value else Decimal(-1)
            pnl = (exit_price - entry) * Decimal(row["quantity"]) * direction
            instant = _timestamp(closed_at)
            connection.execute(
                """
                UPDATE positions
                SET state='CLOSED', closed_at=?, closed_day=?, realized_pnl=?
                WHERE position_id=?
                """,
                (instant, closed_at.astimezone(UTC).date().isoformat(), _decimal(pnl), position_id),
            )
            connection.execute(
                """
                INSERT INTO journal(signal_id, entry_type, amount, occurred_at, details_json)
                VALUES (?, 'POSITION_CLOSED', ?, ?, ?)
                """,
                (
                    row["signal_id"],
                    _decimal(pnl),
                    instant,
                    _json({"position_id": position_id, "exit_price": str(exit_price)}),
                ),
            )
            self._event(
                connection,
                "POSITION_CLOSED",
                position_id,
                closed_at,
                {"realized_pnl": str(pnl)},
            )
            return pnl

    def daily_realized_pnl(self, day: date) -> Decimal:
        with self._lock:
            rows = self._connection.execute(
                "SELECT realized_pnl FROM positions WHERE state='CLOSED' AND closed_day=?",
                (day.isoformat(),),
            ).fetchall()
        return sum((Decimal(row["realized_pnl"]) for row in rows), Decimal(0))

    def consecutive_losses(self) -> int:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT realized_pnl FROM positions
                WHERE state='CLOSED' ORDER BY closed_at DESC, rowid DESC
                """
            ).fetchall()
        count = 0
        for row in rows:
            if Decimal(row["realized_pnl"]) < 0:
                count += 1
            else:
                break
        return count

    def set_kill_switch(
        self,
        *,
        active: bool,
        reason: str,
        actor: str,
        changed_at: datetime,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise ExecutionError("kill-switch change requires actor and reason")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE kill_switch SET active=?, reason=?, actor=?, changed_at=?
                WHERE singleton=1
                """,
                (int(active), reason.strip(), actor.strip(), _timestamp(changed_at)),
            )
            self._event(
                connection,
                "KILL_SWITCH_CHANGED",
                "global",
                changed_at,
                {"active": active, "reason": reason.strip(), "actor": actor.strip()},
            )

    def kill_switch(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT active, reason, actor, changed_at FROM kill_switch WHERE singleton=1"
            ).fetchone()
        result = dict(row)
        result["active"] = bool(result["active"])
        return result

    def journal_summary(self) -> dict[str, Any]:
        with self._lock:
            journal_count = self._connection.execute(
                "SELECT COUNT(*) FROM journal"
            ).fetchone()[0]
            realized = self._connection.execute(
                "SELECT realized_pnl FROM positions WHERE state='CLOSED'"
            ).fetchall()
            order_count = self._connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        total = sum((Decimal(row["realized_pnl"]) for row in realized), Decimal(0))
        return {
            "journal_entries": int(journal_count),
            "orders": int(order_count),
            "closed_positions": len(realized),
            "realized_pnl": str(total),
        }

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table: int(
                    self._connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                for table in ("signals", "approvals", "orders", "fills", "positions")
            }

    def _event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        aggregate_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_events(event_type, aggregate_id, occurred_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, aggregate_id, _timestamp(occurred_at), _json(payload)),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionError("execution timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ExecutionError("execution decimals must be finite")
    return format(value, "f")


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else _decimal(value)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _position(row: sqlite3.Row) -> Position:
    return Position(
        position_id=row["position_id"],
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        security_id=row["security_id"],
        side=OrderSide(row["side"]),
        quantity=row["quantity"],
        average_entry_price=Decimal(row["average_entry_price"]),
        state=PositionState(row["state"]),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        closed_at=(datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None),
        realized_pnl=(Decimal(row["realized_pnl"]) if row["realized_pnl"] else None),
    )
