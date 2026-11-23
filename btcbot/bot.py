"""The main loop: watch prices, buy when a play's rules say so, sell or settle."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import goal as G
from . import rules
from .broker import Fill
from .kalshi import KalshiClient, KalshiError, coinbase_spot
from .ledger import Ledger, Position, State
from .settings import Settings


COINS = {"KXBTC": "Bitcoin", "KXETH": "Ethereum", "KXSOL": "Solana", "KXXRP": "XRP", "KXDOGE": "Dogecoin",
         "KXBNB": "BNB", "KXHYPE": "HYPE", "KXZEC": "Zcash"}


def cents_words(price) -> str:
    return f"{Decimal(price) * 100:.1f}".rstrip("0").rstrip(".") + "¢"


def time_left_words(q: rules.Quote, now: datetime) -> str:
    if q.close_time is None:
        return "unknown time left"
    secs = max(0, int((q.close_time - now).total_seconds()))
    return f"{secs // 60}:{secs % 60:02d} left"


def kalshi_words(e: Exception) -> str:
    """Kalshi's own error message, short."""
    text = getattr(e, "text", "") or str(e)
    try:
        err = json.loads(text).get("error", {})
        msg = " - ".join(x for x in (err.get("code"), err.get("message"), err.get("details")) if x)
        if msg:
            return f"{getattr(e, 'status', '')} {msg}".strip()[:200]
    except (ValueError, AttributeError):
        pass
    return str(e)[:200]


def account_name(n: int) -> str:
    return "your main account" if int(n) == 0 else f"subaccount {n}"

PAPER_RESET_FILE = "PAPER_RESET"   # the website writes the new pretend balance here
TRANSFER_FILE = "TRANSFER_REQUEST"  # the website asks to move money between subaccounts
SETUP_FILE = "SETUP_REQUEST"        # the website asks to set up subaccounts again now
ALLOCATION_FILE = "ALLOCATION_REQUEST"  # the website asks Kalshi to keep money in the crypto pot
POTS = {0: "general", 1: "combos", 2: "crypto", 3: "sports"}


def pot_name(shard) -> str:
    return POTS.get(shard, f"#{shard}") if shard is not None else "main"
CHECK_SECONDS = 10                  # real money: compare our records with Kalshi this often
ACCOUNTS_SECONDS = 30               # real money: refresh subaccount cash for the website
SETUP_RETRY_SECONDS = 600           # real money: try setting up missing subaccounts again this often
CLOSE_MARGIN = 30                   # records check skips markets this close to (or past) their close
MISMATCH_CHECKS = 2                 # pause only if records and Kalshi disagree this many times in a row


def plain(n) -> str:
    """10 -> "10", 2.50 -> "2.5" (never "1E+1")."""
    return format(Decimal(n).normalize(), "f")

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
        self.ready_accounts = {0}
        self.last_setup = None
        self._history_cache: tuple = (None, [])
        self.mismatch_streak = 0
        self.last_check = None
        self.last_accounts = None

    # ---- real money: subaccounts ------------------------------------------
    def prepare_live(self) -> None:
        """Make sure every play's Kalshi subaccount exists, making any that are missing.

        Making subaccounts needs Kalshi's Advanced API level, which Kalshi grants for
        free once one of your last 100 orders came from the API (the bot's first bet).
        """
        needed = sorted({p.subaccount for p in self.s.plays if p.enabled})
        self.broker.accounts = sorted(set(needed) | {0})
        self.ready_accounts = {0}
        self.last_setup = self.clock.now()
        upgrade_refused = False
        problem = ""
        if needed == [0]:
            self.state.setup_note = ""
            return
        try:
            have = set(self.client.subaccount_balances()) | {0}
        except Exception as e:
            log.error("could not list your Kalshi subaccounts (%s)", e)
            have = {0}
        missing = [n for n in needed if n not in have]
        if missing:
            try:
                level = self.client.api_level()
            except Exception as e:
                log.warning("could not read your Kalshi API level (%s)", e)
                level = ""
            if level in ("", "basic"):
                try:
                    self.client.upgrade_api_level()
                    log.info("Kalshi API level upgraded to Advanced (needed for subaccounts)")
                except KalshiError as e:
                    upgrade_refused = True
                    problem = f"Kalshi said: {kalshi_words(e)}"
                    log.error("Kalshi would not upgrade the API level (%s). It needs one bet placed by the "
                              "bot first: run a single play on subaccount 0 for one bet, then try again.", e)
            tries = 0
            while missing and max(missing) not in have and tries < 63:
                tries += 1
                try:
                    n = self.client.create_subaccount()
                except Exception as e:
                    log.error("could not make a Kalshi subaccount (%s)", e)
                    problem = problem or f"Making it failed. Kalshi said: {kalshi_words(e)}"
                    break
                have.add(n)
                log.info("made Kalshi subaccount %s", n)
        self.ready_accounts = {n for n in needed if n in have} | {0}
        not_ready = [n for n in needed if n not in have]
        if not_ready:
            log.error("Kalshi subaccount(s) %s are not set up, so plays using them won't bet",
                      ", ".join(map(str, not_ready)))
            names = ", ".join(f"{account_name(n)} ({', '.join(p.name for p in self.s.plays if p.enabled and p.subaccount == n)})"
                              for n in not_ready)
            self.state.setup_note = (
                f"Not set up on Kalshi yet: {names}. " +
                ("Kalshi makes subaccounts only after the bot has placed one real bet from your main account. "
                 "Once a play on your main account has bet, the bot sets them up by itself within 10 minutes."
                 if upgrade_refused else "The bot tries again every 10 minutes.")
                + (f" ({problem})" if problem else ""))
        else:
            self.state.setup_note = ""
            self.last_accounts = None   # show the new subaccounts soon
        log.info("plays use Kalshi subaccounts: %s", ", ".join(
            f"{p.name}={p.subaccount}" for p in self.s.plays if p.enabled))

    def _apply_allocation_request(self) -> None:
        path = os.path.join(self.ledger.folder, ALLOCATION_FILE)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, ValueError):
            req = {}
        try:
            os.remove(path)
        except OSError:
            pass
        shard = req.get("shard")
        try:
            if shard is None:
                self.client.set_target_allocation({})
                self.state.notice = "Kalshi no longer moves your money between pots by itself."
            else:
                self.client.set_target_allocation({int(shard): 100})
                self.state.notice = (f"Kalshi will keep your money in its {pot_name(int(shard))} pot "
                                     "(it checks every 10 seconds). Your own bets in the app still work.")
            log.info(self.state.notice)
            self.last_accounts = None
        except Exception as e:
            self.state.notice = f"Kalshi didn't accept the money-pot setting: {kalshi_words(e)}"
            log.error(self.state.notice)
        self.ledger.save_state(self.state)

    def _retry_setup(self, now: datetime) -> None:
        asked = os.path.join(self.ledger.folder, SETUP_FILE)
        if os.path.exists(asked):
            try:
                os.remove(asked)
            except OSError:
                pass
            log.info("setting up Kalshi subaccounts again (asked from the website)")
            self.prepare_live()
            return
        missing = any(p.enabled and p.subaccount not in self.ready_accounts for p in self.s.plays)
        if missing and self.last_setup and (now - self.last_setup).total_seconds() >= SETUP_RETRY_SECONDS:
            log.info("trying again to set up Kalshi subaccounts")
            self.prepare_live()

    def market_words(self, q: rules.Quote) -> str:
        """KXBTC15M-26NOV221845-45 -> "Bitcoin 1:45 pm" (the close, in your time zone)."""
        coin = next((name for prefix, name in COINS.items() if q.series.upper().startswith(prefix)), q.series)
        if q.close_time is None:
            return coin
        t = q.close_time.astimezone(ZoneInfo(self.s.day_timezone))
        return f"{coin} {t.strftime('%I:%M %p').lstrip('0').lower()}"

    def _miss_reason(self, q: rules.Quote, side: str, limit: Decimal) -> str:
        """Why an order that fills-now-or-cancels didn't fill, from the prices right after it."""
        if self.broker.live:
            try:
                after = rules.Quote.from_market(self.client.market(q.ticker), q.series)
            except Exception:
                return "Kalshi couldn't be asked why"
            when = "right after"
        else:
            after, when = q, "at that moment"
        ask, size = after.ask(side), after.ask_size(side)
        if ask is None or size == 0:
            return f"nobody was selling {rules.side_name(side)} {when}"
        if ask > limit:
            return (f"the price had moved to {cents_words(ask)} {when} (it offered up to {cents_words(limit)}; "
                    f"raise \"Extra cents\" to chase moves)")
        if not self.broker.live:
            return "not enough pretend money"
        return (f"the price looks fine now ({cents_words(ask)}), so the sellers at that price were taken a "
                f"moment before the order arrived")

    def _account(self, play) -> int:
        return play.subaccount if self.broker.live else 0

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
        self._apply_paper_reset()
        self._clear_notes_if_resumed()
        if self.broker.live:
            self._retry_setup(now)
            self._apply_allocation_request()
            self._apply_transfer_request()
            self._refresh_accounts(now)
        self._goal_check()   # before any new bet, so the goal pauses betting in time
        quotes = self._fetch_quotes(now)
        self._share_prices(quotes, now)
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
        self.check_records(quotes, now)
        self._goal_check()   # and again once bets have settled
        self._prune(quotes)
        self.ledger.save_state(self.state)
        return self._next_wait(quotes, now)

    def _share_prices(self, quotes: dict, now: datetime) -> None:
        """Save the latest prices for the website (best effort; never stops trading)."""
        if not quotes:
            return
        rows = [q.as_row() for q in quotes.values()]
        try:
            self.ledger.save_market(now, rows)
        except OSError as e:
            self._say("market-file", f"could not save prices for the website ({e})")

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

    # ---- real money: safety check, money moves, balances --------------------
    def check_records(self, quotes: dict, now: datetime) -> None:
        """Compare our open bets with what Kalshi holds, per subaccount, in markets still open.

        If they disagree twice in a row, pause new bets (STOP file) and say why.
        Closed markets are skipped: Kalshi may take a moment to settle them.
        """
        if not self.broker.live or (self.last_check and (now - self.last_check).total_seconds() < CHECK_SECONDS):
            return
        self.last_check = now
        problems = []
        for acct in getattr(self.broker, "accounts", [0]):
            try:
                held = self.client.positions(acct)
            except Exception as e:
                log.warning("could not read Kalshi positions for subaccount %s (%s)", acct, e)
                return
            expected: dict = {}
            for p in self.state.open_positions():
                if p.subaccount == acct:
                    n = p.d("contracts") if p.side == "yes" else -p.d("contracts")
                    expected[p.ticker] = expected.get(p.ticker, Decimal("0")) + n
            for ticker in set(held) | set(expected):
                q = quotes.get(ticker)
                # Only markets with time left: around the close Kalshi pays out before the bot
                # records the result, and that's not a real disagreement.
                if q is None or q.close_time is None or (q.close_time - now).total_seconds() < CLOSE_MARGIN:
                    continue
                k, e = held.get(ticker, Decimal("0")), expected.get(ticker, Decimal("0"))
                if k != e:
                    problems.append(f"{ticker} in subaccount {acct}: Kalshi shows {rules.position_words(k)}, "
                                    f"the bot's records show {rules.position_words(e)}")
        if not problems:
            self.mismatch_streak = 0
            return
        self.mismatch_streak += 1
        log.warning("records and Kalshi disagree (check %d of %d): %s", self.mismatch_streak,
                    MISMATCH_CHECKS, "; ".join(problems))
        if self.mismatch_streak >= MISMATCH_CHECKS and not self.state.alert:
            text = ("Paused: the bot's records don't match your Kalshi account. " + "; ".join(problems)
                    + ". Check the Kalshi app, then tap Resume.")
            with open(self.stop_file, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            self.state.alert = text
            self.ledger.save_state(self.state)
            log.error(text)

    def _clear_notes_if_resumed(self) -> None:
        if (self.state.alert or self.state.goal_note) and not os.path.exists(self.stop_file):
            log.info("resumed; watching again")
            self.state.alert = ""
            self.state.goal_note = ""
            self.mismatch_streak = 0

    # ---- goal mode ----------------------------------------------------------
    def _history(self) -> list:
        """All finished bets (pretend and real), re-read only when a trade log changes."""
        stamp = []
        for path in G.history_paths(self.s, self.ledger.folder):
            try:
                st = os.stat(path)
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        if self._history_cache[0] != stamp:
            self._history_cache = (stamp, G.history(self.s, self.ledger.folder))
        return self._history_cache[1]

    def _goal_accounts(self) -> set:
        return {p.subaccount for p in self.s.plays if p.enabled}

    def _equity(self) -> Decimal | None:
        """Money the plays have: cash plus what open bets cost. Real money counts only the plays' subaccounts."""
        opened = self.state.open_positions()
        if not self.broker.live:
            return self.broker.cash + sum((p.cost for p in opened), Decimal("0"))
        if not self.state.accounts:
            return None
        accts = self._goal_accounts()
        cash = sum((Decimal(v) for k, v in self.state.accounts.items() if int(k) in accts), Decimal("0"))
        return cash + sum((p.cost for p in opened if p.subaccount in accts), Decimal("0"))

    def _goal_contracts(self, play, acct: int, balance: Decimal, price: Decimal) -> Decimal:
        g = self.s.goal
        e = G.edge([r for r in self._history() if r["play"] == play.name])
        share = G.bet_share(e, g.boldness, g.max_bet_pct)
        opened = self.state.open_positions()
        if self.broker.live:  # each play grows its own subaccount
            money = balance + sum((p.cost for p in opened if p.subaccount == acct), Decimal("0"))
        else:                 # pretend money is one pot, shared evenly by the plays that are on
            plays_on = max(1, sum(1 for p in self.s.plays if p.enabled))
            money = (self.broker.cash + sum((p.cost for p in opened), Decimal("0"))) / plays_on
        return G.contracts_for(money, share, price, g.max_bet_pct)

    def _goal_check(self) -> None:
        g = self.s.goal
        if not g.enabled:
            if self.state.goal_start or self.state.goal_note:
                self.state.goal_start, self.state.goal_note = "", ""
            return
        equity = self._equity()
        if equity is None:
            return
        if not self.state.goal_start:
            self.state.goal_start = str(equity.quantize(Decimal("0.01")))
            log.info("goal mode on: $%.2f now, aiming for $%.2f", equity, g.target)
        if self.state.goal_note:
            return
        if equity >= g.target:
            note = (f"Goal reached: ${equity:.2f} (started at ${Decimal(self.state.goal_start):.2f}). "
                    "New bets are paused. To keep going, raise the target, then tap Resume.")
        elif g.stop_below > 0 and equity < g.stop_below:
            note = (f"Stopped: the money fell to ${equity:.2f}, below your ${g.stop_below:.2f} stop. "
                    "New bets are paused.")
        else:
            return
        with open(self.stop_file, "w", encoding="utf-8") as fh:
            fh.write(note + "\n")
        self.state.goal_note = note
        log.warning(note)

    def _apply_transfer_request(self) -> None:
        path = os.path.join(self.ledger.folder, TRANSFER_FILE)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                req = json.load(fh)
            os.remove(path)
            frm, to, cents = int(req["from"]), int(req["to"]), int(req["amount_cents"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.state.notice = f"Money move ignored: the request was unreadable ({e})"
            log.warning(self.state.notice)
            try:
                os.remove(path)
            except OSError:
                pass
            return
        try:
            self.client.transfer(frm, to, cents, req.get("id"))
            self.state.notice = f"Moved ${cents / 100:.2f} from {account_name(frm)} to {account_name(to)}."
            log.info(self.state.notice)
            self.last_accounts = None   # show the new amounts soon
            if hasattr(self.broker, "_cache"):
                self.broker._cache = (0.0, {})
        except Exception as e:
            self.state.notice = f"Money move failed: {e}"
            log.error(self.state.notice)
        self.ledger.save_state(self.state)

    def _refresh_accounts(self, now: datetime) -> None:
        if self.last_accounts and (now - self.last_accounts).total_seconds() < ACCOUNTS_SECONDS:
            return
        self.last_accounts = now
        try:
            if getattr(self.broker, "accounts", [0]) == [0] and hasattr(self.client, "balance_summary"):
                total, pots = self.client.balance_summary()
                cash = {0: total}
                self.state.shards = {"0": {str(k): str(v) for k, v in sorted(pots.items())}}
            elif hasattr(self.client, "subaccount_shards"):
                split = self.client.subaccount_shards()
                cash = {n: sum(p.values(), Decimal("0")) for n, p in split.items()}
                self.state.shards = {str(n): {str(k): str(v) for k, v in sorted(p.items())}
                                     for n, p in sorted(split.items())}
            elif hasattr(self.broker, "balances"):
                cash = self.broker.balances()          # shares the bot's remembered balance
            elif getattr(self.broker, "accounts", [0]) == [0]:
                cash = {0: self.client.balance_dollars()}
            else:
                cash = self.client.subaccount_balances()
        except Exception as e:
            log.warning("could not read Kalshi balances (%s)", e)
            self.last_accounts = now - timedelta(seconds=ACCOUNTS_SECONDS - 5)   # try again in 5 seconds
            return
        self.state.accounts = {str(k): str(v) for k, v in sorted(cash.items())}

    def _apply_paper_reset(self) -> None:
        """Paper mode only: set the pretend money to the amount the website asked for.

        Past bets and open bets are kept; today's loss count starts over.
        """
        path = os.path.join(self.ledger.folder, PAPER_RESET_FILE)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                amount = Decimal(fh.read().strip())
        except (OSError, ArithmeticError):
            amount = None
        try:
            os.remove(path)
        except OSError:
            pass
        if self.broker.live:
            log.warning("ignored a pretend-money reset: the bot is using real money")
            return
        if amount is None or amount < 0:
            log.warning("ignored a pretend-money reset: the amount was not a number")
            return
        open_cost = sum((p.cost for p in self.state.open_positions()), Decimal("0"))
        self.broker.cash = amount
        self.state.paper_cash = str(amount)
        self.state.day_start_balance = str(amount + open_cost)
        self.state.day_profit = "0"
        self.ledger.save_state(self.state)
        log.info("pretend money reset to $%.2f; past bets are kept", amount)

    def _entry_block_reason(self) -> str | None:
        if self.state.alert:
            return "paused by the records check; see the website"
        if os.path.exists(self.stop_file):
            try:
                with open(self.stop_file, encoding="utf-8") as fh:
                    why = fh.read().strip().splitlines()[0][:200]
            except (OSError, IndexError):
                why = ""
            return "paused, so no new bets (tap Resume on the website)" + (f": {why}" if why else "")
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
        acct = self._account(play)
        if self.broker.live and acct not in self.ready_accounts:
            self._say(key, f"{play.name}: skip {q.ticker}: its Kalshi subaccount {acct} isn't set up")
            return
        shard = q.exchange_index if self.broker.live else None
        try:
            balance = self.broker.balance(acct, shard)
        except Exception as e:
            log.warning("could not read balance (%s); not trading this second", e)
            return
        open_cost = sum((p.cost for p in self.state.open_positions()
                         if not self.broker.live or p.subaccount == acct), Decimal("0"))
        wanted = self._goal_contracts(play, acct, balance, chk.limit) if self.s.goal.enabled else None
        n = rules.size_contracts(self.s, balance, chk.limit, open_cost, play, wanted)
        if n <= 0:
            if shard is not None and balance < chk.limit:
                self._say(key, f"{play.name}: skip {self.market_words(q)}: only ${balance:.2f} in Kalshi's "
                               f"{pot_name(shard)} money pot, not enough for 1 contract at {cents_words(chk.limit)}. "
                               "Kalshi keeps money in separate pots and doesn't move it for bots; tap "
                               "\"Keep my money in the crypto pot\" in the Kalshi money box on the website.")
            else:
                self._say(key, f"{play.name}: skip {q.ticker}: trade size works out to 0 contracts "
                               f"(balance ${balance:.2f}, price {chk.limit}, share and size limits)")
            return
        self.state.attempts[key] = self.state.attempts.get(key, 0) + 1
        mult = self.multipliers.get(q.series, Decimal("1"))
        try:
            fill = self.broker.buy(q, chk.side, n, chk.limit, mult, account=acct)
        except KalshiError as e:
            if "insufficient_balance" in (e.text or ""):
                # Not marked done: if Kalshi refills the crypto pot, a later try (up to the limit) can still buy.
                log.warning("%s: %s %s not bought: Kalshi says the %s money pot doesn't have enough for %s "
                            "at %s. Kalshi keeps money in separate pots and doesn't move it for bots; tap "
                            "\"Keep my money in the crypto pot\" in the Kalshi money box on the website.", play.name,
                            self.market_words(q), rules.side_name(chk.side), pot_name(q.exchange_index),
                            f"{n} contracts" if n != 1 else "1 contract", cents_words(chk.limit))
                self.ledger.save_state(self.state)
                self.last_accounts = None   # re-read the pots now
                return
            log.error("%s: buy order rejected: %s", play.name, kalshi_words(e))
            return
        except Exception as e:
            if not self.broker.live:
                raise
            # The order may or may not have reached Kalshi. Never resend it; ask Kalshi what we hold.
            log.error("%s: lost contact while buying in %s (%s); not retrying this market", play.name, q.ticker, e)
            fill = self._recover_buy(q, chk.side, chk.limit, mult, acct)
            if fill is None:
                self.state.entered.append(key)
                self.ledger.save_state(self.state)
                return
        if fill is None and self.broker.live:
            # Double-check with Kalshi: if contracts showed up anyway, record them (never buy twice).
            fill = self._recover_buy(q, chk.side, chk.limit, mult, acct)
            if fill is not None:
                log.warning("%s: Kalshi's reply said 'not filled' but the contracts are there; recorded them",
                            play.name)
        if fill is None:
            tries = self.state.attempts.get(key, 0)
            log.info("%s: %s %s didn't fill: tried to buy at up to %s with %s; %s. %s", play.name,
                     self.market_words(q), rules.side_name(chk.side), cents_words(chk.limit),
                     time_left_words(q, now), self._miss_reason(q, chk.side, chk.limit),
                     f"Will try again ({tries} of {MAX_ORDER_TRIES} tries used)." if tries < MAX_ORDER_TRIES
                     else "That was the last try for this market.")
            return
        pos = Position(play.name, q.ticker, q.series, chk.side, plain(fill.contracts), str(fill.price),
                       str(fill.fee), now.isoformat(), q.close_time.isoformat() if q.close_time else "", acct)
        positions = self.state.open_positions() + [pos]
        self.state.set_positions(positions)
        self.state.entered.append(key)
        self.state.entered = self.state.entered[-500:]
        self.state.day_trades += 1
        if not self.broker.live:
            self.state.paper_cash = str(self.broker.cash)
        log.info("%s: BOUGHT %s %s x%s at %s (fee $%s), %.1f min left", play.name, q.ticker,
                 rules.side_name(chk.side), fill.contracts, fill.price, fill.fee, q.minutes_left(now))
        if self.broker.live and self.state.setup_note and self.last_setup:
            self.last_setup = now - timedelta(seconds=SETUP_RETRY_SECONDS - 30)   # set up subaccounts soon
        if self.broker.live:
            try:
                log.info("Kalshi now shows position %s in %s, subaccount %s (+ = Up, - = Down)",
                         self.client.position(q.ticker, subaccount=acct), q.ticker, acct)
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
            fill = self.broker.sell(q, pos.side, pos.d("contracts"), bid, mult, account=pos.subaccount)
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
    def _held_on_kalshi(self, ticker: str, side: str, account: int = 0) -> Decimal | None:
        try:
            pos = self.client.position(ticker, subaccount=account)  # + = Up contracts, - = Down contracts
        except Exception as e:
            log.error("could not check Kalshi for %s (%s); check the Kalshi app", ticker, e)
            return None
        return max(pos, Decimal("0")) if side == "yes" else max(-pos, Decimal("0"))

    def _recorded(self, ticker: str, side: str, account: int = 0) -> Decimal:
        return sum((p.d("contracts") for p in self.state.open_positions()
                    if p.ticker == ticker and p.side == side and p.subaccount == account), Decimal("0"))

    def _recover_buy(self, q, side: str, limit: Decimal, mult: Decimal, account: int = 0) -> Fill | None:
        held = self._held_on_kalshi(q.ticker, side, account)
        if held is None:
            return None
        new = held - self._recorded(q.ticker, side, account)
        if new <= 0:
            log.info("Kalshi shows the order did not fill")
            return None
        log.warning("Kalshi shows %s new contracts; logging them at the limit price %s (estimate)", new, limit)
        return Fill(new, limit, rules.taker_fee(new, limit, mult))

    def _recover_sell(self, pos: Position, price: Decimal, mult: Decimal) -> Fill | None:
        held = self._held_on_kalshi(pos.ticker, pos.side, pos.subaccount)
        if held is None:
            return None
        sold = min(self._recorded(pos.ticker, pos.side, pos.subaccount) - held, pos.d("contracts"))
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
            "contracts": plain(contracts), "entry_price": pos.entry_price,
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
