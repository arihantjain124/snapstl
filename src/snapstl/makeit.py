"""Make It tab: the plates of built batches, waiting to be printed, by Bases and by Names.

* **Bases**: every base plate still to print, with a printer pre-selected — the next *available*
  printer (connected, idle, bed cleared, nothing of ours on its way) with white loaded, the one with
  the most filament left on that spool first.
* **Names**: every name plate with its colour; pre-selected only when an available printer has that
  colour loaded (read from Bambuddy).
* A print starts only when a person presses **Start print**: the plate is sliced if needed, uploaded,
  queued on that printer held, and then released — all from that one click (`farm.Worker`). Nothing
  else starts a print.
* A printed plate goes to **Verify** (0.24): a person checks each base / name (verify.py); spoiled ones
  get a fix plate (extras.py), a failed plate goes back to the list. **Completed** lists checked names.
* **Bed clear**: a print is released only when its printer is free and a person confirmed the bed is
  empty (Start print asks when the printer shows a finished print); a busy printer keeps it held.
* **Merge** (Names): same-colour plates not started, ≤ 3 names, onto one plate (extras.py).
* **Slice** (Make It → Slice): plates are sliced by Bambuddy's slicer with the presets chosen there,
  one at a time in the background; each row shows Sliced / Slicing % / failed. Start print sends the
  sliced file, or slices first and sends it when that's done.
* **Remove** takes a plate off Bases / Names (e.g. printed by hand); **Removed** lists them with
  **Put back**. Nothing is deleted.
* An order is **made** (`orders.made_at`, Orders → Printed – pack it) once its base and name are checked
  good on Verify.

Which plates are listed: those of this computer's built batches whose orders are still Bundled (not
yet marked printed / ready), plus any plate with a print on its way.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import computer, db, extras, farm, spares, verify

AVAILABLE_STATES = {"IDLE", "FINISH", "FAILED", ""}


# ---------------- plates ↔ orders ----------------
# the website packed / shipped / delivered it (or it's coming back): made, wherever (owner, 0.26) — it leaves
# Print, Verify and the base / letters marks even if SnapSTL never printed it
_ST = "REPLACE(REPLACE(UPPER(COALESCE(source_status,'')),'_','-'),' ','-')"
SHIPPED_SQL = f"({_ST} IN ('PACK','HANDOVER','IN-TRANSIT','DELIVERED') OR {_ST} LIKE 'RTO%')"


def _batch_orders(con, b: dict) -> list[dict]:
    ids = json.loads(b.get("order_ids") or "[]")
    if not ids:
        return []
    return db.rows(con, f"SELECT order_id, name, color, CASE WHEN state='bundled' AND {SHIPPED_SQL} THEN 'shipped' "
                        f"ELSE state END AS state, quantity, made_at, express_by FROM orders "
                        f"WHERE order_id IN ({','.join('?' * len(ids))})", *ids)


def plate_order_ids(plates: list[dict], orders: list[dict]) -> dict[str, list[str]]:
    """Plate file → order ids on it. New builds record them; older ones are matched by name (and colour
    for name plates), one copy at a time."""
    out: dict[str, list[str]] = {}
    for kind in ("bases", "names", "stands"):  # stands (0.32) always record their order ids
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
    """Pre-selected printer per plate: an *available* printer whose loaded filament fits the colour best
    (exact, then same family, then nearest), then the most filament left. Each pick makes that printer busy for
    the next plate, so plates spread over the free printers."""
    load = {p["id"]: active.get(p["id"], 0) for p in prs}
    out: dict[str, int | None] = {}
    for pl in plates:
        if pl.get("job_active"):
            continue
        need = farm.needed_color(pl)
        ok = [p for p in prs if p.get("active", True) and available(p, load[p["id"]])]
        if not ok:
            out[pl["file"]] = None
            continue

        def fit(p, need=need, pl=pl):  # how well its best slot fits the colour (a name stand: both colours)
            if pl.get("kind") == farm.STANDS:
                return _stand_fit(pl, p)
            t = farm.best_tray(need, p)
            sp = next((x for x in p.get("spools") or [] if x["tray"] == t), None)
            return farm.tray_score(need, sp) if sp else (-1, -10 ** 7)

        best = min(ok, key=lambda p: (-fit(p)[0], -fit(p)[1], -spool_left(p, need), p["name"]))
        out[pl["file"]] = best["id"]
        load[best["id"]] += 1
    return out


def _stand_fit(pl: dict, p: dict) -> tuple:
    """How well a printer's AMS fits a name stand: both colours (block, letters) — the worse of the two."""
    need, need2 = farm.needed_color(pl), farm.needed_color2(pl) or ""
    t = farm.best_tray(need, p)
    sp = next((x for x in p.get("spools") or [] if x["tray"] == t), None)
    a = farm.tray_score(need, sp) if sp else (-1, -10 ** 7)
    rest = [x for x in p.get("spools") or [] if x["tray"] != t]
    b = max((farm.tray_score(need2, x) for x in rest), default=(-1, -10 ** 7))
    return min(a, b)


def _verify() -> dict:
    from . import staff

    v = verify.name_cards()
    with db.tx() as con:
        on = staff.pins_on(con)
    return {"verify": v["cards"], "verify_waiting_other": v["waiting_other"], "pins_on": on}


def printers_now(prs: list[dict], active_jobs: dict[int, int]) -> dict:
    """Print's top line: printers free for the next print (with the filament loaded), beds to clear, the ones
    printing (with % and minutes left), offline."""
    free, clear, busy, offline, printing = [], [], 0, 0, []
    for p in prs:
        if not p.get("active", True):
            continue
        st = p.get("status") or {}
        if not st.get("connected"):
            offline += 1
        elif st.get("awaiting_plate_clear"):
            clear.append(p["name"])
        elif available(p, active_jobs.get(p["id"], 0)):
            free.append({"id": p["id"], "name": p["name"],
                         "spools": [{"color": s.get("color") or s.get("name") or "", "hex": s.get("hex") or ""}
                                    for s in p.get("spools") or []]})
        else:
            busy += 1
            if st.get("progress") is not None and str(st.get("state", "")).upper() in farm.BUSY_STATES:
                printing.append({"id": p["id"], "name": p["name"], "progress": st.get("progress"),
                                 "remaining": st.get("remaining_time"), "state": st.get("state")})
    return {"free": free, "clear": clear, "busy": busy, "offline": offline, "printing": printing}


def active_plates() -> dict[int, dict]:
    """Printer id → the SnapSTL plate it has now (sent or printing): its kind, colour and names."""
    out: dict[int, dict] = {}
    with db.tx() as con:
        jobs = db.rows(con, f"SELECT * FROM print_jobs WHERE state IN ({','.join('?' * len(farm.ACTIVE))}) "
                            "AND computer_id=? AND printer_id<>0 ORDER BY id", *farm.ACTIVE, computer.ID)
        summ = {}
        for j in jobs:
            if j["batch_id"] not in summ:
                b = con.execute("SELECT summary FROM batches WHERE id=?", (j["batch_id"],)).fetchone()
                summ[j["batch_id"]] = json.loads((b and b[0]) or "{}")
            p = next((x for x in summ[j["batch_id"]].get("plates") or [] if x["file"] == j["plate"]), None)
            names = (p or {}).get("names")
            if p is None:
                x = con.execute("SELECT items FROM extra_plates WHERE batch_id=? AND file=?",
                                (j["batch_id"], j["plate"])).fetchone()
                names = [i.get("name") for i in json.loads((x and x[0]) or "[]")]
            cur = out.get(j["printer_id"])
            if cur is None or j["state"] == "printing":  # what it prints now wins over what waits
                out[j["printer_id"]] = {"kind": j["kind"], "color": j["color"] or "", "names": names or [],
                                        "file": j["plate"].rsplit("/", 1)[-1], "state": j["state"],
                                        "held": bool(j.get("held"))}
    return out


# ---------------- the board ----------------
def board(prs: list[dict] | None = None) -> dict:
    """Bases and Names to print (with printer choices and pre-selections), Verify, Completed, Removed."""
    slices = farm.latest_slices()
    with db.tx() as con:
        batches = _batches(con)
        latest: dict[tuple[int, str], dict] = {}
        for j in db.rows(con, "SELECT * FROM print_jobs WHERE computer_id=? ORDER BY id", computer.ID):
            if farm.replaces(latest.get((j["batch_id"], j["plate"])), j):
                latest[(j["batch_id"], j["plate"])] = j
        mine = {b["id"]: b for b in batches}
        extra_rows = [x for x in verify.extra_plates(con) if x["batch_id"] in mine]
        open_ids = {r[0] for r in con.execute(f"SELECT order_id FROM orders WHERE state='bundled' AND NOT {SHIPPED_SQL}")}
        express = {r[0]: r[1] for r in con.execute("SELECT order_id, express_by FROM orders WHERE express_by IS NOT NULL "
                                                   "AND state='bundled'")}
        rows, removed, printed = [], [], []

        def add(b, summ, p, ids, extra=None):
            j = latest.get((b["id"], p["file"]))
            label = summ.get("label") or f"Batch #{b['id']}"
            open_orders = [i for i in ids if i in open_ids]
            if j and j["state"] in ("merged", "divided"):
                return  # printed on a merged plate, or split into smaller plates
            if j and j["state"] == "removed":
                if open_orders:  # hidden once its orders are packed / shipped / cancelled
                    removed.append({"batch_id": b["id"], "batch": label, "file": p["file"], "kind": p["kind"],
                                    "names": p.get("names", []), "colors": p.get("colors", []),
                                    "removed_at": j["updated_at"], "extra": extra})
                return
            active = bool(j and j["state"] in farm.ACTIVE)
            done = bool(j and j["state"] == "done")
            if done and open_orders:  # Bases → Printed / Letters → Printed (whole plate failed is there)
                printed.append({"batch_id": b["id"], "batch": label, "file": p["file"], "kind": p["kind"],
                                "names": p.get("names", []), "colors": p.get("colors", []), "order_ids": ids,
                                "open_items": open_orders, "extra": extra, "printer": j.get("printer_name") or "",
                                "done_at": j.get("updated_at")})
            if done or not (open_orders or active):
                return  # printed (→ Verify) or its orders are made / gone
            rows.append({
                "batch_id": b["id"], "batch": label, "file": p["file"], "kind": p["kind"],
                "names": p.get("names", []), "colors": p.get("colors", []), "order_ids": ids,
                "open_items": open_orders, "extra": extra, "sliced": False, "slice": None,
                "job": _job(j), "job_active": active, "done": False, "need": farm.needed_color(p),
                # earliest Express needed-by date of its orders still to finish (None = not express)
                "express_by": min((express[i] for i in open_orders if i in express), default=None),
            })

        for b in batches:
            summ = json.loads(b.get("summary") or "{}")
            plates = summ.get("plates") or []
            on = plate_order_ids(plates, _batch_orders(con, b))
            for p in plates:
                add(b, summ, p, on.get(p["file"], []))
        for x in extra_rows:
            b = mine[x["batch_id"]]
            p = {"kind": x["part"], "file": x["file"],
                 "colors": [x["color"]] if x["part"] == "names" else ((x["color"] or "").split("+")
                                                                        if x["part"] == farm.STANDS else []),
                 "names": [i.get("name") + (f" ({i['letters']})" if i.get("letters") else "") for i in x["items"]]}
            add(b, json.loads(b.get("summary") or "{}"), p, [i.get("order_id") for i in x["items"]], x["kind"])
        parts = verify.part_status(con, batches)
        dupes = extras.duplicates(con)  # 0.28: the same names on two plates (a double press) — Remove the copy
        stock = spares.in_stock_orders(con, [o for r in rows for o in r["open_items"]])  # a spare piece fits
    # how far each order's other part is: Letters splits into "base printed" / "base still to print",
    # Bases → Printed shows how far the letters are
    for r in rows:
        r["in_stock"] = [o for o in r["open_items"] if o in stock]
    for r in rows + printed:
        # the order's other part (a name stand is one piece: its own status)
        other = r["kind"] if r["kind"] == farm.STANDS else ("bases" if r["kind"] == "names" else "names")
        r["other"] = {o: parts.get(o, {}).get(other, "todo") for o in r["open_items"]}
        r["other_ready"] = sum(s in ("waiting", "good") for s in r["other"].values())
    # a printed plate stays listed while something on it isn't checked yet or its other part isn't printed
    printed = [r for r in printed if any(parts.get(o, {}).get(r["kind"]) != "good" for o in r["open_items"])
               or r["other_ready"] < len(r["open_items"])]
    printed.sort(key=lambda r: -(r["done_at"] or 0))
    prs = prs or []
    # fix plates first, then plates holding an Express order (earliest needed-by date), the rest as before;
    # free printers are pre-selected for them first
    pending = sorted(rows, key=lambda r: (r["extra"] != "fix", r["express_by"] is None, r["express_by"] or ""))
    active_jobs = farm.active_counts()
    fcfg = farm.load_config()
    beds = {p["id"]: bed_state(p) for p in prs}  # 0.28.1: once per printer, not per plate × printer
    for r in pending:  # letters only on printers with a Letters profile, once any has one (0.27)
        r["choices"] = [{"id": p["id"], "name": p["name"], "why_not": "", "note": farm.match_note(r, p),
                         "available": available(p, active_jobs.get(p["id"], 0)), "left": spool_left(p, r["need"]),
                         "online": bool((p.get("status") or {}).get("connected")),
                         "state": (p.get("status") or {}).get("state", ""), "bed": beds[p["id"]],
                         "trays": farm.tray_choices(r["need"], p), "best_tray": farm.best_tray(r["need"], p),
                         **(_stand_trays(r, p) if r["kind"] == farm.STANDS else {})}
                        for p in prs if farm.may_print(p["id"], r["kind"], fcfg)
                        and (r["kind"] != farm.STANDS or farm.has_ams(p))]
    pick = {}
    if prs:
        for kind in ("bases", "names", farm.STANDS):  # each kind only onto the printers that may print it
            rows_k = [r for r in pending if r["kind"] == kind]
            pick |= choose(rows_k, [p for p in prs if farm.may_print(p["id"], kind, fcfg)
                                    and (kind != farm.STANDS or farm.has_ams(p))], active_jobs)
    keys: dict = {}

    def slice_state(r: dict, pid) -> dict | None:
        """This plate's slice for a printer's profiles (None = not sliced for it yet)."""
        if not pid:
            return None
        k = (pid, r["kind"])
        if k not in keys:
            keys[k] = (farm.target(pid, fcfg, r["kind"])[0], farm.missing_presets(pid, fcfg, r["kind"]))
        key, missing = keys[k]
        if missing:
            return {"state": "needs_presets", "printer": missing}
        x = farm.slice_for(slices, r["batch_id"], r["file"], key)
        return {"state": x["state"], "progress": x["progress"], "error": x["error"]} if x else None

    for r in pending:
        r["suggested"] = pick.get(r["file"])
        for c in r["choices"]:  # the slice for each printer: the row shows the one for the printer selected
            c["slice"] = slice_state(r, c["id"])
        # its job's printer, else the pre-selected one (what the row shows before a printer is picked)
        pid = ((r["job"] or {}).get("printer_id") if r["job_active"] else None) or r["suggested"]
        r["slice_for"] = pid
        r["slice"] = slice_state(r, pid)
        r["sliced"] = bool(r["slice"] and r["slice"]["state"] == "done")
    names = [r for r in pending if r["kind"] == "names"]
    return {"bases": [r for r in pending if r["kind"] == "bases"], "names": names, "printed_bases": [r for r in printed if r["kind"] == "bases"],
            "printed_letters": [r for r in printed if r["kind"] == "names"],
            # name stands (0.32): one plate kind, two colours
            "stands": [r for r in pending if r["kind"] == farm.STANDS],
            "printed_stands": [r for r in printed if r["kind"] == farm.STANDS],
            "now": printers_now(prs, active_jobs),
            "merge": extras.suggestions(names), "duplicates": dupes, **_verify(), "completed": verify.completed(),
            "removed": removed, "staff": verify.staff_list()}


def _job(j: dict | None) -> dict | None:
    if not j:
        return None
    return {"id": j["id"], "state": j["state"], "printer_id": j["printer_id"], "printer_name": j["printer_name"],
            "held": bool(j.get("held")), "progress": j.get("progress"), "error": j.get("error"),
            "updated_at": j.get("updated_at"), "options": json.loads(j.get("options") or "{}"),
            "speed": {v: k for k, v in farm.SPEEDS_SET.items()}.get(j.get("speed") or 0, ""),
            "ams_tray": j.get("ams_tray"), "ams_tray2": j.get("ams_tray2")}


# ---------------- actions (each one a person's click) ----------------
def _stand_trays(r: dict, p: dict) -> dict:
    """A name stand's letters slot list on a printer (the block's is "trays")."""
    need2 = farm.needed_color2(r) or ""
    best = farm.best_tray(r["need"], p)
    rest = {**p, "spools": [x for x in p.get("spools") or [] if x["tray"] != best]}
    return {"trays2": farm.tray_choices(need2, p), "best_tray2": farm.best_tray(need2, rest), "need2": need2}


def _plate(batch_id: int, file: str) -> tuple[dict, dict]:
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=? AND COALESCE(computer_id, ?) = ?",
                        (batch_id, computer.ID, computer.ID)).fetchone()
    if b is None:
        raise farm.FarmError("This batch isn't on this computer")
    b = dict(b)
    plate = next((p for p in json.loads(b.get("summary") or "{}").get("plates", []) if p["file"] == file), None)
    if plate is None:
        with db.tx() as con:
            x = con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND file=? AND state='active'",
                            (batch_id, file)).fetchone()
        if x is None:
            raise farm.FarmError("Not a plate of this batch")
        x = dict(x)
        items = json.loads(x["items"])
        plate = {"kind": x["part"], "file": x["file"], "names": [i.get("name") for i in items],
                 "colors": ([x["color"]] if x["part"] == "names" else
                            (x["color"] or "").split("+") if x["part"] == farm.STANDS else []),
                 "order_ids": [i.get("order_id") for i in items], "extra": x["kind"]}
    return b, plate


def _latest(batch_id: int, file: str) -> dict | None:
    """The plate's job (its newest, unless a cancelled copy would hide one still on its way — farm.replaces)."""
    with db.tx() as con:
        rows = db.rows(con, "SELECT * FROM print_jobs WHERE batch_id=? AND plate=? AND computer_id=? ORDER BY id",
                       batch_id, file, computer.ID)
    return farm.current_job(rows)


_START = threading.Lock()  # 0.28.1: one Start print at a time, so a double click can't send a plate twice


class BedNotClear(farm.FarmError):
    """The printer may still hold a finished print: a person must confirm the bed is empty."""


def bed_state(printer: dict) -> str:
    """'busy' (printing now) | 'check' (a finished print may still be on the bed) | 'clear'."""
    st = printer.get("status") or {}
    state = str(st.get("state", "")).upper()
    if state in farm.BUSY_STATES:
        return "busy"
    if st.get("awaiting_plate_clear"):
        return "check"
    if state in ("FINISH", "FAILED"):
        with db.tx() as con:
            c = con.execute("SELECT at FROM bed_clears WHERE printer_id=?", (printer["id"],)).fetchone()
            last = con.execute("SELECT MAX(updated_at) FROM print_jobs WHERE printer_id=? AND state IN "
                               "('done','failed','cancelled','printing')", (printer["id"],)).fetchone()[0]
        if c is None or (last is not None and last > c[0]):
            return "check"
    return "clear"


def confirm_bed_clear(printer_id: int, awaiting: bool = False) -> None:
    """A person confirmed the bed of this printer is empty (Start print, or Printers → Clear plate)."""
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO bed_clears(printer_id, at) VALUES (?, ?)", (printer_id, db.now()))
    if awaiting:
        farm.clear_plate(printer_id)


def start_print(batch_id: int, file: str, printer_id: int, options: dict | None, prs: list[dict],
                force: bool = False, speed: str = "", tray: int | None = None, bed_clear: bool = False,
                tray2: int | None = None) -> dict:
    with _START:
        return _start_print(batch_id, file, printer_id, options, prs, force, speed, tray, bed_clear, tray2)


def _start_print(batch_id: int, file: str, printer_id: int, options: dict | None, prs: list[dict],
                 force: bool = False, speed: str = "", tray: int | None = None, bed_clear: bool = False,
                 tray2: int | None = None) -> dict:
    """Start print (a person's click): the only way a Make It plate gets printed.
    Not sent yet / failed / cancelled / reprint → sent to that printer and started once queued.
    Waiting held in Bambuddy → moved to that printer if changed, then started.
    The bed must be clear: a printer with a finished print asks first (`bed_clear` = the person confirmed);
    a busy printer gets the plate held in its queue (press Start print again once it's done and empty)."""
    b, plate = _plate(batch_id, file)
    # the printer's print defaults (Slicing), with what was set on this plate on top
    dflt = farm.print_defaults(int(printer_id), kind=plate["kind"]) if printer_id else {}  # letters: their speed
    speed = speed or dflt.get("speed", "")
    opts = farm.clean_options({**{k: v for k, v in dflt.items() if k != "speed"},
                               **{k: v for k, v in (options or {}).items() if v not in ("", None)}})
    if speed and speed not in farm.SPEEDS_SET:
        raise farm.FarmError("Speed is silent, standard, sport or ludicrous")
    spd = farm.SPEEDS_SET.get(speed)
    printer = next((p for p in prs if p["id"] == int(printer_id)), None)
    if printer is None:
        raise farm.FarmError("Choose a printer")
    tray = farm.pick_tray(plate, printer, tray)  # the filament slot it prints from (any loaded one)
    tray2 = farm.pick_tray2(plate, printer, tray2, tray)  # a name stand: the letters' slot (else None)
    if not farm.may_print(printer["id"], plate["kind"]):
        raise farm.FarmError(f"{printer['name']} has no Letters profile — letters print on the printers that have one "
                             "(Slicing → Letters)")
    missing = farm.missing_presets(printer["id"], kind=plate["kind"])
    if missing:
        raise farm.FarmError(f"Pick the slicing profiles for {missing} in Slicing first")
    j = _latest(batch_id, file)
    if j and j["state"] == "merged":
        raise farm.FarmError("This plate is part of a merged plate — print that one, or Split back first")
    if j and j["state"] == "divided":
        raise farm.FarmError("This plate was split into smaller plates — print those, or Join back first")
    if plate.get("extra") in ("merged", "divided"):
        with db.tx() as con:
            copy = next((d for d in extras.duplicates(con) if d["batch_id"] == batch_id and d["file"] == file), None)
        if copy:
            raise farm.FarmError(f"These names are already on {Path(copy['of']['file']).name} — this plate is a copy. "
                                 "Press Remove on it instead of printing it again.")
    bed = bed_state(printer)
    if bed == "check":
        if not bed_clear:
            raise BedNotClear(f"{printer['name']} may still have a finished print on its bed. Take it off and "
                              "clean the plate, then confirm.")
        confirm_bed_clear(printer["id"], bool((printer.get("status") or {}).get("awaiting_plate_clear")))
        bed = "clear"
    if j and j["state"] == "sent" and j.get("held"):
        if bed == "busy":
            raise farm.FarmError(f"{printer['name']} is still printing — press Start print once it's done and the "
                                 "bed is empty")
        if j["printer_id"] != printer["id"] or opts or tray != j.get("ams_tray") or tray2 != j.get("ams_tray2"):
            farm.print_job(j["id"], printer["id"], opts, prs, tray=tray, tray2=tray2)  # move / options / slot while held
        if spd:
            farm._set(j["id"], speed=spd, speed_set=0, speed_tries=0)
        farm.start_job(j["id"], force=force)
        update_made(batch_id)  # its orders → Printing
        return {"job_id": j["id"], "started": True}
    if j and j["state"] in farm.ACTIVE:
        raise farm.FarmError(f"Already on its way ({j['state']}) on {j['printer_name']}")
    if j and j["state"] == "done":
        raise farm.FarmError("Already printed — use Reprint in Completed if it failed")
    now = db.now()
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,options,autostart,speed,"
            "ams_tray,ams_tray2,created_at,updated_at,computer_id) VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)",
            (batch_id, file, plate["kind"], farm.plate_color(plate), printer["id"], printer["name"],
             json.dumps(opts), 0 if bed == "busy" else 1, spd, tray, tray2, now, now, computer.ID))
        db.add_activity(con, "print", f"Start print: {Path(file).name} on {printer['name']}"
                        + (" (held: the printer is busy)" if bed == "busy" else ""))
    update_made(batch_id)  # its orders → Printing
    farm.WORKER.kick()
    return {"job_id": int(cur.lastrowid), "started": False, "held": bed == "busy"}


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


def printed_by_hand(batch_id: int, file: str) -> dict:
    """Removed (or waiting) plate printed by hand: it goes to Verify like any printed plate."""
    _, plate = _plate(batch_id, file)
    j = _latest(batch_id, file)
    if j and j["state"] in farm.ACTIVE:
        raise farm.FarmError(f"It's on its way ({j['state']}) — cancel it in Bambuddy first")
    if j and j["state"] == "done":
        raise farm.FarmError("Already printed — check it on Verify")
    now = db.now()
    with db.tx() as con:
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,progress,created_at,"
                    "updated_at,computer_id) VALUES (?,?,?,?,0,'By hand','done',100,?,?,?)",
                    (batch_id, file, plate["kind"], (plate.get("colors") or [""])[0], now, now, computer.ID))
        db.add_activity(con, "print", f"Printed by hand: {Path(file).name}")
    update_made(batch_id)
    return {"ok": True}


def remove(batch_id: int, file: str, put_back: bool = False) -> dict:
    """Take a plate off Bases / Names (nothing is deleted), or put it back."""
    _, plate = _plate(batch_id, file)
    j = _latest(batch_id, file)  # 0.28: merged plates and split parts can be removed too (e.g. a copy)
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
                      "presets": {k: v.get("name") if isinstance(v, dict) else v
                                  for k, v in json.loads(r["presets"] or "{}").items()}})
    count = lambda st: sum(1 for i in items if i["state"] == st)  # noqa: E731
    cfg = farm.load_config()
    by_printer = {pid: ({k: (v["name"] if isinstance(v, dict) else v) for k, v in p.items()})
                  for pid, p in (cfg.get("presets_by_printer") or {}).items()}
    return {"presets_by_printer": by_printer, "items": items,
            "counts": {st: count(st) for st in ("queued", "uploading", "slicing", "done", "failed")}}


def queues(recent: int = 15) -> dict:
    """The Print page's queue panel: what's waiting / running now (first) and the last few finished, for
    downloading (models from MakerWorld), building (batches), slicing (Bambuddy's slicer) and printing."""
    from .batches import ACTIVE as BUILD_ACTIVE

    def label(summary) -> str | None:
        try:
            return json.loads(summary or "{}").get("label")
        except ValueError:
            return None

    with db.tx() as con:
        dl = db.rows(con, "SELECT d.id, d.name, d.compartments, d.state, d.error, d.updated_at, d.batch_id, b.summary "
                          "FROM downloads d LEFT JOIN batches b ON b.id=d.batch_id WHERE d.computer_id=? AND "
                          "d.state IN ('claimed','queued','failed') ORDER BY CASE d.state WHEN 'claimed' THEN 0 "
                          "WHEN 'queued' THEN 1 ELSE 2 END, d.id LIMIT 300", computer.ID)
        dl += db.rows(con, "SELECT d.id, d.name, d.compartments, d.state, d.error, d.updated_at, d.batch_id, b.summary "
                           "FROM downloads d LEFT JOIN batches b ON b.id=d.batch_id WHERE d.computer_id=? AND "
                           "d.state='done' ORDER BY d.updated_at DESC LIMIT ?", computer.ID, recent)
        q = ",".join("?" * len(BUILD_ACTIVE))
        bl = db.rows(con, f"SELECT id, status, summary, progress, created_at, finished_at FROM batches WHERE "
                          f"COALESCE(computer_id, ?)=? AND status IN ({q}) ORDER BY id", computer.ID, computer.ID, *BUILD_ACTIVE)
        bl += db.rows(con, f"SELECT id, status, summary, progress, created_at, finished_at FROM batches WHERE "
                           f"COALESCE(computer_id, ?)=? AND status NOT IN ({q}, 'created') ORDER BY id DESC LIMIT ?",
                      computer.ID, computer.ID, *BUILD_ACTIVE, recent)
        pq = ",".join("?" * len(farm.ACTIVE))
        pj = db.rows(con, f"SELECT j.id, j.batch_id, j.plate, j.kind, j.color, j.printer_name, j.state, j.progress, "
                          f"j.error, j.updated_at, b.summary FROM print_jobs j LEFT JOIN batches b ON b.id=j.batch_id "
                          f"WHERE j.computer_id=? AND j.state IN ({pq}) ORDER BY j.id", computer.ID, *farm.ACTIVE)
        pj += db.rows(con, f"SELECT j.id, j.batch_id, j.plate, j.kind, j.color, j.printer_name, j.state, j.progress, "
                           f"j.error, j.updated_at, b.summary FROM print_jobs j LEFT JOIN batches b ON b.id=j.batch_id "
                           f"WHERE j.computer_id=? AND j.state NOT IN ({pq}) ORDER BY j.updated_at DESC LIMIT ?",
                      computer.ID, *farm.ACTIVE, recent)
    for r in dl + pj:
        r["batch"] = label(r.pop("summary")) or (f"Batch #{r['batch_id']}" if r.get("batch_id") else "")
    builds = []
    for b in bl:
        try:
            prog = json.loads(b.get("progress") or "{}") or {}
        except ValueError:
            prog = {}
        builds.append({"id": b["id"], "batch": label(b["summary"]) or f"Batch #{b['id']}", "status": b["status"],
                       "models": prog.get("models"), "ready": prog.get("ready"), "failed": prog.get("failed"),
                       "at": b["finished_at"] or b["created_at"]})
    live = ("queued", "uploading", "slicing")
    sl = slicing_summary()["items"]
    sl = [i for i in sl if i["state"] in live][::-1] + [i for i in sl if i["state"] not in live][:recent]
    return {"downloading": dl, "building": builds, "slicing": sl, "printing": pj,
            "counts": {"downloading": sum(1 for r in dl if r["state"] in ("claimed", "queued")),
                       "building": sum(1 for b in builds if b["status"] in BUILD_ACTIVE),
                       "slicing": sum(1 for i in sl if i["state"] in live),
                       "printing": sum(1 for r in pj if r["state"] in farm.ACTIVE)}}


def backfill_started() -> int:
    """Once after updating to 0.23: Printing / made for orders of this computer's built batches from the
    print jobs they already have. Returns how many batches were checked."""
    with db.tx() as con:
        if con.execute("SELECT 1 FROM sync_state WHERE key='started_v1'").fetchone():
            return 0
        ids = [b["id"] for b in _batches(con)]
    for bid in ids:
        update_made(bid)
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('started_v1', '1')")
    return len(ids)


def update_made(batch_id: int) -> None:
    """Orders of this batch (and of batches sharing a merged plate with it): Printing once a plate holding
    them is started; *waiting for check* once every plate is printed; made (Printed – pack it) once their
    base and name are checked good on Verify (verify.update_orders)."""
    verify.update_orders(batch_id)


