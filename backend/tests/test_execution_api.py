from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from teco_quant.api import ApiConfig, JsonWSGIApp
from teco_quant.execution.controller import ExecutionController, ExecutionPolicy
from teco_quant.execution.errors import (
    ApprovalError,
    DuplicatePositionError,
    DuplicateSignalError,
    ExecutionBlockedError,
    ExecutionUncertainError,
    KillSwitchError,
    LiveExecutionDisabledError,
    PlanValidationError,
)
from teco_quant.execution.ledger import ExecutionLedger
from teco_quant.execution.models import (
    ExecutionMode,
    LiveOrderAcknowledgement,
    OrderSide,
    OrderState,
    SignalExecutionState,
    TradePlan,
)
from teco_quant.execution.paper import PaperBroker, PaperBrokerConfig

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
REGISTRY = {
    "NIFTY-25000-CE": "101",
    "NIFTY-25100-CE": "102",
    "NIFTY-25200-CE": "103",
}


def plan(
    signal_id: str = "signal-1",
    *,
    symbol: str = "NIFTY-25000-CE",
    security_id: str = "101",
) -> TradePlan:
    return TradePlan(
        signal_id=signal_id,
        correlation_id=f"correlation-{signal_id}",
        symbol=symbol,
        security_id=security_id,
        side=OrderSide.BUY,
        quantity=10,
        limit_price=Decimal(100),
        maximum_loss=Decimal(500),
        signal_time=NOW,
        data_time=NOW,
        valid_until=NOW + timedelta(minutes=5),
        contract_expiry=NOW + timedelta(days=1),
        event_risk_active=False,
        expiry_risk_clear=True,
        strategy_version="test-strategy-v1",
    )


def controller(
    ledger: ExecutionLedger,
    mode: ExecutionMode,
    **policy_changes,
) -> ExecutionController:
    policy = ExecutionPolicy(mode=mode, **policy_changes)
    return ExecutionController(
        ledger=ledger,
        instrument_registry=REGISTRY,
        policy=policy,
        clock=lambda: NOW,
    )


class ExecutionControllerTests(unittest.TestCase):
    def test_off_data_only_and_default_live_lock_fail_closed(self) -> None:
        for mode in (ExecutionMode.OFF, ExecutionMode.DATA_ONLY):
            with self.subTest(mode=mode):
                ledger = ExecutionLedger()
                self.addCleanup(ledger.close)
                with self.assertRaises(ExecutionBlockedError):
                    controller(ledger, mode).submit(plan(), now=NOW)
                self.assertEqual(ledger.counts()["signals"], 1)
                self.assertEqual(ledger.counts()["orders"], 0)

        live_ledger = ExecutionLedger()
        self.addCleanup(live_ledger.close)
        with self.assertRaises(LiveExecutionDisabledError):
            controller(live_ledger, ExecutionMode.LIVE_AUTOMATIC).submit(plan(), now=NOW)
        self.assertEqual(live_ledger.counts()["orders"], 0)

    def test_plan_safety_gates_are_fail_closed(self) -> None:
        cases = (
            replace(plan(), data_time=NOW - timedelta(seconds=31)),
            replace(plan(), security_id="wrong"),
            replace(plan(), quantity=0),
            replace(plan(), maximum_loss=Decimal(50001)),
            replace(plan(), event_risk_active=None),
            replace(plan(), expiry_risk_clear=False),
            replace(plan(), contract_expiry=NOW + timedelta(minutes=5)),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate):
                ledger = ExecutionLedger()
                self.addCleanup(ledger.close)
                with self.assertRaises(PlanValidationError):
                    controller(ledger, ExecutionMode.PAPER_TRADING).submit(
                        candidate, now=NOW
                    )
                self.assertEqual(ledger.counts()["signals"], 0)

    def test_verified_instrument_registry_can_be_replaced_after_master_refresh(self) -> None:
        ledger = ExecutionLedger()
        self.addCleanup(ledger.close)
        service = controller(ledger, ExecutionMode.PAPER_TRADING)

        service.replace_instrument_registry({"NIFTY-26000-CE": "201"})
        result = service.submit(
            plan(symbol="NIFTY-26000-CE", security_id="201"), now=NOW
        )

        self.assertIs(result.state, SignalExecutionState.FILLED)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            service.replace_instrument_registry({"": "201"})

    def test_paper_broker_has_deterministic_slippage_and_partial_fills(self) -> None:
        ledger = ExecutionLedger()
        self.addCleanup(ledger.close)
        broker = PaperBroker(
            PaperBrokerConfig(
                slippage_bps=Decimal(10), partial_fill_ratio=Decimal("0.5")
            )
        )
        service = ExecutionController(
            ledger=ledger,
            instrument_registry=REGISTRY,
            policy=ExecutionPolicy(mode=ExecutionMode.PAPER_TRADING),
            paper_broker=broker,
            clock=lambda: NOW,
        )

        result = service.submit(plan(), now=NOW)

        self.assertIs(result.state, SignalExecutionState.PARTIALLY_FILLED)
        self.assertEqual(result.order.filled_quantity, 5)
        self.assertEqual(result.order.average_fill_price, Decimal("100.1000"))
        position = ledger.open_position_for_security("101")
        self.assertIsNotNone(position)
        self.assertEqual(position.quantity, 5)

    def test_duplicate_signal_and_duplicate_position_are_blocked(self) -> None:
        ledger = ExecutionLedger()
        self.addCleanup(ledger.close)
        service = controller(ledger, ExecutionMode.PAPER_TRADING)
        service.submit(plan(), now=NOW)

        with self.assertRaises(DuplicateSignalError):
            service.submit(plan(), now=NOW)
        with self.assertRaises(DuplicatePositionError):
            service.submit(plan("signal-2"), now=NOW)
        self.assertEqual(ledger.counts()["orders"], 1)

    def test_manual_approval_records_actor_reason_and_aware_time(self) -> None:
        ledger = ExecutionLedger()
        self.addCleanup(ledger.close)
        service = controller(ledger, ExecutionMode.MANUAL_APPROVAL)
        pending = service.submit(plan(), now=NOW)
        self.assertIs(pending.state, SignalExecutionState.AWAITING_APPROVAL)

        with self.assertRaises(ApprovalError):
            service.approve("signal-1", actor="", reason="reviewed", now=NOW)
        result = service.approve(
            "signal-1",
            actor="risk-manager",
            reason="risk and event gates checked",
            approved_at=NOW,
            now=NOW,
        )

        self.assertIs(result.state, SignalExecutionState.FILLED)
        approval = ledger.approval("signal-1")
        self.assertEqual(approval["actor"], "risk-manager")
        self.assertEqual(approval["reason"], "risk and event gates checked")
        self.assertEqual(approval["approved_at"], NOW.isoformat())
        with self.assertRaises(ApprovalError):
            service.approve(
                "signal-1", actor="risk-manager", reason="duplicate", now=NOW
            )

    def test_timeout_reconciles_by_correlation_and_never_retries_mutation(self) -> None:
        class TimedOutGateway:
            def __init__(self, acknowledgement):
                self.acknowledgement = acknowledgement
                self.submit_calls = 0
                self.lookup_calls = 0

            def submit_order(self, execution_plan, *, correlation_id):
                self.submit_calls += 1
                raise TimeoutError("ambiguous timeout")

            def lookup_by_correlation(self, correlation_id):
                self.lookup_calls += 1
                return self.acknowledgement

        acknowledgement = LiveOrderAcknowledgement(
            broker_order_id="broker-1",
            correlation_id="correlation-signal-1",
            state=OrderState.ACKNOWLEDGED,
        )
        gateway = TimedOutGateway(acknowledgement)
        ledger = ExecutionLedger()
        self.addCleanup(ledger.close)
        service = ExecutionController(
            ledger=ledger,
            instrument_registry=REGISTRY,
            policy=ExecutionPolicy(
                mode=ExecutionMode.LIVE_AUTOMATIC, live_enabled=True
            ),
            live_gateway=gateway,
            clock=lambda: NOW,
        )

        result = service.submit(plan(), now=NOW)

        self.assertIs(result.state, SignalExecutionState.SUBMITTED)
        self.assertEqual(gateway.submit_calls, 1)
        self.assertEqual(gateway.lookup_calls, 1)

        missing_gateway = TimedOutGateway(None)
        missing_ledger = ExecutionLedger()
        self.addCleanup(missing_ledger.close)
        missing_service = ExecutionController(
            ledger=missing_ledger,
            instrument_registry=REGISTRY,
            policy=ExecutionPolicy(
                mode=ExecutionMode.LIVE_AUTOMATIC, live_enabled=True
            ),
            live_gateway=missing_gateway,
            clock=lambda: NOW,
        )
        with self.assertRaises(ExecutionUncertainError):
            missing_service.submit(plan("signal-uncertain"), now=NOW)
        self.assertEqual(missing_gateway.submit_calls, 1)
        self.assertEqual(missing_gateway.lookup_calls, 1)
        self.assertEqual(missing_ledger.counts()["orders"], 1)

    def test_manual_daily_and_consecutive_loss_kill_switches(self) -> None:
        manual_ledger = ExecutionLedger()
        self.addCleanup(manual_ledger.close)
        manual = controller(manual_ledger, ExecutionMode.PAPER_TRADING)
        manual.set_kill_switch(
            active=True, actor="risk-manager", reason="manual halt", changed_at=NOW
        )
        with self.assertRaises(KillSwitchError):
            manual.submit(plan(), now=NOW)

        loss_ledger = ExecutionLedger()
        self.addCleanup(loss_ledger.close)
        loss_service = controller(
            loss_ledger,
            ExecutionMode.PAPER_TRADING,
            max_daily_loss=Decimal(100000),
            max_consecutive_losses=2,
        )
        for index, (symbol, security_id) in enumerate(
            (("NIFTY-25000-CE", "101"), ("NIFTY-25100-CE", "102")), start=1
        ):
            loss_service.submit(
                plan(f"loss-{index}", symbol=symbol, security_id=security_id), now=NOW
            )
            position = loss_ledger.open_position_for_security(security_id)
            loss_ledger.close_position(
                position.position_id, exit_price=Decimal(90), closed_at=NOW
            )
        with self.assertRaises(KillSwitchError):
            loss_service.submit(
                plan("after-losses", symbol="NIFTY-25200-CE", security_id="103"),
                now=NOW,
            )
        self.assertTrue(loss_ledger.kill_switch()["active"])
        self.assertEqual(loss_ledger.kill_switch()["reason"], "consecutive loss limit reached")

        daily_ledger = ExecutionLedger()
        self.addCleanup(daily_ledger.close)
        daily_service = controller(
            daily_ledger,
            ExecutionMode.PAPER_TRADING,
            max_daily_loss=Decimal(100),
            max_consecutive_losses=10,
        )
        daily_service.submit(plan("daily-loss"), now=NOW)
        daily_position = daily_ledger.open_position_for_security("101")
        daily_ledger.close_position(
            daily_position.position_id, exit_price=Decimal(90), closed_at=NOW
        )
        with self.assertRaises(KillSwitchError):
            daily_service.submit(
                plan("daily-blocked", symbol="NIFTY-25100-CE", security_id="102"),
                now=NOW,
            )
        self.assertEqual(daily_ledger.kill_switch()["reason"], "daily loss limit reached")

    def test_execution_ledger_reopens_without_losing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.sqlite3"
            ledger = ExecutionLedger(path)
            service = controller(ledger, ExecutionMode.PAPER_TRADING)
            service.submit(plan(), now=NOW)
            service.set_kill_switch(
                active=True,
                actor="risk-manager",
                reason="restart test",
                changed_at=NOW,
            )
            ledger.close()

            reopened = ExecutionLedger(path)
            try:
                self.assertEqual(reopened.counts()["signals"], 1)
                self.assertEqual(reopened.counts()["orders"], 1)
                self.assertIsNotNone(reopened.open_position_for_security("101"))
                self.assertTrue(reopened.kill_switch()["active"])
            finally:
                reopened.close()


class WSGIApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ExecutionLedger()
        self.addCleanup(self.ledger.close)
        self.controller = controller(self.ledger, ExecutionMode.MANUAL_APPROVAL)
        self.app = JsonWSGIApp(
            self.controller,
            ApiConfig(
                api_key="secret-key",
                allowed_origins=("https://frontend.example",),
                max_body_bytes=256,
            ),
        )

    def call(
        self,
        method: str,
        path: str,
        body: dict | bytes | None = None,
        *,
        api_key: str | None = None,
        origin: str | None = None,
        content_length: int | None = None,
    ) -> tuple[int, dict | None, dict[str, str]]:
        if isinstance(body, dict):
            payload = json.dumps(body).encode("utf-8")
        else:
            payload = body or b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(payload) if content_length is None else content_length),
            "wsgi.input": BytesIO(payload),
        }
        if api_key is not None:
            environ["HTTP_X_API_KEY"] = api_key
        if origin is not None:
            environ["HTTP_ORIGIN"] = origin
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        response = b"".join(self.app(environ, start_response))
        decoded = json.loads(response) if response else None
        return int(captured["status"].split()[0]), decoded, captured["headers"]

    def test_health_status_cors_and_consistent_not_found_error(self) -> None:
        status, payload, headers = self.call(
            "GET", "/health", origin="https://frontend.example"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["live_locked"])
        self.assertEqual(
            headers["Access-Control-Allow-Origin"], "https://frontend.example"
        )

        status, payload, _ = self.call("GET", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_mutations_require_api_key_and_enforce_body_limit(self) -> None:
        status, payload, _ = self.call(
            "POST",
            "/kill-switch",
            {"active": True, "actor": "risk", "reason": "halt"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHORIZED")

        status, payload, _ = self.call(
            "POST",
            "/kill-switch",
            b"x" * 257,
            api_key="secret-key",
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "BODY_TOO_LARGE")

    def test_manual_approval_positions_journal_and_kill_switch_endpoints(self) -> None:
        pending = self.controller.submit(plan(), now=NOW)
        self.assertIs(pending.state, SignalExecutionState.AWAITING_APPROVAL)

        status, payload, _ = self.call(
            "POST",
            "/signals/signal-1/approve",
            {
                "actor": "risk-manager",
                "reason": "reviewed",
                "approved_at": NOW.isoformat(),
            },
            api_key="secret-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "FILLED")

        status, payload, _ = self.call("GET", "/signals/latest")
        self.assertEqual(status, 200)
        self.assertEqual(payload["signal"]["signal_id"], "signal-1")
        status, payload, _ = self.call("GET", "/paper/positions")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["positions"]), 1)
        status, payload, _ = self.call("GET", "/journal/summary")
        self.assertEqual(status, 200)
        self.assertEqual(payload["orders"], 1)

        status, payload, _ = self.call(
            "POST",
            "/kill-switch",
            {"active": True, "actor": "risk-manager", "reason": "manual halt"},
            api_key="secret-key",
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["kill_switch"]["active"])


if __name__ == "__main__":
    unittest.main()
