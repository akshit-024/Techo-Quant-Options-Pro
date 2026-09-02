"""Fail-closed option-chain analytics bound to verified contract identities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite

from teco_quant.domain.enums import Exchange, MarketKind, OptionType, PricingModel
from teco_quant.domain.models import ContractSpec, MarketState, OptionQuote

from .models import AnalyticsInputError, ExpectedMove, aware
from .pricing import OptionModelResult, expected_move, price_option, year_fraction


@dataclass(frozen=True, slots=True)
class AnalyzedOptionLeg:
    security_id: str
    strike: Decimal
    option_type: OptionType
    expiry: datetime
    implied_volatility: float
    time_to_expiry: float
    pricing_security_id: str
    pricing_underlying: Decimal
    analytics: OptionModelResult
    expected_move: ExpectedMove


def _pricing_identity(
    contract: ContractSpec,
    market: MarketState,
    pricing_security_id: str,
) -> Decimal:
    if contract.pricing_model is PricingModel.BLACK_SCHOLES:
        if (
            contract.market_kind is MarketKind.COMMODITY
            or contract.underlying.exchange not in (Exchange.NSE, Exchange.BSE)
        ):
            raise AnalyticsInputError("Black-Scholes requires an NSE/BSE spot contract")
        if pricing_security_id != contract.underlying.security_id:
            raise AnalyticsInputError(
                "spot pricing security does not match the contract underlying"
            )
        price = market.spot_price
        label = "spot price"
    elif contract.pricing_model is PricingModel.BLACK_76:
        if contract.market_kind is not MarketKind.COMMODITY:
            raise AnalyticsInputError("Black-76 is restricted to commodity futures contracts")
        if contract.underlying.exchange is not Exchange.MCX:
            raise AnalyticsInputError("commodity Black-76 contract must be an MCX contract")
        if contract.futures is None:
            raise AnalyticsInputError("Black-76 contract is missing its exact futures record")
        if contract.futures.instrument.exchange is not Exchange.MCX:
            raise AnalyticsInputError("Black-76 pricing future must be an MCX instrument")
        if pricing_security_id != contract.futures.instrument.security_id:
            raise AnalyticsInputError(
                "futures pricing security does not match the exact contract future"
            )
        price = market.futures_price
        label = "futures price"
    else:
        raise AnalyticsInputError("unsupported pricing model")
    if price is None or not price.is_finite() or price <= 0:
        raise AnalyticsInputError(f"{label} must be finite and positive")
    return price


def analyze_option_chain(
    *,
    contract: ContractSpec,
    market: MarketState,
    quotes: Iterable[OptionQuote],
    as_of: datetime,
    pricing_security_id: str,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
) -> tuple[AnalyzedOptionLeg, ...]:
    """Calculate every chain leg from its own IV and the verified pricing instrument."""

    aware(as_of, name="chain evaluation time")
    if market.observed_at.tzinfo is None or market.observed_at.utcoffset() is None:
        raise AnalyticsInputError("market observation time must be timezone-aware")
    if market.observed_at > as_of:
        raise AnalyticsInputError("market observation cannot be after evaluation time")
    if not isinstance(pricing_security_id, str) or not pricing_security_id.strip():
        raise AnalyticsInputError("pricing security id is required")
    try:
        rates_are_finite = isfinite(float(risk_free_rate)) and isfinite(
            float(dividend_yield)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnalyticsInputError("pricing rates must be numeric") from exc
    if not rates_are_finite:
        raise AnalyticsInputError("pricing rates must be finite")
    underlying = _pricing_identity(contract, market, pricing_security_id)

    known_contracts = {
        (record.instrument.security_id, record.strike, record.option_type, record.expiry)
        for record in contract.option_contracts
    }
    materialized = tuple(quotes)
    for quote in materialized:
        if not isinstance(quote, OptionQuote):
            raise AnalyticsInputError("option chain contains a non-quote value")
        if not isinstance(quote.security_id, str) or not quote.security_id.strip():
            raise AnalyticsInputError("option security id is required")
        if not isinstance(quote.option_type, OptionType):
            raise AnalyticsInputError(f"option {quote.security_id} has an invalid option type")
        if (
            not isinstance(quote.strike, Decimal)
            or not quote.strike.is_finite()
            or quote.strike <= 0
        ):
            raise AnalyticsInputError(f"option {quote.security_id} strike must be positive")
    ordered = sorted(
        materialized,
        key=lambda quote: (quote.strike, quote.option_type.value, quote.security_id),
    )
    seen: set[tuple[Decimal, OptionType]] = set()
    result: list[AnalyzedOptionLeg] = []
    for quote in ordered:
        key = (quote.strike, quote.option_type)
        if key in seen:
            raise AnalyticsInputError(
                f"duplicate option leg for {quote.strike} {quote.option_type.value}"
            )
        seen.add(key)
        if quote.observed_at.tzinfo is None or quote.observed_at.utcoffset() is None:
            raise AnalyticsInputError(f"option {quote.security_id} has a naive observation time")
        if quote.observed_at > as_of:
            raise AnalyticsInputError(f"option {quote.security_id} is from the future")
        if quote.expiry != contract.option_expiry:
            raise AnalyticsInputError(
                f"option {quote.security_id} expiry does not match the contract"
            )
        identity = (quote.security_id, quote.strike, quote.option_type, quote.expiry)
        if identity not in known_contracts:
            raise AnalyticsInputError(
                f"option {quote.security_id} is absent from the verified master"
            )
        volatility = quote.implied_volatility
        try:
            valid_volatility = (
                volatility is not None and isfinite(volatility) and volatility > 0
            )
        except (TypeError, ValueError) as exc:
            raise AnalyticsInputError(
                f"option {quote.security_id} IV must be numeric"
            ) from exc
        if not valid_volatility or volatility is None:
            raise AnalyticsInputError(f"option {quote.security_id} IV must be finite and positive")
        years = year_fraction(as_of, quote.expiry)
        analytics = price_option(
            contract.pricing_model,
            option_type=quote.option_type,
            underlying=underlying,
            strike=quote.strike,
            volatility=volatility,
            time_to_expiry=years,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        result.append(
            AnalyzedOptionLeg(
                security_id=quote.security_id,
                strike=quote.strike,
                option_type=quote.option_type,
                expiry=quote.expiry,
                implied_volatility=volatility,
                time_to_expiry=years,
                pricing_security_id=pricing_security_id,
                pricing_underlying=underlying,
                analytics=analytics,
                expected_move=expected_move(underlying, volatility, years),
            )
        )
    if not result:
        raise AnalyticsInputError("option chain cannot be empty")
    return tuple(result)
