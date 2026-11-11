import unittest
from datetime import date, datetime, timedelta, timezone

from recorder.config import CITIES
from recorder.schedule import climate_day_utc, cutoff_utc, due_cutoffs, event_suffix, event_ticker

BY = {c.key: c for c in CITIES}
UTC = timezone.utc
LEAD, LATE = timedelta(minutes=10), timedelta(minutes=20)


class ScheduleTests(unittest.TestCase):
    def test_event_ticker_matches_kalshi_format(self):
        self.assertEqual(event_suffix(date(2026, 11, 11)), "26NOV11")
        self.assertEqual(event_ticker(BY["NY"], date(2026, 11, 11)), "KXHIGHNY-26NOV11")
        self.assertEqual(event_suffix(date(2027, 3, 5)), "27MAR05")

    def test_winter_cutoffs_in_utc(self):
        day = date(2026, 11, 10)
        self.assertEqual(cutoff_utc(BY["NY"], day), datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
        self.assertEqual(cutoff_utc(BY["MIA"], day), datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
        self.assertEqual(cutoff_utc(BY["CHI"], day), datetime(2026, 11, 11, 0, 0, tzinfo=UTC))
        self.assertEqual(cutoff_utc(BY["DEN"], day), datetime(2026, 11, 11, 1, 0, tzinfo=UTC))
        self.assertEqual(cutoff_utc(BY["LAX"], day), datetime(2026, 11, 11, 2, 0, tzinfo=UTC))

    def test_summer_cutoff_follows_daylight_saving(self):
        self.assertEqual(cutoff_utc(BY["NY"], date(2026, 7, 1)), datetime(2026, 7, 1, 22, 0, tzinfo=UTC))
        self.assertEqual(cutoff_utc(BY["LAX"], date(2026, 7, 1)), datetime(2026, 7, 2, 1, 0, tzinfo=UTC))

    def test_climate_day_uses_standard_time_all_year(self):
        # Midnight EST is 05:00 UTC in winter and in summer (when the clock reads 1 a.m. EDT).
        for target in (date(2026, 11, 11), date(2026, 7, 2)):
            start, end = climate_day_utc(BY["NY"], target)
            self.assertEqual(start, datetime(target.year, target.month, target.day, 5, 0, tzinfo=UTC))
            self.assertEqual(end - start, timedelta(hours=24))
        start, _ = climate_day_utc(BY["LAX"], date(2026, 7, 2))
        self.assertEqual(start, datetime(2026, 7, 2, 8, 0, tzinfo=UTC))

    def test_due_when_cutoff_is_minutes_ahead(self):
        due = due_cutoffs(datetime(2026, 11, 10, 22, 52, tzinfo=UTC), CITIES, LEAD, LATE)
        self.assertEqual(list(due), [datetime(2026, 11, 10, 23, 0, tzinfo=UTC)])
        got = {(c.key, t) for c, t in due[datetime(2026, 11, 10, 23, 0, tzinfo=UTC)]}
        self.assertEqual(got, {("NY", date(2026, 11, 11)), ("MIA", date(2026, 11, 11))})

    def test_due_when_slightly_late_but_not_when_very_late(self):
        late = due_cutoffs(datetime(2026, 11, 10, 23, 15, tzinfo=UTC), CITIES, LEAD, LATE)
        self.assertEqual({c.key for items in late.values() for c, _ in items}, {"NY", "MIA"})
        too_late = due_cutoffs(datetime(2026, 11, 10, 23, 25, tzinfo=UTC), CITIES, LEAD, LATE)
        self.assertEqual(too_late, {})

    def test_every_city_gets_exactly_one_cutoff_per_day_across_a_dst_change(self):
        # Walk hour by hour (at minute 52) through the March 2027 clock change.
        seen = {c.key: [] for c in CITIES}
        now = datetime(2027, 3, 12, 0, 52, tzinfo=UTC)
        while now < datetime(2027, 3, 16, 0, 52, tzinfo=UTC):
            for cutoff, items in due_cutoffs(now, CITIES, LEAD, LATE).items():
                if cutoff > now:  # the run that prepares ahead of the cutoff
                    for c, target in items:
                        seen[c.key].append(target)
            now += timedelta(hours=1)
        for key, targets in seen.items():
            # Consecutive days: no day skipped and none captured twice. (The count is 4 or 5
            # depending on where the city's cutoff falls relative to the window edges.)
            self.assertIn(len(targets), (4, 5), key)
            for earlier, later in zip(targets, targets[1:]):
                self.assertEqual(later - earlier, timedelta(days=1), key)


if __name__ == "__main__":
    unittest.main()
