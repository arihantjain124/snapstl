"""0.28: no double merges, Remove on every plate, merge ideas only for printed bases, external spool switch,
other products in the parcel, spare pieces in stock."""
import json

import pytest

from snapstl import db, extras, farm, spares

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)
from .test_ingest import report
from .test_verify import BASE, PINK



@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    """These tests leave made orders, spare pieces and report rows behind: start the next test empty."""
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


# ---------------- merging ----------------


def test_merge_ideas_wait_for_bases_and_a_second_merge_is_refused(client, bb):
    b1, _ = built_batch(client, "MA")
    b2, _ = built_batch(client, "MB")
    assert client.get("/api/makeit").json()["merge"] == []  # bases not printed yet: no idea offered
    for b in (b1, b2):
        assert client.post("/api/makeit/by-hand", json={"batch_id": b, "file": BASE}).status_code == 200
    sug = {s["color"]: s for s in client.get("/api/makeit").json()["merge"]}
    assert set(sug) == {"pink", "blue"}
    plates = [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]
    assert client.post("/api/makeit/merge", json={"plates": plates}).status_code == 200
    r = client.post("/api/makeit/merge", json={"plates": plates})  # pressed again (the page hadn't reloaded)
    assert r.status_code == 409
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM extra_plates WHERE kind='merged' AND state='active'").fetchone()[0]
    assert n == 1


def _copy_of_merged(con, bid, file):
    """What a double press before 0.28 left: a second merged plate with the same names."""
    x = dict(con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND file=?", (bid, file)).fetchone())
    copy = file.replace(".3mf", " (2).3mf")
    extras._record(con, bid, copy, "merged", "names", x["color"], json.loads(x["items"]), {}, x["names"])
    return copy


def test_a_copy_is_shown_refused_and_removed_once(client, bb):
    b1, _ = built_batch(client, "MC")
    b2, _ = built_batch(client, "MD")
    plates = [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]
    merged = client.post("/api/makeit/merge", json={"plates": plates}).json()["file"]
    with db.tx() as con:
        copy = _copy_of_merged(con, b2, merged)
    client.post("/api/makeit/by-hand", json={"batch_id": b2, "file": merged})  # the first one is printed
    m = client.get("/api/makeit").json()
    assert m["duplicates"] == [{"batch_id": b2, "file": copy, "of": {"batch_id": b2, "file": merged}}]
    client.get("/api/farm")
    pid = farm.printers()[0]["id"]
    r = client.post("/api/makeit/start", json={"batch_id": b2, "file": copy, "printer_id": pid})
    assert r.status_code >= 400 and "copy" in r.text
    with db.tx() as con:
        con.execute("DELETE FROM sync_state WHERE key='repair_028'")
    assert extras.repair_028() == 1 and extras.repair_028() == 0  # one time
    m = client.get("/api/makeit").json()
    assert m["duplicates"] == [] and copy not in [r["file"] for r in m["names"]]
    assert copy in [r["file"] for r in m["removed"]]


def test_copies_printed_twice_add_no_spare_letters(client, bb):
    b1, _ = built_batch(client, "ME")
    b2, _ = built_batch(client, "MF")
    plates = [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]
    merged = client.post("/api/makeit/merge", json={"plates": plates}).json()["file"]
    with db.tx() as con:
        copy = _copy_of_merged(con, b2, merged)
        con.execute("DELETE FROM sync_state WHERE key='repair_028'")
    for f in (merged, copy):
        client.post("/api/makeit/by-hand", json={"batch_id": b2, "file": f})
    assert extras.repair_028() == 0  # letters aren't kept: nothing goes to Spare pieces (0.28.1)
    assert client.get("/api/spares").json()["stock"] == []


def test_remove_works_on_a_merged_plate(client, bb):
    b1, _ = built_batch(client, "MG")
    b2, _ = built_batch(client, "MH")
    plates = [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]
    merged = client.post("/api/makeit/merge", json={"plates": plates}).json()["file"]
    assert client.post("/api/makeit/remove", json={"batch_id": b2, "file": merged}).status_code == 200
    m = client.get("/api/makeit").json()
    assert merged not in [r["file"] for r in m["names"]] and merged in [r["file"] for r in m["removed"]]
    assert client.post("/api/makeit/remove", json={"batch_id": b2, "file": merged, "put_back": True}).status_code == 200
    assert merged in [r["file"] for r in client.get("/api/makeit").json()["names"]]


# ---------------- Start print (0.28.1) ----------------
def test_start_print_is_never_shown_held_while_it_is_being_started(client, bb, monkeypatch):
    from .test_farm import wait_for

    client.get("/api/farm")
    bid, _ = built_batch(client, "SP")
    bb.slicer = "ok"
    seen, real = [], farm.start_job

    def start(job_id, force=False):  # what Print would show at that moment
        with db.tx() as con:
            seen.append(con.execute("SELECT held FROM print_jobs WHERE id=?", (job_id,)).fetchone()[0])
        return real(job_id, force)

    monkeypatch.setattr(farm, "start_job", start)
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": BASE, "printer_id": 1}).status_code == 200
    wait_for(lambda: seen)
    assert seen == [0]
    row = next(r for r in client.get("/api/makeit").json()["bases"] if r["file"] == BASE)
    assert row["job_active"] and not row["job"]["held"]


def test_start_print_twice_at_once_sends_the_plate_once(client, bb):
    import threading

    from .test_farm import wait_for

    client.get("/api/farm")
    bid, _ = built_batch(client, "SQ")
    bb.slicer = "ok"
    codes = []
    go = lambda: codes.append(client.post("/api/makeit/start",  # noqa: E731
                                          json={"batch_id": bid, "file": BASE, "printer_id": 1}).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 400]  # the second: "Already on its way"
    wait_for(lambda: bb.queue)
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM print_jobs WHERE plate=?", (BASE,)).fetchone()[0] == 1


def test_a_cancelled_copy_never_hides_the_plate_printing(client, bb):
    from .test_farm import wait_for

    client.get("/api/farm")
    bid, _ = built_batch(client, "SR")
    bb.slicer = "ok"
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": BASE, "printer_id": 1}).status_code == 200
    wait_for(lambda: bb.queue)
    bb.queue[1]["status"] = "printing"
    farm.WORKER.refresh()
    with db.tx() as con:  # what a double click before 0.28.1 left: a second job, cancelled in Bambuddy
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,printer_id,printer_name,state,created_at,updated_at,"
                    "computer_id) SELECT batch_id,plate,kind,printer_id,printer_name,'cancelled',created_at,updated_at,"
                    "computer_id FROM print_jobs WHERE id=1")
    row = next(r for r in client.get("/api/makeit").json()["bases"] if r["file"] == BASE)
    assert row["job_active"] and row["job"]["state"] == "printing"
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": BASE, "printer_id": 1})
    assert r.status_code == 400 and "on its way" in r.text
    # a copy queued behind it is never sent to Bambuddy
    with db.tx() as con:
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,printer_id,printer_name,state,autostart,created_at,"
                    "updated_at,computer_id) SELECT batch_id,plate,kind,printer_id,printer_name,'queued',1,created_at,"
                    "updated_at,computer_id FROM print_jobs WHERE id=1")
    farm.WORKER.kick()
    j = wait_for(lambda: (lambda x: x if x["state"] != "queued" else None)(
        db.rows(db.connect(), "SELECT state, error FROM print_jobs ORDER BY id DESC LIMIT 1")[0]))
    assert j["state"] == "cancelled" and "pressed twice" in j["error"] and len(bb.queue) == 1


# ---------------- external spool ----------------
def test_external_spool_only_when_switched_on_for_a_printer_with_an_ams(client, bb):
    from .test_farm import tray

    bb.status[2]["vt_tray"] = [tray(254, "FFFFFF")]
    client.get("/api/farm")
    slots = lambda pid: [s["slot"] for s in next(p for p in farm.printers() if p["id"] == pid)["spools"]]  # noqa: E731
    assert "External" not in slots(2)  # has an AMS: the external spool isn't offered
    assert slots(1) == ["External"]  # no AMS: the external spool is all it has
    assert client.put("/api/printers/external", json={"printer_id": 2, "on": True}).json()["external"] is True
    assert "External" in slots(2)
    client.put("/api/printers/external", json={"printer_id": 2, "on": False})
    assert "External" not in slots(2)


# ---------------- packing page: other products ----------------
def test_packing_page_shows_other_products_of_the_parcel(client):
    assert client.post("/api/ingest", files={"file": ("r.csv", report([("Q1", "PRINT", "Magicians_35", "Ayra", "Pink", 1)]),
                                                     "text/csv")}).status_code == 200
    with db.tx() as con:
        con.execute("UPDATE orders SET clubbed_id='CL1', state='bundled', print_started_at=1, made_at=2 WHERE order_id='Q1'")
        for oid, sku, code, qty, st in (("Q2", "Magicians_77", "Shopdeck-3D-Decor-Radium-Boo-Ghosts-Peace-Boo", "2", "PRINT"),
                                        ("Q3", "Stickers_005", "Shopdeck-Stickers-Cat", "1", "CANCELLED")):
            con.execute("INSERT INTO shop_lines(order_id, data, status_at, file, sent) VALUES (?,?,?,?,1)",
                        (oid, json.dumps({"Order ID": oid, "Clubbed Order ID": "CL1", "SKU Code": sku,
                                          "Product Code": code, "Quantity": qty, "Order Status": st}), "x", "r.csv"))
    g = client.get("/api/pack/list").json()["groups"]
    assert g[0]["also"] == [{"product": "3D Decor Radium Boo Ghosts Peace Boo", "quantity": 2}]  # not the cancelled one
    assert client.get("/api/logistics/order/Q1").json()["also"] == g[0]["also"]


# ---------------- spare pieces ----------------
def _orders(c, tab):
    return {o["order_id"]: o for o in c.get(f"/api/orders?tab={tab}&limit=500").json()["orders"]}


def test_a_spare_piece_is_matched_used_and_undone(client):
    assert client.post("/api/ingest", files={"file": ("r.csv", report([
        ("S1", "PRINT", "Magicians_35", "Kiara", "Pink", 1),
        ("S2", "PRINT", "Magicians_35", "Kiara", "Blue", 1),
        ("S3", "PRINT", "Magicians_35", "Mehr", "Pink", 1)]), "text/csv")}).status_code == 200
    r = client.post("/api/spares", json={"name": "Kiara", "color": "Pink", "part": "both", "note": "shelf 2"})
    uid = r.json()["added"][0]["uid"]
    assert client.post("/api/spares", json={"name": "Mehr", "color": "pink", "part": "names"}).status_code == 400
    client.post("/api/spares", json={"name": "Mehr", "color": "green", "part": "bases"})  # a base fits any colour
    client.post("/api/spares", json={"name": "kiara", "color": "blue", "part": "both"})  # other capitals: no match
    o = _orders(client, "to_make_returned")
    assert set(o) == {"S1", "S3"} and o["S1"]["spares"][0]["complete"] and not o["S3"]["spares"][0]["complete"]
    assert client.get("/api/orders?tab=to_make").json()["in_stock"] == 2
    assert client.get("/api/spares").json()["stock"][0]["fits"] in (["S1"], ["S3"], [])
    # a batch leaves out the order with a complete piece in stock
    b = client.post("/api/batches", json={"order_ids": ["S1", "S3"]}).json()
    assert b["orders"] == 1 and b["in_stock"] == 1
    assert client.post("/api/orders/S3/use-spare", json={"uid": uid}).status_code == 409  # not its piece
    assert client.post("/api/orders/S1/use-spare", json={"uid": uid}).status_code == 200
    assert "S1" in _orders(client, "pack_it")
    assert client.post("/api/orders/S1/use-spare", json={"uid": uid}).status_code == 409  # used already
    sp = client.get("/api/spares").json()
    assert [s["used_by"] for s in sp["used"]] == ["S1"]
    assert client.post("/api/orders/S1/undo-returned").status_code == 200
    assert "S1" in _orders(client, "to_make") and uid in [s["uid"] for s in client.get("/api/spares").json()["stock"]]
    # take out of stock and put back
    assert client.post(f"/api/spares/{uid}/remove", json={}).status_code == 200
    assert "S1" not in _orders(client, "to_make_returned")
    client.post(f"/api/spares/{uid}/remove", json={"put_back": True})
    assert "S1" in _orders(client, "to_make_returned")
    assert client.post("/api/spares", json={"name": " ", "part": "both"}).status_code == 400


def test_spares_table_is_in_the_shared_records_schema():
    from snapstl import sync

    sql = sync.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS snapstl.spares" in sql and "'spares'] LOOP" in sql
    assert spares.match_sql(complete=True).count("part = 'both'") == 1


def test_a_base_fits_an_order_of_any_letter_colour(client):
    assert client.post("/api/ingest", files={"file": ("r.csv", report([
        ("B1", "PRINT", "Magicians_35", "Riya", "Blue", 1), ("B2", "PRINT", "Magicians_35", "Riya", "Pink", 1)]),
        "text/csv")}).status_code == 200
    s = client.post("/api/spares", json={"name": "Riya", "color": "red", "part": "bases"}).json()["added"][0]
    assert s["color"] == ""  # a base has no letters' colour
    assert set(_orders(client, "to_make_returned")) == {"B1", "B2"}
    assert sorted(client.get("/api/spares").json()["stock"][0]["fits"]) == ["B1", "B2"]
    assert client.post("/api/orders/B1/use-spare", json={"uid": s["uid"]}).status_code == 409  # base only: print letters
