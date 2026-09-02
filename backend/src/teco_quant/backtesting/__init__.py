"""Deterministic point-in-time backtesting for TECO Quant option signals."""

from teco_quant.backtesting.engine import ReplayEngine
from teco_quant.backtesting.models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestReport,
    BacktestSignal,
    BreakdownMetrics,
    CostBreakdown,
    CostConfig,
    ExitReason,
    JournalEntry,
    JournalEvent,
    OpenPositionSummary,
    OptionBar,
    PartialFillConfig,
    ReplayFrame,
    SlippageConfig,
    Trade,
)

__all__ = [
    "BacktestConfig",
    "BacktestMetrics",
    "BacktestReport",
    "BacktestSignal",
    "BreakdownMetrics",
    "CostBreakdown",
    "CostConfig",
    "ExitReason",
    "JournalEntry",
    "JournalEvent",
    "OpenPositionSummary",
    "OptionBar",
    "PartialFillConfig",
    "ReplayEngine",
    "ReplayFrame",
    "SlippageConfig",
    "Trade",
]
