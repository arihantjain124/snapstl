"""Database housekeeping: backups, regular clean-up, full reset.

Everything here is the LOCAL database only. The shared order records in Supabase (sync.py) are
never cleaned up or reset by the app: they are only ever added to and updated.

Clean-up keeps every *bundled* and *printed* order (its Order ID is what stops a later report from
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
        orders = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) AS n FROM orders GROUP BY state")}
        batches = {r["status"]: r["n"] for r in db.rows(con, "SELECT status, COUNT(*) AS n FROM batches GROUP BY status")}
        downloads = {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) AS n FROM downloads GROUP BY state")}
        reports = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        size = _size(db.DB_PATH)
    backups = sorted(BACKUP_DIR.glob("snapstl-*.db")) if BACKUP_DIR.is_dir() else []
    return {
        "orders": orders, "batches": batches, "downloads": downloads, "reports_uploaded": reports,
        "db_bytes": int(size), "cache_bytes": cache.stats()["bytes"],
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
            "print_jobs_removed": con.execute(
                "DELETE FROM print_jobs WHERE state IN ('done','cancelled','failed') AND updated_at < ?",
                (cutoff,)).rowcount
            + con.execute("DELETE FROM slices WHERE state IN ('done','failed') AND updated_at < ?", (cutoff,)).rowcount,
        }
    _vacuum()
    res["backup"] = str(path)
    return res


def reset(shared: bool = False) -> dict:
    """Empty this computer's database (after a backup).

    Never touches files (batch folders and downloaded models stay). Without `shared` it never touches
    the shared records in Supabase: Sync loads the orders back from there, and it is refused while
    changes of this computer haven't reached them yet (they'd be lost).

    With `shared` (owner-requested, typed RESET ALL) the shared records are emptied too, for every
    computer, through `sync.clear` (a copy of every record is saved first). No sync runs in between,
    so nothing of this computer is sent again; other computers keep their own databases.
    """
    from . import sync

    if shared:
        if not sync.enabled():
            raise RuntimeError("No shared order records connected")
        with db.tx() as con:
            if _busy(con):
                raise RuntimeError("A batch is being built right now — try again in a minute")
        path = backup()
        with sync._lock:  # the background sync waits until both are empty
            cleared = sync.clear("CLEAR")
            _empty()
        return {"backup": str(path), "shared_backup": cleared["backup"], "cleared": cleared["cleared"]}
    if sync.enabled():
        sync.sync_now()
        left = sync.pending()
        if left:
            raise RuntimeError(f"{left} change(s) of this computer haven't reached the shared records yet "
                               f"({sync.STATUS.error or 'try Sync now'}) — reset after they're synced")
    path = backup()
    _empty()
    return {"backup": str(path)}


def _empty() -> None:
    with db.tx() as con:
        if _busy(con):
            raise RuntimeError("A batch is being built right now — try again in a minute")
        for t in ("downloads", "batches", "orders", "files", "sync_outbox", "sync_state", "print_jobs", "slices"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM sqlite_sequence WHERE name IN ('downloads','batches','files','print_jobs','slices')")
    _vacuum()


def _vacuum() -> None:
    con = db.connect()
    try:
        con.execute("VACUUM")
    finally:
        con.close()
