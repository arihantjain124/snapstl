"""SnapSTL web server: portal UI, orders/batches API, helper API, settings, maintenance.

Runs locally on 127.0.0.1:8317 (launchd service `snapstl-serve`). In local mode there is no
login; a cross-site guard blocks other websites, and the Chrome helper may call /api/helper/* only.
"""
from __future__ import annotations

import csv
from contextlib import asynccontextmanager
import io
import threading
import time
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import (archive, auth, bambuddy_host, cache, computer, config, db, extras, farm, makeit, platform_os, products,
               sync, tabs, spares, updater, verify)
from . import clubbed, labels
from .ingest import add_adhoc, ingest, reset_to_ready, set_name, set_printed
from . import batches as B
from . import staff as staff_pins
from . import maintenance as M
from .batches import DOWNLOADER, MANAGER, compartments_for
from .scaling import pick_band
from .settings import COMPARTMENT_RANGE, FONTS, NUMBER_RANGES, ROTATE_TEXT, Settings, load_settings, save_settings

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 50 * 1024 * 1024
SECURE_COOKIE = os.environ.get("SNAPSTL_SECURE_COOKIE", "1") == "1"
SETTINGS: Settings = load_settings()


@asynccontextmanager
async def _lifespan(_app):
    MANAGER.output_dir = SETTINGS.output_dir
    cache.set_root(SETTINGS.downloads_dir)  # downloaded models: visible folder, kept until deleted in Finder
    cache.migrate_old_cache()               # one-time move from the old hidden cache folder
    try:  # first: from 0.24 an order is made only when checked, so what was printed before counts as checked
        verify.backfill()                   # one-time (0.24): items printed before 0.24 → checked "Before 0.24"
        checks_ok = True
    except Exception as e:  # noqa: BLE001  (never block starting the app)
        print(f"SnapSTL: check backfill skipped: {e}")
        checks_ok = False
    try:
        if checks_ok:  # else it would take made orders back to Printing
            makeit.backfill_started()       # one-time (0.23): Printing for orders whose plates were started
    except Exception as e:  # noqa: BLE001  (never block starting the app)
        print(f"SnapSTL: Printing backfill skipped: {e}")
    try:
        labels.WATCHER.kick()  # 0.33: a label PDF still awaited when the app stopped
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: label watcher skipped: {e}")
    try:
        from .ingest import repair_026
        print("SnapSTL: 0.26 repair:", repair_026())  # one-time: stuck PRINT orders, packed ≠ problem
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: 0.26 repair skipped: {e}")
    try:
        from .ingest import repair_034
        print("SnapSTL: 0.34 repair:", repair_034())  # one-time: Acceptance pending / AWB expired → To make
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: 0.34 repair skipped: {e}")
    try:
        from . import stands as _stands

        _stands.repair_031()  # 0.32: name stand plates join the batch's plates (Print / Verify)
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: 0.31 stand plates repair skipped: {e}")
    try:
        from .extras import repair_028
        print("SnapSTL: 0.28 repair:", repair_028())  # one-time: merged plates made twice by a double press
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: 0.28 repair skipped: {e}")
    try:
        from .ingest import recheck_small_first
        recheck_small_first()               # one-time (0.23.1): waiting names with a small first letter → Check name
    except Exception as e:  # noqa: BLE001
        print(f"SnapSTL: name re-check skipped: {e}")
    MANAGER.start()  # builds batches once their models arrive
    DOWNLOADER.pause_s = SETTINGS.delay_between_downloads_s
    DOWNLOADER.start()  # re-queues names that were mid-download when the app stopped
    from .logistics import start as start_logistics
    logistics_server = start_logistics()
    sync.LOOP.start()   # shared order records (Supabase), if connected in Settings
    farm.WORKER.start()  # Push to queue → Bambuddy (connects by itself on a fresh install)
    archive.CLEANER.start()  # once a day: old batch folders / unused models → Recycle Bin
    from .reportwatch import WATCHER
    WATCHER.start()  # a new report in Downloads is imported by itself (Settings: report_import)
    bambuddy_host.start_watch()  # Bambuddy computer: Keep running (starts Bambuddy / Docker when they stop)
    yield
    from .logistics import stop as stop_logistics
    stop_logistics()


app = FastAPI(title="SnapSTL", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
from . import logistics_api  # noqa: E402  (the logistics page: also usable on this computer)

logistics_api.register(app)
app.add_api_route("/api/usage", logistics_api.usage, methods=["GET"])  # Settings → Usage

OPEN_PATHS = {"/login", "/api/login", "/api/auth-check", "/favicon.ico"}


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
# Things only the SnapSTL computer itself may do (helper, Finder, folders, portal setup, reset, print-farm
# setup: Bambuddy connection, slicing settings, clearing a bed).
MAC_ONLY_PREFIXES = ("/api/helper/", "/api/downloads/reveal", "/api/portal", "/api/help-portal", "/api/maintenance/reset",
                     "/api/update", "/api/shared-records", "/api/setup", "/api/farm/")


def is_local(request: Request) -> bool:
    """Request comes from this computer (the app window, a browser here, or the helper)."""
    c = request.client.host if request.client else ""
    return c in ("127.0.0.1", "::1") and not request.headers.get("x-forwarded-for")


def _host_name(host: str) -> str:
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0]


def _blocked(request: Request) -> str | None:
    """Why this request must be refused (None = allowed to continue).

    * this computer: must use 127.0.0.1/localhost (DNS-rebinding guard); no login needed
    * other computers: only if Settings → Other computers is on AND a portal password is set; they sign in
    * other websites in any browser: rejected (Origin must match); the Chrome helper only on /api/helper/*
    """
    path = request.url.path
    host = request.headers.get("host", "")
    local = is_local(request)
    if local and _host_name(host) not in LOCAL_HOSTS:
        return "Open SnapSTL as http://127.0.0.1:8317 on this computer"
    origin = request.headers.get("origin")
    if origin:
        if origin.startswith("chrome-extension://"):
            if not (local and path.startswith("/api/helper/")):
                return "Cross-site request blocked"
        elif origin.split("://", 1)[-1] != host:
            return "Cross-site request blocked"
    if not local:
        return "The full SnapSTL app is local only; use the logistics address on port 8002"
    return None


@app.middleware("http")
async def access_control(request: Request, call_next):
    why = _blocked(request)
    if why:
        return JSONResponse({"detail": why}, status_code=403)
    path = request.url.path
    if path in OPEN_PATHS:
        return await call_next(request)
    if is_local(request):
        return await _needs_shared(request, call_next)
    if auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return await _needs_shared(request, call_next)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return RedirectResponse("/login")


# Work that may go on without the shared records: connecting them, settings, the helper's running downloads
WITHOUT_SHARED = ("/api/shared-records", "/api/sync", "/api/settings", "/api/helper/", "/api/update",
                  "/api/login", "/api/logout", "/api/portal", "/api/help-portal", "/api/maintenance/backup", "/api/farm/connect",
                  "/api/bambuddy-host")


async def _needs_shared(request: Request, call_next):
    """Every computer works only with a live connection to the shared records (sync.REQUIRED): without it,
    nothing can be changed — pages still show (GET), the app says why and Settings can fix it."""
    path = request.url.path
    if (sync.REQUIRED and request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/")
            and not path.startswith(WITHOUT_SHARED) and not sync.online()):
        return JSONResponse({"detail": sync.offline_reason()}, status_code=503)
    return await call_next(request)


# ---------------- auth ----------------
@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    if not auth.check_password(str(body.get("password", ""))):
        raise HTTPException(401, "Wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, auth.make_cookie(), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, secure=SECURE_COOKIE and req.url.scheme == "https", samesite="lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth-check")
def auth_check(request: Request):
    if is_local(request) or auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return {"ok": True}
    raise HTTPException(401)


# ---------------- web portal for other computers (this computer only) ----------------
def _portal_urls(port: int = 8002) -> list[str]:
    """Current LAN addresses, with copyable numeric IPs first."""
    hosts = [*platform_os.lan_ips(), platform_os.lan_name()]
    return [f"http://{host}:{port}" for host in dict.fromkeys(hosts) if host]


def _port() -> int:
    return int(os.environ.get("SNAPSTL_PORT", "8317"))


def _restart_service_soon() -> bool:
    """Restart the background service (launchd on a Mac, Task Scheduler on Windows) a second from now."""
    import threading

    if not platform_os.running_as_service():
        return False

    def go():
        time.sleep(1.0)
        platform_os.restart_service()

    threading.Thread(target=go, daemon=True).start()
    return True


@app.get("/api/update")
def update_check(fetch: int = 1):
    """Settings → Updates: is a newer SnapSTL waiting in the repository?"""
    return updater.check(bool(fetch)) | {"version": _version()}


@app.post("/api/update")
def update_apply(body: dict | None = None):
    branch = (body or {}).get("branch")
    if branch is not None and (not isinstance(branch, str) or not branch):
        raise HTTPException(400, "Choose a branch published in the SnapSTL repository")
    if DOWNLOADER.current is not None:
        raise HTTPException(409, f"A name is being downloaded ({DOWNLOADER.current}) — pause downloads or wait, then update")
    with db.tx() as con:
        busy = con.execute("SELECT COUNT(*) AS n FROM batches WHERE status IN ('build_requested','building') "
                           "AND computer_id=?", (computer.ID,)).fetchone()["n"]
    if busy:
        raise HTTPException(409, "A batch is being built — wait for it to finish, then update")
    try:
        r = updater.apply(branch)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    r["restarting"] = _restart_service_soon() if r["updated"] else False
    if r["updated"] and not r["restarting"]:
        r["note"] = "Updated — restart SnapSTL to use the new version"
    return r


@app.post("/api/update/restart")
def update_restart():
    """Settings → Updates: the folder was updated by hand (git pull) — restart to run the new version."""
    if DOWNLOADER.current is not None:
        raise HTTPException(409, f"A name is being downloaded ({DOWNLOADER.current}) — pause downloads or wait, then restart")
    with db.tx() as con:
        busy = con.execute("SELECT COUNT(*) AS n FROM batches WHERE status IN ('build_requested','building') "
                           "AND computer_id=?", (computer.ID,)).fetchone()["n"]
    if busy:
        raise HTTPException(409, "A batch is being built — wait for it to finish, then restart")
    if not _restart_service_soon():
        raise HTTPException(400, "SnapSTL isn't running as a background service here — quit and start it again")
    return {"restarting": True}


@app.get("/api/portal")
def portal_info():
    from .logistics import status
    return {"lan_access": SETTINGS.lan_access, "password_set": auth.enabled(),
            **status(),
            "urls": _portal_urls() if SETTINGS.lan_access else []}


@app.post("/api/portal")
async def portal_update(req: Request):
    global SETTINGS
    body = await req.json()
    pw = str(body.get("password", ""))
    if pw:
        try:
            auth.set_password(pw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    enable = bool(body.get("enabled", SETTINGS.lan_access))
    if enable and not auth.enabled():
        raise HTTPException(400, "Set a portal password first (at least 10 characters)")
    SETTINGS.lan_access = enable
    save_settings(SETTINGS)
    from . import logistics
    await run_in_threadpool(logistics.PACK.start if enable else logistics.PACK.stop)
    return portal_info() | {"restarting": False, "note": "Saved"}


@app.get("/api/help-portal")
def help_portal_info():
    """Settings → Connections → Customer help page (port 8003, its own password)."""
    from .logistics import HELP, HELP_PORT
    return {"enabled": SETTINGS.help_access, "password_set": auth.HELP.enabled(), **HELP.status(),
            "urls": _portal_urls(HELP_PORT) if SETTINGS.help_access else []}


@app.post("/api/help-portal")
async def help_portal_update(req: Request):
    body = await req.json()
    pw = str(body.get("password", ""))
    if pw:
        try:
            auth.HELP.set_password(pw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    enable = bool(body.get("enabled", SETTINGS.help_access))
    if enable and not auth.HELP.enabled():
        raise HTTPException(400, "Set a password for the customer help page first (at least 10 characters)")
    SETTINGS.help_access = enable
    save_settings(SETTINGS)
    from . import logistics
    await run_in_threadpool(logistics.HELP.start if enable else logistics.HELP.stop)
    return help_portal_info() | {"note": "Saved"}



# ---------------- app ----------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("snapstl")
    except Exception:  # noqa: BLE001
        return ""


@app.get("/api/meta")
def meta(request: Request):
    return {
        "fonts": FONTS, "rotate_text": ROTATE_TEXT, "ranges": NUMBER_RANGES,
        "compartment_range": COMPARTMENT_RANGE,
        "local": is_local(request),  # on the SnapSTL computer itself: no login, folder buttons, folder settings
        "version": _version(),
        "computer": {"id": computer.ID, "name": computer.NAME},
        "shared_records": sync.enabled(),  # Supabase order records connected in Settings
        "os": "windows" if platform_os.WINDOWS else "mac",
        "file_manager": platform_os.file_manager(),
    }


@app.get("/api/state")
def state():
    with db.tx() as con:
        counts = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) n FROM orders GROUP BY state")}
    return {
        "counts": counts, "license_set": bool(SETTINGS.license_password),
        "downloader": DOWNLOADER.snapshot(),
        "queue": B.queue_counts(), "building": MANAGER.building, "log": MANAGER.log[-120:],
        "sync": sync.STATUS.snapshot(),
        "shared": {"required": sync.REQUIRED, "online": sync.online(),
                   "reason": "" if sync.online() else sync.offline_reason()},
    }


# ---------------- ingest & orders ----------------
@app.post("/api/ingest")
async def upload_report(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large")
    name = Path(file.filename or "report.xlsx").name
    if not name.lower().endswith((".xlsx", ".xls", ".xlsm", ".csv")):
        raise HTTPException(400, "Upload the .xlsx (or .csv) reconciliation report")
    if sync.enabled():  # know every computer's batched / bundled / printed orders first (best effort)
        await run_in_threadpool(sync.sync_now)
    try:
        s = ingest(data, name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    out = s.__dict__
    if sync.enabled():  # the report's orders and newer statuses go to the shared records now
        st = await run_in_threadpool(sync.sync_now)
        out = out | {"shared": {"error": st["error"], "pending": st["pending"]}}
    return out


def _stand_size(letters: int) -> list | None:
    """Name stand (0.29): [length, height, thickness] from Settings → Name stands."""
    st = SETTINGS.stands
    b = next((b for b in sorted(st.bands, key=lambda b: b.min_letters) if b.min_letters <= letters <= b.max_letters), None)
    return [b.length, b.height, st.thickness] if b else None


def _order_view(r: dict) -> dict:
    letters = sum(c.isalpha() for c in (r.get("name") or ""))
    prod = products.of_order(r)
    stand = prod.kind == "stand"
    band = pick_band(letters, SETTINGS.scaling.bands) if letters and not stand else None
    return {
        "order_id": r["order_id"], "clubbed_id": r["clubbed_id"], "order_date": r["order_date"],
        "customer_name": r.get("customer_name") or "", "phone": r.get("phone") or "", "address": r.get("address") or "",
        "source_status": r["source_status"], "quantity": r["quantity"], "raw_name": r["raw_name"],
        "raw_color": r["raw_color"], "special_request": r["special_request"], "name": r["name"],
        "color": r["color"], "name_edited": bool(r["name_edited"]), "review_reason": r["review_reason"],
        "state": r["state"], "batch_id": r["batch_id"], "last_error": r["last_error"], "letters": letters,
        "batch_computer": r.get("batch_computer") or "", "batch_label": r.get("batch_label") or "",
        "batch_here": bool(r.get("batch_id")), "printed_at": r.get("printed_at"), "bundled_at": r.get("bundled_at"),
        "made_at": r.get("made_at"), "adhoc": r.get("sku") == "Adhoc",
        "product": prod.key, "product_label": prod.label, "product_kind": prod.kind, "added_at": r.get("first_seen"),
        "stand_size": _stand_size(letters) if stand and letters else None,
        "stand_colors": [SETTINGS.stands.products.get(prod.key, {}).get(k, "") for k in ("base_color", "letter_color")]
        if stand else None,
        "clubbed": bool(r.get("club_size", 1) > 1),
        "target": [min(band.length, SETTINGS.scaling.max_length), band.height] if band else None,
        "compartments": compartments_for(letters, SETTINGS) if letters and not stand else None,
        # 0.23 order flow
        "tab": tabs.tab_of(r), "first_status": r.get("first_status"), "print_started_at": r.get("print_started_at"),
        **{c: r.get(c) for g, cs in db.FIELD_GROUPS.items() for c in cs},
        "returned": r.get("_returned") or [],
        "spares": r.get("_spares") or [],  # 0.28: spare pieces in stock that fit
        "made_before": r.get("_made_before"),  # 0.32.1: already made in this batch — Remake to make it again
        "reuse_words": spares.describe(r.get("reuse_of")),
        "club": r.get("_club"),  # 0.34: {size, to_make, check, held, started} of its clubbed order
        "payment": r.get("payment") or "",
    }


# Order tabs (0.23): one rule, tabs.py. Sub-tabs of New orders and the Express list on top.
_CLUB = ("(CASE WHEN COALESCE(clubbed_id,'')='' THEN 1 ELSE "
         "(SELECT COUNT(*) FROM orders o2 WHERE o2.clubbed_id=orders.clubbed_id) END)")
_T = tabs.TAB_SQL
# a returned (RTO) piece with exactly the same name (same letter case) and colour, not used yet
_RTO = "REPLACE(UPPER(COALESCE({t}.source_status,'')),'_','-') LIKE 'RTO-%'"
_RETURNED = (f"EXISTS (SELECT 1 FROM orders rt WHERE rt.order_id<>orders.order_id AND rt.name=orders.name "
             f"AND {products.key_sql('rt.sku')}={products.key_sql('orders.sku')} "  # 0.29: the same product
             f"AND COALESCE(rt.color,'')=COALESCE(orders.color,'') AND {_RTO.format(t='rt')} AND rt.reused_by IS NULL "
             "AND COALESCE(orders.name,'')<>'')")
_RETURNED = f"({_RETURNED} OR {spares.match_sql()})"  # 0.28: or a spare piece in stock
_MAKING_TABS = "('to_make','check_name','ready_to_print','printing','pack_it')"
TABS = {k: f"{_T}='{k}'" for k in tabs.KEYS}
TABS.update({
    "to_make_single": f"{_T}='to_make' AND {_CLUB} <= 1 AND sku<>'Adhoc'",
    "to_make_clubbed": f"{_T}='to_make' AND {_CLUB} > 1",
    "to_make_adhoc": f"{_T}='to_make' AND sku='Adhoc'",  # typed in by hand (ingest.add_adhoc)
    "to_make_returned": f"{_T}='to_make' AND {_RETURNED}",  # a returned piece could be sent instead
    "express": f"express_by IS NOT NULL AND {_T} IN {_MAKING_TABS}",
    # 0.34 (owner): Pending Orders → To make is one list — orders ready to make and those whose name support still
    # has to check with the customer (listed in date order so none is missed, but not tickable)
    "pending": f"{_T} IN ('to_make','check_name')",
    "production": f"{_T} IN ('ready_to_print','printing')",  # 0.26: one list on the Orders page
    "all": "1=1",
})
# Express orders first (earliest needed-by date first), then the oldest order first (0.25, owner: clear the
# backlog in order); 0.34: after Express, clubbed orders whose other names are already in production
_ORDER = (f"ORDER BY (express_by IS NULL), express_by, NOT {clubbed.PARTNER_SQL}, order_date, "
          "COALESCE(clubbed_id,''), order_id")
# payment type from the uploaded report (PREPAID / COD / PARTIAL-COD)
_PAY = "COALESCE((SELECT json_extract(rr.data, '$.payment_method') FROM report_rows rr WHERE rr.order_id=orders.order_id),'')"
# Per-column filters: name → (SQL expression, exact match?)
FILTERS = {
    "order": ("order_id || ' ' || COALESCE(clubbed_id,'')", False),
    "date": ("order_date", False),
    "customer": ("customer_name", False),
    "phone": ("phone", False),
    "address": ("address", False),
    "status": ("source_status", True),
    "name": ("COALESCE(name,'') || ' ' || COALESCE(raw_name,'')", False),
    "color": ("color", True),
    "qty": ("CAST(quantity AS TEXT)", True),
    "express": ("CASE WHEN express_by IS NULL THEN '' ELSE 'yes' END", True),
    "payment": (_PAY, False),
}


def _product_param(request: Request) -> str:
    """Orders page Product chips (0.29): '' = every product."""
    p = (request.query_params.get("product") or "").strip()
    if p and p not in products.BY_KEY:
        raise HTTPException(400, f"Unknown product {p!r}")
    return p


@app.get("/api/products")
def product_list():
    """The products SnapSTL knows (Orders page chips, Settings → Name stands)."""
    return {"products": products.public()}


def _where(request: Request, tab: str) -> tuple[str, list]:
    if tab not in TABS:
        raise HTTPException(400, f"Unknown tab {tab!r}")
    where, args = [TABS[tab]], []
    prod = _product_param(request)
    if prod:
        where.append(products.in_sql([prod]))
    for key, (expr, exact) in FILTERS.items():
        v = (request.query_params.get("f_" + key) or "").strip()
        if not v:
            continue
        if exact:
            where.append(f"COALESCE({expr},'') = ?")
            args.append(v)
        else:
            where.append(f"COALESCE({expr},'') LIKE ?")
            args.append(f"%{v}%")
    q = (request.query_params.get("q") or "").strip()
    if q:
        where.append("(order_id LIKE ? OR name LIKE ? OR raw_name LIKE ? OR clubbed_id LIKE ? OR customer_name LIKE ? OR phone LIKE ?)")
        args += [f"%{q}%"] * 6
    return " AND ".join(where), args


@app.get("/api/orders")
def list_orders(request: Request, tab: str = "to_make", limit: int = 200, offset: int = 0):
    w, args = _where(request, tab)
    limit = max(1, min(limit, 1000))
    with db.tx() as con:
        total = con.execute(f"SELECT COUNT(*) FROM orders WHERE {w}", args).fetchone()[0]
        rs = db.rows(con, f"SELECT *, {_CLUB} AS club_size, {_PAY} AS payment FROM orders WHERE {w} {_ORDER} LIMIT ? OFFSET ?",
                     *args, limit, offset)
        made = verify.made_before(con, [r["order_id"] for r in rs if r["state"] in ("ready", "needs_review")])
        for r in rs:
            if r["state"] in ("ready", "needs_review") and r.get("name"):
                r["_returned"] = returned_pieces(con, r)
                r["_spares"] = spares.for_order(con, r)
            r["_made_before"] = made.get(r["order_id"])
        in_stock = spares.waiting_matches(con)
        g = clubbed.groups(con, [r["clubbed_id"] for r in rs])  # 0.34: made together
        for r in rs:
            if r["clubbed_id"] in g:
                r["_club"] = clubbed.info(g[r["clubbed_id"]])
    return {"total": total, "orders": [_order_view(r) for r in rs], "in_stock": in_stock}


def returned_pieces(con, o: dict) -> list[dict]:
    """Returned (RTO) orders with exactly this order's name (same letter case) and colour whose piece
    isn't used yet: back with us (RTO_DELIVERED) first."""
    if not o.get("name") or o.get("reuse_of"):
        return []
    rs = db.rows(con, f"SELECT order_id, source_status FROM orders rt WHERE rt.order_id<>? AND rt.name=? "
                      f"AND COALESCE(rt.color,'')=? AND {_RTO.format(t='rt')} AND rt.reused_by IS NULL "
                      f"AND {products.key_sql('rt.sku')}=?",
                 o["order_id"], o["name"], o.get("color") or "", products.of_order(o).key)
    out = [{"order_id": r["order_id"], "status": r["source_status"],
            "back": tabs.norm_status(r["source_status"]) == "RTO-DELIVERED"} for r in rs]
    return sorted(out, key=lambda x: (not x["back"], x["order_id"]))


@app.get("/api/orders/ids")
def order_ids(request: Request, tab: str = "to_make", state: str = "ready", count: int = 100000):
    """All selectable order IDs matching the current tab + filters ('Select all matching'), or the oldest `count`
    ('Select oldest'): orders that can go into a batch (Create batch). 0.34: a clubbed order is taken whole — or
    not at all while a name of it still needs its check."""
    if state != "ready":
        raise HTTPException(400, "Unknown state")
    w, args = _where(request, tab)
    with db.tx() as con:
        rs = db.rows(con, f"SELECT order_id FROM orders WHERE {w} AND {_T}='to_make' {_ORDER}", *args)
        ids, _held = clubbed.take(con, [r["order_id"] for r in rs], max(1, count))
    return ids


@app.get("/api/orders/facets")
def order_facets(request: Request, tab: str = "to_make"):
    """Tab counts plus the values offered in the status / colour filter dropdowns."""
    prod = _product_param(request)
    pw = products.in_sql([prod]) if prod else "1=1"  # 0.29: tab counts of the chosen product
    with db.tx() as con:
        counts = {k: 0 for k in TABS}
        counts.update({r[0]: r[1] for r in con.execute(f"SELECT {_T}, COUNT(*) FROM orders WHERE {pw} GROUP BY 1")})
        for k in ("to_make_single", "to_make_clubbed", "to_make_adhoc", "to_make_returned", "express", "production",
                  "pending", "all"):
            counts[k] = con.execute(f"SELECT COUNT(*) FROM orders WHERE {TABS[k]} AND {pw}").fetchone()[0]
        # Product chips: orders to make (To make + Check name) per product
        per = {r[0]: r[1] for r in con.execute(f"SELECT {products.key_sql()}, COUNT(*) FROM orders "
                                               f"WHERE {_T} IN ('to_make','check_name') GROUP BY 1")}
        w = TABS.get(tab, "1=1") + f" AND {pw}"
        statuses = [r["v"] for r in db.rows(con, f"SELECT DISTINCT source_status v FROM orders WHERE {w} ORDER BY v")]
        colors = [r["v"] for r in db.rows(con, f"SELECT DISTINCT COALESCE(color,'') v FROM orders WHERE {w} ORDER BY v")]
    return {"tabs": counts, "statuses": statuses, "colors": colors,
            "products": {p.key: per.get(p.key, 0) for p in products.PRODUCTS}}


@app.get("/api/orders.csv")
def export_orders(request: Request, tab: str = "all"):
    w, args = _where(request, tab)
    with db.tx() as con:
        rs = db.rows(con, "SELECT order_id,clubbed_id,sku,order_date,customer_name,phone,address,source_status,quantity,"
                          "raw_name,name,raw_color,color,state,batch_id,review_reason,last_error,first_file,last_file "
                          f"FROM orders WHERE {w} ORDER BY order_date", *args)
    f = io.StringIO()
    if rs:
        wr = csv.DictWriter(f, fieldnames=list(rs[0]))
        wr.writeheader()
        wr.writerows(rs)
    return StreamingResponse(iter([f.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=snapstl-orders.csv"})


@app.post("/api/orders/adhoc")
async def new_adhoc_order(req: Request):
    """An order typed in by hand (Orders → Ready → Ad-hoc): name to print, mobile, colour."""
    body = await req.json()
    try:
        o = add_adhoc(str(body.get("name", "")), str(body.get("phone", "")), str(body.get("color", "")),
                      body.get("quantity") or 1, str(body.get("note", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return _order_view(o)


@app.post("/api/orders/{order_id}/name")
async def edit_name(order_id: str, req: Request):
    body = await req.json()
    try:
        return _order_view(set_name(order_id, str(body.get("name", "")), body.get("color")))
    except KeyError as e:
        raise HTTPException(404, "No such order") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/orders/{order_id}/reset")
def reset_order(order_id: str):
    """Remake / Make anyway: back to New orders. When the webstore status would keep it in another tab
    (e.g. Delivered), a remake is recorded so the first parcel's statuses are ignored until a new label."""
    try:
        reset_to_ready(order_id)
    except KeyError as e:
        raise HTTPException(404, "No such order") from e
    with db.tx() as con:
        o = dict(con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone())
        if o.get("reuse_of"):  # made again: no longer sent with a returned piece
            db.set_group(con, order_id, "reuse")
            o["reuse_of"] = None
        # 0.32.1: an order already made in an earlier batch is made again only after a recorded Remake
        again = bool(verify.made_before(con, [order_id]))
        if again or (tabs.tab_of(o) not in ("to_make", "check_name") and o.get("flag") is None):
            db.set_group(con, order_id, "remake", remake_at=db.now(), remake_reason="Made again (Remake)")
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/orders/{order_id}/clear-flag")
def clear_flag(order_id: str):
    """Problems – check: a person handled it. The order goes to the tab its state says."""
    with db.tx() as con:
        if not db.set_group(con, order_id, "flag"):
            raise HTTPException(404, "No such order")
        db.count_usage(con, "clear_flag")
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/orders/{order_id}/use-returned")
async def use_returned(order_id: str, req: Request):
    """Send this order with a returned (RTO) piece of the same name and colour instead of making it:
    it goes straight to Printed – pack it; the piece is marked used (never matched again)."""
    rto = str((await req.json()).get("rto_order_id") or "")
    with db.tx() as con:
        cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None:
            raise HTTPException(404, "No such order")
        o = dict(cur)
        if o["state"] not in ("ready", "needs_review") or o.get("reuse_of"):
            raise HTTPException(409, "Only an order waiting to be made can use a returned piece")
        if rto not in {x["order_id"] for x in returned_pieces(con, o)}:
            raise HTTPException(409, "That returned piece doesn't match this order (or is already used)")
        db.set_group(con, order_id, "reuse", reuse_of=rto)
        db.set_group(con, rto, "reuse", reused_by=order_id)
        db.count_usage(con, "use_returned")
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/orders/{order_id}/use-spare")
async def use_spare(order_id: str, req: Request):
    """Send this order with a complete spare piece from stock instead of making it (0.28)."""
    try:
        spares.use(order_id, str((await req.json()).get("uid") or ""))
    except spares.SpareError as e:
        raise HTTPException(409, str(e)) from e
    sync.LOOP.kick()
    return {"ok": True}


@app.get("/api/spares")
def spares_list():
    """Orders → Spare pieces (0.28)."""
    with db.tx() as con:
        return spares.listing(con) | {"in_stock": spares.waiting_matches(con)}


@app.post("/api/spares")
async def spares_add(req: Request):
    b = await req.json()
    try:
        out = spares.add(str(b.get("name") or ""), str(b.get("color") or ""), str(b.get("part") or ""),
                         str(b.get("note") or ""), str(b.get("staff") or ""), int(b.get("count") or 1))
    except (spares.SpareError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    sync.LOOP.kick()
    return {"added": out}


@app.put("/api/spares/{uid}")
async def spares_edit(uid: str, req: Request):
    b = await req.json()
    try:
        spares.edit(uid, str(b.get("name") or ""), str(b.get("color") or ""), str(b.get("part") or ""),
                    str(b.get("note") or ""))
    except spares.SpareError as e:
        raise HTTPException(400, str(e)) from e
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/spares/{uid}/remove")
async def spares_remove(uid: str, req: Request):
    b = await req.json()
    try:
        spares.remove(uid, bool(b.get("put_back")))
    except spares.SpareError as e:
        raise HTTPException(400, str(e)) from e
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/orders/{order_id}/undo-returned")
def undo_returned(order_id: str):
    with db.tx() as con:
        cur = con.execute("SELECT reuse_of FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None or not cur["reuse_of"]:
            raise HTTPException(409, "This order doesn't use a returned piece")
        db.set_group(con, order_id, "reuse")
        if cur["reuse_of"].startswith(spares.PREFIX):
            spares.release(con, order_id, cur["reuse_of"])  # the spare piece is in stock again
        else:
            db.set_group(con, cur["reuse_of"], "reuse")
        db.count_usage(con, "undo_returned")
    sync.LOOP.kick()
    return {"ok": True}


@app.post("/api/orders/printed")
async def orders_printed(req: Request):
    """Bundled orders a person confirms were printed (→ Printed tab), or printed=false to undo."""
    body = await req.json()
    ids = [str(x) for x in body.get("order_ids", [])][:5000]
    n = set_printed(ids, bool(body.get("printed", True)))
    sync.LOOP.kick()
    return {"changed": n}


@app.post("/api/orders/{order_id}/skip")
def skip_order(order_id: str):
    with db.tx() as con:
        con.execute("UPDATE orders SET state='skipped', updated_at=? WHERE order_id=? AND state IN ('ready','needs_review')",
                    (db.now(), order_id))
    sync.LOOP.kick()
    return {"ok": True}


# ---------------- batches: create / build / cancel ----------------
@app.post("/api/batches")
async def new_batch(req: Request):
    body = await req.json()
    errs = SETTINGS.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    try:
        return B.create_batch([str(x) for x in body.get("order_ids", [])][:2000], SETTINGS)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/batches/next")
async def next_batch(req: Request):
    """Orders → Make next batch ({count}): the oldest orders to make (Express first) into a batch; its models are
    downloaded and its plates built by themselves (when a model fails, Batches waits for a person)."""
    body = await req.json()
    n = max(1, min(int(body.get("count") or 50), 500))
    prod = str(body.get("product") or products.ORGANIZER.key)  # 0.30: the product chosen on Orders
    if prod not in products.BY_KEY:
        raise HTTPException(400, f"Unknown product {prod!r}")
    errs = SETTINGS.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    with db.tx() as con:
        ids = [r[0] for r in con.execute(f"SELECT order_id FROM orders WHERE {_T}='to_make' "  # not those with a
                                         f"AND {products.in_sql([prod])} "
                                         f"AND NOT {spares.match_sql(complete=True)} {_ORDER}")]  # spare
        made = verify.made_before(con, ids)  # 0.32.1: already made in an earlier batch → only after Remake
        ids, held = clubbed.take(con, [i for i in ids if i not in made], n)  # 0.34: clubbed orders whole
    if not ids:
        raise HTTPException(400, "Nothing to make: To make is empty" + (f" ({clubbed.held_words(held)})" if held else ""))
    try:
        out = B.create_batch(ids, SETTINGS)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    with db.tx() as con:  # builds by itself once downloaded — not name stands yet (their plates: next update)
        con.execute("UPDATE batches SET auto_build=? WHERE id=?", (int(prod in products.BUILDABLE), out["batch_id"]))
    try:
        return out | {"download": B.start_download(out["batch_id"]), "download_error": ""}
    except ValueError as e:  # the batch is made; the download waits for what's missing (e.g. the license password)
        return out | {"download": None, "download_error": str(e)}


@app.get("/api/report-import")
def report_import_info():
    """Settings / Orders: the Downloads folder watched for new reports, and the last import done by itself."""
    from . import reportwatch

    return {"on": SETTINGS.report_import, "folder": str(reportwatch.folder()), "last": reportwatch.WATCHER.last}


@app.post("/api/report-import")
async def report_import_set(req: Request):
    SETTINGS.report_import = bool((await req.json()).get("on"))
    save_settings(SETTINGS)
    return report_import_info()


@app.post("/api/batches/{batch_id}/download")
async def batch_download(batch_id: int, req: Request):
    """Queue download; with {"force": true} it's Re-download (every model fetched again)."""
    body = await req.json() if (await req.body()) else {}
    try:
        return B.start_download(batch_id, force=bool(body.get("force")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/batches/{batch_id}/build")
def build_now(batch_id: int):
    try:
        B.request_build(batch_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/batches/{batch_id}/cancel")
def cancel(batch_id: int):
    try:
        B.cancel_batch(batch_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/downloads/retry")
def retry_downloads():
    return {"requeued": B.retry_failed()}


# ---------------- SnapSTL Helper (Chrome extension on this computer) ----------------
HELPER_DIR = Path(__file__).resolve().parents[2] / "helper"


@app.get("/api/helper/config")
def helper_config():
    """Which MakerWorld page the helper must be on (Settings → MakerWorld model page link)."""
    from .settings import design_id

    org = design_id(SETTINGS.makerworld_url)
    # 0.30: every product's page the helper may work on (designId → page); the organizer's from Settings
    pages = {org: SETTINGS.makerworld_url} if org else {}
    pages.update({p.design_id: p.url for p in products.STANDS})
    return {"model_url": SETTINGS.makerworld_url, "design_id": org, "pages": pages,
            "shopdeck": bool(SETTINGS.labels.enabled),  # 0.33: shipping labels on ShopDeck (helper 2.5)
            "helper_version": _helper_version()}  # the helper reloads itself when its files were updated


def _helper_version() -> str:
    try:
        return json.loads((updater.ROOT / "helper" / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return ""


@app.get("/api/helper/next")
def helper_next(status: str = "", on_page: int = 1, design: str = ""):
    """`design` (helper 2.4+): the designId of the page the working tab is on — it gets that page's work, or
    `go` = the page to open for the next queued download. Without it (older helpers): organizer work only."""
    DOWNLOADER.seen(status, bool(on_page))
    if not on_page:  # never hand out work to a tab that isn't on a model page
        return {"job": None, "paused": DOWNLOADER.paused}
    d = design.strip() or None
    job = B.helper_next(d)
    go = None if job else B.helper_go(d)
    return {"job": job, "paused": DOWNLOADER.paused, **({"go": go} if go else {})}


@app.post("/api/helper/ping")
async def helper_ping(req: Request):
    body = await req.json()
    if not body.get("probe"):  # the popup's "is the app running?" check doesn't count as the helper working
        on_page = body.get("on_page")
        DOWNLOADER.seen(str(body.get("status", "")), None if on_page is None else bool(on_page))
    return {"paused": DOWNLOADER.paused}


# ---------------- shipping labels (0.33, labels.py; the helper's ShopDeck page: helper/shopdeck.js) ----------------
def _label_call(fn, *a):
    try:
        return fn(*a)
    except labels.LabelError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/helper/shopdeck/next")
def shopdeck_next():
    if not SETTINGS.labels.enabled:
        return {"job": None, "off": True}
    return labels.helper_next(SETTINGS)


@app.post("/api/helper/shopdeck/{job_id}/seen")
async def shopdeck_seen(job_id: int, req: Request):
    return _label_call(labels.helper_seen, job_id, dict(await req.json()))


@app.post("/api/helper/shopdeck/{job_id}/step")
async def shopdeck_step(job_id: int, req: Request):
    b = await req.json()
    return _label_call(labels.helper_step, job_id, str(b.get("step") or ""), dict(b))


@app.post("/api/helper/shopdeck/{job_id}/fail")
async def shopdeck_fail(job_id: int, req: Request):
    b = await req.json()
    return _label_call(labels.helper_fail, job_id, str(b.get("error") or "")[:400])


@app.post("/api/helper/shopdeck/{job_id}/alive")
async def shopdeck_alive(job_id: int, req: Request):
    """0.33.2: the ShopDeck tab's heartbeat while it works on a job (+ its visibility, a note to keep)."""
    return _label_call(labels.helper_alive, job_id, dict(await req.json()))


@app.post("/api/helper/shopdeck/login")
async def shopdeck_login(req: Request):
    b = await req.json()
    return labels.helper_login(int(b["job_id"]) if b.get("job_id") else None)


@app.get("/api/labels")
def labels_state(ids: str = ""):
    """Print → Completed: label jobs, and for these orders whether their parcel can get a label."""
    out = labels.state([i for i in ids.split(",") if i][:300])
    return {**out, "enabled": bool(SETTINGS.labels.enabled), "one_at_a_time": bool(SETTINGS.labels.one_at_a_time),
            "printer": SETTINGS.labels.printer, "tool": bool(platform_os.pdf_printer_tool(SETTINGS.labels.sumatra_path))}


@app.post("/api/labels")
async def labels_request(req: Request):
    """Print label ({order_ids, items_ready, staff/token}): one parcel per order (its clubbed order)."""
    b = await req.json()
    try:
        who = staff_pins.checker(str(b.get("token") or ""), str(b.get("staff") or ""))
    except ValueError:
        who = str(b.get("staff") or "")
    jobs = _label_call(labels.request, [str(x) for x in b.get("order_ids") or []][:50], who,
                       bool(b.get("items_ready")), SETTINGS)
    with db.tx() as con:
        db.count_usage(con, "label_print")
    return {"jobs": jobs}


@app.post("/api/labels/{job_id}/cancel")
def labels_cancel(job_id: int):
    return _label_call(labels.cancel, job_id)


@app.post("/api/labels/{job_id}/continue")
def labels_continue(job_id: int):
    """A person signed in to ShopDeck again: the helper takes over."""
    return _label_call(labels.signed_in, job_id)


@app.get("/api/labels/{job_id}/trace")
def labels_trace(job_id: int):
    """0.33.2: what the label job saw and did, step by step."""
    return {"events": labels.trace(job_id)}


@app.post("/api/labels/{job_id}/reprint")
async def labels_reprint(job_id: int):
    return await run_in_threadpool(_label_call, labels.reprint, job_id)


@app.get("/api/labels/printers")
def labels_printers():
    return {"printers": platform_os.printers(), "tool": platform_os.pdf_printer_tool(SETTINGS.labels.sumatra_path)}


@app.post("/api/helper/jobs/{job_id}/result")
async def helper_result(job_id: int, plate_1: UploadFile | None = File(None), plate_2: UploadFile | None = File(None),
                        model: UploadFile | None = File(None), check: str = Form("")):
    """An organizer: plate_1 + plate_2, `check` (JSON from helper 2.2.0+): did the License password box hold the
    password at Generate? A name stand (helper 2.4): `model` = MakerWorld's one OBJ with both colours."""
    DOWNLOADER.seen("Uploading")
    if model is not None:
        m = await model.read(MAX_UPLOAD + 1)
        if len(m) > MAX_UPLOAD:
            raise HTTPException(413, "Model too large")
        try:
            return B.store_stand_result(job_id, m.decode("utf-8", errors="replace"))
        except KeyError as e:
            raise HTTPException(404, "No such job") from e
        except ValueError as e:
            B.store_failure(job_id, str(e))
            raise HTTPException(400, str(e)) from e
    if plate_1 is None or plate_2 is None:
        raise HTTPException(400, "Send plate_1 and plate_2 (organizer) or model (name stand)")
    try:
        chk = json.loads(check) if check else None
    except json.JSONDecodeError:
        chk = None
    a = await plate_1.read(MAX_UPLOAD + 1)
    b = await plate_2.read(MAX_UPLOAD + 1)
    if len(a) > MAX_UPLOAD or len(b) > MAX_UPLOAD:
        raise HTTPException(413, "Model too large")
    try:
        return B.store_result(job_id, a, b, chk)
    except KeyError as e:
        raise HTTPException(404, "No such job") from e
    except ValueError as e:
        B.store_failure(job_id, str(e))
        raise HTTPException(400, str(e)) from e


@app.post("/api/helper/jobs/{job_id}/fail")
async def helper_fail(job_id: int, req: Request):
    try:
        B.store_failure(job_id, str((await req.json()).get("error", "unknown error")))
    except KeyError as e:
        raise HTTPException(404, "No such job") from e
    DOWNLOADER.current = None
    return {"ok": True}


@app.post("/api/helper/jobs/{job_id}/release")
def helper_release(job_id: int):
    B.helper_release(job_id)
    return {"ok": True}


@app.get("/api/helper.zip")
def helper_zip():
    if not HELPER_DIR.is_dir():
        raise HTTPException(404, "Helper files are missing")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(HELPER_DIR.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, "snapstl-helper/" + f.relative_to(HELPER_DIR).as_posix())
    return StreamingResponse(iter([buf.getvalue()]), media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename=snapstl-helper.zip"})


# ---------------- database maintenance ----------------
@app.get("/api/maintenance")
def maintenance_info():
    return M.stats()


@app.get("/api/maintenance/disk")
def maintenance_disk():
    """Settings → Maintenance: space used by batch folders, models, backups; what the next clean-up moves."""
    return archive.disk()


@app.post("/api/maintenance/clean-files")
async def maintenance_clean_files():
    """Clean up files now: archived batches (10+ days) and unused models (15+ days) → Recycle Bin."""
    return await run_in_threadpool(archive.clean_up)


@app.get("/api/activity")
def activity(days: int = 7, kind: str = ""):
    """Activities tab: what happened on this computer, newest first."""
    since = time.time() - max(1, min(days, 365)) * 86400
    with db.tx() as con:
        rows = db.rows(con, "SELECT at, kind, text FROM activity WHERE at >= ?" + (" AND kind=?" if kind else "")
                       + " ORDER BY at DESC LIMIT 1000", *((since, kind) if kind else (since,)))
        prints = db.rows(con, "SELECT j.updated_at AS at, j.plate, j.state, j.printer_name, j.error, b.summary FROM print_jobs j "
                              "LEFT JOIN batches b ON b.id=j.batch_id WHERE j.updated_at >= ? AND j.state IN "
                              "('done','failed','cancelled') AND j.printer_id <> 0 ORDER BY j.updated_at DESC LIMIT 500",
                         since) if kind in ("", "print") else []
        builds = db.rows(con, "SELECT finished_at AS at, status, summary FROM batches WHERE finished_at >= ? "
                              "ORDER BY finished_at DESC", since) if kind in ("", "build") else []
        dls = db.rows(con, "SELECT updated_at AS at, name, state, error FROM downloads WHERE updated_at >= ? AND state IN "
                           "('done','failed') ORDER BY updated_at DESC LIMIT 500", since) if kind in ("", "download") else []
    word = {"done": "printed", "failed": "failed", "cancelled": "cancelled"}
    for j in prints:
        label = json.loads(j.pop("summary") or "{}").get("label") or ""
        rows.append({"at": j["at"], "kind": "print", "text": f"{j['plate'].rsplit('/', 1)[-1]} {word[j['state']]} on "
                     f"{j['printer_name'] or '?'}" + (f" ({label})" if label else "")
                     + (f" — {j['error']}" if j["state"] == "failed" and j["error"] else "")})
    for b in builds:
        summ = json.loads(b.pop("summary") or "{}")
        rows.append({"at": b["at"], "kind": "build", "text": f"{summ.get('label') or 'Batch'} built: "
                     f"{len(summ.get('plates') or [])} plates" + (f", {len(summ.get('errors') or [])} problem(s)"
                                                                  if summ.get("errors") else "")})
    for d in dls:
        rows.append({"at": d["at"], "kind": "download", "text": f"{d['name']} " + (
            "downloaded" if d["state"] == "done" else f"download failed — {d['error'] or ''}")})
    rows.sort(key=lambda r: -(r["at"] or 0))
    return {"items": rows[:1500]}


@app.post("/api/maintenance/backup")
def maintenance_backup():
    return {"backup": str(M.backup())}


@app.post("/api/maintenance/cleanup")
async def maintenance_cleanup(req: Request):
    days = int((await req.json()).get("older_than_days", 30))
    if days < 0:
        raise HTTPException(400, "Days must be 0 or more")
    try:
        return M.cleanup(days)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/shared-records")
def shared_records_info(check: int = 0):
    """Settings → Connections → Shared order records (this computer only): connected where, this
    computer's name, sync status; ?check=1 also reads what the shared records hold now."""
    out = {"configured": sync.enabled(), "where": config.describe(), "computer_name": computer.NAME,
           "status": sync.STATUS.snapshot()}
    if check and sync.enabled():
        try:
            out["holds"] = sync.summary()
        except sync.SyncError as e:
            out["holds_error"] = str(e)
    return out


@app.post("/api/shared-records/clear")
async def shared_records_clear(req: Request):
    """Empty the shared order records of every computer ({confirm: "CLEAR"}); saved to a file first."""
    if not sync.enabled():
        raise HTTPException(400, "No shared order records connected")
    b = await req.json()
    try:
        r = await run_in_threadpool(sync.clear, str(b.get("confirm") or ""))
    except sync.SyncError as e:
        raise HTTPException(400, str(e)) from e
    await run_in_threadpool(sync.sync_now)
    return r | {"status": sync.STATUS.snapshot()}


@app.post("/api/shared-records")
async def shared_records_save(req: Request):
    """Connect (test first, then save), rename this computer, or disconnect."""
    body = await req.json()
    if "computer_name" in body:
        computer.set_name(str(body.get("computer_name") or ""))
    if body.get("disconnect"):
        config.set_database_url("")
        return shared_records_info()
    url = config.encode_password(str(body.get("url") or ""))  # passwords with symbols are fine
    if url:
        problem = config.check_url(url)
        if problem:
            raise HTTPException(400, problem)
        try:
            await run_in_threadpool(sync.check, url)  # reach it and create the tables before saving
        except sync.SyncError as e:
            raise HTTPException(400, f"{e}. Check the password and that you copied the Session pooler address.") from e
        config.set_database_url(url)
        sync.LOOP.start()
        await run_in_threadpool(sync.sync_now)  # send this computer's batched/bundled/printed orders now
        if sync.STATUS.error:  # connected, but the first sync failed: say so instead of "connected"
            raise HTTPException(400, f"Connected and saved, but the first sync failed: {sync.STATUS.error}")
    return shared_records_info(check=1)


@app.get("/api/setup")
def setup_status():
    """Settings → Setup checklist: what's done on this computer."""
    d = DOWNLOADER.snapshot()
    u = updater.check(fetch=False)
    return {
        "helper": {"connected": d["online"], "on_page": d["on_page"]},
        "model": {"link": bool(SETTINGS.design_id), "license": bool(SETTINGS.license_password)},
        "folders": {"output": SETTINGS.output_dir, "downloads": SETTINGS.downloads_dir},
        "shared_records": {"configured": sync.enabled(), "ok": bool(sync.STATUS.last_ok) and not sync.STATUS.error,
                           "error": sync.STATUS.error},
        "updates": {"git": u.get("git", False) and not u.get("error"), "error": u.get("error", "")},
        "helper_folder": str(config.ROOT / "helper"),
        "farm": _farm_setup(),
        "slicing": {k: v for k, v in health().items() if k in ("slicer", "presets_missing", "printers")},
    }


def _farm_setup() -> dict:
    cfg = farm.load_config()
    st = farm.status(cfg)
    n = 0
    if st["connected"]:
        try:
            n = len(farm.printers(cfg, with_status=False))
        except farm.FarmError as e:
            st["error"] = str(e)
    return {"connected": st["connected"], "error": st["error"], "printers": n}


@app.post("/api/setup/helper-folder")
def setup_helper_folder():
    """Open the helper folder (for Chrome → Load unpacked)."""
    platform_os.open_folder(config.ROOT / "helper")
    return {"opened": str(config.ROOT / "helper")}


@app.get("/api/sync")
def sync_status():
    return sync.STATUS.snapshot()


@app.post("/api/sync")
def sync_run():
    """Settings → Database → Sync now: push this computer's changes, load everyone's."""
    if not sync.enabled():
        raise HTTPException(400, "No shared order records set up — connect them in Settings → Connections → Shared order records")
    return sync.sync_now()


@app.post("/api/maintenance/match")
def maintenance_match():
    """Settings → Database → Clean up: this computer's orders match the shared records (no confirmation)."""
    if not sync.enabled():
        raise HTTPException(400, "No shared order records set up — connect them in Settings → Connections")
    try:
        return sync.match()
    except (sync.SyncError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/maintenance/reset")
async def maintenance_reset(req: Request):
    body = await req.json()
    shared = bool(body.get("shared"))  # also empty the shared records in Supabase (every computer)
    if body.get("confirm") != ("RESET ALL" if shared else "RESET"):
        raise HTTPException(400, "Type RESET ALL to confirm" if shared else "Type RESET to confirm")
    try:
        return await run_in_threadpool(M.reset, shared)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/downloads")
def downloads():
    """Download queue for the UI: active + failed first, then the 30 most recent finished."""
    with db.tx() as con:
        active = db.rows(con, "SELECT id,name,compartments,state,attempts,error,updated_at,batch_id,watermark,license_check FROM downloads "
                              "WHERE state IN ('claimed','queued','failed') AND computer_id=? "
                              "ORDER BY CASE state WHEN 'claimed' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id LIMIT 500",
                     computer.ID)
        recent = db.rows(con, "SELECT id,name,compartments,state,attempts,error,updated_at,batch_id,watermark,license_check FROM downloads "
                              "WHERE state='done' AND computer_id=? ORDER BY updated_at DESC LIMIT 30", computer.ID)
    return {"items": active + recent, "downloader": DOWNLOADER.snapshot(), "folder": cache.stats()}


@app.post("/api/downloads/reveal")
def downloads_reveal(request: Request):
    """SnapSTL computer only: open the Downloads folder (saved models) in Finder / File Explorer."""
    if not is_local(request):
        raise HTTPException(404)
    cache.ROOT.mkdir(parents=True, exist_ok=True)
    platform_os.open_folder(cache.ROOT)
    return {"opened": str(cache.ROOT)}


@app.post("/api/downloader/{action}")
def downloader_action(action: str):
    if action == "pause":
        DOWNLOADER.paused = True
    elif action == "resume":
        DOWNLOADER.paused = False
    else:
        raise HTTPException(404)
    return DOWNLOADER.snapshot()


# ---------------- batches ----------------
@app.get("/api/batches")
def batches():
    """Batches page: every batch with its names, progress (printed / checked / packed / shipped), status in
    words and Archive (archive.py)."""
    return archive.batch_rows()


def _batch(batch_id: int) -> dict:
    with db.tx() as con:
        r = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "No such batch")
    return dict(r)


def _own_batch(batch_id: int) -> dict:
    """A batch whose files are on this computer (its plates and pictures live where it was built)."""
    b = _batch(batch_id)
    if (b.get("computer_id") or computer.ID) != computer.ID:
        raise HTTPException(409, f"This batch's files are on {b.get('computer_name') or 'another computer'}")
    return b


@app.get("/api/batches/{batch_id}")
def batch(batch_id: int):
    r = _batch(batch_id)
    return {"id": r["id"], "created_at": r["created_at"], "status": r["status"],
            "computer": r.get("computer_name") or "", "mine": (r.get("computer_id") or computer.ID) == computer.ID,
            "order_ids": json.loads(r["order_ids"] or "[]"), "summary": json.loads(r["summary"] or "{}"),
            "progress": B.batch_progress(r) if r["status"] in B.OPEN else None}


@app.get("/api/batches/{batch_id}/models")
def batch_models(batch_id: int):
    """The batch's objects (one per distinct name/compartments) with download + watermark state."""
    try:
        return {"items": B.batch_models(batch_id)}
    except KeyError as e:
        raise HTTPException(404, "No such batch") from e


@app.post("/api/batches/{batch_id}/models/redownload")
async def batch_model_redownload(batch_id: int, req: Request):
    body = await req.json()
    try:
        return B.redownload_model(batch_id, str(body.get("key", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


KEY_RE = re.compile(r"^[0-9a-f]{24}$")


@app.get("/api/models/{key}/view.png")
def model_view(key: str, side: str = "back"):
    """Picture of a downloaded base (side=back shows where MakerWorld puts its watermark; top = from above).
    Watermark faces are painted red. Cached in the app's previews folder."""
    from . import render, watermark

    if not KEY_RE.match(key) or side not in ("back", "top"):
        raise HTTPException(404)
    d = cache.find(key)
    if not d or not (d / "base.stl").exists():
        raise HTTPException(404, "Not downloaded")
    stamp = int((d / "base.stl").stat().st_mtime)
    out = B.PREVIEW_DIR / "models" / f"{key}-{side}-{stamp}.png"
    if not out.exists():
        got = cache.load(key)
        if not got:
            raise HTTPException(404, "Can't read the model")
        base = got[0]
        out.parent.mkdir(parents=True, exist_ok=True)
        for old in out.parent.glob(f"{key}-{side}-*.png"):
            old.unlink(missing_ok=True)
        out.write_bytes(render.render([(base, watermark.detect(base).faces)], view=side, px=0.4,
                                      max_samples=1_200_000))
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "max-age=60"})


@app.get("/api/batches/{batch_id}/plate-image")
def batch_plate_image(batch_id: int, f: str):
    """Top-view picture of a base plate, drawn when the plates were built."""
    b = _own_batch(batch_id)
    label = Path(b["out_dir"] or "").name
    root = (B.PREVIEW_DIR / f"batch-{batch_id}" / label).resolve()
    target = (root / f).resolve()
    if not label or root not in target.parents or not target.is_file() or target.suffix != ".png":
        raise HTTPException(404, "No picture")
    return FileResponse(target, media_type="image/png")


@app.get("/api/batches/{batch_id}/file")
def batch_file(batch_id: int, path: str):
    root = Path(_own_batch(batch_id)["out_dir"] or "/nonexistent").resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "No such file")
    return FileResponse(target, filename=target.name)


@app.post("/api/batches/{batch_id}/reveal")
async def batch_reveal(batch_id: int, req: Request):
    """SnapSTL computer only: show the batch folder (or one plate file) in Finder / File Explorer."""
    if not is_local(req):
        raise HTTPException(404)
    body = await req.json()
    root = Path(_own_batch(batch_id)["out_dir"] or "/nonexistent").resolve()
    if not root.is_dir():
        raise HTTPException(404, "Batch folder is gone")
    target = root
    if body.get("file"):
        target = (root / str(body["file"])).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "No such file")
    platform_os.reveal(target)
    return {"ok": True}


@app.get("/api/batches/{batch_id}/zip")
def batch_zip(batch_id: int):
    """On-demand download only: the ZIP is built in a temp file and deleted after sending (never saved)."""
    b = _own_batch(batch_id)
    root = Path(b["out_dir"] or "/nonexistent")
    if not root.is_dir():
        raise HTTPException(404, "Batch folder is gone")
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    tmp.close()
    return FileResponse(tmp.name, filename=f"snapstl-batch-{batch_id}.zip",
                        background=BackgroundTask(os.unlink, tmp.name))


# ---------------- print farm (Bambuddy) ----------------
def _farm_err(e: farm.FarmError) -> HTTPException:
    return HTTPException(400, str(e))


def _job_view(j: dict | None) -> dict | None:
    if not j:
        return None
    v = {k: j.get(k) for k in ("id", "plate", "kind", "color", "printer_id", "printer_name", "state", "sliced",
                               "progress", "error", "created_at", "updated_at", "batch_id")}
    v["held"] = bool(j.get("held"))
    try:
        v["options"] = json.loads(j.get("options") or "{}")
    except ValueError:
        v["options"] = {}
    return v


@app.get("/api/farm")
def farm_info(request: Request):
    """Printers page (read only): Bambuddy connection, printers with live state and filament loaded,
    recent print jobs; slicing settings (shown in Settings)."""
    cfg = farm.load_config()
    st = farm.status(cfg)
    if st["reachable"] and not st["connected"] and is_local(request):
        try:  # fresh install: connect by itself (Bambuddy's login is off)
            st = farm.connect()
            cfg = farm.load_config()
        except farm.FarmError as e:
            st["error"] = str(e)
    prs: list[dict] = []
    if st["connected"]:
        try:
            prs = farm.printers(cfg)
        except farm.FarmError as e:
            st["error"] = str(e)
    jobs, seen = [], set()
    for j in farm.jobs(limit=60):  # newest first; only a plate's newest job can be printed / moved
        v = _job_view(j)
        key = (j["batch_id"], j["plate"])
        v["latest"] = key not in seen
        seen.add(key)
        if v["latest"] and prs and j["state"] not in ("slicing", "uploading", "printing"):
            v["choices"] = farm.job_choices(j, prs)
        jobs.append(v)
    return {"status": st, "config": farm.public_config(cfg), "printers": prs, "jobs": jobs,
            "options": {k: list(v) for k, v in farm.OPTIONS.items()}, "worker": {"current": farm.WORKER.current,
                                                                          "log": farm.WORKER.log[-20:]},
            "local": is_local(request)}


# ---------------- Make It (Bases / Names / Completed) ----------------
_BOARD_LOCK = threading.Lock()
_BOARD: tuple[float, dict] | None = None


@app.get("/api/makeit")
def makeit_board():
    """Make It tab: plates to print by Bases and Names (with pre-selected printers) and Completed.
    0.28.1: one at a time — requests that arrive while it is being worked out get that same answer, so pages
    asking together (several tabs, the 5 s refresh) never pile up and keep the computer busy."""
    global _BOARD
    asked = time.time()
    with _BOARD_LOCK:
        if _BOARD is not None and _BOARD[0] >= asked:
            return _BOARD[1]
        started = time.time()
        out = _makeit_board()
        _BOARD = (started, out)
        return out


def _makeit_board() -> dict:
    cfg = farm.load_config()
    st = farm.status(cfg)
    prs: list[dict] = []
    if st["connected"]:
        try:
            prs = farm.printers(cfg)
        except farm.FarmError as e:
            st["error"] = str(e)
    for p in prs:  # Print: a plate's options start from its printer's print defaults (Slicing)
        p["defaults"] = farm.print_defaults(p["id"], cfg)
        p["defaults_letters"] = farm.print_defaults(p["id"], cfg, "names")
    return {"status": st, "printers": prs, **makeit.board(prs),
            "options": {k: list(v) for k, v in farm.OPTIONS.items()},
            "speeds": list(farm.SPEEDS_SET),
            "worker": {"log": farm.WORKER.log[-20:]}}


@app.post("/api/makeit/start")
async def makeit_start(req: Request):
    """Start print — a person's click: {batch_id, file, printer_id, tray, options, force} (tray = filament slot,
    a global tray id from the plate's printer choices; missing = SnapSTL's best colour fit)."""
    b = await req.json()
    if not b.get("printer_id"):
        raise HTTPException(400, "Choose a printer")
    _own_batch(int(b.get("batch_id") or 0))
    try:
        prs = await run_in_threadpool(farm.printers)
        return await run_in_threadpool(makeit.start_print, int(b["batch_id"]), str(b.get("file", "")),
                                       int(b["printer_id"]), b.get("options") or {}, prs, bool(b.get("force")),
                                       str(b.get("speed") or ""), b.get("tray"), bool(b.get("bed_clear")),
                                       b.get("tray2"))  # tray2: a name stand's letters slot (0.32)
    except makeit.BedNotClear as e:
        raise HTTPException(428, str(e)) from e
    except farm.StartBlocked as e:
        raise HTTPException(409, str(e)) from e
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.get("/api/queues")
def queues():
    """Print page's queue panel: downloading, building, slicing and printing (this computer)."""
    return makeit.queues()


@app.get("/api/makeit/presets")
def makeit_presets(printer: int = 0, refresh: int = 0):
    """Slicing: every profile stored in Bambuddy, the ones picked per printer, the farm's printers (with
    their print defaults) and automatic slicing. `refresh=1` makes Bambuddy re-read its cloud lists."""
    cfg = farm.load_config()
    try:
        prs = farm.printers(with_status=False)
    except farm.FarmError:
        prs = []
    ids = [p["id"] for p in prs]
    if printer not in ids:
        printer = ids[0] if ids else 0
    set_for = {str(p["id"]): bool(all(farm.chosen_presets(p["id"], cfg).get(sl) for sl in farm.PRESET_SLOTS))
               for p in prs}
    base = {"printer": printer, "set_for": set_for,
            "printers": [{"id": p["id"], "name": p["name"], "model": p.get("model") or "",
                          "defaults": farm.print_defaults(p["id"], cfg)} for p in prs],
            "bambuddy_url": cfg.get("bambuddy_url") or "",
            "speeds": list(farm.SPEEDS_SET), "options": {k: list(v) for k, v in farm.OPTIONS.items()},
            "plates": list(farm.PLATE_TYPES), "activity": makeit.slicing_summary()}
    chosen = farm.chosen_presets(printer, cfg)
    lp = farm.letters_process(printer, cfg)
    base |= {"chosen": {k: f"{v['source']}:{v['id']}" for k, v in chosen.items()},
             "names": {k: v["name"] for k, v in chosen.items()},
             "letters": {"process": f"{lp['source']}:{lp['id']}" if lp else "", "name": (lp or {}).get("name", ""),
                         "speed": (cfg.get("letters_speed_by_printer") or {}).get(str(printer), "")},
             "letters_printers": sorted(farm.letters_printers(cfg))}
    try:  # local (yours in Bambuddy) + standard profiles only — no cloud (ARCHITECTURE #62)
        avail, notice = farm.preset_listing(cfg, refresh=bool(refresh))
    except farm.FarmError as e:
        return base | {"available": {sl: [] for sl in farm.PRESET_SLOTS}, "error": str(e), "status": {}, "notice": ""}
    return base | {"available": avail, "error": "", "status": {}, "cloud_problem": "", "notice": notice}


@app.put("/api/makeit/presets")
async def makeit_presets_save(req: Request):
    """{printer_id, printer, process, filament} ("source:id" from Bambuddy's list) or {printer_id, clear: true}
    — that printer slices with those Bambuddy profiles."""
    b = await req.json()
    try:
        pid = int(b.get("printer_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    clear = bool(b.get("clear"))
    if not pid:
        raise HTTPException(400, "Choose the printer these profiles are for")
    _health_stale()
    try:
        chosen = await run_in_threadpool(farm.set_presets, b, pid, clear)
    except farm.FarmError as e:
        raise _farm_err(e) from e
    return {"names": {k: v["name"] for k, v in chosen.items()}}


@app.put("/api/printers/external")
async def printers_external(req: Request):
    """Printers → External spool (0.28): {printer_id, on}. A printer with an AMS offers its external spool for
    printing only when on."""
    b = await req.json()
    if not b.get("printer_id"):
        raise HTTPException(400, "Choose a printer")
    return await run_in_threadpool(farm.set_external, int(b["printer_id"]), bool(b.get("on")))


@app.put("/api/makeit/letters")
async def makeit_letters_save(req: Request):
    """Slicing → Letters: {printer_id, process ("source:id" or "" = none), speed ("" = the printer's)}. Letter plates
    slice with that process profile and print at that speed; with any set, letters print only on those printers."""
    b = await req.json()
    if not b.get("printer_id"):
        raise HTTPException(400, "Choose a printer")
    try:
        return await run_in_threadpool(farm.set_letters, int(b["printer_id"]), str(b.get("process") or ""),
                                       str(b.get("speed") or ""))
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.put("/api/makeit/print-defaults")
async def makeit_print_defaults(req: Request):
    """Slicing → Print defaults: {printer_id, speed, bed_levelling, flow_cali, timelapse, plate_type} ("" =
    Bambuddy's; plate_type "" = the process profile's plate)."""
    b = await req.json()
    if not b.get("printer_id"):
        raise HTTPException(400, "Choose a printer")
    try:
        return farm.set_print_defaults(int(b["printer_id"]), b)
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.post("/api/makeit/slice")
async def makeit_slice(req: Request):
    """Slice plates: {plates: [{batch_id, file, printer_id}], again} — each with the Bambuddy profiles of the
    printer chosen for it in Print (required: there is no default printer). Runs in the background, one
    at a time. Plates are sliced only from here (Slice / Slice all) or at Start print — never by themselves."""
    b = await req.json()
    todo, pids = [], []
    for p in [p for p in b.get("plates", []) if isinstance(p, dict)][:500]:
        bid, file = int(p.get("batch_id") or 0), str(p.get("file", ""))
        _own_batch(bid)
        try:
            _, plate = makeit._plate(bid, file)
        except farm.FarmError as e:
            raise _farm_err(e) from e
        if not p.get("printer_id"):
            raise HTTPException(400, "Choose the printer to slice for — a plate is sliced with its printer's profiles")
        todo.append((bid, file, plate["kind"], farm.plate_color(plate)))  # a name stand: "block+letters"
        pids.append(int(p["printer_id"]))
    if not todo:
        raise HTTPException(400, "Choose the plates to slice")
    n = 0
    for t, pid in zip(todo, pids):
        n += await run_in_threadpool(farm.request_slices, [t], [pid], bool(b.get("again")))
    return {"queued": n}


@app.post("/api/makeit/remove")
async def makeit_remove(req: Request):
    """Take a plate off Bases / Names (e.g. printed by hand); Put back returns it."""
    b = await req.json()
    _own_batch(int(b.get("batch_id") or 0))
    try:
        return makeit.remove(int(b["batch_id"]), str(b.get("file", "")), bool(b.get("put_back")))
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.post("/api/makeit/by-hand")
async def makeit_by_hand(req: Request):
    """Removed / waiting plate printed by hand → Verify."""
    b = await req.json()
    _own_batch(int(b.get("batch_id") or 0))
    try:
        return makeit.printed_by_hand(int(b["batch_id"]), str(b.get("file", "")))
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.post("/api/makeit/merge")
async def makeit_merge(req: Request):
    """Names → Merge: same-colour plates not started (≤ 3 names) onto one plate."""
    b = await req.json()
    try:
        try:  # who merged (the activity log); merging doesn't need a PIN
            who = staff_pins.checker(str(b.get("token") or ""), str(b.get("staff") or ""))
        except ValueError:
            who = ""
        return await run_in_threadpool(extras.merge, list(b.get("plates") or []), who)
    except extras.ExtraError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/makeit/split")
async def makeit_split(req: Request):
    b = await req.json()
    try:
        return extras.split(int(b.get("batch_id") or 0), str(b.get("file", "")))
    except extras.ExtraError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/makeit/divide")
async def makeit_divide(req: Request):
    """Split a plate that isn't started into smaller plates ({batch_id, file, sizes: [2, 1]})."""
    b = await req.json()
    try:
        return await run_in_threadpool(extras.divide, int(b.get("batch_id") or 0), str(b.get("file", "")),
                                       list(b.get("sizes") or []))
    except (extras.ExtraError, ValueError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/makeit/join")
async def makeit_join(req: Request):
    b = await req.json()
    try:
        return extras.join(int(b.get("batch_id") or 0), str(b.get("file", "")))
    except extras.ExtraError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/verify")
async def verify_submit(req: Request):
    """Print → Verify (and Completed → Found a problem): {carrier: {batch_id, file}, staff, results, note}."""
    b = await req.json()
    try:
        out = await run_in_threadpool(verify.submit, b.get("carrier") or {}, _checker(b),
                                      list(b.get("results") or []), str(b.get("note") or ""))
    except (ValueError, extras.ExtraError) as e:
        raise HTTPException(409, str(e)) from e
    with db.tx() as con:
        db.count_usage(con, "verify_problem" if any(r.get("result") != "good" for r in b.get("results") or [])
                       else "verify_good")
    sync.LOOP.kick()
    return out


@app.get("/api/verify/letters")
def verify_letters(batch_id: int, plate: str, idx: int):
    try:
        return {"pieces": verify.letter_pieces(batch_id, plate, idx)}
    except (ValueError, extras.ExtraError) as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/printers/now")
def printers_now_light():
    """Every page, every 30 s: each printer's state, for the "printer finished / failed" alert."""
    cfg = farm.load_config()
    if not farm.status(cfg)["connected"]:
        return {"printers": []}
    try:
        prs = farm.printers(cfg)
    except farm.FarmError:
        return {"printers": []}
    return {"printers": [{"id": p["id"], "name": p["name"], "state": (p.get("status") or {}).get("state", ""),
                          "connected": (p.get("status") or {}).get("connected", False),
                          "clear": (p.get("status") or {}).get("awaiting_plate_clear", False)} for p in prs]}


@app.get("/api/stats")
def stats_get(days: int = 30):
    """Stats page: made per day against new orders, backlog, printers, checkers, waiting times."""
    from . import stats

    return stats.stats(days, SETTINGS.daily_target)


@app.post("/api/stats/target")
async def stats_target(req: Request):
    """The daily target (orders made per day) shown on Stats."""
    try:
        n = int((await req.json()).get("target") or 0)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Type a number") from e
    if not 0 <= n <= 10000:
        raise HTTPException(400, "Type a number from 0 to 10000")
    SETTINGS.daily_target = n
    save_settings(SETTINGS)
    return {"target": n}


@app.get("/api/staff")
def staff_get(all: int = 0):
    """Settings → Staff and Print → Verify: the people, who has a PIN, whether PINs are in use."""
    with db.tx() as con:
        rows = db.rows(con, "SELECT name, hidden, COALESCE(pin_hash,'')<>'' AS pin FROM staff ORDER BY name COLLATE NOCASE")
        on = staff_pins.pins_on(con)
    return {"staff": [{"name": r["name"], "hidden": bool(r["hidden"]), "pin_set": bool(r["pin"])} for r in rows
                      if all or not r["hidden"]],
            "pins_on": on, "owner_pin_set": staff_pins.owner_set()}


@app.post("/api/staff")
async def staff_post(req: Request):
    """Settings → Staff (owner PIN once set): add a person ({name}), hide / show one ({name, hidden}), set or
    clear their PIN ({name, pin} / {name, clear_pin}). Every change: {owner_pin}."""
    b = await req.json()
    try:
        staff_pins.require_owner(str(b.get("owner_pin") or ""))
        with db.tx() as con:
            name = str(b.get("name") or "")
            if "hidden" in b:
                verify.hide_staff(con, name, bool(b.get("hidden")))
            elif b.get("clear_pin"):
                staff_pins.clear_pin(con, name)
            else:  # add (or show again), with a PIN if one is given; an existing person: just the new PIN
                if "pin" not in b or not con.execute("SELECT 1 FROM staff WHERE name=?", (name,)).fetchone():
                    name = verify.add_staff(con, name)
                if "pin" in b:
                    staff_pins.set_pin(con, name, str(b.get("pin") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    sync.LOOP.kick()
    return {"name": name}


@app.post("/api/staff/owner-pin")
async def staff_owner_pin(req: Request):
    """Set the owner PIN ({pin}) or change it ({pin, old_pin}); it guards Settings → Staff on this computer."""
    b = await req.json()
    try:
        staff_pins.set_owner(str(b.get("pin") or ""), str(b.get("old_pin") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/staff/start")
async def staff_start(req: Request):
    """Print → Verify: {pin} → {name, token}: checks are saved under this person until Done or 30 idle minutes."""
    b = await req.json()
    try:
        return staff_pins.start(str(b.get("pin") or ""))
    except ValueError as e:
        raise HTTPException(403, str(e)) from e


@app.post("/api/verify/plate-failed")
async def verify_plate_failed(req: Request):
    """The whole printed plate failed ({carrier: {batch_id, file}}): its unchecked items go back to print."""
    b = await req.json()
    who = _checker(b)
    try:
        out = await run_in_threadpool(verify.plate_failed, b.get("carrier") or {}, who)
    except (ValueError, extras.ExtraError) as e:
        raise HTTPException(409, str(e)) from e
    sync.LOOP.kick()
    return out


@app.post("/api/staff/end")
async def staff_end(req: Request):
    staff_pins.end(str((await req.json()).get("token") or ""))
    return {"ok": True}


def _checker(b: dict) -> str:
    """Who a check or merge is saved under (the PIN's person once PINs are in use)."""
    try:
        return staff_pins.checker(str(b.get("token") or ""), str(b.get("staff") or ""))
    except ValueError as e:
        raise HTTPException(403, str(e)) from e


@app.post("/api/makeit/reprint")
async def makeit_reprint(req: Request):
    """Completed → back to the queue (a person confirmed the print failed)."""
    b = await req.json()
    _own_batch(int(b.get("batch_id") or 0))
    try:
        return makeit.reprint(int(b["batch_id"]), str(b.get("file", "")))
    except farm.FarmError as e:
        raise _farm_err(e) from e


_health: tuple[float, dict] = (0.0, {})


def _health_stale() -> None:
    global _health
    _health = (0.0, {})


@app.get("/api/health")
def health():
    """The sidebar status bar: helper, Bambuddy, slicer, printers online, models without presets (≤10 s old)."""
    global _health

    if time.time() - _health[0] < 10 and _health[1]:
        return _health[1] | {"helper": DOWNLOADER.snapshot()["online"]}
    cfg = farm.load_config()
    st = farm.status(cfg)
    online = total = 0
    missing: list[str] = []
    if st["connected"]:
        try:
            prs = farm.printers(cfg)
            total = len(prs)
            online = sum(1 for p in prs if (p.get("status") or {}).get("connected"))
            missing = [p["name"] for p in prs if farm.missing_presets(p["id"], cfg)]
        except farm.FarmError:
            pass
    # Bambuddy's slicer runs with Bambuddy (its sidecar); a slicer that's down shows on the plates it fails
    out = {"bambuddy": {"connected": st["connected"], "error": st["error"]},
           "slicer": {"name": "Bambuddy's slicer", "found": st["connected"]},
           "printers": {"online": online, "total": total}, "presets_missing": missing,
           "network": _network(st["url"])}
    _health = (time.time(), out)
    return out | {"helper": DOWNLOADER.snapshot()["online"]}


def _network(bambuddy_url: str) -> dict:
    """The sidebar's "This computer" line: its address on the network, for connecting other computers —
    to this SnapSTL's web portal, and to Bambuddy when Bambuddy runs here."""
    from urllib.parse import urlparse

    ips = platform_os.lan_ips()
    ip = ips[0] if ips else ""
    u = urlparse(bambuddy_url or "")
    here = (u.hostname or "") in ("127.0.0.1", "localhost", "::1", ip) and bool(u.hostname)
    share = f"{u.scheme or 'http'}://{ip}:{u.port or 8000}" if here and ip else (bambuddy_url or "")
    return {"ip": ip, "name": platform_os.lan_name(), "portal": f"http://{ip}:{_port()}" if ip else "",
            "bambuddy_url": bambuddy_url or "", "bambuddy_here": here, "bambuddy_share": share}


@app.get("/api/printer-status")
def printer_status_all(download: int = 0):
    """Printer status tab: everything Bambuddy reports about every printer (read only, no secrets).
    ?download=1 → the same as a JSON file."""
    data = farm.everything()
    st = farm.status()
    data["bambuddy"] = {"url": st["url"], "reachable": st["reachable"], "connected": st["connected"], "error": st["error"]}
    data["log"] = farm.WORKER.log[-40:]
    data["slicing"] = makeit.slicing_summary()
    plates = makeit.active_plates()
    for p in data.get("printers") or []:
        p["plate"] = plates.get(p.get("id"))
    if download:
        name = time.strftime("bambuddy-printers-%Y-%m-%d-%H%M.json")
        return StreamingResponse(iter([json.dumps(data, indent=2, default=str)]), media_type="application/json",
                                 headers={"Content-Disposition": f"attachment; filename={name}"})
    return data


# ---- the Bambuddy computer (Windows): Bambuddy's service + the slicer service in Docker ----
@app.get("/api/bambuddy-host")
def bambuddy_host_status():
    """Settings → Connections → This Bambuddy computer: is each part up (empty on other computers)."""
    return bambuddy_host.HOST.status()


@app.post("/api/bambuddy-host/{action}")
async def bambuddy_host_action(action: str, req: Request):
    """start (what is down; may ask Windows for admin rights) · restart-bambuddy · restart-slicer ·
    restart-octoeverywhere · restart-docker · restart-all (Bambuddy, slicer, OctoEverywhere) ·
    open-octoeverywhere (its Plugin Manager) · keep-running {on}. Only from a person's click; nothing running is stopped otherwise."""
    if not bambuddy_host.is_here():
        raise HTTPException(400, "Bambuddy isn't set up on this computer — run scripts/windows/bambuddy-server.ps1 once")
    if action == "keep-running":
        body = await req.json()
        return {"keep_running": bambuddy_host.set_keep_running(bool(body.get("on")))}
    try:
        fn = {"start": lambda: bambuddy_host.HOST.start(elevate=True),
              "restart-bambuddy": bambuddy_host.HOST.restart_bambuddy,
              "restart-slicer": bambuddy_host.HOST.restart_slicer,
              "restart-octoeverywhere": bambuddy_host.HOST.restart_octoeverywhere,
              "restart-docker": bambuddy_host.HOST.restart_docker,
              "restart-all": bambuddy_host.HOST.restart_all,
              "open-octoeverywhere": bambuddy_host.HOST.open_octoeverywhere}.get(action)
        if fn is None:
            raise HTTPException(404, "Unknown action")
        started = fn()
    except PermissionError as e:
        raise HTTPException(400, str(e)) from e
    if not started:
        raise HTTPException(409, f"{bambuddy_host.HOST.busy} — wait for it to finish")
    _health_stale()
    return {"ok": True}


@app.post("/api/farm/connect")
async def farm_connect(req: Request):
    body = await req.json()
    _health_stale()
    try:
        return await run_in_threadpool(farm.connect, str(body.get("url") or "") or None,
                                       str(body.get("api_key") or "") or None)
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.post("/api/farm/refresh")
def farm_refresh():
    """Printers → Refresh from Bambuddy: every printer reports its status and filament again (e.g. after a
    filament change), and SnapSTL forgets what it had cached."""
    try:
        out = farm.refresh_from_bambuddy()
    except farm.FarmError as e:
        raise _farm_err(e) from e
    _health_stale()
    return out


@app.post("/api/farm/printers/{pid}/clear-plate")
def farm_clear_plate(pid: int):
    """A person checked the bed is empty: the next queued print may start."""
    try:
        farm.clear_plate(pid)
        makeit.confirm_bed_clear(pid)  # Start print won't ask again for this bed
    except farm.FarmError as e:
        raise _farm_err(e) from e
    return {"ok": True}


def _plates_view(b: dict) -> dict:
    summ = json.loads(b.get("summary") or "{}")
    out_dir = Path(b["out_dir"]) if b.get("out_dir") else None
    latest = farm.latest_jobs(b["id"])
    plates = []
    for p in summ.get("plates", []):
        j = latest.get(p["file"])
        plates.append({"file": p["file"], "kind": p["kind"], "names": p.get("names", []), "colors": p.get("colors", []),
                       "sliced": None,
                       "job": _job_view(j), "job_active": bool(j and j["state"] in farm.ACTIVE)})
    cfg = farm.load_config()
    st = farm.status(cfg)
    prs: list[dict] = []
    if st["connected"]:
        try:
            prs = farm.printers(cfg)
        except farm.FarmError as e:
            st["error"] = str(e)
    sugg = farm.suggest(plates, prs, farm.active_counts()) if prs else {}
    for pl in plates:
        pl["suggested"] = sugg.get(pl["file"])
        pl["need"] = farm.needed_color(pl)
        pl["choices"] = [{"id": p["id"], "name": p["name"], "why_not": farm.eligible(pl, p), "note": farm.match_note(pl, p),
                          "state": (p.get("status") or {}).get("state", ""),
                          "connected": (p.get("status") or {}).get("connected", False)} for p in prs]
    done = bool(plates) and all(pl["job"] and pl["job"]["state"] == "done" for pl in plates)
    return {"plates": plates, "farm": {"connected": st["connected"], "error": st["error"],
                                       "printers": len(prs)},
            "all_printed": done, "folder": str(out_dir) if out_dir else ""}


@app.get("/api/batches/{batch_id}/plates")
def batch_plates(batch_id: int):
    """Push to queue: every plate of a built batch, its sliced file, its print job and the printers that fit."""
    b = _own_batch(batch_id)
    if not str(b["status"]).startswith("done"):
        raise HTTPException(400, "Plates can be pushed once the batch is built")
    return _plates_view(b)


@app.post("/api/batches/{batch_id}/push")
async def batch_push(batch_id: int, req: Request):
    """Push to queue: {plates: [{file, printer_id}], again: false}. Each plate goes to that printer's queue
    in Bambuddy (sliced first if needed). Bambuddy waits for a person to clear the bed before each print."""
    b = _own_batch(batch_id)
    if not str(b["status"]).startswith("done"):
        raise HTTPException(400, "Plates can be pushed once the batch is built")
    body = await req.json()
    picks = [p for p in body.get("plates", []) if isinstance(p, dict)][:500]
    if not picks:
        raise HTTPException(400, "Choose a printer for at least one plate")
    try:
        prs = await run_in_threadpool(farm.printers)  # with live filament: the colour check uses it
        return farm.queue_jobs(b, picks, prs, again=bool(body.get("again")))
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.get("/api/print-jobs")
def print_jobs(batch_id: int | None = None):
    return {"jobs": [_job_view(j) for j in farm.jobs(batch_id)]}


@app.post("/api/print-jobs/{job_id}/print")
async def print_job_print(job_id: int, req: Request):
    """Printers page → Print: {printer_id, tray, options: {bed_levelling, flow_cali, timelapse}} (tray = filament
    slot; missing = the best colour fit)."""
    b = await req.json()
    if not b.get("printer_id"):
        raise HTTPException(400, "Choose a printer")
    try:
        prs = await run_in_threadpool(farm.printers)
        return await run_in_threadpool(lambda: farm.print_job(job_id, int(b["printer_id"]), b.get("options") or {}, prs,
                                                              tray=b.get("tray")))
    except farm.FarmError as e:
        raise _farm_err(e) from e


@app.post("/api/print-jobs/{job_id}/start")
async def print_job_start(job_id: int, req: Request):
    """Start: the only way a print begins. {force: true} = start anyway after Bambuddy's warning."""
    b = await req.json() if (await req.body()) else {}
    try:
        await run_in_threadpool(farm.start_job, job_id, bool(b.get("force")))
    except farm.StartBlocked as e:
        raise HTTPException(409, str(e)) from e
    except farm.FarmError as e:
        raise _farm_err(e) from e
    return {"ok": True}


@app.post("/api/print-jobs/{job_id}/{action}")
def print_job_action(job_id: int, action: str):
    try:
        if action == "retry":
            farm.retry_job(job_id)
        elif action == "cancel":
            farm.cancel_job(job_id)
        else:
            raise HTTPException(404)
    except farm.FarmError as e:
        raise _farm_err(e) from e
    return {"ok": True}


# ---------------- settings ----------------
@app.get("/api/settings")
def get_settings(request: Request):
    d = SETTINGS.to_dict()
    if not is_local(request):  # other computers never see this computer's license passwords
        d["license_passwords"] = {}
    return d


@app.put("/api/settings")
async def put_settings(req: Request):
    global SETTINGS
    try:
        new = Settings.from_dict(await req.json())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid settings: {e}") from e
    new.lan_access = SETTINGS.lan_access  # changed only through /api/portal (this computer)
    new.help_access = SETTINGS.help_access  # … and /api/help-portal
    new.daily_target = SETTINGS.daily_target  # Stats → daily target
    new.report_import = SETTINGS.report_import  # /api/report-import
    if is_local(req):  # on the SnapSTL computer: folders can be changed
        out = Path(new.output_dir).expanduser()
        if not out.is_absolute():
            raise HTTPException(400, "Output folder must be a full path (e.g. /Users/you/SnapSTL/output)")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Can't use that output folder: {e}") from e
        new.output_dir = str(out)
        dl = Path(new.downloads_dir).expanduser()
        if not dl.is_absolute():
            raise HTTPException(400, "Downloads folder must be a full path (e.g. /Users/you/SnapSTL/Downloads)")
        try:
            dl.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Can't use that downloads folder: {e}") from e
        new.downloads_dir = str(dl)
    else:
        new.output_dir = SETTINGS.output_dir
        new.downloads_dir = SETTINGS.downloads_dir
        new.license_passwords = SETTINGS.license_passwords  # passwords are managed on the SnapSTL computer
        new.makerworld_url = SETTINGS.makerworld_url
    errs = new.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    SETTINGS = new
    save_settings(SETTINGS)
    MANAGER.output_dir = SETTINGS.output_dir
    cache.set_root(SETTINGS.downloads_dir)
    DOWNLOADER.pause_s = SETTINGS.delay_between_downloads_s
    return SETTINGS.to_dict()


@app.post("/api/settings/reset")
def reset_settings():
    global SETTINGS
    SETTINGS = Settings(output_dir=SETTINGS.output_dir, downloads_dir=SETTINGS.downloads_dir)
    save_settings(SETTINGS)
    return SETTINGS.to_dict()

