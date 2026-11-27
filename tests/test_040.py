"""0.40: any plate merges (split parts, merged plates again), labels printed outside SnapSTL, English-only board."""
import re

import pytest

from snapstl import db, verify
from snapstl.threemf import read_3mf

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)
from .test_verify import BASE, PINK, _cards, _check, _res, _tab


@pytest.fixture(autouse=True)
def _clean():
    """The test database is shared by the whole run: take this file's orders (QA… QE…) off the lists after each
    test, so later tests see an empty Completed / Print."""
    yield
    with db.tx() as con:
        con.execute("UPDATE orders SET state='not_needed' WHERE order_id GLOB 'Q[A-E][0-9]*'")


def _names(c, kind="names"):
    return {(r["batch_id"], r["file"]): r for r in c.get("/api/makeit").json()[kind]}


def test_a_merged_letter_plate_merges_again_and_splits_back_step_by_step(client, bb):  # noqa: F811
    b1, _ = built_batch(client, "QA")
    b2, _ = built_batch(client, "QB")
    b3, f3 = built_batch(client, "QC")
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]})
    assert r.status_code == 200, r.text
    m1 = (r.json()["batch_id"], r.json()["file"])
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": m1[0], "file": m1[1]},
                                                          {"batch_id": b3, "file": PINK}]})
    assert r.status_code == 200, r.text  # before 0.40: a merged plate could not be merged again
    m2 = (r.json()["batch_id"], r.json()["file"])
    assert len(read_3mf(f3 / m2[1])) == 3
    n = _names(client)
    assert m2 in n and m1 not in n and (b3, PINK) not in n and (b1, PINK) not in n
    assert client.get("/api/makeit").json()["duplicates"] == []
    # Split back returns the merged plate as it was, then that one splits back into the single plates
    assert client.post("/api/makeit/split", json={"batch_id": m2[0], "file": m2[1]}).status_code == 200
    n = _names(client)
    assert m1 in n and (b3, PINK) in n and m2 not in n and (b1, PINK) not in n
    assert client.post("/api/makeit/split", json={"batch_id": m1[0], "file": m1[1]}).status_code == 200
    n = _names(client)
    assert (b1, PINK) in n and (b2, PINK) in n and m1 not in n
    # merge twice again, print the 3-name plate: one check makes all three orders
    m1 = client.post("/api/makeit/merge", json={"plates": [{"batch_id": b1, "file": PINK},
                                                           {"batch_id": b2, "file": PINK}]}).json()
    m2 = client.post("/api/makeit/merge", json={"plates": [{"batch_id": m1["batch_id"], "file": m1["file"]},
                                                           {"batch_id": b3, "file": PINK}]}).json()
    client.post("/api/staff", json={"name": "Asha"})
    for b in (b1, b2, b3):
        assert client.post("/api/makeit/by-hand", json={"batch_id": b, "file": BASE}).status_code == 200
    assert client.post("/api/makeit/by-hand", json={"batch_id": m2["batch_id"], "file": m2["file"]}).status_code == 200
    card = _cards(client)[(m2["batch_id"], m2["file"])]
    assert sorted(i["order_id"] for i in card["items"]) == ["QA1", "QB1", "QC1"]
    assert _check(client, m2["batch_id"], m2["file"], _res(card, lambda i: "good")).status_code == 200
    for b in (b1, b2, b3):
        _check(client, b, BASE, _res(_cards(client)[(b, BASE)], lambda i: "good"))
    assert all(_tab(client, t)[t + "1"] == "pack_it" for t in ("QA", "QB", "QC"))


def test_split_parts_of_a_base_plate_merge(client, bb):  # noqa: F811
    bid, folder = built_batch(client, "QD")
    parts = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE, "sizes": [1, 1]}).json()["files"]
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": bid, "file": f} for f in parts]})
    assert r.status_code == 200, r.text  # before 0.40: "Only letter plates and base plates (and their fix plates)"
    merged = r.json()["file"]
    assert len(read_3mf(folder / merged)) == 2
    bases = _names(client, "bases")
    assert (bid, merged) in bases and not any((bid, f) in bases for f in parts)
    assert client.post("/api/makeit/join", json={"batch_id": bid, "file": parts[0]}).status_code == 409  # merged
    assert client.post("/api/makeit/split", json={"batch_id": bid, "file": merged}).status_code == 200
    assert {f for (_, f) in _names(client, "bases")} == set(parts)
    # merge ideas list split parts too (they have footprints now)
    assert any({p["file"] for p in s["plates"]} == set(parts) for s in client.get("/api/makeit").json()["merge_bases"])


def test_label_printed_outside_snapstl_and_undo(client, bb):  # noqa: F811
    bid, _ = built_batch(client, "QE")
    client.post("/api/staff", json={"name": "Asha"})
    for f in (BASE, PINK, "Names/blue/Blue - Riya.3mf"):
        client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": f})
    for k, card in _cards(client).items():
        _check(client, k[0], k[1], _res(card, lambda i: "good"))
    comp = {r["order_id"]: r for r in client.get("/api/makeit").json()["completed"]}
    assert comp["QE1"]["tab"] == "pack_it" and comp["QE1"]["label_doubt"] is False
    r = client.post("/api/logistics/order/QE1/label", json={"outside": True, "staff": "Asha"})
    assert r.status_code == 200, r.text
    comp = {r["order_id"]: r for r in client.get("/api/makeit").json()["completed"]}
    assert comp["QE1"]["tab"] == "ship_it" and comp["QE1"]["label_by"] == "Printed outside SnapSTL · Asha"
    assert comp["QE2"]["tab"] == "pack_it"  # only that name
    assert client.post("/api/logistics/order/QE1/label", json={"outside": True}).status_code == 409  # not twice
    assert client.post("/api/logistics/order/QE1/label", json={"undo": True}).status_code == 200
    comp = {r["order_id"]: r for r in client.get("/api/makeit").json()["completed"]}
    assert comp["QE1"]["tab"] == "pack_it" and comp["QE1"]["label_at"] is None


def test_an_outside_mark_shopdeck_never_confirmed_comes_back_with_a_warning():
    t = 1_000_000.0
    r = {"tab": "ship_it", "label_at": t, "label_by": "Printed outside SnapSTL · Asha"}
    later = t + 7 * 3600
    assert verify.doubt(r, ("enqueued", t + 6.5 * 3600), now=later)        # ShopDeck still shows no label
    assert not verify.doubt(r, ("printed", t + 6.5 * 3600), now=later)     # labelled on ShopDeck
    assert not verify.doubt(r, ("enqueued", t - 60), now=later)            # not seen since the mark
    assert not verify.doubt(r, ("enqueued", t + 3600), now=t + 2 * 3600)   # under 6 h
    assert not verify.doubt(r, None, now=later)                            # ShopDeck not connected
    assert not verify.doubt({**r, "label_by": "SnapSTL label"}, ("enqueued", later), now=later)


def test_the_board_is_english_only(client):  # noqa: F811
    page = client.get("/").text
    assert "Today's work" in page and "tdlang" not in page
    assert not re.search(r"[ऀ-ॿ]", page)
    with db.tx() as con:
        assert con.execute("SELECT 1").fetchone()
