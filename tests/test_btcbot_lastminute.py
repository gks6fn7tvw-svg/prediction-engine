"""Last-minute watch: settlement estimate, and the guard/follow ideas it only logs."""
import csv
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from btcbot import lastminute
from btcbot.lastminute import Tracker

from test_btcbot import CLOSE, TICKER, market
from test_btcbot_live import CHEAP, MAIN, LiveCase


class TestEstimate(unittest.TestCase):
    def test_half_the_minute_is_already_counted(self):
        close = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
        tr = Tracker(close, D("97000"))
        for s in range(60, 30, -1):                 # first 30 seconds of the final minute at $96,950
            tr.add(close - timedelta(seconds=s), D("96950"))
        tr.add(close - timedelta(seconds=30), D("96980"))   # now: $96,980
        est = tr.estimate(close - timedelta(seconds=30))
        self.assertEqual(est.locked, 30)
        self.assertAlmostEqual(float(est.projected), 96965.48, places=1)
        self.assertLess(est.gap, 0)
        self.assertEqual(est.favored(), "no")

    def test_before_the_last_minute_it_is_just_the_price(self):
        close = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
        tr = Tracker(close, D("97000"))
        tr.add(close - timedelta(seconds=90), D("97040"))
        est = tr.estimate(close - timedelta(seconds=90))
        self.assertEqual((est.projected, est.locked), (D("97040"), 0))

    def test_median_of_exchanges(self):
        class S:
            def get(self, url, params=None, timeout=None):
                class R:
                    def __init__(self, j): self._j = j
                    def json(self): return self._j
                if "coinbase" in url:
                    return R({"bid": "100", "ask": "102"})
                if "kraken" in url:
                    return R({"result": {"XXBTZUSD": {"b": ["99", "1"], "a": ["101", "1"]}}})
                raise OSError("bitstamp down")
        price, n = lastminute.index_proxy(S(), "BTC")
        self.assertEqual((price, n), (D("100.5"), 2))


class TestWatch(LiveCase):
    def setUp(self):
        super().setUp()
        self.price = D("60000")

    def run_bot(self, minutes_left, yes_bid, yes_ask, seconds=0):
        self.bot.index_fn = lambda coin: (self.price, 3)
        return self.at(minutes_left, yes_bid, yes_ask, seconds)

    def watch_rows(self):
        path = os.path.join(self.ledger.folder, "watch.csv")
        with open(path) as fh:
            return list(csv.DictReader(fh))

    def test_guard_only_watches_and_reports_what_it_would_have_saved(self):
        self.make([MAIN])
        self.run_bot(7.5, 0.80, 0.82)                       # main buys Up at 82c
        orders = len(self.client.orders)
        self.assertEqual(orders, 1)
        self.price = D("59950")                              # Bitcoin $50 under the target
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.run_bot(1, 0.55, 0.57)
        self.assertIn("the guard would sell now at 55", "\n".join(logs.output))
        self.assertEqual(len(self.client.orders), orders)   # nothing sent
        self.client.results[TICKER] = "no"
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.run_bot(-0.2, 0.01, 0.02)
        self.assertIn("the guard would have saved", "\n".join(logs.output))
        row = self.watch_rows()[0]
        self.assertEqual((row["kind"], row["play"], row["result"]), ("guard", "main", "DOWN"))
        self.assertGreater(D(row["difference"]), 0)
        self.assertEqual(self.bot.state.watch, {})

    def test_guard_cost_when_the_bet_comes_back(self):
        self.make([MAIN])
        self.run_bot(7.5, 0.80, 0.82)
        self.price = D("59950")
        self.run_bot(1, 0.55, 0.57)
        self.client.results[TICKER] = "yes"                  # it came back and won
        self.run_bot(-0.2, 0.98, 0.99)
        self.assertLess(D(self.watch_rows()[0]["difference"]), 0)

    def test_copy_the_crowd_notes_a_sell_when_our_side_falls(self):
        self.make([MAIN])
        self.run_bot(7.5, 0.80, 0.82)                       # bought Up at 82c
        self.price = D("60005")                              # our average still says Up, barely
        self.run_bot(1, 0.62, 0.64)                          # Up at 62c: nothing yet
        self.assertNotIn(f"crowd|main|{TICKER}", self.bot.state.watch)
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.run_bot(0, 0.48, 0.50, seconds=50)          # crowd sold Up down to 48c
        self.assertIn("fell to 48", "\n".join(logs.output))
        self.client.results[TICKER] = "no"
        self.run_bot(-0.2, 0.01, 0.02)
        rows = {r["kind"]: r for r in self.watch_rows()}
        self.assertGreater(D(rows["crowd"]["difference"]), 0)

    def test_follow_logs_a_pretend_buy_and_its_result(self):
        self.make([MAIN])
        self.price = D("60060")                              # $60 above the target
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.run_bot(0, 0.78, 0.80, seconds=40)          # Up still only 80c
        self.assertIn("follow would buy Bitcoin", "\n".join(logs.output))
        self.assertEqual(self.client.orders, [])
        self.client.results[TICKER] = "yes"
        self.run_bot(-0.2, 0.99, 1.0)
        row = self.watch_rows()[0]
        self.assertEqual((row["kind"], row["side"], row["price"]), ("follow", "UP", "0.8000"))
        self.assertAlmostEqual(float(row["would_profit"]), 0.20 - 0.02, places=2)

    def test_follow_skips_small_gaps_and_dear_prices(self):
        self.make([MAIN])
        self.price = D("60010")                              # only $10 past
        self.run_bot(0, 0.78, 0.80, seconds=40)
        self.price = D("60060")
        self.run_bot(0, 0.90, 0.92, seconds=39)              # Up already 92c (cap 85c)
        self.assertEqual(self.bot.state.watch, {})

    def test_records_the_final_stretch_and_shares_the_estimate(self):
        import json
        self.make([MAIN])
        self.price = D("59990")
        self.run_bot(0, 0.40, 0.42, seconds=30)
        folder = os.path.join(self.ledger.folder, "lastminute")
        files = os.listdir(folder)
        self.assertEqual(len(files), 1)
        with open(os.path.join(folder, files[0])) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[-1]["gap"], "-10.00")
        with open(os.path.join(self.ledger.folder, "market.json")) as fh:
            m = json.load(fh)["markets"][0]
        self.assertEqual(m["est_gap"], "-10.00")

    def test_records_a_futures_price_next_to_ours(self):
        self.make([MAIN])
        self.price = D("59990")
        self.bot.fut_source = "hyperliquid"
        self.bot.futures_fn = lambda source: D("60001.5")
        self.run_bot(0, 0.40, 0.42, seconds=30)
        folder = os.path.join(self.ledger.folder, "lastminute")
        with open(os.path.join(folder, os.listdir(folder)[0])) as fh:
            row = list(csv.DictReader(fh))[-1]
        self.assertEqual((row["fut_price"], row["fut_from"]), ("60001.5", "hyperliquid"))

    def test_looks_for_a_futures_market_in_the_background(self):
        import threading
        self.make([MAIN])
        done = threading.Event()

        def check():
            done.set()
            return [{"source": "binance", "label": "Binance futures", "ok": False},
                    {"source": "deribit", "label": "Deribit", "ok": True}]
        self.bot.futures_check_fn = check
        self.price = D("59990")
        self.run_bot(0, 0.40, 0.42, seconds=30)
        self.assertTrue(done.wait(2))
        for _ in range(50):
            if self.bot.fut_source:
                break
            import time
            time.sleep(0.01)
        self.assertEqual(self.bot.fut_source, "deribit")

    def test_switched_off(self):
        self.make([MAIN], last_minute={"enabled": False})
        self.price = D("60060")
        self.run_bot(0, 0.78, 0.80, seconds=40)
        self.assertEqual(self.bot.state.watch, {})
        self.assertFalse(os.path.exists(os.path.join(self.ledger.folder, "lastminute")))


class FakeFutures:
    """Hyperliquid and Deribit answer; Binance blocks this server; the rest time out."""
    class R:
        def __init__(self, code, j=None):
            self.status_code, self._j = code, j

        def json(self):
            return self._j

    def post(self, url, json=None, timeout=None):
        return self.R(200, {"BTC": "60123.5", "ETH": "3000"})

    def get(self, url, params=None, timeout=None):
        if "binance" in url:
            return self.R(451, {"msg": "restricted location"})
        if "deribit" in url:
            return self.R(200, {"result": {"best_bid_price": 60120.0, "best_ask_price": 60121.0}})
        raise TimeoutError("timed out")


class TestFutures(unittest.TestCase):
    def test_check_says_which_answer(self):
        results = {r["source"]: r for r in lastminute.futures_check(FakeFutures(), timeout=0.1)}
        self.assertEqual([r["source"] for r in lastminute.futures_check(FakeFutures())][:2], ["binance", "bybit"])
        self.assertTrue(results["hyperliquid"]["ok"])
        self.assertEqual(results["hyperliquid"]["price"], D("60123.5"))
        self.assertEqual(results["deribit"]["price"], D("60120.5"))
        self.assertFalse(results["binance"]["ok"])
        self.assertIn("blocked from here", results["binance"]["note"])
        self.assertFalse(results["okx"]["ok"])

    def test_mid_never_raises(self):
        self.assertIsNone(lastminute.futures_mid(FakeFutures(), "bybit"))
        self.assertEqual(lastminute.futures_mid(FakeFutures(), "hyperliquid"), D("60123.5"))


class TestSummary(unittest.TestCase):
    def test_totals(self):
        rows = [{"kind": "guard", "would_profit": "-0.50", "difference": "1.10"},
                {"kind": "guard", "would_profit": "-0.40", "difference": "-0.70"},
                {"kind": "follow", "would_profit": "0.18", "difference": "0.18"}]
        s = lastminute.summary(rows)
        self.assertEqual((s["guard"]["count"], s["guard"]["helped"]), (2, 1))
        self.assertEqual(s["guard"]["difference"], D("0.40"))
        self.assertEqual((s["follow"]["count"], s["follow"]["wins"]), (1, 1))


if __name__ == "__main__":
    unittest.main()


from test_btcbot_web import KEY, WebCase, good_form  # noqa: E402


class TestWatchPages(WebCase):
    def test_settings_save_and_show(self):
        form = dict(good_form(), lm_on="1", lm_guard_s="120", lm_guard_m="15", lm_follow_s="45",
                    lm_follow_m="40", lm_follow_max="80", lm_crowd_s="60", lm_crowd_p="55", action="save")
        status, _, _ = self.call("/settings", data=form)
        self.assertEqual(status, 303)
        from btcbot.settings import load
        w = load(self.path).watch
        self.assertEqual((w.guard_seconds, w.guard_margin, w.follow_seconds, w.follow_margin, w.follow_max_price),
                         (D("120"), D("15"), D("45"), D("40"), D("0.8")))
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn('name="lm_guard_s" value="120"', body)
        self.assertEqual((w.crowd_seconds, w.crowd_price), (D("60"), D("0.55")))

    def test_futures_check_button(self):
        import time
        from decimal import Decimal as Dd
        self.app.futures_check = lambda session: [
            {"source": "binance", "label": "Binance futures", "ok": False, "price": None, "ms": 80,
             "note": "answered 451 (blocked from here)"},
            {"source": "hyperliquid", "label": "Hyperliquid", "ok": True, "price": Dd("60123.5"), "ms": 120, "note": ""}]
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Futures price feeds", body)
        self.call("/futures", data={"key": KEY})
        for _ in range(50):
            if self.app.futures_job.get("state") == "done":
                break
            time.sleep(0.02)
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("✅ <b>Hyperliquid</b> · $60,124 · 120 ms", body)
        self.assertIn("✖ Binance futures · answered 451 (blocked from here)", body)

    def test_overview_summary_and_downloads(self):
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        os.makedirs(folder, exist_ok=True)
        lastminute.append_csv(os.path.join(folder, "watch.csv"), lastminute.WATCH_COLUMNS, {
            "kind": "guard", "play": "main", "would_profit": "-0.50", "held_profit": "-1.66", "difference": "1.16"})
        lastminute.append_csv(os.path.join(folder, "watch.csv"), lastminute.WATCH_COLUMNS, {
            "kind": "follow", "play": "follow", "would_profit": "0.18", "held_profit": "0", "difference": "0.18"})
        lastminute.append_csv(os.path.join(folder, "lastminute", "2026-11-22.csv"), lastminute.DATA_COLUMNS,
                              {"market": TICKER, "gap": "-35.00"})
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn("Last-minute watch", body)
        self.assertIn("1 times it would have sold early; it would have helped 1", body)
        self.assertIn("saved $1.16", body)
        self.assertIn("1 buys it would have made", body)
        _, text, _ = self.call("/watch.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("guard,main", text)
        _, text, _ = self.call("/lastminute.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("-35.00", text)
        # an older day saved with fewer columns lines up under the new header
        day_dir = os.path.join(folder, "lastminute")
        with open(os.path.join(day_dir, "2000-01-01.csv"), "w") as fh:
            fh.write("time,market,seconds_left,target,price,sources,projected,gap,yes_bid,yes_ask\n"
                     "t0,OLD,12,60000,59990,3,59990,-10.00,0.40,0.42\n")
        _, text, _ = self.call("/lastminute.csv?days=5", cookie=f"btcbot_key={KEY}")
        import io
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(rows[0]), lastminute.DATA_COLUMNS)
        old = next(r for r in rows if r["market"] == "OLD")
        self.assertEqual((old["yes_ask"], old["fut_price"]), ("0.42", ""))

    def test_market_card_shows_the_estimate(self):
        import json
        from datetime import datetime, timedelta, timezone
        from btcbot.settings import load, mode_folder
        folder = mode_folder(load(self.path))
        os.makedirs(folder, exist_ok=True)
        close = datetime.now(timezone.utc) + timedelta(seconds=40)
        row = {"ticker": "KXBTC15M-X", "series": "KXBTC15M", "close_time": close.isoformat(), "yes_bid": "0.40",
               "yes_ask": "0.42", "target": "60000", "est_gap": "-35.00", "est_locked": 20}
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": [row]}, fh)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('<span class="head">$35 ▼</span>', body)          # where the average is heading
        # With the current price: always shown, short, first line under the prices.
        close = datetime.now(timezone.utc) + timedelta(minutes=10)
        row.update(close_time=close.isoformat(), btc_now="60042.10", btc_gap="42.10")
        del row["est_gap"]
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": [row]}, fh)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('<div class="gap"><span class="now">$42 ▲</span><div class="brkrow"></div></div>', body)
        self.assertNotIn('<span class="head">', body)                  # heading only near the end
        # a breakout right now shows a red or green tag under the numbers (they stay white)
        row.update(move="down")
        with open(os.path.join(folder, "market.json"), "w") as fh:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(), "markets": [row]}, fh)
        _, body, _ = self.call(f"/?key={KEY}")
        self.assertIn('<div class="brkrow"><span class="brk down">Breakout ▼</span></div>', body)


class TestCloseCallFilter(LiveCase):
    """A last-minute favorite play with 'this many $ past the target' uses the running average."""
    LATE = {"name": "second", "buy": "favorite", "min_price": 0.88, "max_price": 0.95,
            "act_from_minutes_left": 1, "act_until_minutes_left": 0,
            "conditions": {"btc_distance_min_dollars": 25}}

    def setup_bot(self):
        self.make([self.LATE])
        self.bot.spot_fn = lambda product: D("60030")       # live price: $30 above the target
        self.prices = iter([])

    def step(self, seconds_left, price, yes_bid, yes_ask):
        self.bot.index_fn = lambda coin: (D(price), 3)
        return self.at(0, yes_bid, yes_ask, seconds=seconds_left)

    def test_skips_when_the_average_is_a_close_call(self):
        self.setup_bot()
        for s in range(75, 20, -1):                          # most of the minute below the target
            self.step(s, "59990", 0.05, 0.06)
        with self.assertLogs("btcbot", level="INFO") as logs:
            self.step(20, "60030", 0.90, 0.91)               # now $30 up, Up at 91c
        self.assertEqual(self.client.orders, [])
        self.assertIn("on the wrong side of the target", "\n".join(logs.output))

    def test_buys_when_the_average_is_clearly_past(self):
        self.setup_bot()
        for s in range(75, 20, -1):
            self.step(s, "60040", 0.88, 0.89)
        self.step(20, "60040", 0.90, 0.91)
        self.assertEqual(len(self.client.orders), 1)


class TestDownloadDays(WebCase):
    def test_last_three_days_in_one_file(self):
        from btcbot.settings import load, mode_folder
        folder = os.path.join(mode_folder(load(self.path)), "lastminute")
        for day, gap in (("2026-11-21.csv", "-1"), ("2026-11-22.csv", "-2"), ("2026-11-23.csv", "-3")):
            lastminute.append_csv(os.path.join(folder, day), lastminute.DATA_COLUMNS, {"market": "M", "gap": gap})
        _, text, _ = self.call("/lastminute.csv?days=3", cookie=f"btcbot_key={KEY}")
        self.assertEqual(text.count("time,market"), 1)
        self.assertEqual([line.split(",")[7] for line in text.strip().split("\n")[1:]], ["-1", "-2", "-3"])
        _, text, _ = self.call("/lastminute.csv", cookie=f"btcbot_key={KEY}")
        self.assertNotIn(",-1,", text)


class TestCheapBetsAreNotDefended(LiveCase):
    def test_underdog_bets_are_left_alone(self):
        self.make([CHEAP])
        self.bot.index_fn = lambda coin: (D("60000"), 3)
        self.at(12, 0.80, 0.82)                              # buys Down at about 20c
        self.assertEqual(len(self.client.orders), 1)
        self.bot.index_fn = lambda coin: (D("60200"), 3)     # far against it
        self.at(1, 0.95, 0.96)
        self.assertEqual(self.bot.state.watch, {})
