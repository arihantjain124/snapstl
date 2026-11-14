"""MakerWorld login: the session cookie you paste in Settings.

SnapSTL never asks for your MakerWorld password. You copy the cookie from a signed-in
Chrome tab once; it is stored in the macOS Keychain and used to
  * check the login is still valid (an account API call), and
  * sign in SnapSTL's own Chrome window that generates the models (browser_gen.py).

Note: MakerWorld only processes "Generate" requests that come from its web page, which
is why generation runs in a real browser window rather than as plain HTTP requests.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://makerworld.com"
USER_AGENT = "SnapSTL/1.0 (+local batch tool)"


class MakerWorldError(RuntimeError):
    pass


class LoginExpired(MakerWorldError):
    """The pasted cookie no longer signs in — the user must paste a fresh one."""


class NotConfigured(MakerWorldError):
    pass


def parse_cookie(cookie: str) -> dict[str, str]:
    out = {}
    cookie = cookie.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:]
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def check_cookie_shape(cookie: str) -> list[str]:
    c = parse_cookie(cookie)
    if not c:
        return ["That doesn't look like a cookie header (expected name=value; name=value …)"]
    if "bbl_csrf_token" not in c:
        return ["The cookie is missing bbl_csrf_token — copy the whole cookie header from a makerworld.com/api request"]
    return []


@dataclass
class Session:
    cookie: str

    def _headers(self) -> dict:
        c = parse_cookie(self.cookie)
        h = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Cookie": "; ".join(f"{k}={v}" for k, v in c.items()),
        }
        if "bbl_csrf_token" in c:
            h["x-bbl-csrf-token"] = c["bbl_csrf_token"]
        return h

    def check_login(self) -> bool:
        """True if MakerWorld accepts the cookie (account API answers)."""
        req = urllib.request.Request(API + "/api/v1/user-service/my/message/count", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.loads(r.read() or b"{}")
                return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not e.headers.get("cf-mitigated"):
                return False
            raise MakerWorldError(f"MakerWorld returned HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise MakerWorldError(f"Can't reach MakerWorld: {e.reason}") from e
