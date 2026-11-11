"""GitHubSink against a small in-memory imitation of GitHub's Git Data API."""
import base64
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from recorder.store import GitHubSink, StoreError
from tests.helpers import FakeClock


class FakeGitHub:
    def __init__(self):
        self.blobs, self.trees, self.commits = {}, {"t0": {}}, {"c0": "t0"}
        self.head = "c0"
        self.n = 0
        self.conflicts_to_inject = 0
        self.fail_next = []          # statuses to return for the next calls
        self.calls = []

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if headers.get("Authorization") != "Bearer secret-token":
            return 401, {}, b'{"message":"Bad credentials"}'
        if self.fail_next:
            return self.fail_next.pop(0), {}, b'{"message":"injected"}'
        u = urlparse(url)
        path = u.path.split("/repos/me/data", 1)[1]
        data = json.loads(body) if body else None
        reply = lambda status, obj: (status, {}, json.dumps(obj).encode())
        if method == "GET" and path.startswith("/contents/"):
            files = self.trees[self.commits[self.head]]
            name = unquote(path[len("/contents/"):])
            return (200, {}, self.blobs[files[name]]) if name in files else reply(404, {"message": "Not Found"})
        if method == "GET" and path == "/git/ref/heads/main":
            return reply(200, {"object": {"sha": self.head}})
        if method == "GET" and path.startswith("/git/commits/"):
            return reply(200, {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]}})
        if method == "POST" and path == "/git/blobs":
            sha = self._id("b")
            self.blobs[sha] = base64.b64decode(data["content"])
            return reply(201, {"sha": sha})
        if method == "POST" and path == "/git/trees":
            sha = self._id("t")
            self.trees[sha] = {**self.trees[data["base_tree"]], **{e["path"]: e["sha"] for e in data["tree"]}}
            return reply(201, {"sha": sha})
        if method == "POST" and path == "/git/commits":
            sha = self._id("c")
            self.commits[sha] = data["tree"]
            self.parents = getattr(self, "parents", {})
            self.parents[sha] = data["parents"][0]
            return reply(201, {"sha": sha})
        if method == "PATCH" and path == "/git/refs/heads/main":
            if self.conflicts_to_inject:
                # Someone else committed first: the branch moves and our update is refused.
                self.conflicts_to_inject -= 1
                other_tree = self._id("t")
                self.trees[other_tree] = {**self.trees[self.commits[self.head]], "other.txt": self._put(b"x")}
                other = self._id("c")
                self.commits[other] = other_tree
                self.head = other
                return reply(422, {"message": "Update is not a fast forward"})
            if self.parents[data["sha"]] != self.head:
                return reply(422, {"message": "Update is not a fast forward"})
            self.head = data["sha"]
            return reply(200, {"object": {"sha": self.head}})
        return reply(404, {"message": "no route"})

    def _put(self, content):
        sha = self._id("b")
        self.blobs[sha] = content
        return sha


class GitHubSinkTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.clock = FakeClock(datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.sink = GitHubSink("me/data", "secret-token", transport=self.gh, clock=self.clock)

    def test_commit_then_read_back(self):
        sha = self.sink.commit({"runs/a/run.json": b'{"a":1}', "state/state.json": b"{}"}, "msg")
        self.assertEqual(self.gh.head, sha)
        self.assertEqual(self.sink.read("runs/a/run.json"), b'{"a":1}')
        self.assertIsNone(self.sink.read("nope.json"))

    def test_binary_content_survives(self):
        blob = bytes(range(256)) * 4
        self.sink.commit({"x.gz": blob}, "msg")
        self.assertEqual(self.sink.read("x.gz"), blob)

    def test_earlier_files_are_kept(self):
        self.sink.commit({"one.json": b"1"}, "first")
        self.sink.commit({"two.json": b"2"}, "second")
        self.assertEqual((self.sink.read("one.json"), self.sink.read("two.json")), (b"1", b"2"))

    def test_retries_when_branch_moved_and_keeps_the_other_commit(self):
        self.gh.conflicts_to_inject = 1
        self.sink.commit({"mine.json": b"m"}, "msg")
        self.assertEqual(self.sink.read("mine.json"), b"m")
        self.assertEqual(self.sink.read("other.txt"), b"x")

    def test_gives_up_after_repeated_conflicts(self):
        self.gh.conflicts_to_inject = 10
        with self.assertRaises(StoreError):
            self.sink.commit({"mine.json": b"m"}, "msg")

    def test_transient_server_error_is_retried(self):
        self.gh.fail_next = [503]
        self.sink.commit({"a.json": b"a"}, "msg")
        self.assertEqual(self.sink.read("a.json"), b"a")

    def test_bad_token_raises_without_leaking_it(self):
        bad = GitHubSink("me/data", "wrong-token", transport=self.gh, clock=self.clock)
        with self.assertRaises(StoreError) as ctx:
            bad.read("state/state.json")
        self.assertNotIn("wrong-token", str(ctx.exception))
        with self.assertRaises(StoreError):
            bad.commit({"a.json": b"a"}, "msg")


if __name__ == "__main__":
    unittest.main()
