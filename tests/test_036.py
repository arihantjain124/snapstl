"""0.36: printer roles (Bases / Letters / Flex / Any), the AMS colour plan per slot, the shift; Print pre-selects
printers by role."""
import pytest

from snapstl import farm

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)

PINK = "Names/pink/Pink - Ayra.3mf"


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    farm.update_config(role_by_printer={}, slots_by_printer={}, shift={"start": "08:00", "end": "22:00"})
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def test_roles_slots_and_shift_saved_and_checked(client, bb):  # noqa: F811
    r = client.get("/api/farm/roles").json()
    assert r["shift"] == {"start": "08:00", "end": "22:00"} and r["roles"] == ["any", "bases", "letters", "flex"]
    names = {p["name"]: p for p in r["printers"]}
    assert names["Names A"]["ams"] and not names["Base 1"]["ams"] and names["Base 1"]["role"] == "any"
    r = client.post("/api/farm/roles", json={"roles": {"1": "bases", "2": "letters", "3": "any"},
                                             "slots": {"2": ["pink", "lavender", "", ""]},
                                             "shift": {"start": "8:30", "end": "21:00"}})
    assert r.status_code == 200, r.text
    names = {p["name"]: p for p in r.json()["printers"]}
    assert r.json()["shift"] == {"start": "08:30", "end": "21:00"}
    assert names["Names A"]["role"] == "letters" and names["Names A"]["slot_plan"] == ["pink", "lavender", "", ""]
    # slot 1 holds pink as planned; slot 2 holds blue, not lavender
    assert names["Names A"]["slot_issues"] == ["slot 2 should be Lavender — Blue is loaded"]
    assert client.post("/api/farm/roles", json={"roles": {"1": "boss"}}).status_code == 400
    assert client.post("/api/farm/roles", json={"roles": {}, "shift": {"start": "25:00", "end": "22:00"}}).status_code == 400
    board = client.get("/api/makeit").json()
    assert board["shift"] == {"start": "08:30", "end": "21:00"}
    assert {p["name"]: p["role"] for p in board["printers"]}["Names A"] == "letters"


def test_preselection_follows_roles(client, bb):  # noqa: F811
    bid, _ = built_batch(client, "RL")
    pink = lambda b: next(r for r in b["names"] if r["batch_id"] == bid and r["file"] == PINK)
    base = lambda b: next(r for r in b["bases"] if r["batch_id"] == bid)
    by = {p["name"]: p["id"] for p in client.get("/api/makeit").json()["printers"]}
    # Names B (hot pink) is a Letters printer, Names A a Bases printer: pink letters go to Names B
    client.post("/api/farm/roles", json={"roles": {str(by["Names A"]): "bases", str(by["Names B"]): "letters",
                                                   str(by["Base 1"]): "letters"}})
    b = client.get("/api/makeit").json()
    assert pink(b)["suggested"] == by["Names B"]
    assert {c["name"]: c["role"] for c in pink(b)["choices"]}["Names A"] == "bases"
    # bases never pre-selected on a Letters printer (Base 1 has white, but it's Letters now): Names A (Bases)
    assert base(b)["suggested"] == by["Names A"]
    # no printer with a role that takes letters free → none pre-selected (a person can still pick one)
    client.post("/api/farm/roles", json={"roles": {str(by["Names A"]): "bases", str(by["Names B"]): "bases",
                                                   str(by["Base 1"]): "bases"}})
    assert pink(client.get("/api/makeit").json())["suggested"] is None
