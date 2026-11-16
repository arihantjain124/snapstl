"""Make It tab: the plates of built batches, waiting to be printed, by Bases and by Names.

* **Bases**: every base plate still to print, with a printer pre-selected — the next *available*
  printer (connected, idle, bed cleared, nothing of ours on its way) with white loaded, the one with
  the most filament left on that spool first.
* **Names**: every name plate with its colour; pre-selected only when an available printer has that
  colour loaded (read from Bambuddy).
* A print starts only when a person presses **Start print**: the plate is sliced if needed, uploaded,
  queued on that printer held, and then released — all from that one click (`farm.Worker`). Nothing
  else starts a print.
* A printed plate leaves the queue for **Completed**. **Reprint** (only after a person confirms the
  print failed) puts it back in the queue.
* **Slice** (Make It → Slice): plates are sliced by Bambuddy's slicer with the presets chosen there,
  one at a time in the background; each row shows Sliced / Slicing % / failed. Start print sends the
  sliced file, or slices first and sends it when that's done.
* **Remove** takes a plate off Bases / Names (e.g. printed by hand); **Removed** lists them with
  **Put back**. Nothing is deleted.
* An order is **Ready to ship** (`orders.made_at`, Orders → Ready to ship) once every base plate and
  name plate holding it is printed.

Which plates are listed: those of this computer's built batches whose orders are still Bundled (not
yet marked printed / ready), plus any plate with a print on its way.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import computer, db, farm

AVAILABLE_STATES = {"IDLE", "FINISH", "FAILED", ""}


# ---------------- plates ↔ orders ----------------
def _batch_orders(con, b: dict) -> list[dict]:
    ids = json.loads(b.get("order_ids") or "[]")
    if not ids:
        return []
    return db.rows(con, f"SELECT order_id, name, color, state, quantity, made_at FROM orders "
                        f"WHERE order_id IN ({','.join('?' * len(ids))})", *ids)


def plate_order_ids(plates: list[dict], orders: list[dict]) -> dict[str, list[str]]:
    """Plate file → order ids on it. New builds record them; older ones are matched by name (and colour
    for name plates), one copy at a time."""
    out: dict[str, list[str]] = {}
    for kind in ("bases", "names"):
        pool = [(o["order_id"], (o["name"] or "").lower(), (o["color"] or "").lower())
                for o in orders for _ in range(max(1, int(o.get("quantity") or 1)))]
        for p in [p for p in plates if p["kind"] == kind]:
            if p.get("order_ids"):
                out[p["file"]] = list(p["order_ids"])
                continue
            got = []
            color = (p.get("colors") or [""])[0].lower() if kind == "names" else None
            for n in p.get("names", []):
                for i, (oid, name, c) in enumerate(pool):
                    if name == n.lower() and (color is None or c == color):
                        got.append(oid)
                        pool.pop(i)
                        break
            out[p["file"]] = got
    return out


def _batches(con) -> list[dict]:
    return db.rows(con, "SELECT * FROM batches WHERE status IN ('done','done_with_problems') AND "
                        "COALESCE(computer_id, ?) = ? ORDER BY id", computer.ID, computer.ID)


# ---------------- printer choice ----------------
def spool_left(printer: dict, need: str) -> int:
    """Most filament left (%) on a loaded spool of the needed colour (-1 = unknown)."""
    best = -1
    for s in printer.get("spools") or []:
        if farm.color_match(need, [s.get("color", "")])[0] and isinstance(s.get("remain"), int):
            best = max(best, s["remain"])
    return best


def available(printer: dict, load: int) -> bool:
    st = printer.get("status") or {}
    return (bool(st.get("connected")) and not st.get("awaiting_plate_clear") and load == 0
            and str(st.get("state", "")).upper() in AVAILABLE_STATES)


def choose(plates: list[dict], prs: list[dict], active: dict[int, int]) -> dict[str, int | None]:
    """Pre-selected printer per plate: only an *available* printer that fits; exact colour first, then
    the most filament left. Each pick makes that printer busy for
    the next plate, so plates spread over the free printers."""
    load = {p["id"]: active.get(p["id"], 0) for p in prs}
    out: dict[str, int | None] = {}
    for pl in plates:
        if pl.get("job_active"):
            continue
        need = farm.needed_color(pl)
        ok = [p for p in prs if p.get("active", True) and not farm.eligible(pl, p) and available(p, load[p["id"]])]
        if not ok:
            out[pl["file"]] = None
            continue
        best = min(ok, key=lambda p: (-farm.color_match(need, p.get("colors") or [])[0], -spool_left(p, need),
                                      p["name"]))
        out[pl["file"]] = best["id"]
        load[best["id"]] += 1
    return out


# ---------------- the board ----------------
def board(prs: list[dict] | None = None) -> dict:
    """Bases queue, Names queue and Completed, with printer choices and pre-selections."""
    slices = farm.latest_slices()
    with db.tx() as con:
        batches = _batches(con)
        latest: dict[tuple[int, str], dict] = {}
        for j in db.rows(con, "SELECT * FROM print_jobs WHERE computer_id=? ORDER BY id", computer.ID):
            latest[(j["batch_id"], j["plate"])] = j
        rows, removed = [], []
        for b in batches:
            summ = json.loads(b.get("summary") or "{}")
            plates = summ.get("plates") or []
            orders = _batch_orders(con, b)
            state_of = {o["order_id"]: o["state"] for o in orders}
            on = plate_order_ids(plates, orders)
            out_dir = Path(b["out_dir"]) if b.get("out_dir") else None
            for p in plates:
                j = latest.get((b["id"], p["file"]))
                ids = on.get(p["file"], [])
                if j and j["state"] == "removed":
                    removed.append({"batch_id": b["id"], "batch": summ.get("label") or f"Batch #{b['id']}",
                                    "file": p["file"], "kind": p["kind"], "names": p.get("names", []),
                                    "colors": p.get("colors", []), "removed_at": j["updated_at"]})
                    continue
                open_orders = [i for i in ids if state_of.get(i) == "bundled"]
                active = bool(j and j["state"] in farm.ACTIVE)
                done = bool(j and j["state"] == "done")
                if not (open_orders or active or done):
                    continue  # its orders were marked printed / made again elsewhere
                sliced = farm.find_sliced(out_dir, p["file"]) if out_dir else None
                rows.append({
                    "batch_id": b["id"], "batch": summ.get("label") or f"Batch #{b['id']}", "file": p["file"],
                    "kind": p["kind"], "names": p.get("names", []), "colors": p.get("colors", []),
                    "order_ids": ids, "sliced": bool(sliced), "by_file": bool(sliced), "slice": None,
                    "job": _job(j), "job_active": active, "done": done,
                    "need": farm.needed_color(p),
                })
    prs = prs or []
    pending = [r for r in rows if not r["done"]]
    active_jobs = farm.active_counts()
    for r in pending:
        r["choices"] = [{"id": p["id"], "name": p["name"], "why_not": farm.eligible(r, p), "note": farm.match_note(r, p),
                         "available": available(p, active_jobs.get(p["id"], 0)), "left": spool_left(p, r["need"]),
                         "state": (p.get("status") or {}).get("state", "")} for p in prs]
    pick = choose(pending, prs, active_jobs) if prs else {}
    keys: dict = {}
    for r in pending:
        r["suggested"] = pick.get(r["file"])
        # sliced with the presets of the printer this plate goes to: its job's printer, else the pre-selected one
        pid = ((r["job"] or {}).get("printer_id") if r["job_active"] else None) or r["suggested"]
        if pid not in keys:
            keys[pid] = farm.target(pid)[0]
        r["slice_for"] = pid
        if not r["by_file"]:
            x = farm.slice_for(slices, r["batch_id"], r["file"], keys[pid])
            r["slice"] = {"state": x["state"], "progress": x["progress"], "error": x["error"]} if x else None
            r["sliced"] = bool(x and x["state"] == "done")
        else:
            r["slice"] = {"state": "done", "by": "file"}
    done = sorted([r for r in rows if r["done"]], key=lambda r: -(r["job"] or {}).get("updated_at", 0))[:300]
    return {"bases": [r for r in pending if r["kind"] == "bases"],
            "names": [r for r in pending if r["kind"] == "names"],
            "completed": done, "removed": removed}


def _job(j: dict | None) -> dict | None:
    if not j:
        return None
    return {"id": j["id"], "state": j["state"], "printer_id": j["printer_id"], "printer_name": j["printer_name"],
            "held": bool(j.get("held")), "progress": j.get("progress"), "error": j.get("error"),
            "updated_at": j.get("updated_at"), "options": json.loads(j.get("options") or "{}"),
            "speed": {v: k for k, v in farm.SPEEDS_SET.items()}.get(j.get("speed") or 0, "")}


# ---------------- actions (each one a person's click) ----------------
def _plate(batch_id: int, file: str) -> tuple[dict, dict]:
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=? AND COALESCE(computer_id, ?) = ?",
                        (batch_id, computer.ID, computer.ID)).fetchone()
    if b is None:
        raise farm.FarmError("This batch isn't on this computer")
    b = dict(b)
    plate = next((p for p in json.loads(b.get("summary") or "{}").get("plates", []) if p["file"] == file), None)
    if plate is None:
        raise farm.FarmError("Not a plate of this batch")
    return b, plate


def _latest(batch_id: int, file: str) -> dict | None:
    with db.tx() as con:
        j = con.execute("SELECT * FROM print_jobs WHERE batch_id=? AND plate=? AND computer_id=? ORDER BY id DESC LIMIT 1",
                        (batch_id, file, computer.ID)).fetchone()
    return dict(j) if j else None


def waiting_plates(prs: list[dict] | None = None) -> list[dict]:
    """Plates in Bases / Names with no print on its way, with their pre-selected printer (automatic
    slicing works through these)."""
    b = board(prs)
    return [r for r in b["bases"] + b["names"] if not r["job_active"]]


def start_print(batch_id: int, file: str, printer_id: int, options: dict | None, prs: list[dict],
                force: bool = False, speed: str = "") -> dict:
    """Start print (a person's click): the only way a Make It plate gets printed.
    Not sent yet / failed / cancelled / reprint → sent to that printer and started once queued.
    Waiting held in Bambuddy → moved to that printer if changed, then started."""
    b, plate = _plate(batch_id, file)
    opts = farm.clean_options(options)
    if speed and speed not in farm.SPEEDS_SET:
        raise farm.FarmError("Speed is silent, standard, sport or ludicrous")
    spd = farm.SPEEDS_SET.get(speed)
    printer = next((p for p in prs if p["id"] == int(printer_id)), None)
    if printer is None:
        raise farm.FarmError("Choose a printer")
    why = farm.eligible(plate, printer)
    if why:
        raise farm.FarmError(f"{printer['name']} {why}")
    j = _latest(batch_id, file)
    if j and j["state"] == "sent" and j.get("held"):
        if j["printer_id"] != printer["id"] or opts:
            farm.print_job(j["id"], printer["id"], opts, prs)  # move / options while still held
        if spd:
            farm._set(j["id"], speed=spd, speed_set=0)
        farm.start_job(j["id"], force=force)
        return {"job_id": j["id"], "started": True}
    if j and j["state"] in farm.ACTIVE:
        raise farm.FarmError(f"Already on its way ({j['state']}) on {j['printer_name']}")
    if j and j["state"] == "done":
        raise farm.FarmError("Already printed — use Reprint in Completed if it failed")
    now = db.now()
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,options,autostart,speed,"
            "created_at,updated_at,computer_id) VALUES (?,?,?,?,?,?,'queued',?,1,?,?,?,?)",
            (batch_id, file, plate["kind"], (plate.get("colors") or [""])[0], printer["id"], printer["name"],
             json.dumps(opts), spd, now, now, computer.ID))
    farm.WORKER.kick()
    return {"job_id": int(cur.lastrowid), "started": False}


def reprint(batch_id: int, file: str) -> dict:
    """Completed → back to the queue (a person confirmed the print failed). Its orders leave Ready to ship."""
    _plate(batch_id, file)
    j = _latest(batch_id, file)
    if not j or j["state"] != "done":
        raise farm.FarmError("Only a completed plate can be reprinted")
    now = db.now()
    with db.tx() as con:
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,updated_at,"
                    "computer_id) VALUES (?,?,?,?,0,'','reprint',?,?,?)",
                    (batch_id, file, j["kind"], j["color"], now, now, computer.ID))
    update_made(batch_id)
    return {"ok": True}


def remove(batch_id: int, file: str, put_back: bool = False) -> dict:
    """Take a plate off Bases / Names (nothing is deleted), or put it back."""
    _, plate = _plate(batch_id, file)
    j = _latest(batch_id, file)
    if put_back:
        if not j or j["state"] != "removed":
            raise farm.FarmError("This plate isn't removed")
        state = "reprint"  # back in the queue, not started
    else:
        if j and j["state"] in farm.ACTIVE:
            raise farm.FarmError(f"It's on its way ({j['state']}) — cancel it in Bambuddy first")
        state = "removed"
    now = db.now()
    with db.tx() as con:
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,updated_at,"
                    "computer_id) VALUES (?,?,?,?,0,'',?,?,?,?)",
                    (batch_id, file, plate["kind"], (plate.get("colors") or [""])[0], state, now, now, computer.ID))
    return {"ok": True}


def slicing_summary() -> dict:
    """For the Printer status page: what Bambuddy is slicing for SnapSTL right now and recently."""
    with db.tx() as con:
        rs = db.rows(con, "SELECT s.*, b.summary FROM slices s LEFT JOIN batches b ON b.id=s.batch_id "
                          "WHERE s.computer_id=? ORDER BY s.id DESC LIMIT 40", computer.ID)
    items = []
    for r in rs:
        label = json.loads(r.pop("summary") or "{}").get("label") or f"Batch #{r['batch_id']}"
        items.append({"batch": label, "plate": r["plate"], "kind": r["kind"], "color": r["color"], "state": r["state"],
                      "progress": r["progress"], "error": r["error"], "updated_at": r["updated_at"],
                      "presets": {k: v.get("name") for k, v in json.loads(r["presets"] or "{}").items()}})
    count = lambda st: sum(1 for i in items if i["state"] == st)  # noqa: E731
    saved = {k: v["name"] for k, v in farm.chosen_presets().items()}
    return {"presets": saved or {k: f"{v} (Bambu standard)" for k, v in farm.DEFAULT_PRESETS.items()}, "items": items,
            "counts": {st: count(st) for st in ("queued", "uploading", "slicing", "done", "failed")}}


def update_made(batch_id: int) -> None:
    """Orders whose every base plate and name plate is printed → Ready to ship (made_at); else not."""
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b is None:
            return
        b = dict(b)
        plates = json.loads(b.get("summary") or "{}").get("plates") or []
        orders = _batch_orders(con, b)
        on = plate_order_ids(plates, orders)
        done = {r["plate"] for r in db.rows(
            con, "SELECT j.plate FROM print_jobs j WHERE j.batch_id=? AND j.id = (SELECT MAX(id) FROM print_jobs k "
                 "WHERE k.batch_id=j.batch_id AND k.plate=j.plate) AND j.state='done'", batch_id)}
        kinds: dict[str, dict[str, bool]] = {}
        for p in plates:
            for oid in on.get(p["file"], []):
                k = kinds.setdefault(oid, {"bases": True, "names": True, "seen_b": False, "seen_n": False})
                k["seen_b" if p["kind"] == "bases" else "seen_n"] = True
                k[p["kind"]] = k[p["kind"]] and p["file"] in done
        now = db.now()
        for oid, k in kinds.items():
            made = k["seen_b"] and k["seen_n"] and k["bases"] and k["names"]
            if made:
                con.execute("UPDATE orders SET made_at=COALESCE(made_at, ?), updated_at=? WHERE order_id=? AND state='bundled'",
                            (now, now, oid))
            else:
                con.execute("UPDATE orders SET made_at=NULL WHERE order_id=? AND made_at IS NOT NULL AND state='bundled'",
                            (oid,))
