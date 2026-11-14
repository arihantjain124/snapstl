"""SQLite store for orders, ingested files and batches.

Only what's needed to make the product is stored (order id, status, name,
colour, quantity). Customer contact details from the report are NOT stored.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .settings import APP_DIR

DB_PATH = APP_DIR / "snapstl.db"

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
    state           TEXT NOT NULL, -- needs_review | ready | bundled | not_needed
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
    status      TEXT,              -- waiting | build_requested | building | done | done_with_problems | failed | cancelled
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
]


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
    return con


@contextmanager
def tx(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


def now() -> float:
    return time.time()


def rows(con: sqlite3.Connection, sql: str, *args) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def create_batch(con, order_ids: list[str], out_dir: str, settings_json: str = "{}", status: str = "waiting") -> int:
    cur = con.execute(
        "INSERT INTO batches(created_at,out_dir,status,order_ids,summary,settings) VALUES (?,?,?,?,?,?)",
        (now(), out_dir, status, json.dumps(order_ids), "{}", settings_json),
    )
    return int(cur.lastrowid)


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
