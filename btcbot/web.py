"""The bot's private website: status, every play's bets and score, and every setting.

Runs as its own small service next to the bot. It only reads the bot's files,
writes settings.yaml (the bot notices and restarts with the new settings),
creates or removes the STOP file, and can download the newest bot code.
Anyone without the private key in the link gets nothing.
"""
from __future__ import annotations

import copy
import hmac
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from . import ledger as L
from .rules import parse_time
from .settings import (PACKAGE_DIR, SIZE_MODES, Settings, SettingsError, load, load_raw, mode_folder,
                       save_raw)

KEY_FILE_ENV = "BTCBOT_WEB_KEY_FILE"
MARKETS = [("KXBTC15M", "Bitcoin"), ("KXETH15M", "Ethereum"), ("KXSOL15M", "Solana"), ("KXXRP15M", "XRP")]
BUY_CHOICES = [("favorite", "The favorite"), ("underdog", "The underdog"), ("up", "Always Up"), ("down", "Always Down")]
SIZE_CHOICES = [("contracts", "Contracts"), ("dollars", "Dollars"), ("percent", "% of balance")]
LOSS_CHOICES = [("dollars", "Dollars"), ("percent", "% of the day's balance")]
EXIT_WORDS = {"take_profit": "take profit", "stop_loss": "stop loss"}
REPO_DIR = os.path.dirname(PACKAGE_DIR)
COOKIE = "btcbot_key"
MAX_BODY = 64_000
ONE = Decimal("1")
ON_PILL = '<span class="pill on-pill">ON</span>'
OFF_PILL = '<span class="pill off-pill">OFF</span>'
DONE_MESSAGES = {
    "saved": "Saved. The bot restarts with the new settings in about 15 seconds.",
    "added": "Added a new play. It starts switched off: set it up, tick \"This play is on\", then Save.",
    "copied": "Copied. The copy starts switched off: change it, tick \"This play is on\", then Save.",
    "deleted": "Deleted. Its past bets stay in the trade list.",
}


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
                       "favorite_rising_seconds": None, "btc_distance_min_dollars": None},
    }


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
            if play is not None and not msg.startswith(f"{play}:"):
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
        d["rows"] = L.read_trades(os.path.join(folder, "trades.csv"))
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
            elif action != "save":
                raise SettingsError("that button didn't work; please try again")
        except (ValueError, IndexError):
            raise SettingsError("that button didn't work; please reload the page and try again")
        save_raw(self.settings_path, raw)
        return done, focus

    def set_paused(self, paused: bool) -> None:
        s, _ = self.settings()
        if s is None:
            return
        folder = mode_folder(s)
        os.makedirs(folder, exist_ok=True)
        stop = os.path.join(folder, "STOP")
        if paused:
            open(stop, "w").close()
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
            },
        }
        size_mode = field(form, p + "size_mode", "default")
        if size_mode in SIZE_MODES:
            play["size"] = {
                "mode": size_mode,
                "value": number(form, p + "size_value", f"{label} bet size"),
                "max_contracts": number(form, p + "size_max", f"{label} most contracts", blank_ok=True) or 0,
            }
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
.empty{color:var(--dim);font-size:14px;padding:6px 0}.back{display:inline-block;color:#8fb0ff;text-decoration:none;margin-bottom:6px}
"""


def page(title: str, body: str, tab: str, refresh=None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    on = lambda t: ' class="on"' if t == tab else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0d0f14">'
            f'{meta}<title>{esc(title)}</title><style>{CSS}</style></head><body><main>'
            f'<nav><a href="/"{on("status")}>Overview</a><a href="/settings"{on("settings")}>Settings</a></nav>'
            f'{body}</main></body></html>')


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


def open_bet(pos: dict, play, d: dict) -> str:
    zone = d["zone"]
    n = Decimal(pos["contracts"])
    paid = Decimal(pos["entry_price"])
    close = parse_time(pos["close_time"])
    mins = (close - datetime.now(timezone.utc)).total_seconds() / 60 if close else None
    when = (f"closes {clock(close, zone)} · {max(mins, 0):.0f} min left" if mins is not None and mins > 0
            else "closed · waiting for the result")
    plan = []
    if play is not None and play.take_profit is not None:
        plan.append(f"sells at {cents_text(play.take_profit)}")
    if play is not None and play.stop_loss is not None:
        plan.append(f"or {cents_text(play.stop_loss)}")
    fresh = d.get("market_age") is not None and d["market_age"] < 120
    bid = now_bid(pos, d["market"]) if fresh else None
    if bid is not None and (mins is None or mins > 0):
        change = (bid - paid) * n - Decimal(pos["entry_fee"])
        right = f'now {cents_text(bid)}<div class="sub">{money(change)} if sold</div>'
    else:
        right = '<span class="dim">open</span>'
    return (f'<div class="bet"><div>{side_tag(pos["side"])}<b>{n:g} × {cents_text(paid)}</b>'
            f'<div class="sub">{esc(when)}{" · " + esc(" ".join(plan)) if plan else ""}</div></div>'
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

    def box(word, arrow, p, fav):
        return (f'<div class="price{" fav" if fav else ""}">{arrow} {word}'
                f'<b>{cents_text(p) if p is not None else "—"}</b></div>')

    out = []
    for series, m in soonest.items():
        t = parse_time(m["close_time"])
        mins = (t - now).total_seconds() / 60
        up = Decimal(m["yes_ask"]) if m.get("yes_ask") else None
        down = ONE - Decimal(m["yes_bid"]) if m.get("yes_bid") else None
        fav_up = up is not None and down is not None and up >= down
        target = f' · target ${float(m["target"]):,.2f}' if m.get("target") else ""
        stale = f' · prices {int(age)}s old' if age > 30 else ""
        out.append(f'<div class="box market"><div class="top"><span><b style="color:var(--text)">'
                   f'{esc(names.get(series, series))}</b> · closes {clock(t, zone)}</span>'
                   f'<span>{mins:.0f} min left</span></div>'
                   f'<div class="prices">{box("Up", "▲", up, fav_up)}{box("Down", "▼", down, not fav_up)}</div>'
                   f'<p class="hint">Price to buy right now{target}{stale}</p></div>')
    return "".join(out)


def play_names(d: dict) -> list:
    """Plays in the settings first, then old plays that still have bets on record."""
    names = [p.name for p in d["s"].plays]
    held = [p["play"] for p in (d["state"].positions if d.get("state") else [])]
    return names + [n for n in dict.fromkeys([r["play"] for r in d["rows"]] + held) if n not in names]


def play_link(name: str) -> str:
    return "/play?name=" + quote(name, safe="")


def status_pill(play) -> str:
    if play is None:
        return '<span class="pill old-pill">REMOVED</span>'
    return ON_PILL if play.enabled else OFF_PILL


# ---------------------------------------------------------------- pages
def overview_page(app: App) -> str:
    d = app.snapshot()
    s = d["s"]
    if s is None:
        body = (f'<h1>Kalshi bot</h1><div class="box bad"><b>Settings problem:</b> {esc(d["error"])}<br>'
                'Fix it on the Settings page. The bot can\'t start until then.</div>')
        return page("Kalshi bot", body, "status", refresh=60)
    st = d["state"]
    parts = [f"<h1>Kalshi bot {mode_pill(s.mode)}</h1>", running_box(d)]
    if d["paused"]:
        parts.append('<div class="box warn"><b>Paused:</b> no new bets. Open bets still finish.'
                     '<form method="post" action="/resume"><button>Resume betting</button></form></div>')
    total = L.stats(d["rows"])
    if st is not None:
        bal = f"${float(st.paper_cash):.2f}" if s.mode == "paper" else "Kalshi app"
        parts.append(
            '<div class="cards">'
            f'<div class="card"><span>Balance</span><b>{bal}</b></div>'
            f'<div class="card"><span>Today</span><b>{money(st.day_profit)}</b><span>{bets(st.day_trades)}</span></div>'
            f'<div class="card"><span>All time</span><b>{money(total["profit"])}</b><span>{bets(total["trades"])}</span></div>'
            '</div>')
    parts.append(market_cards(d))
    parts.append("<h2>Your plays</h2>")
    by_name = {p.name: p for p in s.plays}
    positions = st.positions if st is not None else []
    for name in play_names(d):
        play = by_name.get(name)
        ps = L.stats([r for r in d["rows"] if r["play"] == name])
        opened = sum(1 for p in positions if p["play"] == name)
        desc = describe_play(play, s) if play else "Removed or renamed. Its past bets are kept here."
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
    log = tail_log(os.path.join(d["folder"], "bot.log"), 15)
    if log:
        parts.append('<h2>Recent activity</h2><div class="box log">' + esc("\n".join(log)) + "</div>")
    if not d["paused"]:
        parts.append('<form method="post" action="/pause"><button class="btn2">Pause new bets</button></form>')
    parts.append(f'<h2>Bot version</h2><div class="box">Version {esc(code_version())}'
                 '<form method="post" action="/update"><button class="btn2">Get the newest version</button></form>'
                 '<p class="hint">Only needed when Claude tells you there\'s an update. Takes about a minute. '
                 'Open bets are kept.</p></div>'
                 '<p class="hint">This page refreshes by itself every 30 seconds.</p>')
    return page("Kalshi bot", "".join(parts), "status", refresh=30)


def play_page(app: App, name: str) -> str:
    d = app.snapshot()
    s = d["s"]
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
    parts = ['<a class="back" href="/">‹ All plays</a>', f"<h1>{esc(name)} {status_pill(play)}</h1>"]
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
    positions = [p for p in (d["state"].positions if d.get("state") else []) if p["play"] == name]
    parts.append(f'<h2>Open bets · {len(positions)}</h2><div class="box">')
    parts.append("".join(open_bet(p, play, d) for p in positions) or '<div class="empty">None right now.</div>')
    parts.append("</div>")
    parts.append(f'<h2>Closed bets · {len(rows)}</h2><div class="box">')
    parts.append("".join(closed_bet(r, zone) for r in reversed(rows[-60:])) or '<div class="empty">None yet.</div>')
    if len(rows) > 60:
        parts.append(f'<p class="hint">Showing the newest 60 of {len(rows)}.</p>')
    parts.append('<p class="hint"><a href="/trades.csv" style="color:#8fb0ff">Download every bet</a></p></div>')
    log = tail_log(os.path.join(d["folder"], "bot.log"), 15, play=name)
    if log:
        parts.append('<h2>What it did lately</h2><div class="box log">' + esc("\n".join(log)) + "</div>")
    if play is not None:
        parts.append(f'<a class="btn btn2" href="/settings#play-{s.plays.index(play)}">Edit this play</a>')
    return page(name, "".join(parts), "status", refresh=30)


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
        '<details><summary>Extra conditions</summary>'
        f'<label>Extra cents it may pay above the price on screen</label>{inp(pre + "slip", plain(p.get("slippage_cents", 0)))}'
        f'<label>Skip if the gap between buy and sell price is wider than (¢)</label>{inp(pre + "spread", plain(c.get("max_spread_cents")))}'
        f'<label>Skip if fewer contracts are offered than</label>{inp(pre + "depth", plain(c.get("min_contracts_at_price")))}'
        f'<label>Only buy if its side rose over the last N seconds</label>{inp(pre + "rising", plain(c.get("favorite_rising_seconds")))}'
        f'<label>Only buy if the price is this many $ past the target</label>{inp(pre + "dist", plain(c.get("btc_distance_min_dollars")))}'
        '<p class="hint">Blank = off.</p></details>'
        f'<div class="row2"><button class="btn2" name="action" value="copy:{i}">Copy this play</button>'
        f'<button class="danger" name="action" value="delete:{i}" onclick="return confirm(this.dataset.q)" '
        f'data-q="{confirm}">Delete</button></div>'
        '<p class="hint">Renaming a play starts its score over; its old bets stay under the old name.</p></div>')


def settings_page(app: App, raw: dict | None = None, message: str | None = None, error: str | None = None) -> str:
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
    out.append(f'<div class="box">Mode: {mode_pill(raw.get("mode", "paper"))}'
               '<p class="hint">For safety, real-money mode can\'t be turned on from this page.</p></div>')
    # The hidden first button makes Enter in a box save, instead of pressing the first Copy button.
    out.append('<form method="post" action="/settings">'
               '<button name="action" value="save" tabindex="-1" aria-hidden="true" '
               'style="position:absolute;left:-9999px;width:1px">Save</button>')
    plays = raw.get("plays") or []
    out.append(f'<input type="hidden" name="play_count" value="{len(plays)}">')
    out.extend(play_form(i, p, opt, inp) for i, p in enumerate(plays))
    out.append('<div id="add"></div><button class="btn2" name="action" value="add">+ Add a play</button>')
    out.append(
        "<h2>Markets</h2><div class=\"box\">" + "".join(
            f'<div class="check"><input type="checkbox" name="market_{m}" value="1"'
            f'{" checked" if m in markets else ""}><span>{esc(n)} 15-minute</span></div>'
            for m, n in MARKETS) + f'<label>Other series (comma separated)</label>{inp("market_other", other, "text")}</div>')
    out.append(
        '<h2>Money and limits (all plays)</h2><div class="box">'
        f'<label>Pretend starting money ($, paper mode)</label>{inp("paper_balance", plain(raw.get("paper_balance", 11)))}'
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


def denied_page() -> str:
    return page("Private", '<div class="box bad">This page is private. Open your private link '
                '(the one that ends with <b>?key=…</b>).</div>', "")


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by make_server
    server_version = "btcbot"

    def log_message(self, fmt, *args):  # keep the server quiet in the logs
        pass

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

    def _send(self, status: int, body: str, set_cookie: bool = False, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        if set_cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={self.app.key}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
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
        query = parse_qs(url.query)
        ok, from_link = self._authed(query)
        if not ok:
            return self._send(403, denied_page())
        if url.path == "/":
            return self._send(200, overview_page(self.app), set_cookie=from_link)
        if url.path == "/play":
            return self._send(200, play_page(self.app, (query.get("name") or [""])[0]), set_cookie=from_link)
        if url.path == "/settings":
            done = (query.get("done") or [""])[0]
            return self._send(200, settings_page(self.app, message=DONE_MESSAGES.get(done)), set_cookie=from_link)
        if url.path == "/trades.csv":
            s, _ = self.app.settings()
            path = os.path.join(mode_folder(s), "trades.csv") if s else ""
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = ",".join(L.TRADE_COLUMNS) + "\n"
            return self._send(200, text, ctype="text/csv; charset=utf-8")
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
                return self._send(200, settings_page(self.app, raw=shown, error=str(e)))
            return self._redirect(f"/settings?done={done}" + (f"#play-{focus}" if focus is not None else ""))
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
    print(f"website running on port {port}", flush=True)
    server.serve_forever()
