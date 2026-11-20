"""Print farm: finished plates go to Bambuddy, the open-source print-farm manager.

SnapSTL never talks to a printer itself. Bambuddy (on the Windows Bambuddy computer, set up by
scripts/windows/bambuddy-server.ps1 — its address in Settings → Connections) holds the printer
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

Printers only print *sliced* plates. Slicing is done only by Bambuddy's slicer (its slicer sidecar, e.g.
in Docker on another computer), with the profiles stored in Bambuddy that the Slicing page picks for the
model of the printer the plate goes to (`target`). SnapSTL stores no slicing settings of its own: only
which Bambuddy profile (source + id) each printer model uses. The sliced file stays in Bambuddy's library.
Slices run in the background one at a time and never block printing.

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
    # Which Bambuddy profiles each printer slices with (Slicing) — references only, never their content:
    # Bambuddy printer id (str) → {"printer"|"process"|"filament": {source, id, name}}
    "presets_by_printer": {},
    # before 0.22.7, per printer model (e.g. "A1"): still used for a printer with no profiles of its own
    "presets_by_model": {},
    "print_defaults": {},         # Bambuddy printer id (str) → {speed, bed_levelling, flow_cali, timelapse, plate_type}
    # on the Windows Bambuddy computer: start Bambuddy / Docker / the slicer service when they stop (bambuddy_host)
    "keep_bambuddy_running": True,
}
PRESET_SLOTS = ("printer", "process", "filament")
SPEEDS_SET = {"silent": 1, "standard": 2, "sport": 3, "ludicrous": 4}  # print speed per job (Make It)
# Build plates a printer can slice for (Bambuddy's `bed_type`: BambuStudio / OrcaSlicer `curr_bed_type`;
# Textured Cool Plate is OrcaSlicer only). "" = what the process profile says (often Cool Plate).
PLATE_TYPES = ("Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate", "Supertack Plate",
               "Textured Cool Plate")
SPEED_TRIES = 5  # speed commands sent per print until the printer reports that speed
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
                            "(Queue, Read status, Library, Control printer — not Allow cloud access: SnapSTL "
                            "uses no cloud profiles) and paste it here")
        # Login off: Bambuddy refuses can_access_cloud on such a key ("requires authentication") — and
        # doesn't need it: its own Bambu Cloud sign-in serves every caller, SnapSTL included.
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


def refresh_from_bambuddy(cfg: dict | None = None, wait_s: float = 2.0) -> dict:
    """A person pressed Refresh: Bambuddy asks every printer for a full status report (its "pushall" — AMS
    slots, colours, filament left), then SnapSTL drops its cached status, colour names and printer list."""
    global _color_map
    cfg = cfg or load_config()
    asked, failed = 0, []
    for p in api("GET", "/printers/", cfg=cfg) or []:
        try:
            api("POST", f"/printers/{int(p['id'])}/refresh-status", cfg=cfg, timeout=8)
            asked += 1
        except FarmError as e:
            failed.append(f"{p.get('name') or p['id']}: {e}")
    if asked and wait_s:
        time.sleep(wait_s)  # the printers answer within a second or two
    _status_cache.clear()
    _color_map = (0.0, {})
    WORKER._models = (0.0, {})
    return {"asked": asked, "failed": failed}


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


def tray_id(unit: dict | None, tray: dict) -> int:
    """Bambu's global tray id of a filament slot (what Bambuddy's `ams_mapping` takes): AMS n, slot k →
    n·4 + k; AMS HT (id 128+) → its id; external spool → 254 (255 = second one)."""
    if unit is None:
        try:
            return int(tray.get("id", 254)) if int(tray.get("id", 254)) >= 254 else 254
        except (TypeError, ValueError):
            return 254
    uid = int(unit.get("id", 0) or 0)
    return uid if uid >= 128 else uid * 4 + int(tray.get("id", 0) or 0) % 4


def spools(s: dict, cmap: dict) -> list[dict]:
    """Filament loaded in a printer, from Bambuddy's live status: AMS slots + external spool(s). `color` is
    Bambuddy's colour name (else the nearest SnapSTL colour); `tray` is the slot's global tray id."""
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
        out.append({"slot": slot, "tray": tray_id(unit, t), "hex": f"#{h.upper()}" if h else "", "name": name,
                    "material": material, "color": _norm(name or _nearest_palette(h)),
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
            item["colors"] = sorted({x["color"] for x in item["spools"] if x["color"]})
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


# ---------------- slicing (Bambuddy's slicer only) ----------------
PRESET_TIERS = ("local", "standard")  # no cloud profiles (owner decision 0.22.19, ARCHITECTURE #62)


LOCAL_ONLY_NOTICE = ("Showing your own profiles from Bambuddy. Bambuddy is signed in to Bambu Cloud and can't read that "
                     "list (a Bambuddy bug), so the slicer's standard profiles aren't shown. Slicing with your own "
                     "profiles still works.")


def _local_presets(cfg: dict | None = None) -> dict:
    """Your profiles in Bambuddy (Local Profiles), read from their own list: it works while Bambuddy's combined
    list is crashing on Bambu Cloud."""
    r = api("GET", "/local-presets/", cfg=cfg, timeout=30) or {}
    if not isinstance(r, dict):
        raise FarmError("Bambuddy's list of local profiles came back in an unexpected form — is Bambuddy up to date?")
    return {slot: [{"key": f"local:{p['id']}", "name": p.get("name") or str(p["id"]), "source": "local"}
                   for p in r.get(slot) or []] for slot in ("printer", "process", "filament")}


def preset_listing(cfg: dict | None = None, refresh: bool = False) -> tuple[dict, str]:
    """The slicer profiles SnapSTL may use, as Bambuddy lists them, and a note when the list is only partial:
    ({printer|process|filament: [{key: "source:id", name, source}]}, note) — yours imported in Bambuddy (Local
    Profiles) first, then the slicer's standard ones. Bambu Cloud / Orca Cloud profiles are never used (they made
    slicing depend on the cloud). When Bambuddy's combined list crashes, yours are read from their own list."""
    try:
        r = api("GET", "/slicer/presets" + ("?refresh=true" if refresh else ""), cfg=cfg, timeout=30) or {}
    except FarmError as e:
        if "internal server error" in str(e).lower():  # Bambuddy's own crash (its log has why); usually CLOUD_LIST_BUG
            try:
                return _local_presets(cfg), LOCAL_ONLY_NOTICE
            except FarmError:
                raise FarmError(f"Bambuddy failed listing its profiles ({e}). {CLOUD_LIST_HINT}") from e
        raise
    if not isinstance(r, dict):
        raise FarmError("Bambuddy's list of slicer profiles came back in an unexpected form — is Bambuddy up to date?")
    out: dict[str, list[dict]] = {"printer": [], "process": [], "filament": []}
    for tier in PRESET_TIERS:
        for slot in out:
            for p in (r.get(tier) or {}).get(slot) or []:
                out[slot].append({"key": f"{p.get('source', tier)}:{p['id']}", "name": p.get("name") or p["id"],
                                  "source": p.get("source", tier)})
    return out, ""


def bambuddy_presets(cfg: dict | None = None, refresh: bool = False, status: dict | None = None) -> dict:
    """The profiles of `preset_listing`, without its note."""
    return preset_listing(cfg, refresh)[0]






def chosen_presets(printer_id: int | None, cfg: dict | None = None) -> dict:
    """The Bambuddy profiles picked for a printer in Slicing (no built-in defaults). A printer with none of
    its own uses what was picked for its model before 0.22.7."""
    cfg = cfg or load_config()
    if not printer_id:
        return {}
    own = (cfg.get("presets_by_printer") or {}).get(str(printer_id))
    if own:
        return dict(own)
    return dict((cfg.get("presets_by_model") or {}).get(WORKER.model_of(printer_id)) or {})


def set_presets(keys: dict, printer_id: int, clear: bool = False) -> dict:
    """Slicing: pick the printer / process / filament profile ("source:id" from Bambuddy's list) a printer
    slices with; `clear` removes the pick. Only the reference is kept — the profile stays in Bambuddy."""
    cfg = load_config()
    if clear:
        cfg.setdefault("presets_by_printer", {}).pop(str(printer_id), None)
        model = WORKER.model_of(printer_id)  # its model's old pick would otherwise still apply
        if model and model in (cfg.get("presets_by_model") or {}):
            for pid, m in WORKER.models().items():
                if m == model and pid != printer_id and str(pid) not in cfg["presets_by_printer"]:
                    cfg["presets_by_printer"][str(pid)] = cfg["presets_by_model"][model]
            cfg["presets_by_model"].pop(model, None)
        save_config(cfg)
        return {}
    avail = bambuddy_presets(cfg)
    chosen = {}
    for slot in PRESET_SLOTS:
        k = str(keys.get(slot) or "")
        hit = next((p for p in avail[slot] if p["key"] == k), None)
        if hit is None:
            raise FarmError(f"Choose a {slot} profile from Bambuddy's list")
        src, pid = k.split(":", 1)
        chosen[slot] = {"source": src, "id": pid, "name": hit["name"]}
    cfg.setdefault("presets_by_printer", {})[str(printer_id)] = chosen
    save_config(cfg)
    return chosen


def forget_local_slicing() -> None:
    """Since 0.21 only Bambuddy's slicer slices, and SnapSTL keeps no slicing settings: drop what the
    slicers on this computer (OrcaSlicer / Bambu Studio, before 0.21) left behind — their preset picks in
    farm.json, SnapSTL's own copy of imported presets, and their unfinished slices."""
    import shutil

    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and any(k not in DEFAULTS for k in raw):
        save_config(load_config())  # writes only the settings that still exist
    shutil.rmtree(app_dir() / "slicer-presets", ignore_errors=True)
    with db.tx() as con:
        con.execute("UPDATE slices SET state='failed', error='Was for a slicer on this computer (before 0.21)', "
                    "updated_at=? WHERE COALESCE(engine,'') IN ('orca','bambu') AND state IN "
                    "('queued','uploading','slicing') AND computer_id=?", (db.now(), computer.ID))


def missing_presets(printer_id: int | None, cfg: dict | None = None) -> str | None:
    """The printer (its name) whose slicing profiles aren't picked yet (None = all picked)."""
    p = chosen_presets(printer_id, cfg)
    return None if all(p.get(s) for s in PRESET_SLOTS) else (WORKER.name_of(printer_id) or "this printer")


def target(printer_id: int | None, cfg: dict | None = None) -> tuple[str, dict]:
    """What a plate is sliced with for a printer: (key, profiles) — the profiles picked for that printer, plus
    its plate type (`"plate"`, when one is picked in Print defaults). The key names those, so printers with the
    same profiles and plate share one slice, and picking other profiles or another plate means a new slice."""
    import hashlib

    cfg = cfg or load_config()
    presets = chosen_presets(printer_id, cfg)
    plate = print_defaults(printer_id, cfg).get("plate_type") or ""
    if plate and all(presets.get(s) for s in PRESET_SLOTS):  # the plate type is sliced in, so it's part of the key
        presets["plate"] = {"name": plate}
    refs = {k: [v.get("source"), v.get("id")] if k != "plate" else v["name"] for k, v in presets.items()}
    key = "bambuddy:" + hashlib.sha1(json.dumps(refs, sort_keys=True).encode()).hexdigest()[:12]
    return key, presets


def print_defaults(printer_id: int | None, cfg: dict | None = None) -> dict:
    """A printer's print defaults (Slicing): {speed, bed_levelling, flow_cali, timelapse, plate_type}; "" =
    Bambuddy's (plate_type "" = the process profile's plate)."""
    cfg = cfg or load_config()
    return dict((cfg.get("print_defaults") or {}).get(str(printer_id)) or {}) if printer_id else {}


def set_print_defaults(printer_id: int, values: dict) -> dict:
    speed = str(values.get("speed") or "")
    if speed and speed not in SPEEDS_SET:
        raise FarmError("Speed is silent, standard, sport or ludicrous")
    plate = str(values.get("plate_type") or "")
    if plate and plate not in PLATE_TYPES:
        raise FarmError(f"Plate type: choose {', '.join(PLATE_TYPES)}")
    out = {"speed": speed, **clean_options(values)}
    if plate:
        out["plate_type"] = plate
    cfg = load_config()
    cfg.setdefault("print_defaults", {})[str(printer_id)] = out
    save_config(cfg)
    return out


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
        raise FarmError(f"Pick the {', '.join(missing)} profile for this printer in Slicing")
    ref = lambda s: {"source": presets[s]["source"], "id": presets[s]["id"]}  # noqa: E731
    body = {"printer_preset": ref("printer"), "process_preset": ref("process"), "filament_presets": [ref("filament")],
            "export_3mf": True, "plate": 1}
    if color in COLOR_HEX:
        body["filament_colours"] = [COLOR_HEX[color]]
    if (presets.get("plate") or {}).get("name"):
        body["bed_type"] = presets["plate"]["name"]  # else the process profile's plate (usually Cool Plate)
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
                   again: bool = False) -> int:
    """Slice these plates [(batch_id, plate, kind, color)] for these printers (their Bambuddy profiles; a
    printer without profiles — or None — slices nothing), one at a time in the background. Only a person's
    Slice / Start print asks for this: nothing is sliced by itself (owner decision 0.22.22). Skipped: being
    sliced, already sliced with those profiles (unless `again`)."""
    cfg = load_config()
    cur = latest_slices()
    targets = {}
    for pid in (printer_ids or [None]):
        key, presets = target(pid, cfg)
        if not all(presets.get(s) for s in PRESET_SLOTS):
            continue  # no Bambuddy profiles picked for this printer model yet (Slicing) — nothing is guessed
        targets[key] = presets
    n = 0
    now = db.now()
    with db.tx() as con:
        for bid, plate, kind, color in plates:
            for key, presets in targets.items():
                s = cur.get((bid, plate, key))
                if s and s["state"] in ("queued", "uploading", "slicing") or (s and s["state"] == "done" and not again):
                    continue
                con.execute("INSERT INTO slices(batch_id,plate,kind,color,model,engine,presets,state,created_at,"
                            "updated_at,computer_id) VALUES (?,?,?,?,?,'bambuddy',?,'queued',?,?,?)",
                            (bid, plate, kind, color or "", key, json.dumps(presets), now, now, computer.ID))
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


# Bambuddy 1.3.1 crashes reading Bambu Cloud's profile list when part of it comes back empty (null, since
# Nov 2026): its profile list and slices with a Bambu Cloud process profile fail with this. SnapSTL uses no cloud
# profiles; the list still fails while Bambuddy is signed in to Bambu Cloud and SnapSTL's key may use the cloud.
CLOUD_LIST_BUG = "'nonetype' object has no attribute 'get'"
CLOUD_LIST_HINT = ("Bambuddy fails reading Bambu Cloud's profile list (a Bambuddy bug). SnapSTL uses only Bambuddy's "
                   "local and standard profiles: in Bambuddy sign out of Bambu Cloud (Settings → Bambu Cloud), or give "
                   "SnapSTL a key without \"Allow cloud access\", then pick local or standard profiles in Slicing and "
                   "slice again.")


def slicer_hint(err: str) -> str:
    """Plain English for Bambuddy's slicer errors."""
    low = (err or "").lower()
    if CLOUD_LIST_BUG in low:
        return f"{CLOUD_LIST_HINT} Details: {err}"
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
    """'' — any printer may print any plate (owner decision 0.22.20): the colour is chosen per print by its
    filament slot (`best_tray`, the Print tab's slot list), so nothing is ruled out by colour."""
    return ""


def _hex_rgb(h: str) -> tuple[int, int, int] | None:
    h = _hex(h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if h else None  # type: ignore[return-value]


def tray_score(need: str, sp: dict) -> tuple[int, int]:
    """How well a loaded slot fits a colour: (2 exact name / 1 same family / 0 other, -colour distance)."""
    q, _ = color_match(need, [sp.get("color", "")])
    want, have = _hex_rgb(COLOR_HEX.get(_norm(need), "")), _hex_rgb(sp.get("hex", ""))
    dist = sum((a - b) ** 2 for a, b in zip(want, have)) if want and have else 10 ** 6
    return q, -dist


def best_tray(need: str, printer: dict) -> int | None:
    """The slot SnapSTL suggests for a colour on a printer: the best colour fit, then the most filament left.
    None = no filament reported."""
    sps = printer.get("spools") or []
    if not sps:
        return None
    return max(sps, key=lambda sp: (*tray_score(need, sp), sp.get("remain") if isinstance(sp.get("remain"), int) else -1))["tray"]


def tray_choices(need: str, printer: dict) -> list[dict]:
    """A printer's loaded slots for the Print tab's slot list, best fit first."""
    out = [{"tray": sp["tray"], "slot": sp["slot"], "color": sp.get("color", ""), "name": sp.get("name", ""),
            "hex": sp.get("hex", ""), "material": sp.get("material", ""), "remain": sp.get("remain"),
            "match": tray_score(need, sp)[0]} for sp in printer.get("spools") or []]
    return sorted(out, key=lambda t: (-t["match"], t["tray"]))


def pick_tray(plate: dict, printer: dict, tray) -> int | None:
    """The slot a print uses: the one a person chose (must be loaded in that printer), else the best fit."""
    if tray in (None, ""):
        return best_tray(needed_color(plate), printer)
    try:
        tray = int(tray)
    except (TypeError, ValueError) as e:
        raise FarmError("Choose a filament slot") from e
    if printer.get("spools") and tray not in {sp["tray"] for sp in printer["spools"]}:
        raise FarmError(f"{printer['name']} has no filament in that slot any more — Refresh, then choose again")
    return tray


def match_note(plate: dict, printer: dict) -> str:
    """How the suggested slot fits the plate's colour, for the printer list."""
    need = needed_color(plate)
    t = best_tray(need, printer)
    sp = next((x for x in printer.get("spools") or [] if x["tray"] == t), None)
    if sp is None:
        return "no filament reported"
    q = tray_score(need, sp)[0]
    return "" if q == 2 else f"{'close' if q == 1 else 'no'} {need}: {sp.get('color') or sp.get('hex') or '?'} loaded"


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


def print_job(job_id: int, printer_id: int, options: dict | None, prs: list[dict], tray=None) -> dict:
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
    tray = pick_tray(plate, printer, tray)
    if j["state"] in ("slicing", "uploading", "printing"):
        raise FarmError(f"It's {j['state']} — wait until it's finished")
    if j["state"] == "queued":
        _set(job_id, printer_id=printer["id"], printer_name=printer["name"], options=json.dumps(opts), ams_tray=tray)
        WORKER.kick()
        return {"job_id": job_id, "moved": True}
    if j["state"] == "sent":
        api("PATCH", f"/queue/{j['queue_item_id']}", json_body={"printer_id": printer["id"], **opts,
                                                                **({"ams_mapping": [tray]} if tray is not None else {})})
        _set(job_id, printer_id=printer["id"], printer_name=printer["name"], options=json.dumps(opts), ams_tray=tray,
             error=None)
        return {"job_id": job_id, "moved": True}
    now = db.now()
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,options,ams_tray,created_at,"
            "updated_at,computer_id) VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)",
            (j["batch_id"], j["plate"], j["kind"], j["color"], printer["id"], printer["name"], json.dumps(opts), tray, now,
             now, computer.ID))
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

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: int | None = None
        self.last_refresh = 0.0
        self._models: tuple[float, dict[int, tuple[str, str]]] = (0.0, {})
        self.log: list[str] = []

    def start(self) -> None:
        forget_local_slicing()
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
        # sliced by Bambuddy for this printer's model, or slice it now and send it after
        missing = missing_presets(j["printer_id"], cfg)
        if missing:
            raise FarmError(f"Pick the slicing profiles for {missing} in Slicing, then Start print again")
        key, _ = target(j["printer_id"], cfg)
        s = slice_for(latest_slices(j["batch_id"]), j["batch_id"], j["plate"], key)
        if s and s["state"] == "done" and s["sliced_file_id"]:
            file_id = int(s["sliced_file_id"])
            _set(j["id"], sliced=f"Bambuddy library #{file_id}")
        elif s and s["state"] == "failed" and (s["updated_at"] or 0) >= (j["updated_at"] or 0):
            raise FarmError(f"Slicing failed: {s['error']}")  # failed for this try (Try again slices anew)
        else:
            if not (s and s["state"] in ("queued", "uploading", "slicing")):
                request_slices([(j["batch_id"], j["plate"], j["kind"], j["color"])], [j["printer_id"]], again=True)
            _set(j["id"], state="slicing", error=None)  # sent once Bambuddy has sliced it
            return
        # Held (manual_start): Bambuddy never starts it by itself — a person presses Start.
        opts = clean_options(json.loads(j.get("options") or "{}"))
        tray = j.get("ams_tray")  # the filament slot chosen in Print (else Bambuddy matches by colour)
        item = api("POST", "/queue/", cfg=cfg, json_body={"library_file_id": file_id, "printer_id": j["printer_id"],
                                                         "manual_start": True, **opts,
                                                         **({"ams_mapping": [int(tray)]} if tray is not None else {})})
        qid = int((item or {})["id"])
        _set(j["id"], state="sent", file_id=file_id, queue_item_id=qid, held=1, error=None)
        self._log(f"{Path(j['plate']).name} → in {j['printer_name']}'s queue, waiting for Start")
        busy = str((printer_status(j["printer_id"], cfg, max_age=0) or {}).get("state", "")).upper() in BUSY_STATES
        if j.get("autostart") and busy:  # it would start right after the current print, on a full bed
            _set(j["id"], error="Held: the printer started another print — press Start print once it's done "
                                "and the bed is empty")
        elif j.get("autostart"):  # a person pressed Start print (Make It): release it now that it's queued
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
        try:
            if not b or not b["out_dir"] or not (Path(b["out_dir"]) / s["plate"]).is_file():
                raise FarmError("The plate file is gone")
            cfg = load_config()
            presets = json.loads(s.get("presets") or "{}")
            if not (presets and all(isinstance(v, dict) for v in presets.values())):
                raise FarmError("Picked before 0.21 for a slicer on this computer — pick the Bambuddy profiles "
                                "in Slicing, then slice again")
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
            self._log(f"Slicing {src.name} in Bambuddy with {presets['printer'].get('name') or s.get('model')}")
        except FarmError as e:
            _slice_set(s["id"], state="failed", error=slicer_hint(str(e)))
            self._log(f"Slicing {Path(s['plate']).name}: {e}")

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

    def _printer_info(self, printer_id: int | None = None) -> dict:
        """Printer id → (model, name), cached a minute."""
        if time.time() - self._models[0] > 60 or (printer_id is not None and printer_id not in self._models[1]):
            try:
                self._models = (time.time(), {p["id"]: (p.get("model") or "", p.get("name") or f"Printer {p['id']}")
                                              for p in printers(with_status=False)})
            except FarmError:
                pass
        return self._models[1]

    def model_of(self, printer_id: int) -> str:
        """A printer's model (cached a minute)."""
        return (self._printer_info(printer_id).get(printer_id) or ("", ""))[0]

    def name_of(self, printer_id: int | None) -> str:
        return (self._printer_info(printer_id).get(printer_id) or ("", ""))[1] if printer_id else ""

    def models(self) -> dict:
        return {pid: v[0] for pid, v in self._printer_info().items()}

    @staticmethod
    def _apply_speed(j: dict, cfg: dict) -> dict:
        """The speed chosen in Print, applied once the printer really prints (state RUNNING, first layer
        on): Bambuddy's queue says "printing" while the printer is still heating and calibrating, and a
        speed sent then is lost when the print begins. The printer's own speed level is checked every
        round; the command is sent again until it matches (SPEED_TRIES at most), then the job says so."""
        want = int(j["speed"])
        s = printer_status(j["printer_id"], cfg, max_age=0)
        if s.get("speed_level") == want:
            out = {"speed_set": 1}
            if str(j.get("error") or "").startswith("Print speed"):
                out["error"] = None
            return out
        running = str(s.get("state") or "").upper() == "RUNNING"
        layer = s.get("layer_num")
        if not running or (isinstance(layer, int) and layer < 1):
            return {}  # still preparing: wait
        tries = int(j.get("speed_tries") or 0)
        name = SPEEDS.get(want, str(want))
        if tries >= SPEED_TRIES:
            return {"speed_set": 1, "error": f"Print speed not applied: the printer stayed at "
                    f"{SPEEDS.get(s.get('speed_level'), 'its speed')} after {tries} tries — set {name} in Bambuddy"}
        try:
            api("POST", f"/printers/{j['printer_id']}/print-speed?mode={want}", cfg=cfg)
            return {"speed_tries": tries + 1}
        except FarmError as e:
            return {"speed_tries": tries + 1, "error": f"Print speed not set: {e}"}

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
                changes |= self._apply_speed(j, cfg)
            if new == "printing" and j["state"] != "printing":
                changes["started_at"] = time.time()  # Stats: hours printing per printer
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
