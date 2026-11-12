"""A run that can't be saved is retried, and if it still fails it goes to the log, never lost.

On 2026-11-12 the first live noon capture was lost: GitHub answered the tree creation with
HTTP 422 "Sorry, your request timed out", which was not retried, and the run's files only
existed in Render's container.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from recorder.run import COMMIT_WAITS, Recorder
from recorder.store import GitHubSink, LocalSink, StoreError
from recorder.unsaved import checked, decode, encode_lines
from recorder.config import CITIES
from tests.helpers import FakeClock, Replay
from tests.test_store import FakeGitHub

UTC = timezone.utc
TWO = [c for c in CITIES if c.key in ("NY", "MIA")]
GITHUB_TIMEOUT = (b'{"message":"Sorry, your request timed out.  It\'s likely that your input was too large '
                  b'to process.  Consider building the tree incrementally"}')


class TreeTimeoutGitHub(FakeGitHub):
    def __init__(self, timeouts):
        super().__init__()
        self.timeouts = timeouts

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/git/trees") and self.timeouts:
            self.timeouts -= 1
            self.calls.append((method, url))
            return 422, {}, GITHUB_TIMEOUT
        return super().__call__(method, url, headers, body, timeout)


class StoreRetryTests(unittest.TestCase):
    def sink(self, gh):
        return GitHubSink("me/data", "secret-token", transport=gh, clock=FakeClock(datetime(2026, 11, 12, tzinfo=UTC)))

    def test_githubs_tree_timeout_is_retried(self):
        gh = TreeTimeoutGitHub(timeouts=2)
        sha = self.sink(gh).commit({"a.json": b"{}"}, "m")
        self.assertEqual(gh.head, sha)
        self.assertEqual(sum(1 for m, u in gh.calls if m == "POST" and u.endswith("/git/trees")), 3)

    def test_a_persistent_tree_timeout_still_raises(self):
        with self.assertRaises(StoreError) as ctx:
            self.sink(TreeTimeoutGitHub(timeouts=99)).commit({"a.json": b"{}"}, "m")
        self.assertIn("timed out", str(ctx.exception))


class FlakySink(LocalSink):
    def __init__(self, root, failures):
        super().__init__(root)
        self.failures, self.commits = failures, 0

    def commit(self, files, message):
        self.commits += 1
        if self.failures:
            self.failures -= 1
            raise StoreError("create tree: HTTP 422 Sorry, your request timed out.")
        return super().commit(files, message)


class RecorderSaveTests(unittest.TestCase):
    def run_noon(self, failures, **replay):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clock = FakeClock(datetime(2026, 11, 11, 16, 52, tzinfo=UTC))
        rp = Replay(clock)
        for k, v in replay.items():
            setattr(rp, k, v)
        sink = FlakySink(self.tmp.name, failures)
        rec = Recorder(clock, rp, sink, cities=TWO, live_check_on_first_run=False)
        return rec, rec.execute(), sink, clock

    def test_save_is_retried_over_minutes_and_succeeds(self):
        rec, summary, sink, clock = self.run_noon(failures=2)
        self.assertIsNotNone(summary["commit"])
        self.assertNotIn("commit_error", summary)
        self.assertEqual(len(summary["commit_attempt_errors"]), 2)
        self.assertTrue(summary["ok"])
        self.assertEqual(clock.sleeps[-2:], list(COMMIT_WAITS[:2]))
        self.assertIsNotNone(sink.read(f"{rec.base}/noon_20261111T1700Z/books_1.json"))
        self.assertEqual(rec.unsaved, {})

    def test_a_run_that_cannot_be_saved_goes_to_the_log_and_comes_back_intact(self):
        rec, summary, sink, clock = self.run_noon(failures=99)
        self.assertEqual(sink.commits, len(COMMIT_WAITS) + 1)
        self.assertIsNone(summary["commit"])
        self.assertIn("timed out", summary["commit_error"])
        self.assertFalse(summary["ok"])
        self.assertLess(sum(COMMIT_WAITS), 10 * 60)  # finishes well before the next hourly run
        lines = encode_lines(rec.run_id, rec.unsaved)
        self.assertTrue(all(len(x) < 8100 for x in lines))
        log = "\n".join(["unrelated line", json.dumps(summary)] + lines + ["more noise"])
        back = decode(log)[rec.run_id]
        self.assertEqual(back, rec.unsaved)
        keep = checked(back)
        self.assertNotIn("state/state.json", keep)  # never restore an old state file
        self.assertIn(f"{rec.base}/noon_20261111T1700Z/books_1.json", keep)

    def test_recovery_rejects_damaged_or_incomplete_logs(self):
        rec, *_ = self.run_noon(failures=99)
        files = dict(rec.unsaved)
        files[f"{rec.base}/noon_20261111T1700Z/books_1.json"] += b" "
        with self.assertRaises(ValueError):
            checked(files)
        lines = encode_lines(rec.run_id, {"x": os.urandom(30000)})  # incompressible: several lines
        self.assertGreater(len(lines), 2)
        with self.assertRaises(ValueError) as ctx:
            decode("\n".join(lines[:1] + lines[2:]))  # one line lost from the log
        self.assertIn("missing log lines [2]", str(ctx.exception))

    def test_failure_reasons_are_in_the_log_line(self):
        _, summary, *_ = self.run_noon(failures=0, drop_books={"KXHIGHMIA"})
        self.assertEqual(summary["noon_failed"], ["MIA"])
        self.assertEqual(summary["noon_failed_reasons"], {"MIA": "missing_books"})
        self.assertEqual(summary["failed_reasons"], {})


if __name__ == "__main__":
    unittest.main()
