"""Public deterministic analytics API for TECO Quant."""

from .chain import AnalyzedOptionLeg, analyze_option_chain
from .indicators import (
    atr,
    ema,
    hourly_confirmation,
    resample_completed_candles,
    true_range,
    vwap,
    wilder_rsi,
    wma,
)
from .models import (
    AnalyticsInputError,
    Candle,
    ExpectedMove,
    HourlyConfirmation,
    TrendDirection,
)
from .pricing import (
    OptionModelResult,
    black_76,
    black_scholes,
    expected_move,
    price_option,
    year_fraction,
)

__all__ = [
    "AnalyticsInputError",
    "AnalyzedOptionLeg",
    "Candle",
    "ExpectedMove",
    "HourlyConfirmation",
    "OptionModelResult",
    "TrendDirection",
    "analyze_option_chain",
    "atr",
    "black_76",
    "black_scholes",
    "ema",
    "expected_move",
    "hourly_confirmation",
    "price_option",
    "resample_completed_candles",
    "true_range",
    "vwap",
    "wilder_rsi",
    "wma",
    "year_fraction",
]
