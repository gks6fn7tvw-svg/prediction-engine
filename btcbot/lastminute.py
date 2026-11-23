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
}
SERIES_COIN = {"KXBTC": "BTC", "KXETH": "ETH", "KXSOL": "SOL"}

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
                "yes_bid", "yes_ask"]


def append_csv(path: str, columns: list, row: dict) -> None:
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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
CANDLE_PATHS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
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


def _phi(z: float) -> float:
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


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
                 "down_score", "best", "best_score", "result", "best_won"]
CHECKPOINTS = (600, 300, 240, 180, 120, 60, 30)   # seconds left when a score is written down


def score_summary(rows: list[dict]) -> dict:
    """How often the best side won, by how high it scored."""
    out = {}
    for label, lo, hi in (("8-10", 8, 10), ("6-7", 6, 7), ("1-5", 1, 5)):
        rs = [r for r in rows if r.get("best_won") in ("yes", "no") and lo <= int(r.get("best_score") or 0) <= hi]
        out[label] = (sum(1 for r in rs if r["best_won"] == "yes"), len(rs))
    return out
