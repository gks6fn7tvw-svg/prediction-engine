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

from verify.check_history import check_batch

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

    def test_holdout_date_labelled_development_is_caught(self):
        self.edit("manifest.json", lambda m: m.update(target_date="2026-10-02"))
        self.assertTrue(any("partition" in f for f in self.failures()))


if __name__ == "__main__":
    unittest.main()
