"""A free locked (https) link for the website.

Let's Encrypt gives free 6-day certificates for a server's IP address. certbot
(run as the bot's own user, no root needed) proves the server owns its IP by
putting a file in a folder the website serves on port 80, then the website
also answers on port 443 with the certificate. A new certificate is fetched
when less than 3 days are left, or when the server's IP changes.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from . import keystore

log = logging.getLogger("btcbot.web")
IP_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
          "network-interfaces/0/access-configs/0/external-ip")
RENEW_BEFORE = timedelta(days=3)
CHECK_SECONDS = 6 * 3600      # look again every 6 hours (and right after starting)
TOKEN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


def external_ip(timeout: float = 2.0) -> str | None:
    """The server's public IP from Google Cloud; None elsewhere."""
    override = os.environ.get("BTCBOT_PUBLIC_IP")
    if override:
        return override
    try:
        req = urllib.request.Request(IP_URL, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ip = resp.read().decode().strip()
        ipaddress.ip_address(ip)
        return ip
    except Exception:
        return None


class CertStore:
    """certbot's folders, the challenge folder, and the certificate it makes."""

    def __init__(self, base: str | None = None):
        self.base = base or os.path.join(keystore.folder(), "https")
        self.config = os.path.join(self.base, "config")
        self.work = os.path.join(self.base, "work")
        self.logs = os.path.join(self.base, "logs")
        self.webroot = os.path.join(self.base, "webroot")
        self.fullchain = os.path.join(self.config, "live", "btcbot", "fullchain.pem")
        self.privkey = os.path.join(self.config, "live", "btcbot", "privkey.pem")

    def challenge_file(self, token: str) -> str | None:
        if not TOKEN.match(token or ""):
            return None
        return os.path.join(self.webroot, ".well-known", "acme-challenge", token)

    def info(self) -> tuple[datetime, list[str]] | None:
        """(expiry, IP addresses) of the current certificate, or None."""
        try:
            from cryptography import x509
            with open(self.fullchain, "rb") as fh:
                cert = x509.load_pem_x509_certificate(fh.read())
            try:
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
            except x509.ExtensionNotFound:
                ips = []
            return cert.not_valid_after_utc, ips
        except (OSError, ValueError):
            return None

    def good_for(self, ip: str, now: datetime) -> bool:
        info = self.info()
        return bool(info and ip in info[1] and info[0] - now > RENEW_BEFORE)

    def usable_for(self, ip: str, now: datetime) -> bool:
        info = self.info()
        return bool(info and ip in info[1] and info[0] > now)

    def certbot_command(self, ip: str) -> list[str]:
        return [sys.executable, "-m", "certbot", "certonly", "--non-interactive", "--agree-tos",
                "--register-unsafely-without-email", "--preferred-profile", "shortlived",
                "--webroot", "--webroot-path", self.webroot, "--ip-address", ip,
                "--cert-name", "btcbot", "--force-renewal",
                "--config-dir", self.config, "--work-dir", self.work, "--logs-dir", self.logs]

    def request(self, ip: str, runner=subprocess.run) -> tuple[bool, str]:
        for d in (self.config, self.work, self.logs, self.webroot):
            os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(self.webroot, 0o755)
        try:
            r = runner(self.certbot_command(ip), capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"certbot didn't run ({e})"
        if r.returncode != 0:
            text = (r.stderr or r.stdout or "").strip()
            if "No module named certbot" in text:
                return False, "certbot isn't installed yet (it comes with the newest version)"
            return False, text.splitlines()[-1][-300:] if text else f"certbot failed ({r.returncode})"
        return True, "ok"


class TLSServer(ThreadingHTTPServer):
    """The same website on port 443, locked with the certificate. The lock can be swapped live."""

    secure = True
    daemon_threads = True

    def __init__(self, address, handler, context: ssl.SSLContext):
        self.context = context
        super().__init__(address, handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.settimeout(30)
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass  # scanners and bad handshakes are noise


def make_context(store: CertStore) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(store.fullchain, store.privkey)
    return ctx


class SecureLink:
    """Keeps the https link working: gets and renews the certificate, runs the port-443 site."""

    def __init__(self, handler_class, store: CertStore | None = None, port: int = 443,
                 ip_fn=external_ip, runner=subprocess.run, clock=lambda: datetime.now(timezone.utc)):
        self.handler_class = handler_class
        self.store = store or CertStore()
        self.port = port
        self.ip_fn = ip_fn
        self.runner = runner
        self.clock = clock
        self.server: TLSServer | None = None
        self.ip: str | None = None
        self.status = "starting"     # starting | ready | failed | off
        self.message = "Setting up the secure link…"
        self.lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.server is not None

    def url(self) -> str | None:
        return f"https://{self.ip}" + ("" if self.port == 443 else f":{self.port}") if self.ready else None

    def tick(self) -> None:
        with self.lock:
            ip = self.ip_fn()
            if not ip:
                self.status, self.message = "off", "The secure link only works on the Google Cloud server."
                return
            self.ip = ip
            now = self.clock()
            if not self.store.good_for(ip, now):
                ok, why = self.store.request(ip, self.runner)
                if not ok:
                    log.warning("secure link: %s", why)
                    if not self.store.usable_for(ip, now):
                        self.status, self.message = "failed", f"Couldn't get the lock yet: {why}"
                        return
            try:
                ctx = make_context(self.store)
            except (OSError, ssl.SSLError) as e:
                self.status, self.message = "failed", f"Couldn't load the lock ({e})"
                return
            if self.server is None:
                try:
                    self.server = TLSServer(("0.0.0.0", self.port), self.handler_class, ctx)
                except OSError as e:
                    self.status, self.message = "failed", f"Couldn't open port {self.port} ({e})"
                    return
                self.port = self.server.server_address[1]   # matters only when asked for port 0 (tests)
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            else:
                self.server.context = ctx   # new connections use the new certificate
            expires = self.store.info()[0]
            self.status = "ready"
            self.message = f"Locked link ready (certificate renews by itself; this one ends {expires:%b %d})."

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # never take the website down
                self.status, self.message = "failed", f"Secure link problem: {e}"
                log.exception("secure link")
            time.sleep(CHECK_SECONDS)
