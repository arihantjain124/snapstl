"""Print farm: finished plates go to Bambuddy, the open-source print-farm manager on this computer.

SnapSTL never talks to a printer itself. Bambuddy (installed next to SnapSTL by the installers,
http://127.0.0.1:8000 — scripts/bambuddy-mac.sh, scripts/windows/bambuddy.ps1) holds the printer
connections (LAN Only + Developer Mode on each printer), the print queue and "Clear plate & start next".
SnapSTL:

  * connects to Bambuddy — on a fresh install (Bambuddy's login off) it creates its own API key;
  * adds printers there (Printers page) and remembers each printer's job here: **Bases** or **Names**
    and the colours loaded;
  * after a build, **Push to queue**: for each plate the person picks a printer (a suggestion is
    pre-filled: right job, right colour, least busy). The plate's sliced file (.gcode.3mf) is uploaded
    to Bambuddy's library and added to that printer's queue; the job is followed until it's printed.
    Bambuddy waits for a person to clear the bed before each next print.

Printers only print *sliced* plates. A plate is sliced either
  * by hand in Bambu Studio (open the plate → Slice → Export plate sliced file), saved in the batch
    folder as `Sliced/<same path>.gcode.3mf` (or `<plate name>.gcode.3mf` next to the plate), or
  * by a slicer service, when one is set (Bambuddy's slicer sidecar — an HTTP wrapper around the
    Bambu Studio / OrcaSlicer command line, Docker). The result is saved as `Sliced/….gcode.3mf`.
The Bambu Studio app's own command line can't slice on macOS (it crashes; ARCHITECTURE decision log #28).

Secrets: the Bambuddy API key is kept only in `farm.json` in the app folder (this user only). Printer
access codes go straight to Bambuddy and are never stored by SnapSTL.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

from . import computer, db
from .platform_os import app_dir

CONFIG_PATH = app_dir() / "farm.json"
DEFAULT_URL = os.environ.get("SNAPSTL_BAMBUDDY_URL", "http://127.0.0.1:8000")
ROLES = ("bases", "names")
MODELS = ["A1", "A1 mini", "P1P", "P1S", "P2S", "X1", "X1C", "X1E", "H2D", "H2S", "H2C"]
ACTIVE = ("queued", "slicing", "uploading", "sent", "printing")
BUSY_STATES = {"RUNNING", "PAUSE", "PREPARE", "SLICING", "INIT"}
# Bambuddy's queue item status → SnapSTL job state
QUEUE_STATE = {"pending": "sent", "printing": "printing", "completed": "done", "failed": "failed",
               "skipped": "cancelled", "cancelled": "cancelled"}
# Name colours (same as the UI) → filament colour in the sliced file, so Bambuddy can match AMS slots.
COLOR_HEX = {"pink": "#F4A7C0", "hot pink": "#FF4FA0", "baby pink": "#F9C9D9", "lavender": "#B9A3E3",
             "red": "#E5534B", "orange": "#F39A3D", "yellow": "#F2D04B", "green": "#5FB36A",
             "blue": "#4A86D9", "baby blue": "#9FD0F3", "sky blue": "#7CC4F0", "purple": "#9A6BD1",
             "black": "#000000", "white": "#FFFFFF", "grey": "#999999", "lightgray": "#CCCCCC"}

DEFAULTS = {
    "bambuddy_url": DEFAULT_URL,
    "api_key": "",
    "slicer_url": "",  # e.g. http://127.0.0.1:3001 (Bambuddy's Bambu Studio sidecar); "" = sliced by hand
    "printer_preset": "Bambu Lab A1 0.4 nozzle",
    "process_preset": "0.20mm Standard @BBL A1",
    "filament_preset": "Bambu PLA Basic @BBL A1",
    "roles": {},  # Bambuddy printer id (str) → {"role": "bases"|"names", "colors": ["pink", …]}
}


class FarmError(Exception):
    """Something the person can fix, in plain English."""


# ---------------- settings (farm.json, this computer only) ----------------
_cfg_lock = threading.Lock()


def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        raw = {}
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    cfg["roles"] = {str(k): _clean_role(v) for k, v in (cfg.get("roles") or {}).items() if isinstance(v, dict)}
    return cfg


def save_config(cfg: dict) -> None:
    """Readable by this user only (it holds the Bambuddy API key)."""
    with _cfg_lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: cfg.get(k, v) for k, v in DEFAULTS.items()}, f, indent=1)
        os.replace(tmp, CONFIG_PATH)


def update_config(**changes) -> dict:
    cfg = load_config()
    cfg.update(changes)
    save_config(cfg)
    return cfg


def _clean_role(v: dict) -> dict:
    role = v.get("role") if v.get("role") in ROLES else ""
    colors = sorted({str(c).strip().lower().replace("gray", "grey") for c in (v.get("colors") or []) if str(c).strip()})
    return {"role": role, "colors": colors}


def public_config(cfg: dict | None = None) -> dict:
    """Settings for the UI — never the API key itself."""
    cfg = cfg or load_config()
    return {k: v for k, v in cfg.items() if k != "api_key"} | {"key_set": bool(cfg["api_key"])}


# ---------------- HTTP (stdlib only; tests replace `_request`) ----------------
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local addresses never via a proxy


def _request(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _multipart(fields: dict, files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "snapstl" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, filename, data, ctype in files:
        safe = filename.replace('"', "'").replace("\r", "").replace("\n", "")
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _call(base: str, method: str, path: str, *, key: str = "", json_body=None, fields=None, files=None,
          timeout: float = 20, what: str = "Bambuddy"):
    headers = {"Accept": "application/json"}
    if key:
        headers["X-API-Key"] = key
    body = None
    if files:
        body, headers["Content-Type"] = _multipart(fields or {}, files)
    elif fields:
        body, headers["Content-Type"] = _multipart(fields, [])
    elif json_body is not None:
        body, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
    try:
        status, data = _request(method, base.rstrip("/") + path, headers, body, timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise FarmError(f"Can't reach {what} at {base} ({reason}) — is it running?") from e
    return status, data


def _detail(status: int, data: bytes) -> str:
    try:
        d = json.loads(data).get("detail")
    except (ValueError, AttributeError):
        d = None
    if isinstance(d, dict):
        d = d.get("message") or d.get("code") or json.dumps(d)
    elif isinstance(d, list):
        d = "; ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
    return str(d or (data[:200].decode("utf-8", "replace") if data else "") or f"HTTP {status}")


def api(method: str, path: str, *, cfg: dict | None = None, url: str | None = None, key: str | None = None,
        json_body=None, files=None, timeout: float = 20):
    """Call Bambuddy's API (/api/v1…). Returns the decoded JSON; raises FarmError."""
    cfg = cfg or load_config()
    base = (url or cfg["bambuddy_url"]).rstrip("/") + "/api/v1"
    status, data = _call(base, method, path, key=cfg["api_key"] if key is None else key, json_body=json_body,
                         files=files, timeout=timeout)
    if status in (401, 403):
        raise FarmError(f"Bambuddy refused SnapSTL ({_detail(status, data)}) — connect again on the Printers page")
    if status >= 400:
        raise FarmError(f"Bambuddy: {_detail(status, data)}")
    try:
        return json.loads(data) if data else None
    except ValueError as e:
        raise FarmError(f"Bambuddy answered something unexpected at {path} — is {base} really Bambuddy?") from e


# ---------------- connection ----------------
def status(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    out = {"url": cfg["bambuddy_url"], "reachable": False, "auth_enabled": None, "key_set": bool(cfg["api_key"]),
           "error": ""}
    try:
        s = api("GET", "/auth/status", cfg=cfg, key="", timeout=4)
        out["reachable"] = True
        out["auth_enabled"] = bool((s or {}).get("auth_enabled"))
    except FarmError as e:
        out["error"] = str(e)
    out["connected"] = out["reachable"] and (out["key_set"] or out["auth_enabled"] is False)
    return out


def connect(url: str | None = None, api_key: str | None = None) -> dict:
    """Point SnapSTL at Bambuddy. Without a key and with Bambuddy's login off, SnapSTL makes its own key."""
    cfg = load_config()
    if url:
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise FarmError("Bambuddy's address starts with http:// (e.g. http://127.0.0.1:8000)")
        cfg["bambuddy_url"] = url
    st = status(cfg)
    if not st["reachable"]:
        raise FarmError(st["error"])
    if api_key:
        api("GET", "/printers/", cfg=cfg, key=api_key.strip())  # the key works
        cfg["api_key"] = api_key.strip()
    elif not cfg["api_key"] or url:
        if st["auth_enabled"]:
            raise FarmError("Bambuddy's login is on: in Bambuddy open Settings → API Keys, create a key "
                            "(Queue, Read status, Library, Control printer) and paste it here")
        made = api("POST", "/api-keys/", cfg=cfg, key="", json_body={
            "name": f"SnapSTL ({computer.NAME})"[:100], "can_queue": True, "can_read_status": True,
            "can_manage_library": True, "can_control_printer": True})
        cfg["api_key"] = (made or {}).get("key", "")
        if not cfg["api_key"]:
            raise FarmError("Bambuddy didn't hand out an API key")
    save_config(cfg)
    return status(cfg)


def ensure_connected() -> None:
    """First start after installing: connect by itself when Bambuddy is there with its login off."""
    cfg = load_config()
    if cfg["api_key"]:
        return
    st = status(cfg)
    if st["reachable"] and st["auth_enabled"] is False:
        connect()


# ---------------- printers ----------------
_status_cache: dict[int, tuple[float, dict]] = {}


def printer_status(pid: int, cfg: dict | None = None, max_age: float = 3.0) -> dict:
    hit = _status_cache.get(pid)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        s = api("GET", f"/printers/{pid}/status", cfg=cfg, timeout=6) or {}
    except FarmError as e:
        s = {"connected": False, "error": str(e)}
    _status_cache[pid] = (time.time(), s)
    return s


def printers(cfg: dict | None = None, with_status: bool = True) -> list[dict]:
    """Bambuddy's printers with their SnapSTL job (role, colours) and live state."""
    cfg = cfg or load_config()
    out = []
    for p in api("GET", "/printers/", cfg=cfg) or []:
        pid = int(p["id"])
        role = cfg["roles"].get(str(pid), {"role": "", "colors": []})
        item = {"id": pid, "name": p.get("name") or f"Printer {pid}", "model": p.get("model") or "",
                "serial": p.get("serial_number") or "", "ip": p.get("ip_address") or "",
                "location": p.get("location") or "", "active": p.get("is_active", True), **role}
        if with_status:
            s = printer_status(pid, cfg)
            item["status"] = {
                "connected": bool(s.get("connected")), "state": s.get("state") or "",
                "progress": s.get("progress"), "remaining_time": s.get("remaining_time"),
                "awaiting_plate_clear": bool(s.get("awaiting_plate_clear")),
                "errors": len(s.get("hms_errors") or []), "error": s.get("error", ""),
                "file": s.get("subtask_name") or s.get("current_print") or "",
            }
        out.append(item)
    return out


def add_printer(name: str, ip: str, serial: str, access_code: str, model: str, role: str, colors: list[str]) -> dict:
    """Add a printer to Bambuddy (it checks the connection first) and remember its job here."""
    name, ip, serial, access_code = name.strip(), ip.strip(), serial.strip().upper(), access_code.strip()
    if not (name and ip and serial and access_code):
        raise FarmError("Fill in the name, IP address, serial number and access code")
    if role not in ROLES:
        raise FarmError("Choose what the printer makes: Bases or Names")
    body = {"name": name, "ip_address": ip, "serial_number": serial, "access_code": access_code,
            "model": model or None, "location": "Bases" if role == "bases" else "Names"}
    p = api("POST", "/printers/", json_body=body, timeout=45)
    set_role(int(p["id"]), role, colors, push_location=False)
    return {"id": int(p["id"]), "name": p.get("name", name)}


def set_role(pid: int, role: str, colors: list[str], push_location: bool = True) -> dict:
    if role and role not in ROLES:
        raise FarmError("Role is Bases or Names")
    cfg = load_config()
    cfg["roles"][str(pid)] = _clean_role({"role": role, "colors": colors})
    save_config(cfg)
    if push_location and role:
        try:  # shown in Bambuddy too; not needed for SnapSTL
            api("PATCH", f"/printers/{pid}", cfg=cfg, json_body={"location": "Bases" if role == "bases" else "Names"})
        except FarmError:
            pass
    return cfg["roles"][str(pid)]


def clear_plate(pid: int) -> None:
    """A person confirmed the bed is empty: Bambuddy may start the next queued print."""
    api("POST", f"/printers/{pid}/clear-plate")
    _status_cache.pop(pid, None)


# ---------------- sliced files ----------------
def _stem(plate_rel: str) -> str:
    return plate_rel[:-4] if plate_rel.lower().endswith(".3mf") else plate_rel


def sliced_target(out_dir: Path, plate_rel: str) -> Path:
    return Path(out_dir) / "Sliced" / (_stem(plate_rel) + ".gcode.3mf")


def find_sliced(out_dir: Path, plate_rel: str) -> Path | None:
    """The plate's sliced file, if someone (or the slicer service) made it."""
    for p in (sliced_target(out_dir, plate_rel), Path(out_dir) / (_stem(plate_rel) + ".gcode.3mf")):
        if p.is_file():
            return p
    return None


def check_sliced(data: bytes) -> None:
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile as e:
        raise FarmError("That isn't a sliced 3MF file") from e
    if not any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in names):
        raise FarmError("The 3MF has no sliced plate inside (Metadata/plate_1.gcode) — slice it first")


def slicer_profiles(cfg: dict, color: str | None) -> list[tuple[str, str, bytes, str]]:
    """Printer / process / filament as user presets that inherit Bambu's system presets (by name)."""
    def prof(typ: str, parent: str, **extra) -> bytes:
        return json.dumps({"type": typ, "name": f"SnapSTL {parent}"[:120], "from": "User", "inherits": parent,
                           **extra}).encode()

    fil = {"filament_colour": [COLOR_HEX[color]]} if color in COLOR_HEX else {}
    return [("printerProfile", "printer.json", prof("machine", cfg["printer_preset"]), "application/json"),
            ("presetProfile", "preset.json", prof("process", cfg["process_preset"]), "application/json"),
            ("filamentProfile", "filament_1.json", prof("filament", cfg["filament_preset"], **fil), "application/json")]


def slice_plate(src: Path, dst: Path, color: str | None, cfg: dict | None = None) -> Path:
    """Slice one plate with the slicer service; parts stay exactly where SnapSTL put them (no arrange)."""
    cfg = cfg or load_config()
    if not cfg["slicer_url"]:
        raise FarmError("Not sliced yet: open the plate in Bambu Studio → Slice → Export plate sliced file, "
                        f"save it as {dst.name} in the batch's Sliced folder — or set a slicer service on the Printers page")
    files = [("file", src.name, src.read_bytes(), "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")]
    files += slicer_profiles(cfg, color)
    st, data = _call(cfg["slicer_url"], "POST", "/slice", fields={"exportType": "3mf"}, files=files, timeout=900,
                     what="the slicer service")
    if st >= 400:
        raise FarmError(f"Slicer service: {_detail(st, data)}")
    check_sliced(data)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
    return dst


def check_slicer(url: str) -> str:
    """'' if the slicer service answers its health check, else why not."""
    try:
        st, data = _call(url, "GET", "/health", timeout=5, what="the slicer service")
    except FarmError as e:
        return str(e)
    return "" if st < 400 else f"The slicer service answered HTTP {st}"


# ---------------- plates of a batch → printers ----------------
def eligible(plate: dict, printer: dict) -> str:
    """'' if the printer can print this plate, else why not."""
    if printer.get("role") != plate["kind"]:
        want = "Bases" if plate["kind"] == "bases" else "Names"
        return f"set to {'Names' if printer.get('role') == 'names' else 'Bases' if printer.get('role') else 'no job'} (needs {want})"
    if plate["kind"] == "names":
        color = (plate.get("colors") or [""])[0]
        if color not in printer.get("colors", []):
            loaded = ", ".join(printer.get("colors") or []) or "no colour set"
            return f"has {loaded} (needs {color})"
    return ""


def _busy(printer: dict) -> int:
    s = printer.get("status") or {}
    return int(s.get("state", "").upper() in BUSY_STATES or bool(s.get("awaiting_plate_clear")))


def suggest(plates: list[dict], prs: list[dict], active_jobs: dict[int, int]) -> dict[str, int | None]:
    """Plate file → suggested printer id: right job and colour, connected, least busy (spread out)."""
    load = {p["id"]: active_jobs.get(p["id"], 0) + _busy(p) for p in prs}
    out: dict[str, int | None] = {}
    for pl in plates:
        if pl.get("job_active"):
            continue
        ok = [p for p in prs if not eligible(pl, p) and p.get("active", True)]
        if not ok:
            out[pl["file"]] = None
            continue
        best = min(ok, key=lambda p: (not (p.get("status") or {}).get("connected", True), load[p["id"]], p["name"]))
        out[pl["file"]] = best["id"]
        load[best["id"]] += 1
    return out


def active_counts() -> dict[int, int]:
    with db.tx() as con:
        rs = db.rows(con, f"SELECT printer_id, COUNT(*) n FROM print_jobs WHERE state IN ({','.join('?' * len(ACTIVE))}) "
                          "AND computer_id=? GROUP BY printer_id", *ACTIVE, computer.ID)
    return {r["printer_id"]: r["n"] for r in rs}


def jobs(batch_id: int | None = None, limit: int = 200) -> list[dict]:
    with db.tx() as con:
        if batch_id is None:
            return db.rows(con, "SELECT * FROM print_jobs WHERE computer_id=? ORDER BY id DESC LIMIT ?", computer.ID, limit)
        return db.rows(con, "SELECT * FROM print_jobs WHERE batch_id=? AND computer_id=? ORDER BY id", batch_id, computer.ID)


def latest_jobs(batch_id: int) -> dict[str, dict]:
    """Plate file → its newest job."""
    out: dict[str, dict] = {}
    for j in jobs(batch_id):
        out[j["plate"]] = j
    return out


def queue_jobs(batch: dict, picks: list[dict], prs: list[dict], again: bool = False) -> dict:
    """Push to queue: one job per (plate, printer). Plates with a job on its way are skipped unless `again`."""
    summ = json.loads(batch.get("summary") or "{}")
    plates = {p["file"]: p for p in summ.get("plates", [])}
    by_id = {p["id"]: p for p in prs}
    current = latest_jobs(batch["id"])
    made, skipped, problems = [], [], []
    for pick in picks:
        f, pid = str(pick.get("file", "")), pick.get("printer_id")
        plate = plates.get(f)
        if plate is None:
            problems.append(f"{f}: not a plate of this batch")
            continue
        if pid in (None, ""):
            continue
        printer = by_id.get(int(pid))
        if printer is None:
            problems.append(f"{Path(f).name}: printer {pid} isn't in Bambuddy")
            continue
        why = eligible(plate, printer)
        if why:
            problems.append(f"{Path(f).name}: {printer['name']} {why}")
            continue
        if not again and current.get(f, {}).get("state") in ACTIVE + ("done",):
            skipped.append(f)
            continue
        made.append((f, plate, printer))
    if problems:
        raise FarmError("; ".join(problems))
    now = db.now()
    with db.tx() as con:
        for f, plate, printer in made:
            con.execute(
                "INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,updated_at,computer_id) "
                "VALUES (?,?,?,?,?,?,'queued',?,?,?)",
                (batch["id"], f, plate["kind"], (plate.get("colors") or [""])[0], printer["id"], printer["name"], now, now,
                 computer.ID))
    WORKER.kick()
    return {"queued": len(made), "skipped": len(skipped)}


def retry_job(job_id: int) -> None:
    with db.tx() as con:
        n = con.execute("UPDATE print_jobs SET state='queued', error=NULL, updated_at=? WHERE id=? AND computer_id=? "
                        "AND state IN ('failed','cancelled')", (db.now(), job_id, computer.ID)).rowcount
    if not n:
        raise FarmError("Only failed or cancelled jobs can be tried again")
    WORKER.kick()


def cancel_job(job_id: int) -> None:
    with db.tx() as con:
        j = con.execute("SELECT * FROM print_jobs WHERE id=? AND computer_id=?", (job_id, computer.ID)).fetchone()
    if j is None:
        raise FarmError("No such print job")
    if j["state"] == "printing":
        raise FarmError("It's printing — stop it on the printer or in Bambuddy")
    if j["state"] == "sent" and j["queue_item_id"]:
        api("POST", f"/queue/{j['queue_item_id']}/cancel")
    elif j["state"] not in ("queued", "failed"):
        raise FarmError(f"Can't cancel a job that is {j['state']}")
    _set(job_id, state="cancelled")


def _set(job_id: int, **cols) -> None:
    cols["updated_at"] = db.now()
    with db.tx() as con:
        con.execute(f"UPDATE print_jobs SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), job_id))


# ---------------- background worker ----------------
class Worker:
    """Slices / uploads / queues one job at a time, oldest first; follows sent jobs in Bambuddy's queue."""

    REFRESH_S = 15

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: int | None = None
        self.last_refresh = 0.0
        self.log: list[str] = []

    def start(self) -> None:
        with db.tx() as con:  # a job cut off by a restart starts over (an upload never became a queue item)
            con.execute("UPDATE print_jobs SET state='queued', updated_at=? WHERE state IN ('slicing','uploading') "
                        "AND computer_id=?", (db.now(), computer.ID))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="farm", daemon=True)
            self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        try:
            ensure_connected()
        except Exception:  # noqa: BLE001 — Bambuddy may not be installed; the Printers page says so
            pass
        while True:
            self._wake.wait(2.0)
            self._wake.clear()
            try:
                while self.run_next():
                    pass
                if time.time() - self.last_refresh > self.REFRESH_S:
                    self.refresh()
            except Exception as e:  # noqa: BLE001 — keep the worker alive
                self._log(f"Print farm: {e}")

    def _log(self, msg: str) -> None:
        self.log = (self.log + [time.strftime("%H:%M:%S ") + msg])[-60:]

    def run_next(self) -> bool:
        with db.tx() as con:
            j = con.execute("SELECT * FROM print_jobs WHERE state='queued' AND computer_id=? ORDER BY id LIMIT 1",
                            (computer.ID,)).fetchone()
            b = con.execute("SELECT out_dir, summary FROM batches WHERE id=?", (j["batch_id"],)).fetchone() if j else None
        if j is None:
            return False
        j = dict(j)
        self.current = j["id"]
        try:
            self._send(j, dict(b) if b else None)
        except FarmError as e:
            _set(j["id"], state="failed", error=str(e))
            self._log(f"{Path(j['plate']).name} → {j['printer_name']}: {e}")
        except Exception as e:  # noqa: BLE001
            _set(j["id"], state="failed", error=f"Unexpected problem: {e}")
            self._log(f"{Path(j['plate']).name}: {e}")
        finally:
            self.current = None
        return True

    def _send(self, j: dict, b: dict | None) -> None:
        if not b or not b["out_dir"]:
            raise FarmError("The batch folder is unknown")
        out = Path(b["out_dir"])
        src = out / j["plate"]
        if not src.is_file():
            raise FarmError(f"The plate file is gone: {src}")
        cfg = load_config()
        sliced = find_sliced(out, j["plate"])
        if sliced is None:
            _set(j["id"], state="slicing", error=None)
            sliced = slice_plate(src, sliced_target(out, j["plate"]), j["color"] or None, cfg)
        data = sliced.read_bytes()
        check_sliced(data)
        _set(j["id"], state="uploading", error=None, sliced=str(sliced.relative_to(out)) if out in sliced.parents else str(sliced))
        label = Path(b["out_dir"]).name  # "Batch 2 - 2026-11-16"
        kind = "Bases" if j["kind"] == "bases" else f"Names {j['color']}".strip()
        filename = _upload_name(label, kind, sliced.name)
        up = api("POST", "/library/files", cfg=cfg, files=[("file", filename, data, "application/octet-stream")], timeout=300)
        file_id = int(up["id"])
        r = api("POST", "/library/files/add-to-queue", cfg=cfg, json_body={"file_ids": [file_id], "printer_id": j["printer_id"]})
        added = (r or {}).get("added") or []
        if not added:
            errs = "; ".join(e.get("error", "") for e in (r or {}).get("errors") or []) or "not added"
            raise FarmError(f"Bambuddy didn't queue it: {errs}")
        _set(j["id"], state="sent", file_id=file_id, queue_item_id=int(added[0]["queue_item_id"]), error=None)
        self._log(f"{Path(j['plate']).name} → queued on {j['printer_name']}")

    def refresh(self) -> None:
        """Follow jobs sent to Bambuddy: waiting → printing → printed (or failed / cancelled there)."""
        self.last_refresh = time.time()
        with db.tx() as con:
            rs = db.rows(con, "SELECT * FROM print_jobs WHERE state IN ('sent','printing') AND computer_id=?", computer.ID)
        if not rs:
            return
        cfg = load_config()
        for j in rs:
            try:
                item = api("GET", f"/queue/{j['queue_item_id']}", cfg=cfg, timeout=8)
            except FarmError as e:
                if "not found" in str(e).lower():
                    _set(j["id"], state="cancelled", error="Removed from Bambuddy's queue")
                continue
            new = QUEUE_STATE.get((item or {}).get("status", ""), j["state"])
            changes: dict = {}
            if new != j["state"]:
                changes["state"] = new
                if new == "failed":
                    changes["error"] = (item or {}).get("error_message") or "The print failed (see Bambuddy)"
                if new == "done":
                    changes["progress"] = 100.0
            if new == "printing":
                s = printer_status(j["printer_id"], cfg, max_age=10)
                if s.get("progress") is not None:
                    changes["progress"] = float(s["progress"])
            if changes:
                _set(j["id"], **changes)


def _upload_name(label: str, kind: str, name: str) -> str:
    """File name in Bambuddy and on the printer's SD card (no characters FAT cards refuse; ≤ 200 bytes)."""
    suffix = ".gcode.3mf"
    body = f"{label} - {kind} - {name[:-len(suffix)] if name.endswith(suffix) else name}"
    for ch in '<>:"/\\|?*':
        body = body.replace(ch, "_")
    while len((body + suffix).encode()) > 200:
        body = body[:-1]
    return body.rstrip(" .") + suffix


WORKER = Worker()
