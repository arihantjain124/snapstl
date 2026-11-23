"""0.31: plates for name stands — sized by Settings → Name stands, up to 3 of one product per plate, each stand
one object of two parts (Base → filament 1, Letters → filament 2), every file read back."""
import csv
import json
import zipfile

import pytest

from snapstl import db, stands
from snapstl.meshes import obj_by_material
from snapstl.orders import Order
from snapstl.settings import Settings
from snapstl.threemf import read_3mf, read_parts_config

from .test_029 import ROWS, report
from .test_030 import stand_obj
from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def _pieces(length=180.0):
    parts = obj_by_material(stand_obj(length=length))
    return tuple(sorted(parts.values(), key=lambda m: m.bounds[0][2]))  # block, letters


NAMES = ["Om", "Riya", "Kabir", "SIDHARTH"]


def test_plates_of_three_two_parts_two_filaments(tmp_path):
    s = Settings()
    orders = [Order(i, n, "", f"G{i}", 0, "gta") for i, n in enumerate(NAMES)]
    res = stands.build(orders, {o.idx: _pieces() for o in orders}, s, tmp_path)
    assert res.ok, res.errors
    assert [len(p.names) for p in res.plates] == [3, 1]
    assert [p.file for p in res.plates] == ["Stands/GTA/GTA - Om + Riya + Kabir.3mf", "Stands/GTA/GTA - SIDHARTH.3mf"]
    assert res.plates[0].colors == ["black", "white"] and all(res.complete(o.idx) for o in orders)
    path = tmp_path / res.plates[0].file
    objs = read_3mf(path)
    assert len(objs) == 6  # 3 stands × (base + letters)
    conf = read_parts_config(path)
    assert [n.split(" · ")[0] for n, _ in conf] == ["Om - GTA", "Riya - GTA", "Kabir - GTA"]
    assert all(ps == [("Base", 1), ("Letters", 2)] for _, ps in conf)
    with zipfile.ZipFile(path) as z:  # Bambu Studio's format: one object made of two parts
        model = z.read("3D/3dmodel.model").decode()
    assert model.count("<components>") == 3 and model.count("<item ") == 3
    # sizes from the table: Kabir (5 letters) 210 × 40, 40 thick (37 + 3); letters on the block
    blk, let = objs[4], objs[5]
    size = blk.vertices.max(axis=0) - blk.vertices.min(axis=0)
    assert abs(size[0] - 210) < 0.01 and abs(size[1] - 40) < 0.01
    assert abs(let.vertices.max(axis=0)[2] - 40) < 0.01 and abs(let.vertices.min(axis=0)[2] - 37) < 0.01
    allv = __import__("numpy").vstack([o.vertices for o in objs])
    assert allv.min(axis=0)[2] == pytest.approx(0, abs=1e-6)
    assert allv.max(axis=0)[1] <= s.plate.bed_y - stands.PRIME_TOWER_MM - s.plate.margin + 0.01  # tower strip free
    rows = list(csv.DictReader(l for l in (tmp_path / "report.csv").open() if not l.startswith("#")))
    assert [(r["name"], r["target_length"], r["status"]) for r in rows][:3] == [
        ("Om", "195", "ok"), ("Riya", "200", "ok"), ("Kabir", "210", "ok")]


def test_squeezed_letters_are_a_warning(tmp_path):
    s = Settings()
    o = Order(0, "Om", "", "G0", 0, "gta")
    res = stands.build([o], {0: _pieces(length=60)}, s, tmp_path)  # 60 → 195 mm long, 34 → 40 high
    assert res.ok and any("stretched" in w for w in res.warnings)


def test_a_stand_batch_is_built_end_to_end(client):  # noqa: F811
    assert client.post("/api/ingest", files={"file": ("r.csv", report(ROWS), "text/csv")}).status_code == 200
    bid = client.post("/api/batches", json={"order_ids": ["G1"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download", json={"force": True})  # even if an earlier test left it saved
    job = client.get("/api/helper/next", params={"design": "2737384"}).json()["job"]
    assert client.post(f"/api/helper/jobs/{job['job_id']}/result",
                       files={"model": ("model.obj", stand_obj(), "text/plain")}).status_code == 200
    wait_for(lambda: _status(bid) == "models_ready")
    assert client.post(f"/api/batches/{bid}/build").status_code == 200
    wait_for(lambda: _status(bid) in ("done", "done_with_problems", "failed"))
    with db.tx() as con:
        b = dict(con.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone())
        st = con.execute("SELECT state FROM orders WHERE order_id='G1'").fetchone()[0]
    summ = json.loads(b["summary"])
    assert b["status"] == "done", summ["errors"]
    assert summ["plates"] == [] and [p["file"] for p in summ["stand_plates"]] == ["Stands/GTA/GTA - SIDHARTH.3mf"]
    assert summ["stand_plates"][0]["order_ids"] == ["G1"] and st == "bundled"
    row = next(r for r in client.get("/api/batches").json()["batches"] if r["id"] == bid)
    assert row["plates"] == 1 and row["printable"] is False
    m = client.get("/api/makeit").json()  # Print page: stands aren't on it yet, nothing breaks
    assert all(r["batch_id"] != bid for r in m["bases"] + m["names"])
    import shutil

    from snapstl import cache

    for x in client.get(f"/api/batches/{bid}/models").json()["items"]:  # other tests download it again
        shutil.rmtree(cache.find(x["key"]), ignore_errors=True)
    shutil.rmtree(b["out_dir"], ignore_errors=True)  # other tests expect today's first folder to be "Batch 1"


def _status(bid):
    with db.tx() as con:
        return con.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()[0]
