from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from teco_quant.domain.enums import MarketKind
from teco_quant.ingestion.dhan_catalog import (
    IST,
    SUPPORTED_UNIVERSE,
    DhanCatalogError,
    DhanInstrumentCatalog,
    build_supported_dhan_catalog_batch,
)
from teco_quant.ingestion.normalization import (
    NormalizationError,
    dhan_api_segment,
    normalize_dhan_instrument_master,
)

NOW = datetime(2026, 8, 25, 4, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "dhan_master_nifty.csv"
DERIVATIVE_FAMILY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "dhan_master_nifty_derivative_family.csv"
)
UNSUPPORTED_ROUTE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "dhan_master_unsupported_route.csv"
)


class DhanCatalogTests(unittest.TestCase):
    def test_current_raw_segments_map_to_documented_api_segments(self) -> None:
        self.assertEqual(
            dhan_api_segment(exchange="NSE", segment="E", instrument="EQUITY"),
            "NSE_EQ",
        )
        self.assertEqual(
            dhan_api_segment(exchange="NSE", segment="D", instrument="OPTIDX"),
            "NSE_FNO",
        )
        self.assertEqual(
            dhan_api_segment(exchange="BSE", segment="C", instrument="FUTCUR"),
            "BSE_CURRENCY",
        )
        self.assertEqual(
            dhan_api_segment(exchange="MCX", segment="M", instrument="FUTCOM"),
            "MCX_COMM",
        )
        self.assertEqual(
            dhan_api_segment(exchange="BSE", segment="E", instrument="INDEX"),
            "IDX_I",
        )
        with self.assertRaises(NormalizationError):
            dhan_api_segment(exchange="MCX", segment="D", instrument="FUTCOM")

    def test_master_ignores_known_unsupported_route_before_field_normalization(self) -> None:
        records = normalize_dhan_instrument_master(
            UNSUPPORTED_ROUTE_FIXTURE.read_text(encoding="utf-8")
        )

        self.assertEqual([record.security_id for record in records], ["11536", "200001"])
        self.assertEqual([record.segment for record in records], ["NSE_EQ", "MCX_COMM"])

    def test_supported_master_route_remains_strict(self) -> None:
        malformed_supported_row = UNSUPPORTED_ROUTE_FIXTURE.read_text(
            encoding="utf-8"
        ).replace(
            "NSE,M,999001,UNRELATED-GOLD-OPTION",
            "MCX,M,999001,UNRELATED-GOLD-OPTION",
            1,
        )

        with self.assertRaisesRegex(
            NormalizationError,
            "unsupported option side",
        ):
            normalize_dhan_instrument_master(malformed_supported_row)

    def test_fixture_resolves_exact_nifty_contract_without_fixed_security_ids(self) -> None:
        batch = build_supported_dhan_catalog_batch(
            FIXTURE.read_text(encoding="utf-8"),
            fetched_at=NOW,
            source_url="https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        )
        family = DhanInstrumentCatalog(batch).family("NIFTY 50", as_of=NOW)
        resolved = family.contract_at(Decimal(24812))

        self.assertEqual(resolved.contract.underlying.security_id, "13")
        self.assertEqual(resolved.contract.futures.instrument.security_id, "50001")
        self.assertEqual(resolved.contract.strike_interval, Decimal(50))
        self.assertEqual(len(resolved.contract.option_contracts), 10)
        self.assertEqual(len(resolved.subscriptions), 12)
        self.assertEqual(resolved.option_chain_segment, "IDX_I")
        self.assertEqual(resolved.historical_instrument, "INDEX")
        self.assertEqual(batch.provenance.row_count, len(batch.records))
        self.assertIn("supported-universe", batch.provenance.schema_version)
        self.assertNotEqual(batch.provenance.content_hash, batch.source_content_hash)
        self.assertEqual(
            resolved.contract.option_expiry.utcoffset(), timedelta(hours=5, minutes=30)
        )

    def test_derivative_family_id_resolves_one_exact_named_index_record(self) -> None:
        batch = build_supported_dhan_catalog_batch(
            DERIVATIVE_FAMILY_FIXTURE.read_text(encoding="utf-8"),
            fetched_at=NOW,
            source_url="https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        )

        resolved = DhanInstrumentCatalog(batch).family("NIFTY", as_of=NOW).contract_at(
            Decimal(24800)
        )

        self.assertEqual(resolved.contract.underlying.security_id, "13")
        self.assertEqual(resolved.contract.futures.underlying_security_id, "26000")
        self.assertTrue(
            all(
                option.underlying_security_id == "26000"
                for option in resolved.contract.option_contracts
            )
        )
        self.assertEqual(resolved.option_chain_security_id, "13")
        self.assertEqual(resolved.option_chain_segment, "IDX_I")

    def test_derivative_family_alias_bridge_fails_when_index_is_ambiguous(self) -> None:
        text = DERIVATIVE_FAMILY_FIXTURE.read_text(encoding="utf-8").replace(
            "NSE,I,13,NIFTY,Nifty 50,INDEX,INDEX,,,,,,,\n",
            "NSE,I,13,NIFTY,Nifty 50,INDEX,INDEX,,,,,,,\n"
            "NSE,I,14,NIFTY,Nifty 50 Duplicate,INDEX,INDEX,,,,,,,\n",
            1,
        )
        batch = build_supported_dhan_catalog_batch(
            text,
            fetched_at=NOW,
            source_url="https://example.invalid/master.csv",
        )

        with self.assertRaisesRegex(
            DhanCatalogError,
            "do not map to one exact underlying record",
        ):
            DhanInstrumentCatalog(batch).family("NIFTY", as_of=NOW)

    def test_every_supported_symbol_resolves_from_relationships(self) -> None:
        csv_text, expected_ids = _all_universe_master()
        normalized = normalize_dhan_instrument_master(csv_text)
        self.assertTrue(all(record.segment not in {"D", "M", "E", "C"} for record in normalized))
        batch = build_supported_dhan_catalog_batch(
            csv_text,
            fetched_at=NOW,
            source_url="https://example.invalid/dhan-master.csv",
        )
        catalog = DhanInstrumentCatalog(batch)

        for symbol, definition in SUPPORTED_UNIVERSE.items():
            with self.subTest(symbol=symbol):
                family = catalog.family(symbol, as_of=NOW)
                resolved = family.contract_at(Decimal(1000))
                self.assertEqual(len(resolved.contract.option_contracts), 10)
                self.assertEqual(resolved.contract.futures.instrument.security_id, expected_ids[symbol][1])
                if definition.market_kind is MarketKind.COMMODITY:
                    self.assertEqual(
                        resolved.contract.underlying.security_id,
                        resolved.contract.futures.instrument.security_id,
                    )
                    self.assertEqual(resolved.option_chain_segment, "MCX_COMM")
                else:
                    self.assertEqual(
                        resolved.contract.underlying.security_id,
                        expected_ids[symbol][0],
                    )

    def test_expired_or_incomplete_contracts_fail_closed(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        batch = build_supported_dhan_catalog_batch(
            text,
            fetched_at=NOW,
            source_url="https://example.invalid/master.csv",
        )
        catalog = DhanInstrumentCatalog(batch)
        after_expiry = datetime(2026, 8, 27, 15, 31, tzinfo=IST)
        with self.assertRaisesRegex(DhanCatalogError, "no active"):
            catalog.family("NIFTY", as_of=after_expiry)

        incomplete = "\n".join(text.splitlines()[:-1]) + "\n"
        incomplete_batch = build_supported_dhan_catalog_batch(
            incomplete,
            fetched_at=NOW,
            source_url="https://example.invalid/master.csv",
        )
        incomplete_family = DhanInstrumentCatalog(incomplete_batch).family(
            "NIFTY", as_of=NOW
        )
        with self.assertRaisesRegex(DhanCatalogError, "five complete|exact ATM"):
            incomplete_family.contract_at(Decimal(24800))


def _all_universe_master() -> tuple[str, dict[str, tuple[str, str]]]:
    header = (
        "EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT,"
        "INSTRUMENT_TYPE,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SM_EXPIRY_DATE,"
        "STRIKE_PRICE,OPTION_TYPE,LOT_SIZE,TICK_SIZE"
    )
    rows = [header]
    expected: dict[str, tuple[str, str]] = {}
    next_id = 700_000
    for symbol, definition in SUPPORTED_UNIVERSE.items():
        base_id = str(next_id)
        future_id = str(next_id + 1)
        expected[symbol] = (base_id, future_id)
        raw_derivative = "M" if definition.market_kind is MarketKind.COMMODITY else "D"
        if definition.market_kind is not MarketKind.COMMODITY:
            rows.append(
                ",".join(
                    (
                        definition.exchange.value,
                        "E",
                        base_id,
                        definition.aliases[0],
                        definition.aliases[0],
                        definition.underlying_instrument,
                        definition.underlying_instrument,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    )
                )
            )
        future_underlying = "" if definition.market_kind is MarketKind.COMMODITY else base_id
        rows.append(
            ",".join(  # noqa: FLY002 - fixture row is clearer as explicit columns
                (
                    definition.exchange.value,
                    raw_derivative,
                    future_id,
                    symbol,
                    symbol,
                    definition.future_instrument,
                    definition.future_instrument,
                    future_underlying,
                    symbol,
                    "2026-08-28",
                    "",
                    "",
                    "50",
                    "0.05",
                )
            )
        )
        option_underlying = (
            future_id if definition.market_kind is MarketKind.COMMODITY else base_id
        )
        for offset, strike in enumerate((980, 990, 1000, 1010, 1020)):
            for side_index, side in enumerate(("CE", "PE")):
                security_id = str(next_id + 10 + offset * 2 + side_index)
                rows.append(
                    ",".join(
                        (
                            definition.exchange.value,
                            raw_derivative,
                            security_id,
                            f"{symbol}-{strike}-{side}",
                            f"{symbol} {strike} {side}",
                            definition.option_instrument,
                            definition.option_instrument,
                            option_underlying,
                            symbol,
                            "2026-08-27",
                            str(strike),
                            side,
                            "50",
                            "0.05",
                        )
                    )
                )
        next_id += 100
    return "\n".join(rows) + "\n", expected


if __name__ == "__main__":
    unittest.main()
