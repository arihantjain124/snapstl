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
    ("batches", "auto_build", "INTEGER"),  # 0.26 Make next batch: build the plates once every model is in
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
    ("slices", "model", "TEXT"),             # printer model it was sliced for ("" = before 0.16)
    ("print_jobs", "speed", "INTEGER"),     # print speed chosen in Make It (1 silent … 4 ludicrous)
    ("print_jobs", "speed_set", "INTEGER"),  # 1 = the printer reports the chosen speed (or SnapSTL gave up)
    ("print_jobs", "speed_tries", "INTEGER"),  # speed commands sent so far (farm.SPEED_TRIES at most)
    ("slices", "engine", "TEXT"),           # "orca" (OrcaSlicer on this computer) | "bambuddy"
    ("slices", "sliced_path", "TEXT"),      # OrcaSlicer's sliced file (batch folder / Sliced/<model>/…)
    ("files", "sent", "INTEGER"),           # 1 = in the shared records' report_uploads
    ("print_jobs", "ams_tray", "INTEGER"),  # filament slot it prints from (Bambu global tray id: AMS n·4+slot, 254 external)
    # 0.23: order flow + logistics (docs/build-plan-order-flow-and-logistics.md)
    ("orders", "first_status", "TEXT"),      # webstore status when first seen (orders first seen past PRINT are history)
    ("orders", "print_started_at", "REAL"),  # a plate holding it was first started → Printing
    # 0.24: Print → Verify, batches archive / clean-up (docs/build-plan-batches-print-verify.md)
    ("batches", "archived_at", "REAL"),      # every order on the way or later (or cancelled) → Batches → Archive
    ("batches", "files_cleaned_at", "REAL"),  # its folder went to the Recycle Bin (10 days after archived)
]

# Things people set on an order (mostly on the logistics page). Each group travels as one piece between
# computers with its own `<group>_changed` time: the newest change of a group wins everywhere, whatever
# happens to the order's state. Clearing a group sets its fields to NULL and still stamps `_changed`.
FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "label": ("label_at", "label_by", "label_note"),            # Label Generated → Label done – ship it
    "express": ("express_by", "express_note"),                  # needed-by date (YYYY-MM-DD) + note
    "flag": ("flag", "flag_note"),                              # why it is in Problems – check
    "name_note": ("name_change_reason", "name_confirmed_at"),   # name changed after confirming with the customer
    "remake": ("remake_at", "remake_reason"),                   # remake requested (by logistics)
    "reuse": ("reuse_of", "reused_by"),                         # new order ← returned (RTO) piece of another order
    # Print → Verify (0.24): every plate holding it printed (waiting for check) / both parts checked good, by whom
    "check": ("print_done_at", "checked_at", "checked_by"),
}
_GROUP_TYPES = {"label_at": "REAL", "name_confirmed_at": "REAL", "remake_at": "REAL", "print_done_at": "REAL",
                "checked_at": "REAL"}
FLAGS = ("past_print_not_made", "cancelled_in_production", "cancel_request", "back_to_print", "unknown_status")
MIGRATIONS += [("orders", c, _GROUP_TYPES.get(c, "TEXT")) for g in FIELD_GROUPS.values() for c in g]
MIGRATIONS += [("orders", f"{g}_changed", "REAL") for g in FIELD_GROUPS]
GROUP_COLS = tuple(c for g, cs in FIELD_GROUPS.items() for c in (*cs, f"{g}_changed"))

# Bookkeeping for the shared records, kept by triggers so no code path can forget it:
#  * orders.state_at = time of the last state change (unless the writer sets it itself);
#  * sync_outbox = orders whose change still has to reach the shared records. Changes written by the
#    sync itself (inside `quiet()`) are not queued again;
#  * batches.synced = 0 whenever a batch changes.
_NOW = "(julianday('now') - 2440587.5) * 86400.0"
_SENT_COLS = ("state", "source_status", "status_at", "name", "color", "review_reason", "made_at",
              "first_status", "print_started_at", *(f"{g}_changed" for g in FIELD_GROUPS))
POST_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sync_outbox (order_id TEXT PRIMARY KEY, queued_at REAL);
-- call notes on an order (logistics page); shared with every computer, never edited or deleted
CREATE TABLE IF NOT EXISTS order_notes (uid TEXT PRIMARY KEY, order_id TEXT NOT NULL, at REAL NOT NULL,
    text TEXT NOT NULL, author TEXT, sent INTEGER);
CREATE INDEX IF NOT EXISTS order_notes_order ON order_notes(order_id, at);
-- how often each feature is used on this computer, per day (no customer data)
CREATE TABLE IF NOT EXISTS usage_counts (day TEXT NOT NULL, feature TEXT NOT NULL, count INTEGER NOT NULL,
    sent INTEGER, PRIMARY KEY (day, feature));
-- Print → Verify (0.24): who may check prints (a name is hidden, never removed); shared with every computer
-- spare name pieces in stock (0.28, spares.py); shared with every computer, never deleted
CREATE TABLE IF NOT EXISTS spares (uid TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT, part TEXT NOT NULL,
    note TEXT, staff TEXT, added_at REAL, used_by TEXT, used_at REAL, removed_at REAL, changed REAL, computer TEXT,
    sent INTEGER);
CREATE INDEX IF NOT EXISTS spares_name ON spares(name, color);
CREATE TABLE IF NOT EXISTS staff (name TEXT PRIMARY KEY COLLATE NOCASE, added_at REAL, hidden INTEGER NOT NULL DEFAULT 0,
    changed REAL, sent INTEGER);
-- one row per printed item checked (a base or a name of one copy of an order on a plate); the newest row of an
-- item counts. batch/plate/idx = the item's own plate in its batch, also when it was printed on a merged or
-- fix plate (printed_on). Shared with every computer, never edited or deleted.
CREATE TABLE IF NOT EXISTS plate_checks (uid TEXT PRIMARY KEY, batch_uid TEXT, batch_id INTEGER, plate TEXT NOT NULL,
    idx INTEGER NOT NULL, order_id TEXT, part TEXT NOT NULL, name TEXT, color TEXT, result TEXT NOT NULL,
    letters TEXT, staff TEXT, note TEXT, at REAL NOT NULL, printed_on TEXT, printer TEXT, fix_file TEXT,
    computer TEXT, sent INTEGER);
CREATE INDEX IF NOT EXISTS plate_checks_item ON plate_checks(batch_id, plate, idx, at);
CREATE INDEX IF NOT EXISTS plate_checks_order ON plate_checks(order_id, at);
-- merged plates (several name plates of one colour on one) and fix plates (spoiled letters / a name / a base
-- printed again), kept in the folder of batch `batch_id`; items = the items they hold (verify.py)
CREATE TABLE IF NOT EXISTS extra_plates (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, batch_id INTEGER NOT NULL,
    file TEXT NOT NULL, kind TEXT NOT NULL, part TEXT NOT NULL, color TEXT, names TEXT, items TEXT NOT NULL,
    preview TEXT, created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active', staff TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS extra_plates_file ON extra_plates(batch_id, file);
CREATE TABLE IF NOT EXISTS extra_items (plate_id INTEGER NOT NULL, batch_id INTEGER NOT NULL, plate TEXT NOT NULL,
    idx INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS extra_items_batch ON extra_items(batch_id);
-- Activities tab: what happened on this computer, in plain words
CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
    text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at);
-- a person confirmed a printer's bed is empty (Start print / Printers → Clear plate)
CREATE TABLE IF NOT EXISTS bed_clears (printer_id INTEGER PRIMARY KEY, at REAL NOT NULL);
-- when a downloaded model was last used by a batch (models unused for 15 days go to the Recycle Bin)
CREATE TABLE IF NOT EXISTS model_use (folder TEXT PRIMARY KEY, last_used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS report_rows (order_id TEXT PRIMARY KEY, data TEXT, status_at TEXT, file TEXT, sent INTEGER);
CREATE TABLE IF NOT EXISTS shop_lines (order_id TEXT PRIMARY KEY, data TEXT, status_at TEXT, file TEXT, sent INTEGER);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_flags (quiet INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS orders_state_at AFTER UPDATE OF state ON orders
  WHEN NEW.state IS NOT OLD.state AND NEW.state_at IS OLD.state_at
  BEGIN UPDATE orders SET state_at = {_NOW} WHERE order_id = NEW.order_id; END;
DROP TRIGGER IF EXISTS orders_outbox_upd;
DROP TRIGGER IF EXISTS orders_outbox_upd2;
CREATE TRIGGER IF NOT EXISTS orders_outbox_upd3 AFTER UPDATE ON orders
  WHEN ({' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in _SENT_COLS)})
   AND (COALESCE(NEW.in_remote, 0) = 1 OR NEW.state IN ('in_batch', 'bundled', 'printed'))
   AND NOT EXISTS (SELECT 1 FROM sync_flags WHERE quiet = 1)
  BEGIN INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES (NEW.order_id, {_NOW}); END;
CREATE TRIGGER IF NOT EXISTS orders_outbox_ins AFTER INSERT ON orders
  WHEN NEW.state IN ('in_batch', 'bundled', 'printed') AND NOT EXISTS (SELECT 1 FROM sync_flags WHERE quiet = 1)
  BEGIN INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES (NEW.order_id, {_NOW}); END;
CREATE TRIGGER IF NOT EXISTS batches_unsynced AFTER UPDATE OF status, summary, out_dir, order_ids ON batches
  BEGIN UPDATE batches SET synced = 0 WHERE id = NEW.id; END;
"""


STAMP = "@"  # status_at since 0.22.21: "@<file as of>|<row event date>|<upload>" ('@' sorts above older stamps)


def upgrade_stamp(s: str | None) -> str | None:
    """A status stamp from before 0.22.21 ("event|file|upload") in today's order ("@file|event|upload"):
    the newest export wins, then the row's own event date."""
    if not s or s.startswith(STAMP) or s.count("|") < 2:
        return s
    event, file, upload = s.split("|", 2)
    return f"{STAMP}{file}|{event}|{upload}"


def _upgrade_stamps(con) -> None:
    """Once: every stored status stamp in today's order (changed orders go to the shared records again)."""
    if con.execute("SELECT 1 FROM sync_state WHERE key='stamps_v2'").fetchone():
        return
    for table in ("orders", "report_rows", "shop_lines"):
        old = con.execute(f"SELECT order_id, status_at FROM {table} WHERE status_at LIKE '%|%|%' "
                          f"AND status_at NOT LIKE '{STAMP}%'").fetchall()
        con.executemany(f"UPDATE {table} SET status_at=? WHERE order_id=?",
                        [(upgrade_stamp(s), oid) for oid, s in old])
    con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('stamps_v2', '1')")


# columns of tables made in POST_SCHEMA
LATE_MIGRATIONS = [
    ("print_jobs", "started_at", "REAL"),  # 0.26: when Bambuddy said it is printing (Stats: hours printing)
    ("staff", "pin_hash", "TEXT"),         # 0.25.1: salted hash of the person's 4-digit PIN (staff.py)
    ("staff", "pin_changed", "REAL"),
]


def _add_columns(con, migrations) -> None:
    for table, col, typ in migrations:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:  # another thread added it a moment ago (first start)
                if "duplicate column" not in str(e):
                    raise


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _add_columns(con, MIGRATIONS)
    con.executescript(POST_SCHEMA)
    _add_columns(con, LATE_MIGRATIONS)
    # older rows: everything here is this computer's, and every batch gets a unique id
    con.execute("UPDATE batches SET computer_id=?, computer_name=? WHERE computer_id IS NULL", (computer.ID, computer.NAME))
    con.execute("UPDATE downloads SET computer_id=? WHERE computer_id IS NULL", (computer.ID,))
    con.execute("UPDATE batches SET uid=lower(hex(randomblob(16))) WHERE uid IS NULL")
    con.execute("UPDATE orders SET state_at=COALESCE(updated_at, first_seen) WHERE state_at IS NULL")
    # orders batched before they recorded their batch (uid / computer): fill it in from the batch and send
    # them again, or the shared records hold them without an owner (and refuse the owner's Bundled)
    legacy = ("FROM orders WHERE batch_id IS NOT NULL AND batch_uid IS NULL "
              "AND state IN ('in_batch','bundled','printed')")
    con.execute(f"INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) SELECT order_id, {_NOW} {legacy}")
    con.execute("UPDATE orders SET batch_uid=b.uid, batch_computer=COALESCE(orders.batch_computer, b.computer_name), "
                "batch_computer_id=COALESCE(orders.batch_computer_id, b.computer_id) FROM batches b "
                "WHERE b.id=orders.batch_id AND orders.batch_uid IS NULL "
                "AND orders.state IN ('in_batch','bundled','printed')")
    _upgrade_stamps(con)
    # orders from before 0.23: ones SnapSTL listed to make started at PRINT; the rest are history
    con.execute("UPDATE orders SET first_status=CASE WHEN state IN ('ready','needs_review','in_batch','bundled',"
                "'printed') THEN 'PRINT' ELSE UPPER(COALESCE(source_status,'')) END WHERE first_status IS NULL")
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


def set_group(con, order_id: str, group: str, **values) -> bool:
    """Set (or, with no values, clear) one FIELD_GROUPS group of an order and stamp it as the newest
    change, so it reaches every computer. Fields not given become NULL. Returns False for no such order."""
    cols = FIELD_GROUPS[group]
    unknown = set(values) - set(cols)
    if unknown:
        raise ValueError(f"Not part of {group}: {', '.join(sorted(unknown))}")
    sets = ", ".join(f"{c}=?" for c in cols)
    return con.execute(f"UPDATE orders SET {sets}, {group}_changed=?, updated_at=? WHERE order_id=?",
                       (*(values.get(c) for c in cols), now(), now(), order_id)).rowcount > 0


def add_note(con, order_id: str, text: str, author: str = "") -> dict:
    """A call note on an order (shared with every computer; never edited or deleted)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write the note first")
    n = {"uid": uuid.uuid4().hex, "order_id": order_id, "at": now(), "text": text[:2000], "author": author or None}
    con.execute("INSERT INTO order_notes(uid, order_id, at, text, author, sent) VALUES (?,?,?,?,?,0)",
                (n["uid"], n["order_id"], n["at"], n["text"], n["author"]))
    return n


def add_activity(con, kind: str, text: str) -> None:
    """One line on the Activities tab (kinds: download, build, print, check, merge, cleanup, sync)."""
    con.execute("INSERT INTO activity(at, kind, text) VALUES (?,?,?)", (now(), kind, (text or "")[:500]))


def count_usage(con, feature: str, day: str | None = None) -> None:
    """+1 use of a feature today (Settings → Usage). Feature names only, never customer data."""
    feature = (feature or "").strip()[:64]
    if not feature:
        return
    day = day or time.strftime("%Y-%m-%d")
    con.execute("INSERT INTO usage_counts(day, feature, count, sent) VALUES (?,?,1,0) ON CONFLICT(day, feature) "
                "DO UPDATE SET count=count+1, sent=0", (day, feature))


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
