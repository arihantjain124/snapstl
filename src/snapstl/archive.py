"""Batches page (0.24): names, progress, plain-word status, Archive, and the clean-up of old files.

* A batch is **archived** once every one of its orders is On the way, Delivered, Coming back (RTO) or
  Cancelled (owner decision 2026-11-18); a cancelled or failed batch right away.
* **Clean-up** (once a day, and Settings → Maintenance → Clean up files now), never while a batch is
  downloading or building:
  - the folder of a batch archived 10+ days ago goes to the Recycle Bin (its record stays, marked
    *files cleaned*);
  - a downloaded model no batch used for 15 days goes to the Recycle Bin, unless an order waiting to
    be made has that name (it's downloaded again if ever needed).
  Nothing is deleted for good.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import cache, computer, db, platform_os
from .tabs import TAB_SQL

BATCH_DAYS = 10
MODEL_DAYS = 15
SHIPPED = ("on_the_way", "delivered", "rto")
DONE_TABS = SHIPPED + ("cancelled",)
BUILDING = ("waiting", "models_ready", "build_requested", "building")
STATUS_WORD = {"created": "Not started", "waiting": "Downloading", "models_ready": "Ready to build",
               "build_requested": "Building", "building": "Building", "failed": "Failed", "cancelled": "Cancelled"}


# ---------------- the Batches page ----------------
def _orders(con) -> dict[str, list[dict]]:
    """Batch uid → its orders (name, colour, tab, …), from the orders' own batch link."""
    out: dict[str, list[dict]] = {}
    for o in db.rows(con, f"SELECT order_id, name, color, quantity, batch_uid, batch_id, made_at, print_done_at, "
                          f"print_started_at, {TAB_SQL} AS tab FROM orders WHERE batch_uid IS NOT NULL"):
        out.setdefault(o["batch_uid"], []).append(o)
    return out


def progress(orders: list[dict]) -> dict:
    n = {"orders": len(orders), "printed": 0, "checked": 0, "packed": 0, "shipped": 0, "cancelled": 0,
         "printing": 0}
    for o in orders:
        t = o["tab"]
        if t == "cancelled":
            n["cancelled"] += 1
            continue
        shipped = t in SHIPPED
        packed = shipped or t == "ship_it"
        checked = packed or t == "pack_it"
        printed = checked or (t == "printing" and o.get("print_done_at"))
        n["shipped"] += shipped
        n["packed"] += packed
        n["checked"] += checked
        n["printed"] += bool(printed)
        n["printing"] += t == "printing" or bool(printed)
    return n


def status_word(b: dict, n: dict) -> str:
    st = b["status"]
    if st in STATUS_WORD:
        return STATUS_WORD[st]
    live = n["orders"] - n["cancelled"]
    if b.get("archived_at") or (n["orders"] and n["shipped"] == live):
        return "All shipped"
    if not n["orders"]:
        return "No orders"
    if n["packed"] == live:
        return "Shipping"
    if n["checked"] == live:
        return "Packing"
    if n["printed"] > n["checked"]:
        return "Checking"
    if n["printing"]:
        return "Printing"
    return "Ready to print"


def update_archive(con) -> None:
    """Archive batches whose orders are all on the way or later (or cancelled); a batch that gets an
    order back (e.g. its label expired) leaves Archive while its files are still there."""
    orders = _orders(con)
    now = db.now()
    for b in db.rows(con, "SELECT id, uid, status, archived_at, files_cleaned_at FROM batches"):
        os_ = orders.get(b["uid"], [])
        if b["status"] in ("cancelled", "failed"):
            done = True
        elif b["status"] in ("done", "done_with_problems"):
            done = bool(os_) and all(o["tab"] in DONE_TABS for o in os_)
        else:
            done = False
        if done and b["archived_at"] is None:
            con.execute("UPDATE batches SET archived_at=? WHERE id=?", (now, b["id"]))
        elif not done and b["archived_at"] is not None and b["files_cleaned_at"] is None:
            con.execute("UPDATE batches SET archived_at=NULL WHERE id=?", (b["id"],))


def batch_rows(limit: int = 1000) -> dict:
    """GET /api/batches: every batch with its names, progress and status in words."""
    from . import batches as B

    from . import verify

    with db.tx() as con:
        update_archive(con)
        rs = db.rows(con, "SELECT * FROM batches ORDER BY id DESC LIMIT ?", limit)
        orders = _orders(con)
        # base / letters of each name still being made (this computer's prints)
        parts = verify.part_status(con, [r for r in rs if not r.get("archived_at")
                                         and (r.get("computer_id") or computer.ID) == computer.ID])
    out = []
    for r in rs:
        summ = json.loads(r["summary"] or "{}")
        os_ = orders.get(r["uid"], [])
        n = progress(os_)
        item = {
            "id": r["id"], "created_at": r["created_at"], "finished_at": r.get("finished_at"), "status": r["status"],
            "orders": len(json.loads(r["order_ids"] or "[]")), "plates": len(summ.get("plates", [])),
            "errors": len(summ.get("errors", [])), "label": summ.get("label", ""),
            "computer": r.get("computer_name") or "", "mine": (r.get("computer_id") or computer.ID) == computer.ID,
            "names": [{"name": o["name"], "color": o["color"], "tab": o["tab"], "order_id": o["order_id"],
                       "quantity": o["quantity"], "base": parts.get(o["order_id"], {}).get("bases"),
                       "letters": parts.get(o["order_id"], {}).get("names")} for o in sorted(os_, key=lambda o: (o["color"] or "", o["name"] or ""))],
            "count": n, "word": status_word(r, n), "archived_at": r.get("archived_at"),
            "files_cleaned_at": r.get("files_cleaned_at"),
            "clean_on": (r["archived_at"] + BATCH_DAYS * 86400) if r.get("archived_at") and not r.get("files_cleaned_at") else None,
        }
        if r["status"] in B.OPEN:
            item["progress"] = B.batch_progress(r)
        out.append(item)
    computers = {r["computer"] for r in out if r["computer"]}
    return {"batches": out, "computers": len(computers)}


# ---------------- clean-up ----------------
def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0
    except OSError:
        return 0


def _busy(con) -> bool:
    q = ",".join("?" * len(BUILDING))
    return con.execute(f"SELECT 1 FROM batches WHERE status IN ({q}) LIMIT 1", BUILDING).fetchone() is not None


def _wanted_names(con) -> set[str]:
    """Names of orders still waiting to be made: their models are kept."""
    return {r[0] for r in con.execute("SELECT name FROM orders WHERE state IN ('ready','needs_review','in_batch') "
                                      "AND name IS NOT NULL")}


def _model_folders() -> list[Path]:
    root = cache.ROOT
    return [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []


def due(now: float | None = None) -> dict:
    """What the next clean-up would move to the Recycle Bin (nothing is moved here)."""
    now = now or time.time()
    with db.tx() as con:
        update_archive(con)
        batches = db.rows(con, "SELECT id, out_dir, summary, archived_at FROM batches WHERE archived_at IS NOT NULL "
                               "AND files_cleaned_at IS NULL AND archived_at <= ? AND COALESCE(computer_id, ?) = ?",
                          now - BATCH_DAYS * 86400, computer.ID, computer.ID)
        used = dict(con.execute("SELECT folder, last_used FROM model_use").fetchall())
        names = _wanted_names(con)
        seen = []
        for d in _model_folders():  # first time seen: counts as used today (nothing goes before 15 days)
            if d.name not in used:
                used[d.name] = now
                seen.append((d.name, now))
        con.executemany("INSERT OR IGNORE INTO model_use(folder, last_used) VALUES (?, ?)", seen)
    models = []
    for d in _model_folders():
        if used.get(d.name, now) > now - MODEL_DAYS * 86400:
            continue
        name = d.name.split(" - ")[0]
        if name in names:
            continue
        models.append(d)
    return {"batches": batches, "models": models}


def clean_up(now: float | None = None) -> dict:
    """Move old batch folders and unused models to the Recycle Bin. Returns what was moved."""
    now = now or time.time()
    with db.tx() as con:
        if _busy(con):
            return {"skipped": "A batch is downloading or building — clean-up waits until it's done",
                    "batches": 0, "models": 0, "bytes": 0}
    d = due(now)
    moved_b = moved_m = size = 0
    failed = []
    for b in d["batches"]:
        p = Path(b["out_dir"]) if b["out_dir"] else None
        label = json.loads(b["summary"] or "{}").get("label") or f"Batch #{b['id']}"
        ok = True
        if p and p.exists():
            s = _size(p)
            try:
                ok = platform_os.to_trash(p)
            except Exception as e:  # noqa: BLE001
                ok = False
                failed.append(f"{label}: {e}")
            if ok:
                size += s
        if ok:
            moved_b += 1
            with db.tx() as con:
                con.execute("UPDATE batches SET files_cleaned_at=? WHERE id=?", (now, b["id"]))
                db.add_activity(con, "cleanup", f"{label}: files moved to the Recycle Bin (all shipped 10+ days ago)")
    for m in d["models"]:
        s = _size(m)
        try:
            ok = platform_os.to_trash(m)
        except Exception as e:  # noqa: BLE001
            ok = False
            failed.append(f"{m.name}: {e}")
        if ok:
            moved_m += 1
            size += s
            with db.tx() as con:
                con.execute("DELETE FROM model_use WHERE folder=?", (m.name,))
    with db.tx() as con:
        if moved_m:
            db.add_activity(con, "cleanup", f"{moved_m} downloaded model(s) not used for {MODEL_DAYS} days moved to the "
                                            "Recycle Bin")
        con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('cleanup_at', ?)", (str(now),))
    return {"batches": moved_b, "models": moved_m, "bytes": size, "failed": failed}


def disk() -> dict:
    """Settings → Maintenance: space used, and what the next clean-up moves."""
    from .maintenance import BACKUP_DIR

    with db.tx() as con:
        own = db.rows(con, "SELECT out_dir FROM batches WHERE files_cleaned_at IS NULL AND COALESCE(computer_id, ?) = ?",
                      computer.ID, computer.ID)
        last = con.execute("SELECT value FROM sync_state WHERE key='cleanup_at'").fetchone()
    folders = {Path(b["out_dir"]) for b in own if b["out_dir"]}
    d = due()
    return {"batch_bytes": sum(_size(p) for p in folders if p.exists()), "batch_folders": sum(p.exists() for p in folders),
            "model_bytes": cache.stats()["bytes"], "models": cache.stats()["models"],
            "backup_bytes": _size(BACKUP_DIR), "db_bytes": _size(db.DB_PATH),
            "due_batches": len(d["batches"]), "due_models": len(d["models"]),
            "due_bytes": sum(_size(Path(b["out_dir"])) for b in d["batches"] if b["out_dir"]) + sum(_size(m) for m in d["models"]),
            "last_cleanup": float(last[0]) if last else None, "batch_days": BATCH_DAYS, "model_days": MODEL_DAYS}


class Cleaner:
    """Runs the clean-up once a day in the background."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.environ.get("SNAPSTL_NO_CLEANUP") or (self._thread and self._thread.is_alive()):
            return  # tests and throwaway copies never move anything
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-cleanup")
        self._thread.start()

    def _run(self) -> None:
        time.sleep(120)  # let the app start first
        while True:
            try:
                with db.tx() as con:
                    r = con.execute("SELECT value FROM sync_state WHERE key='cleanup_at'").fetchone()
                if not r or time.time() - float(r[0]) > 24 * 3600:
                    clean_up()
            except Exception as e:  # noqa: BLE001  (never stop the app)
                print(f"SnapSTL: clean-up skipped: {e}")
            time.sleep(3600)


CLEANER = Cleaner()
