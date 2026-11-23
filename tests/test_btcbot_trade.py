"""Trades you make yourself on the website: the bot places them, so its records stay right."""
import json
import os
import unittest
from datetime import timedelta
from decimal import Decimal as D

from btcbot.bot import MANUAL, TRADE_FILE

from test_btcbot import CLOSE, TICKER, BotCase, market
from test_btcbot_live import MAIN, LiveCase
from test_btcbot_web import KEY, WebCase


def ask(folder, clock, minutes_left=None, **req):
    if minutes_left is not None:
        clock.t = CLOSE - timedelta(minutes=minutes_left)
    req.setdefault("at", clock.t.isoformat())
    with open(os.path.join(folder, TRADE_FILE), "w") as fh:
        json.dump(req, fh)


class TestRealMoneyTrades(LiveCase):
    def test_buy_up_now(self):
        self.make([MAIN])
        ask(self.ledger.folder, self.clock, 12, action="buy", side="yes", contracts=2, series="KXBTC15M")
        self.at(12, 0.68, 0.70)                              # outside main's window: only the trade happens
        self.assertEqual(len(self.client.orders), 1)
        self.assertEqual(self.client.orders[0]["subaccount"], 0)
        held = self.bot.state.open_positions()
        self.assertEqual([(p.play, p.side, p.contracts, p.subaccount) for p in held], [(MANUAL, "yes", "2", 0)])
        self.assertIn("BOUGHT 2 Up", self.bot.state.trade_note)
        self.assertFalse(os.path.exists(os.path.join(self.ledger.folder, TRADE_FILE)))
        self.bot.check_records({TICKER: None}, self.clock.t)  # Kalshi and the records agree
        self.assertEqual(self.bot.state.alert, "")

    def test_price_above_your_limit(self):
        self.make([MAIN])
        ask(self.ledger.folder, self.clock, 12, action="buy", side="yes", contracts=1, max_price="0.75")
        self.at(12, 0.88, 0.90)
        self.assertEqual(self.client.orders, [])
        self.assertIn("costs 90", self.bot.state.trade_note)

    def test_old_request_is_dropped(self):
        self.make([MAIN])
        ask(self.ledger.folder, self.clock, action="buy", side="no", contracts=1,
            at=(CLOSE - timedelta(minutes=20)).isoformat())
        self.at(12, 0.68, 0.70)
        self.assertEqual(self.client.orders, [])
        self.assertIn("too late", self.bot.state.trade_note)

    def test_sell_now(self):
        self.make([MAIN])
        ask(self.ledger.folder, self.clock, 12, action="buy", side="yes", contracts=1)
        self.at(12, 0.68, 0.70)
        ask(self.ledger.folder, self.clock, 11.9, action="sell", side="yes", ticker=TICKER)
        self.at(11.9, 0.80, 0.82)
        self.assertEqual(len(self.client.orders), 2)
        self.assertTrue(self.client.orders[1].get("reduce_only"))
        self.assertEqual(self.bot.state.open_positions(), [])
        self.assertIn("SOLD 1 Up", self.bot.state.trade_note)

    def test_wakes_up_for_a_trade(self):
        self.make([MAIN])
        naps = []
        self.clock.sleep = lambda s: naps.append(s)
        self.bot.nap(15)
        self.assertEqual(len(naps), 60)                      # a quarter second at a time
        naps.clear()
        ask(self.ledger.folder, self.clock, action="buy", side="yes")
        self.bot.nap(15)
        self.assertEqual(naps, [])                           # a trade is waiting: no sleep


class TestPretendTrades(BotCase):
    def test_paper_mode_uses_pretend_money(self):
        bot = self.make()
        bot.clock.t = CLOSE - timedelta(minutes=12)
        ask(bot.ledger.folder, bot.clock, action="buy", side="no", contracts=1)
        self.client.open = [market(0.68, 0.70)]
        bot.step()
        self.assertIn("BOUGHT 1 Down", bot.state.trade_note)
        self.assertLess(D(bot.state.paper_cash), D("11"))


class TestTradeBox(WebCase):
    def go_live(self):
        import yaml
        with open(self.path) as fh:
            raw = yaml.safe_load(fh)
        raw.update(mode="live", live_confirm="I accept real-money risk")
        raw["plays"] = raw["plays"][:1]
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)

    def trade_file(self):
        from btcbot.settings import load, mode_folder
        return os.path.join(mode_folder(load(self.path)), TRADE_FILE)

    def test_the_tapped_button_decides_the_side(self):
        # A browser sends the form's hidden fields, then the button that was tapped.
        import re
        _, body, _ = self.call(f"/?key={KEY}")
        form = body[body.index('<form method="post" action="/trade"><input type="hidden" name="action" value="buy">'):]
        form = form[:form.index("</form>")]
        fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form)
        self.assertNotIn("side", [n for n, _ in fields])      # only the buttons say Up or Down
        self.call("/trade", data=[("key", KEY)] + fields + [("contracts", "1"), ("side", "no")])
        with open(self.trade_file()) as fh:
            self.assertEqual(json.load(fh)["side"], "no")
        os.remove(self.trade_file())
        # A page loaded before this fix still sent side=yes first: the tapped button (last) wins.
        self.call("/trade", data=[("key", KEY), ("action", "buy"), ("side", "yes"), ("contracts", "1"),
                                  ("series", "KXBTC15M"), ("side", "no")])
        with open(self.trade_file()) as fh:
            self.assertEqual(json.load(fh)["side"], "no")

    def test_paper_mode_trade_is_sent_to_the_bot(self):
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Make a trade", body)
        self.assertIn("▲ Buy Up", body)
        self.assertLess(body.index("▼ Buy Down"), body.index("▲ Buy Up"))     # Down left, Up right, like the prices
        self.call("/trade", data={"key": KEY, "action": "buy", "side": "no", "contracts": "3", "max": "40",
                                  "series": "KXBTC15M"})
        with open(self.trade_file()) as fh:
            req = json.load(fh)
        self.assertEqual((req["side"], req["contracts"], req["max_price"]), ("no", 3, "0.4"))
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Sent to the bot: buy Down", body)

    def test_real_money_needs_the_locked_link(self):
        self.go_live()
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("only on the 🔒 locked link", body)
        self.call("/trade", data={"key": KEY, "action": "buy", "side": "yes", "contracts": "1"})
        self.assertFalse(os.path.exists(self.trade_file()))
        self.server.secure = True                            # as if on the locked link
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertNotIn("Buy with REAL money now?", body)     # no pop-up: one tap buys
        self.assertIn("with real money", body)
        self.call("/trade", data={"key": KEY, "action": "buy", "side": "yes", "contracts": "1"})
        self.assertTrue(os.path.exists(self.trade_file()))


if __name__ == "__main__":
    unittest.main()


class TestSmallerUpdates(WebCase):
    def test_live_updates_are_compressed_when_the_phone_accepts_it(self):
        import gzip
        import urllib.request
        from datetime import datetime, timedelta, timezone
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        os.makedirs(folder, exist_ok=True)
        close = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = [{"ticker": f"KXBTC15M-{i}", "series": "KXBTC15M", "close_time": close.isoformat(),
                 "yes_bid": "0.40", "yes_ask": "0.42", "target": "60000"} for i in range(3)]
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": rows}, fh)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/live",
                                     headers={"Cookie": f"btcbot_key={KEY}", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        data = json.loads(gzip.decompress(raw))
        self.assertIn("live-markets", data["parts"])
        self.assertLess(len(raw), len(gzip.decompress(raw)))
        _, body, headers = self.call(f"/?key={KEY}")            # pages with the private link: never compressed
        self.assertNotIn("Content-Encoding", headers)
