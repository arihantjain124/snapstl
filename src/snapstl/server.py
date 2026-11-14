"""SnapSTL web server (behind Caddy on the Lightsail instance, or local)."""
from __future__ import annotations

import csv
from contextlib import asynccontextmanager
import io
import time
import json
import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from . import auth, db
from .ingest import ingest, reset_to_ready, set_name
from . import batches as B
from . import maintenance as M
from .batches import DOWNLOADER, MANAGER, compartments_for
from .keychain import get_password, set_password
from .scaling import pick_band
from .settings import COMPARTMENT_RANGE, FONTS, NUMBER_RANGES, ROTATE_TEXT, Settings, load_settings, save_settings

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 50 * 1024 * 1024
SECURE_COOKIE = os.environ.get("SNAPSTL_SECURE_COOKIE", "1") == "1"
SETTINGS: Settings = load_settings()


@asynccontextmanager
async def _lifespan(_app):
    MANAGER.output_dir = SETTINGS.output_dir
    MANAGER.start()  # builds batches once their models arrive
    DOWNLOADER.pause_s = SETTINGS.delay_between_downloads_s
    DOWNLOADER.start()  # re-queues names that were mid-download when the app stopped
    yield


app = FastAPI(title="SnapSTL", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

OPEN_PATHS = {"/login", "/api/login", "/api/auth-check", "/favicon.ico"}


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _cross_site(request: Request) -> bool:
    """True for requests sent by some other website open in a browser (CSRF / DNS-rebinding guard)."""
    host = request.headers.get("host", "")
    if not auth.enabled() and host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
        return True  # local app must only be reached as 127.0.0.1 / localhost
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin.startswith("chrome-extension://"):
        # only the helper extension's endpoints accept extension calls, and only on this Mac
        return not (request.url.path.startswith("/api/helper/") and not auth.enabled())
    return origin.split("://", 1)[-1] != host


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if _cross_site(request):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    if path in OPEN_PATHS or not auth.enabled():
        if not auth.enabled() and path not in ("/login", "/api/login"):
            # No password configured: only allow local access (desktop use).
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1") or request.headers.get("x-forwarded-for"):
                return JSONResponse({"detail": "Server password not set"}, status_code=503)
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
                    httponly=True, secure=SECURE_COOKIE, samesite="lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth-check")
def auth_check(request: Request):
    """Used by Caddy forward_auth to protect the remote-browser view."""
    if auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        return {"ok": True}
    raise HTTPException(401)


# ---------------- app ----------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "fonts": FONTS, "rotate_text": ROTATE_TEXT, "ranges": NUMBER_RANGES,
        "compartment_range": COMPARTMENT_RANGE,
        "local": not auth.enabled(),  # desktop app on this Mac (no login, Finder buttons)
    }


@app.get("/api/state")
def state():
    with db.tx() as con:
        counts = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) n FROM orders GROUP BY state")}
        ready_colors = db.rows(
            con, "SELECT color, COUNT(*) n, SUM(quantity) q FROM orders WHERE state='ready' GROUP BY color ORDER BY n DESC"
        )
    return {
        "counts": counts, "ready_colors": ready_colors, "license_set": bool(get_password()),
        "downloader": DOWNLOADER.snapshot(),
        "queue": B.queue_counts(), "building": MANAGER.building, "log": MANAGER.log[-120:],
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
    try:
        s = ingest(data, name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
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
def order_ids(request: Request, tab: str = "ready"):
    """All selectable (ready) order IDs matching the current tab + filters ('Select all matching')."""
    w, args = _where(request, tab)
    with db.tx() as con:
        rs = db.rows(con, f"SELECT order_id FROM orders WHERE {w} AND state='ready' ORDER BY order_date, order_id", *args)
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
    return {"ok": True}


@app.post("/api/orders/{order_id}/skip")
def skip_order(order_id: str):
    with db.tx() as con:
        con.execute("UPDATE orders SET state='skipped', updated_at=? WHERE order_id=? AND state IN ('ready','needs_review')",
                    (db.now(), order_id))
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
def batch_download(batch_id: int):
    try:
        return B.start_download(batch_id)
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


# ---------------- SnapSTL Helper (Chrome extension on this Mac) ----------------
HELPER_DIR = Path(__file__).resolve().parents[2] / "helper"


@app.get("/api/helper/next")
def helper_next(status: str = ""):
    DOWNLOADER.seen(status)
    return {"job": B.helper_next(), "paused": DOWNLOADER.paused}


@app.post("/api/helper/ping")
async def helper_ping(req: Request):
    body = await req.json()
    if not body.get("probe"):  # the popup's "is the app running?" check doesn't count as the helper working
        DOWNLOADER.seen(str(body.get("status", "")))
    return {"paused": DOWNLOADER.paused}


@app.post("/api/helper/jobs/{job_id}/result")
async def helper_result(job_id: int, plate_1: UploadFile = File(...), plate_2: UploadFile = File(...)):
    DOWNLOADER.seen("Uploading")
    a = await plate_1.read(MAX_UPLOAD + 1)
    b = await plate_2.read(MAX_UPLOAD + 1)
    if len(a) > MAX_UPLOAD or len(b) > MAX_UPLOAD:
        raise HTTPException(413, "Model too large")
    try:
        return B.store_result(job_id, a, b)
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


@app.post("/api/maintenance/reset")
async def maintenance_reset(req: Request):
    body = await req.json()
    if body.get("confirm") != "RESET":
        raise HTTPException(400, 'Type RESET to confirm')
    try:
        return M.reset(clear_cache=bool(body.get("clear_cache")))
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/downloads")
def downloads():
    """Download queue for the UI: active + failed first, then the 30 most recent finished."""
    with db.tx() as con:
        active = db.rows(con, "SELECT id,name,compartments,state,attempts,error,updated_at FROM downloads "
                              "WHERE state IN ('claimed','queued','failed') "
                              "ORDER BY CASE state WHEN 'claimed' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id LIMIT 500")
        recent = db.rows(con, "SELECT id,name,compartments,state,attempts,error,updated_at FROM downloads "
                              "WHERE state='done' ORDER BY updated_at DESC LIMIT 30")
    return {"items": active + recent, "downloader": DOWNLOADER.snapshot()}


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


@app.get("/api/batches/{batch_id}")
def batch(batch_id: int):
    r = _batch(batch_id)
    return {"id": r["id"], "created_at": r["created_at"], "status": r["status"],
            "order_ids": json.loads(r["order_ids"] or "[]"), "summary": json.loads(r["summary"] or "{}"),
            "progress": B.batch_progress(r) if r["status"] in B.OPEN else None}


@app.get("/api/batches/{batch_id}/file")
def batch_file(batch_id: int, path: str):
    root = Path(_batch(batch_id)["out_dir"]).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "No such file")
    return FileResponse(target, filename=target.name)


@app.post("/api/batches/{batch_id}/reveal")
async def batch_reveal(batch_id: int, req: Request):
    """Desktop app only: show the batch folder (or one plate file) in Finder."""
    if auth.enabled():
        raise HTTPException(404)
    body = await req.json()
    root = Path(_batch(batch_id)["out_dir"] or "/nonexistent").resolve()
    if not root.is_dir():
        raise HTTPException(404, "Batch folder is gone")
    target = root
    if body.get("file"):
        target = (root / str(body["file"])).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "No such file")
    import subprocess

    subprocess.run(["open", "-R", str(target)] if target.is_file() else ["open", str(target)], check=False)
    return {"ok": True}


@app.get("/api/batches/{batch_id}/zip")
def batch_zip(batch_id: int):
    """On-demand download only: the ZIP is built in a temp file and deleted after sending (never saved)."""
    b = _batch(batch_id)
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
    if not auth.enabled():  # desktop app: the output folder can be changed
        out = Path(new.output_dir).expanduser()
        if not out.is_absolute():
            raise HTTPException(400, "Output folder must be a full path (e.g. /Users/you/SnapSTL/output)")
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Can't use that output folder: {e}") from e
        new.output_dir = str(out)
    else:
        new.output_dir = SETTINGS.output_dir
    errs = new.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    SETTINGS = new
    save_settings(SETTINGS)
    MANAGER.output_dir = SETTINGS.output_dir
    DOWNLOADER.pause_s = SETTINGS.delay_between_downloads_s
    return SETTINGS.to_dict()


@app.post("/api/settings/reset")
def reset_settings():
    global SETTINGS
    SETTINGS = Settings(output_dir=SETTINGS.output_dir)
    save_settings(SETTINGS)
    return SETTINGS.to_dict()


@app.post("/api/license")
async def license_password(req: Request):
    body = await req.json()
    set_password(str(body.get("password", "")))
    return {"license_set": bool(get_password())}
