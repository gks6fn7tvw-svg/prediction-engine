"""The main loop: watch prices, buy when a play's rules say so, sell or settle."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import goal as G
from . import rules
from .broker import Fill
from . import lastminute
from .kalshi import KalshiClient, KalshiError, coinbase_spot
from .ledger import Ledger, Position, State
from .settings import Settings


COINS = {"KXBTC": "Bitcoin", "KXETH": "Ethereum", "KXSOL": "Solana", "KXXRP": "XRP", "KXDOGE": "Dogecoin",
         "KXBNB": "BNB", "KXHYPE": "HYPE", "KXZEC": "Zcash"}


ONE = Decimal("1")


def signed(d: Decimal) -> str:
    return f"+${d:.2f}" if d >= 0 else f"-${-d:.2f}"


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
POT_MOVE_FILE = "POT_MOVE_REQUEST"      # the website asks to move the main account's cash into the crypto pot
MAKE_SUB_FILE = "MAKE_SUBACCOUNT_REQUEST"  # the website asks for a new subaccount inside the crypto pot
TRADE_FILE = "TRADE_REQUEST"        # the website asks to buy or sell right now (a trade you make yourself)
MANUAL = "manual"                   # the play name trades you make on the website are recorded under
TRADE_MAX_AGE = 15                  # seconds: an older trade request is dropped (prices have moved)
WAKE_FILES = ("TRADE_REQUEST", "TRANSFER_REQUEST", "POT_MOVE_REQUEST", "ALLOCATION_REQUEST",
              "MAKE_SUBACCOUNT_REQUEST", "SETUP_REQUEST")
POTS = {0: "general", 1: "combos", 2: "crypto", 3: "sports"}
CRYPTO_POT = 2                      # the money pot Kalshi uses for crypto markets
DEFEND_FROM = Decimal("0.5")        # guard/crowd watch only bets bought as the favorite (cheap bets start "losing")


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


class PretendTag(logging.Filter):
    """Marks every log line from the pretend-money bot, so the activity never mixes it up with real bets."""
    PREFIX = "pretend money · "

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith(self.PREFIX):
            record.msg = self.PREFIX + str(record.msg)
        return True


def run_together(real: "Bot", fake: "Bot", should_stop=None) -> None:
    """Real money and pretend-only plays side by side: one loop, two separate sets of books."""
    real.load_fees()
    fake.load_fees()
    log.info("bot started in %s mode, watching %s", real.s.mode.upper(), ", ".join(real.s.markets))
    log.info("pretend money only: %s", ", ".join(p.name for p in fake.s.plays if p.enabled))
    tag = PretendTag()
    while not (should_stop and should_stop()):
        waits = []
        try:
            waits.append(real.step())
        except Exception:
            log.exception("unexpected problem; trying again shortly")
            waits.append(real.s.slow_seconds)
        log.addFilter(tag)
        try:
            waits.append(fake.step())
        except Exception:
            log.exception("unexpected problem; trying again shortly")
            waits.append(fake.s.slow_seconds)
        finally:
            log.removeFilter(tag)
        real.nap(min(waits))


class Bot:
    def __init__(self, settings: Settings, client: KalshiClient, broker, ledger: Ledger,
                 state: State, clock=None, spot_fn=None, index_fn=None, sigma_fn=None, jump_fn=None):
        self.s = settings
        self.client = client
        self.broker = broker
        self.ledger = ledger
        self.state = state
        self.clock = clock or RealClock()
        self.spot_fn = spot_fn or (lambda product: coinbase_spot(client.session, product))
        # Last-minute watch: a stand-in for Kalshi's settlement index (median of exchanges).
        self.index_fn = index_fn or (lambda coin: lastminute.index_proxy(client.session, coin))
        self.trackers: dict[str, lastminute.Tracker] = {}
        # Live score: how jumpy the coin has been ($ per second), refreshed once a minute.
        self.sigma_fn = sigma_fn or (lambda coin: lastminute.candles_sigma(client.session, coin))
        # Jumpiness gauge for the card (1-100 vs the last few hours), refreshed every 20 seconds.
        self.jump_fn = jump_fn or (lambda coin: lastminute.candles_jumpiness(client.session, coin))
        self.jumps: dict = {}
        self.sigmas: dict = {}
        self.scores: dict = {}
        self.btc_now: dict = {}                    # ticker -> latest coin price (for the card)
        self.estimates: dict[str, lastminute.Estimate] = {}
        self.plays = {p.name: p for p in settings.plays}
        self.history = defaultdict(lambda: deque(maxlen=2000))
        self.multipliers: dict[str, Decimal] = {}
        self.last_reason: dict[str, str] = {}
        self.last_settle_check: dict[str, datetime] = {}
        self.fail_streak = 0
        self.book_ok = True          # live order-book prices (turned off if Kalshi refuses them)
        self.book_fails = 0
        self.stop_file = os.path.join(ledger.folder, "STOP")  # same folder as the trade log
        self.ready_accounts = {0}
        self.last_setup = None
        self._history_cache: tuple = (None, [])
        self.mismatch_streak = 0
        self.last_check = None
        self.last_accounts = None

    # ---- real money: subaccounts ------------------------------------------
    def prepare_live(self) -> None:
        """Make sure every play's Kalshi subaccount exists INSIDE the crypto pot.

        Kalshi keeps subaccounts per money pot, and these markets use the crypto pot (2).
        A missing number is made there. A number that exists only in another pot can't be
        used (Kalshi answers 404), so the play waits and the website says which number to
        pick instead. Making subaccounts needs Kalshi's Advanced API level, which Kalshi
        grants for free once one of your last 100 orders came from the API.
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
        crypto = {0} | {int(n) for n in self.state.crypto_subaccounts}
        exists = {0} | {int(n) for n in self.state.subaccounts}
        try:
            if hasattr(self.client, "subaccount_shards"):
                listed = self.client.subaccount_shards()
                exists |= set(listed)
                crypto |= {n for n, pots in listed.items() if CRYPTO_POT in pots}
            else:
                listed = self.client.subaccount_balances()
                exists |= set(listed)
                crypto |= set(listed)
        except Exception as e:
            log.error("could not list your Kalshi subaccounts (%s)", e)
        exists |= crypto
        missing = [n for n in needed if n not in exists]
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
            # Kalshi numbers new subaccounts in order. Never make more than the highest number
            # needed (they are written down, so a restart doesn't make them again).
            for _ in range(max(missing)):
                try:
                    n = self.client.create_subaccount(CRYPTO_POT)
                except Exception as e:
                    log.error("could not make a Kalshi subaccount (%s)", e)
                    problem = problem or f"Making it failed. Kalshi said: {kalshi_words(e)}"
                    break
                exists.add(n)
                crypto.add(n)
                self.state.crypto_subaccounts = sorted(set(self.state.crypto_subaccounts) | {n})
                log.info("made Kalshi subaccount %s in the crypto pot", n)
                if n > max(missing):
                    problem = f"Kalshi made subaccount {n} instead. Set the play to subaccount {n} on Settings."
                    log.warning(problem)
                    break
                if all(m in exists for m in missing):
                    break
        self.ready_accounts = {n for n in needed if n in crypto} | {0}
        self.state.subaccounts = sorted(n for n in exists if n != 0)
        self.ledger.save_state(self.state)
        not_ready = [n for n in needed if n not in crypto]
        wrong_pot = [n for n in not_ready if n in exists]
        if not_ready:
            log.error("Kalshi subaccount(s) %s are not set up in the crypto pot, so plays using them won't bet",
                      ", ".join(map(str, not_ready)))
            def who(n):
                return f"{account_name(n)} ({', '.join(p.name for p in self.s.plays if p.enabled and p.subaccount == n)})"
            parts = []
            if wrong_pot:
                spare = sorted(set(range(1, 64)) - exists)[:len(wrong_pot)]
                parts.append(f"{', '.join(who(n) for n in wrong_pot)}: made outside the crypto pot, so it can't be "
                             f"used. On Settings, change it to {' and '.join(map(str, spare))}; the bot then makes "
                             "it in the crypto pot.")
            others = [n for n in not_ready if n not in exists]
            if others:
                parts.append(f"Not set up on Kalshi yet: {', '.join(who(n) for n in others)}. " +
                             ("Kalshi makes subaccounts only after the bot has placed one real bet from your main "
                              "account; the bot tries again by itself." if upgrade_refused
                              else "The bot tries again every 10 minutes."))
            self.state.setup_note = " ".join(parts) + (f" ({problem})" if problem else "")
        else:
            self.state.setup_note = ""
            self.last_accounts = None   # show the new subaccounts soon
        log.info("plays use Kalshi subaccounts: %s", ", ".join(
            f"{p.name}={p.subaccount}" for p in self.s.plays if p.enabled))

    def clock_words(self, t: datetime) -> str:
        return t.astimezone(ZoneInfo(self.s.day_timezone)).strftime("%I:%M %p").lstrip("0").lower()

    def _crypto_pot(self, account: int = 0) -> Decimal | None:
        value = (self.state.shards.get(str(account)) or {}).get(str(CRYPTO_POT))
        return Decimal(value) if value not in (None, "") else None

    def _check_pot_moved(self, now: datetime) -> None:
        """A minute after the pot setting: say plainly whether Kalshi actually moved any money."""
        check = self.state.pot_check
        if not check:
            return
        try:
            asked = datetime.fromisoformat(check["at"])
        except (KeyError, ValueError):
            self.state.pot_check = {}
            return
        if (now - asked).total_seconds() < int(check.get("wait", 60)):
            return
        before, after = Decimal(check.get("before") or "0"), self._crypto_pot(0)
        if after is not None and after > before + Decimal("0.01"):
            self.state.pot_check = {}
            self.state.notice = (f"{self.clock_words(now)}: Kalshi moved money into the crypto pot "
                                 f"(${before:.2f} → ${after:.2f}). The bot can bet with it now.")
            log.info(self.state.notice)
        elif check.get("kind") == "move":
            status = ""
            if check.get("transfer_id"):
                try:
                    status = str(self.client.pot_move(check["transfer_id"]).get("status", ""))
                except Exception as e:
                    status = f"couldn't ask ({kalshi_words(e)})"
            if status.lower() in ("pending", "processing", "queued", "in_progress") and \
                    (now - asked).total_seconds() < 600:
                check["wait"] = int(check.get("wait", 20)) + 30      # still going: look again soon
                return
            self.state.pot_check = {}
            self.state.notice = (f"{self.clock_words(now)}: the move didn't show up: the crypto pot still has "
                                 f"${before:.2f}" + (f" (Kalshi says the move is: {status})" if status else "") +
                                 ". Nothing was sent twice. Send me this message.")
            log.warning(self.state.notice)
        else:
            self.state.pot_check = {}
            self.state.notice = (f"{self.clock_words(now)}: Kalshi took the setting, but after a minute it still "
                                 f"hasn't moved any money into the crypto pot (${before:.2f}). Tap the \"Move ... "
                                 "into the crypto pot\" button instead.")
            log.warning(self.state.notice)
        self.ledger.save_state(self.state)

    def _apply_make_sub_request(self) -> None:
        """Website button: make one new Kalshi subaccount inside the crypto pot."""
        path = os.path.join(self.ledger.folder, MAKE_SUB_FILE)
        if not os.path.exists(path):
            return
        try:
            os.remove(path)
        except OSError:
            pass
        now = self.clock.now()
        used = {p.subaccount for p in self.s.plays}
        spare = [n for n in self.state.crypto_subaccounts if n not in used]
        if len(spare) >= 2:
            self.state.notice = (f"{self.clock_words(now)}: not made: subaccounts {', '.join(map(str, spare))} are "
                                 "already in the crypto pot and no play uses them yet. Set a play to one of them on "
                                 "Settings.")
            log.info(self.state.notice)
            self.ledger.save_state(self.state)
            return
        try:
            n = self.client.create_subaccount(CRYPTO_POT)
        except Exception as e:
            self.state.notice = f"{self.clock_words(now)}: Kalshi didn't make the subaccount: {kalshi_words(e)}"
            log.error(self.state.notice)
        else:
            self.state.crypto_subaccounts = sorted(set(self.state.crypto_subaccounts) | {n})
            self.state.subaccounts = sorted(set(self.state.subaccounts) | {n})
            self.state.notice = (f"{self.clock_words(now)}: made subaccount {n} in the crypto pot. Next: on Settings, "
                                 f"set a play to subaccount {n} and save; then move money into it here.")
            log.info(self.state.notice)
        self.ledger.save_state(self.state)

    def _apply_pot_move_request(self) -> None:
        """Website button: move the main account's cash from the other pots into the crypto pot."""
        path = os.path.join(self.ledger.folder, POT_MOVE_FILE)
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
        now = self.clock.now()
        try:
            pots = self.client.subaccount_shards().get(0, {})
        except Exception as e:
            self.state.notice = f"{self.clock_words(now)}: couldn't read your pots: {kalshi_words(e)}"
            log.error(self.state.notice)
            self.ledger.save_state(self.state)
            return
        before = pots.get(CRYPTO_POT, Decimal("0"))
        moves = [(k, v.quantize(Decimal("0.01"), rounding="ROUND_DOWN")) for k, v in sorted(pots.items())
                 if k != CRYPTO_POT and v >= Decimal("0.01")]
        if not moves:
            self.state.notice = f"{self.clock_words(now)}: nothing to move; all your cash is already in the crypto pot."
            log.info(self.state.notice)
            self.ledger.save_state(self.state)
            return
        done, last_id = [], ""
        for i, (shard, amount) in enumerate(moves):
            client_id = f"{req.get('id') or uuid.uuid4()}"
            if i:
                client_id = str(uuid.uuid5(uuid.UUID(client_id), str(shard)))
            # Write it down before asking, so a lost reply is never followed by a second move.
            self.state.pot_check = {"at": now.isoformat(), "before": str(before), "kind": "move", "wait": 20,
                                    "client_id": client_id}
            self.ledger.save_state(self.state)
            try:
                last_id = self.client.move_between_pots(shard, CRYPTO_POT, amount, transfer_id=client_id)
                done.append(f"${amount:.2f} from {pot_name(shard)}")
                log.info("asked Kalshi to move $%s from the %s pot to the crypto pot (transfer %s)",
                         amount, pot_name(shard), last_id or "?")
            except KalshiError as e:
                self.state.notice = (f"{self.clock_words(now)}: Kalshi said no to moving ${amount:.2f} from "
                                     f"{pot_name(shard)} to crypto: {kalshi_words(e)}")
                log.error(self.state.notice)
                if not done:
                    self.state.pot_check = {}
                break
            except Exception as e:
                self.state.notice = (f"{self.clock_words(now)}: lost contact while asking Kalshi to move the money "
                                     f"({e}). The bot won't ask again by itself; check the pots in a minute.")
                log.error(self.state.notice)
                break
        if done:
            self.state.pot_check["transfer_id"] = last_id
            self.state.notice = (f"{self.clock_words(now)}: asked Kalshi to move {', '.join(done)} into the crypto "
                                 "pot. Checking in a few seconds that it arrived.")
        self.last_accounts = None
        self.ledger.save_state(self.state)

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
        now = self.clock.now()
        try:
            if shard is None:
                self.client.set_target_allocation({})
                self.state.pot_check = {}
                self.state.notice = f"{self.clock_words(now)}: Kalshi no longer moves your money between pots."
            else:
                self.client.set_target_allocation({int(shard): 100})
                try:
                    got = self.client.target_allocation()
                    said = ", ".join(f"{pot_name(k)} {v}%" for k, v in sorted(got.items())) or "off"
                except Exception as e:
                    said = f"couldn't read it back ({kalshi_words(e)})"
                before = self._crypto_pot(0)
                self.state.pot_check = {"at": now.isoformat(), "before": str(before or 0)}
                self.state.notice = (f"{self.clock_words(now)}: Kalshi took the setting (it now says: {said}). "
                                     "Checking in a minute whether it really moves the money.")
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
            self._apply_pot_move_request()
            self._apply_make_sub_request()
            self._apply_transfer_request()
            self._refresh_accounts(now)
        self._goal_check()   # before any new bet, so the goal pauses betting in time
        quotes = self._fetch_quotes(now)
        self._apply_trade_request(quotes, now)
        for pos in list(self.state.open_positions()):
            q = quotes.get(pos.ticker)
            if q is not None:
                self._maybe_exit(pos, q)
        self._score(quotes, now)     # before entries: "our pick" plays and the score check use it
        reason = self._entry_block_reason()
        if reason is None:
            spots = self._spots(quotes, now)
            for q in quotes.values():
                for play in self.s.plays:
                    if play.enabled:
                        self._maybe_enter(play, q, now, self._distance_price(q, now, spots.get(q.series)))
        else:
            self._say("block", reason)
        self._watch(quotes, now)
        self._share_prices(quotes, now)
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
        rows = []
        for q in quotes.values():
            row = q.as_row()
            sc = self.scores.get(q.ticker) if self.s.watch.score else None
            if sc is not None:
                row.update(chance_up=f"{sc['chance_up']:.3f}", score_up=sc["up"], score_down=sc["down"],
                           score_best=sc["best"], score_level=sc["level"])
            if q.ticker in self.btc_now and q.target is not None:
                row.update(btc_now=f"{self.btc_now[q.ticker]:.2f}",
                           btc_gap=f"{self.btc_now[q.ticker] - float(q.target):.2f}")
            left = (q.close_time - now).total_seconds() if q.close_time else None
            coin = lastminute.coin_for(q.series)
            if self.s.watch.score and coin and left is not None and 0 < left <= 900:
                j = self._jump(coin, now)
                if j is not None:
                    row.update(jump=j[0], jump_dollars=f"{j[1]:.0f}")
            est = self.estimates.get(q.ticker)
            if est is not None:
                row.update(est_projected=str(est.projected.quantize(Decimal("0.01"))),
                           est_gap=str(est.gap.quantize(Decimal("0.01"))), est_locked=est.locked,
                           est_price=str(est.price.quantize(Decimal("0.01"))))
            rows.append(row)
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
            self.nap(wait)

    def nap(self, seconds: float) -> None:
        """Sleep, but wake within a quarter second when the website asks for something (like a trade)."""
        paths = [os.path.join(self.ledger.folder, f) for f in WAKE_FILES]
        for _ in range(max(1, int(seconds / 0.25 + 0.999))):
            if any(os.path.exists(p) for p in paths):
                return
            self.clock.sleep(min(0.25, seconds))

    # ---- trades you make yourself on the website ------------------------------
    def _apply_trade_request(self, quotes: dict, now: datetime) -> None:
        path = os.path.join(self.ledger.folder, TRADE_FILE)
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
        try:
            note = self._trade(req, quotes, now)
        except Exception as e:   # never let a button press stop the bot
            note = f"not done: {e}"
            log.exception("trade from the website failed")
        self.state.trade_note = f"{self.clock_words(now)}: {note}"
        log.info("%s: %s", MANUAL, note)
        self.ledger.save_state(self.state)

    def _trade(self, req: dict, quotes: dict, now: datetime) -> str:
        asked = rules.parse_time(req.get("at"))
        if asked is None or abs((now - asked).total_seconds()) > TRADE_MAX_AGE:
            return "not done: the bot got the request too late, and prices may have moved. Try again."
        if self.state.alert:
            return "not done: the bot is stopped for safety (see the warning at the top)."
        side = req.get("side")
        if side not in ("yes", "no"):
            return "not done: pick Up or Down."
        word = "Up" if side == "yes" else "Down"
        if req.get("action") == "sell":
            return self._manual_sell(req, side, quotes)
        try:
            n = Decimal(str(req.get("contracts", 1)))
        except ArithmeticError:
            n = Decimal("0")
        if n != n.to_integral_value() or not 1 <= n <= 500:
            return "not done: contracts must be a whole number from 1 to 500."
        series = req.get("series") or (self.s.markets[0] if self.s.markets else "")
        open_now = [q for q in quotes.values() if q.series == series and q.close_time and q.close_time > now]
        if not open_now:
            return "not done: no open market right now (prices didn't load). Try again in a moment."
        q = min(open_now, key=lambda x: x.close_time)
        other = [p for p in self.state.open_positions()
                 if p.ticker == q.ticker and p.side != side and (p.subaccount or 0) == 0
                 and (self.broker.live or p.play == MANUAL)]
        if other:
            held = "Up" if other[0].side == "yes" else "Down"
            return (f"not bought: you already hold {held} in {self.market_words(q)}. Buying {word} too would "
                    f"cancel it out on Kalshi. To switch sides, tap Sell now on the {held} bet first.")
        ask = q.ask(side)
        if ask is None:
            return f"not bought: nobody is selling {word} in {self.market_words(q)} right now."
        cap = Decimal(str(req["max_price"])) if req.get("max_price") not in (None, "") else None
        limit = min(cap if cap is not None else ask + Decimal("0.02"), Decimal("0.99"))
        what = f"{n:g} {word} in {self.market_words(q)}"
        if ask > limit:
            return f"not bought: {word} costs {cents_words(ask)} now, more than your {cents_words(limit)}."
        mult = self.multipliers.get(q.series, ONE)
        try:
            fill = self.broker.buy(q, side, n, limit, mult, account=0)
        except KalshiError as e:
            if "insufficient_balance" in (e.text or ""):
                return (f"not bought: not enough money in your main account's crypto pot for {what} at up to "
                        f"{cents_words(limit)}.")
            return f"not bought: Kalshi said {kalshi_words(e)}"
        except Exception as e:
            if not self.broker.live:
                raise
            log.error("%s: lost contact while buying (%s); checking what Kalshi shows", MANUAL, e)
            fill = self._recover_buy(q, side, limit, mult, 0)
        if fill is None and self.broker.live:
            fill = self._recover_buy(q, side, limit, mult, 0)
        if fill is None:
            return f"didn't fill: {what} at up to {cents_words(limit)}; {self._miss_reason(q, side, limit)}."
        pos = Position(MANUAL, q.ticker, q.series, side, plain(fill.contracts), str(fill.price), str(fill.fee),
                       now.isoformat(), q.close_time.isoformat() if q.close_time else "", 0)
        self.state.set_positions(self.state.open_positions() + [pos])
        self.state.day_trades += 1
        if not self.broker.live:
            self.state.paper_cash = str(self.broker.cash)
        return (f"BOUGHT {plain(fill.contracts)} {word} in {self.market_words(q)} at "
                f"{cents_words(fill.price)} (fee {cents_words(fill.fee)}).")

    def _manual_sell(self, req: dict, side: str, quotes: dict) -> str:
        ticker = req.get("ticker")
        word = "Up" if side == "yes" else "Down"
        held = [p for p in self.state.open_positions() if p.play == MANUAL and p.ticker == ticker and p.side == side]
        if not held:
            return "not sold: that bet isn't open anymore."
        pos = held[0]
        q = quotes.get(ticker)
        bid = q.bid(side) if q is not None else None
        if bid is None:
            return "not sold: nobody is buying that side right now."
        floor = max(bid - Decimal("0.02"), Decimal("0.01"))
        mult = self.multipliers.get(pos.series, ONE)
        try:
            fill = self.broker.sell(q, side, pos.d("contracts"), floor, mult, account=pos.subaccount)
        except KalshiError as e:
            return f"not sold: Kalshi said {kalshi_words(e)}"
        except Exception as e:
            if not self.broker.live:
                raise
            log.error("%s: lost contact while selling (%s); checking what Kalshi shows", MANUAL, e)
            fill = self._recover_sell(pos, floor, mult)
        if fill is None:
            return f"didn't sell: tried at {cents_words(floor)} or more; the price moved."
        self._close(pos, fill.contracts, "sold_by_you", fill, None)
        return f"SOLD {plain(fill.contracts)} {word} at {cents_words(fill.price)} (fee {cents_words(fill.fee)})."

    # ---- pieces --------------------------------------------------------------
    def _fetch_quotes(self, now: datetime) -> dict:
        quotes = {}
        for series in self.s.markets:
            try:
                for m in self.client.open_markets(series):
                    q = rules.Quote.from_market(m, series)
                    if q.ticker:
                        quotes[q.ticker] = self._live_book(q, now)
                        mid = quotes[q.ticker].yes_mid()
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

    def _live_book(self, q: rules.Quote, now: datetime) -> rules.Quote:
        """Use the order book's live prices for the market that's running now.

        Kalshi's market list only updates its prices about every 30 seconds, so a jump to 90c
        could still show as 70c there. Markets more than 15 minutes from closing keep the list's
        prices (nothing trades on them yet), and so does any market whose book can't be read.
        """
        fn = getattr(self.client, "best_prices", None)
        if fn is None or not self.book_ok or q.close_time is None \
                or not 0 < (q.close_time - now).total_seconds() <= 900:
            return q
        try:
            b = fn(q.ticker)
        except Exception as e:
            code = getattr(e, "status", None)
            if code in (401, 403):
                self.book_ok = False
                log.warning("Kalshi won't share live order-book prices (%s); using the market list's prices", code)
            else:
                self.book_fails += 1
                if self.book_fails in (1, 10) or self.book_fails % 100 == 0:
                    log.warning("could not read the live order book for %s (%s)", q.ticker, e)
            return q
        if b.get("yes_bid") is None and b.get("yes_ask") is None:
            return q        # an empty book this instant: keep the list's prices
        from dataclasses import replace
        return replace(q, yes_bid=b.get("yes_bid"), yes_ask=b.get("yes_ask"),
                       yes_bid_size=b.get("yes_bid_size"), yes_ask_size=b.get("yes_ask_size"))

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
            # The bot only bets on crypto, so money moves happen inside the crypto pot.
            try:
                self.client.transfer(frm, to, cents, req.get("id"), exchange_index=CRYPTO_POT)
            except KalshiError as e:
                if e.status != 404:
                    raise
                # Kalshi keeps subaccounts per pot; this one has no crypto-pot side yet. Don't make
                # new subaccounts here (each tap would make another); just say so.
                raise KalshiError(0, f"Kalshi says {account_name(to) if to else account_name(frm)} has no crypto "
                                     "pot (404 not found). Keep your plays on your main account for now.")
            self.state.notice = (f"Moved ${cents / 100:.2f} from {account_name(frm)} to {account_name(to)} "
                                 "(crypto pot).")
            log.info(self.state.notice)
            self.last_accounts = None   # show the new amounts soon
            if hasattr(self.broker, "_cache"):
                self.broker._cache = (0.0, {})
        except Exception as e:
            words = e.text if isinstance(e, KalshiError) and e.status == 0 else kalshi_words(e)
            self.state.notice = f"Money not moved: {words}"
            log.error(self.state.notice)
        self.ledger.save_state(self.state)

    def _refresh_accounts(self, now: datetime) -> None:
        if self.last_accounts and (now - self.last_accounts).total_seconds() < ACCOUNTS_SECONDS:
            return
        self.last_accounts = now
        try:
            if hasattr(self.client, "subaccount_shards"):
                split = self.client.subaccount_shards()
                cash = {n: sum(p.values(), Decimal("0")) for n, p in split.items()
                        if n in getattr(self.broker, "accounts", [0]) or n == 0}
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
        for n in getattr(self, "ready_accounts", {0}):
            cash.setdefault(n, Decimal("0"))            # a new, empty subaccount still exists
        self.state.accounts = {str(k): str(v) for k, v in sorted(cash.items())}
        self._check_pot_moved(now)

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

    @staticmethod
    def _max_tries(play) -> int:
        """Order tries per play per market (every buy uses one): 5, plus 2 more for each extra buy allowed."""
        return MAX_ORDER_TRIES + 2 * (play.max_buys - 1)

    def _maybe_enter(self, play, q: rules.Quote, now: datetime, spot) -> None:
        key = f"{play.name}|{q.ticker}"
        bought = self.state.entered.count(key)
        if bought >= play.max_buys or f"{key}|stop" in self.state.entered \
                or self.state.attempts.get(key, 0) >= self._max_tries(play):
            return
        held = None
        if bought:
            # Adding to a bet: only while it's still open, not too soon after the last buy, same side.
            mine = [p for p in self.state.open_positions() if p.play == play.name and p.ticker == q.ticker]
            if not mine:
                return          # it already sold out of this market: don't buy back in
            last = max((rules.parse_time(p.opened_at) for p in mine if rules.parse_time(p.opened_at)), default=None)
            if last is not None and (now - last).total_seconds() < float(play.seconds_between_buys):
                return
            held = mine[0].side
        chk = rules.check_entry(play, q, now, self.history.get(q.ticker), spot, self.scores.get(q.ticker))
        if not chk.ok:
            if chk.reason != "outside time window":
                self._say(key, f"{play.name}: skip {q.ticker}: {chk.reason}")
            return
        if held is not None and chk.side != held:
            self._say(key, f"{play.name}: no more buys in {self.market_words(q)}: it now wants "
                           f"{rules.side_name(chk.side)} but holds {rules.side_name(held)} "
                           f"(buying both would cancel out on Kalshi)")
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
                fix = ("tap \"Move ... into the crypto pot\" in the Kalshi money box" if acct == 0 else
                       f"move money into {account_name(acct)} in the Kalshi money box")
                self._say(key, f"{play.name}: skip {self.market_words(q)}: only ${balance:.2f} in "
                               f"{account_name(acct)}'s {pot_name(shard)} pot, not enough for 1 contract at "
                               f"{cents_words(chk.limit)}. To fix: {fix}.")
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
                self.state.entered.append(f"{key}|stop")      # unsure what happened: no more buys here
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
                     f"Will try again ({tries} of {self._max_tries(play)} tries used)."
                     if tries < self._max_tries(play) else "That was the last try for this market.")
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
        log.info("%s: BOUGHT %s %s x%s at %s (fee $%s), %.1f min left%s", play.name, q.ticker,
                 rules.side_name(chk.side), fill.contracts, fill.price, fill.fee, q.minutes_left(now),
                 f" (buy {bought + 1} of up to {play.max_buys})" if play.max_buys > 1 else "")
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
        if self.s.watch.score and any(q.close_time and 0 < (q.close_time - now).total_seconds() <= 900
                                      for q in quotes.values()):
            fast_enough = min(5.0, self.s.slow_seconds)     # keep the live score fresh
        else:
            fast_enough = self.s.slow_seconds
        watch = float(self.s.watch.seconds)
        if watch and any(q.close_time and -1 <= (q.close_time - now).total_seconds() <= watch + self.s.slow_seconds
                         for q in quotes.values()):
            return self.s.fast_seconds
        if any(rules.parse_time(p.close_time) and now >= rules.parse_time(p.close_time)
               for p in self.state.open_positions()):
            return max(self.s.fast_seconds, 5.0)
        return fast_enough

    # ---- last-minute watch (logs ideas; never sends an order) ----------------
    def _watch(self, quotes: dict, now: datetime) -> None:
        w = self.s.watch
        if not w.enabled:
            return
        prices: dict = {}
        for q in quotes.values():
            if q.close_time is None or q.target is None:
                continue
            left = (q.close_time - now).total_seconds()
            if not 0 <= left <= float(w.seconds) + 1:
                continue
            self._watch_crowd(q, now, left)
            coin = lastminute.coin_for(q.series)
            if coin is None:
                continue
            if coin not in prices:
                try:
                    prices[coin] = self.index_fn(coin)
                except Exception:
                    prices[coin] = (None, 0)
            price, sources = prices[coin]
            tr = self.trackers.get(q.ticker)
            if tr is None:
                tr = self.trackers[q.ticker] = lastminute.Tracker(q.close_time, q.target)
            if price is not None:
                tr.add(now, price)
            est = tr.estimate(now)
            if est is None:
                continue
            self.estimates[q.ticker] = est
            self._watch_data(q, now, est, sources)
            self._watch_guard(q, now, est)
            self._watch_follow(q, now, est)
        for ticker, tr in list(self.trackers.items()):
            if now - tr.close > timedelta(seconds=120):
                del self.trackers[ticker]
                self.estimates.pop(ticker, None)
        self._watch_resolve(now)

    def _distance_price(self, q: rules.Quote, now: datetime, spot):
        """Price for the "this many $ past the target" check.

        In the final stretch: the bot's running estimate of the settlement average (what
        actually decides the market). Example: target $61,300, Bitcoin at $61,325 with 10
        seconds left, but the first 50 seconds averaged $61,295, so the average heads to
        about $61,299: Down by $1, a coin flip, even though the live price says Up by $25.
        Before that (or if the watch is off): Bitcoin's live price.
        """
        est, tr = self.estimates.get(q.ticker), self.trackers.get(q.ticker)
        if est is not None and tr is not None and tr.samples and (now - tr.samples[-1][0]).total_seconds() <= 5:
            return est.projected
        return spot

    def _watch_data(self, q: rules.Quote, now: datetime, est, sources: int) -> None:
        """Second-by-second record of the final stretch, for checking ideas later."""
        day = now.astimezone(ZoneInfo(self.s.day_timezone)).strftime("%Y-%m-%d")
        folder = os.path.join(self.ledger.folder, "lastminute")
        path = os.path.join(folder, f"{day}.csv")
        try:
            if not os.path.exists(path):
                for old in sorted(os.listdir(folder))[:-13] if os.path.isdir(folder) else []:
                    os.remove(os.path.join(folder, old))     # keep two weeks
            lastminute.append_csv(path, lastminute.DATA_COLUMNS, {
                "time": now.isoformat(timespec="seconds"), "market": q.ticker,
                "seconds_left": int(est.seconds_left), "target": str(q.target), "price": str(est.price),
                "sources": sources, "projected": str(est.projected.quantize(Decimal("0.01"))),
                "gap": str(est.gap.quantize(Decimal("0.01"))),
                "yes_bid": str(q.yes_bid or ""), "yes_ask": str(q.yes_ask or "")})
        except OSError as e:
            self._say("lastminute-file", f"could not save last-minute data ({e})")

    def _watch_guard(self, q: rules.Quote, now: datetime, est) -> None:
        w = self.s.watch
        if est.seconds_left > float(w.guard_seconds):
            return
        for pos in self.state.open_positions():
            key = f"guard|{pos.play}|{pos.ticker}"
            if pos.ticker != q.ticker or key in self.state.watch or pos.d("entry_price") < DEFEND_FROM:
                continue
            against = -est.gap if pos.side == "yes" else est.gap
            bid = q.bid(pos.side)
            if against < w.guard_margin or bid is None:
                continue
            n = pos.d("contracts")
            fee = rules.taker_fee(n, bid, self.multipliers.get(q.series, ONE))
            self.state.watch[key] = {
                "kind": "guard", "play": pos.play, "market": pos.ticker, "side": pos.side, "contracts": str(n),
                "price": str(bid), "fee": str(fee), "entry_price": pos.entry_price, "entry_fee": pos.entry_fee,
                "seconds_left": int(est.seconds_left), "gap": str(est.gap.quantize(Decimal("1"))),
                "close": q.close_time.isoformat()}
            log.info("watch (no order sent): %s's %s %s: the guard would sell now at %s (%s, %s left)",
                     pos.play, self.market_words(q), rules.side_name(pos.side), cents_words(bid),
                     lastminute.gap_words(est), lastminute.seconds_words(est.seconds_left))

    def _watch_crowd(self, q: rules.Quote, now: datetime, left: float) -> None:
        """Copy the crowd: when the price of a bet we hold falls to the set level late, note 'would sell'."""
        w = self.s.watch
        if left > float(w.crowd_seconds):
            return
        for pos in self.state.open_positions():
            key = f"crowd|{pos.play}|{pos.ticker}"
            if pos.ticker != q.ticker or key in self.state.watch or pos.d("entry_price") < DEFEND_FROM:
                continue
            bid = q.bid(pos.side)
            if bid is None or bid > w.crowd_price:
                continue
            n = pos.d("contracts")
            fee = rules.taker_fee(n, bid, self.multipliers.get(q.series, ONE))
            self.state.watch[key] = {
                "kind": "crowd", "play": pos.play, "market": pos.ticker, "side": pos.side, "contracts": str(n),
                "price": str(bid), "fee": str(fee), "entry_price": pos.entry_price, "entry_fee": pos.entry_fee,
                "seconds_left": int(left), "gap": str(self.estimates[q.ticker].gap.quantize(Decimal("1")))
                if q.ticker in self.estimates else "", "close": q.close_time.isoformat()}
            log.info("watch (no order sent): %s's %s %s fell to %s with %s left; copying the crowd would sell now",
                     pos.play, self.market_words(q), rules.side_name(pos.side), cents_words(bid),
                     lastminute.seconds_words(left))

    def _watch_follow(self, q: rules.Quote, now: datetime, est) -> None:
        w = self.s.watch
        key = f"follow|{q.ticker}"
        if key in self.state.watch or not 3 <= est.seconds_left <= float(w.follow_seconds):
            return
        if abs(est.gap) < w.follow_margin:
            return
        side = est.favored()
        ask = q.ask(side)
        if ask is None or ask > w.follow_max_price:
            return
        fee = rules.taker_fee(ONE, ask, self.multipliers.get(q.series, ONE))
        self.state.watch[key] = {
            "kind": "follow", "play": "follow", "market": q.ticker, "side": side, "contracts": "1",
            "price": str(ask), "fee": str(fee), "seconds_left": int(est.seconds_left),
            "gap": str(est.gap.quantize(Decimal("1"))), "close": q.close_time.isoformat()}
        log.info("watch (no order sent): follow would buy %s %s at %s (%s, %s left)", self.market_words(q),
                 rules.side_name(side), cents_words(ask), lastminute.gap_words(est),
                 lastminute.seconds_words(est.seconds_left))

    def _watch_resolve(self, now: datetime) -> None:
        """After the close: compare each idea with what really happened, and write it down."""
        for key, e in list(self.state.watch.items()):
            close = rules.parse_time(e.get("close"))
            if close is None or now < close + timedelta(seconds=SETTLE_DELAY):
                continue
            last = self.last_settle_check.get(key)
            if last and (now - last).total_seconds() < SETTLE_RECHECK:
                continue
            self.last_settle_check[key] = now
            try:
                result = self.client.market(e["market"]).get("result")
            except Exception:
                continue
            if result not in ("yes", "no"):
                if now - close > timedelta(hours=2):
                    del self.state.watch[key]
                continue
            n, price, fee = Decimal(e["contracts"]), Decimal(e["price"]), Decimal(e["fee"])
            won = result == e["side"]
            q = rules.Quote(e["market"], e["market"].split("-")[0], close, "", None, None, None, None, None)
            where = f"{self.market_words(q)} {rules.side_name(e['side'])}"
            if e["kind"] in ("guard", "crowd"):
                entry, entry_fee = Decimal(e["entry_price"]), Decimal(e["entry_fee"])
                held = (n if won else Decimal("0")) - n * entry - entry_fee
                would = n * price - fee - n * entry - entry_fee
                diff = would - held
                who = "the guard" if e["kind"] == "guard" else "copying the crowd"
                verdict = (f"{who} would have saved ${diff:.2f}" if diff > 0
                           else f"{who} would have cost ${-diff:.2f}")
                log.info("watch result: %s's %s %s. Holding to the end: %s. Selling at %s: %s. So %s.",
                         e["play"], where, "won" if won else "lost", signed(held), cents_words(price), signed(would),
                         verdict)
            else:
                held = Decimal("0")
                would = (ONE if won else Decimal("0")) - price - fee
                diff = would
                log.info("watch result: follow's %s at %s would have %s: %s per contract.", where,
                         cents_words(price), "won" if won else "lost", signed(would))
            try:
                lastminute.append_csv(os.path.join(self.ledger.folder, "watch.csv"), lastminute.WATCH_COLUMNS, {
                    "time": close.isoformat(), "kind": e["kind"], "play": e["play"], "market": e["market"],
                    "side": rules.side_name(e["side"]), "contracts": e["contracts"],
                    "seconds_left": e["seconds_left"], "gap_dollars": e["gap"], "price": e["price"],
                    "entry_price": e.get("entry_price", ""), "result": rules.side_name(result),
                    "would_profit": f"{would:.4f}", "held_profit": f"{held:.4f}", "difference": f"{diff:.4f}"})
            except OSError as err:
                self._say("watch-file", f"could not save a last-minute result ({err})")
            del self.state.watch[key]

    # ---- live score (1-10 for each side) ---------------------------------------
    def _jump(self, coin: str, now: datetime):
        hit = self.jumps.get(coin)
        if hit is None or (now - hit[0]).total_seconds() >= 20:
            try:
                value = self.jump_fn(coin)
            except Exception:
                value = None
            if value is None and hit is not None and (now - hit[0]).total_seconds() < 180:
                value = hit[1]          # keep a recent good one
            self.jumps[coin] = hit = (now, value)
        return hit[1]

    def _sigma(self, coin: str, now: datetime) -> float | None:
        hit = self.sigmas.get(coin)
        if hit is None or (now - hit[0]).total_seconds() >= 60:
            try:
                value = self.sigma_fn(coin)
            except Exception:
                value = None
            if value is None and hit is not None:
                value = hit[1]          # keep the last good one
            self.sigmas[coin] = hit = (now, value)
        return hit[1]

    @property
    def needs_score(self) -> bool:
        """A play that buys "our pick" or checks the score needs our chance even if the card is off."""
        return any(p.enabled and (p.buy == "pick" or p.conditions.min_score is not None) for p in self.s.plays)

    def _score(self, quotes: dict, now: datetime) -> None:
        """Our chance for each side vs Kalshi's price, as 1-10 (5 = fair), plus snapshots to check later."""
        if not (self.s.watch.score or self.needs_score):
            return
        spot_cache: dict = {}
        for q in quotes.values():
            if q.close_time is None or q.target is None:
                continue
            left = (q.close_time - now).total_seconds()
            if not 0 < left <= 900:
                continue
            est, tr = self.estimates.get(q.ticker), self.trackers.get(q.ticker)
            if est is not None and tr is not None and tr.samples and (now - tr.samples[-1][0]).total_seconds() <= 5:
                mean, price_now = float(est.projected), float(est.price)
            else:
                product = spot_product(q.series)
                if product not in spot_cache:
                    try:
                        spot_cache[product] = self.spot_fn(product) if product else None
                    except Exception:
                        spot_cache[product] = None
                if spot_cache[product] is None:
                    continue
                mean = price_now = float(spot_cache[product])
            self.btc_now[q.ticker] = price_now          # shown on the card all the time
            coin = lastminute.coin_for(q.series)
            sigma = self._sigma(coin, now) if coin else None
            if sigma is None:
                continue
            p = lastminute.chance_up(mean, float(q.target), left, sigma)
            up_ask, down_ask = q.ask("yes"), q.ask("no")
            su = lastminute.score(p, float(up_ask) if up_ask is not None else None)
            sd = lastminute.score(1 - p, float(down_ask) if down_ask is not None else None)
            best = "yes" if p >= 0.5 else "no"      # the side we think will win (its score says if it's a deal)
            chance_best = p if best == "yes" else 1 - p
            level = lastminute.confidence(chance_best)
            self.scores[q.ticker] = {"chance_up": p, "up": su, "down": sd, "best": best, "level": level}
            if self.s.watch.score:
                self._score_snapshot(q, now, left, p, up_ask, down_ask, su, sd, best, level)
        for ticker in list(self.scores):
            if ticker not in quotes:
                del self.scores[ticker]
        for ticker in list(self.btc_now):
            if ticker not in quotes:
                del self.btc_now[ticker]
        if self.s.watch.score:
            self._score_resolve(now)

    def _score_snapshot(self, q, now, left, p, up_ask, down_ask, su, sd, best, level=1) -> None:
        pending = self.state.score_pending.setdefault(q.ticker, {"close": q.close_time.isoformat(), "snaps": []})
        taken = {s["checkpoint"] for s in pending["snaps"]}
        for cp in lastminute.CHECKPOINTS:
            if cp not in taken and cp - 20 < left <= cp:
                pending["snaps"].append({
                    "checkpoint": cp, "time": now.isoformat(timespec="seconds"), "seconds_left": int(left),
                    "chance_up": f"{p:.3f}", "up_cost": str(up_ask or ""), "down_cost": str(down_ask or ""),
                    "up_score": su or "", "down_score": sd or "", "best": rules.side_name(best),
                    "best_score": (su if best == "yes" else sd) or "", "level": level})

    def _score_resolve(self, now: datetime) -> None:
        for ticker, pending in list(self.state.score_pending.items()):
            close = rules.parse_time(pending.get("close"))
            if close is None or now < close + timedelta(seconds=SETTLE_DELAY):
                continue
            if not pending.get("snaps"):
                del self.state.score_pending[ticker]
                continue
            key = f"score|{ticker}"
            last = self.last_settle_check.get(key)
            if last and (now - last).total_seconds() < SETTLE_RECHECK:
                continue
            self.last_settle_check[key] = now
            try:
                result = self.client.market(ticker).get("result")
            except Exception:
                continue
            if result not in ("yes", "no"):
                if now - close > timedelta(hours=2):
                    del self.state.score_pending[ticker]
                continue
            path = os.path.join(self.ledger.folder, "scores.csv")
            for snap in pending["snaps"]:
                row = dict(snap, market=ticker, result=rules.side_name(result),
                           best_won="yes" if snap["best"] == rules.side_name(result) else "no")
                try:
                    lastminute.append_csv(path, lastminute.SCORE_COLUMNS, row)
                except OSError as e:
                    self._say("score-file", f"could not save a score result ({e})")
                    break
            del self.state.score_pending[ticker]

    def _say(self, key: str, message: str) -> None:
        """Log a message only when it changes, so the log stays readable."""
        if self.last_reason.get(key) != message:
            self.last_reason[key] = message
            log.info(message)
