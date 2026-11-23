"""Backtest data: past markets, their minute prices and results."""
import csv
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from btcbot import history as H
from btcbot.kalshi import KalshiError

from test_btcbot_web import KEY, WebCase

END = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def market(i):
    close = END - timedelta(minutes=15 * i)
    return {"ticker": f"KXBTC15M-{i:03d}", "close_time": close.isoformat().replace("+00:00", "Z"),
            "result": "yes" if i % 2 else "no", "floor_strike": 60000 + i, "expiration_value": "60010.5"}


class FakeKalshi:
    def __init__(self, n):
        self.markets = [market(i) for i in range(1, n + 1)]
        self.calls = []
        self.key_id, self.private_key = None, None

    def _request(self, method, path, params=None, signed=False, body=None):
        self.calls.append((path, dict(params or {})))
        if path == "/markets":
            start = int(params.get("cursor") or 0)
            page = self.markets[start:start + 2]
            return {"markets": page, "cursor": str(start + 2) if start + 2 < len(self.markets) else ""}
        if path == "/markets/candlesticks":
            tickers = params["market_tickers"].split(",")
            periods = (params["end_ts"] - params["start_ts"]) // 60 * len(tickers)
            assert periods <= 10000, periods
            out = []
            for t in tickers:
                m = next(x for x in self.markets if x["ticker"] == t)
                close = H._ts(m["close_time"])
                out.append({"market_ticker": t, "candlesticks": [
                    {"end_period_ts": close - 60 * k, "yes_bid": {"close_dollars": "0.40"},
                     "yes_ask": {"close_dollars": "0.42", "low_dollars": "0.41"}, "volume_fp": "3.00"}
                    for k in range(15, -1, -1)]})
            return {"markets": out}
        if path == "/cfbenchmarks/values":
            raise KalshiError(403, "not entitled")
        raise KalshiError(404, path)


class FakeCoinbase:
    def get(self, url, params=None, timeout=None):
        start = int(datetime.fromisoformat(params["start"]).timestamp())
        end = int(datetime.fromisoformat(params["end"]).timestamp())

        class R:
            def json(self_inner):
                return [[t, 59990, 60020, 60000, 60005, 1.5] for t in range(start, end, 60)]
        return R()


class FakeBinance(FakeCoinbase):
    """Coinbase candles, Binance daily files (microsecond times) and Binance.US for the newest day."""
    def __init__(self, files_work=True):
        self.files_work = files_work
        self.urls = []

    def get(self, url, params=None, timeout=None):
        import io
        import zipfile
        self.urls.append(url)
        if "data.binance.vision" in url:
            day = datetime.strptime(url.rsplit("-1m-", 1)[1][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if not self.files_work or day.date() >= END.date():
                return type("R", (), {"status_code": 404, "content": b"<Error/>"})()
            start = int(day.timestamp())
            lines = [f"{(start + 60 * k) * 1_000_000},60000,60010,59990,60005,10.0,0,0,0,7.0,0,0"
                     for k in range(1440)]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("x.csv", "\n".join(lines))
            return type("R", (), {"status_code": 200, "content": buf.getvalue()})()
        if "binance.us" in url:
            t0 = params["startTime"] // 1000
            stop = params["endTime"] // 1000
            rows = [[(t0 + 60 * k) * 1000, "1", "1", "1", "1", "2.0", 0, 0, 0, "0.5", 0, 0]
                    for k in range(1000) if t0 + 60 * k < stop]
            return type("R", (), {"status_code": 200, "json": lambda self: rows})()
        return super().get(url, params, timeout)


class TestBuyingPower(unittest.TestCase):
    def test_daily_files_then_binance_us_for_today(self):
        start, end = int((END - timedelta(days=1)).timestamp()), int(END.timestamp())
        flow = H.buying_power(FakeBinance(), "BTCUSDT", start, end, sleep=lambda s: None)
        old, new = flow[start], flow[end - 60]
        self.assertEqual(old, (7.0, 10.0, "binance"))           # 70% bought by eager buyers
        self.assertEqual(new, (0.5, 2.0, "binance.us"))         # today's file isn't out yet
        self.assertTrue(all(m in flow for m in range(start, end, 60)))   # no gaps

    def test_rows_get_the_buying_power(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "history.csv")
        summary = H.collect(FakeKalshi(4), FakeBinance(), "KXBTC15M", 1, path, sleep=lambda s: None,
                            now=END.timestamp() + 1)
        self.assertIn("buying power for", summary)
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(all(r["buy_volume"] and r["all_volume"] and r["flow_from"] for r in rows))

    def test_binance_us_alone_when_files_fail(self):
        start, end = int((END - timedelta(hours=20)).timestamp()), int(END.timestamp())
        flow = H.buying_power(FakeBinance(files_work=False), "BTCUSDT", start, end, sleep=lambda s: None)
        self.assertEqual({v[2] for v in flow.values()}, {"binance.us"})
        self.assertEqual(len(flow), (end - start) // 60)


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_whole_job(self):
        k = FakeKalshi(40)
        path = os.path.join(self.dir, "history.csv")
        steps = []
        summary = H.collect(k, FakeCoinbase(), "KXBTC15M", 1, path, progress=steps.append,
                            sleep=lambda s: None, now=END.timestamp() + 1)
        self.assertIn("40 markets (40 with Kalshi prices)", summary)
        self.assertIn("official index", summary)
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 40 * 16)
        first = [r for r in rows if r["market"] == "KXBTC15M-040"]
        self.assertEqual([int(r["minutes_left"]) for r in first][:2], [15, 14])
        self.assertEqual((first[0]["up_ask"], first[0]["btc_close"], first[0]["settled_value"]),
                         ("0.42", "60005", "60010.5"))
        self.assertTrue(any("Kalshi prices" in s for s in steps))
        batches = [c for c in k.calls if c[0] == "/markets/candlesticks"]
        self.assertGreater(len(batches), 1)                  # split to stay under Kalshi's limit

    def test_index_access_message(self):
        k = FakeKalshi(1)
        self.assertIn("no key saved", H.brti_access(k))
        k.key_id, k.private_key = "kid", object()
        self.assertIn("didn't allow", H.brti_access(k))


class TestHistoryPage(WebCase):
    def test_collect_and_download(self):
        def fake_collect(client, session, series, days, path, progress=None):
            progress("halfway")
            with open(path, "w") as fh:
                fh.write(",".join(H.COLUMNS) + "\nKXBTC15M-1\n")
            return f"{days} days done"
        self.app.history_collect = fake_collect
        _, body, _ = self.call(f"/settings?key={KEY}")
        self.assertIn("Collect history", body)
        self.call("/history", data={"key": KEY, "days": "7", "series": "all"})
        for _ in range(50):
            if self.app.history_job.get("state") == "done":
                break
            time.sleep(0.05)
        _, body, _ = self.call("/settings", cookie=f"btcbot_key={KEY}")
        self.assertIn("Done:</b> Bitcoin: 7 days done | Ethereum: 7 days done", body)
        _, text, _ = self.call("/history.csv", cookie=f"btcbot_key={KEY}")
        self.assertEqual(text.count("market,close_time"), 1)          # one header for all markets
        self.assertEqual(text.count("KXBTC15M-1"), 4)   # Bitcoin, Ethereum, Solana, XRP
        self.assertIn("Solana 15-minute", body)


if __name__ == "__main__":
    unittest.main()
