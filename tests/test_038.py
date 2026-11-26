"""0.38: Stats — labels (all on ShopDeck, and from SnapSTL) and shipping (parcels, pieces, the day's names)."""
import csv
import io
import time

import pytest

from snapstl import db, ingest, shopdeck, stats

from .test_037 import _connect, sd  # noqa: F401  (sd is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)

COLS = ["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
        "Customer Name", "Product Customisation", "Pickup Date"]


@pytest.fixture(autouse=True)
def _clean(client):  # noqa: F811
    with db.tx() as con:
        con.execute("DELETE FROM floor_events")
        con.execute("DELETE FROM label_jobs")
    yield


def report(rows) -> bytes:
    """rows: (order_id, clubbed, status, sku, qty, customer, customisation, pickup DD/MM/YYYY)."""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Date Range: 01/11/2026 to 27/11/2026"])
    w.writerow(COLS)
    for oid, club, st, sku, qty, cust, text, pick in rows:
        w.writerow([oid, club, "20-11-2026", st, sku, qty, cust, text, pick])
    return f.getvalue().encode()


def test_label_and_pickup_moments_are_only_what_we_saw_happen(client, sd):  # noqa: F811
    _connect(client)
    sd.orders = [{"id": "F1", "order_status": "created"}, {"id": "F2", "order_status": "printed"},
                 {"id": "F3", "order_status": "created", "clubbed_id": "C9"},
                 {"id": "F4", "order_status": "created", "clubbed_id": "C9"},
                 {"id": "F5", "order_status": "delivered"}]
    shopdeck.POLLER.once()  # the first look: nothing counted (it can't know when these happened)
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM floor_events").fetchone()[0] == 0
    sd.orders = [{"id": "F1", "order_status": "printed"}, {"id": "F2", "order_status": "dispatched"},
                 {"id": "F3", "order_status": "dispatched", "clubbed_id": "C9"},
                 {"id": "F4", "order_status": "dispatched", "clubbed_id": "C9"},
                 {"id": "F5", "order_status": "delivered"}]
    shopdeck.POLLER.once()
    with db.tx() as con:
        ev = {(r["order_id"], r["kind"]): r["group_key"] for r in db.rows(con, "SELECT * FROM floor_events")}
    assert ev == {("F1", "label"): "F1", ("F2", "shipped"): "F2", ("F3", "label"): "C9", ("F3", "shipped"): "C9",
                  ("F4", "label"): "C9", ("F4", "shipped"): "C9"}
    today = time.strftime("%Y-%m-%d")
    with db.tx() as con:
        d = stats.floor_days(con, [today])[today]
    assert d["labels_all"] == 2  # F1 and the clubbed C9 (one label)
    assert (d["ship_orders"], d["ship_pcs"]) == (2, 3)  # F2 and C9; 3 pieces


def test_shipped_from_the_report_every_product_with_names(client, sd):  # noqa: F811
    ingest.ingest(report([
        ("S1", "", "IN-TRANSIT", "Magicians_35", 1, "Asha K", "Enter Your First Name: Riya\nColor: Pink", "26/11/2026"),
        ("S2", "CL1", "IN-TRANSIT", "Magicians_35", 2, "Ravi", "Enter Your First Name: Dev\nColor: Red", "26/11/2026"),
        ("S3", "CL1", "IN-TRANSIT", "Magicians_174", 1, "Ravi", "Write your NAME here.: ROCKET", "26/11/2026"),
        ("S4", "", "IN-TRANSIT", "Magicians_219", 3, "Meena", "", "26/11/2026"),     # a product SnapSTL doesn't make
        ("S5", "", "IN-TRANSIT", "Magicians_219", 1, "Tara", "", "25/11/2026"),       # another day
        ("S6", "", "PRINT", "Magicians_35", 1, "Zoya", "Enter Your First Name: Zoya\nColor: Pink", "")]),
        "r.csv")
    with db.tx() as con:  # the API also saw S4 dispatched (counted once, on the report's day) and S7 (no report yet)
        con.execute("INSERT INTO floor_events VALUES ('S4','shipped','S4','2026-11-27',0)")
        con.execute("INSERT INTO floor_events VALUES ('S7','shipped','S7','2026-11-26',0)")
        d = stats.floor_days(con, ["2026-11-25", "2026-11-26", "2026-11-27"])
    assert (d["2026-11-26"]["ship_orders"], d["2026-11-26"]["ship_pcs"]) == (4, 8)  # S1, CL1 (S2+S3), S4, S7
    assert (d["2026-11-25"]["ship_orders"], d["2026-11-27"]["ship_orders"]) == (1, 0)
    log = stats.shipped_log("2026-11-26")
    assert (log["orders"], log["pcs"]) == (4, 8)
    cl = next(p for p in log["rows"] if p["group"] == "CL1")
    assert cl["order_ids"] == ["S2", "S3"] and cl["pcs"] == 3 and cl["customer"] == "Ravi"
    assert [i["name"] for i in cl["items"]] == ["Dev (Red)", "ROCKET"]
    assert log["by_product"]["Organizer"] == 3 and log["by_product"]["Magicians_219"] == 3
    rows = list(csv.reader(io.StringIO(client.get("/api/stats/shipped", params={"day": "2026-11-26", "csv": 1}).text)))
    assert rows[0][:5] == ["Pickup day", "Order ID", "Product", "Pieces", "Name printed"] and len(rows) == 6
    assert ["2026-11-26", "S1", "Organizer", "1", "Riya (Pink)", "Asha K", "report"] in rows
    assert client.get("/api/stats/shipped", params={"day": "26-11-2026"}).status_code == 400


def test_stats_page_has_labels_and_shipping(client):  # noqa: F811
    now = time.time()
    with db.tx() as con:
        for i, (key, state) in enumerate([("K1", "done"), ("K1", "done"), ("K2", "done"), ("K3", "failed")]):
            con.execute("INSERT INTO label_jobs(key, search, expected, state, step, created_at, updated_at, printed_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (key, key, "{}", state, state, now, now, now if state == "done" else None))
        con.execute("INSERT INTO floor_events VALUES ('L1','label','L1',?,?)", (time.strftime("%Y-%m-%d"), now))
    r = client.get("/api/stats", params={"days": 7}).json()
    assert r["today"]["labels_snapstl"] == 2 and r["today"]["labels_all"] == 1  # K1 twice = one order's label
    assert r["labels_since"] == time.strftime("%Y-%m-%d")
    assert all("ship_pcs" in d and "labels_all" in d for d in r["days"])
