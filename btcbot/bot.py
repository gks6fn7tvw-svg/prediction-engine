"""The main loop: watch prices, buy when a play's rules say so, sell or settle."""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import rules
from .broker import Fill
from .kalshi import KalshiClient, KalshiError, coinbase_spot
from .ledger import Ledger, Position, State
from .settings import Settings

log = logging.getLogger("btcbot")

MAX_ORDER_TRIES = 5          # per play per market, so a moving price can't spam orders
SETTLE_DELAY = 5             # seconds after close before asking for the result
SETTLE_RECHECK = 10          # seconds between result checks
HISTORY_SECONDS = 900        # keep 15 minutes of prices for the "rising" check
SPOT_PRODUCTS = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD", "KXSOL": "SOL-USD",
                 "KXXRP": "XRP-USD", "KXDOGE": "DOGE-USD"}


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def spot_product(series: str) -> str | None:
    for prefix, product in SPOT_PRODUCTS.items():
        if series.upper().startswith(prefix):
            return product
    return None


class Bot:
    def __init__(self, settings: Settings, client: KalshiClient, broker, ledger: Ledger,
                 state: State, clock=None, spot_fn=None):
        self.s = settings
        self.client = client
        self.broker = broker
        self.ledger = ledger
        self.state = state
        self.clock = clock or RealClock()
        self.spot_fn = spot_fn or (lambda product: coinbase_spot(client.session, product))
        self.plays = {p.name: p for p in settings.plays}
        self.history = defaultdict(lambda: deque(maxlen=2000))
        self.multipliers: dict[str, Decimal] = {}
        self.last_reason: dict[str, str] = {}
        self.last_settle_check: dict[str, datetime] = {}
        self.fail_streak = 0
        self.stop_file = os.path.join(ledger.folder, "STOP")  # same folder as the trade log

    # ---- setup ------------------------------------------------------------
    def load_fees(self) -> None:
        for series in self.s.markets:
            try:
                info = self.client.series(series)
                mult = Decimal(str(info.get("fee_multiplier", 1) or 1))
                log.info("%s fee type %s, multiplier %s", series, info.get("fee_type"), mult)
            except Exception as e:  # keep going with the standard fee
                mult = Decimal("1")
                log.warning("could not read %s fee info (%s); using the standard fee", series, e)
            self.multipliers[series] = mult

    # ---- one pass ----------------------------------------------------------
    def step(self) -> float:
        now = self.clock.now()
        self._roll_day(now)
        quotes = self._fetch_quotes(now)
        for pos in list(self.state.open_positions()):
            q = quotes.get(pos.ticker)
            if q is not None:
                self._maybe_exit(pos, q)
        reason = self._entry_block_reason()
        if reason is None:
            spots = self._spots(quotes, now)
            for q in quotes.values():
                for play in self.s.plays:
                    if play.enabled:
                        self._maybe_enter(play, q, now, spots.get(q.series))
        else:
            self._say("block", reason)
        self._settle(now)
        self._prune(quotes)
        self.ledger.save_state(self.state)
        return self._next_wait(quotes, now)

    def _prune(self, quotes: dict) -> None:
        """Forget bookkeeping for markets that have closed, so files stay small."""
        if not quotes:
            return  # a failed price check is not a reason to forget anything
        live = set(quotes) | {p.ticker for p in self.state.open_positions()}
        self.state.attempts = {k: v for k, v in self.state.attempts.items() if k.split("|", 1)[-1] in live}
        self.last_reason = {k: v for k, v in self.last_reason.items()
                            if "|" not in k or k.split("|", 1)[-1] in live}
        self.last_settle_check = {k: v for k, v in self.last_settle_check.items() if k in live}

    def run_forever(self, should_stop=None) -> None:
        """Run until should_stop() says so (used to restart when the settings change)."""
        self.load_fees()
        log.info("bot started in %s mode, watching %s", self.s.mode.upper(), ", ".join(self.s.markets))
        while not (should_stop and should_stop()):
            try:
                wait = self.step()
            except Exception:
                log.exception("unexpected problem; trying again shortly")
                wait = self.s.slow_seconds
            self.clock.sleep(wait)

    # ---- pieces --------------------------------------------------------------
    def _fetch_quotes(self, now: datetime) -> dict:
        quotes = {}
        for series in self.s.markets:
            try:
                for m in self.client.open_markets(series):
                    q = rules.Quote.from_market(m, series)
                    if q.ticker:
                        quotes[q.ticker] = q
                        mid = q.yes_mid()
                        if mid is not None:
                            self.history[q.ticker].append((now, mid))
                self.fail_streak = 0
            except Exception as e:
                self.fail_streak += 1
                if self.fail_streak in (1, 10) or self.fail_streak % 100 == 0:
                    log.warning("could not get %s prices (%s); try %d", series, e, self.fail_streak)
        cutoff = now - timedelta(seconds=HISTORY_SECONDS)
        for ticker in list(self.history):
            h = self.history[ticker]
            while h and h[0][0] < cutoff:
                h.popleft()
            if not h:
                del self.history[ticker]
        return quotes

    def _spots(self, quotes: dict, now: datetime) -> dict:
        """Spot prices, fetched only when a play that needs them is in its window."""
        wanted = set()
        for q in quotes.values():
            for p in self.s.plays:
                if p.enabled and p.conditions.btc_distance_min_dollars is not None \
                        and rules.in_window(p, q.minutes_left(now)):
                    wanted.add(q.series)
        out = {}
        for series in wanted:
            product = spot_product(series)
            out[series] = self.spot_fn(product) if product else None
        return out

    def _roll_day(self, now: datetime) -> None:
        day = now.astimezone(ZoneInfo(self.s.day_timezone)).date().isoformat()
        if day == self.state.day:
            return
        try:
            bal = self.broker.balance()
        except Exception as e:
            log.warning("could not read balance for the new day (%s)", e)
            return
        open_cost = sum((p.cost for p in self.state.open_positions()), Decimal("0"))
        self.state.day = day
        self.state.day_start_balance = str(bal + open_cost)
        self.state.day_profit = "0"
        self.state.day_trades = 0
        log.info("new day %s: starting balance $%.2f", day, bal + open_cost)

    def _entry_block_reason(self) -> str | None:
        if os.path.exists(self.stop_file):
            return f"STOP file found, so no new trades (delete {self.stop_file} to resume)"
        limit = rules.daily_loss_limit(self.s, Decimal(self.state.day_start_balance))
        if limit > 0 and Decimal(self.state.day_profit) <= -limit:
            return f"daily loss limit hit (${-Decimal(self.state.day_profit):.2f} lost today); done until tomorrow"
        if self.s.max_trades_per_day and self.state.day_trades >= self.s.max_trades_per_day:
            return f"reached {self.s.max_trades_per_day} trades today; done until tomorrow"
        return None

    def _maybe_enter(self, play, q: rules.Quote, now: datetime, spot) -> None:
        key = f"{play.name}|{q.ticker}"
        if key in self.state.entered or self.state.attempts.get(key, 0) >= MAX_ORDER_TRIES:
            return
        chk = rules.check_entry(play, q, now, self.history.get(q.ticker), spot)
        if not chk.ok:
            if chk.reason != "outside time window":
                self._say(key, f"{play.name}: skip {q.ticker}: {chk.reason}")
            return
        try:
            balance = self.broker.balance()
        except Exception as e:
            log.warning("could not read balance (%s); not trading this second", e)
            return
        open_cost = sum((p.cost for p in self.state.open_positions()), Decimal("0"))
        n = rules.size_contracts(self.s, balance, chk.limit, open_cost)
        if n <= 0:
            self._say(key, f"{play.name}: skip {q.ticker}: trade size works out to 0 contracts "
                           f"(balance ${balance:.2f}, price {chk.limit}, share and size limits)")
            return
        self.state.attempts[key] = self.state.attempts.get(key, 0) + 1
        mult = self.multipliers.get(q.series, Decimal("1"))
        try:
            fill = self.broker.buy(q, chk.side, n, chk.limit, mult)
        except KalshiError as e:
            log.error("%s: buy order rejected: %s", play.name, e)
            return
        except Exception as e:
            if not self.broker.live:
                raise
            # The order may or may not have reached Kalshi. Never resend it; ask Kalshi what we hold.
            log.error("%s: lost contact while buying in %s (%s); not retrying this market", play.name, q.ticker, e)
            fill = self._recover_buy(q, chk.side, chk.limit, mult)
            if fill is None:
                self.state.entered.append(key)
                self.ledger.save_state(self.state)
                return
        if fill is None:
            self._say(key, f"{play.name}: {q.ticker}: price moved before the order filled; will retry")
            return
        pos = Position(play.name, q.ticker, q.series, chk.side, str(fill.contracts), str(fill.price),
                       str(fill.fee), now.isoformat(), q.close_time.isoformat() if q.close_time else "")
        positions = self.state.open_positions() + [pos]
        self.state.set_positions(positions)
        self.state.entered.append(key)
        self.state.entered = self.state.entered[-500:]
        self.state.day_trades += 1
        if not self.broker.live:
            self.state.paper_cash = str(self.broker.cash)
        log.info("%s: BOUGHT %s %s x%s at %s (fee $%s), %.1f min left", play.name, q.ticker,
                 rules.side_name(chk.side), fill.contracts, fill.price, fill.fee, q.minutes_left(now))
        if self.broker.live:
            try:
                log.info("Kalshi now shows position %s in %s (+ = Up, - = Down)",
                         self.client.position(q.ticker), q.ticker)
            except Exception as e:
                log.warning("could not double-check the position (%s)", e)
        self.ledger.save_state(self.state)

    def _maybe_exit(self, pos: Position, q: rules.Quote) -> None:
        play = self.plays.get(pos.play)
        if play is None:
            return
        signal = rules.exit_signal(play, pos.side, q)
        if signal is None:
            return
        kind, bid = signal
        mult = self.multipliers.get(pos.series, Decimal("1"))
        try:
            fill = self.broker.sell(q, pos.side, pos.d("contracts"), bid, mult)
        except KalshiError as e:
            log.error("%s: sell order rejected: %s", pos.play, e)
            return
        except Exception as e:
            if not self.broker.live:
                raise
            log.error("%s: lost contact while selling %s (%s); checking what Kalshi shows", pos.play, pos.ticker, e)
            fill = self._recover_sell(pos, bid, mult)
        if fill is None:
            return
        self._close(pos, fill.contracts, kind, fill, None)

    # ---- live only: work out what happened when a reply got lost ------------
    def _held_on_kalshi(self, ticker: str, side: str) -> Decimal | None:
        try:
            pos = self.client.position(ticker)  # + = Up contracts, - = Down contracts
        except Exception as e:
            log.error("could not check Kalshi for %s (%s); check the Kalshi app", ticker, e)
            return None
        return max(pos, Decimal("0")) if side == "yes" else max(-pos, Decimal("0"))

    def _recorded(self, ticker: str, side: str) -> Decimal:
        return sum((p.d("contracts") for p in self.state.open_positions()
                    if p.ticker == ticker and p.side == side), Decimal("0"))

    def _recover_buy(self, q, side: str, limit: Decimal, mult: Decimal) -> Fill | None:
        held = self._held_on_kalshi(q.ticker, side)
        if held is None:
            return None
        new = held - self._recorded(q.ticker, side)
        if new <= 0:
            log.info("Kalshi shows the order did not fill")
            return None
        log.warning("Kalshi shows %s new contracts; logging them at the limit price %s (estimate)", new, limit)
        return Fill(new, limit, rules.taker_fee(new, limit, mult))

    def _recover_sell(self, pos: Position, price: Decimal, mult: Decimal) -> Fill | None:
        held = self._held_on_kalshi(pos.ticker, pos.side)
        if held is None:
            return None
        sold = min(self._recorded(pos.ticker, pos.side) - held, pos.d("contracts"))
        if sold <= 0:
            log.info("Kalshi shows the sale did not happen; will try again")
            return None
        log.warning("Kalshi shows %s contracts were sold; logging them at %s (estimate)", sold, price)
        return Fill(sold, price, rules.taker_fee(sold, price, mult))

    def _settle(self, now: datetime) -> None:
        for pos in self.state.open_positions():
            close = rules.parse_time(pos.close_time)
            if close is None or now < close + timedelta(seconds=SETTLE_DELAY):
                continue
            last = self.last_settle_check.get(pos.ticker)
            if last and (now - last).total_seconds() < SETTLE_RECHECK:
                continue
            self.last_settle_check[pos.ticker] = now
            try:
                m = self.client.market(pos.ticker)
            except Exception as e:
                log.warning("could not check result for %s (%s)", pos.ticker, e)
                continue
            result = m.get("result")
            if result in ("yes", "no"):
                payout = pos.d("contracts") if result == pos.side else Decimal("0")
                self.broker.collect(payout)
                self._close(pos, pos.d("contracts"), "settled", None, result)
            elif now - close > timedelta(hours=2):
                self._say(f"late|{pos.ticker}", f"{pos.ticker} still has no result 2 hours after closing")

    def _close(self, pos: Position, contracts: Decimal, kind: str, fill: Fill | None, result: str | None) -> None:
        share = contracts / pos.d("contracts")
        entry_fee = pos.d("entry_fee") * share
        cost = contracts * pos.d("entry_price")
        if fill is not None:  # sold early
            payout = fill.contracts * fill.price
            exit_fee = fill.fee
            exit_price = fill.price
        else:  # held to the end: $1 per winning contract
            payout = contracts if result == pos.side else Decimal("0")
            exit_fee = Decimal("0")
            exit_price = Decimal("1") if result == pos.side else Decimal("0")
        profit = payout - cost - entry_fee - exit_fee
        now = self.clock.now()
        self.ledger.record_trade({
            "opened_at": pos.opened_at, "closed_at": now.isoformat(), "mode": self.s.mode,
            "play": pos.play, "market": pos.ticker, "side": rules.side_name(pos.side),
            "contracts": str(contracts), "entry_price": pos.entry_price,
            "entry_fee": f"{entry_fee:.4f}", "exit_type": kind, "exit_price": str(exit_price),
            "exit_fee": f"{exit_fee:.4f}", "result": rules.side_name(result) if result else "",
            "payout": f"{payout:.4f}", "profit": f"{profit:.4f}",
        })
        self.state.day_profit = str(Decimal(self.state.day_profit) + profit)
        remaining = []
        for p in self.state.open_positions():
            if p == pos:
                left = p.d("contracts") - contracts
                if left > 0:
                    p.contracts = str(left)
                    p.entry_fee = str(p.d("entry_fee") - entry_fee)
                    remaining.append(p)
            else:
                remaining.append(p)
        self.state.set_positions(remaining)
        if not self.broker.live:
            self.state.paper_cash = str(self.broker.cash)
        log.info("%s: CLOSED %s %s x%s by %s, profit $%+.2f (today $%+.2f)", pos.play, pos.ticker,
                 rules.side_name(pos.side), contracts, kind, profit, Decimal(self.state.day_profit))
        self.ledger.save_state(self.state)

    def _next_wait(self, quotes: dict, now: datetime) -> float:
        """Check every second when something could happen soon; otherwise rest."""
        ahead = Decimal(str(self.s.slow_seconds)) / 60 + Decimal("0.1")
        for q in quotes.values():
            mins = q.minutes_left(now)
            if mins is None:
                continue
            for p in self.s.plays:
                if p.enabled and p.act_until_minutes_left <= mins <= p.act_from_minutes_left + ahead:
                    return self.s.fast_seconds
        for pos in self.state.open_positions():
            play = self.plays.get(pos.play)
            if play is not None and play.has_exits and pos.ticker in quotes:
                return self.s.fast_seconds
        if any(rules.parse_time(p.close_time) and now >= rules.parse_time(p.close_time)
               for p in self.state.open_positions()):
            return max(self.s.fast_seconds, 5.0)
        return self.s.slow_seconds

    def _say(self, key: str, message: str) -> None:
        """Log a message only when it changes, so the log stays readable."""
        if self.last_reason.get(key) != message:
            self.last_reason[key] = message
            log.info(message)
