"""0.23 order-flow data (phase 1): new order fields, field groups, call notes, usage counts."""
import csv
import io
import sqlite3

import pytest

from snapstl import db
from snapstl.ingest import ingest


def report(rows) -> bytes:
    """rows: (order_id, status, sku, name, colour, quantity)"""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Product Customisation"])
    for oid, status, sku, name, color, qty in rows:
        w.writerow([oid, "", "01-11-2026", status, sku, qty, "Private Person",
                    f"Enter Your First Name: {name}\nColor: {color}\n"])
    return f.getvalue().encode()


def one(p, oid):
    with db.tx(p) as con:
        return dict(con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())


def outbox(p):
    with db.tx(p) as con:
        return {r[0] for r in con.execute("SELECT order_id FROM sync_outbox")}


def test_first_status_is_recorded(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("F1", "PRINT", "Magicians_35", "Asha", "Pink", 1),
                   ("F2", "DELIVERED", "Magicians_35", "Ravi", "Blue", 1)]), "a.csv", p)
    assert one(p, "F1")["first_status"] == "PRINT"
    assert one(p, "F2")["first_status"] == "DELIVERED"
    # a later report never changes it
    ingest(report([("F1", "PACK", "Magicians_35", "Asha", "Pink", 1)]), "b.csv", p)
    assert one(p, "F1")["first_status"] == "PRINT"


def test_older_databases_get_first_status_and_new_columns(tmp_path):
    """A database from before 0.23: new columns are added, and first_status is filled in once:
    PRINT for orders SnapSTL listed to make, the webstore status for history."""
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, clubbed_id TEXT, sku TEXT NOT NULL, order_date TEXT, "
                "source_status TEXT, quantity INTEGER NOT NULL DEFAULT 1, raw_name TEXT, raw_color TEXT, "
                "special_request TEXT, name TEXT, color TEXT, name_edited INTEGER NOT NULL DEFAULT 0, "
                "review_reason TEXT, state TEXT NOT NULL, batch_id INTEGER, last_error TEXT, first_file TEXT, "
                "last_file TEXT, first_seen REAL, updated_at REAL)")
    con.executemany("INSERT INTO orders(order_id, sku, source_status, state) VALUES (?,?,?,?)",
                    [("H1", "Magicians_35", "PACK", "bundled"), ("H2", "Magicians_35", "Delivered", "not_needed"),
                     ("H3", "Magicians_35", "PRINT", "ready")])
    con.commit()
    con.close()
    with db.tx(p) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
    assert set(db.GROUP_COLS) | {"first_status", "print_started_at"} <= cols
    assert one(p, "H1")["first_status"] == "PRINT"
    assert one(p, "H2")["first_status"] == "DELIVERED"
    assert one(p, "H3")["first_status"] == "PRINT"


def test_set_group_stamps_the_change_and_queues_it(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("G1", "PRINT", "Magicians_35", "Asha", "Pink", 1)]), "a.csv", p)
    with db.tx(p) as con:
        con.execute("UPDATE orders SET in_remote=1")
        con.execute("DELETE FROM sync_outbox")
        assert db.set_group(con, "G1", "express", express_by="2026-11-25", express_note="birthday")
    o = one(p, "G1")
    assert (o["express_by"], o["express_note"]) == ("2026-11-25", "birthday") and o["express_changed"]
    assert outbox(p) == {"G1"}  # reaches the shared records
    first = o["express_changed"]
    with db.tx(p) as con:  # clearing it is a change too
        assert db.set_group(con, "G1", "express")
    o = one(p, "G1")
    assert o["express_by"] is None and o["express_note"] is None and o["express_changed"] >= first
    with db.tx(p) as con:
        assert not db.set_group(con, "nope", "label", label_at=1.0)
        with pytest.raises(ValueError):
            db.set_group(con, "G1", "label", express_by="x")


def test_call_notes_and_usage_counts(tmp_path):
    p = tmp_path / "t.db"
    with db.tx(p) as con:
        n = db.add_note(con, "N1", "  Customer wants it by Friday ", "logistics")
        with pytest.raises(ValueError):
            db.add_note(con, "N1", "   ")
        for _ in range(3):
            db.count_usage(con, "copy_tracking_link", day="2026-11-18")
        db.count_usage(con, "whatsapp", day="2026-11-18")
        db.count_usage(con, "")
    with db.tx(p) as con:
        notes = db.rows(con, "SELECT * FROM order_notes")
        use = {r["feature"]: (r["count"], r["sent"]) for r in db.rows(con, "SELECT * FROM usage_counts")}
    assert [(x["uid"], x["text"], x["author"], x["sent"]) for x in notes] == [
        (n["uid"], "Customer wants it by Friday", "logistics", 0)]
    assert use == {"copy_tracking_link": (3, 0), "whatsapp": (1, 0)}
