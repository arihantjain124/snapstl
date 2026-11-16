"""Shared order records in Supabase (Postgres): every computer's batched, bundled and printed orders.

Each computer works in its own local database (db.py). This module keeps a shared, durable record of
the orders that have entered the making process, so all computers agree on who makes what:

  * Taking orders into a batch **claims** them in the shared records first (one statement per order,
    `… ON CONFLICT DO UPDATE … WHERE state = 'released'`). An order another computer already has is not
    taken, so no order is made twice. Without the shared records reachable, no batch is created.
  * Every later change is queued locally (sync_outbox, kept by triggers in db.py) and **pushed**:
    bundled, printed (by a person), released (back to Ready, cancelled batch, reset to ready) and newer
    webstore statuses from an uploaded report.
  * Every order from an uploaded report is **offered** as `released` (nobody is making it yet), so all
    computers see the same orders whichever one the report was uploaded on. An offer never changes
    the state of a record that's already there (`_OFFER`); if another computer has it, this one
    takes that state.
  * Changes from other computers are **pulled** (rows updated since the last pull) and merged locally.
    An order first seen this way gets the same review rules as an uploaded report.
  * The shared records are only ever added to and updated, never deleted row by row: the app has no
    DELETE, and triggers in the database refuse DELETE and TRUNCATE. Resetting the app's database
    leaves them as they are; the next sync loads them back. The one exception is `clear()` (Settings →
    Connections → Clear shared records, typed confirmation): it saves every record to a file on this
    computer, then drops the whole `snapstl` schema and starts it again empty.

Conflicts are settled in the database, by rules, not by whoever writes last (see `_WIN`):
  * the computer that claimed an order owns its in_batch → bundled steps (and can release it); an
    in_batch record without an owner (sent by an older SnapSTL) takes the next computer that sends it;
  * anyone can mark a bundled order printed, or undo that, or send a made order back to Ready;
  * a change only applies if it is at least as new as the stored one (state_at);
  * a webstore status only applies if its report is newer (status_at, see ingest.status_stamp).

Runs every SYNC_EVERY seconds in the background, right after important changes, and on Settings →
Database → Sync now. Settings are never stored here. The connection string is entered in
Settings → Shared order records and kept only on this computer (config.py).
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

from . import computer, config, db

SCHEMA = "snapstl"
SYNC_EVERY = 60
KEEP_DAILY = 30  # daily copies of the shared records kept in this computer's backups folder
PULL_OVERLAP_S = 120  # re-read rows changed shortly before the last pull (commits can land out of order)

REMOTE_STATES = ("in_batch", "bundled", "printed", "released")
_COLS = ("order_id", "clubbed_id", "order_date", "sku", "quantity", "customer_name", "phone", "address",
         "raw_name", "raw_color", "special_request", "name", "color", "source_status", "status_at",
         "state", "state_at", "batch_uid", "batch_label", "computer_id", "computer_name", "bundled_at",
         "printed_at", "updated_by")

_SETUP_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.order_records (
    order_id        text PRIMARY KEY,
    clubbed_id      text,
    order_date      text,
    sku             text,
    quantity        integer,
    customer_name   text,
    phone           text,
    address         text,
    raw_name        text,
    raw_color       text,
    special_request text,
    name            text,
    color           text,
    source_status   text,
    status_at       text,
    state           text NOT NULL CHECK (state IN ('in_batch', 'bundled', 'printed', 'released')),
    state_at        double precision NOT NULL,
    batch_uid       text,
    batch_label     text,
    computer_id     text,
    computer_name   text,
    bundled_at      double precision,
    printed_at      double precision,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_by      text
);
CREATE INDEX IF NOT EXISTS order_records_updated ON {SCHEMA}.order_records (updated_at);
CREATE TABLE IF NOT EXISTS {SCHEMA}.batch_records (
    uid           text PRIMARY KEY,
    computer_id   text,
    computer_name text,
    local_id      integer,
    label         text,
    status        text,
    created_at    double precision,
    finished_at   double precision,
    order_ids     text,
    orders        integer,
    plates        integer,
    folder        text,
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.meta (key text PRIMARY KEY, value text);
CREATE OR REPLACE FUNCTION {SCHEMA}.refuse_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'SnapSTL records are never deleted (% on %.%)', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END $$;
CREATE OR REPLACE TRIGGER order_records_no_delete BEFORE DELETE ON {SCHEMA}.order_records
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER order_records_no_truncate BEFORE TRUNCATE ON {SCHEMA}.order_records
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER batch_records_no_delete BEFORE DELETE ON {SCHEMA}.batch_records
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER batch_records_no_truncate BEFORE TRUNCATE ON {SCHEMA}.batch_records
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.refuse_delete();
-- Only SnapSTL (the database owner, through the connection string) reads or writes these tables.
-- Row level security with no policies refuses everyone else, and Supabase's API roles get no access
-- even if the schema is ever exposed in the Data API settings. The owner bypasses RLS.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['order_records', 'batch_records', 'meta'] LOOP
    IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = format('{SCHEMA}.%I', t)::regclass) THEN
      EXECUTE format('ALTER TABLE {SCHEMA}.%I ENABLE ROW LEVEL SECURITY', t);
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA {SCHEMA} FROM anon, authenticated;
    REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM anon, authenticated;
  END IF;
END $$;
"""
SCHEMA_VERSION = 1  # of the shared records; a newer one makes older apps stop syncing (update them)


def schema_sql() -> str:
    """Everything the app creates in the shared records, as one script (docs/supabase-schema.sql).
    Safe to run again; the app runs the same statements by itself on its first connection."""
    return ("-- SnapSTL shared order records (Supabase / Postgres). Generated by `uv run snapstl-db schema`;\n"
            "-- paste into Supabase → SQL Editor → Run. Safe to run again. SnapSTL creates the same by itself\n"
            "-- the first time it connects (Settings → Shared order records).\n"
            + _SETUP_SQL.strip() + "\n"
            f"INSERT INTO {SCHEMA}.meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}')\n"
            "    ON CONFLICT (key) DO NOTHING;\n")

# When may an incoming change replace the stored state? (r = stored row, EXCLUDED = incoming)
_WIN = """(EXCLUDED.state_at >= r.state_at AND (
           r.state = 'released'
        OR r.computer_id = %(actor)s
        OR (r.state = 'in_batch' AND r.computer_id IS NULL)
        OR (EXCLUDED.state = 'printed'  AND r.state IN ('bundled', 'printed'))
        OR (EXCLUDED.state = 'bundled'  AND r.state = 'printed')
        OR (EXCLUDED.state = 'released' AND r.state IN ('bundled', 'printed'))))"""
_STATE_GROUP = ("state", "state_at", "batch_uid", "batch_label", "computer_id", "computer_name", "bundled_at",
                "printed_at")
_KEEP_FIRST = ("clubbed_id", "order_date", "sku", "quantity", "raw_name", "raw_color", "special_request")
_UPSERT = (
    f"INSERT INTO {SCHEMA}.order_records AS r ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in _COLS)}) ON CONFLICT (order_id) DO UPDATE SET "
    + ", ".join(f"{c} = CASE WHEN {_WIN} THEN EXCLUDED.{c} ELSE r.{c} END" for c in _STATE_GROUP) + ", "
    + ", ".join(f"{c} = CASE WHEN {_WIN} THEN COALESCE(EXCLUDED.{c}, r.{c}) ELSE r.{c} END" for c in ("name", "color"))
    + ", "
    + ", ".join(f"{c} = COALESCE(r.{c}, EXCLUDED.{c})" for c in _KEEP_FIRST) + ", "
    + ", ".join(f"{c} = COALESCE(NULLIF(EXCLUDED.{c}, ''), r.{c})" for c in ("customer_name", "phone", "address"))
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)"
    + ", updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by "
    "RETURNING *"
)
# Claiming for a batch: only orders nobody has (new, or released) — or a claim of this computer
# whose batch no longer exists here (e.g. after the app's database was reset).
_CLAIM = (
    f"INSERT INTO {SCHEMA}.order_records AS r ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in _COLS)}) ON CONFLICT (order_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _STATE_GROUP + ("name", "color"))
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)"
    + ", updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by "
    "WHERE r.state = 'released' OR (r.state = 'in_batch' AND r.computer_id = %(actor)s "
    "AND NOT (r.batch_uid = ANY(%(live_uids)s))) RETURNING order_id"
)
# Offering an order from an uploaded report: new → stored as released; already there → only a newer
# webstore status and missing details are taken, the state is never changed.
_OFFER = (
    f"INSERT INTO {SCHEMA}.order_records AS r ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in _COLS)}) ON CONFLICT (order_id) DO UPDATE SET "
    + ", ".join(f"{c} = COALESCE(r.{c}, EXCLUDED.{c})" for c in _KEEP_FIRST) + ", "
    + ", ".join(f"{c} = COALESCE(NULLIF(r.{c}, ''), EXCLUDED.{c})" for c in ("customer_name", "phone", "address"))
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)"
    + ", updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by "
    "RETURNING *"
)
PUSH_BATCH = 1000  # orders sent per round trip group; sync_now repeats until the queue is empty


class SyncError(RuntimeError):
    """The shared records couldn't be reached or refused the change."""


def enabled() -> bool:
    return bool(config.database_url())


# ---------------- connection ----------------
_setup_done = ""  # connection string the tables were last checked on
_lock = threading.RLock()


@contextmanager
def _remote(url: str | None = None):
    """One transaction on the shared records (committed at the end, rolled back on error)."""
    global _setup_done
    import psycopg
    from psycopg.rows import dict_row

    url = url or config.database_url()
    try:
        con = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None,
                              connect_timeout=12, application_name="snapstl")
    except psycopg.Error as e:
        raise SyncError(f"Can't reach the shared records: {_short(e)}") from e
    try:
        with con:
            if _setup_done != url:
                con.execute("SELECT pg_advisory_xact_lock(83170002)")
                con.execute(_SETUP_SQL)
                r = con.execute(f"SELECT value FROM {SCHEMA}.meta WHERE key='schema_version'").fetchone()
                have = int(r["value"]) if r else 0
                if have > SCHEMA_VERSION:
                    raise SyncError("The shared records were set up by a newer SnapSTL — update this computer "
                                    "(Settings → Updates)")
                if have < SCHEMA_VERSION:
                    con.execute(f"INSERT INTO {SCHEMA}.meta(key, value) VALUES ('schema_version', %s) "
                                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(SCHEMA_VERSION),))
                _setup_done = url
            yield con
    except psycopg.Error as e:
        raise SyncError(f"The shared records refused the change: {_short(e)}") from e
    finally:
        con.close()


def _short(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


# ---------------- local ↔ remote rows ----------------
def remote_state(local_state: str) -> str:
    return local_state if local_state in ("in_batch", "bundled", "printed") else "released"


def _payload(o: dict) -> dict:
    """A local order as a shared record (written by this computer)."""
    st = remote_state(o["state"])
    own = st != "released"
    return {
        "order_id": o["order_id"], "clubbed_id": o.get("clubbed_id"), "order_date": o.get("order_date"),
        "sku": o.get("sku"), "quantity": o.get("quantity"), "customer_name": o.get("customer_name") or "",
        "phone": o.get("phone") or "", "address": o.get("address") or "", "raw_name": o.get("raw_name"),
        "raw_color": o.get("raw_color"), "special_request": o.get("special_request"), "name": o.get("name"),
        "color": o.get("color"), "source_status": o.get("source_status"), "status_at": o.get("status_at"),
        "state": st, "state_at": float(o.get("state_at") or db.now()),
        "batch_uid": o.get("batch_uid") if own else None, "batch_label": o.get("batch_label") if own else None,
        "computer_id": o.get("batch_computer_id") if own else None,
        "computer_name": o.get("batch_computer") if own else None,
        "bundled_at": o.get("bundled_at"), "printed_at": o.get("printed_at") if st == "printed" else None,
        "updated_by": computer.NAME, "actor": computer.ID,
    }


def _evaluate(local: dict, status: str | None) -> str:
    """Local state for an order nobody is making (released)."""
    from .ingest import PRINT_STATUSES

    if (status or "") not in PRINT_STATUSES:
        return "not_needed"
    return "needs_review" if (local.get("review_reason") or not local.get("color")) else "ready"


def _review(r: dict) -> tuple[str, str | None]:
    """(state, review_reason) for an order first seen in the shared records: the same rules as an
    uploaded report (ingest). A name changed by a person on another computer counts as checked."""
    from .ingest import _evaluate as judge, clean_name

    name0, review = clean_name(r.get("raw_name") or "")
    if r.get("name") and r["name"] != name0:
        review = None
    if r.get("special_request"):
        review = "; ".join(x for x in [review, f"Special request: {r['special_request']}"] if x)
    return judge(r.get("name") or "", r.get("color") or "", review, (r.get("source_status") or "").upper())


def _many(rc, sql: str, params: list[dict]) -> list[dict]:
    """One statement per params, sent together (pipelined); the RETURNING row of each, in order."""
    if not params:
        return []
    cur = rc.cursor()
    cur.executemany(sql, params, returning=True)
    out = []
    while True:
        out.append(cur.fetchone())
        if not cur.nextset():
            break
    return out


def _local_batch_uids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT uid FROM batches WHERE uid IS NOT NULL")}


def merge(con, r: dict, force: bool = False) -> str | None:
    """Bring one shared record into the local database. Returns the order_id if this computer must
    release a stale claim of its own (its batch no longer exists here)."""
    oid = r["order_id"]
    cur = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
    L = dict(cur) if cur else None
    mine = r.get("computer_id") == computer.ID
    uids = _local_batch_uids(con)
    if L is None:
        state = r["state"]
        stale = state == "in_batch" and mine and r.get("batch_uid") not in uids
        review = None
        if state == "released" or stale:
            state, review = _review(r)
        local_batch = None
        if mine and r.get("batch_uid") in uids:
            row = con.execute("SELECT id FROM batches WHERE uid=?", (r["batch_uid"],)).fetchone()
            local_batch = row[0] if row else None
        con.execute(
            "INSERT INTO orders(order_id,clubbed_id,sku,order_date,source_status,quantity,raw_name,raw_color,"
            "special_request,name,color,review_reason,state,batch_id,first_file,last_file,first_seen,updated_at,"
            "customer_name,phone,address,state_at,status_at,in_remote,batch_uid,batch_label,batch_computer,"
            "batch_computer_id,printed_at,bundled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (oid, r.get("clubbed_id"), r.get("sku") or "Magicians_35", r.get("order_date"), r.get("source_status"),
             r.get("quantity") or 1, r.get("raw_name"), r.get("raw_color"), r.get("special_request"), r.get("name"),
             r.get("color"), review, state, local_batch, "shared records", "shared records", db.now(), db.now(),
             r.get("customer_name"), r.get("phone"), r.get("address"), r["state_at"], r.get("status_at"),
             r.get("batch_uid") if state in ("in_batch", "bundled", "printed") else None, r.get("batch_label"),
             r.get("computer_name"), r.get("computer_id"), r.get("printed_at"), r.get("bundled_at")))
        return oid if stale else None

    sets: dict = {"in_remote": 1}
    # webstore status: the newer report wins
    if (r.get("status_at") or "") > (L.get("status_at") or ""):
        sets["source_status"], sets["status_at"] = r.get("source_status"), r.get("status_at")
        if L["state"] in ("ready", "needs_review", "not_needed"):
            sets["state"] = _evaluate(L, r.get("source_status"))
    # who's making it: the newer change wins (unless this computer has its own change on the way)
    pending = con.execute("SELECT 1 FROM sync_outbox WHERE order_id=?", (oid,)).fetchone() is not None
    # a name / colour fixed on another computer (not yet made anywhere)
    if (not pending and r.get("state") == "released" and r.get("name")
            and L["state"] in ("ready", "needs_review", "not_needed")
            and (r["name"], r.get("color") or L.get("color")) != (L.get("name"), L.get("color"))):
        color = r.get("color") or L.get("color")
        sets.update(name=r["name"], color=color, name_edited=1, review_reason=None)
        if sets.get("state", L["state"]) in ("ready", "needs_review"):
            sets["state"] = "ready" if color else "needs_review"
    stale = None
    if force or (not pending and float(r["state_at"]) > float(L.get("state_at") or 0)):
        st = r["state"]
        own_live = mine and r.get("batch_uid") in uids
        if st == "in_batch" and mine and not own_live:
            stale = oid  # this computer's claim, but the batch is gone here: release it
        elif st == "released":
            if L["state"] in ("in_batch", "bundled", "printed") and not (L["state"] == "in_batch" and L["batch_id"]):
                sets.update(state=_evaluate(L, sets.get("source_status", L.get("source_status"))), batch_id=None,
                            batch_uid=None, batch_label=None, batch_computer=None, batch_computer_id=None,
                            printed_at=None, state_at=r["state_at"])
        elif not (L["state"] == "in_batch" and L["batch_id"] and not mine):  # never pull out of a live local batch
            local_batch = L["batch_id"] if own_live else None
            if own_live and local_batch is None:
                row = con.execute("SELECT id FROM batches WHERE uid=?", (r["batch_uid"],)).fetchone()
                local_batch = row[0] if row else None
            sets.update(state=st, batch_id=local_batch, batch_uid=r.get("batch_uid"),
                        batch_label=r.get("batch_label"), batch_computer=r.get("computer_name"),
                        batch_computer_id=r.get("computer_id"), printed_at=r.get("printed_at"),
                        bundled_at=r.get("bundled_at"), state_at=r["state_at"])
    cols = ", ".join(f"{k}=?" for k in sets)
    con.execute(f"UPDATE orders SET {cols} WHERE order_id=?", (*sets.values(), oid))
    return stale


def _release_stale(con, ids: list[str]) -> None:
    """Claims of this computer whose batch is gone: back to Ready here and released in the shared records."""
    for oid in ids:
        L = dict(con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())
        st = _evaluate(L, L.get("source_status")) if L["state"] in ("in_batch", "ready", "needs_review",
                                                                     "not_needed") else L["state"]
        con.execute("UPDATE orders SET state=?, batch_id=NULL, batch_uid=NULL, batch_label=NULL, batch_computer=NULL, "
                    "batch_computer_id=NULL, state_at=? WHERE order_id=?", (st, db.now(), oid))
        con.execute("INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES (?, ?)", (oid, db.now()))


# ---------------- claim (creating a batch) ----------------
def claim(orders: list[dict], batch_uid: str) -> tuple[list[str], float]:
    """Claim these local orders for a new batch of this computer. Returns (claimed ids, state_at).

    Orders already taken elsewhere are merged in locally (they leave Ready here) and not returned."""
    t = db.now()
    with db.tx() as con:
        live = sorted(_local_batch_uids(con))
    claimed: list[str] = []
    taken: list[dict] = []
    with _lock, _remote() as rc:
        for o in orders:
            p = _payload({**o, "state": "in_batch", "state_at": t, "batch_uid": batch_uid, "batch_label": None,
                          "batch_computer_id": computer.ID, "batch_computer": computer.NAME})
            if rc.execute(_CLAIM, {**p, "live_uids": live}).fetchone():
                claimed.append(o["order_id"])
        rest = [o["order_id"] for o in orders if o["order_id"] not in claimed]
        if rest:
            taken = rc.execute(f"SELECT * FROM {SCHEMA}.order_records WHERE order_id = ANY(%s)", (rest,)).fetchall()
    if taken:
        with db.tx() as con, db.quiet(con):
            for r in taken:
                merge(con, r, force=True)
    return claimed, t


# ---------------- push / pull ----------------
def _push(rc) -> int:
    """Send queued changes and offer orders the shared records don't have yet. Returns how many."""
    with db.tx() as con:
        con.execute("INSERT OR IGNORE INTO sync_outbox(order_id, queued_at) SELECT order_id, ? FROM orders "
                    "WHERE COALESCE(in_remote,0)=0", (db.now(),))
        queued = db.rows(con, "SELECT o.*, q.queued_at FROM sync_outbox q JOIN orders o USING(order_id) "
                              f"ORDER BY q.queued_at LIMIT {PUSH_BATCH}")
        orphans = [r[0] for r in con.execute("SELECT order_id FROM sync_outbox WHERE order_id NOT IN "
                                             "(SELECT order_id FROM orders)")]
        if orphans:
            con.execute(f"DELETE FROM sync_outbox WHERE order_id IN ({','.join('?' * len(orphans))})", orphans)
    offers = [o for o in queued if not o.get("in_remote") and o["state"] not in ("in_batch", "bundled", "printed")]
    changes = [o for o in queued if o not in offers]
    results = []
    for group, sql in ((offers, _OFFER), (changes, _UPSERT)):
        ps = [_payload(o) for o in group]
        results += [(o, p, back, sql is _OFFER) for o, p, back in zip(group, ps, _many(rc, sql, ps))]
    with db.tx() as con:
        stale = []
        with db.quiet(con):
            for o, p, back, offer in results:
                refused = (back["state"] != "released" if offer else
                           (back["state"], back["state_at"], back.get("batch_uid")) != (p["state"], p["state_at"], p["batch_uid"]))
                if refused:
                    s = merge(con, back, force=True)  # someone else has it / refused by the rules: take the stored truth
                    if s:
                        stale.append(s)
                else:
                    con.execute("UPDATE orders SET in_remote=1 WHERE order_id=?", (o["order_id"],))
                con.execute("DELETE FROM sync_outbox WHERE order_id=? AND queued_at=?", (o["order_id"], o["queued_at"]))
        _release_stale(con, stale)
    return len(queued)


def _push_batches(rc) -> int:
    with db.tx() as con:
        bs = db.rows(con, "SELECT * FROM batches WHERE COALESCE(synced,0)=0")
    for b in bs:
        s = json.loads(b.get("summary") or "{}")
        rc.execute(
            f"INSERT INTO {SCHEMA}.batch_records AS r (uid, computer_id, computer_name, local_id, label, status, "
            "created_at, finished_at, order_ids, orders, plates, folder) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (uid) DO UPDATE SET label=EXCLUDED.label, "
            "status=EXCLUDED.status, finished_at=EXCLUDED.finished_at, order_ids=EXCLUDED.order_ids, "
            "orders=EXCLUDED.orders, plates=EXCLUDED.plates, folder=EXCLUDED.folder, updated_at=clock_timestamp()",
            (b["uid"], b.get("computer_id") or computer.ID, b.get("computer_name") or computer.NAME, b["id"],
             s.get("label"), b["status"], b.get("created_at"), b.get("finished_at"), b.get("order_ids"),
             len(json.loads(b.get("order_ids") or "[]")), len(s.get("plates", [])), s.get("folder")))
    if bs:
        with db.tx() as con:
            for b in bs:  # only if it hasn't changed again meanwhile
                con.execute("UPDATE batches SET synced=1 WHERE id=? AND status=? AND COALESCE(summary,'')=?",
                            (b["id"], b["status"], b.get("summary") or ""))
    return len(bs)


def _pull(rc) -> int:
    with db.tx() as con:
        r = con.execute("SELECT value FROM sync_state WHERE key='pulled_until'").fetchone()
    since = r[0] if r else None
    if since:
        rows = rc.execute(f"SELECT *, updated_at::text AS updated_text FROM {SCHEMA}.order_records "
                          f"WHERE updated_at > %s::timestamptz - interval '{PULL_OVERLAP_S} seconds' "
                          "ORDER BY updated_at", (since,)).fetchall()
    else:
        rows = rc.execute(f"SELECT *, updated_at::text AS updated_text FROM {SCHEMA}.order_records "
                          "ORDER BY updated_at").fetchall()
    with db.tx() as con:
        stale = []
        with db.quiet(con):
            for row in rows:
                s = merge(con, row)
                if s:
                    stale.append(s)
            if rows:
                con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('pulled_until', ?)",
                            (rows[-1]["updated_text"],))
        _release_stale(con, stale)
    return len(rows)


# ---------------- running it ----------------
class Status:
    def __init__(self) -> None:
        self.last_ok = 0.0
        self.last_try = 0.0
        self.error = ""
        self.pushed = 0
        self.pulled = 0
        self.running = False
        self.copy_error = ""

    def snapshot(self) -> dict:
        with db.tx() as con:
            pending = con.execute("SELECT COUNT(*) FROM (SELECT order_id FROM sync_outbox UNION "
                                  "SELECT order_id FROM orders WHERE COALESCE(in_remote,0)=0)").fetchone()[0]
        return {"enabled": enabled(), "last_ok": self.last_ok, "last_try": self.last_try, "error": self.error,
                "pending": pending, "pushed": self.pushed, "pulled": self.pulled, "running": self.running,
                "computer": computer.NAME, "last_copy": last_copy(), "copy_error": self.copy_error}


STATUS = Status()


def sync_now() -> dict:
    """Push this computer's changes, then pull everyone's. Safe to call any time (one at a time)."""
    if not enabled():
        return STATUS.snapshot()
    with _lock:
        STATUS.running = True
        STATUS.last_try = time.time()
        try:
            with _remote() as rc:
                STATUS.pushed = n = _push(rc)
                for _ in range(20):  # a big report: keep sending until everything is there
                    if n < PUSH_BATCH:
                        break
                    n = _push(rc)
                    STATUS.pushed += n
                _push_batches(rc)
            with _remote() as rc:  # pushes are committed before pulling
                STATUS.pulled = _pull(rc)
            with _remote() as rc:  # stale claims released while pulling
                STATUS.pushed += _push(rc)
            STATUS.last_ok = time.time()
            STATUS.error = ""
        except SyncError as e:
            STATUS.error = str(e)
        except Exception as e:  # noqa: BLE001  (never let the background loop die)
            STATUS.error = f"Sync failed: {_short(e)}"
        finally:
            STATUS.running = False
    return STATUS.snapshot()


def pending() -> int:
    with db.tx() as con:
        return con.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]


class _Loop:
    def __init__(self) -> None:
        self._ev = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not enabled() or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-sync")
        self._thread.start()

    def kick(self) -> None:
        """Sync soon (after an important change)."""
        self._ev.set()

    def _run(self) -> None:
        while True:
            sync_now()
            daily_copy()
            self._ev.wait(SYNC_EVERY)
            self._ev.clear()
            time.sleep(1.0)  # let a burst of changes settle


LOOP = _Loop()


def check(url: str | None = None) -> dict:
    """Connection test (creates the tables if needed) and a short summary of the shared records."""
    with _remote(url) as rc:
        n = rc.execute(f"SELECT state, COUNT(*) AS n FROM {SCHEMA}.order_records GROUP BY state").fetchall()
        b = rc.execute(f"SELECT computer_name, COUNT(*) AS n FROM {SCHEMA}.batch_records GROUP BY computer_name").fetchall()
    return {"orders": {r["state"]: r["n"] for r in n}, "batches": {r["computer_name"]: r["n"] for r in b}}


def summary() -> dict:
    """What the shared records hold now: orders per state, batches per computer (Settings → Connections)."""
    return check()


def clear(confirm: str) -> dict:
    """Empty the shared records (every computer's), after saving them all to a JSON file in this
    computer's backups folder. Dropping the schema bypasses the no-delete triggers on purpose; the
    tables are created again, empty, right away. This computer's batched / bundled / printed orders are
    sent again by the next sync (they're still being made here); other computers send theirs when they
    are cleared there too or when their orders change."""
    from .maintenance import BACKUP_DIR

    if confirm != "CLEAR":
        raise SyncError("Type CLEAR to confirm")
    global _setup_done
    with _lock:
        dest = BACKUP_DIR / time.strftime("shared-records-%Y%m%d-%H%M%S.json")
        with _remote() as rc:
            saved = _save_copy(rc, dest)
        import psycopg

        try:
            with psycopg.connect(config.database_url(), autocommit=True, connect_timeout=12,
                                 prepare_threshold=None, application_name="snapstl") as c:
                c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        except psycopg.Error as e:
            raise SyncError(f"The shared records couldn't be cleared: {_short(e)}") from e
        _setup_done = ""
        with db.tx() as con:  # this computer starts over: send its own again, load everything anew
            con.execute("UPDATE orders SET in_remote=0")
            con.execute("UPDATE batches SET synced=0")
            con.execute("DELETE FROM sync_state WHERE key='pulled_until'")
        with _remote():
            pass  # create the empty tables again
    return {"cleared": saved, "backup": str(dest)}


# ---------------- copies on this computer ----------------
def _save_copy(rc, dest) -> dict:
    """Every shared record, as JSON, in `dest` (readable by this user only: names, phones, addresses)."""
    orders = rc.execute(f"SELECT * FROM {SCHEMA}.order_records ORDER BY order_id").fetchall()
    batches = rc.execute(f"SELECT * FROM {SCHEMA}.batch_records ORDER BY uid").fetchall()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"saved_at": time.time(), "by": computer.NAME, "order_records": orders,
                   "batch_records": batches}, f, default=str, indent=1)
    os.replace(tmp, dest)
    return {"orders": len(orders), "batches": len(batches)}


def _daily_path():
    from .maintenance import BACKUP_DIR

    return BACKUP_DIR / time.strftime("shared-records-daily-%Y-%m-%d.json")


def export(daily: bool = False) -> dict:
    """Save a copy of every shared record in this computer's backups folder. Daily copies are one file
    per day (the newest KEEP_DAILY kept); others are timestamped and kept (`snapstl-db export`)."""
    from .maintenance import BACKUP_DIR

    dest = _daily_path() if daily else BACKUP_DIR / time.strftime("shared-records-%Y%m%d-%H%M%S.json")
    with _lock, _remote() as rc:
        saved = _save_copy(rc, dest)
    if daily:
        for old in sorted(BACKUP_DIR.glob("shared-records-daily-*.json"))[:-KEEP_DAILY]:
            old.unlink(missing_ok=True)
    return {"file": str(dest), **saved}


def last_copy() -> dict:
    """The newest copy of the shared records on this computer (Settings → Connections)."""
    from .maintenance import BACKUP_DIR

    files = list(BACKUP_DIR.glob("shared-records-*.json")) if BACKUP_DIR.is_dir() else []
    if not files:
        return {}
    f = max(files, key=lambda p: p.stat().st_mtime)
    return {"file": f.name, "at": f.stat().st_mtime}


def daily_copy() -> None:
    """Once a day, after a good sync: a copy of the shared records (Supabase's free plan keeps no
    backups you can download). Failures are shown in the sync status and tried again next round."""
    if STATUS.error or _daily_path().exists():
        return
    try:
        export(daily=True)
        STATUS.copy_error = ""
    except (SyncError, OSError) as e:
        STATUS.copy_error = f"Daily copy failed: {_short(e)}"
