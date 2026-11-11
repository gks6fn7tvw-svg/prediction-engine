"""Where captured files go.

GitHubSink writes all files from one run as a single commit to the data
repository, using GitHub's Git Data API. LocalSink writes to a folder and is
used for tests and dry runs.
"""
import base64
import json
import os
from urllib.parse import quote

from .net import RealClock, urllib_transport

API = "https://api.github.com"


class StoreError(Exception):
    pass


class LocalSink:
    def __init__(self, root: str):
        self.root = root

    def read(self, path: str) -> bytes | None:
        full = os.path.join(self.root, path)
        if not os.path.exists(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def list_files(self, prefix: str = "") -> list[str]:
        out = []
        for base, _, names in os.walk(self.root):
            for name in names:
                rel = os.path.relpath(os.path.join(base, name), self.root).replace(os.sep, "/")
                if rel.startswith(prefix) and not rel.startswith(".git/"):
                    out.append(rel)
        return sorted(out)

    def commit(self, files: dict[str, bytes], message: str) -> str:
        for path, data in files.items():
            full = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)
        return "local"


class GitHubSink:
    def __init__(self, repo: str, token: str, branch: str = "main",
                 transport=urllib_transport, clock=None, timeout: int = 30):
        self.repo, self.token, self.branch = repo, token, branch
        self.transport, self.clock, self.timeout = transport, clock or RealClock(), timeout

    def _call(self, method, path, payload=None, accept="application/vnd.github+json", tries=3):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prediction-engine-recorder",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        url = f"{API}/repos/{self.repo}{path}"
        last = None
        for attempt in range(1, tries + 1):
            try:
                status, _, data = self.transport(method, url, headers, body, self.timeout)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if status not in (429, 500, 502, 503, 504):
                    return status, data
                last = f"HTTP {status}"
            if attempt < tries:
                self.clock.sleep(2 * attempt)
        raise StoreError(f"GitHub {method} {path} failed: {last}")

    @staticmethod
    def _fail(what, status, data):
        raise StoreError(f"{what}: HTTP {status} {data[:200].decode('utf-8', 'replace')}")

    def read(self, path: str) -> bytes | None:
        status, data = self._call(
            "GET", f"/contents/{quote(path)}?ref={quote(self.branch)}",
            accept="application/vnd.github.raw+json")
        if status == 200:
            return data
        if status == 404:
            return None
        self._fail(f"read {path}", status, data)

    def list_files(self, prefix: str = "") -> list[str]:
        """Paths of all files on the branch that start with prefix."""
        status, data = self._call("GET", f"/git/trees/{quote(self.branch)}?recursive=1")
        if status != 200:
            self._fail("list files", status, data)
        tree = json.loads(data)
        if tree.get("truncated"):
            raise StoreError("repository tree listing was truncated")
        return sorted(e["path"] for e in tree.get("tree", [])
                      if e.get("type") == "blob" and e["path"].startswith(prefix))

    def _head(self) -> str:
        status, data = self._call("GET", f"/git/ref/heads/{quote(self.branch)}")
        if status != 200:
            self._fail("read branch", status, data)
        return json.loads(data)["object"]["sha"]

    def commit(self, files: dict[str, bytes], message: str, attempts: int = 4) -> str:
        """Add files in one commit. Retries if the branch moved. Returns the commit sha."""
        entries = []
        for path, content in sorted(files.items()):
            status, data = self._call("POST", "/git/blobs", {
                "content": base64.b64encode(content).decode(), "encoding": "base64"})
            if status != 201:
                self._fail(f"upload {path}", status, data)
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": json.loads(data)["sha"]})
        for attempt in range(1, attempts + 1):
            parent = self._head()
            status, data = self._call("GET", f"/git/commits/{parent}")
            if status != 200:
                self._fail("read commit", status, data)
            base_tree = json.loads(data)["tree"]["sha"]
            status, data = self._call("POST", "/git/trees", {"base_tree": base_tree, "tree": entries})
            if status != 201:
                self._fail("create tree", status, data)
            status, data = self._call("POST", "/git/commits", {
                "message": message, "tree": json.loads(data)["sha"], "parents": [parent]})
            if status != 201:
                self._fail("create commit", status, data)
            new = json.loads(data)["sha"]
            status, data = self._call("PATCH", f"/git/refs/heads/{quote(self.branch)}",
                                      {"sha": new, "force": False})
            if status == 200:
                if self._head() != new:  # confirm the data really landed
                    raise StoreError("commit was accepted but the branch does not point at it")
                return new
            if status in (409, 422) and attempt < attempts:  # branch moved underneath us
                self.clock.sleep(attempt)
                continue
            self._fail("update branch", status, data)
        raise StoreError("could not update branch")
