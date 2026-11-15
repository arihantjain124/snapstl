"""Database housekeeping: backups, regular clean-up, full reset.

Clean-up keeps every *bundled* order (its Order ID is what stops a later report from
bringing an already-printed order back as "ready"). Both clean-up and reset save a
backup copy of the database first (APP_DIR/backups, newest 20 kept).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import db
from . import cache
from .settings import APP_DIR

BACKUP_DIR = APP_DIR / "backups"
KEEP_BACKUPS = 20
FINISHED = ("done", "done_with_problems", "failed", "cancelled")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


def stats() -> dict:
    with db.tx() as con:
        orders = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) n FROM orders GROUP BY state")}
        batches = {r["status"]: r["n"] for r in db.rows(con, "SELECT status, COUNT(*) n FROM batches GROUP BY status")}
        downloads = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) n FROM downloads GROUP BY state")}
        reports = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    backups = sorted(BACKUP_DIR.glob("snapstl-*.db")) if BACKUP_DIR.is_dir() else []
    return {
        "orders": orders, "batches": batches, "downloads": downloads, "reports_uploaded": reports,
        "db_bytes": _size(db.DB_PATH), "cache_bytes": cache.stats()["bytes"],
        "cache_models": cache.stats()["models"], "downloads_dir": str(cache.ROOT),
        "backups": len(backups), "last_backup": backups[-1].name if backups else None,
    }


def backup() -> Path:
    """Consistent copy of the live database (SQLite online backup)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"snapstl-{stamp}.db"
    n = 2
    while dest.exists():  # two backups in the same second must not overwrite each other
        dest = BACKUP_DIR / f"snapstl-{stamp}-{n}.db"
        n += 1
    src = db.connect()
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    for old in sorted(BACKUP_DIR.glob("snapstl-*.db"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def _busy(con) -> bool:
    return con.execute("SELECT 1 FROM batches WHERE status='building'").fetchone() is not None


def cleanup(older_than_days: int = 30) -> dict:
    """Remove old records nobody needs; keep bundled orders and anything still in progress."""
    cutoff = time.time() - older_than_days * 86400
    path = backup()
    with db.tx() as con:
        if _busy(con):
            raise RuntimeError("A batch is being built right now — try again in a minute")
        q = ",".join("?" * len(FINISHED))
        res = {
            "orders_removed": con.execute(
                "DELETE FROM orders WHERE state='not_needed' AND updated_at < ?", (cutoff,)).rowcount,
            "batches_removed": con.execute(
                f"DELETE FROM batches WHERE status IN ({q}) AND COALESCE(finished_at, created_at) < ?",
                (*FINISHED, cutoff)).rowcount,
            "downloads_removed": con.execute(
                "DELETE FROM downloads WHERE state IN ('done','cancelled','failed') AND updated_at < ?",
                (cutoff,)).rowcount,
            "reports_removed": con.execute("DELETE FROM files WHERE ingested_at < ?", (cutoff,)).rowcount,
        }
    _vacuum()
    res["backup"] = str(path)
    return res


def reset() -> dict:
    """Empty the database (after a backup).

    Never touches files: batch folders and downloaded models stay until the user deletes them in Finder.
    """
    path = backup()
    with db.tx() as con:
        if _busy(con):
            raise RuntimeError("A batch is being built right now — try again in a minute")
        for t in ("downloads", "batches", "orders", "files"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM sqlite_sequence WHERE name IN ('downloads','batches','files')")
    _vacuum()
    return {"backup": str(path)}


def _vacuum() -> None:
    con = db.connect()
    try:
        con.execute("VACUUM")
    finally:
        con.close()
