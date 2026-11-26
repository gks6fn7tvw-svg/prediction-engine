"""Order-book pressure on Coinbase, recorded every second of the final stretch.

How much Bitcoin is waiting to be bought just under the price, against how much is waiting
to be sold just above it. A lopsided book often comes before the next few seconds' move.
This is only recorded for now, so we can test whether it calls Bitcoin's next 10-30
seconds before Kalshi's price moves. It is not part of the score.

Example: 4.1 BTC waiting to buy within $10 under the price and 0.9 BTC waiting to sell
within $10 above it is a book leaning up.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("btcbot")

BOOK_URL = "https://api.coinbase.com/api/v3/brokerage/market/product_book"
LEVELS = 250              # price levels asked for on each side
BANDS = (10, 25, 50)      # dollars from the middle price
COLUMNS = ["cb_mid"] + [f"cb_{side}{b}" for b in BANDS for side in ("bid", "ask")] + ["cb_reach", "cb_age"]


def summarize(bids: list, asks: list) -> dict | None:
    """Bitcoin waiting on each side within $10, $25 and $50 of the middle price.

    bids/asks are (price, size) pairs. cb_reach is how far the book we got goes on its
    shorter side, so a band wider than that is only partly counted.
    """
    bids = [(p, s) for p, s in bids if s > 0]
    asks = [(p, s) for p, s in asks if s > 0]
    if not bids or not asks:
        return None
    mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
    out = {"cb_mid": round(mid, 2)}
    for b in BANDS:
        out[f"cb_bid{b}"] = round(sum(s for p, s in bids if p >= mid - b), 4)
        out[f"cb_ask{b}"] = round(sum(s for p, s in asks if p <= mid + b), 4)
    out["cb_reach"] = round(min(mid - min(p for p, _ in bids), max(p for p, _ in asks) - mid), 2)
    return out


def fetch(session, timeout: float = 2.0) -> dict | None:
    """One look at Coinbase's BTC-USD book (the top 250 price levels on each side).

    Only this address: Coinbase's older one sends the whole book, megabytes a second,
    too heavy for the small server.
    """
    r = session.get(BOOK_URL, params={"product_id": "BTC-USD", "limit": LEVELS}, timeout=timeout)
    r.raise_for_status()
    book = r.json().get("pricebook") or {}
    side = lambda k: [(float(x["price"]), float(x["size"])) for x in book.get(k) or []]
    return summarize(side("bids"), side("asks"))


class Watcher:
    """Looks at the book about once a second in the background, only while the bot is in a
    final stretch, so the bot's own one-second loop never waits on Coinbase."""

    def __init__(self, fetch_fn=None, every: float = 1.0, idle: float = 5.0):
        if fetch_fn is None:
            import requests
            session = requests.Session()          # its own session: the bot's isn't shared across threads
            fetch_fn = lambda: fetch(session)
        self.fetch_fn = fetch_fn
        self.every, self.idle = every, idle
        self._latest, self._at = None, 0.0
        self._until = 0.0
        self._thread = None
        self._lock = threading.Lock()
        self.fails = 0

    def want(self) -> None:
        """Called every step while recording; keeps the background looks going a few more seconds."""
        self._until = time.monotonic() + self.idle
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="book-pressure", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while time.monotonic() < self._until:
            start = time.monotonic()
            try:
                got = self.fetch_fn()
            except Exception as e:
                got = None
                self.fails += 1
                if self.fails in (1, 10) or self.fails % 500 == 0:
                    log.warning("could not read Coinbase's order book (%s)", e)
            if got:
                with self._lock:
                    self._latest, self._at = got, time.monotonic()
            time.sleep(max(0.0, self.every - (time.monotonic() - start)))

    def read(self, max_age: float = 3.0) -> dict:
        """The latest look, with its age in seconds; empty if it's older than max_age."""
        with self._lock:
            got, at = self._latest, self._at
        age = time.monotonic() - at
        if not got or age > max_age:
            return {}
        return dict(got, cb_age=round(age, 1))
