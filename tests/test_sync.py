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
