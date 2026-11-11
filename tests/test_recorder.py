"""End-to-end runs against real Kalshi and NWS responses saved on 2026-11-10."""
import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from recorder.config import CITIES
from recorder.run import Recorder, best_quotes, first_book_verdict, settlement_signature
from recorder.store import LocalSink
from tests.helpers import GRIDPOINT, FakeClock, Replay, ShortSleepClock, event_markets, fixture

UTC = timezone.utc
TWO = [c for c in CITIES if c.key in ("NY", "MIA")]
CUTOFF_DIR = "cutoff_20261110T2300Z"


class World:
    """One data folder that survives across runs, like the data repository does."""

    def __init__(self, start, clock_type=FakeClock):
        self.clock = clock_type(start)
        self.replay = Replay(self.clock)
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = LocalSink(self.tmp.name)

    def run_at(self, when=None, cities=TWO, live_check=False):
        if when:
            self.clock.t = when
        rec = Recorder(self.clock, self.replay, self.sink, cities=cities,
                       live_check_on_first_run=live_check)
        return rec, rec.execute()

    def read(self, path):
        return self.sink.read(path)

    def state(self):
        return json.loads(self.read("state/state.json"))


class CutoffCaptureTests(unittest.TestCase):
    def setUp(self):
        self.w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.rec, self.summary = self.w.run_at()
        self.addCleanup(self.w.tmp.cleanup)
        self.base = f"{self.rec.base}/{CUTOFF_DIR}"
        self.run = json.loads(self.w.read(f"{self.rec.base}/run.json"))

    def test_run_succeeds_and_saves_raw_bytes_unchanged(self):
        self.assertTrue(self.summary["ok"], self.summary)
        for name in ("markets", "series"):
            self.assertEqual(self.w.read(f"{self.base}/NY/kalshi_{name}.json"),
                             fixture(f"KXHIGHNY_{name}.json"))
        self.assertEqual(self.w.read(f"{self.base}/MIA/nws_hourly.json"), fixture("KMIA_hourly.json"))
        self.assertEqual(gzip.decompress(self.w.read(f"{self.base}/NY/nws_gridpoint.json.gz")), GRIDPOINT)

    def test_finds_the_six_ranges_for_tomorrows_event(self):
        ny = self.run["cutoffs"][0]["cities"]["NY"]
        self.assertEqual(ny["event_ticker"], "KXHIGHNY-26NOV11")
        self.assertEqual(len(ny["tickers"]), 6)
        self.assertIn("KXHIGHNY-26NOV11-B61.5", ny["tickers"])
        self.assertEqual(ny["status"], "captured")
        self.assertEqual(ny["problems"], [])
        self.assertEqual(ny["climate_day_utc"][0][:16], "2026-11-11T05:00")

    def test_books_are_taken_at_the_cutoff_and_rechecked_after_a_second(self):
        books = self.run["cutoffs"][0]["books"]
        self.assertEqual(books["tickers_requested"], 12)
        self.assertEqual(books["request_lateness_seconds"], 0.0)
        self.assertEqual(books["receipt_lateness_seconds"], 0.2)
        self.assertEqual((books["eligible"], books["reason_code"]), (True, "on_time"))
        self.assertGreaterEqual(books["recheck_gap_seconds"], 1.0)
        first = json.loads(self.w.read(f"{self.base}/books_1.json"))
        self.assertEqual(len(first["orderbooks"]), 12)
        self.assertIsNotNone(self.w.read(f"{self.base}/books_2.json"))

    def test_weather_is_fetched_before_the_cutoff(self):
        cutoff = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
        for when, url in self.w.replay.calls:
            if "weather.gov/gridpoints" in url or "weather.gov/points" in url:
                self.assertLess(when, cutoff, url)
        self.assertTrue(self.run["cutoffs"][0]["cities"]["NY"]["forecast"]["fetched_before_cutoff"])

    def test_derived_values_match_what_was_checked_by_hand(self):
        cities = self.run["cutoffs"][0]["cities"]
        self.assertEqual(cities["NY"]["forecast"]["hourly_max_in_climate_day"], 61)
        self.assertEqual(cities["MIA"]["forecast"]["hourly_max_in_climate_day"], 84)
        self.assertEqual(cities["NY"]["forecast"]["hourly_points_in_climate_day"], 24)
        self.assertEqual(len(cities["NY"]["forecast"]["gridpoint_max_temperature_near_day"]), 1)
        quote = self.run["cutoffs"][0]["books"]["quotes_first"]["KXHIGHNY-26NOV11-B61.5"]
        self.assertEqual((quote["yes_bid"], quote["yes_ask"]), ("0.6200", "0.6300"))

    def test_manifest_hashes_match_stored_files(self):
        self.assertGreaterEqual(len(self.run["files"]), 14)
        for path, meta in self.run["files"].items():
            data = self.w.read(path)
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta["sha256"], path)
        entry = next(f for f in self.run["fetches"] if f.get("path", "").endswith("NY/kalshi_markets.json"))
        self.assertEqual(entry["sha256"], hashlib.sha256(fixture("KXHIGHNY_markets.json")).hexdigest())

    def test_state_tracks_the_capture_and_the_event(self):
        state = self.w.state()
        entry = state["cutoffs"]["NY"]["2026-11-11"]
        self.assertEqual((entry["status"], entry["eligible"], entry["reason_code"]),
                         ("captured", True, "on_time"))
        self.assertEqual(state["events"]["KXHIGHMIA-26NOV11"]["poll_after"][:16], "2026-11-12T05:00")

    def test_same_cutoff_is_not_captured_twice(self):
        _, again = self.w.run_at(datetime(2026, 11, 10, 23, 5, tzinfo=UTC))
        self.assertEqual(again["cutoffs"], 0)

    def test_quiet_hour_writes_nothing(self):
        _, quiet = self.w.run_at(datetime(2026, 11, 11, 3, 52, tzinfo=UTC))
        self.assertEqual((quiet["files"], quiet["commit"], quiet["ok"]), (0, None, True))


class LateAndFailedTests(unittest.TestCase):
    def test_late_start_captures_immediately_and_says_so(self):
        w = World(datetime(2026, 11, 10, 23, 5, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        rec, summary = w.run_at()
        rec_cut = rec.run["cutoffs"][0]
        self.assertTrue(summary["ok"])
        self.assertFalse(rec_cut["started_before_cutoff"])
        self.assertGreater(rec_cut["books"]["request_lateness_seconds"], 300)
        # Five minutes late is inside the ten-minute limit, so it still counts.
        self.assertEqual((rec_cut["books"]["eligible"], rec_cut["books"]["reason_code"]), (True, "on_time"))
        self.assertFalse(rec_cut["cities"]["NY"]["forecast"]["fetched_before_cutoff"])

    def test_missing_markets_is_a_recorded_failure_not_a_crash(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        w.replay.broken.add("series_ticker=KXHIGHMIA")
        rec, summary = w.run_at()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed_captures"], ["MIA"])
        cities = rec.run["cutoffs"][0]["cities"]
        self.assertEqual(cities["NY"]["status"], "captured")
        self.assertIn("markets_unavailable", cities["MIA"]["problems"])
        self.assertEqual((cities["MIA"]["eligible"], cities["MIA"]["reason_code"]), (False, "no_markets"))
        self.assertEqual((cities["NY"]["eligible"], cities["NY"]["reason_code"]), (True, "on_time"))
        self.assertEqual(w.state()["cutoffs"]["MIA"]["2026-11-11"]["status"], "failed")
        fetch = next(f for f in rec.run["fetches"] if "series_ticker=KXHIGHMIA" in f["url"])
        self.assertEqual((fetch["http_status"], fetch["attempts"]), (503, 2))

    def test_skipped_day_is_recorded_as_missed_once(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        w.run_at()
        rec, summary = w.run_at(datetime(2026, 11, 12, 5, 52, tzinfo=UTC))
        self.assertEqual(sorted((m["city"], m["target_date"]) for m in rec.run["missed"]),
                         [("MIA", "2026-11-12"), ("NY", "2026-11-12")])
        rec2, _ = w.run_at(datetime(2026, 11, 12, 6, 52, tzinfo=UTC))
        self.assertEqual(rec2.run["missed"], [])


class CutoffRuleTests(unittest.TestCase):
    """Design v0.3: the first order book must be RECEIVED from the cutoff through ten
    minutes after it, both ends included. Anything else is saved but not scoreable."""

    CUTOFF = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
    NY = [c for c in CITIES if c.key == "NY"]

    def capture(self, request_offset, book_latency):
        """Run so the first book is requested `request_offset` seconds after the cutoff
        and its response takes `book_latency` seconds to arrive."""
        discovery = 2 * 0.2  # the series and markets requests that come first
        w = World(self.CUTOFF + timedelta(seconds=request_offset - discovery))
        self.addCleanup(w.tmp.cleanup)
        w.replay.latency_for["markets/orderbooks"] = book_latency
        rec, summary = w.run_at(cities=self.NY)
        return w, rec, summary, rec.run["cutoffs"][0]

    def test_book_received_exactly_ten_minutes_after_cutoff_passes(self):
        w, rec, summary, cut = self.capture(request_offset=599, book_latency=1.0)
        books = cut["books"]
        self.assertEqual(books["request_lateness_seconds"], 599.0)
        self.assertEqual(books["receipt_lateness_seconds"], 600.0)
        self.assertEqual(books["lateness_limit_seconds"], 600.0)
        self.assertEqual((books["eligible"], books["reason_code"]), (True, "on_time"))
        self.assertTrue(cut["cities"]["NY"]["eligible"])
        self.assertTrue(w.state()["cutoffs"]["NY"]["2026-11-11"]["eligible"])
        self.assertEqual(summary["ineligible_captures"], {})

    def test_request_at_9_59_with_book_arriving_at_10_01_is_saved_late_and_excluded(self):
        w, rec, summary, cut = self.capture(request_offset=599, book_latency=2.0)
        books = cut["books"]
        self.assertEqual(books["request_lateness_seconds"], 599.0)   # sent at +9:59
        self.assertEqual(books["receipt_lateness_seconds"], 601.0)   # arrived at +10:01
        self.assertEqual((books["eligible"], books["reason_code"]), (False, "late_first_book"))
        # Saved in full...
        base = f"{rec.base}/{CUTOFF_DIR}"
        self.assertEqual(len(json.loads(w.read(f"{base}/books_1.json"))["orderbooks"]), 6)
        self.assertIsNotNone(w.read(f"{base}/books_2.json"))
        self.assertEqual(cut["cities"]["NY"]["status"], "captured")
        # ...with the reason code everywhere scoring would look, and not treated as a crash.
        self.assertEqual((cut["cities"]["NY"]["eligible"], cut["cities"]["NY"]["reason_code"]),
                         (False, "late_first_book"))
        entry = w.state()["cutoffs"]["NY"]["2026-11-11"]
        self.assertEqual((entry["status"], entry["eligible"], entry["reason_code"]),
                         ("captured", False, "late_first_book"))
        self.assertEqual(entry["receipt_lateness_seconds"], 601.0)
        self.assertEqual(summary["ineligible_captures"], {"NY": "late_first_book"})
        self.assertEqual(summary["failed_captures"], [])

    def test_one_microsecond_past_the_limit_is_late(self):
        _, _, _, cut = self.capture(request_offset=599, book_latency=1.000001)
        self.assertEqual(cut["books"]["receipt_lateness_seconds"], 600.000001)
        self.assertEqual((cut["books"]["eligible"], cut["books"]["reason_code"]),
                         (False, "late_first_book"))

    def test_a_late_capture_is_not_retried_or_double_counted(self):
        w, _, _, _ = self.capture(request_offset=599, book_latency=2.0)
        _, again = w.run_at(self.CUTOFF + timedelta(minutes=15), cities=self.NY)
        self.assertEqual(again["cutoffs"], 0)
        self.assertEqual(w.state()["cutoffs"]["NY"]["2026-11-11"]["reason_code"], "late_first_book")

    def test_failed_first_book_is_ineligible_with_its_own_reason(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        w.replay.broken.add("markets/orderbooks")
        rec, summary = w.run_at(cities=self.NY)
        city = rec.run["cutoffs"][0]["cities"]["NY"]
        self.assertEqual((city["status"], city["eligible"], city["reason_code"]),
                         ("failed", False, "first_book_failed"))
        self.assertEqual(summary["failed_captures"], ["NY"])

    def test_verdict_boundaries(self):
        c, us = self.CUTOFF, timedelta(microseconds=1)
        ten = timedelta(minutes=10)
        self.assertEqual(first_book_verdict(c, True, c), (True, "on_time"))
        self.assertEqual(first_book_verdict(c, True, c + ten), (True, "on_time"))
        self.assertEqual(first_book_verdict(c, True, c + ten + us), (False, "late_first_book"))
        self.assertEqual(first_book_verdict(c, True, c - us), (False, "early_first_book"))
        self.assertEqual(first_book_verdict(c, False, c), (False, "first_book_failed"))

    def test_waits_are_rechecked_when_sleep_returns_early(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC), clock_type=ShortSleepClock)
        self.addCleanup(w.tmp.cleanup)
        rec, _ = w.run_at(cities=self.NY)
        books = rec.run["cutoffs"][0]["books"]
        self.assertGreaterEqual(books["request_lateness_seconds"], 0.0)  # never before the cutoff
        self.assertLess(books["request_lateness_seconds"], 0.01)
        self.assertGreaterEqual(books["recheck_gap_seconds"], 1.0)        # a full measured second
        self.assertTrue(books["eligible"])


class LiveCheckTests(unittest.TestCase):
    """The one-time check that runs on the first run against an empty data repository."""

    QUIET = datetime(2026, 11, 11, 3, 52, tzinfo=UTC)  # no cutoff due at this hour

    def test_first_run_checks_every_source_once_and_saves_it_apart(self):
        w = World(self.QUIET)
        self.addCleanup(w.tmp.cleanup)
        rec, summary = w.run_at(live_check=True)
        self.assertTrue(summary["ok"], summary)
        self.assertTrue(summary["live_check_ok"])
        self.assertEqual(summary["live_check_problems"], [])
        self.assertEqual(summary["cutoffs"], 0)
        check = rec.run["live_check"]
        ny = check["cities"]["NY"]
        self.assertEqual((ny["fee_type"], ny["fee_multiplier"]), ("quadratic", 1))
        self.assertEqual(ny["settlement_sources"], ["The Weather Company"])
        self.assertEqual(ny["checked_event"], "KXHIGHNY-26NOV11")
        self.assertEqual(check["books"], {"ok": True, "requested": 12, "returned": 12})
        base = f"checks/{rec.run_id}"
        for path in ("books.json", "NY/kalshi_markets.json", "NY/nws_hourly.json",
                     "NY/nws_gridpoint.json.gz", "MIA/nws_cli.html"):
            self.assertIsNotNone(w.read(f"{base}/{path}"), path)
        # Kept apart from real captures: no cutoffs, no events to follow.
        state = w.state()
        self.assertEqual((state["cutoffs"], state["events"]), ({}, {}))
        self.assertEqual(state["live_check"]["run_id"], rec.run_id)

    def test_it_runs_only_once(self):
        w = World(self.QUIET)
        self.addCleanup(w.tmp.cleanup)
        w.run_at(live_check=True)
        rec, summary = w.run_at(self.QUIET + timedelta(hours=1), live_check=True)
        self.assertNotIn("live_check", rec.run)
        self.assertEqual((summary["files"], summary["commit"]), (0, None))

    def test_a_broken_source_fails_the_run_with_the_reason(self):
        w = World(self.QUIET)
        self.addCleanup(w.tmp.cleanup)
        w.replay.broken.add("forecast/hourly")
        _, summary = w.run_at(live_check=True)
        self.assertFalse(summary["ok"])
        self.assertIn("NY:hourly_forecast_unavailable", summary["live_check_problems"])

    def test_an_unreadable_data_repo_fails_the_run(self):
        class BrokenSink(LocalSink):
            def read(self, path):
                raise RuntimeError("HTTP 401 Bad credentials")
        w = World(self.QUIET)
        self.addCleanup(w.tmp.cleanup)
        w.sink = BrokenSink(w.tmp.name)
        rec, summary = w.run_at(live_check=True)
        self.assertFalse(summary["ok"])
        self.assertEqual(rec.run["errors"][0]["phase"], "load_state")
        self.assertNotIn("live_check", rec.run)  # never check against a state we couldn't read


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(self.w.tmp.cleanup)
        self.w.run_at()
        self.ev = "KXHIGHNY-26NOV11"
        for series in ("KXHIGHNY", "KXHIGHMIA"):
            self.w.replay.settlement[f"{series}-26NOV11"] = event_markets(series, f"{series}-26NOV11")

    def polls(self):
        return [u for _, u in self.w.replay.calls if "event_ticker=" in u]

    def test_no_polling_before_the_climate_day_ends(self):
        self.w.run_at(datetime(2026, 11, 12, 4, 52, tzinfo=UTC))
        self.assertEqual(self.polls(), [])

    def test_saves_each_change_once_and_stops_after_finalization(self):
        rec, _ = self.w.run_at(datetime(2026, 11, 12, 5, 52, tzinfo=UTC))
        first = f"settlement/{self.ev}/{rec.run_id}.json"
        self.assertIsNotNone(self.w.read(first))
        self.assertIsNone(self.w.state()["events"][self.ev]["final_at"])

        rec, _ = self.w.run_at(datetime(2026, 11, 12, 6, 52, tzinfo=UTC))
        self.assertIsNone(self.w.read(f"settlement/{self.ev}/{rec.run_id}.json"))

        for series in ("KXHIGHNY", "KXHIGHMIA"):
            self.w.replay.settlement[f"{series}-26NOV11"] = event_markets(
                series, f"{series}-26NOV11", status="finalized", result="no", expiration_value="62")
        rec, _ = self.w.run_at(datetime(2026, 11, 12, 13, 52, tzinfo=UTC))
        self.assertIsNotNone(self.w.read(f"settlement/{self.ev}/{rec.run_id}.json"))
        info = self.w.state()["events"][self.ev]
        self.assertEqual(info["versions"], 2)
        self.assertIsNotNone(info["final_at"])
        self.assertFalse(info["done"])

        self.w.run_at(datetime(2026, 11, 14, 14, 52, tzinfo=UTC))
        before = len(self.polls())
        self.w.run_at(datetime(2026, 11, 14, 15, 52, tzinfo=UTC))
        self.assertEqual(len(self.polls()), before)

    def test_final_event_that_leaves_the_live_endpoint_is_closed_not_reopened(self):
        final = event_markets("KXHIGHNY", self.ev, status="finalized", result="no")
        self.w.replay.settlement[self.ev] = final
        self.w.run_at(datetime(2026, 11, 12, 13, 52, tzinfo=UTC))
        self.w.replay.settlement[self.ev] = []  # moved to Kalshi's historical endpoints
        rec, _ = self.w.run_at(datetime(2026, 11, 12, 14, 52, tzinfo=UTC))
        info = self.w.state()["events"][self.ev]
        self.assertTrue(info["done"])
        self.assertIsNotNone(info["final_at"])
        self.assertNotIn("unresolved", info)
        self.assertIsNone(self.w.read(f"settlement/{self.ev}/{rec.run_id}.json"))

    def test_signature_ignores_prices_but_sees_results(self):
        base = event_markets("KXHIGHNY", self.ev)
        moved = [{**m, "yes_bid_dollars": "0.5000", "volume_fp": "9.00"} for m in base]
        settled = [{**m, "status": "finalized", "result": "yes"} for m in base]
        self.assertEqual(settlement_signature(base)[0], settlement_signature(moved)[0])
        self.assertNotEqual(settlement_signature(base)[0], settlement_signature(settled)[0])
        self.assertEqual((settlement_signature(base)[1], settlement_signature(settled)[1]), (False, True))
        self.assertFalse(settlement_signature([])[1])


class ClimateReportTests(unittest.TestCase):
    def test_report_saved_only_when_its_text_changes(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        rec, _ = w.run_at()
        self.assertEqual(w.read(f"cli/NY/{rec.run_id}.html"), fixture("CLINYC.html"))
        # Same report text inside different page furniture is not a change.
        w.replay.cli_override["NYC"] = fixture("CLINYC.html").replace(b"<body", b"<body data-x='1'", 1)
        rec, _ = w.run_at(datetime(2026, 11, 11, 3, 52, tzinfo=UTC))
        self.assertIsNone(w.read(f"cli/NY/{rec.run_id}.html"))
        w.replay.cli_override["NYC"] = fixture("CLINYC.html").replace(b"MAXIMUM         62", b"MAXIMUM         63")
        rec, _ = w.run_at(datetime(2026, 11, 11, 4, 52, tzinfo=UTC))
        self.assertIn(b"MAXIMUM         63", w.read(f"cli/NY/{rec.run_id}.html"))


class QuoteTests(unittest.TestCase):
    def test_best_quotes_from_a_real_book(self):
        books = json.loads(fixture("KXHIGHNY_orderbooks.json"))["orderbooks"]
        by = {b["ticker"]: best_quotes(b["orderbook_fp"]) for b in books}
        self.assertEqual(by["KXHIGHNY-26NOV11-B59.5"],
                         {"yes_bid": "0.1800", "yes_bid_size": "806.93",
                          "yes_ask": "0.1900", "yes_ask_size": "112.05"})
        self.assertIsNone(by["KXHIGHNY-26NOV11-T66"]["yes_bid"])
        self.assertEqual(by["KXHIGHNY-26NOV11-T66"]["yes_ask"], "0.0100")


if __name__ == "__main__":
    unittest.main()
