"""Offline tests for the bot's private website."""
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import yaml

from btcbot import web
from btcbot.__main__ import settings_watcher
from btcbot.ledger import Ledger, Position
from btcbot.settings import load, mode_folder

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "btcbot", "settings.example.yaml")
KEY = "test-key-123"


def read(path):
    with open(path) as fh:
        return fh.read()


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def good_form(**over):
    form = {
        "key": KEY, "market_KXBTC15M": "1", "market_other": "", "paper_balance": "20",
        "balance_share_pct": "50", "size_mode": "contracts", "size_value": "2", "max_contracts": "3",
        "loss_mode": "dollars", "loss_value": "4", "max_trades_per_day": "0", "play_count": "1",
        "p0_name": "main", "p0_enabled": "1", "p0_buy": "favorite", "p0_min": "75", "p0_max": "88",
        "p0_slip": "0", "p0_from": "1", "p0_until": "0", "p0_tp": "", "p0_sl": "40",
        "p0_spread": "3", "p0_depth": "", "p0_rising": "", "p0_dist": "", "p0_size_mode": "default",
    }
    form.update(over)
    return form


def offline(api_base, series):
    raise OSError("no network in tests")


class WebCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        with open(EXAMPLE) as fh:
            raw = yaml.safe_load(fh)
        raw["files"] = {"folder": os.path.join(self.dir, "data")}
        self.path = os.path.join(self.dir, "settings.yaml")
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        self.server = web.make_server(self.path, 0, KEY, host="127.0.0.1")
        self.port = self.server.server_address[1]
        self.app = self.server.RequestHandlerClass.app
        self.app.fetcher = offline              # tests never reach Kalshi
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, data=None, cookie=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        if cookie:
            req.add_header("Cookie", cookie)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)


class TestAccess(WebCase):
    def test_needs_the_key(self):
        status, body, _ = self.call("/")
        self.assertEqual(status, 403)
        self.assertIn("private", body)
        self.assertEqual(self.call("/?key=wrong")[0], 403)
        self.assertEqual(self.call("/settings", data={"key": "wrong"})[0], 403)

    def test_link_sets_a_cookie_that_works_later(self):
        status, body, headers = self.call(f"/?key={KEY}")
        self.assertEqual(status, 200)
        self.assertIn("Kalshi bot", body)
        self.assertIn("btcbot_key=", headers.get("Set-Cookie", ""))
        status, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertEqual(status, 200)
        self.assertIn("Save settings", body)


class TestSettings(WebCase):
    def test_saving_writes_checked_settings(self):
        status, _, headers = self.call("/settings", data=good_form())
        self.assertEqual(status, 303)
        s = load(self.path)
        p = s.plays[0]
        self.assertEqual((p.min_price, p.max_price, p.stop_loss, p.take_profit),
                         (D("0.75"), D("0.88"), D("0.4"), None))
        self.assertEqual((p.act_from_minutes_left, p.act_until_minutes_left), (D(1), D(0)))
        self.assertEqual(p.conditions.max_spread_cents, D(3))
        self.assertEqual((s.size_value, s.max_contracts, s.paper_balance), (D(2), D(3), D(20)))
        self.assertEqual(len(s.plays), 1)

    def test_bad_settings_are_not_saved(self):
        before = read(self.path)
        status, body, _ = self.call("/settings", data=good_form(p0_min="90", p0_max="80"))
        self.assertEqual(status, 200)
        self.assertIn("Not saved", body)
        self.assertEqual(read(self.path), before)
        status, body, _ = self.call("/settings", data=good_form(p0_min="seventy"))
        self.assertIn("is not a number", body)

    def test_website_cannot_switch_to_real_money(self):
        self.call("/settings", data=good_form(mode="live", live_confirm="I accept real-money risk"))
        self.assertEqual(load(self.path).mode, "paper")

    def test_page_shows_current_values_in_cents(self):
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn('name="p0_min" value="70"', body)
        self.assertIn('name="p0_max" value="85"', body)


class TestStatus(WebCase):
    def test_pause_and_resume(self):
        folder = mode_folder(load(self.path))
        self.call("/pause", data={"key": KEY})
        self.assertTrue(os.path.exists(os.path.join(folder, "STOP")))
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Paused", body)
        self.call("/resume", data={"key": KEY})
        self.assertFalse(os.path.exists(os.path.join(folder, "STOP")))

    def setup_bets(self):
        folder = mode_folder(load(self.path))
        ledger = Ledger(folder)
        state = ledger.load_state(D(11))
        state.paper_cash = "10.18"
        close = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
        state.set_positions([Position("main", "KXBTC15M-X", "KXBTC15M", "no", "1", "0.80", "0.02",
                                      "2026-11-22T03:00:00+00:00", close)])
        ledger.save_state(state)
        ledger.save_market(datetime.now(timezone.utc), [
            {"ticker": "KXBTC15M-X", "series": "KXBTC15M", "close_time": close,
             "yes_bid": "0.12", "yes_ask": "0.14", "target": "59229.55"}])
        add_trade(ledger, "main", "0.18", side="DOWN", result="DOWN")
        with open(os.path.join(folder, "bot.log"), "w") as fh:
            fh.write("2026-11-21 17:07:30 INFO main: BOUGHT KXBTC15M-X DOWN x1 at 0.80\n"
                     "2026-11-21 17:08:00 INFO cheap: skip KXBTC15M-X: UP costs 0.14\n")
        return folder

    def test_overview_shows_money_market_and_plays(self):
        self.setup_bets()
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Running", body)
        self.assertIn("$10.18", body)
        self.assertIn("5:07 pm", body)
        self.assertIn("1 open", body)
        self.assertIn("target $59,229.55", body)          # live market card
        self.assertIn("88¢", body)                         # buying Down costs 1 - Up's bid (0.12)
        self.assertIn('href="/play?name=main"', body)

    def test_play_page_shows_its_own_bets(self):
        self.setup_bets()
        _, body, _ = self.call(f"/play?name=main&key={KEY}")
        self.assertIn("Open bets · 1", body)
        self.assertIn("now 86¢", body)                     # selling Down gets 1 - Up's ask (0.14)
        self.assertIn("Closed bets · 1", body)
        self.assertIn("DOWN won", body)
        self.assertIn("main: BOUGHT", body)
        self.assertNotIn("cheap: skip", body)              # only this play's activity
        _, body, _ = self.call(f"/play?name=nothing&key={KEY}")
        self.assertIn("No play called", body)
        status, csv_text, headers = self.call("/trades.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("KXBTC15M-Y", csv_text)

    def test_broken_settings_file_shows_the_problem(self):
        with open(self.path, "w") as fh:
            fh.write("mode: sideways\n")
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Settings problem", body)


def add_trade(ledger, play, profit, side="UP", result="DOWN", exit_type="settled", exit_price="0", price="0.10"):
    ledger.record_trade({"opened_at": "2026-11-22T02:52:00+00:00", "closed_at": "2026-11-22T03:00:20+00:00",
                         "mode": "paper", "play": play, "market": "KXBTC15M-Y", "side": side,
                         "contracts": "1", "entry_price": price, "entry_fee": "0.01", "exit_type": exit_type,
                         "exit_price": exit_price, "exit_fee": "0", "result": result, "payout": "0",
                         "profit": profit})


class TestPlaysAndUpdates(WebCase):
    def test_each_play_gets_its_own_page_and_score(self):
        ledger = Ledger(mode_folder(load(self.path)))
        add_trade(ledger, "main", "0.18")
        add_trade(ledger, "cheap", "-0.11")
        add_trade(ledger, "cheap", "0.13", exit_type="take_profit", exit_price="0.25", result="")
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('href="/play?name=cheap"', body)
        self.assertIn("REMOVED", body)                    # cheap is not in the settings any more
        _, body, _ = self.call(f"/play?name=cheap&key={KEY}")
        self.assertIn("1 won · 1 lost", body)
        self.assertIn("sold at 25¢ (take profit)", body)
        self.assertNotIn("Edit this play", body)
        _, body, _ = self.call(f"/play?name=main&key={KEY}")
        self.assertIn("1 won · 0 lost", body)
        self.assertIn("Edit this play", body)

    def test_update_button(self):
        app = self.server.RequestHandlerClass.app
        calls = []
        app.updater = lambda: True
        app.restarter = lambda path: calls.append(path) or True
        _, body, _ = self.call("/update", data={"key": KEY})
        self.assertIn("Updated", body)
        self.assertEqual(calls, [self.path])
        app.updater = lambda: False
        _, body, _ = self.call("/update", data={"key": KEY})
        self.assertIn("Already the newest", body)
        self.assertEqual(len(calls), 1)

        def broken():
            raise RuntimeError("couldn't reach GitHub")
        app.updater = broken
        _, body, _ = self.call("/update", data={"key": KEY})
        self.assertIn("Not updated", body)
        self.assertEqual(self.call("/update", data={"key": "wrong"})[0], 403)

    def test_status_page_shows_version(self):
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Get the newest version", body)


class TestPlayButtons(WebCase):
    def form_from_page(self):
        """Every field as the settings page shows it now, like a browser would send."""
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        import re
        form = {"key": KEY}
        for name, value in re.findall(r'<input name="([^"]+)" value="([^"]*)"', body):
            form[name] = value.replace("&amp;", "&")
        for name in re.findall(r'<input type="checkbox" name="([^"]+)" value="1" checked>', body):
            form[name] = "1"
        for name, opts in re.findall(r'<select name="([^"]+)">(.*?)</select>', body):
            chosen = re.search(r'<option value="([^"]+)" selected>', opts)
            form[name] = chosen.group(1) if chosen else re.search(r'<option value="([^"]+)"', opts).group(1)
        form["play_count"] = re.search(r'name="play_count" value="(\d+)"', body).group(1)
        return form

    def test_add_copy_rename_and_delete(self):
        form = self.form_from_page()
        status, _, headers = self.call("/settings", data=dict(form, action="add"))
        self.assertEqual(status, 303)
        self.assertIn("done=added#play-2", headers["Location"])
        s = load(self.path)
        self.assertEqual([p.name for p in s.plays], ["main", "second", "new play"])
        self.assertFalse(s.plays[2].enabled)

        form = self.form_from_page()
        self.call("/settings", data=dict(form, action="copy:0"))
        s = load(self.path)
        self.assertEqual([p.name for p in s.plays], ["main", "main copy", "second", "new play"])
        self.assertFalse(s.plays[1].enabled)
        self.assertEqual(s.plays[1].min_price, s.plays[0].min_price)

        form = self.form_from_page()
        form["p1_name"] = "cheap 25"
        form["p1_enabled"] = "1"
        form["p1_buy"] = "underdog"
        form["p1_min"], form["p1_max"], form["p1_from"], form["p1_until"] = "5", "15", "15", "11"
        form["p1_tp"] = "25"
        form["p1_size_mode"], form["p1_size_value"] = "dollars", "3"
        self.call("/settings", data=dict(form, action="save"))
        s = load(self.path)
        cheap = s.plays[1]
        self.assertEqual((cheap.name, cheap.enabled, cheap.buy), ("cheap 25", True, "underdog"))
        self.assertEqual((cheap.min_price, cheap.max_price, cheap.take_profit), (D("0.05"), D("0.15"), D("0.25")))
        self.assertEqual((cheap.size.mode, cheap.size.value, cheap.size.max_contracts), ("dollars", D(3), D(0)))
        self.assertIsNone(s.plays[0].size)                 # main keeps the default size
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn('name="p1_size_value" value="3"', body)

        form = self.form_from_page()
        self.call("/settings", data=dict(form, action="delete:3"))
        self.assertEqual([p.name for p in load(self.path).plays], ["main", "cheap 25", "second"])

    def test_buttons_with_bad_input_save_nothing(self):
        before = read(self.path)
        form = self.form_from_page()
        form["p0_name"] = "a|b"
        _, body, _ = self.call("/settings", data=dict(form, action="add"))
        self.assertIn("Not saved", body)
        form = self.form_from_page()
        form["p1_name"] = "main"
        _, body, _ = self.call("/settings", data=dict(form, action="save"))
        self.assertIn("its own name", body)
        form = self.form_from_page()
        _, body, _ = self.call("/settings", data=dict(form, action="delete:9"))
        self.assertIn("please reload the page", body)
        self.assertEqual(read(self.path), before)

    def test_cannot_delete_the_only_play(self):
        form = self.form_from_page()
        self.call("/settings", data=dict(form, action="delete:1"))
        form = self.form_from_page()
        _, body, _ = self.call("/settings", data=dict(form, action="delete:0"))
        self.assertIn("at least one play", body)


class TestLive(WebCase):
    def row(self, minutes=6, bid="0.20", ask="0.22"):
        close = (datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=30)).isoformat()
        return {"ticker": "KXBTC15M-L", "series": "KXBTC15M", "close_time": close,
                "yes_bid": bid, "yes_ask": ask, "target": "60000"}

    def live(self, path="/api/live"):
        import json as _json
        status, body, headers = self.call(path, cookie=f"btcbot_key={KEY}")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        return _json.loads(body)

    def test_needs_the_key(self):
        self.assertEqual(self.call("/api/live")[0], 403)

    def test_pages_poll_for_live_numbers(self):
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('data-live="/api/live"', body)
        self.assertIn('id="live-dot"', body)
        self.assertNotIn('http-equiv="refresh"', body)
        _, body, _ = self.call(f"/play?name=main&key={KEY}")
        self.assertIn('data-live="/api/live?play=main"', body)

    def test_asks_kalshi_when_the_bot_has_not_saved_fresh_prices(self):
        calls = []
        self.app.fetcher = lambda base, series: calls.append(series) or [self.row()]
        j = self.live()
        self.assertIn("80¢", j["parts"]["live-markets"])          # buying Down costs 1 - Up's bid (0.20)
        self.assertIn("6:", j["parts"]["live-markets"])           # countdown with seconds
        self.live()
        self.assertEqual(calls, ["KXBTC15M"])                      # second call within a second uses the cache

    def test_uses_the_bots_prices_when_fresh(self):
        self.app.fetcher = lambda base, series: self.fail("should not ask Kalshi")
        Ledger(mode_folder(load(self.path))).save_market(datetime.now(timezone.utc), [self.row(bid="0.30", ask="0.33")])
        j = self.live()
        self.assertIn("70¢", j["parts"]["live-markets"])          # Down = 1 - 0.30

    def test_signature_changes_when_a_bet_closes(self):
        first = self.live()["sig"]
        self.assertEqual(self.live()["sig"], first)
        add_trade(Ledger(mode_folder(load(self.path))), "main", "0.18")
        self.assertNotEqual(self.live()["sig"], first)

    def test_play_view_sends_its_open_bets(self):
        ledger = Ledger(mode_folder(load(self.path)))
        state = ledger.load_state(D(11))
        r = self.row(bid="0.17", ask="0.19")
        state.set_positions([Position("main", r["ticker"], "KXBTC15M", "yes", "25", "0.12", "0.19",
                                      datetime.now(timezone.utc).isoformat(), r["close_time"])])
        ledger.save_state(state)
        self.app.fetcher = lambda base, series: [r]
        j = self.live("/api/live?play=main")
        self.assertIn("now 17¢", j["parts"]["live-open"])
        self.assertIn('data-k="now-main|KXBTC15M-L"', j["parts"]["live-open"])
        self.assertIn("+$1.06", j["parts"]["live-open"])        # (0.17 - 0.12) x 25 - 0.19 fee
        j = self.live()
        self.assertIn("main · closes", j["parts"]["live-open"])  # overview names the play


class TestPaperMoney(WebCase):
    def test_low_money_warning_and_reset_button(self):
        folder = mode_folder(load(self.path))
        ledger = Ledger(folder)
        state = ledger.load_state(D(11))
        state.paper_cash = "0.001"
        ledger.save_state(state)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Pretend money is almost gone", body)
        form = TestPlayButtons.form_from_page(self)
        form["paper_balance"] = "50"
        status, _, headers = self.call("/settings", data=dict(form, action="reset_paper"))
        self.assertEqual(status, 303)
        self.assertIn("done=reset", headers["Location"])
        self.assertEqual(load(self.path).paper_balance, D(50))
        self.assertEqual(read(os.path.join(folder, "PAPER_RESET")), "50")
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Pretend money right now: $0.00", body)


class TestRestartOnChange(unittest.TestCase):
    def test_watcher_notices_a_saved_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "s.yaml")
        write(path, "a: 1\n")
        changed = settings_watcher(path)
        self.assertFalse(changed())
        time.sleep(0.01)
        write(path, "a: 2\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertTrue(changed())


class TestKey(unittest.TestCase):
    def test_key_is_made_once_and_private(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "web.key")
        k1 = web.load_key(path)
        self.assertGreaterEqual(len(k1), 20)
        self.assertEqual(web.load_key(path), k1)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
