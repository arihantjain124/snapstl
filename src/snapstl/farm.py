"""Print farm: finished plates go to Bambuddy, the open-source print-farm manager on this computer.

SnapSTL never talks to a printer itself. Bambuddy (installed next to SnapSTL by the installers,
http://127.0.0.1:8000 — scripts/bambuddy-mac.sh, scripts/windows/bambuddy.ps1) holds the printer
connections (LAN Only + Developer Mode on each printer), the print queue and "Clear plate & start next".
SnapSTL:

  * connects to Bambuddy — on a fresh install (Bambuddy's login off) it creates its own API key;
  * only *reads* the printers from Bambuddy (Printers page): live state and the filament loaded in
    each AMS slot / external spool, named with Bambuddy's colour catalogue ("Hot Pink", "Jade White").
    Printers, filament and everything else are set up in Bambuddy itself. A printer's **Location** in
    Bambuddy says what it makes: "Bases…" or "Names…" (anything else: both);
  * after a build, **Push to queue**: for each plate the person picks a printer (a suggestion is
    pre-filled: the plate's colour loaded — white for bases — right job, least busy).
  * nothing is queued or started by itself: a plate goes to Bambuddy only when a person presses Push
    to queue / Print, and it waits there **held** (manual start) until a person presses **Start**. The plate's sliced file (.gcode.3mf) is uploaded
    to Bambuddy's library and added to that printer's queue; the job is followed until it's printed.
    Bambuddy waits for a person to clear the bed before each next print.

Printers only print *sliced* plates. A plate is sliced either
  * by hand in Bambu Studio (open the plate → Slice → Export plate sliced file), saved in the batch
    folder as `Sliced/<same path>.gcode.3mf` (or `<plate name>.gcode.3mf` next to the plate), or
  * by a slicer service, when one is set (Bambuddy's slicer sidecar — an HTTP wrapper around the
    Bambu Studio / OrcaSlicer command line, Docker). The result is saved as `Sliced/….gcode.3mf`.
The Bambu Studio app's own command line can't slice on macOS (it crashes; ARCHITECTURE decision log #28).

Secrets: the Bambuddy API key is kept only in `farm.json` in the app folder (this user only). SnapSTL
never sees printer access codes.
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
    "slicer_url": "",  # e.g. http://127.0.0.1:3001 (Bambuddy's Bambu Studio sidecar); "" = sliced by hand
    "printer_preset": "Bambu Lab A1 0.4 nozzle",
    "process_preset": "0.20mm Standard @BBL A1",
    "filament_preset": "Bambu PLA Basic @BBL A1",
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


def role_of(location: str) -> str:
    """What a printer makes, from its Location in Bambuddy: 'Bases…' / 'Names…'; anything else = both."""
    loc = (location or "").strip().lower()
    if loc.startswith("base"):
        return "bases"
    if loc.startswith("name"):
        return "names"
    return ""


def printers(cfg: dict | None = None, with_status: bool = True) -> list[dict]:
    """Bambuddy's printers (read only): what they make (Location), live state, filament loaded."""
    cfg = cfg or load_config()
    cmap = color_names(cfg) if with_status else {}
    out = []
    for p in api("GET", "/printers/", cfg=cfg) or []:
        pid = int(p["id"])
        item = {"id": pid, "name": p.get("name") or f"Printer {pid}", "model": p.get("model") or "",
                "serial": p.get("serial_number") or "", "ip": p.get("ip_address") or "",
                "location": p.get("location") or "", "role": role_of(p.get("location") or ""),
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
    plist = get("/printers/")
    if isinstance(plist, dict):  # error
        out["error"] = plist.get("error", "")
        return out
    for p in plist or []:
        pid = int(p["id"])
        status = get(f"/printers/{pid}/status")
        _status_cache[pid] = (time.time(), status if "error" not in status else {"connected": False, **status})
        out["printers"].append({"id": pid, "name": p.get("name") or f"Printer {pid}", "printer": redact(p),
                                "status": redact(status), "queue": redact(get(f"/queue/?printer_id={pid}"))})
    return out


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
    """'' if the printer can print this plate (right job, the plate's colour loaded), else why not."""
    role = printer.get("role")
    if role and role != plate["kind"]:
        return f"makes {'names' if role == 'names' else 'bases'} (Location in Bambuddy)"
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
        best = min(ok, key=lambda p: (-color_match(need, p.get("colors") or [])[0], load[p["id"]],
                                      bool(p.get("role")) is False, p["name"]))
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
            if new == "printing":
                s = printer_status(j["printer_id"], cfg, max_age=10)
                if s.get("progress") is not None:
                    changes["progress"] = float(s["progress"])
            if changes:
                _set(j["id"], **changes)
                if changes.get("state") == "done":
                    from . import makeit

                    makeit.update_made(j["batch_id"])  # both parts printed → Ready to ship


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
