"""H1 archive-lag check: does a later observation archive show reports the live noon snapshot did not have?

The same-day backtest assumes a report stamped t was usable at noon if t <= noon - lag, for
lag 20 or 60 minutes (ChatGPT #21). The live noon capture records which reports had
actually arrived, with usable temperatures, before noon. This compares the two, per
station and day (ChatGPT #22):

- allowed:   archive reports with a usable temperature and timestamp <= noon - lag
- present:   allowed reports that were in the live snapshot with a usable temperature
- missing:   allowed reports the live snapshot did not have (the backtest would be seeing
             data a live trader could not)
- changed:   reports in both whose temperatures differ by more than 0.05 C (later QC edits)
- freshness: age of the newest usable report in the live snapshot

Timestamps are matched to the minute, since sources format seconds differently.
The archive can be any list of {"timestamp", "temperature_c"}; a later NWS
/observations response (GeoJSON) is converted by `from_nws_geojson`.
Development dates only: this reads no prices and no outcomes.
"""
import json
from datetime import datetime, timedelta, timezone

LAGS_MINUTES = (20, 60)
TEMP_TOLERANCE_C = 0.05


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _minute(s: str) -> str:
    return _utc(s).strftime("%Y-%m-%dT%H:%MZ")


def from_nws_geojson(raw: bytes) -> list[dict]:
    rows = []
    for ft in (json.loads(raw).get("features") or []):
        p = ft.get("properties") or {}
        if p.get("timestamp"):
            rows.append({"timestamp": p["timestamp"], "temperature_c": (p.get("temperature") or {}).get("value")})
    return rows


def lag_check(snapshot: dict, archive: list[dict], cutoff: datetime, climate_start: datetime,
              lags=LAGS_MINUTES) -> dict:
    """`snapshot` is a noon record's info["observations"] from run.json."""
    on_time = bool(snapshot.get("ok") and snapshot.get("received_before_cutoff"))
    live = {}
    if on_time:
        live = {_minute(r["timestamp"]): r["temperature_c"] for r in snapshot.get("usable") or []
                if r.get("temperature_c") is not None and _utc(r["timestamp"]) <= cutoff}
    usable_archive = {}
    for r in archive:
        t, v = _utc(r["timestamp"]), r.get("temperature_c")
        if v is not None and climate_start <= t <= cutoff:
            usable_archive[_minute(r["timestamp"])] = (t, v)
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
    out["changed"] = [{"timestamp": k, "live_c": live[k], "archive_c": usable_archive[k][1]}
                      for k in sorted(set(live) & set(usable_archive))
                      if abs(live[k] - usable_archive[k][1]) > TEMP_TOLERANCE_C]
    out["live_only"] = sorted(set(live) - set(usable_archive))
    return out
