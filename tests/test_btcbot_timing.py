"""Timing data: second-by-second trades from Kalshi, futures markets and index exchanges."""
import csv
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from btcbot import timing as T
from btcbot.history import _ts

from test_btcbot_web import KEY, WebCase

END = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
CLOSE = int((END - timedelta(minutes=15)).timestamp())      # one market closing at 11:45


class FakeKalshi:
    key_id, private_key = None, None

    def _request(self, method, path, params=None, signed=False, body=None):
        if path == "/markets":
            return {"markets": [{"ticker": "KXBTC15M-A", "close_time": datetime.fromtimestamp(CLOSE, timezone.utc)
                                 .isoformat().replace("+00:00", "Z"), "result": "yes", "floor_strike": 60000}]}
        if path == "/markets/trades":
            t = lambda s: datetime.fromtimestamp(CLOSE - s, timezone.utc).isoformat().replace("+00:00", "Z")
            return {"trades": [{"created_time": t(200.4), "yes_price_dollars": "0.6000", "count_fp": "3.00"},
                               {"created_time": t(100.2), "yes_price_dollars": "0.7500", "count_fp": "1.00"},
                               {"created_time": t(100.1), "yes_price": 76, "count": 2}], "cursor": ""}
        return {"markets": []}


class R:
    def __init__(self, j, code=200):
        self._j, self.status_code = j, code

    def json(self):
        return self._j


class FakeSession:
    """Deribit and Kraken answer; OKX moves 2 seconds before them; Coinbase blocks this server."""
    def __init__(self):
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if "deribit" in url:
            a = params["start_timestamp"] // 1000
            return R({"result": {"has_more": False, "trades": [
                {"timestamp": (a + 50) * 1000 + 300, "price": 60010.0},
                {"timestamp": (a + 150) * 1000, "price": 60050.0}]}})
        if "okx" in url:
            after = int(params["after"])
            if after > CLOSE * 1000:
                return R({"code": "0", "data": [{"ts": str((CLOSE - 152) * 1000), "px": "60055.1"},
                                                 {"ts": str((CLOSE - 252) * 1000), "px": "60015.1"}]})
            return R({"code": "0", "data": []})
        if "kraken" in url:
            a = int(float(params["since"]))
            return R({"error": [], "result": {"XXBTZUSD": [["60000.0", "0.1", a + 10.5, "b", "l", "", 1],
                                                           ["60048.0", "0.2", a + 152.0, "s", "l", "", 2]],
                                              "last": "1"}})
        if "coinbase" in url:
            return R({"message": "forbidden"}, 403)
        raise AssertionError(url)


class TestPieces(unittest.TestCase):
    def test_last_price_each_second(self):
        self.assertEqual(T.per_second([(100.5, 1.0), (102, 2.0)], 99, 104), [None, 1.0, 2.0, 2.0, 2.0])

    def test_kalshi_trades_read_both_price_formats(self):
        got = T.kalshi_trades(FakeKalshi(), "KXBTC15M-A", CLOSE - 300, CLOSE, sleep=lambda s: None)
        self.assertEqual([p for _, p, _ in got], [0.60, 0.75, 0.76])
        self.assertEqual([n for _, _, n in got], [3.0, 1.0, 2.0])


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_whole_job(self):
        path = os.path.join(self.dir, "timing.csv")
        summary = T.collect(FakeKalshi(), FakeSession(), 1, path, sleep=lambda s: None, now=END.timestamp())
        self.assertIn("1 markets, 300 second rows", summary)
        self.assertIn("Kalshi ✓ 1", summary)
        self.assertIn("Deribit ✓ 1", summary)
        self.assertIn("Coinbase ✗ (answered 403 (blocked from here))", summary)
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 300)
        by_left = {int(r["seconds_left"]): r for r in rows}
        self.assertEqual(by_left[299]["deribit"], "")                    # nothing traded yet
        self.assertEqual(by_left[150]["okx"], "60055.10")                 # OKX moved at 152 s left...
        self.assertEqual(by_left[151]["deribit"], "60010.00")             # ...Deribit only at 150 s
        self.assertEqual(by_left[149]["deribit"], "60050.00")
        self.assertEqual(by_left[100]["kalshi"], "0.7600")
        self.assertEqual(by_left[100]["kalshi_trades"], "2")              # two trades in that second
        self.assertEqual(by_left[0]["kraken"], "60048.00")
        self.assertEqual(by_left[0]["coinbase"], "")


class TestTimingPage(WebCase):
    def test_collect_and_download(self):
        def fake(client, session, days, path, progress=None):
            progress("Market 1 of 2")
            with open(path, "w") as fh:
                fh.write(",".join(T.COLUMNS) + "\n" + "KXBTC15M-1,2026-11-23T11:45:00Z,60000,yes,5,0.6,0,,,,\n" * 5)
            return f"{days} days: Deribit ✓ 2"
        self.app.timing_collect = fake
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Collect timing data", body)
        self.call("/timing", data={"key": KEY, "days": "3"})
        for _ in range(50):
            if self.app.timing_job.get("state") == "done":
                break
            time.sleep(0.02)
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Done:</b> 3 days: Deribit ✓ 2", body)
        _, text, _ = self.call("/timing.csv", cookie=f"btcbot_key={KEY}")
        self.assertIn("KXBTC15M-1", text)
        # the link carries the key, so it works even if the phone doesn't send the login cookie
        self.assertIn(f'href="/timing.csv?key={KEY}"', body)
        status, text, _ = self.call(f"/timing.csv?key={KEY}")
        self.assertEqual(status, 200)
        self.assertIn("KXBTC15M-1", text)
        # still offered after the website restarts (the job is forgotten, the file isn't)
        self.app.timing_job = {}
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Download the timing data", body)


if __name__ == "__main__":
    unittest.main()
