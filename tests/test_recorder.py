"""End-to-end runs against real Kalshi and NWS responses saved on 2026-11-10."""
import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from recorder.config import CITIES
from recorder.run import Recorder, best_quotes, settlement_signature
from recorder.store import LocalSink
from tests.helpers import GRIDPOINT, FakeClock, Replay, event_markets, fixture

UTC = timezone.utc
TWO = [c for c in CITIES if c.key in ("NY", "MIA")]
CUTOFF_DIR = "cutoff_20261110T2300Z"


class World:
    """One data folder that survives across runs, like the data repository does."""

    def __init__(self, start):
        self.clock = FakeClock(start)
        self.replay = Replay(self.clock)
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = LocalSink(self.tmp.name)

    def run_at(self, when=None, cities=TWO):
        if when:
            self.clock.t = when
        rec = Recorder(self.clock, self.replay, self.sink, cities=cities)
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
        self.assertEqual(books["lateness_seconds"], 0.0)
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
        self.assertEqual(state["cutoffs"]["NY"]["2026-11-11"]["status"], "captured")
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
        self.assertGreater(rec_cut["books"]["lateness_seconds"], 300)
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
