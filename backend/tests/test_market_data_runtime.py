from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from typing import Any

from teco_quant.analytics.models import Candle
from teco_quant.brokers.dhan import DhanCredentials
from teco_quant.config import RuntimeSettings, StrategyInputSettings
from teco_quant.domain.enums import OperatingMode, TradingStyle
from teco_quant.ingestion.dhan_acquisition import ExplicitStrategyInputs
from teco_quant.ingestion.dhan_historical import CompletedTechnicalResult
from teco_quant.market_data_runtime import attach_dhan_market_data
from teco_quant.runtime import create_runtime
from tests.helpers import NOW, valid_snapshot


class _FakeAcquisition:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.stop_results: list[bool] = []
        self.health: dict[str, object] = {
            "lifecycle_state": "INITIALIZING",
            "configured": kwargs.get("credentials") is not None,
            "decision_inputs_configured": kwargs.get("context_provider") is not None,
            "subscriptions_count": 0,
            "successful_markets": 0,
            "failed_markets": 0,
            "markets": {},
        }

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, *, timeout: float = 15.0) -> bool:
        del timeout
        self.stopped += 1
        return self.stop_results.pop(0) if self.stop_results else True

    def health_snapshot(self) -> dict[str, object]:
        return dict(self.health)


class _FakeFeed:
    def __init__(self, credentials: DhanCredentials, instruments: object, **kwargs: Any):
        self.credentials = credentials
        self.instruments = tuple(instruments)  # type: ignore[arg-type]
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.stop_results: list[bool] = []
        self.replacements: list[tuple[tuple[str, str], ...]] = []

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        self.stopped += 1
        return self.stop_results.pop(0) if self.stop_results else True

    def replace_instruments(self, instruments: object) -> bool:
        replacement = tuple(instruments)  # type: ignore[arg-type]
        self.replacements.append(replacement)
        self.instruments = replacement
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "HEALTHY",
            "connected": True,
            "healthy": True,
            "expected_instruments": len(self.instruments),
            "ready_instruments": len(self.instruments),
            "packets_received": 5,
            "packet_age_seconds": 0.2,
            "missing_instruments": [],
        }


class _BlockingStartFeed(_FakeFeed):
    entered = Event()
    release = Event()

    def start(self) -> None:
        self.entered.set()
        self.release.wait(2)
        super().start()


class _FailingStartFeed(_FakeFeed):
    def start(self) -> None:
        raise RuntimeError("simulated sanitized feed-start failure")


class MarketDataRuntimeTests(unittest.TestCase):
    def _settings(self, root: Path, *, enabled: bool = True) -> RuntimeSettings:
        return RuntimeSettings(
            database_path=str(root / "market.db"),
            execution_database_path=str(root / "execution.db"),
            signal_history_database_path=str(root / "signals.db"),
            dhan_live_enabled=enabled,
        )

    def test_missing_credentials_starts_backend_but_no_network_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            acquisitions: list[_FakeAcquisition] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            with create_runtime(self._settings(Path(directory))) as runtime:
                integration = attach_dhan_market_data(
                    runtime,
                    credentials=None,
                    acquisition_factory=acquisition_factory,
                    live_feed_factory=_FakeFeed,
                )
                self.assertEqual(acquisitions[0].started, 0)
                self.assertEqual(
                    integration.health_snapshot()["state"], "CONFIG_REQUIRED"
                )
                self.assertFalse(integration.health_snapshot()["configured"])
                self.assertEqual(
                    runtime.application.feed_health, integration.health_snapshot
                )
            self.assertEqual(acquisitions[0].stopped, 1)

    def test_contract_discovery_starts_and_rotates_supervised_feed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            acquisitions: list[_FakeAcquisition] = []
            feeds: list[_FakeFeed] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            def feed_factory(*args: Any, **kwargs: Any) -> _FakeFeed:
                feed = _FakeFeed(*args, **kwargs)
                feeds.append(feed)
                return feed

            credentials = DhanCredentials("client", "secret-token")
            with create_runtime(self._settings(Path(directory))) as runtime:
                integration = attach_dhan_market_data(
                    runtime,
                    credentials=credentials,
                    acquisition_factory=acquisition_factory,
                    live_feed_factory=feed_factory,
                )
                acquisition = acquisitions[0]
                self.assertEqual(acquisition.started, 1)
                self.assertEqual(feeds, [])

                subscription_callback = acquisition.kwargs["on_subscriptions"]
                subscription_callback((("IDX_I", "13"), ("NSE_FNO", "100")))
                self.assertEqual(len(feeds), 1)
                self.assertEqual(feeds[0].started, 1)
                on_packet = feeds[0].kwargs["on_packet"]
                self.assertIs(on_packet.__self__, runtime.market_read_model)
                self.assertIs(
                    on_packet.__func__, runtime.market_read_model.publish_feed_tick.__func__
                )
                subscription_callback((("IDX_I", "13"), ("NSE_FNO", "101")))
                self.assertEqual(feeds[0].replacements[-1][-1], ("NSE_FNO", "101"))

                contract_callback = acquisition.kwargs["on_contracts"]
                contract_callback({}, {"NIFTY": {"NIFTY-CALL": "101"}})
                self.assertEqual(
                    runtime.controller._instrument_registry, {"NIFTY-CALL": "101"}
                )

                acquisition.health.update(
                    {
                        "lifecycle_state": "RUNNING",
                        "configured": True,
                        "subscriptions_count": 2,
                        "successful_markets": 1,
                        "markets": {
                            "NIFTY": {
                                "accepted": True,
                                "error_code": None,
                                "data_age_seconds": 0.0,
                            }
                        },
                    }
                )
                health = integration.health_snapshot()
            self.assertEqual(health["state"], "RUNNING")
            self.assertTrue(health["healthy"])
            self.assertFalse(health["actionable_ready"])
            self.assertEqual(health["ready_instruments"], 2)
            self.assertEqual(acquisition.stopped, 1)
            self.assertEqual(feeds[0].stopped, 1)

    def test_disabled_setting_ignores_credentials_and_stays_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            acquisitions: list[_FakeAcquisition] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            with create_runtime(
                self._settings(Path(directory), enabled=False)
            ) as runtime:
                integration = attach_dhan_market_data(
                    runtime,
                    credentials=DhanCredentials("client", "secret-token"),
                    acquisition_factory=acquisition_factory,
                    live_feed_factory=_FakeFeed,
                )
                self.assertIsNone(acquisitions[0].kwargs["credentials"])
                self.assertEqual(acquisitions[0].started, 0)
                self.assertEqual(integration.health_snapshot()["state"], "DISABLED")

    def test_complete_operator_inputs_are_injected_without_enabling_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = self._settings(Path(directory))
            settings = RuntimeSettings(
                database_path=base.database_path,
                execution_database_path=base.execution_database_path,
                signal_history_database_path=base.signal_history_database_path,
                dhan_live_enabled=True,
                strategy_inputs=StrategyInputSettings(
                    account_capital=Decimal(250000),
                    risk_per_trade=0.01,
                    maximum_premium_allocation=0.10,
                    event_risk_active=False,
                    expected_holding_hours=3.5,
                    trading_style=TradingStyle.INTRADAY,
                    operating_mode=OperatingMode.PRO,
                    price_action_confirmed=None,
                ),
            )
            acquisitions: list[_FakeAcquisition] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            with create_runtime(settings) as runtime:
                integration = attach_dhan_market_data(
                    runtime,
                    credentials=DhanCredentials("client", "secret-token"),
                    acquisition_factory=acquisition_factory,
                    live_feed_factory=_FakeFeed,
                )
                provider = acquisitions[0].kwargs["context_provider"]
                self.assertIs(provider.__self__, integration)
                configured = integration._explicit_inputs
                self.assertIsInstance(configured, ExplicitStrategyInputs)
                assert configured is not None
                self.assertEqual(configured.account_capital, Decimal(250000))
                self.assertIsNone(configured.price_action_confirmed)
                self.assertTrue(integration.health_snapshot()["decision_inputs_configured"])
                self.assertIsNone(runtime.controller._live)

                baseline = valid_snapshot()
                technicals = CompletedTechnicalResult(
                    state=baseline.technicals,
                    session_vwap=baseline.market.vwap or Decimal(1),
                    latest_candle=Candle(
                        start=NOW - timedelta(minutes=15),
                        end=NOW,
                        open=Decimal(100),
                        high=Decimal(102),
                        low=Decimal(99),
                        close=Decimal(101),
                        volume=Decimal(1000),
                        completed=True,
                    ),
                    completed_candle_count=45,
                )
                gated = provider("NIFTY", baseline.contract, technicals, NOW)
                self.assertIsNone(gated.event_risk_active)
                acquisitions[0].kwargs["on_subscriptions"]((("IDX_I", "13"),))
                live = provider("NIFTY", baseline.contract, technicals, NOW)
                self.assertFalse(live.event_risk_active)

    def test_feed_start_and_shutdown_are_serialized_without_orphan_worker(self) -> None:
        _BlockingStartFeed.entered.clear()
        _BlockingStartFeed.release.clear()
        with tempfile.TemporaryDirectory() as directory:
            runtime = create_runtime(self._settings(Path(directory)))
            acquisitions: list[_FakeAcquisition] = []
            feeds: list[_BlockingStartFeed] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            def feed_factory(*args: Any, **kwargs: Any) -> _BlockingStartFeed:
                feed = _BlockingStartFeed(*args, **kwargs)
                feeds.append(feed)
                return feed

            attach_dhan_market_data(
                runtime,
                credentials=DhanCredentials("client", "secret-token"),
                acquisition_factory=acquisition_factory,
                live_feed_factory=feed_factory,
            )
            callback = acquisitions[0].kwargs["on_subscriptions"]
            callback_thread = Thread(
                target=callback,
                args=((("IDX_I", "13"),),),
                daemon=True,
            )
            callback_thread.start()
            self.assertTrue(_BlockingStartFeed.entered.wait(1))

            close_errors: list[BaseException] = []

            def close_runtime() -> None:
                try:
                    runtime.close()
                except Exception as exc:  # noqa: BLE001 - capture background assertion data
                    close_errors.append(exc)

            close_thread = Thread(target=close_runtime, daemon=True)
            close_thread.start()
            self.assertTrue(close_thread.is_alive())
            _BlockingStartFeed.release.set()
            callback_thread.join(1)
            close_thread.join(1)

            self.assertFalse(callback_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(close_errors, [])
            self.assertEqual(feeds[0].started, 1)
            self.assertEqual(feeds[0].stopped, 1)

    def test_failed_producer_stop_keeps_dependencies_open_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = create_runtime(self._settings(Path(directory)))
            acquisitions: list[_FakeAcquisition] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisition.stop_results = [False, True]
                acquisitions.append(acquisition)
                return acquisition

            attach_dhan_market_data(
                runtime,
                credentials=DhanCredentials("client", "secret-token"),
                acquisition_factory=acquisition_factory,
                live_feed_factory=_FakeFeed,
            )
            with self.assertRaisesRegex(RuntimeError, "dependencies remain open"):
                runtime.close()
            self.assertEqual(runtime.repository.accepted_snapshot_count(), 0)
            self.assertEqual(runtime.market_read_model.markets()["markets"], [])

            runtime.close()
            self.assertEqual(acquisitions[0].stopped, 2)

    def test_failed_feed_start_is_reconciled_on_next_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = create_runtime(self._settings(Path(directory)))
            acquisitions: list[_FakeAcquisition] = []
            feeds: list[_FakeFeed] = []

            def acquisition_factory(**kwargs: Any) -> _FakeAcquisition:
                acquisition = _FakeAcquisition(**kwargs)
                acquisitions.append(acquisition)
                return acquisition

            def feed_factory(*args: Any, **kwargs: Any) -> _FakeFeed:
                feed: _FakeFeed
                if not feeds:
                    feed = _FailingStartFeed(*args, **kwargs)
                else:
                    feed = _FakeFeed(*args, **kwargs)
                feeds.append(feed)
                return feed

            attach_dhan_market_data(
                runtime,
                credentials=DhanCredentials("client", "secret-token"),
                acquisition_factory=acquisition_factory,
                live_feed_factory=feed_factory,
            )
            callback = acquisitions[0].kwargs["on_subscriptions"]
            subscriptions = (("IDX_I", "13"),)
            with self.assertRaisesRegex(RuntimeError, "feed-start"):
                callback(subscriptions)
            callback(subscriptions)
            self.assertEqual(len(feeds), 2)
            self.assertEqual(feeds[1].started, 1)
            runtime.close()

    def test_duplicate_attachment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, create_runtime(
            self._settings(Path(directory))
        ) as runtime:
            credentials = DhanCredentials("client", "secret-token")
            attach_dhan_market_data(
                runtime,
                credentials=credentials,
                acquisition_factory=_FakeAcquisition,
                live_feed_factory=_FakeFeed,
            )
            with self.assertRaisesRegex(RuntimeError, "already has"):
                attach_dhan_market_data(
                    runtime,
                    credentials=credentials,
                    acquisition_factory=_FakeAcquisition,
                    live_feed_factory=_FakeFeed,
                )


if __name__ == "__main__":
    unittest.main()
