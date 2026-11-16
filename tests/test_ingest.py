import csv
import io

from snapstl import db
from snapstl.ingest import clean_name, ingest, reset_to_ready, set_name

HEAD = ["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
        "Customer Name", "Product Customisation"]


def report(rows) -> bytes:
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Date Range: x"])
    w.writerow(HEAD)
    for oid, status, sku, name, color, qty in rows:
        cust = f"Enter Your First Name: {name}\nColor: {color}\nNumber of Compartments: 9731704521\n" if name is not None else ""
        w.writerow([oid, "", "01-11-2026", status, sku, qty, "Private Person", cust])
    return f.getvalue().encode()


def states(p):
    with db.tx(p) as con:
        return {r["order_id"]: (r["state"], r["name"], r["color"]) for r in db.rows(con, "SELECT * FROM orders")}


def test_ingest_filters_sku_status_and_dedupes(tmp_path):
    p = tmp_path / "t.db"
    s = ingest(report([
        ("A1", "PRINT", "Magicians_35", "Ayra", "Hot Pink", 1),
        ("A2", "DELIVERED", "Magicians_35", "Manvi", "Red", 1),
        ("A3", "PRINT", "Stickers_014", "Nope", "Red", 1),
        ("A4", "PRINT", "Magicians_35", "Ayra Indoria", "Pink", 1),
        ("A5", "PRINT", "Magicians_35", None, None, 1),
        ("A6", "PRINT", "magicians_35", "Riya 🩷", "Gray", 2),
    ]), "r1.csv", p)
    assert s.sku_rows == 5 and s.new == 5
    st = states(p)
    assert "A3" not in st
    assert st["A1"] == ("ready", "Ayra", "hot pink")
    assert st["A2"][0] == "not_needed"
    assert st["A4"][0] == "needs_review"  # space → review
    assert st["A5"][0] == "needs_review"  # no customisation
    assert st["A6"] == ("ready", "Riya", "grey")  # emoji stripped, gray→grey

    # second report: same orders again + a status change → no duplicates
    s2 = ingest(report([
        ("A1", "PRINT", "Magicians_35", "Ayra", "Hot Pink", 1),
        ("A6", "PACK", "Magicians_35", "Riya", "Gray", 2),
        ("A7", "PRINT", "Magicians_35", "Zoya", "Blue", 1),
    ]), "r2.csv", p)
    assert s2.new == 1 and s2.became_not_needed == 1
    st = states(p)
    assert len(st) == 6
    assert st["A6"][0] == "not_needed"


def test_bundled_orders_are_never_rebundled(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("B1", "PRINT", "Magicians_35", "Kabir", "Blue", 1)]), "r1.csv", p)
    with db.tx(p) as con:
        bid = db.create_batch(con, ["B1"], "/tmp/x")
        db.finish_batch(con, bid, "done", {}, ["B1"])
    ingest(report([("B1", "PRINT", "Magicians_35", "Kabir", "Blue", 1)]), "r2.csv", p)
    assert states(p)["B1"][0] == "bundled"
    reset_to_ready("B1", p)
    assert states(p)["B1"][0] == "ready"


def test_review_edit_makes_ready(tmp_path):
    p = tmp_path / "t.db"
    ingest(report([("C1", "PRINT", "Magicians_35", "Ayra Indoria", "Pink", 1)]), "r.csv", p)
    row = set_name("C1", "Ayra", None, p)
    assert row["state"] == "ready" and row["name"] == "Ayra" and row["name_edited"] == 1
    # an edited name survives later reports
    ingest(report([("C1", "PRINT", "Magicians_35", "Ayra Indoria", "Pink", 1)]), "r2.csv", p)
    assert states(p)["C1"][1] == "Ayra"


def test_clean_name():
    assert clean_name("Jeenal") == ("Jeenal", None)
    assert clean_name("  Anaya ❤️ ")[0] == "Anaya" and clean_name("  Anaya ❤️ ")[1] is None
    n, why = clean_name("A.J.")
    assert n == "AJ" and "Removed" in why
    assert clean_name("")[1] == "No name in the order"


def test_newest_entry_in_the_file_dates_its_statuses(tmp_path):
    """No Date Range line: the newest date/time in the file says how recent its statuses are, so an older
    file uploaded later doesn't undo a newer status, and a newer one replaces it."""
    p = tmp_path / "t.db"

    def plain(rows):  # (order_id, created, status)
        f = io.StringIO()
        w = csv.writer(f)
        w.writerow(HEAD)
        for oid, created, status in rows:
            w.writerow([oid, "", created, status, "Magicians_35", 1, "Private Person",
                        "Enter Your First Name: Asha\nColor: Pink\n"])
        return f.getvalue().encode()

    new = ingest(plain([("T1", "01-11-2026 09:00:00", "DELIVERED"), ("T2", "15-11-2026 13:22:05", "PRINT")]), "new.csv", p)
    assert new.as_of == "2026-11-15~13:22:05" and new.report_date == ""
    old = ingest(plain([("T1", "01-11-2026 09:00:00", "PRINT"), ("T3", "14-11-2026 18:00:00", "PRINT")]), "old.csv", p)
    assert old.as_of == "2026-11-14~18:00:00" and old.kept_newer_status == 1
    with db.tx(p) as con:
        assert con.execute("SELECT source_status FROM orders WHERE order_id='T1'").fetchone()[0] == "DELIVERED"
    newer = ingest(plain([("T1", "01-11-2026 09:00:00", "RTO"), ("T4", "16-11-2026 08:00", "PRINT")]), "newer.csv", p)
    assert newer.as_of == "2026-11-16~08:00:00" and newer.kept_newer_status == 0
    with db.tx(p) as con:
        assert con.execute("SELECT source_status FROM orders WHERE order_id='T1'").fetchone()[0] == "RTO"
    # a stamp from before 0.22.8 (day only) ranks below the same day with a time
    assert "2026-11-15|2026-11-15~10:00:00|x" > "2026-11-15|2026-11-15|y"
