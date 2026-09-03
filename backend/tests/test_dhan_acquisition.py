from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event

from teco_quant.api.market_read_model import MarketReadModelStore
from teco_quant.domain.enums import DataSource
from teco_quant.ingestion.dhan_acquisition import DhanAcquisitionService
from teco_quant.ingestion.dhan_historical import completed_15m_boundary
from teco_quant.ingestion.normalization import NormalizationError
from teco_quant.ingestion.service import SnapshotIngestionService
from teco_quant.ingestion.validation import SnapshotValidator
from teco_quant.persistence.sqlite import SQLiteRepository
from teco_quant.strategy.spec import DEFAULT_STRATEGY_CONFIG

NOW = datetime(2026, 8, 25, 6, 30, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
FIXTURE = Path(__file__).parent / "fixtures" / "dhan_master_nifty.csv"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeDhanClient:
    def __init__(
        self,
        clock: MutableClock,
        *,
        quote_delay: float = 0,
        chain_delay: float = 0,
    ) -> None:
        self.clock = clock
        self.quote_delay = quote_delay
        self.chain_delay = chain_delay
        self.master_calls = 0
        self.expiry_calls = 0
        self.quote_calls = 0
        self.history_calls = 0
        self.chain_calls = 0
        self.close_calls = 0

    def instrument_master(self, *, detailed=True):
        self.master_calls += 1
        return FIXTURE.read_text(encoding="utf-8")

    def expiry_list(self, *, underlying_security_id, underlying_segment):
        self.expiry_calls += 1
        return ["2026-08-27", "2026-09-03"]

    def market_quote(self, instruments_by_segment):
        self.quote_calls += 1
        self.clock.advance(self.quote_delay)
        data = {}
        for segment, security_ids in instruments_by_segment.items():
            segment_data = {}
            for security_id in security_ids:
                selected = str(security_id)
                future = selected == "50001"
                price = 24835 if future else 24800
                segment_data[selected] = {
                    "last_price": price,
                    "ohlc": {
                        "open": price - 50,
                        "close": price - 100,
                        "high": price + 100,
                        "low": price - 150,
                    },
                    "volume": 100_000,
                    "oi": 250_000 if future else 0,
                    "depth": {
                        "buy": [{"price": price - 1}],
                        "sell": [{"price": price + 1}],
                    },
                }
            data[segment] = segment_data
        return {"status": "success", "data": data}

    def intraday_candles(
        self,
        *,
        security_id,
        exchange_segment,
        instrument,
        interval,
        from_datetime,
        to_datetime,
        include_open_interest=False,
    ):
        self.history_calls += 1
        return _historical_payload(to_datetime)

    def option_chain(self, *, underlying_security_id, underlying_segment, expiry):
        self.chain_calls += 1
        self.clock.advance(self.chain_delay)
        return _chain_payload(self.chain_calls)

    def close(self):
        self.close_calls += 1


class BlockingMasterClient(FakeDhanClient):
    def __init__(self, clock: MutableClock) -> None:
        super().__init__(clock)
        self.entered = Event()
        self.release = Event()

    def instrument_master(self, *, detailed=True):
        self.entered.set()
        self.release.wait(5)
        raise RuntimeError("blocked master released")


class PartialExpiryClient(FakeDhanClient):
    def instrument_master(self, *, detailed=True):
        self.master_calls += 1
        return FIXTURE.read_text(encoding="utf-8") + _tcs_master_rows()

    def expiry_list(self, *, underlying_security_id, underlying_segment):
        self.expiry_calls += 1
        if int(underlying_security_id) == 11536:
            raise RuntimeError("simulated TCS expiry failure")
        return ["2026-08-27"]


class PartialAfterBaselineClient(FakeDhanClient):
    def instrument_master(self, *, detailed=True):
        self.master_calls += 1
        master = FIXTURE.read_text(encoding="utf-8") + _tcs_master_rows()
        if self.master_calls > 1:
            return _renumber_nifty_options(master)
        return master

    def expiry_list(self, *, underlying_security_id, underlying_segment):
        self.expiry_calls += 1
        if self.master_calls > 1 and int(underlying_security_id) == 11536:
            raise RuntimeError("simulated TCS expiry failure after complete baseline")
        return ["2026-08-27"]


class LaggingHistoryClient(FakeDhanClient):
    def __init__(self, clock: MutableClock, lag: timedelta) -> None:
        super().__init__(clock)
        self.lag = lag

    def intraday_candles(
        self,
        *,
        security_id,
        exchange_segment,
        instrument,
        interval,
        from_datetime,
        to_datetime,
        include_open_interest=False,
    ):
        self.history_calls += 1
        return _historical_payload(to_datetime - self.lag)


class RejectedSnapshotClient(FakeDhanClient):
    def option_chain(self, *, underlying_security_id, underlying_segment, expiry):
        payload = super().option_chain(
            underlying_security_id=underlying_security_id,
            underlying_segment=underlying_segment,
            expiry=expiry,
        )
        del payload["data"]["oc"]["24800"]["pe"]
        return payload


class ProviderTimestampClient(FakeDhanClient):
    def __init__(self, clock: MutableClock, *, trade_lag: timedelta) -> None:
        super().__init__(clock)
        self.trade_lag = trade_lag

    def market_quote(self, instruments_by_segment):
        payload = super().market_quote(instruments_by_segment)
        trade_time = (self.clock.value - self.trade_lag).astimezone(IST).strftime(
            "%d/%m/%Y %H:%M:%S"
        )
        for segment_data in payload["data"].values():
            for quote in segment_data.values():
                quote["last_trade_time"] = trade_time
        return payload


class DhanAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock(NOW)
        self.repository = SQLiteRepository(":memory:")
        self.repository.publish_strategy_config(DEFAULT_STRATEGY_CONFIG, published_at=NOW)
        validator = SnapshotValidator(
            clock=self.clock,
            change_oi_reference_loader=self.repository.accepted_option_snapshot,
        )
        self.ingestion = SnapshotIngestionService(
            validator=validator,
            repository=self.repository,
        )
        self.read_models = MarketReadModelStore(clock=self.clock)

    def tearDown(self) -> None:
        self.read_models.close()
        self.repository.close()

    def service(self, client) -> DhanAcquisitionService:
        return DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY",),
            clock=self.clock,
            close_client_on_stop=False,
        )

    def test_two_cycles_seed_then_compute_change_oi_without_actionable_defaults(self) -> None:
        client = FakeDhanClient(self.clock)
        subscriptions = []
        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY",),
            clock=self.clock,
            on_subscriptions=subscriptions.append,
            close_client_on_stop=False,
        )

        first = service.run_once(now=self.clock.value)
        self.clock.advance(10)
        second = service.run_once(now=self.clock.value)

        self.assertTrue(first.markets[0].accepted)
        self.assertTrue(second.markets[0].accepted)
        self.assertEqual(self.repository.accepted_snapshot_count(), 2)
        previous = self.repository.latest_previous_option_snapshot(
            second.markets[0].contract_key or "",
            DataSource.DHAN_REST,
        )
        self.assertIsNotNone(previous)
        assert previous is not None
        self.assertTrue(all(quote.change_open_interest == 100 for quote in previous.option_chain))
        self.assertTrue(
            all(quote.change_oi_source_snapshot_id == first.markets[0].snapshot_id for quote in previous.option_chain)
        )
        workspace = self.read_models.workspace("NIFTY", "NIFTY", now=self.clock.value)
        self.assertIsNotNone(workspace)
        assert workspace is not None
        self.assertFalse(workspace["read_model"]["actionable"])
        self.assertIn("EVENT_RISK", str(workspace))
        self.assertFalse(service.health_snapshot()["decision_inputs_configured"])
        self.assertEqual(client.master_calls, 1)
        self.assertEqual(client.expiry_calls, 1)
        self.assertEqual(client.history_calls, 1)
        self.assertEqual(client.chain_calls, 2)
        self.assertEqual(len(subscriptions), 2)
        self.assertEqual(len(subscriptions[0]), 12)
        self.assertEqual(subscriptions[0], subscriptions[1])

    def test_subscription_callback_failure_is_retried_until_success(self) -> None:
        client = FakeDhanClient(self.clock)
        deliveries = []

        def flaky_callback(subscriptions):
            deliveries.append(subscriptions)
            if len(deliveries) == 1:
                raise RuntimeError("simulated feed reconciliation failure")

        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY",),
            clock=self.clock,
            on_subscriptions=flaky_callback,
            close_client_on_stop=False,
        )

        service.run_once(now=self.clock.value)
        first_health = service.health_snapshot()
        self.clock.advance(10)
        service.run_once(now=self.clock.value)
        second_health = service.health_snapshot()

        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0], deliveries[1])
        self.assertEqual(first_health["callback_error_code"], "RUNTIMEERROR")
        self.assertEqual(first_health["lifecycle_state"], "PARTIAL")
        self.assertIsNone(second_health["callback_error_code"])
        self.assertEqual(second_health["lifecycle_state"], "RUNNING")

    def test_initial_failed_resolution_does_not_deliver_empty_callbacks(self) -> None:
        client = PartialExpiryClient(self.clock)
        subscription_deliveries = []
        contract_deliveries = []
        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("TCS",),
            clock=self.clock,
            on_subscriptions=subscription_deliveries.append,
            on_contracts=lambda contracts, registries: contract_deliveries.append(
                (contracts, registries)
            ),
            close_client_on_stop=False,
        )

        result = service.run_once(now=self.clock.value)

        self.assertFalse(result.markets[0].success)
        self.assertEqual(subscription_deliveries, [])
        self.assertEqual(contract_deliveries, [])
        self.assertIsNone(service.health_snapshot()["callback_error_code"])

    def test_partial_resolution_keeps_exact_last_complete_subscription_generation(self) -> None:
        client = PartialAfterBaselineClient(self.clock)
        deliveries = []
        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY", "TCS"),
            clock=self.clock,
            on_subscriptions=deliveries.append,
            close_client_on_stop=False,
        )

        service.run_once(now=self.clock.value)
        complete_generation = service.subscriptions_snapshot()
        self.clock.advance(24 * 60 * 60)
        service.run_once(now=self.clock.value)

        self.assertEqual(len(complete_generation), 24)
        self.assertEqual(service.subscriptions_snapshot(), complete_generation)
        self.assertEqual(deliveries, [complete_generation, complete_generation])
        self.assertTrue(
            {str(value) for value in range(30_000, 30_010)}.isdisjoint(
                security_id for _, security_id in service.subscriptions_snapshot()
            )
        )

    def test_slow_quote_chain_cycle_is_not_published_as_fresh(self) -> None:
        client = FakeDhanClient(self.clock, quote_delay=10, chain_delay=10)
        service = self.service(client)

        result = service.run_once(now=self.clock.value)

        self.assertFalse(result.markets[0].success)
        self.assertEqual(result.markets[0].error_code, "VALUEERROR")
        self.assertEqual(self.repository.ingestion_attempt_count(), 0)
        self.assertEqual(self.read_models.markets(now=self.clock.value)["markets"], [])
        self.assertEqual(client.quote_calls, 2)

    def test_master_expiry_and_indicator_caches_refresh_on_their_boundaries(self) -> None:
        client = FakeDhanClient(self.clock)
        service = self.service(client)

        self.assertTrue(service.run_once(now=self.clock.value).markets[0].success)
        self.clock.advance(10)
        self.assertTrue(service.run_once(now=self.clock.value).markets[0].success)
        self.assertEqual((client.master_calls, client.expiry_calls, client.history_calls), (1, 1, 1))

        self.clock.advance(15 * 60)
        self.assertTrue(service.run_once(now=self.clock.value).markets[0].success)
        self.assertEqual((client.master_calls, client.expiry_calls, client.history_calls), (1, 1, 2))

        self.clock.advance(24 * 60 * 60)
        self.assertTrue(service.run_once(now=self.clock.value).markets[0].success)
        self.assertEqual((client.master_calls, client.expiry_calls, client.history_calls), (2, 2, 3))

    def test_six_hour_old_history_fails_before_quotes_or_cache(self) -> None:
        client = LaggingHistoryClient(self.clock, timedelta(hours=6))
        service = self.service(client)

        result = service.run_once(now=self.clock.value)

        self.assertFalse(result.markets[0].success)
        self.assertEqual(result.markets[0].error_code, "NORMALIZATIONERROR")
        self.assertEqual(client.history_calls, 1)
        self.assertEqual(client.quote_calls, 0)
        self.assertEqual(self.repository.ingestion_attempt_count(), 0)

    def test_one_bucket_lagging_history_fails_closed(self) -> None:
        client = LaggingHistoryClient(self.clock, timedelta(minutes=15))
        service = self.service(client)

        result = service.run_once(now=self.clock.value)

        self.assertFalse(result.markets[0].success)
        self.assertEqual(result.markets[0].error_code, "NORMALIZATIONERROR")
        self.assertEqual(client.quote_calls, 0)

    def test_after_hours_cannot_re_stamp_a_cached_close_candle(self) -> None:
        self.clock.value = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
        client = FakeDhanClient(self.clock)
        service = self.service(client)
        service._technicals_for(
            segment="IDX_I",
            security_id="13",
            instrument="INDEX",
            now=self.clock.value,
        )
        self.assertEqual(client.history_calls, 1)
        self.clock.advance(60)

        with self.assertRaisesRegex(NormalizationError, "active IST session"):
            service._technicals_for(
                segment="IDX_I",
                security_id="13",
                instrument="INDEX",
                now=self.clock.value,
            )
        self.assertEqual(client.history_calls, 1)

    def test_all_rejected_cycle_is_transport_success_but_not_data_healthy(self) -> None:
        client = RejectedSnapshotClient(self.clock)
        service = self.service(client)

        result = service.run_once(now=self.clock.value)
        health = service.health_snapshot()
        market_health = health["markets"]["NIFTY"]

        self.assertTrue(result.markets[0].success)
        self.assertFalse(result.markets[0].accepted)
        self.assertTrue(result.markets[0].published)
        self.assertFalse(result.markets[0].data_success)
        self.assertEqual(result.markets[0].error_code, "SNAPSHOT_REJECTED")
        self.assertEqual(result.successful_markets, 1)
        self.assertEqual(result.accepted_markets, 0)
        self.assertEqual(result.published_markets, 1)
        self.assertEqual(result.data_successful_markets, 0)
        self.assertEqual(health["successful_markets"], 1)
        self.assertEqual(health["accepted_markets"], 0)
        self.assertEqual(health["published_markets"], 1)
        self.assertEqual(health["data_successful_markets"], 0)
        self.assertEqual(health["lifecycle_state"], "ERROR")
        self.assertIsNone(health["last_success_at"])
        self.assertIsNone(market_health["last_success_at"])
        self.assertEqual(market_health["error_code"], "SNAPSHOT_REJECTED")

    def test_provider_trade_time_drives_market_freshness_not_http_receipt(self) -> None:
        client = ProviderTimestampClient(self.clock, trade_lag=timedelta(seconds=1))
        service = self.service(client)
        provider_time = (self.clock.value - timedelta(seconds=1)).astimezone(IST)

        result = service.run_once(now=self.clock.value)
        workspace = self.read_models.workspace("NIFTY", "NIFTY", now=self.clock.value)

        self.assertTrue(result.markets[0].data_success)
        self.assertIsNotNone(workspace)
        assert workspace is not None
        self.assertEqual(workspace["market"]["observed_at"], provider_time.isoformat())
        self.assertEqual(client.quote_calls, 1)

    def test_one_symbol_failure_does_not_block_another_market(self) -> None:
        client = PartialExpiryClient(self.clock)
        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY", "TCS"),
            clock=self.clock,
            close_client_on_stop=False,
        )

        result = service.run_once(now=self.clock.value)

        self.assertTrue(result.markets[0].success)
        self.assertFalse(result.markets[1].success)
        self.assertEqual(result.successful_markets, 1)
        self.assertEqual(service.health_snapshot()["lifecycle_state"], "PARTIAL")
        self.assertIsNotNone(self.read_models.workspace("NIFTY", "NIFTY", now=self.clock.value))

    def test_missing_client_is_import_safe_and_reports_configuration_required(self) -> None:
        service = DhanAcquisitionService(
            client=None,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY",),
            clock=self.clock,
        )

        self.assertFalse(service.start())
        result = service.run_once(now=NOW)
        self.assertFalse(result.configured)
        self.assertEqual(result.markets[0].error_code, "CONFIG_REQUIRED")
        health = service.health_snapshot()
        self.assertEqual(health["lifecycle_state"], "CONFIG_REQUIRED")
        self.assertNotIn("token", str(health).lower())

    def test_stop_does_not_close_client_under_a_blocked_worker(self) -> None:
        client = BlockingMasterClient(self.clock)
        service = DhanAcquisitionService(
            client=client,
            repository=self.repository,
            ingestion_service=self.ingestion,
            read_models=self.read_models,
            symbols=("NIFTY",),
            clock=self.clock,
            poll_interval_seconds=3,
            maximum_backoff_seconds=3,
        )
        self.assertTrue(service.start())
        self.assertTrue(client.entered.wait(1))

        self.assertFalse(service.stop(timeout=0.01))
        self.assertEqual(client.close_calls, 0)
        self.assertTrue(service.health_snapshot()["running"])

        client.release.set()
        self.assertTrue(service.stop(timeout=2))
        self.assertEqual(client.close_calls, 1)
        self.assertFalse(service.health_snapshot()["running"])


def _historical_payload(as_of: datetime) -> dict:
    count = 46
    latest_end = completed_15m_boundary(as_of).astimezone(UTC)
    first = latest_end - timedelta(minutes=15 * count)
    result = {name: [] for name in ("open", "high", "low", "close", "volume", "timestamp")}
    for index in range(count):
        opening = Decimal(24700) + Decimal(index * 3) + Decimal(index % 4)
        closing = opening + (Decimal(2) if index % 2 else Decimal(-1))
        result["open"].append(float(opening))
        result["high"].append(float(max(opening, closing) + Decimal(4)))
        result["low"].append(float(min(opening, closing) - Decimal(4)))
        result["close"].append(float(closing))
        result["volume"].append(1000 + index * 10)
        result["timestamp"].append(int((first + timedelta(minutes=15 * index)).timestamp()))
    return result


def _chain_payload(cycle: int) -> dict:
    chain = {}
    security_id = 10000
    for strike_index, strike in enumerate((24700, 24750, 24800, 24850, 24900)):
        sides = {}
        for side_index, side in enumerate(("ce", "pe")):
            base = 200 + strike_index * 10 + side_index
            sides[side] = {
                "security_id": security_id,
                "top_bid_price": base,
                "top_ask_price": base + 2,
                "last_price": base + 1,
                "top_bid_quantity": 100,
                "top_ask_quantity": 100,
                "volume": 2000 + strike_index,
                "oi": 5000 + strike_index + cycle * 100,
                "previous_oi": 4900 + strike_index,
                "previous_close_price": base - 5,
                "implied_volatility": 18 + strike_index / 10,
                "greeks": {
                    "delta": 0.5 if side == "ce" else -0.5,
                    "gamma": 0.001,
                    "theta": -10,
                    "vega": 12,
                },
            }
            security_id += 1
        chain[str(strike)] = sides
    # This unrelated strike remains in raw evidence but must not enter the exact
    # contract-bound normalized chain.
    chain["26000"] = {"ce": dict(chain["24800"]["ce"], security_id=99999)}
    return {"status": "success", "data": {"last_price": 24800, "oc": chain}}


def _tcs_master_rows() -> str:
    rows = [
        "NSE,E,11536,TCS,TCS,EQUITY,EQUITY,,,,,,,",
        "NSE,D,60001,TCS-AUG-FUT,TCS AUG FUT,FUTSTK,FUTSTK,11536,TCS,2026-08-28,,,75,0.05",
    ]
    security_id = 11000
    for strike in (24700, 24750, 24800, 24850, 24900):
        for side in ("CE", "PE"):
            rows.append(
                f"NSE,D,{security_id},TCS-{strike}-{side},TCS {strike} {side},"
                f"OPTSTK,OPTSTK,11536,TCS,2026-08-27,{strike},{side},75,0.05"
            )
            security_id += 1
    return "\n".join(rows) + "\n"


def _renumber_nifty_options(master: str) -> str:
    rows = []
    for row in master.splitlines():
        columns = row.split(",")
        if len(columns) > 5 and columns[5] == "OPTIDX":
            columns[2] = str(int(columns[2]) + 20_000)
        rows.append(",".join(columns))
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    unittest.main()
