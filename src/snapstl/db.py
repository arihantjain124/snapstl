"""The local database (SQLite in the app folder): orders, ingested files, batches, download queue.

Each computer has its own. The shared order records in Supabase are a separate, add-and-update-only
copy kept in step by `sync.py`; resetting this database never touches them.
Stores what's needed to make and track each order (order id, status, name, colour, quantity) plus
customer name / phone / address shown in the app.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import computer
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
    state           TEXT NOT NULL, -- ready | needs_review | in_batch | bundled | printed | skipped | not_needed
    batch_id        INTEGER,       -- this computer's batch (NULL when batched on another computer)
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
-- Print farm (farm.py): one row per plate pushed to a printer's queue in Bambuddy.
CREATE TABLE IF NOT EXISTS print_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL,
    plate         TEXT NOT NULL,   -- plate file, relative to the batch folder (Bases/…, Names/<colour>/…)
    kind          TEXT NOT NULL,   -- bases | names
    color         TEXT,            -- names: the plate's colour
    printer_id    INTEGER NOT NULL, -- Bambuddy's printer id
    printer_name  TEXT,
    state         TEXT NOT NULL,   -- queued | slicing | uploading | sent | printing | done | failed | cancelled | reprint
    sliced        TEXT,            -- sliced file used (relative to the batch folder)
    file_id       INTEGER,         -- Bambuddy library file
    queue_item_id INTEGER,         -- Bambuddy queue item
    progress      REAL,
    error         TEXT,
    created_at    REAL,
    updated_at    REAL,
    computer_id   TEXT
);
CREATE INDEX IF NOT EXISTS print_jobs_state ON print_jobs(state, id);
-- Make It → Slice (farm.py): one row per plate sliced by Bambuddy's slicer; a plate's newest row counts.
CREATE TABLE IF NOT EXISTS slices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       INTEGER NOT NULL,
    plate          TEXT NOT NULL,   -- plate file, relative to the batch folder
    kind           TEXT NOT NULL,   -- bases | names
    color          TEXT,
    state          TEXT NOT NULL,   -- queued | uploading | slicing | done | failed
    source_file_id INTEGER,         -- the plain plate in Bambuddy's library
    slice_job_id   INTEGER,         -- Bambuddy's slice job
    sliced_file_id INTEGER,         -- the sliced plate (.gcode.3mf) in Bambuddy's library
    progress       REAL,
    last_progress  TEXT,            -- Bambuddy's last progress snapshot (any change = still working)
    error          TEXT,
    presets        TEXT,            -- JSON presets used
    started_at     REAL,
    created_at     REAL,
    updated_at     REAL,            -- last change (progress included): no change for 20 min = given up
    computer_id    TEXT
);
CREATE INDEX IF NOT EXISTS slices_state ON slices(state, id);
CREATE INDEX IF NOT EXISTS print_jobs_batch ON print_jobs(batch_id);
"""

MIGRATIONS = [
    ("batches", "settings", "TEXT"),       # JSON settings snapshot used for this batch
    ("batches", "finished_at", "REAL"),
    ("orders", "customer_name", "TEXT"),   # shown in the UI
    ("orders", "phone", "TEXT"),
    ("orders", "address", "TEXT"),         # address, city, state, pincode joined
    ("downloads", "batch_id", "INTEGER"),  # batch that queued this download (shown in the queue)
    ("downloads", "watermark", "INTEGER"),  # 1 = MakerWorld watermark found on the base, 0 = clean
    ("downloads", "license_check", "TEXT"),  # what the helper confirmed about the license password
    ("batches", "computer_id", "TEXT"),     # this computer (a batch is made where it's created)
    ("batches", "computer_name", "TEXT"),
    ("batches", "progress", "TEXT"),        # JSON: models downloaded / failed
    ("downloads", "computer_id", "TEXT"),
    # shared order records (sync.py)
    ("batches", "uid", "TEXT"),             # unique id in the shared records (local ids restart after a reset)
    ("batches", "synced", "INTEGER"),       # 1 = the shared records have this batch as it is now
    ("orders", "state_at", "REAL"),         # when `state` last changed (trigger below)
    ("orders", "status_at", "TEXT"),        # how recent source_status is (ingest.status_stamp)
    ("orders", "in_remote", "INTEGER"),     # 1 = the shared records have this order
    ("orders", "batch_uid", "TEXT"),        # its batch in the shared records (also batches of other computers)
    ("orders", "batch_label", "TEXT"),      # e.g. "Batch 2 - 2026-11-15"
    ("orders", "batch_computer", "TEXT"),   # computer that batched it (name)
    ("orders", "batch_computer_id", "TEXT"),  # … and its id
    ("orders", "bundled_at", "REAL"),
    ("orders", "printed_at", "REAL"),       # marked printed by a person
    ("print_jobs", "options", "TEXT"),      # JSON print options for Bambuddy (bed levelling, flow cali, timelapse)
    ("print_jobs", "held", "INTEGER"),      # 1 = waiting in Bambuddy for a person to press Start
    ("print_jobs", "autostart", "INTEGER"),  # 1 = a person pressed Start print (Make It): start once queued
    ("orders", "made_at", "REAL"),          # base and name both printed (Make It) → Ready to ship
]

# Bookkeeping for the shared records, kept by triggers so no code path can forget it:
#  * orders.state_at = time of the last state change (unless the writer sets it itself);
#  * sync_outbox = orders whose change still has to reach the shared records. Changes written by the
#    sync itself (inside `quiet()`) are not queued again;
#  * batches.synced = 0 whenever a batch changes.
_NOW = "(julianday('now') - 2440587.5) * 86400.0"
POST_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sync_outbox (order_id TEXT PRIMARY KEY, queued_at REAL);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_flags (quiet INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS orders_state_at AFTER UPDATE OF state ON orders
  WHEN NEW.state IS NOT OLD.state AND NEW.state_at IS OLD.state_at
  BEGIN UPDATE orders SET state_at = {_NOW} WHERE order_id = NEW.order_id; END;
CREATE TRIGGER IF NOT EXISTS orders_outbox_upd AFTER UPDATE ON orders
  WHEN (NEW.state IS NOT OLD.state OR NEW.source_status IS NOT OLD.source_status
        OR NEW.status_at IS NOT OLD.status_at OR NEW.name IS NOT OLD.name OR NEW.color IS NOT OLD.color)
   AND (COALESCE(NEW.in_remote, 0) = 1 OR NEW.state IN ('in_batch', 'bundled', 'printed'))
   AND NOT EXISTS (SELECT 1 FROM sync_flags WHERE quiet = 1)
  BEGIN INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES (NEW.order_id, {_NOW}); END;
CREATE TRIGGER IF NOT EXISTS orders_outbox_ins AFTER INSERT ON orders
  WHEN NEW.state IN ('in_batch', 'bundled', 'printed') AND NOT EXISTS (SELECT 1 FROM sync_flags WHERE quiet = 1)
  BEGIN INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES (NEW.order_id, {_NOW}); END;
CREATE TRIGGER IF NOT EXISTS batches_unsynced AFTER UPDATE OF status, summary, out_dir, order_ids ON batches
  BEGIN UPDATE batches SET synced = 0 WHERE id = NEW.id; END;
"""


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
    con.executescript(POST_SCHEMA)
    # older rows: everything here is this computer's, and every batch gets a unique id
    con.execute("UPDATE batches SET computer_id=?, computer_name=? WHERE computer_id IS NULL", (computer.ID, computer.NAME))
    con.execute("UPDATE downloads SET computer_id=? WHERE computer_id IS NULL", (computer.ID,))
    con.execute("UPDATE batches SET uid=lower(hex(randomblob(16))) WHERE uid IS NULL")
    con.execute("UPDATE orders SET state_at=COALESCE(updated_at, first_seen) WHERE state_at IS NULL")
    con.commit()
    return con


@contextmanager
def tx(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


@contextmanager
def quiet(con) -> Iterator[None]:
    """Changes made inside are not queued for the shared records (they came from there)."""
    con.execute("INSERT INTO sync_flags(quiet) VALUES (1)")
    try:
        yield
    finally:
        con.execute("DELETE FROM sync_flags")


def now() -> float:
    return time.time()


def rows(con, sql: str, *args) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def create_batch(con, order_ids: list[str], out_dir: str, settings_json: str = "{}", status: str = "waiting",
                 uid: str | None = None) -> int:
    """A new batch on this computer."""
    cur = con.execute(
        "INSERT INTO batches(created_at,out_dir,status,order_ids,summary,settings,computer_id,computer_name,uid,synced) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
        (now(), out_dir, status, json.dumps(order_ids), "{}", settings_json, computer.ID, computer.NAME,
         uid or uuid.uuid4().hex),
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
            f"UPDATE orders SET state='bundled', batch_id=?, last_error=NULL, updated_at=?, bundled_at=?, "
            f"batch_label=?, batch_computer=?, batch_computer_id=?, "
            f"batch_uid=(SELECT uid FROM batches WHERE id=?) WHERE order_id IN ({q})",
            (batch_id, now(), now(), summary.get("label") or None, computer.NAME, computer.ID, batch_id,
             *bundled_ids),
        )
    release_batch_orders(con, batch_id)


def release_batch_orders(con, batch_id: int) -> None:
    """Orders still marked in_batch for this batch return to ready / needs_review."""
    con.execute(
        "UPDATE orders SET state=CASE WHEN review_reason IS NULL THEN 'ready' ELSE 'needs_review' END, "
        "batch_id=NULL, batch_uid=NULL, batch_label=NULL, batch_computer=NULL, batch_computer_id=NULL, updated_at=? "
        "WHERE batch_id=? AND state='in_batch'",
        (now(), batch_id),
    )
