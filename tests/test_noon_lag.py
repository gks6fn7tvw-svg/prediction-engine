"""The H1 archive-lag check compares a live noon snapshot with a later archive."""
import unittest
from datetime import datetime, timezone

from verify.noon_lag import from_nws_geojson, lag_check
from tests.helpers import observations

UTC = timezone.utc
NOON = datetime(2026, 11, 11, 17, tzinfo=UTC)
START = datetime(2026, 11, 11, 5, tzinfo=UTC)


def snapshot(rows, ok=True, on_time=True, age=540.0):
    return {"ok": ok, "received_before_cutoff": on_time, "usable": rows, "newest_usable_age_seconds": age}


class LagCheckTests(unittest.TestCase):
    def setUp(self):
        self.archive = from_nws_geojson(observations("KNYC", "2026-11-11T05:00:00Z", "2026-11-11T17:00:00Z"))
        self.full = [{"timestamp": r["timestamp"], "temperature_c": r["temperature_c"]}
                     for r in self.archive if r["temperature_c"] is not None]

    def test_complete_snapshot_has_everything_both_lags_allow(self):
        out = lag_check(snapshot(self.full), self.archive, NOON, START)
        # :51 reports from 05:51 to 16:51. 20-min lag allows up to 16:40 -> 11; 60-min up to 16:00 -> 11.
        self.assertEqual(out["lags"]["20"], {"allowed": 11, "present": 11, "missing": [], "share_present": 1.0})
        self.assertEqual(out["lags"]["60"]["allowed"], 11)
        self.assertEqual((out["changed"], out["live_only"]), ([], []))
        self.assertEqual(out["newest_usable_age_seconds"], 540.0)

    def test_a_report_that_arrived_after_noon_is_flagged_missing(self):
        late = "2026-11-11T15:51:00+00:00"
        live = [r for r in self.full if r["timestamp"] != late]
        out = lag_check(snapshot(live), self.archive, NOON, START)
        self.assertEqual(out["lags"]["20"]["missing"], ["2026-11-11T15:51Z"])
        self.assertEqual(out["lags"]["60"]["missing"], ["2026-11-11T15:51Z"])
        self.assertEqual(out["lags"]["20"]["share_present"], round(10 / 11, 4))

    def test_the_newest_report_only_counts_against_the_lag_that_allows_it(self):
        archive = self.archive + [{"timestamp": "2026-11-11T16:35:00Z", "temperature_c": 15.0}]
        out = lag_check(snapshot(self.full), archive, NOON, START)
        self.assertEqual(out["lags"]["20"]["missing"], ["2026-11-11T16:35Z"])  # 16:35 <= 16:40
        self.assertEqual(out["lags"]["60"]["missing"], [])                     # 16:35 > 16:00

    def test_later_temperature_edits_are_reported(self):
        archive = [dict(r) for r in self.archive]
        archive[3]["temperature_c"] = (archive[3]["temperature_c"] or 0) + 1.0
        out = lag_check(snapshot(self.full), archive, NOON, START)
        self.assertEqual(len(out["changed"]), 1)

    def test_a_late_or_failed_snapshot_has_nothing_present(self):
        out = lag_check(snapshot(self.full, on_time=False), self.archive, NOON, START)
        self.assertEqual(out["lags"]["20"]["present"], 0)
        self.assertFalse(out["live_snapshot_on_time"])

    def test_timestamps_match_to_the_minute_across_formats(self):
        live = [{"timestamp": r["timestamp"].replace("+00:00", "Z").replace(":00Z", ":00.000Z"),
                 "temperature_c": r["temperature_c"]} for r in self.full]
        out = lag_check(snapshot(live), self.archive, NOON, START)
        self.assertEqual(out["lags"]["20"]["missing"], [])


if __name__ == "__main__":
    unittest.main()
