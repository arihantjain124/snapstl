"""Printer care (0.41, owner): maintenance on Today's work, and why a print failed.

Bambuddy keeps the maintenance record (owner's choice): its task types, each printer's print hours, the tasks
assigned to each printer, "performed" and the history (`/api/v1/maintenance/*`). SnapSTL only
* sets it up once (`setup`, again with Printers → Set up maintenance): the tasks below with our intervals — the
  printers print ~20 h a day, so Bambuddy's defaults (e.g. oiling every 50 h) are too often — and Clean Build
  Plate switched off (the plate is washed after every print anyway);
* shows what is due on Today's work, on a printer that is free anyway, in the day only (`cards`):
  - first a **full service** per printer (every task; the counters start from 0 after it — owner: printer ages
    unknown), one printer a day, the worst first (`FIRST`);
  - after that the tasks Bambuddy says are due; the long ones (500 h: grease, oil, PTFE, fans) on one printer a
    day at most, so output barely drops;
* ticks them in Bambuddy when the person presses Done (`done`; `care_log` here too, for Stats).

Why a print failed: a print Bambuddy recorded as failed / aborted in the last 24 hours gets one card on Today's
work with one tap per reason (`REASONS`); saved here (`print_failures`) and on the Bambuddy print archive
(`failure_reason`, so Bambuddy's failure analysis shows it too).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from . import db, farm
from .settings import APP_DIR

# Bambuddy task name → (printer family or "" for both, every … print hours, minutes it takes, how — plain words)
TASKS: dict[str, tuple[str, int, int, str]] = {
    "Clean Nozzle/Hotend": ("", 100, 5, "Heat the nozzle, wipe the outside with the brass brush. Check the nozzle "
                                        "wiper is not torn."),
    "Clean Carbon Rods": ("P1S", 100, 5, "Wipe the 3 carbon rods with a dry cloth (a little IPA if sticky). "
                                         "Never oil them."),
    "Clean Linear Rails": ("A1", 100, 5, "Wipe the X rail and the Y rail with a dry cloth."),
    "Check Belt Tension": ("", 200, 5, "Push each belt in the middle: it should spring back, not flap. Tighten if "
                                        "loose (Bambu wiki: belt tension)."),
    "Clean Extruder Gears": ("", 200, 5, "Open the extruder cover, brush the filament dust off the gears."),
    "Check Heatbed Cable": ("A1", 200, 3, "Move the bed by hand: the cable behind it must not be cracked, kinked "
                                          "or rubbing."),
    "Check PTFE Tube": ("", 500, 5, "Look at the PTFE tube ends: no brown, no cuts, pushed fully in."),
    "Lubricate Linear Rails": ("A1", 500, 5, "One drop of sewing-machine oil on a cloth, wipe the rails, move the "
                                              "axis by hand a few times."),
    "Grease Z Lead Screws": ("", 500, 10, "Thin layer of PTFE or white lithium grease on the Z screws, then move "
                                          "Z up and down."),
    "Clean Fans and Filter": ("P1S", 500, 5, "Blow or brush the dust off the fans; look at the carbon filter."),
}
OFF = ("Clean Build Plate",)  # washed after every print: not tracked (owner)
ICON = {"Clean Extruder Gears": "Cog", "Check Heatbed Cable": "Cable", "Grease Z Lead Screws": "Droplet",
        "Clean Fans and Filter": "Fan"}
LONG_H = 500     # tasks this long apart are the long ones: one printer a day
FIRST = ("A5", "X8", "A4", "A3", "A2", "X7")  # full service first on the printers that fail most / are oldest
SETUP_VERSION = 1
REASONS = {"edges": "Edges lifted (warping)", "spaghetti": "Spaghetti / came off the plate",
           "layer": "Layer shift", "nozzle": "Nozzle blocked", "filament": "Filament ran out / tangled",
           "other": "Other"}
FAILED = ("failed", "aborted")
_FILE = APP_DIR / "printercare.json"
_TTL = 60
_CACHE: dict = {}
_LOCK = threading.Lock()


class CareError(ValueError):
    pass


def family(model: str) -> str:
    m = (model or "").upper().replace(" ", "")
    return "A1" if m.startswith("A1") else "P1S" if m.startswith(("P1", "X1")) else m


def short(name: str) -> str:
    """'X8 BOB' → 'X8' (the name the floor uses)."""
    return (name or "").split(" ")[0]


def _rank(name: str) -> tuple[int, str]:
    s = short(name)
    return (FIRST.index(s) if s in FIRST else len(FIRST), name)


def wanted(model: str) -> dict[str, tuple[str, int, int, str]]:
    f = family(model)
    return {n: t for n, t in TASKS.items() if not t[0] or t[0] == f}


# ---------------- setting Bambuddy up (once) ----------------
def _state() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_set_up() -> bool:
    return _state().get("setup") == SETUP_VERSION


def setup() -> dict:
    """Our tasks and intervals in Bambuddy (idempotent): new task types made, assigned to the printers they are
    for, interval and enabled set; Clean Build Plate off. Tasks we don't know are left as they are."""
    types = {t["name"].lower(): t for t in farm.api("GET", "/maintenance/types") or []}
    made = []
    for name, (_, h, _, how) in TASKS.items():
        if name.lower() not in types:
            t = farm.api("POST", "/maintenance/types", json_body={
                "name": name, "description": how, "default_interval_hours": float(h), "interval_type": "hours",
                "icon": ICON.get(name)})
            types[name.lower()] = t
            made.append(name)
    assigned = 0
    for p in farm.api("GET", "/maintenance/overview") or []:
        have = {i["maintenance_type_name"].lower() for i in p.get("maintenance_items") or []}
        for name in wanted(p.get("printer_model") or ""):
            if name.lower() not in have:
                farm.api("POST", f"/maintenance/printers/{int(p['printer_id'])}/assign/{int(types[name.lower()]['id'])}")
                assigned += 1
    changed = 0
    for p in farm.api("GET", "/maintenance/overview") or []:
        want = {n.lower(): t for n, t in wanted(p.get("printer_model") or "").items()}
        for it in p.get("maintenance_items") or []:
            n = it["maintenance_type_name"].lower()
            body = None
            if n in want:
                h = float(want[n][1])
                if not it.get("enabled") or float(it.get("interval_hours") or 0) != h or it.get("interval_type") != "hours":
                    body = {"custom_interval_hours": h, "custom_interval_type": "hours", "enabled": True}
            elif n in (o.lower() for o in OFF) and it.get("enabled"):
                body = {"enabled": False}
            if body:
                farm.api("PATCH", f"/maintenance/items/{int(it['id'])}", json_body=body)
                changed += 1
    _FILE.write_text(json.dumps({"setup": SETUP_VERSION, "at": time.time()}), encoding="utf-8")
    with _LOCK:
        _CACHE.clear()
    with db.tx() as con:
        db.add_activity(con, "care", f"Maintenance set up in Bambuddy: {len(made)} task types made, {assigned} "
                                     f"assigned, {changed} changed")
    return {"made": made, "assigned": assigned, "changed": changed}


class Setup:
    """Sets Bambuddy up once in the background (tried again every 10 minutes while Bambuddy can't be reached)."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self.error = ""

    def start(self) -> None:
        import os

        if os.environ.get("SNAPSTL_NO_CARESETUP") or is_set_up() or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="printer-care-setup", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not is_set_up():
            try:
                if farm.load_config().get("api_key"):
                    setup()
                    self.error = ""
                    return
            except Exception as e:  # noqa: BLE001 — Bambuddy down / refused: try again later
                self.error = str(e)
            time.sleep(600)


SETUP = Setup()


# ---------------- what is due ----------------
def _cached(key: str, fn):
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    v = fn()
    with _LOCK:
        _CACHE[key] = (time.time(), v)
    return v


def overview() -> list[dict]:
    return _cached("overview", lambda: farm.api("GET", "/maintenance/overview") or [])


def _log(con, since: float) -> list[dict]:
    return db.rows(con, "SELECT printer_id, kind, tasks, at FROM care_log WHERE at >= ? ORDER BY at", since)


def full_done(con) -> set[int]:
    return {r[0] for r in con.execute("SELECT DISTINCT printer_id FROM care_log WHERE kind='full'")}


def _item(it: dict) -> dict:
    name = it["maintenance_type_name"]
    t = TASKS.get(name, ("", int(it.get("interval_hours") or 0), 10, it.get("description") or ""))
    return {"id": int(it["id"]), "name": name, "minutes": t[2], "how": t[3],
            "every_h": int(it.get("interval_hours") or t[1]), "over_h": round(-float(it.get("hours_until_due") or 0), 1),
            "long": int(it.get("interval_hours") or t[1]) >= LONG_H}


def cards(free_ids: set[int], ov: list[dict] | None = None, now: float | None = None) -> list[dict]:
    """Maintenance cards for the printers free now (call it in the day only)."""
    now = now or time.time()
    try:
        ov = overview() if ov is None else ov
    except farm.FarmError:
        return []
    day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with db.tx() as con:
        done_full = full_done(con)
        today = _log(con, day0)
    big_today = any(r["kind"] == "full" or any(t.get("long") for t in json.loads(r["tasks"] or "[]")) for r in today)
    out = []
    by = {int(p["printer_id"]): p for p in ov}
    # one full service a day: the free printer first in line
    need = sorted((p for p in ov if int(p["printer_id"]) not in done_full), key=lambda p: _rank(p["printer_name"]))
    if not big_today:
        pick = next((p for p in need if int(p["printer_id"]) in free_ids), None)
        if pick is not None:
            its = [_item(i) for i in pick.get("maintenance_items") or [] if i.get("enabled")]
            out.append({"printer_id": int(pick["printer_id"]), "printer": pick["printer_name"], "full": True,
                        "items": its, "minutes": sum(i["minutes"] for i in its),
                        "hours": round(float(pick.get("total_print_hours") or 0))})
            big_today = True
    for pid in sorted(free_ids):
        p = by.get(pid)
        if p is None or pid not in done_full or any(c["printer_id"] == pid for c in out):
            continue  # waits for its full service first
        its = [_item(i) for i in p.get("maintenance_items") or [] if i.get("enabled") and i.get("is_due")]
        if big_today:
            its = [i for i in its if not i["long"]]
        if not its:
            continue
        if any(i["long"] for i in its):
            big_today = True
        out.append({"printer_id": pid, "printer": p["printer_name"], "full": False, "items": its,
                    "minutes": sum(i["minutes"] for i in its), "hours": round(float(p.get("total_print_hours") or 0))})
    return out


def waiting_full(ov: list[dict] | None = None) -> list[str]:
    """Printers still waiting for their first full service, in the order they get it."""
    try:
        ov = overview() if ov is None else ov
    except farm.FarmError:
        return []
    with db.tx() as con:
        done_full = full_done(con)
    return [p["printer_name"] for p in sorted(ov, key=lambda p: _rank(p["printer_name"]))
            if int(p["printer_id"]) not in done_full]


def done(printer_id: int, item_ids: list[int] | None, full: bool, staff: str = "") -> dict:
    """Done on a maintenance card: the tasks performed in Bambuddy (a full service: every task of the printer)."""
    p = farm.api("GET", f"/maintenance/printers/{int(printer_id)}") or {}
    items = [i for i in p.get("maintenance_items") or [] if i.get("enabled")]
    if not full:
        ids = {int(i) for i in item_ids or []}
        items = [i for i in items if int(i["id"]) in ids]
        if not ids or len(items) != len(ids):
            raise CareError("Those tasks are not on this printer — refresh the page")
    if not items:
        raise CareError("No tasks on this printer in Bambuddy")
    note = "SnapSTL · Today's work" + (f" · {staff}" if staff else "")
    for i in items:
        farm.api("POST", f"/maintenance/items/{int(i['id'])}/perform", json_body={"notes": note})
    tasks = [_item(i) for i in items]
    name = p.get("printer_name") or str(printer_id)
    with db.tx() as con:
        con.execute("INSERT INTO care_log(printer_id, printer, kind, tasks, staff, at) VALUES (?,?,?,?,?,?)",
                    (int(printer_id), name, "full" if full else "tasks", json.dumps(tasks), staff or None, db.now()))
        db.add_activity(con, "care", f"{name}: {'full service' if full else ', '.join(t['name'] for t in tasks)} "
                                     f"done" + (f" ({staff})" if staff else ""))
    with _LOCK:
        _CACHE.clear()
    return {"ok": True, "tasks": len(tasks)}


# ---------------- why a print failed ----------------
def failures(now: float | None = None) -> list[dict]:
    """Prints Bambuddy recorded as failed / aborted in the last 24 hours that nobody gave a reason for yet."""
    now = now or time.time()
    since = datetime.fromtimestamp(now) - timedelta(hours=24)

    def get():
        a = farm.api("GET", f"/archives/?date_from={since.date().isoformat()}&limit=300") or []
        return a if isinstance(a, list) else a.get("items") or []
    try:
        arch = _cached("archives", get)
    except farm.FarmError:
        return []
    with db.tx() as con:
        noted = {r[0] for r in con.execute("SELECT archive_id FROM print_failures")}
    out = []
    for x in arch:
        when = x.get("completed_at") or x.get("created_at") or ""
        if x.get("status") not in FAILED or int(x["id"]) in noted or not when or when < since.isoformat():
            continue
        out.append({"archive_id": int(x["id"]), "printer_id": x.get("printer_id"), "print_name": x.get("print_name") or "",
                    "failed_at": when, "hours": round((x.get("actual_time_seconds") or 0) / 3600, 1)})
    out.sort(key=lambda x: x["failed_at"], reverse=True)
    return out


def note_failure(archive_id: int, reason: str, staff: str = "", printer: str = "", printer_id: int | None = None,
                 print_name: str = "", failed_at: str = "") -> dict:
    """One tap on a failed print: the reason saved here and on the Bambuddy archive ('' = skipped)."""
    if reason and reason not in REASONS:
        raise CareError("Choose a reason")
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO print_failures(archive_id, printer_id, printer, print_name, failed_at, reason, "
                    "staff, at) VALUES (?,?,?,?,?,?,?,?)", (int(archive_id), printer_id, printer or None,
                                                            print_name or None, failed_at or None, reason,
                                                            staff or None, db.now()))
    synced = False
    if reason:
        try:
            farm.api("PATCH", f"/archives/{int(archive_id)}", json_body={"failure_reason": REASONS[reason]})
            synced = True
        except farm.FarmError:
            pass  # kept here; Bambuddy's own analysis just won't show it
    with _LOCK:
        _CACHE.pop("archives", None)
    return {"ok": True, "bambuddy": synced}


# ---------------- Stats ----------------
def printer_stats(con, weeks: int = 4, now: float | None = None) -> dict:
    """Per printer: prints and failures per week (SnapSTL's print jobs), last full service / tasks, top reasons."""
    now = now or time.time()
    d = datetime.fromtimestamp(now).date()
    monday = d - timedelta(days=d.weekday())
    starts = [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    t0 = datetime.combine(starts[0], datetime.min.time()).timestamp()
    rows: dict[str, dict] = {}

    def row(name):
        return rows.setdefault(name, {"printer": name, "weeks": [{"prints": 0, "failed": 0} for _ in starts],
                                      "full_at": None, "tasks_at": None, "reasons": {}})
    for name, st, at in con.execute("SELECT printer_name, state, updated_at FROM print_jobs WHERE printer_id > 0 "
                                    "AND state IN ('done','failed') AND updated_at >= ?", (t0,)):
        if not name:
            continue
        day = datetime.fromtimestamp(at).date()
        w = max(i for i, s in enumerate(starts) if day >= s)
        x = row(name)["weeks"][w]
        x["prints"] += 1
        x["failed"] += st == "failed"
    for pid, name, kind, at in con.execute("SELECT printer_id, printer, kind, MAX(at) FROM care_log GROUP BY printer_id, kind"):
        r = row(name or str(pid))
        r["full_at" if kind == "full" else "tasks_at"] = at
    for name, reason, n in con.execute("SELECT printer, reason, COUNT(*) FROM print_failures WHERE reason <> '' AND at >= ? "
                                       "GROUP BY printer, reason", (now - 30 * 86400,)):
        if name:
            row(name)["reasons"][REASONS.get(reason, reason)] = n
    return {"weeks": [s.isoformat() for s in starts], "printers": sorted(rows.values(), key=lambda r: _rank(r["printer"]))}
