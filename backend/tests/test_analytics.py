from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import exp

from teco_quant.analytics import (
    AnalyticsInputError,
    Candle,
    TrendDirection,
    analyze_option_chain,
    atr,
    black_76,
    black_scholes,
    ema,
    expected_move,
    hourly_confirmation,
    price_option,
    resample_completed_candles,
    true_range,
    vwap,
    wilder_rsi,
    wma,
    year_fraction,
)
from teco_quant.domain.enums import Exchange, MarketKind, OptionType, PricingModel
from teco_quant.domain.models import (
    ContractSpec,
    Greeks,
    InstrumentId,
    InstrumentMasterRecord,
    MarketState,
    OptionQuote,
)
from tests.helpers import NOW, valid_snapshot

BASE = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


def make_candle(
    index: int,
    close: Decimal | int | str,
    *,
    minutes: int = 15,
    completed: bool = True,
    volume: Decimal | int | str = 100,
) -> Candle:
    normalized_close = Decimal(str(close))
    start = BASE + timedelta(minutes=index * minutes)
    return Candle(
        start=start,
        end=start + timedelta(minutes=minutes),
        open=normalized_close - Decimal("0.25"),
        high=normalized_close + Decimal(1),
        low=normalized_close - Decimal(1),
        close=normalized_close,
        volume=Decimal(str(volume)),
        completed=completed,
    )


class CandleIndicatorTests(unittest.TestCase):
    def test_candle_rejects_invalid_money_time_and_ohlc(self) -> None:
        with self.assertRaisesRegex(AnalyticsInputError, "timezone-aware"):
            Candle(
                start=datetime.fromisoformat("2026-01-01T00:00:00"),
                end=datetime.fromisoformat("2026-01-01T00:01:00"),
                open=Decimal(1),
                high=Decimal(2),
                low=Decimal(1),
                close=Decimal(1),
                volume=Decimal(1),
            )
        with self.assertRaisesRegex(AnalyticsInputError, "finite"):
            make_candle(0, Decimal("NaN"))
        with self.assertRaisesRegex(AnalyticsInputError, "high"):
            Candle(
                start=BASE,
                end=BASE + timedelta(minutes=1),
                open=Decimal(10),
                high=Decimal(9),
                low=Decimal(8),
                close=Decimal(10),
                volume=Decimal(1),
            )

    def test_vwap_ema_wma_and_incomplete_candle_exclusion(self) -> None:
        candles = tuple(make_candle(index, index + 10) for index in range(5))
        incomplete = make_candle(5, 1000, completed=False, volume=1_000_000)
        with_incomplete = (*candles, incomplete)

        self.assertEqual(ema(with_incomplete, 3), Decimal(13))
        self.assertEqual(wma(with_incomplete, 3), Decimal(80) / Decimal(6))
        self.assertEqual(vwap(with_incomplete), vwap(candles))
        self.assertIsNone(ema(candles[:2], 3))
        self.assertIsNone(vwap((make_candle(0, 10, volume=0),)))

    def test_wilder_rsi_matches_hand_calculated_wilder_seed(self) -> None:
        closes = (
            "54.8",
            "56.8",
            "57.85",
            "59.85",
            "60.57",
            "61.1",
            "62.17",
            "60.6",
            "62.35",
            "62.15",
            "62.35",
            "61.45",
            "62.8",
            "61.37",
            "62.5",
        )
        result = wilder_rsi(tuple(make_candle(index, value) for index, value in enumerate(closes)))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result or 0.0, 74.2138364780, places=9)

        flat = tuple(make_candle(index, 50) for index in range(15))
        self.assertEqual(wilder_rsi(flat), 50.0)

    def test_true_range_and_wilder_atr_regression(self) -> None:
        def explicit(index: int, high: str, low: str, close: str) -> Candle:
            start = BASE + timedelta(minutes=15 * index)
            return Candle(
                start=start,
                end=start + timedelta(minutes=15),
                open=Decimal(close),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=Decimal(1),
            )

        candles = (
            explicit(0, "10", "8", "9"),
            explicit(1, "12", "9", "11"),
            explicit(2, "13", "10", "12"),
            explicit(3, "15", "11", "14"),
        )
        self.assertEqual(true_range(candles[1], Decimal(9)), Decimal(3))
        self.assertEqual(atr(candles, 3), Decimal(28) / Decimal(9))
        with self.assertRaisesRegex(AnalyticsInputError, "completed"):
            true_range(make_candle(0, 10, completed=False))

    def test_resampling_emits_only_complete_contiguous_closed_buckets(self) -> None:
        candles = tuple(make_candle(index, 100 + index) for index in range(7)) + (
            make_candle(7, 107, completed=False),
        )
        hourly = resample_completed_candles(
            candles,
            timedelta(hours=1),
            as_of=BASE + timedelta(hours=2),
            anchor=BASE,
        )
        self.assertEqual(len(hourly), 1)
        self.assertEqual(hourly[0].open, Decimal("99.75"))
        self.assertEqual(hourly[0].close, Decimal(103))
        self.assertEqual(hourly[0].high, Decimal(104))
        self.assertEqual(hourly[0].volume, Decimal(400))

        # Even a completed source candle ending after as_of cannot leak into a bucket.
        partial = resample_completed_candles(
            tuple(make_candle(index, 100 + index) for index in range(8)),
            timedelta(hours=1),
            as_of=BASE + timedelta(hours=1, minutes=45),
            anchor=BASE,
        )
        self.assertEqual(len(partial), 1)

    def test_hourly_confirmation_has_no_incomplete_candle_lookahead(self) -> None:
        completed = tuple(make_candle(index, 100 + index) for index in range(16))
        future_spike = tuple(
            make_candle(index, 1000 + index, completed=False) for index in range(16, 20)
        )
        kwargs = {
            "as_of": BASE + timedelta(hours=5),
            "fast_period": 2,
            "slow_period": 3,
            "anchor": BASE,
        }
        baseline = hourly_confirmation(completed, **kwargs)
        with_spike = hourly_confirmation((*completed, *future_spike), **kwargs)
        self.assertEqual(with_spike, baseline)
        self.assertEqual(baseline.completed_hours, 4)
        self.assertEqual(baseline.last_completed_hour, BASE + timedelta(hours=4))
        self.assertEqual(baseline.direction, TrendDirection.BULLISH)


class OptionPricingTests(unittest.TestCase):
    def test_black_scholes_matches_canonical_one_year_example(self) -> None:
        call = black_scholes(
            option_type=OptionType.CALL,
            spot=Decimal(100),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=1.0,
            risk_free_rate=0.05,
        )
        put = black_scholes(
            option_type=OptionType.PUT,
            spot=Decimal(100),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=1.0,
            risk_free_rate=0.05,
        )
        self.assertAlmostEqual(float(call.price), 10.4505835722, places=9)
        self.assertAlmostEqual(float(put.price), 5.5735260223, places=9)
        self.assertAlmostEqual(call.delta, 0.6368306512, places=9)
        self.assertAlmostEqual(put.delta, -0.3631693488, places=9)
        self.assertAlmostEqual(call.gamma, 0.01876201735, places=10)
        self.assertAlmostEqual(call.theta_per_day, -0.0175726782094, places=12)
        self.assertAlmostEqual(call.vega_per_vol_point, 0.3752403469, places=9)
        self.assertAlmostEqual(call.gamma, put.gamma, places=14)
        self.assertAlmostEqual(call.vega_per_vol_point, put.vega_per_vol_point, places=14)

    def test_black_scholes_put_call_parity_with_dividend_yield(self) -> None:
        parameters = {
            "spot": Decimal(125),
            "strike": Decimal(120),
            "volatility": 0.31,
            "time_to_expiry": 0.75,
            "risk_free_rate": 0.06,
            "dividend_yield": 0.015,
        }
        call = black_scholes(option_type=OptionType.CALL, **parameters)
        put = black_scholes(option_type=OptionType.PUT, **parameters)
        expected = 125 * exp(-0.015 * 0.75) - 120 * exp(-0.06 * 0.75)
        self.assertAlmostEqual(float(call.price - put.price), expected, places=11)

    def test_black_76_regression_parity_and_call_put_symmetry(self) -> None:
        call = black_76(
            option_type=OptionType.CALL,
            futures=Decimal(100),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=1.0,
            risk_free_rate=0.05,
        )
        put = black_76(
            option_type=OptionType.PUT,
            futures=Decimal(100),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=1.0,
            risk_free_rate=0.05,
        )
        self.assertAlmostEqual(float(call.price), 7.5770821464, places=9)
        self.assertAlmostEqual(float(put.price), 7.5770821464, places=9)
        self.assertAlmostEqual(call.price - put.price, Decimal(0), places=12)
        self.assertAlmostEqual(call.gamma, put.gamma, places=14)
        self.assertAlmostEqual(call.theta_per_day, put.theta_per_day, places=14)
        self.assertAlmostEqual(call.vega_per_vol_point, put.vega_per_vol_point, places=14)

        non_atm_call = black_76(
            option_type=OptionType.CALL,
            futures=Decimal(105),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=0.5,
            risk_free_rate=0.04,
        )
        non_atm_put = black_76(
            option_type=OptionType.PUT,
            futures=Decimal(105),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=0.5,
            risk_free_rate=0.04,
        )
        parity = exp(-0.04 * 0.5) * 5
        self.assertAlmostEqual(float(non_atm_call.price - non_atm_put.price), parity, places=11)

    def test_expected_move_dispatch_and_invalid_numeric_edges(self) -> None:
        move = expected_move(Decimal(100), 0.2, 1.0)
        self.assertEqual(move.move, Decimal("20.0"))
        self.assertEqual(move.lower_bound, Decimal("80.0"))
        self.assertEqual(move.upper_bound, Decimal("120.0"))

        dispatched = price_option(
            PricingModel.BLACK_76,
            option_type=OptionType.CALL,
            underlying=Decimal(100),
            strike=Decimal(100),
            volatility=0.2,
            time_to_expiry=1.0,
            risk_free_rate=0.05,
        )
        self.assertEqual(dispatched.model, PricingModel.BLACK_76)
        with self.assertRaisesRegex(AnalyticsInputError, "dividend yield"):
            price_option(
                PricingModel.BLACK_76,
                option_type=OptionType.CALL,
                underlying=Decimal(100),
                strike=Decimal(100),
                volatility=0.2,
                time_to_expiry=1.0,
                risk_free_rate=0.05,
                dividend_yield=0.01,
            )
        for field, value in (
            ("volatility", 0.0),
            ("volatility", float("nan")),
            ("time_to_expiry", 0.0),
            ("time_to_expiry", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                arguments = {
                    "option_type": OptionType.CALL,
                    "spot": Decimal(100),
                    "strike": Decimal(100),
                    "volatility": 0.2,
                    "time_to_expiry": 1.0,
                    "risk_free_rate": 0.05,
                }
                arguments[field] = value
                with self.assertRaises(AnalyticsInputError):
                    black_scholes(**arguments)

    def test_year_fraction_uses_aware_exact_seconds_and_rejects_expiry(self) -> None:
        self.assertEqual(year_fraction(BASE, BASE + timedelta(days=365)), 1.0)
        with self.assertRaisesRegex(AnalyticsInputError, "after"):
            year_fraction(BASE, BASE)
        with self.assertRaisesRegex(AnalyticsInputError, "timezone-aware"):
            year_fraction(datetime.fromisoformat("2026-01-01T00:00:00"), BASE)


class ChainAnalyticsTests(unittest.TestCase):
    def test_chain_uses_each_leg_iv_and_deterministic_contract_order(self) -> None:
        snapshot = valid_snapshot()
        analyzed = analyze_option_chain(
            contract=snapshot.contract,
            market=snapshot.market,
            quotes=reversed(snapshot.option_chain),
            as_of=NOW,
            pricing_security_id=snapshot.contract.underlying.security_id,
            risk_free_rate=0.06,
            dividend_yield=0.01,
        )
        self.assertEqual(len(analyzed), 10)
        self.assertEqual(analyzed[0].strike, Decimal(24700))
        self.assertEqual(analyzed[0].option_type, OptionType.CALL)
        self.assertEqual(analyzed[0].implied_volatility, 0.18)
        self.assertEqual(analyzed[-1].implied_volatility, 0.184)
        self.assertNotEqual(analyzed[0].expected_move.move, analyzed[-1].expected_move.move)
        self.assertTrue(all(leg.analytics.model is PricingModel.BLACK_SCHOLES for leg in analyzed))

    def test_chain_fails_closed_on_identity_iv_future_data_and_expiry(self) -> None:
        snapshot = valid_snapshot()
        base = {
            "contract": snapshot.contract,
            "market": snapshot.market,
            "quotes": snapshot.option_chain,
            "as_of": NOW,
            "pricing_security_id": snapshot.contract.underlying.security_id,
            "risk_free_rate": 0.06,
        }
        with self.assertRaisesRegex(AnalyticsInputError, "does not match"):
            analyze_option_chain(**{**base, "pricing_security_id": "WRONG"})

        bad_iv = (replace(snapshot.option_chain[0], implied_volatility=float("nan")),)
        with self.assertRaisesRegex(AnalyticsInputError, "IV"):
            analyze_option_chain(**{**base, "quotes": bad_iv})

        future_quote = (replace(snapshot.option_chain[0], observed_at=NOW + timedelta(seconds=1)),)
        with self.assertRaisesRegex(AnalyticsInputError, "future"):
            analyze_option_chain(**{**base, "quotes": future_quote})

        with self.assertRaisesRegex(AnalyticsInputError, "expiry"):
            analyze_option_chain(**{**base, "as_of": snapshot.contract.option_expiry})

    def test_mcx_black_76_requires_the_exact_futures_security(self) -> None:
        base_contract = valid_snapshot().contract
        expiry = NOW + timedelta(days=10)
        underlying = InstrumentId(
            exchange=Exchange.MCX,
            segment="MCX_COMM",
            security_id="114",
            symbol="GOLD",
        )
        future = InstrumentMasterRecord(
            instrument=InstrumentId(
                exchange=Exchange.MCX,
                segment="MCX_COMM",
                security_id="90001",
                symbol="GOLD-AUG-FUT",
            ),
            display_name="GOLD AUG FUT",
            instrument_type="FUTCOM",
            underlying_security_id="114",
            expiry=expiry + timedelta(days=1),
            lot_size=1,
            tick_size=Decimal(1),
        )
        option_record = InstrumentMasterRecord(
            instrument=InstrumentId(
                exchange=Exchange.MCX,
                segment="MCX_COMM",
                security_id="91001",
                symbol="GOLD-72000-CE",
            ),
            display_name="GOLD 72000 CE",
            instrument_type="OPTFUT",
            underlying_security_id="114",
            expiry=expiry,
            strike=Decimal(72000),
            option_type=OptionType.CALL,
            lot_size=1,
            tick_size=Decimal(1),
        )
        contract = ContractSpec(
            underlying=underlying,
            market_kind=MarketKind.COMMODITY,
            pricing_model=PricingModel.BLACK_76,
            option_expiry=expiry,
            lot_size=1,
            strike_interval=Decimal(100),
            tick_size=Decimal(1),
            master=base_contract.master,
            option_contracts=(option_record,),
            futures=future,
        )
        market = MarketState(
            observed_at=NOW,
            spot_price=None,
            futures_price=Decimal(72100),
        )
        quote = OptionQuote(
            security_id="91001",
            strike=Decimal(72000),
            option_type=OptionType.CALL,
            expiry=expiry,
            bid=Decimal(100),
            ask=Decimal(101),
            ltp=Decimal("100.5"),
            volume=100,
            open_interest=1000,
            previous_open_interest=900,
            change_open_interest=100,
            implied_volatility=0.21,
            greeks=Greeks(),
            observed_at=NOW,
        )
        result = analyze_option_chain(
            contract=contract,
            market=market,
            quotes=(quote,),
            as_of=NOW,
            pricing_security_id="90001",
            risk_free_rate=0.06,
        )
        self.assertEqual(result[0].analytics.model, PricingModel.BLACK_76)
        self.assertEqual(result[0].pricing_underlying, Decimal(72100))
        with self.assertRaisesRegex(AnalyticsInputError, "exact contract future"):
            analyze_option_chain(
                contract=contract,
                market=market,
                quotes=(quote,),
                as_of=NOW,
                pricing_security_id="DIFFERENT-FUTURE",
                risk_free_rate=0.06,
            )


if __name__ == "__main__":
    unittest.main()
