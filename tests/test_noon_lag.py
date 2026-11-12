"""The H1 archive-lag check compares a live noon snapshot with a later archive."""
import unittest
from datetime import datetime, timezone

from verify.noon_lag import from_iem_csv, from_nws_geojson, iem_station, lag_check
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



IEM_CSV = """station,valid,tmpf
NYC,2026-11-11 14:51,51.08
NYC,2026-11-11 15:51,51.98
NYC,2026-11-11 16:20,M
NYC,2026-11-11 16:51,53.60
LGA,2026-11-11 15:51,60.00
"""


class IemArchiveTests(unittest.TestCase):
    """ChatGPT #23: H1 trains on the IEM ASOS/METAR CSV (onlycomma, UTC, tmpf, report types 3+4)."""

    def test_station_ids_match_nws_to_iem(self):
        self.assertEqual([iem_station(s) for s in ("KNYC", "KMDW", "KMIA", "KDEN", "KLAX")],
                         ["NYC", "MDW", "MIA", "DEN", "LAX"])

    def test_csv_is_filtered_to_the_station_and_converted_to_celsius(self):
        rows = from_iem_csv("#DEBUG: comment line\n" + IEM_CSV, "KNYC")
        self.assertEqual([r["timestamp"][:16] for r in rows],
                         ["2026-11-11T14:51", "2026-11-11T15:51", "2026-11-11T16:20", "2026-11-11T16:51"])
        self.assertAlmostEqual(rows[0]["temperature_c"], 10.6, places=6)
        self.assertIsNone(rows[2]["temperature_c"])  # "M" is missing, never zero

    def test_comparison_allows_for_source_precision_but_flags_real_edits(self):
        archive = from_iem_csv(IEM_CSV, "KNYC")
        live = [{"timestamp": "2026-11-11T14:51:00+00:00", "temperature_c": 10.6},   # same reading
                {"timestamp": "2026-11-11T15:51:00+00:00", "temperature_c": 11.0},   # whole-degree C only
                {"timestamp": "2026-11-11T16:51:00+00:00", "temperature_c": 10.6}]   # archive says 12.0
        out = lag_check(snapshot(live), archive, NOON, START)
        self.assertEqual(out["within_precision"], 1)
        self.assertEqual([c["timestamp"] for c in out["changed"]], ["2026-11-11T16:51Z"])
        # The missing 16:20 report has no temperature, so no lag counts it as allowed.
        self.assertEqual(out["lags"]["20"], {"allowed": 2, "present": 2, "missing": [], "share_present": 1.0})


if __name__ == "__main__":
    unittest.main()
