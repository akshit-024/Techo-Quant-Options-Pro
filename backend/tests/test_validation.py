from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from teco_quant.domain.enums import DataSource, Exchange, MarketKind, OptionType, PricingModel
from teco_quant.domain.models import (
    ContractSpec,
    InstrumentId,
    InstrumentMasterRecord,
    PreviousOptionSnapshot,
)
from teco_quant.ingestion.validation import SnapshotValidator
from tests.helpers import NOW, valid_contract, valid_snapshot


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = SnapshotValidator(clock=lambda: NOW)

    def test_valid_atomic_snapshot_is_accepted(self) -> None:
        snapshot = valid_snapshot()
        report = self.validator.validate(snapshot)
        self.assertTrue(report.accepted, report.issues)
        self.assertEqual(report.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(len(report.snapshot_hash or ""), 64)

    def test_index_derivative_family_and_distinct_future_tick_are_accepted(self) -> None:
        snapshot = valid_snapshot()
        assert snapshot.contract.futures is not None
        contract = replace(
            snapshot.contract,
            futures=replace(
                snapshot.contract.futures,
                underlying_security_id="26000",
                tick_size=Decimal("0.10"),
            ),
            option_contracts=tuple(
                replace(record, underlying_security_id="26000")
                for record in snapshot.contract.option_contracts
            ),
        )

        report = self.validator.validate(replace(snapshot, contract=contract))

        self.assertTrue(report.accepted, report.issues)

    def test_distinct_future_lot_size_remains_rejected(self) -> None:
        snapshot = valid_snapshot()
        assert snapshot.contract.futures is not None
        contract = replace(
            snapshot.contract,
            futures=replace(
                snapshot.contract.futures,
                lot_size=snapshot.contract.lot_size + 1,
            ),
        )

        report = self.validator.validate(replace(snapshot, contract=contract))

        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("FUTURES_MASTER_SPEC_MISMATCH"), report.issues)

    def test_index_future_must_match_the_option_derivative_family(self) -> None:
        snapshot = valid_snapshot()
        assert snapshot.contract.futures is not None
        contract = replace(
            snapshot.contract,
            futures=replace(
                snapshot.contract.futures,
                underlying_security_id="26009",
            ),
            option_contracts=tuple(
                replace(record, underlying_security_id="26000")
                for record in snapshot.contract.option_contracts
            ),
        )

        report = self.validator.validate(replace(snapshot, contract=contract))

        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("FUTURES_UNDERLYING_MISMATCH"), report.issues)

    def test_index_options_must_share_one_derivative_family_id(self) -> None:
        snapshot = valid_snapshot()
        assert snapshot.contract.futures is not None
        option_contracts = tuple(
            replace(record, underlying_security_id="26000")
            for record in snapshot.contract.option_contracts
        )
        option_contracts = (
            replace(option_contracts[0], underlying_security_id="26009"),
            *option_contracts[1:],
        )
        contract = replace(
            snapshot.contract,
            futures=replace(
                snapshot.contract.futures,
                underlying_security_id="26000",
            ),
            option_contracts=option_contracts,
        )

        report = self.validator.validate(replace(snapshot, contract=contract))

        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("OPTION_MASTER_IDENTITY_MISMATCH"), report.issues)
        self.assertTrue(report.has_code("FUTURES_UNDERLYING_MISMATCH"), report.issues)

    def test_missing_leg_price_rejects_snapshot(self) -> None:
        snapshot = valid_snapshot()
        broken = replace(snapshot.option_chain[0], bid=None)
        snapshot = replace(snapshot, option_chain=(broken,) + snapshot.option_chain[1:])
        report = self.validator.validate(snapshot)
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("QUOTE_FIELD_MISSING"))

    def test_stale_snapshot_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        old = NOW - timedelta(seconds=31)
        quotes = tuple(replace(quote, observed_at=old) for quote in snapshot.option_chain)
        snapshot = replace(
            snapshot,
            source_timestamp=old,
            market=replace(snapshot.market, observed_at=old),
            technicals=replace(snapshot.technicals, observed_at=old),
            option_chain=quotes,
        )
        report = self.validator.validate(snapshot)
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("STALE_DATA"))
        self.assertTrue(report.has_code("STALE_COMPONENT"))

    def test_stale_contract_master_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        contract = replace(
            snapshot.contract,
            master=replace(
                snapshot.contract.master,
                fetched_at=NOW - timedelta(hours=37),
            ),
        )
        report = self.validator.validate(replace(snapshot, contract=contract))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("STALE_CONTRACT_MASTER"))

    def test_incomplete_technical_candle_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        technicals = replace(snapshot.technicals, completed_candle=False)
        report = self.validator.validate(replace(snapshot, technicals=technicals))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("INCOMPLETE_TECHNICAL_CANDLE"))

    def test_risk_above_hard_ceiling_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        context = replace(snapshot.context, risk_per_trade=0.03)
        report = self.validator.validate(replace(snapshot, context=context))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("RISK_LIMIT_EXCEEDED"))

    def test_non_finite_context_numbers_are_rejected_without_crashing(self) -> None:
        cases = (
            ("account_capital", Decimal("NaN"), "INVALID_ACCOUNT_CAPITAL"),
            ("risk_per_trade", float("nan"), "RISK_LIMIT_EXCEEDED"),
            (
                "maximum_premium_allocation",
                float("inf"),
                "INVALID_PREMIUM_ALLOCATION",
            ),
            ("signal_candle_high", Decimal("Infinity"), "SIGNAL_CANDLE_MISSING"),
            ("expected_holding_hours", float("-inf"), "INVALID_HOLDING_PERIOD"),
        )
        snapshot = valid_snapshot()
        for field, value, expected_code in cases:
            with self.subTest(field=field):
                context = replace(snapshot.context, **{field: value})
                report = self.validator.validate(replace(snapshot, context=context))
                self.assertFalse(report.accepted)
                self.assertTrue(report.has_code(expected_code), report.issues)

    def test_unknown_event_risk_is_a_visible_warning(self) -> None:
        snapshot = valid_snapshot()
        context = replace(snapshot.context, event_risk_active=None)
        report = self.validator.validate(replace(snapshot, context=context))
        self.assertTrue(report.accepted, report.errors)
        self.assertTrue(report.has_code("EVENT_RISK_UNKNOWN"))

    def test_duplicate_security_id_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        second = replace(snapshot.option_chain[1], security_id=snapshot.option_chain[0].security_id)
        snapshot = replace(snapshot, option_chain=(snapshot.option_chain[0], second) + snapshot.option_chain[2:])
        report = self.validator.validate(snapshot)
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("DUPLICATE_SECURITY_ID"))

    def test_missing_strike_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        quotes = tuple(quote for quote in snapshot.option_chain if quote.strike != Decimal(24700))
        report = self.validator.validate(replace(snapshot, option_chain=quotes))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("FIVE_STRIKES_UNAVAILABLE"))

    def test_first_snapshot_without_change_oi_is_stored_as_visible_baseline(self) -> None:
        snapshot = valid_snapshot()
        quote = replace(
            snapshot.option_chain[0],
            change_open_interest=None,
            change_oi_source_snapshot_id=None,
            change_oi_interval_seconds=None,
        )
        report = self.validator.validate(
            replace(snapshot, option_chain=(quote,) + snapshot.option_chain[1:])
        )
        self.assertTrue(report.accepted, report.errors)
        self.assertTrue(report.has_code("CHANGE_OI_BASELINE_REQUIRED"))

    def test_change_oi_is_recomputed_from_loaded_exact_prior_leg(self) -> None:
        baseline = valid_snapshot()
        prior_time = NOW - timedelta(seconds=1)
        prior_quotes = tuple(
            replace(quote, observed_at=prior_time) for quote in baseline.option_chain
        )
        previous = PreviousOptionSnapshot(
            snapshot_id="accepted-prior",
            sequence=1,
            source=DataSource.DHAN_REST,
            source_timestamp=prior_time,
            contract_key=baseline.contract.contract_key,
            option_chain=prior_quotes,
        )
        first = baseline.option_chain[0]
        forged = replace(
            first,
            open_interest=(first.open_interest or 0) + 7,
            change_open_interest=999,
            change_oi_source_snapshot_id=previous.snapshot_id,
            change_oi_interval_seconds=1.0,
        )
        candidate = replace(
            baseline,
            sequence=2,
            source=DataSource.DHAN_REST,
            option_chain=(forged,) + baseline.option_chain[1:],
        )
        validator = SnapshotValidator(
            clock=lambda: NOW,
            change_oi_reference_loader=lambda snapshot_id: (
                previous if snapshot_id == previous.snapshot_id else None
            ),
        )

        report = validator.validate(candidate)

        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("CHANGE_OI_DELTA_MISMATCH"), report.issues)

    def test_mcx_requires_black_76_and_exact_future(self) -> None:
        snapshot = valid_snapshot()
        bad_contract = replace(
            valid_contract(),
            market_kind=MarketKind.COMMODITY,
            pricing_model=PricingModel.BLACK_SCHOLES,
            futures=None,
        )
        bad_quotes = tuple(
            replace(quote, expiry=bad_contract.option_expiry) for quote in snapshot.option_chain
        )
        report = self.validator.validate(
            replace(snapshot, contract=bad_contract, option_chain=bad_quotes)
        )
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("WRONG_PRICING_MODEL"))
        self.assertTrue(report.has_code("FUTURES_MAPPING_MISSING"))

    def test_nse_contract_cannot_be_relabelled_as_mcx(self) -> None:
        snapshot = valid_snapshot()
        contract = replace(
            snapshot.contract,
            market_kind=MarketKind.COMMODITY,
            pricing_model=PricingModel.BLACK_76,
        )
        report = self.validator.validate(replace(snapshot, contract=contract))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("MARKET_EXCHANGE_MISMATCH"))
        self.assertTrue(report.has_code("MCX_FUTURES_MAPPING_MISMATCH"))

    def test_valid_mcx_contract_uses_exact_future_and_black_76(self) -> None:
        snapshot = valid_snapshot()
        option_expiry = NOW + timedelta(days=7)

        # Mirrors Dhan's real MCX derivative-family relationship:
        # the quoteable futures security ID is distinct from the
        # derivative-family underlying_security_id used by both
        # the future master row and its option master rows.
        derivative_family_id = "294"

        future_instrument = InstrumentId(
            exchange=Exchange.MCX,
            segment="MCX_COMM",
            security_id="70001",
            symbol="CRUDEOIL-AUG-FUT",
        )

        future = InstrumentMasterRecord(
            instrument=future_instrument,
            display_name="CRUDE OIL AUG FUT",
            instrument_type="FUTCOM",
            underlying_security_id=derivative_family_id,
            expiry=NOW + timedelta(days=8),
            lot_size=100,
            tick_size=Decimal("0.10"),
        )

        records: list[InstrumentMasterRecord] = []
        quotes = []

        for index, strike in enumerate(
            (
                Decimal(6900),
                Decimal(6950),
                Decimal(7000),
                Decimal(7050),
                Decimal(7100),
            )
        ):
            for side_index, option_type in enumerate(
                (OptionType.CALL, OptionType.PUT)
            ):
                security_id = str(80000 + index * 2 + side_index)

                records.append(
                    InstrumentMasterRecord(
                        instrument=InstrumentId(
                            exchange=Exchange.MCX,
                            segment="MCX_COMM",
                            security_id=security_id,
                            symbol=f"CRUDEOIL-{strike}-{option_type.value}",
                        ),
                        display_name=f"CRUDE OIL {strike} {option_type.value}",
                        instrument_type="OPTFUT",
                        underlying_security_id=derivative_family_id,
                        expiry=option_expiry,
                        strike=strike,
                        option_type=option_type,
                        lot_size=100,
                        tick_size=Decimal("0.10"),
                    )
                )

                source_quote = snapshot.option_chain[index * 2 + side_index]
                quotes.append(
                    replace(
                        source_quote,
                        security_id=security_id,
                        strike=strike,
                        expiry=option_expiry,
                    )
                )

        contract = ContractSpec(
            underlying=future_instrument,
            market_kind=MarketKind.COMMODITY,
            pricing_model=PricingModel.BLACK_76,
            option_expiry=option_expiry,
            lot_size=100,
            strike_interval=Decimal(50),
            tick_size=Decimal("0.10"),
            master=replace(
                snapshot.contract.master,
                batch_id="DHAN:" + "e" * 16,
                content_hash="e" * 64,
                row_count=11,
            ),
            option_contracts=tuple(records),
            futures=future,
        )

        candidate = replace(
            snapshot,
            contract=contract,
            market=replace(
                snapshot.market,
                spot_price=None,
                futures_price=Decimal(7000),
                previous_close=Decimal(6950),
                day_open=Decimal(6975),
                day_high=Decimal(7050),
                day_low=Decimal(6900),
                vwap=Decimal(6990),
            ),
            technicals=replace(
                snapshot.technicals,
                ema_9=Decimal(7010),
                ema_21=Decimal(6990),
                wma_44=Decimal(6980),
                previous_wma_44=Decimal(6970),
                atr_14=Decimal(80),
            ),
            context=replace(
                snapshot.context,
                signal_candle_high=Decimal(7025),
                signal_candle_low=Decimal(6975),
            ),
            option_chain=tuple(quotes),
        )

        report = self.validator.validate(candidate)

        self.assertTrue(report.accepted, report.errors)

    def test_wrong_but_unique_option_security_id_is_rejected(self) -> None:
        snapshot = valid_snapshot()
        wrong = replace(snapshot.option_chain[0], security_id="99999999")
        report = self.validator.validate(
            replace(snapshot, option_chain=(wrong,) + snapshot.option_chain[1:])
        )
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("OPTION_SECURITY_ID_MISMATCH"))

    def test_naive_evaluation_and_futures_times_reject_without_crashing(self) -> None:
        snapshot = valid_snapshot()
        report = self.validator.validate(snapshot, now=NOW.replace(tzinfo=None))
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("NAIVE_TIMESTAMP"))

        assert snapshot.contract.futures is not None
        future = replace(
            snapshot.contract.futures,
            expiry=snapshot.contract.futures.expiry.replace(tzinfo=None),
        )
        report = self.validator.validate(
            replace(snapshot, contract=replace(snapshot.contract, futures=future))
        )
        self.assertFalse(report.accepted)
        self.assertTrue(report.has_code("NAIVE_TIMESTAMP"))

    def test_extreme_expiry_is_visible_but_data_remains_structurally_valid(self) -> None:
        snapshot = valid_snapshot()
        expiry = NOW + timedelta(hours=8)
        contract = replace(
            snapshot.contract,
            option_expiry=expiry,
            option_contracts=tuple(
                replace(record, expiry=expiry)
                for record in snapshot.contract.option_contracts
            ),
        )
        quotes = tuple(replace(quote, expiry=contract.option_expiry) for quote in snapshot.option_chain)
        report = self.validator.validate(
            replace(snapshot, contract=contract, option_chain=quotes)
        )
        self.assertTrue(report.accepted, report.errors)
        self.assertTrue(report.has_code("EXTREME_EXPIRY_RISK"))


if __name__ == "__main__":
    unittest.main()
