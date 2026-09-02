"""Credential-safe runtime configuration for the backend process."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from teco_quant.domain.enums import OperatingMode, TradingStyle

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_SAFE_EXECUTION_MODES = frozenset({"DATA_ONLY", "PAPER_TRADING"})
_SEGMENT_PATTERN = re.compile(r"^[A-Z0-9_]+$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STRATEGY_REQUIRED_ENV = (
    "TECO_ACCOUNT_CAPITAL",
    "TECO_RISK_PER_TRADE",
    "TECO_MAX_PREMIUM_ALLOCATION",
    "TECO_EVENT_RISK_ACTIVE",
    "TECO_EXPECTED_HOLDING_HOURS",
    "TECO_TRADING_STYLE",
    "TECO_OPERATING_MODE",
)
_STRATEGY_OPTIONAL_ENV = ("TECO_PRICE_ACTION_CONFIRMED",)


def environment_with_dotenv(
    environment: Mapping[str, str] | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Return process settings supplemented by ``backend/.env``.

    Existing process values always win. Values are not expanded, evaluated, logged, or
    written back to :data:`os.environ`, which keeps launch behavior predictable and
    prevents secrets from being exposed through command construction.
    """

    combined = dict(os.environ if environment is None else environment)
    environment_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[2] / ".env"
    )
    if not environment_path.exists():
        return combined
    if not environment_path.is_file():
        raise ValueError("backend .env path is not a regular file")

    seen: set[str] = set()
    try:
        lines = environment_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError("backend .env could not be read") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"backend .env line {line_number} must contain '='")
        raw_name, raw_value = line.split("=", 1)
        name = raw_name.strip()
        if not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"backend .env line {line_number} has an invalid name")
        if name in seen:
            raise ValueError(f"backend .env contains duplicate setting {name}")
        seen.add(name)
        value = _dotenv_value(raw_value.strip(), line_number)
        combined.setdefault(name, value)
    return combined


@dataclass(frozen=True, slots=True)
class StrategyInputSettings:
    """Explicit operator-owned inputs required before signals may become actionable."""

    account_capital: Decimal
    risk_per_trade: float
    maximum_premium_allocation: float
    event_risk_active: bool
    expected_holding_hours: float
    trading_style: TradingStyle
    operating_mode: OperatingMode
    price_action_confirmed: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.account_capital, Decimal):
            raise TypeError("TECO_ACCOUNT_CAPITAL must be a Decimal amount")
        if not self.account_capital.is_finite() or self.account_capital <= 0:
            raise ValueError("TECO_ACCOUNT_CAPITAL must be a positive finite amount")
        for name, value in (
            ("TECO_RISK_PER_TRADE", self.risk_per_trade),
            ("TECO_MAX_PREMIUM_ALLOCATION", self.maximum_premium_allocation),
        ):
            if isinstance(value, bool) or not isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be a fraction within (0, 1]")
        if (
            isinstance(self.expected_holding_hours, bool)
            or not isfinite(self.expected_holding_hours)
            or self.expected_holding_hours <= 0
        ):
            raise ValueError(
                "TECO_EXPECTED_HOLDING_HOURS must be finite and positive"
            )
        if not isinstance(self.event_risk_active, bool):
            raise TypeError("TECO_EVENT_RISK_ACTIVE must be explicitly true or false")
        if not isinstance(self.trading_style, TradingStyle):
            raise TypeError("TECO_TRADING_STYLE must be a TradingStyle")
        if not isinstance(self.operating_mode, OperatingMode):
            raise TypeError("TECO_OPERATING_MODE must be an OperatingMode")
        if self.price_action_confirmed is not None and not isinstance(
            self.price_action_confirmed, bool
        ):
            raise TypeError(
                "TECO_PRICE_ACTION_CONFIRMED must be true, false, or unset"
            )


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Validated process settings with secrets excluded from ``repr``."""

    database_path: str = "data/teco_quant.db"
    execution_database_path: str = "data/teco_execution.db"
    signal_history_database_path: str = "data/teco_signals.db"
    log_level: str = "INFO"
    live_max_age_seconds: float = 30.0
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    api_max_body_bytes: int = 16_384
    api_key: str | None = field(default=None, repr=False)
    execution_mode: str = "DATA_ONLY"
    dhan_live_enabled: bool = False
    dhan_instruments: tuple[tuple[str, str], ...] = ()
    feed_idle_timeout_seconds: float = 30.0
    feed_reconnect_max_seconds: float = 60.0
    strategy_inputs: StrategyInputSettings | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> RuntimeSettings:
        values = environment if environment is not None else os.environ
        defaults = cls()

        database_path = _required_trimmed(
            values, "TECO_DATABASE_PATH", defaults.database_path
        )
        execution_database_path = _required_trimmed(
            values,
            "TECO_EXECUTION_DATABASE_PATH",
            defaults.execution_database_path,
        )
        signal_history_database_path = _required_trimmed(
            values,
            "TECO_SIGNAL_HISTORY_DATABASE_PATH",
            defaults.signal_history_database_path,
        )
        log_level = _required_trimmed(
            values, "TECO_LOG_LEVEL", defaults.log_level
        ).upper()
        if log_level not in _ALLOWED_LOG_LEVELS:
            allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
            raise ValueError(f"TECO_LOG_LEVEL must be one of: {allowed}")

        live_max_age_seconds = _positive_float(
            values,
            "TECO_LIVE_MAX_AGE_SECONDS",
            defaults.live_max_age_seconds,
        )
        server_host = _required_trimmed(
            values, "TECO_SERVER_HOST", defaults.server_host
        )
        if any(character.isspace() for character in server_host):
            raise ValueError("TECO_SERVER_HOST cannot contain whitespace")
        if not _is_loopback_host(server_host):
            raise ValueError(
                "TECO_SERVER_HOST must remain a loopback address for the built-in server"
            )
        server_port = _positive_int(
            values, "TECO_SERVER_PORT", defaults.server_port, maximum=65_535
        )

        if "TECO_ALLOWED_ORIGINS" in values:
            allowed_origins = _parse_origins(values["TECO_ALLOWED_ORIGINS"])
        else:
            allowed_origins = defaults.allowed_origins

        api_max_body_bytes = _positive_int(
            values,
            "TECO_API_MAX_BODY_BYTES",
            defaults.api_max_body_bytes,
        )
        api_key = _optional_secret(values, "TECO_API_KEY")

        execution_mode = _required_trimmed(
            values, "TECO_EXECUTION_MODE", defaults.execution_mode
        ).upper()
        if execution_mode not in _SAFE_EXECUTION_MODES:
            allowed = ", ".join(sorted(_SAFE_EXECUTION_MODES))
            raise ValueError(
                "TECO_EXECUTION_MODE must remain non-live and be one of: " + allowed
            )

        dhan_live_enabled = _boolean(
            values,
            "TECO_DHAN_LIVE_ENABLED",
            defaults.dhan_live_enabled,
        )
        dhan_instruments = _parse_instruments(
            values.get("TECO_DHAN_INSTRUMENTS", "")
        )
        feed_idle_timeout_seconds = _positive_float(
            values,
            "TECO_FEED_IDLE_TIMEOUT_SECONDS",
            defaults.feed_idle_timeout_seconds,
        )
        feed_reconnect_max_seconds = _positive_float(
            values,
            "TECO_FEED_RECONNECT_MAX_SECONDS",
            defaults.feed_reconnect_max_seconds,
        )
        strategy_inputs = _strategy_input_settings(values)

        return cls(
            database_path=database_path,
            execution_database_path=execution_database_path,
            signal_history_database_path=signal_history_database_path,
            log_level=log_level,
            live_max_age_seconds=live_max_age_seconds,
            server_host=server_host,
            server_port=server_port,
            allowed_origins=allowed_origins,
            api_max_body_bytes=api_max_body_bytes,
            api_key=api_key,
            execution_mode=execution_mode,
            dhan_live_enabled=dhan_live_enabled,
            dhan_instruments=dhan_instruments,
            feed_idle_timeout_seconds=feed_idle_timeout_seconds,
            feed_reconnect_max_seconds=feed_reconnect_max_seconds,
            strategy_inputs=strategy_inputs,
        )


def _required_trimmed(
    values: Mapping[str, str], name: str, default: str
) -> str:
    value = values.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} cannot be blank")
    return value


def _dotenv_value(raw_value: str, line_number: int) -> str:
    if not raw_value:
        return ""
    if raw_value[0] not in {"'", '"'}:
        return raw_value
    quote = raw_value[0]
    if len(raw_value) < 2 or raw_value[-1] != quote:
        raise ValueError(f"backend .env line {line_number} has an unterminated quote")
    # Do not interpret escapes or variable substitutions. Broker tokens must be loaded
    # exactly as the operator stored them.
    return raw_value[1:-1]


def _positive_float(
    values: Mapping[str, str], name: str, default: float
) -> float:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _positive_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int | None = None,
) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        suffix = f" between 1 and {maximum}" if maximum is not None else " positive"
        raise ValueError(f"{name} must be{suffix}")
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = values.get(name, "true" if default else "false").strip().lower()
    if raw_value == "true":
        return True
    if raw_value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_secret(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None or not value.strip():
        return None
    if value != value.strip():
        raise ValueError(f"{name} cannot have leading or trailing whitespace")
    return value


def _strategy_input_settings(
    values: Mapping[str, str],
) -> StrategyInputSettings | None:
    names = (*_STRATEGY_REQUIRED_ENV, *_STRATEGY_OPTIONAL_ENV)
    supplied = {
        name
        for name in names
        if name in values and bool(str(values[name]).strip())
    }
    if not supplied:
        return None
    missing = [name for name in _STRATEGY_REQUIRED_ENV if name not in supplied]
    if missing:
        raise ValueError(
            "strategy decision inputs must be supplied as one complete group; missing: "
            + ", ".join(missing)
        )

    account_capital = _positive_decimal(values, "TECO_ACCOUNT_CAPITAL")
    risk_per_trade = _positive_float(values, "TECO_RISK_PER_TRADE", 0.0)
    maximum_premium_allocation = _positive_float(
        values, "TECO_MAX_PREMIUM_ALLOCATION", 0.0
    )
    event_risk_active = _boolean(values, "TECO_EVENT_RISK_ACTIVE", False)
    expected_holding_hours = _positive_float(
        values, "TECO_EXPECTED_HOLDING_HOURS", 0.0
    )
    try:
        trading_style = TradingStyle(
            values["TECO_TRADING_STYLE"].strip().upper()
        )
    except ValueError as exc:
        raise ValueError(
            "TECO_TRADING_STYLE must be INTRADAY or POSITIONAL"
        ) from exc
    try:
        operating_mode = OperatingMode(
            values["TECO_OPERATING_MODE"].strip().upper()
        )
    except ValueError as exc:
        raise ValueError("TECO_OPERATING_MODE must be QUICK or PRO") from exc
    price_action_confirmed = (
        _boolean(values, "TECO_PRICE_ACTION_CONFIRMED", False)
        if "TECO_PRICE_ACTION_CONFIRMED" in supplied
        else None
    )
    return StrategyInputSettings(
        account_capital=account_capital,
        risk_per_trade=risk_per_trade,
        maximum_premium_allocation=maximum_premium_allocation,
        event_risk_active=event_risk_active,
        expected_holding_hours=expected_holding_hours,
        trading_style=trading_style,
        operating_mode=operating_mode,
        price_action_confirmed=price_action_confirmed,
    )


def _positive_decimal(values: Mapping[str, str], name: str) -> Decimal:
    raw_value = values[name].strip()
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _parse_origins(raw_value: str) -> tuple[str, ...]:
    if not raw_value.strip():
        return ()
    values = raw_value.split(",")
    if any(not value.strip() for value in values):
        raise ValueError("TECO_ALLOWED_ORIGINS contains a blank origin")

    origins: list[str] = []
    for raw_origin in values:
        origin = raw_origin.strip()
        if any(character.isspace() for character in origin):
            raise ValueError("TECO_ALLOWED_ORIGINS cannot contain whitespace")
        if origin == "*":
            raise ValueError("TECO_ALLOWED_ORIGINS does not permit wildcard origins")
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("TECO_ALLOWED_ORIGINS contains an invalid port") from exc
        del port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TECO_ALLOWED_ORIGINS entries must be exact HTTP(S) origins without paths"
            )
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _is_loopback_host(value: str) -> bool:
    selected = value.strip().strip("[]").lower()
    if selected == "localhost":
        return True
    try:
        return ip_address(selected).is_loopback
    except ValueError:
        return False


def _parse_instruments(raw_value: str) -> tuple[tuple[str, str], ...]:
    if not raw_value.strip():
        return ()
    instruments: list[tuple[str, str]] = []
    for raw_instrument in raw_value.split(","):
        instrument = raw_instrument.strip()
        parts = instrument.split(":")
        if len(parts) != 2:
            raise ValueError(
                "TECO_DHAN_INSTRUMENTS entries must use SEGMENT:SECURITY_ID"
            )
        segment, security_id = (part.strip() for part in parts)
        if not _SEGMENT_PATTERN.fullmatch(segment):
            raise ValueError(
                "TECO_DHAN_INSTRUMENTS contains an invalid exchange segment"
            )
        if not security_id.isascii() or not security_id.isdigit() or int(security_id) <= 0:
            raise ValueError(
                "TECO_DHAN_INSTRUMENTS security IDs must be positive integers"
            )
        pair = (segment, security_id)
        if pair not in instruments:
            instruments.append(pair)
    return tuple(instruments)
