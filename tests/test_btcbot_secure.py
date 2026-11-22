"""The locked (https) link, the saved Kalshi key, and switching money modes. No network."""
import json
import os
import shutil
import ssl
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from btcbot import keystore, web
from btcbot.kalshi import KalshiError
from btcbot.secure import CertStore, SecureLink
from btcbot.settings import load

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "btcbot", "settings.example.yaml")
KEY = "web-key-1234"
NOW = datetime.now(timezone.utc)


def write_cert(store: CertStore, days: float, ip="127.0.0.1"):
    """A self-signed stand-in for the Let's Encrypt certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "btcbot test")])
    import ipaddress
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(hours=1)).not_valid_after(NOW + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(os.path.dirname(store.fullchain), exist_ok=True)
    with open(store.fullchain, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(store.privkey, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return store.fullchain


def rsa_pem() -> str:
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption()).decode()


class FakeRunner:
    """Stands in for certbot: records the command and (optionally) 'issues' a certificate."""

    def __init__(self, store, ok=True):
        self.store, self.ok, self.calls = store, ok, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        import subprocess
        if self.ok:
            write_cert(self.store, 6)
            return subprocess.CompletedProcess(cmd, 0, "done", "")
        return subprocess.CompletedProcess(cmd, 1, "", "Some challenges have failed.")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.old_env = os.environ.get(keystore.DIR_ENV)
        os.environ[keystore.DIR_ENV] = os.path.join(self.dir, "secrets")
        self.addCleanup(self.restore_env)
        with open(EXAMPLE) as fh:
            raw = yaml.safe_load(fh)
        raw["files"] = {"folder": os.path.join(self.dir, "data")}
        raw["plays"][1]["enabled"] = False
        self.path = os.path.join(self.dir, "settings.yaml")
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        self.http = web.make_server(self.path, 0, KEY, host="127.0.0.1")
        self.app = self.http.RequestHandlerClass.app
        self.app.fetcher = lambda base, series: (_ for _ in ()).throw(OSError("offline"))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.store = CertStore(os.path.join(self.dir, "https"))

    def restore_env(self):
        if self.old_env is None:
            os.environ.pop(keystore.DIR_ENV, None)
        else:
            os.environ[keystore.DIR_ENV] = self.old_env

    def start_link(self, runner=None):
        self.link = SecureLink(self.http.RequestHandlerClass, self.store, port=0,
                               ip_fn=lambda: "127.0.0.1", runner=runner or FakeRunner(self.store))
        self.app.secure_link = self.link
        self.link.tick()
        if self.link.server:
            self.addCleanup(self.link.server.server_close)
            self.addCleanup(self.link.server.shutdown)
        return self.link

    def call(self, path, data=None, secure=False, cookie=True):
        base = f"https://127.0.0.1:{self.link.port}" if secure else f"http://127.0.0.1:{self.http.server_address[1]}"
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(base + path, data=body)
        if cookie:
            req.add_header("Cookie", f"btcbot_key={KEY}")
        ctx = None
        if secure:
            ctx = ssl.create_default_context(cafile=self.store.fullchain)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ctx))
        try:
            r = opener.open(req, timeout=10)
            return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)


class TestLockedLink(Base):
    def test_gets_a_certificate_and_serves_https(self):
        runner = FakeRunner(self.store)
        link = self.start_link(runner)
        self.assertEqual(link.status, "ready")
        cmd = runner.calls[0]
        for flag in ("certonly", "--preferred-profile", "shortlived", "--webroot", "--non-interactive",
                     "--register-unsafely-without-email"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--ip-address") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--webroot-path") + 1], self.store.webroot)
        status, body, headers = self.call(f"/?key={KEY}", secure=True, cookie=False)
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers.get("Set-Cookie", ""))
        self.assertNotIn("A locked link is ready", body)
        _, body, _ = self.call("/")                                 # plain http points to it
        self.assertIn("A locked link is ready", body)
        self.assertIn(f"https://127.0.0.1:{link.port}/?key={KEY}", body)

    def test_good_certificate_is_kept(self):
        write_cert(self.store, 5)
        runner = FakeRunner(self.store)
        self.start_link(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.link.status, "ready")

    def test_renews_when_3_days_are_left_and_swaps_the_lock(self):
        write_cert(self.store, 5)
        self.start_link()
        old = self.link.server.context
        write_cert(self.store, 2)                                   # time passed
        runner = FakeRunner(self.store)
        self.link.runner = runner
        self.link.tick()
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNot(self.link.server.context, old)
        self.assertEqual(self.call("/", secure=True)[0], 200)

    def test_new_ip_gets_a_new_certificate(self):
        write_cert(self.store, 5, ip="10.0.0.9")
        runner = FakeRunner(self.store)
        self.start_link(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_failure_is_reported_and_http_keeps_working(self):
        link = self.start_link(FakeRunner(self.store, ok=False))
        self.assertEqual(link.status, "failed")
        self.assertIn("challenges have failed", link.message)
        self.assertIsNone(link.server)
        self.assertEqual(self.call("/")[0], 200)

    def test_challenge_file_is_served_without_the_key(self):
        self.start_link()
        token = "abcDEF123_-xyz"
        path = self.store.challenge_file(token)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("token.thumbprint")
        status, body, _ = self.call(f"/.well-known/acme-challenge/{token}", cookie=False)
        self.assertEqual((status, body), (200, "token.thumbprint"))
        self.assertEqual(self.call("/.well-known/acme-challenge/..%2Fsecrets", cookie=False)[0], 404)
        self.assertEqual(self.call("/.well-known/acme-challenge/missing-token-123", cookie=False)[0], 404)


class TestKalshiKey(Base):
    def test_key_form_only_over_the_locked_link(self):
        self.start_link()
        self.app.key_tester = lambda *a: D("12.34")
        _, body, _ = self.call("/settings")
        self.assertNotIn('name="pem"', body)
        self.assertIn("open the 🔒 locked link", body)
        self.call("/key", data={"key_id": "1a2b3c4d-1111-2222-3333-444455556666", "pem": rsa_pem()})
        self.assertEqual(keystore.load_key(), (None, None))
        _, body, _ = self.call("/settings")
        self.assertIn("Not done: open the 🔒 locked link", body)

    def test_saves_a_working_key_privately(self):
        self.start_link()
        seen = []
        self.app.key_tester = lambda key_id, pem, base: seen.append((key_id, base)) or D("12.34")
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn('name="pem"', body)
        status, _, _ = self.call("/key", secure=True,
                                 data={"key_id": " 1a2b3c4d-1111-2222-3333-444455556666 ", "pem": rsa_pem()})
        self.assertEqual(status, 303)
        key_id, key_path = keystore.load_key()
        self.assertEqual(key_id, "1a2b3c4d-1111-2222-3333-444455556666")
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(keystore.folder()).st_mode), 0o700)
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn("Key saved and it works: your main Kalshi account has $12.34", body)
        self.assertIn("ID 1a2b…6666", body)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", body)

    def test_rejected_key_is_not_saved(self):
        self.start_link()

        def refuse(*a):
            raise KalshiError(401, "unauthorized")
        self.app.key_tester = refuse
        self.call("/key", secure=True, data={"key_id": "1a2b3c4d-1111", "pem": rsa_pem()})
        self.assertEqual(keystore.load_key(), (None, None))
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn("Kalshi didn&#x27;t accept it", body)

    def test_bad_paste_is_explained(self):
        self.start_link()
        self.call("/key", secure=True, data={"key_id": "1a2b3c4d-1111", "pem": "hello"})
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn("doesn&#x27;t look like a private key", body)

    def test_one_line_paste_still_works(self):
        pem = rsa_pem()
        flat = pem.replace("\n", " ")
        key_id, cleaned = keystore.check_key("1a2b3c4d-1111", flat)
        serialization.load_pem_private_key(cleaned, password=None)

    def test_bot_uses_the_saved_key(self):
        from btcbot.__main__ import make_client
        pem = rsa_pem()
        key_id, cleaned = keystore.check_key("1a2b3c4d-1111", pem)
        keystore.save_key(key_id, cleaned)
        env = {k: os.environ.pop(k, None) for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH")}
        try:
            client = make_client(load(self.path), need_key=True)
        finally:
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(client.key_id, "1a2b3c4d-1111")
        self.assertIsNotNone(client.private_key)


class TestMoneyMode(Base):
    def test_switch_to_real_money_and_back(self):
        self.start_link()
        self.app.key_tester = lambda *a: D("20")
        self.call("/live", secure=True, data={"confirm": "I accept real-money risk"})
        self.assertEqual(load(self.path).mode, "paper")               # no key yet
        self.call("/key", secure=True, data={"key_id": "1a2b3c4d-1111", "pem": rsa_pem()})
        self.call("/live", secure=True, data={"confirm": "yes"})
        self.assertEqual(load(self.path).mode, "paper")               # wrong words
        self.call("/live", data={"confirm": "I accept real-money risk"})
        self.assertEqual(load(self.path).mode, "paper")               # not over plain http
        self.call("/live", secure=True, data={"confirm": "I accept real-money risk"})
        self.assertEqual(load(self.path).mode, "live")
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn("Switched to REAL MONEY", body)
        self.call("/paper", data={})                                  # going back is always allowed
        self.assertEqual(load(self.path).mode, "paper")

    def test_live_switch_explains_subaccount_rule(self):
        self.start_link()
        self.app.key_tester = lambda *a: D("20")
        self.call("/key", secure=True, data={"key_id": "1a2b3c4d-1111", "pem": rsa_pem()})
        with open(self.path) as fh:
            raw = yaml.safe_load(fh)
        raw["plays"][1]["enabled"] = True
        with open(self.path, "w") as fh:
            yaml.safe_dump(raw, fh)
        self.call("/live", secure=True, data={"confirm": "I accept real-money risk"})
        self.assertEqual(load(self.path).mode, "paper")
        _, body, _ = self.call("/settings", secure=True)
        self.assertIn("its own Kalshi subaccount", body)


if __name__ == "__main__":
    unittest.main()
