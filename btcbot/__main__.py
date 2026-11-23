"""Start the bot from the command line.

    python -m btcbot check    # read settings, show today's market and what each play would do
    python -m btcbot run      # run the bot (paper or live, from settings.yaml)
    python -m btcbot score    # report card for finished trades
    python -m btcbot web      # the private website (status and settings)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import requests

from . import keystore, rules
from .bot import Bot, RealClock, run_together
from .broker import LiveBroker, PaperBroker
from .kalshi import KalshiClient, KalshiError, load_private_key
from .ledger import Ledger, score
from .settings import DEFAULT_PATH, SettingsError, load, mode_folder, split_pretend


def setup_logging(folder: str, tz: str) -> None:
    os.makedirs(folder, exist_ok=True)
    zone = ZoneInfo(tz)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, zone).timetuple()
    root = logging.getLogger("btcbot")
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    RotatingFileHandler(os.path.join(folder, "bot.log"), maxBytes=2_000_000, backupCount=3)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def make_client(settings, need_key: bool) -> KalshiClient:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not (key_id and key_path):  # the key saved from the website
        key_id, key_path = keystore.load_key()
    if need_key and not (key_id and key_path):
        raise SettingsError("real-money mode needs your Kalshi API key: add it on the website's "
                            "Settings page (Real money), over the secure https link")
    private_key = load_private_key(key_path) if (key_id and key_path and need_key) else None
    return KalshiClient(settings.api_base, key_id if private_key is not None else None, private_key)


def settings_watcher(path: str):
    """True once the settings file changes, so the bot restarts with the new settings."""
    def mtime():
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None
    start = mtime()

    def changed() -> bool:
        if mtime() != start:
            logging.getLogger("btcbot").info("settings changed; restarting to use them")
            return True
        return False
    return changed


def cmd_run(settings, path: str = DEFAULT_PATH) -> None:
    folder = mode_folder(settings)
    setup_logging(folder, settings.day_timezone)
    client = make_client(settings, need_key=settings.live)
    ledger = Ledger(folder)
    state = ledger.load_state(settings.paper_balance)
    if settings.live:
        broker = LiveBroker(client)
        try:
            logging.getLogger("btcbot").warning("LIVE MODE: real money. Main account balance $%.2f",
                                                broker.balance(0))
        except Exception as e:  # the bot keeps going; it reads the balance again when it needs it
            logging.getLogger("btcbot").warning("LIVE MODE: real money (balance not read yet: %s)", e)
    else:
        broker = PaperBroker(Decimal(state.paper_cash))
    real_s, fake_s = split_pretend(settings)
    bot = Bot(real_s, client, broker, ledger, state, RealClock())
    if settings.live:
        bot.prepare_live()
    if fake_s is None:
        bot.run_forever(settings_watcher(path))
        return
    # Plays switched to "pretend money only" run beside the real ones, with their own
    # pretend money and records (the paper folder), and never touch Kalshi money.
    fake_ledger = Ledger(mode_folder(settings, "paper"))
    fake_state = fake_ledger.load_state(settings.paper_balance)
    fake = Bot(fake_s, client, PaperBroker(Decimal(fake_state.paper_cash)), fake_ledger, fake_state, bot.clock)
    fake.stop_file = bot.stop_file          # the Pause button stops both
    run_together(bot, fake, settings_watcher(path))


def cmd_check(settings) -> None:
    print(f"Mode: {settings.mode.upper()}   Markets: {', '.join(settings.markets)}")
    for p in settings.plays:
        exits = []
        if p.take_profit is not None:
            exits.append(f"take profit at {p.take_profit}")
        if p.stop_loss is not None:
            exits.append(f"stop loss at {p.stop_loss}")
        size = settings.size_for(p)
        print(f"- play '{p.name}' ({'ON' if p.enabled else 'off'}): buy {p.buy} at {p.min_price}-{p.max_price}, "
              f"from {p.act_from_minutes_left} to {p.act_until_minutes_left} min left, "
              f"{', '.join(exits) or 'hold to the end'}; size {size.value} {size.mode}"
              f"{'' if p.size else ' (default)'}")
    client = make_client(settings, need_key=settings.live)
    now = RealClock().now()
    for series in settings.markets:
        markets = client.open_markets(series)
        print(f"\n{series}: {len(markets)} open market(s)")
        for m in markets:
            q = rules.Quote.from_market(m, series)
            fav = rules.favorite(q)
            print(f"  {q.ticker}: {q.minutes_left(now):.1f} min left, target {q.target}, "
                  f"Up {q.ask('yes')} / Down {q.ask('no')} to buy, favorite "
                  f"{rules.side_name(fav) if fav else 'none'}")
            for p in settings.plays:
                if p.enabled:
                    chk = rules.check_entry(p, q, now)
                    said = (f"would buy {rules.side_name(chk.side)} at {chk.ask} right now"
                            if chk.ok else f"would not buy: {chk.reason}")
                    print(f"    {p.name}: {said}")
    if settings.live:
        print(f"\nKalshi balance: ${client.balance_dollars():.2f} (key works)")
        if any(p.subaccount for p in settings.plays if p.enabled):
            try:
                print(f"Kalshi API level: {client.api_level() or 'unknown'}")
                for n, cash in sorted(client.subaccount_balances().items()):
                    names = [p.name for p in settings.plays if p.enabled and p.subaccount == n]
                    print(f"  subaccount {n}: ${cash:.2f}" + (f" ({', '.join(names)})" if names else ""))
            except Exception as e:
                print(f"Could not list subaccounts yet ({e}); the bot sets them up when it starts.")
    print("\nNo orders were placed.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="btcbot", description="Kalshi 15-minute market bot")
    ap.add_argument("command", choices=["run", "check", "score", "web"])
    ap.add_argument("--settings", default=DEFAULT_PATH)
    ap.add_argument("--port", type=int, default=8080, help="website port (web only)")
    args = ap.parse_args(argv)
    if args.command == "web":  # the website must start even when the settings are broken
        from .web import serve
        serve(args.settings, args.port)
        return 0
    try:
        settings = load(args.settings)
    except SettingsError as e:
        print(f"Settings problem: {e}")
        return 2
    if args.command == "score":
        print(score(os.path.join(mode_folder(settings), "trades.csv")))
        return 0
    try:
        if args.command == "check":
            cmd_check(settings)
        else:
            cmd_run(settings, args.settings)
    except SettingsError as e:
        print(f"Settings problem: {e}")
        return 2
    except KalshiError as e:
        print(f"Kalshi said no: {e}")
        if e.status in (401, 403):
            print("That usually means the API key ID or key file is wrong.")
        return 3
    except requests.RequestException as e:
        print(f"Could not reach Kalshi (internet problem?): {e}")
        return 3
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
