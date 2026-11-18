"""Print → Verify (0.24): a person checks every printed base and name before the order is packed.

Words used here:

* **item** — one base or one name of one copy of an order, on its own plate in its batch: the key is
  (batch id, plate file, position on that plate). It never changes, even when the item is printed on a
  merged plate or again on a fix plate.
* **carrier** — the plate an item is printed on now: its own plate, or a merged / fix plate
  (`extra_plates`). The newest active extra plate holding the item wins, unless the item was checked
  after it was made.
* **check** — a row in `plate_checks` (who, when, Good or what went wrong). The newest check of an item
  counts. Checks are shared with every computer and never edited.

An order is **made** (`made_at`, Orders → Printed – pack it) only when every item of it (its base and its
name, each copy) is checked Good. When every item is printed but not all are checked, the order shows
*Printed – waiting for check* (`print_done_at`). Everything printed before 0.24 counts as checked
("Before 0.24", owner decision 2026-11-18).
"""
from __future__ import annotations

import json
import uuid

from . import computer, db

BEFORE = "Before 0.24"
RESULTS = {
    "good": "Good",
    "letters": "Letter(s) spoiled",
    "name": "Whole name spoiled",
    "colour": "Wrong colour",
    "base": "Base broken or warped",
    "plate_failed": "Whole plate failed",
}
FIX_RESULTS = ("letters", "name", "colour", "base")  # a small fix plate is made for these
NOT_STARTED = ("removed", "merged", "unmerged")      # print job states that don't mean "a print started"
MAX_FIXES_WARN = 2                                   # a third fix of one item: check the model


# ---------------- plates and items ----------------
def plate_items(batch_id: int, plate: dict, order_ids: list[str]) -> list[dict]:
    """The items of one of a batch's own plates, in plate order."""
    names = plate.get("names") or []
    colors = plate.get("colors") or []
    out = []
    for i, oid in enumerate(order_ids):
        out.append({"batch_id": batch_id, "plate": plate["file"], "idx": i, "order_id": oid,
                    "part": plate["kind"], "name": names[i] if i < len(names) else "",
                    "color": (colors[0] if plate["kind"] == "names" and colors else "")})
    return out


def key(it: dict) -> tuple[int, str, int]:
    return (int(it["batch_id"]), it["plate"], int(it["idx"]))


def latest_checks(con, batch_ids) -> dict[tuple, dict]:
    ids = sorted({int(b) for b in batch_ids if b is not None})
    if not ids:
        return {}
    out: dict[tuple, dict] = {}
    for r in db.rows(con, f"SELECT * FROM plate_checks WHERE batch_id IN ({','.join('?' * len(ids))}) "
                          "ORDER BY at, rowid", *ids):
        out[(int(r["batch_id"]), r["plate"], int(r["idx"]))] = r
    return out


def fix_counts(con, batch_ids) -> dict[tuple, int]:
    ids = sorted({int(b) for b in batch_ids if b is not None})
    if not ids:
        return {}
    return {(int(r["batch_id"]), r["plate"], int(r["idx"])): r["n"] for r in db.rows(
        con, f"SELECT batch_id, plate, idx, COUNT(*) n FROM plate_checks WHERE batch_id IN "
             f"({','.join('?' * len(ids))}) AND result IN ({','.join('?' * len(FIX_RESULTS))}) GROUP BY 1,2,3",
        *ids, *FIX_RESULTS)}


def extra_plates(con, batch_ids=None, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM extra_plates"
    conds, args = [], []
    if active_only:
        conds.append("state='active'")
    if batch_ids is not None:
        ids = sorted({int(b) for b in batch_ids})
        if not ids:
            return []
        conds.append(f"id IN (SELECT plate_id FROM extra_items WHERE batch_id IN ({','.join('?' * len(ids))}))")
        args += ids
    if conds:
        q += " WHERE " + " AND ".join(conds)
    out = db.rows(con, q + " ORDER BY id", *args)
    for x in out:
        x["items"] = json.loads(x.get("items") or "[]")
        x["preview"] = json.loads(x.get("preview") or "null")
    return out


def latest_jobs(con) -> dict[tuple[int, str], dict]:
    out: dict[tuple[int, str], dict] = {}
    for j in db.rows(con, "SELECT * FROM print_jobs WHERE COALESCE(computer_id, ?)=? ORDER BY id", computer.ID, computer.ID):
        out[(j["batch_id"], j["plate"])] = j
    return out


def carrier_of(it: dict, extras_by_item: dict[tuple, list[dict]], chk: dict | None) -> tuple[int, str, dict | None]:
    """(batch id, plate file, extra plate or None) the item is printed on now: after "whole plate failed"
    the plate that failed (printed again); else the newest active extra plate made after its last check."""
    xs = sorted(extras_by_item.get(key(it), []), key=lambda x: -x["created_at"])
    if chk is not None and chk["result"] == "plate_failed":
        for x in xs:
            if x["file"] == chk.get("printed_on"):
                return x["batch_id"], x["file"], x
        return int(it["batch_id"]), it["plate"], None
    for x in xs:
        if chk is None or x["created_at"] >= chk["at"] - 0.001:
            return x["batch_id"], x["file"], x
    return int(it["batch_id"]), it["plate"], None


def item_status(it: dict, chk: dict | None, jobs: dict, extras_by_item: dict) -> dict:
    """good | waiting (printed, not checked yet) | printing | todo, with the carrier."""
    cb, cf, x = carrier_of(it, extras_by_item, chk)
    j = jobs.get((cb, cf))
    if chk and chk["result"] == "good":
        st = "good"
    elif j and j["state"] == "done" and (chk is None or (j["updated_at"] or 0) >= chk["at"] - 0.001):
        st = "waiting"
    elif j and j["state"] not in NOT_STARTED and j["state"] not in ("done", "failed", "cancelled", "reprint"):
        st = "printing"
    else:
        st = "todo"
    return {"status": st, "carrier": (cb, cf), "extra": x, "job": j,
            "started": bool(j and j["state"] not in NOT_STARTED)}


# ---------------- order state from checks ----------------
def _batch(con, batch_id: int) -> dict | None:
    b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    return dict(b) if b else None


def batch_items(con, b: dict) -> tuple[list[dict], dict[str, str]]:
    """Every item of a batch's own plates, and order id → state."""
    from .makeit import _batch_orders, plate_order_ids

    plates = [p for p in json.loads(b.get("summary") or "{}").get("plates") or []]
    orders = _batch_orders(con, b)
    on = plate_order_ids(plates, orders)
    items = []
    for p in plates:
        items += plate_items(b["id"], p, on.get(p["file"], []))
    return items, {o["order_id"]: o["state"] for o in orders}


def _by_item(extras: list[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for x in extras:
        for it in x["items"]:
            out.setdefault(key(it), []).append(x)
    return out


def related_batches(con, batch_id: int) -> set[int]:
    """This batch and every batch with an item on an extra plate kept in it (merged plates)."""
    ids = {int(batch_id)}
    for r in con.execute("SELECT DISTINCT i.batch_id FROM extra_items i JOIN extra_plates p ON p.id=i.plate_id "
                         "WHERE p.batch_id=?", (batch_id,)):
        ids.add(int(r[0]))
    return ids


def update_orders(batch_id: int) -> None:
    """Orders of this batch (and of batches sharing a merged plate with it): Printing / waiting for check
    / made (Printed – pack it), from their print jobs and checks."""
    with db.tx() as con:
        for bid in sorted(related_batches(con, batch_id)):
            _update_one(con, bid)


def _update_one(con, batch_id: int) -> None:
    b = _batch(con, batch_id)
    if b is None:
        return
    items, state_of = batch_items(con, b)
    if not items:
        return
    checks = latest_checks(con, [batch_id])
    extras = _by_item(extra_plates(con, [batch_id]))
    jobs = latest_jobs(con)
    per: dict[str, dict] = {}
    now = db.now()
    for it in items:
        chk = checks.get(key(it))
        s = item_status(it, chk, jobs, extras)
        o = per.setdefault(it["order_id"], {"parts": set(), "st": [], "started": False, "checks": []})
        o["parts"].add(it["part"])
        o["st"].append(s["status"])
        o["started"] = o["started"] or s["started"] or s["status"] in ("good", "waiting")
        if chk:
            o["checks"].append(chk)
    for oid, o in per.items():
        if state_of.get(oid) != "bundled":
            continue
        cur = con.execute("SELECT made_at, print_started_at, print_done_at, checked_at, checked_by FROM orders "
                          "WHERE order_id=?", (oid,)).fetchone()
        if cur is None:
            continue
        if o["started"] and cur["print_started_at"] is None:
            con.execute("UPDATE orders SET print_started_at=?, updated_at=? WHERE order_id=?", (now, now, oid))
        both = o["parts"] == {"bases", "names"}
        good = both and all(s == "good" for s in o["st"])
        printed = both and all(s in ("good", "waiting") for s in o["st"])
        if good:
            last = max((c for c in o["checks"] if c["result"] == "good"), key=lambda c: c["at"])
            want = (cur["print_done_at"] or last["at"], last["at"], last["staff"])
            con.execute("UPDATE orders SET made_at=COALESCE(made_at, ?), updated_at=? WHERE order_id=?",
                        (now, now, oid))
        elif printed:
            want = (cur["print_done_at"] or now, None, None)
            con.execute("UPDATE orders SET made_at=NULL WHERE order_id=? AND made_at IS NOT NULL", (oid,))
        else:
            want = (None, None, None)
            con.execute("UPDATE orders SET made_at=NULL WHERE order_id=? AND made_at IS NOT NULL", (oid,))
        have = (cur["print_done_at"], cur["checked_at"], cur["checked_by"])
        if have != want:
            if any(v is not None for v in want):
                db.set_group(con, oid, "check", print_done_at=want[0], checked_at=want[1], checked_by=want[2])
            else:
                db.set_group(con, oid, "check")


# ---------------- checks ----------------
def add_check(con, it: dict, result: str, staff: str, *, letters: str = "", note: str = "",
              printed_on: str = "", printer: str = "", fix_file: str = "", at: float | None = None) -> dict:
    if result not in RESULTS:
        raise ValueError(f"Unknown result {result!r}")
    uid = con.execute("SELECT uid FROM batches WHERE id=?", (it["batch_id"],)).fetchone()
    row = {"uid": uuid.uuid4().hex, "batch_uid": uid[0] if uid else None, "batch_id": int(it["batch_id"]),
           "plate": it["plate"], "idx": int(it["idx"]), "order_id": it.get("order_id"), "part": it["part"],
           "name": it.get("name"), "color": it.get("color"), "result": result, "letters": letters or None,
           "staff": staff, "note": note or None, "at": at if at is not None else db.now(),
           "printed_on": printed_on or None, "printer": printer or None, "fix_file": fix_file or None,
           "computer": computer.NAME}
    con.execute(f"INSERT INTO plate_checks({', '.join(row)}, sent) VALUES ({', '.join('?' * len(row))}, 0)",
                tuple(row.values()))
    return row


def staff_names(con, all_: bool = False) -> list[str]:
    q = "SELECT name FROM staff" + ("" if all_ else " WHERE hidden=0") + " ORDER BY name COLLATE NOCASE"
    return [r[0] for r in con.execute(q)]


def add_staff(con, name: str) -> str:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Type the name first")
    if name.lower() == BEFORE.lower():
        raise ValueError("Choose another name")
    now = db.now()
    con.execute("INSERT INTO staff(name, added_at, hidden, changed, sent) VALUES (?,?,0,?,0) ON CONFLICT(name) "
                "DO UPDATE SET hidden=0, changed=excluded.changed, sent=0", (name, now, now))
    return name


def hide_staff(con, name: str, hidden: bool = True) -> None:
    con.execute("UPDATE staff SET hidden=?, changed=?, sent=0 WHERE name=?", (int(hidden), db.now(), name))


# ---------------- one-time backfill ----------------
def backfill(force: bool = False) -> int:
    """Once after updating to 0.24: every item already printed (its plate printed, or its order already
    made / past packing) is checked Good by "Before 0.24", so no order changes tab. Returns items marked."""
    from .makeit import _batches

    with db.tx() as con:
        if not force and con.execute("SELECT 1 FROM sync_state WHERE key='checks_v1'").fetchone():
            return 0
        jobs = latest_jobs(con)
        n = 0
        for b in _batches(con):
            checks = latest_checks(con, [b["id"]])
            items, state_of = batch_items(con, b)
            made = {r[0] for r in con.execute(
                f"SELECT order_id FROM orders WHERE order_id IN ({','.join('?' * len(state_of))}) "
                "AND made_at IS NOT NULL", tuple(state_of))} if state_of else set()
            for it in items:
                if key(it) in checks or state_of.get(it["order_id"]) != "bundled":
                    continue  # only orders still being made need a check
                j = jobs.get((b["id"], it["plate"]))
                if it["order_id"] in made or (j and j["state"] == "done"):
                    add_check(con, it, "good", BEFORE, printed_on=it["plate"],
                              printer=(j or {}).get("printer_name") or "", at=(j or {}).get("updated_at") or db.now())
                    n += 1
            if items:
                _update_one(con, b["id"])
        con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('checks_v1', '1')")
        if n:
            db.add_activity(con, "check", f"{n} items printed before 0.24 counted as checked")
    return n


# ---------------- Print → Verify / Completed ----------------
def staff_list() -> list[str]:
    with db.tx() as con:
        return staff_names(con)


def _label(b: dict) -> str:
    return json.loads(b.get("summary") or "{}").get("label") or f"Batch #{b['id']}"


def _plate_preview(b: dict, file: str, extras_list: list[dict]) -> dict | None:
    for p in json.loads(b.get("summary") or "{}").get("plates") or []:
        if p["file"] == file:
            return p.get("preview")
    x = next((x for x in extras_list if x["batch_id"] == b["id"] and x["file"] == file), None)
    return x["preview"] if x else None


def cards() -> list[dict]:
    """Print → Verify: one card per printed plate with items still to check (oldest print first)."""
    from .makeit import _batches

    with db.tx() as con:
        batches = {b["id"]: b for b in _batches(con)}
        jobs = latest_jobs(con)
        exs = extra_plates(con)
        by_item = _by_item(exs)
        checks = latest_checks(con, batches)
        fixes = fix_counts(con, batches)
        info = {r["order_id"]: r for r in db.rows(con, "SELECT order_id, flag, flag_note, express_by FROM orders "
                                                       "WHERE state='bundled'")}
        out: dict[tuple, dict] = {}
        for b in batches.values():
            items, state_of = batch_items(con, b)
            for it in items:
                if state_of.get(it["order_id"]) != "bundled":
                    continue  # packed, shipped or gone: nothing to check
                s = item_status(it, checks.get(key(it)), jobs, by_item)
                if s["status"] != "waiting":
                    continue
                cb, cf = s["carrier"]
                if cb not in batches:
                    continue
                j = s["job"] or {}
                x = s["extra"]
                card = out.setdefault((cb, cf), {
                    "batch_id": cb, "file": cf, "batch": _label(batches[cb]), "kind": it["part"],
                    "extra": x["kind"] if x else None, "printer": j.get("printer_name") or "",
                    "done_at": j.get("updated_at"), "preview": _plate_preview(batches[cb], cf, exs),
                    "color": it.get("color") or "", "items": []})
                o = info.get(it["order_id"]) or {}
                card["items"].append({
                    **{k: it[k] for k in ("batch_id", "plate", "idx", "order_id", "part", "name", "color")},
                    "letters": next((i.get("letters") for i in (x or {}).get("items", []) if key(i) == key(it)), ""),
                    "fixes": fixes.get(key(it), 0), "warn_model": fixes.get(key(it), 0) >= MAX_FIXES_WARN,
                    "flag": o.get("flag"), "flag_note": o.get("flag_note"), "express_by": o.get("express_by")})
    return sorted(out.values(), key=lambda c: c["done_at"] or 0)


def completed(limit: int = 500) -> list[dict]:
    """Print → Completed: orders checked good and not shipped yet (Printed – pack it / Label done)."""
    from .tabs import TAB_SQL

    with db.tx() as con:
        rs = db.rows(con, f"SELECT order_id, name, color, quantity, checked_at, checked_by, made_at, batch_label, "
                          f"batch_id, express_by, {TAB_SQL} AS tab FROM orders WHERE state='bundled' AND made_at IS NOT NULL "
                          f"AND {TAB_SQL} IN ('pack_it', 'ship_it') ORDER BY COALESCE(checked_at, made_at) DESC LIMIT ?",
                     limit)
        ids = [r["order_id"] for r in rs]
        extra: dict[str, dict] = {}
        if ids:
            q = ",".join("?" * len(ids))
            for c in db.rows(con, f"SELECT order_id, batch_id, plate, idx, part, result, printer, at FROM plate_checks "
                                  f"WHERE order_id IN ({q}) ORDER BY at", *ids):
                e = extra.setdefault(c["order_id"], {"fixes": 0, "printer": "", "items": {}})
                if c["result"] in FIX_RESULTS or c["result"] == "plate_failed":
                    e["fixes"] += 1
                if c["result"] == "good" and c["part"] == "names" and c["printer"]:
                    e["printer"] = c["printer"]
                if c["batch_id"] is not None:
                    e["items"][(c["batch_id"], c["plate"], c["idx"])] = {
                        "batch_id": c["batch_id"], "plate": c["plate"], "idx": c["idx"], "part": c["part"]}
    for r in rs:
        e = extra.get(r["order_id"], {})
        r["had_fix"] = e.get("fixes", 0)
        r["printer"] = e.get("printer", "")
        r["items"] = list((e.get("items") or {}).values())  # for "Found a problem"
    return rs


def _items_of(con, batch_id: int) -> tuple[dict[tuple, dict], dict[str, str]]:
    b = _batch(con, batch_id)
    if b is None:
        raise ValueError("No such batch")
    items, state_of = batch_items(con, b)
    return {key(i): i for i in items}, state_of


ALLOWED = {"bases": ("good", "base", "plate_failed"), "names": ("good", "letters", "name", "colour", "plate_failed")}


def submit(carrier: dict, staff: str, results: list[dict], note: str = "") -> dict:
    """Checks for the items of one printed plate (or one item from Completed): results = [{batch_id, plate,
    idx, result, pieces}]. Problems make fix plates; "whole plate failed" sends the plate back to its list."""
    from . import extras

    staff = " ".join((staff or "").split())
    if not results:
        raise ValueError("Nothing to check")
    with db.tx() as con:
        if not staff or staff.lower() not in {s.lower() for s in staff_names(con, all_=True)}:
            raise ValueError("Choose who is checking (add your name first)")
        cb, cf = int(carrier["batch_id"]), str(carrier["file"])
        j = latest_jobs(con).get((cb, cf)) or {}
        at = db.now()
        cache: dict[int, tuple] = {}
        wanted, failed, n_good, part = [], False, 0, ""
        for r in results:
            bid = int(r["batch_id"])
            if bid not in cache:
                cache[bid] = _items_of(con, bid)
            it = cache[bid][0].get((bid, r["plate"], int(r["idx"])))
            if it is None:
                raise ValueError("That item isn't on this batch's plates")
            part = it["part"]
            res = r.get("result")
            if res not in ALLOWED[it["part"]]:
                raise ValueError(f"{it['name']}: choose what is wrong")
            if res == "letters" and not r.get("pieces"):
                raise ValueError(f"{it['name']}: tap the spoiled letters")
            if res in FIX_RESULTS:
                wanted.append({"item": it, "result": res,
                               "pieces": [int(x) for x in r.get("pieces") or []] if res == "letters" else None})
            failed = failed or res == "plate_failed"
            n_good += res == "good"
            add_check(con, it, res, staff, note=note, printed_on=cf, printer=j.get("printer_name") or "", at=at)
        files = []
        if wanted:
            files = extras.make_fixes(con, cb, wanted, staff, at + 0.0005)
            for w in wanted:  # which letters went on the fix plate
                if w.get("letters"):
                    con.execute("UPDATE plate_checks SET letters=? WHERE batch_id=? AND plate=? AND idx=? AND at=?",
                                (w["letters"], int(w["item"]["batch_id"]), w["item"]["plate"], int(w["item"]["idx"]), at))
        if failed:
            con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                        "updated_at,computer_id) VALUES (?,?,?,?,0,'','reprint',?,?,?)",
                        (cb, cf, j.get("kind") or part, j.get("color"), at + 0.001, at + 0.001, computer.ID))
        name = cf.rsplit("/", 1)[-1]
        if failed:
            db.add_activity(con, "check", f"{staff}: {name} failed — back to the list to print again")
        else:
            bad = len(results) - n_good
            db.add_activity(con, "check", f"{staff} checked {name}: {n_good} good"
                            + (f", {bad} to fix ({', '.join(f.rsplit('/', 1)[-1] for f in files)})" if bad else ""))
        touched = {cb} | set(cache)
    for bid in touched:
        update_orders(bid)
    return {"ok": True, "fixes": files, "failed": failed}


def letter_pieces(batch_id: int, plate: str, idx: int) -> list[dict]:
    """The letters of one name, for the 'Letter(s) spoiled' picker: [{n, label, rings}]."""
    from . import extras

    with db.tx() as con:
        items, _ = _items_of(con, batch_id)
        it = items.get((batch_id, plate, idx))
        if it is None:
            raise ValueError("That item isn't on this batch's plates")
        b = _batch(con, batch_id)
    return [{"n": p["n"], "label": p["label"], "rings": p["rings"]} for p in extras.pieces(b, it)]
