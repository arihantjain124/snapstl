"""0.34: Pending Orders — one To make list (name checks listed, not tickable), Acceptance pending and AWB expired
orders to make, clubbed orders made together (whole, or none while a name still needs its check)."""
import csv
import io

import pytest

from snapstl import db, ingest, labels

from .test_flow import client  # noqa: F401  (client is a fixture)


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def report(rows) -> bytes:
    """(order id, clubbed id, status, name, date dd-mm-yyyy); colour pink, or none for a name starting with '?'."""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Product Customisation"])
    for oid, club, status, name, date in rows:
        colour = "" if name.startswith("?") else "\nColor: Pink"
        w.writerow([oid, club, date, status, "Magicians_35", 1, "Private Person",
                    f"Enter Your First Name: {name.lstrip('?')}{colour}\n"])
    return f.getvalue().encode()


def upload(c, rows):
    r = c.post("/api/ingest", files={"file": ("r.csv", report(rows), "text/csv")})
    assert r.status_code == 200, r.text


def listed(c, tab="pending"):
    return {o["order_id"]: o for o in c.get("/api/orders", params={"tab": tab, "limit": 500}).json()["orders"]}


def ids(c, tab="pending"):
    return [o["order_id"] for o in c.get("/api/orders", params={"tab": tab, "limit": 500}).json()["orders"]]


def test_acceptance_pending_and_awb_expired_are_to_make(client):  # noqa: F811
    upload(client, [("A1", "", "ACCEPTANCE_PENDING", "Tara", "20-11-2026"), ("A2", "", "AWB EXPIRED", "Mina", "19-11-2026"),
                    ("A3", "", "PRINT", "Rina", "21-11-2026"), ("A4", "", "DELIVERED", "Lia", "18-11-2026")])
    assert ids(client) == ["A2", "A1", "A3"]  # oldest first, all in To make
    assert client.get("/api/orders/facets").json()["tabs"]["problems"] == 0
    # accepted later: still to make; cancelled: not needed
    assert ingest.status_state("ready", "ACCEPTANCE_PENDING", "PRINT", "ready", False) == "ready"
    assert ingest.status_state("ready", "ACCEPTANCE_PENDING", "CANCELLED INITIATED", "ready", False) == "not_needed"
    assert ingest.status_state("not_needed", "ACCEPTANCE_PENDING", "PRINT", "ready", False) == "ready"


def test_repair_034_moves_old_ones_back_once(client, monkeypatch):  # noqa: F811
    upload(client, [("R1", "", "ACCEPTANCE_PENDING", "Tara", "24-11-2026"), ("R2", "", "AWB EXPIRED", "Mina", "14-11-2026"),
                    ("R3", "", "AWB EXPIRED", "Rina", "10-11-2026"), ("R4", "", "ACCEPTANCE_PENDING", "?Lia", "24-11-2026")])
    with db.tx() as con:  # as before 0.34
        con.execute("UPDATE orders SET state='not_needed'")
        con.execute("DELETE FROM sync_state WHERE key='repair_034'")
    assert ids(client) == []
    assert ingest.repair_034() == {"back": 3}  # R3 is from before SnapSTL made orders
    got = listed(client)
    assert sorted(got) == ["R1", "R2", "R4"] and got["R4"]["tab"] == "check_name"
    assert ingest.repair_034() == {"back": 0}


def test_name_checks_are_listed_by_date_but_not_tickable(client):  # noqa: F811
    upload(client, [("C1", "", "PRINT", "Tara", "21-11-2026"), ("C2", "", "PRINT", "?Mina", "20-11-2026")])
    got = listed(client)
    assert list(got) == ["C2", "C1"] and got["C2"]["tab"] == "check_name"
    assert client.get("/api/orders/ids", params={"tab": "pending"}).json() == ["C1"]
    assert client.get("/api/orders/facets").json()["tabs"]["pending"] == 2


def test_clubbed_order_made_whole(client):  # noqa: F811
    upload(client, [("K1", "CL1", "PRINT", "Tara", "20-11-2026"), ("K2", "CL1", "PRINT", "Mina", "20-11-2026"),
                    ("K3", "CL1", "PRINT", "Rina", "20-11-2026"), ("S1", "", "PRINT", "Lia", "19-11-2026")])
    o = listed(client)["K1"]
    assert o["club"] == {"size": 3, "to_make": 3, "check": [], "held": False, "started": 0}
    r = client.post("/api/batches", json={"order_ids": ["K2"]})
    assert r.status_code == 200, r.text
    assert (r.json()["orders"], r.json()["clubbed_added"]) == (3, 2)
    assert sorted(ids(client, "production")) == ["K1", "K2", "K3"]


def test_clubbed_order_waits_for_a_name_check(client):  # noqa: F811
    upload(client, [("K1", "CL1", "PRINT", "Tara", "20-11-2026"), ("K2", "CL1", "PRINT", "?Mina", "20-11-2026"),
                    ("S1", "", "PRINT", "Lia", "21-11-2026")])
    got = listed(client)
    assert got["K1"]["club"]["held"] and got["K1"]["club"]["check"] == ["Mina"]
    assert client.get("/api/orders/ids", params={"tab": "pending"}).json() == ["S1"]
    r = client.post("/api/batches", json={"order_ids": ["K1"]})
    assert r.status_code == 400 and "name check of Mina" in r.text
    r = client.post("/api/batches", json={"order_ids": ["K1", "S1"]})
    assert r.json()["orders"] == 1 and "name check of Mina" in r.json()["clubbed_held"]


def test_select_oldest_never_splits_a_clubbed_order(client):  # noqa: F811
    upload(client, [("S1", "", "PRINT", "Lia", "18-11-2026"), ("K1", "CL1", "PRINT", "Tara", "19-11-2026"),
                    ("K2", "CL1", "PRINT", "Mina", "19-11-2026"), ("S2", "", "PRINT", "Rina", "20-11-2026")])
    two = client.get("/api/orders/ids", params={"tab": "pending", "count": 2}).json()
    assert two == ["S1", "S2"]  # the clubbed order doesn't fit in 2 with S1: the next single one instead
    three = client.get("/api/orders/ids", params={"tab": "pending", "count": 3}).json()
    assert three == ["S1", "K1", "K2"]
    assert client.get("/api/orders/ids", params={"tab": "pending", "count": 1}).json() == ["S1"]


def test_clubbed_order_with_names_in_production_comes_first(client):  # noqa: F811
    upload(client, [("S1", "", "PRINT", "Lia", "18-11-2026"), ("K1", "CL1", "PRINT", "Tara", "20-11-2026"),
                    ("K2", "CL1", "PRINT", "Mina", "20-11-2026")])
    with db.tx() as con:  # made before 0.34: one name went into a batch alone
        con.execute("UPDATE orders SET state='in_batch', batch_id=99 WHERE order_id='K1'")
    got = listed(client)
    assert list(got) == ["K2", "S1"] and got["K2"]["club"]["started"] == 1


def test_no_label_before_acceptance():
    p = {"lines": [{"order_id": "A1", "ours": True, "made": True, "status": "ACCEPTANCE-PENDING", "shown": "Tara"}]}
    assert labels.problems(p) == ["A1 is still waiting for acceptance — accept it on ShopDeck first"]
