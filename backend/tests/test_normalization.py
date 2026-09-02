from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from teco_quant.domain.enums import DataSource, OptionType
from teco_quant.domain.models import PreviousOptionSnapshot
from teco_quant.ingestion.normalization import (
    NormalizationError,
    build_dhan_master_batch,
    decimal_value,
    materialize_master_records,
    normalize_dhan_market_quote,
    normalize_dhan_option_chain,
    normalize_option_side,
    raw_payload_hash,
)
from tests.helpers import NOW, valid_contract, valid_quotes


def dhan_payload(oi: int = 5_100) -> dict:
    return {
        "status": "success",
        "data": {
            "last_price": 24800,
            "oc": {
                "24800.000000": {
                    "ce": {
                        "security_id": 123,
                        "top_bid_price": 218,
                        "top_ask_price": 222,
                        "last_price": 220,
                        "top_bid_quantity": 75,
                        "top_ask_quantity": 150,
                        "volume": 2000,
                        "oi": oi,
                        "previous_oi": 4900,
                        "previous_close_price": 210,
                        "implied_volatility": 18.5,
                        "greeks": {
                            "delta": 0.53,
                            "gamma": 0.001,
                            "theta": -10,
                            "vega": 12,
                        },
                    },
                    "pe": {
                        "security_id": "124",
                        "top_bid_price": 217,
                        "top_ask_price": 223,
                        "last_price": 220,
                        "top_bid_quantity": 75,
                        "top_ask_quantity": 150,
                        "volume": 2100,
                        "oi": 5200,
                        "previous_oi": 5000,
                        "previous_close_price": 211,
                        "implied_volatility": 19,
                        "greeks": {
                            "delta": -0.47,
                            "gamma": 0.001,
                            "theta": -11,
                            "vega": 12.2,
                        },
                    },
                }
            },
        },
    }


def prior_snapshot(
    *,
    quotes=(),
    source: DataSource = DataSource.DHAN_REST,
    source_timestamp=NOW,
    sequence: int = 1,
    contract_key: str | None = None,
    snapshot_id: str = "prior-snapshot",
) -> PreviousOptionSnapshot:
    contract = valid_contract()
    return PreviousOptionSnapshot(
        snapshot_id=snapshot_id,
        sequence=sequence,
        source=source,
        source_timestamp=source_timestamp,
        contract_key=contract_key or contract.contract_key,
        option_chain=tuple(quotes),
    )


def _market_quote_payload() -> dict:
    return {
        "status": "success",
        "data": {
            "IDX_I": {
                "13": {
                    "last_price": 24800,
                    "ohlc": {
                        "open": 24750,
                        "close": 24700,
                        "high": 24900,
                        "low": 24600,
                    },
                    "volume": 100_000,
                    "oi": 0,
                    "depth": {
                        "buy": [{"price": 24799}],
                        "sell": [{"price": 24801}],
                    },
                }
            }
        },
    }


class NormalizationTests(unittest.TestCase):
    def test_dhan_quote_uses_provider_trade_time_and_retains_http_receipt(self) -> None:
        received_at = datetime(2026, 8, 25, 6, 30, 5, tzinfo=UTC)
        payload = _market_quote_payload()
        payload["data"]["IDX_I"]["13"]["last_trade_time"] = (
            "25/08/2026 12:00:02"
        )

        quote = normalize_dhan_market_quote(
            payload,
            segment="IDX_I",
            security_id="13",
            observed_at=received_at,
        )

        self.assertEqual(quote.observed_at.isoformat(), "2026-08-25T12:00:02+05:30")
        self.assertEqual(quote.received_at, received_at)
        self.assertIsNone(quote.timestamp_warning)

    def test_dhan_quote_receipt_fallback_requires_field_to_be_absent(self) -> None:
        received_at = datetime(2026, 8, 25, 6, 30, 5, tzinfo=UTC)
        quote = normalize_dhan_market_quote(
            _market_quote_payload(),
            segment="IDX_I",
            security_id="13",
            observed_at=received_at,
        )

        self.assertEqual(quote.observed_at, received_at)
        self.assertEqual(
            quote.timestamp_warning,
            "LAST_TRADE_TIME_ABSENT_RECEIPT_FALLBACK",
        )

    def test_dhan_quote_rejects_malformed_sentinel_old_and_future_trade_times(self) -> None:
        received_at = datetime(2026, 8, 25, 6, 30, 5, tzinfo=UTC)
        for value, message in (
            (None, "malformed"),
            ("2026-08-25T12:00:00", "DD/MM/YYYY"),
            ("01/01/1980 00:00:00", "stale or a sentinel"),
            ("25/08/2026 11:59:00", "stale or a sentinel"),
            ("25/08/2026 12:00:10", "future-dated"),
        ):
            with self.subTest(value=value):
                payload = _market_quote_payload()
                payload["data"]["IDX_I"]["13"]["last_trade_time"] = value
                with self.assertRaisesRegex(NormalizationError, message):
                    normalize_dhan_market_quote(
                        payload,
                        segment="IDX_I",
                        security_id="13",
                        observed_at=received_at,
                    )

    def test_instrument_master_is_hashed_and_expiry_is_explicitly_resolved(self) -> None:
        csv_text = (
            "EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT,"
            "INSTRUMENT_TYPE,UNDERLYING_SECURITY_ID,SM_EXPIRY_DATE,STRIKE_PRICE,"
            "OPTION_TYPE,LOT_SIZE,TICK_SIZE\n"
            "NSE,NSE_FNO,10004,NIFTY-24800-CE,NIFTY 24800 CE,OPTIDX,OPTIDX,13,"
            "2026-08-27,24800,CE,75,0.05\n"
        )
        provenance, records = build_dhan_master_batch(
            csv_text,
            fetched_at=NOW,
            source_url="https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        )
        expiry = datetime(
            2026,
            8,
            27,
            15,
            30,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        )
        materialized = materialize_master_records(
            records,
            expiry_resolver=lambda _: expiry,
        )

        self.assertEqual(provenance.row_count, 1)
        self.assertEqual(len(provenance.content_hash), 64)
        self.assertEqual(records[0].expiry, "2026-08-27")
        self.assertEqual(materialized[0].expiry, expiry)
        self.assertEqual(materialized[0].instrument.security_id, "10004")

        later_provenance, later_records = build_dhan_master_batch(
            csv_text,
            fetched_at=NOW + timedelta(days=2),
            source_url="https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        )
        self.assertEqual(later_provenance.content_hash, provenance.content_hash)
        self.assertNotEqual(later_provenance.batch_id, provenance.batch_id)
        self.assertEqual(later_records, records)

        with self.assertRaisesRegex(NormalizationError, "differs"):
            materialize_master_records(
                records,
                expiry_resolver=lambda _: expiry + timedelta(days=1),
            )

    def test_dhan_iv_is_converted_from_percent_to_decimal(self) -> None:
        quotes = normalize_dhan_option_chain(
            dhan_payload(),
            contract=valid_contract(),
            observed_at=NOW,
            sequence=1,
            source=DataSource.DHAN_REST,
        )
        self.assertEqual(len(quotes), 2)
        self.assertAlmostEqual(quotes[0].implied_volatility or 0, 0.185)
        self.assertIsNone(quotes[0].change_open_interest)
        self.assertIsNone(quotes[0].change_oi_source_snapshot_id)
        self.assertIsNone(quotes[0].change_oi_interval_seconds)
        self.assertEqual(quotes[0].security_id, "123")

    def test_change_oi_uses_previous_compatible_snapshot_not_provider_previous_day(self) -> None:
        previous = [
            quote for quote in valid_quotes() if quote.strike == Decimal(24800)
        ]
        previous_call = replace(previous[0], open_interest=5_000, security_id="123")
        previous_put = replace(previous[1], open_interest=5_000, security_id="124")
        quotes = normalize_dhan_option_chain(
            dhan_payload(oi=5_100),
            contract=valid_contract(),
            observed_at=NOW + timedelta(seconds=5),
            sequence=2,
            source=DataSource.DHAN_REST,
            previous_snapshot=prior_snapshot(
                quotes=(previous_call, previous_put),
            ),
        )
        self.assertEqual(quotes[0].change_open_interest, 100)
        self.assertEqual(quotes[1].change_open_interest, 200)
        self.assertEqual(
            quotes[0].change_oi_source_snapshot_id,
            "prior-snapshot",
        )
        self.assertEqual(quotes[0].change_oi_interval_seconds, 5.0)

    def test_change_oi_is_not_carried_across_security_ids(self) -> None:
        previous_call = next(
            quote
            for quote in valid_quotes()
            if quote.strike == Decimal(24800) and quote.option_type.value == "CE"
        )
        previous_call = replace(
            previous_call,
            open_interest=5_000,
            security_id="expired-contract-id",
        )

        quotes = normalize_dhan_option_chain(
            dhan_payload(oi=5_100),
            contract=valid_contract(),
            observed_at=NOW + timedelta(seconds=5),
            sequence=2,
            source=DataSource.DHAN_REST,
            previous_snapshot=prior_snapshot(quotes=(previous_call,)),
        )

        self.assertIsNone(quotes[0].change_open_interest)
        self.assertIsNone(quotes[0].change_oi_source_snapshot_id)

    def test_change_oi_requires_exact_leg_identity(self) -> None:
        call = next(
            quote
            for quote in valid_quotes()
            if quote.strike == Decimal(24800) and quote.option_type.value == "CE"
        )
        call = replace(call, open_interest=5_000, security_id="123")
        mismatched_legs = {
            "expiry": replace(call, expiry=call.expiry + timedelta(days=7)),
            "strike": replace(call, strike=Decimal(24850)),
            "side": replace(call, option_type=OptionType.PUT),
        }

        for field, prior_call in mismatched_legs.items():
            with self.subTest(field=field):
                quote = normalize_dhan_option_chain(
                    dhan_payload(oi=5_100),
                    contract=valid_contract(),
                    observed_at=NOW + timedelta(seconds=5),
                    sequence=2,
                    source=DataSource.DHAN_REST,
                    previous_snapshot=prior_snapshot(quotes=(prior_call,)),
                )[0]
                self.assertIsNone(quote.change_open_interest)
                self.assertIsNone(quote.change_oi_source_snapshot_id)

    def test_snapshot_provenance_must_be_compatible(self) -> None:
        call = next(
            quote
            for quote in valid_quotes()
            if quote.strike == Decimal(24800) and quote.option_type.value == "CE"
        )
        call = replace(call, open_interest=5_000, security_id="123")
        current_time = NOW + timedelta(seconds=5)
        incompatible_snapshots = {
            "wrong contract": prior_snapshot(
                quotes=(call,), contract_key="contract:wrong"
            ),
            "wrong source": prior_snapshot(
                quotes=(call,), source=DataSource.DHAN_LIVE
            ),
            "same sequence": prior_snapshot(quotes=(call,), sequence=2),
            "future source": prior_snapshot(
                quotes=(call,), source_timestamp=current_time
            ),
            "stale source": prior_snapshot(
                quotes=(call,), source_timestamp=NOW - timedelta(seconds=26)
            ),
            "missing snapshot id": prior_snapshot(quotes=(call,), snapshot_id=""),
        }

        for reason, previous in incompatible_snapshots.items():
            with self.subTest(reason=reason):
                quote = normalize_dhan_option_chain(
                    dhan_payload(oi=5_100),
                    contract=valid_contract(),
                    observed_at=current_time,
                    sequence=2,
                    source=DataSource.DHAN_REST,
                    previous_snapshot=previous,
                )[0]
                self.assertIsNone(quote.change_open_interest)
                self.assertIsNone(quote.change_oi_source_snapshot_id)
                self.assertIsNone(quote.change_oi_interval_seconds)

        wrong_current_source = normalize_dhan_option_chain(
            dhan_payload(oi=5_100),
            contract=valid_contract(),
            observed_at=current_time,
            sequence=2,
            source=DataSource.DHAN_LIVE,
            previous_snapshot=prior_snapshot(quotes=(call,)),
        )[0]
        self.assertIsNone(wrong_current_source.change_open_interest)

    def test_each_prior_leg_timestamp_must_be_strictly_earlier_and_recent(self) -> None:
        call = next(
            quote
            for quote in valid_quotes()
            if quote.strike == Decimal(24800) and quote.option_type.value == "CE"
        )
        current_time = NOW + timedelta(seconds=5)
        for reason, leg_time in (
            ("equal", current_time),
            ("future", current_time + timedelta(seconds=1)),
            ("stale", NOW - timedelta(seconds=26)),
        ):
            with self.subTest(reason=reason):
                prior_call = replace(
                    call,
                    security_id="123",
                    open_interest=5_000,
                    observed_at=leg_time,
                )
                quote = normalize_dhan_option_chain(
                    dhan_payload(oi=5_100),
                    contract=valid_contract(),
                    observed_at=current_time,
                    sequence=2,
                    source=DataSource.DHAN_REST,
                    previous_snapshot=prior_snapshot(quotes=(prior_call,)),
                )[0]
                self.assertIsNone(quote.change_open_interest)

    def test_change_oi_max_interval_is_configurable(self) -> None:
        call = next(
            quote
            for quote in valid_quotes()
            if quote.strike == Decimal(24800) and quote.option_type.value == "CE"
        )
        call = replace(call, open_interest=5_000, security_id="123")
        quote = normalize_dhan_option_chain(
            dhan_payload(oi=5_100),
            contract=valid_contract(),
            observed_at=NOW + timedelta(seconds=45),
            sequence=2,
            source=DataSource.DHAN_REST,
            previous_snapshot=prior_snapshot(quotes=(call,)),
            max_change_oi_interval=timedelta(seconds=60),
        )[0]

        self.assertEqual(quote.change_open_interest, 100)
        self.assertEqual(quote.change_oi_interval_seconds, 45.0)

    def test_non_positive_change_oi_interval_is_rejected(self) -> None:
        with self.assertRaises(NormalizationError):
            normalize_dhan_option_chain(
                dhan_payload(),
                contract=valid_contract(),
                observed_at=NOW,
                sequence=1,
                source=DataSource.DHAN_REST,
                max_change_oi_interval=timedelta(0),
            )

    def test_invalid_non_finite_values_are_rejected(self) -> None:
        with self.assertRaises(NormalizationError):
            decimal_value("NaN", field="price")
        with self.assertRaises(NormalizationError):
            decimal_value(True, field="price")

    def test_aliases_and_hash_are_deterministic(self) -> None:
        self.assertEqual(normalize_option_side("call").value, "CE")
        self.assertEqual(normalize_option_side("PE").value, "PE")
        self.assertEqual(raw_payload_hash({"b": 2, "a": 1}), raw_payload_hash({"a": 1, "b": 2}))


if __name__ == "__main__":
    unittest.main()
