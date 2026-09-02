"""Pure report-metric aggregation for backtest trades."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from math import sqrt
from statistics import stdev

from teco_quant.backtesting.models import BacktestMetrics, BreakdownMetrics, Trade

ZERO = Decimal(0)


def _trade_breakdown(trades: Sequence[Trade]) -> BreakdownMetrics:
    count = len(trades)
    wins = sum(trade.net_pnl > ZERO for trade in trades)
    return BreakdownMetrics(
        trades=count,
        wins=wins,
        win_rate=wins / count if count else 0.0,
        gross_pnl=sum((trade.gross_pnl for trade in trades), ZERO),
        net_pnl=sum((trade.net_pnl for trade in trades), ZERO),
        expectancy=(sum((trade.net_pnl for trade in trades), ZERO) / count)
        if count
        else ZERO,
        average_r=(sum((trade.r_multiple for trade in trades), ZERO) / count)
        if count
        else ZERO,
    )


def build_breakdowns(trades: Sequence[Trade]) -> dict[str, dict[str, BreakdownMetrics]]:
    dimensions: dict[str, Callable[[Trade], str]] = {
        "score_band": lambda trade: trade.score_band,
        "market": lambda trade: trade.market,
        "expiry": lambda trade: trade.expiry_bucket,
        "moneyness": lambda trade: trade.moneyness,
        "time": lambda trade: trade.time_bucket,
        "volatility": lambda trade: trade.volatility_bucket,
        "regime": lambda trade: trade.regime,
    }
    result: dict[str, dict[str, BreakdownMetrics]] = {}
    for dimension, selector in dimensions.items():
        groups: dict[str, list[Trade]] = {}
        for trade in trades:
            groups.setdefault(selector(trade), []).append(trade)
        result[dimension] = {
            label: _trade_breakdown(group) for label, group in sorted(groups.items())
        }
    return result


def _maximum_drawdown(initial_capital: Decimal, trades: Sequence[Trade]) -> Decimal:
    equity = initial_capital
    peak = initial_capital
    maximum = ZERO
    for trade in sorted(trades, key=lambda item: (item.exit_time, item.signal_id)):
        equity += trade.net_pnl
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _exposure_ratio(
    started_at: datetime, ended_at: datetime, trades: Sequence[Trade]
) -> float:
    total_seconds = (ended_at - started_at).total_seconds()
    if total_seconds <= 0 or not trades:
        return 0.0
    intervals = sorted((trade.entry_time, trade.exit_time) for trade in trades)
    covered = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            covered += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    covered += (current_end - current_start).total_seconds()
    return min(1.0, max(0.0, covered / total_seconds))


def calculate_metrics(
    *,
    initial_capital: Decimal,
    started_at: datetime,
    ended_at: datetime,
    trades: Sequence[Trade],
) -> BacktestMetrics:
    count = len(trades)
    wins = sum(trade.net_pnl > ZERO for trade in trades)
    losses = sum(trade.net_pnl < ZERO for trade in trades)
    gross_pnl = sum((trade.gross_pnl for trade in trades), ZERO)
    net_pnl = sum((trade.net_pnl for trade in trades), ZERO)
    gross_profit = sum((trade.net_pnl for trade in trades if trade.net_pnl > ZERO), ZERO)
    gross_loss = sum((trade.net_pnl for trade in trades if trade.net_pnl < ZERO), ZERO)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < ZERO else None
    expectancy = net_pnl / count if count else ZERO
    average_r = (
        sum((trade.r_multiple for trade in trades), ZERO) / count if count else ZERO
    )
    returns = [float(trade.net_pnl / initial_capital) for trade in trades]
    sharpe_like: float | None = None
    if len(returns) >= 2:
        dispersion = stdev(returns)
        if dispersion > 0:
            sharpe_like = (sum(returns) / len(returns)) / dispersion * sqrt(len(returns))
    average_holding_seconds = (
        sum(trade.holding_seconds for trade in trades) / count if count else 0.0
    )
    return BacktestMetrics(
        trades=count,
        wins=wins,
        losses=losses,
        win_rate=wins / count if count else 0.0,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_drawdown=_maximum_drawdown(initial_capital, trades),
        average_r=average_r,
        sharpe_like=sharpe_like,
        return_on_capital=net_pnl / initial_capital,
        exposure_ratio=_exposure_ratio(started_at, ended_at, trades),
        average_holding_seconds=average_holding_seconds,
        total_slippage=sum((trade.slippage_cost for trade in trades), ZERO),
        total_costs=sum((trade.total_costs for trade in trades), ZERO),
    )
