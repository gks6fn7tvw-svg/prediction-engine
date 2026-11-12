"""Independent offline checks of a historical batch in the data repository.

Written separately from the collector on purpose: it recomputes cutoffs, climate
days, quote selection and payout identity from the raw files and this repo's own
schedule code, instead of trusting the batch manifest.

    python -m verify.check_history /path/to/prediction-engine-data history/development/2026-09-11

Exit status 0 means every hard check passed. Notes (soft findings) never fail a batch.
These checks need no network. Re-fetching sources from Kalshi and NOAA is a separate step.
"""
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from recorder.config import CITIES
from recorder.schedule import climate_day_utc, cutoff_utc, event_ticker

BY_KEY = {c.key: c for c in CITIES}
HOLDOUT_START, HOLDOUT_END = date(2026, 10, 1), date(2026, 11, 11)  # fixed in ChatGPT #5
FINAL = {"finalized", "settled"}


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class Report:
    def __init__(self):
        self.failures, self.notes, self.passed = [], [], 0
        self.primary = {}    # strict: every range has an interior quote, 0 < bid <= ask < 1
        self.inclusive = {}  # inclusive candidate: every range has a valid quote, boundaries allowed

    def check(self, ok: bool, what: str):
        if ok:
            self.passed += 1
        else:
            self.failures.append(what)

    def note(self, what: str):
        self.notes.append(what)


def pick_quote(candles: list, cutoff: datetime, lookback_s: int):
    """Latest candle that ENDS at or before the cutoff, within the lookback, with both sides."""
    best = None
    for c in candles:
        end = datetime.fromtimestamp(c["end_period_ts"], tz=timezone.utc)
        if end > cutoff or end < cutoff - timedelta(seconds=lookback_s):
            continue
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        if not bid or not ask:
            continue
        try:
            b, a = Decimal(bid), Decimal(ask)
        except ArithmeticError:
            continue
        if not (b.is_finite() and a.is_finite()):  # NaN or Infinity: unusable, fall back
            continue
        if not (0 <= b <= a <= 1):  # crossed or out-of-range quote: fall back to an earlier candle
            continue
        if best is None or c["end_period_ts"] > best["end_period_ts"]:
            best = c
    return best


def in_range(market: dict, value: Decimal) -> bool:
    kind, lo, hi = market["strike_type"], market.get("floor_strike"), market.get("cap_strike")
    if kind == "less":
        return value < hi
    if kind == "greater":
        return value > lo
    if kind == "between":
        return lo <= value <= hi
    raise ValueError(f"unknown strike_type {kind}")


def check_forecast(r: Report, city, target: date, nbm: dict, p: str):
    """Provenance checks for one city's forecast file, shared by full and staged batches."""
    cut = cutoff_utc(city, target - timedelta(days=1))
    start, _ = climate_day_utc(city, target)
    r.check(nbm.get("city") == city.key and nbm.get("station") == city.station, f"{p} forecast is for another city")
    cycle = _utc(nbm["cycle_utc"])
    r.check(cycle < cut, f"{p} NBM cycle {cycle} not before cutoff")
    r.check(_utc(nbm["cutoff_utc"]) == cut, f"{p} nbm.json cutoff differs")
    r.check(abs(nbm["latitude"] - city.lat) < 1e-3 and abs(nbm["longitude"] - city.lon) < 1e-3,
            f"{p} NBM point is not the station")
    expected = [start + timedelta(hours=i) for i in range(24)]
    got = [_utc(hr["valid_time_utc"]) for hr in nbm["hours"]]
    r.check(got == expected, f"{p} forecast hours do not exactly cover the climate day")
    late = []
    for hr in nbm["hours"]:
        s = hr["source"]
        for when, label in ((s["last_modified_utc"], "grib"), (s["index"]["last_modified_utc"], "idx")):
            if _utc(when) > cut:
                late.append(f"{label} {s['s3_key']}")
        fh = int((_utc(hr["valid_time_utc"]) - cycle).total_seconds() // 3600)
        r.check(s["s3_key"] == f"blend.{cycle:%Y%m%d}/{cycle:%H}/core/blend.t{cycle:%H}z.core.f{fh:03d}.co.grib2",
                f"{p} S3 key does not match cycle and forecast hour: {s['s3_key']}")
        r.check(s["range_end_inclusive"] - s["range_start"] + 1 == s["message_bytes"], f"{p} byte range length")
        r.check(-60 < hr["temperature_c"] < 60, f"{p} implausible temperature {hr['temperature_c']}")
    r.check(not late, f"{p} objects published after the cutoff: {late[:3]}")
    r.check(nbm.get("units") == "C", f"{p} units {nbm.get('units')}")


STAGED_RE = "staging/holdout/{date}"

# Exact schema of a forecast file. Anything extra (e.g. an outcome field) is a failure.
FORECAST_KEYS = {"city", "station", "latitude", "longitude", "cycle_utc", "cutoff_utc", "sampling", "units", "hours"}
HOUR_KEYS = {"valid_time_utc", "temperature_c", "source"}
SOURCE_KEYS = {"s3_key", "etag", "last_modified_utc", "s3_object_bytes", "range_start", "range_end_inclusive",
               "message_bytes", "message_sha256", "index"}
INDEX_KEYS = {"s3_key", "etag", "last_modified_utc", "bytes", "sha256"}
STAGED_MANIFEST_KEYS = {"schema_version", "target_date", "nbm_bucket", "files", "missing"}


def forecast_schema_problems(nbm) -> list[str]:
    if not isinstance(nbm, dict):
        return ["not a JSON object"]
    out = []
    if set(nbm) != FORECAST_KEYS:
        out.append(f"top-level keys {sorted(set(nbm) ^ FORECAST_KEYS)} differ from the forecast schema")
    hours = nbm.get("hours")
    if not isinstance(hours, list):
        return out + ["hours is not a list"]
    for i, h in enumerate(hours):
        if not isinstance(h, dict) or set(h) != HOUR_KEYS:
            out.append(f"hour {i} keys differ from the schema")
            continue
        src = h["source"]
        if not isinstance(src, dict) or set(src) != SOURCE_KEYS:
            out.append(f"hour {i} source keys differ from the schema")
        elif not isinstance(src["index"], dict) or set(src["index"]) != INDEX_KEYS:
            out.append(f"hour {i} index keys differ from the schema")
    return out


def load_json_object(r: Report, path: Path, what: str) -> dict | None:
    """Load an object or record a batch failure."""
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        r.check(False, f"{what}: file missing")
        return None
    except ValueError as exc:
        r.check(False, f"{what}: malformed JSON ({exc})")
        return None
    except OSError as exc:
        r.check(False, f"{what}: could not read file ({exc})")
        return None
    if not isinstance(data, dict):
        r.check(False, f"{what}: not a JSON object")
        return None
    return data


def check_staged(data_root: Path, batch_rel: str) -> Report:
    """A forecast-only holdout batch staged before registration: forecasts, nothing else."""
    r = Report()
    batch = data_root / batch_rel
    manifest = load_json_object(r, batch / "forecast-manifest.json", "forecast-manifest.json")
    if manifest is None:
        return r
    r.check(set(manifest) <= STAGED_MANIFEST_KEYS, f"forecast-manifest has unexpected keys "
            f"{sorted(set(manifest) - STAGED_MANIFEST_KEYS)}")
    try:
        target = date.fromisoformat(manifest["target_date"])
    except (KeyError, TypeError, ValueError):
        r.check(False, "forecast-manifest target_date missing or invalid")
        return r
    r.check(batch_rel.rstrip("/") == STAGED_RE.format(date=target.isoformat()), "staged folder does not match its date")
    r.check(HOLDOUT_START <= target <= HOLDOUT_END, f"staged date {target} is outside the holdout")
    if manifest.get("nbm_bucket") != "noaa-nbm-grib2-pds":
        r.check(False, "forecast-manifest must name the NOAA NBM archive")
    listed = manifest.get("files")
    reasons = manifest.get("missing", {})
    if not isinstance(listed, dict) or not isinstance(reasons, dict):
        r.check(False, "forecast-manifest files and missing must be JSON objects")
        return r

    allowed = {f"{k}/nbm.json" for k in BY_KEY}
    r.check(set(listed) <= allowed, f"staged batch holds more than forecasts: {sorted(set(listed) - allowed)}")
    for rel in sorted(set(listed) & allowed):
        meta = listed[rel]
        if (not isinstance(meta, dict) or set(meta) != {"bytes", "sha256"}
                or type(meta["bytes"]) is not int or not isinstance(meta["sha256"], str)):
            r.check(False, f"{rel}: invalid manifest file entry")
            continue
        try:
            data = (batch / rel).read_bytes()
        except FileNotFoundError:
            r.check(False, f"{rel}: listed in the manifest but missing")
            continue
        except OSError as exc:
            r.check(False, f"{rel}: could not read file ({exc})")
            continue
        r.check(len(data) == meta["bytes"] and hashlib.sha256(data).hexdigest() == meta["sha256"],
                f"{rel}: bytes or sha256 differ from manifest")
    on_disk = {q.relative_to(batch).as_posix() for q in batch.rglob("*") if q.is_file()} - {"forecast-manifest.json"}
    r.check(on_disk == set(listed), f"files on disk differ from manifest: {sorted(on_disk ^ set(listed))}")
    for rel in sorted(allowed - set(listed)):
        reason = reasons.get(rel.split("/")[0])
        r.check(isinstance(reason, str) and bool(reason.strip()), f"{rel} missing without a reason")
    for key, city in BY_KEY.items():
        if f"{key}/nbm.json" not in listed:
            continue
        nbm = load_json_object(r, batch / key / "nbm.json", f"{key}/nbm.json")
        if nbm is None:
            continue
        problems = forecast_schema_problems(nbm)
        r.check(not problems, f"{key}: not forecast-only: {problems[:3]}")
        if problems:
            continue
        try:
            check_forecast(r, city, target, nbm, f"{key}:")
        except (KeyError, TypeError, ValueError) as exc:
            r.check(False, f"{key}: malformed forecast ({type(exc).__name__}: {exc})")
    return r


def check_batch(data_root: Path, batch_rel: str) -> Report:
    r = Report()
    batch = data_root / batch_rel
    manifest = json.loads((batch / "manifest.json").read_bytes())

    # 1. Files match the manifest, and nothing unlisted sits in the batch.
    listed = manifest.get("files", {})
    for rel, meta in listed.items():
        data = (batch / rel).read_bytes()
        r.check(len(data) == meta["bytes"] and hashlib.sha256(data).hexdigest() == meta["sha256"],
                f"{rel}: bytes or sha256 differ from manifest")
    on_disk = {p.relative_to(batch).as_posix() for p in batch.rglob("*") if p.is_file()} - {"manifest.json"}
    r.check(on_disk == set(listed), f"files on disk differ from manifest: {sorted(on_disk ^ set(listed))}")

    # 2. Partition matches the fixed split.
    target = date.fromisoformat(manifest["target_date"])
    in_holdout = HOLDOUT_START <= target <= HOLDOUT_END
    r.check(manifest["partition"] == ("holdout" if in_holdout else "development"),
            f"partition {manifest['partition']!r} wrong for {target}")
    r.check(batch_rel.rstrip("/").endswith(f"{manifest['partition']}/{target.isoformat()}"),
            "batch folder does not match partition and target date")
    h = manifest.get("holdout", {})
    r.check((h.get("start_target_date"), h.get("end_target_date_inclusive")) == ("2026-10-01", "2026-11-11"),
            "manifest holdout bounds differ from the registered split")

    # Every registered city needs an entry, so a dropped city can't shrink the denominator.
    # A city whose inputs were unavailable stays in, as {"status": "missing", "reason": "..."}.
    r.check(set(manifest["cities"]) == set(BY_KEY),
            f"city entries {sorted(manifest['cities'])} != registered {sorted(BY_KEY)}")
    for key, info in manifest["cities"].items():
        city = BY_KEY.get(key)
        r.check(city is not None, f"{key}: unknown city")
        if city is None:
            continue
        p = f"{key}:"
        if info.get("status") == "missing":
            r.check(isinstance(info.get("reason"), str) and info["reason"].strip() != "",
                    f"{p} missing-city record without a reason")
            r.check(not any(rel.startswith(f"{key}/") for rel in listed), f"{p} marked missing but has files")
            r.note(f"{p} missing ({info.get('reason')}); counts against coverage")
            continue
        # 3. Cutoff and climate day, recomputed with the recorder's own schedule code.
        cut = cutoff_utc(city, target - timedelta(days=1))
        start, end = climate_day_utc(city, target)
        r.check(info["event_ticker"] == event_ticker(city, target), f"{p} event ticker {info['event_ticker']}")
        r.check(_utc(info["cutoff_utc"]) == cut, f"{p} cutoff {info['cutoff_utc']} != {cut.isoformat()}")
        r.check(_utc(info["climate_day_start_utc"]) == start and _utc(info["climate_day_end_exclusive_utc"]) == end,
                f"{p} climate day window differs from local standard time")

        # 4. Payout identity from the raw markets response.
        markets = [m for m in json.loads((batch / key / "markets.json").read_bytes())["markets"]]
        r.check(all(m["event_ticker"] == info["event_ticker"] for m in markets), f"{p} foreign market in markets.json")
        r.check(len(markets) == info["market_count"], f"{p} market count")
        r.check(all(m.get("market_type") == "binary" and m.get("status") in FINAL for m in markets),
                f"{p} a market is not a final binary")
        yes = [m["ticker"] for m in markets if m.get("result") == "yes"]
        r.check(len(yes) == 1 and yes[0] == info["yes_winner_ticker"], f"{p} Yes winners {yes}")
        r.check(all(m.get("result") in ("yes", "no") for m in markets), f"{p} result outside yes/no")
        # The payout itself, not just the label: a Yes pays $1 per contract, a No pays $0.
        for m in markets:
            paid = m.get("settlement_value_dollars")
            want = {"yes": Decimal(1), "no": Decimal(0)}.get(m.get("result"))
            try:
                ok = paid is not None and want is not None and Decimal(paid) == want
            except ArithmeticError:
                ok = False
            r.check(ok, f"{p} {m['ticker']}: settlement_value_dollars {paid!r} does not match result {m.get('result')!r}")
        values = {m.get("expiration_value") for m in markets}
        r.check(len(values) == 1, f"{p} markets disagree on expiration_value {values}")
        if len(values) == 1 and None not in values and "" not in values:
            v = Decimal(values.pop())
            covering = [m["ticker"] for m in markets if in_range(m, v)]
            r.check(covering == yes, f"{p} expiration value {v} falls in {covering}, but Yes is {yes}")
        sources = {"Weather Company" if "The Weather Company" in m["rules_primary"] else
                   "NWS" if "National Weather Service" in m["rules_primary"] else "other" for m in markets}
        r.check(len(sources) == 1 and "other" not in sources, f"{p} settlement source unclear {sources}")
        r.check(all(f"(CLI{city.cli_id})" in m["rules_primary"] for m in markets),
                f"{p} rules do not name station CLI{city.cli_id}")

        # 5. Quote selection, recomputed: candle END at or before the cutoff.
        raw = {x["market_ticker"]: x["candlesticks"]
               for x in json.loads((batch / key / "candles.json").read_bytes())["markets"]}
        r.check(set(raw) == {m["ticker"] for m in markets}, f"{p} candles cover a different market set")
        r.check(set(info["quotes"]) == {m["ticker"] for m in markets}, f"{p} quotes cover a different market set")
        for t, q in info["quotes"].items():
            mine = pick_quote(raw.get(t, []), cut, info["quote_lookback_seconds"])
            if mine is None:
                r.check(q is None, f"{p} {t}: manifest has a quote but none qualifies")
                continue
            r.check(q is not None and q["candle_end_unix"] == mine["end_period_ts"],
                    f"{p} {t}: chosen candle {q and q['candle_end_unix']} != {mine['end_period_ts']}")
            if q:
                r.check(q["candle_end_unix"] <= int(cut.timestamp()), f"{p} {t}: candle ends after cutoff")
                r.check(q["age_seconds"] == int(cut.timestamp()) - q["candle_end_unix"], f"{p} {t}: age")
                r.check((q["yes_bid_close_dollars"], q["yes_ask_close_dollars"]) ==
                        (mine["yes_bid"]["close_dollars"], mine["yes_ask"]["close_dollars"]), f"{p} {t}: bid/ask")
        mids = [(Decimal(q["yes_bid_close_dollars"]) + Decimal(q["yes_ask_close_dollars"])) / 2
                for q in info["quotes"].values() if q]
        if len(mids) == len(markets):
            r.note(f"{p} midpoints sum to {sum(mids)}; oldest quote {max(q['age_seconds'] for q in info['quotes'].values())} s")
        # Scoring convention (ChatGPT #8): primary Brier needs 0 < bid <= ask < 1 on EVERY range.
        # A boundary quote is kept for audit, never replaced by an older interior one, and makes
        # the city-day incomplete for primary scoring while staying in the coverage denominator.
        boundary = sorted(t for t, q in info["quotes"].items() if q and (
            Decimal(q["yes_bid_close_dollars"]) <= 0 or Decimal(q["yes_ask_close_dollars"]) >= 1))
        missing = sorted(t for t, q in info["quotes"].items() if not q)
        r.primary[key] = not boundary and not missing
        r.inclusive[key] = not missing  # boundary quotes allowed, scored at their raw midpoint
        if boundary:
            r.note(f"{p} boundary quotes ($0 bid or $1 ask), city-day incomplete for primary scoring: {boundary}")

        # 6. Forecast: every object public before the cutoff, hours cover the climate day.
        nbm = json.loads((batch / key / "nbm.json").read_bytes())
        check_forecast(r, city, target, nbm, p)
        # Holdout: the forecast must be byte-identical to the version staged before registration.
        if in_holdout:
            staged = data_root / "staging" / "holdout" / target.isoformat() / "forecast-manifest.json"
            if staged.exists():
                listed_sha = json.loads(staged.read_bytes())["files"].get(f"{key}/nbm.json", {}).get("sha256")
                staged_file = staged.parent / key / "nbm.json"
                staged_sha = hashlib.sha256(staged_file.read_bytes()).hexdigest() if staged_file.exists() else None
                final_sha = hashlib.sha256((batch / key / "nbm.json").read_bytes()).hexdigest()
                # The staged file must match its own manifest, and the final file must match those bytes.
                r.check(staged_sha is not None and staged_sha == listed_sha,
                        f"{p} staged forecast file does not match the staged manifest")
                r.check(staged_sha is not None and final_sha == staged_sha,
                        f"{p} holdout forecast differs from the staged forecast")
            else:
                r.check(False, f"{p} no staged forecast for this holdout date")
        hourly_max_f = max(hr["temperature_c"] for hr in nbm["hours"]) * 9 / 5 + 32
        # Never note forecast-vs-outcome on holdout dates: that would leak model performance
        # to whoever builds the model before the holdout is scored.
        if len(values) <= 1 and not in_holdout:
            r.note(f"{p} NBM hourly max {hourly_max_f:.1f}F vs settled {markets[0].get('expiration_value')}F")
    return r


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    staged = sys.argv[2].rstrip("/").startswith("staging/")
    rep = (check_staged if staged else check_batch)(Path(sys.argv[1]), sys.argv[2])
    print(json.dumps({"batch": sys.argv[2], "passed_checks": rep.passed, "failures": rep.failures,
                      "primary_complete": rep.primary, "inclusive_complete": rep.inclusive,
                      "notes": rep.notes}, indent=1))
    return 0 if not rep.failures else 1


if __name__ == "__main__":
    sys.exit(main())
