"""Staff PINs (0.25.1): a check is saved under the person who typed their PIN, not a name anyone can pick.

* Each staff member (``staff`` table, shared with every computer) may have a 4-digit **PIN**, stored only as a
  salted hash (``pin_hash``; the newest ``pin_changed`` wins between computers). PINs are unique among the
  people shown.
* On Print → Verify a person types their PIN once; the computer keeps them signed in for checking until
  ``IDLE_S`` with no checks, or until they press Done. A short-lived token (kept in memory here, never stored)
  stands for them.
* The **owner PIN** (this computer, ``owner-pin.json``, mode 600) guards Settings → Staff: adding or hiding
  people and setting or resetting their PINs. Forgotten: ``uv run snapstl-admin reset-owner-pin`` on this
  computer.
* Until anyone has a PIN, Verify works as before (choose the name), so updating never blocks checking.

A 4-digit PIN stops one person marking checks under someone else's name; it can't stop two people sharing one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import db
from .settings import APP_DIR

OWNER_FILE = APP_DIR / "owner-pin.json"
IDLE_S = 30 * 60
_PIN = re.compile(r"\d{4}")
_N = 2 ** 12  # scrypt cost: a PIN is checked against every person's hash


class PinError(ValueError):
    pass


def _hash(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=8, p=1).hex()


def _match(pin: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    try:
        return hmac.compare_digest(_hash(pin, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def _check_format(pin: str) -> str:
    pin = (pin or "").strip()
    if not _PIN.fullmatch(pin):
        raise PinError("A PIN is 4 digits")
    return pin


# ---------------- owner PIN (this computer) ----------------
def owner_set() -> bool:
    try:
        return bool(json.loads(OWNER_FILE.read_text()).get("hash"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def owner_ok(pin: str) -> bool:
    try:
        return _match((pin or "").strip(), json.loads(OWNER_FILE.read_text()).get("hash"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def set_owner(pin: str, old: str = "") -> None:
    pin = _check_format(pin)
    if owner_set() and not owner_ok(old):
        raise PinError("The current owner PIN is wrong")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(OWNER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"hash": _hash(pin)}, f)


def reset_owner() -> None:
    OWNER_FILE.unlink(missing_ok=True)


def require_owner(pin: str) -> None:
    """Settings → Staff changes need the owner PIN once it is set."""
    if owner_set() and not owner_ok(pin):
        raise PinError("Type the owner PIN to change staff")


# ---------------- staff PINs (shared) ----------------
def pins_on(con) -> bool:
    """True once anyone shown has a PIN: from then on a check needs a PIN."""
    return con.execute("SELECT 1 FROM staff WHERE hidden=0 AND COALESCE(pin_hash,'')<>'' LIMIT 1").fetchone() is not None


def _who(con, pin: str, but: str = "") -> str | None:
    for name, h in con.execute("SELECT name, pin_hash FROM staff WHERE hidden=0 AND COALESCE(pin_hash,'')<>''"):
        if name.lower() != but.lower() and _match(pin, h):
            return name
    return None


def set_pin(con, name: str, pin: str) -> None:
    pin = _check_format(pin)
    row = con.execute("SELECT name FROM staff WHERE name=?", (name,)).fetchone()
    if row is None:
        raise PinError("No such staff member")
    if _who(con, pin, but=row[0]):
        raise PinError("Someone else has that PIN — choose another")
    now = db.now()
    con.execute("UPDATE staff SET pin_hash=?, pin_changed=?, sent=0 WHERE name=?", (_hash(pin), now, row[0]))


def clear_pin(con, name: str) -> None:
    con.execute("UPDATE staff SET pin_hash='', pin_changed=?, sent=0 WHERE name=?", (db.now(), name))


# ---------------- who is checking (Verify) ----------------
_SESSIONS: dict[str, tuple[str, float]] = {}
_LOCK = threading.Lock()


def start(pin: str) -> dict:
    pin = (pin or "").strip()
    with db.tx() as con:
        name = _who(con, pin) if _PIN.fullmatch(pin) else None
    if not name:
        raise PinError("Wrong PIN")
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _SESSIONS[token] = (name, time.time())
    return {"name": name, "token": token, "idle_minutes": IDLE_S // 60}


def end(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token or "", None)


def resolve(token: str) -> str:
    """The person a token stands for (and they stay signed in); PinError when it has expired."""
    now = time.time()
    with _LOCK:
        s = _SESSIONS.get(token or "")
        if s is None or now - s[1] > IDLE_S:
            _SESSIONS.pop(token or "", None)
            raise PinError("Type your PIN again (signed out after 30 minutes without a check)")
        _SESSIONS[token] = (s[0], now)
        return s[0]


def checker(token: str, name: str) -> str:
    """Who a check / merge is saved under: the PIN's person once PINs are in use, else the chosen name."""
    with db.tx() as con:
        on = pins_on(con)
    return resolve(token) if on else name
