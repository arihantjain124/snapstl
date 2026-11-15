"""Database for orders, ingested files, batches and the download queue.

Two kinds, same SQL:
  * SQLite in the app folder (default): this computer only.
  * PostgreSQL (Supabase) when .env sets SNAPSTL_DATABASE_URL: shared by every SnapSTL computer.
    Tables live in their own schema `snapstl` (not exposed by Supabase's web API).

Code writes SQLite-style SQL (`?` placeholders, LIKE); `_pg()` adapts it for Postgres (LIKE becomes
ILIKE so searches stay case-insensitive). Stores what's needed to make and track each order (order id,
status, name, colour, quantity) plus customer name / phone / address shown in the app.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from . import computer, config
from .settings import APP_DIR

DB_PATH = APP_DIR / "snapstl.db"
PG_SCHEMA = "snapstl"
SCHEMA_VERSION = 2  # bump when the database layout changes; older SnapSTLs then ask to be updated

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    clubbed_id      TEXT,
    sku             TEXT NOT NULL,
    order_date      TEXT,
    source_status   TEXT,
    quantity        INTEGER NOT NULL DEFAULT 1,
    raw_name        TEXT,
    raw_color       TEXT,
    special_request TEXT,
    name            TEXT,          -- text that goes on the organizer
    color           TEXT,          -- normalised colour
    name_edited     INTEGER NOT NULL DEFAULT 0,
    review_reason   TEXT,          -- why it needs a human; NULL when fine
    state           TEXT NOT NULL, -- ready | needs_review | in_batch | bundled | skipped | not_needed
    batch_id        INTEGER,
    last_error      TEXT,
    first_file      TEXT,
    last_file       TEXT,
    first_seen      REAL,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS orders_state ON orders(state);
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT,
    sha256       TEXT,
    ingested_at  REAL,
    summary      TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL,
    out_dir     TEXT,
    status      TEXT,              -- created | waiting | models_ready | build_requested | building | done | done_with_problems | failed | cancelled
    order_ids   TEXT,              -- JSON list
    summary     TEXT               -- JSON (plates, errors, warnings)
);
-- One row per model to generate on MakerWorld (deduplicated by cache_key).
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key   TEXT NOT NULL,
    name        TEXT NOT NULL,
    compartments INTEGER NOT NULL,
    model_json  TEXT NOT NULL,
    state       TEXT NOT NULL,     -- queued | claimed | done | failed | cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed_at  REAL,
    error       TEXT,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS downloads_state ON downloads(state, id);
CREATE INDEX IF NOT EXISTS downloads_key ON downloads(cache_key);
"""

MIGRATIONS = [
    ("batches", "settings", "TEXT"),       # JSON settings snapshot used for this batch
    ("batches", "finished_at", "REAL"),
    ("orders", "customer_name", "TEXT"),   # shown in the UI (owner's request, local only)
    ("orders", "phone", "TEXT"),
    ("orders", "address", "TEXT"),         # address, city, state, pincode joined
    ("downloads", "batch_id", "INTEGER"),  # batch that queued this download (shown in the queue)
    ("downloads", "watermark", "INTEGER"),  # 1 = MakerWorld watermark found on the base, 0 = clean
    ("downloads", "license_check", "TEXT"),  # what the helper confirmed about the license password
    # shared database: every batch belongs to the computer that created it (it downloads + builds it)
    ("batches", "computer_id", "TEXT"),
    ("batches", "computer_name", "TEXT"),
    ("batches", "progress", "TEXT"),        # JSON written by the owning computer (others can't see its files)
    ("downloads", "computer_id", "TEXT"),   # computer whose helper downloads it (= the batch's computer)
]


def shared() -> bool:
    """True when this SnapSTL uses the shared Postgres database (.env SNAPSTL_DATABASE_URL)."""
    return bool(config.database_url())


def kind() -> str:
    return "postgres" if shared() else "sqlite"


# ---------------- SQLite ----------------
def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # one computer, one database: everything belongs to this computer
    con.execute("UPDATE batches SET computer_id=?, computer_name=? WHERE computer_id IS NULL", (computer.ID, computer.NAME))
    con.execute("UPDATE downloads SET computer_id=? WHERE computer_id IS NULL", (computer.ID,))
    con.commit()
    return con


# ---------------- PostgreSQL ----------------
class Row(dict):
    """A result row usable like sqlite3.Row: r["col"] and r[0]."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


@lru_cache(maxsize=512)
def _pg(sql: str) -> str:
    sql = sql.replace("%", "%%").replace("?", "%s")
    return re.sub(r"\bLIKE\b", "ILIKE", sql)


class PGConnection:
    """Wraps a psycopg connection so callers use the same API as sqlite3."""

    def __init__(self, con) -> None:
        self._c = con

    def execute(self, sql: str, params=()):
        return self._c.execute(_pg(sql), tuple(params))


_POOL = None
_POOL_LOCK = threading.Lock()


def _pg_schema() -> str:
    t = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
    return re.sub(r"--[^\n]*", "", t)


def _setup_pg(con) -> None:
    """Create / upgrade the shared tables once per start, under a lock (several computers may start at once)."""
    con.execute("SELECT pg_advisory_xact_lock(83170001)")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")
    con.execute(f"SET search_path TO {PG_SCHEMA}")
    for stmt in _pg_schema().split(";"):
        if stmt.strip():
            con.execute(stmt)
    for table, col, typ in MIGRATIONS:
        typ = "DOUBLE PRECISION" if typ == "REAL" else typ
        con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    r = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    have = int(r["value"]) if r else 0
    if have > SCHEMA_VERSION:
        raise RuntimeError("The shared database was set up by a newer SnapSTL — update this computer "
                           "(Settings → Updates, or scripts/update)")
    if have < SCHEMA_VERSION:
        con.execute("INSERT INTO meta(key,value) VALUES ('schema_version',%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(SCHEMA_VERSION),))


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            import psycopg
            from psycopg_pool import ConnectionPool

            url = config.database_url()
            with psycopg.connect(url, row_factory=_row_factory, prepare_threshold=None, connect_timeout=15) as c:
                _setup_pg(c)

            def configure(c):
                c.execute(f"SET search_path TO {PG_SCHEMA}")
                c.commit()

            _POOL = ConnectionPool(url, min_size=1, max_size=6, open=True, timeout=30, configure=configure,
                                   kwargs={"row_factory": _row_factory, "prepare_threshold": None,
                                           "connect_timeout": 15})
        return _POOL


def close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


# ---------------- both ----------------
@contextmanager
def tx(path: Path | None = None) -> Iterator:
    """One transaction: committed at the end, rolled back on error. A path forces a local SQLite file."""
    if path is None and shared():
        with _pool().connection() as c:  # commits on success, rolls back on an exception
            yield PGConnection(c)
        return
    con = connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def lock(con, name: str) -> None:
    """Serialise a critical step across every SnapSTL computer (Postgres) for this transaction.
    SQLite needs nothing: it allows one writer at a time anyway."""
    if isinstance(con, PGConnection):
        con.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (name,))


def now() -> float:
    return time.time()


def rows(con, sql: str, *args) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def create_batch(con, order_ids: list[str], out_dir: str, settings_json: str = "{}", status: str = "waiting") -> int:
    """A new batch owned by this computer."""
    r = con.execute(
        "INSERT INTO batches(created_at,out_dir,status,order_ids,summary,settings,computer_id,computer_name) "
        "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (now(), out_dir, status, json.dumps(order_ids), "{}", settings_json, computer.ID, computer.NAME),
    ).fetchone()
    return int(r["id"])


def finish_batch(con, batch_id: int, status: str, summary: dict, bundled_ids: list[str]) -> None:
    """Record the result. Orders in `bundled_ids` become bundled; the batch's other orders go back to ready."""
    con.execute(
        "UPDATE batches SET status=?, summary=?, finished_at=? WHERE id=?",
        (status, json.dumps(summary), now(), batch_id),
    )
    if bundled_ids:
        q = ",".join("?" * len(bundled_ids))
        con.execute(
            f"UPDATE orders SET state='bundled', batch_id=?, last_error=NULL, updated_at=? WHERE order_id IN ({q})",
            (batch_id, now(), *bundled_ids),
        )
    release_batch_orders(con, batch_id)


def release_batch_orders(con, batch_id: int) -> None:
    """Orders still marked in_batch for this batch return to ready / needs_review."""
    con.execute(
        "UPDATE orders SET state=CASE WHEN review_reason IS NULL THEN 'ready' ELSE 'needs_review' END, "
        "batch_id=NULL, updated_at=? WHERE batch_id=? AND state='in_batch'",
        (now(), batch_id),
    )
