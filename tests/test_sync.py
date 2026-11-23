"""Shared order records (Supabase/Postgres) between two simulated computers.

Runs only with a throwaway Postgres: SNAPSTL_TEST_DATABASE_URL=postgresql://… (README → Development).
Each "computer" is its own local SQLite database + computer id; both sync with the same records.
"""
import csv
import io
import os
import time
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
        c.execute("DROP SCHEMA IF EXISTS shop CASCADE")
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
        ok(sync.sync_now())  # this computer follows the reset: matches the empty records, sends nothing back
        assert order("C1") is None and order("C2") is None
        with db.tx() as con:
            assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        assert sync.summary() == {"orders": {}, "batches": {}}


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
            assert rls == {"order_records": True, "batch_records": True, "meta": True, "report_rows": True,
                           "report_uploads": True, "order_notes": True, "usage_counts": True, "staff": True,
                           "print_checks": True, "spares": True}
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
        assert today.exists() and (os.name == "nt" or (today.stat().st_mode & 0o777) == 0o600)
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


def test_clean_up_makes_this_computer_match_the_shared_records(two, tmp_path, monkeypatch):
    from snapstl import batches as B

    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "backups")
    data = report([("M1", "PRINT", "Ana", ""), ("M2", "PRINT", "Ben", ""), ("M3", "PRINT", "Cyd", "")])
    with two("A"):
        ingest.ingest(data, "r.csv")
        ok(sync.sync_now())
    with two("B"):
        ingest.ingest(data, "r.csv")
        b = B.create_batch(["M3"], Settings())  # B makes M3
        ok(sync.sync_now())
    with two("A"):
        a = B.create_batch(["M1"], Settings())
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1 - 2026-11-16"}, ["M1"])
        ok(sync.sync_now())
    with two("B"):
        with db.tx() as con, db.quiet(con):  # B drifted (e.g. a wrong clock): a normal sync keeps its "newer" state
            con.execute("UPDATE orders SET state='ready', batch_id=NULL, state_at=9e9 WHERE order_id='M1'")
        ok(sync.sync_now())
        assert order("M1")["state"] == "ready"
        r = sync.match()
        assert r["orders"] == 3 and r["updated"] >= 1 and r["removed"] == 0 and os.path.exists(r["backup"])
        assert order("M1")["state"] == "bundled" and order("M1")["batch_computer"] == "PC A"
        with db.tx() as con:  # a download row of B's batch (it goes with the batch)
            con.execute("INSERT INTO downloads(cache_key, name, compartments, model_json, state, batch_id, "
                        "created_at, updated_at) VALUES ('k', 'Cyd', 3, '{}', 'done', ?, 0, 0)", (b["batch_id"],))
    with two("A"):  # A resets everything, Supabase included
        maintenance.reset(shared=True)
    with two("B"):  # B follows at its next sync: matches the empty records, nothing comes back
        s = ok(sync.sync_now())
        assert "cleared on a computer" in s["note"]
        assert order("M1") is None and order("M2") is None and order("M3") is None
        with db.tx() as con:
            assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
        assert sync.summary() == {"orders": {}, "batches": {}}
        r = sync.match()  # Clean up afterwards: nothing left to change
        assert r["removed"] == 0 and r["batches_removed"] == 0 and r["orders"] == 0


def test_clean_up_sends_back_a_batch_its_shared_orders_still_name(two, tmp_path, monkeypatch):
    """The records hold a batch's orders but not the batch itself (its 'already sent' mark outlived a
    clear): Clean up keeps the batch and sends it again."""
    import psycopg

    from snapstl import batches as B

    monkeypatch.setattr(maintenance, "BACKUP_DIR", tmp_path / "backups")
    with two("A"):
        ingest.ingest(report([("K1", "PRINT", "Kia", "")]), "r.csv")
        a = B.create_batch(["K1"], Settings())
        ok(sync.sync_now())
        with psycopg.connect(os.environ["SNAPSTL_TEST_DATABASE_URL"], autocommit=True) as c:  # test database only
            c.execute("ALTER TABLE snapstl.batch_records DISABLE TRIGGER USER")
            c.execute("DELETE FROM snapstl.batch_records")
            c.execute("ALTER TABLE snapstl.batch_records ENABLE TRIGGER USER")
        r = sync.match()
        assert r["batches_removed"] == 0 and order("K1")["batch_id"] == a["batch_id"]
        assert sync.summary()["batches"] == {"PC A": 1}  # sent again


def test_shared_records_carry_the_orders_page_tab(two):
    """Every record says which Orders page tab (and Ready sub-tab) it's in, for anyone reading Supabase."""
    from snapstl import batches as B

    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Date Range: 30/10/2026 to 14/11/2026"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Delivered Date", "Product Customisation"])
    for oid, club, status, name in [("T1", "", "PRINT", "Asha"), ("T2", "C9", "PRINT", "Ravi"),
                                    ("T3", "C9", "PRINT", "Neha"), ("T4", "", "PRINT", "Anu Sharma"),
                                    ("T5", "", "DELIVERED", "Ira"), ("T6", "", "RTO_INITIATED", "Om")]:
        w.writerow([oid, club, "01-11-2026", status, "Magicians_35", 1, "Private Person", "",
                    f"Enter Your First Name: {name}\nColor: Pink\n"])
    tab = lambda oid: (lambda r: (r["tab"], r["ready_type"]))(remote(oid))  # noqa: E731
    with two("A"):
        ingest.ingest(f.getvalue().encode(), "r.csv")
        ad = ingest.add_adhoc("Kiara", "98765 43210", "pink")
        ok(sync.sync_now())
        assert tab("T1") == ("to_make", "single")
        assert tab("T2") == ("to_make", "clubbed") and tab("T3") == ("to_make", "clubbed")
        assert tab("T4") == ("check_name", None) and "space" in remote("T4")["review_reason"]
        assert tab("T5") == ("delivered", None) and tab("T6") == ("rto", None)
        assert tab(ad["order_id"]) == ("to_make", "adhoc")
        assert remote("T1")["app_state"] == "ready"

        ingest.set_name("T4", "Anu")  # reviewed → Ready
        a = B.create_batch(["T1"], Settings())
        ok(sync.sync_now())
        assert tab("T4") == ("to_make", "single") and tab("T1") == ("ready_to_print", None)
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1 - 2026-11-16"}, ["T1"])
        ok(sync.sync_now())
        assert tab("T1") == ("ready_to_print", None)
        with db.tx() as con:  # base and name both printed (Print) → Ready to ship
            con.execute("UPDATE orders SET made_at=? WHERE order_id='T1'", (db.now(),))
        ok(sync.sync_now())
        assert tab("T1") == ("pack_it", None)
        ingest.set_printed(["T1"])
        ok(sync.sync_now())
        assert tab("T1") == ("pack_it", None)

        # a row written by an older SnapSTL (no app_state) still gets a tab from its status
        with sync._remote() as rc:
            rc.execute("UPDATE snapstl.order_records SET app_state=NULL, review_reason=NULL WHERE order_id IN ('T2','T5')")
        assert tab("T2") == ("to_make", "clubbed") and tab("T5") == ("delivered", None)
        # first sync after updating sends every order once, so the records get the app's own state
        with db.tx() as con:
            con.execute("DELETE FROM sync_state WHERE key=?", (sync.TABS_SENT,))
        ok(sync.sync_now())
        assert remote("T2")["app_state"] == "ready" and remote("T5")["app_state"] == "not_needed"
    with two("B"):  # another computer reads the same tabs
        ok(sync.sync_now())
        assert order("T4")["name"] == "Anu" and tab("T4") == ("to_make", "single")


def test_report_columns_reach_the_shared_records(two):
    """Every other report column of a Magicians_35 order is kept, typed, in snapstl.report_rows (and
    orders_full); a newer file wins column by column, an older one or a blank never erases; uploads listed."""
    def rep(rows, end):
        f = io.StringIO()
        w = csv.writer(f)
        w.writerow(["Order Reconciliation Report"])
        w.writerow([f"Date Range: 30/10/2026 to {end}"])
        w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                    "Customer Name", "City", "Payment Method", "Invoice Total (Incl. Tax)", "AWB", "Shipping Partner",
                    "Delivered Date", "Online Transaction ID", "Product Customisation"])
        for oid, sku, status, city, pay, total, awb, delivered in rows:
            w.writerow([oid, "", "01-11-2026", status, sku, 1, "Private Person", city, pay, total, awb, "Delhivery",
                        delivered, "TXN-SECRET", "Enter Your First Name: Asha\nColor: Pink\n"])
        return f.getvalue().encode()

    with two("A"):
        ingest.ingest(rep([("R1", "Magicians_35", "PRINT", "Pune", "COD", "₹1,299.00", "AWB1", ""),
                           ("R9", "Other_SKU", "PRINT", "Goa", "COD", "500", "AWB9", "")], "14/11/2026"), "old.csv")
        ok(sync.sync_now())
        with sync._remote() as rc:
            r = rc.execute("SELECT * FROM snapstl.report_rows WHERE order_id='R1'").fetchone()
            assert r["city"] == "Pune" and r["payment_method"] == "COD" and float(r["invoice_total_incl_tax"]) == 1299.0
            assert r["awb"] == "AWB1" and "TXN-SECRET" not in str(r)  # transaction IDs aren't kept
            assert rc.execute("SELECT 1 FROM snapstl.report_rows WHERE order_id='R9'").fetchone() is None  # Magicians_35 only
            f = rc.execute("SELECT * FROM snapstl.orders_full WHERE order_id='R1'").fetchone()
            assert f["tab"] == "to_make" and f["city"] == "Pune"
        # a newer file: delivered, AWB blank (kept), city changed
        ingest.ingest(rep([("R1", "Magicians_35", "DELIVERED", "Mumbai", "COD", "1299", "", "16/11/2026")],
                          "16/11/2026"), "new.csv")
        # an older file afterwards changes nothing
        ingest.ingest(rep([("R1", "Magicians_35", "PRINT", "Nagpur", "Prepaid", "999", "AWB0", "")], "10/11/2026"),
                      "older.csv")
        ok(sync.sync_now())
        with sync._remote() as rc:
            r = rc.execute("SELECT * FROM snapstl.report_rows WHERE order_id='R1'").fetchone()
            assert r["city"] == "Mumbai" and r["awb"] == "AWB1" and r["payment_method"] == "COD"
            assert str(r["delivered_date"]).startswith("2026-11-16") and r["report_file"] == "new.csv"
            ups = rc.execute("SELECT filename, magicians_rows, computer_name FROM snapstl.report_uploads "
                             "ORDER BY uploaded_at").fetchall()
            assert [u["filename"] for u in ups] == ["old.csv", "new.csv", "older.csv"] and ups[0]["computer_name"] == "PC A"
            # listed to make, never made, now delivered: delivered (0.26: made, wherever — no flag)
            full = rc.execute("SELECT tab, flag FROM snapstl.order_records WHERE order_id='R1'").fetchone()
            assert (full["tab"], full["flag"]) == ("delivered", None)
        with pytest.raises(sync.SyncError, match="never deleted"):
            with sync._remote() as rc:
                rc.execute("DELETE FROM snapstl.report_rows")


def _full_report(rows, end="14/11/2026"):
    """rows: dicts of report columns (any product)."""
    cols = ["Order ID", "Clubbed Order ID", "Order Creation Date", "Channel", "Order Status", "Product Code", "SKU Code",
            "Quantity", "Customer Name", "Mobile", "Address", "Pincode", "City", "State", "Payment Method",
            "Invoice Total (Incl. Tax)", "COD Remittance Status", "COD Payable Amount", "Shipping Partner", "AWB",
            "Dimensions (cm)", "Delivered Date", "Online Transaction ID", "Product Customisation"]
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow([f"Date Range: 30/10/2026 to {end}"])
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return f.getvalue().encode()


def test_shop_schema_holds_the_whole_report(two):
    """Every product's row reaches shop.* (tidied in the database); the app still shows only Magicians_35;
    shop.production matches the app."""
    base = {"Channel": "WEB", "Quantity": "1", "Customer Name": "Private Person", "Address": "1 Road", "City": "Pune",
            "State": "Maharashtra", "Pincode": "411001", "Online Transaction ID": "TXN-SECRET"}
    rows = [
        base | {"Order ID": "W1", "Order Creation Date": "01-11-2026", "Order Status": "PRINT", "Product Code": "P35",
                "SKU Code": "Magicians_35", "Mobile": "+91 98765 43210", "Payment Method": "COD",
                "Invoice Total (Incl. Tax)": "₹1,299", "Product Customisation": "Enter Your First Name: Anu Sharma\nColor: Pink"},
        base | {"Order ID": "W2", "Order Creation Date": "02-11-2026", "Order Status": "DELIVERED", "Product Code": "P77",
                "SKU Code": "Magicians_77", "Mobile": "9876543210", "Payment Method": "PARTIAL-COD",
                "Invoice Total (Incl. Tax)": "499", "COD Remittance Status": "PENDING", "COD Payable Amount": "300",
                "Shipping Partner": "Xpressbees Surface SURFACE", "AWB": "AWB2", "Dimensions (cm)": "8.48 x 8.48 x 8.48",
                "Delivered Date": "05/11/2026"},
        base | {"Order ID": "W3", "Order Creation Date": "02-11-2026", "Order Status": "RTO_INITIATED", "Product Code": "S6",
                "SKU Code": "Stickers_006", "Mobile": "9123456789", "Payment Method": "ONLINE",
                "Shipping Partner": "Blue Dart AIR"},
    ]
    with two("A"):
        s = ingest.ingest(_full_report(rows), "full.csv")
        assert s.sku_rows == 1 and order("W2") is None  # the app keeps Magicians_35 only
        ok(sync.sync_now())
        with sync._remote() as rc:
            q = lambda sql, *a: rc.execute(sql, a).fetchall()  # noqa: E731
            o = {r["order_id"]: r for r in q("SELECT * FROM shop.orders")}
            assert set(o) == {"W1", "W2", "W3"}
            assert o["W2"]["status"] == "delivered" and o["W3"]["status_group"] == "rto" and o["W1"]["status"] == "print"
            assert o["W2"]["payment_method"] == "partial_cod" and o["W2"]["is_cod"] and not o["W3"]["is_cod"]
            assert str(o["W1"]["order_date"]) == "2026-11-01" and o["W1"]["colour"] == "pink"
            assert o["W1"]["customisation"]["enter your first name"] == "Anu Sharma"
            assert o["W1"]["customer_id"] == o["W2"]["customer_id"]  # same mobile, written two ways
            c = q("SELECT * FROM shop.customers WHERE customer_id=%s", o["W1"]["customer_id"])[0]
            assert c["mobile"] == "9876543210" and c["state"] == "Maharashtra" and str(c["first_order_date"]) == "2026-11-01"
            f = {r["order_id"]: r for r in q("SELECT * FROM shop.order_finance")}
            assert float(f["W1"]["invoice_total"]) == 1299 and f["W2"]["cod_remittance_status"] == "pending"
            sh = {r["order_id"]: r for r in q("SELECT * FROM shop.shipments")}
            assert (sh["W2"]["courier"], sh["W2"]["mode"]) == ("Xpressbees", "surface") and sh["W3"]["mode"] == "air"
            assert float(sh["W2"]["length_cm"]) == 8.48 and sh["W2"]["days_to_deliver"] == 3
            assert {r["sku"]: r["made_by_snapstl"] for r in q("SELECT * FROM shop.products")} == {
                "Magicians_35": True, "Magicians_77": False, "Stickers_006": False}
            assert "TXN-SECRET" not in str(q("SELECT data FROM shop.report_lines"))
            p = q("SELECT * FROM shop.production WHERE order_id='W1'")[0]  # what the app shows
            assert p["tab"] == "check_name" and order("W1")["state"] == "needs_review" and "space" in p["review_reason"]
            assert q("SELECT orders FROM shop.production_queue WHERE tab='check_name'")[0]["orders"] == 1
            assert q("SELECT * FROM shop.cod_outstanding")[0]["order_id"] == "W2"
            assert q("SELECT term FROM shop.glossary WHERE term='partial_cod'")
            assert q("SELECT shop.status_group('handover') AS g")[0]["g"] == "in_transit"
            assert q("SELECT term FROM shop.glossary WHERE term='handover'")
        # a newer file: W3 comes back (RTO delivered), AWB added; an older file changes nothing
        ingest.ingest(_full_report([rows[2] | {"Order Status": "RTO_DELIVERED", "AWB": "AWB3"}], end="16/11/2026"), "new.csv")
        ingest.ingest(_full_report([rows[2] | {"Order Status": "IN-TRANSIT"}], end="10/11/2026"), "old.csv")
        ok(sync.sync_now())
        with sync._remote() as rc:
            r = rc.execute("SELECT o.status, s.awb FROM shop.orders o JOIN shop.shipments s USING (order_id) "
                           "WHERE order_id='W3'").fetchone()
            assert (r["status"], r["awb"]) == ("rto_delivered", "AWB3")
            hist = [h["status"] for h in rc.execute("SELECT status FROM shop.status_history WHERE order_id='W3' ORDER BY id")]
            assert hist == ["rto_initiated", "rto_delivered"]
        # the app's review fix travels: shop.production follows
        ingest.set_name("W1", "Anu")
        ok(sync.sync_now())
        with sync._remote() as rc:
            assert rc.execute("SELECT tab FROM shop.production WHERE order_id='W1'").fetchone()["tab"] == "to_make"
        # Clear empties shop too, and every computer follows
        sync.clear("CLEAR")
        ok(sync.sync_now())
        with sync._remote() as rc:
            assert rc.execute("SELECT count(*) AS n FROM shop.orders").fetchone()["n"] == 0
        with db.tx() as con:
            assert con.execute("SELECT count(*) FROM shop_lines").fetchone()[0] == 0


def test_review_state_survives_clock_noise(two):
    """Supabase keeps a state time a few microseconds off this computer's: the app's ready / needs-review
    view still gets there (the 27-orders bug of 0.22.15)."""
    with two("A"):
        ingest.ingest(report([("N1", "PRINT", "Anu Sharma", "")]), "r.csv")
        ok(sync.sync_now())
        with sync._remote() as rc:  # as found in the live records
            rc.execute("UPDATE snapstl.order_records SET state_at = state_at + 0.000003, app_state = NULL, "
                       "review_reason = NULL WHERE order_id='N1'")
        with db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES ('N1', ?)", (db.now(),))
        ok(sync.sync_now())
        r = remote("N1")
        assert r["app_state"] == "needs_review" and r["tab"] == "check_name" and "space" in r["review_reason"]


def test_skip_approve_and_ready_to_ship_reach_every_computer(two):
    """Skip / Make anyway / Approve and Ready to ship pressed on one computer show on the others, and a
    computer that hadn't heard of a skip can't undo it in the shared records."""
    from snapstl import batches as B

    pc = two
    data = report([("V1", "PRINT", "Asha", ""), ("V2", "PRINT", "Anu Sharma", ""), ("V3", "PRINT", "Ravi", "")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
            ok(sync.sync_now())
    with pc("A"):  # Skip V1, approve V2 as it is
        with db.tx() as con:
            con.execute("UPDATE orders SET state='skipped' WHERE order_id='V1'")
        ingest.set_name("V2", order("V2")["name"])
        ok(sync.sync_now())
        assert remote("V1")["tab"] == "skipped" and remote("V2")["tab"] == "to_make"
    with pc("B"):
        # B uploads a newer report before it pulls: its older "ready" doesn't undo A's skip
        ingest.ingest(report([("V1", "PRINT", "Asha", "")], range_end="15/11/2026"), "new.csv")
        ok(sync.sync_now())
        assert remote("V1")["app_state"] == "skipped"
        assert order("V1")["state"] == "skipped"
        assert order("V2")["state"] == "ready" and order("V2")["review_reason"] is None
        ingest.reset_to_ready("V1")  # Make anyway on B
        ok(sync.sync_now())
    with pc("A"):
        ok(sync.sync_now())
        assert order("V1")["state"] == "ready" and remote("V1")["tab"] == "to_make"
        # A makes V3; base and name printed → Ready to ship on B too
        a = B.create_batch(["V3"], Settings())
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1"}, ["V3"])
            con.execute("UPDATE orders SET made_at=? WHERE order_id='V3'", (db.now(),))
        ok(sync.sync_now())
        assert remote("V3")["tab"] == "pack_it"
    with pc("B"):
        ok(sync.sync_now())
        assert order("V3")["state"] == "bundled" and order("V3")["made_at"]


def test_people_set_fields_reach_every_computer(two):
    """0.23: Label Generated, Express, flags … set on one computer reach the others; per group the
    newest change wins, so an older one sent later can't undo it."""
    pc = two
    data = report([("P1", "PRINT", "Asha", ""), ("P2", "DELIVERED", "Ravi", "02/11/2026")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
            ok(sync.sync_now())
    with pc("A"):
        with db.tx() as con:
            db.set_group(con, "P1", "express", express_by="2026-11-25", express_note="birthday")
            db.set_group(con, "P1", "label", label_at=db.now(), label_by="logistics")
        ok(sync.sync_now())
        r = remote("P1")
        assert (r["express_by"], r["express_note"], r["label_by"]) == ("2026-11-25", "birthday", "logistics")
        assert r["first_status"] == "PRINT" and remote("P2")["first_status"] == "DELIVERED"
    with pc("B"):
        ok(sync.sync_now())
        o = order("P1")
        assert (o["express_by"], o["express_note"], o["label_by"]) == ("2026-11-25", "birthday", "logistics")
        with db.tx() as con:  # B undoes Label Generated (a newer change)
            db.set_group(con, "P1", "label")
        ok(sync.sync_now())
        assert remote("P1")["label_at"] is None
    with pc("A"):
        # A sends its older label again (e.g. it hadn't pulled yet): the newer undo stays
        with db.tx() as con:
            con.execute("INSERT OR REPLACE INTO sync_outbox(order_id, queued_at) VALUES ('P1', ?)", (db.now(),))
        ok(sync.sync_now())
        assert remote("P1")["label_at"] is None and order("P1")["label_at"] is None
        assert order("P1")["express_by"] == "2026-11-25"


def test_print_started_reaches_every_computer(two):
    from snapstl import batches as B

    pc = two
    data = report([("S1", "PRINT", "Asha", "")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
            ok(sync.sync_now())
    with pc("A"):
        a = B.create_batch(["S1"], Settings())
        with db.tx() as con:
            db.finish_batch(con, a["batch_id"], "done", {"label": "Batch 1"}, ["S1"])
            con.execute("UPDATE orders SET print_started_at=? WHERE order_id='S1'", (db.now(),))
        ok(sync.sync_now())
        assert remote("S1")["print_started_at"]
    with pc("B"):
        ok(sync.sync_now())
        assert order("S1")["state"] == "bundled" and order("S1")["print_started_at"]


def test_call_notes_and_usage_reach_the_shared_records(two):
    pc = two
    with pc("A"):
        ingest.ingest(report([("C1", "PRINT", "Asha", "")]), "r.csv")
        with db.tx() as con:
            db.add_note(con, "C1", "Customer called, wants it by Friday", "logistics")
            db.count_usage(con, "copy_tracking_link")
            db.count_usage(con, "copy_tracking_link")
        ok(sync.sync_now())
        assert sync.pending() == 0
        with db.tx() as con:
            db.count_usage(con, "copy_tracking_link")
        ok(sync.sync_now())
    with pc("B"):
        ok(sync.sync_now())
        with db.tx() as con:
            notes = db.rows(con, "SELECT order_id, text, author FROM order_notes")
        assert notes == [{"order_id": "C1", "text": "Customer called, wants it by Friday", "author": "logistics"}]
        assert sync.usage() == [{"feature": "copy_tracking_link", "count": 3, "days": 1}]
    with sync._remote() as rc:  # never deleted
        import psycopg
        with pytest.raises(psycopg.Error):
            rc.execute("DELETE FROM snapstl.order_notes")


def test_older_snapstl_cannot_write_after_the_upgrade(two, monkeypatch):
    with two("A"):
        ok(sync.sync_now())
        with sync._remote() as rc:
            assert rc.execute("SELECT value FROM snapstl.meta WHERE key='schema_version'").fetchone()["value"] \
                == str(sync.SCHEMA_VERSION)
        monkeypatch.setattr(sync, "SCHEMA_VERSION", sync.SCHEMA_VERSION - 1)  # an older SnapSTL
        monkeypatch.setattr(sync, "_setup_done", "")
        st = sync.sync_now()
        assert "newer SnapSTL" in st["error"]


def test_print_checks_and_staff_reach_every_computer(two):
    """0.24: who checked a print (and the staff list) is shared; a hidden name stays hidden everywhere."""
    from snapstl import staff as staff_pins
    from snapstl import verify

    pc = two
    data = report([("V1", "PRINT", "Asha", "")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
            ok(sync.sync_now())
    with pc("A"):
        with db.tx() as con:
            verify.add_staff(con, "Meena")
            verify.add_staff(con, "Ravi")
            db.set_group(con, "V1", "check", print_done_at=1.0, checked_at=2.0, checked_by="Meena")
            verify.add_check(con, {"batch_id": 0, "plate": "Names/pink/Pink - Asha.3mf", "idx": 0, "order_id": "V1",
                                   "part": "names", "name": "Asha", "color": "pink"}, "letters", "Meena", letters="s")
        ok(sync.sync_now())
        r = remote("V1")
        assert (r["checked_by"], r["checked_at"]) == ("Meena", 2.0)
    with pc("B"):
        ok(sync.sync_now())
        assert order("V1")["checked_by"] == "Meena"
        with db.tx() as con:
            assert verify.staff_names(con) == ["Meena", "Ravi"]
            c = db.rows(con, "SELECT order_id, result, letters, staff, computer FROM plate_checks")
            assert c == [{"order_id": "V1", "result": "letters", "letters": "s", "staff": "Meena", "computer": "PC A"}]
            verify.hide_staff(con, "Ravi")
        ok(sync.sync_now())
    with pc("A"):
        ok(sync.sync_now())
        with db.tx() as con:
            assert verify.staff_names(con) == ["Meena"]
            staff_pins.set_pin(con, "Meena", "4826")  # 0.25.1: a PIN set on one computer works on the others
        ok(sync.sync_now())
    with pc("B"):
        ok(sync.sync_now())
        assert staff_pins.start("4826")["name"] == "Meena"
        with db.tx() as con:
            staff_pins.set_pin(con, "Meena", "5937")  # changed again: the newest PIN wins everywhere
        ok(sync.sync_now())
    with pc("A"):
        ok(sync.sync_now())
        assert staff_pins.start("5937")["name"] == "Meena"
        with pytest.raises(staff_pins.PinError):
            staff_pins.start("4826")
    with sync._remote() as rc:  # never deleted
        import psycopg
        with pytest.raises(psycopg.Error):
            rc.execute("DELETE FROM snapstl.print_checks")


def test_a_leftover_connection_never_hangs_the_sync(two, monkeypatch):
    """0.26.1: a sync cut off midway (app restarted) can leave its transaction open, holding a table. The setup
    used to wait for it forever ("Connecting to the shared order records…"); now it gives up and retries."""
    import psycopg

    pc = two
    with pc("A"):
        ok(sync.sync_now())
        monkeypatch.setattr(sync, "LOCK_WAIT", "1s")
        monkeypatch.setattr(sync, "_setup_done", "")  # as after a restart: the setup runs again
        held = psycopg.connect(os.environ["SNAPSTL_TEST_DATABASE_URL"])
        try:
            held.execute("SELECT * FROM snapstl.staff").fetchall()  # left open, like the cut-off sync
            t = time.time()
            s = sync.sync_now()
            assert "busy" in s["error"] and time.time() - t < 15
        finally:
            held.close()
        ok(sync.sync_now())  # and works again once the old connection is gone
        with sync._remote() as rc:
            assert rc.execute("SHOW idle_in_transaction_session_timeout").fetchone()["idle_in_transaction_session_timeout"] == "2min"


def test_spare_pieces_reach_every_computer(two):
    """0.28: the spare stock is shared; using a piece on one computer shows it used on the others."""
    from snapstl import spares

    pc = two
    data = report([("SP1", "PRINT", "Kiara", "")])
    for name in ("A", "B"):
        with pc(name):
            ingest.ingest(data, "r.csv")
            ok(sync.sync_now())
    with pc("A"):
        uid = spares.add("Kiara", "pink", "both", "shelf 2", "Meena")[0]["uid"]
        ok(sync.sync_now())
    with pc("B"):
        ok(sync.sync_now())
        st = spares.listing()["stock"]
        assert [(s["uid"], s["name"], s["note"], s["fits"]) for s in st] == [(uid, "Kiara", "shelf 2", ["SP1"])]
        spares.use("SP1", uid)
        ok(sync.sync_now())
        assert order("SP1")["reuse_of"] == "spare:" + uid
    with pc("A"):
        ok(sync.sync_now())
        assert spares.listing()["used"][0]["used_by"] == "SP1" and order("SP1")["reuse_of"] == "spare:" + uid
    with sync._remote() as rc:  # never deleted
        import psycopg
        with pytest.raises(psycopg.Error):
            rc.execute("DELETE FROM snapstl.spares")


def test_name_stand_orders_stay_on_this_computer(two):
    """0.29: stand orders are not sent to the shared records yet (a computer before 0.29 would take them for
    organizers); the organizer's still are."""
    from .test_029 import ROWS, report as report_029

    pc = two
    with pc("A"):
        ingest.ingest(report_029(ROWS), "r.csv")
        ok(sync.sync_now())
        assert remote("O1") is not None
        assert remote("G1") is None and remote("H1") is None
        with db.tx() as con:
            assert con.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
    with pc("B"):
        ok(sync.sync_now())
        assert order("O1") is not None and order("G1") is None
