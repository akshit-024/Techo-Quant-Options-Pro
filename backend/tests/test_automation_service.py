from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from teco_quant.api import JsonWSGIApp
from teco_quant.automation import (
    SignalHistoryRepository,
    TecoAutomationService,
    execution_registry_for_contract,
)
from teco_quant.domain.enums import DecisionState, OperatingMode
from teco_quant.execution import ExecutionController, ExecutionLedger, ExecutionPolicy
from teco_quant.execution.models import ExecutionMode, SignalExecutionState
from tests.test_signal_pipeline import accepted_report, directional_snapshots


class AutomationServiceTests(unittest.TestCase):
    def resources(self, mode: ExecutionMode):
        snapshot, previous = directional_snapshots()
        ledger = ExecutionLedger()
        history = SignalHistoryRepository()
        controller = ExecutionController(
            ledger=ledger,
            instrument_registry=execution_registry_for_contract(snapshot.contract),
            policy=ExecutionPolicy(mode=mode),
            clock=lambda: snapshot.source_timestamp,
        )
        service = TecoAutomationService(
            execution_controller=controller,
            signal_history=history,
        )
        self.addCleanup(ledger.close)
        self.addCleanup(history.close)
        return snapshot, previous, ledger, history, controller, service

    def test_actionable_signal_flows_into_paper_ledger_and_signal_history(self) -> None:
        snapshot, previous, ledger, history, _, service = self.resources(
            ExecutionMode.PAPER_TRADING
        )

        result = service.process(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=snapshot.source_timestamp,
        )

        self.assertIs(result.signal.decision, DecisionState.BUY_CALL)
        self.assertIs(result.execution.state, SignalExecutionState.FILLED)
        self.assertEqual(history.count(), 1)
        self.assertEqual(history.latest()["decision"], "BUY_CALL")
        self.assertEqual(ledger.counts()["orders"], 1)
        self.assertEqual(len(ledger.positions()), 1)

    def test_data_only_records_signal_but_never_creates_an_order(self) -> None:
        snapshot, previous, ledger, history, _, service = self.resources(
            ExecutionMode.DATA_ONLY
        )

        result = service.process(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=snapshot.source_timestamp,
        )

        self.assertIsNone(result.execution)
        self.assertIn("DATA_ONLY", result.execution_blocked_reason)
        self.assertEqual(history.count(), 1)
        self.assertEqual(ledger.counts()["orders"], 0)

    def test_advisory_wait_is_historicized_without_entering_execution(self) -> None:
        snapshot, previous, ledger, history, _, service = self.resources(
            ExecutionMode.PAPER_TRADING
        )
        snapshot, previous = directional_snapshots(mode=OperatingMode.QUICK)

        result = service.process(
            snapshot,
            accepted_report(snapshot),
            previous_snapshot=previous,
            now=snapshot.source_timestamp,
        )

        self.assertIs(result.signal.decision, DecisionState.WAIT)
        self.assertIsNone(result.execution)
        self.assertEqual(history.latest()["decision"], "WAIT")
        self.assertEqual(ledger.counts()["signals"], 0)

    def test_signal_history_reopens_and_can_back_latest_api_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.sqlite3"
            snapshot, previous = directional_snapshots()
            first = SignalHistoryRepository(path)
            ledger = ExecutionLedger()
            self.addCleanup(ledger.close)
            controller = ExecutionController(
                ledger=ledger,
                instrument_registry=execution_registry_for_contract(snapshot.contract),
                policy=ExecutionPolicy(mode=ExecutionMode.DATA_ONLY),
                clock=lambda: snapshot.source_timestamp,
            )
            service = TecoAutomationService(
                execution_controller=controller, signal_history=first
            )
            service.process(
                snapshot,
                accepted_report(snapshot),
                previous_snapshot=previous,
                now=snapshot.source_timestamp,
            )
            first.close()

            reopened = SignalHistoryRepository(path)
            try:
                app = JsonWSGIApp(controller, signal_history=reopened)
                captured = {}

                def start_response(status, headers):
                    captured["status"] = status

                body = b"".join(
                    app(
                        {
                            "REQUEST_METHOD": "GET",
                            "PATH_INFO": "/signals/latest",
                            "wsgi.input": BytesIO(b""),
                            "CONTENT_LENGTH": "0",
                        },
                        start_response,
                    )
                )
                payload = json.loads(body)
                self.assertTrue(captured["status"].startswith("200"))
                self.assertEqual(payload["signal"]["decision"], "BUY_CALL")
                self.assertEqual(reopened.count(), 1)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
