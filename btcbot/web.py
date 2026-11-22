"""The bot's private website: status, score, trades and every setting, from a phone.

Runs as its own small service next to the bot. It only reads the bot's files,
writes settings.yaml (the bot notices and restarts with the new settings) and
creates or removes the STOP file. Anyone without the private key in the link
gets nothing.
"""
from __future__ import annotations

import hmac
import html
import os
import secrets
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import ledger as L
from .rules import parse_time
from .settings import (PACKAGE_DIR, Settings, SettingsError, load, load_raw, mode_folder,
                       save_raw)

KEY_FILE_ENV = "BTCBOT_WEB_KEY_FILE"
MARKETS = [("KXBTC15M", "Bitcoin"), ("KXETH15M", "Ethereum"), ("KXSOL15M", "Solana"), ("KXXRP15M", "XRP")]
BUY_CHOICES = [("favorite", "The favorite"), ("underdog", "The underdog"), ("up", "Always Up"), ("down", "Always Down")]
COOKIE = "btcbot_key"
MAX_BODY = 64_000


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


# ---------------------------------------------------------------- helpers
def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def cents(dollars) -> str:
    """0.7 -> "70" for the form; None -> ""."""
    if dollars in (None, ""):
        return ""
    value = float(dollars) * 100
    return f"{value:g}"


def plain(value) -> str:
    return "" if value in (None, "") else f"{float(value):g}"


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


class App:
    """Everything the pages need: where the files are and the private key."""

    def __init__(self, settings_path: str, key: str):
        self.settings_path = settings_path
        self.key = key

    # -- reading
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

    def folder(self, s: Settings | None) -> str | None:
        return mode_folder(s) if s else None

    # -- writing
    def save_form(self, form: dict) -> Settings:
        raw = form_to_raw(form, self.raw())
        return save_raw(self.settings_path, raw)

    def set_paused(self, paused: bool) -> None:
        s, _ = self.settings()
        if s is None:
            return
        folder = self.folder(s)
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
    raw["balance_share_pct"] = number(form, "balance_share_pct", "How much of your Kalshi it can use")
    raw["size"] = {
        "mode": field(form, "size_mode", "contracts"),
        "value": number(form, "size_value", "Size per trade"),
        "max_contracts": number(form, "max_contracts", "Most contracts per trade"),
    }
    raw["daily_loss_limit"] = {
        "mode": field(form, "loss_mode", "dollars"),
        "value": number(form, "loss_value", "Daily loss limit"),
    }
    raw["max_trades_per_day"] = number(form, "max_trades_per_day", "Most trades per day")
    plays = []
    for i in range(int(field(form, "play_count", "0") or 0)):
        p = f"p{i}_"
        name = field(form, p + "name") or f"play{i + 1}"
        label = f"Play '{name}'"
        plays.append({
            "name": name,
            "enabled": bool(field(form, p + "enabled")),
            "buy": field(form, p + "buy", "favorite"),
            "min_price": number(form, p + "min", f"{label} lowest price", scale=100),
            "max_price": number(form, p + "max", f"{label} highest price", scale=100),
            "slippage_cents": number(form, p + "slip", f"{label} extra cents"),
            "act_from_minutes_left": number(form, p + "from", f"{label} start minute"),
            "act_until_minutes_left": number(form, p + "until", f"{label} end minute"),
            "take_profit": number(form, p + "tp", f"{label} take profit", blank_ok=True, scale=100),
            "stop_loss": number(form, p + "sl", f"{label} stop loss", blank_ok=True, scale=100),
            "conditions": {
                "max_spread_cents": number(form, p + "spread", f"{label} spread", blank_ok=True),
                "min_contracts_at_price": number(form, p + "depth", f"{label} contracts offered", blank_ok=True),
                "favorite_rising_seconds": number(form, p + "rising", f"{label} rising seconds", blank_ok=True),
                "btc_distance_min_dollars": number(form, p + "dist", f"{label} distance", blank_ok=True),
            },
        })
    raw["plays"] = plays
    return raw


# ---------------------------------------------------------------- pages
CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.45 -apple-system,system-ui,sans-serif;background:#0f1115;color:#e8eaed}
main{max-width:720px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 12px}h2{font-size:17px;margin:22px 0 8px;color:#9aa0a6}
nav{display:flex;gap:8px;margin-bottom:14px}nav a{flex:1;text-align:center;padding:10px;border-radius:10px;background:#1c1f26;color:#e8eaed;text-decoration:none}
nav a.on{background:#2b5cff}.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;margin-left:6px;vertical-align:middle}
.paper{background:#1f3b2b;color:#7ee2a8}.live{background:#4a1f1f;color:#ff9b9b}
.box{background:#1c1f26;border-radius:14px;padding:14px;margin:10px 0}.ok{border-left:4px solid #34c37a}.bad{border-left:4px solid #ff5c5c}.warn{border-left:4px solid #f4b740}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.card{background:#1c1f26;border-radius:14px;padding:12px}.card b{display:block;font-size:20px}.card span{color:#9aa0a6;font-size:13px}
.pos{color:#5ee08f}.neg{color:#ff7b7b}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:7px 4px;border-bottom:1px solid #2a2e37;text-align:left}th{color:#9aa0a6;font-weight:500}
.log{font:13px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:#c4c7cc}button,.btn{background:#2b5cff;color:#fff;border:0;border-radius:10px;padding:12px 16px;font-size:16px;width:100%;margin-top:8px}
.btn2{background:#30343d}label{display:block;margin:10px 0 4px;color:#c4c7cc;font-size:14px}input,select{width:100%;padding:10px;border-radius:10px;border:1px solid #3a3f4a;background:#0f1115;color:#e8eaed;font-size:16px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.check{display:flex;gap:10px;align-items:center;margin:8px 0}.check input{width:22px;height:22px}
.hint{color:#9aa0a6;font-size:13px;margin:2px 0}details{margin-top:10px}summary{cursor:pointer;color:#8ab4ff}
"""


def page(title: str, body: str, tab: str, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    on = lambda t: ' class="on"' if t == tab else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1">{meta}<title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><main>'
            f'<nav><a href="/"{on("status")}>Status</a><a href="/settings"{on("settings")}>Settings</a></nav>'
            f'{body}</main></body></html>')


def money(d) -> str:
    cls = "pos" if d > 0 else "neg" if d < 0 else ""
    sign = "+" if d > 0 else "−" if d < 0 else ""
    return f'<span class="{cls}">{sign}${abs(d):.2f}</span>'


def local_time(iso: str | None, zone: ZoneInfo) -> str:
    t = parse_time(iso)
    if t is None:
        return ""
    t = t.astimezone(zone)
    return f"{t.strftime('%b')} {t.day} {t.strftime('%I:%M %p').lstrip('0').lower()}"


def describe_play(p) -> str:
    buy = dict(BUY_CHOICES).get(p.buy, p.buy).lower()
    c = lambda price: f"{float(price) * 100:g}"
    exits = []
    if p.take_profit is not None:
        exits.append(f"sells at {c(p.take_profit)}¢ or more")
    if p.stop_loss is not None:
        exits.append(f"sells if it drops to {c(p.stop_loss)}¢")
    end = "; ".join(exits) if exits else "holds to the end"
    return (f"<b>{esc(p.name)}</b>: buys {esc(buy)} at {c(p.min_price)}–{c(p.max_price)}¢, "
            f"from {float(p.act_from_minutes_left):g} to {float(p.act_until_minutes_left):g} min before each close; "
            f"{esc(end)}.")


def tail_log(path: str, lines: int = 25) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 40_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines()[-lines:]:
        if len(line) > 20 and line[4] == "-" and line[13] == ":":
            hh, mm = int(line[11:13]), line[14:16]
            line = f"{(hh % 12) or 12}:{mm} {'pm' if hh >= 12 else 'am'}  {line[20:]}".replace("INFO ", "")
        out.append(line)
    return list(reversed(out))


def status_page(app: App) -> str:
    s, error = app.settings()
    zone = ZoneInfo(s.day_timezone if s else "Pacific/Honolulu")
    parts = []
    mode = s.mode if s else "?"
    parts.append(f'<h1>Kalshi bot <span class="badge {"live" if mode == "live" else "paper"}">'
                 f'{"REAL MONEY" if mode == "live" else "PAPER (pretend money)"}</span></h1>')
    if error:
        parts.append(f'<div class="box bad"><b>Settings problem:</b> {esc(error)}<br>'
                     f'Fix it on the Settings page. The bot can\'t start until then.</div>')
        return page("Kalshi bot", "".join(parts), "status", refresh=60)
    folder = app.folder(s)
    state_path = os.path.join(folder, "state.json")
    try:
        st = L.Ledger(folder).load_state(s.paper_balance)
        age = time.time() - os.stat(state_path).st_mtime
    except OSError:
        st, age = None, None
    if age is None:
        parts.append('<div class="box warn">Starting up. Nothing saved yet. Check back in a minute.</div>')
    elif age < max(90, s.slow_seconds * 4):
        secs = int(age)
        parts.append(f'<div class="box ok">✅ Running. Last price check {secs} second{"" if secs == 1 else "s"} ago.</div>')
    else:
        parts.append(f'<div class="box bad">⚠️ Not running. Last price check {int(age // 60)} minutes ago.</div>')
    paused = os.path.exists(os.path.join(folder, "STOP"))
    if paused:
        parts.append('<div class="box warn"><b>Paused:</b> no new trades. Open trades still finish.'
                     '<form method="post" action="/resume"><button>Resume trading</button></form></div>')
    rows = L.read_trades(os.path.join(folder, "trades.csv"))
    total = L.stats(rows)
    if st is not None:
        bal = f"${float(st.paper_cash):.2f}" if mode == "paper" else "see Kalshi app"
        parts.append(
            '<div class="cards">'
            f'<div class="card"><span>Balance</span><b>{bal}</b></div>'
            f'<div class="card"><span>Today</span><b>{money(Decimal(st.day_profit))}</b><span>{st.day_trades} trades</span></div>'
            f'<div class="card"><span>All time</span><b>{money(total["profit"])}</b><span>{total["trades"]} trades</span></div>'
            '</div>')
    if total["trades"]:
        parts.append(
            f'<div class="box"><b>Win rate {100 * total["win_rate"]:.0f}%</b> · needs about '
            f'{100 * total["need"]:.0f}% to break even · luck range ±{100 * total["luck"]:.0f} points'
            f'<p class="hint">{esc(total["verdict"])}</p></div>')
    parts.append("<h2>What it's doing</h2><div class=\"box\">" +
                 "<br>".join(describe_play(p) for p in s.plays if p.enabled) +
                 f'<p class="hint">Watching: {esc(", ".join(s.markets))}. Closes are at :00, :15, :30 and :45.</p></div>')
    if st is not None and st.positions:
        items = "".join(
            f"<tr><td>{esc(local_time(p['close_time'], zone))}</td>"
            f"<td>{'UP' if p['side'] == 'yes' else 'DOWN'}</td><td>{float(p['contracts']):g}</td>"
            f"<td>{float(p['entry_price']) * 100:.0f}¢</td><td>{esc(p['play'])}</td></tr>"
            for p in st.positions)
        parts.append("<h2>Open trades</h2><div class=\"box\"><table><tr><th>Closes</th><th>Side</th>"
                     f"<th>Qty</th><th>Paid</th><th>Play</th></tr>{items}</table></div>")
    if rows:
        items = "".join(
            f"<tr><td>{esc(local_time(r['closed_at'], zone))}</td><td>{esc(r['side'])}</td>"
            f"<td>{float(r['entry_price']) * 100:.0f}¢</td>"
            f"<td>{esc(r['result'] + ' won' if r['exit_type'] == 'settled' else 'sold early')}</td>"
            f"<td>{money(Decimal(r['profit']))}</td></tr>"
            for r in reversed(rows[-20:]))
        parts.append("<h2>Recent trades</h2><div class=\"box\"><table><tr><th>When</th><th>Side</th>"
                     f"<th>Paid</th><th>Result</th><th>Profit</th></tr>{items}</table>"
                     '<p class="hint"><a href="/trades.csv" style="color:#8ab4ff">Download every trade</a></p></div>')
    log = tail_log(os.path.join(folder, "bot.log"))
    if log:
        parts.append('<h2>Recent activity</h2><div class="box log">' + esc("\n".join(log)) + "</div>")
    if not paused:
        parts.append('<form method="post" action="/pause"><button class="btn btn2">Pause new trades</button></form>')
    parts.append('<p class="hint">This page refreshes by itself every 30 seconds.</p>')
    return page("Kalshi bot", "".join(parts), "status", refresh=30)


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
    mode = raw.get("mode", "paper")
    out.append(f'<div class="box">Mode: <b>{"REAL MONEY" if mode == "live" else "Paper (pretend money)"}</b>'
               '<p class="hint">For safety, real-money mode can\'t be turned on from this page.</p></div>')
    out.append('<form method="post" action="/settings">')
    out.append("<h2>Markets</h2><div class=\"box\">" + "".join(
        f'<div class="check"><input type="checkbox" name="market_{m}" value="1"'
        f'{" checked" if m in markets else ""}><span>{esc(n)} 15-minute ({m})</span></div>'
        for m, n in MARKETS) + f'<label>Other series (comma separated)</label>{inp("market_other", other, "text")}</div>')
    out.append(
        "<h2>Money and limits</h2><div class=\"box\">"
        f'<label>Pretend starting money ($, paper mode)</label>{inp("paper_balance", plain(raw.get("paper_balance", 11)))}'
        f'<label>How much of your Kalshi it can use (%)</label>{inp("balance_share_pct", plain(raw.get("balance_share_pct", 50)))}'
        f'<div class="row"><div><label>Size per trade is</label><select name="size_mode">'
        f'{opt([("contracts", "Contracts"), ("percent", "% of balance"), ("dollars", "Dollars")], size.get("mode", "contracts"))}'
        f'</select></div><div><label>Amount</label>{inp("size_value", plain(size.get("value", 1)))}</div></div>'
        f'<label>Most contracts per trade (0 = no cap)</label>{inp("max_contracts", plain(size.get("max_contracts", 1)), "numeric")}'
        f'<div class="row"><div><label>Daily loss limit in</label><select name="loss_mode">'
        f'{opt([("dollars", "Dollars"), ("percent", "% of the day\'s balance")], loss.get("mode", "dollars"))}'
        f'</select></div><div><label>Stop after losing</label>{inp("loss_value", plain(loss.get("value", 5)))}</div></div>'
        f'<label>Most trades per day (0 = no cap)</label>{inp("max_trades_per_day", plain(raw.get("max_trades_per_day", 0)), "numeric")}'
        "</div>")
    plays = raw.get("plays") or []
    out.append(f'<input type="hidden" name="play_count" value="{len(plays)}">')
    for i, p in enumerate(plays):
        c = p.get("conditions") or {}
        pre = f"p{i}_"
        out.append(
            f'<h2>Play: {esc(p.get("name", f"play{i + 1}"))}</h2><div class="box">'
            f'<input type="hidden" name="{pre}name" value="{esc(p.get("name", f"play{i + 1}"))}">'
            f'<div class="check"><input type="checkbox" name="{pre}enabled" value="1"'
            f'{" checked" if p.get("enabled", True) else ""}><span>This play is on</span></div>'
            f'<label>What it buys</label><select name="{pre}buy">{opt(BUY_CHOICES, p.get("buy", "favorite"))}</select>'
            f'<div class="row"><div><label>Lowest price (¢)</label>{inp(pre + "min", cents(p.get("min_price", 0.7)))}</div>'
            f'<div><label>Highest price (¢)</label>{inp(pre + "max", cents(p.get("max_price", 0.85)))}</div></div>'
            f'<div class="row"><div><label>Start (minutes left)</label>{inp(pre + "from", plain(p.get("act_from_minutes_left", 8)))}</div>'
            f'<div><label>Stop (minutes left)</label>{inp(pre + "until", plain(p.get("act_until_minutes_left", 7)))}</div></div>'
            f'<p class="hint">Example: 8 and 7 means it buys between 8 and 7 minutes before the market closes.</p>'
            f'<div class="row"><div><label>Take profit at (¢, blank = off)</label>{inp(pre + "tp", cents(p.get("take_profit")))}</div>'
            f'<div><label>Stop loss at (¢, blank = off)</label>{inp(pre + "sl", cents(p.get("stop_loss")))}</div></div>'
            f'<p class="hint">Both blank = hold to the end and get $1 per contract if it wins.</p>'
            f'<details><summary>Extra conditions</summary>'
            f'<label>Extra cents it may pay above the price on screen</label>{inp(pre + "slip", plain(p.get("slippage_cents", 0)))}'
            f'<label>Skip if the spread is wider than (¢, blank = off)</label>{inp(pre + "spread", plain(c.get("max_spread_cents")))}'
            f'<label>Skip if fewer contracts are offered than (blank = off)</label>{inp(pre + "depth", plain(c.get("min_contracts_at_price")))}'
            f'<label>Only buy if the side rose over the last N seconds (blank = off)</label>{inp(pre + "rising", plain(c.get("favorite_rising_seconds")))}'
            f'<label>Only buy if the price is this many $ past the target (blank = off)</label>{inp(pre + "dist", plain(c.get("btc_distance_min_dollars")))}'
            "</details></div>")
    out.append('<button>Save settings</button>'
               '<p class="hint">The bot restarts with the new settings in about 15 seconds. Open trades are kept.</p></form>')
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
            return self._send(200, status_page(self.app), set_cookie=from_link)
        if url.path == "/settings":
            msg = "Saved. The bot is restarting with the new settings." if "saved" in query else None
            return self._send(200, settings_page(self.app, message=msg), set_cookie=from_link)
        if url.path == "/trades.csv":
            s, _ = self.app.settings()
            path = os.path.join(self.app.folder(s), "trades.csv") if s else ""
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
                self.app.save_form(form)
            except SettingsError as e:
                try:
                    shown = form_to_raw(form, self.app.raw())
                except SettingsError:
                    shown = None
                return self._send(200, settings_page(self.app, raw=shown, error=str(e)))
            return self._redirect("/settings?saved=1")
        if url.path in ("/pause", "/resume"):
            self.app.set_paused(url.path == "/pause")
            return self._redirect("/")
        return self._send(404, page("Not found", '<div class="box">Not found.</div>', ""))


def make_server(settings_path: str, port: int, key: str, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": App(settings_path, key)})
    return ThreadingHTTPServer((host, port), handler)


def serve(settings_path: str, port: int) -> None:
    key = load_key()
    server = make_server(settings_path, port, key)
    print(f"website running on port {port}", flush=True)
    server.serve_forever()
