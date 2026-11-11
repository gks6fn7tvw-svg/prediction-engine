"""Independent network re-check of historical batches. Runs as its own Render job.

Each run looks for batches under history/ in the data repository that have no
verification report yet (keyed by the hash of the batch manifest, so a revised
batch is checked again). For each one it:

  1. runs the offline checks in verify.check_history on a fresh download;
  2. re-fetches the Kalshi markets and candles from the URLs the collector saved,
     and compares payout fields and every candle ending at or before the cutoff;
  3. for a sample of forecast hours per city, re-downloads the exact GRIB byte
     range and index from NOAA's archive, checks their SHA-256, size, ETag and
     LastModified against S3's own listing, and checks the index entry really is
     2 m temperature at that byte range;
  4. checks that no LATER forecast cycle was completely published before the
     cutoff (the collector must use the latest complete cycle);
  5. if pygrib is installed, decodes the sampled messages with it (a different
     decoder from the collector's GDAL) and compares the station's temperature.

It writes one JSON report per batch under verification/ and never touches
history/, runs/ or state/. Holdout reports never compare forecasts with outcomes.
It reads only public data and places no orders.
"""
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from recorder.net import RealClock, iso, urllib_transport
from recorder.store import GitHubSink, LocalSink
from verify.check_history import BY_KEY, HOLDOUT_END, HOLDOUT_START, check_batch, check_staged

S3 = "https://{bucket}.s3.amazonaws.com"
BATCH_RE = re.compile(r"^history/(development|holdout)/(\d{4}-\d{2}-\d{2})/manifest\.json$")
STAGED_RE = re.compile(r"^staging/holdout/(\d{4}-\d{2}-\d{2})/forecast-manifest\.json$")
SAMPLE_HOURS = int(os.environ.get("VERIFY_SAMPLE_HOURS", "3"))
MAX_BATCHES = int(os.environ.get("VERIFY_MAX_BATCHES", "10"))
USER_AGENT = "prediction-engine-verifier (public-data research check)"
TEMP_TOLERANCE_C = 0.05


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class TransientError(Exception):
    """A retryable problem (network failure, timeout, 429/5xx, moved branch): try again next run."""


class SourceError(Exception):
    """A non-retryable answer from a source (for example HTTP 404): a confirmed result."""


def retryable(status) -> bool:
    """Rate limiting and every server-side (5xx) error is worth another try next run."""
    return status == 429 or (isinstance(status, int) and 500 <= status <= 599)


# Minimum seconds between requests to a host. Kalshi rate-limits (HTTP 429), and Render's
# outbound addresses are shared, so pace politely rather than burst.
MIN_INTERVAL = {"external-api.kalshi.com": float(os.environ.get("VERIFY_KALSHI_INTERVAL", "1.0"))}
BACKOFF = (5, 15, 45)  # seconds before retries 2, 3 and 4, unless Retry-After says otherwise
MAX_RETRY_AFTER = 120


def retry_after_seconds(headers, now: datetime) -> float | None:
    """Seconds to wait per a Retry-After header (delta-seconds or HTTP-date), or None if absent."""
    for k, v in (headers or {}).items():
        if k.lower() != "retry-after":
            continue
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            pass
        try:
            when = parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - now).total_seconds())
    return None


class Net:
    """GET with pacing and retries through an injectable transport (tests replay saved responses)."""

    def __init__(self, transport, clock):
        self.transport, self.clock = transport, clock
        self.last_request = {}  # host -> monotonic time of the previous request

    def _pace(self, url):
        host = url.split("/")[2] if "://" in url else ""
        gap = MIN_INTERVAL.get(host, 0.0)
        if gap and host in self.last_request:
            wait = self.last_request[host] + gap - self.clock.monotonic()
            if wait > 0:
                self.clock.sleep(wait)
        self.last_request[host] = self.clock.monotonic()

    def get(self, url, headers=None, tries=4, ok=(200,)):
        last = None
        for attempt in range(1, tries + 1):
            self._pace(url)
            wait = None
            try:
                status, hdrs, body = self.transport(
                    "GET", url, {"User-Agent": USER_AGENT, **(headers or {})}, None, 60)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if status in ok:
                    return status, hdrs, body
                last = f"HTTP {status}"
                if not retryable(status):
                    raise SourceError(f"GET {url} failed: {last}")
                wait = retry_after_seconds(hdrs, self.clock.now())
                if wait is not None and wait > MAX_RETRY_AFTER:
                    # Asked to wait longer than this run should: leave it pending for a later run.
                    raise TransientError(f"GET {url}: {last}, Retry-After {wait:.0f}s exceeds the "
                                         f"{MAX_RETRY_AFTER}s per-run budget")
            if attempt < tries:
                self.clock.sleep(wait if wait is not None else BACKOFF[min(attempt, len(BACKOFF)) - 1])
        raise TransientError(f"GET {url} failed after {tries} tries: {last}")


class S3Listing:
    """ListObjectsV2 for one prefix, cached per run."""

    def __init__(self, net, bucket):
        self.net, self.bucket, self.cache = net, bucket, {}

    def objects(self, prefix: str) -> dict:
        if prefix not in self.cache:
            out, token = {}, None
            while True:
                url = f"{S3.format(bucket=self.bucket)}/?list-type=2&prefix={quote(prefix)}"
                if token:
                    url += f"&continuation-token={quote(token)}"
                _, _, body = self.net.get(url)
                root = ET.fromstring(body)
                ns = {"s": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
                find = (lambda e, t: e.find(f"s:{t}", ns)) if ns else (lambda e, t: e.find(t))
                for c in (root.findall("s:Contents", ns) if ns else root.findall("Contents")):
                    out[find(c, "Key").text] = {
                        "last_modified": _utc(find(c, "LastModified").text),
                        "etag": find(c, "ETag").text,
                        "size": int(find(c, "Size").text),
                    }
                trunc = find(root, "IsTruncated")
                if trunc is not None and trunc.text == "true":
                    token = find(root, "NextContinuationToken").text
                else:
                    break
            self.cache[prefix] = out
        return self.cache[prefix]


def nearest_value(values, lats, lons, lat, lon):
    """Value at the grid cell nearest (lat, lon), by great-circle distance. numpy arrays."""
    import numpy as np
    lat1, lon1 = np.radians(lat), np.radians(lon)
    la, lo = np.radians(lats), np.radians(((lons + 180) % 360) - 180)
    d = np.sin((la - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(la) * np.sin((lo - lon1) / 2) ** 2
    i = np.unravel_index(np.argmin(d), d.shape)
    return float(values[i])


def decode_station_c(message: bytes, lat: float, lon: float):
    """Temperature (C) at the station from one GRIB2 message, using pygrib. None if unavailable."""
    try:
        import pygrib  # noqa: F401  (optional dependency, installed only on the Render job)
    except ImportError:
        return None
    import pygrib
    grb = pygrib.fromstring(message)
    lats, lons = grb.latlons()
    kelvin = nearest_value(grb.values, lats, lons, lat, lon)
    return kelvin - 273.15


def idx_entry(idx_text: str, start: int):
    """The .idx line whose byte offset is `start`, and the next line's offset (or None)."""
    rows = []
    for line in idx_text.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[1].isdigit():
            rows.append((int(parts[1]), line))
    for i, (offset, line) in enumerate(rows):
        if offset == start:
            return line, (rows[i + 1][0] if i + 1 < len(rows) else None)
    return None, None


class Verifier:
    def __init__(self, sink, net, clock, rng_seed=None):
        self.sink, self.net, self.clock = sink, net, clock
        self.rng_seed = rng_seed

    def pending(self):
        """Batches without a canonical report, all read at ONE pinned data-repo commit."""
        ref = self.sink.head()
        paths = self.sink.list_files("history/", ref=ref) + self.sink.list_files("staging/", ref=ref)
        done = set(self.sink.list_files("verification/", ref=ref))
        todo = []
        for path in paths:
            m, st = BATCH_RE.match(path), STAGED_RE.match(path)
            if m:
                prefix = f"verification/{m.group(1)}/{m.group(2)}"
            elif st:  # forecast-only holdout batch staged before registration
                prefix = f"verification/staging/holdout/{st.group(1)}"
            else:
                continue
            raw = self.sink.read(path, ref=ref)
            digest = hashlib.sha256(raw).hexdigest()
            report = f"{prefix}/{digest}.json"
            if report not in done:
                todo.append((path.rsplit("/", 1)[0], digest, report, ref))
        return todo[:MAX_BATCHES]

    def download(self, batch: str, dest: Path, ref: str):
        paths = self.sink.list_files(batch + "/", ref=ref)
        m = BATCH_RE.match(batch + "/manifest.json")
        if m and m.group(1) == "holdout":  # the final holdout batch is checked against its staged forecasts
            paths += self.sink.list_files(f"staging/holdout/{m.group(2)}/", ref=ref)
        for path in paths:
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.sink.read(path, ref=ref))

    def verify(self, batch: str, digest: str, ref: str) -> dict:
        report = {"batch": batch, "manifest_sha256": digest, "data_commit": ref,
                  "started_at": iso(self.clock.now()), "failures": [], "notes": [], "cities": {},
                  "transient_errors": []}
        fail = report["failures"].append
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                self.download(batch, root, ref)
            except Exception as exc:
                report["transient_errors"].append(f"download: {type(exc).__name__}: {exc}")
                return self._finish(report)
            staged = batch.startswith("staging/")
            manifest_name = "forecast-manifest.json" if staged else "manifest.json"
            got = hashlib.sha256((root / batch / manifest_name).read_bytes()).hexdigest()
            if got != digest:  # never certify a manifest other than the one discovered
                report["transient_errors"].append(f"manifest at {ref} hashes to {got}, expected {digest}")
                return self._finish(report)
            if staged:
                return self._verify_staged(root, batch, digest, report)
            offline = check_batch(root, batch)
            report["offline"] = {"passed_checks": offline.passed, "failures": offline.failures,
                                 "primary_complete": offline.primary,
                                 "inclusive_complete": offline.inclusive, "notes": offline.notes}
            for f in offline.failures:
                fail(f"offline: {f}")
            manifest = json.loads((root / batch / "manifest.json").read_bytes())
            target = datetime.fromisoformat(manifest["target_date"]).date()
            holdout = HOLDOUT_START <= target <= HOLDOUT_END
            listing = S3Listing(self.net, manifest["nbm_bucket"])
            rng = random.Random(self.rng_seed if self.rng_seed is not None else digest)
            for key, info in manifest["cities"].items():
                if info.get("status") == "missing":
                    report["cities"][key] = {"status": "missing"}
                    continue
                city = report["cities"][key] = {}
                for name, step in (("Kalshi", lambda: self.check_kalshi(root / batch, key, info, city, fail)),
                                   ("NOAA", lambda: self.check_nbm(root / batch, key, info, city, fail,
                                                                   listing, rng, holdout))):
                    try:
                        step()
                    except TransientError as exc:
                        report["transient_errors"].append(f"{key}: {name}: {exc}")
                    except Exception as exc:
                        fail(f"{key}: {name} re-fetch error {type(exc).__name__}: {exc}")
        return self._finish(report)

    def _verify_staged(self, root: Path, batch: str, digest: str, report: dict) -> dict:
        """Forecast-only holdout batch: offline checks plus NOAA re-fetch. No Kalshi, no outcomes."""
        fail = report["failures"].append
        offline = check_staged(root, batch)
        report["offline"] = {"passed_checks": offline.passed, "failures": offline.failures}
        for f in offline.failures:
            fail(f"offline: {f}")
        manifest = json.loads((root / batch / "forecast-manifest.json").read_bytes())
        listing = S3Listing(self.net, manifest["nbm_bucket"])
        rng = random.Random(self.rng_seed if self.rng_seed is not None else digest)
        for key in BY_KEY:
            rel = f"{key}/nbm.json"
            if rel not in manifest.get("files", {}):
                report["cities"][key] = {"status": "missing"}
                continue
            nbm = json.loads((root / batch / rel).read_bytes())
            info = {"forecast_path": rel, "cutoff_utc": nbm["cutoff_utc"]}
            city = report["cities"][key] = {}
            try:
                self.check_nbm(root / batch, key, info, city, fail, listing, rng, True)
            except TransientError as exc:
                report["transient_errors"].append(f"{key}: NOAA: {exc}")
            except Exception as exc:
                fail(f"{key}: NOAA re-fetch error {type(exc).__name__}: {exc}")
        return self._finish(report)

    def _finish(self, report: dict) -> dict:
        report["finished_at"] = iso(self.clock.now())
        # "retry": something could not be checked for a retryable reason. No canonical report
        # is written, so the batch stays pending; the attempt is logged instead.
        report["status"] = ("retry" if report["transient_errors"]
                            else "ok" if not report["failures"] else "failed")
        report["ok"] = report["status"] == "ok"
        return report

    # ---- Kalshi ----

    def check_kalshi(self, bdir, key, info, city, fail):
        src = info["kalshi_sources"]
        _, _, body = self.net.get(src["markets"]["url"])
        live = {m["ticker"]: m for m in json.loads(body)["markets"]}
        saved = {m["ticker"]: m for m in json.loads((bdir / src["markets"]["path"]).read_bytes())["markets"]}
        fields = ("event_ticker", "status", "result", "settlement_value_dollars", "expiration_value",
                  "rules_primary", "strike_type", "floor_strike", "cap_strike")
        if set(live) != set(saved):
            fail(f"{key}: re-fetched market set differs {sorted(set(live) ^ set(saved))}")
        diffs = [(t, f) for t in saved if t in live for f in fields if live[t].get(f) != saved[t].get(f)]
        city["markets_match"] = not diffs
        if diffs:
            fail(f"{key}: re-fetched markets differ on {diffs[:5]}")

        cutoff = int(_utc(info["cutoff_utc"]).timestamp())
        _, _, body = self.net.get(src["candles"]["url"])

        def upto_cutoff(markets):
            return {x["market_ticker"]: [(c["end_period_ts"], (c.get("yes_bid") or {}).get("close_dollars"),
                                          (c.get("yes_ask") or {}).get("close_dollars"))
                                         for c in x["candlesticks"] if c["end_period_ts"] <= cutoff]
                    for x in markets}
        live_c = upto_cutoff(json.loads(body)["markets"])
        saved_c = upto_cutoff(json.loads((bdir / src["candles"]["path"]).read_bytes())["markets"])
        bad = sorted(t for t in set(live_c) | set(saved_c) if live_c.get(t) != saved_c.get(t))
        city["candles_match"] = not bad
        if bad:
            fail(f"{key}: re-fetched candles up to the cutoff differ for {bad}")

    # ---- NOAA NBM ----

    def check_nbm(self, bdir, key, info, city, fail, listing, rng, holdout):
        nbm = json.loads((bdir / info["forecast_path"]).read_bytes())
        cutoff = _utc(info["cutoff_utc"])
        cycle = _utc(nbm["cycle_utc"])
        bucket = listing.bucket
        hours = nbm["hours"]
        if not hours:
            fail(f"{key}: forecast has no hours")
            return

        # Latest complete cycle: no later cycle may have had every needed file public by the cutoff.
        needed_fh = [int((_utc(h["valid_time_utc"]) - cycle).total_seconds() // 3600) for h in hours]
        later = []
        c = cycle + timedelta(hours=1)
        while c < cutoff:
            prefix = f"blend.{c:%Y%m%d}/{c:%H}/core/"
            objs = listing.objects(prefix)
            keys = []
            for h in hours:
                fh = int((_utc(h["valid_time_utc"]) - c).total_seconds() // 3600)
                base = f"{prefix}blend.t{c:%H}z.core.f{fh:03d}.co.grib2"
                keys += [base, base + ".idx"]
            if all(k in objs and objs[k]["last_modified"] <= cutoff for k in keys):
                later.append(f"{c:%Y-%m-%dT%HZ}")
            c += timedelta(hours=1)
        city["later_complete_cycles"] = later
        if later:
            fail(f"{key}: a later cycle was complete before the cutoff: {later}")

        # Sampled hours: always the warmest forecast hour, plus random others.
        warmest = max(range(len(hours)), key=lambda i: hours[i]["temperature_c"])
        others = [i for i in range(len(hours)) if i != warmest]
        picks = [warmest] + rng.sample(others, min(max(SAMPLE_HOURS - 1, 0), len(others)))
        city["sampled_hours"] = []
        for i in sorted(picks):
            h, s = hours[i], hours[i]["source"]
            row = {"valid_time_utc": h["valid_time_utc"], "s3_key": s["s3_key"]}
            city["sampled_hours"].append(row)
            prefix = s["s3_key"].rsplit("/", 1)[0] + "/"
            objs = listing.objects(prefix)
            obj, idx = objs.get(s["s3_key"]), objs.get(s["index"]["s3_key"])
            if obj is None or idx is None:
                fail(f"{key}: {s['s3_key']} or its index is not in NOAA's listing")
                continue
            for what, meta, saved_lm, saved_etag, size in (
                    ("grib", obj, s["last_modified_utc"], s["etag"], s["s3_object_bytes"]),
                    ("idx", idx, s["index"]["last_modified_utc"], s["index"]["etag"], s["index"]["bytes"])):
                if meta["last_modified"] != _utc(saved_lm) or meta["etag"] != saved_etag or meta["size"] != size:
                    fail(f"{key}: {what} {s['s3_key']} listing differs (LastModified/ETag/size)")
                if meta["last_modified"] > cutoff:
                    fail(f"{key}: {what} {s['s3_key']} was published after the cutoff")
            base = S3.format(bucket=bucket)
            _, _, idx_bytes = self.net.get(f"{base}/{s['index']['s3_key']}")
            if hashlib.sha256(idx_bytes).hexdigest() != s["index"]["sha256"]:
                fail(f"{key}: index {s['index']['s3_key']} hash differs")
            line, nxt = idx_entry(idx_bytes.decode("utf-8", "replace"), s["range_start"])
            row["idx_line"] = line
            if line is None or ":TMP:2 m above ground:" not in line:
                fail(f"{key}: index entry at {s['range_start']} is not 2 m temperature: {line!r}")
            elif nxt is not None and nxt - 1 != s["range_end_inclusive"]:
                fail(f"{key}: byte range end {s['range_end_inclusive']} != next index offset - 1 ({nxt - 1})")
            _, _, msg = self.net.get(f"{base}/{s['s3_key']}",
                                     headers={"Range": f"bytes={s['range_start']}-{s['range_end_inclusive']}"},
                                     ok=(206, 200))
            row["sha256_match"] = hashlib.sha256(msg).hexdigest() == s["message_sha256"]
            if not row["sha256_match"]:
                fail(f"{key}: GRIB message for {h['valid_time_utc']} hash differs")
                continue
            decoded = decode_station_c(msg, nbm["latitude"], nbm["longitude"])
            if decoded is None:
                row["decode"] = "skipped (pygrib not installed)"
            else:
                row["decoded_c"] = round(decoded, 4)
                if abs(decoded - h["temperature_c"]) > TEMP_TOLERANCE_C:
                    fail(f"{key}: pygrib {decoded:.3f}C != collector {h['temperature_c']:.3f}C at {h['valid_time_utc']}")
        if holdout:
            city["note"] = "holdout: no forecast-vs-outcome comparison"


def run_once(v: Verifier, sink, run_id: str, code_version=None) -> list[dict]:
    """Verify pending batches. Canonical reports only for completed checks; retries are logged."""
    results = []
    for batch, digest, report_path, ref in v.pending():
        report = v.verify(batch, digest, ref)
        report["code_version"] = code_version
        if report["status"] == "retry":
            path = report_path.replace("verification/", "verification/attempts/", 1)
            path = path[: -len(".json")] + f"/{run_id}.json"
            message = f"verify {batch}: retry later ({len(report['transient_errors'])} transient errors)"
        else:
            path = report_path
            message = f"verify {batch} ({'ok' if report['ok'] else 'FAILED'})"
        sink.commit({path: json.dumps(report, indent=1, default=str).encode()}, message)
        results.append({"batch": batch, "status": report["status"], "failures": len(report["failures"]),
                        "transient_errors": len(report["transient_errors"]), "report": path})
    return results


def main() -> int:
    local = os.environ.get("VERIFY_LOCAL_DIR")
    repo, token = os.environ.get("DATA_REPO"), os.environ.get("GITHUB_TOKEN")
    if local:
        sink = LocalSink(local)
    elif repo and token:
        sink = GitHubSink(repo, token, os.environ.get("DATA_BRANCH", "main"))
    else:
        print("Set DATA_REPO and GITHUB_TOKEN (or VERIFY_LOCAL_DIR).", file=sys.stderr)
        return 2
    clock = RealClock()
    v = Verifier(sink, Net(urllib_transport, clock), clock)
    run_id = f"{clock.now():%Y%m%dT%H%M%SZ}"
    try:
        results = run_once(v, sink, run_id, os.environ.get("RENDER_GIT_COMMIT"))
    except Exception as exc:  # e.g. GitHub unreachable during discovery: nothing written, retry next hour
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"checked": results, "code_version": os.environ.get("RENDER_GIT_COMMIT")}))
    # Confirmed failures and retries both exit nonzero so they show in Render's history.
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
