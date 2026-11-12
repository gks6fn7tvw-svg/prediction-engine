"""Cutoff scheduling.

The cutoff is 6 p.m. in each city's IANA civil time (so it follows daylight
saving). The climate day the contract measures is different: midnight to
midnight local STANDARD time, which is 1 a.m. to 1 a.m. on the clock during
daylight saving. Both are computed here so the two are never confused.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import CUTOFF_HOUR, City

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def event_suffix(d: date) -> str:
    """Kalshi's date code for an event, e.g. 2026-11-11 -> '26NOV11'."""
    return f"{d.year % 100:02d}{_MONTHS[d.month - 1]}{d.day:02d}"


def event_ticker(city: City, target: date) -> str:
    return f"{city.series}-{event_suffix(target)}"


def cutoff_utc(city: City, local_day: date) -> datetime:
    """UTC instant of the 6 p.m. cutoff on the given local calendar day."""
    local = datetime.combine(local_day, time(CUTOFF_HOUR, 0), tzinfo=ZoneInfo(city.tz))
    return local.astimezone(timezone.utc)


def climate_day_utc(city: City, target: date) -> tuple[datetime, datetime]:
    """UTC start and end of the climate day (midnight-midnight local standard time)."""
    lst = timezone(timedelta(hours=city.lst_utc_offset))
    start = datetime.combine(target, time(0, 0), tzinfo=lst).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def cutoffs_between(city: City, start: datetime, end: datetime) -> list[tuple[datetime, date]]:
    """All (cutoff_utc, target_date) for this city with start <= cutoff <= end."""
    out = []
    day = start.astimezone(ZoneInfo(city.tz)).date() - timedelta(days=1)
    last = end.astimezone(ZoneInfo(city.tz)).date() + timedelta(days=1)
    while day <= last:
        c = cutoff_utc(city, day)
        if start <= c <= end:
            out.append((c, day + timedelta(days=1)))
        day += timedelta(days=1)
    return out


def due_cutoffs(now: datetime, cities: list[City], lead: timedelta, max_late: timedelta):
    """Cutoffs to capture on this run, grouped by instant.

    A cutoff is due if it is at most `lead` ahead of now (we wait for it) or
    at most `max_late` behind now (we capture immediately and record how late).
    Returns {cutoff_utc: [(city, target_date), ...]} sorted by time.
    """
    due: dict[datetime, list[tuple[City, date]]] = {}
    for city in cities:
        for c, target in cutoffs_between(city, now - max_late, now + lead):
            due.setdefault(c, []).append((city, target))
    return dict(sorted(due.items()))


# ---- Same-day decision time (H1): noon local STANDARD time, all year ----

NOON_HOUR_LST = 12


def noon_cutoff_utc(city: City, day: date) -> datetime:
    """UTC instant of noon local standard time on `day` (1 p.m. on the clock during daylight saving)."""
    lst = timezone(timedelta(hours=city.lst_utc_offset))
    return datetime.combine(day, time(NOON_HOUR_LST, 0), tzinfo=lst).astimezone(timezone.utc)


def noon_cutoffs_between(city: City, start: datetime, end: datetime) -> list[tuple[datetime, date]]:
    """All (noon_cutoff_utc, target_date) with start <= cutoff <= end. The target is the same day."""
    lst = timezone(timedelta(hours=city.lst_utc_offset))
    out = []
    day = start.astimezone(lst).date() - timedelta(days=1)
    last = end.astimezone(lst).date() + timedelta(days=1)
    while day <= last:
        c = noon_cutoff_utc(city, day)
        if start <= c <= end:
            out.append((c, day))
        day += timedelta(days=1)
    return out


def due_noon(now: datetime, cities: list[City], lead: timedelta, max_late: timedelta):
    """Noon captures to take on this run, grouped by instant (same window rule as due_cutoffs)."""
    due: dict[datetime, list[tuple[City, date]]] = {}
    for city in cities:
        for c, target in noon_cutoffs_between(city, now - max_late, now + lead):
            due.setdefault(c, []).append((city, target))
    return dict(sorted(due.items()))
