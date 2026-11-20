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
# Every computer needs the shared records: without a working connection nothing can be changed (Orders,
# Batches, Print …) until it's back — only Settings, Sync and the helper's running downloads work.
REQUIRED = os.environ.get("SNAPSTL_REQUIRE_SHARED", "1") != "0"
KEEP_DAILY = 30  # daily copies of the shared records kept in this computer's backups folder
PULL_OVERLAP_S = 120  # re-read rows changed shortly before the last pull (commits can land out of order)

REMOTE_STATES = ("in_batch", "bundled", "printed", "released")
_COLS = ("order_id", "clubbed_id", "order_date", "sku", "quantity", "customer_name", "phone", "address",
         "raw_name", "raw_color", "special_request", "name", "color", "source_status", "status_at",
         "state", "state_at", "batch_uid", "batch_label", "computer_id", "computer_name", "bundled_at",
         "printed_at", "app_state", "review_reason", "made_at", "updated_by", "first_status", "print_started_at",
         *db.GROUP_COLS)

from .ingest import PRINT_STATUSES as _PS  # noqa: E402

PRINT_SQL = ", ".join(f"'{x}'" for x in sorted(_PS))
from . import report as _report  # noqa: E402
from . import tabs as _tabs  # noqa: E402

REPORT_SQL = _report.setup_sql(SCHEMA).strip() + "\n"
_REPORT_UPSERT = _report.upsert_sql(SCHEMA)
_PG_TYPE = {"REAL": "double precision", "TEXT": "text"}
_NEW_COLS = (("first_status", "TEXT",
              "Webstore status when SnapSTL first saw the order (first seen past PRINT = history, never flagged)."),
             ("print_started_at", "REAL", "When a plate holding the order was first started (epoch seconds) → Printing."),
             *((c, db._GROUP_TYPES.get(c, "TEXT"), None) for g in db.FIELD_GROUPS.values() for c in g),
             *((f"{g}_changed", "REAL", f"Last change of {', '.join(cs)} (epoch seconds): the newest change wins.")
               for g, cs in db.FIELD_GROUPS.items()))
_COL_COMMENTS = {
    "label_at": "Label Generated pressed on the logistics page (epoch seconds) → Label done – ship it; NULL = no label yet.",
    "label_by": "Who pressed Label Generated (e.g. logistics).",
    "label_note": "Shown with the label, e.g. 'Label expired – make a new label' (AWB EXPIRED clears label_at).",
    "express_by": "Express: date the customer needs it by (YYYY-MM-DD); NULL = not express.",
    "express_note": "Express: note from the customer call.",
    "flag": "Why the order is in Problems – check: " + " | ".join(db.FLAGS) + "; NULL = none.",
    "flag_note": "Words shown with the flag.",
    "name_change_reason": "Why the name / colour was changed after confirming with the customer.",
    "name_confirmed_at": "When that change was confirmed with the customer (epoch seconds).",
    "remake_at": "Remake requested (epoch seconds); NULL = none.",
    "remake_reason": "Why a remake was requested.",
    "reuse_of": "This order is sent with the returned (RTO) piece of that order.",
    "reused_by": "This returned (RTO) piece went to that order (each piece is used once).",
    "print_done_at": "Every plate holding the order printed (epoch seconds): Printing, waiting for check.",
    "checked_at": "Base and name checked good on Print → Verify (epoch seconds) → Printed – pack it.",
    "checked_by": "Staff member who checked it (Print → Verify); Before 0.24 = printed before checks existed.",
}
_NEW_COLS_SQL = "".join(
    f"ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS {c} {_PG_TYPE[t]};\n" for c, t, _ in _NEW_COLS) + "".join(
    f"COMMENT ON COLUMN {SCHEMA}.order_records.{c} IS '{(d or _COL_COMMENTS[c]).replace(chr(39), chr(39) * 2)}';\n"
    for c, _, d in _NEW_COLS if d or c in _COL_COMMENTS)
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
-- A new id every time the records are created (again, after Clear / Reset with "also clear Supabase"):
-- each computer notices it changed and makes itself match the empty records (sync._follow_reset).
INSERT INTO {SCHEMA}.meta(key, value) VALUES ('instance', gen_random_uuid()::text) ON CONFLICT (key) DO NOTHING;
-- The Orders page's tabs, for anyone reading the records (e.g. another agent querying them): the app
-- sends its own state, review reason and Ready-to-ship time; a trigger works out `tab` and `ready_type`
-- from the row with the same rules as the Orders page, whichever computer (or SnapSTL version) wrote it.
ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS app_state text;
ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS review_reason text;
ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS made_at double precision;
ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS tab text;
ALTER TABLE {SCHEMA}.order_records ADD COLUMN IF NOT EXISTS ready_type text;
{_NEW_COLS_SQL}CREATE INDEX IF NOT EXISTS order_records_tab ON {SCHEMA}.order_records (tab, ready_type);
CREATE INDEX IF NOT EXISTS order_records_club ON {SCHEMA}.order_records (clubbed_id);
CREATE OR REPLACE FUNCTION {SCHEMA}.set_tab() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s text;  -- the app state: who's making it, else the app's own view (older SnapSTL: from the status)
BEGIN
  s := CASE WHEN NEW.state IN ('in_batch', 'bundled', 'printed') THEN NEW.state
            WHEN NEW.app_state IN ('ready', 'needs_review', 'skipped', 'not_needed') THEN NEW.app_state
            WHEN upper(coalesce(NEW.source_status, '')) IN ({PRINT_SQL}) THEN 'ready'
            ELSE 'not_needed' END;
  NEW.tab := {_tabs.case_sql(lambda c: "NEW." + c, state="s")};  -- tabs.py: the Orders page's rule
  NEW.ready_type := CASE
    WHEN NEW.tab <> 'to_make' THEN NULL
    WHEN NEW.sku = 'Adhoc' THEN 'adhoc'
    WHEN coalesce(NEW.clubbed_id, '') <> '' AND (SELECT count(*) FROM {SCHEMA}.order_records o
         WHERE o.clubbed_id = NEW.clubbed_id AND o.order_id <> NEW.order_id) > 0 THEN 'clubbed'
    ELSE 'single' END;
  RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER order_records_tab BEFORE INSERT OR UPDATE ON {SCHEMA}.order_records
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.set_tab();
-- a new order of a clubbed group makes its partners "clubbed" too
CREATE OR REPLACE FUNCTION {SCHEMA}.retab_club() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(NEW.clubbed_id, '') <> '' THEN
    UPDATE {SCHEMA}.order_records SET ready_type = ready_type
     WHERE clubbed_id = NEW.clubbed_id AND order_id <> NEW.order_id AND tab = 'to_make' AND ready_type IS DISTINCT FROM 'clubbed';
  END IF;
  RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER order_records_retab_club AFTER INSERT ON {SCHEMA}.order_records
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.retab_club();
-- rows without a tab, or with a tab from before 0.23: work it out again (once; updated_at is kept)
UPDATE {SCHEMA}.order_records SET tab = tab WHERE tab IS NULL OR tab NOT IN ({", ".join(f"'{k}'" for k in _tabs.KEYS)});
COMMENT ON TABLE {SCHEMA}.order_records IS 'SnapSTL orders shared by every computer (one row per webstore order ID). Read-only for anyone but SnapSTL. Never deleted.';
COMMENT ON COLUMN {SCHEMA}.order_records.tab IS 'Orders page tab (0.23, tabs.py): {" | ".join(_tabs.KEYS)}. Make: to_make, check_name, ready_to_print, printing; pack & ship: pack_it (printed), ship_it (label done or PACK / HANDOVER); records: on_the_way, delivered, rto, cancelled, problems (see flag), skipped. Set by a trigger.';
COMMENT ON COLUMN {SCHEMA}.order_records.ready_type IS 'For tab = to_make: single | clubbed (several orders share clubbed_id) | adhoc (typed in by hand, sku Adhoc); NULL otherwise. Set by a trigger.';
COMMENT ON COLUMN {SCHEMA}.order_records.state IS 'Who is making it: in_batch | bundled | printed (computer_name has it) | released (nobody yet — see tab).';
COMMENT ON COLUMN {SCHEMA}.order_records.app_state IS 'SnapSTL''s own state on the computer that last wrote the row: ready | needs_review | in_batch | bundled | printed | skipped | not_needed.';
COMMENT ON COLUMN {SCHEMA}.order_records.review_reason IS 'Why the order needs a person to check it (tab = needs_review), e.g. a name with a space or a special request.';
COMMENT ON COLUMN {SCHEMA}.order_records.made_at IS 'When base and name were both printed (epoch seconds) → tab pack_it.';
COMMENT ON COLUMN {SCHEMA}.order_records.source_status IS 'Webstore order status from the newest uploaded report (status_at says how recent).';
COMMENT ON COLUMN {SCHEMA}.order_records.name IS 'Name to print (cleaned or corrected); raw_name is what the customer typed.';
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
CREATE TABLE IF NOT EXISTS {SCHEMA}.order_notes (
    uid       text PRIMARY KEY,
    order_id  text NOT NULL,
    at        double precision NOT NULL,
    text      text NOT NULL,
    author    text,
    computer  text,
    stored_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS order_notes_order ON {SCHEMA}.order_notes (order_id, at);
CREATE INDEX IF NOT EXISTS order_notes_stored ON {SCHEMA}.order_notes (stored_at);
COMMENT ON TABLE {SCHEMA}.order_notes IS 'Call notes on an order (logistics page); `at` = epoch seconds. Never edited or deleted.';
CREATE TABLE IF NOT EXISTS {SCHEMA}.usage_counts (
    computer_id   text NOT NULL,
    computer_name text,
    day           text NOT NULL,
    feature       text NOT NULL,
    count         integer NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (computer_id, day, feature)
);
COMMENT ON TABLE {SCHEMA}.usage_counts IS 'How often each SnapSTL feature was used, per computer per day (feature names only, no customer data).';
CREATE TABLE IF NOT EXISTS {SCHEMA}.staff (
    name       text PRIMARY KEY,
    added_at   double precision,
    hidden     boolean NOT NULL DEFAULT false,
    changed    double precision,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE {SCHEMA}.staff IS 'Staff who check prints (Print → Verify); a name is hidden, never removed. Newest `changed` wins.';
ALTER TABLE {SCHEMA}.staff ADD COLUMN IF NOT EXISTS pin_hash text;
ALTER TABLE {SCHEMA}.staff ADD COLUMN IF NOT EXISTS pin_changed double precision;
COMMENT ON COLUMN {SCHEMA}.staff.pin_hash IS 'Salted scrypt hash of the 4-digit PIN typed on Print → Verify (0.25.1); newest pin_changed wins.';
CREATE TABLE IF NOT EXISTS {SCHEMA}.print_checks (
    uid        text PRIMARY KEY,
    batch_uid  text,
    plate      text NOT NULL,
    idx        integer NOT NULL,
    order_id   text,
    part       text NOT NULL,
    name       text,
    color      text,
    result     text NOT NULL,
    letters    text,
    staff      text,
    note       text,
    at         double precision NOT NULL,
    printed_on text,
    printer    text,
    fix_file   text,
    computer   text,
    stored_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS print_checks_order ON {SCHEMA}.print_checks (order_id, at);
CREATE INDEX IF NOT EXISTS print_checks_stored ON {SCHEMA}.print_checks (stored_at);
COMMENT ON TABLE {SCHEMA}.print_checks IS 'Print → Verify: one row per printed base / name checked (result good | letters | name | base | colour | plate_failed), by whom. The newest row of an item counts. Never edited or deleted.';
CREATE OR REPLACE TRIGGER print_checks_no_delete BEFORE DELETE ON {SCHEMA}.print_checks
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER print_checks_no_truncate BEFORE TRUNCATE ON {SCHEMA}.print_checks
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER staff_no_delete BEFORE DELETE ON {SCHEMA}.staff
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER order_notes_no_delete BEFORE DELETE ON {SCHEMA}.order_notes
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER order_notes_no_truncate BEFORE TRUNCATE ON {SCHEMA}.order_notes
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER usage_counts_no_delete BEFORE DELETE ON {SCHEMA}.usage_counts
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.refuse_delete();
CREATE OR REPLACE TRIGGER usage_counts_no_truncate BEFORE TRUNCATE ON {SCHEMA}.usage_counts
    FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.refuse_delete();
{REPORT_SQL}-- Only SnapSTL (the database owner, through the connection string) reads or writes these tables.
-- Row level security with no policies refuses everyone else, and Supabase's API roles get no access
-- even if the schema is ever exposed in the Data API settings. The owner bypasses RLS.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['order_records', 'batch_records', 'meta', 'report_rows', 'report_uploads',
                           'order_notes', 'usage_counts', 'staff', 'print_checks'] LOOP
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
from . import shop as _shop  # noqa: E402

_SETUP_SQL += _shop.setup_sql()  # the `shop` schema for questions (shop.py) — after snapstl, it reads it
# Status stamps since 0.22.21 put the file's moment first ("@file|event|upload", ingest.status_stamp).
# Older ones ("event|file|upload", also from computers not updated yet) are rewritten here; the shop
# triggers are paused meanwhile (only the stamp's order changes, nothing to tidy again). A failure
# here only warns.
_SETUP_SQL += f"""CREATE OR REPLACE FUNCTION {SCHEMA}.stamp_v2(s text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN s IS NULL OR s LIKE '@%' OR s NOT LIKE '%|%|%' THEN s
    ELSE '@' || split_part(s, '|', 2) || '|' || split_part(s, '|', 1) || '|' || split_part(s, '|', 3) END $$;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM {SCHEMA}.order_records WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%')
     OR EXISTS (SELECT 1 FROM {SCHEMA}.report_rows WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%')
     OR EXISTS (SELECT 1 FROM shop.report_lines WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%') THEN
    ALTER TABLE {SCHEMA}.order_records DISABLE TRIGGER shop_bridge;
    ALTER TABLE {SCHEMA}.report_rows DISABLE TRIGGER shop_bridge;
    ALTER TABLE shop.report_lines DISABLE TRIGGER report_lines_apply;
    UPDATE {SCHEMA}.order_records SET status_at = {SCHEMA}.stamp_v2(status_at)
     WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%';
    UPDATE {SCHEMA}.report_rows SET status_at = {SCHEMA}.stamp_v2(status_at)
     WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%';
    UPDATE shop.report_lines SET status_at = {SCHEMA}.stamp_v2(status_at)
     WHERE status_at LIKE '%|%|%' AND status_at NOT LIKE '@%';
    ALTER TABLE {SCHEMA}.order_records ENABLE TRIGGER shop_bridge;
    ALTER TABLE {SCHEMA}.report_rows ENABLE TRIGGER shop_bridge;
    ALTER TABLE shop.report_lines ENABLE TRIGGER report_lines_apply;
  END IF;
EXCEPTION WHEN others THEN  -- never block the sync: the stamps are only compared, and merge upgrades them too
  RAISE WARNING 'SnapSTL: status stamps not rewritten: %', SQLERRM;
END $$;
"""
SCHEMA_VERSION = 3  # of the shared records; a newer one makes older apps stop syncing (update them)
# 2 (0.23): order flow + logistics fields, order_notes, usage_counts: older SnapSTLs must not write
# 3 (0.24): Print → Verify (check fields, staff, print_checks): an older SnapSTL would move orders to
#           Printed – pack it without a check


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
_WIN = """(EXCLUDED.state_at >= r.state_at - 0.001 AND (  -- 1 ms: local clocks are stored with rounding noise
           r.state = 'released'
        OR r.computer_id = %(actor)s
        OR (r.state = 'in_batch' AND r.computer_id IS NULL)
        OR (EXCLUDED.state = 'printed'  AND r.state IN ('bundled', 'printed'))
        OR (EXCLUDED.state = 'bundled'  AND r.state = 'printed')
        OR (EXCLUDED.state = 'released' AND r.state IN ('bundled', 'printed'))))"""
_STATE_GROUP = ("state", "state_at", "batch_uid", "batch_label", "computer_id", "computer_name", "bundled_at",
                "printed_at", "made_at", "print_started_at")
# The app's own view of an order nobody makes yet (ready / needs review / not needed, and why) — the newest
# one sent applies, whatever the state rules say (they are about who makes it).
_VIEW = ("app_state", "review_reason")
_KEEP_FIRST = ("clubbed_id", "order_date", "sku", "quantity", "raw_name", "raw_color", "special_request",
               "first_status")
# Things people set on an order (db.FIELD_GROUPS): per group, the newest `<group>_changed` wins, whoever sent it.
_GROUPS_SQL = ", " + ", ".join(
    f"{c} = CASE WHEN COALESCE(EXCLUDED.{g}_changed, 0) > COALESCE(r.{g}_changed, 0) THEN EXCLUDED.{c} ELSE r.{c} END"
    for g, cs in db.FIELD_GROUPS.items() for c in (*cs, f"{g}_changed"))
_UPSERT = (
    f"INSERT INTO {SCHEMA}.order_records AS r ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in _COLS)}) ON CONFLICT (order_id) DO UPDATE SET "
    + ", ".join(f"{c} = CASE WHEN {_WIN} THEN EXCLUDED.{c} ELSE r.{c} END" for c in _STATE_GROUP) + ", "
    # the app's view (ready / needs review / skipped) follows the newest change (_WIN), so a computer that
    # hasn't heard of a Skip or Approve yet can't overwrite it with its older view; a record without
    # any view yet (older SnapSTL) takes the first one sent
    + ", ".join(f"{c} = CASE WHEN {_WIN} OR (r.state = 'released' AND EXCLUDED.state = 'released' "
                "AND r.app_state IS NULL) "
                f"THEN EXCLUDED.{c} ELSE r.{c} END" for c in _VIEW) + ", "
    + ", ".join(f"{c} = CASE WHEN {_WIN} THEN COALESCE(EXCLUDED.{c}, r.{c}) ELSE r.{c} END" for c in ("name", "color"))
    + ", "
    + ", ".join(f"{c} = COALESCE(r.{c}, EXCLUDED.{c})" for c in _KEEP_FIRST) + ", "
    + ", ".join(f"{c} = COALESCE(NULLIF(EXCLUDED.{c}, ''), r.{c})" for c in ("customer_name", "phone", "address"))
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)" + _GROUPS_SQL
    + ", updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by "
    "RETURNING *"
)
# Claiming for a batch: only orders nobody has (new, or released) — or a claim of this computer
# whose batch no longer exists here (e.g. after the app's database was reset).
_CLAIM = (
    f"INSERT INTO {SCHEMA}.order_records AS r ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('%(' + c + ')s' for c in _COLS)}) ON CONFLICT (order_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _STATE_GROUP + _VIEW + ("name", "color"))
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)" + _GROUPS_SQL
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
    + ", app_state = CASE WHEN r.state = 'released' THEN COALESCE(r.app_state, EXCLUDED.app_state) ELSE r.app_state END"
    + ", review_reason = CASE WHEN r.app_state IS NULL THEN EXCLUDED.review_reason ELSE r.review_reason END"
    + ", source_status = CASE WHEN COALESCE(EXCLUDED.status_at, '') > COALESCE(r.status_at, '') "
      "THEN EXCLUDED.source_status ELSE r.source_status END"
    + ", status_at = GREATEST(r.status_at, EXCLUDED.status_at)" + _GROUPS_SQL
    + ", updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by "
    "RETURNING *"
)
PUSH_BATCH = 1000  # orders sent per round trip group; sync_now repeats until the queue is empty


class SyncError(RuntimeError):
    """The shared records couldn't be reached or refused the change."""


def enabled() -> bool:
    return bool(config.database_url())


def online() -> bool:
    """Connected and the last sync worked (the background sync checks every minute)."""
    return enabled() and STATUS.last_ok > 0 and not STATUS.error


def offline_reason() -> str:
    if not enabled():
        return "SnapSTL needs the shared order records (Supabase): connect them in Settings → Connections"
    if not STATUS.last_ok and not STATUS.error:
        return "Connecting to the shared order records (Supabase)…"
    return (f"No connection to the shared order records (Supabase): {STATUS.error or 'not reached yet'} — "
            "everything is disabled until it's back (Settings → Connections → Sync now)")


# ---------------- connection ----------------
_setup_done = ""  # connection string the tables were last checked on
_lock = threading.RLock()


# 0.26.1: never hang. A sync cut off midway (app restarted) can leave its transaction open in the pooler; the
# setup then waited for it forever ("Connecting to the shared order records…"). Now a transaction left idle
# ends by itself, and the setup gives up after LOCK_WAIT and tries again at the next sync.
IDLE_TX_TIMEOUT = "120s"
LOCK_WAIT = "20s"


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
            con.execute(f"SET idle_in_transaction_session_timeout = '{IDLE_TX_TIMEOUT}'")
            if _setup_done != url:
                con.execute(f"SET LOCAL lock_timeout = '{LOCK_WAIT}'")
                try:
                    con.execute("SELECT pg_advisory_xact_lock(83170002)")
                    con.execute(_SETUP_SQL)
                except psycopg.errors.LockNotAvailable as e:
                    raise SyncError("The shared records are busy (an earlier connection still holds a table) — "
                                    "trying again in a minute") from e
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
        "app_state": o.get("state"), "review_reason": o.get("review_reason"), "made_at": o.get("made_at"),
        "first_status": o.get("first_status"), "print_started_at": o.get("print_started_at") if own else None,
        **{c: o.get(c) for c in db.GROUP_COLS},
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


_VIEW_STATES = ("ready", "needs_review", "skipped")


def _view(L: dict, r: dict, status: str | None) -> dict:
    """Local changes for an order nobody makes, from the app state another computer last sent
    (`app_state`): Skip → skipped; Make anyway / Approve → ready or needs review (by its review reason
    and the webstore status). Empty when the record carries no such state (older SnapSTL, not needed)."""
    app = r.get("app_state")
    if app not in _VIEW_STATES:
        return {}
    if app == "skipped":
        return {"state": "skipped"}
    review = r.get("review_reason")
    return {"state": _evaluate({**L, "review_reason": review}, status), "review_reason": review}


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
    r = {**r, "status_at": db.upgrade_stamp(r.get("status_at"))}  # written by a SnapSTL before 0.22.21
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
            if r.get("app_state") in _VIEW_STATES:  # skipped / approved on another computer
                v = _view({"color": r.get("color")}, r, r.get("source_status"))
                state, review = v["state"], v.get("review_reason", r.get("review_reason"))
        local_batch = None
        if mine and r.get("batch_uid") in uids:
            row = con.execute("SELECT id FROM batches WHERE uid=?", (r["batch_uid"],)).fetchone()
            local_batch = row[0] if row else None
        con.execute(
            "INSERT INTO orders(order_id,clubbed_id,sku,order_date,source_status,quantity,raw_name,raw_color,"
            "special_request,name,color,review_reason,state,batch_id,first_file,last_file,first_seen,updated_at,"
            "customer_name,phone,address,state_at,status_at,in_remote,batch_uid,batch_label,batch_computer,"
            "batch_computer_id,printed_at,bundled_at,made_at,first_status,print_started_at,"
            f"{','.join(db.GROUP_COLS)}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?"
            f"{',?' * len(db.GROUP_COLS)})",
            (oid, r.get("clubbed_id"), r.get("sku") or "Magicians_35", r.get("order_date"), r.get("source_status"),
             r.get("quantity") or 1, r.get("raw_name"), r.get("raw_color"), r.get("special_request"), r.get("name"),
             r.get("color"), review, state, local_batch, "shared records", "shared records", db.now(), db.now(),
             r.get("customer_name"), r.get("phone"), r.get("address"), r["state_at"], r.get("status_at"),
             r.get("batch_uid") if state in ("in_batch", "bundled", "printed") else None, r.get("batch_label"),
             r.get("computer_name"), r.get("computer_id"), r.get("printed_at"), r.get("bundled_at"),
             r.get("made_at") if state in ("bundled", "printed") else None,
             r.get("first_status") or (r.get("source_status") or "").upper(),
             r.get("print_started_at") if state in ("in_batch", "bundled", "printed") else None,
             *(r.get(c) for c in db.GROUP_COLS)))
        return oid if stale else None

    sets: dict = {"in_remote": 1}
    # webstore status: the newer report wins
    if (r.get("status_at") or "") > (L.get("status_at") or ""):
        sets["source_status"], sets["status_at"] = r.get("source_status"), r.get("status_at")
        if L["state"] in ("ready", "needs_review", "not_needed"):
            from .ingest import status_state  # the same rules as an uploaded report (never backwards)

            sets["state"] = status_state(L["state"], L.get("source_status"), r.get("source_status"),
                                         _evaluate(L, "PRINT"), bool(L.get("batch_id")))
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
            status = sets.get("source_status", L.get("source_status"))
            making = L["state"] in ("in_batch", "bundled", "printed") and not (L["state"] == "in_batch" and L["batch_id"])
            if making:
                sets.update(state=_evaluate(L, status), batch_id=None, batch_uid=None, batch_label=None,
                            batch_computer=None, batch_computer_id=None, printed_at=None, made_at=None,
                            print_started_at=None, state_at=r["state_at"])
            if making or sets.get("state", L["state"]) in ("ready", "needs_review", "not_needed", "skipped"):
                # Skip / Make anyway / Approve pressed on another computer
                v = _view({**L, **sets}, r, status)
                if v and (v["state"] != L["state"] or v.get("review_reason", L["review_reason"]) != L["review_reason"]):
                    sets.update(v, state_at=r["state_at"])
        elif not (L["state"] == "in_batch" and L["batch_id"] and not mine):  # never pull out of a live local batch
            local_batch = L["batch_id"] if own_live else None
            if own_live and local_batch is None:
                row = con.execute("SELECT id FROM batches WHERE uid=?", (r["batch_uid"],)).fetchone()
                local_batch = row[0] if row else None
            sets.update(state=st, batch_id=local_batch, batch_uid=r.get("batch_uid"),
                        batch_label=r.get("batch_label"), batch_computer=r.get("computer_name"),
                        batch_computer_id=r.get("computer_id"), printed_at=r.get("printed_at"),
                        bundled_at=r.get("bundled_at"), state_at=r["state_at"])
    # Ready to ship (base and name printed) is worked out by the batch's computer; others mirror it.
    # It doesn't change state_at, so it's taken whenever both sides agree on the state.
    final = sets.get("state", L["state"])
    if ((force or not pending) and final in ("bundled", "printed") and r.get("state") == final
            and r.get("made_at") != L.get("made_at")):
        sets["made_at"] = r.get("made_at")
    # Printing started: also worked out by the batch's computer
    if ((force or not pending) and final in ("in_batch", "bundled", "printed") and r.get("state") == final
            and r.get("print_started_at") != L.get("print_started_at")):
        sets["print_started_at"] = r.get("print_started_at")
    if not L.get("first_status") and r.get("first_status"):
        sets["first_status"] = r["first_status"]
    # Things people set (Label Generated, Express, flags, …): the newest change of each group wins
    for g, cols in db.FIELD_GROUPS.items():
        if float(r.get(f"{g}_changed") or 0) > float(L.get(f"{g}_changed") or 0):
            sets.update({c: r.get(c) for c in cols}, **{f"{g}_changed": r.get(f"{g}_changed")})
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
TABS_SENT = "tabs_v1"  # sync_state key: every order sent once with app_state / review_reason / made_at
VIEW_SENT = "view_v1"  # sync_state key: skips sent again + one full pull, since merge applies app_state / made_at


def _push(rc) -> int:
    """Send queued changes and offer orders the shared records don't have yet. Returns how many."""
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM sync_state WHERE key=?", (TABS_SENT,)).fetchone():
            # since 0.22.15 the records carry the Orders page tab: send every order once more
            con.execute("INSERT OR IGNORE INTO sync_outbox(order_id, queued_at) SELECT order_id, ? FROM orders "
                        "WHERE COALESCE(in_remote,0)=1", (db.now(),))
            con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, '1')", (TABS_SENT,))
        if not con.execute("SELECT 1 FROM sync_state WHERE key=?", (VIEW_SENT,)).fetchone():
            # before 0.22.21 Skip / Make anyway / Approve and Ready to ship stayed on the computer they were
            # pressed on: send every order of this computer again (the rules keep newer changes made
            # elsewhere and bring them back here) and read every record once more
            con.execute("INSERT OR IGNORE INTO sync_outbox(order_id, queued_at) SELECT order_id, ? FROM orders "
                        "WHERE COALESCE(in_remote,0)=1", (db.now(),))
            con.execute("DELETE FROM sync_state WHERE key='pulled_until'")
            con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, '1')", (VIEW_SENT,))
        con.execute("INSERT OR IGNORE INTO sync_outbox(order_id, queued_at) SELECT order_id, ? FROM orders "
                    "WHERE COALESCE(in_remote,0)=0", (db.now(),))
        queued = db.rows(con, "SELECT o.*, q.queued_at FROM sync_outbox q JOIN orders o USING(order_id) "
                              f"ORDER BY q.queued_at LIMIT {PUSH_BATCH}")
        orphans = [r[0] for r in con.execute("SELECT order_id FROM sync_outbox WHERE order_id NOT IN "
                                             "(SELECT order_id FROM orders)")]
        if orphans:
            con.execute(f"DELETE FROM sync_outbox WHERE order_id IN ({','.join('?' * len(orphans))})", orphans)
    # a skip is a person's choice, not just a report's order: it goes by the normal rules (_UPSERT), so it
    # also reaches a record another computer offered first
    offers = [o for o in queued if not o.get("in_remote")
              and o["state"] not in ("in_batch", "bundled", "printed", "skipped")]
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


def _push_reports(rc) -> int:
    """Report rows and uploads not in the shared records yet (report.py)."""
    with db.tx() as con:
        rows = db.rows(con, f"SELECT * FROM report_rows WHERE COALESCE(sent,0)=0 LIMIT {PUSH_BATCH}")
        ups = db.rows(con, "SELECT * FROM files WHERE COALESCE(sent,0)=0")
    _many(rc, _REPORT_UPSERT, [_report.payload(r, computer.NAME) for r in rows])
    for f in ups:
        s = json.loads(f.get("summary") or "{}")
        rc.execute(f"INSERT INTO {SCHEMA}.report_uploads(uid, filename, sha256, computer_name, uploaded_at, as_of, "
                   "report_date, rows_total, magicians_rows, new_orders, updated_orders) VALUES "
                   "(%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s) ON CONFLICT (uid) DO NOTHING",
                   (f"{computer.ID}:{f['sha256']}:{int(f['ingested_at'] or 0)}", f["filename"], f["sha256"],
                    computer.NAME, f["ingested_at"] or 0, s.get("as_of"), s.get("report_date"), s.get("rows_total"),
                    s.get("sku_rows"), s.get("new"), s.get("updated")))
    with db.tx() as con:
        for r in rows:  # only if it didn't change again meanwhile
            con.execute("UPDATE report_rows SET sent=1 WHERE order_id=? AND status_at=? AND data=?",
                        (r["order_id"], r["status_at"], r["data"]))
        for f in ups:
            con.execute("UPDATE files SET sent=1 WHERE id=?", (f["id"],))
    return len(rows)


def _push_lines(rc) -> int:
    """Every product's uploaded report rows → shop.report_lines (shop.put_line tidies them in Supabase)."""
    total = 0
    for _ in range(20):
        with db.tx() as con:
            rows = db.rows(con, f"SELECT * FROM shop_lines WHERE COALESCE(sent,0)=0 LIMIT {PUSH_BATCH}")
        if not rows:
            break
        _many(rc, _shop.PUT_LINE, [{"order_id": r["order_id"], "data": r["data"], "status_at": r["status_at"] or "",
                                     "file": r["file"], "by": computer.NAME} for r in rows])
        with db.tx() as con:
            for r in rows:  # only if it didn't change again meanwhile
                con.execute("UPDATE shop_lines SET sent=1 WHERE order_id=? AND status_at=? AND data=?",
                            (r["order_id"], r["status_at"], r["data"]))
        total += len(rows)
        if len(rows) < PUSH_BATCH:
            break
    return total


def _push_notes(rc) -> int:
    """Call notes and usage counts of this computer not in the shared records yet."""
    with db.tx() as con:
        notes = db.rows(con, f"SELECT * FROM order_notes WHERE COALESCE(sent,0)=0 LIMIT {PUSH_BATCH}")
        usage = db.rows(con, "SELECT * FROM usage_counts WHERE COALESCE(sent,0)=0")
    _many(rc, f"INSERT INTO {SCHEMA}.order_notes(uid, order_id, at, text, author, computer) "
              "VALUES (%(uid)s, %(order_id)s, %(at)s, %(text)s, %(author)s, %(computer)s) "
              "ON CONFLICT (uid) DO NOTHING RETURNING uid",
          [{**n, "computer": computer.NAME} for n in notes])
    _many(rc, f"INSERT INTO {SCHEMA}.usage_counts(computer_id, computer_name, day, feature, count) "
              "VALUES (%(cid)s, %(cname)s, %(day)s, %(feature)s, %(count)s) ON CONFLICT (computer_id, day, feature) "
              "DO UPDATE SET count = GREATEST(EXCLUDED.count, usage_counts.count), computer_name = EXCLUDED.computer_name, "
              "updated_at = clock_timestamp() RETURNING day",
          [{**u, "cid": computer.ID, "cname": computer.NAME} for u in usage])
    with db.tx() as con:
        con.executemany("UPDATE order_notes SET sent=1 WHERE uid=?", [(n["uid"],) for n in notes])
        for u in usage:  # only if it wasn't used again meanwhile
            con.execute("UPDATE usage_counts SET sent=1 WHERE day=? AND feature=? AND count=?",
                        (u["day"], u["feature"], u["count"]))
    _push_checks(rc)
    return len(notes)


CHECK_COLS = ("uid", "batch_uid", "plate", "idx", "order_id", "part", "name", "color", "result", "letters", "staff",
              "note", "at", "printed_on", "printer", "fix_file", "computer")


def _push_checks(rc) -> None:
    """Staff names and print checks of this computer not in the shared records yet."""
    with db.tx() as con:
        staff = db.rows(con, "SELECT * FROM staff WHERE COALESCE(sent,0)=0")
        checks = db.rows(con, f"SELECT * FROM plate_checks WHERE COALESCE(sent,0)=0 LIMIT {PUSH_BATCH}")
    _many(rc, f"INSERT INTO {SCHEMA}.staff AS s(name, added_at, hidden, changed, pin_hash, pin_changed) VALUES "
              "(%(name)s, %(added_at)s, %(hidden)s, %(changed)s, %(pin_hash)s, %(pin_changed)s) ON CONFLICT (name) DO "
              "UPDATE SET hidden = CASE WHEN COALESCE(EXCLUDED.changed, 0) > COALESCE(s.changed, 0) THEN EXCLUDED.hidden "
              "ELSE s.hidden END, changed = GREATEST(s.changed, EXCLUDED.changed), "
              "pin_hash = CASE WHEN COALESCE(EXCLUDED.pin_changed, 0) > COALESCE(s.pin_changed, 0) THEN EXCLUDED.pin_hash "
              "ELSE s.pin_hash END, pin_changed = GREATEST(s.pin_changed, EXCLUDED.pin_changed), "
              "updated_at = clock_timestamp() RETURNING name",
          [{**x, "hidden": bool(x["hidden"]), "pin_hash": x.get("pin_hash"), "pin_changed": x.get("pin_changed")}
           for x in staff])
    _many(rc, f"INSERT INTO {SCHEMA}.print_checks({', '.join(CHECK_COLS)}) VALUES "
              f"({', '.join('%(' + c + ')s' for c in CHECK_COLS)}) ON CONFLICT (uid) DO NOTHING RETURNING uid",
          [{c: x.get(c) for c in CHECK_COLS} for x in checks])
    with db.tx() as con:
        for x in staff:  # only if it wasn't changed again meanwhile
            con.execute("UPDATE staff SET sent=1 WHERE name=? AND changed IS ? AND pin_changed IS ?",
                        (x["name"], x["changed"], x.get("pin_changed")))
        con.executemany("UPDATE plate_checks SET sent=1 WHERE uid=?", [(x["uid"],) for x in checks])


def _pull_checks(rc) -> None:
    staff = rc.execute(f"SELECT name, added_at, hidden, changed, pin_hash, pin_changed FROM {SCHEMA}.staff").fetchall()
    with db.tx() as con:
        for x in staff:
            con.execute("INSERT INTO staff(name, added_at, hidden, changed, sent) VALUES (?,?,?,?,1) ON CONFLICT(name) "
                        "DO UPDATE SET hidden=excluded.hidden, changed=excluded.changed, sent=1 "
                        "WHERE COALESCE(excluded.changed, 0) > COALESCE(staff.changed, 0)",
                        (x["name"], x["added_at"], int(bool(x["hidden"])), x["changed"]))
            con.execute("UPDATE staff SET pin_hash=?, pin_changed=? WHERE name=? "
                        "AND COALESCE(?, 0) > COALESCE(pin_changed, 0)",
                        (x["pin_hash"], x["pin_changed"], x["name"], x["pin_changed"]))
        r = con.execute("SELECT value FROM sync_state WHERE key='checks_pulled_until'").fetchone()
    q = f"SELECT *, stored_at::text AS stored_text FROM {SCHEMA}.print_checks"
    rows = (rc.execute(q + f" WHERE stored_at > %s::timestamptz - interval '{PULL_OVERLAP_S} seconds' "
                           "ORDER BY stored_at", (r[0],)) if r else rc.execute(q + " ORDER BY stored_at")).fetchall()
    with db.tx() as con:
        uid_id = {u: i for i, u in con.execute("SELECT id, uid FROM batches WHERE uid IS NOT NULL")}
        con.executemany(
            "INSERT OR IGNORE INTO plate_checks(uid, batch_uid, batch_id, plate, idx, order_id, part, name, color, result, "
            "letters, staff, note, at, printed_on, printer, fix_file, computer, sent) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            [(x["uid"], x["batch_uid"], uid_id.get(x["batch_uid"]), x["plate"], x["idx"], x["order_id"], x["part"],
              x["name"], x["color"], x["result"], x["letters"], x["staff"], x["note"], x["at"], x["printed_on"],
              x["printer"], x["fix_file"], x["computer"]) for x in rows])
        if rows:
            con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('checks_pulled_until', ?)",
                        (rows[-1]["stored_text"],))


def _pull_notes(rc) -> int:
    with db.tx() as con:
        r = con.execute("SELECT value FROM sync_state WHERE key='notes_pulled_until'").fetchone()
    q = f"SELECT *, stored_at::text AS stored_text FROM {SCHEMA}.order_notes"
    rows = (rc.execute(q + f" WHERE stored_at > %s::timestamptz - interval '{PULL_OVERLAP_S} seconds' "
                           "ORDER BY stored_at", (r[0],)) if r else rc.execute(q + " ORDER BY stored_at")).fetchall()
    with db.tx() as con:
        con.executemany("INSERT OR IGNORE INTO order_notes(uid, order_id, at, text, author, sent) VALUES (?,?,?,?,?,1)",
                        [(n["uid"], n["order_id"], n["at"], n["text"], n["author"]) for n in rows])
        if rows:
            con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('notes_pulled_until', ?)",
                        (rows[-1]["stored_text"],))
    return len(rows)


def usage(days: int = 30) -> list[dict]:
    """Settings → Usage: every computer's feature counts of the last `days` days, most used first."""
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with _remote() as rc:
        return rc.execute(f"SELECT feature, SUM(count)::int AS count, COUNT(DISTINCT day)::int AS days "
                          f"FROM {SCHEMA}.usage_counts WHERE day >= %s GROUP BY feature ORDER BY 2 DESC, 1",
                          (since,)).fetchall()


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
        self.note = ""  # e.g. this computer followed a reset of the shared records

    def snapshot(self) -> dict:
        with db.tx() as con:
            pending = con.execute("SELECT COUNT(*) FROM (SELECT order_id FROM sync_outbox UNION "
                                  "SELECT order_id FROM orders WHERE COALESCE(in_remote,0)=0)").fetchone()[0]
        return {"enabled": enabled(), "last_ok": self.last_ok, "last_try": self.last_try, "error": self.error,
                "pending": pending, "pushed": self.pushed, "pulled": self.pulled, "running": self.running,
                "computer": computer.NAME, "last_copy": last_copy(), "copy_error": self.copy_error,
                "note": self.note}


STATUS = Status()


def sync_now() -> dict:
    """Push this computer's changes, then pull everyone's. Safe to call any time (one at a time)."""
    if not enabled():
        return STATUS.snapshot()
    with _lock:
        STATUS.running = True
        STATUS.last_try = time.time()
        try:
            _follow_reset()  # the records were cleared on some computer: match them before sending anything
            with _remote() as rc:
                STATUS.pushed = n = _push(rc)
                for _ in range(20):  # a big report: keep sending until everything is there
                    if n < PUSH_BATCH:
                        break
                    n = _push(rc)
                    STATUS.pushed += n
                _push_batches(rc)
                _push_reports(rc)
                _push_lines(rc)
                _push_notes(rc)
            with _remote() as rc:  # pushes are committed before pulling
                STATUS.pulled = _pull(rc)
                _pull_notes(rc)
                _pull_checks(rc)
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
        return (con.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0]
                + con.execute("SELECT COUNT(*) FROM order_notes WHERE COALESCE(sent,0)=0").fetchone()[0])


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
    tables are created again, empty, right away, with a new `instance` id: at their next sync every
    computer — this one included — notices and makes itself match the empty records (`_follow_reset`),
    so nothing is sent back."""
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
                c.execute("DROP SCHEMA IF EXISTS shop CASCADE")  # made again empty with the rest
                c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        except psycopg.Error as e:
            raise SyncError(f"The shared records couldn't be cleared: {_short(e)}") from e
        _setup_done = ""
        with _remote():
            pass  # create the empty tables again (with a new instance id)
    return {"cleared": saved, "backup": str(dest)}


# ---------------- copies on this computer ----------------
def _save_copy(rc, dest) -> dict:
    """Every shared record, as JSON, in `dest` (readable by this user only: names, phones, addresses)."""
    orders = rc.execute(f"SELECT * FROM {SCHEMA}.order_records ORDER BY order_id").fetchall()
    batches = rc.execute(f"SELECT * FROM {SCHEMA}.batch_records ORDER BY uid").fetchall()
    reports = rc.execute(f"SELECT * FROM {SCHEMA}.report_rows ORDER BY order_id").fetchall()
    uploads = rc.execute(f"SELECT * FROM {SCHEMA}.report_uploads ORDER BY uid").fetchall()
    lines = rc.execute("SELECT * FROM shop.report_lines ORDER BY order_id").fetchall()
    notes = rc.execute(f"SELECT * FROM {SCHEMA}.order_notes ORDER BY stored_at").fetchall()
    usage_rows = rc.execute(f"SELECT * FROM {SCHEMA}.usage_counts ORDER BY day, feature").fetchall()
    staff = rc.execute(f"SELECT * FROM {SCHEMA}.staff ORDER BY name").fetchall()
    checks = rc.execute(f"SELECT * FROM {SCHEMA}.print_checks ORDER BY stored_at").fetchall()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"saved_at": time.time(), "by": computer.NAME, "order_records": orders,
                   "batch_records": batches, "report_rows": reports, "report_uploads": uploads,
                   "shop_report_lines": lines, "order_notes": notes, "usage_counts": usage_rows,
                   "staff": staff, "print_checks": checks},
                  f, default=str, indent=1)
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


# ---------------- clean up: this computer matches the shared records ----------------
def _apply_match(rows: list[dict], batch_uids: set[str], report_ids: set[str] | None = None,
                 line_ids: set[str] | None = None) -> dict:
    """Make this computer's database hold exactly what the shared records hold: every record's state is
    taken (merge(force=True)); orders the records don't have are removed, and so are this computer's
    batches they don't have (with their download, print and slice rows) — unless a shared order still
    belongs to it. Files on disk (batch folders, models) are never touched."""
    from .maintenance import _busy

    have = {r["order_id"] for r in rows}
    keep_uids = set(batch_uids) | {r["batch_uid"] for r in rows if r.get("batch_uid")}
    look = "SELECT order_id, state, source_status, name, color, batch_uid FROM orders"
    with db.tx() as con:
        if _busy(con):
            raise SyncError("A batch is being built right now — try again in a minute")
        before = {r[0]: tuple(r[1:]) for r in con.execute(look)}
        stale = []
        with db.quiet(con):
            for r in rows:
                x = merge(con, r, force=True)
                if x:
                    stale.append(x)
            gone = [oid for oid in before if oid not in have]
            for i in range(0, len(gone), 500):
                part = gone[i:i + 500]
                q = ",".join("?" * len(part))
                con.execute(f"DELETE FROM sync_outbox WHERE order_id IN ({q})", part)
                con.execute(f"DELETE FROM orders WHERE order_id IN ({q})", part)
            drop = [b[0] for b in con.execute("SELECT id, uid FROM batches") if b[1] not in keep_uids]
            for bid in drop:
                for t in ("downloads", "print_jobs", "slices"):
                    con.execute(f"DELETE FROM {t} WHERE batch_id=?", (bid,))
                con.execute("UPDATE orders SET batch_id=NULL WHERE batch_id=?", (bid,))
                con.execute("DELETE FROM batches WHERE id=?", (bid,))
            # report rows go with their orders; ones the records lack are sent again
            con.execute("DELETE FROM report_rows WHERE order_id NOT IN (SELECT order_id FROM orders)")
            if line_ids is not None:  # every-product report rows: only what shop.report_lines has
                for (oid,) in con.execute("SELECT order_id FROM shop_lines WHERE COALESCE(sent,0)=1").fetchall():
                    if oid not in line_ids:
                        con.execute("DELETE FROM shop_lines WHERE order_id=?", (oid,))
            if report_ids is not None:
                for (oid,) in con.execute("SELECT order_id FROM report_rows WHERE COALESCE(sent,0)=1").fetchall():
                    if oid not in report_ids:
                        con.execute("UPDATE report_rows SET sent=0 WHERE order_id=?", (oid,))
            # kept batches the records lack (e.g. cleared while its orders were sent again): send them again
            for (uid,) in con.execute("SELECT uid FROM batches").fetchall():
                if uid not in batch_uids:
                    con.execute("UPDATE batches SET synced=0 WHERE uid=?", (uid,))
            if rows:
                con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('pulled_until', ?)",
                            (max(r["updated_text"] for r in rows),))
            else:
                con.execute("DELETE FROM sync_state WHERE key='pulled_until'")
        _release_stale(con, stale)
        after = {r[0]: tuple(r[1:]) for r in con.execute(look)}
    return {"orders": len(after), "added": sum(1 for o in after if o not in before),
            "updated": sum(1 for o, v in after.items() if o in before and before[o] != v),
            "removed": len(gone), "batches_removed": len(drop)}


def _everything(rc) -> tuple[list[dict], set[str], set[str], set[str]]:
    rows = rc.execute(f"SELECT *, updated_at::text AS updated_text FROM {SCHEMA}.order_records").fetchall()
    uids = {r["uid"] for r in rc.execute(f"SELECT uid FROM {SCHEMA}.batch_records").fetchall()}
    reps = {r["order_id"] for r in rc.execute(f"SELECT order_id FROM {SCHEMA}.report_rows").fetchall()}
    lines = {r["order_id"] for r in rc.execute("SELECT order_id FROM shop.report_lines").fetchall()}
    return rows, uids, reps, lines


def _follow_reset() -> None:
    """At the start of every sync: if the shared records were created again since this computer last
    synced (Clear shared records / Reset with "also clear Supabase" on any computer), make this computer
    match them — a backup first — so nothing of the old records is sent back."""
    from .maintenance import backup

    with _remote() as rc:
        r = rc.execute(f"SELECT value FROM {SCHEMA}.meta WHERE key='instance'").fetchone()
        now = r["value"] if r else ""
        with db.tx() as con:
            seen = con.execute("SELECT value FROM sync_state WHERE key='instance'").fetchone()
        seen = seen[0] if seen else ""
        if not now or now == seen:
            return
        if seen:  # cleared since: match (first sync after updating just learns the id)
            rows, uids, reps, lines = _everything(rc)
            backup()
            res = _apply_match(rows, uids, reps, lines)
            STATUS.note = (f"The shared records were cleared on a computer: this one now matches them "
                           f"({res['removed']} order(s), {res['batches_removed']} batch(es) removed here; backup saved)")
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('instance', ?)", (now,))


def match() -> dict:
    """Settings → Database → Clean up: make this computer match the shared records exactly.

    Syncs first (this computer's changes are sent), then loads every shared record again and makes the
    local database hold exactly that — orders and their states; orders the records don't have are
    removed, and this computer's batches they don't have (with their download / print / slice rows).
    Old upload records and history (30 days) are tidied. A backup is saved first; nothing is deleted in
    the shared records and no file on disk is touched."""
    from . import maintenance as M

    st = sync_now()
    if st["error"]:
        raise SyncError(st["error"])
    path = M.backup()
    with _lock:
        with _remote() as rc:
            rows, uids, reps, lines = _everything(rc)
        res = _apply_match(rows, uids, reps, lines)
    tidy = M.cleanup(30, orders=False, take_backup=False)
    sync_now()  # kept batches the records lacked go back up
    return res | {"backup": str(path), "tidied": {k: v for k, v in tidy.items() if k != "backup"}}
