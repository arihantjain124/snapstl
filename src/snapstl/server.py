"""SnapSTL web server: portal UI, orders/batches API, helper API, settings, maintenance.

Runs locally on 127.0.0.1:8317 (launchd service `snapstl-serve`). In local mode there is no
login; a cross-site guard blocks other websites, and the Chrome helper may call /api/helper/* only.
"""
from __future__ import annotations

import csv
from contextlib import asynccontextmanager
import io
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

from . import auth, cache, computer, db, platform_os, sync, updater
from .ingest import ingest, reset_to_ready, set_name, set_printed
from . import batches as B
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
    MANAGER.start()  # builds batches once their models arrive
    DOWNLOADER.pause_s = SETTINGS.delay_between_downloads_s
    DOWNLOADER.start()  # re-queues names that were mid-download when the app stopped
    sync.LOOP.start()   # shared order records (Supabase), if .env sets them up
    yield


app = FastAPI(title="SnapSTL", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

OPEN_PATHS = {"/login", "/api/login", "/api/auth-check", "/favicon.ico"}


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
# Things only the SnapSTL computer itself may do (helper, Finder, folders, portal setup, reset).
MAC_ONLY_PREFIXES = ("/api/helper/", "/api/downloads/reveal", "/api/portal", "/api/maintenance/reset",
                     "/api/update")


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
        if not (SETTINGS.lan_access and auth.enabled()):
            return "This SnapSTL only accepts connections from its own computer (Settings → Other computers)"
        if path.startswith(MAC_ONLY_PREFIXES) or path.endswith("/reveal"):
            return "Only available on the SnapSTL computer"
    return None


@app.middleware("http")
async def access_control(request: Request, call_next):
    why = _blocked(request)
    if why:
        return JSONResponse({"detail": why}, status_code=403)
    path = request.url.path
    if is_local(request) or path in OPEN_PATHS:
        return await call_next(request)
    if auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Sign in required"}, status_code=401)
    return RedirectResponse("/login")


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
def _portal_urls() -> list[str]:
    urls = [f"http://{platform_os.lan_name()}:{_port()}"]
    urls += [f"http://{ip}:{_port()}" for ip in platform_os.lan_ips()]
    return urls


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
def update_apply():
    if DOWNLOADER.current is not None:
        raise HTTPException(409, f"A name is being downloaded ({DOWNLOADER.current}) — pause downloads or wait, then update")
    with db.tx() as con:
        busy = con.execute("SELECT COUNT(*) AS n FROM batches WHERE status IN ('build_requested','building') "
                           "AND computer_id=?", (computer.ID,)).fetchone()["n"]
    if busy:
        raise HTTPException(409, "A batch is being built — wait for it to finish, then update")
    try:
        r = updater.apply()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    r["restarting"] = _restart_service_soon() if r["updated"] else False
    if r["updated"] and not r["restarting"]:
        r["note"] = "Updated — restart SnapSTL to use the new version"
    return r


@app.get("/api/portal")
def portal_info():
    return {"lan_access": SETTINGS.lan_access, "password_set": auth.enabled(),
            "listening_on_network": os.environ.get("SNAPSTL_BIND", "127.0.0.1") != "127.0.0.1",
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
    changed = enable != SETTINGS.lan_access
    SETTINGS.lan_access = enable
    save_settings(SETTINGS)
    restarting = _restart_service_soon() if changed else False
    return {"lan_access": enable, "password_set": auth.enabled(), "restarting": restarting,
            "note": "" if (restarting or not changed) else "Restart SnapSTL for this to take effect"}


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
        "shared_records": sync.enabled(),  # Supabase order records set up in .env
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
    sync.LOOP.kick()  # newer webstore statuses go to the shared records
    return s.__dict__


def _order_view(r: dict) -> dict:
    letters = sum(c.isalpha() for c in (r.get("name") or ""))
    band = pick_band(letters, SETTINGS.scaling.bands) if letters else None
    return {
        "order_id": r["order_id"], "clubbed_id": r["clubbed_id"], "order_date": r["order_date"],
        "customer_name": r.get("customer_name") or "", "phone": r.get("phone") or "", "address": r.get("address") or "",
        "source_status": r["source_status"], "quantity": r["quantity"], "raw_name": r["raw_name"],
        "raw_color": r["raw_color"], "special_request": r["special_request"], "name": r["name"],
        "color": r["color"], "name_edited": bool(r["name_edited"]), "review_reason": r["review_reason"],
        "state": r["state"], "batch_id": r["batch_id"], "last_error": r["last_error"], "letters": letters,
        "batch_computer": r.get("batch_computer") or "", "batch_label": r.get("batch_label") or "",
        "batch_here": bool(r.get("batch_id")), "printed_at": r.get("printed_at"), "bundled_at": r.get("bundled_at"),
        "clubbed": bool(r.get("club_size", 1) > 1),
        "target": [min(band.length, SETTINGS.scaling.max_length), band.height] if band else None,
        "compartments": compartments_for(letters, SETTINGS) if letters else None,
    }


# Order tabs. "Not needed" orders are split by their webstore report status.
_CLUB = ("(CASE WHEN COALESCE(clubbed_id,'')='' THEN 1 ELSE "
         "(SELECT COUNT(*) FROM orders o2 WHERE o2.clubbed_id=orders.clubbed_id) END)")
_NN = "state='not_needed' AND "
TABS = {
    "ready": "state='ready'",
    "ready_single": f"state='ready' AND {_CLUB} <= 1",
    "ready_clubbed": f"state='ready' AND {_CLUB} > 1",
    "needs_review": "state='needs_review'",
    "in_batch": "state='in_batch'",
    "bundled": "state='bundled'",
    "printed": "state='printed'",
    "skipped": "state='skipped'",
    "delivered": _NN + "UPPER(source_status)='DELIVERED'",
    "rto": _NN + "UPPER(source_status) LIKE 'RTO%'",
    "in_transit": _NN + "REPLACE(REPLACE(UPPER(source_status),'_','-'),' ','-')='IN-TRANSIT'",
    "cancelled": _NN + "UPPER(source_status) LIKE 'CANCEL%'",
    "others": _NN + "NOT (UPPER(source_status)='DELIVERED' OR UPPER(source_status) LIKE 'RTO%' OR "
              "REPLACE(REPLACE(UPPER(source_status),'_','-'),' ','-')='IN-TRANSIT' OR UPPER(source_status) LIKE 'CANCEL%')",
    "all": "1=1",
}
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
}


def _where(request: Request, tab: str) -> tuple[str, list]:
    if tab not in TABS:
        raise HTTPException(400, f"Unknown tab {tab!r}")
    where, args = [TABS[tab]], []
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
def list_orders(request: Request, tab: str = "ready", limit: int = 200, offset: int = 0):
    w, args = _where(request, tab)
    limit = max(1, min(limit, 1000))
    with db.tx() as con:
        total = con.execute(f"SELECT COUNT(*) FROM orders WHERE {w}", args).fetchone()[0]
        rs = db.rows(con, f"SELECT *, {_CLUB} AS club_size FROM orders WHERE {w} "
                          "ORDER BY order_date DESC, COALESCE(clubbed_id,''), order_id LIMIT ? OFFSET ?",
                     *args, limit, offset)
    return {"total": total, "orders": [_order_view(r) for r in rs]}


@app.get("/api/orders/ids")
def order_ids(request: Request, tab: str = "ready", state: str = "ready"):
    """All selectable order IDs matching the current tab + filters ('Select all matching'):
    ready ones (Create batch), bundled ones (Mark printed) or printed ones (Back to Bundled)."""
    if state not in ("ready", "bundled", "printed"):
        raise HTTPException(400, "Unknown state")
    w, args = _where(request, tab)
    with db.tx() as con:
        rs = db.rows(con, f"SELECT order_id FROM orders WHERE {w} AND state=? ORDER BY order_date, order_id", *args, state)
    return [r["order_id"] for r in rs]


@app.get("/api/orders/facets")
def order_facets(request: Request, tab: str = "ready"):
    """Tab counts plus the values offered in the status / colour filter dropdowns."""
    with db.tx() as con:
        counts = {k: con.execute(f"SELECT COUNT(*) FROM orders WHERE {v}").fetchone()[0] for k, v in TABS.items()}
        w = TABS.get(tab, "1=1")
        statuses = [r["v"] for r in db.rows(con, f"SELECT DISTINCT source_status v FROM orders WHERE {w} ORDER BY v")]
        colors = [r["v"] for r in db.rows(con, f"SELECT DISTINCT COALESCE(color,'') v FROM orders WHERE {w} ORDER BY v")]
    return {"tabs": counts, "statuses": statuses, "colors": colors}


@app.get("/api/orders.csv")
def export_orders(request: Request, tab: str = "all"):
    w, args = _where(request, tab)
    with db.tx() as con:
        rs = db.rows(con, "SELECT order_id,clubbed_id,order_date,customer_name,phone,address,source_status,quantity,"
                          "raw_name,name,raw_color,color,state,batch_id,review_reason,last_error,first_file,last_file "
                          f"FROM orders WHERE {w} ORDER BY order_date", *args)
    f = io.StringIO()
    if rs:
        wr = csv.DictWriter(f, fieldnames=list(rs[0]))
        wr.writeheader()
        wr.writerows(rs)
    return StreamingResponse(iter([f.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=snapstl-orders.csv"})


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
    try:
        reset_to_ready(order_id)
    except KeyError as e:
        raise HTTPException(404, "No such order") from e
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

    return {"model_url": SETTINGS.makerworld_url, "design_id": design_id(SETTINGS.makerworld_url),
            "helper_version": _helper_version()}  # the helper reloads itself when its files were updated


def _helper_version() -> str:
    try:
        return json.loads((updater.ROOT / "helper" / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return ""


@app.get("/api/helper/next")
def helper_next(status: str = "", on_page: int = 1):
    DOWNLOADER.seen(status, bool(on_page))
    if not on_page:  # never hand out work to a tab that isn't on the model page
        return {"job": None, "paused": DOWNLOADER.paused}
    return {"job": B.helper_next(), "paused": DOWNLOADER.paused}


@app.post("/api/helper/ping")
async def helper_ping(req: Request):
    body = await req.json()
    if not body.get("probe"):  # the popup's "is the app running?" check doesn't count as the helper working
        on_page = body.get("on_page")
        DOWNLOADER.seen(str(body.get("status", "")), None if on_page is None else bool(on_page))
    return {"paused": DOWNLOADER.paused}


@app.post("/api/helper/jobs/{job_id}/result")
async def helper_result(job_id: int, plate_1: UploadFile = File(...), plate_2: UploadFile = File(...),
                        check: str = Form("")):
    """`check` (JSON from helper 2.2.0+): did the License password box hold the password at Generate?"""
    DOWNLOADER.seen("Uploading")
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


@app.get("/api/sync")
def sync_status():
    return sync.STATUS.snapshot()


@app.post("/api/sync")
def sync_run():
    """Settings → Database → Sync now: push this computer's changes, load everyone's."""
    if not sync.enabled():
        raise HTTPException(400, "No shared records set up — add SNAPSTL_DATABASE_URL to the .env file (README → Shared order records)")
    return sync.sync_now()


@app.post("/api/maintenance/reset")
async def maintenance_reset(req: Request):
    body = await req.json()
    if body.get("confirm") != "RESET":
        raise HTTPException(400, 'Type RESET to confirm')
    try:
        return M.reset()
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
    with db.tx() as con:
        rs = db.rows(con, "SELECT * FROM batches ORDER BY id DESC LIMIT 100")
    out = []
    for r in rs:
        summ = json.loads(r["summary"] or "{}")
        item = {
            "id": r["id"], "created_at": r["created_at"], "status": r["status"],
            "orders": len(json.loads(r["order_ids"] or "[]")),
            "plates": len(summ.get("plates", [])), "errors": len(summ.get("errors", [])),
            "label": summ.get("label", ""),
            "computer": r.get("computer_name") or "", "mine": (r.get("computer_id") or computer.ID) == computer.ID,
        }
        if r["status"] in B.OPEN:
            item["progress"] = B.batch_progress(r)
        out.append(item)
    return out


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


# ---------------- settings ----------------
@app.get("/api/settings")
def get_settings():
    return SETTINGS.to_dict()


@app.put("/api/settings")
async def put_settings(req: Request):
    global SETTINGS
    try:
        new = Settings.from_dict(await req.json())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Invalid settings: {e}") from e
    new.lan_access = SETTINGS.lan_access  # changed only through /api/portal (this computer)
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

