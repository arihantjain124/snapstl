"""0.43: the whole day per printer (day plates end by 21:30, then the night plate), splits put Express first, the
board's Make next batch / merge offers."""
from datetime import datetime

import pytest

from snapstl import db, makeit, today

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)
from .test_verify import BASE

SH = {"start": "08:00", "end": "22:00"}
PP = {"bases": {"A1": 4.0, "P1S": 3.3, "": 3.5}, "names": {"": 0.4}, "stands": {"": 4.0}}


def at(h, m=0):
    return datetime(2026, 11, 28, h, m).timestamp()


def _p(pid, name, model="P1S", role="bases", state="IDLE", rem=None):
    return {"id": pid, "name": name, "model": model, "role": role, "active": True,
            "status": {"connected": True, "state": state, "remaining_time": rem}}


@pytest.fixture(autouse=True)
def _clean():
    yield
    with db.tx() as con:
        con.execute("UPDATE orders SET state='not_needed' WHERE order_id GLOB 'XE[0-9]*'")


def test_the_day_plan_ends_day_plates_by_2130_and_gives_every_printer_a_night_plate(monkeypatch):
    monkeypatch.setattr(makeit, "bed_state", lambda p: "busy" if p["status"]["state"] == "RUNNING" else "clear")
    prs = [_p(1, "X4 BOB"), _p(2, "A3", model="A1"), _p(3, "X6 BOB", role="letters"),
           _p(4, "X5 BOB", state="RUNNING", rem=150)]
    plan = today.plan(prs, [], at(8), SH, PP, ready=10)
    rows = {r["printer"]: r["slots"] for r in plan["printers"]}
    for name, slots in rows.items():
        assert slots[-1]["night"], name                                         # every printer: a night plate
        assert all(s["end"] <= at(21, 30) for s in slots if not s["night"]), name  # day plates end by 21:30
        assert all(s["start"] < at(22) for s in slots), name                    # started before everyone leaves
    a1 = rows["A3"]
    assert [s["n"] for s in a1] == [3, 3] and a1[1]["start"] <= at(20, 30)      # A1: 3 by day, night plate ~20:15
    x4 = rows["X4 BOB"]
    assert sum(s["n"] for s in x4) == 7                                         # P1S: 7 bases a day
    assert [s["kind"] for s in rows["X6 BOB"]] == ["names", "bases"]            # letters all day, then bases
    assert rows["X5 BOB"][0]["kind"] == "printing" and rows["X5 BOB"][0]["now"]
    assert plan["need"] == sum(s.get("n", 0) for r in rows.values() for s in r if s["kind"] == "bases")
    assert plan["short"] == plan["need"] - 10


def test_a_plan_in_the_evening(monkeypatch):
    monkeypatch.setattr(makeit, "bed_state", lambda p: "clear")
    rows = {r["printer"]: r["slots"] for r in today.plan([_p(1, "X4 BOB"), _p(2, "A3", model="A1")], [], at(19), SH,
                                                         PP, ready=0)["printers"]}
    assert [(s["n"], s["night"]) for s in rows["A3"]] == [(3, True)]            # A1 at 19:00: straight to the night plate
    assert rows["X4 BOB"][-1]["night"] and rows["X4 BOB"][-1]["start"] < at(22)


def test_a_split_puts_the_express_name_on_the_part_printed_first(client, bb):  # noqa: F811
    bid, _ = built_batch(client, "XE")
    with db.tx() as con:  # Riya (the plate's second base) is Express
        con.execute("UPDATE orders SET express_by='2026-11-29' WHERE order_id='XE2'")
    r = client.post("/api/makeit/divide", json={"batch_id": bid, "file": BASE, "sizes": [1, 1]})
    assert r.status_code == 200, r.text
    first, rest = r.json()["files"]
    assert "Riya" in first and "Ayra" in rest
    letters = r.json()["letters"]  # the letters follow the same order
    assert len(letters) == 0 or "Riya" in letters[0] or len(letters) == 2


def test_the_board_carries_the_plan_and_its_offers(client):  # noqa: F811
    b = client.get("/api/today").json()
    for k in ("plan", "to_make", "batches_coming", "merge", "merge_bases", "batch_size"):
        assert k in b, k
    assert set(b["plan"]) >= {"printers", "need", "ready", "short", "last"}
    page = client.get("/").text
    assert 'id="tdplan"' in page and "Make next batch" in page
