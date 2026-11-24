"""0.32.2: splitting a bases plate splits its letter plates the same way; an order packed on the website but
printed by SnapSTL and not checked yet still gets its Verify check."""
from pathlib import Path

import pytest

from snapstl import db

from .test_farm import bb  # noqa: F401  (bb is a fixture)
from .test_flow import build_and_wait, client, helper_work  # noqa: F401  (client is a fixture)
from .test_ingest import report
from .test_verify import _cards, _check, _res

NAMES = ("Ayra", "Riya", "Kiara")
BASE3 = "Bases/Bases - Ayra + Riya + Kiara.3mf"
PINK3 = "Names/pink/Pink - Ayra + Riya + Kiara.3mf"


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def _three_pink(c, tag):
    data = report([(f"{tag}{i}", "PRINT", "Magicians_35", n, "Pink", 1) for i, n in enumerate(NAMES, 1)])
    assert c.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    bid = c.post("/api/batches", json={"order_ids": [f"{tag}{i}" for i in (1, 2, 3)]}).json()["batch_id"]
    c.post(f"/api/batches/{bid}/download")
    helper_work(c)
    b = build_and_wait(c, bid)
    plates = {p["file"] for p in b["summary"]["plates"]}
    assert {BASE3, PINK3} <= plates, plates
    return bid


def test_splitting_bases_splits_the_letters_the_same_way_and_joins_back(client, bb):  # noqa: F811
    bid = _three_pink(client, "SL")
    r = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE3, "sizes": [2, 1]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert [Path(f).name for f in j["files"]] == ["Part - Bases - Ayra + Riya.3mf", "Part - Bases - Kiara.3mf"]
    assert [Path(f).name for f in j["letters"]] == ["Part - Pink - Ayra + Riya.3mf", "Part - Pink - Kiara.3mf"]
    m = client.get("/api/makeit").json()
    assert {Path(x["file"]).name for x in m["names"]} == {"Part - Pink - Ayra + Riya.3mf", "Part - Pink - Kiara.3mf"}
    # Join back on the bases puts the letters back on one plate too (nothing started)
    assert client.post("/api/makeit/join", json={"batch_id": bid, "file": j["files"][0]}).json()["letters"] == [PINK3]
    m = client.get("/api/makeit").json()
    assert {x["file"] for x in m["bases"]} == {BASE3} and {x["file"] for x in m["names"]} == {PINK3}


def test_letters_already_printed_are_left_alone(client, bb):  # noqa: F811
    bid = _three_pink(client, "SP")
    assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": PINK3}).status_code == 200
    j = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE3, "sizes": [1, 1, 1]}).json()
    assert len(j["files"]) == 3 and j["letters"] == [] and j["letters_skipped"] == [PINK3]


def test_splitting_letters_alone_still_works(client, bb):  # noqa: F811
    bid = _three_pink(client, "SN")
    j = client.post("/api/makeit/divide", json={"batch_id": bid, "file": PINK3, "sizes": [1, 2]}).json()
    assert len(j["files"]) == 2 and j["letters"] == []
    assert {x["file"] for x in client.get("/api/makeit").json()["bases"]} == {BASE3}  # bases untouched


def test_packed_on_the_website_before_its_check_still_shows_in_verify(client, bb):  # noqa: F811
    """A split bases plate and the letters printed; the website then says PACK (label made there) before anyone
    checked it: it stays in Verify, marked packed, and can be checked. Shipped orders leave it."""
    bid = _three_pink(client, "PK")
    parts = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE3, "sizes": [2, 1]}).json()
    client.post("/api/staff", json={"name": "Asha"})
    for f in parts["files"] + parts["letters"]:
        assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": f}).status_code == 200
    with db.tx() as con:
        con.execute("UPDATE orders SET source_status='PACK' WHERE order_id='PK1'")
        con.execute("UPDATE orders SET source_status='IN-TRANSIT' WHERE order_id='PK3'")
    cards = {c["order_id"]: c for c in client.get("/api/makeit").json()["verify"]}
    assert set(cards) == {"PK1", "PK2"}  # PK3 is on its way: nothing left to check
    assert cards["PK1"]["packed"] and not cards["PK2"]["packed"]
    for (b, f), card in _cards(client).items():
        assert _check(client, b, f, _res(card, lambda i: "good")).status_code == 200
    with db.tx() as con:
        who = con.execute("SELECT checked_by FROM orders WHERE order_id='PK1'").fetchone()[0]
    assert who == "Asha"
