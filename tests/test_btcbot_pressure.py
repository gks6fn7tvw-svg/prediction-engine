"""Order-book pressure: Coinbase's book and Kalshi's line, recorded every second (not scored)."""
import time
import unittest
from decimal import Decimal as D

from btcbot import pressure
from btcbot.kalshi import KalshiClient
from tests.test_btcbot import TICKER, FakeSession


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.data


class GetSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        return self.replies.pop(0)


class TestSummary(unittest.TestCase):
    def test_bands_around_the_middle(self):
        bids = [(99_995, 1.0), (99_985, 2.0), (99_960, 4.0)]
        asks = [(100_005, 0.5), (100_020, 0.5), (100_045, 3.0)]
        s = pressure.summarize(bids, asks)
        self.assertEqual(s["cb_mid"], 100_000)
        self.assertEqual((s["cb_bid10"], s["cb_ask10"]), (1.0, 0.5))       # leaning up close in
        self.assertEqual((s["cb_bid25"], s["cb_ask25"]), (3.0, 1.0))
        self.assertEqual((s["cb_bid50"], s["cb_ask50"]), (7.0, 4.0))
        self.assertEqual(s["cb_reach"], 40)                                 # bids only go $40 down
        self.assertIsNone(pressure.summarize([], asks))

    def test_reads_coinbase(self):
        book = {"pricebook": {"bids": [{"price": "99995.00", "size": "1.5"}],
                              "asks": [{"price": "100005.00", "size": "0.25"}]}}
        s = pressure.fetch(GetSession(Reply(book)))
        self.assertEqual((s["cb_mid"], s["cb_bid10"], s["cb_ask10"]), (100_000, 1.5, 0.25))

    def test_a_bad_reply_raises(self):
        with self.assertRaises(RuntimeError):
            pressure.fetch(GetSession(Reply({}, 404)))


class TestWatcher(unittest.TestCase):
    def test_looks_in_the_background_only_while_wanted(self):
        looks = []

        def look():
            looks.append(1)
            return {"cb_mid": 100_000.0}
        w = pressure.Watcher(fetch_fn=look, every=0.01, idle=0.05)
        self.assertEqual(w.read(), {})                    # nothing yet
        w.want()
        time.sleep(0.03)
        got = w.read()
        self.assertEqual(got["cb_mid"], 100_000.0)
        self.assertLess(got["cb_age"], 1)
        time.sleep(0.15)                                  # not wanted any more: it stops looking
        n = len(looks)
        time.sleep(0.05)
        self.assertEqual(len(looks), n)
        self.assertEqual(w.read(max_age=0.01), {})        # too old to record

    def test_a_failed_look_keeps_going(self):
        calls = []

        def look():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("down")
            return {"cb_mid": 1.0}
        w = pressure.Watcher(fetch_fn=look, every=0.01, idle=0.05)
        w.want()
        time.sleep(0.04)
        self.assertEqual(w.read()["cb_mid"], 1.0)
        self.assertEqual(w.fails, 1)


class TestKalshiLine(unittest.TestCase):
    def test_depth_within_3c(self):
        session = FakeSession({"orderbook_fp": {
            "yes_dollars": [["0.5800", "10.00"], ["0.6000", "2.00"], ["0.6200", "3.00"]],
            "no_dollars": [["0.3000", "4.00"], ["0.3600", "7.00"]]}})
        b = KalshiClient(session=session).best_prices(TICKER)
        self.assertEqual((b["yes_bid_depth"], b["yes_ask_depth"]), (D("5.00"), D("7.00")))   # 62c+60c; 36c only


if __name__ == "__main__":
    unittest.main()
