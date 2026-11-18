"""0.23 phase 2: one tab rule (tabs.py), the report rules (ingest), Printing from print jobs."""
import csv
import io
import itertools
import json
import sqlite3

from snapstl import db, makeit, tabs
from snapstl.ingest import ingest


def report(rows) -> bytes:
    """rows: (order_id, status). Every order: name Asha, colour Pink."""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Product Customisation"])
    for oid, status in rows:
        w.writerow([oid, "", "01-11-2026", status, "Magicians_35", 1, "Private Person",
                    "Enter Your First Name: Asha\nColor: Pink\n"])
    return f.getvalue().encode()


def get(p, oid):
    with db.tx(p) as con:
        return dict(con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())


def tab(p, oid):
    o = get(p, oid)
    with db.tx(p) as con:  # the SQL rule (Orders page) must say the same
        assert con.execute(f"SELECT {tabs.TAB_SQL} FROM orders WHERE order_id=?", (oid,)).fetchone()[0] == tabs.tab_of(o)
    return tabs.tab_of(o)


def sql(p, q, *args):
    with db.tx(p) as con:
        con.execute(q, args)


# ---------------- the rule ----------------
def test_python_and_sql_rules_always_agree():
    states = ("ready", "needs_review", "in_batch", "bundled", "printed", "skipped", "not_needed")
    statuses = ("PRINT", "PACK", "HANDOVER", "IN-TRANSIT", "in transit", "DELIVERED", "CANCELLED INITIATED",
                "RTO_INITIATED", "RTO_DELIVERED", "AWB EXPIRED", "", None, "SOMETHING NEW")
    cols = ("state", "source_status", "made_at", "print_started_at", "label_at", "remake_at", "flag", "reuse_of")
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE orders ({', '.join(cols)})")
    rows = list(itertools.product(states, statuses, (None, 1.0), (None, 1.0), (None, 5.0), (None, 3.0, 10.0),
                                  (None, "cancel_request"), (None, "R9")))
    con.executemany(f"INSERT INTO orders VALUES ({','.join('?' * len(cols))})", rows)
    got = con.execute(f"SELECT {', '.join(cols)}, {tabs.TAB_SQL} FROM orders").fetchall()
    assert len(got) == len(rows)
    for r in got:
        o = dict(zip(cols, r[:-1]))
        assert r[-1] == tabs.tab_of(o), o
        assert r[-1] in tabs.KEYS


def test_tabs_by_example():
    t = lambda **o: tabs.tab_of({"source_status": "PRINT", **o})  # noqa: E731
    assert t(state="ready") == "to_make"
    assert t(state="needs_review") == "check_name"
    assert t(state="in_batch") == "ready_to_print" and t(state="bundled") == "ready_to_print"
    assert t(state="bundled", print_started_at=1) == "printing"
    assert t(state="bundled", print_started_at=1, made_at=2) == "pack_it"
    assert t(state="printed") == "pack_it"
    assert t(state="ready", reuse_of="RTO1") == "pack_it"  # sent with a returned piece
    assert t(state="bundled", made_at=2, label_at=3) == "ship_it"
    assert t(state="bundled", made_at=2, source_status="PACK") == "ship_it"
    assert t(state="not_needed", source_status="HANDOVER") == "ship_it"  # HANDOVER joins Label done
    assert t(state="bundled", made_at=2, source_status="IN-TRANSIT") == "on_the_way"
    assert t(state="not_needed", source_status="DELIVERED") == "delivered"
    assert t(state="not_needed", source_status="RTO_ACKNOWLEDGED") == "rto"
    assert t(state="not_needed", source_status="CANCELLED INITIATED") == "cancelled"
    assert t(state="not_needed", source_status="AWB EXPIRED") == "problems"  # history: nobody knows if made
    assert t(state="bundled", flag="cancelled_in_production", source_status="CANCELLED INITIATED") == "problems"
    assert t(state="skipped", flag="x") == "skipped"
    # a remake ignores the first parcel's statuses until a new label is made
    assert t(state="ready", source_status="DELIVERED", remake_at=10) == "to_make"
    assert t(state="bundled", made_at=12, source_status="DELIVERED", remake_at=10) == "pack_it"
    assert t(state="bundled", made_at=12, source_status="DELIVERED", remake_at=10, label_at=13) == "delivered"


# ---------------- report rules ----------------
def test_a_report_never_moves_an_order_backwards(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("B1", "PRINT")]), "a.csv", p)
    sql(p, "UPDATE orders SET state='bundled', print_started_at=1, made_at=2 WHERE order_id='B1'")
    with db.tx(p) as con:
        db.set_group(con, "B1", "label", label_at=db.now(), label_by="logistics")
    assert tab(p, "B1") == "ship_it"
    ingest(report([("B1", "PRINT")]), "b.csv", p)  # a report taken before the label was made
    assert tab(p, "B1") == "ship_it" and get(p, "B1")["flag"] is None


def test_awb_expired(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("A1", "PRINT"), ("A2", "PRINT")]), "a.csv", p)
    # made + label done → label expired: back to Printed – pack it with a note
    sql(p, "UPDATE orders SET state='bundled', made_at=2 WHERE order_id='A1'")
    with db.tx(p) as con:
        db.set_group(con, "A1", "label", label_at=db.now(), label_by="logistics")
    ingest(report([("A1", "PACK"), ("A2", "AWB EXPIRED")]), "b.csv", p)
    ingest(report([("A1", "AWB EXPIRED")]), "c.csv", p)
    a1 = get(p, "A1")
    assert a1["label_at"] is None and a1["label_note"] == "Label expired – make a new label" and a1["label_changed"]
    assert tab(p, "A1") == "pack_it"
    ingest(report([("A1", "PRINT")]), "d.csv", p)  # the website puts it back to PRINT
    assert tab(p, "A1") == "pack_it" and get(p, "A1")["state"] == "bundled"
    # not made yet: still to make, no flag
    assert get(p, "A2")["state"] == "ready" and tab(p, "A2") == "to_make" and get(p, "A2")["flag"] is None


def test_problems_are_flagged(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("P1", "PRINT"), ("P2", "PRINT"), ("P3", "PRINT"), ("P4", "PRINT"),
                   ("H1", "PACK"), ("H2", "DELIVERED")]), "a.csv", p)
    sql(p, "UPDATE orders SET state='bundled' WHERE order_id IN ('P2','P3')")
    sql(p, "UPDATE orders SET state='bundled', made_at=2 WHERE order_id='P4'")
    ingest(report([("P1", "PACK"), ("P2", "PACK"), ("P3", "CANCELLED INITIATED"), ("P4", "PACK")]), "b.csv", p)
    # listed to make, never made, website packed it
    assert (get(p, "P1")["flag"], tab(p, "P1")) == ("past_print_not_made", "problems")
    assert "PACK" in get(p, "P1")["flag_note"] and get(p, "P1")["state"] == "not_needed"
    # in a batch, not printed yet, website packed it
    assert (get(p, "P2")["flag"], tab(p, "P2")) == ("past_print_not_made", "problems")
    # cancelled while being made
    assert (get(p, "P3")["flag"], get(p, "P3")["flag_note"]) == ("cancelled_in_production", "Cancelled – don't pack")
    # made and packed: just Label done, no flag
    assert get(p, "P4")["flag"] is None and tab(p, "P4") == "ship_it"
    # first seen past PRINT = history: never flagged
    assert get(p, "H1")["flag"] is None and tab(p, "H1") == "ship_it" and get(p, "H1")["first_status"] == "PACK"
    assert tab(p, "H2") == "delivered"
    # history packed outside SnapSTL, then label expired and back to PRINT: flagged, never made again
    ingest(report([("H1", "AWB EXPIRED")]), "c.csv", p)
    assert get(p, "H1")["flag"] == "back_to_print"
    ingest(report([("H1", "PRINT")]), "d.csv", p)
    assert get(p, "H1")["state"] == "not_needed" and tab(p, "H1") == "problems"
    with db.tx(p) as con:  # a person clears the flag: it stays out of New orders
        db.set_group(con, "H1", "flag")
    assert tab(p, "H1") == "problems" and get(p, "H1")["state"] == "not_needed"


def test_cancelled_before_making_is_just_cancelled(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("C1", "PRINT")]), "a.csv", p)
    ingest(report([("C1", "CANCELLED INITIATED")]), "b.csv", p)
    assert get(p, "C1")["flag"] is None and tab(p, "C1") == "cancelled"


# ---------------- Printing ----------------
def test_starting_a_plate_moves_its_orders_to_printing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(makeit.farm.WORKER, "kick", lambda: None, raising=False)
    ingest(report([("S1", "PRINT"), ("S2", "PRINT")]), "a.csv")
    plates = [{"file": "Bases/b1.3mf", "kind": "bases", "order_ids": ["S1", "S2"]},
              {"file": "Names/pink/n1.3mf", "kind": "names", "order_ids": ["S1"]},
              {"file": "Names/pink/n2.3mf", "kind": "names", "order_ids": ["S2"]}]
    with db.tx() as con:
        bid = db.create_batch(con, ["S1", "S2"], str(tmp_path), status="done")
        con.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps({"plates": plates}), bid))
        con.execute("UPDATE orders SET state='bundled', batch_id=? WHERE order_id IN ('S1','S2')", (bid,))
    assert tab(db.DB_PATH, "S1") == "ready_to_print"

    def job(plate, state):
        with db.tx() as con:
            con.execute("INSERT INTO print_jobs(batch_id, plate, kind, printer_id, state, created_at) "
                        "VALUES (?,?,?,1,?,?)", (bid, plate, plate.split("/")[0].lower(), state, db.now()))

    job("Names/pink/n1.3mf", "queued")  # a person pressed Start print for S1's name
    makeit.update_made(bid)
    assert tab(db.DB_PATH, "S1") == "printing" and tab(db.DB_PATH, "S2") == "ready_to_print"
    job("Names/pink/n1.3mf", "done")
    job("Bases/b1.3mf", "done")
    makeit.update_made(bid)
    # 0.24: printed is not made — S1 waits for a person's check (Print → Verify)
    assert tab(db.DB_PATH, "S1") == "printing" and get(db.DB_PATH, "S1")["print_done_at"]
    from snapstl import verify
    with db.tx() as con:
        verify.add_staff(con, "Asha")
    for f in ("Names/pink/n1.3mf", "Bases/b1.3mf"):
        verify.submit({"batch_id": bid, "file": f}, "Asha",
                      [{"batch_id": bid, "plate": f, "idx": 0, "result": "good"}])
    assert tab(db.DB_PATH, "S1") == "pack_it" and tab(db.DB_PATH, "S2") == "printing"
    assert get(db.DB_PATH, "S1")["checked_by"] == "Asha"
    # once after updating, older batches get Printing from the jobs they have
    sql(db.DB_PATH, "UPDATE orders SET print_started_at=NULL")
    assert makeit.backfill_started() >= 1 and get(db.DB_PATH, "S2")["print_started_at"]
    assert makeit.backfill_started() == 0


def test_a_name_starting_with_a_small_letter_is_checked_first(tmp_path):
    """0.23.1: "riya" is almost always a typing slip — Check name first; the name still prints as typed."""
    from snapstl.ingest import SMALL_FIRST, clean_name, recheck_small_first

    assert clean_name("riya") == ("riya", SMALL_FIRST)
    assert clean_name("Riya") == ("Riya", None)
    assert clean_name("❤️ riya")[1] == SMALL_FIRST  # after the heart is dropped
    p = tmp_path / "t.db"
    ingest(report([("L1", "PRINT")]), "a.csv", p)
    with db.tx(p) as con:  # as orders from before 0.23.1: lower-case names already Ready
        con.execute("INSERT INTO orders(order_id, sku, source_status, name, raw_name, color, state, first_status) VALUES "
                    "('L2','Magicians_35','PRINT','riya','riya','pink','ready','PRINT'), "
                    "('L3','Magicians_35','PRINT','meera','meera','pink','ready','PRINT')")
        con.execute("UPDATE orders SET name_edited=1 WHERE order_id='L3'")  # a person already approved it
    assert recheck_small_first(p) == 1
    l2 = get(p, "L2")
    assert (l2["state"], tab(p, "L2"), l2["review_reason"]) == ("needs_review", "check_name", SMALL_FIRST)
    assert get(p, "L3")["state"] == "ready" and get(p, "L1")["state"] == "ready"
    assert recheck_small_first(p) == 0  # only once
