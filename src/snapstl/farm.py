"""Print farm: finished plates go to Bambuddy, the open-source print-farm manager on this computer.

SnapSTL never talks to a printer itself. Bambuddy (installed next to SnapSTL by the installers,
http://127.0.0.1:8000 — scripts/bambuddy-mac.sh, scripts/windows/bambuddy.ps1) holds the printer
connections (LAN Only + Developer Mode on each printer), the print queue and "Clear plate & start next".
SnapSTL:

  * connects to Bambuddy — on a fresh install (Bambuddy's login off) it creates its own API key;
  * only *reads* the printers from Bambuddy (Printers page): live state and the filament loaded in
    each AMS slot / external spool, named with Bambuddy's colour catalogue ("Hot Pink", "Jade White").
    Printers, filament and everything else are set up in Bambuddy itself. Any printer can print bases
    or names: it only needs the right filament loaded (white for bases, the name's colour for names);
  * after a build, **Push to queue**: for each plate the person picks a printer (a suggestion is
    pre-filled: the plate's colour loaded — white for bases — right job, least busy).
  * nothing is queued or started by itself: a plate goes to Bambuddy only when a person presses Push
    to queue / Print, and it waits there **held** (manual start) until a person presses **Start**. The plate's sliced file (.gcode.3mf) is uploaded
    to Bambuddy's library and added to that printer's queue; the job is followed until it's printed.
    Bambuddy waits for a person to clear the bed before each next print.

Printers only print *sliced* plates. Slicing (Slicing tab) runs on this computer with OrcaSlicer or
Bambu Studio (`slicer.py`), or with Bambuddy's slicer — always with the presets of the printer the plate
goes to (`target`: each printer's own presets, else its model's, else the default). Slices run in the
background one at a time and never block printing; a sliced file already in the batch folder is used as
it is.

Secrets: the Bambuddy API key is kept only in `farm.json` in the app folder (this user only). SnapSTL
never sees printer access codes.
"""
from __future__ import annotations

import io
import json
import os
import re
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
BASE_COLOR = "white"  # bases are printed in white
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
    # Bambuddy slicer presets chosen in Make It → Slice: {"printer"|"process"|"filament": {source, id, name}}
    "presets_by_model": {},       # printer model (as in Bambuddy, e.g. "A1") → its own presets
    "auto_slice": True,           # slice new plates by themselves (printing still needs Start print)
    "slicer": "",                 # "orca" | "bambu" (Bambu Studio, Windows) on this computer | "bambuddy" | "" = auto
    "orca_presets_by_model": {},  # printer model → its own preset names
    "print_defaults": {},         # Bambuddy printer id (str) → {speed, bed_levelling, flow_cali, timelapse}
}
PRESET_SLOTS = ("printer", "process", "filament")
SPEEDS_SET = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}  # print speed per job (Make It)
SLICE_TIMEOUT_S = 20 * 60  # a slice with no progress for this long is given up (it never blocks printing)


class FarmError(Exception):
    """Something the person can fix, in plain English."""


# ---------------- settings (farm.json, this computer only) ----------------
_cfg_lock = threading.Lock()


def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        raw = {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


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


_color_map: tuple[float, dict] = (0.0, {})


def color_names(cfg: dict | None = None) -> dict:
    """Bambuddy's colour catalogue: {"colors": {hex: name}, "by_material": {"PLA Basic|hex": name}}."""
    global _color_map
    if time.time() - _color_map[0] < 600 and _color_map[1]:
        return _color_map[1]
    try:
        m = api("GET", "/inventory/colors/map", cfg=cfg, timeout=8) or {}
    except FarmError:
        m = {}
    m = {"colors": {str(k).lower(): v for k, v in (m.get("colors") or {}).items()},
         "by_material": {str(k).lower(): v for k, v in (m.get("by_material") or {}).items()}}
    if m["colors"]:
        _color_map = (time.time(), m)
    return m


def _norm(c: str) -> str:
    """'Light Gray' → 'light grey'; order colours and filament names compare in this form."""
    return " ".join(str(c or "").strip().lower().replace("gray", "grey").replace("lightgrey", "light grey").split())


def _hex(c: str | None) -> str:
    h = str(c or "").strip().lstrip("#")[:6].lower()
    return h if len(h) == 6 and all(ch in "0123456789abcdef" for ch in h) else ""


def _nearest_palette(h: str) -> str:
    """Closest SnapSTL colour name for a spool with no catalogue name (only for display / 'close')."""
    if not h:
        return ""
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    best, dist = "", 10 ** 9
    for name, hx in COLOR_HEX.items():
        o = [int(hx[1:][i:i + 2], 16) for i in (0, 2, 4)]
        d = sum((a - b) ** 2 for a, b in zip(rgb, o))
        if d < dist:
            best, dist = name, d
    return _norm(best) if dist < 60 ** 2 else ""


def spools(s: dict, cmap: dict) -> list[dict]:
    """Filament loaded in a printer, from Bambuddy's live status: AMS slots + external spool(s)."""
    out = []
    trays = [(u, t) for u in (s.get("ams") or []) for t in (u.get("tray") or [])]
    trays += [(None, t) for t in (s.get("vt_tray") or [])]
    for unit, t in trays:
        h = _hex(t.get("tray_color"))
        if t.get("exists") is False or t.get("state") == 9 or not (h or t.get("tray_type")):
            continue
        material = t.get("tray_sub_brands") or t.get("tray_type") or ""
        name = (cmap.get("by_material", {}).get(f"{material.lower()}|{h}") or cmap.get("colors", {}).get(h) or "")
        slot = (f"AMS {int(unit.get('id', 0)) + 1} · {int(t.get('id', 0)) % 4 + 1}" if unit is not None
                else "External")
        out.append({"slot": slot, "hex": f"#{h.upper()}" if h else "", "name": name, "material": material,
                    "color": _norm(name or _nearest_palette(h)),
                    "remain": t.get("remain") if isinstance(t.get("remain"), int) and t.get("remain") >= 0 else None,
                    "approx": "" if name else _nearest_palette(h)})
    return out


def printers(cfg: dict | None = None, with_status: bool = True) -> list[dict]:
    """Bambuddy's printers (read only): live state and filament loaded."""
    cfg = cfg or load_config()
    cmap = color_names(cfg) if with_status else {}
    out = []
    for p in api("GET", "/printers/", cfg=cfg) or []:
        pid = int(p["id"])
        item = {"id": pid, "name": p.get("name") or f"Printer {pid}", "model": p.get("model") or "",
                "serial": p.get("serial_number") or "", "ip": p.get("ip_address") or "",
                "location": p.get("location") or "",
                "active": p.get("is_active", True), "spools": [], "colors": []}
        if with_status:
            s = printer_status(pid, cfg)
            temps = s.get("temperatures") or {}
            item["spools"] = spools(s, cmap)
            item["colors"] = sorted({_norm(x["name"] or x["approx"]) for x in item["spools"] if x["name"] or x["approx"]})
            item["status"] = {
                "connected": bool(s.get("connected")), "state": s.get("state") or "",
                "progress": s.get("progress"), "remaining_time": s.get("remaining_time"),
                "awaiting_plate_clear": bool(s.get("awaiting_plate_clear")),
                "errors": len(s.get("hms_errors") or []), "error": s.get("error", ""),
                "file": s.get("subtask_name") or s.get("current_print") or "",
                "layer": s.get("layer_num"), "layers": s.get("total_layers"),
                "nozzle": temps.get("nozzle"), "nozzle_target": temps.get("nozzle_target"),
                "bed": temps.get("bed"), "bed_target": temps.get("bed_target"),
                "wifi": s.get("wifi_signal"), "wired": bool(s.get("wired_network")),
                "sdcard": s.get("sdcard"), "door_open": bool(s.get("door_open")),
            }
        out.append(item)
    return out


_SECRET = ("access_code", "password", "token", "secret", "api_key", "apikey", "private_key", "auth")


def redact(v):
    """Remove secrets (printer access codes, keys, tokens) from anything Bambuddy returns."""
    if isinstance(v, dict):
        return {k: ("•••" if any(x in str(k).lower() for x in _SECRET) else redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


def everything(cfg: dict | None = None) -> dict:
    """Printer status tab: all Bambuddy reports about each printer (record, full live status, its queue)
    plus Bambuddy's system info — read only, secrets removed. Each part is fetched on its own, so one
    failing part shows its error instead of hiding the rest."""
    cfg = cfg or load_config()

    def get(path):
        try:
            return api("GET", path, cfg=cfg, timeout=10)
        except FarmError as e:
            return {"error": str(e)}

    out = {"fetched_at": time.time(), "bambuddy_url": cfg["bambuddy_url"], "system": redact(get("/system/info")),
           "printers": []}
    cmap = color_names(cfg)
    plist = get("/printers/")
    if isinstance(plist, dict):  # error
        out["error"] = plist.get("error", "")
        return out
    for p in plist or []:
        pid = int(p["id"])
        status = get(f"/printers/{pid}/status")
        _status_cache[pid] = (time.time(), status if "error" not in status else {"connected": False, **status})
        queue = get(f"/queue/?printer_id={pid}")
        out["printers"].append({"id": pid, "name": p.get("name") or f"Printer {pid}",
                                "summary": summarize(p, status, queue, cmap), "printer": redact(p),
                                "status": redact(status), "queue": redact(queue)})
    return out


SPEEDS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
SEVERITY = {1: "Error — print stopped", 2: "Warning — print paused", 3: "Notice"}
STATES = {"IDLE": "Idle", "RUNNING": "Printing", "PAUSE": "Paused", "FINISH": "Finished", "FAILED": "Failed",
          "PREPARE": "Preparing", "SLICING": "Slicing", "INIT": "Starting"}


def summarize(p: dict, s: dict, queue, cmap: dict) -> dict:
    """The parts of Bambuddy's printer data a person needs, in plain words (Printer status page)."""
    s = s if isinstance(s, dict) else {}
    t = s.get("temperatures") or {}
    state = str(s.get("state") or "").upper()
    units = []
    for u in s.get("ams") or []:
        units.append({"name": f"AMS {int(u.get('id', 0)) + 1}" + (" (AMS HT)" if u.get("is_ams_ht") else ""),
                      "humidity": u.get("humidity"), "temp": u.get("temp"),
                      "drying": bool(u.get("dry_time")), "dry_minutes_left": u.get("dry_time") or 0})
    items = queue if isinstance(queue, list) else []
    return {
        "connected": bool(s.get("connected")) and "error" not in s, "error": s.get("error", ""),
        "state": STATES.get(state, state.title() or "Unknown"), "raw_state": state,
        "location": p.get("location") or "", "model": p.get("model") or "", "ip": p.get("ip_address") or "",
        "serial": p.get("serial_number") or "", "firmware": s.get("firmware_version"),
        "developer_mode": s.get("developer_mode"),
        "job": {"file": s.get("subtask_name") or s.get("current_print") or s.get("gcode_file") or "",
                "progress": s.get("progress"), "remaining_min": s.get("remaining_time"),
                "layer": s.get("layer_num"), "layers": s.get("total_layers"), "stage": s.get("stg_cur_name") or "",
                "speed": SPEEDS.get(s.get("speed_level"), "")} if state in ("RUNNING", "PAUSE", "PREPARE") else None,
        "awaiting_plate_clear": bool(s.get("awaiting_plate_clear")),
        "temps": {"nozzle": t.get("nozzle"), "nozzle_target": t.get("nozzle_target"), "bed": t.get("bed"),
                  "bed_target": t.get("bed_target"), "chamber": t.get("chamber")},
        "fans": {"part": s.get("cooling_fan_speed"), "aux": s.get("left_aux_fan_speed") or s.get("big_fan1_speed"),
                 "heatbreak": s.get("heatbreak_fan_speed")},
        "spools": spools(s, cmap), "ams_units": units,
        "nozzles": [f"{n.get('nozzle_diameter') or '?'} mm {str(n.get('nozzle_type') or '').replace('_', ' ')}".strip()
                    for n in s.get("nozzles") or []],
        "errors": [{"code": e.get("code"), "severity": SEVERITY.get(e.get("severity"), "Message"),
                    "actions": e.get("actions") or []} for e in s.get("hms_errors") or []],
        "print_error": s.get("print_error") or 0,
        "network": {"wired": bool(s.get("wired_network")), "wifi_dbm": s.get("wifi_signal")},
        "sdcard": s.get("sdcard"), "door_open": bool(s.get("door_open")), "timelapse": bool(s.get("timelapse")),
        "camera": bool(s.get("ipcam")),
        "ai": {k: v for k, v in (s.get("print_options") or {}).items() if isinstance(v, bool) and v},
        "queue": [{"id": q.get("id"), "status": q.get("status"), "held": bool(q.get("manual_start")),
                   "file": q.get("library_file_name") or q.get("file_name") or q.get("archive_name")
                   or (q.get("library_file") or {}).get("filename") or f"item {q.get('id')}"} for q in items
                  if isinstance(q, dict)],
    }


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


# ---------------- slicing (Bambuddy's slicer only) ----------------
def bambuddy_presets(cfg: dict | None = None) -> dict:
    """Bambuddy's slicer presets for the Slice menu: {printer|process|filament: [{key, name, source}]}."""
    r = api("GET", "/slicer/presets", cfg=cfg, timeout=20) or {}
    out: dict[str, list[dict]] = {"printer": [], "process": [], "filament": []}
    for tier in ("local", "orca_cloud", "cloud", "standard"):  # Bambuddy's priority order
        for slot in out:
            for p in (r.get(tier) or {}).get(slot) or []:
                out[slot].append({"key": f"{p.get('source', tier)}:{p['id']}", "name": p.get("name") or p["id"],
                                  "source": p.get("source", tier)})
    return out


def chosen_presets(cfg: dict | None = None, available: dict | None = None, model: str = "",
                   printer_id: int | None = None) -> dict:
    """Bambuddy's slicer: the presets set for a printer model in Slicing (no built-in defaults)."""
    cfg = cfg or load_config()
    return dict((cfg.get("presets_by_model") or {}).get(model or "") or {})


def set_presets(keys: dict, model: str = "", clear: bool = False, printer_id: int | None = None) -> dict:
    """Slicing (Bambuddy's slicer): save the printer / process / filament preset ("source:id" from
    Bambuddy's list) for a printer model; `clear` removes them."""
    cfg = load_config()
    if clear:
        cfg.setdefault("presets_by_model", {}).pop(model, None)
        save_config(cfg)
        return {}
    avail = bambuddy_presets()
    chosen = {}
    for slot in PRESET_SLOTS:
        k = str(keys.get(slot) or "")
        hit = next((p for p in avail[slot] if p["key"] == k), None)
        if hit is None:
            raise FarmError(f"Choose a {slot} preset from Bambuddy's list")
        src, pid = k.split(":", 1)
        chosen[slot] = {"source": src, "id": pid, "name": hit["name"]}
    cfg.setdefault("presets_by_model", {})[model] = chosen
    save_config(cfg)
    return chosen


LOCAL = ("orca", "bambu")  # slicers run on this computer


def engine(cfg: dict | None = None) -> str:
    """Which slicer slices: the one chosen in Slicing, else OrcaSlicer when installed, else Bambu Studio
    (where its command line works), else Bambuddy's."""
    from . import slicer

    cfg = cfg or load_config()
    if cfg.get("slicer") in ("orca", "bambu", "bambuddy"):
        return cfg["slicer"]
    if slicer.exe_path("orca"):
        return "orca"
    if slicer.exe_path("bambu") and slicer.works_here("bambu"):
        return "bambu"
    return "bambuddy"


def orca_presets_for(model: str, cfg: dict | None = None, printer_id: int | None = None) -> dict[str, str]:
    """Preset names a slicer on this computer uses for a printer model — the ones set in Slicing. No
    built-in defaults: a model without its presets isn't sliced (see `missing_presets`)."""
    cfg = cfg or load_config()
    return {k: v for k, v in ((cfg.get("orca_presets_by_model") or {}).get(model or "") or {}).items() if v}


def set_orca_presets(names: dict, model: str = "", clear: bool = False, printer_id: int | None = None) -> dict[str, str]:
    """Slicing: save the printer / process / filament presets for a printer model (every printer of that
    model slices with them); `clear` removes them."""
    from . import slicer

    cfg = load_config()
    if clear:
        cfg.setdefault("orca_presets_by_model", {}).pop(model, None)
    else:
        avail = slicer.list_presets()
        chosen = {}
        for slot in PRESET_SLOTS:
            name = str(names.get(slot) or "")
            if not any(p["name"] == name for p in avail[slot]):
                raise FarmError(f"Choose a {slot} preset from the list")
            chosen[slot] = name
        cfg.setdefault("orca_presets_by_model", {})[model] = chosen
    save_config(cfg)
    return orca_presets_for(model, cfg)


def presets_for_model(model: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    return orca_presets_for(model, cfg) if engine(cfg) in LOCAL else chosen_presets(cfg, None, model)


def missing_presets(printer_id: int | None, cfg: dict | None = None) -> str | None:
    """The printer model whose slicing presets aren't set yet (None = all set)."""
    model = WORKER.model_of(printer_id) if printer_id else ""
    p = presets_for_model(model, cfg)
    return None if all(p.get(s) for s in PRESET_SLOTS) else (model or "these printers")


def target(printer_id: int | None, cfg: dict | None = None) -> tuple[str, dict]:
    """What a plate is sliced with for a printer: (key, presets) — the presets of the printer's model. The
    key is the slicer + those presets, so printers of the same model share one slice, and changing a
    model's presets means a new slice."""
    import hashlib

    cfg = cfg or load_config()
    eng = engine(cfg)
    model = WORKER.model_of(printer_id) if printer_id else ""
    presets = presets_for_model(model, cfg)
    key = eng + ":" + hashlib.sha1(json.dumps(presets, sort_keys=True).encode()).hexdigest()[:12]
    return key, presets


def print_defaults(printer_id: int | None, cfg: dict | None = None) -> dict:
    """A printer's print defaults (Slicing): {speed, bed_levelling, flow_cali, timelapse}; "" = Bambuddy's."""
    cfg = cfg or load_config()
    return dict((cfg.get("print_defaults") or {}).get(str(printer_id)) or {}) if printer_id else {}


def set_print_defaults(printer_id: int, values: dict) -> dict:
    speed = str(values.get("speed") or "")
    if speed and speed not in SPEEDS_SET:
        raise FarmError("Speed is silent, standard, sport or ludicrous")
    out = {"speed": speed, **clean_options(values)}
    cfg = load_config()
    cfg.setdefault("print_defaults", {})[str(printer_id)] = out
    save_config(cfg)
    return out


def import_presets(filename: str, data: bytes) -> dict:
    """Add preset files to Bambuddy (Bambu Studio / OrcaSlicer exports: .json, .bbscfg, .bbsflmt,
    .orca_filament, .zip). Bambuddy sorts them into printer / process / filament presets."""
    if not data:
        raise FarmError("The file is empty")
    if not filename.lower().endswith((".json", ".bbscfg", ".bbsflmt", ".orca_filament", ".zip")):
        raise FarmError("Choose a preset file exported from Bambu Studio or OrcaSlicer "
                        "(.json, .bbscfg, .bbsflmt, .orca_filament or .zip)")
    r = api("POST", "/local-presets/import", files=[("file", filename, data, "application/octet-stream")], timeout=60) or {}
    return {"imported": int(r.get("imported") or 0), "skipped": int(r.get("skipped") or 0),
            "errors": [str(e) for e in r.get("errors") or []]}


def printer_models(prs: list[dict]) -> list[str]:
    """Printer models in the farm, most printers first (the first one is what plates are sliced for
    when no printer is chosen yet)."""
    count: dict[str, int] = {}
    for p in prs:
        if p.get("model"):
            count[p["model"]] = count.get(p["model"], 0) + 1
    return sorted(count, key=lambda m: (-count[m], m))


def slice_request(presets: dict, color: str | None) -> dict:
    missing = [s for s in ("printer", "process", "filament") if s not in presets]
    if missing:
        raise FarmError(f"Choose the {', '.join(missing)} preset in Make It → Slice")
    ref = lambda s: {"source": presets[s]["source"], "id": presets[s]["id"]}  # noqa: E731
    body = {"printer_preset": ref("printer"), "process_preset": ref("process"), "filament_presets": [ref("filament")],
            "export_3mf": True, "plate": 1}
    if color in COLOR_HEX:
        body["filament_colours"] = [COLOR_HEX[color]]
    return body


def _slice_set(slice_id: int, **cols) -> None:
    cols["updated_at"] = db.now()
    with db.tx() as con:
        con.execute(f"UPDATE slices SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), slice_id))


def latest_slices(batch_id: int | None = None) -> dict[tuple[int, str, str], dict]:
    """(batch, plate, target key) → its newest slice (see `target`)."""
    with db.tx() as con:
        rs = db.rows(con, "SELECT * FROM slices WHERE computer_id=?" + (" AND batch_id=?" if batch_id else "") +
                     " ORDER BY id", computer.ID, *([batch_id] if batch_id else []))
    return {(r["batch_id"], r["plate"], r.get("model") or ""): r for r in rs}


def slice_for(slices: dict, batch_id: int, plate: str, key: str) -> dict | None:
    return slices.get((batch_id, plate, key or ""))


def request_slices(plates: list[tuple[int, str, str, str]], printer_ids: list[int | None] | None = None,
                   again: bool = False, skip_failed: bool = False) -> int:
    """Slice these plates [(batch_id, plate, kind, color)] for these printers (their presets; None = the
    default), one at a time in the background. Skipped: being sliced, already sliced with those presets
    (unless `again`), failed (with `skip_failed` — automatic slicing never retries a failure by itself)."""
    cfg = load_config()
    cur = latest_slices()
    eng = engine(cfg)
    targets = {}
    for pid in (printer_ids or [None]):
        key, presets = target(pid, cfg)
        if not all(presets.get(s) for s in PRESET_SLOTS):
            continue  # this printer model has no slicing presets yet (Slicing) — nothing is guessed
        targets[key] = presets
    n = 0
    now = db.now()
    with db.tx() as con:
        for bid, plate, kind, color in plates:
            for key, presets in targets.items():
                s = cur.get((bid, plate, key))
                if s and s["state"] in ("queued", "uploading", "slicing") or (s and s["state"] == "done" and not again) \
                        or (s and s["state"] == "failed" and skip_failed):
                    continue
                con.execute("INSERT INTO slices(batch_id,plate,kind,color,model,engine,presets,state,created_at,"
                            "updated_at,computer_id) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)",
                            (bid, plate, kind, color or "", key, eng, json.dumps(presets), now, now, computer.ID))
                n += 1
    WORKER.kick()
    return n


def _percent(snap) -> float | None:
    """A percentage from Bambuddy's slice progress snapshot (its keys vary by slicer version)."""
    if isinstance(snap, (int, float)):
        return float(snap) if snap > 1 else float(snap) * 100
    if isinstance(snap, dict):
        for k in ("percent", "total_percent", "overall_percent", "progress", "pct"):
            v = snap.get(k)
            if isinstance(v, (int, float)):
                return float(v) if v > 1 else float(v) * 100
    return None


def slicer_hint(err: str) -> str:
    """Plain English for Bambuddy's slicer errors."""
    low = (err or "").lower()
    if "unreachable" in low or "sidecar" in low or "connection refused" in low:
        return ("Bambuddy's slicer isn't running: in Bambuddy open Settings → Slicer, start its slicer service "
                "(Docker) and turn on Use Slicer API. Details: " + err)
    return err


# ---------------- plates of a batch → printers ----------------
def needed_color(plate: dict) -> str:
    return BASE_COLOR if plate["kind"] == "bases" else _norm((plate.get("colors") or [""])[0])


def color_match(need: str, colors: list[str]) -> tuple[int, str]:
    """2 = exactly that colour loaded; 1 = same family ('Jade White' for white, 'Hot Pink' for pink);
    0 = not loaded. Also returns the loaded colour that matched."""
    need = _norm(need)
    for c in colors:
        if c == need:
            return 2, c
    fam = need.split()[-1] if need else ""
    for c in colors:
        if fam and c.split() and c.split()[-1] == fam:
            return 1, c
    return 0, ""


def eligible(plate: dict, printer: dict) -> str:
    """'' if the printer can print this plate (the plate's colour loaded: white for bases), else why not."""
    st = printer.get("status") or {}
    if st and not st.get("connected"):
        return "offline"
    need = needed_color(plate)
    q, _ = color_match(need, printer.get("colors") or [])
    if not q:
        loaded = ", ".join(printer.get("colors") or []) or "no filament reported"
        return f"has {loaded} (needs {need})"
    return ""


def match_note(plate: dict, printer: dict) -> str:
    """For a close (not exact) colour: which filament would be used."""
    q, c = color_match(needed_color(plate), printer.get("colors") or [])
    return f"loaded: {c}" if q == 1 else ""


def _busy(printer: dict) -> int:
    s = printer.get("status") or {}
    return int(s.get("state", "").upper() in BUSY_STATES or bool(s.get("awaiting_plate_clear")))


def suggest(plates: list[dict], prs: list[dict], active_jobs: dict[int, int]) -> dict[str, int | None]:
    """Plate file → suggested printer id: the plate's colour loaded (exact before close), right job,
    least busy (spread across plates)."""
    load = {p["id"]: active_jobs.get(p["id"], 0) + _busy(p) for p in prs}
    out: dict[str, int | None] = {}
    for pl in plates:
        if pl.get("job_active"):
            continue
        ok = [p for p in prs if not eligible(pl, p) and p.get("active", True)]
        if not ok:
            out[pl["file"]] = None
            continue
        need = needed_color(pl)
        best = min(ok, key=lambda p: (-color_match(need, p.get("colors") or [])[0], load[p["id"]], p["name"]))
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


# Print options a person can choose per job ("" / missing = Bambuddy's default)
OPTIONS = {"bed_levelling": ("auto", "on", "off"), "flow_cali": ("auto", "on", "off"), "timelapse": (False, True)}


def clean_options(o: dict | None) -> dict:
    out = {}
    for k, allowed in OPTIONS.items():
        v = (o or {}).get(k)
        if v in ("", None):
            continue
        if allowed == (False, True):
            v = v in (True, "true", "on", 1, "1")
        elif v not in allowed:
            raise FarmError(f"{k}: choose {', '.join(allowed)}")
        out[k] = v
    return out


def print_job(job_id: int, printer_id: int, options: dict | None, prs: list[dict]) -> dict:
    """Printers page → Print: send this job's plate to the chosen printer with these options.
    Waiting to send → just changed; waiting in Bambuddy's queue → moved there; failed / cancelled /
    printed → printed again as a new job. A plate that is printing can't be changed. Nothing here
    starts a print: every job waits in Bambuddy until a person presses Start (`start_job`)."""
    opts = clean_options(options)
    with db.tx() as con:
        j = con.execute("SELECT * FROM print_jobs WHERE id=? AND computer_id=?", (job_id, computer.ID)).fetchone()
    if j is None:
        raise FarmError("No such print job")
    j = dict(j)
    printer = next((p for p in prs if p["id"] == int(printer_id)), None)
    if printer is None:
        raise FarmError(f"Printer {printer_id} isn't in Bambuddy")
    plate = {"kind": j["kind"], "colors": [j["color"]] if j["color"] else [], "file": j["plate"]}
    why = eligible(plate, printer)
    if why:
        raise FarmError(f"{printer['name']} {why}")
    if j["state"] in ("slicing", "uploading", "printing"):
        raise FarmError(f"It's {j['state']} — wait until it's finished")
    if j["state"] == "queued":
        _set(job_id, printer_id=printer["id"], printer_name=printer["name"], options=json.dumps(opts))
        WORKER.kick()
        return {"job_id": job_id, "moved": True}
    if j["state"] == "sent":
        api("PATCH", f"/queue/{j['queue_item_id']}", json_body={"printer_id": printer["id"], **opts})
        _set(job_id, printer_id=printer["id"], printer_name=printer["name"], options=json.dumps(opts), error=None)
        return {"job_id": job_id, "moved": True}
    now = db.now()
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,options,created_at,updated_at,computer_id) "
            "VALUES (?,?,?,?,?,?,'queued',?,?,?,?)",
            (j["batch_id"], j["plate"], j["kind"], j["color"], printer["id"], printer["name"], json.dumps(opts), now, now,
             computer.ID))
    WORKER.kick()
    return {"job_id": int(cur.lastrowid), "moved": False}


class StartBlocked(FarmError):
    """Bambuddy wants a confirmation before starting (e.g. not enough filament on the spool)."""


def start_job(job_id: int, force: bool = False) -> None:
    """A person pressed Start: Bambuddy may print it once the printer is free and its bed is cleared."""
    with db.tx() as con:
        j = con.execute("SELECT * FROM print_jobs WHERE id=? AND computer_id=?", (job_id, computer.ID)).fetchone()
    if j is None:
        raise FarmError("No such print job")
    if j["state"] != "sent" or not j["queue_item_id"]:
        raise FarmError("Only a plate waiting in Bambuddy's queue can be started")
    cfg = load_config()
    base = cfg["bambuddy_url"].rstrip("/") + "/api/v1"
    q = "?skip_filament_check=true" if force else ""
    st, data = _call(base, "POST", f"/queue/{j['queue_item_id']}/start{q}", key=cfg["api_key"])
    if st == 409:
        raise StartBlocked(f"Bambuddy: {_detail(st, data)}")
    if st >= 400:
        raise FarmError(f"Bambuddy: {_detail(st, data)}")
    _set(job_id, held=0, error=None)


def job_choices(job: dict, prs: list[dict]) -> list[dict]:
    plate = {"kind": job["kind"], "colors": [job["color"]] if job.get("color") else [], "file": job["plate"]}
    return [{"id": p["id"], "name": p["name"], "why_not": eligible(plate, p), "note": match_note(plate, p),
             "state": (p.get("status") or {}).get("state", "")} for p in prs]


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
    AUTO_SLICE_S = 30

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: int | None = None
        self.last_refresh = 0.0
        self.last_auto = 0.0
        self._models: tuple[float, dict[int, str]] = (0.0, {})
        self.log: list[str] = []

    def start(self) -> None:
        with db.tx() as con:  # work cut off by a restart starts over (an upload never became a queue item)
            con.execute("UPDATE print_jobs SET state='queued', updated_at=? WHERE state IN ('slicing','uploading') "
                        "AND computer_id=?", (db.now(), computer.ID))
            con.execute("UPDATE slices SET state='queued', updated_at=? WHERE (state='uploading' OR (state='slicing' "
                        "AND slice_job_id IS NULL)) AND computer_id=?", (db.now(), computer.ID))
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
                if time.time() - self.last_auto > self.AUTO_SLICE_S:
                    self.auto_slice()
                self.poll_slices()
                self.slice_next()
                self.check_waiting()
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
        if sliced is not None:  # a sliced file in the batch folder: upload it as it is
            data = sliced.read_bytes()
            check_sliced(data)
            _set(j["id"], state="uploading", error=None,
                 sliced=str(sliced.relative_to(out)) if out in sliced.parents else str(sliced))
            label = Path(b["out_dir"]).name  # "Batch 2 - 2026-11-16"
            kind = "Bases" if j["kind"] == "bases" else f"Names {j['color']}".strip()
            up = api("POST", "/library/files", cfg=cfg, timeout=300,
                     files=[("file", _upload_name(label, kind, sliced.name), data, "application/octet-stream")])
            file_id = int(up["id"])
        else:  # sliced for this printer's model, or slice it now and send it after
            missing = missing_presets(j["printer_id"], cfg)
            if missing:
                raise FarmError(f"Set the slicing presets for {missing} printers in Slicing, then Start print again")
            key, _ = target(j["printer_id"], cfg)  # sliced with this printer model's presets
            s = slice_for(latest_slices(j["batch_id"]), j["batch_id"], j["plate"], key)
            if s and s["state"] == "done" and s.get("sliced_path") and Path(s["sliced_path"]).is_file():
                data = Path(s["sliced_path"]).read_bytes()  # sliced by OrcaSlicer on this computer
                check_sliced(data)
                _set(j["id"], state="uploading", sliced=str(s["sliced_path"]))
                label = Path(b["out_dir"]).name
                kind = "Bases" if j["kind"] == "bases" else f"Names {j['color']}".strip()
                up = api("POST", "/library/files", cfg=cfg, timeout=300,
                         files=[("file", _upload_name(label, kind, Path(s["sliced_path"]).name), data,
                                 "application/octet-stream")])
                file_id = int(up["id"])
            elif s and s["state"] == "done" and s["sliced_file_id"]:
                file_id = int(s["sliced_file_id"])
                _set(j["id"], sliced=f"Bambuddy library #{file_id}")
            elif s and s["state"] == "failed" and s["created_at"] >= j["created_at"]:
                raise FarmError(f"Slicing failed: {s['error']}")
            else:
                if not (s and s["state"] in ("queued", "uploading", "slicing")):
                    request_slices([(j["batch_id"], j["plate"], j["kind"], j["color"])], [j["printer_id"]], again=True)
                _set(j["id"], state="slicing", error=None)  # sent once Bambuddy has sliced it
                return
        # Held (manual_start): Bambuddy never starts it by itself — a person presses Start.
        opts = clean_options(json.loads(j.get("options") or "{}"))
        item = api("POST", "/queue/", cfg=cfg, json_body={"library_file_id": file_id, "printer_id": j["printer_id"],
                                                         "manual_start": True, **opts})
        qid = int((item or {})["id"])
        _set(j["id"], state="sent", file_id=file_id, queue_item_id=qid, held=1, error=None)
        self._log(f"{Path(j['plate']).name} → in {j['printer_name']}'s queue, waiting for Start")
        if j.get("autostart"):  # a person pressed Start print (Make It): release it now that it's queued
            try:
                start_job(j["id"])
                self._log(f"{Path(j['plate']).name} → started on {j['printer_name']}")
            except StartBlocked as e:
                _set(j["id"], error=f"{e} — press Start print again to start anyway")
            except FarmError as e:
                _set(j["id"], error=f"Queued, but not started: {e}")

    # ---- slicing with Bambuddy (one plate at a time) ----
    def slice_next(self) -> None:
        with db.tx() as con:
            if con.execute("SELECT 1 FROM slices WHERE state IN ('uploading','slicing') AND computer_id=?",
                           (computer.ID,)).fetchone():
                return
            s = con.execute("SELECT * FROM slices WHERE state='queued' AND computer_id=? ORDER BY id LIMIT 1",
                            (computer.ID,)).fetchone()
            b = con.execute("SELECT out_dir FROM batches WHERE id=?", (s["batch_id"],)).fetchone() if s else None
        if s is None:
            return
        s = dict(s)
        eng = s.get("engine") or engine()
        if eng in LOCAL:
            if not b or not b["out_dir"] or not (Path(b["out_dir"]) / s["plate"]).is_file():
                _slice_set(s["id"], state="failed", error="The plate file is gone")
                return
            names = json.loads(s.get("presets") or "{}") or orca_presets_for("")
            _slice_set(s["id"], state="slicing", progress=0, started_at=db.now(), error=None)
            threading.Thread(target=self._local, args=(s, Path(b["out_dir"]), names, eng), daemon=True,
                             name="local-slice").start()
            return
        try:
            if not b or not b["out_dir"] or not (Path(b["out_dir"]) / s["plate"]).is_file():
                raise FarmError("The plate file is gone")
            cfg = load_config()
            saved = json.loads(s.get("presets") or "{}")
            presets = saved if all(isinstance(v, dict) for v in saved.values()) and saved else chosen_presets(cfg, bambuddy_presets(cfg))
            body = slice_request(presets, _slice_color(s) or None)
            _slice_set(s["id"], state="uploading", error=None, presets=json.dumps(presets))
            src = Path(b["out_dir"]) / s["plate"]
            kind = "Bases" if s["kind"] == "bases" else f"Names {s['color']}".strip()
            name = _upload_name(Path(b["out_dir"]).name, kind, src.name, suffix=".3mf")
            up = api("POST", "/library/files", cfg=cfg, timeout=300,
                     files=[("file", name, src.read_bytes(), "application/octet-stream")])
            r = api("POST", f"/library/files/{int(up['id'])}/slice", cfg=cfg, json_body=body, timeout=60)
            _slice_set(s["id"], state="slicing", source_file_id=int(up["id"]), slice_job_id=int(r["job_id"]),
                       progress=0, started_at=db.now())
            self._log(f"Slicing {src.name} in Bambuddy" + (f" for {s['model']}" if s.get("model") else ""))
        except FarmError as e:
            _slice_set(s["id"], state="failed", error=slicer_hint(str(e)))
            self._log(f"Slicing {Path(s['plate']).name}: {e}")

    def _local(self, s: dict, out_dir: Path, names: dict, eng: str = "orca") -> None:
        """One plate with OrcaSlicer / Bambu Studio on this computer (own thread: printing never waits)."""
        from . import slicer

        folder = re.sub(r"[^A-Za-z0-9._-]", "_", (s.get("model") or "any").split(":")[-1])  # the presets' key
        dst = out_dir / "Sliced" / folder / (_stem(s["plate"]) + ".gcode.3mf")
        try:
            slicer.slice_plate(out_dir / s["plate"], dst, names, COLOR_HEX.get(_slice_color(s)), timeout=SLICE_TIMEOUT_S,
                               engine=eng)
            _slice_set(s["id"], state="done", progress=100, sliced_path=str(dst), error=None)
            self._log(f"Sliced {Path(s['plate']).name} with {slicer.NAMES.get(eng, eng)} ({names.get('printer', '')})")
        except Exception as e:  # noqa: BLE001 — SlicerError, or anything unexpected: the slice fails, visibly
            _slice_set(s["id"], state="failed", error=str(e))
            self._log(f"Slicing {Path(s['plate']).name}: {e}")
        self.kick()

    def poll_slices(self) -> None:
        with db.tx() as con:
            rs = db.rows(con, "SELECT * FROM slices WHERE state='slicing' AND slice_job_id IS NOT NULL AND computer_id=?",
                         computer.ID)
        if not rs:
            return
        cfg = load_config()
        for s in rs:
            try:
                r = api("GET", f"/slice-jobs/{s['slice_job_id']}", cfg=cfg, timeout=8) or {}
            except FarmError as e:
                if "not found" in str(e).lower() or "expired" in str(e).lower():
                    _slice_set(s["id"], state="failed", error="Bambuddy lost this slice (restarted?) — slice it again")
                continue
            st = r.get("status")
            if st == "completed":
                fid = (r.get("result") or {}).get("library_file_id")
                if fid:
                    _slice_set(s["id"], state="done", sliced_file_id=int(fid), progress=100, error=None)
                    self._log(f"Sliced {Path(s['plate']).name}")
                else:
                    _slice_set(s["id"], state="failed", error="Bambuddy finished but returned no sliced file")
            elif st == "failed":
                _slice_set(s["id"], state="failed",
                           error=slicer_hint(str(r.get("error_detail") or r.get("error_status") or "slicing failed")))
            else:
                snap = r.get("progress")
                key = json.dumps(snap, sort_keys=True, default=str) if snap is not None else None
                if key is not None and key != s.get("last_progress"):  # any change = still working
                    _slice_set(s["id"], last_progress=key, progress=_percent(snap) if _percent(snap) is not None else s["progress"])
                elif time.time() - (s["updated_at"] or s["started_at"] or 0) > SLICE_TIMEOUT_S:
                    _slice_set(s["id"], state="failed",
                               error=f"Bambuddy's slicer made no progress for {SLICE_TIMEOUT_S // 60} min — check "
                                     "Bambuddy → Settings → Slicer, then slice again")

    def check_waiting(self) -> None:
        """Print jobs waiting for their slice: sliced → send; slice failed → the job fails with why."""
        with db.tx() as con:
            js = db.rows(con, "SELECT * FROM print_jobs WHERE state='slicing' AND computer_id=?", computer.ID)
        if not js:
            return
        sl = latest_slices()
        for j in js:
            s = slice_for(sl, j["batch_id"], j["plate"], target(j["printer_id"])[0])
            if s is None or s["state"] == "done":
                _set(j["id"], state="queued")
            elif s["state"] == "failed":
                _set(j["id"], state="failed", error=f"Slicing failed: {s['error']}")

    def model_of(self, printer_id: int) -> str:
        """A printer's model (cached a minute)."""
        if time.time() - self._models[0] > 60 or printer_id not in self._models[1]:
            try:
                self._models = (time.time(), {p["id"]: p.get("model") or "" for p in printers(with_status=False)})
            except FarmError:
                pass
        return self._models[1].get(printer_id, "")

    def auto_slice(self) -> None:
        """Slice new Make It plates for every printer model in the farm (when on). Never retries a failure."""
        self.last_auto = time.time()
        cfg = load_config()
        if not cfg.get("auto_slice", True) or not cfg["api_key"] and not status(cfg)["connected"]:
            return
        from . import makeit

        try:
            prs = printers(cfg)
        except FarmError:
            return
        n = 0
        for r in makeit.waiting_plates(prs):  # for the printer pre-selected for each plate (or its job's)
            pid = r.get("suggested") or ((r.get("job") or {}).get("printer_id") or None)
            if pid:
                n += request_slices([(r["batch_id"], r["file"], r["kind"], (r["colors"] or [""])[0])], [pid],
                                    skip_failed=True)
        if n:
            self._log(f"Slicing {n} plate(s) automatically for their pre-selected printers")

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
            held = 1 if (item or {}).get("manual_start") and new == "sent" else 0
            if held != (j.get("held") or 0):
                changes["held"] = held
            if new != j["state"]:
                changes["state"] = new
                if new == "failed":
                    changes["error"] = (item or {}).get("error_message") or "The print failed (see Bambuddy)"
                if new == "done":
                    changes["progress"] = 100.0
            if new == "printing" and j.get("speed") and not j.get("speed_set"):
                try:  # the speed chosen in Make It, once the print has started
                    api("POST", f"/printers/{j['printer_id']}/print-speed?mode={int(j['speed'])}", cfg=cfg)
                    changes["speed_set"] = 1
                except FarmError as e:
                    changes["error"] = f"Print speed not set: {e}"
            if new == "printing":
                s = printer_status(j["printer_id"], cfg, max_age=10)
                if s.get("progress") is not None:
                    changes["progress"] = float(s["progress"])
            if changes:
                _set(j["id"], **changes)
                if changes.get("state") == "done":
                    from . import makeit

                    makeit.update_made(j["batch_id"])  # both parts printed → Ready to ship


def _slice_color(s: dict) -> str:
    """Filament colour a plate is sliced with: white for bases (whatever names are on it), else the name colour."""
    return BASE_COLOR if s.get("kind") == "bases" else (s.get("color") or "")


def _upload_name(label: str, kind: str, name: str, suffix: str = ".gcode.3mf") -> str:
    """File name in Bambuddy and on the printer's SD card (no characters FAT cards refuse; ≤ 200 bytes)."""
    body = f"{label} - {kind} - {name[:-len(suffix)] if name.endswith(suffix) else name}"
    for ch in '<>:"/\\|?*':
        body = body.replace(ch, "_")
    while len((body + suffix).encode()) > 200:
        body = body[:-1]
    return body.rstrip(" .") + suffix


WORKER = Worker()
