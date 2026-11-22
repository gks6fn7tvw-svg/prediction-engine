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
        "p0_spread": "3", "p0_depth": "", "p0_rising": "", "p0_dist": "",
    }
    form.update(over)
    return form


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

    def test_shows_trades_positions_and_activity(self):
        folder = mode_folder(load(self.path))
        ledger = Ledger(folder)
        state = ledger.load_state(D(11))
        state.paper_cash = "10.18"
        state.set_positions([Position("main", "KXBTC15M-X", "KXBTC15M", "no", "1", "0.80", "0.02",
                                      "2026-11-22T03:00:00+00:00", "2026-11-22T03:15:00+00:00")])
        ledger.save_state(state)
        ledger.record_trade({"opened_at": "2026-11-22T02:52:00+00:00", "closed_at": "2026-11-22T03:00:20+00:00",
                             "mode": "paper", "play": "main", "market": "KXBTC15M-Y", "side": "DOWN",
                             "contracts": "1", "entry_price": "0.80", "entry_fee": "0.02", "exit_type": "settled",
                             "exit_price": "1", "exit_fee": "0", "result": "DOWN", "payout": "1", "profit": "0.18"})
        with open(os.path.join(folder, "bot.log"), "w") as fh:
            fh.write("2026-11-21 17:07:30 INFO main: BOUGHT KXBTC15M-X DOWN x1 at 0.80\n")
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Running", body)
        self.assertIn("$10.18", body)
        self.assertIn("DOWN won", body)
        self.assertIn("5:07 pm", body)
        self.assertIn("Open trades", body)
        status, csv_text, headers = self.call("/trades.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("KXBTC15M-Y", csv_text)

    def test_broken_settings_file_shows_the_problem(self):
        with open(self.path, "w") as fh:
            fh.write("mode: sideways\n")
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Settings problem", body)


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
