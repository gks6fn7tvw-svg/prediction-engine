"""Last-minute watch: settlement estimate, and the guard/follow ideas it only logs."""
import csv
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from btcbot import lastminute
from btcbot.lastminute import Tracker

from test_btcbot import CLOSE, TICKER, market
from test_btcbot_live import MAIN, LiveCase


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

    def test_switched_off(self):
        self.make([MAIN], last_minute={"enabled": False})
        self.price = D("60060")
        self.run_bot(0, 0.78, 0.80, seconds=40)
        self.assertEqual(self.bot.state.watch, {})
        self.assertFalse(os.path.exists(os.path.join(self.ledger.folder, "lastminute")))


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
        self.assertIn("the average is heading <b>$35 below</b> the target (Down ahead)", body)
        self.assertIn("20 of 60 seconds counted", body)
