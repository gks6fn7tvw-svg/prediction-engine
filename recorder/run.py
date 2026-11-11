"""One recorder run. Scheduled hourly at minute 52 (UTC).

Each run:
  1. For every city whose 6 p.m. local cutoff is a few minutes away, fetch the
     market rules and weather forecasts BEFORE the cutoff, wait for the cutoff,
     then take two order-book snapshots at least one second apart.
  2. Poll events captured earlier until Kalshi finalizes them.
  3. Save the NWS climate report for those cities whenever it changes.
  4. Commit everything to the data repository in a single commit.

Raw response bytes are saved exactly as received (the large gridpoint file is
gzipped). Values derived here and written to run.json are conveniences for a
quick look. They are never the evidence; the raw files are.

This recorder only reads public data. It places no orders and uses no Kalshi
account.
"""
import gzip
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import CITIES, KALSHI_BASE, NWS_BASE
from .net import RealClock, fetch, iso, urllib_transport
from .schedule import climate_day_utc, cutoffs_between, due_cutoffs, event_ticker
from .store import GitHubSink, LocalSink

LEAD = timedelta(minutes=10)            # start preparing this long before a cutoff
LATENESS_LIMIT = timedelta(minutes=10)  # design v0.3: first book must be RECEIVED by then
MAX_LATE = timedelta(minutes=20)        # still captured (and flagged) up to here; then "missed"
PRE_FETCH_MARGIN = timedelta(seconds=45)  # weather fetches not started by then are deferred
RECHECK_SECONDS = 1.0
FINAL_STATUSES = {"finalized", "settled"}
POST_FINAL_POLL = timedelta(hours=48)   # keep watching for revisions after finalization
GIVE_UP = timedelta(days=14)
MISSED_LOOKBACK = timedelta(days=3)
STATE_PATH = "state/state.json"
USER_AGENT = os.environ.get(
    "RECORDER_USER_AGENT", "prediction-engine-recorder (public-data research logger)")


def _json(body: bytes):
    try:
        return json.loads(body)
    except Exception:
        return None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def best_quotes(orderbook_fp: dict) -> dict:
    """Best Yes bid and ask from a Kalshi book. The Yes ask is 1 minus the best No bid."""
    def top(levels):
        rows = [(Decimal(p), Decimal(q)) for p, q in (levels or [])]
        return max(rows) if rows else None
    yes, no = top(orderbook_fp.get("yes_dollars")), top(orderbook_fp.get("no_dollars"))
    return {
        "yes_bid": str(yes[0]) if yes else None,
        "yes_bid_size": str(yes[1]) if yes else None,
        "yes_ask": str(Decimal(1) - no[0]) if no else None,
        "yes_ask_size": str(no[1]) if no else None,
    }


def first_book_verdict(cutoff: datetime, ok: bool, received: datetime) -> tuple[bool, str]:
    """Cutoff eligibility (design v0.3), judged on when the first order book ARRIVED.

    Eligible only if it was received from the cutoff through LATENESS_LIMIT after it,
    both ends included. Judging on receipt means a slow response cannot qualify just
    because its request was sent in time. Ineligible captures are still saved; they
    stay in the coverage count and are excluded from confirmatory scoring.
    """
    if not ok:
        return False, "first_book_failed"
    if received < cutoff:
        return False, "early_first_book"
    if received > cutoff + LATENESS_LIMIT:
        return False, "late_first_book"
    return True, "on_time"


def returned_tickers(f) -> set | None:
    """Tickers present in an order-book response, or None if it is unusable."""
    if not f.ok:
        return None
    books = (_json(f.body) or {}).get("orderbooks")
    if not isinstance(books, list):
        return None
    return {b.get("ticker") for b in books if isinstance(b, dict) and b.get("ticker")}


def settlement_signature(markets: list) -> tuple[str, bool]:
    """A fingerprint of the settlement-relevant fields, and whether all markets are final."""
    keep = ("status", "result", "settle", "expiration_value")
    rows = sorted(
        json.dumps({k: v for k, v in m.items() if k == "ticker" or any(s in k for s in keep)},
                   sort_keys=True)
        for m in markets)
    sig = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    final = bool(markets) and all(
        m.get("status") in FINAL_STATUSES and m.get("result") not in (None, "") for m in markets)
    return sig, final


def cli_text(body: bytes) -> bytes:
    """The report text inside the page, so page furniture doesn't count as a change."""
    match = re.search(rb"<pre[^>]*>(.*?)</pre>", body, re.S)
    return match.group(1) if match else body


class Recorder:
    def __init__(self, clock, transport, sink, cities=CITIES, code_version=None,
                 live_check_on_first_run=True):
        self.clock, self.transport, self.sink, self.cities = clock, transport, sink, cities
        self.live_check_on_first_run = live_check_on_first_run
        start = clock.now()
        self.run_id = f"{start:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        self.base = f"runs/{start:%Y/%m/%d}/{self.run_id}"
        self.files: dict[str, bytes] = {}
        self.state = None
        self.state_ok = False
        self.run = {
            "run_id": self.run_id, "started_at": iso(start), "code_version": code_version,
            "cutoffs": [], "missed": [], "settlement": [], "cli": [], "fetches": [], "errors": [],
        }

    # ---------- plumbing ----------

    def get(self, url, kind, path=None, gzip_it=False, **kw):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
        f = fetch(self.clock, self.transport, url, headers=headers, **kw)
        entry = f.meta()
        entry["kind"] = kind
        self.run["fetches"].append(entry)
        if path and f.body:  # keep whatever came back, including error pages
            self.save(entry, path, f.body, gzip_it)
        return f, entry

    def save(self, entry, path, body, gzip_it=False):
        self.files[path] = gzip.compress(body, mtime=0) if gzip_it else body
        entry["path"] = path
        entry["stored_gzip"] = gzip_it

    def guard(self, name, fn):
        try:
            fn()
        except Exception as exc:  # one phase failing must not lose the others
            self.run["errors"].append({"phase": name, "error": f"{type(exc).__name__}: {exc}"})

    def load_state(self):
        now = iso(self.clock.now())
        fresh = {"version": 1, "created_at": now, "cutoffs": {}, "events": {}, "cli": {}}
        try:
            raw = self.sink.read(STATE_PATH)
        except Exception as exc:
            # Carry on capturing, but never overwrite a state file we could not read.
            self.run["errors"].append({"phase": "load_state", "error": f"{type(exc).__name__}: {exc}"})
            self.state, self.state_ok = fresh, False
            return
        self.state_ok = True
        if raw is None:
            self.state = fresh
            return
        parsed = _json(raw)
        if not isinstance(parsed, dict) or "events" not in parsed:
            self.files[f"state/unreadable_{self.run_id}.json"] = raw
            self.run["errors"].append({"phase": "load_state", "error": "state file unreadable; kept a copy"})
            self.state = fresh
            return
        for key, default in fresh.items():
            parsed.setdefault(key, default)
        self.state = parsed

    # ---------- cutoff capture ----------

    def capture_cutoff(self, cutoff: datetime, items):
        cdir = f"{self.base}/cutoff_{cutoff:%Y%m%dT%H%MZ}"
        rec = {"cutoff_utc": iso(cutoff), "started_before_cutoff": self.clock.now() < cutoff,
               "cities": {}}
        self.run["cutoffs"].append(rec)

        # Kalshi discovery first: the tickers are needed for the books.
        for city, target in items:
            start, end = climate_day_utc(city, target)
            info = {
                "series": city.series, "event_ticker": event_ticker(city, target),
                "target_date": target.isoformat(), "station": city.station,
                "cutoff_local_zone": city.tz,
                "climate_day_utc": [iso(start), iso(end)],
                "tickers": [], "problems": [],
            }
            rec["cities"][city.key] = info
            self.get(f"{KALSHI_BASE}/series/{city.series}", "kalshi_series",
                     f"{cdir}/{city.key}/kalshi_series.json", attempts=2)
            m, _ = self.get(
                f"{KALSHI_BASE}/markets?series_ticker={city.series}&status=open&limit=200",
                "kalshi_markets", f"{cdir}/{city.key}/kalshi_markets.json", attempts=2)
            payload = _json(m.body) if m.ok else None
            markets = payload.get("markets") if isinstance(payload, dict) else None
            if markets is None:
                info["problems"].append("markets_unavailable")
                continue
            info["tickers"] = sorted(
                x["ticker"] for x in markets
                if x.get("event_ticker") == info["event_ticker"] and x.get("ticker"))
            if not info["tickers"]:
                info["problems"].append("no_open_markets_for_event")
            if payload.get("cursor"):
                info["problems"].append("markets_response_paginated")

        # Weather before the cutoff where time allows; anything left is fetched after and flagged.
        deferred = []
        for city, target in items:
            if self.clock.now() < cutoff - PRE_FETCH_MARGIN:
                self.capture_weather(cdir, city, target, rec["cities"][city.key], cutoff)
            else:
                deferred.append((city, target))

        tickers = [t for info in rec["cities"].values() for t in info["tickers"]]
        # Never request the book before the cutoff, even if a sleep returns early.
        while (wait := (cutoff - self.clock.now()).total_seconds()) > 0:
            self.clock.sleep(wait)

        books_ok, eligible, reason = False, False, "no_markets"
        got1 = got2 = None
        if tickers:
            url = f"{KALSHI_BASE}/markets/orderbooks?" + "&".join(
                "tickers=" + quote(t, safe="") for t in tickers)
            b1, _ = self.get(url, "kalshi_books_1", f"{cdir}/books_1.json")
            # At least one MEASURED second after the first book arrived.
            while (gap := b1.monotonic_received + RECHECK_SECONDS - self.clock.monotonic()) > 0:
                self.clock.sleep(gap)
            b2, _ = self.get(url, "kalshi_books_2", f"{cdir}/books_2.json")
            books_ok = b1.ok
            eligible, reason = first_book_verdict(cutoff, b1.ok, b1.received)
            got1, got2 = returned_tickers(b1), returned_tickers(b2)
            rec["books"] = {
                "tickers_requested": len(tickers),
                "tickers_returned_first": len(got1) if got1 is not None else None,
                "unexpected_tickers_first": sorted((got1 or set()) - set(tickers)),
                "first_requested_at": iso(b1.requested),
                "first_received_at": iso(b1.received),
                "request_lateness_seconds": round((b1.requested - cutoff).total_seconds(), 6),
                "receipt_lateness_seconds": round((b1.received - cutoff).total_seconds(), 6),
                "lateness_limit_seconds": LATENESS_LIMIT.total_seconds(),
                "timing_eligible": eligible, "timing_reason_code": reason,
                "recheck_gap_seconds": round(b2.monotonic_requested - b1.monotonic_received, 6),
                "first_ok": b1.ok, "recheck_ok": b2.ok,
                "note": "timing is judged on first-book receipt time; each city's eligible and "
                        "reason_code are authoritative, since a batch can omit one city's books; "
                        "one batch request per snapshot is not proof of an atomic snapshot",
            }
            self.guard("quotes", lambda: rec["books"].update(
                quotes_first=self.quotes(b1), quotes_recheck=self.quotes(b2)))

        for city, target in deferred:
            self.capture_weather(cdir, city, target, rec["cities"][city.key], cutoff)

        now = iso(self.clock.now())
        for city, target in items:
            info = rec["cities"][city.key]
            # Judge each city on its OWN books: an on-time 200 response can still omit some.
            wanted = set(info["tickers"])
            info["books_missing_first"] = sorted(wanted - (got1 or set()))
            info["books_missing_recheck"] = sorted(wanted - (got2 or set()))
            if not info["tickers"]:
                info["status"], info["eligible"], info["reason_code"] = "failed", False, "no_markets"
            elif not books_ok:
                info["status"], info["eligible"], info["reason_code"] = "failed", False, reason
            elif info["books_missing_first"]:
                # The raw response is kept as received; this city just has no complete book in it.
                info["status"], info["eligible"], info["reason_code"] = "failed", False, "missing_books"
            else:
                info["status"], info["eligible"], info["reason_code"] = "captured", eligible, reason
            self.state["cutoffs"].setdefault(city.key, {})[target.isoformat()] = {
                "status": info["status"], "eligible": info["eligible"],
                "reason_code": info["reason_code"],
                "run_id": self.run_id, "cutoff_utc": iso(cutoff),
                "receipt_lateness_seconds": rec.get("books", {}).get("receipt_lateness_seconds"),
            }
            if info["tickers"]:
                self.state["events"].setdefault(info["event_ticker"], {
                    "city": city.key, "target_date": target.isoformat(), "first_seen": now,
                    "poll_after": info["climate_day_utc"][1],
                    "last_sig": None, "versions": 0, "final_at": None, "done": False,
                })

    def capture_weather(self, cdir, city, target, info, cutoff):
        d = f"{cdir}/{city.key}"
        p, _ = self.get(city.points_url, "nws_points", f"{d}/nws_points.json", attempts=2)
        props = ((_json(p.body) or {}).get("properties") or {}) if p.ok else {}
        grid_url = props.get("forecastGridData") or ""
        hourly_url = props.get("forecastHourly") or ""
        if not (grid_url.startswith(NWS_BASE + "/") and hourly_url.startswith(NWS_BASE + "/")):
            info["problems"].append("points_lookup_failed_used_configured_grid")
            grid_url, hourly_url = city.grid_url, city.grid_url + "/forecast/hourly"
        elif grid_url != city.grid_url:
            info["problems"].append("grid_differs_from_configured")
        h, _ = self.get(hourly_url, "nws_hourly", f"{d}/nws_hourly.json", attempts=2)
        g, _ = self.get(grid_url, "nws_gridpoint", f"{d}/nws_gridpoint.json.gz", gzip_it=True, attempts=2)
        if not h.ok:
            info["problems"].append("hourly_forecast_unavailable")
        if not g.ok:
            info["problems"].append("gridpoint_unavailable")
        info["forecast"] = {
            "grid_url": grid_url,
            "hourly_requested_at": iso(h.requested),
            "fetched_before_cutoff": h.received <= cutoff and g.received <= cutoff,
        }
        self.guard("forecast_summary", lambda: info["forecast"].update(
            self.forecast_summary(h, g, *climate_day_utc(city, target))))

    @staticmethod
    def forecast_summary(h, g, start, end) -> dict:
        out = {}
        hp = ((_json(h.body) or {}).get("properties") or {}) if h.ok else {}
        if hp:
            temps = [p["temperature"] for p in hp.get("periods", [])
                     if start <= _parse(p["startTime"]).astimezone(timezone.utc) < end]
            out.update(hourly_update_time=hp.get("updateTime"), hourly_generated_at=hp.get("generatedAt"),
                       hourly_points_in_climate_day=len(temps),
                       hourly_max_in_climate_day=max(temps) if temps else None,
                       hourly_unit=(hp.get("periods") or [{}])[0].get("temperatureUnit"))
        gp = ((_json(g.body) or {}).get("properties") or {}) if g.ok else {}
        if gp:
            mx = gp.get("maxTemperature") or {}
            out.update(gridpoint_update_time=gp.get("updateTime"),
                       gridpoint_max_temperature_uom=mx.get("uom"),
                       gridpoint_max_temperature_near_day=[
                           v for v in mx.get("values", [])
                           if start - timedelta(hours=12)
                           <= _parse(v["validTime"].split("/")[0]).astimezone(timezone.utc) < end])
        return out

    @staticmethod
    def quotes(f) -> dict:
        books = (_json(f.body) or {}).get("orderbooks") or [] if f.ok else []
        return {b["ticker"]: best_quotes(b.get("orderbook_fp") or {}) for b in books}

    # ---------- missed cutoffs ----------

    def record_missed(self):
        if not self.state_ok:
            return
        now = self.clock.now()
        since = max(_parse(self.state["created_at"]), now - MISSED_LOOKBACK)
        for city in self.cities:
            for cutoff, target in cutoffs_between(city, since, now - MAX_LATE):
                seen = self.state["cutoffs"].setdefault(city.key, {})
                if target.isoformat() not in seen:
                    entry = {"status": "missed", "run_id": self.run_id, "cutoff_utc": iso(cutoff)}
                    seen[target.isoformat()] = entry
                    self.run["missed"].append({"city": city.key, "target_date": target.isoformat(), **entry})

    # ---------- settlement ----------

    def poll_settlements(self):
        now = self.clock.now()
        for ev, info in sorted(self.state["events"].items()):
            if info.get("done") or now < _parse(info["poll_after"]):
                continue
            f, entry = self.get(f"{KALSHI_BASE}/markets?event_ticker={quote(ev, safe='')}&limit=200",
                                "kalshi_settlement_poll", attempts=2)
            payload = _json(f.body) if f.ok else None
            markets = payload.get("markets") if isinstance(payload, dict) else None
            note = {"event_ticker": ev, "polled_at": iso(f.requested)}
            if markets is None:
                note["outcome"] = "poll_failed"
            elif not markets and info.get("final_at"):
                # Kalshi moves settled markets to its historical endpoints after a cutoff.
                # Already final and saved, so stop here instead of treating it as a reversal.
                info["done"] = True
                note["outcome"] = "no_longer_listed_after_final"
            else:
                sig, final = settlement_signature(markets)
                note.update(markets=len(markets), final=final, changed=sig != info.get("last_sig"))
                if note["changed"]:
                    info["versions"] = info.get("versions", 0) + 1
                    info["last_sig"] = sig
                    self.save(entry, f"settlement/{ev}/{self.run_id}.json", f.body)
                    note["path"] = entry["path"]
                if final and not info.get("final_at"):
                    info["final_at"] = iso(now)
                elif not final and info.get("final_at"):
                    info["final_at"] = None  # it stopped looking final; keep watching
                    note["outcome"] = "no_longer_final"
            if info.get("final_at") and now - _parse(info["final_at"]) > POST_FINAL_POLL:
                info["done"] = True
            elif now - _parse(info["first_seen"]) > GIVE_UP:
                info["done"], info["unresolved"] = True, True
                note["outcome"] = "gave_up_unresolved"
            self.run["settlement"].append(note)

    # ---------- NWS climate reports ----------

    def capture_cli(self):
        watching = {i["city"] for i in self.state["events"].values() if not i.get("done")}
        for city in self.cities:
            if city.key not in watching:
                continue
            f, entry = self.get(city.cli_url, "nws_cli", attempts=2)
            if not f.ok:
                self.run["cli"].append({"city": city.key, "outcome": "fetch_failed"})
                continue
            digest = hashlib.sha256(cli_text(f.body)).hexdigest()
            changed = digest != self.state["cli"].get(city.key, {}).get("last_sha256")
            if changed:
                self.save(entry, f"cli/{city.key}/{self.run_id}.html", f.body)
                self.state["cli"][city.key] = {"last_sha256": digest, "saved_at": iso(f.received)}
            self.run["cli"].append({"city": city.key, "changed": changed})

    # ---------- one-time live check ----------

    def live_check(self):
        """Fetch every source once on the first run against a new data repository.

        This exists so a broken connection shows up hours before the first real cutoff,
        not at it. Results go under checks/, are never treated as a cutoff capture, and
        are never used for scoring. It runs once; delete "live_check" from the state file
        to run it again.
        """
        now = self.clock.now()
        cdir = f"checks/{self.run_id}"
        rec = {"note": "connectivity check only; not a cutoff capture; never used for scoring",
               "cities": {}, "problems": []}
        self.run["live_check"] = rec
        self.state["live_check"] = {"run_id": self.run_id, "at": iso(now)}
        tickers = []
        for city in self.cities:
            info = {"problems": []}
            rec["cities"][city.key] = info
            s, _ = self.get(f"{KALSHI_BASE}/series/{city.series}", "check_kalshi_series",
                            f"{cdir}/{city.key}/kalshi_series.json", attempts=2)
            series = ((_json(s.body) or {}).get("series") or {}) if s.ok else {}
            info["fee_type"], info["fee_multiplier"] = series.get("fee_type"), series.get("fee_multiplier")
            info["settlement_sources"] = [x.get("name") for x in series.get("settlement_sources") or []]
            if not s.ok:
                info["problems"].append("series_unavailable")
            m, _ = self.get(
                f"{KALSHI_BASE}/markets?series_ticker={city.series}&status=open&limit=200",
                "check_kalshi_markets", f"{cdir}/{city.key}/kalshi_markets.json", attempts=2)
            payload = _json(m.body) if m.ok else None
            markets = payload.get("markets") if isinstance(payload, dict) else None
            if markets is None:
                info["problems"].append("markets_unavailable")
            else:
                events = sorted({x.get("event_ticker") for x in markets if x.get("event_ticker")})
                info["open_events"] = events
                if events:
                    picked = sorted(x["ticker"] for x in markets if x.get("event_ticker") == events[-1])
                    info["checked_event"], info["tickers"] = events[-1], picked
                    tickers += picked
                else:
                    info["problems"].append("no_open_events")
            target = now.astimezone(ZoneInfo(city.tz)).date() + timedelta(days=1)
            self.capture_weather(cdir, city, target, info, now)
            c, _ = self.get(city.cli_url, "check_nws_cli", f"{cdir}/{city.key}/nws_cli.html", attempts=2)
            if not c.ok:
                info["problems"].append("cli_unavailable")
        if tickers:
            url = f"{KALSHI_BASE}/markets/orderbooks?" + "&".join(
                "tickers=" + quote(t, safe="") for t in tickers)
            b, _ = self.get(url, "check_kalshi_books", f"{cdir}/books.json")
            books = (_json(b.body) or {}).get("orderbooks") if b.ok else None
            rec["books"] = {"ok": b.ok, "requested": len(tickers),
                            "returned": len(books) if isinstance(books, list) else None}
            if not b.ok or not isinstance(books, list) or len(books) != len(tickers):
                rec["problems"].append("books_incomplete")
        else:
            rec["problems"].append("no_tickers_to_check_books")
        problems = rec["problems"] + [f"{k}:{p}" for k, i in rec["cities"].items() for p in i["problems"]]
        rec["ok"] = not problems
        rec["all_problems"] = problems

    # ---------- run ----------

    def execute(self) -> dict:
        self.load_state()
        self.state_loaded = json.dumps(self.state, sort_keys=True)
        now = self.clock.now()
        for cutoff, items in due_cutoffs(now, self.cities, LEAD, MAX_LATE).items():
            todo = [(c, t) for c, t in items
                    if self.state["cutoffs"].get(c.key, {}).get(t.isoformat(), {}).get("status") != "captured"]
            if todo:
                self.guard(f"cutoff {iso(cutoff)}", lambda: self.capture_cutoff(cutoff, todo))
        self.guard("missed", self.record_missed)
        self.guard("settlement", self.poll_settlements)
        self.guard("cli", self.capture_cli)
        if self.live_check_on_first_run and self.state_ok and "live_check" not in self.state:
            self.guard("live_check", self.live_check)
        return self.finish()

    def finish(self) -> dict:
        # Save when anything was captured, or when tracking state changed (for example an
        # event was closed out), so that a change is never held only in memory.
        state_changed = self.state_ok and json.dumps(self.state, sort_keys=True) != self.state_loaded
        worth_saving = bool(self.files or self.run["cutoffs"] or self.run["missed"] or state_changed)
        failed = [c for rec in self.run["cutoffs"] for c, i in rec["cities"].items()
                  if i.get("status") != "captured"]
        # Saved, but outside the cutoff rule (for example a late first book): not scoreable.
        ineligible = {c: i.get("reason_code") for rec in self.run["cutoffs"]
                      for c, i in rec["cities"].items()
                      if i.get("status") == "captured" and not i.get("eligible")}
        summary = {
            "run_id": self.run_id, "files": len(self.files), "cutoffs": len(self.run["cutoffs"]),
            "failed_captures": failed, "ineligible_captures": ineligible,
            "missed": len(self.run["missed"]),
            "errors": len(self.run["errors"]), "commit": None,
        }
        if worth_saving:
            self.run["finished_at"] = iso(self.clock.now())
            self.run["files"] = {p: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                                 for p, b in sorted(self.files.items())}
            self.files[f"{self.base}/run.json"] = json.dumps(self.run, indent=1).encode()
            if self.state_ok:
                self.prune_state()
                self.files[STATE_PATH] = json.dumps(self.state, indent=1, sort_keys=True).encode()
            try:
                summary["commit"] = self.sink.commit(self.files, f"recorder run {self.run_id}")
            except Exception as exc:
                summary["commit_error"] = f"{type(exc).__name__}: {exc}"
        check = self.run.get("live_check")
        if check is not None:
            summary["live_check_ok"] = check.get("ok", False)
            summary["live_check_problems"] = check.get("all_problems")
        # Any error (including an unreadable state file or a bad token) fails the run, so it
        # shows up in Render's logs on the hour rather than at the next cutoff.
        summary["ok"] = ("commit_error" not in summary and not failed and not self.run["errors"]
                         and (check is None or check.get("ok", False)))
        return summary

    def prune_state(self):
        now = self.clock.now()
        self.state["events"] = {
            ev: i for ev, i in self.state["events"].items()
            if not (i.get("done") and now - _parse(i["first_seen"]) > timedelta(days=45))}
        horizon = (now - timedelta(days=60)).date().isoformat()
        for city, seen in self.state["cutoffs"].items():
            self.state["cutoffs"][city] = {d: v for d, v in seen.items() if d >= horizon}


def main() -> int:
    local = os.environ.get("RECORDER_LOCAL_DIR")
    repo, token = os.environ.get("DATA_REPO"), os.environ.get("GITHUB_TOKEN")
    if local:
        sink = LocalSink(local)
    elif repo and token:
        sink = GitHubSink(repo, token, os.environ.get("DATA_BRANCH", "main"))
    else:
        print("Set DATA_REPO and GITHUB_TOKEN (or RECORDER_LOCAL_DIR for a dry run).", file=sys.stderr)
        return 2
    recorder = Recorder(RealClock(), urllib_transport, sink,
                        code_version=os.environ.get("RENDER_GIT_COMMIT"))
    summary = recorder.execute()
    print(json.dumps(summary))
    if recorder.run["errors"]:
        print(json.dumps({"errors": recorder.run["errors"]}), file=sys.stderr)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
