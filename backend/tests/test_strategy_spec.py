from __future__ import annotations

import unittest
from decimal import Decimal

from teco_quant.domain.enums import (
    DecisionReason,
    DecisionState,
    OperatingMode,
    OptionType,
    ScoreBand,
)
from teco_quant.strategy.spec import (
    DecisionInputs,
    StrategyConfig,
    atm_straddle,
    calculate_position_size,
    confirm_price_action,
    executable_straddle,
    liquidity_score,
    resolve_decision,
    score_band,
    synthetic_futures,
    weighted_score,
)


class StrategySpecTests(unittest.TestCase):
    def test_invalid_strategy_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "minimum volume"):
            StrategyConfig(minimum_volume=0)
        with self.assertRaisesRegex(ValueError, "delta range"):
            StrategyConfig(intraday_delta_minimum=0.8, intraday_delta_maximum=0.7)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            StrategyConfig(target_r_multiples=(1.0, 1.0, 2.0))
        with self.assertRaisesRegex(ValueError, "risk-free rate"):
            StrategyConfig(risk_free_rate=-1.0)
        with self.assertRaisesRegex(ValueError, "dividend yield"):
            StrategyConfig(dividend_yield=-0.01)
        with self.assertRaisesRegex(ValueError, "hard 2% ceiling"):
            StrategyConfig(maximum_risk_per_trade=0.03)

    def test_strategy_configuration_rejects_non_finite_values(self) -> None:
        for field, value in (
            ("maximum_risk_per_trade", float("nan")),
            ("risk_free_rate", float("inf")),
            ("live_max_age_seconds", float("inf")),
            ("conflict_gap", float("-inf")),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ValueError, "must be finite"
            ):
                StrategyConfig(**{field: value})

        with self.assertRaisesRegex(ValueError, "target R multiples must be finite"):
            StrategyConfig(target_r_multiples=(1.0, float("nan"), 3.0))

    def test_corrected_formula_sources_use_prices(self) -> None:
        self.assertEqual(atm_straddle(Decimal(220), Decimal(220)), Decimal(440))
        self.assertEqual(
            executable_straddle(Decimal(222), Decimal(222)), Decimal(444)
        )
        self.assertEqual(
            synthetic_futures(Decimal(24800), Decimal(220), Decimal(220)),
            Decimal(24800),
        )

    def test_liquidity_is_side_independent_and_change_oi_is_not_an_input(self) -> None:
        call = liquidity_score(
            bid=Decimal(218), ask=Decimal(222), volume=2_000, open_interest=5_000
        )
        put = liquidity_score(
            bid=Decimal(218), ask=Decimal(222), volume=2_000, open_interest=5_000
        )
        self.assertEqual(call, put)
        self.assertTrue(call.eligible)

    def test_spread_over_limit_is_rejected(self) -> None:
        result = liquidity_score(
            bid=Decimal(128), ask=Decimal(132), volume=10_000, open_interest=10_000
        )
        self.assertFalse(result.eligible)
        self.assertIn("WIDE_SPREAD", result.rejection_reasons)

    def test_weights_require_every_normalized_factor(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing score factors"):
            weighted_score({"trend": 1.0})
        result = weighted_score(
            {
                "trend": 1.0,
                "premium": 1.0,
                "open_interest": 1.0,
                "liquidity": 1.0,
                "greeks": 1.0,
                "volatility": 1.0,
                "risk_reward": 1.0,
            }
        )
        self.assertEqual(result.total, 100)
        self.assertIs(result.band, ScoreBand.STRONG)

    def test_score_bands_follow_documented_thresholds(self) -> None:
        self.assertIs(score_band(64.99), ScoreBand.REJECTED)
        self.assertIs(score_band(65), ScoreBand.WATCHLIST)
        self.assertIs(score_band(75), ScoreBand.TRADABLE)
        self.assertIs(score_band(85), ScoreBand.STRONG)

    def test_fail_closed_decision_precedence(self) -> None:
        base = {
            "data_complete": True,
            "data_stale": False,
            "expiry_valid": True,
            "extreme_expiry_risk": False,
            "event_risk_active": False,
            "liquid_strike_available": True,
            "affordable": True,
            "call_score": 82,
            "put_score": 70,
            "price_action_confirmed": True,
        }
        outcome = resolve_decision(DecisionInputs(**base))
        self.assertIs(outcome.state, DecisionState.BUY_CALL)

        base["price_action_confirmed"] = None
        outcome = resolve_decision(DecisionInputs(**base))
        self.assertIs(outcome.state, DecisionState.WAIT)
        self.assertIs(outcome.reason, DecisionReason.PRICE_ACTION_PENDING)

        base["data_complete"] = False
        base["data_stale"] = True
        outcome = resolve_decision(DecisionInputs(**base))
        self.assertIs(outcome.state, DecisionState.INSUFFICIENT_DATA)
        self.assertIs(outcome.reason, DecisionReason.DATA_INCOMPLETE)

    def test_watchlist_score_does_not_become_buy(self) -> None:
        outcome = resolve_decision(
            DecisionInputs(
                data_complete=True,
                data_stale=False,
                expiry_valid=True,
                extreme_expiry_risk=False,
                event_risk_active=False,
                liquid_strike_available=True,
                affordable=True,
                call_score=72,
                put_score=50,
                price_action_confirmed=True,
            )
        )
        self.assertIs(outcome.state, DecisionState.WAIT)
        self.assertIs(outcome.reason, DecisionReason.WATCHLIST_ONLY)

    def test_quick_mode_is_advisory_and_never_emits_buy(self) -> None:
        outcome = resolve_decision(
            DecisionInputs(
                data_complete=True,
                data_stale=False,
                expiry_valid=True,
                extreme_expiry_risk=False,
                event_risk_active=False,
                liquid_strike_available=True,
                affordable=True,
                call_score=90,
                put_score=60,
                price_action_confirmed=True,
                operating_mode=OperatingMode.QUICK,
            )
        )
        self.assertIs(outcome.state, DecisionState.WAIT)
        self.assertIs(outcome.reason, DecisionReason.QUICK_MODE_ONLY)

    def test_unknown_event_risk_fails_closed(self) -> None:
        outcome = resolve_decision(
            DecisionInputs(
                data_complete=True,
                data_stale=False,
                expiry_valid=True,
                extreme_expiry_risk=False,
                event_risk_active=None,
                liquid_strike_available=True,
                affordable=True,
                call_score=90,
                put_score=60,
                price_action_confirmed=True,
            )
        )
        self.assertIs(outcome.state, DecisionState.NO_TRADE)
        self.assertIs(outcome.reason, DecisionReason.EVENT_RISK)

    def test_price_action_uses_underlying_signal_candle(self) -> None:
        self.assertTrue(
            confirm_price_action(
                option_type=OptionType.CALL,
                underlying_price=Decimal(24851),
                signal_candle_high=Decimal(24850),
                signal_candle_low=Decimal(24750),
            )
        )
        self.assertTrue(
            confirm_price_action(
                option_type=OptionType.PUT,
                underlying_price=Decimal(24749),
                signal_candle_high=Decimal(24850),
                signal_candle_low=Decimal(24750),
            )
        )

    def test_position_size_includes_estimated_costs(self) -> None:
        result = calculate_position_size(
            account_capital=Decimal(500000),
            risk_rate=0.01,
            maximum_premium_allocation=0.25,
            entry=Decimal(200),
            stop=Decimal(160),
            lot_size=75,
            estimated_round_trip_cost_per_lot=Decimal(58),
        )
        self.assertEqual(result.maximum_risk, Decimal("5000.00"))
        self.assertEqual(result.maximum_lots, 1)
        self.assertEqual(result.quantity, 75)
        self.assertLessEqual(result.risk_per_lot * result.maximum_lots, result.maximum_risk)

    def test_position_size_enforces_configured_risk_ceiling(self) -> None:
        inputs = {
            "account_capital": Decimal(500000),
            "maximum_premium_allocation": 0.25,
            "entry": Decimal(200),
            "stop": Decimal(160),
            "lot_size": 75,
        }
        with self.assertRaisesRegex(ValueError, "configured maximum risk"):
            calculate_position_size(risk_rate=0.03, **inputs)

        with self.assertRaisesRegex(ValueError, "configured maximum risk"):
            calculate_position_size(
                risk_rate=0.02,
                config=StrategyConfig(maximum_risk_per_trade=0.015),
                **inputs,
            )

        result = calculate_position_size(
            risk_rate=0.015,
            config=StrategyConfig(maximum_risk_per_trade=0.015),
            **inputs,
        )
        self.assertEqual(result.maximum_risk, Decimal("7500.000"))


if __name__ == "__main__":
    unittest.main()
