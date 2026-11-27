"""0.42: a spare base (base only) for an order — its base leaves its plate, only the letters are printed."""
from pathlib import Path

import pytest

from snapstl import db

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import build_and_wait, client, helper_work, wait_for  # noqa: F401  (client is a fixture)
from .test_ingest import report
from .test_verify import BASE, PINK, _cards, _check, _res, _tab


@pytest.fixture(autouse=True)
def _clean():
    yield
    with db.tx() as con:  # the database is shared by the whole run
        con.execute("UPDATE orders SET state='not_needed' WHERE order_id GLOB 'W[Q-T][0-9]*'")
        con.execute("DELETE FROM spares WHERE note='test 0.42'")


def _spare(c, name):
    r = c.post("/api/spares", json={"name": name, "part": "bases", "note": "test 0.42"})
    assert r.status_code == 200, r.text
    with db.tx() as con:
        return con.execute("SELECT uid FROM spares WHERE name=? AND note='test 0.42' AND used_by IS NULL "
                           "ORDER BY added_at DESC", (name,)).fetchone()[0]


def _bases(c):
    return {r["file"]: r for r in c.get("/api/makeit").json()["bases"]}


def test_a_spare_base_takes_the_base_off_its_plate_and_the_letters_finish_the_order(client, bb):  # noqa: F811
    bid, folder = built_batch(client, "WQ")
    uid = _spare(client, "Ayra")
    row = _bases(client)[BASE]
    assert [(x["order_id"], x["name"], x["used"]) for x in row["spare_bases"]] == [("WQ1", "Ayra", False)]
    r = client.post("/api/orders/WQ1/use-spare-base", json={"uid": uid})
    assert r.status_code == 200, r.text
    bases = _bases(client)
    assert BASE not in bases and len(bases) == 1
    part = next(iter(bases.values()))
    assert part["extra"] == "divided" and part["names"] == ["Riya"]  # the plate again without Ayra's base
    assert client.post("/api/orders/WQ1/use-spare-base", json={"uid": uid}).status_code == 409  # not twice
    # print and check only the letters: the order is made
    client.post("/api/staff", json={"name": "Asha"})
    client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": PINK})
    card = _cards(client)[(bid, PINK)]
    assert [i["order_id"] for i in card["items"]] == ["WQ1"]
    assert _check(client, bid, PINK, _res(card, lambda i: "good")).status_code == 200
    assert _tab(client, "WQ") == {"WQ1": "pack_it", "WQ2": "ready_to_print"}
    with db.tx() as con:
        assert con.execute("SELECT used_by FROM spares WHERE uid=?", (uid,)).fetchone()[0] == "WQ1"
    assert client.post("/api/orders/WQ1/undo-spare-base", json={}).status_code == 409  # made: Completed instead


def test_a_plate_with_only_that_base_leaves_the_list_and_undo_prints_it_again(client, bb):  # noqa: F811
    bid, folder = built_batch(client, "WR")
    r = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE, "sizes": [1, 1]})
    parts = r.json()["files"]
    uid = _spare(client, "Riya")
    assert client.post("/api/orders/WR2/use-spare-base", json={"uid": uid}).status_code == 200
    assert set(_bases(client)) == {parts[0]}  # Riya's own part left the list (nothing else on it)
    r = client.post("/api/orders/WR2/undo-spare-base", json={})
    assert r.status_code == 200, r.text
    fix = r.json()["fixes"][0]
    bases = _bases(client)
    assert bases[fix]["extra"] == "fix" and bases[fix]["names"] == ["Riya"]
    with db.tx() as con:
        assert con.execute("SELECT used_by FROM spares WHERE uid=?", (uid,)).fetchone()[0] is None  # back in stock


def test_a_spare_base_kept_for_an_order_not_in_a_batch_yet(client, bb):  # noqa: F811
    data = report([("WS1", "PRINT", "Magicians_35", "Ayra", "Pink", 1), ("WS2", "PRINT", "Magicians_35", "Riya", "Blue", 1)])
    assert client.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    uid = _spare(client, "Ayra")
    o = next(x for x in client.get("/api/orders?tab=to_make&limit=200").json()["orders"] if x["order_id"] == "WS1")
    assert o["spare_base"] == {"uid": uid, "used": False}
    r = client.post("/api/orders/WS1/use-spare-base", json={"uid": uid})
    assert r.status_code == 200 and "once it is in a batch" in r.json()["what"]
    bid = client.post("/api/batches", json={"order_ids": ["WS1", "WS2"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download")
    helper_work(client)
    assert build_and_wait(client, bid)["status"] == "done"
    bases = wait_for(lambda: (b := _bases(client)) and BASE not in b and b)  # applied right after the build
    assert BASE not in bases and [r["names"] for r in bases.values()] == [["Riya"]]
    with db.tx() as con:
        assert con.execute("SELECT result, note FROM plate_checks WHERE order_id='WS1' AND part='bases'").fetchone()[:] \
            == ("good", "spare base")


def test_refused_once_the_base_is_printed(client, bb):  # noqa: F811
    bid, folder = built_batch(client, "WT")
    uid = _spare(client, "Ayra")
    assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": BASE}).status_code == 200
    r = client.post("/api/orders/WT1/use-spare-base", json={"uid": uid})
    assert r.status_code == 409 and "already printing or printed" in r.json()["detail"]
    with db.tx() as con:
        assert con.execute("SELECT used_by FROM spares WHERE uid=?", (uid,)).fetchone()[0] is None
    assert Path(folder / BASE).is_file()
