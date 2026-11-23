"""Collect past 15-minute markets for backtests.

For each settled market in the last N days: Kalshi's 1-minute prices (Up bid/ask), the
result, the official settlement number (the 60-second index average), and Bitcoin's
1-minute prices from Coinbase for the same minutes. One CSV row per market-minute.

It also adds Bitcoin's "buying power" for each minute from Binance, the biggest exchange:
how much was bought by eager buyers (who pay the asking price) out of everything traded.
Example: 30 BTC traded in a minute, 21 of it bought by eager buyers -> 70% buying.

Everything here is public market data (no key needed). It also checks, with your key if
one is saved, whether Kalshi lets this account read the official CF Benchmarks index
(BRTI) through its passthrough; that index is what the markets settle on.
"""
from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from datetime import datetime, timezone

COLUMNS = ["market", "close_time", "target", "result", "settled_value", "minutes_left",
           "up_bid", "up_ask", "up_bid_low", "up_bid_high", "up_ask_low", "up_ask_high", "volume",
           "btc_open", "btc_high", "btc_low", "btc_close", "buy_volume", "all_volume", "flow_from"]
COINBASE = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD", "KXSOL": "SOL-USD", "KXXRP": "XRP-USD"}
BINANCE = {"KXBTC": "BTCUSDT", "KXETH": "ETHUSDT", "KXSOL": "SOLUSDT", "KXXRP": "XRPUSDT"}
BINANCE_FILES = "https://data.binance.vision/data/spot/daily/klines/{sym}/1m/{sym}-1m-{day}.zip"
BINANCE_US = "https://api.binance.us/api/v3/klines"
MAX_PERIODS = 9000          # Kalshi allows 10,000 candle periods per batch request


def _ts(text: str) -> int | None:
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def settled_markets(client, series: str, start_ts: int, end_ts: int, sleep=time.sleep) -> list[dict]:
    """Markets of a series that closed in the window and have a result, oldest first."""
    found: dict[str, dict] = {}
    for path in ("/markets", "/historical/markets"):
        cursor = None
        for _ in range(60):
            params = {"series_ticker": series, "min_close_ts": start_ts, "max_close_ts": end_ts, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                data = client._request("GET", path, params=params)
            except Exception:
                break
            for m in data.get("markets", []):
                if m.get("result") in ("yes", "no") and m.get("ticker"):
                    found.setdefault(m["ticker"], m)
            cursor = data.get("cursor")
            sleep(0.3)
            if not cursor:
                break
        expected = (end_ts - start_ts) // 900
        if len(found) >= expected * 0.9:
            break                    # the live list had (nearly) everything; no need for older storage
    return sorted(found.values(), key=lambda m: _ts(m.get("close_time")) or 0)


def kalshi_candles(client, markets: list[dict], sleep=time.sleep, progress=None) -> dict[str, list]:
    """1-minute candles for each market, fetched in batches of neighbouring markets."""
    out: dict[str, list] = {}
    i = 0
    while i < len(markets):
        batch = [markets[i]]
        while i + len(batch) < len(markets):
            nxt = markets[i + len(batch)]
            first = _ts(batch[0]["close_time"]) - 960
            last = _ts(nxt["close_time"])
            if (last - first) // 60 * (len(batch) + 1) > MAX_PERIODS or len(batch) >= 100:
                break
            batch.append(nxt)
        start = _ts(batch[0]["close_time"]) - 960
        end = _ts(batch[-1]["close_time"])
        try:
            data = client._request("GET", "/markets/candlesticks", params={
                "market_tickers": ",".join(m["ticker"] for m in batch), "start_ts": start, "end_ts": end,
                "period_interval": 1})
            for entry in data.get("markets", []):
                out[entry.get("market_ticker")] = entry.get("candlesticks") or []
        except Exception:
            for m in batch:          # fall back to one market at a time (older ones live in storage)
                close = _ts(m["close_time"])
                for path in (f"/series/{m.get('series_ticker') or m['ticker'].split('-')[0]}/markets/"
                             f"{m['ticker']}/candlesticks", f"/historical/markets/{m['ticker']}/candlesticks"):
                    try:
                        data = client._request("GET", path, params={
                            "start_ts": close - 960, "end_ts": close, "period_interval": 1})
                        out[m["ticker"]] = data.get("candlesticks") or []
                        break
                    except Exception:
                        continue
                sleep(0.3)
        i += len(batch)
        if progress:
            progress(f"Kalshi prices: {min(i, len(markets))} of {len(markets)} markets")
        sleep(0.3)
    return out


def coin_candles(session, product: str, start_ts: int, end_ts: int, sleep=time.sleep, progress=None) -> dict:
    """Coinbase 1-minute candles: minute start -> (open, high, low, close)."""
    out = {}
    t = start_ts - start_ts % 60
    while t < end_ts:
        stop = min(t + 300 * 60, end_ts)
        try:
            r = session.get(f"https://api.exchange.coinbase.com/products/{product}/candles", timeout=10,
                            params={"granularity": 60,
                                    "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                                    "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()})
            for c in r.json():
                out[int(c[0])] = (c[3], c[2], c[1], c[4])
        except Exception:
            pass
        t = stop
        if progress:
            progress(f"Bitcoin prices: {100 * (t - start_ts) // max(1, end_ts - start_ts)}%")
        sleep(0.25)
    return out


def _kline_minutes(rows, source: str) -> dict:
    """Binance 1-minute klines -> {minute start: (bought by eager buyers, all traded, source)}."""
    out = {}
    for r in rows:
        try:
            t = int(float(r[0]))
            if t > 10 ** 14:
                t //= 1_000_000          # microseconds (Binance's daily files since 2025)
            elif t > 10 ** 11:
                t //= 1000               # milliseconds
            out[t - t % 60] = (float(r[9]), float(r[5]), source)
        except (IndexError, TypeError, ValueError):
            continue
    return out


def buying_power(session, symbol: str, start_ts: int, end_ts: int, sleep=time.sleep, progress=None) -> dict:
    """Per minute, how much of the coin eager buyers bought vs all traded, from Binance.

    Daily files from data.binance.vision first (they come out the next day); any minutes still
    missing (like today) from Binance.US, which has the same numbers for its own, smaller market.
    """
    out: dict = {}
    first = start_ts - start_ts % 86400
    days = list(range(first, end_ts, 86400))
    for i, d in enumerate(days):
        day = datetime.fromtimestamp(d, timezone.utc).strftime("%Y-%m-%d")
        try:
            r = session.get(BINANCE_FILES.format(sym=symbol, day=day), timeout=30)
            body = getattr(r, "content", b"") or b""
            if getattr(r, "status_code", 200) == 200 and body[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    text = z.read(z.namelist()[0]).decode("utf-8", "replace")
                rows = [line.split(",") for line in text.splitlines() if line[:1].isdigit()]
                out.update(_kline_minutes(rows, "binance"))
        except Exception:
            pass
        if progress:
            progress(f"Bitcoin buying power: day {i + 1} of {len(days)}")
        sleep(0.2)
    t = start_ts - start_ts % 60
    while t < end_ts:
        if all(m in out for m in range(t, min(t + 60000, end_ts), 60)):
            t += 60000
            continue
        try:
            r = session.get(BINANCE_US, timeout=15, params={
                "symbol": symbol, "interval": "1m", "startTime": t * 1000, "endTime": end_ts * 1000, "limit": 1000})
            rows = r.json() if getattr(r, "status_code", 200) == 200 else []
        except Exception:
            rows = []
        got = _kline_minutes(rows if isinstance(rows, list) else [], "binance.us")
        for m, v in got.items():
            out.setdefault(m, v)
        if not got:
            break
        t = max(got) + 60
        sleep(0.2)
    return out


def _d(candle: dict, key: str, part: str) -> str:
    v = (candle.get(key) or {}).get(part)
    return "" if v is None else str(v)


def write_rows(path: str, markets: list[dict], candles: dict, coin: dict, flow: dict | None = None) -> int:
    rows = 0
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for m in markets:
            close = _ts(m["close_time"])
            for c in candles.get(m["ticker"], []):
                end = int(c.get("end_period_ts") or 0)
                left = (close - end) // 60
                if not 0 <= left <= 15:
                    continue
                o, h, lo, cl = coin.get(end - 60, ("", "", "", ""))
                buy, allv, src = (flow or {}).get(end - 60, ("", "", ""))
                w.writerow({
                    "market": m["ticker"], "close_time": m["close_time"], "target": m.get("floor_strike", ""),
                    "result": m.get("result", ""), "settled_value": m.get("expiration_value", ""),
                    "minutes_left": left,
                    "up_bid": _d(c, "yes_bid", "close_dollars"), "up_ask": _d(c, "yes_ask", "close_dollars"),
                    "up_bid_low": _d(c, "yes_bid", "low_dollars"), "up_bid_high": _d(c, "yes_bid", "high_dollars"),
                    "up_ask_low": _d(c, "yes_ask", "low_dollars"), "up_ask_high": _d(c, "yes_ask", "high_dollars"),
                    "volume": c.get("volume_fp", ""), "btc_open": o, "btc_high": h, "btc_low": lo, "btc_close": cl,
                    "buy_volume": buy, "all_volume": allv, "flow_from": src})
                rows += 1
    os.replace(tmp, path)
    return rows


def brti_access(client) -> str:
    """Can this account read the official index through Kalshi? (needs a saved key)"""
    if not (client.key_id and client.private_key is not None):
        return "no key saved, so the official index (BRTI) access wasn't checked"
    try:
        client._request("GET", "/cfbenchmarks/values", params={"id": "BRTI"}, signed=True)
        return "YES: this account can read the official index (BRTI) that the markets settle on"
    except Exception as e:
        code = getattr(e, "status", "")
        return f"no: Kalshi didn't allow reading the official index (BRTI) ({code or e})"


def collect(client, session, series: str, days: int, path: str, progress=None, sleep=time.sleep,
            now: float | None = None) -> str:
    """Do the whole job; returns a one-line summary."""
    end = int(now if now is not None else time.time())
    start = end - days * 86400
    say = progress or (lambda msg: None)
    say("Finding past markets...")
    markets = settled_markets(client, series, start, end, sleep)
    if not markets:
        return "no past markets found"
    say(f"Found {len(markets)} markets. Getting Kalshi's prices...")
    candles = kalshi_candles(client, markets, sleep, say)
    product = next((p for prefix, p in COINBASE.items() if series.upper().startswith(prefix)), None)
    coin = coin_candles(session, product, start - 960, end, sleep, say) if product else {}
    symbol = next((b for prefix, b in BINANCE.items() if series.upper().startswith(prefix)), None)
    try:
        flow = buying_power(session, symbol, start - 960, end, sleep, say) if symbol else {}
    except Exception:
        flow = {}
    rows = write_rows(path, markets, candles, coin, flow)
    with_prices = sum(1 for m in markets if candles.get(m["ticker"]))
    sources = sorted({v[2] for v in flow.values()})
    power = (f"buying power for {len(flow)} minutes from {' + '.join(sources)}" if flow
             else "buying power: Binance didn't answer")
    return (f"{len(markets)} markets ({with_prices} with Kalshi prices), {rows} minute rows; {power}; "
            f"official index: {brti_access(client)}")
