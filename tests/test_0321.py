"""0.32.1: no part made twice.

* A plate whose print is done stays done when a cancelled copy of a double press is newer (it came back to
  Bases to print and was printed a second time).
* An order already made and checked Good in an earlier batch is never taken into a new batch by itself
  (it came back to To make and its base and letters were made again) — only after Remake.
* An order leaving its batch is written to Activities.
"""
import pytest

from snapstl import db, farm, makeit, sync

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)
from .test_verify import BASE, BLUE, PINK, _cards, _check, _res


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def test_a_cancelled_copy_never_hides_a_done_print():
    done = {"id": 1, "state": "done"}
    copy = {"id": 2, "state": "cancelled"}
    assert farm.current_job([done, copy]) is done
    assert farm.current_job([done, {"id": 2, "state": "failed"}]) is done
    # a Reprint still brings it back, and the print after it counts
    again = [done, copy, {"id": 3, "state": "reprint"}, {"id": 4, "state": "failed"}]
    assert farm.current_job(again)["id"] == 4
    # unchanged: a cancelled copy doesn't hide one still printing, and newest wins otherwise
    assert farm.current_job([{"id": 1, "state": "printing"}, copy])["id"] == 1
    assert farm.current_job([{"id": 1, "state": "failed"}, {"id": 2, "state": "done"}])["id"] == 2


def test_double_pressed_plate_does_not_come_back_once_printed(client, bb):  # noqa: F811
    bid, _ = built_batch(client, "DB")
    now = db.now()
    with db.tx() as con:  # Start print pressed twice: the first printed, the copy was cancelled
        for state in ("done", "cancelled"):
            con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                        "updated_at,computer_id) VALUES (?,?,'bases','',1,'Base 1',?,?,?,?)",
                        (bid, BASE, state, now, now, makeit.computer.ID))
    m = client.get("/api/makeit").json()
    assert BASE not in [r["file"] for r in m["bases"]]  # not offered to print again
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": BASE, "printer_id": 1}).status_code >= 400


def _made(client, tag):  # noqa: F811
    bid, _ = built_batch(client, tag)
    client.post("/api/staff", json={"name": "Asha"})
    for f in (BASE, PINK, BLUE):
        assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": f}).status_code == 200
    for (b, f), card in _cards(client).items():
        assert _check(client, b, f, _res(card, lambda i: "good")).status_code == 200
    return bid


def _released(oid):
    """What happened to the order in the field: back to To make with its batch and made time gone."""
    with db.tx() as con:
        con.execute("UPDATE orders SET state='ready', batch_id=NULL, batch_uid=NULL, batch_label=NULL, made_at=NULL, "
                    "print_started_at=NULL WHERE order_id=?", (oid,))


def test_an_order_made_before_is_not_batched_again_until_remake(client, bb):  # noqa: F811
    _made(client, "MB")
    _released("MB1")
    o = next(o for o in client.get("/api/orders?tab=to_make&limit=100").json()["orders"] if o["order_id"] == "MB1")
    assert o["made_before"]  # the Orders page says where it was made
    r = client.post("/api/batches", json={"order_ids": ["MB1"]})
    assert r.status_code == 400 and "already made" in r.json()["detail"]
    r = client.post("/api/batches/next", json={"count": 10})
    assert r.status_code == 400  # Make next batch passes over it: nothing else to make
    # Remake on it: recorded, and then it may be made again
    assert client.post("/api/orders/MB1/reset").status_code == 200
    with db.tx() as con:
        assert con.execute("SELECT remake_at FROM orders WHERE order_id='MB1'").fetchone()[0] is not None
    r = client.post("/api/batches", json={"order_ids": ["MB1"]})
    assert r.status_code == 200 and r.json()["orders"] == 1


def test_mixed_selection_leaves_the_made_order_out(client, bb):  # noqa: F811
    _made(client, "MX")
    _released("MX1")
    from .test_ingest import report
    data = report([("MX9", "PRINT", "Magicians_35", "Kiara", "Pink", 1)])
    assert client.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    r = client.post("/api/batches", json={"order_ids": ["MX1", "MX9"]})
    assert r.status_code == 200
    j = r.json()
    assert j["orders"] == 1 and j["already_made"] == 1 and "already made" in j["already_made_note"]


def test_partly_checked_order_is_not_blocked(client, bb):  # noqa: F811
    bid, _ = built_batch(client, "MP")
    client.post("/api/staff", json={"name": "Asha"})
    assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": BASE}).status_code == 200
    for (b, f), card in _cards(client).items():  # only the bases checked
        assert _check(client, b, f, _res(card, lambda i: "good")).status_code == 200
    _released("MP1")
    with db.tx() as con:
        assert makeit.verify.made_before(con, ["MP1"]) == {}


def test_leaving_a_batch_is_written_to_activities(client, bb):  # noqa: F811
    _made(client, "MA")
    assert client.post("/api/orders/MA1/reset").status_code == 200
    # released in the shared records by another computer
    with db.tx() as con:
        L = dict(con.execute("SELECT * FROM orders WHERE order_id='MA2'").fetchone())
        r = {**L, "state": "released", "state_at": db.now() + 5, "updated_by": "OTHER-PC", "computer_id": "x",
             "computer_name": "OTHER-PC"}
        sync.merge(con, r, force=True)  # (the test computer has its own change on the way)
        texts = [t for (t,) in con.execute("SELECT text FROM activity WHERE kind='orders' ORDER BY at")]
    assert any("(MA1) left" in t and "Remake" in t and "after it was made" in t for t in texts)
    assert any("(MA2) left" in t and "OTHER-PC" in t for t in texts)
