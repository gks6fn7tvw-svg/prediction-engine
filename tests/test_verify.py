"""The independent history checker passes ChatGPT's real Sept 11 batch and catches tampering.

The fixture is a byte-for-byte copy of history/development/2026-09-11 from the data
repository (commit 97be780). Each tamper test changes one thing and re-hashes the
file in the manifest, so only the targeted check can catch it.
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from verify.check_history import check_batch, check_staged

FIXTURE = Path(__file__).parent / "fixtures"
BATCH = "history/development/2026-09-11"


class HistoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(FIXTURE / "history", self.root / "history")
        self.batch = self.root / BATCH

    def edit(self, rel, fn):
        """Change one JSON file and update its manifest hash, so only the targeted check fires."""
        path = self.batch / rel
        data = json.loads(path.read_bytes())
        fn(data)
        raw = json.dumps(data).encode()
        path.write_bytes(raw)
        if rel != "manifest.json":
            man = json.loads((self.batch / "manifest.json").read_bytes())
            man["files"][rel] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            (self.batch / "manifest.json").write_bytes(json.dumps(man).encode())

    def failures(self):
        return check_batch(self.root, BATCH).failures

    def test_real_batch_passes(self):
        report = check_batch(self.root, BATCH)
        self.assertEqual(report.failures, [])
        self.assertGreater(report.passed, 500)

    def test_byte_change_without_rehash_is_caught(self):
        path = self.batch / "NY/markets.json"
        path.write_bytes(path.read_bytes() + b" ")
        self.assertTrue(any("NY/markets.json" in f for f in self.failures()))

    def test_candle_ending_after_cutoff_is_caught(self):
        def later(m):
            q = m["cities"]["NY"]["quotes"]["KXHIGHNY-26SEP11-B79.5"]
            q["candle_end_unix"] += 60
            q["age_seconds"] -= 60
        self.edit("manifest.json", later)
        self.assertTrue(any("B79.5" in f for f in self.failures()))

    def test_forecast_object_published_after_cutoff_is_caught(self):
        def late(n):
            n["hours"][5]["source"]["last_modified_utc"] = "2026-09-10T22:00:01.000Z"
        self.edit("NY/nbm.json", late)
        self.assertTrue(any("published after the cutoff" in f for f in self.failures()))

    def test_wrong_yes_winner_is_caught(self):
        def flip(d):
            for m in d["markets"]:
                if m["ticker"].endswith("B79.5"):
                    m["result"] = "no"
                if m["ticker"].endswith("B81.5"):
                    m["result"] = "yes"
        self.edit("NY/markets.json", flip)
        self.assertTrue(any("Yes winners" in f for f in self.failures()))

    def test_missing_forecast_hour_is_caught(self):
        self.edit("MIA/nbm.json", lambda n: n["hours"].pop())
        self.assertTrue(any("cover the climate day" in f for f in self.failures()))

    # --- found in ChatGPT's review of PR #3 ---

    def test_dropping_a_whole_city_is_caught(self):
        shutil.rmtree(self.batch / "NY")

        def drop(m):
            del m["cities"]["NY"]
            for rel in [k for k in m["files"] if k.startswith("NY/")]:
                del m["files"][rel]
        self.edit("manifest.json", drop)
        self.assertTrue(any("registered" in f for f in self.failures()))

    def test_explicit_missing_city_record_is_accepted_and_noted(self):
        shutil.rmtree(self.batch / "NY")

        def mark(m):
            m["cities"]["NY"] = {"status": "missing", "reason": "no NBM cycle before cutoff"}
            for rel in [k for k in m["files"] if k.startswith("NY/")]:
                del m["files"][rel]
        self.edit("manifest.json", mark)
        report = check_batch(self.root, BATCH)
        self.assertEqual(report.failures, [])
        self.assertTrue(any("NY: missing" in n for n in report.notes))

    def test_missing_city_needs_a_reason(self):
        shutil.rmtree(self.batch / "NY")

        def mark(m):
            m["cities"]["NY"] = {"status": "missing"}
            for rel in [k for k in m["files"] if k.startswith("NY/")]:
                del m["files"][rel]
        self.edit("manifest.json", mark)
        self.assertTrue(any("without a reason" in f for f in self.failures()))

    def test_wrong_payout_on_the_winner_is_caught(self):
        def underpay(d):
            for m in d["markets"]:
                if m["ticker"].endswith("B79.5"):
                    m["settlement_value_dollars"] = "0.5000"
        self.edit("NY/markets.json", underpay)
        self.assertTrue(any("settlement_value_dollars" in f for f in self.failures()))

    def test_crossed_quote_is_skipped_for_an_earlier_valid_candle(self):
        ticker = "KXHIGHNY-26SEP11-B79.5"
        candles_before = {x["market_ticker"]: x["candlesticks"] for x in
                          json.loads((self.batch / "NY/candles.json").read_bytes())["markets"]}[ticker]
        chosen = json.loads((self.batch / "manifest.json").read_bytes())["cities"]["NY"]["quotes"][ticker]
        earlier = max((c for c in candles_before if c["end_period_ts"] < chosen["candle_end_unix"]),
                      key=lambda c: c["end_period_ts"])

        def cross(d):
            for x in d["markets"]:
                if x["market_ticker"] == ticker:
                    for c in x["candlesticks"]:
                        if c["end_period_ts"] == chosen["candle_end_unix"]:
                            c["yes_bid"]["close_dollars"] = "0.9000"  # above its 0.48 ask
        self.edit("NY/candles.json", cross)
        # The manifest still claims the crossed candle: the checker must reject that choice...
        self.assertTrue(any(ticker in f and "chosen candle" in f for f in self.failures()))

        # ...and accept the earlier valid candle once the manifest falls back to it.
        def fall_back(m):
            q = m["cities"]["NY"]["quotes"][ticker]
            q["candle_end_unix"] = earlier["end_period_ts"]
            q["age_seconds"] = 1789077600 - earlier["end_period_ts"]  # NY cutoff 2026-09-10T22:00Z
            q["yes_bid_close_dollars"] = earlier["yes_bid"]["close_dollars"]
            q["yes_ask_close_dollars"] = earlier["yes_ask"]["close_dollars"]
        self.edit("manifest.json", fall_back)
        self.assertEqual([f for f in self.failures() if ticker in f], [])

    def test_nan_candle_falls_back_instead_of_crashing(self):
        ticker = "KXHIGHNY-26SEP11-B79.5"
        chosen = json.loads((self.batch / "manifest.json").read_bytes())["cities"]["NY"]["quotes"][ticker]

        def poison(d):
            for x in d["markets"]:
                if x["market_ticker"] == ticker:
                    for c in x["candlesticks"]:
                        if c["end_period_ts"] == chosen["candle_end_unix"]:
                            c["yes_ask"]["close_dollars"] = "NaN"
        self.edit("NY/candles.json", poison)
        failures = self.failures()  # must not raise
        self.assertTrue(any(ticker in f and "chosen candle" in f for f in failures))

    def test_boundary_quotes_make_the_city_day_incomplete_for_primary_scoring(self):
        report = check_batch(self.root, BATCH)
        self.assertEqual(report.primary, {"NY": False, "MIA": False, "CHI": True, "DEN": True, "LAX": False})
        self.assertEqual(report.inclusive, {k: True for k in ("NY", "MIA", "CHI", "DEN", "LAX")})
        self.assertTrue(any("NY: boundary quotes" in n and "T86" in n for n in report.notes))
        self.assertEqual(report.failures, [])  # a convention, not an integrity failure

    def test_holdout_date_labelled_development_is_caught(self):
        self.edit("manifest.json", lambda m: m.update(target_date="2026-10-02"))
        self.assertTrue(any("partition" in f for f in self.failures()))


STAGED = "staging/holdout/2026-10-01"


class StagedCheckTests(unittest.TestCase):
    """Forecast-only holdout batches staged before registration (ChatGPT #13)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(FIXTURE / "staging", self.root / "staging")
        self.batch = self.root / STAGED

    def manifest(self, fn):
        path = self.batch / "forecast-manifest.json"
        m = json.loads(path.read_bytes())
        fn(m)
        path.write_bytes(json.dumps(m).encode())

    def test_real_staged_batch_passes(self):
        report = check_staged(self.root, STAGED)
        self.assertEqual(report.failures, [])
        self.assertGreater(report.passed, 350)

    def test_anything_but_forecasts_is_rejected(self):
        (self.batch / "NY" / "kalshi_markets.json").write_bytes(b"{}")
        self.manifest(lambda m: m["files"].update({"NY/kalshi_markets.json": {
            "bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}}))
        self.assertTrue(any("more than forecasts" in f for f in check_staged(self.root, STAGED).failures))

    def test_dropped_city_needs_a_reason(self):
        shutil.rmtree(self.batch / "LAX")
        self.manifest(lambda m: m["files"].pop("LAX/nbm.json"))
        self.assertTrue(any("LAX/nbm.json missing without a reason" in f
                            for f in check_staged(self.root, STAGED).failures))
        self.manifest(lambda m: m.update(missing={"LAX": "no complete cycle before cutoff"}))
        self.assertEqual(check_staged(self.root, STAGED).failures, [])

    def test_forecast_published_after_cutoff_is_caught(self):
        path = self.batch / "DEN" / "nbm.json"
        n = json.loads(path.read_bytes())
        n["hours"][3]["source"]["index"]["last_modified_utc"] = "2026-10-01T01:00:01.000Z"
        raw = json.dumps(n).encode()
        path.write_bytes(raw)
        self.manifest(lambda m: m["files"].update({"DEN/nbm.json": {
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}))
        self.assertTrue(any("DEN: objects published after the cutoff" in f
                            for f in check_staged(self.root, STAGED).failures))

    def test_final_holdout_batch_must_reuse_the_staged_forecast_exactly(self):
        # A minimal stand-in for the final holdout batch: only the forecast matters here.
        final = self.root / "history/holdout/2026-10-01"
        shutil.copytree(FIXTURE / BATCH, final)
        man = json.loads((final / "manifest.json").read_bytes())
        man.update(target_date="2026-10-01", partition="holdout")
        (final / "manifest.json").write_bytes(json.dumps(man).encode())
        for key in ("NY", "MIA", "CHI", "DEN", "LAX"):  # the staged forecasts, byte for byte
            shutil.copy(self.batch / key / "nbm.json", final / key / "nbm.json")
        fails = check_batch(self.root, "history/holdout/2026-10-01").failures
        self.assertFalse(any("differs from the staged forecast" in f for f in fails))
        (final / "NY" / "nbm.json").write_bytes((final / "NY" / "nbm.json").read_bytes() + b" ")
        fails = check_batch(self.root, "history/holdout/2026-10-01").failures
        self.assertTrue(any("NY: holdout forecast differs from the staged forecast" in f for f in fails))
        shutil.rmtree(self.root / "staging")
        fails = check_batch(self.root, "history/holdout/2026-10-01").failures
        self.assertTrue(any("no staged forecast" in f for f in fails))


if __name__ == "__main__":
    unittest.main()
