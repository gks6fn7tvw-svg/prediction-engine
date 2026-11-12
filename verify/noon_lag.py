"""H1 archive-lag check: does a later observation archive show reports the live noon snapshot did not have?

The same-day backtest assumes a report stamped t was usable at noon if t <= noon - lag, for
lag 20 or 60 minutes (ChatGPT #21). The live noon capture records which reports had
actually arrived, with usable temperatures, before noon. This compares the two, per
station and day (ChatGPT #22):

- allowed:   archive reports with a usable temperature and timestamp <= noon - lag
- present:   allowed reports that were in the live snapshot with a usable temperature
- missing:   allowed reports the live snapshot did not have (the backtest would be seeing
             data a live trader could not)
- changed:   reports in both whose temperatures differ by more than source precision allows
- freshness: age of the newest usable report in the live snapshot

The archive H1 trains on is the Iowa Environmental Mesonet ASOS/METAR CSV (ChatGPT #23):
format=onlycomma, tz=Etc/UTC, data=tmpf, report_type 3 and 4; columns station, valid
(UTC), tmpf (Fahrenheit, "M" = missing). `from_iem_csv` converts it. IEM names US stations
without the leading K (KNYC -> NYC). Timestamps are matched to the minute.
Development dates only: this reads no prices and no outcomes.
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

LAGS_MINUTES = (20, 60)
# Both sources carry tenths of a degree C when the METAR has its T-group (IEM keeps two
# decimals of F, the NWS API one decimal of C), so rounding alone stays under 0.06 C.
BASE_TOLERANCE_C = 0.06
WHOLE_C_ALLOWANCE = 0.5    # a value with only whole-degree C precision (no T-group)
WHOLE_F_ALLOWANCE = 0.28   # an archive value with only whole-degree F precision


def _utc(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    t = datetime.fromisoformat(s if "T" in s or "+" in s else s.replace(" ", "T"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _minute(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def iem_station(nws_station: str) -> str:
    """IEM's id for an NWS station: four-letter K stations drop the K."""
    return nws_station[1:] if len(nws_station) == 4 and nws_station.startswith("K") else nws_station


def from_nws_geojson(raw: bytes) -> list[dict]:
    rows = []
    for ft in (json.loads(raw).get("features") or []):
        p = ft.get("properties") or {}
        if p.get("timestamp"):
            rows.append({"timestamp": p["timestamp"], "temperature_c": (p.get("temperature") or {}).get("value")})
    return rows


def from_iem_csv(text: str, nws_station: str) -> list[dict]:
    """IEM onlycomma CSV -> rows for one station, Fahrenheit converted to Celsius."""
    want = iem_station(nws_station)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines))):
        if (r.get("station") or "").strip() != want:
            continue
        raw = (r.get("tmpf") or "").strip()
        f = None if raw in ("", "M") else float(raw)
        rows.append({"timestamp": _utc(r["valid"]).isoformat(),
                     "temperature_c": None if f is None else (f - 32) * 5 / 9,
                     "whole_f": f is not None and f == int(f)})
    return rows


def _whole_c(v: float) -> bool:
    return abs(v - round(v)) < 0.01


def tolerance(live_c: float, archive_row: dict) -> float:
    tol = BASE_TOLERANCE_C
    if _whole_c(live_c) or _whole_c(archive_row["temperature_c"]):
        tol += WHOLE_C_ALLOWANCE
    if archive_row.get("whole_f"):
        tol += WHOLE_F_ALLOWANCE
    return tol


def lag_check(snapshot: dict, archive: list[dict], cutoff: datetime, climate_start: datetime,
              lags=LAGS_MINUTES) -> dict:
    """`snapshot` is a noon record's info["observations"] from run.json."""
    on_time = bool(snapshot.get("ok") and snapshot.get("received_before_cutoff"))
    live = {}
    if on_time:
        for r in snapshot.get("usable") or []:
            t = _utc(r["timestamp"])
            if r.get("temperature_c") is not None and t <= cutoff:
                live[_minute(t)] = r["temperature_c"]
    usable_archive = {}
    for r in archive:
        t = _utc(r["timestamp"])
        if r.get("temperature_c") is not None and climate_start <= t <= cutoff:
            usable_archive[_minute(t)] = (t, r)
    out = {"live_snapshot_usable": len(live), "live_snapshot_on_time": on_time,
           "newest_usable_age_seconds": snapshot.get("newest_usable_age_seconds"), "lags": {}}
    for lag in lags:
        limit = cutoff - timedelta(minutes=lag)
        allowed = sorted(k for k, (t, _) in usable_archive.items() if t <= limit)
        present = [k for k in allowed if k in live]
        out["lags"][str(lag)] = {
            "allowed": len(allowed), "present": len(present),
            "missing": [k for k in allowed if k not in live],
            "share_present": round(len(present) / len(allowed), 4) if allowed else None,
        }
    out["changed"], out["within_precision"] = [], 0
    for k in sorted(set(live) & set(usable_archive)):
        row = usable_archive[k][1]
        diff = abs(live[k] - row["temperature_c"])
        if diff > tolerance(live[k], row):
            out["changed"].append({"timestamp": k, "live_c": live[k], "archive_c": round(row["temperature_c"], 3)})
        elif diff > BASE_TOLERANCE_C:
            out["within_precision"] += 1
    out["live_only"] = sorted(set(live) - set(usable_archive))
    return out
