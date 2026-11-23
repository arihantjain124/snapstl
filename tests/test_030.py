"""0.30: name stands are batched (one product per batch) and downloaded by the helper (2.4) on their own
MakerWorld pages; an older helper never gets a stand. (0.31: their plates — test_031.)"""
import json

import pytest
import trimesh

from snapstl import cache, db, products
from snapstl.batches import DOWNLOADER

from .test_029 import ROWS, report
from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)

GTA = products.BY_KEY["gta"]


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def stand_obj(length=180.0, height=34.0, back=37.0, letters=3.0, n_mat=2) -> bytes:
    """Like MakerWorld's OpenSCAD export: one OBJ, the block (bottom) and the letters (top) as materials."""
    block = trimesh.creation.box([length, height, back])
    block.apply_translation([0, height / 2, 0])  # z −back/2 … back/2
    top = trimesh.creation.box([length - 8, height - 8, letters])
    top.apply_translation([0, height / 2, back / 2 + letters / 2])
    out, base = ["# OpenSCAD obj exporter", "mtllib Parametric_Model_Maker.mtl"], 0
    parts = [("#000000", block), ("#ffffff", top)][:n_mat] if n_mat > 1 else [("#000000", trimesh.util.concatenate([block, top]))]
    for mat, m in parts:
        out += [f"v {x} {y} {z}" for x, y, z in m.vertices]
        out.append(f"usemtl {mat}")
        out += ["f " + " ".join(str(base + i + 1) for i in f) for f in m.faces]
        base += len(m.vertices)
    return "\n".join(out).encode()


def _batch(client, ids):  # noqa: F811
    assert client.post("/api/ingest", files={"file": ("r.csv", report(ROWS), "text/csv")}).status_code == 200
    b = client.post("/api/batches", json={"order_ids": ids})
    assert b.status_code == 200, b.text
    bid = b.json()["batch_id"]
    r = client.post(f"/api/batches/{bid}/download", json={"force": True})  # even if an earlier test left it saved
    assert r.status_code == 200, r.text
    return bid


def test_a_stand_is_downloaded_on_its_own_page(client):  # noqa: F811
    bid = _batch(client, ["G1"])
    # an older helper (no `design`) and the organizer's page never get a stand; the page to open is offered
    assert client.get("/api/helper/next").json()["job"] is None
    org = client.get("/api/helper/config").json()
    assert org["pages"][GTA.design_id] == GTA.url and org["pages"][org["design_id"]] == org["model_url"]
    r = client.get("/api/helper/next", params={"design": org["design_id"]}).json()
    assert r["job"] is None and r["go"] == GTA.url
    job = client.get("/api/helper/next", params={"design": GTA.design_id}).json()["job"]
    assert job["kind"] == "stand" and job["name"] == "SIDHARTH" and job["product"] == "GTA"
    f = {x[0]: (x[1], x[2]) for x in job["fields"]}
    assert f == {"text input": ("text", "SIDHARTH"), "font size": ("number", 40), "letter spacing": ("number", 25),
                 "back thickness": ("number", 37), "letter protrusion": ("number", 3), "brim offset": ("number", 6),
                 "flat bottom cut": ("number", 0.5), "Place in printing position": ("check", True),
                 "Use Font Features": ("check", True)}
    # a model that isn't block + letters is refused and tried again
    bad = client.post(f"/api/helper/jobs/{job['job_id']}/result",
                      files={"model": ("model.obj", stand_obj(n_mat=1), "text/plain")})
    assert bad.status_code == 400 and "2 colours" in bad.text
    job = client.get("/api/helper/next", params={"design": GTA.design_id}).json()["job"]
    ok = client.post(f"/api/helper/jobs/{job['job_id']}/result", files={"model": ("model.obj", stand_obj(), "text/plain")})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"block": [180.0, 34.0, 37.0], "letters": [172.0, 26.0, 3.0]}
    m = client.get(f"/api/batches/{bid}/models").json()["items"]
    assert [(x["name"], x["status"], x["watermark"], x["product"]) for x in m] == [("SIDHARTH", "ready", None, "gta")]
    d = cache.find(m[0]["key"])
    assert d.name.startswith("SIDHARTH - GTA - ") and {p.name for p in d.iterdir()} == {
        "model.obj", "base.stl", "letters.stl", "info.json"}
    info = json.loads((d / "info.json").read_text())
    assert info["kind"] == "stand" and info["block"] == [180.0, 34.0, 37.0]
    block, letters = cache.load(m[0]["key"])
    assert round(letters.bounds[0][2] - block.bounds[1][2], 3) == 0  # the letters sit on the block
    # ready to build (0.31: plates — test_031); the Print page for stands comes later
    row = wait_for(lambda: next((b for b in client.get("/api/batches").json()["batches"]
                                 if b["id"] == bid and b["status"] == "models_ready"), None))
    assert (row["product"], row["product_label"], row["buildable"], row["printable"]) == ("gta", "GTA", True, False)


def test_letters_off_the_block_are_refused(client):  # noqa: F811
    _batch(client, ["H1"])
    job = client.get("/api/helper/next", params={"design": products.BY_KEY["harry_potter"].design_id}).json()["job"]
    assert "Use Font Features" not in [x[0] for x in job["fields"]]  # GTA only
    r = client.post(f"/api/helper/jobs/{job['job_id']}/result",
                    files={"model": ("model.obj", stand_obj(back=30), "text/plain")})  # not the 37 mm sent
    assert r.status_code == 400 and "Unexpected name stand" in r.text


def test_the_organizer_download_is_unchanged(client):  # noqa: F811
    _batch(client, ["O1"])
    job = client.get("/api/helper/next").json()["job"]  # an older helper still gets organizers
    assert job["name"] == "Ayra" and "kind" not in job and job["license_password"]
    client.post(f"/api/helper/jobs/{job['job_id']}/release")
    org = client.get("/api/helper/config").json()["design_id"]
    assert client.get("/api/helper/next", params={"design": org}).json()["job"]["name"] == "Ayra"
    client.post(f"/api/helper/jobs/{job['job_id']}/release")
    assert not DOWNLOADER.paused
    r = client.get("/api/helper/next", params={"design": GTA.design_id}).json()
    assert r["job"] is None and r["go"] == client.get("/api/helper/config").json()["model_url"]


def test_stand_settings_change_the_download_key(client):  # noqa: F811
    from snapstl.batches import model_key
    from snapstl.orders import Order
    from snapstl.settings import Settings

    s = Settings()
    o = Order(0, "SIDHARTH", "", "G1", 0, "gta")
    k = model_key(o, s)
    s.stands.brim_offset = 5
    assert model_key(o, s) != k and model_key(Order(0, "SIDHARTH", "", "G1", 0, "barbie"), Settings()) != k


def test_make_next_batch_of_stands_builds_by_itself(client):  # noqa: F811
    assert client.post("/api/ingest", files={"file": ("r.csv", report(ROWS), "text/csv")}).status_code == 200
    r = client.post("/api/batches/next", json={"count": 5, "product": "gta"}).json()
    with db.tx() as con:
        assert con.execute("SELECT auto_build FROM batches WHERE id=?", (r["batch_id"],)).fetchone()[0] == 1
