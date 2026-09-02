from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from teco_quant.domain.enums import (
    DataSource,
    Exchange,
    MarketKind,
    OperatingMode,
    OptionType,
    PricingModel,
    TradingStyle,
)
from teco_quant.domain.models import (
    AtomicSnapshot,
    ContractSpec,
    Greeks,
    InstrumentId,
    InstrumentMasterProvenance,
    InstrumentMasterRecord,
    MarketState,
    OptionQuote,
    StrategyContext,
    TechnicalState,
)
from teco_quant.strategy.spec import STRATEGY_VERSION

NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
MASTER_HASH = "f" * 64


def valid_master() -> InstrumentMasterProvenance:
    return InstrumentMasterProvenance(
        batch_id=f"DHAN:{MASTER_HASH[:16]}",
        provider="DHAN",
        source_url="https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        content_hash=MASTER_HASH,
        schema_version="dhan-detailed-v1",
        fetched_at=NOW - timedelta(days=1),
        row_count=12,
    )


def valid_master_records() -> tuple[InstrumentMasterRecord, ...]:
    option_expiry = NOW + timedelta(days=7)
    underlying = InstrumentMasterRecord(
        instrument=InstrumentId(
            exchange=Exchange.NSE,
            segment="IDX_I",
            security_id="13",
            symbol="NIFTY",
        ),
        display_name="NIFTY 50",
        instrument_type="INDEX",
    )
    futures = InstrumentMasterRecord(
        instrument=InstrumentId(
            exchange=Exchange.NSE,
            segment="NSE_FNO",
            security_id="50001",
            symbol="NIFTY-AUG-FUT",
        ),
        display_name="NIFTY AUG FUT",
        instrument_type="FUTIDX",
        underlying_security_id="13",
        expiry=NOW + timedelta(days=8),
        lot_size=75,
        tick_size=Decimal("0.05"),
    )
    options: list[InstrumentMasterRecord] = []
    for strike_index, strike in enumerate(
        (
            Decimal(24700),
            Decimal(24750),
            Decimal(24800),
            Decimal(24850),
            Decimal(24900),
        )
    ):
        for side_index, option_type in enumerate((OptionType.CALL, OptionType.PUT)):
            security_id = str(10000 + strike_index * 2 + side_index)
            options.append(
                InstrumentMasterRecord(
                    instrument=InstrumentId(
                        exchange=Exchange.NSE,
                        segment="NSE_FNO",
                        security_id=security_id,
                        symbol=f"NIFTY-{strike}-{option_type.value}",
                    ),
                    display_name=f"NIFTY {strike} {option_type.value}",
                    instrument_type="OPTIDX",
                    underlying_security_id="13",
                    expiry=option_expiry,
                    strike=strike,
                    option_type=option_type,
                    lot_size=75,
                    tick_size=Decimal("0.05"),
                )
            )
    return (underlying, futures, *options)


def valid_contract() -> ContractSpec:
    records = valid_master_records()
    underlying = records[0].instrument
    futures = records[1]
    return ContractSpec(
        underlying=underlying,
        futures=futures,
        market_kind=MarketKind.INDEX,
        pricing_model=PricingModel.BLACK_SCHOLES,
        option_expiry=NOW + timedelta(days=7),
        lot_size=75,
        strike_interval=Decimal(50),
        tick_size=Decimal("0.05"),
        master=valid_master(),
        option_contracts=tuple(records[2:]),
    )


def valid_quotes(contract: ContractSpec | None = None) -> tuple[OptionQuote, ...]:
    selected_contract = contract or valid_contract()
    quotes: list[OptionQuote] = []
    for strike_index, strike in enumerate(
        (Decimal(24700), Decimal(24750), Decimal(24800), Decimal(24850), Decimal(24900))
    ):
        for side_index, option_type in enumerate((OptionType.CALL, OptionType.PUT)):
            security_number = 10000 + strike_index * 2 + side_index
            base = Decimal(200) + Decimal(str(strike_index * 10 + side_index))
            quotes.append(
                OptionQuote(
                    security_id=str(security_number),
                    strike=strike,
                    option_type=option_type,
                    expiry=selected_contract.option_expiry,
                    bid=base,
                    ask=base + Decimal(1),
                    ltp=base + Decimal("0.50"),
                    volume=2_000 + strike_index,
                    open_interest=5_000 + strike_index,
                    previous_open_interest=4_900 + strike_index,
                    change_open_interest=None,
                    implied_volatility=0.18 + strike_index * 0.001,
                    greeks=Greeks(
                        delta=0.55 if option_type is OptionType.CALL else -0.45,
                        gamma=0.001,
                        theta=-10.0,
                        vega=12.0,
                    ),
                    observed_at=NOW,
                    bid_quantity=100,
                    ask_quantity=100,
                    previous_close=base - Decimal(5),
                )
            )
    return tuple(quotes)


def valid_snapshot() -> AtomicSnapshot:
    contract = valid_contract()
    return AtomicSnapshot.create(
        sequence=1,
        source=DataSource.MANUAL,
        source_timestamp=NOW,
        received_at=NOW,
        contract=contract,
        market=MarketState(
            observed_at=NOW,
            spot_price=Decimal(24800),
            futures_price=Decimal(24835),
            previous_close=Decimal(24700),
            day_open=Decimal(24750),
            day_high=Decimal(24900),
            day_low=Decimal(24650),
            vwap=Decimal(24810),
            futures_open_interest=100_000,
        ),
        technicals=TechnicalState(
            observed_at=NOW,
            ema_9=Decimal(24820),
            ema_21=Decimal(24780),
            wma_44=Decimal(24760),
            previous_wma_44=Decimal(24740),
            rsi_14=60.0,
            atr_14=Decimal(180),
            reference_volatility=0.12,
            timeframe="15m",
        ),
        context=StrategyContext(
            operating_mode=OperatingMode.PRO,
            trading_style=TradingStyle.INTRADAY,
            account_capital=Decimal(500000),
            risk_per_trade=0.01,
            maximum_premium_allocation=0.25,
            event_risk_active=False,
            price_action_confirmed=None,
            signal_candle_high=Decimal(24850),
            signal_candle_low=Decimal(24750),
            expected_holding_hours=6.0,
        ),
        option_chain=valid_quotes(contract),
        strategy_version=STRATEGY_VERSION,
        metadata={"raw_payload_hash": "a" * 64, "provider": "DHAN"},
    )
