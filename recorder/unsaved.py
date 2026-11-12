"""Last resort when a run cannot be saved to GitHub: put its files in the log, and get them back.

A capture can never be repeated, so a run whose commit fails even after retries prints
every file it would have saved to stdout (which Render keeps in its logs), compressed and
split into numbered lines:

    UNSAVED_RUN <run_id> <i>/<n> <base64 chunk>

`python -m recorder.unsaved <log file>` reassembles them and checks every file against the
sha256 recorded in run.json. With --commit (and DATA_REPO, GITHUB_TOKEN) it saves the run
folder to the data repository. state/state.json is never restored: the recorder has moved
on since, so an old copy would undo later bookkeeping.
"""
import base64
import gzip
import hashlib
import json
import os
import re
import sys

PREFIX = "UNSAVED_RUN"
CHUNK = 8000
STATE_PATH = "state/state.json"
_LINE = re.compile(rf"{PREFIX} (\S+) (\d+)/(\d+) ([A-Za-z0-9+/=]+)")


def encode_lines(run_id: str, files: dict[str, bytes]) -> list[str]:
    payload = json.dumps({p: base64.b64encode(b).decode() for p, b in sorted(files.items())}).encode()
    blob = base64.b64encode(gzip.compress(payload, mtime=0)).decode()
    chunks = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)] or [""]
    return [f"{PREFIX} {run_id} {i}/{len(chunks)} {c}" for i, c in enumerate(chunks, 1)]


def decode(text: str) -> dict[str, dict[str, bytes]]:
    """All complete runs found in a log, as {run_id: {path: bytes}}. Incomplete runs raise."""
    parts: dict[str, dict[int, str]] = {}
    totals: dict[str, int] = {}
    for m in _LINE.finditer(text):
        run_id, i, n, chunk = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        parts.setdefault(run_id, {})[i] = chunk
        totals[run_id] = n
    out = {}
    for run_id, got in parts.items():
        missing = sorted(set(range(1, totals[run_id] + 1)) - set(got))
        if missing:
            raise ValueError(f"{run_id}: missing log lines {missing}")
        blob = "".join(got[i] for i in range(1, totals[run_id] + 1))
        payload = json.loads(gzip.decompress(base64.b64decode(blob)))
        out[run_id] = {p: base64.b64decode(b) for p, b in payload.items()}
    return out


def checked(files: dict[str, bytes]) -> dict[str, bytes]:
    """The run's files without state.json, each verified against run.json's sha256 list."""
    run_json = next((p for p in files if p.endswith("/run.json")), None)
    if run_json is None:
        raise ValueError("no run.json among the recovered files")
    listed = json.loads(files[run_json])["files"]
    keep = {p: b for p, b in files.items() if p != STATE_PATH}
    for path, meta in listed.items():
        if hashlib.sha256(keep[path]).hexdigest() != meta["sha256"]:
            raise ValueError(f"{path}: sha256 does not match run.json")
    extra = set(keep) - set(listed) - {run_json}
    if extra:
        raise ValueError(f"files not listed in run.json: {sorted(extra)}")
    return keep


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    commit = "--commit" in args
    args = [a for a in args if a != "--commit"]
    if len(args) != 1:
        print("usage: python -m recorder.unsaved <log file> [--commit]", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8", errors="replace") as fh:
        runs = decode(fh.read())
    for run_id, files in sorted(runs.items()):
        keep = checked(files)
        print(f"{run_id}: {len(keep)} files verified")
        if commit:
            from .store import GitHubSink
            sink = GitHubSink(os.environ["DATA_REPO"], os.environ["GITHUB_TOKEN"], os.environ.get("DATA_BRANCH", "main"))
            print(f"  committed {sink.commit(keep, f'recover unsaved recorder run {run_id}')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
