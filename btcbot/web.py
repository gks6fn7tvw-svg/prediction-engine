"""The bot's private website: status, every play's bets and score, and every setting.

Runs as its own small service next to the bot. It only reads the bot's files,
writes settings.yaml (the bot notices and restarts with the new settings),
creates or removes the STOP file, and can download the newest bot code.
Anyone without the private key in the link gets nothing.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import hmac
import html
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from . import goal as G
from . import keystore
from . import ledger as L
from .kalshi import KalshiClient, KalshiError
from . import history as H
from . import lastminute
from .bot import (ALLOCATION_FILE, MAKE_SUB_FILE, MANUAL, PAPER_RESET_FILE, POT_MOVE_FILE, SETUP_FILE,
                  TRADE_FILE, TRANSFER_FILE, account_name, pot_name)
from .rules import Quote, parse_time
from .secure import SecureLink
from .settings import (LIVE_CONFIRM_TEXT, PACKAGE_DIR, SIZE_MODES, Settings, SettingsError, load, load_raw,
                       mode_folder, save_raw)

KEY_FILE_ENV = "BTCBOT_WEB_KEY_FILE"
MARKETS = [("KXBTC15M", "Bitcoin"), ("KXETH15M", "Ethereum"), ("KXSOL15M", "Solana"), ("KXXRP15M", "XRP")]
BUY_CHOICES = [("favorite", "The favorite"), ("underdog", "The underdog"), ("up", "Always Up"), ("down", "Always Down"),
               ("pick", "Our pick (the side our chance says wins)")]
SIZE_CHOICES = [("contracts", "Contracts"), ("dollars", "Dollars"), ("percent", "% of balance")]
LOSS_CHOICES = [("dollars", "Dollars"), ("percent", "% of the day's balance")]
EXIT_WORDS = {"take_profit": "take profit", "stop_loss": "stop loss"}
REPO_DIR = os.path.dirname(PACKAGE_DIR)
COOKIE = "btcbot_key"
MAX_BODY = 64_000
FRESH_SECONDS = 2.5   # the bot's saved prices count as live when newer than this
CACHE_SECONDS = 1.0   # the website asks Kalshi at most this often per market
ONE = Decimal("1")
ON_PILL = '<span class="pill on-pill">ON</span>'
OFF_PILL = '<span class="pill off-pill">OFF</span>'
DONE_MESSAGES = {
    "saved": "Saved. The bot restarts with the new settings in about 15 seconds.",
    "added": "Added a new play. It starts switched off: set it up, tick \"This play is on\", then Save.",
    "copied": "Copied. The copy starts switched off: change it, tick \"This play is on\", then Save.",
    "deal": "Added the deal play with pretend money: it buys our pick when its score is 7 or more, 5 to 1 minutes "
            "before the close, 1 contract. It's already on.",
    "deleted": "Deleted. Its past bets stay in the trade list.",
    "reset": "Pretend money reset. The bot picks it up within a few seconds; past bets are kept.",
}
LOW_MONEY = Decimal("1")


def load_key(path: str | None = None) -> str:
    """The private key for the link. Made once and kept in a file only the bot user can read."""
    path = path or os.environ.get(KEY_FILE_ENV) or os.path.join(PACKAGE_DIR, "data", "web.key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_urlsafe(18)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return key


# ---------------------------------------------------------------- updates
def _git(*args, timeout=90):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True, timeout=timeout)


def code_version() -> str:
    try:
        r = _git("log", "-1", "--format=%h (%cs)", timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def update_code() -> bool:
    """Download the newest bot code the same way the server does at boot.

    Returns True if the code changed. Raises RuntimeError with a plain reason.
    """
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "btcbot"
        before = _git("rev-parse", "HEAD").stdout.strip()
        for b in dict.fromkeys([branch, "main"]):
            if _git("fetch", "--depth", "1", "origin", b).returncode == 0:
                break
        else:
            raise RuntimeError("couldn't reach GitHub. Try again in a few minutes.")
        r = _git("checkout", "-q", "-f", "-B", b, "FETCH_HEAD")
        if r.returncode:
            raise RuntimeError("couldn't switch to the new code: " + r.stderr.strip()[-300:])
        after = _git("rev-parse", "HEAD").stdout.strip()
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                              os.path.join(PACKAGE_DIR, "requirements.txt")],
                             capture_output=True, text=True, timeout=600)
        if pip.returncode:
            raise RuntimeError("couldn't install python packages: " + pip.stderr.strip()[-300:])
        return before != after
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"update failed: {e}")


def restart_soon(settings_path: str) -> bool:
    """Restart the bot (it restarts when its settings file changes) and this website.

    Returns False when not running under systemd, where nothing would bring
    the website back.
    """
    os.utime(settings_path, None)
    if not os.environ.get("INVOCATION_ID"):
        return False
    threading.Timer(1.5, os._exit, (0,)).start()
    return True


# ---------------------------------------------------------------- small helpers
def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def cents(dollars) -> str:
    """0.7 -> "70" for the form; None -> ""."""
    if dollars in (None, ""):
        return ""
    return f"{float(dollars) * 100:g}"


def cents_text(dollars) -> str:
    return f"{float(dollars) * 100:.0f}¢"


def plain(value) -> str:
    return "" if value in (None, "") else f"{float(value):g}"


def bets(n: int) -> str:
    return f"{n} bet{'' if n == 1 else 's'}"


def money(d) -> str:
    d = Decimal(str(d))
    cls = "pos" if d > 0 else "neg" if d < 0 else ""
    sign = "+" if d > 0 else "−" if d < 0 else ""
    return f'<span class="{cls}">{sign}${abs(d):.2f}</span>'


def clock(t: datetime, zone: ZoneInfo) -> str:
    return t.astimezone(zone).strftime("%I:%M %p").lstrip("0").lower()


def local_time(iso: str | None, zone: ZoneInfo) -> str:
    t = parse_time(iso)
    if t is None:
        return ""
    lt = t.astimezone(zone)
    today = datetime.now(zone).date()
    day = "Today" if lt.date() == today else f"{lt.strftime('%b')} {lt.day}"
    return f"{day} {clock(t, zone)}"


def number(form: dict, name: str, label: str, blank_ok: bool = False, scale: float = 1.0):
    text = (form.get(name, [""])[0] or "").strip().replace("$", "").replace("%", "").replace("¢", "")
    if text == "":
        if blank_ok:
            return None
        raise SettingsError(f"{label}: please fill this in")
    try:
        value = float(text) / scale
    except ValueError:
        raise SettingsError(f"{label}: \"{text}\" is not a number")
    value = round(value, 4)
    return int(value) if value == int(value) and scale == 1.0 else value


def field(form: dict, name: str, default: str = "") -> str:
    return (form.get(name, [default])[0] or default).strip()


def unique_name(base: str, names: list) -> str:
    if base not in names:
        return base
    i = 2
    while f"{base} {i}" in names:
        i += 1
    return f"{base} {i}"


def new_play(names: list) -> dict:
    return {
        "name": unique_name("new play", names), "enabled": False, "buy": "favorite",
        "min_price": 0.7, "max_price": 0.85, "act_from_minutes_left": 8, "act_until_minutes_left": 7,
        "slippage_cents": 0, "take_profit": None, "stop_loss": None,
        "conditions": {"max_spread_cents": None, "min_contracts_at_price": None,
                       "favorite_rising_seconds": None, "btc_distance_min_dollars": None, "min_score": None},
    }


def deal_play(names: list) -> dict:
    """The backtest's best rule (Nov 9-23 2026): our pick with a score of 7+ in the last 5 minutes
    won about 3 in 4 at about 62c and made money both weeks. Starts on, with pretend money."""
    p = new_play(names)
    p.update(name=unique_name("deal", names) if "deal" in names else "deal", enabled=True, pretend=True,
             buy="pick", min_price=0.01, max_price=0.99, act_from_minutes_left=5, act_until_minutes_left=1,
             size={"mode": "contracts", "value": 1, "max_contracts": 1})
    p["conditions"]["min_score"] = 7
    return p


def size_words(size) -> str:
    v = f"{float(size.value):g}"
    if size.mode == "dollars":
        text = f"${v}"
    elif size.mode == "percent":
        text = f"{v}% of the balance"
    else:
        text = f"{v} contract{'' if v == '1' else 's'}"
    if size.mode != "contracts" and size.max_contracts > 0:
        text += f" (at most {float(size.max_contracts):g} contracts)"
    return text


def describe_play(p, s: Settings) -> str:
    buy = dict(BUY_CHOICES).get(p.buy, p.buy).lower()
    if p.conditions.min_score is not None:
        buy += f" when its score is {float(p.conditions.min_score):g} or more"
    exits = []
    if p.take_profit is not None:
        exits.append(f"sells at {cents_text(p.take_profit)} or more")
    if p.stop_loss is not None:
        exits.append(f"sells if it drops to {cents_text(p.stop_loss)}")
    end = " and ".join(exits) if exits else "holds to the end"
    return (f"Buys {esc(buy)} at {cents_text(p.min_price)}–{cents_text(p.max_price)}, "
            f"from {float(p.act_from_minutes_left):g} to {float(p.act_until_minutes_left):g} minutes "
            f"before each close, bets {esc(size_words(s.size_for(p)))}, then {esc(end)}.")


def tail_log(path: str, lines: int = 25, play: str | None = None) -> list[str]:
    """Newest log lines first, with the time shown as 5:07 pm. Optionally one play's lines."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in reversed(text.splitlines()):
        if len(line) > 20 and line[4] == "-" and line[13] == ":":
            msg, warn = line[20:], False
            for level in ("INFO ", "WARNING ", "ERROR "):
                if msg.startswith(level):
                    msg, warn = msg[len(level):], level != "INFO "
            if play is not None and not msg.replace("pretend money · ", "", 1).startswith(f"{play}:"):
                continue
            hh, mm = int(line[11:13]), line[14:16]
            line = f"{(hh % 12) or 12}:{mm} {'pm' if hh >= 12 else 'am'}  {'⚠️ ' if warn else ''}{msg}"
        elif play is not None:
            continue
        out.append(line)
        if len(out) >= lines:
            break
    return out


# ---------------------------------------------------------------- data
class App:
    """Everything the pages need: where the files are and the private key."""

    def __init__(self, settings_path: str, key: str):
        self.settings_path = settings_path
        self.key = key
        self.updater = update_code
        self.restarter = restart_soon
        self.moved = ""
        self.trade_sent = ""   # "sent to the bot" message for the trade box
        self.flash = ""               # one-time message for the Settings page
        self.secure_link: SecureLink | None = None
        self.key_tester = test_key
        self.history_job: dict = {}           # backtest data download (one at a time)
        self.history_collect = H.collect      # replaced in tests
        self.fetcher = self._fetch_from_kalshi
        self._client = None
        self._lock = threading.Lock()
        self._price_cache: dict = {}
        self._rows_cache: dict = {}   # slot -> (file stamp, rows)

    def _fetch_from_kalshi(self, api_base: str, series: str) -> list:
        if self._client is None or self._client.base_url != api_base.rstrip("/"):
            self._client = KalshiClient(api_base, timeout=4)
        return [Quote.from_market(m, series).as_row() for m in self._client.open_markets(series)]

    def refresh_prices(self, d: dict) -> None:
        """Live prices: the bot's own when fresh, otherwise straight from Kalshi (at most once a second)."""
        age = d.get("market_age")
        if age is not None and age < FRESH_SECONDS:
            return
        fetched = {}
        with self._lock:
            now = time.monotonic()
            for series in d["s"].markets:
                hit = self._price_cache.get(series)
                if hit and now - hit[0] < CACHE_SECONDS:
                    rows = hit[1]
                else:
                    try:
                        rows = self.fetcher(d["s"].api_base, series)
                    except Exception:
                        continue  # keep whatever the bot saved last
                    self._price_cache[series] = (now, rows)
                fetched.update({r["ticker"]: r for r in rows if r.get("ticker")})
        if fetched:
            # keep the bot's own extras (like the last-minute estimate) under Kalshi's fresher prices
            d["market"] = {**d["market"], **{t: {**d["market"].get(t, {}), **r} for t, r in fetched.items()}}
            d["market_age"] = 0.0

    def trades(self, path: str, slot: str = "main") -> list:
        """The trade list, read again only when the file changes."""
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        stamp = (path,) + stamp
        hit = self._rows_cache.get(slot)
        if hit is None or hit[0] != stamp:
            hit = self._rows_cache[slot] = (stamp, L.read_trades(path))
        return hit[1]

    def settings(self) -> tuple[Settings | None, str | None]:
        try:
            return load(self.settings_path), None
        except SettingsError as e:
            return None, str(e)

    def raw(self) -> dict:
        try:
            raw = load_raw(self.settings_path)
        except Exception:  # missing or unreadable file: start from the defaults
            return {}
        return raw if isinstance(raw, dict) else {}

    def snapshot(self) -> dict:
        """One read of everything the status pages show."""
        s, error = self.settings()
        d = {"s": s, "error": error}
        if s is None:
            return d
        folder = mode_folder(s)
        d["folder"] = folder
        d["zone"] = ZoneInfo(s.day_timezone)
        state_path = os.path.join(folder, "state.json")
        try:
            d["state"] = L.Ledger(folder).load_state(s.paper_balance)
            d["age"] = time.time() - os.stat(state_path).st_mtime
        except (OSError, ValueError):
            d["state"], d["age"] = None, None
        d["paused"] = os.path.exists(os.path.join(folder, "STOP"))
        d["rows"] = self.trades(os.path.join(folder, "trades.csv"))
        d["real_rows"] = d["rows"]
        d["pretend_positions"], d["pretend_cash"] = [], None
        pretend = {p.name for p in s.plays if p.pretend} if s.mode == "live" else set()
        d["pretend_names"] = pretend
        if pretend:
            # Real money with some plays on "pretend money only": their bets live in the pretend books.
            paper = mode_folder(s, "paper")
            fake_rows = self.trades(os.path.join(paper, "trades.csv"), slot="paper")
            d["rows"] = [r for r in d["rows"] if r["play"] not in pretend] + \
                        [r for r in fake_rows if r["play"] in pretend]
            try:
                fake = L.Ledger(paper).load_state(s.paper_balance)
                d["pretend_positions"] = [q for q in fake.positions if q["play"] in pretend]
                d["pretend_cash"] = fake.paper_cash
            except (OSError, ValueError):
                pass
        d["market"], d["market_age"] = {}, None
        try:
            with open(os.path.join(folder, "market.json"), encoding="utf-8") as fh:
                m = json.load(fh)
            d["market"] = {r["ticker"]: r for r in m.get("markets", [])}
            updated = parse_time(m.get("updated"))
            if updated:
                d["market_age"] = (datetime.now(timezone.utc) - updated).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.refresh_prices(d)
        # When this changes (a bet opened or closed, settings saved, paused) the page reloads itself.
        try:
            settings_time = os.stat(self.settings_path).st_mtime_ns
        except OSError:
            settings_time = 0
        held = sorted(f'{p["play"]}|{p["ticker"]}' for p in all_positions(d))
        extra = [d["state"].alert, d["state"].notice, d["state"].trade_note] if d["state"] else []
        d["sig"] = hashlib.sha1(json.dumps([settings_time, len(d["rows"]), held, d["paused"], extra])
                                .encode()).hexdigest()[:12]
        return d

    def save_form(self, form: dict) -> tuple[str, int | None]:
        """Apply the settings form and the button that was pressed. Returns (done, play to show)."""
        raw = form_to_raw(form, self.raw())
        plays = raw["plays"]
        names = [p["name"] for p in plays]
        action = field(form, "action", "save")
        focus = None
        done = "saved"
        try:
            if action == "add":
                plays.append(new_play(names))
                focus, done = len(plays) - 1, "added"
            elif action == "add_deal":
                plays.append(deal_play(names))
                focus, done = len(plays) - 1, "deal"
            elif action.startswith("copy:"):
                i = int(action[5:])
                c = copy.deepcopy(plays[i])
                c["name"] = unique_name(f"{plays[i]['name']} copy", names)
                c["enabled"] = False
                plays.insert(i + 1, c)
                focus, done = i + 1, "copied"
            elif action.startswith("delete:"):
                i = int(action[7:])
                if len(plays) == 1:
                    raise SettingsError("you need at least one play, so this one can't be deleted")
                plays.pop(i)
                done = "deleted"
            elif action == "reset_paper":
                if raw.get("mode", "paper") != "paper" and not any(p.get("pretend") for p in plays):
                    raise SettingsError("pretend money can only be reset in paper mode or for pretend-only plays")
                done = "reset"
            elif action != "save":
                raise SettingsError("that button didn't work; please try again")
        except (ValueError, IndexError):
            raise SettingsError("that button didn't work; please reload the page and try again")
        s = save_raw(self.settings_path, raw)
        if done == "reset":
            folder = mode_folder(s, "paper")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, PAPER_RESET_FILE), "w", encoding="utf-8") as fh:
                fh.write(str(s.paper_balance))
        return done, focus

    def set_paused(self, paused: bool) -> None:
        s, _ = self.settings()
        if s is None:
            return
        folder = mode_folder(s)
        os.makedirs(folder, exist_ok=True)
        stop = os.path.join(folder, "STOP")
        if paused:
            with open(stop, "w", encoding="utf-8") as fh:
                when = datetime.now(ZoneInfo(s.day_timezone)).strftime("%b %d %I:%M %p")
                fh.write(f"Pause button tapped on the website ({when})\n")
        elif os.path.exists(stop):
            os.remove(stop)


def form_to_raw(form: dict, current: dict) -> dict:
    """Turn the settings form back into the settings file's layout.

    Keeps everything the form doesn't show (mode, live confirmation, polling,
    folders), so the website can never switch the bot to real money.
    """
    raw = dict(current)
    markets = [m for m, _ in MARKETS if field(form, f"market_{m}")]
    markets += [m.strip().upper() for m in field(form, "market_other").split(",") if m.strip()]
    if not markets:
        raise SettingsError("Markets: tick at least one market")
    raw["markets"] = list(dict.fromkeys(markets))
    raw["paper_balance"] = number(form, "paper_balance", "Pretend starting money")
    raw["balance_share_pct"] = number(form, "balance_share_pct", "Most of your money in open bets")
    raw["size"] = {
        "mode": field(form, "size_mode", "contracts"),
        "value": number(form, "size_value", "Default bet size"),
        "max_contracts": number(form, "max_contracts", "Most contracts per bet"),
    }
    raw["daily_loss_limit"] = {
        "mode": field(form, "loss_mode", "dollars"),
        "value": number(form, "loss_value", "Daily loss limit"),
    }
    raw["max_trades_per_day"] = number(form, "max_trades_per_day", "Most bets per day")
    if "goal_target" in form:
        raw["goal"] = {
            "enabled": bool(field(form, "goal_on")),
            "target": number(form, "goal_target", "Goal"),
            "boldness": field(form, "goal_boldness", "normal"),
            "stop_below": number(form, "goal_stop", "Goal stop", blank_ok=True) or 0,
            "max_bet_pct": number(form, "goal_max", "Biggest bet"),
        }
    if "lm_guard_s" in form:
        raw["last_minute"] = {
            "enabled": bool(field(form, "lm_on")),
            "guard_seconds": number(form, "lm_guard_s", "Guard: seconds before the close"),
            "guard_margin_dollars": number(form, "lm_guard_m", "Guard: dollars against the bet"),
            "follow_seconds": number(form, "lm_follow_s", "Follow: seconds before the close"),
            "follow_margin_dollars": number(form, "lm_follow_m", "Follow: dollars past the target"),
            "follow_max_cents": number(form, "lm_follow_max", "Follow: highest price"),
            "crowd_seconds": number(form, "lm_crowd_s", "Copy the crowd: seconds before the close"),
            "crowd_cents": number(form, "lm_crowd_p", "Copy the crowd: price"),
            "score": bool(field(form, "lm_score")),
        }
    plays = []
    for i in range(int(field(form, "play_count", "0") or 0)):
        p = f"p{i}_"
        name = field(form, p + "name")
        if not name:
            raise SettingsError(f"Play {i + 1}: every play needs a name")
        label = f"Play '{name}'"
        play = {
            "name": name,
            "enabled": bool(field(form, p + "enabled")),
            "pretend": bool(field(form, p + "pretend")),
            "buy": field(form, p + "buy", "favorite"),
            "min_price": number(form, p + "min", f"{label} lowest price", scale=100),
            "max_price": number(form, p + "max", f"{label} highest price", scale=100),
            "slippage_cents": number(form, p + "slip", f"{label} extra cents"),
            "act_from_minutes_left": number(form, p + "from", f"{label} start minute"),
            "act_until_minutes_left": number(form, p + "until", f"{label} stop minute"),
            "take_profit": number(form, p + "tp", f"{label} take profit", blank_ok=True, scale=100),
            "stop_loss": number(form, p + "sl", f"{label} stop loss", blank_ok=True, scale=100),
            "conditions": {
                "max_spread_cents": number(form, p + "spread", f"{label} spread", blank_ok=True),
                "min_contracts_at_price": number(form, p + "depth", f"{label} contracts offered", blank_ok=True),
                "favorite_rising_seconds": number(form, p + "rising", f"{label} rising seconds", blank_ok=True),
                "btc_distance_min_dollars": number(form, p + "dist", f"{label} distance", blank_ok=True),
                "min_score": number(form, p + "minscore", f"{label} lowest score", blank_ok=True),
            },
        }
        size_mode = field(form, p + "size_mode", "default")
        if size_mode in SIZE_MODES:
            play["size"] = {
                "mode": size_mode,
                "value": number(form, p + "size_value", f"{label} bet size"),
                "max_contracts": number(form, p + "size_max", f"{label} most contracts", blank_ok=True) or 0,
            }
        sub = number(form, p + "sub", f"{label} Kalshi subaccount", blank_ok=True)
        if sub:
            play["subaccount"] = sub
        plays.append(play)
    raw["plays"] = plays
    return raw


# ---------------------------------------------------------------- page parts
CSS = """
:root{--bg:#0d0f14;--card:#171a21;--card2:#1f232c;--line:#2a2f3a;--text:#eceef2;--dim:#9aa1ad;--blue:#3d6bff;
--green:#36d37f;--red:#ff6b6b;--amber:#f4b740}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}
main{max-width:720px;margin:0 auto;padding:14px 14px 40px}a{color:inherit}
nav{display:flex;gap:6px;margin-bottom:14px;background:var(--card);padding:5px;border-radius:14px}
nav a{flex:1;text-align:center;padding:9px;border-radius:10px;text-decoration:none;color:var(--dim);font-weight:600}
nav a.on{background:var(--blue);color:#fff}
h1{font-size:24px;margin:2px 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;margin:22px 0 8px;color:var(--dim)}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700;letter-spacing:.03em}
.paper{background:#173626;color:#7ee2a8}.live{background:#4a1f1f;color:#ff9b9b}.on-pill{background:#173626;color:#7ee2a8}
.off-pill{background:#2a2f3a;color:var(--dim)}.old-pill{background:#3a2f1a;color:var(--amber)}
.box{background:var(--card);border-radius:16px;padding:14px;margin:10px 0}
.ok{border-left:4px solid var(--green)}.bad{border-left:4px solid var(--red)}.warn{border-left:4px solid var(--amber)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.card{background:var(--card);border-radius:16px;padding:12px}.card>span{color:var(--dim);font-size:12px;display:block}
.card b{display:block;font-size:20px;margin:2px 0}
.pos{color:var(--green)}.neg{color:var(--red)}.dim,.hint{color:var(--dim)}.hint{font-size:13px;margin:6px 0 0}
.market .top{display:flex;justify-content:space-between;color:var(--dim);font-size:14px}
.prices{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.price{background:var(--card2);border-radius:12px;padding:10px;text-align:center;border:2px solid transparent}
.price.pick{border-color:#2fa86a}.score{position:absolute;top:-12px;left:50%;transform:translateX(-50%);
font-size:15px;font-weight:700;padding:1px 12px;border-radius:999px;white-space:nowrap}
.score sup{font-size:10px;margin-left:1px}.score.good{background:#163a2a;color:#7ee2a8}
.score.ok{background:#3a3216;color:#ffd27a}.score.bad{background:#3d1d22;color:#ff9da4}
.market .prices{margin-top:14px}
.price b{display:block;font-size:24px}.price.fav{border-color:var(--blue)}
.play{display:block;text-decoration:none;background:var(--card);border-radius:16px;padding:14px;margin:10px 0}
.play .head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.play .name{font-size:18px;font-weight:700}.play .desc{color:var(--dim);font-size:14px;margin:6px 0 10px}
.mini{display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:14px}.mini b{font-size:16px}
.bet{display:flex;justify-content:space-between;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)}
.bet:last-of-type{border-bottom:0}.bet .sub{color:var(--dim);font-size:13px;margin-top:2px}
.bet .amt{text-align:right;font-weight:700;white-space:nowrap}.bet .amt .sub{font-weight:400}
.side{display:inline-block;font-size:12px;font-weight:800;padding:2px 8px;border-radius:6px;margin-right:6px}
.side.up{background:#163a2a;color:#7ee2a8}.side.down{background:#3d1d22;color:#ff9da4}
.log{font:12.5px/1.55 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:#c4c9d2}
button,.btn{display:block;text-align:center;text-decoration:none;background:var(--blue);color:#fff;border:0;border-radius:12px;
padding:13px 16px;font-size:16px;font-weight:600;width:100%;margin-top:8px;cursor:pointer}
.btn2{background:var(--card2);color:var(--text)}.danger{background:#3d1d22;color:#ff9da4}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}.row2 button{margin-top:10px}
label{display:block;margin:12px 0 4px;color:#c4c9d2;font-size:14px}
input,select{width:100%;padding:11px;border-radius:10px;border:1px solid #363c48;background:var(--bg);color:var(--text);font-size:16px}
.check{display:flex;gap:10px;align-items:center;margin:12px 0 4px}.check input{width:22px;height:22px;flex:none}
details{margin-top:12px}summary{cursor:pointer;color:#8fb0ff}
.savebar{position:sticky;bottom:0;padding:10px 0 6px;background:linear-gradient(transparent,var(--bg) 35%)}
.livebadge{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--dim);margin-left:auto}
.dot{width:9px;height:9px;border-radius:50%;background:#555}
.dot.on{background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(54,211,127,.6)}70%{box-shadow:0 0 0 7px rgba(54,211,127,0)}100%{box-shadow:0 0 0 0 rgba(54,211,127,0)}}
@keyframes fu{from{background:rgba(54,211,127,.45)}to{background:transparent}}
@keyframes fd{from{background:rgba(255,107,107,.45)}to{background:transparent}}
.flash-up{animation:fu 1.3s ease-out;border-radius:6px}.flash-down{animation:fd 1.3s ease-out;border-radius:6px}
.bar{height:12px;border-radius:99px;background:var(--card2);overflow:hidden;margin:10px 0 4px}
.bar span{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green))}
.empty{color:var(--dim);font-size:14px;padding:6px 0}.back{display:inline-block;color:#8fb0ff;text-decoration:none;margin-bottom:6px}
"""


LIVE_JS = """
(function(){
  var b = document.body, sig = b.dataset.sig, url = b.dataset.live, fails = 0, busy = false, old = {};
  function dot(ok){ var d = document.getElementById('live-dot'); if (d) d.className = ok ? 'dot on' : 'dot'; }
  function remember(flash){
    document.querySelectorAll('[data-k]').forEach(function(e){
      var k = e.dataset.k, v = parseFloat(e.dataset.v);
      if (flash && old[k] !== undefined && !isNaN(v) && v !== old[k]) e.classList.add(v > old[k] ? 'flash-up' : 'flash-down');
      old[k] = v;
    });
  }
  function tick(){
    if (document.hidden || busy) return;
    busy = true;
    fetch(url, {cache: 'no-store', credentials: 'same-origin'})
      .then(function(r){ if (!r.ok) throw r.status; return r.json(); })
      .then(function(j){
        busy = false; fails = 0; dot(true);
        if (j.sig !== sig) { location.reload(); return; }
        for (var id in j.parts) {
          var el = document.getElementById(id);
          if (el && el.innerHTML !== j.parts[id]) el.innerHTML = j.parts[id];
        }
        remember(true);
      })
      .catch(function(){ busy = false; if (++fails > 3) dot(false); });
  }
  remember(false); dot(true);
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', tick);
})();
"""


def page(title: str, body: str, tab: str, refresh=None, live: str | None = None, sig: str = "") -> str:
    """A whole page. Pages with `live` update their numbers every second instead of reloading."""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh and not live else ""
    on = lambda t: ' class="on"' if t == tab else ""
    body_attrs = f' data-live="{esc(live)}" data-sig="{esc(sig)}"' if live else ""
    script = f"<script>{COPY_JS}{LIVE_JS if live else ''}</script>"
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0d0f14">'
            f'{meta}<title>{esc(title)}</title><style>{CSS}</style></head><body{body_attrs}><main>'
            f'<nav><a href="/"{on("status")}>Overview</a><a href="/settings"{on("settings")}>Settings</a></nav>'
            f'{body}</main>{script}</body></html>')


COPY_JS = """
function copyLog(btn){
  var text = document.getElementById(btn.dataset.target).value;
  function done(){ btn.textContent = 'Copied ✓'; setTimeout(function(){ btn.textContent = 'Copy'; }, 2000); }
  function fail(){
    var box = document.getElementById(btn.dataset.target);
    box.hidden = false; box.style.display = 'block'; box.focus(); box.select();
    btn.textContent = 'Press and hold the text to copy';
  }
  function legacy(){
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.left = '0';
    ta.style.opacity = '0'; ta.style.fontSize = '16px';
    document.body.appendChild(ta);
    ta.contentEditable = true; ta.readOnly = false;
    var range = document.createRange(); range.selectNodeContents(ta);
    var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    ta.setSelectionRange(0, 9999999);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    if (ok) { done(); } else { fail(); }
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, legacy);
  } else { legacy(); }
}
"""


def activity_box(title: str, path: str, box_id: str, play: str | None = None) -> str:
    """Recent log lines (newest first) with a Copy button that copies the last 300."""
    shown = tail_log(path, 15, play=play)
    if not shown:
        return ""
    everything = tail_log(path, 300, play=play)
    return (f'<div style="display:flex;justify-content:space-between;align-items:center;margin:22px 0 8px">'
            f'<h2 style="margin:0">{esc(title)}</h2>'
            f'<button type="button" class="btn2" style="width:auto;margin:0;padding:7px 14px;font-size:14px" '
            f'data-target="{box_id}" onclick="copyLog(this)">Copy</button></div>'
            f'<div class="box log">{esc(chr(10).join(shown))}</div>'
            f'<textarea id="{box_id}" hidden readonly rows="12" class="log" style="width:100%;background:var(--bg);'
            f'color:var(--text);border-radius:10px;padding:10px">{esc(chr(10).join(everything))}</textarea>'
            f'<p class="hint">Copy takes the last {len(everything)} lines, newest first.</p>')


LIVE_BADGE = '<span class="livebadge"><span id="live-dot" class="dot"></span>LIVE</span>'


def mode_pill(mode: str) -> str:
    return ('<span class="pill live">REAL MONEY</span>' if mode == "live"
            else '<span class="pill paper">PAPER · pretend money</span>')


def running_box(d: dict) -> str:
    s, age = d["s"], d.get("age")
    if age is None:
        return '<div class="box warn">Starting up. Nothing saved yet; check back in a minute.</div>'
    if age < max(90, s.slow_seconds * 4):
        secs = int(age)
        return f'<div class="box ok">✅ Running · last price check {secs} second{"" if secs == 1 else "s"} ago</div>'
    return f'<div class="box bad">⚠️ Not running · last price check {int(age // 60)} minutes ago</div>'


def countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def side_tag(side_word: str) -> str:
    up = side_word.upper() in ("UP", "YES")
    return f'<span class="side {"up" if up else "down"}">{"▲ UP" if up else "▼ DOWN"}</span>'


def now_bid(pos: dict, market: dict) -> Decimal | None:
    m = market.get(pos["ticker"])
    if not m:
        return None
    if pos["side"] == "yes":
        return Decimal(m["yes_bid"]) if m.get("yes_bid") else None
    return ONE - Decimal(m["yes_ask"]) if m.get("yes_ask") else None


def open_bet(pos: dict, play, d: dict, show_play: bool = False) -> str:
    zone = d["zone"]
    n = Decimal(pos["contracts"])
    paid = Decimal(pos["entry_price"])
    close = parse_time(pos["close_time"])
    mins = (close - datetime.now(timezone.utc)).total_seconds() / 60 if close else None
    when = (f"closes {clock(close, zone)} · {countdown(mins * 60)} left" if mins is not None and mins > 0
            else "closed · waiting for the result")
    if show_play:
        when = f"{pos['play']}{' (pretend)' if pos['play'] in d.get('pretend_names', ()) else ''} · {when}"
    plan = []
    if play is not None and play.take_profit is not None:
        plan.append(f"sells at {cents_text(play.take_profit)}")
    if play is not None and play.stop_loss is not None:
        plan.append(f"or {cents_text(play.stop_loss)}")
    fresh = d.get("market_age") is not None and d["market_age"] < 120
    bid = now_bid(pos, d["market"]) if fresh else None
    if bid is not None and (mins is None or mins > 0):
        change = (bid - paid) * n - Decimal(pos["entry_fee"])
        key = esc(f'{pos["play"]}|{pos["ticker"]}')
        right = (f'<span data-k="now-{key}" data-v="{bid}">now {cents_text(bid)}</span>'
                 f'<div class="sub">{money(change)} if sold</div>')
    else:
        right = '<span class="dim">open</span>'
    sell = ""
    if pos["play"] == MANUAL and (mins is None or mins > 0) and d.get("can_trade"):
        sell = (f'<form method="post" action="/trade" style="margin:6px 0 0">'
                f'<input type="hidden" name="action" value="sell"><input type="hidden" name="side" value="{esc(pos["side"])}">'
                f'<input type="hidden" name="ticker" value="{esc(pos["ticker"])}">'
                f'<button class="btn2" style="padding:6px 10px;font-size:14px" onclick="return confirm(\'Sell this '
                f'bet now at the price buyers are paying?\')">Sell now</button></form>')
    return (f'<div class="bet"><div>{side_tag(pos["side"])}<b>{n:g} × {cents_text(paid)}</b>'
            f'<div class="sub">{esc(when)}{" · " + esc(" ".join(plan)) if plan else ""}</div>{sell}</div>'
            f'<div class="amt">{right}</div></div>')


def closed_bet(r: dict, zone: ZoneInfo) -> str:
    profit = Decimal(r["profit"])
    if r["exit_type"] == "settled":
        how = f"{r['result']} won" if r["result"] else "settled"
    else:
        how = f"sold at {cents_text(r['exit_price'])} ({EXIT_WORDS.get(r['exit_type'], r['exit_type'])})"
    return (f'<div class="bet"><div>{side_tag(r["side"])}<b>{float(r["contracts"]):g} × {cents_text(r["entry_price"])}</b>'
            f'<div class="sub">{esc(how)} · {esc(local_time(r["closed_at"] or r["opened_at"], zone))}</div></div>'
            f'<div class="amt">{money(profit)}</div></div>')


def market_cards(d: dict) -> str:
    age = d.get("market_age")
    if age is None or age > 300 or not d["market"]:
        return ""
    zone = d["zone"]
    now = datetime.now(timezone.utc)
    soonest: dict = {}
    for m in d["market"].values():
        t = parse_time(m.get("close_time"))
        if t is None or t <= now:
            continue
        if m["series"] not in soonest or t < parse_time(soonest[m["series"]]["close_time"]):
            soonest[m["series"]] = m
    names = dict(MARKETS)

    def box(series, word, arrow, p, fav, badge=""):
        value = f' data-k="{esc(series)}-{word}" data-v="{p}"' if p is not None else ""
        return (f'<a href="#trade" onclick="pickSide(\'{"yes" if word == "Up" else "no"}\')" '
                f'class="price{" pick" if badge else " fav" if fav else ""}" '
                f'style="text-decoration:none;color:inherit;position:relative">{badge}{arrow} {word}'
                f'<b{value}>{cents_text(p) if p is not None else "—"}</b></a>')

    out = []
    for series, m in soonest.items():
        t = parse_time(m["close_time"])
        mins = (t - now).total_seconds() / 60
        up = Decimal(m["yes_ask"]) if m.get("yes_ask") else None
        down = ONE - Decimal(m["yes_bid"]) if m.get("yes_bid") else None
        fav_up = up is not None and down is not None and up >= down
        target = f' · target ${float(m["target"]):,.2f}' if m.get("target") else ""
        stale = f' · prices {int(age)}s old' if age > 10 else ""
        badge_up = badge_down = line = ""
        if m.get("score_best") in ("yes", "no"):
            pick_up = m["score_best"] == "yes"
            top = m.get("score_up") if pick_up else m.get("score_down")
            level = int(m.get("score_level") or 1)
            if isinstance(top, int):
                tone = "good" if top >= 7 else "ok" if top >= 5 else "bad"
                badge = f'<span class="score {tone}">{top}<sup>{level}</sup></span>'
                badge_up, badge_down = (badge, "") if pick_up else ("", badge)
                chance = float(m.get("chance_up") or 0.5)
                mine = chance if pick_up else 1 - chance
                price = up if pick_up else down
                side = "Up" if pick_up else "Down"
                kalshi = f"Kalshi says {float(price) * 100:.0f}%" if price is not None else ""
                sure = lastminute.LEVEL_WORDS.get(level, "")
                price_words = ("looks cheap" if top >= 6 else "fair" if top == 5 else "costs more than it's worth")
                what = (f"{side} likely wins ({sure}). Price {price_words}: "
                        f"we think {mine * 100:.0f}%, {kalshi}.")
                line = f'<p class="hint" style="margin-top:8px">{esc(what)}</p>'
        est = ""
        if m.get("est_gap") not in (None, "") and mins * 60 <= 600:
            gap = float(m["est_gap"])
            est = (f'<p class="hint">Last minute: average heading ${abs(gap):,.0f} '
                   f'{"above" if gap >= 0 else "below"} the target · {int(m.get("est_locked") or 0)} of 60 s counted</p>')
        out.append(f'<div class="box market"><div class="top"><span><b style="color:var(--text)">'
                   f'{esc(names.get(series, series))}</b> · closes {clock(t, zone)}</span>'
                   f'<span>{countdown(mins * 60)} left</span></div>'
                   f'<div class="prices">{box(series, "Down", "▼", down, not fav_up, badge_down)}'
                   f'{box(series, "Up", "▲", up, fav_up, badge_up)}</div>'
                   f'{line}<p class="hint" style="margin-top:4px">Price to buy right now{target}{stale}</p>{est}</div>')
    return "".join(out)


def play_names(d: dict) -> list:
    """Plays in the settings first, then old plays that still have bets on record."""
    names = [p.name for p in d["s"].plays]
    held = [p["play"] for p in all_positions(d)]
    return names + [n for n in dict.fromkeys([r["play"] for r in d["rows"]] + held) if n not in names]


def play_link(name: str) -> str:
    return "/play?name=" + quote(name, safe="")


def status_pill(play) -> str:
    if play is None:
        return '<span class="pill old-pill">REMOVED</span>'
    return ON_PILL if play.enabled else OFF_PILL


# ---------------------------------------------------------------- pages
def overview_page(app: App, secure: bool = False) -> str:
    d = app.snapshot()
    s = d["s"]
    if s is None:
        body = (f'<h1>Kalshi bot</h1><div class="box bad"><b>Settings problem:</b> {esc(d["error"])}<br>'
                'Fix it on the Settings page. The bot can\'t start until then.</div>')
        return page("Kalshi bot", body, "status", refresh=60)
    st = d["state"]
    parts = [f"<h1>Kalshi bot {mode_pill(s.mode)}{LIVE_BADGE}</h1>",
             f'<div id="live-running">{running_box(d)}</div>']
    if st is not None and st.alert:
        parts.append(f'<div class="box bad"><b>⚠️ {esc(st.alert)}</b> Open bets still finish.'
                     '<form method="post" action="/resume"><button>Resume betting</button></form></div>')
    elif d["paused"]:
        why = pause_reason(d["folder"])
        parts.append('<div class="box warn"><b>Paused:</b> no new bets. Open bets still finish.'
                     + (f'<p class="hint">Why: {esc(why)}</p>' if why else "") +
                     '<form method="post" action="/resume"><button>Resume betting</button></form></div>')
    if app.moved and s is not None and s.mode == "live":
        d["moved"] = app.moved          # shown inside the Kalshi money box, where the buttons are
        app.moved = ""
    elif app.moved:
        parts.append(f'<div class="box ok">{esc(app.moved)}</div>')
        app.moved = ""
    if not secure and app.secure_link is not None and app.secure_link.ready:
        url = f"{app.secure_link.url()}/?key={app.key}"
        parts.append(f'<div class="box ok">🔒 <b>A locked link is ready.</b> <a href="{esc(url)}" '
                     'style="color:#8fb0ff">Open it</a> and add it to your home screen instead of this one. '
                     '<span class="hint">If it won\'t load, tick "Allow HTTPS traffic" on the server in Google '
                     'Cloud (one time).</span></div>')
    total = L.stats(d.get("real_rows", d["rows"]))
    if st is not None and s.mode == "paper" and Decimal(st.paper_cash) < LOW_MONEY:
        parts.append(f'<div class="box bad"><b>Pretend money is almost gone (${float(st.paper_cash):.2f}),</b> '
                     'so the plays can\'t place new bets. '
                     '<a href="/settings#money" style="color:#8fb0ff">Reset it on the Settings page</a>.</div>')
    if st is not None:
        if s.mode == "paper":
            bal = f"${float(st.paper_cash):.2f}"
        elif st.accounts:
            bal = f"${sum(float(v) for v in st.accounts.values()):.2f}"
        else:
            bal = "Kalshi app"
        parts.append(
            '<div class="cards">'
            f'<div class="card"><span>Balance</span><b>{bal}</b></div>'
            f'<div class="card"><span>Today</span><b>{money(st.day_profit)}</b><span>{bets(st.day_trades)}</span></div>'
            f'<div class="card"><span>All time</span><b>{money(total["profit"])}</b><span>{bets(total["trades"])}</span></div>'
            '</div>')
    parts.append(f'<div id="live-markets">{market_cards(d)}</div>')
    parts.append(trade_box(app, d, secure))
    if all_positions(d):
        parts.append(f'<h2>Open bets · {len(all_positions(d))}</h2>'
                     f'<div class="box" id="live-open">{overview_open_bets(d)}</div>')
    history = G.history(s) if s.goal.enabled else []
    if s.goal.enabled and st is not None:
        parts.append(goal_box(d, s, st))
    parts.append("<h2>Your plays</h2>")
    if d.get("pretend_names"):
        cash = d.get("pretend_cash")
        parts.append(f'<p class="hint">🧪 Pretend-only plays bet with pretend money'
                     f'{f" (${float(cash):.2f} left)" if cash is not None else ""}; their results never count '
                     'toward your real money above.</p>')
    by_name = {p.name: p for p in s.plays}
    positions = all_positions(d)
    for name in play_names(d):
        play = by_name.get(name)
        ps = L.stats([r for r in d["rows"] if r["play"] == name])
        opened = sum(1 for p in positions if p["play"] == name)
        desc = (describe_play(play, s) if play else
                "Trades you made yourself on the website (Make a trade)." if name == MANUAL else
                "Removed or renamed. Its past bets are kept here.")
        if play and s.mode == "live":
            desc += (" 🧪 <b>Pretend money only.</b>" if play.pretend
                     else f" Uses {esc(account_name(play.subaccount))}.")
        if play and play.enabled and s.goal.enabled:
            e = G.edge([r for r in history if r["play"] == name])
            share = G.bet_share(e, s.goal.boldness, s.goal.max_bet_pct)
            desc += f" <b>Goal mode:</b> {esc(G.describe(e, share))}."
        if ps["trades"]:
            mini = (f'<span><b>{ps["trades"]}</b> bet{"" if ps["trades"] == 1 else "s"}</span><span><b>{100 * ps["win_rate"]:.0f}%</b> won</span>'
                    f'<span><b>{money(ps["profit"])}</b></span>')
        else:
            mini = '<span class="dim">No finished bets yet</span>'
        if opened:
            mini += f'<span class="pill on-pill">{opened} open</span>'
        parts.append(f'<a class="play" href="{play_link(name)}"><div class="head"><span class="name">{esc(name)}</span>'
                     f'{status_pill(play)}</div><div class="desc">{desc}</div><div class="mini">{mini}</div></a>')
    parts.append('<a class="btn btn2" href="/settings#add">+ Add or change plays</a>')
    if s.mode == "live":
        parts.append(money_section(d))
    parts.append(watch_section(d))
    parts.append(activity_box("Recent activity", os.path.join(d["folder"], "bot.log"), "log-all"))
    if not d["paused"]:
        parts.append('<form method="post" action="/pause"><button class="btn2" onclick="return confirm('
                     '\'Pause new bets? Open bets still finish.\')">Pause new bets</button></form>')
    parts.append(f'<h2>Bot version</h2><div class="box">Version {esc(code_version())}'
                 '<form method="post" action="/update"><button class="btn2">Get the newest version</button></form>'
                 '<p class="hint">Only needed when Claude tells you there\'s an update. Takes about a minute. '
                 'Open bets are kept.</p></div>'
                 '<p class="hint">Numbers update live while this page is open.</p>')
    return page("Kalshi bot", "".join(parts), "status", live="/api/live", sig=d["sig"])


def pause_reason(folder: str) -> str:
    try:
        with open(os.path.join(folder, "STOP"), encoding="utf-8") as fh:
            return fh.read().strip().splitlines()[0][:300]
    except (OSError, IndexError):
        return ""


def money_section(d: dict) -> str:
    """Real money: cash in each Kalshi subaccount, and a form to move money between them."""
    s, st = d["s"], d["state"]
    accounts = {int(k): v for k, v in (st.accounts if st else {}).items()}
    for p in s.plays:
        if p.enabled and not p.pretend:
            accounts.setdefault(p.subaccount, None)
    shards = (st.shards if st else {}) or {}
    rows = ""
    for n in sorted(accounts):
        names = ", ".join(p.name for p in s.plays if p.enabled and not p.pretend and p.subaccount == n)
        cash = accounts[n]
        pots = shards.get(str(n)) or {}
        crypto = pots.get("2")
        where = ""
        if cash is not None and pots:
            others = " · ".join(f"{pot_name(int(k))} ${float(v):.2f}" for k, v in sorted(pots.items())
                                if k != "2" and float(v) >= 0.01)
            where = (f' · <b>${float(crypto or 0):.2f}</b> in the crypto pot'
                     + (" ⚠️" if float(crypto or 0) < 1 and float(cash) >= 1 else "")
                     + (f'<br><span class="dim">bot can\'t use: {esc(others)}</span>' if others else ""))
        rows += (f'<div class="bet"><div><b>{esc(account_name(n).capitalize())}</b>'
                 f'<div class="sub">{esc(names) or "no play uses it"}{where}</div></div>'
                 f'<div class="amt">{"$%.2f" % float(cash) if cash is not None else "not set up yet"}</div></div>')
    pots_box = ""
    if st is not None and shards:
        elsewhere = sum((float(v) for k, v in (shards.get("0") or {}).items() if k != "2"), 0.0)
        move = (f'<button name="pot" value="move">Move ${elsewhere:.2f} into the crypto pot</button>'
                if elsewhere >= 0.01 else "")
        pots_box = ('<p class="hint">Kalshi splits your cash into pots by kind of market. The bot can only bet '
                    'with what\'s in the <b>crypto</b> pot. Your own bets in the app move money for you; '
                    'bot bets don\'t. Example: $8 total but $0 in crypto = the bot can\'t buy.</p>'
                    f'<form method="post" action="/pots">{move}'
                    '<details><summary>Let Kalshi keep it there by itself</summary>'
                    '<div style="display:flex;gap:8px;flex-wrap:wrap">'
                    '<button class="btn2" name="pot" value="2">Keep my money in the crypto pot</button>'
                    '<button class="btn2" name="pot" value="off" style="opacity:.7">Stop</button></div>'
                    '<p class="hint">Kalshi took this setting before but didn\'t move anything.</p>'
                    '</details></form>')
    numbers = sorted(accounts)
    first_sub = next((n for n in numbers if n != 0), 0)
    choices = "".join(f'<option value="{n}">{esc(account_name(n))}</option>' for n in numbers)
    to_choices = "".join(f'<option value="{n}"{" selected" if n == first_sub else ""}>{esc(account_name(n))}</option>'
                         for n in numbers)
    notice = f'<p class="hint">Last change: {esc(st.notice)}</p>' if st and st.notice else ""
    if d.get("moved"):
        bad = d["moved"].startswith("Not ")
        notice = f'<div class="box {"bad" if bad else "ok"}" style="margin:8px 0">{esc(d["moved"])}</div>' + notice
    if st and st.setup_note:
        notice = (f'<div class="box warn" style="margin:8px 0">{esc(st.setup_note)}'
                  '<form method="post" action="/setup"><button class="btn2">Try setting up again now</button>'
                  '</form></div>' + notice)
    made = sorted(int(n) for n in (st.crypto_subaccounts if st else []))
    make_box = ('<form method="post" action="/subaccount"><button class="btn2">Make a subaccount in the crypto pot'
                '</button></form><p class="hint">Each play that\'s on needs its own subaccount inside the crypto '
                'pot. Easiest: on Settings give the play a new number (like 2 or 3) and save; the bot makes it in '
                'the crypto pot by itself. Then move money into it here.'
                + (f' Made so far: {", ".join(map(str, made))}.' if made else "") + '</p>')
    pots_box += make_box
    if numbers == [0]:   # nothing to move between yet
        return ('<h2 id="money">Kalshi money</h2><div class="box">' + rows + notice + pots_box +
                '<p class="hint">Every play that\'s on uses your main account, so there\'s nothing to move '
                'between accounts.</p></div>')
    return ('<h2 id="money">Kalshi money</h2><div class="box">' + rows + notice + pots_box +
            '<form method="post" action="/transfer">'
            f'<div class="row2"><div><label>Move from</label><select name="from">{choices}</select></div>'
            f'<div><label>To</label><select name="to">{to_choices}</select></div></div>'
            '<label>Amount ($)</label><input name="amount" inputmode="decimal" placeholder="5">'
            '<button class="btn2">Move money</button></form>'
            '<p class="hint">Each play bets only with the money in its own subaccount. Moves stay inside your '
            'Kalshi account. The Kalshi app shows only your main account.</p></div>')


def goal_money(s: Settings, st) -> Decimal | None:
    """Same count as the bot: cash plus what open bets cost (real money: only the plays' subaccounts)."""
    opened = st.open_positions()
    if s.mode == "paper":
        return Decimal(st.paper_cash) + sum((p.cost for p in opened), Decimal("0"))
    if not st.accounts:
        return None
    accts = {p.subaccount for p in s.plays if p.enabled and not p.pretend}
    cash = sum((Decimal(v) for k, v in st.accounts.items() if int(k) in accts), Decimal("0"))
    return cash + sum((p.cost for p in opened if p.subaccount in accts), Decimal("0"))


def goal_box(d: dict, s: Settings, st) -> str:
    g = s.goal
    now = goal_money(s, st)
    start = Decimal(st.goal_start) if st.goal_start else now
    out = ['<h2>Goal</h2><div class="box">']
    if now is None or start is None:
        out.append(f"Aiming for ${g.target:.2f}. Waiting for the first balance check…</div>")
        return "".join(out)
    span = g.target - start
    done = 0 if span <= 0 else max(0, min(100, float((now - start) / span * 100)))
    out.append(f'<div style="display:flex;justify-content:space-between"><b>${start:.2f} → ${g.target:.2f}</b>'
               f'<span>now <b>${now:.2f}</b></span></div>'
               f'<div class="bar"><span style="width:{done:.0f}%"></span></div>'
               f'<p class="hint">{done:.0f}% of the way · {esc(g.boldness)} sizing · biggest bet '
               f'{float(g.max_bet_pct):g}% of the money'
               + (f" · stops below ${g.stop_below:.2f}" if g.stop_below > 0 else "") + "</p>")
    if st.goal_note:
        good = st.goal_note.startswith("Goal reached")
        out.append(f'<div class="box {"ok" if good else "bad"}" style="margin:8px 0 0">'
                   f'{"🎉 " if good else ""}{esc(st.goal_note)}</div>')
    out.append('<p class="hint">Each play bets 1 contract until its record proves an edge (30+ bets, '
               'counted cautiously). Then its bets grow with the money and shrink after losses.</p></div>')
    return "".join(out)


def trade_box(app: "App", d: dict, secure: bool) -> str:
    """Buy right now from the website (the bot places it, so its records stay right)."""
    s, st = d["s"], d.get("state")
    real = s.mode == "live"
    d["can_trade"] = secure or not real
    out = ['<h2 id="trade">Make a trade</h2><div class="box">']
    if app.trade_sent:
        out.append(f'<div class="box ok" style="margin:0 0 10px">{esc(app.trade_sent)}</div>')
        app.trade_sent = ""
    if st is not None and st.trade_note:
        bad = st.trade_note.split(": ", 1)[-1].startswith(("not ", "didn't"))
        out.append(f'<p class="{"bad" if bad else "hint"}" style="margin-top:0">Last trade: {esc(st.trade_note)}</p>')
    if not d["can_trade"]:
        link = app.secure_link
        where = (f' <a href="{esc(link.url())}/?key={esc(app.key)}#trade" style="color:#8fb0ff">Open it</a>.'
                 if link is not None and link.ready else "")
        out.append(f'<p class="hint">Trading with real money works only on the 🔒 locked link.{where}</p></div>')
        return "".join(out)
    names = dict(MARKETS)
    pick = "".join(f'<option value="{esc(m)}">{esc(names.get(m, m))}</option>' for m in s.markets)
    market = (f'<label>Market</label><select name="series">{pick}</select>' if len(s.markets) > 1
              else f'<input type="hidden" name="series" value="{esc(s.markets[0])}">')
    money = "real money from your main account\'s crypto pot" if real else "pretend money"
    ask = (f' onclick="return confirm(\'Buy with REAL money now?\')"' if real else "")
    out.append(
        '<form method="post" action="/trade"><input type="hidden" name="action" value="buy">'
        f'<input type="hidden" name="side" id="trade-side" value="yes">{market}'
        '<div class="row2"><div><label>Contracts</label>'
        '<input name="contracts" value="1" inputmode="numeric"></div>'
        '<div><label>Most I\'ll pay (¢)</label><input name="max" inputmode="decimal" placeholder="price + 2"></div></div>'
        '<div class="row2">'
        f'<button name="side" value="no"{ask} style="background:#7a2a33">▼ Buy Down</button>'
        f'<button name="side" value="yes"{ask}>▲ Buy Up</button></div>'
        f'<p class="hint">Buys right away in the market closing next, at the price on screen (up to 2¢ more, or '
        f'your limit), with {money}. Your trades show up as the play "{MANUAL}"; each open one has a Sell now '
        'button.</p></form></div>'
        '<script>function pickSide(v){var e=document.getElementById("trade-side");if(e)e.value=v;}</script>')
    return "".join(out)


def all_positions(d: dict) -> list:
    """Open bets: real ones first, then pretend-only plays' (when the bot uses real money)."""
    st = d.get("state")
    return (st.positions if st is not None else []) + d.get("pretend_positions", [])


def overview_open_bets(d: dict) -> str:
    by_name = {p.name: p for p in d["s"].plays}
    return "".join(open_bet(p, by_name.get(p["play"]), d, show_play=True) for p in all_positions(d))


def play_open_bets(d: dict, name: str, play) -> str:
    positions = [p for p in all_positions(d) if p["play"] == name]
    return "".join(open_bet(p, play, d) for p in positions) or '<div class="empty">None right now.</div>'


def live_parts(app: App, name: str | None, secure: bool = False) -> dict:
    """The parts of a page that change every second, for /api/live."""
    d = app.snapshot()
    if d["s"] is None:
        return {"sig": "settings-problem", "parts": {}}
    d["can_trade"] = secure or d["s"].mode != "live"
    parts = {"live-running": running_box(d), "live-markets": market_cards(d)}
    if name is None:
        if all_positions(d):
            parts["live-open"] = overview_open_bets(d)
    else:
        play = {p.name: p for p in d["s"].plays}.get(name)
        parts["live-open"] = play_open_bets(d, name, play)
    return {"sig": d["sig"], "parts": parts}


def play_page(app: App, name: str, secure: bool = False) -> str:
    d = app.snapshot()
    s = d["s"]
    if s is not None:
        d["can_trade"] = secure or s.mode != "live"
    if s is None:
        return overview_page(app)
    by_name = {p.name: p for p in s.plays}
    play = by_name.get(name)
    if play is None and name not in play_names(d):
        return page("Not found", '<a class="back" href="/">‹ All plays</a><div class="box">No play called '
                    f'"{esc(name)}".</div>', "status")
    zone = d["zone"]
    rows = [r for r in d["rows"] if r["play"] == name]
    ps = L.stats(rows)
    parts = ['<a class="back" href="/">‹ All plays</a>', f"<h1>{esc(name)} {status_pill(play)}{LIVE_BADGE}</h1>"]
    if play is not None:
        parts.append(f'<p class="dim" style="margin-top:0">{describe_play(play, s)}</p>')
    else:
        parts.append('<p class="dim">This play was removed or renamed. Its past bets are kept here.</p>')
    parts.append(
        '<div class="cards">'
        f'<div class="card"><span>Bets</span><b>{ps["trades"]}</b><span>{ps["wins"]} won · {ps["losses"]} lost</span></div>'
        f'<div class="card"><span>Won</span><b>{100 * ps["win_rate"]:.0f}%</b><span>needs ~{100 * ps["need"]:.0f}%</span></div>'
        f'<div class="card"><span>Profit</span><b>{money(ps["profit"])}</b><span>after fees</span></div>'
        '</div>')
    if ps["trades"]:
        verdict = ps["verdict"].replace("Verdict: ", "")
        parts.append(f'<div class="box"><b>{esc(verdict[:1].upper() + verdict[1:])}</b>'
                     f'<p class="hint">"Needs" is the win rate this play must beat to make money. Luck can move a '
                     f'win rate about ±{100 * ps["luck"]:.0f} points at {bets(ps["trades"])}.</p></div>')
    positions = [p for p in all_positions(d) if p["play"] == name]
    parts.append(f'<h2>Open bets · {len(positions)}</h2><div class="box" id="live-open">'
                 f'{play_open_bets(d, name, play)}</div>')
    parts.append(f'<h2>Closed bets · {len(rows)}</h2><div class="box">')
    parts.append("".join(closed_bet(r, zone) for r in reversed(rows[-60:])) or '<div class="empty">None yet.</div>')
    if len(rows) > 60:
        parts.append(f'<p class="hint">Showing the newest 60 of {len(rows)}.</p>')
    book = "?book=pretend" if name in d.get("pretend_names", ()) else ""
    parts.append(f'<p class="hint"><a href="/trades.csv{book}" style="color:#8fb0ff">Download every bet</a></p></div>')
    parts.append(activity_box("What it did lately", os.path.join(d["folder"], "bot.log"), "log-play", play=name))
    if play is not None:
        parts.append(f'<a class="btn btn2" href="/settings#play-{s.plays.index(play)}">Edit this play</a>')
    return page(name, "".join(parts), "status", live="/api/live?play=" + quote(name, safe=""), sig=d["sig"])


def play_form(i: int, p: dict, opt, inp) -> str:
    c = p.get("conditions") or {}
    psize = p.get("size") if isinstance(p.get("size"), dict) else None
    pre = f"p{i}_"
    name = p.get("name", f"play{i + 1}")
    on = p.get("enabled", True)
    size_mode = psize.get("mode") if psize else "default"
    size_value = plain(psize.get("value")) if psize else ""
    size_max = plain(psize.get("max_contracts") or None) if psize else ""
    confirm = esc(f"Delete the play {name}? Its past bets are kept.")
    return (
        f'<h2 id="play-{i}">Play {i + 1}</h2><div class="box">'
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<b style="font-size:18px">{esc(name)}</b>{ON_PILL if on else OFF_PILL}</div>'
        f'<label>Name</label><input name="{pre}name" value="{esc(name)}" maxlength="40">'
        f'<div class="check"><input type="checkbox" name="{pre}enabled" value="1"'
        f'{" checked" if on else ""}><span>This play is on</span></div>'
        f'<div class="check"><input type="checkbox" name="{pre}pretend" value="1"'
        f'{" checked" if p.get("pretend") else ""}><span>🧪 Pretend money only (even when the bot uses real '
        'money)</span></div>'
        f'<label>What it buys</label><select name="{pre}buy">{opt(BUY_CHOICES, p.get("buy", "favorite"))}</select>'
        f'<div class="row2"><div><label>Lowest price (¢)</label>{inp(pre + "min", cents(p.get("min_price", 0.7)))}</div>'
        f'<div><label>Highest price (¢)</label>{inp(pre + "max", cents(p.get("max_price", 0.85)))}</div></div>'
        f'<div class="row2"><div><label>Start (minutes left)</label>{inp(pre + "from", plain(p.get("act_from_minutes_left", 8)))}</div>'
        f'<div><label>Stop (minutes left)</label>{inp(pre + "until", plain(p.get("act_until_minutes_left", 7)))}</div></div>'
        '<p class="hint">Example: 8 and 7 means it buys between 8 and 7 minutes before the market closes.</p>'
        f'<div class="row2"><div><label>Take profit at (¢)</label>{inp(pre + "tp", cents(p.get("take_profit")))}</div>'
        f'<div><label>Stop loss at (¢)</label>{inp(pre + "sl", cents(p.get("stop_loss")))}</div></div>'
        '<p class="hint">Blank = off. Both blank = hold to the end and get $1 per contract if it wins.</p>'
        f'<label>Bet size</label><select name="{pre}size_mode">'
        f'{opt([("default", "Same as the default size")] + SIZE_CHOICES, size_mode)}</select>'
        f'<div class="row2"><div><label>Amount</label>{inp(pre + "size_value", size_value)}</div>'
        f'<div><label>Most contracts</label>{inp(pre + "size_max", size_max, "numeric")}</div></div>'
        '<p class="hint">Example: Dollars and 3 means it spends about $3 on each bet. Most contracts blank = no cap.</p>'
        f'<label>Kalshi subaccount (real money only)</label>{inp(pre + "sub", plain(p.get("subaccount") or 0), "numeric")}'
        '<p class="hint">0 = your main account. With real money, give each play that\'s on its own number '
        '(1, 2, 3…), so their bets can\'t cancel each other. The bot makes them on Kalshi.</p>'
        '<details><summary>Extra conditions</summary>'
        f'<label>Extra cents it may pay above the price on screen</label>{inp(pre + "slip", plain(p.get("slippage_cents", 0)))}'
        f'<label>Skip if the gap between buy and sell price is wider than (¢)</label>{inp(pre + "spread", plain(c.get("max_spread_cents")))}'
        f'<label>Skip if fewer contracts are offered than</label>{inp(pre + "depth", plain(c.get("min_contracts_at_price")))}'
        f'<label>Only buy if its side rose over the last N seconds</label>{inp(pre + "rising", plain(c.get("favorite_rising_seconds")))}'
        f'<label>Only buy if the price is this many $ past the target</label>{inp(pre + "dist", plain(c.get("btc_distance_min_dollars")))}'
        f'<label>Only buy if its live score is at least (1-10)</label>{inp(pre + "minscore", plain(c.get("min_score")), "numeric")}'
        '<p class="hint">Blank = off. In the last few minutes the "$ past the target" check uses the bot\'s '
        'running average (what Kalshi settles on) instead of Bitcoin\'s live price. Example: 25 for a last-minute '
        'play skips close calls, where the average is within $25 of the target. Live score 7 = only buy when '
        'that side looks cheap for its chance (the big number on the price card).</p></details>'
        f'<div class="row2"><button class="btn2" name="action" value="copy:{i}">Copy this play</button>'
        f'<button class="danger" name="action" value="delete:{i}" onclick="return confirm(this.dataset.q)" '
        f'data-q="{confirm}">Delete</button></div>'
        '<p class="hint">Renaming a play starts its score over; its old bets stay under the old name.</p></div>')


def settings_page(app: App, raw: dict | None = None, message: str | None = None, error: str | None = None,
                  secure: bool = False) -> str:
    raw = raw if raw is not None else app.raw()
    size = raw.get("size") or {}
    loss = raw.get("daily_loss_limit") or {}
    markets = raw.get("markets") or ["KXBTC15M"]
    known = [m for m, _ in MARKETS]
    other = ", ".join(m for m in markets if m not in known)

    def opt(options, current):
        return "".join(f'<option value="{v}"{" selected" if v == current else ""}>{esc(t)}</option>'
                       for v, t in options)

    def inp(name, value, mode="decimal"):
        return f'<input name="{name}" value="{esc(value)}" inputmode="{mode}">'

    out = ["<h1>Settings</h1>"]
    if message:
        out.append(f'<div class="box ok">{esc(message)}</div>')
    if error:
        out.append(f'<div class="box bad"><b>Not saved:</b> {esc(error)}</div>')
    if app.flash:
        out.append(f'<div class="box {"bad" if app.flash.startswith(("Not", "Key not")) else "ok"}">'
                   f'{esc(app.flash)}</div>')
        app.flash = ""
    out.append(real_money_section(app, raw, secure))
    out.append(history_section(app))
    # The hidden first button makes Enter in a box save, instead of pressing the first Copy button.
    out.append('<form method="post" action="/settings">'
               '<button name="action" value="save" tabindex="-1" aria-hidden="true" '
               'style="position:absolute;left:-9999px;width:1px">Save</button>')
    plays = raw.get("plays") or []
    out.append(f'<input type="hidden" name="play_count" value="{len(plays)}">')
    out.extend(play_form(i, p, opt, inp) for i, p in enumerate(plays))
    out.append('<div id="add"></div><button class="btn2" name="action" value="add">+ Add a play</button>')
    if not any(p.get("buy") == "pick" for p in plays):
        out.append('<button class="btn2" name="action" value="add_deal" style="margin-top:8px">'
                   '+ Add the deal play (pretend money)</button><p class="hint">Buys our pick when its score is '
                   '7 or more, 5 to 1 minutes before the close, 1 contract. The best rule in the backtest.</p>')
    out.append(
        "<h2>Markets</h2><div class=\"box\">" + "".join(
            f'<div class="check"><input type="checkbox" name="market_{m}" value="1"'
            f'{" checked" if m in markets else ""}><span>{esc(n)} 15-minute</span></div>'
            for m, n in MARKETS) + f'<label>Other series (comma separated)</label>{inp("market_other", other, "text")}</div>')
    reset = ""
    if raw.get("mode", "paper") == "paper" or any(p.get("pretend") for p in raw.get("plays") or []):
        try:
            s = load(app.settings_path)
            now_cash = L.Ledger(mode_folder(s, "paper")).load_state(s.paper_balance).paper_cash
            reset = f'<p class="hint">Pretend money right now: ${float(now_cash):.2f}</p>'
        except (SettingsError, OSError, ValueError):
            pass
        reset += ('<button class="btn2" name="action" value="reset_paper" onclick="return confirm(this.dataset.q)" '
                  'data-q="Set the pretend money to the amount above? Past bets are kept.">'
                  'Reset pretend money to this amount</button>')
    out.append(
        goal_form(raw.get("goal") or {}, opt, inp) + watch_form(raw.get("last_minute") or {}, inp) +
        '<h2 id="money">Money and limits (all plays)</h2><div class="box">'
        f'<label>Pretend starting money ($, paper mode)</label>{inp("paper_balance", plain(raw.get("paper_balance", 11)))}'
        f'{reset}'
        f'<label>Most of your money in open bets at once (%)</label>{inp("balance_share_pct", plain(raw.get("balance_share_pct", 50)))}'
        f'<div class="row2"><div><label>Default bet size</label><select name="size_mode">'
        f'{opt(SIZE_CHOICES, size.get("mode", "contracts"))}</select></div>'
        f'<div><label>Amount</label>{inp("size_value", plain(size.get("value", 1)))}</div></div>'
        f'<label>Most contracts per bet (0 = no cap)</label>{inp("max_contracts", plain(size.get("max_contracts", 1)), "numeric")}'
        '<p class="hint">Plays set to "Same as the default size" use these.</p>'
        f'<div class="row2"><div><label>Daily loss limit in</label><select name="loss_mode">'
        f'{opt(LOSS_CHOICES, loss.get("mode", "dollars"))}</select></div>'
        f'<div><label>Stop after losing</label>{inp("loss_value", plain(loss.get("value", 5)))}</div></div>'
        f'<label>Most bets per day (0 = no cap)</label>{inp("max_trades_per_day", plain(raw.get("max_trades_per_day", 0)), "numeric")}'
        "</div>")
    out.append('<div class="savebar"><button name="action" value="save">Save settings</button></div>'
               '<p class="hint">The bot restarts with the new settings in about 15 seconds. Open bets are kept.</p></form>')
    return page("Settings", "".join(out), "settings")


def test_key(key_id: str, pem: bytes, api_base: str):
    """Ask Kalshi for the main account balance with this key. Returns dollars."""
    from cryptography.hazmat.primitives import serialization
    private_key = serialization.load_pem_private_key(pem, password=None)
    return KalshiClient(api_base, key_id, private_key, timeout=8).balance_dollars()


def save_kalshi_key(app: App, form: dict) -> str:
    try:
        key_id, pem = keystore.check_key(field(form, "key_id"), (form.get("pem") or [""])[0])
    except keystore.KeyError_ as e:
        return f"Key not saved: {e}"
    s, _ = app.settings()
    api_base = s.api_base if s else "https://external-api.kalshi.com/trade-api/v2"
    try:
        balance = app.key_tester(key_id, pem, api_base)
    except KalshiError as e:
        if e.status in (401, 403):
            return ("Key not saved: Kalshi didn't accept it. Check the Key ID matches the private key "
                    "(make a new key on Kalshi if unsure).")
        keystore.save_key(key_id, pem)
        return f"Key saved, but Kalshi gave an error when testing it ({e.status}). Try again later."
    except Exception as e:
        keystore.save_key(key_id, pem)
        return f"Key saved, but Kalshi couldn't be reached to test it ({e.__class__.__name__})."
    keystore.save_key(key_id, pem)
    return f"Key saved and it works: your main Kalshi account has ${balance:.2f}."


def switch_mode(app: App, mode: str, confirm: str = "") -> str:
    raw = app.raw()
    if mode == "live":
        if confirm.strip() != LIVE_CONFIRM_TEXT:
            return f'Not switched: type exactly "{LIVE_CONFIRM_TEXT}".'
        if keystore.load_key()[0] is None:
            return "Not switched: add your Kalshi key first."
        raw["live_confirm"] = LIVE_CONFIRM_TEXT
    raw["mode"] = mode
    try:
        save_raw(app.settings_path, raw)
    except SettingsError as e:
        return f"Not switched: {e}"
    return ("Switched to REAL MONEY. The bot restarts in about 15 seconds." if mode == "live" else
            "Switched to pretend money. The bot restarts in about 15 seconds.")


def goal_form(g: dict, opt, inp) -> str:
    choices = [("careful", "Careful: ¼ of the fastest-growth bet"), ("normal", "Normal: ½ of it"),
               ("bold", "Bold: all of it (big swings)")]
    return (
        '<h2 id="goal">Goal mode</h2><div class="box">'
        f'<div class="check"><input type="checkbox" name="goal_on" value="1"{" checked" if g.get("enabled") else ""}>'
        '<span>Size bets automatically to reach a goal</span></div>'
        f'<div class="row2"><div><label>Goal ($)</label>{inp("goal_target", plain(g.get("target", 100)))}</div>'
        f'<div><label>Stop if money falls below ($)</label>{inp("goal_stop", plain(g.get("stop_below") or 0))}</div></div>'
        f'<label>How bold</label><select name="goal_boldness">{opt(choices, g.get("boldness", "normal"))}</select>'
        f'<label>Biggest bet (% of the money)</label>{inp("goal_max", plain(g.get("max_bet_pct", 25)))}'
        '<p class="hint">Goal mode replaces the bet sizes above. Each play bets 1 contract until its record proves '
        'an edge, then bets the share of its money that grows it fastest for that edge, and pauses at the goal. '
        'It can\'t make a play without an edge win; it only keeps a good play from betting too little or too much. '
        '0 = no stop.</p></div>')


def watch_form(w: dict, inp) -> str:
    on = w.get("enabled", True)
    return (
        '<h2 id="lastminute">Last-minute watch</h2><div class="box">'
        f'<div class="check"><input type="checkbox" name="lm_on" value="1"{" checked" if on else ""}>'
        '<span>Watch the last minute (never sends orders)</span></div>'
        f'<div class="check"><input type="checkbox" name="lm_score" value="1"{" checked" if w.get("score", True) else ""}>'
        '<span>Show a live 1-10 score for Up and Down</span></div>'
        '<p class="hint">Kalshi decides Up or Down by Bitcoin\'s average over the final 60 seconds. The bot keeps '
        'its own running average and writes down what two ideas would have done, then checks them after the '
        'close. Nothing here buys or sells.</p>'
        '<label><b>Guard</b>: sell a bet that\'s turning against you</label>'
        f'<div class="row2"><div><label>Seconds before the close</label>{inp("lm_guard_s", plain(w.get("guard_seconds", 90)))}</div>'
        f'<div><label>When the average is this many $ against it</label>{inp("lm_guard_m", plain(w.get("guard_margin_dollars", 20)))}</div></div>'
        '<label><b>Follow</b>: buy the side the average says is winning</label>'
        f'<div class="row2"><div><label>Seconds before the close</label>{inp("lm_follow_s", plain(w.get("follow_seconds", 60)))}</div>'
        f'<div><label>When it\'s this many $ past the target</label>{inp("lm_follow_m", plain(w.get("follow_margin_dollars", 30)))}</div></div>'
        f'<label>Follow only if that side costs at most (¢)</label>{inp("lm_follow_max", plain(w.get("follow_max_cents", 85)))}'
        '<label><b>Copy the crowd</b>: sell when everyone else is selling your side</label>'
        f'<div class="row2"><div><label>Seconds before the close</label>{inp("lm_crowd_s", plain(w.get("crowd_seconds", 90)))}</div>'
        f'<div><label>When your bet\'s price falls to (¢)</label>{inp("lm_crowd_p", plain(w.get("crowd_cents", 50)))}</div></div>'
        '<p class="hint">Example: Guard 90 and 20 = with 1:30 or less left, if the average is heading $20 or more to '
        'the wrong side of your bet, note "would sell now". Follow 60, 30 and 85 = with a minute or less left, if '
        'the average is $30 or more past the target and that side still costs 85¢ or less, note "would buy". '
        'Copy the crowd 90 and 50 = with 1:30 or less left, if your Up bet (bought at 80¢) falls to 50¢ because '
        'others are selling, note "would sell now". Guard uses our own average; Copy the crowd uses Kalshi\'s '
        'price, which is where the fast bots leave their footprints.</p>'
        '</div>')


def watch_section(d: dict) -> str:
    """Overview: how the watched ideas would have done so far."""
    s = d["s"]
    if s is None or not s.watch.enabled:
        return ""
    rows = lastminute.read_watch(os.path.join(d["folder"], "watch.csv"))
    sm = lastminute.summary(rows)
    g, c, f = sm["guard"], sm["crowd"], sm["follow"]

    def money(x):
        return f"${abs(x):.2f}"
    out = ['<h2 id="watch">Last-minute watch · no orders</h2><div class="box">']
    if not rows:
        out.append('<div class="empty">Nothing yet. It fills in as bets reach their last minute.</div>')
    if g["count"]:
        word = "saved" if g["difference"] >= 0 else "cost"
        out.append(f'<div class="bet"><div><b>Guard</b><div class="sub">{g["count"]} times it would have sold early; '
                   f'it would have helped {g["helped"]} of them</div></div>'
                   f'<div class="amt {"pos" if g["difference"] >= 0 else "neg"}">{word} {money(g["difference"])}</div></div>')
    if c["count"]:
        word = "saved" if c["difference"] >= 0 else "cost"
        out.append(f'<div class="bet"><div><b>Copy the crowd</b><div class="sub">{c["count"]} times it would have '
                   f'sold early; it would have helped {c["helped"]} of them</div></div>'
                   f'<div class="amt {"pos" if c["difference"] >= 0 else "neg"}">{word} {money(c["difference"])}</div></div>')
    if f["count"]:
        out.append(f'<div class="bet"><div><b>Follow</b><div class="sub">{f["count"]} buys it would have made '
                   f'(1 contract each); {f["wins"]} won</div></div>'
                   f'<div class="amt {"pos" if f["would"] >= 0 else "neg"}">'
                   f'{"+" if f["would"] >= 0 else "-"}{money(f["would"])}</div></div>')
    srows = lastminute.read_watch(os.path.join(d["folder"], "scores.csv"))
    if srows:
        ss = lastminute.score_summary(srows)
        parts_ = [f"{label}: won {w} of {n}" for label, (w, n) in ss.items() if n]
        out.append('<div class="bet"><div><b>Score track record</b><div class="sub">When the best side scored '
                   + esc(" · ".join(parts_) or "no results yet") + '</div></div></div>')
    out.append('<p class="hint">Compared with what really happened, after fees. Change the numbers on Settings '
               '(Last-minute watch). <a href="/watch.csv" style="color:#8fb0ff">Download the results</a> · '
               'second-by-second data: <a href="/lastminute.csv" style="color:#8fb0ff">today</a> · '
               '<a href="/lastminute.csv?days=3" style="color:#8fb0ff">last 3 days</a> · '
               '<a href="/scores.csv" style="color:#8fb0ff">scores</a></p></div>')
    return "".join(out)


def start_history(app: App, days: int, series: list | None = None) -> str:
    """Download past markets for backtests in the background (website stays usable)."""
    job = app.history_job
    if job.get("state") == "running":
        return "Already collecting; wait for it to finish."
    s, _ = app.settings()
    if s is None:
        return "Fix the settings first."
    folder = mode_folder(s)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "history.csv")
    series = [m for m in (series or s.markets[:1]) if m]
    app.history_job = job = {"state": "running", "msg": "Starting...", "days": days,
                             "started": datetime.now(timezone.utc).isoformat()}

    def run():
        try:
            key_id, key_path = keystore.load_key()
            private_key = None
            if key_id and key_path:
                from .kalshi import load_private_key
                private_key = load_private_key(key_path)
            client = KalshiClient(s.api_base, key_id=key_id, private_key=private_key, timeout=15)
            names = dict(MARKETS)
            done = []
            with open(path + ".all", "w", encoding="utf-8") as out:
                for i, one in enumerate(series):
                    name = names.get(one, one)
                    part = f"{path}.{one}"
                    summary = app.history_collect(client, client.session, one, days, part,
                                                  progress=lambda m, name=name: job.update(msg=f"{name}: {m}"))
                    done.append(f"{name}: {summary}")
                    try:
                        with open(part, encoding="utf-8") as fh:
                            text = fh.read()
                        os.remove(part)
                    except OSError:
                        text = ""
                    out.write(text if i == 0 or not text else text.split("\n", 1)[-1])
            os.replace(path + ".all", path)
            job.update(state="done", msg=" | ".join(done))
        except Exception as e:     # shown on the page; never crashes the website
            job.update(state="error", msg=f"stopped: {e}")
    threading.Thread(target=run, daemon=True).start()
    return f"Collecting the last {days} days. Refresh this page to see how far it got."


def history_section(app: App) -> str:
    job = app.history_job
    status = ""
    if job:
        word = {"running": "Working", "done": "Done", "error": "Problem"}.get(job.get("state"), "")
        status = f'<p class="{"bad" if job.get("state") == "error" else "hint"}"><b>{word}:</b> {esc(job.get("msg", ""))}</p>'
    link = ('<p><a class="btn btn2" href="/history.csv">Download the history (history.csv)</a></p>'
            if job.get("state") == "done" else "")
    return ('<h2 id="history">Backtest data</h2><div class="box">'
            '<p class="hint">Downloads past markets so ideas can be tested on hundreds of them instead of '
            'waiting days: Kalshi\'s minute-by-minute prices, each result and its official settlement number, '
            'and Bitcoin\'s minute-by-minute price. It takes a few minutes and uses only free public data. '
            'It also checks whether your Kalshi account can read the official index the markets settle on.</p>'
            f'{status}<form method="post" action="/history">'
            '<label>Markets</label><select name="series">'
            + "".join(f'<option value="{esc(m)}">{esc(n)} 15-minute</option>' for m, n in MARKETS)
            + '<option value="all">All of them (takes longer)</option></select>'
            '<div style="display:flex;gap:8px;align-items:end">'
            '<div style="flex:1"><label>Days back</label><input name="days" value="14" inputmode="numeric"></div>'
            '<button class="btn2" style="flex:1">Collect history</button></div></form>'
            f'{link}</div>')


def real_money_section(app: App, raw: dict, secure: bool) -> str:
    mode = raw.get("mode", "paper")
    link = app.secure_link
    key_id, _ = keystore.load_key()
    out = ['<h2 id="real">Real money</h2><div class="box">',
           f'Mode: {mode_pill(mode)}']
    if not secure:
        if link is not None and link.ready:
            out.append(f'<p>To add your Kalshi key or switch to real money, open the 🔒 locked link: '
                       f'<a href="{esc(link.url())}/settings?key={esc(app.key)}#real" style="color:#8fb0ff">'
                       f'{esc(link.url())}</a></p>')
        else:
            out.append('<p class="hint">Adding your Kalshi key and switching to real money only work over the '
                       '🔒 locked link. ' + esc(link.message if link else "It isn't set up on this computer.") + '</p>')
        if mode == "live":
            out.append('<form method="post" action="/paper"><button class="btn2">Switch back to pretend money'
                       '</button></form>')
        out.append("</div>")
        return "".join(out)
    out.append('<p class="hint">🔒 You are on the locked link, so what you type here is private.</p>')
    if key_id:
        out.append(f'<p>Kalshi key saved (ID {esc(keystore.hint(key_id))}).</p>'
                   '<form method="post" action="/key/remove"><button class="btn2" onclick="return confirm('
                   '\'Remove the saved Kalshi key?\')">Remove key</button></form>')
    out.append('<form method="post" action="/key">'
               f'<label>Kalshi API Key ID</label><input name="key_id" autocomplete="off" autocapitalize="none" '
               f'spellcheck="false">'
               '<label>Private key (the whole text of the key file)</label>'
               '<textarea name="pem" rows="6" autocomplete="off" autocapitalize="none" spellcheck="false" '
               'style="width:100%;padding:11px;border-radius:10px;border:1px solid #363c48;background:var(--bg);'
               'color:var(--text);font:13px ui-monospace,monospace"></textarea>'
               f'<button>{"Replace key" if key_id else "Save key"}</button>'
               '<p class="hint">Make the key on kalshi.com → profile → API Keys → Create New API Key. '
               'It is checked with Kalshi, then kept on your server where only the bot can read it.</p></form>')
    if mode == "live":
        out.append('<form method="post" action="/paper"><button class="btn2">Switch back to pretend money'
                   '</button></form>')
    else:
        out.append('<form method="post" action="/live">'
                   f'<label>To bet real money, type: {esc(LIVE_CONFIRM_TEXT)}</label>'
                   '<input name="confirm" autocomplete="off" autocapitalize="none">'
                   '<button class="danger">Switch to real money</button>'
                   '<p class="hint">Start with one play, 1 contract, and a small daily loss limit.</p></form>')
    out.append("</div>")
    return "".join(out)


def request_transfer(app: App, form: dict) -> str:
    """Hand a money move to the bot (it holds the Kalshi key). Returns a message for the page."""
    s, _ = app.settings()
    if s is None or s.mode != "live":
        return "Money moves are only for real-money mode."
    try:
        frm, to = int(field(form, "from")), int(field(form, "to"))
        amount = float(field(form, "amount").replace("$", ""))
    except ValueError:
        return "Not moved: please pick both subaccounts and type an amount."
    if frm == to or not (0 <= frm <= 63 and 0 <= to <= 63):
        return "Not moved: pick two different subaccounts."
    if not 0 < amount <= 100_000:
        return "Not moved: type an amount above $0."
    folder = mode_folder(s)
    try:
        existing = {int(k) for k in L.Ledger(folder).load_state(Decimal(0)).accounts}
    except (OSError, ValueError):
        existing = set()
    for n in (frm, to):
        if existing and n not in existing:
            return (f"Not moved: {account_name(n)} isn't set up on Kalshi yet. Kalshi makes subaccounts only "
                    "after the bot's first real bet; the bot then sets them up by itself.")
    try:
        pot = (L.Ledger(folder).load_state(Decimal(0)).shards.get(str(frm)) or {}).get("2")
    except (OSError, ValueError, AttributeError):
        pot = None
    if pot is not None and Decimal(pot) < Decimal(str(amount)):
        return (f"Not moved: only ${Decimal(pot):.2f} of {account_name(frm)} is in the crypto pot, and moves "
                "happen inside the crypto pot. Get the money into the crypto pot first.")
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, TRANSFER_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"id": str(uuid.uuid4()), "from": frm, "to": to, "amount_cents": round(amount * 100)}, fh)
    os.replace(tmp, os.path.join(folder, TRANSFER_FILE))
    return (f"Asked the bot to move ${amount:.2f} from {account_name(frm)} to {account_name(to)}. "
            "The result shows below in a few seconds.")


def denied_page() -> str:
    return page("Private", '<div class="box bad">This page is private. Open your private link '
                '(the one that ends with <b>?key=…</b>).</div>', "")


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by make_server
    server_version = "btcbot"

    def log_message(self, fmt, *args):  # keep the server quiet in the logs
        pass

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()   # the locked (https) site
        super().setup()

    @property
    def secure(self) -> bool:
        return bool(getattr(self.server, "secure", False))

    def _authed(self, query: dict, form: dict | None = None) -> tuple[bool, bool]:
        """(allowed, came_from_link)."""
        given = (query.get("key") or [""])[0] or ((form or {}).get("key") or [""])[0]
        if given and hmac.compare_digest(given, self.app.key):
            return True, True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and value and hmac.compare_digest(value, self.app.key):
                return True, False
        return False, False

    def _send(self, status: int, body: str, set_cookie: bool = False, ctype="text/html; charset=utf-8",
              compress: bool = False):
        data = body.encode("utf-8")
        # Squeeze the once-a-second updates (and downloads) to save the server's free monthly data.
        # Pages that show the private link are never compressed (keeps them safe from size-guessing tricks).
        gz = compress and len(data) > 512 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            data = gzip.compress(data, compresslevel=6)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        if set_cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={self.app.key}; Max-Age=31536000; Path=/; HttpOnly; "
                             f"SameSite=Strict{'; Secure' if self.secure else ''}")
        if self.secure:
            self.send_header("Strict-Transport-Security", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _challenge(self, token: str):
        """Let's Encrypt checks this file to confirm the server owns its IP (for the locked link)."""
        link = self.app.secure_link
        path = link.store.challenge_file(token) if link else None
        try:
            with open(path, "rb") as fh:
                data = fh.read(4096)
        except (OSError, TypeError):
            return self._send(404, "not found", ctype="text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, where: str):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._safely(self._get)

    def do_POST(self):
        self._safely(self._post)

    def _safely(self, handler):
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # show the problem instead of a blank page
            try:
                self._send(500, page("Problem", f'<div class="box bad">Something went wrong: {esc(e)}</div>', ""))
            except OSError:
                pass

    def _get(self):
        url = urlparse(self.path)
        if url.path.startswith("/.well-known/acme-challenge/"):
            return self._challenge(url.path.rsplit("/", 1)[-1])
        query = parse_qs(url.query)
        ok, from_link = self._authed(query)
        if not ok:
            return self._send(403, denied_page())
        if url.path == "/":
            return self._send(200, overview_page(self.app, self.secure), set_cookie=from_link)
        if url.path == "/api/live":
            name = (query.get("play") or [None])[0]
            return self._send(200, json.dumps(live_parts(self.app, name, self.secure)), ctype="application/json",
                              compress=True)
        if url.path == "/play":
            return self._send(200, play_page(self.app, (query.get("name") or [""])[0], self.secure), set_cookie=from_link)
        if url.path == "/settings":
            done = (query.get("done") or [""])[0]
            return self._send(200, settings_page(self.app, message=DONE_MESSAGES.get(done), secure=self.secure),
                              set_cookie=from_link)
        if url.path == "/history.csv":
            s, _ = self.app.settings()
            try:
                with open(os.path.join(mode_folder(s), "history.csv"), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, TypeError):
                text = ",".join(H.COLUMNS) + "\n"
            return self._send(200, text, ctype="text/csv; charset=utf-8", compress=True)
        if url.path in ("/watch.csv", "/lastminute.csv", "/scores.csv"):
            s, _ = self.app.settings()
            folder = mode_folder(s) if s else ""
            if url.path == "/watch.csv":
                path, empty = os.path.join(folder, "watch.csv"), ",".join(lastminute.WATCH_COLUMNS) + "\n"
            elif url.path == "/scores.csv":
                path, empty = os.path.join(folder, "scores.csv"), ",".join(lastminute.SCORE_COLUMNS) + "\n"
            else:
                day_dir = os.path.join(folder, "lastminute")
                days = sorted(os.listdir(day_dir)) if os.path.isdir(day_dir) else []
                try:
                    want = max(1, min(14, int((query.get("days") or ["1"])[0])))
                except ValueError:
                    want = 1
                empty = ",".join(lastminute.DATA_COLUMNS) + "\n"
                text = ""
                for i, day in enumerate(days[-want:]):
                    try:
                        with open(os.path.join(day_dir, day), encoding="utf-8") as fh:
                            body = fh.read()
                    except OSError:
                        continue
                    text += body if not text else body.split("\n", 1)[-1]   # one header only
                return self._send(200, text or empty, ctype="text/csv; charset=utf-8", compress=True)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = empty
            return self._send(200, text, ctype="text/csv; charset=utf-8", compress=True)
        if url.path == "/trades.csv":
            s, _ = self.app.settings()
            book = "paper" if (query.get("book") or [""])[0] == "pretend" else None
            path = os.path.join(mode_folder(s, book), "trades.csv") if s else ""
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = ",".join(L.TRADE_COLUMNS) + "\n"
            return self._send(200, text, ctype="text/csv; charset=utf-8", compress=True)
        return self._send(404, page("Not found", '<div class="box">Not found.</div>', ""))

    def _post(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, page("Too big", '<div class="box bad">Too much data.</div>', ""))
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        ok, _ = self._authed(parse_qs(url.query), form)
        if not ok:
            return self._send(403, denied_page())
        if url.path == "/settings":
            try:
                done, focus = self.app.save_form(form)
            except SettingsError as e:
                try:
                    shown = form_to_raw(form, self.app.raw())
                except SettingsError:
                    shown = None
                return self._send(200, settings_page(self.app, raw=shown, error=str(e), secure=self.secure))
            return self._redirect(f"/settings?done={done}" + (f"#play-{focus}" if focus is not None else ""))
        if url.path == "/transfer":
            self.app.moved = request_transfer(self.app, form)
            return self._redirect("/#money")
        if url.path == "/pots":
            s, _ = self.app.settings()
            if s is not None and s.mode == "live":
                folder = mode_folder(s)
                os.makedirs(folder, exist_ok=True)
                choice = field(form, "pot")
                if choice == "move":
                    with open(os.path.join(folder, POT_MOVE_FILE), "w", encoding="utf-8") as fh:
                        json.dump({"id": str(uuid.uuid4())}, fh)
                    self.app.moved = "Asked the bot to move your money into the crypto pot. The result shows here soon."
                else:
                    with open(os.path.join(folder, ALLOCATION_FILE), "w", encoding="utf-8") as fh:
                        json.dump({"shard": 2 if choice == "2" else None}, fh)
                    self.app.moved = ("Asked Kalshi to keep your money in the crypto pot. Check back in a minute."
                                      if choice == "2" else "Asked Kalshi to stop moving money between pots.")
            return self._redirect("/#money")
        if url.path == "/history":
            try:
                days = max(1, min(60, int(float(field(form, "days", "14") or 14))))
            except ValueError:
                days = 14
            pick = field(form, "series")
            start_history(self.app, days, [m for m, _ in MARKETS] if pick == "all" else [pick] if pick else None)
            return self._redirect("/settings#history")
        if url.path == "/trade":
            s, _ = self.app.settings()
            if s is None:
                return self._redirect("/")
            if s.mode == "live" and not self.secure:
                self.app.trade_sent = "Not sent: real-money trades only work on the 🔒 locked link."
                return self._redirect("/#trade")
            action = field(form, "action", "buy")
            req = {"id": str(uuid.uuid4()), "action": "sell" if action == "sell" else "buy",
                   "side": field(form, "side"), "at": datetime.now(timezone.utc).isoformat()}
            try:
                if req["action"] == "buy":
                    req["series"] = field(form, "series") or s.markets[0]
                    req["contracts"] = int(float(field(form, "contracts", "1") or 1))
                    cap = number(form, "max", "Most I'll pay", blank_ok=True)
                    if cap is not None:
                        if not 1 <= cap <= 99:
                            raise SettingsError("Most I'll pay: use 1 to 99 cents")
                        req["max_price"] = str(Decimal(str(cap)) / 100)
                else:
                    req["ticker"] = field(form, "ticker")
            except (SettingsError, ValueError) as e:
                self.app.trade_sent = f"Not sent: {e}"
                return self._redirect("/#trade")
            folder = mode_folder(s)
            os.makedirs(folder, exist_ok=True)
            tmp = os.path.join(folder, TRADE_FILE + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(req, fh)
            os.replace(tmp, os.path.join(folder, TRADE_FILE))
            word = "Up" if req["side"] == "yes" else "Down"
            self.app.trade_sent = (f"Sent to the bot: {'sell' if req['action'] == 'sell' else 'buy'} {word}. "
                                   "The result shows below in a second or two.")
            return self._redirect("/#trade")
        if url.path == "/subaccount":
            s, _ = self.app.settings()
            if s is not None and s.mode == "live":
                folder = mode_folder(s)
                os.makedirs(folder, exist_ok=True)
                open(os.path.join(folder, MAKE_SUB_FILE), "w").close()
                self.app.moved = "Asked the bot to make a subaccount in the crypto pot. The number shows here soon."
            return self._redirect("/#money")
        if url.path == "/setup":
            s, _ = self.app.settings()
            if s is not None and s.mode == "live":
                folder = mode_folder(s)
                os.makedirs(folder, exist_ok=True)
                open(os.path.join(folder, SETUP_FILE), "w").close()
                self.app.moved = "Asked the bot to set up subaccounts again. Check back in a few seconds."
            return self._redirect("/#money")
        if url.path in ("/key", "/live") and not self.secure:
            self.app.flash = "Not done: open the 🔒 locked link for this."
            return self._redirect("/settings#real")
        if url.path == "/key":
            self.app.flash = save_kalshi_key(self.app, form)
            return self._redirect("/settings#real")
        if url.path == "/key/remove":
            keystore.remove_key()
            self.app.flash = "Kalshi key removed."
            return self._redirect("/settings#real")
        if url.path == "/live":
            self.app.flash = switch_mode(self.app, "live", field(form, "confirm"))
            return self._redirect("/settings#real")
        if url.path == "/paper":
            self.app.flash = switch_mode(self.app, "paper")
            return self._redirect("/settings#real")
        if url.path in ("/pause", "/resume"):
            self.app.set_paused(url.path == "/pause")
            return self._redirect("/")
        if url.path == "/update":
            try:
                changed = self.app.updater()
            except RuntimeError as e:
                return self._send(200, page("Update", f'<div class="box bad"><b>Not updated:</b> {esc(e)}</div>'
                                            '<a class="btn" href="/">Back</a>', "status"))
            if not changed:
                return self._send(200, page("Update", '<div class="box ok">Already the newest version.</div>'
                                            '<a class="btn" href="/">Back</a>', "status"))
            restarting = self.app.restarter(self.app.settings_path)
            note = ("Updated. The bot and this page are restarting. This page comes back by itself in about "
                    "20 seconds." if restarting else
                    "Updated. The bot is restarting. Restart this website yourself to use its new version.")
            return self._send(200, page("Update", f'<div class="box ok">{note}</div>', "status", refresh="20;url=/"))
        return self._send(404, page("Not found", '<div class="box">Not found.</div>', ""))


def make_server(settings_path: str, port: int, key: str, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": App(settings_path, key)})
    return ThreadingHTTPServer((host, port), handler)


def serve(settings_path: str, port: int) -> None:
    key = load_key()
    server = make_server(settings_path, port, key)
    link = SecureLink(server.RequestHandlerClass)
    server.RequestHandlerClass.app.secure_link = link
    threading.Thread(target=link.run_forever, daemon=True).start()
    print(f"website running on port {port}", flush=True)
    server.serve_forever()
