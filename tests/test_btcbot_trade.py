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

    def test_buy_more_of_the_same_side_but_not_the_other(self):
        self.make([MAIN])
        ask(self.ledger.folder, self.clock, 12, action="buy", side="yes", contracts=1, series="KXBTC15M")
        self.at(12, 0.68, 0.70)
        ask(self.ledger.folder, self.clock, 11.5, action="buy", side="yes", contracts=1, series="KXBTC15M")
        self.at(11.5, 0.70, 0.72)
        self.assertEqual(len(self.bot.state.open_positions()), 2)          # added to the Up bet
        ask(self.ledger.folder, self.clock, 11, action="buy", side="no", contracts=1, series="KXBTC15M")
        self.at(11, 0.70, 0.72)
        self.assertEqual(len(self.client.orders), 2)                       # Down was not bought
        self.assertIn("you already hold Up", self.bot.state.trade_note)
        self.assertIn("Sell now", self.bot.state.trade_note)

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


class TestAutoBuy(LiveCase):
    """The auto-buy switch: the bot buys the moment the buy button lights up (once per market)."""

    def switch(self, on=True, contracts=1, stop=3):
        from btcbot.bot import AUTO_FILE
        with open(os.path.join(self.ledger.folder, AUTO_FILE), "w") as fh:
            json.dump({"on": on, "contracts": contracts, "stop": stop}, fh)

    def go(self, minutes, bid, ask, price="60040", book=True):
        self.bot.spot_fn = lambda product: D(price)
        self.bot.index_fn = lambda coin: (D(price), 3)
        self.bot.sigma_fn = lambda coin: 3.0                 # a calm day: about $3 a second
        if book:                                             # live order-book prices, read this second
            self.client.best_prices = lambda t: {"yes_bid": D(str(bid)), "yes_ask": D(str(ask)),
                                                 "yes_bid_size": D(5), "yes_ask_size": D(5)}
        self.at(minutes, bid, ask)

    def test_buys_once_when_it_lights_up(self):
        from btcbot.bot import AUTO
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch(contracts=2)
        self.go(3, 0.69, 0.70)                               # $40 ahead with 3 min left, Up only 70c: 7+
        held = self.bot.state.open_positions()
        self.assertEqual([(p.play, p.side, p.contracts, p.subaccount) for p in held], [(AUTO, "yes", "2", 0)])
        self.assertIn("auto-buy BOUGHT 2 Up", self.bot.state.trade_note)
        self.assertEqual(self.client.orders[0]["price"], D("0.71"))           # at most 1c over the price shown
        self.go(2.9, 0.69, 0.70)
        self.assertEqual(len(self.client.orders), 1)         # once per market

    def test_can_go_for_the_other_side_after_a_miss(self):
        from btcbot.bot import AUTO
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch()
        self.bot.state.attempts[f"{AUTO}|{TICKER}"] = 1      # it went for Up here and missed
        self.go(3, 0.30, 0.31, price="59960")               # now Down is our pick and cheap: buys it
        self.assertEqual([p.side for p in self.bot.state.open_positions()], ["no"])

    def test_skips_coin_flip_prices(self):
        self.make([MAIN])
        self.switch()
        self.go(3, 0.53, 0.54, price="59995")               # Down is our pick at 47c: under 50c, skip
        self.assertEqual(self.client.orders, [])

    def test_off_or_missing_does_nothing(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.go(3, 0.69, 0.70)
        self.switch(on=False)
        self.go(2.9, 0.69, 0.70)
        self.assertEqual(self.client.orders, [])

    def test_never_on_old_list_prices(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch()
        self.go(3, 0.69, 0.70, book=False)                   # no live book this second
        self.assertEqual(self.client.orders, [])

    def test_only_in_the_last_4_minutes(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch()
        self.go(6, 0.60, 0.61)
        self.assertEqual(self.client.orders, [])

    def test_not_when_the_price_is_fair(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch()
        self.go(3, 0.92, 0.93)                               # a ³, but Kalshi has caught up: score under 7
        self.assertEqual(self.client.orders, [])

    def test_not_when_you_already_hold_the_market(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        ask(self.ledger.folder, self.clock, 3.2, action="buy", side="yes", contracts=1, series="KXBTC15M")
        self.go(3.2, 0.69, 0.70)                             # your own buy (and auto-buy waits)
        self.switch()
        self.go(3, 0.69, 0.70)
        self.assertEqual(len(self.client.orders), 1)
        self.assertEqual([p.play for p in self.bot.state.open_positions()], [MANUAL])

    def test_daily_stop_switches_it_off(self):
        from btcbot.bot import AUTO_FILE
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch(stop=3)
        self.bot.state.auto_day = self.clock.t.astimezone(__import__("zoneinfo").ZoneInfo(self.s.day_timezone)).date().isoformat()
        self.bot.state.auto_profit = "-3.20"
        self.go(3, 0.69, 0.70)
        self.assertEqual(self.client.orders, [])
        with open(os.path.join(self.ledger.folder, AUTO_FILE)) as fh:
            cfg = json.load(fh)
        self.assertFalse(cfg["on"])
        self.assertIn("lost $3.20 today", cfg["note"])

    def test_a_settled_loss_counts_toward_the_stop(self):
        self.make([MAIN])                                    # main only buys 8-7 min left
        self.switch()
        self.go(3, 0.69, 0.70)
        [pos] = self.bot.state.open_positions()
        self.bot._close(pos, pos.d("contracts"), "settled", None, "no")
        self.assertLess(D(self.bot.state.auto_profit), D("-0.70"))
        self.assertLess(self.bot.auto_lost_today(self.clock.t) - D("0.70"), D("0.05"))


class TestAutoBuySwitch(WebCase):
    def test_switch_on_and_off(self):
        from btcbot.bot import AUTO_FILE
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        body = self.call(f"/?key={KEY}")[1]
        self.assertIn("Auto-buy when it lights up", body)
        self.call("/autobuy", data={"key": KEY, "on": "1", "contracts": "2", "stop": "5"})
        with open(os.path.join(folder, AUTO_FILE)) as fh:
            cfg = json.load(fh)
        self.assertEqual((cfg["on"], cfg["contracts"], cfg["stop"]), (True, 2, 5.0))
        body = self.call(f"/?key={KEY}")[1]
        self.assertIn("Buys 2 the moment a button lights up", body)
        self.call("/autobuy", data={"key": KEY, "contracts": "2", "stop": "5"})          # unticked: no "on"
        with open(os.path.join(folder, AUTO_FILE)) as fh:
            self.assertFalse(json.load(fh)["on"])
        self.call("/autobuy", data={"key": KEY, "on": "1", "contracts": "99", "stop": "5"})
        with open(os.path.join(folder, AUTO_FILE)) as fh:
            self.assertFalse(json.load(fh)["on"])                       # 99 contracts refused, left off
