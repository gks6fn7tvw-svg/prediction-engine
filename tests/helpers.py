"""Test doubles: a fake clock and a transport that replays saved real responses."""
import json
import os
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

STATION_BY_POINT = {"40.7833,-73.9667": "KNYC", "25.7906,-80.3164": "KMIA"}
STATION_BY_GRID = {"OKX/34,45": "KNYC", "MFL/105,51": "KMIA"}

GRIDPOINT = json.dumps({"properties": {
    "updateTime": "2026-11-10T20:36:50+00:00",
    "maxTemperature": {"uom": "wmoUnit:degC", "values": [
        {"validTime": "2026-11-11T12:00:00+00:00/PT13H", "value": 16.1},
        {"validTime": "2026-11-13T12:00:00+00:00/PT13H", "value": 12.2}]}}}).encode()


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


class FakeClock:
    def __init__(self, start):
        self.t, self.m, self.sleeps = start, 1000.0, []

    def now(self):
        return self.t

    def monotonic(self):
        return self.m

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)
        self.m += seconds

    def sleep(self, seconds):
        if seconds > 0:
            self.sleeps.append(seconds)
            self.advance(seconds)


class ShortSleepClock(FakeClock):
    """A clock whose sleeps return early, to prove waits are re-checked rather than trusted."""

    def sleep(self, seconds):
        super().sleep(seconds / 2 if seconds > 0.001 else seconds)


class Replay:
    """Answers Kalshi and NWS URLs from files captured on 2026-11-10."""

    def __init__(self, clock, latency=0.2):
        self.clock, self.latency = clock, latency
        self.calls = []
        self.settlement = {}   # event ticker -> list of market dicts to return
        self.cli_override = {}  # cli id -> bytes
        self.broken = set()    # substrings; any URL containing one fails with 503
        self.latency_for = {}  # substring -> seconds this kind of request takes to answer
        self.drop_books = set()  # ticker prefixes left out of an otherwise normal 200 response

    def __call__(self, method, url, headers, body, timeout):
        self.clock.advance(next((s for k, s in self.latency_for.items() if k in url), self.latency))
        self.calls.append((self.clock.now(), url))
        if any(b in url for b in self.broken):
            return 503, {}, b"unavailable"
        u = urlparse(url)
        q = parse_qs(u.query)
        ok = lambda data: (200, {"Date": "Wed, 11 Nov 2026 00:00:00 GMT"}, data)
        try:
            if u.netloc == "external-api.kalshi.com":
                if "/series/" in u.path:
                    return ok(fixture(u.path.rsplit("/", 1)[1] + "_series.json"))
                if u.path.endswith("/markets/orderbooks"):
                    books = []
                    for series in sorted({t.split("-")[0] for t in q["tickers"]}):
                        books += json.loads(fixture(series + "_orderbooks.json"))["orderbooks"]
                    wanted = {t for t in q["tickers"]
                              if not any(t.startswith(p) for p in self.drop_books)}
                    return ok(json.dumps({"orderbooks": [b for b in books if b["ticker"] in wanted]}).encode())
                if u.path.endswith("/markets"):
                    if "event_ticker" in q:
                        ev = q["event_ticker"][0]
                        return ok(json.dumps({"cursor": "", "markets": self.settlement.get(ev, [])}).encode())
                    return ok(fixture(q["series_ticker"][0] + "_markets.json"))
            if u.netloc == "api.weather.gov":
                if "/points/" in u.path:
                    return ok(fixture(STATION_BY_POINT[u.path.rsplit("/", 1)[1]] + "_point.json"))
                grid = u.path.split("/gridpoints/")[1]
                if grid.endswith("/forecast/hourly"):
                    return ok(fixture(STATION_BY_GRID[grid[:-len("/forecast/hourly")]] + "_hourly.json"))
                return ok(GRIDPOINT)
            if u.netloc == "forecast.weather.gov":
                cli = q["issuedby"][0]
                return ok(self.cli_override.get(cli) or fixture(f"CLI{cli}.html"))
        except (KeyError, FileNotFoundError):
            pass
        return 404, {}, b"not found"


def event_markets(series: str, event: str, **changes) -> list:
    """The saved markets for one event, optionally with fields overridden."""
    markets = json.loads(fixture(series + "_markets.json"))["markets"]
    return [{**m, **changes} for m in markets if m["event_ticker"] == event]
