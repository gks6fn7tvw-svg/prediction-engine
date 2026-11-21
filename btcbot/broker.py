"""Two ways to place a trade: pretend (paper) or real (live). Same interface."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .kalshi import KalshiClient, dollars
from .rules import ONE, Quote, taker_fee

log = logging.getLogger("btcbot")
TOLERANCE = Decimal("0.005")


@dataclass
class Fill:
    contracts: Decimal
    price: Decimal   # average price for OUR side (Up or Down), in dollars
    fee: Decimal     # total fee for this order, in dollars


class PaperBroker:
    """Fills at the price on screen, only up to the contracts offered there.

    It never waits in line for a cheaper price, so every fill pays the taker fee.
    """

    live = False

    def __init__(self, cash: Decimal):
        self.cash = cash

    def balance(self) -> Decimal:
        return self.cash

    def buy(self, q: Quote, side: str, contracts: Decimal, limit: Decimal, multiplier: Decimal) -> Fill | None:
        ask = q.ask(side)
        if ask is None or ask > limit:
            return None
        n = _cap(contracts, q.ask_size(side))
        if n <= 0:
            return None
        fee = taker_fee(n, ask, multiplier)
        if n * ask + fee > self.cash:
            return None
        self.cash -= n * ask + fee
        return Fill(n, ask, fee)

    def sell(self, q: Quote, side: str, contracts: Decimal, min_price: Decimal, multiplier: Decimal) -> Fill | None:
        bid = q.bid(side)
        if bid is None or bid < min_price:
            return None
        n = _cap(contracts, q.bid_size(side))
        if n <= 0:
            return None
        fee = taker_fee(n, bid, multiplier)
        self.cash += n * bid - fee
        return Fill(n, bid, fee)

    def collect(self, payout: Decimal) -> None:
        self.cash += payout


class LiveBroker:
    """Real orders on Kalshi. Every order fills right away or cancels (no waiting orders)."""

    live = True

    def __init__(self, client: KalshiClient):
        self.client = client

    def balance(self) -> Decimal:
        return self.client.balance_dollars()

    def buy(self, q: Quote, side: str, contracts: Decimal, limit: Decimal, multiplier: Decimal) -> Fill | None:
        if side == "yes":
            resp = self.client.place_ioc(q.ticker, "bid", contracts, limit)
        else:  # buying Down at `limit` = selling Up at 1 - limit
            resp = self.client.place_ioc(q.ticker, "ask", contracts, ONE - limit)
        return self._fill(resp, side, limit, multiplier, buying=True)

    def sell(self, q: Quote, side: str, contracts: Decimal, min_price: Decimal, multiplier: Decimal) -> Fill | None:
        if side == "yes":
            resp = self.client.place_ioc(q.ticker, "ask", contracts, min_price, reduce_only=True)
        else:  # selling Down at `min_price` = buying Up at 1 - min_price
            resp = self.client.place_ioc(q.ticker, "bid", contracts, ONE - min_price, reduce_only=True)
        return self._fill(resp, side, min_price, multiplier, buying=False)

    def collect(self, payout: Decimal) -> None:
        pass  # Kalshi pays winning contracts into your account by itself

    def _fill(self, resp: dict, side: str, limit: Decimal, multiplier: Decimal, buying: bool) -> Fill | None:
        n = dollars(resp.get("fill_count")) or Decimal("0")
        if n <= 0:
            log.info("order did not fill (order id %s)", resp.get("order_id"))
            return None
        yes_avg = dollars(resp.get("average_fill_price"))
        price = self._our_price(yes_avg, side, limit, buying)
        fee_each = dollars(resp.get("average_fee_paid"))
        fee = (fee_each * n).quantize(Decimal("0.0001")) if fee_each is not None else taker_fee(n, price, multiplier)
        log.info("order %s filled %s contracts at %s (fee $%s)", resp.get("order_id"), n, price, fee)
        return Fill(n, price, fee)

    @staticmethod
    def _our_price(yes_avg: Decimal | None, side: str, limit: Decimal, buying: bool) -> Decimal:
        """V2 orders are written on the Up (YES) leg; turn the fill price into our side's price.

        For Down, the YES-leg price P means a Down price of 1 - P. If that does not
        fit the limit we sent but the raw number does, Kalshi already reported the
        Down price, so use it and warn. Both checks keep a bad number out of the log.
        """
        if yes_avg is None:
            log.warning("fill price missing from Kalshi's reply; using the limit %s", limit)
            return limit
        if side == "yes":
            return yes_avg
        converted = ONE - yes_avg
        fits = (lambda p: p <= limit + TOLERANCE) if buying else (lambda p: p >= limit - TOLERANCE)
        if fits(converted):
            return converted
        if fits(yes_avg):
            log.warning("Kalshi seems to report the Down price directly (%s); using it", yes_avg)
            return yes_avg
        log.warning("fill price %s does not match the limit %s; logging 1 - price", yes_avg, limit)
        return converted


def _cap(contracts: Decimal, offered: Decimal | None) -> Decimal:
    if offered is None:
        return contracts
    return min(contracts, offered.to_integral_value(rounding=ROUND_FLOOR))
