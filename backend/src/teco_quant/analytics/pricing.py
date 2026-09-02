"""European option analytics for spot and futures under continuous compounding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import erf, exp, isfinite, log, pi, sqrt

from teco_quant.domain.enums import OptionType, PricingModel

from .models import AnalyticsInputError, ExpectedMove, aware, decimal_value


@dataclass(frozen=True, slots=True)
class OptionModelResult:
    """Model output with explicit sensitivity units.

    ``theta_per_day`` is calendar-day decay and ``vega_per_vol_point`` is the
    price change for one percentage-point change in volatility (for example,
    20% to 21%).
    """

    model: PricingModel
    option_type: OptionType
    price: Decimal
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_vol_point: float


def _finite_float(value: float | Decimal, *, name: str) -> float:
    if isinstance(value, bool):
        raise AnalyticsInputError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnalyticsInputError(f"{name} must be numeric") from exc
    if not isfinite(result):
        raise AnalyticsInputError(f"{name} must be finite")
    return result


def _positive_decimal(value: Decimal | float | str, *, name: str) -> Decimal:
    result = decimal_value(value, name=name)
    if result <= 0:
        raise AnalyticsInputError(f"{name} must be positive")
    return result


def _positive_float(value: float | Decimal, *, name: str) -> float:
    result = _finite_float(value, name=name)
    if result <= 0:
        raise AnalyticsInputError(f"{name} must be positive")
    return result


def _normal_pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2.0 * pi)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _money(value: float) -> Decimal:
    if not isfinite(value):
        raise AnalyticsInputError("pricing model produced a non-finite value")
    # A model computed in binary floating point should not manufacture additional
    # binary digits at the domain's Decimal money boundary.
    return Decimal(str(max(0.0, value)))


def _result(
    *,
    model: PricingModel,
    option_type: OptionType,
    price: float,
    delta: float,
    gamma: float,
    theta_per_day: float,
    vega_per_vol_point: float,
) -> OptionModelResult:
    values = (delta, gamma, theta_per_day, vega_per_vol_point)
    if not all(isfinite(value) for value in values):
        raise AnalyticsInputError("pricing model produced non-finite Greeks")
    return OptionModelResult(
        model=model,
        option_type=option_type,
        price=_money(price),
        delta=delta,
        gamma=gamma,
        theta_per_day=theta_per_day,
        vega_per_vol_point=vega_per_vol_point,
    )


def year_fraction(
    observed_at: datetime, expiry: datetime, *, basis_days: float = 365.0
) -> float:
    """Return an exact-second expiry fraction on the requested day-count basis."""

    aware(observed_at, name="pricing observation time")
    aware(expiry, name="option expiry")
    basis = _positive_float(basis_days, name="day-count basis")
    seconds = (expiry - observed_at).total_seconds()
    if not isfinite(seconds) or seconds <= 0:
        raise AnalyticsInputError("option expiry must be after the observation time")
    return seconds / (basis * 24.0 * 60.0 * 60.0)


def black_scholes(
    *,
    option_type: OptionType,
    spot: Decimal,
    strike: Decimal,
    volatility: float,
    time_to_expiry: float,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
) -> OptionModelResult:
    """Price a European spot option and return complete first-order analytics."""

    if not isinstance(option_type, OptionType):
        raise AnalyticsInputError("option type must be CE or PE")
    spot_decimal = _positive_decimal(spot, name="spot")
    strike_decimal = _positive_decimal(strike, name="strike")
    sigma = _positive_float(volatility, name="volatility")
    years = _positive_float(time_to_expiry, name="time to expiry")
    rate = _finite_float(risk_free_rate, name="risk-free rate")
    carry = _finite_float(dividend_yield, name="dividend yield")
    spot_float = float(spot_decimal)
    strike_float = float(strike_decimal)
    try:
        root_time = sqrt(years)
        d1 = (
            log(spot_float / strike_float) + (rate - carry + 0.5 * sigma * sigma) * years
        ) / (sigma * root_time)
        d2 = d1 - sigma * root_time
        spot_discount = exp(-carry * years)
        strike_discount = exp(-rate * years)
        density = _normal_pdf(d1)
        gamma = spot_discount * density / (spot_float * sigma * root_time)
        vega = spot_float * spot_discount * density * root_time / 100.0
        common_theta = -spot_float * spot_discount * density * sigma / (2.0 * root_time)
        if option_type is OptionType.CALL:
            price = (
                spot_float * spot_discount * _normal_cdf(d1)
                - strike_float * strike_discount * _normal_cdf(d2)
            )
            delta = spot_discount * _normal_cdf(d1)
            annual_theta = (
                common_theta
                - rate * strike_float * strike_discount * _normal_cdf(d2)
                + carry * spot_float * spot_discount * _normal_cdf(d1)
            )
        else:
            price = (
                strike_float * strike_discount * _normal_cdf(-d2)
                - spot_float * spot_discount * _normal_cdf(-d1)
            )
            delta = spot_discount * (_normal_cdf(d1) - 1.0)
            annual_theta = (
                common_theta
                + rate * strike_float * strike_discount * _normal_cdf(-d2)
                - carry * spot_float * spot_discount * _normal_cdf(-d1)
            )
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise AnalyticsInputError("Black-Scholes inputs are outside the numeric domain") from exc
    return _result(
        model=PricingModel.BLACK_SCHOLES,
        option_type=option_type,
        price=price,
        delta=delta,
        gamma=gamma,
        theta_per_day=annual_theta / 365.0,
        vega_per_vol_point=vega,
    )


def black_76(
    *,
    option_type: OptionType,
    futures: Decimal,
    strike: Decimal,
    volatility: float,
    time_to_expiry: float,
    risk_free_rate: float,
) -> OptionModelResult:
    """Price a European option on its exact futures contract under Black-76."""

    if not isinstance(option_type, OptionType):
        raise AnalyticsInputError("option type must be CE or PE")
    futures_decimal = _positive_decimal(futures, name="futures price")
    strike_decimal = _positive_decimal(strike, name="strike")
    sigma = _positive_float(volatility, name="volatility")
    years = _positive_float(time_to_expiry, name="time to expiry")
    rate = _finite_float(risk_free_rate, name="risk-free rate")
    futures_float = float(futures_decimal)
    strike_float = float(strike_decimal)
    try:
        root_time = sqrt(years)
        d1 = (
            log(futures_float / strike_float) + 0.5 * sigma * sigma * years
        ) / (sigma * root_time)
        d2 = d1 - sigma * root_time
        discount = exp(-rate * years)
        density = _normal_pdf(d1)
        gamma = discount * density / (futures_float * sigma * root_time)
        vega = discount * futures_float * density * root_time / 100.0
        if option_type is OptionType.CALL:
            price = discount * (
                futures_float * _normal_cdf(d1) - strike_float * _normal_cdf(d2)
            )
            delta = discount * _normal_cdf(d1)
        else:
            price = discount * (
                strike_float * _normal_cdf(-d2) - futures_float * _normal_cdf(-d1)
            )
            delta = -discount * _normal_cdf(-d1)
        annual_theta = (
            rate * price
            - discount * futures_float * density * sigma / (2.0 * root_time)
        )
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise AnalyticsInputError("Black-76 inputs are outside the numeric domain") from exc
    return _result(
        model=PricingModel.BLACK_76,
        option_type=option_type,
        price=price,
        delta=delta,
        gamma=gamma,
        theta_per_day=annual_theta / 365.0,
        vega_per_vol_point=vega,
    )


def price_option(
    model: PricingModel,
    *,
    option_type: OptionType,
    underlying: Decimal,
    strike: Decimal,
    volatility: float,
    time_to_expiry: float,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
) -> OptionModelResult:
    """Dispatch to the contract's explicit model; never infer it from the symbol."""

    if model is PricingModel.BLACK_SCHOLES:
        return black_scholes(
            option_type=option_type,
            spot=underlying,
            strike=strike,
            volatility=volatility,
            time_to_expiry=time_to_expiry,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
    if model is PricingModel.BLACK_76:
        if _finite_float(dividend_yield, name="dividend yield") != 0.0:
            raise AnalyticsInputError("dividend yield is not an input to Black-76")
        return black_76(
            option_type=option_type,
            futures=underlying,
            strike=strike,
            volatility=volatility,
            time_to_expiry=time_to_expiry,
            risk_free_rate=risk_free_rate,
        )
    raise AnalyticsInputError("unsupported pricing model")


def expected_move(
    underlying: Decimal,
    volatility: float,
    time_to_expiry: float,
) -> ExpectedMove:
    """Return one-standard-deviation move and non-negative symmetric bounds."""

    underlying_decimal = _positive_decimal(underlying, name="underlying")
    sigma = _positive_float(volatility, name="volatility")
    years = _positive_float(time_to_expiry, name="time to expiry")
    calculated = float(underlying_decimal) * sigma * sqrt(years)
    move = _money(calculated)
    return ExpectedMove(
        underlying=underlying_decimal,
        move=move,
        lower_bound=max(Decimal(0), underlying_decimal - move),
        upper_bound=underlying_decimal + move,
    )
