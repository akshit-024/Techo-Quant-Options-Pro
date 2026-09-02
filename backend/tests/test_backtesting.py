from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from teco_quant.backtesting import (
    BacktestConfig,
    BacktestSignal,
    CostConfig,
    ExitReason,
    JournalEvent,
    OptionBar,
    PartialFillConfig,
    ReplayEngine,
    ReplayFrame,
    SlippageConfig,
)

BASE = datetime(2026, 8, 21, 9, 15, tzinfo=UTC)


def bar(
    minute: int,
    *,
    instrument: str = "OPT-1",
    open_: str = "10",
    high: str = "11",
    low: str = "9",
    close: str = "10",
    bid: str = "9.8",
    ask: str = "10",
    bid_quantity: int | None = None,
    ask_quantity: int | None = None,
) -> OptionBar:
    return OptionBar(
        instrument_id=instrument,
        timestamp=BASE + timedelta(minutes=minute),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_quantity=bid_quantity,
        ask_quantity=ask_quantity,
    )


def signal(
    minute: int = 0,
    *,
    signal_id: str = "S-1",
    instrument: str = "OPT-1",
    lots: int = 1,
    lot_size: int = 10,
    expiry_minute: int = 60,
    max_holding: timedelta | None = None,
    score_band: str = "STRONG",
) -> BacktestSignal:
    return BacktestSignal(
        signal_id=signal_id,
        instrument_id=instrument,
        generated_at=BASE + timedelta(minutes=minute),
        option_side="CE",
        lots=lots,
        lot_size=lot_size,
        stop_price=Decimal(8),
        target_price=Decimal(15),
        contract_expiry=BASE + timedelta(minutes=expiry_minute),
        max_holding=max_holding,
        score=88 if score_band == "STRONG" else 78,
        score_band=score_band,
        market="NIFTY",
        expiry_bucket="WEEKLY",
        moneyness="ATM",
        time_bucket="OPEN",
        volatility_bucket="NORMAL",
        regime="TREND",
    )


def frame(
    minute: int,
    *,
    bars: tuple[OptionBar, ...] = (),
    signals: tuple[BacktestSignal, ...] = (),
) -> ReplayFrame:
    return ReplayFrame(
        timestamp=BASE + timedelta(minutes=minute), bars=bars, signals=signals
    )


class ReplayEngineTests(unittest.TestCase):
    def test_signal_uses_next_frame_and_cannot_see_its_entry_bars_ohlc(self) -> None:
        report = ReplayEngine(BacktestConfig(initial_capital=Decimal(100000))).run(
            (
                frame(
                    0,
                    bars=(bar(0, ask="5", bid="4.9", low="4", high="16"),),
                    signals=(signal(),),
                ),
                # This bar hits both thresholds, but it supplies the entry quote and
                # therefore cannot also be used to decide the new position's outcome.
                frame(
                    1,
                    bars=(bar(1, ask="10", bid="9.8", low="7", high="16"),),
                ),
                frame(2, bars=(bar(2, ask="10", bid="9.5", low="9", high="11"),)),
            )
        )

        self.assertEqual(len(report.trades), 1)
        trade = report.trades[0]
        self.assertEqual(trade.entry_time, BASE + timedelta(minutes=1))
        self.assertEqual(trade.entry_price, Decimal(10))
        self.assertIs(trade.exit_reason, ExitReason.END_OF_DATA)

    def test_stop_wins_when_stop_and_target_are_both_touched(self) -> None:
        config = BacktestConfig(
            initial_capital=Decimal(100000),
            slippage=SlippageConfig(
                fixed_entry_per_unit=Decimal("0.1"),
                fixed_exit_per_unit=Decimal("0.2"),
            ),
        )
        report = ReplayEngine(config).run(
            (
                frame(0, signals=(signal(),)),
                frame(1, bars=(bar(1, ask="10", bid="9.8"),)),
                frame(2, bars=(bar(2, low="7", high="16", bid="9", ask="9.2"),)),
            )
        )

        trade = report.trades[0]
        self.assertIs(trade.exit_reason, ExitReason.STOP)
        self.assertEqual(trade.entry_price, Decimal("10.1"))
        self.assertEqual(trade.exit_price, Decimal("8.8"))
        self.assertEqual(trade.slippage_cost, Decimal("3.0"))

    def test_costs_and_required_metrics_are_deterministic(self) -> None:
        config = BacktestConfig(
            initial_capital=Decimal(1000),
            costs=CostConfig(
                brokerage_per_order=Decimal(1),
                entry_tax_rate=Decimal("0.01"),
                exit_tax_rate=Decimal("0.02"),
            ),
        )
        report = ReplayEngine(config).run(
            (
                frame(0, signals=(signal(),)),
                frame(1, bars=(bar(1, ask="10", bid="9.8"),)),
                frame(
                    2,
                    bars=(bar(2, low="10", high="16", bid="12", ask="12.2"),),
                    signals=(
                        signal(2, signal_id="S-2", score_band="TRADABLE"),
                    ),
                ),
                frame(3, bars=(bar(3, ask="10", bid="9.8"),)),
                frame(4, bars=(bar(4, low="7", high="10", bid="8", ask="8.2"),)),
            )
        )

        winner, loser = report.trades
        self.assertEqual(winner.gross_pnl, Decimal(20))
        self.assertEqual(winner.total_costs, Decimal("5.40"))
        self.assertEqual(winner.net_pnl, Decimal("14.60"))
        self.assertEqual(loser.net_pnl, Decimal("-24.60"))

        metrics = report.metrics
        self.assertEqual(metrics.trades, 2)
        self.assertEqual(metrics.win_rate, 0.5)
        self.assertEqual(metrics.gross_pnl, Decimal(0))
        self.assertEqual(metrics.net_pnl, Decimal("-10.00"))
        self.assertEqual(metrics.profit_factor, Decimal("0.5934959349593495934959349593"))
        self.assertEqual(metrics.expectancy, Decimal("-5.00"))
        self.assertEqual(metrics.max_drawdown, Decimal("24.60"))
        self.assertEqual(metrics.average_r, Decimal("-0.25"))
        self.assertIsNotNone(metrics.sharpe_like)
        self.assertEqual(metrics.return_on_capital, Decimal("-0.01"))
        self.assertEqual(metrics.exposure_ratio, 0.5)
        self.assertEqual(metrics.average_holding_seconds, 60.0)
        self.assertEqual(metrics.total_slippage, Decimal(0))
        self.assertEqual(metrics.total_costs, Decimal("10.00"))
        self.assertEqual(report.ending_capital, Decimal("990.00"))

        self.assertEqual(
            set(report.breakdowns),
            {"score_band", "market", "expiry", "moneyness", "time", "volatility", "regime"},
        )
        self.assertEqual(report.breakdowns["score_band"]["STRONG"].trades, 1)
        self.assertEqual(report.breakdowns["market"]["NIFTY"].trades, 2)
        self.assertTrue(any(item.event is JournalEvent.ENTRY for item in report.journal))
        self.assertTrue(any(item.event is JournalEvent.EXIT for item in report.journal))

    def test_expiry_and_maximum_holding_time_close_positions(self) -> None:
        expiry_signal = signal(
            signal_id="EXPIRY", instrument="EXP", expiry_minute=2
        )
        time_signal = signal(
            signal_id="TIME",
            instrument="TIME",
            max_holding=timedelta(minutes=1),
        )
        report = ReplayEngine(BacktestConfig(initial_capital=Decimal(100000))).run(
            (
                frame(0, signals=(expiry_signal, time_signal)),
                frame(
                    1,
                    bars=(bar(1, instrument="EXP"), bar(1, instrument="TIME")),
                ),
                frame(
                    2,
                    bars=(bar(2, instrument="EXP"), bar(2, instrument="TIME")),
                ),
            )
        )
        reasons = {trade.signal_id: trade.exit_reason for trade in report.trades}
        self.assertEqual(
            reasons, {"EXPIRY": ExitReason.EXPIRY, "TIME": ExitReason.TIME}
        )

    def test_partial_fill_behavior_is_opt_in_and_whole_lot_only(self) -> None:
        frames = (
            frame(0, signals=(signal(lots=2),)),
            frame(
                1,
                bars=(bar(1, ask_quantity=10, bid_quantity=10),),
            ),
            frame(
                2,
                bars=(bar(2, low="10", high="16", bid="12", ask="12.2", bid_quantity=10),),
            ),
        )
        normal = ReplayEngine(BacktestConfig(initial_capital=Decimal(100000))).run(
            frames
        )
        self.assertEqual(normal.trades[0].quantity, 20)

        partial = ReplayEngine(
            BacktestConfig(
                initial_capital=Decimal(100000),
                partial_fills=PartialFillConfig(enabled=True),
            )
        ).run(frames)
        self.assertEqual(partial.trades[0].quantity, 10)
        entry = next(item for item in partial.journal if item.event is JournalEvent.ENTRY)
        self.assertIn("partial", entry.message)

    def test_replay_rejects_unsorted_or_duplicate_timestamps(self) -> None:
        engine = ReplayEngine(BacktestConfig(initial_capital=Decimal(100000)))
        with self.assertRaisesRegex(ValueError, "strictly sorted and unique"):
            engine.run((frame(1), frame(0)))
        with self.assertRaisesRegex(ValueError, "strictly sorted and unique"):
            engine.run((frame(0), frame(0)))

    def test_signal_cannot_be_attached_to_a_frame_from_another_instant(self) -> None:
        with self.assertRaisesRegex(ValueError, "where it became available"):
            ReplayFrame(timestamp=BASE, signals=(signal(1),))


if __name__ == "__main__":
    unittest.main()
