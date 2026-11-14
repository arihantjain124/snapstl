"""Secrets: the MakerWorld license password.

macOS: the login Keychain. Elsewhere / test runs (SNAPSTL_HOME set): files readable
only by this user in APP_DIR (mode 600).
"""
from __future__ import annotations

import os
import sys

from .settings import APP_DIR

SERVICE = "SnapSTL"
LICENSE = "makerworld-license-password"


def _use_keyring() -> bool:
    return sys.platform == "darwin" and not os.environ.get("SNAPSTL_HOME")


def get_secret(name: str) -> str:
    if _use_keyring():
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(SERVICE, name) or ""
        except KeyringError:
            return ""
    try:
        return (APP_DIR / f"{name}.secret").read_text().strip()
    except FileNotFoundError:
        return ""


def set_secret(name: str, value: str) -> None:
    if _use_keyring():
        import keyring
        from keyring.errors import PasswordDeleteError

        if value:
            keyring.set_password(SERVICE, name, value)
        else:
            try:
                keyring.delete_password(SERVICE, name)
            except PasswordDeleteError:
                pass
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = APP_DIR / f"{name}.secret"
    if not value:
        path.unlink(missing_ok=True)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)


def get_password() -> str:
    return get_secret(LICENSE)


def set_password(pw: str) -> None:
    set_secret(LICENSE, pw)
