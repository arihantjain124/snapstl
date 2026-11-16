"""Shared order records (Supabase/Postgres) between two simulated computers.

Runs only with a throwaway Postgres: SNAPSTL_TEST_DATABASE_URL=postgresql://… (README → Development).
Each "computer" is its own local SQLite database + computer id; both sync with the same records.
"""
import csv
import io
import os
from contextlib import contextmanager

import pytest

from snapstl import computer, config, db, ingest, maintenance, sync
from snapstl.settings import Settings

pytestmark = pytest.mark.skipif(not os.environ.get("SNAPSTL_TEST_DATABASE_URL"),
                                reason="needs SNAPSTL_TEST_DATABASE_URL (a throwaway Postgres)")


def report(rows, range_end="14/11/2026") -> bytes:
    """rows: (order_id, status, name, delivered_date)"""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow([f"Date Range: 30/10/2026 to {range_end}"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Delivered Date", "Product Customisation"])
    for oid, status, name, delivered in rows:
        w.writerow([oid, "", "01-11-2026", status, "Magicians_35", 1, "Private Person", delivered,
                    f"Enter Your First Name: {name}\nColor: Pink\n"])
    return f.getvalue().encode()


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Two computers: use `with pc("A"):` / `with pc("B"):` to act as one of them."""
    import psycopg

    with psycopg.connect(os.environ["SNAPSTL_TEST_DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS snapstl CASCADE")
    monkeypatch.setattr(sync, "_setup_done", "")
    monkeypatch.setenv("SNAPSTL_DATABASE_URL", os.environ["SNAPSTL_TEST_DATABASE_URL"])

    @contextmanager
    def pc(name):
        old = (db.DB_PATH, computer.ID, computer.NAME)
        db.DB_PATH, computer.ID, computer.NAME = tmp_path / f"{name}.db", f"id-{name}", f"PC {name}"
        try:
            yield
        finally:
            db.DB_PATH, computer.ID, computer.NAME = old

    return pc


def order(oid):
    with db.tx() as con:
        r = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
        return dict(r) if r else None


def remote(oid):
    with sync._remote() as rc:
        return rc.execute("SELECT * FROM snapstl.order_records WHERE order_id=%s", (oid,)).fetchone()


def ok(s):
    assert s["error"] == "", s["error"]
    return s


def test_two_computers_share_order_records(two):
    pc = two
    data = report([("O1", "PRINT", "Asha", ""), ("O2", "PRINT", "Ravi", ""), ("O3", "PRINT", "Neha", "")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
    st = Settings()

    # A batches O1 + O2: claimed in the shared records
    from snapstl import batches as B

    with pc("A"):
        a = B.create_batch(["O1", "O2"], st)
        assert a["orders"] == 2
        assert remote("O1")["state"] == "in_batch" and remote("O1")["computer_name"] == "PC A"

    # B wants O2 + O3: only O3 is its; O2 leaves Ready on B and shows A's batch
    with pc("B"):
        b = B.create_batch(["O2", "O3"], st)
        assert b["orders"] == 1 and b["taken_elsewhere"] == 1
        o2 = order("O2")
        assert o2["state"] == "in_batch" and o2["batch_id"] is None and o2["batch_computer"] == "PC A"
        with pytest.raises(ValueError, match="another computer"):  # B didn't know yet: refused anyway
            B.create_batch(["O1"], st)
        assert order("O1")["state"] == "in_batch" and order("O1")["batch_computer"] == "PC A"

    # A builds: O1 bundled, O2 left out (back to Ready) → after syncing, B sees both
    with pc("A"):
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1 - 2026-11-16"}, ["O1"])
        ok(sync.sync_now())
        assert remote("O1")["state"] == "bundled" and remote("O1")["batch_label"] == "Batch 1 - 2026-11-16"
        assert remote("O2")["state"] == "released"
    with pc("B"):
        ok(sync.sync_now())
        assert order("O1")["state"] == "bundled" and order("O1")["batch_computer"] == "PC A"
        assert order("O2")["state"] == "ready"
        # a person on B confirms O1 was printed
        assert ingest.set_printed(["O1"]) == 1
        ok(sync.sync_now())
    with pc("A"):
        ok(sync.sync_now())
        o1 = order("O1")
        assert o1["state"] == "printed" and o1["batch_id"] == a["batch_id"]  # still in its batch, for the record

    # newer webstore status from B's report reaches A; an older report on A doesn't undo it
    with pc("B"):
        ingest.ingest(report([("O1", "DELIVERED", "Asha", "15/11/2026")], range_end="15/11/2026"), "new.csv")
        ok(sync.sync_now())
    with pc("A"):
        ok(sync.sync_now())
        assert order("O1")["source_status"] == "DELIVERED" and order("O1")["state"] == "printed"
        s = ingest.ingest(report([("O1", "PRINT", "Asha", "")], range_end="10/11/2026"), "old.csv")
        assert s.kept_newer_status == 1 and order("O1")["source_status"] == "DELIVERED"
        ok(sync.sync_now())
        assert remote("O1")["source_status"] == "DELIVERED"

    # the records can't be deleted
    with pc("A"), pytest.raises(sync.SyncError, match="never deleted"):
        with sync._remote() as rc:
            rc.execute("DELETE FROM snapstl.order_records WHERE order_id='O1'")

    # resetting A's app clears only A; Sync brings the records back (and A's lost claim is released)
    with pc("A"):
        B.create_batch(["O2"], st)  # claimed by A …
        assert remote("O2")["state"] == "in_batch"
        maintenance.reset()  # … then A's database is reset before the batch is made
        assert order("O1") is None and remote("O1")["state"] == "printed"
        ok(sync.sync_now())
        assert order("O1")["state"] == "printed" and order("O1")["batch_label"] == "Batch 1 - 2026-11-16"
        assert order("O3")["state"] == "in_batch" and order("O3")["batch_computer"] == "PC B"
        assert order("O2")["state"] == "ready" and remote("O2")["state"] == "released"


def test_no_batch_without_the_shared_records(two, monkeypatch):
    from snapstl import batches as B

    with two("A"):
        ingest.ingest(report([("Q1", "PRINT", "Tara", "")]), "r.csv")
        monkeypatch.setattr(config, "database_url", lambda: "postgresql://nobody@127.0.0.1:1/none")
        with pytest.raises(ValueError, match="no batch was created"):
            B.create_batch(["Q1"], Settings())
        assert order("Q1")["state"] == "ready"
        with db.tx() as con:
            assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        s = sync.sync_now()
        assert "Can't reach" in s["error"]


def test_reset_waits_for_unsynced_changes(two, monkeypatch):
    with two("A"):
        ingest.ingest(report([("R1", "PRINT", "Mira", "")]), "r.csv")
        with db.tx() as con:  # a bundled order whose change hasn't been sent yet
            con.execute("UPDATE orders SET state='bundled' WHERE order_id='R1'")
        monkeypatch.setattr(config, "database_url", lambda: "postgresql://nobody@127.0.0.1:1/none")
        with pytest.raises(RuntimeError, match="haven't reached the shared records"):
            maintenance.reset()
        assert order("R1")["state"] == "bundled"


def test_clear_shared_records(two, tmp_path, monkeypatch):
    import json

    pc = two
    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "backups")
    from snapstl import batches as B

    with pc("A"):
        ingest.ingest(report([("C1", "PRINT", "Asha", ""), ("C2", "PRINT", "Ravi", "")]), "r.csv")
        B.create_batch(["C1"], Settings())
        ok(sync.sync_now())
        assert sync.summary()["orders"] == {"in_batch": 1, "released": 1}  # C2 is shared too, nobody makes it yet
        with pytest.raises(sync.SyncError, match="CLEAR"):
            sync.clear("yes")
        r = sync.clear("CLEAR")
        assert r["cleared"] == {"orders": 2, "batches": 1}
        saved = json.loads(open(r["backup"]).read())
        assert [o["order_id"] for o in saved["order_records"]] == ["C1", "C2"]  # nothing lost: kept on this computer
        assert sync.summary() == {"orders": {}, "batches": {}}  # empty, tables there again
        assert order("C1")["state"] == "in_batch"  # this computer still makes it …
        ok(sync.sync_now())
        assert remote("C1")["state"] == "in_batch"  # … so it's sent again
        assert remote("C2")["state"] == "released"  # and its report's orders are offered again


def test_old_batch_orders_get_their_owner(two):
    """A batch made before its orders recorded their batch (uid / computer) was sent without an owner;
    then the owner's own Bundled was refused and pulled the orders out of the batch. Opening the
    database fills the owner in and sends it again; an ownerless in_batch record takes it."""
    import sqlite3

    from snapstl import batches as B

    with two("A"):
        ingest.ingest(report([("L1", "PRINT", "Lata", ""), ("L2", "PRINT", "Mohan", "")]), "r.csv")
        a = B.create_batch(["L1", "L2"], Settings())
        ok(sync.sync_now())
        # as an older SnapSTL left it: the orders don't name their batch, nor do their records
        raw = sqlite3.connect(db.DB_PATH)
        raw.execute("UPDATE orders SET batch_uid=NULL, batch_computer=NULL, batch_computer_id=NULL")
        raw.commit()
        raw.close()
        with sync._remote() as rc:
            rc.execute("UPDATE snapstl.order_records SET batch_uid=NULL, computer_id=NULL, computer_name=NULL")

        assert order("L1")["batch_computer_id"] == computer.ID  # filled in from its batch on opening
        ok(sync.sync_now())
        assert remote("L1")["computer_id"] == computer.ID and remote("L1")["computer_name"] == "PC A"
        assert remote("L1")["batch_uid"] == order("L1")["batch_uid"]

        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1 - 2026-11-16"}, ["L1", "L2"])
        ok(sync.sync_now())
        assert order("L1")["state"] == "bundled" and order("L1")["batch_id"] == a["batch_id"]
        assert remote("L1")["state"] == "bundled" and remote("L2")["state"] == "bundled"


def test_only_snapstl_reaches_the_records(two):
    """Row level security on, and Supabase's API roles get no access even if someone granted it."""
    import psycopg

    url = os.environ["SNAPSTL_TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as c:
        for role in ("anon", "authenticated"):
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                c.execute(f"CREATE ROLE {role} NOLOGIN")
    with two("A"):
        sync.check()  # creates the tables
        with psycopg.connect(url, autocommit=True) as c:  # e.g. the schema exposed in the Data API settings
            c.execute("GRANT USAGE ON SCHEMA snapstl TO anon, authenticated")
            c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA snapstl TO anon, authenticated")
        sync._setup_done = ""
        sync.check()  # every SnapSTL start runs the setup again
        with psycopg.connect(url, autocommit=True) as c:
            rls = dict(c.execute("SELECT relname, relrowsecurity FROM pg_class WHERE relnamespace = "
                                 "'snapstl'::regnamespace AND relkind = 'r'").fetchall())
            assert rls == {"order_records": True, "batch_records": True, "meta": True}
            c.execute("SET ROLE anon")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT * FROM snapstl.order_records")
        ok(sync.sync_now())  # SnapSTL itself (the owner) still reads and writes


def test_daily_copy_of_the_shared_records(two):
    from snapstl import batches as B
    from snapstl.maintenance import BACKUP_DIR

    with two("A"):
        ingest.ingest(report([("D1", "PRINT", "Devi", "")]), "r.csv")
        B.create_batch(["D1"], Settings())
        ok(sync.sync_now())
        for old in BACKUP_DIR.glob("shared-records-*.json"):
            old.unlink()
        for day in range(1, 32):  # older daily copies: only the newest KEEP_DAILY stay
            (BACKUP_DIR / f"shared-records-daily-2026-01-{day:02d}.json").write_text("{}")

        sync.daily_copy()
        today = sync._daily_path()
        assert today.exists() and (today.stat().st_mode & 0o777) == 0o600
        import json

        saved = json.loads(today.read_text())
        assert [o["order_id"] for o in saved["order_records"]] == ["D1"] and len(saved["batch_records"]) == 1
        daily = sorted(BACKUP_DIR.glob("shared-records-daily-*.json"))
        assert len(daily) == sync.KEEP_DAILY and daily[-1] == today
        first = today.stat().st_mtime_ns
        sync.daily_copy()  # once a day
        assert today.stat().st_mtime_ns == first
        assert sync.STATUS.snapshot()["last_copy"]["file"] == today.name

        r = sync.export()  # `snapstl-db export`: a kept, timestamped copy
        assert r["orders"] == 1 and "daily" not in r["file"]


def test_uploaded_orders_reach_every_computer(two):
    """A report uploaded on one computer is in the shared records; the others get its orders with the
    same review rules, and name fixes; an offer never undoes what another computer is making."""
    from snapstl import batches as B

    pc = two
    data = report([("U1", "PRINT", "Kabir", ""), ("U2", "PRINT", "Anu Sharma", ""), ("U3", "DELIVERED", "Ira", "12/11/2026"),
                   ("U4", "PRINT", "Zoya", "")])
    with pc("A"):
        ingest.ingest(data, "r.csv")
        s = ok(sync.sync_now())
        assert s["pending"] == 0
        assert remote("U1")["state"] == "released" and remote("U1")["customer_name"] == "Private Person"
        a = B.create_batch(["U4"], Settings())
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1 - 2026-11-16"}, ["U4"])
        ok(sync.sync_now())
    with pc("B"):  # never uploaded the report
        ok(sync.sync_now())
        assert order("U1")["state"] == "ready" and order("U1")["name"] == "Kabir"
        assert order("U2")["state"] == "needs_review" and "space" in order("U2")["review_reason"]
        assert order("U3")["state"] == "not_needed"
        assert order("U4")["state"] == "bundled" and order("U4")["batch_computer"] == "PC A"
    with pc("A"):  # a person fixes the name on A …
        ingest.set_name("U2", "Anu")
        ok(sync.sync_now())
    with pc("B"):  # … B gets it
        ok(sync.sync_now())
        assert order("U2")["name"] == "Anu" and order("U2")["state"] == "ready"

    with pc("C"):  # uploads the same report without having synced: its offers don't undo A's work
        ingest.ingest(data, "r.csv")
        assert order("U4")["state"] == "ready"
        ok(sync.sync_now())
        assert remote("U4")["state"] == "bundled"
        assert order("U4")["state"] == "bundled" and order("U4")["batch_computer"] == "PC A"
        assert order("U2")["name"] == "Anu"


def test_reset_also_clears_the_shared_records(two, tmp_path, monkeypatch):
    """Settings → Maintenance → Reset with "also clear Supabase": both emptied, copies saved first,
    and nothing of this computer is sent back afterwards."""
    import json

    from snapstl import batches as B

    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "backups")
    with two("A"):
        ingest.ingest(report([("Z1", "PRINT", "Esha", ""), ("Z2", "PRINT", "Om", "")]), "r.csv")
        B.create_batch(["Z1"], Settings())
        ok(sync.sync_now())
        with db.tx() as con:  # a change not sent yet doesn't block it: everything is being emptied
            con.execute("UPDATE orders SET state='bundled' WHERE order_id='Z1'")
        r = maintenance.reset(shared=True)
        assert r["cleared"] == {"orders": 2, "batches": 1}
        saved = json.loads(open(r["shared_backup"]).read())
        assert sorted(o["order_id"] for o in saved["order_records"]) == ["Z1", "Z2"]
        assert os.path.exists(r["backup"])
        assert order("Z1") is None and order("Z2") is None
        ok(sync.sync_now())
        assert sync.summary() == {"orders": {}, "batches": {}}  # nothing came back
