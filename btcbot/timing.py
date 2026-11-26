"""Timing data: who moves first, second by second, in the last minutes of each market.

For the last 5 minutes of every settled Bitcoin 15-minute market in the last N days it
downloads the trades (each with its exact time) from:
  - Kalshi (the market itself),
  - Deribit and OKX (big Bitcoin futures markets that answer from US servers),
  - Kraken and Coinbase (two of the exchanges in the index Kalshi settles on).
Then it writes one row per second with the last traded price on each, so we can measure how
many seconds the futures move before the exchanges and before Kalshi.

Everything here is public data (no key needed). A source that keeps failing is skipped for
the rest of the run, and the summary says which ones worked.
"""
from __future__ import annotations

import concurrent.futures
import csv
import math
import os
import time
from datetime import datetime, timezone

from .history import _ts, settled_markets


def _when(text) -> float | None:
    """ISO time -> seconds, keeping fractions (12:00:00.4 stays .4, so it lands in the right second)."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None

SERIES = "KXBTC15M"
WINDOW = 300            # seconds before each close
SOURCES = ["kalshi", "deribit", "okx", "kraken", "coinbase"]
COLUMNS = ["market", "close_time", "target", "result", "seconds_left", "kalshi", "kalshi_trades",
           "deribit", "okx", "kraken", "coinbase"]
OKX_MAX_CALLS = 15      # OKX sends 100 trades per call; this caps the time spent per market


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kalshi_trades(client, ticker: str, start: int, end: int, sleep=time.sleep) -> list:
    """[(time, Up price 0-1, contracts)] for one market (older markets: Kalshi's historical store)."""
    out = _kalshi_trades(client, "/markets/trades", ticker, start, end, sleep)
    if not out:
        try:
            out = _kalshi_trades(client, "/historical/trades", ticker, start, end, sleep)
        except Exception:
            pass
    return out


def _kalshi_trades(client, path: str, ticker: str, start: int, end: int, sleep) -> list:
    out, cursor = [], None
    for _ in range(30):
        params = {"ticker": ticker, "min_ts": start, "max_ts": end, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = client._request("GET", path, params=params)
        trades = data.get("trades") or []
        for t in trades:
            ts = _when(t.get("created_time"))
            price = _num(t.get("yes_price_dollars"))
            if price is None and _num(t.get("yes_price")) is not None:
                price = _num(t.get("yes_price")) / 100
            n = _num(t.get("count_fp")) or _num(t.get("count")) or 0
            if ts is not None and price is not None:
                out.append((ts, price, n))
        cursor = data.get("cursor")
        if not cursor or not trades:
            break
        sleep(0.1)
    return out


def _get(session, url: str, params=None, timeout: float = 10.0):
    r = session.get(url, params=params, timeout=timeout)
    code = getattr(r, "status_code", 200)
    if code != 200:
        raise RuntimeError(f"answered {code}" + (" (blocked from here)" if code in (403, 451) else ""))
    return r.json()


def deribit_trades(session, start: int, end: int, sleep=time.sleep) -> list:
    """[(time, price)] for Deribit's BTC perpetual."""
    url = "https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time"
    params = {"instrument_name": "BTC-PERPETUAL", "start_timestamp": start * 1000,
              "end_timestamp": end * 1000, "count": 1000, "sorting": "asc"}
    out = []
    for _ in range(30):
        res = _get(session, url, params).get("result") or {}
        trades = res.get("trades") or []
        out += [(t["timestamp"] / 1000, float(t["price"])) for t in trades]
        if not res.get("has_more") or not trades:
            break
        params["start_timestamp"] = trades[-1]["timestamp"] + 1
        sleep(0.05)
    return out


def okx_trades(session, start: int, end: int, sleep=time.sleep) -> list:
    """[(time, price)] for OKX's BTC-USDT perpetual, newest first from the close backwards."""
    url = "https://www.okx.com/api/v5/market/history-trades"
    after, out = end * 1000 + 1, []
    for _ in range(OKX_MAX_CALLS):
        data = _get(session, url, {"instId": "BTC-USDT-SWAP", "type": "2", "after": str(after), "limit": "100"})
        rows = data.get("data") or []
        if not rows:
            break
        stamps = []
        for d in rows:
            ts = int(d["ts"])
            stamps.append(ts)
            if ts >= start * 1000:
                out.append((ts / 1000, float(d["px"])))
        oldest = min(stamps)
        if oldest < start * 1000 or oldest >= after:
            break
        after = oldest
        sleep(0.12)                      # OKX allows about 10 of these a second
    return sorted(out)


def kraken_trades(session, start: int, end: int, sleep=time.sleep) -> list:
    """[(time, price)] for Kraken's BTC/USD (one of the index exchanges)."""
    url = "https://api.kraken.com/0/public/Trades"
    since, out = str(start), []
    for _ in range(20):
        data = _get(session, url, {"pair": "XBTUSD", "since": since, "count": 1000})
        if data.get("error"):
            raise RuntimeError(", ".join(data["error"]))
        res = data.get("result") or {}
        rows = next((v for k, v in res.items() if k != "last"), [])
        out += [(float(t[2]), float(t[0])) for t in rows if start <= float(t[2]) <= end]
        if not rows or float(rows[-1][2]) > end or len(rows) < 1000:
            break
        since = res.get("last") or since
        sleep(1.0)                       # Kraken's public limit is about one call a second
    return out


def coinbase_trades(session, start: int, end: int, sleep=time.sleep) -> list:
    """[(time, price)] for Coinbase's BTC-USD, a minute at a time."""
    url = "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/ticker"
    out = []
    for a in range(start, end, 60):
        b = min(a + 60, end)
        data = _get(session, url, {"limit": 1000, "start": str(a), "end": str(b)})
        for t in data.get("trades") or []:
            ts = _when(t.get("time"))
            if ts is not None and _num(t.get("price")) is not None:
                out.append((float(ts), float(t["price"])))
        sleep(0.1)
    return sorted(out)


def per_second(trades: list, start: int, end: int) -> list:
    """Last traded price at each whole second start+1..end (blank until the first trade)."""
    trades = sorted(trades)
    out, i, last = [], 0, None
    for s in range(start + 1, end + 1):
        while i < len(trades) and trades[i][0] <= s:
            last = trades[i][1]
            i += 1
        out.append(last)
    return out


def collect(client, session, days: int, path: str, progress=None, sleep=time.sleep, now: float | None = None) -> str:
    """Do the whole job; returns a one-line summary."""
    say = progress or (lambda msg: None)
    end_all = int(now if now is not None else time.time())
    say("Finding past markets...")
    markets = settled_markets(client, SERIES, end_all - days * 86400, end_all, sleep)
    if not markets:
        return "no past markets found"
    fetchers = {
        "kalshi": lambda m, a, b: kalshi_trades(client, m["ticker"], a, b, sleep),
        "deribit": lambda m, a, b: deribit_trades(session, a, b, sleep),
        "okx": lambda m, a, b: okx_trades(session, a, b, sleep),
        "kraken": lambda m, a, b: kraken_trades(session, a, b, sleep),
        "coinbase": lambda m, a, b: coinbase_trades(session, a, b, sleep),
    }
    worked = {s: 0 for s in SOURCES}
    fails = {s: 0 for s in SOURCES}
    reason = {s: "" for s in SOURCES}
    given_up: set = set()
    rows = 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as fh, \
            concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for i, m in enumerate(markets):
            close = _ts(m.get("close_time"))
            if close is None:
                continue
            start = close - WINDOW
            say(f"Market {i + 1} of {len(markets)}")
            jobs = {s: pool.submit(f, m, start, close) for s, f in fetchers.items() if s not in given_up}
            got = {}
            for s, job in jobs.items():
                try:
                    got[s] = job.result(timeout=120)
                    worked[s] += 1
                    fails[s] = 0
                except Exception as e:
                    fails[s] += 1
                    reason[s] = str(e)[:60] or type(e).__name__
                    if fails[s] >= 5 and worked[s] == 0:
                        given_up.add(s)          # never worked: stop wasting time on it
            k = got.get("kalshi") or []
            kalshi = per_second([(t, p) for t, p, _ in k], start, close)
            counts = [0] * WINDOW               # Kalshi trades in each second (same seconds as the prices)
            for t, _, _ in k:
                idx = math.ceil(t) - start - 1
                if 0 <= idx < WINDOW:
                    counts[idx] += 1
            others = {s: per_second(got.get(s) or [], start, close) for s in ("deribit", "okx", "kraken", "coinbase")}
            for j in range(WINDOW):
                w.writerow({"market": m["ticker"], "close_time": m.get("close_time"),
                            "target": m.get("floor_strike", ""), "result": m.get("result", ""),
                            "seconds_left": WINDOW - 1 - j, "kalshi": _fmt(kalshi[j], 4),
                            "kalshi_trades": counts[j],
                            **{s: _fmt(others[s][j], 2) for s in others}})
                rows += 1
    os.replace(path + ".tmp", path)
    parts = []
    for s in SOURCES:
        name = {"kalshi": "Kalshi", "deribit": "Deribit", "okx": "OKX", "kraken": "Kraken",
                "coinbase": "Coinbase"}[s]
        parts.append(f"{name} ✓ {worked[s]}" if worked[s] else f"{name} ✗ ({reason[s] or 'no answer'})")
    return f"{len(markets)} markets, {rows} second rows; " + " · ".join(parts)


def _fmt(v, places: int) -> str:
    return "" if v is None else f"{v:.{places}f}"
