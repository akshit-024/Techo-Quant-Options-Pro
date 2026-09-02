"""Versioned canonical strategy policy for TECO Quant Pro Sprint 1.

This module deliberately contains only deterministic, pure policy. Provider payloads and
Excel cell locations are not allowed here. Sprint 2 will calculate the individual evidence
factors that feed :func:`weighted_score`; this file defines how those factors are combined
and how a candidate becomes a final decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from math import isfinite, sqrt

from teco_quant.domain.enums import (
    DecisionReason,
    DecisionState,
    OperatingMode,
    OptionType,
    ScoreBand,
    TradingStyle,
)

STRATEGY_VERSION = "teco-canonical-1.0.0"
HARD_MAXIMUM_RISK_PER_TRADE = 0.02


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    trend: float = 20.0
    premium: float = 15.0
    open_interest: float = 15.0
    liquidity: float = 15.0
    greeks: float = 15.0
    volatility: float = 10.0
    risk_reward: float = 10.0

    def as_mapping(self) -> Mapping[str, float]:
        return {
            "trend": self.trend,
            "premium": self.premium,
            "open_interest": self.open_interest,
            "liquidity": self.liquidity,
            "greeks": self.greeks,
            "volatility": self.volatility,
            "risk_reward": self.risk_reward,
        }

    def __post_init__(self) -> None:
        values = self.as_mapping()
        if any(not isfinite(value) for value in values.values()):
            raise ValueError("score weights must be finite")
        if any(value < 0 for value in values.values()):
            raise ValueError("score weights cannot be negative")
        if abs(sum(values.values()) - 100.0) > 1e-9:
            raise ValueError("score weights must sum to 100")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    version: str = STRATEGY_VERSION
    risk_free_rate: float = 0.06
    dividend_yield: float = 0.0
    maximum_risk_per_trade: float = 0.02
    intraday_delta_minimum: float = 0.45
    intraday_delta_maximum: float = 0.70
    positional_delta_minimum: float = 0.55
    positional_delta_maximum: float = 0.75
    minimum_volume: int = 1_000
    minimum_open_interest: int = 1_000
    maximum_spread_ratio: float = 0.03
    minimum_liquidity_score: float = 60.0
    watchlist_score: float = 65.0
    minimum_tradable_score: float = 75.0
    strong_setup_score: float = 85.0
    conflict_gap: float = 8.0
    live_max_age_seconds: float = 30.0
    component_skew_seconds: float = 5.0
    future_clock_skew_seconds: float = 2.0
    instrument_master_max_age_seconds: float = 36 * 60 * 60
    extreme_expiry_seconds: float = 9 * 60 * 60
    premium_stop_ratio: float = 0.20
    atr_stop_multiple: float = 1.0
    target_r_multiples: tuple[float, float, float] = (1.0, 2.0, 3.0)
    event_risk_blocks_new_trades: bool = True
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def delta_range(self, trading_style: TradingStyle) -> tuple[float, float]:
        if trading_style is TradingStyle.POSITIONAL:
            return (self.positional_delta_minimum, self.positional_delta_maximum)
        return (self.intraday_delta_minimum, self.intraday_delta_maximum)

    def __post_init__(self) -> None:
        numeric_fields = {
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "maximum_risk_per_trade": self.maximum_risk_per_trade,
            "intraday_delta_minimum": self.intraday_delta_minimum,
            "intraday_delta_maximum": self.intraday_delta_maximum,
            "positional_delta_minimum": self.positional_delta_minimum,
            "positional_delta_maximum": self.positional_delta_maximum,
            "minimum_volume": self.minimum_volume,
            "minimum_open_interest": self.minimum_open_interest,
            "maximum_spread_ratio": self.maximum_spread_ratio,
            "minimum_liquidity_score": self.minimum_liquidity_score,
            "watchlist_score": self.watchlist_score,
            "minimum_tradable_score": self.minimum_tradable_score,
            "strong_setup_score": self.strong_setup_score,
            "conflict_gap": self.conflict_gap,
            "live_max_age_seconds": self.live_max_age_seconds,
            "component_skew_seconds": self.component_skew_seconds,
            "future_clock_skew_seconds": self.future_clock_skew_seconds,
            "instrument_master_max_age_seconds": self.instrument_master_max_age_seconds,
            "extreme_expiry_seconds": self.extreme_expiry_seconds,
            "premium_stop_ratio": self.premium_stop_ratio,
            "atr_stop_multiple": self.atr_stop_multiple,
        }
        non_finite = [
            name for name, value in numeric_fields.items() if not isfinite(value)
        ]
        if non_finite:
            raise ValueError(
                f"strategy configuration values must be finite: {', '.join(non_finite)}"
            )
        if not -1 < self.risk_free_rate <= 1:
            raise ValueError("risk-free rate must be within (-1, 1]")
        if not 0 <= self.dividend_yield <= 1:
            raise ValueError("dividend yield must be within [0, 1]")
        if any(not isfinite(value) for value in self.target_r_multiples):
            raise ValueError("target R multiples must be finite")
        rate_fields = (self.maximum_spread_ratio, self.premium_stop_ratio)
        if any(value <= 0 or value > 1 for value in rate_fields):
            raise ValueError("configured rates must be within (0, 1]")
        if not 0 < self.maximum_risk_per_trade <= HARD_MAXIMUM_RISK_PER_TRADE:
            raise ValueError("maximum risk per trade cannot exceed the hard 2% ceiling")
        if not (
            0 <= self.watchlist_score
            < self.minimum_tradable_score
            <= self.strong_setup_score
            <= 100
        ):
            raise ValueError("score thresholds must be ordered within 0..100")
        if self.conflict_gap < 0:
            raise ValueError("conflict gap cannot be negative")
        for name, minimum, maximum in (
            (
                "intraday delta",
                self.intraday_delta_minimum,
                self.intraday_delta_maximum,
            ),
            (
                "positional delta",
                self.positional_delta_minimum,
                self.positional_delta_maximum,
            ),
        ):
            if not 0 < minimum <= maximum <= 1:
                raise ValueError(f"{name} range must satisfy 0 < minimum <= maximum <= 1")
        if self.minimum_volume <= 0 or self.minimum_open_interest <= 0:
            raise ValueError("minimum volume and open interest must be positive")
        if not 0 <= self.minimum_liquidity_score <= 100:
            raise ValueError("minimum liquidity score must be within 0..100")
        if not 0 <= self.conflict_gap <= 100:
            raise ValueError("conflict gap must be within 0..100")
        if self.live_max_age_seconds <= 0:
            raise ValueError("live maximum age must be positive")
        if self.component_skew_seconds < 0 or self.future_clock_skew_seconds < 0:
            raise ValueError("clock-skew tolerances cannot be negative")
        if self.instrument_master_max_age_seconds <= 0:
            raise ValueError("instrument-master maximum age must be positive")
        if self.extreme_expiry_seconds <= 0:
            raise ValueError("extreme-expiry window must be positive")
        if self.atr_stop_multiple <= 0:
            raise ValueError("ATR stop multiple must be positive")
        if not self.target_r_multiples or any(
            value <= 0 for value in self.target_r_multiples
        ):
            raise ValueError("target R multiples must be positive")
        if any(
            left >= right
            for left, right in zip(
                self.target_r_multiples, self.target_r_multiples[1:]
            )
        ):
            raise ValueError("target R multiples must be strictly increasing")


DEFAULT_STRATEGY_CONFIG = StrategyConfig()


@dataclass(frozen=True, slots=True)
class ScoreResult:
    total: float
    points: Mapping[str, float]
    band: ScoreBand


def score_band(score: float, config: StrategyConfig = DEFAULT_STRATEGY_CONFIG) -> ScoreBand:
    if score >= config.strong_setup_score:
        return ScoreBand.STRONG
    if score >= config.minimum_tradable_score:
        return ScoreBand.TRADABLE
    if score >= config.watchlist_score:
        return ScoreBand.WATCHLIST
    return ScoreBand.REJECTED


def weighted_score(
    factors: Mapping[str, float], config: StrategyConfig = DEFAULT_STRATEGY_CONFIG
) -> ScoreResult:
    """Combine normalized 0..1 evidence factors using the versioned weights.

    Missing, non-finite, or out-of-range factors are rejected rather than silently assigned
    a favorable value. This prevents the workbook's former baseline-score behavior from
    turning incomplete data into a recommendation.
    """

    points: dict[str, float] = {}
    weights = config.weights.as_mapping()
    missing = sorted(set(weights) - set(factors))
    if missing:
        raise ValueError(f"missing score factors: {', '.join(missing)}")
    for name, weight in weights.items():
        factor = factors[name]
        if not 0.0 <= factor <= 1.0:
            raise ValueError(f"score factor {name!r} must be within 0..1")
        points[name] = factor * weight
    total = min(100.0, max(0.0, sum(points.values())))
    return ScoreResult(total=total, points=points, band=score_band(total, config))


@dataclass(frozen=True, slots=True)
class LiquidityResult:
    score: float
    eligible: bool
    rejection_reasons: tuple[str, ...]


def liquidity_score(
    *,
    bid: Decimal | None,
    ask: Decimal | None,
    volume: int | None,
    open_interest: int | None,
    config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
) -> LiquidityResult:
    """Score CE or PE independently using identical rules.

    Spread contributes 50 points and volume/OI contribute 25 each. Hard thresholds are
    evaluated separately, so a high value in one field cannot hide a missing or invalid one.
    """

    reasons: list[str] = []
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        reasons.append("MISSING_OR_NON_POSITIVE_MARKET")
    elif ask < bid:
        reasons.append("ASK_BELOW_BID")

    spread_ratio = 1.0
    if bid is not None and ask is not None and ask > 0:
        spread_ratio = float((ask - bid) / ask)
        if spread_ratio < 0:
            spread_ratio = 1.0
        if spread_ratio > config.maximum_spread_ratio:
            reasons.append("WIDE_SPREAD")

    if volume is None or volume < config.minimum_volume:
        reasons.append("LOW_VOLUME")
    if open_interest is None or open_interest < config.minimum_open_interest:
        reasons.append("LOW_OPEN_INTEREST")

    spread_points = 50.0 * max(
        0.0, 1.0 - spread_ratio / config.maximum_spread_ratio
    )
    volume_points = 25.0 * min(1.0, max(0, volume or 0) / config.minimum_volume)
    oi_points = 25.0 * min(
        1.0, max(0, open_interest or 0) / config.minimum_open_interest
    )
    score = max(0.0, min(100.0, spread_points + volume_points + oi_points))
    eligible = not reasons and score >= config.minimum_liquidity_score
    if not reasons and not eligible:
        reasons.append("LIQUIDITY_SCORE_BELOW_MINIMUM")
    return LiquidityResult(score=score, eligible=eligible, rejection_reasons=tuple(reasons))


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    data_complete: bool
    data_stale: bool
    expiry_valid: bool
    extreme_expiry_risk: bool
    event_risk_active: bool | None
    liquid_strike_available: bool
    affordable: bool
    call_score: float
    put_score: float
    price_action_confirmed: bool | None
    operating_mode: OperatingMode = OperatingMode.PRO


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    state: DecisionState
    reason: DecisionReason
    call_band: ScoreBand
    put_band: ScoreBand
    leading_score: float
    score_gap: float


def resolve_decision(
    inputs: DecisionInputs, config: StrategyConfig = DEFAULT_STRATEGY_CONFIG
) -> DecisionOutcome:
    """Apply the documented fail-closed decision precedence.

    A score never bypasses data, expiry, liquidity, event, affordability, or manual price
    confirmation gates.
    """

    if not 0 <= inputs.call_score <= 100 or not 0 <= inputs.put_score <= 100:
        raise ValueError("call and put scores must be within 0..100")
    leading = max(inputs.call_score, inputs.put_score)
    gap = abs(inputs.call_score - inputs.put_score)
    call_band = score_band(inputs.call_score, config)
    put_band = score_band(inputs.put_score, config)

    def outcome(state: DecisionState, reason: DecisionReason) -> DecisionOutcome:
        return DecisionOutcome(
            state=state,
            reason=reason,
            call_band=call_band,
            put_band=put_band,
            leading_score=leading,
            score_gap=gap,
        )

    if not inputs.data_complete:
        return outcome(DecisionState.INSUFFICIENT_DATA, DecisionReason.DATA_INCOMPLETE)
    if inputs.data_stale:
        return outcome(DecisionState.NO_TRADE, DecisionReason.DATA_STALE)
    if not inputs.expiry_valid:
        return outcome(DecisionState.NO_TRADE, DecisionReason.INVALID_EXPIRY)
    if inputs.extreme_expiry_risk:
        return outcome(DecisionState.NO_TRADE, DecisionReason.EXTREME_EXPIRY_RISK)
    if inputs.event_risk_active is not False and config.event_risk_blocks_new_trades:
        return outcome(DecisionState.NO_TRADE, DecisionReason.EVENT_RISK)
    if not inputs.liquid_strike_available:
        return outcome(DecisionState.NO_TRADE, DecisionReason.NO_LIQUID_STRIKE)
    if not inputs.affordable:
        return outcome(DecisionState.NO_TRADE, DecisionReason.NOT_AFFORDABLE)
    if leading < config.watchlist_score:
        return outcome(DecisionState.NO_TRADE, DecisionReason.BELOW_WATCHLIST_SCORE)
    if leading < config.minimum_tradable_score:
        return outcome(DecisionState.WAIT, DecisionReason.WATCHLIST_ONLY)
    if gap < config.conflict_gap:
        return outcome(DecisionState.WAIT, DecisionReason.CONFLICTING_SCORES)
    if inputs.operating_mode is OperatingMode.QUICK:
        return outcome(DecisionState.WAIT, DecisionReason.QUICK_MODE_ONLY)
    if inputs.price_action_confirmed is not True:
        return outcome(DecisionState.WAIT, DecisionReason.PRICE_ACTION_PENDING)
    if inputs.call_score > inputs.put_score:
        return outcome(DecisionState.BUY_CALL, DecisionReason.BULLISH_CONFIRMED)
    return outcome(DecisionState.BUY_PUT, DecisionReason.BEARISH_CONFIRMED)


def confirm_price_action(
    *,
    option_type: OptionType,
    underlying_price: Decimal,
    signal_candle_high: Decimal,
    signal_candle_low: Decimal,
) -> bool:
    """Confirm direction using the underlying, never the option premium."""

    if underlying_price <= 0 or signal_candle_high <= 0 or signal_candle_low <= 0:
        raise ValueError("price-action inputs must be positive")
    if signal_candle_high < signal_candle_low:
        raise ValueError("signal-candle high cannot be below its low")
    if option_type is OptionType.CALL:
        return underlying_price > signal_candle_high
    return underlying_price < signal_candle_low


def midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    if bid <= 0 or ask <= 0 or ask < bid:
        raise ValueError("a midpoint requires positive bid <= ask")
    return (bid + ask) / Decimal(2)


def atm_straddle(call_ltp: Decimal, put_ltp: Decimal) -> Decimal:
    if call_ltp <= 0 or put_ltp <= 0:
        raise ValueError("ATM LTP values must be positive")
    return call_ltp + put_ltp


def executable_straddle(call_ask: Decimal, put_ask: Decimal) -> Decimal:
    if call_ask <= 0 or put_ask <= 0:
        raise ValueError("ATM ask values must be positive")
    return call_ask + put_ask


def time_to_expiry_years(now: datetime, expiry: datetime) -> float:
    if now.tzinfo is None or expiry.tzinfo is None:
        raise ValueError("expiry calculations require timezone-aware datetimes")
    return max(0.0, (expiry - now).total_seconds() / (365.0 * 24.0 * 60.0 * 60.0))


def iv_expected_move(underlying: Decimal, iv_decimal: float, years: float) -> Decimal:
    if underlying <= 0 or iv_decimal <= 0 or years <= 0:
        raise ValueError("underlying, IV, and time must be positive")
    return underlying * Decimal(str(iv_decimal * sqrt(years)))


def expected_bounds(underlying: Decimal, expected_move: Decimal) -> tuple[Decimal, Decimal]:
    if underlying <= 0 or expected_move < 0:
        raise ValueError("underlying must be positive and expected move non-negative")
    return (underlying - expected_move, underlying + expected_move)


def synthetic_futures(
    atm_strike: Decimal, call_price: Decimal, put_price: Decimal
) -> Decimal:
    """Parity cross-check using call and put prices from the same timestamp/basis."""

    if atm_strike <= 0 or call_price <= 0 or put_price <= 0:
        raise ValueError("synthetic-futures inputs must be positive")
    return atm_strike + call_price - put_price


def nearest_atm(underlying: Decimal, listed_strikes: Sequence[Decimal]) -> Decimal:
    if underlying <= 0 or not listed_strikes:
        raise ValueError("underlying and at least one listed strike are required")
    unique = sorted(set(listed_strikes))
    return min(unique, key=lambda strike: (abs(strike - underlying), strike))


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    maximum_risk: Decimal
    risk_per_lot: Decimal
    premium_per_lot: Decimal
    lots_by_risk: int
    lots_by_allocation: int
    maximum_lots: int
    quantity: int
    affordable: bool


def calculate_position_size(
    *,
    account_capital: Decimal,
    risk_rate: float,
    maximum_premium_allocation: float,
    entry: Decimal,
    stop: Decimal,
    lot_size: int,
    estimated_round_trip_cost_per_lot: Decimal = Decimal(0),
    config: StrategyConfig = DEFAULT_STRATEGY_CONFIG,
) -> PositionSizeResult:
    """Calculate a conservative long-option size, including estimated costs in risk."""

    if not all(
        value.is_finite()
        for value in (
            account_capital,
            entry,
            stop,
            estimated_round_trip_cost_per_lot,
        )
    ):
        raise ValueError("position-sizing decimal inputs must be finite")
    if account_capital <= 0 or entry <= 0 or lot_size <= 0:
        raise ValueError("capital, entry, and lot size must be positive")
    if not isfinite(risk_rate) or not isfinite(maximum_premium_allocation):
        raise ValueError("risk and allocation rates must be finite")
    if not 0 < risk_rate <= config.maximum_risk_per_trade:
        raise ValueError(
            "risk rate must be positive and within the configured maximum risk per trade"
        )
    if not 0 < maximum_premium_allocation <= 1:
        raise ValueError("premium allocation rate must be within (0, 1]")
    if stop < 0 or stop >= entry:
        raise ValueError("a long-option stop must be within [0, entry)")
    if estimated_round_trip_cost_per_lot < 0:
        raise ValueError("estimated costs cannot be negative")

    maximum_risk = account_capital * Decimal(str(risk_rate))
    risk_per_lot = (entry - stop) * lot_size + estimated_round_trip_cost_per_lot
    premium_per_lot = entry * lot_size
    allocation = account_capital * Decimal(str(maximum_premium_allocation))
    lots_by_risk = int((maximum_risk / risk_per_lot).to_integral_value(rounding=ROUND_FLOOR))
    lots_by_allocation = int(
        (allocation / premium_per_lot).to_integral_value(rounding=ROUND_FLOOR)
    )
    maximum_lots = max(0, min(lots_by_risk, lots_by_allocation))
    return PositionSizeResult(
        maximum_risk=maximum_risk,
        risk_per_lot=risk_per_lot,
        premium_per_lot=premium_per_lot,
        lots_by_risk=lots_by_risk,
        lots_by_allocation=lots_by_allocation,
        maximum_lots=maximum_lots,
        quantity=maximum_lots * lot_size,
        affordable=maximum_lots > 0,
    )
