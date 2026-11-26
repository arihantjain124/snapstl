"""0.29: products — name stands (GTA, Harry Potter, Barbie) are read from the report and listed under their
product; spare pieces and Stats stay the organizer's. (0.30: they can be batched, one product per batch.)"""
import csv
import io

import pytest

from snapstl import db, ingest, products, sync
from snapstl.settings import Settings

from .test_flow import client  # noqa: F401  (client is a fixture)
from .test_ingest import HEAD


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def report(rows) -> bytes:
    """(order id, status, sku, customisation text)"""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Date Range: x"])
    w.writerow(HEAD)
    for oid, status, sku, cust in rows:
        w.writerow([oid, "", "01-11-2026", status, sku, 1, "Private Person", cust])
    return f.getvalue().encode()


ROWS = [
    ("O1", "PRINT", "Magicians_35", "Enter Your First Name: Ayra\nColor: Pink"),
    ("G1", "PRINT", "Magicians_174", "Write your NAME here.: SIDHARTH"),
    ("G2", "PRINT", "magicians_174", "Write your NAME here.: #WIN"),
    ("H1", "PRINT", "Magicians_156", "Write your NAME here.: Sahil"),
    ("B1", "DELIVERED", "Magicians_117", "Write your NAME here.: Nora"),
    ("X1", "PRINT", "Stickers_014", "Write your NAME here.: Nope"),
]


def _ingest(client):  # noqa: F811
    r = client.post("/api/ingest", files={"file": ("r.csv", report(ROWS), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _orders(client, **q):  # noqa: F811
    qs = "&".join(f"{k}={v}" for k, v in {"tab": "all", "limit": 500, **q}.items())
    return {o["order_id"]: o for o in client.get(f"/api/orders?{qs}").json()["orders"]}


def test_products_list():
    assert [p.key for p in products.PRODUCTS] == ["organizer", "gta", "harry_potter", "barbie"]
    assert products.by_sku("MAGICIANS_174").key == "gta" and products.by_sku("Stickers_014") is None
    assert products.of_order({"sku": "Adhoc"}) is products.ORGANIZER and products.of_order({}) is products.ORGANIZER
    assert products.canonical_sku("magicians_117") == "Magicians_117"


def test_stand_orders_are_read_from_the_report(client):  # noqa: F811
    _ingest(client)
    with db.tx() as con:
        st = {r["order_id"]: r for r in db.rows(con, "SELECT * FROM orders")}
    assert set(st) == {"O1", "G1", "G2", "H1", "B1"}  # other products: shop records only
    assert (st["G1"]["sku"], st["G1"]["name"], st["G1"]["color"], st["G1"]["state"]) == ("Magicians_174", "SIDHARTH", "", "ready")
    assert st["G2"]["sku"] == "Magicians_174" and st["G2"]["state"] == "needs_review"  # '#' removed: checked as today
    assert st["B1"]["state"] == "not_needed" and st["O1"]["color"] == "pink"
    with db.tx() as con:  # the organizer's report columns only (shared records' report_rows)
        assert [r[0] for r in con.execute("SELECT order_id FROM report_rows")] == ["O1"]


def test_orders_page_product_chips(client):  # noqa: F811
    _ingest(client)
    assert set(_orders(client)) == {"O1", "G1", "G2", "H1", "B1"}  # no product = every product
    assert set(_orders(client, product="organizer")) == {"O1"}
    assert set(_orders(client, product="gta")) == {"G1", "G2"}
    g = _orders(client, product="gta")["G1"]
    assert g["product"] == "gta" and g["product_label"] == "GTA" and g["product_kind"] == "stand"
    assert g["stand_colors"] == ["black", "white"] and g["stand_size"] == [232, 40, 40.0] and g["compartments"] is None
    f = client.get("/api/orders/facets?tab=to_make&product=organizer").json()
    assert f["tabs"]["to_make"] == 1 and f["products"] == {"organizer": 1, "gta": 2, "harry_potter": 1, "barbie": 0}
    assert client.get("/api/orders/facets?tab=to_make&product=gta").json()["tabs"]["to_make"] == 1
    assert client.get("/api/orders?tab=all&product=nope").status_code == 400
    assert client.get("/api/products").json()["products"][1] == {
        "key": "gta", "label": "GTA", "kind": "stand", "skus": ["Magicians_174"], "makeable": True,
        "buildable": True, "printable": True}
    # Check name: a stand is approved without a colour
    assert client.post("/api/orders/G2/name", json={"name": "WIN", "color": ""}).status_code == 200
    assert _orders(client)["G2"]["tab"] == "to_make"


def test_one_product_per_batch(client):  # noqa: F811
    _ingest(client)
    r = client.post("/api/batches", json={"order_ids": ["G1", "H1", "O1"]})
    assert r.status_code == 400 and "One product per batch" in r.text
    assert client.post("/api/batches", json={"order_ids": ["G1"]}).json()["orders"] == 1
    with db.tx() as con:
        assert con.execute("SELECT state FROM orders WHERE order_id='G1'").fetchone()[0] == "in_batch"


def test_make_next_batch_takes_the_chosen_product(client):  # noqa: F811
    _ingest(client)
    with db.tx() as con:
        con.execute("UPDATE orders SET state='bundled' WHERE order_id='O1'")
    r = client.post("/api/batches/next", json={"count": 10})  # no product: the organizer, as before 0.30
    assert r.status_code == 400 and "Nothing to make" in r.text
    r = client.post("/api/batches/next", json={"count": 10, "product": "harry_potter"})
    assert r.status_code == 200, r.text
    assert r.json()["orders"] == 1 and r.json()["download_error"] == ""  # no license password needed


def test_spare_pieces_and_returned_pieces_stay_within_the_product(client):  # noqa: F811
    _ingest(client)
    client.post("/api/spares", json={"name": "Sahil", "part": "bases"})
    with db.tx() as con:
        con.execute("INSERT INTO orders(order_id,sku,source_status,name,color,state,first_seen,updated_at,quantity) "
                    "VALUES ('R1','Magicians_35','RTO_DELIVERED','SIDHARTH','',  'not_needed',1,1,1)")
    o = _orders(client)
    assert o["H1"]["spares"] == [] and o["G1"]["returned"] == []  # a base / an organizer isn't a stand
    assert client.get("/api/orders?tab=all").json()["in_stock"] == 0
    assert client.get("/api/spares").json()["stock"][0]["fits"] == []


def test_unknown_products_stay_out_of_the_shared_records():
    with db.tx() as con:
        before = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert sync.merge(con, {"order_id": "Z9", "sku": "Magicians_999", "state": "ready", "state_at": 1}) is None
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before


def test_name_stand_settings(client):  # noqa: F811
    s = Settings()
    assert (s.stands.thickness, s.stands.letter_protrusion, s.stands.brim_offset, s.stands.font_size) == (40, 3, 6, 40)
    assert s.stands.products["harry_potter"] == {"letter_spacing": 0, "base_color": "yellow", "letter_color": "black"}
    cur = client.get("/api/settings").json()
    cur["stands"]["brim_offset"] = 5
    cur["stands"]["products"]["gta"]["letter_spacing"] = 20
    r = client.put("/api/settings", json=cur)
    assert r.status_code == 200, r.text
    assert r.json()["stands"]["brim_offset"] == 5 and r.json()["stands"]["products"]["gta"]["letter_spacing"] == 20
    cur["stands"]["letter_protrusion"] = 50
    assert client.put("/api/settings", json=cur).status_code == 400
    client.post("/api/settings/reset")


def test_ingest_summary_counts_products(tmp_path):
    s = ingest.ingest(report(ROWS), "r.csv", tmp_path / "t.db")
    assert s.sku_rows == 5 and s.by_product == {"organizer": 1, "gta": 2, "harry_potter": 1, "barbie": 1}
