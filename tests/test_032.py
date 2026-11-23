"""0.32: name stands on the Print page — only printers with an AMS, two filament slots (block, letters),
sliced with two filaments, sent with a two-slot AMS mapping; Verify one piece per order; a spoiled stand is
printed again whole on a fix plate."""
import json
import shutil
import time

import pytest

from snapstl import cache, db, farm
from snapstl.threemf import read_3mf, read_parts_config

from .test_029 import ROWS, report
from .test_030 import stand_obj
from .test_farm import bb, tray  # noqa: F401  (bb is a fixture)
from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)

STAND = "Stands/GTA/GTA - SIDHARTH.3mf"


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    for _ in range(50):  # a build may still be finishing
        if client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200:
            return
        time.sleep(0.2)
    raise AssertionError("reset refused")


def _status(bid):
    with db.tx() as con:
        return con.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()[0]


def _stand_batch(client):  # noqa: F811
    assert client.post("/api/ingest", files={"file": ("r.csv", report(ROWS), "text/csv")}).status_code == 200
    bid = client.post("/api/batches", json={"order_ids": ["G1"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download", json={"force": True})
    job = client.get("/api/helper/next", params={"design": "2737384"}).json()["job"]
    assert client.post(f"/api/helper/jobs/{job['job_id']}/result",
                       files={"model": ("model.obj", stand_obj(), "text/plain")}).status_code == 200
    wait_for(lambda: _status(bid) == "models_ready")
    assert client.post(f"/api/batches/{bid}/build").status_code == 200
    wait_for(lambda: _status(bid) == "done")
    return bid


def _cleanup(client, bid):  # noqa: F811
    with db.tx() as con:
        out = con.execute("SELECT out_dir FROM batches WHERE id=?", (bid,)).fetchone()[0]
    for x in client.get(f"/api/batches/{bid}/models").json()["items"]:
        shutil.rmtree(cache.find(x["key"]) or "", ignore_errors=True)
    shutil.rmtree(out or "", ignore_errors=True)


def test_a_stand_prints_with_two_filaments_and_is_checked(client, bb):  # noqa: F811
    # a printer whose AMS holds black and white (the GTA colours)
    bb.printers[4] = {"id": 4, "name": "Stands P1S", "model": "A1", "serial_number": "S4", "ip_address": "10.0.0.30",
                      "location": "Stands", "is_active": True}
    bb.status[4] = {"connected": True, "state": "IDLE",
                    "ams": [{"id": 0, "tray": [tray(0, "F55A74"), tray(1, "000000"), tray(2, "FFFFFF")]}]}
    bb.colors["colors"].update({"000000": "Black"})
    bid = _stand_batch(client)
    try:
        client.get("/api/farm")
        m = client.get("/api/makeit").json()
        row = next(r for r in m["stands"] if r["file"] == STAND)
        assert row["colors"] == ["black", "white"] and row["kind"] == "stands"
        ch = {c["id"]: c for c in row["choices"]}
        assert 1 not in ch  # Base 1 has no AMS: never offered for a stand
        assert row["suggested"] == 4 and (ch[4]["best_tray"], ch[4]["best_tray2"]) == (1, 2)
        assert all(r["file"] != STAND for r in m["bases"] + m["names"])
        # no AMS → refused; the same slot twice → refused
        r = client.post("/api/makeit/start", json={"batch_id": bid, "file": STAND, "printer_id": 1})
        assert r.status_code >= 400 and "no AMS" in r.text
        r = client.post("/api/makeit/start", json={"batch_id": bid, "file": STAND, "printer_id": 4, "tray": 1, "tray2": 1})
        assert r.status_code >= 400 and "two different" in r.text
        # start: sliced with two filaments (black, white), sent with both slots
        bb.slicer = "ok"
        r = client.post("/api/makeit/start", json={"batch_id": bid, "file": STAND, "printer_id": 4})
        assert r.status_code == 200, r.text
        wait_for(lambda: bb.queue)
        body = bb.slice_bodies[-1][1]
        assert len(body["filament_presets"]) == 2
        assert body["filament_colours"] == [farm.COLOR_HEX["black"], farm.COLOR_HEX["white"]]
        q = bb.queue[max(bb.queue)]
        assert q["ams_mapping"] == [1, 2] and q["printer_id"] == 4
        with db.tx() as con:
            j = dict(con.execute("SELECT * FROM print_jobs WHERE plate=? ORDER BY id DESC", (STAND,)).fetchone())
        assert (j["color"], j["ams_tray"], j["ams_tray2"]) == ("black+white", 1, 2)
        # printed → Verify: one card, one piece (the stand)
        q["status"] = "completed"
        farm.WORKER.refresh()
        m = client.get("/api/makeit").json()
        card = next(c for c in m["verify"] if c["order_id"] == "G1")
        assert [i["part"] for i in card["items"]] == ["stands"]
        assert [r["file"] for r in m["printed_stands"]] == [STAND]
        # spoiled → printed again whole (block + letters) on a fix plate
        assert client.post("/api/staff", json={"name": "Asha"}).status_code == 200
        it = card["items"][0]
        r = client.post("/api/verify", json={"carrier": it["carrier"], "staff": "Asha",
                                             "results": [{**{k: it[k] for k in ("batch_id", "plate", "idx")},
                                                          "result": "stand"}]})
        assert r.status_code == 200, r.text
        fix = r.json()["fixes"][0]
        assert fix.startswith("Fixes/Fix - Stand - SIDHARTH")
        with db.tx() as con:
            out = con.execute("SELECT out_dir FROM batches WHERE id=?", (bid,)).fetchone()[0]
        from pathlib import Path

        path = Path(out) / fix
        assert len(read_3mf(path)) == 2 and read_parts_config(path)[0][1] == [("Base", 1), ("Letters", 2)]
        m = client.get("/api/makeit").json()
        fr = next(r for r in m["stands"] if r["file"] == fix)
        assert fr["extra"] == "fix" and fr["colors"] == ["black", "white"]
        # the fix printed by hand and checked good → made (Completed)
        assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": fix}).status_code == 200
        card = next(c for c in client.get("/api/makeit").json()["verify"] if c["order_id"] == "G1")
        it = card["items"][0]
        r = client.post("/api/verify", json={"carrier": it["carrier"], "staff": "Asha",
                                             "results": [{**{k: it[k] for k in ("batch_id", "plate", "idx")},
                                                          "result": "good"}]})
        assert r.status_code == 200, r.text
        done = client.get("/api/makeit").json()["completed"]
        assert [(c["order_id"], [i["part"] for i in c["items"]]) for c in done] == [("G1", ["stands"])]
        # a stand plate can't be split (organizer plates only)
        r = client.post("/api/makeit/divide", json={"batch_id": bid, "file": STAND, "sizes": [1]})
        assert r.status_code >= 400
    finally:
        _cleanup(client, bid)


def test_old_031_batches_move_their_stand_plates(client):  # noqa: F811
    from snapstl import stands

    with db.tx() as con:
        con.execute("INSERT INTO batches(id, created_at, status, order_ids, settings, summary, computer_id) "
                    "VALUES (900, 1, 'done', '[]', '{}', ?, NULL)",
                    (json.dumps({"plates": [], "stand_plates": [{"kind": "stands", "file": "Stands/GTA/x.3mf"}]}),))
    assert stands.repair_031() == 1 and stands.repair_031() == 0
    with db.tx() as con:
        s = json.loads(con.execute("SELECT summary FROM batches WHERE id=900").fetchone()[0])
    assert [p["file"] for p in s["plates"]] == ["Stands/GTA/x.3mf"] and "stand_plates" not in s


def test_slice_request_with_two_filaments():
    pr = {k: {"source": "local", "id": "1", "name": k} for k in ("printer", "process", "filament")}
    b = farm.slice_request(pr, "hot pink+white")
    assert len(b["filament_presets"]) == 2 and b["filament_colours"] == [farm.COLOR_HEX["hot pink"], farm.COLOR_HEX["white"]]
    b = farm.slice_request(pr, "hot pink")  # an organizer's letters: unchanged
    assert len(b["filament_presets"]) == 1 and b["filament_colours"] == [farm.COLOR_HEX["hot pink"]]
    assert farm.ams_mapping(5, None) == {"ams_mapping": [5]} and farm.ams_mapping(5, 6) == {"ams_mapping": [5, 6]}
