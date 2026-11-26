"""ShopDeck partner API (0.37, owner 2026-11-26): live order status between reports.

* **Connect** (Settings → ShopDeck): the owner pastes the app's App ID (ShopDeck partner portal → Apps), presses
  Connect ShopDeck and allows it on ShopDeck's own consent screen. ShopDeck sends the browser back to
  ``/api/shopdeck/callback`` on this computer with a one-time code; SnapSTL exchanges it for an access key
  (90 days) and a refresh token (180 days, one use) and renews both 30 days before the key runs out, so the
  owner never has to allow it again. Only this computer holds the key (``shopdeck.json``, mode 600): every
  renewal makes the previous key useless, so two computers can't share it. Other computers get the statuses
  through the shared records, like a report's.
* **Live status**: every ``poll_s`` the orders of the last 7 days (and every 6th time the last 35) are read
  (GET /orders: status, AWB, courier, clubbed order, label link) and kept in ``shopdeck_live``. An order SnapSTL
  knows is moved **forward only**, and only to the statuses the API names without doubt (checked against the
  report, 2026-11-26): printed → PACK, packed → HANDOVER, dispatched → IN-TRANSIT, delivered → DELIVERED,
  cancel_initiated / cancelled → CANCELLED…, rto_* → RTO_*. ``created`` is left to the report: ShopDeck uses it
  for PRINT, AWB EXPIRED and ACCEPTANCE PENDING alike. The change goes through the report's own rules
  (``ingest.live_status``: the same stamp, the same flags, e.g. *Cancelled – don't pack*), so a status from the
  API and one from a report never undo each other's newer news.
* The API has no name / colour (customisation), no courier change and no Regenerate: the report upload and the
  helper stay for those.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import db
from .settings import APP_DIR

CONF_FILE = APP_DIR / "shopdeck.json"
API_BASE = "https://integrations.shopdeck.com/api/setu"
CONSENT_URL = "https://integrations.shopdeck.com/oauth"
SCOPES = ("read_orders", "write_orders", "read_customers")
FIELDS = "id,order_status,awb_no,courier_name,clubbed_id,label,updated_at"
RENEW_BEFORE_S = 30 * 86400
STATE_TTL_S = 30 * 60
MAX_PAGES = 40  # per 7-day window (100 orders a page)

# ShopDeck order_status → the report's status words (only the ones that mean one thing)
STATUS = {
    "printed": "PACK",
    "packed": "HANDOVER",
    "dispatched": "IN-TRANSIT",
    "delivered": "DELIVERED",
    "cancel_initiated": "CANCELLED INITIATED",
    "cancelled": "CANCELLED",
    "rto_initiated": "RTO_INITIATED",
    "rto_delivered": "RTO_DELIVERED",
    "rto_acknowledged": "RTO_ACKNOWLEDGED",
}
# how far along an order is (report words, normalised); a status only ever moves to a higher rank
_RANK = {"PRINT": 0, "AWB-EXPIRED": 0, "ACCEPTANCE-PENDING": 0, "PACK": 1, "HANDOVER": 2, "IN-TRANSIT": 3,
         "DELIVERED": 4, "RTO-INITIATED": 4, "RTO-DELIVERED": 5, "RTO-ACKNOWLEDGED": 6}


class ShopDeckError(RuntimeError):
    pass


def _norm(s: str | None) -> str:
    return (s or "").upper().replace("_", "-").replace(" ", "-")


def forward(cur: str | None, new: str) -> bool:
    """Whether the live status `new` (report words) may replace the order's status `cur`."""
    c, n = _norm(cur), _norm(new)
    if not c or c == n:
        return False
    if n.startswith("CANCEL"):  # cancelled before it shipped; CANCELLED INITIATED → CANCELLED
        if c.startswith("CANCEL"):
            return c == "CANCELLED-INITIATED" and n == "CANCELLED"
        return c in _RANK and _RANK[c] <= 2
    if c.startswith("CANCEL") or c not in _RANK or n not in _RANK:
        return False
    return _RANK[n] > _RANK[c]


# ---------------- this computer's connection (shopdeck.json) ----------------
_LOCK = threading.RLock()


def load() -> dict:
    try:
        d = json.loads(CONF_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(d: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the access key
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, indent=2))
    os.replace(tmp, CONF_FILE)
    try:
        os.chmod(CONF_FILE, 0o600)
    except OSError:
        pass


def _update(**kw) -> dict:
    with _LOCK:
        d = load()
        for k, v in kw.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        _save(d)
        return d


def connected(d: dict | None = None) -> bool:
    d = load() if d is None else d
    return bool(d.get("key_secret") and d.get("refresh_token"))


def set_config(app_id: str | None = None, apply_status: bool | None = None, poll_s: int | None = None) -> dict:
    kw: dict = {}
    if app_id is not None:
        a = app_id.strip()
        if a and not (a.isalnum() and 8 <= len(a) <= 64):
            raise ShopDeckError("App ID: the letters and digits shown on the app's page in the partner portal")
        kw["app_id"] = a or None
    if apply_status is not None:
        kw["apply_status"] = bool(apply_status)
    if poll_s is not None:
        if not isinstance(poll_s, int) or isinstance(poll_s, bool) or not 60 <= poll_s <= 3600:
            raise ShopDeckError("Check every: 60–3600 seconds")
        kw["poll_s"] = poll_s
    return _update(**kw)


def redirect_url() -> str:
    from . import PORT

    return f"http://127.0.0.1:{PORT}/api/shopdeck/callback"


def connect_url() -> str:
    """ShopDeck's consent screen for this app (a fresh `state` each time, checked on the way back)."""
    d = load()
    if not d.get("app_id"):
        raise ShopDeckError("Paste the App ID first (Settings → ShopDeck)")
    state = secrets.token_urlsafe(18)
    _update(pending_state=state, pending_at=time.time())
    q = urllib.parse.urlencode({"app_id": d["app_id"], "scope": ",".join(SCOPES),
                                "redirection_url": redirect_url(), "state": state})
    return f"{d.get('consent_url') or CONSENT_URL}?{q}"


def _post(url: str, body: dict, key: str = "") -> dict:
    return _http("POST", url, body=body, key=key)


def _http(method: str, url: str, body: dict | None = None, key: str = "", timeout: float = 30) -> dict:
    """One call; ShopDeck's error list → ShopDeckError (its code and message). Tests replace this."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "SnapSTL",
        **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = (json.loads(e.read() or b"{}").get("errors") or [{}])[0]
        except ValueError:
            err = {}
        x = ShopDeckError(f"ShopDeck {e.code}: {err.get('message') or e.reason}")
        x.status, x.code = e.code, err.get("code") or ""  # type: ignore[attr-defined]
        x.retry_after = err.get("retryAfterSeconds")  # type: ignore[attr-defined]
        raise x from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        x = ShopDeckError(f"ShopDeck can't be reached: {e}")
        x.status, x.code = 0, "NETWORK"  # type: ignore[attr-defined]
        raise x from None


def _store_tokens(data: dict, **extra) -> dict:
    t = data.get("data") or {}
    if not t.get("key_secret") or not t.get("refresh_token"):
        raise ShopDeckError("ShopDeck answered without a key")
    return _update(key_secret=t["key_secret"], refresh_token=t["refresh_token"], expires_at=t.get("expires_at"),
                   refresh_expires_at=t.get("refresh_expires_at"), scope=t.get("scope"), last_error=None, **extra)


def callback(code: str, state: str) -> dict:
    """The consent screen sent the browser back: check `state`, exchange the code (once) for the keys."""
    d = load()
    want, at = d.get("pending_state"), float(d.get("pending_at") or 0)
    if not want or not secrets.compare_digest(str(state or ""), str(want)) or time.time() - at > STATE_TTL_S:
        raise ShopDeckError("This sign-in link isn't the one SnapSTL started (or it's over 30 minutes old) — "
                            "press Connect ShopDeck again")
    _update(pending_state=None, pending_at=None)
    base = d.get("api_base") or API_BASE
    r = _post(f"{base}/oauth/access-token", {"code": code, "redirection_url": redirect_url(), "state": state})
    return _store_tokens(r, connected_at=time.time())


def _iso_ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def renew_if_due(force: bool = False) -> bool:
    """Refresh the keys 30 days before the access key expires (each refresh token works once)."""
    with _LOCK:
        d = load()
        if not connected(d):
            return False
        if not force and _iso_ts(d.get("expires_at")) - time.time() > RENEW_BEFORE_S:
            return False
        base = d.get("api_base") or API_BASE
        try:
            r = _post(f"{base}/oauth/refresh-access-token", {"refresh_token": d["refresh_token"]})
        except ShopDeckError as e:
            if getattr(e, "status", 0) in (401, 404):  # used, expired or revoked: only Connect fixes it
                _update(key_secret=None, refresh_token=None, last_error=f"{e} — press Connect ShopDeck again")
            raise
        _store_tokens(r, renewed_at=time.time())
        return True


def disconnect() -> None:
    _update(key_secret=None, refresh_token=None, expires_at=None, refresh_expires_at=None, scope=None,
            pending_state=None, pending_at=None)


def api_get(path: str, params: dict) -> dict:
    d = load()
    if not connected(d):
        raise ShopDeckError("ShopDeck isn't connected")
    url = f"{d.get('api_base') or API_BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(2):
        try:
            return _http("GET", url, key=d["key_secret"])
        except ShopDeckError as e:
            if getattr(e, "code", "") == "RATE_LIMITED" and attempt == 0:
                time.sleep(min(60, int(getattr(e, "retry_after", 0) or 30)))
                continue
            raise
    raise ShopDeckError("ShopDeck: rate limited")


# ---------------- live status ----------------
def _windows(days: int, today: date) -> list[tuple[str, str]]:
    out, end = [], today
    while (today - end).days < days:
        start = end - timedelta(days=6)
        out.append((start.isoformat(), end.isoformat()))
        end = start - timedelta(days=1)
    return out


def fetch(days: int = 7, today: date | None = None) -> list[dict]:
    """Every order item created in the last `days` days (7-day windows, 100 a page)."""
    rows: list[dict] = []
    for start, end in _windows(days, today or _ist_today()):
        for page in range(1, MAX_PAGES + 1):
            r = api_get("/orders", {"start_date": start, "end_date": end, "fields": FIELDS,
                                    "page_size": 100, "page_no": page})
            got = r.get("data") or []
            rows += [x for x in got if isinstance(x, dict) and x.get("id")]
            if not got or not (r.get("page_info") or {}).get("has_next_page"):
                break
    return rows


def _ist_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def stamp() -> str:
    """A status stamp like a report's (ingest.status_stamp): as of today (IST), seen now."""
    d = _ist_today().isoformat()
    return f"{db.STAMP}{d}|{d}|{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"


def store_and_apply(rows: list[dict], apply: bool = True) -> dict:
    """Keep what ShopDeck said; move the orders SnapSTL knows forward. Returns counts and the changes."""
    from .ingest import live_status

    now, st = time.time(), stamp()
    out = {"seen": len(rows), "known": 0, "changed": []}
    with db.tx() as con:
        for x in rows:
            oid = str(x["id"])
            con.execute("""INSERT INTO shopdeck_live(order_id, status, awb_no, courier, clubbed_id, label, sd_updated_at, seen_at)
                           VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,
                           awb_no=excluded.awb_no, courier=excluded.courier, clubbed_id=excluded.clubbed_id,
                           label=excluded.label, sd_updated_at=excluded.sd_updated_at, seen_at=excluded.seen_at""",
                        (oid, x.get("order_status"), x.get("awb_no"), x.get("courier_name"), x.get("clubbed_id"),
                         x.get("label"), x.get("updated_at"), now))
            cur = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
            if cur is None:
                continue
            out["known"] += 1
            new = STATUS.get(str(x.get("order_status") or "").lower())
            if not apply or not new or not forward(cur["source_status"], new):
                continue
            if live_status(con, dict(cur), new, st):
                out["changed"].append({"order_id": oid, "from": cur["source_status"], "to": new, "at": now})
    return out


class Poller:
    """Every `poll_s` (default 5 minutes): renew the key when due, read the orders, apply the statuses."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self.n = 0
        self.last: dict = {}
        self.changes: list[dict] = []  # the latest changes (newest first), for Settings → ShopDeck

    def start(self) -> None:
        if os.environ.get("SNAPSTL_NO_SHOPDECK") or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-shopdeck")
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def once(self) -> dict:
        d = load()
        if not connected(d):
            return {}
        t0 = time.time()
        try:
            renew_if_due()
            days = 35 if self.n % 6 == 0 else 7
            self.n += 1
            r = store_and_apply(fetch(days), apply=d.get("apply_status", True))
            self.changes = (r["changed"][::-1] + self.changes)[:50]
            self.last = {"at": t0, "ok": True, "days": days, "seen": r["seen"], "known": r["known"],
                         "changed": len(r["changed"]), "took_s": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001  (a bad answer never stops the loop)
            self.last = {"at": t0, "ok": False, "error": str(e)[:300]}
        return self.last

    def _run(self) -> None:
        time.sleep(20)  # let the app start first
        while True:
            self.once()
            self._wake.wait(max(60, int(load().get("poll_s") or 300)))
            self._wake.clear()


POLLER = Poller()


def status() -> dict:
    """Settings → ShopDeck (never the keys themselves)."""
    d = load()
    return {"app_id": d.get("app_id") or "", "connected": connected(d), "expires_at": d.get("expires_at"),
            "refresh_expires_at": d.get("refresh_expires_at"), "scope": d.get("scope") or [],
            "connected_at": d.get("connected_at"), "renewed_at": d.get("renewed_at"),
            "apply_status": d.get("apply_status", True), "poll_s": int(d.get("poll_s") or 300),
            "redirect_url": redirect_url(), "last_error": d.get("last_error"), "last": POLLER.last,
            "changes": POLLER.changes[:20], "bridge": _bridge()}


def _bridge() -> dict:
    """0.37.1: the customisation bridge's inbox (reportwatch.import_bridge) and its latest files."""
    from . import reportwatch

    return {"folder": str(reportwatch.bridge_folder()), "files": reportwatch.WATCHER.bridge_last[:10]}


def live(order_ids: list[str]) -> dict:
    """What ShopDeck last said about these orders (Orders page)."""
    if not order_ids:
        return {}
    with db.tx() as con:
        q = ",".join("?" * len(order_ids))
        return {r["order_id"]: r for r in db.rows(con, f"SELECT * FROM shopdeck_live WHERE order_id IN ({q})",
                                                  *order_ids)}
