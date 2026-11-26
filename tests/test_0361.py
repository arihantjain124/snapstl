"""0.36.1: Create batch downloads and builds by itself; Build together puts small batches waiting with their models
ready into one batch per product (fuller plates)."""
import pytest

from snapstl import db

from .test_farm import report
from .test_flow import build_and_wait, client, helper_work, status, wait_for  # noqa: F401  (client is a fixture)


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def held_batch(c, oid, name):
    """A one-name batch left waiting at models ready (as made before 0.36.1)."""
    data = report([(oid, "PRINT", "Magicians_35", name, "Pink", 1)])
    assert c.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    bid = c.post("/api/batches", json={"order_ids": [oid]}).json()["batch_id"]
    with db.tx() as con:
        con.execute("UPDATE batches SET auto_build=0 WHERE id=?", (bid,))
    helper_work(c)
    wait_for(lambda: status(c, bid) == "models_ready")
    return bid


def test_build_together(client):  # noqa: F811
    assert client.post("/api/batches/combine").status_code == 400  # nothing waiting
    b1 = held_batch(client, "BT1", "Zuri")
    b2 = held_batch(client, "BT2", "Noor")
    r = client.post("/api/batches/combine")
    assert r.status_code == 200, r.text
    c = r.json()["combined"]
    assert len(c) == 1 and c[0]["from"] == [b1, b2] and c[0]["orders"] == 2 and c[0]["download_error"] == ""
    assert status(client, b1) == "cancelled" and status(client, b2) == "cancelled"
    b = build_and_wait(client, c[0]["batch_id"])  # nothing to download again: it builds by itself
    assert b["status"] == "done", b["summary"].get("errors")
    bases = [p for p in b["summary"]["plates"] if p["kind"] == "bases"]
    assert len(bases) == 1 and sorted(bases[0]["names"]) == ["Noor", "Zuri"]  # one plate instead of two
    with db.tx() as con:
        states = {r[0]: r[1] for r in con.execute("SELECT order_id, state FROM orders WHERE order_id LIKE 'BT%'")}
    assert states == {"BT1": "bundled", "BT2": "bundled"}
