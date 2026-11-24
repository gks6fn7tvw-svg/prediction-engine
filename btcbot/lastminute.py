"""Last-minute watch: estimate how a 15-minute market will settle, and log what two
last-minute ideas WOULD have done, without ever placing an order.

How Kalshi settles these markets: the official number is the simple average of the
60 once-a-second CF Benchmarks index prices (BRTI) in the final minute, compared with
the target. So once part of that minute has passed, part of the answer is locked in.
Example: target $97,000, 30 seconds left, the first 30 seconds averaged $96,950 and
Bitcoin is at $96,980 now. Even if it stays at $96,980, the final average is about
$96,965, so Down is very likely, whatever Kalshi's price still says.

The BRTI itself is a paid feed. Its basket is Bitstamp, Bullish, Coinbase, Crypto.com,
Gemini, Kraken and LMAX Digital; this module uses the middle (median) of the free
public prices from Coinbase, Kraken and Bitstamp as a stand-in. It is close, not exact.

Two ideas are watched (they never trade):
- guard:  you hold a bet, and in the final stretch the estimate turns against it by at
          least a margin -> "would sell now at the bid". After the close we compare
          that with holding to the end.
- follow: in the final stretch the estimate is past the target by at least a margin,
          and Kalshi's price for that side is still at or below a cap -> "would buy".
          After the close we record whether it would have won.
"""
from __future__ import annotations

import concurrent.futures
import csv
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

ONE = Decimal("1")
WINDOW = 60   # seconds Kalshi averages at the end

# Public "best bid / best ask" endpoints (no key needed). Coin -> exchange -> product.
PRODUCTS = {
    "BTC": {"coinbase": "BTC-USD", "kraken": "XBTUSD", "bitstamp": "btcusd"},
    "ETH": {"coinbase": "ETH-USD", "kraken": "ETHUSD", "bitstamp": "ethusd"},
    "SOL": {"coinbase": "SOL-USD", "kraken": "SOLUSD", "bitstamp": "solusd"},
    "XRP": {"coinbase": "XRP-USD", "kraken": "XRPUSD", "bitstamp": "xrpusd"},
}
SERIES_COIN = {"KXBTC": "BTC", "KXETH": "ETH", "KXSOL": "SOL", "KXXRP": "XRP"}

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)


def coin_for(series: str) -> str | None:
    return next((c for prefix, c in SERIES_COIN.items() if series.upper().startswith(prefix)), None)


def _mid(bid, ask) -> Decimal | None:
    try:
        b, a = Decimal(str(bid)), Decimal(str(ask))
    except Exception:
        return None
    return (b + a) / 2 if b > 0 and a > 0 else None


def exchange_mid(session, exchange: str, product: str, timeout: float = 1.5) -> Decimal | None:
    """Middle of one exchange's best bid and ask, or None if it didn't answer in time."""
    try:
        if exchange == "coinbase":
            r = session.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=timeout)
            j = r.json()
            return _mid(j.get("bid"), j.get("ask"))
        if exchange == "kraken":
            r = session.get("https://api.kraken.com/0/public/Ticker", params={"pair": product}, timeout=timeout)
            res = (r.json().get("result") or {})
            t = next(iter(res.values()), None)
            return _mid(t["b"][0], t["a"][0]) if t else None
        if exchange == "bitstamp":
            r = session.get(f"https://www.bitstamp.net/api/v2/ticker/{product}/", timeout=timeout)
            j = r.json()
            return _mid(j.get("bid"), j.get("ask"))
    except Exception:
        return None
    return None


def index_proxy(session, coin: str) -> tuple[Decimal | None, int]:
    """Stand-in for Kalshi's index: the median of the exchanges that answered, and how many did."""
    products = PRODUCTS.get(coin) or {}
    jobs = [_pool.submit(exchange_mid, session, ex, prod) for ex, prod in products.items()]
    mids = []
    for j in jobs:
        try:
            m = j.result(timeout=2.5)
        except Exception:
            m = None
        if m is not None:
            mids.append(m)
    if not mids:
        return None, 0
    return Decimal(str(statistics.median(mids))), len(mids)


# Bitcoin futures markets, biggest first. Research finds the big futures markets usually move
# before US exchanges like Coinbase; many of them block US servers, so the bot checks which answer.
FUTURES = [("binance", "Binance futures"), ("bybit", "Bybit"), ("okx", "OKX"), ("hyperliquid", "Hyperliquid"),
           ("coinbase_intl", "Coinbase International"), ("deribit", "Deribit"), ("bitmex", "BitMEX"),
           ("kraken_futures", "Kraken Futures")]


def _answered(r) -> None:
    code = getattr(r, "status_code", 200)
    if code != 200:
        raise RuntimeError(f"answered {code}" + (" (blocked from here)" if code in (403, 451) else ""))


def _futures_fetch(session, source: str, timeout: float) -> Decimal:
    """Middle of the best bid and ask for Bitcoin's perpetual future on one market (raises if it fails)."""
    if source == "hyperliquid":
        r = session.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=timeout)
        _answered(r)
        return Decimal(str(r.json()["BTC"]))
    urls = {
        "binance": ("https://fapi.binance.com/fapi/v1/ticker/bookTicker", {"symbol": "BTCUSDT"}),
        "bybit": ("https://api.bybit.com/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}),
        "okx": ("https://www.okx.com/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"}),
        "coinbase_intl": ("https://api.international.coinbase.com/api/v1/instruments/BTC-PERP/quote", None),
        "deribit": ("https://www.deribit.com/api/v2/public/ticker", {"instrument_name": "BTC-PERPETUAL"}),
        "bitmex": ("https://www.bitmex.com/api/v1/instrument", {"symbol": "XBTUSD", "columns": "bidPrice,askPrice"}),
        "kraken_futures": ("https://futures.kraken.com/derivatives/api/v3/tickers/PF_XBTUSD", None),
    }
    url, params = urls[source]
    r = session.get(url, params=params, timeout=timeout)
    _answered(r)
    j = r.json()
    if source == "binance":
        bid, ask = j["bidPrice"], j["askPrice"]
    elif source == "bybit":
        t = j["result"]["list"][0]
        bid, ask = t["bid1Price"], t["ask1Price"]
    elif source == "okx":
        t = j["data"][0]
        bid, ask = t["bidPx"], t["askPx"]
    elif source == "coinbase_intl":
        bid, ask = j["best_bid_price"], j["best_ask_price"]
    elif source == "deribit":
        bid, ask = j["result"]["best_bid_price"], j["result"]["best_ask_price"]
    elif source == "bitmex":
        bid, ask = j[0]["bidPrice"], j[0]["askPrice"]
    else:
        t = j.get("ticker") or next(x for x in j.get("tickers", []) if x.get("symbol") == "PF_XBTUSD")
        bid, ask = t["bid"], t["ask"]
    mid = _mid(bid, ask)
    if mid is None:
        raise RuntimeError("no price")
    return mid


def futures_mid(session, source: str, timeout: float = 1.5) -> Decimal | None:
    try:
        return _futures_fetch(session, source, timeout)
    except Exception:
        return None


def futures_check(session, timeout: float = 4.0) -> list[dict]:
    """Try every futures market once: which ones answer from this server, their price and speed."""
    import time as _t

    def one(item):
        source, label = item
        start = _t.monotonic()
        try:
            price = _futures_fetch(session, source, timeout)
            return {"source": source, "label": label, "ok": True, "price": price,
                    "ms": int((_t.monotonic() - start) * 1000), "note": ""}
        except Exception as e:
            return {"source": source, "label": label, "ok": False, "price": None,
                    "ms": int((_t.monotonic() - start) * 1000), "note": str(e)[:80] or type(e).__name__}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(FUTURES)) as ex:
        return list(ex.map(one, FUTURES))


@dataclass
class Estimate:
    projected: Decimal       # expected final average if the price stays where it is now
    gap: Decimal             # projected - target (+ = Up is ahead)
    locked: int              # seconds of the final minute already averaged (0-60)
    seconds_left: float
    price: Decimal           # the latest stand-in price

    def favored(self) -> str:
        return "yes" if self.gap >= 0 else "no"


@dataclass
class Tracker:
    """Prices seen for one market near its close."""
    close: datetime
    target: Decimal
    samples: list = field(default_factory=list)   # (time, price)

    def add(self, t: datetime, price: Decimal) -> None:
        self.samples.append((t, price))
        cutoff = self.close - timedelta(seconds=WINDOW + 5)
        while len(self.samples) > 1 and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def estimate(self, now: datetime) -> Estimate | None:
        if not self.samples:
            return None
        start = self.close - timedelta(seconds=WINDOW)
        inside = [p for t, p in self.samples if start <= t <= self.close]
        elapsed = max(0.0, min(float(WINDOW), (now - start).total_seconds()))
        current = self.samples[-1][1]
        if inside and elapsed > 0:
            avg = sum(inside, Decimal("0")) / len(inside)
            e = Decimal(str(elapsed))
            projected = (avg * e + current * (WINDOW - e)) / WINDOW
        else:
            projected = current
        left = (self.close - now).total_seconds()
        return Estimate(projected, projected - self.target, int(elapsed), left, current)


def seconds_words(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def money(d: Decimal) -> str:
    return f"${abs(d):,.0f}"


def gap_words(est: Estimate) -> str:
    where = "above" if est.gap >= 0 else "below"
    return f"the minute average is heading {money(est.gap)} {where} the target"


WATCH_COLUMNS = ["time", "kind", "play", "market", "side", "contracts", "seconds_left", "gap_dollars",
                 "price", "entry_price", "result", "would_profit", "held_profit", "difference"]
DATA_COLUMNS = ["time", "market", "seconds_left", "target", "price", "sources", "projected", "gap",
                "yes_bid", "yes_ask",
                # what the card showed that second, so backtests can replay it exactly
                "sigma", "chance_up", "score_up", "score_down", "level", "soon",
                # Kalshi's line: contracts waiting at the best Up bid / ask, and within 3c of them
                "up_bid_size", "up_ask_size", "up_bid_3c", "up_ask_3c",
                # Coinbase's book: Bitcoin waiting to buy / sell within $10, $25, $50 (see pressure.py)
                "cb_mid", "cb_bid10", "cb_ask10", "cb_bid25", "cb_ask25", "cb_bid50", "cb_ask50",
                "cb_reach", "cb_age"]


def append_csv(path: str, columns: list, row: dict) -> None:
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not new:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if header != columns:              # columns changed since the file was started: rewrite it once
            with open(path, newline="", encoding="utf-8") as fh:
                old = list(csv.DictReader(fh))
            with open(path + ".tmp", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=columns)
                w.writeheader()
                for r in old:
                    w.writerow({k: r.get(k, "") for k in columns})
            os.replace(path + ".tmp", path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in columns})


def read_watch(path: str) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def summary(rows: list[dict]) -> dict:
    """Totals for the website: per kind, how often, and money compared with what really happened."""
    out = {}
    for kind in ("guard", "crowd", "follow"):
        rs = [r for r in rows if r.get("kind") == kind]
        diff = sum((Decimal(r.get("difference") or "0") for r in rs), Decimal("0"))
        would = sum((Decimal(r.get("would_profit") or "0") for r in rs), Decimal("0"))
        wins = sum(1 for r in rs if Decimal(r.get("would_profit") or "0") > 0)
        helped = sum(1 for r in rs if Decimal(r.get("difference") or "0") > 0)
        out[kind] = {"count": len(rs), "difference": diff, "would": would, "wins": wins, "helped": helped}
    return out


# ---- live score: our chance for each side, compared with Kalshi's price -------------------
CANDLE_PATHS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
PROXY_ERROR = 3.0   # $: our stand-in index vs the real one (adds a little doubt near the target)


def candles_sigma(session, coin: str, minutes: int = 30, timeout: float = 4.0) -> float | None:
    """How jumpy the coin has been: typical $ move per second, from Coinbase's 1-minute candles."""
    product = CANDLE_PATHS.get(coin)
    if not product:
        return None
    try:
        r = session.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                        params={"granularity": 60}, timeout=timeout)
        rows = r.json()
        closes = [float(c[4]) for c in sorted(rows, key=lambda c: c[0])][-(minutes + 1):]
    except Exception:
        return None
    moves = [b - a for a, b in zip(closes, closes[1:])]
    if len(moves) < 5:
        return None
    per_minute = statistics.pstdev(moves)
    return per_minute / 60 ** 0.5 if per_minute > 0 else None


RSI_BAR, RSI_N, RSI_SPAN, RSI_STEP = 5, 14, 90, 3      # 5-second candles, 14 of them; show the last 90 s


def rsi_series(samples, now, bar: int = RSI_BAR, n: int = RSI_N, span: int = RSI_SPAN, step: int = RSI_STEP) -> list:
    """RSI (0-100) on `bar`-second candles, every `step` seconds over the last `span` seconds.

    samples: [(time, price)] oldest first. Returns [(seconds ago, rsi)]. Each RSI compares the
    ups and downs of the last 14 candles (about 70 seconds): all ups = 100, all downs = 0.
    Above 70 = it has been pushing up hard ("too high"); below 30 = pushing down hard.
    """
    import bisect
    from datetime import timedelta
    times = [t for t, _ in samples]
    prices = [p for _, p in samples]

    def at(t):
        i = bisect.bisect_right(times, t) - 1
        return prices[i] if i >= 0 else None
    out = []
    for ago in range(span, -1, -step):
        t = now - timedelta(seconds=ago)
        pts = [at(t - timedelta(seconds=bar * j)) for j in range(n, -1, -1)]
        if any(p is None for p in pts):
            continue
        diffs = [b - a for a, b in zip(pts, pts[1:])]
        gain = sum(x for x in diffs if x > 0) / n
        loss = sum(-x for x in diffs if x < 0) / n
        rsi = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        out.append((ago, rsi))
    return out


def rsi_guess(samples, now, ahead: int = 9, step: int = 3, carry: float = 0.3) -> list:
    """A for-fun guess of the RSI line over the next few seconds: [(seconds ahead, rsi)].

    It assumes the last 10 seconds' move carries on a little (30% of it over the next 10 s),
    which is roughly what the timing data showed for moves of a few seconds.
    Example: up $20 in the last 10 s -> it assumes about +$6 more by 10 s from now.
    """
    from datetime import timedelta
    if not samples:
        return []
    p_now = samples[-1][1]
    back = [p for t, p in samples if t <= now - timedelta(seconds=10)]
    if not back:
        return []
    push = (p_now - back[-1]) * carry
    future = [(now + timedelta(seconds=k), p_now + push * k / 10) for k in range(1, ahead + 1)]
    line = rsi_series(list(samples) + future, now + timedelta(seconds=ahead), span=ahead, step=step)
    return [(ahead - ago, r) for ago, r in line if ahead - ago > 0]


def jumpiness_from_closes(closes: list, window: int = 5) -> tuple | None:
    """How jumpy the coin is right now, 1-100, and its typical $ move per minute lately.

    "Right now" = the average size of the last 5 one-minute moves. It's ranked against every
    5-minute stretch in the candles given (about the last 5 hours): 80 = jumpier than 80% of
    them, 10 = calmer than nearly all. Example: moves of $40, $55, $30, $60, $45 -> $46 a
    minute, which might rank 85 on a quiet day.
    """
    moves = [abs(b - a) for a, b in zip(closes, closes[1:])]
    if len(moves) < window + 20:
        return None
    avgs = [sum(moves[i - window:i]) / window for i in range(window, len(moves) + 1)]
    now, past = avgs[-1], avgs[:-1]
    rank = sum(1 for v in past if v < now) / len(past) * 100
    return max(1, min(100, int(round(rank)))), now


def candles_jumpiness(session, coin: str, timeout: float = 4.0) -> tuple | None:
    """Jumpiness from Coinbase's 1-minute candles (it sends the last 300 minutes)."""
    product = CANDLE_PATHS.get(coin)
    if not product:
        return None
    try:
        r = session.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                        params={"granularity": 60}, timeout=timeout)
        closes = [float(c[4]) for c in sorted(r.json(), key=lambda c: c[0])]
    except Exception:
        return None
    return jumpiness_from_closes(closes)


def _phi(z: float) -> float:
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def close_sd(seconds_left: float, sigma: float) -> float:
    """How far ($, one typical amount) the final 60-second average may still land from `mean`."""
    import math
    t = max(float(seconds_left), 0.5)
    if t > WINDOW:
        sd = sigma * math.sqrt(t - WINDOW + WINDOW / 3)
    else:
        sd = sigma * (t / WINDOW) * math.sqrt(t / 3)
    return math.sqrt(sd * sd + PROXY_ERROR ** 2)


CLOSE_RANGE_SECONDS = 240     # the yellow "where it will probably close" range shows in the last 4 minutes
CLOSE_RANGE_Z = 1.645         # ...covering about 9 times in 10


def chance_up(mean: float, target: float, seconds_left: float, sigma: float) -> float:
    """Chance the final 60-second average ends at or above the target.

    Before the last minute: Bitcoin can wander for (seconds_left - 60) seconds, then the
    average over the final minute smooths part of the rest. Inside the last minute: only the
    seconds still to come can move it, and they count for (seconds_left / 60) of the average.
    `mean` is the current price before the last minute, or the running projection inside it.
    """
    import math
    t = max(float(seconds_left), 0.5)
    if t > WINDOW:
        sd = sigma * math.sqrt(t - WINDOW + WINDOW / 3)
    else:
        sd = sigma * (t / WINDOW) * math.sqrt(t / 3)
    sd = math.sqrt(sd * sd + PROXY_ERROR ** 2)
    return _phi((mean - target) / sd)


def score(chance: float, ask: float | None) -> int | None:
    """1-10: 5 = Kalshi's price is fair; higher = that side looks cheap for its chance (fee included)."""
    if ask is None or ask <= 0:
        return None
    import math
    fee = math.ceil(0.07 * ask * (1 - ask) * 100 - 1e-9) / 100
    edge = chance - (ask + fee)
    return max(1, min(10, math.floor(5 + edge * 25 + 0.5)))


SCORE_COLUMNS = ["time", "market", "seconds_left", "chance_up", "up_cost", "down_cost", "up_score",
                 "down_score", "best", "best_score", "level", "jump", "result", "best_won"]


def confidence(chance: float) -> int:
    """How sure the pick is to win: 1, 2 or 3 (price doesn't matter here).

    From the backtest (1,328 Bitcoin markets, Nov 9-23 2026), picks in each band won:
    1 = our chance under 70%   -> about 6 in 10
    2 = 70% up to 90%          -> about 8 in 10
    3 = 90% or more            -> about 19 in 20
    Being steady for minutes or "pulling away" didn't change how often a pick won once
    the chance was known, so they're left out. Example: Down at 93% -> 3, at 72% -> 2.
    """
    if chance >= 0.90:
        return 3
    if chance >= 0.70:
        return 2
    return 1


LEVEL_WORDS = {1: "about 6 in 10", 2: "about 8 in 10", 3: "about 19 in 20"}

SOON_SECONDS = 20     # "³ soon": a ² that turns ³ this many seconds from now if Bitcoin just holds
SOON_FROM = 240       # ...shown in the last 4 minutes only (where it was tested)

# The buy button lights up (last 4 minutes, on our pick) when either:
#   - "³ soon" shows and the price is 88c or less (best guess until a week of real data is in), or
#   - the score is 7+ (the two-week test: our pick at 7+ late made money; a plain ³ under 7 didn't).
BUY_SOON_MAX = Decimal("0.88")
BUY_SCORE = 7


def three_soon(mean: float, target: float, seconds_left: float, sigma: float, up: bool) -> bool:
    """True when the pick is a ² now and the clock alone makes it a ³ within 20 seconds.

    Tested on Nov 22-23 2026, every second: the first "³ soon" in a market hit ³ and won
    about 94 times in 100 (usually about 9 seconds later). It's a heads-up, not a deal:
    Kalshi's price climbs with the clock too, so buying on it alone didn't reliably pay.
    """
    if seconds_left > SOON_FROM:
        return False
    now = chance_up(mean, target, seconds_left, sigma)
    later = chance_up(mean, target, seconds_left - SOON_SECONDS, sigma)
    if not up:
        now, later = 1 - now, 1 - later
    return confidence(now) == 2 and confidence(later) == 3


CHECKPOINTS = (600, 300, 240, 180, 120, 60, 30)   # seconds left when a score is written down


def score_summary(rows: list[dict]) -> dict:
    """How often the best side won, by how high it scored."""
    out = {}
    for label, lo, hi in (("8-10", 8, 10), ("6-7", 6, 7), ("1-5", 1, 5)):
        rs = [r for r in rows if r.get("best_won") in ("yes", "no") and lo <= int(r.get("best_score") or 0) <= hi]
        out[label] = (sum(1 for r in rs if r["best_won"] == "yes"), len(rs))
    return out
