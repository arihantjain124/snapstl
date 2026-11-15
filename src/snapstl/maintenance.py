"""Database housekeeping: backups, regular clean-up, full reset.

Clean-up keeps every *bundled* order (its Order ID is what stops a later report from
bringing an already-printed order back as "ready"). Both clean-up and reset save a
backup copy of the database first (APP_DIR/backups, newest 20 kept).

With the shared database (Postgres) a backup is a local SQLite copy of every table, so each
computer can always keep its own copy of the data; Supabase also keeps daily backups.
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
        computers = db.rows(con, "SELECT computer_name AS name, COUNT(*) AS batches FROM batches "
                                 "WHERE computer_name IS NOT NULL GROUP BY computer_name ORDER BY computer_name")
        size = (con.execute("SELECT COALESCE(SUM(pg_total_relation_size(c.oid)),0) FROM pg_class c JOIN pg_namespace n "
                            "ON n.oid=c.relnamespace WHERE n.nspname=? AND c.relkind='r'", (db.PG_SCHEMA,)).fetchone()[0]
                if db.shared() else _size(db.DB_PATH))
    backups = sorted(BACKUP_DIR.glob("snapstl-*.db")) if BACKUP_DIR.is_dir() else []
    return {
        "orders": orders, "batches": batches, "downloads": downloads, "reports_uploaded": reports,
        "db_bytes": int(size), "database": db.kind(), "computers": computers, "cache_bytes": cache.stats()["bytes"],
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
    if db.shared():
        _copy_to_sqlite(dest)
    else:
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


TABLES = ("orders", "files", "batches", "downloads")


def _copy_to_sqlite(dest: Path) -> None:
    """Every table of the shared database into a new local SQLite file (same layout)."""
    out = db.connect(dest)
    try:
        with db.tx() as con, out:
            for t in TABLES:
                rs = db.rows(con, f"SELECT * FROM {t}")
                if rs:
                    cols = list(rs[0])
                    out.executemany(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                    [tuple(r[c] for c in cols) for r in rs])
    finally:
        out.close()


def copy_local_to_shared(path: Path | None = None) -> dict:
    """One-time move: this computer's SQLite data into the (empty) shared database.

    Batches and downloads keep belonging to this computer. Refuses if the shared database already
    has orders (so two computers can't both push their data and mix it up)."""
    if not db.shared():
        raise RuntimeError("No shared database configured (.env SNAPSTL_DATABASE_URL)")
    src = db.connect(path or db.DB_PATH)
    counts = {}
    try:
        with db.tx() as con:
            db.lock(con, "orders")
            if con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
                raise RuntimeError("The shared database already has orders — the data was moved already "
                                   "(from this or another computer)")
            for t in TABLES:
                rs = [dict(r) for r in src.execute(f"SELECT * FROM {t}").fetchall()]
                counts[t] = len(rs)
                for r in rs:
                    cols = list(r)
                    con.execute(f"INSERT INTO {t}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                tuple(r[c] for c in cols))
                if t != "orders" and rs:  # keep numbering after the copied ids
                    con.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), (SELECT MAX(id) FROM {t}))")
    finally:
        src.close()
    return counts


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
        if not db.shared():
            con.execute("DELETE FROM sqlite_sequence WHERE name IN ('downloads','batches','files')")
    _vacuum()
    return {"backup": str(path)}


def _vacuum() -> None:
    if db.shared():
        return  # Supabase looks after its own storage
    con = db.connect()
    try:
        con.execute("VACUUM")
    finally:
        con.close()
