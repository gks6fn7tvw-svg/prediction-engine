"""Fixture and regression tests for the offline paper replay."""

import json
import unittest
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from paper.execution import SUB_CENT, WHOLE_CENT, Fill, buy_fees, walk_buy
from paper.replay import replay_capture
from tests.test_recorder import CUTOFF_DIR, World


class BookWalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_dir = Path(__file__).resolve().parents[1] / "reference"
        with zipfile.ZipFile(fixture_dir / "Prediction_Engine_First_Capture_v0_1.zip") as archive:
            books = json.loads(archive.read("KXHIGHLAX_orderbooks.json"))["orderbooks"]
        cls.lax_book = next(
            row["orderbook_fp"] for row in books if row["ticker"] == "KXHIGHLAX-26NOV11-B83.5"
        )

    def test_los_angeles_sweep_prices_each_level(self):
        walk = walk_buy(self.lax_book, side="yes", limit=Decimal("0.36"))
        self.assertEqual(walk.fills, (
            Fill(Decimal("0.35"), Decimal("0.01")),
            Fill(Decimal("0.36"), Decimal("0.99")),
        ))
        self.assertEqual(walk.notional, Decimal("0.3599"))
        whole = buy_fees(walk.fills, multiplier=Decimal(1), balance_precision=WHOLE_CENT)
        sub = buy_fees(walk.fills, multiplier=Decimal(1), balance_precision=SUB_CENT)
        self.assertEqual(whole.total_debit, Decimal("0.38"))
        self.assertEqual(whole.per_fill[1].rebate, Decimal("0.01"))
        self.assertEqual(sub.total_debit, Decimal("0.3761"))

    def test_limit_excludes_worse_levels_and_skips_partial_depth(self):
        walk = walk_buy(self.lax_book, side="yes", limit=Decimal("0.35"))
        self.assertEqual(walk.quantity_at_limit, Decimal("0.01"))
        self.assertEqual(walk.fills, ())

    def test_no_buy_uses_yes_bids_and_supports_fractional_sizes(self):
        book = {"yes_dollars": [["0.8700", "4.00"], ["0.8766", "0.75"]]}
        walk = walk_buy(book, side="no", limit=Decimal("0.1234"), quantity=Decimal("0.5"))
        self.assertEqual(walk.fills, (Fill(Decimal("0.1234"), Decimal("0.5")),))
        self.assertEqual(walk.notional, Decimal("0.0617"))

    def test_empty_opposite_side_and_quantity_above_the_book(self):
        empty = walk_buy({"yes_dollars": [["0.40", "10"]]}, side="yes", limit=Decimal("1"))
        self.assertIsNone(empty.best_ask)
        self.assertEqual((empty.quantity_at_limit, empty.fills), (0, ()))
        thin = walk_buy(
            {"no_dollars": [["0.65", "0.75"]]},
            side="yes", limit=Decimal("0.36"), quantity=Decimal("2"),
        )
        self.assertEqual((thin.quantity_at_limit, thin.fills), (Decimal("0.75"), ()))

    def test_invalid_level_fails_closed(self):
        for price, quantity in (("NaN", "1"), ("0.35", "-1"), ("0.35111", "1"), ("0.35", "0.011")):
            with self.subTest(price=price, quantity=quantity), self.assertRaises(ValueError):
                walk_buy({"no_dollars": [[price, quantity]]}, side="yes", limit=Decimal(1))


class FeeTests(unittest.TestCase):
    def test_one_contract_at_35_cents_under_both_precisions(self):
        fills = [Fill(Decimal("0.35"), Decimal(1))]
        for precision, total in ((WHOLE_CENT, "0.37"), (SUB_CENT, "0.3660")):
            with self.subTest(precision=precision):
                result = buy_fees(fills, multiplier=Decimal(1), balance_precision=precision)
                self.assertEqual(result.per_fill[0].model_fee, Decimal("0.015925"))
                self.assertEqual(result.total_debit, Decimal(total))

    def test_documented_whole_cent_example_rounds_up_the_trade_fee(self):
        fees = buy_fees(
            [Fill(Decimal("0.055"), Decimal(1))],
            multiplier=Decimal(1), balance_precision=WHOLE_CENT,
        )
        fill = fees.per_fill[0]
        self.assertEqual(fill.model_fee, Decimal("0.00363825"))
        self.assertEqual(fill.trade_fee, Decimal("0.003639"))
        self.assertEqual(fill.rounding_fee, Decimal("0.001361"))
        self.assertEqual(fees.total_debit, Decimal("0.06"))

    def test_one_and_ninety_nine_cent_prices(self):
        for price, whole, sub in (("0.01", "0.02", "0.0107"), ("0.99", "1.00", "0.9907")):
            with self.subTest(price=price):
                fills = [Fill(Decimal(price), Decimal(1))]
                cent_result = buy_fees(fills, multiplier=Decimal(1), balance_precision=WHOLE_CENT)
                sub_result = buy_fees(fills, multiplier=Decimal(1), balance_precision=SUB_CENT)
                self.assertEqual(cent_result.per_fill[0].trade_fee, Decimal("0.000693"))
                self.assertEqual(cent_result.total_debit, Decimal(whole))
                self.assertEqual(sub_result.total_debit, Decimal(sub))

    def test_multiplier_changes_the_model_fee(self):
        fees = buy_fees(
            [Fill(Decimal("0.35"), Decimal(1))],
            multiplier=Decimal("0.5"), balance_precision=SUB_CENT,
        )
        self.assertEqual(fees.per_fill[0].model_fee, Decimal("0.0079625"))
        self.assertEqual(fees.per_fill[0].trade_fee, Decimal("0.007963"))
        self.assertEqual(fees.total_debit, Decimal("0.3580"))

    def test_rebate_cap_preserves_carry_for_later_fills(self):
        small = Fill(Decimal("0.50"), Decimal("0.01"))
        large = Fill(Decimal("0.50"), Decimal(1))
        fees = buy_fees([small, small, small, large], multiplier=Decimal(1), balance_precision=WHOLE_CENT)
        self.assertEqual(fees.per_fill[2].rounding_carry, Decimal("0.014475"))
        self.assertEqual(fees.per_fill[2].rebate, Decimal(0))
        self.assertEqual(fees.per_fill[3].rebate, Decimal("0.01"))
        self.assertEqual(fees.per_fill[3].rounding_carry, Decimal("0.006975"))
        self.assertTrue(all(fill.net_fee >= 0 for fill in fees.per_fill))

    def test_sub_cent_rebate_and_order_reset(self):
        fill = Fill(Decimal("0.35"), Decimal(1))
        two = buy_fees([fill, fill], multiplier=Decimal(1), balance_precision=SUB_CENT)
        one = buy_fees([fill], multiplier=Decimal(1), balance_precision=SUB_CENT)
        self.assertEqual(two.per_fill[1].rebate, Decimal("0.0001"))
        self.assertEqual(two.total_debit, Decimal("0.7319"))
        self.assertEqual(one.per_fill[0].rebate, Decimal(0))


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.world = World(datetime(2026, 11, 10, 22, 52, tzinfo=timezone.utc))
        self.addCleanup(self.world.tmp.cleanup)
        self.data_dir = Path(self.world.tmp.name)

    def replay(self, recorder, city="NY", ticker="KXHIGHNY-26NOV11-B61.5", limit="1"):
        return replay_capture(
            self.data_dir, Path(recorder.base) / "run.json",
            city=city, ticker=ticker, side="yes", limit=Decimal(limit), fee_multiplier=Decimal(1),
        )

    def test_replays_an_eligible_city_from_the_recorders_raw_first_book(self):
        recorder, _ = self.world.run_at()
        result = self.replay(recorder)
        self.assertEqual(result["status"], "simulated_fill")
        self.assertEqual(result["event_ticker"], "KXHIGHNY-26NOV11")
        self.assertEqual(result["first_book_path"], f"{recorder.base}/{CUTOFF_DIR}/books_1.json")
        self.assertEqual(result["first_book_sha256"], recorder.run["files"][result["first_book_path"]]["sha256"])
        self.assertEqual(sum(fill["quantity"] for fill in result["fills"]), Decimal(1))
        self.assertEqual(
            result["primary_whole_cent"]["total_debit"],
            result["notional"] + result["primary_whole_cent"]["net_fee"],
        )

    def test_on_time_batch_does_not_override_an_ineligible_city(self):
        self.world.replay.drop_books.add("KXHIGHMIA-")
        recorder, _ = self.world.run_at()
        self.assertTrue(recorder.run["cutoffs"][0]["books"]["timing_eligible"])
        result = self.replay(recorder, city="MIA", ticker="KXHIGHMIA-26NOV11-B83.5")
        self.assertEqual((result["status"], result["reason_code"], result["capture_reason_code"]),
                         ("skipped", "capture_ineligible", "missing_books"))
        self.assertEqual(self.replay(recorder)["status"], "simulated_fill")

    def test_depth_skip_reports_displayed_quantity_without_fees(self):
        recorder, _ = self.world.run_at()
        result = self.replay(recorder, limit="0")
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "insufficient_depth"))
        self.assertEqual(result["quantity_at_limit"], Decimal(0))
        self.assertNotIn("primary_whole_cent", result)

    def test_modified_book_fails_manifest_validation(self):
        recorder, _ = self.world.run_at()
        book_path = self.data_dir / recorder.base / CUTOFF_DIR / "books_1.json"
        book_path.write_bytes(book_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "does not match the capture manifest"):
            self.replay(recorder)


if __name__ == "__main__":
    unittest.main()
