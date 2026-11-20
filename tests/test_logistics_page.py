"""0.23 phase 4: the logistics page (label and customer-care team): lists, search, actions, usage."""
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
    assert c.post("/api/ingest", files={"file": (name, report(rows), "text/csv")}).status_code == 200


def lst(c, view="labels", q=""):
    return [o["order_id"] for o in c.get("/api/logistics/orders", params={"view": view, "q": q}).json()["orders"]]


def detail(c, oid):
    r = c.get(f"/api/logistics/order/{oid}")
    assert r.status_code == 200, r.text
    return r.json()


def act(c, oid, what, body=None):
    return c.post(f"/api/logistics/order/{oid}/{what}", json=body or {})


def made(*ids, **extra):
    with db.tx() as con:
        for i in ids:
            con.execute("UPDATE orders SET state='bundled', print_started_at=1, made_at=2 WHERE order_id=?", (i,))
            for k, v in extra.items():
                con.execute(f"UPDATE orders SET {k}=? WHERE order_id=?", (v, i))


def test_label_generated_and_undo(client):
    upload(client, [("L1", "PRINT"), ("L2", "PRINT")])
    made("L1")
    assert lst(client) == ["L1"]
    assert act(client, "L2", "label").status_code == 409  # not printed yet
    assert act(client, "L1", "label").status_code == 200
    o = detail(client, "L1")
    assert o["tab"] == "ship_it" and o["label_by"] == "logistics" and o["words"].startswith("Packed")
    assert lst(client) == [] and lst(client, "ship_it") == ["L1"]
    assert act(client, "L1", "label", {"undo": True}).status_code == 200
    assert detail(client, "L1")["tab"] == "pack_it"
    assert act(client, "L1", "label", {"undo": True}).status_code == 409


def test_one_label_for_a_clubbed_order(client):
    upload(client, [("C1", "PRINT"), ("C2", "PRINT")])
    with db.tx() as con:
        con.execute("UPDATE orders SET clubbed_id='G1' WHERE order_id IN ('C1','C2')")
    made("C1")
    r = act(client, "C1", "label")
    assert r.status_code == 409 and "C2" in r.json()["detail"]
    made("C2")
    assert act(client, "C1", "label").status_code == 200
    assert detail(client, "C2")["tab"] == "ship_it"  # the whole parcel
    assert [g["order_id"] for g in detail(client, "C1")["group"]] == ["C2"]


def test_express(client):
    upload(client, [("E1", "PRINT"), ("E2", "DELIVERED")])
    assert act(client, "E1", "express", {"needed_by": "25-11-2026"}).status_code == 400
    assert act(client, "E1", "express", {"needed_by": "2026-11-25", "note": "birthday"}).status_code == 200
    o = detail(client, "E1")
    assert (o["express_by"], o["express_note"]) == ("2026-11-25", "birthday")
    assert act(client, "E2", "express", {"needed_by": "2026-11-25"}).status_code == 409  # already delivered
    assert act(client, "E1", "express", {"needed_by": ""}).status_code == 200
    assert detail(client, "E1")["express_by"] is None


def test_name_change_confirmed_with_the_customer(client):
    upload(client, [("N1", "PRINT"), ("N2", "PRINT")])
    with db.tx() as con:
        con.execute("UPDATE orders SET state='needs_review', review_reason='Name has a space' WHERE order_id='N2'")
    assert act(client, "N1", "name", {"name": "Riyaa", "color": "pink", "reason": "asked"}).status_code == 400
    assert act(client, "N1", "name", {"name": "Riyaa", "color": "pink", "confirmed": True}).status_code == 400
    assert act(client, "N1", "name", {"name": "Riyaa", "color": "pink", "reason": "Customer: Riya → Riyaa",
                                      "confirmed": True}).status_code == 200
    o = detail(client, "N1")
    assert o["name"] == "Riyaa" and o["name_change_reason"] == "Customer: Riya → Riyaa" and o["name_confirmed_at"]
    # Check name first → Checked with customer → New orders
    assert act(client, "N2", "name", {"name": "Anu", "color": "pink", "reason": "Customer: first name only",
                                      "confirmed": True}).status_code == 200
    assert detail(client, "N2")["tab"] == "to_make"
    made("N1")
    assert act(client, "N1", "name", {"name": "X", "color": "pink", "reason": "r", "confirmed": True}).status_code == 409


def test_notes_cancel_request_and_remake(client):
    upload(client, [("R1", "PRINT"), ("R2", "DELIVERED")])
    assert act(client, "R1", "note", {"text": "  "}).status_code == 400
    assert act(client, "R1", "note", {"text": "Customer called"}).status_code == 200
    assert [n["text"] for n in detail(client, "R1")["notes"]] == ["Customer called"]
    assert act(client, "R1", "cancel-request", {"note": "ordered twice"}).status_code == 200
    o = detail(client, "R1")
    assert o["flag"] == "cancel_request" and o["tab"] == "problems" and "ordered twice" in o["flag_note"]
    assert lst(client, "problems") == ["R1"]
    assert "R1" not in client.get("/api/orders/ids", params={"tab": "all"}).json()  # can't be batched now
    # delivered but broken: make it again
    assert act(client, "R2", "remake").status_code == 400  # needs a reason
    assert act(client, "R2", "remake", {"reason": "arrived broken"}).status_code == 200
    o = detail(client, "R2")
    assert o["tab"] == "to_make" and o["remake_reason"] == "arrived broken" and lst(client, "remakes") == ["R2"]
    assert act(client, "R2", "remake", {"reason": "again"}).status_code == 409  # being made already


def test_search_by_the_name_to_print(client):
    from snapstl.ingest import set_name

    upload(client, [("N1", "PRINT"), ("N2", "PRINT")])
    set_name("N2", "Esha")  # corrected after a call: the name the customer typed still finds the order
    assert sorted(lst(client, q="asha")) == ["N1", "N2"]  # any capitals, as the customer typed it
    assert lst(client, q="ESH") == ["N2"]  # part of the corrected name
    assert lst(client, q="nobody") == []


def test_search_late_and_tracking(client, monkeypatch):
    from snapstl import server

    upload(client, [("S1", "PRINT"), ("S2", "IN-TRANSIT")])
    with db.tx() as con:
        con.execute("UPDATE orders SET phone='+91 98765-43210', customer_name='Meera Rao', order_date='2026-01-01' "
                    "WHERE order_id='S1'")
        con.execute("INSERT OR REPLACE INTO report_rows(order_id, data, status_at, file, sent) VALUES "
                    "('S2', '{\"awb\": \"90739741525\", \"shipping_partner\": \"Blue Dart AIR\", "
                    "\"pickup_date\": \"2026-01-02 10:00:00\"}', '@x', 'r.csv', 1)")
    assert lst(client, q="9876543210") == ["S1"] and lst(client, q="meera") == ["S1"] and lst(client, q="S2") == ["S2"]
    s1 = detail(client, "S1")
    assert s1["late"] and s1["tracking"] == "" and s1["words"].startswith("In queue")
    s2 = detail(client, "S2")
    assert s2["tracking"] == "https://shopdeck.clickpost.ai/en?waybill=90739741525&cp_id=105"
    assert s2["stuck"] and s2["words"] == "Shipped with Blue Dart AIR"
    monkeypatch.setattr(server.SETTINGS.logistics, "tracking_url", "https://example.test/t/{order_id}")
    assert detail(client, "S2")["tracking"] == "https://example.test/t/S2"
    csv = client.get("/api/logistics/orders.csv", params={"view": "being_made"}).text
    assert csv.splitlines()[0].startswith("order_id,clubbed_id,order_date") and "S1" in csv


def test_usage_counts(client):
    upload(client, [("U1", "PRINT")])
    assert client.post("/api/usage", json={"feature": "copy_tracking_link"}).status_code == 200
    assert client.post("/api/usage", json={"feature": "Bad Name!"}).status_code == 400
    act(client, "U1", "note", {"text": "hi"})
    rows = {r["feature"]: r["count"] for r in client.get("/api/usage").json()["rows"]}
    assert rows["copy_tracking_link"] == 1 and rows["note_added"] == 1


def test_logistics_settings(client):
    from snapstl import server

    s = client.get("/api/settings").json()
    assert s["logistics"]["tracking_url"].startswith("https://shopdeck.clickpost.ai/") and "Shipped – tracking" in s["logistics"]["messages"]
    good = {**s, "logistics": {**s["logistics"], "tracking_url": "https://store.test/track/{order_id}", "late_days": 5,
                               "messages": {"Hi": "Hello {customer}"}}}
    r = client.put("/api/settings", json=good)
    assert r.status_code == 200, r.text
    assert server.SETTINGS.logistics.late_days == 5 and server.SETTINGS.logistics.messages == {"Hi": "Hello {customer}"}
    assert client.get("/api/logistics/meta").json()["late_days"] == 5
    for bad in ({"tracking_url": "http://store.test/{awb}"}, {"tracking_url": "https://store.test/no-placeholder"},
                {"late_days": 99}, {"stuck_days": "x"}, {"messages": {"": "text"}}):
        assert client.put("/api/settings", json={**s, "logistics": {**s["logistics"], **bad}}).status_code == 400, bad
    assert client.put("/api/settings", json=s).status_code == 200  # back to the defaults


def test_queue_ready_around_from_the_recent_pace(client):
    """0.25: a waiting order says when it should be ready: its place in the queue (Express first, then oldest)
    divided by the orders finished per day over the last week."""
    import math
    import time
    from datetime import date, timedelta

    upload(client, [("Q1", "PRINT"), ("Q2", "PRINT"), ("Q3", "PRINT"), ("M1", "PRINT"), ("M2", "PRINT")])
    with db.tx() as con:
        for oid, d in (("Q1", "2026-11-01"), ("Q2", "2026-11-02"), ("Q3", "2026-11-03")):
            con.execute("UPDATE orders SET order_date=? WHERE order_id=?", (d, oid))
        # two finished, the first 6.5 days ago → 2 per 6.5 days
        con.execute("UPDATE orders SET state='bundled', made_at=? WHERE order_id IN ('M1','M2')", (time.time() - 6.5 * 86400,))
    q = {o["order_id"]: o for o in client.get("/api/logistics/orders", params={"view": "being_made", "limit": 50}).json()["orders"]}
    days = lambda n: (date.today() + timedelta(days=math.ceil(n * 6.5 / 2))).isoformat()  # noqa: E731
    assert q["Q1"]["ready_by"] == days(1) and q["Q3"]["ready_by"] == days(3)
    assert q["Q1"]["words"].startswith("In queue – ready around ")


def test_packing_page_shows_only_made_orders_and_whole_clubbed_orders(client):
    """0.26 packing page: made (checked) orders only, oldest first; a clubbed order only when every name is made,
    and a search shows the whole clubbed order; an order not ready says where it is."""
    upload(client, [("K1", "PRINT"), ("K2", "PRINT"), ("K3", "PRINT"), ("K4", "PRINT")])
    with db.tx() as con:
        con.execute("UPDATE orders SET clubbed_id='C9' WHERE order_id IN ('K2','K3')")
        con.execute("UPDATE orders SET order_date='2026-11-10' WHERE order_id='K1'")
        con.execute("UPDATE orders SET order_date='2026-11-02' WHERE order_id IN ('K2','K3','K4')")
        con.execute("UPDATE orders SET state='bundled', made_at=1 WHERE order_id IN ('K1','K2','K4')")
        con.execute("UPDATE orders SET state='bundled' WHERE order_id='K3'")  # still being made
        for oid, n in (("K1", "Riya"), ("K2", "Tara"), ("K3", "Zoravar"), ("K4", "Ayra")):
            con.execute("UPDATE orders SET name=?, raw_name=? WHERE order_id=?", (n, n, oid))

    def pk(**p):
        return client.get("/api/pack/list", params=p).json()

    r = pk()
    assert [[o["order_id"] for o in g["orders"]] for g in r["groups"]] == [["K4"], ["K1"]] and r["held_clubbed"] == 1
    with db.tx() as con:
        con.execute("UPDATE orders SET made_at=1 WHERE order_id='K3'")
    r = pk()
    assert [[o["order_id"] for o in g["orders"]] for g in r["groups"]] == [["K2", "K3"], ["K4"], ["K1"]]
    assert act(client, "K2", "label").status_code == 200  # one label covers the clubbed order
    assert [g["key"] for g in pk()["groups"]] == ["K4", "K1"] and [g["key"] for g in pk(view="today")["groups"]] == ["C9"]
    assert [g["key"] for g in pk(q="zorav")["groups"]] == ["C9"]  # a search shows the whole clubbed order
    assert pk(q="K4")["groups"] == [] and pk(q="Private")["groups"] == []  # only the name to print is searched
    upload(client, [("K5", "PRINT")])
    with db.tx() as con:
        con.execute("UPDATE orders SET name='Mehr', raw_name='mehr' WHERE order_id='K5'")
    r = pk(q="mehr")
    assert r["groups"] == [] and r["not_ready"][0]["order_id"] == "K5"
