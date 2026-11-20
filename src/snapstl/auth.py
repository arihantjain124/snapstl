"""Single shared web-UI password + signed session cookie.

Password hash (scrypt) and the cookie-signing key live in APP_DIR/auth.json
(mode 600). Set the password with:  uv run snapstl-admin set-password
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time

from .settings import APP_DIR

AUTH_FILE = APP_DIR / "auth.json"
COOKIE = "snapstl_session"
SESSION_DAYS = 30


def _load() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def enabled() -> bool:
    return bool(_load().get("hash"))


def set_password(pw: str) -> None:
    if len(pw) < 10:
        raise ValueError("Use at least 10 characters")
    salt = secrets.token_bytes(16)
    d = _load()
    d.update({"salt": salt.hex(), "hash": _hash(pw, salt), "key": secrets.token_hex(32)})  # new key logs everyone out
    _save(d)


def check_password(pw: str) -> bool:
    d = _load()
    if not d.get("hash"):
        return False
    return hmac.compare_digest(_hash(pw, bytes.fromhex(d["salt"])), d["hash"])


def make_cookie() -> str:
    d = _load()
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(bytes.fromhex(d["key"]), exp.encode(), hashlib.sha256).digest()
    return exp + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def valid_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    d = _load()
    if not d.get("key"):
        return False
    exp, sig = value.split(".", 1)
    good = base64.urlsafe_b64encode(
        hmac.new(bytes.fromhex(d["key"]), exp.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    try:
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except ValueError:
        return False


def admin() -> None:
    """CLI: snapstl-admin set-password [password] | reset-owner-pin"""
    args = sys.argv[1:]
    if args and args[0] == "reset-owner-pin":  # forgotten owner PIN (Settings → Staff): set a new one there
        from . import staff

        staff.reset_owner()
        print("Owner PIN removed. Set a new one in Settings → Staff.")
        return
    if not args or args[0] != "set-password":
        print("usage: snapstl-admin set-password [password] | reset-owner-pin")
        raise SystemExit(2)
    if len(args) > 1:
        pw = args[1]
    else:
        import getpass

        pw = getpass.getpass("New web UI password: ")
    set_password(pw)
    print("Password set. Existing sessions were signed out.")
