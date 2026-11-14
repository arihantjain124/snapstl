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
