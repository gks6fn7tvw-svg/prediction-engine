"""Fixture and regression tests for the offline paper replay."""

import hashlib
import json
import unittest
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from paper.execution import SUB_CENT, WHOLE_CENT, Fill, buy_fees, walk_buy, whole_cent_worst_case
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
        worst = whole_cent_worst_case(walk.fills, multiplier=Decimal(1))
        sub = buy_fees(walk.fills, multiplier=Decimal(1), balance_precision=SUB_CENT)
        self.assertEqual(whole.total_debit, Decimal("0.38"))
        self.assertEqual(whole.per_fill[1].rebate, Decimal("0.01"))
        self.assertEqual(worst.total_debit, Decimal("1.00"))
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
    def test_five_cent_order_can_cost_one_dollar_with_minimum_size_fills(self):
        fills = [Fill(Decimal("0.05"), Decimal(1))]
        best = buy_fees(fills, multiplier=Decimal(1), balance_precision=WHOLE_CENT)
        worst = whole_cent_worst_case(fills, multiplier=Decimal(1))
        sub = buy_fees(fills, multiplier=Decimal(1), balance_precision=SUB_CENT)
        self.assertEqual(best.total_debit, Decimal("0.06"))
        self.assertEqual(sub.total_debit, Decimal("0.0534"))
        self.assertEqual(len(worst.per_fill), 100)
        self.assertTrue(all(fill.rebate == 0 for fill in worst.per_fill))
        self.assertEqual(worst.net_fee, Decimal("0.95"))
        self.assertEqual(worst.total_debit, Decimal("1.00"))

    def test_fragmentation_keeps_rebates_when_the_cap_allows_them(self):
        worst = whole_cent_worst_case([Fill(Decimal("0.80"), Decimal(1))], multiplier=Decimal(20))
        self.assertEqual(sum(fill.rebate for fill in worst.per_fill), Decimal("0.97"))
        self.assertEqual(worst.total_debit, Decimal("1.03"))

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
            city=city, ticker=ticker, side="yes", limit=Decimal(limit),
        )

    def replace_recorded_json(self, recorder, name, edit):
        relative = f"{recorder.base}/{CUTOFF_DIR}/{name}"
        path = self.data_dir / relative
        payload = json.loads(path.read_bytes())
        edit(payload)
        data = json.dumps(payload).encode()
        path.write_bytes(data)
        run_path = self.data_dir / recorder.base / "run.json"
        run = json.loads(run_path.read_bytes())
        run["files"][relative] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        run_path.write_text(json.dumps(run), encoding="utf-8")
        return run["files"][relative]["sha256"]

    def test_replays_an_eligible_city_from_the_recorders_raw_first_book(self):
        recorder, _ = self.world.run_at()
        result = self.replay(recorder)
        self.assertEqual(result["status"], "simulated_fill")
        self.assertEqual(result["event_ticker"], "KXHIGHNY-26NOV11")
        self.assertEqual(result["first_book_path"], f"{recorder.base}/{CUTOFF_DIR}/books_1.json")
        self.assertEqual(result["first_book_sha256"], recorder.run["files"][result["first_book_path"]]["sha256"])
        self.assertEqual(sum(fill["quantity"] for fill in result["fills"]), Decimal(1))
        self.assertEqual(result["fee_multiplier"], Decimal(1))
        self.assertIsNone(result["primary_fee_scenario"])
        self.assertEqual(
            result["whole_cent_best_case"]["total_debit"],
            result["notional"] + result["whole_cent_best_case"]["net_fee"],
        )
        self.assertEqual(result["whole_cent_worst_case"]["total_debit"], Decimal("1.00"))
        self.assertEqual(result["sub_cent_best_case"]["total_debit"], Decimal("0.6464"))

    def test_replay_prices_the_recorded_fractional_multiplier(self):
        recorder, _ = self.world.run_at()
        source_hash = self.replace_recorded_json(
            recorder, "NY/kalshi_series.json", lambda payload: payload["series"].update(fee_multiplier=0.5),
        )
        result = self.replay(recorder)
        self.assertEqual(result["fee_type"], "quadratic")
        self.assertEqual(result["fee_multiplier"], Decimal("0.5"))
        self.assertEqual(result["fee_series_sha256"], source_hash)
        self.assertEqual(result["sub_cent_best_case"]["total_debit"], Decimal("0.6382"))

    def test_missing_series_metadata_skips_pricing(self):
        recorder, _ = self.world.run_at()
        (self.data_dir / recorder.base / CUTOFF_DIR / "NY/kalshi_series.json").unlink()
        result = self.replay(recorder)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "fee_metadata_unavailable"))
        self.assertNotIn("whole_cent_worst_case", result)

    def test_unsupported_fee_type_or_invalid_multiplier_skips_pricing(self):
        recorder, _ = self.world.run_at()
        for fee_type, multiplier, reason in (
            ("flat", 1, "fee_type_unsupported"),
            ("quadratic", True, "fee_multiplier_invalid"),
            ("quadratic", -1, "fee_multiplier_invalid"),
        ):
            with self.subTest(fee_type=fee_type, multiplier=multiplier):
                self.replace_recorded_json(
                    recorder, "NY/kalshi_series.json",
                    lambda payload: payload["series"].update(fee_type=fee_type, fee_multiplier=multiplier),
                )
                result = self.replay(recorder)
                self.assertEqual((result["status"], result["reason_code"]), ("skipped", reason))

    def test_manifest_valid_book_with_malformed_level_skips_pricing(self):
        recorder, _ = self.world.run_at()

        def break_level(payload):
            book = next(row for row in payload["orderbooks"] if row["ticker"] == "KXHIGHNY-26NOV11-B61.5")
            book["orderbook_fp"]["no_dollars"].append(["invalid_price", "1.00"])

        source_hash = self.replace_recorded_json(recorder, "books_1.json", break_level)
        result = self.replay(recorder)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "book_unparseable"))
        self.assertEqual(result["first_book_sha256"], source_hash)

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
        self.assertNotIn("whole_cent_worst_case", result)

    def test_modified_book_fails_manifest_validation(self):
        recorder, _ = self.world.run_at()
        book_path = self.data_dir / recorder.base / CUTOFF_DIR / "books_1.json"
        book_path.write_bytes(book_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "does not match the capture manifest"):
            self.replay(recorder)


if __name__ == "__main__":
    unittest.main()
