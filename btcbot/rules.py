"""The trading rules. Pure functions with no network, so they are easy to test.

Words used here:
- "yes" = Up  (Bitcoin ends at or above the target)
- "no"  = Down
- ask = what you pay to buy now; bid = what you get if you sell now.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .kalshi import dollars
from .settings import Play, Settings

ONE = Decimal("1")
CENT = Decimal("0.01")
TAKER_RATE = Decimal("0.07")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass
class Quote:
    ticker: str
    series: str
    close_time: datetime | None
    status: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask_size: Decimal | None
    target: Decimal | None

    @classmethod
    def from_market(cls, m: dict, series: str) -> "Quote":
        def price(key):
            p = dollars(m.get(key))
            return p if p is not None and Decimal("0") < p < ONE else None

        def size(key):
            s = dollars(m.get(key))
            return s if s is not None and s >= 0 else None

        strike = m.get("floor_strike")
        return cls(
            ticker=m.get("ticker", ""),
            series=series,
            close_time=parse_time(m.get("close_time")),
            status=str(m.get("status", "")),
            yes_bid=price("yes_bid_dollars"),
            yes_ask=price("yes_ask_dollars"),
            yes_bid_size=size("yes_bid_size_fp"),
            yes_ask_size=size("yes_ask_size_fp"),
            target=Decimal(str(strike)) if strike not in (None, "") else None,
        )

    def as_row(self) -> dict:
        """Plain prices for the website."""
        text = lambda v: None if v is None else str(v)
        return {"ticker": self.ticker, "series": self.series,
                "close_time": self.close_time.isoformat() if self.close_time else None,
                "yes_bid": text(self.yes_bid), "yes_ask": text(self.yes_ask), "target": text(self.target)}

    # Buying NO matches someone's YES bid, so NO ask = 1 - YES bid (and the reverse).
    def ask(self, side: str) -> Decimal | None:
        if side == "yes":
            return self.yes_ask
        return ONE - self.yes_bid if self.yes_bid is not None else None

    def bid(self, side: str) -> Decimal | None:
        if side == "yes":
            return self.yes_bid
        return ONE - self.yes_ask if self.yes_ask is not None else None

    def ask_size(self, side: str) -> Decimal | None:
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    def bid_size(self, side: str) -> Decimal | None:
        return self.yes_bid_size if side == "yes" else self.yes_ask_size

    def yes_mid(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    def minutes_left(self, now: datetime) -> Decimal | None:
        if self.close_time is None:
            return None
        return Decimal(str((self.close_time - now).total_seconds())) / 60


def side_name(side: str) -> str:
    return "UP" if side == "yes" else "DOWN"


def favorite(q: Quote) -> str | None:
    """The side the market thinks is more likely, judged by the middle price."""
    mid = q.yes_mid()
    if mid is None or mid == Decimal("0.5"):
        return None
    return "yes" if mid > Decimal("0.5") else "no"


def pick_side(play: Play, q: Quote) -> str | None:
    if play.buy == "up":
        return "yes"
    if play.buy == "down":
        return "no"
    fav = favorite(q)
    if fav is None:
        return None
    if play.buy == "favorite":
        return fav
    return "no" if fav == "yes" else "yes"  # underdog


def in_window(play: Play, minutes_left: Decimal | None) -> bool:
    return minutes_left is not None and play.act_until_minutes_left <= minutes_left <= play.act_from_minutes_left


def taker_fee(contracts: Decimal, price: Decimal, multiplier: Decimal = ONE) -> Decimal:
    """Kalshi's general taker fee for one order, rounded UP to the cent.

    fee = 0.07 x contracts x P x (1 - P) x series multiplier. Rounding to the
    whole cent per order matches the fee schedule table for normal accounts.
    """
    raw = TAKER_RATE * multiplier * contracts * price * (ONE - price)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


@dataclass
class EntryCheck:
    ok: bool
    reason: str
    side: str | None = None
    ask: Decimal | None = None
    limit: Decimal | None = None


def check_entry(play: Play, q: Quote, now: datetime, history=None, spot: Decimal | None = None) -> EntryCheck:
    """Decide whether `play` wants to buy in this market right now (ignores money)."""
    mins = q.minutes_left(now)
    if not in_window(play, mins):
        return EntryCheck(False, "outside time window")
    side = pick_side(play, q)
    if side is None:
        return EntryCheck(False, "no clear favorite (prices missing or exactly 50/50)")
    ask = q.ask(side)
    if ask is None:
        return EntryCheck(False, f"nobody is selling {side_name(side)} right now", side)
    if ask < play.min_price or ask > play.max_price:
        return EntryCheck(False, f"{side_name(side)} costs {ask} (wanted {play.min_price}-{play.max_price})", side, ask)
    c = play.conditions
    if c.max_spread_cents is not None:
        bid = q.bid(side)
        if bid is None:
            return EntryCheck(False, "no buyers, so the spread is unknown", side, ask)
        spread = (ask - bid) * 100
        if spread > c.max_spread_cents:
            return EntryCheck(False, f"spread {spread}c is wider than {c.max_spread_cents}c", side, ask)
    if c.min_contracts_at_price is not None:
        size = q.ask_size(side)
        if size is None or size < c.min_contracts_at_price:
            return EntryCheck(False, f"only {size} contracts offered at {ask}", side, ask)
    if c.favorite_rising_seconds is not None:
        past = _price_seconds_ago(history, now, c.favorite_rising_seconds)
        cur = q.yes_mid()
        if past is None or cur is None:
            return EntryCheck(False, "not enough price history yet for the rising check", side, ask)
        rising = cur > past if side == "yes" else cur < past
        if not rising:
            return EntryCheck(False, f"{side_name(side)} has not risen in the last {c.favorite_rising_seconds}s", side, ask)
    if c.btc_distance_min_dollars is not None:
        if spot is None or q.target is None:
            return EntryCheck(False, "spot price or target missing for the distance check", side, ask)
        gap = spot - q.target if side == "yes" else q.target - spot
        if gap < c.btc_distance_min_dollars:
            return EntryCheck(False, f"price is only ${gap} past the target (wanted ${c.btc_distance_min_dollars})", side, ask)
    limit = min(play.max_price, ask + play.slippage_cents * CENT)
    return EntryCheck(True, "ok", side, ask, limit)


def _price_seconds_ago(history, now: datetime, seconds: Decimal) -> Decimal | None:
    """Most recent YES middle price that is at least `seconds` old."""
    if not history:
        return None
    best = None
    for t, mid in history:
        if (now - t).total_seconds() >= float(seconds):
            best = mid
    return best


def size_contracts(s: Settings, balance: Decimal, price: Decimal, open_cost: Decimal,
                   play: Play | None = None) -> Decimal:
    """How many whole contracts to buy, after every money limit.

    Uses the play's own size when it has one, otherwise the default size.
    """
    if price <= 0:
        return Decimal("0")
    size = s.size_for(play)
    if size.mode == "contracts":
        n = size.value
    elif size.mode == "dollars":
        n = size.value / price
    else:  # percent of balance
        n = balance * size.value / 100 / price
    n = n.to_integral_value(rounding=ROUND_FLOOR)
    if size.max_contracts > 0:
        n = min(n, size.max_contracts)
    # "How much of your Kalshi it can use": money in open trades stays under this share.
    room = (balance + open_cost) * s.balance_share_pct / 100 - open_cost
    while n > 0 and n * price + taker_fee(n, price) > room:
        n -= 1
    while n > 0 and n * price + taker_fee(n, price) > balance:  # never spend more cash than you have
        n -= 1
    return max(n, Decimal("0"))


def daily_loss_limit(s: Settings, day_start_balance: Decimal) -> Decimal:
    if s.daily_loss_mode == "percent":
        return day_start_balance * s.daily_loss_value / 100
    return s.daily_loss_value


def exit_signal(play: Play, side: str, q: Quote) -> tuple[str, Decimal] | None:
    """Take profit or stop loss, judged on what you could sell for right now."""
    if not play.has_exits:
        return None
    bid = q.bid(side)
    if bid is None:
        return None
    if play.take_profit is not None and bid >= play.take_profit:
        return ("take_profit", bid)
    if play.stop_loss is not None and bid <= play.stop_loss:
        return ("stop_loss", bid)
    return None
