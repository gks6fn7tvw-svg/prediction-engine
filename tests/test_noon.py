"""The same-day noon (local standard time) capture, per ChatGPT #22.

New York and Miami noon LST is 17:00 UTC all year. Saved Kalshi responses from
2026-11-10 hold the Nov 11 event, so a noon capture on Nov 11 can be replayed.
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from recorder.config import CITIES
from recorder.run import Recorder
from recorder.schedule import due_noon, noon_cutoff_utc
from recorder.store import LocalSink
from tests.helpers import FakeClock, Replay, ShortSleepClock

UTC = timezone.utc
TWO = [c for c in CITIES if c.key in ("NY", "MIA")]
BY = {c.key: c for c in CITIES}
NOON = datetime(2026, 11, 11, 17, 0, tzinfo=UTC)


class World:
    def __init__(self, start, clock_type=FakeClock):
        self.clock = clock_type(start)
        self.replay = Replay(self.clock)
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = LocalSink(self.tmp.name)

    def run_at(self, when=None, cities=TWO):
        if when:
            self.clock.t = when
        rec = Recorder(self.clock, self.replay, self.sink, cities=cities, live_check_on_first_run=False)
        return rec, rec.execute()

    def state(self):
        return json.loads(self.sink.read("state/state.json"))


class NoonScheduleTests(unittest.TestCase):
    def test_noon_is_local_standard_time_all_year(self):
        self.assertEqual(noon_cutoff_utc(BY["NY"], date(2026, 11, 11)), datetime(2026, 11, 11, 17, tzinfo=UTC))
        self.assertEqual(noon_cutoff_utc(BY["NY"], date(2026, 7, 1)), datetime(2026, 7, 1, 17, tzinfo=UTC))
        self.assertEqual(noon_cutoff_utc(BY["LAX"], date(2026, 7, 1)), datetime(2026, 7, 1, 20, tzinfo=UTC))
        self.assertEqual(noon_cutoff_utc(BY["DEN"], date(2026, 11, 11)), datetime(2026, 11, 11, 19, tzinfo=UTC))

    def test_noon_targets_the_same_day(self):
        due = due_noon(datetime(2026, 11, 11, 16, 52, tzinfo=UTC), CITIES, timedelta(minutes=10),
                       timedelta(minutes=20))
        self.assertEqual({(c.key, t) for c, t in due[NOON]},
                         {("NY", date(2026, 11, 11)), ("MIA", date(2026, 11, 11))})


class NoonCaptureTests(unittest.TestCase):
    def setUp(self):
        self.w = World(datetime(2026, 11, 11, 16, 52, tzinfo=UTC))
        self.addCleanup(self.w.tmp.cleanup)

    def capture(self):
        rec, summary = self.w.run_at()
        return rec, summary, rec.run["noon"][0]

    def test_on_time_capture_is_eligible_and_saves_inputs_only(self):
        rec, summary, noon = self.capture()
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["noon_captures"], 1)
        for key in ("NY", "MIA"):
            c = noon["cities"][key]
            self.assertEqual(c["event_ticker"], f"KXHIGH{key}-26NOV11")
            self.assertEqual((c["status"], c["eligible"], c["reason_code"]), ("captured", True, "on_time"))
            self.assertEqual(len(c["tickers"]), 6)
            self.assertTrue(c["observations"]["received_before_cutoff"])
        books = noon["books"]
        self.assertGreaterEqual(books["request_lateness_seconds"], 0.0)
        self.assertLess(books["request_lateness_seconds"], 1.0)
        self.assertGreaterEqual(books["recheck_gap_seconds"], 1.0)
        # Inputs only: markets, observations and books. No settlement, CLI or outcome files.
        folder = f"{rec.base}/noon_20261111T1700Z/"
        names = sorted(p[len(folder):] for p in rec.run["files"] if p.startswith(folder))
        self.assertEqual(names, ["MIA/kalshi_markets.json", "MIA/nws_observations.json", "NY/kalshi_markets.json",
                                 "NY/nws_observations.json", "books_1.json", "books_2.json"])
        self.assertEqual(rec.run["settlement"], [])

    def test_observations_cover_the_climate_day_up_to_noon(self):
        _, _, noon = self.capture()
        obs = noon["cities"]["NY"]["observations"]
        # Climate day starts 05:00 UTC; hourly :51 reports up to 16:51 -> 12 usable; one null is dropped.
        self.assertEqual(obs["usable_reports"], 12)
        self.assertEqual(obs["reports"], 13)
        self.assertEqual(obs["newest_usable_timestamp"][:16], "2026-11-11T16:51")
        self.assertEqual(obs["newest_usable_age_seconds"], 540.0)
        self.assertTrue(all(r["temperature_c"] is not None for r in obs["usable"]))
        self.assertLess(obs["requested_at"], obs["received_at"])
        self.assertLessEqual(obs["received_at"], "2026-11-11T17:00:00")

    def test_late_weather_is_ineligible_and_never_delays_the_books(self):
        self.w.replay.latency_for["/observations"] = 20.0  # longer than each city's share of the window
        rec, summary, noon = self.capture()
        self.assertLess(noon["books"]["request_lateness_seconds"], 1.0)
        for key in ("NY", "MIA"):
            c = noon["cities"][key]
            self.assertEqual((c["status"], c["eligible"], c["reason_code"]), ("captured", False, "weather_late"))
            self.assertFalse(c["observations"]["received_before_cutoff"])
            self.assertTrue(c["observations"]["no_answer_in_time"])
            self.assertGreaterEqual(c["observations"]["tries"], 1)  # each city got a try: no starvation
            # The late response is kept, clearly labelled, and never replaces the noon snapshot.
            late = c["observations_late"]
            self.assertTrue(late["ok"] and late["audit_only"])
            self.assertFalse(late["received_before_cutoff"])
            self.assertEqual(late["usable_reports"], 12)
            self.assertIn(f"{rec.base}/noon_20261111T1700Z/{key}/nws_observations_late.json", rec.run["files"])
            self.assertNotIn(f"{rec.base}/noon_20261111T1700Z/{key}/nws_observations.json", rec.run["files"])
        self.assertEqual(summary["noon_ineligible"], {"NY": "weather_late", "MIA": "weather_late"})
        # No pre-noon weather request ran past the deadline 3 s before noon.
        pre = [f for f in rec.run["fetches"] if f["kind"] == "noon_nws_observations"]
        stop = datetime(2026, 11, 11, 16, 59, 57, tzinfo=UTC)
        self.assertTrue(pre and all(datetime.fromisoformat(f["received_at"]) <= stop for f in pre), pre)

    def test_slow_first_station_does_not_starve_the_second(self):
        self.w.replay.latency_for["KNYC/observations"] = 20.0
        _, _, noon = self.capture()
        self.assertEqual(noon["cities"]["NY"]["reason_code"], "weather_late")
        self.assertEqual((noon["cities"]["MIA"]["eligible"], noon["cities"]["MIA"]["reason_code"]),
                         (True, "on_time"))

    def test_weather_outage_never_delays_the_books(self):
        self.w.replay.broken.add("/observations")
        _, summary, noon = self.capture()
        self.assertLess(noon["books"]["request_lateness_seconds"], 1.0)
        self.assertEqual(noon["cities"]["NY"]["reason_code"], "weather_failed")
        self.assertFalse(noon["cities"]["NY"]["eligible"])
        self.assertGreater(noon["cities"]["NY"]["observations"]["tries"], 1)  # retried within its share
        self.assertTrue(summary["ok"])  # books were captured; the city is just not scoreable

    def test_late_books_win_over_weather_as_the_reason(self):
        self.w.clock.t = datetime(2026, 11, 11, 17, 12, tzinfo=UTC)  # run started 12 minutes late
        self.w.replay.broken.add("/observations")
        _, _, noon = self.capture()
        c = noon["cities"]["NY"]
        self.assertFalse(c["eligible"])
        self.assertNotIn(c["reason_code"], ("on_time", "weather_failed", "weather_late"))
        self.assertTrue(c["observations"]["no_answer_in_time"])  # no time was left before noon
        self.assertEqual(c["observations"]["tries"], 0)

    def test_station_with_no_usable_temperatures_is_ineligible(self):
        from tests import helpers
        original = helpers.observations
        self.addCleanup(setattr, helpers, "observations", original)
        helpers.observations = lambda station, start, end: json.dumps({"features": [
            {"properties": {"timestamp": "2026-11-11T16:51:00+00:00", "temperature": {"value": None}}}]}).encode()
        _, _, noon = self.capture()
        self.assertEqual(noon["cities"]["NY"]["reason_code"], "weather_empty")

    def test_recheck_is_a_measured_second_after_the_first_book_arrives(self):
        w = World(datetime(2026, 11, 11, 16, 52, tzinfo=UTC), clock_type=ShortSleepClock)
        self.addCleanup(w.tmp.cleanup)
        rec, _ = w.run_at()
        books = rec.run["noon"][0]["books"]
        self.assertGreaterEqual(books["recheck_gap_seconds"], 1.0)
        self.assertGreaterEqual(books["request_lateness_seconds"], 0.0)

    def test_noon_has_its_own_state_and_is_not_recaptured(self):
        self.capture()
        st = self.w.state()
        self.assertEqual(st["noon"]["NY"]["2026-11-11"]["status"], "captured")
        self.assertEqual(st["cutoffs"], {})  # the day-before table is untouched
        _, again = self.w.run_at(datetime(2026, 11, 11, 17, 5, tzinfo=UTC))
        self.assertEqual(again["noon_captures"], 0)


class BothSnapshotsTests(unittest.TestCase):
    def test_day_before_and_noon_snapshots_for_the_same_target_both_survive(self):
        w = World(datetime(2026, 11, 10, 22, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        first, s1 = w.run_at()            # 6 p.m. Nov 10 capture, target Nov 11
        second, s2 = w.run_at(datetime(2026, 11, 11, 16, 52, tzinfo=UTC))  # noon Nov 11, target Nov 11
        self.assertEqual((s1["cutoffs"], s2["noon_captures"]), (1, 1))
        st = w.state()
        for key in ("NY", "MIA"):
            self.assertEqual(st["cutoffs"][key]["2026-11-11"]["status"], "captured")
            self.assertEqual(st["noon"][key]["2026-11-11"]["status"], "captured")
            self.assertEqual(st["noon"][key]["2026-11-11"]["run_id"], second.run_id)
        self.assertIsNotNone(w.sink.read(f"{first.base}/cutoff_20261110T2300Z/books_1.json"))
        self.assertIsNotNone(w.sink.read(f"{second.base}/noon_20261111T1700Z/books_1.json"))
        # The event is still followed to settlement once, from the day-before capture.
        self.assertEqual(sorted(st["events"]), ["KXHIGHMIA-26NOV11", "KXHIGHNY-26NOV11"])


class NoonMissedTests(unittest.TestCase):
    def test_missed_noon_is_recorded_once_and_only_after_the_feature_started(self):
        w = World(datetime(2026, 11, 11, 16, 52, tzinfo=UTC))
        self.addCleanup(w.tmp.cleanup)
        first, _ = w.run_at()
        self.assertEqual(first.run["noon_missed"], [])  # nothing before the feature existed
        rec, summary = w.run_at(datetime(2026, 11, 13, 3, 52, tzinfo=UTC))
        self.assertEqual(sorted((m["city"], m["target_date"]) for m in rec.run["noon_missed"]),
                         [("MIA", "2026-11-12"), ("NY", "2026-11-12")])
        # Day-before misses (targets Nov 12 and 13) are counted separately from noon misses.
        self.assertEqual((summary["missed"], summary["noon_missed"]), (4, 2))
        rec2, _ = w.run_at(datetime(2026, 11, 13, 4, 52, tzinfo=UTC))
        self.assertEqual(rec2.run["noon_missed"], [])


if __name__ == "__main__":
    unittest.main()
