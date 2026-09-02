from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from teco_quant.config import (
    RuntimeSettings,
    StrategyInputSettings,
    environment_with_dotenv,
)
from teco_quant.domain.enums import OperatingMode, TradingStyle


class RuntimeSettingsTests(unittest.TestCase):
    def test_empty_environment_uses_declared_defaults_with_slots(self) -> None:
        settings = RuntimeSettings.from_environment({})

        self.assertEqual(settings, RuntimeSettings())
        self.assertIsNone(settings.strategy_inputs)

    def test_strategy_inputs_are_atomic_explicit_and_fractional(self) -> None:
        settings = RuntimeSettings.from_environment(
            {
                "TECO_ACCOUNT_CAPITAL": "250000",
                "TECO_RISK_PER_TRADE": "0.01",
                "TECO_MAX_PREMIUM_ALLOCATION": "0.10",
                "TECO_EVENT_RISK_ACTIVE": "false",
                "TECO_EXPECTED_HOLDING_HOURS": "3.5",
                "TECO_TRADING_STYLE": "intraday",
                "TECO_OPERATING_MODE": "pro",
                "TECO_PRICE_ACTION_CONFIRMED": "true",
            }
        )

        self.assertEqual(
            settings.strategy_inputs,
            StrategyInputSettings(
                account_capital=Decimal(250000),
                risk_per_trade=0.01,
                maximum_premium_allocation=0.10,
                event_risk_active=False,
                expected_holding_hours=3.5,
                trading_style=TradingStyle.INTRADAY,
                operating_mode=OperatingMode.PRO,
                price_action_confirmed=True,
            ),
        )

    def test_partial_or_invalid_strategy_inputs_are_rejected(self) -> None:
        complete = {
            "TECO_ACCOUNT_CAPITAL": "250000",
            "TECO_RISK_PER_TRADE": "0.01",
            "TECO_MAX_PREMIUM_ALLOCATION": "0.10",
            "TECO_EVENT_RISK_ACTIVE": "false",
            "TECO_EXPECTED_HOLDING_HOURS": "3.5",
            "TECO_TRADING_STYLE": "INTRADAY",
            "TECO_OPERATING_MODE": "PRO",
        }
        cases = (
            ({"TECO_ACCOUNT_CAPITAL": "250000"}, "complete group"),
            ({**complete, "TECO_RISK_PER_TRADE": "1.1"}, "fraction"),
            ({**complete, "TECO_EVENT_RISK_ACTIVE": "unknown"}, "ACTIVE"),
            ({**complete, "TECO_TRADING_STYLE": "SCALP"}, "TRADING_STYLE"),
        )
        for environment, message in cases:
            with self.subTest(environment=environment), self.assertRaisesRegex(
                ValueError, message
            ):
                RuntimeSettings.from_environment(environment)

    def test_environment_values_are_trimmed_and_normalized(self) -> None:
        settings = RuntimeSettings.from_environment(
            {
                "TECO_DATABASE_PATH": " data/test.db ",
                "TECO_LOG_LEVEL": " warning ",
                "TECO_LIVE_MAX_AGE_SECONDS": " 15.5 ",
            }
        )

        self.assertEqual(settings.database_path, "data/test.db")
        self.assertEqual(settings.log_level, "WARNING")
        self.assertEqual(settings.live_max_age_seconds, 15.5)

    def test_blank_or_unknown_log_level_is_rejected(self) -> None:
        for value in ("", "   ", "TRACE", "verbose"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "TECO_LOG_LEVEL"
            ):
                RuntimeSettings.from_environment({"TECO_LOG_LEVEL": value})

    def test_live_age_must_be_finite_and_positive(self) -> None:
        for value in ("nan", "inf", "-inf", "0", "-1"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "TECO_LIVE_MAX_AGE_SECONDS"
            ):
                RuntimeSettings.from_environment(
                    {"TECO_LIVE_MAX_AGE_SECONDS": value}
                )

    def test_server_feed_and_secret_values_are_validated_without_secret_repr(self) -> None:
        settings = RuntimeSettings.from_environment(
            {
                "TECO_SERVER_HOST": " 127.0.0.1 ",
                "TECO_SERVER_PORT": " 9000 ",
                "TECO_ALLOWED_ORIGINS": (
                    "http://localhost:5173,https://frontend.example,"
                    "http://localhost:5173"
                ),
                "TECO_API_KEY": "server-only-secret",
                "TECO_API_MAX_BODY_BYTES": "2048",
                "TECO_EXECUTION_MODE": "paper_trading",
                "TECO_DHAN_LIVE_ENABLED": "true",
                "TECO_DHAN_INSTRUMENTS": "NSE_FNO:101, IDX_I:13,NSE_FNO:101",
                "TECO_FEED_IDLE_TIMEOUT_SECONDS": "12.5",
                "TECO_FEED_RECONNECT_MAX_SECONDS": "45",
            }
        )

        self.assertEqual(settings.server_host, "127.0.0.1")
        self.assertEqual(settings.server_port, 9000)
        self.assertEqual(
            settings.allowed_origins,
            ("http://localhost:5173", "https://frontend.example"),
        )
        self.assertEqual(settings.execution_mode, "PAPER_TRADING")
        self.assertTrue(settings.dhan_live_enabled)
        self.assertEqual(
            settings.dhan_instruments,
            (("NSE_FNO", "101"), ("IDX_I", "13")),
        )
        self.assertNotIn("server-only-secret", repr(settings))

    def test_live_execution_wildcard_cors_and_bad_feed_values_are_rejected(self) -> None:
        cases = (
            ({"TECO_EXECUTION_MODE": "LIVE_AUTOMATIC"}, "TECO_EXECUTION_MODE"),
            ({"TECO_ALLOWED_ORIGINS": "*"}, "wildcard"),
            ({"TECO_ALLOWED_ORIGINS": "http://localhost:5173/path"}, "origins"),
            ({"TECO_DHAN_LIVE_ENABLED": "yes"}, "TECO_DHAN_LIVE_ENABLED"),
            ({"TECO_DHAN_INSTRUMENTS": "NSE_FNO:not-an-id"}, "security IDs"),
            ({"TECO_SERVER_PORT": "65536"}, "TECO_SERVER_PORT"),
            ({"TECO_SERVER_HOST": "0.0.0.0"}, "loopback"),
        )
        for environment, message in cases:
            with self.subTest(environment=environment), self.assertRaisesRegex(
                ValueError, message
            ):
                RuntimeSettings.from_environment(environment)

    def test_dotenv_supplements_but_never_overrides_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# local settings\n"
                "TECO_SERVER_PORT=9000\n"
                "DHAN_ACCESS_TOKEN='stored-exactly'\n",
                encoding="utf-8",
            )

            combined = environment_with_dotenv(
                {"TECO_SERVER_PORT": "8001"}, path
            )

        self.assertEqual(combined["TECO_SERVER_PORT"], "8001")
        self.assertEqual(combined["DHAN_ACCESS_TOKEN"], "stored-exactly")

    def test_dotenv_rejects_malformed_or_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for contents in (
                "MISSING_EQUALS",
                "BAD-NAME=value",
                "NAME=one\nNAME=two",
                'NAME="unterminated',
            ):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        environment_with_dotenv({}, path)


if __name__ == "__main__":
    unittest.main()
