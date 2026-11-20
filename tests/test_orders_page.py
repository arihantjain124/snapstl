"""0.23 phase 3: the Orders page's new tabs and buttons (Clear flag, Use returned piece, Remake, Express)."""
import pytest
from fastapi.testclient import TestClient

from snapstl import db

from .test_tabs import report


@pytest.fixture
def client():
    from snapstl import server

    with TestClient(server.app, base_url="http://127.0.0.1:8317", client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200
        yield c


def upload(c, rows, name="r.csv"):
    r = c.post("/api/ingest", files={"file": (name, report(rows), "text/csv")})
    assert r.status_code == 200, r.text


def ids(c, tab, **f):
    return [o["order_id"] for o in c.get("/api/orders", params={"tab": tab, "limit": 500, **f}).json()["orders"]]


def order(c, oid):
    return next(o for o in c.get("/api/orders", params={"tab": "all", "limit": 500}).json()["orders"]
                if o["order_id"] == oid)


def test_tabs_and_counts(client):
    upload(client, [("N1", "PRINT"), ("N2", "PRINT"), ("D1", "DELIVERED"), ("K1", "PACK")])
    f = client.get("/api/orders/facets", params={"tab": "to_make"}).json()["tabs"]
    assert (f["to_make"], f["delivered"], f["ship_it"], f["problems"], f["all"]) == (2, 1, 1, 0, 4)
    assert sorted(ids(client, "to_make")) == ["N1", "N2"]
    assert order(client, "N1")["tab"] == "to_make"
    assert client.get("/api/orders", params={"tab": "ready"}).status_code == 400  # old tab names are gone


def test_express_orders_come_first(client):
    upload(client, [("E1", "PRINT"), ("E2", "PRINT"), ("E3", "PRINT")])
    with db.tx() as con:
        db.set_group(con, "E3", "express", express_by="2026-11-25", express_note="birthday")
        db.set_group(con, "E1", "express", express_by="2026-11-21")
    assert ids(client, "to_make")[:2] == ["E1", "E3"]
    assert sorted(ids(client, "express")) == ["E1", "E3"]
    assert ids(client, "to_make", f_express="yes") == ["E1", "E3"]
    o = order(client, "E3")
    assert (o["express_by"], o["express_note"]) == ("2026-11-25", "birthday")


def test_clear_flag(client):
    upload(client, [("F1", "PRINT")])
    upload(client, [("F1", "PACK")], "b.csv")  # website packed it, SnapSTL never made it
    assert ids(client, "problems") == ["F1"] and order(client, "F1")["flag"] == "past_print_not_made"
    assert client.post("/api/orders/F1/clear-flag").status_code == 200
    assert ids(client, "problems") == [] and ids(client, "ship_it") == ["F1"]
    assert client.post("/api/orders/nope/clear-flag").status_code == 404
    used = {r["feature"]: r["count"] for r in client.get("/api/usage", params={"days": 7}).json()["rows"]}
    assert used["clear_flag"] == 1  # Settings → Usage


def test_flagged_or_reused_orders_never_go_into_a_batch(client):
    upload(client, [("X1", "PRINT"), ("X2", "PRINT")])
    with db.tx() as con:
        db.set_group(con, "X1", "flag", flag="cancel_request", flag_note="Customer wants to cancel")
    assert client.get("/api/orders/ids", params={"tab": "all"}).json() == ["X2"]
    r = client.post("/api/batches", json={"order_ids": ["X1"]})
    assert r.status_code == 400


def test_use_a_returned_piece(client):
    # R1 came back (RTO_DELIVERED); R2 is on its way back; N1 / N2 are new orders for "Asha" in pink
    upload(client, [("R1", "RTO_DELIVERED"), ("R2", "RTO_INITIATED"), ("N1", "PRINT"), ("N2", "PRINT")])
    with db.tx() as con:  # a returned piece must match exactly, letter case included
        con.execute("INSERT INTO orders(order_id, sku, source_status, name, color, state, first_status) "
                    "VALUES ('R3', 'Magicians_35', 'RTO_DELIVERED', 'ASHA', 'pink', 'not_needed', 'RTO_DELIVERED')")
    n1 = order(client, "N1")
    assert [(x["order_id"], x["back"]) for x in n1["returned"]] == [("R1", True), ("R2", False)]
    assert sorted(ids(client, "to_make_returned")) == ["N1", "N2"]
    # only a piece that is back with us, and that matches
    assert client.post("/api/orders/N1/use-returned", json={"rto_order_id": "R3"}).status_code == 409
    assert client.post("/api/orders/N1/use-returned", json={"rto_order_id": "R1"}).status_code == 200
    assert order(client, "N1")["tab"] == "pack_it" and order(client, "R1")["reused_by"] == "N1"
    # the piece is used once: N2 no longer sees it
    assert [x["order_id"] for x in order(client, "N2")["returned"]] == ["R2"]
    assert client.post("/api/orders/N2/use-returned", json={"rto_order_id": "R1"}).status_code == 409
    assert "N1" not in client.get("/api/orders/ids", params={"tab": "all"}).json()
    # undo: N1 back to New orders, the piece free again
    assert client.post("/api/orders/N1/undo-returned").status_code == 200
    assert order(client, "N1")["tab"] == "to_make" and order(client, "R1")["reused_by"] is None
    assert client.post("/api/orders/N1/undo-returned").status_code == 409


def test_make_anyway_on_a_delivered_order(client):
    upload(client, [("M1", "DELIVERED")])
    assert client.post("/api/orders/M1/reset").status_code == 200
    o = order(client, "M1")
    assert o["tab"] == "to_make" and o["remake_at"] and o["remake_reason"]


def test_oldest_first_payment_and_select_oldest(client):
    """0.25: the backlog is listed oldest first (Express still on top), with the payment type from the report."""
    upload(client, [("O1", "PRINT"), ("O2", "PRINT"), ("O3", "PRINT")])
    with db.tx() as con:
        for oid, d in (("O1", "2026-11-10"), ("O2", "2026-11-01"), ("O3", "2026-11-05")):
            con.execute("UPDATE orders SET order_date=? WHERE order_id=?", (d, oid))
        con.execute("INSERT OR REPLACE INTO report_rows(order_id, data, status_at, file, sent) VALUES "
                    "('O3', '{\"payment_method\": \"COD\"}', '@x', 'r.csv', 1)")
    assert ids(client, "to_make") == ["O2", "O3", "O1"]
    assert client.get("/api/orders/ids", params={"tab": "to_make"}).json() == ["O2", "O3", "O1"]
    assert order(client, "O3")["payment"] == "COD" and order(client, "O1")["payment"] == ""
    assert ids(client, "all", f_payment="cod") == ["O3"]
    with db.tx() as con:
        con.execute("UPDATE orders SET express_by='2026-12-01' WHERE order_id='O1'")
    assert ids(client, "to_make") == ["O1", "O2", "O3"]
