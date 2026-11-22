"""Where the Kalshi key is kept on the server: a folder only the bot's user can read.

The website saves it here (over the locked https link only), and the bot reads
it when it trades with real money. Nothing ever shows the key again; the
website only shows the first and last few letters of the Key ID.
"""
from __future__ import annotations

import os
import re

DIR_ENV = "BTCBOT_SECRETS_DIR"
KEY_ID_FILE = "kalshi_key_id"
KEY_FILE = "kalshi_key.pem"


class KeyError_(ValueError):
    """A key that can't be used, with a plain reason."""


def folder() -> str:
    return os.environ.get(DIR_ENV) or os.path.expanduser("~/.btcbot")


def paths() -> tuple[str, str]:
    return os.path.join(folder(), KEY_ID_FILE), os.path.join(folder(), KEY_FILE)


def normalize_pem(text: str) -> bytes:
    """Clean up a pasted key: stray spaces, Windows line endings, or a key pasted as one long line."""
    text = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        raise KeyError_("that doesn't look like a private key file. Paste the whole text, "
                        "including the -----BEGIN and -----END lines.")
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)).encode()


def check_key(key_id: str, pem_text: str) -> tuple[str, bytes]:
    """Return the cleaned Key ID and key, or raise KeyError_ saying what's wrong."""
    from cryptography.hazmat.primitives import serialization

    key_id = (key_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", key_id):
        raise KeyError_("the Key ID should be the letters and numbers Kalshi showed (like 1a2b3c4d-...).")
    pem = normalize_pem(pem_text)
    try:
        serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as e:
        raise KeyError_(f"the private key couldn't be read ({e.__class__.__name__}). "
                        "Copy the whole file again.")
    return key_id, pem


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_key(key_id: str, pem: bytes) -> None:
    os.makedirs(folder(), mode=0o700, exist_ok=True)
    os.chmod(folder(), 0o700)
    id_path, key_path = paths()
    _write(key_path, pem)
    _write(id_path, (key_id + "\n").encode())


def load_key() -> tuple[str | None, str | None]:
    """(Key ID, path to the key file), or (None, None) when no key is saved."""
    id_path, key_path = paths()
    try:
        with open(id_path, encoding="utf-8") as fh:
            key_id = fh.read().strip()
    except OSError:
        return None, None
    if not key_id or not os.path.exists(key_path):
        return None, None
    return key_id, key_path


def remove_key() -> None:
    for path in paths():
        try:
            os.remove(path)
        except OSError:
            pass


def hint(key_id: str | None) -> str:
    if not key_id:
        return ""
    return f"{key_id[:4]}…{key_id[-4:]}" if len(key_id) > 10 else "…"
