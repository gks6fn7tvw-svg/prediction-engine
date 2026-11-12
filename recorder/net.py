"""HTTP fetching with timing and hashing. Standard library only.

The clock and the transport are passed in, so tests can replay saved
responses without touching the network.
"""
import hashlib
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

RETRY_STATUS = {429, 500, 502, 503, 504}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Background:
    """Runs fn on a daemon thread. fn must not touch shared state: it returns its results,
    and the caller records them after join(), so an abandoned thread can never change a run
    that is already being saved."""

    def __init__(self, fn):
        self.value = self.error = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn):
        try:
            self.value = fn()
        except Exception as exc:  # recorded by the caller, never raised in the thread
            self.error = exc

    def join(self, timeout: float | None) -> bool:
        """Wait up to `timeout` seconds; True if fn has finished."""
        self._thread.join(timeout)
        return not self._thread.is_alive()


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def background(self, fn) -> Background:
        return Background(fn)


def urllib_transport(method, url, headers, body, timeout):
    """Returns (status, headers, body). HTTP error statuses are returned, not raised."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()) if err.headers else {}, err.read()


@dataclass
class Fetch:
    url: str
    requested: datetime
    received: datetime
    monotonic_requested: float
    monotonic_received: float
    status: int | None
    body: bytes
    server_date: str | None
    error: str | None
    attempts: int = 1
    earlier_attempts: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.body).hexdigest() if self.body else None

    def meta(self) -> dict:
        out = {
            "url": self.url,
            "requested_at": iso(self.requested),
            "received_at": iso(self.received),
            "elapsed_seconds": round(self.monotonic_received - self.monotonic_requested, 6),
            "http_status": self.status,
            "server_date": self.server_date,
            "bytes": len(self.body),
            "sha256": self.sha256,
            "error": self.error,
            "attempts": self.attempts,
        }
        if self.earlier_attempts:
            out["earlier_attempts"] = self.earlier_attempts
        return out


def fetch(clock, transport, url, headers=None, timeout=15, attempts=3, backoff=1.0) -> Fetch:
    """GET a URL. Retries network errors and 429/5xx; every attempt is kept in the record."""
    earlier = []
    result = None
    for attempt in range(1, attempts + 1):
        requested, m0 = clock.now(), clock.monotonic()
        try:
            status, resp_headers, body = transport("GET", url, headers or {}, None, timeout)
            error = None
        except Exception as exc:  # network failure, timeout, DNS, TLS
            status, resp_headers, body, error = None, {}, b"", f"{type(exc).__name__}: {exc}"
        received, m1 = clock.now(), clock.monotonic()
        server_date = next((v for k, v in (resp_headers or {}).items() if k.lower() == "date"), None)
        result = Fetch(url, requested, received, m0, m1, status, body or b"", server_date, error,
                       attempts=attempt, earlier_attempts=list(earlier))
        if error is None and status not in RETRY_STATUS:
            return result
        earlier.append({"requested_at": iso(requested), "http_status": status, "error": error})
        if attempt < attempts:
            clock.sleep(backoff * attempt)
    return result
