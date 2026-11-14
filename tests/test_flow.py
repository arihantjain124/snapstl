"""End-to-end: report → select orders → batch → in-app downloader (MakerWorld stubbed) → plates → ZIP."""
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from snapstl import auth, makerworld
from snapstl.batches import DOWNLOADER

from .conftest import base_mesh, name_mesh
from .test_ingest import report

CALLS = []


def fake_generate(name, model, compartments, pw):
    CALLS.append((name, compartments))
    if name == "Broken":
        raise RuntimeError("MakerWorld said no")
    k = sum(c.isalpha() for c in name)
    # plate order deliberately swapped: the app must tell base from name by shape
    return name_mesh(k).export(file_type="obj").encode(), base_mesh(length=48 * k + 10).export(file_type="obj").encode()


@pytest.fixture
def client():
    from snapstl import server

    auth.set_password("test-password-123")
    DOWNLOADER.generate = fake_generate
    DOWNLOADER.pause_s = 0
    c = TestClient(server.app)
    c.__enter__()
    DOWNLOADER.pause_s = 0
    DOWNLOADER.setup_changed()
    assert c.post("/api/login", json={"password": "test-password-123"}).status_code == 200
    yield c
    c.__exit__(None, None, None)


def wait_for(fn, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_batch_downloads_builds_and_zips(client):
    data = report([
        ("F1", "PRINT", "Magicians_35", "Ayra", "Pink", 1),
        ("F2", "PRINT", "Magicians_35", "Manvi", "Pink", 1),
        ("F3", "PRINT", "Magicians_35", "Riya", "Blue", 1),
        ("F4", "PRINT", "Magicians_35", "Zoya", "Blue", 1),
    ])
    assert client.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    r = client.post("/api/batches", json={"order_ids": ["F1", "F2", "F3"]})
    assert r.status_code == 200, r.text
    bid = r.json()["batch_id"]
    states = {o["order_id"]: o["state"] for o in client.get("/api/orders?limit=10").json()["orders"]}
    assert states == {"F1": "in_batch", "F2": "in_batch", "F3": "in_batch", "F4": "ready"}
    assert client.post("/api/batches", json={"order_ids": ["F1"]}).status_code == 400

    b = wait_for(lambda: (lambda d: d if d["status"].startswith("done") else None)(client.get(f"/api/batches/{bid}").json()))
    assert b["status"] == "done", b["summary"].get("errors")
    assert sorted(p["file"] for p in b["summary"]["plates"]) == [
        "bases/bases-(Ayra,Manvi,Riya,pink,pink,blue).stl",
        "names/(Ayra,Manvi)-(pink).stl",
        "names/(Riya)-(blue).stl",
    ]
    # compartments come from the scaling table (4 letters → 4, 5 letters → 5)
    assert ("Ayra", 4) in CALLS and ("Manvi", 5) in CALLS
    # a ZIP sits next to the batch folder, named after it
    z = Path(b["summary"]["zip"])
    assert z.is_file() and z.name.startswith(f"batch-{bid:03d}_")
    names = zipfile.ZipFile(z).namelist()
    assert any(n.endswith("/report.csv") for n in names) and len([n for n in names if n.endswith(".stl")]) == 3
    assert client.get(f"/api/batches/{bid}/zip").status_code == 200
    states = {o["order_id"]: o["state"] for o in client.get("/api/orders?limit=10").json()["orders"]}
    assert states["F1"] == "bundled" and states["F4"] == "ready"
    st = client.get("/api/state").json()
    assert st["downloader"]["done"] >= 3


def test_failed_download_then_build_now(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("G1", "PRINT", "Magicians_35", "Broken", "Red", 1),
        ("G2", "PRINT", "Magicians_35", "Kabir", "Red", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["G1", "G2"]}).json()["batch_id"]
    # Broken fails twice → failed; batch builds with Kabir only, Broken goes back to Ready
    b = wait_for(lambda: (lambda d: d if d["status"].startswith("done") else None)(client.get(f"/api/batches/{bid}").json()))
    assert b["status"] == "done_with_problems"
    assert any("Broken" in e for e in b["summary"]["errors"])
    o = {x["order_id"]: x["state"] for x in client.get("/api/orders", params={"q": "G"}).json()["orders"]}
    assert o["G1"] == "ready" and o["G2"] == "bundled"


def test_cancel_returns_orders(client):
    DOWNLOADER.paused = True
    client.post("/api/ingest", files={"file": ("r.csv", report([("H1", "PRINT", "Magicians_35", "Tara", "Green", 1)]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["H1"]}).json()["batch_id"]
    assert client.post(f"/api/batches/{bid}/build").status_code == 400  # nothing downloaded yet
    assert client.post(f"/api/batches/{bid}/cancel").status_code == 200
    assert client.get("/api/state").json()["queue"].get("queued", 0) == 0  # its download was dropped
    assert client.get("/api/orders", params={"q": "H1"}).json()["orders"][0]["state"] == "ready"
    DOWNLOADER.paused = False


# ---------------- MakerWorld client ----------------
def test_cookie_parsing():
    c = makerworld.parse_cookie("cookie: a=1; bbl_csrf_token=xyz; token=t.o.k")
    assert c["bbl_csrf_token"] == "xyz" and c["token"] == "t.o.k"
    assert makerworld.check_cookie_shape("a=1") and not makerworld.check_cookie_shape("a=1; bbl_csrf_token=x")
    assert makerworld.Session("a=1; bbl_csrf_token=xyz")._headers()["x-bbl-csrf-token"] == "xyz"


def test_browser_cookies():
    from snapstl.browser_gen import cookies_for_browser

    ck = cookies_for_browser("bbl_csrf_token=x; token=abc")
    assert {c["name"] for c in ck} == {"bbl_csrf_token", "token"}
    assert all(c["domain"] == ".makerworld.com" and c["secure"] for c in ck)


def test_interrupted_download_resumes_after_restart(client):
    from snapstl import db
    from snapstl.batches import Downloader

    with db.tx() as con:
        con.execute("INSERT INTO downloads(cache_key,name,compartments,model_json,state,attempts,claimed_at,created_at,updated_at)"
                    " VALUES ('k-resume','Resumo',4,'{}','claimed',1,?,?,?)", (db.now(), db.now(), db.now()))
    d = Downloader()
    d.paused = True  # don't actually process; just run the startup recovery
    d.start()
    with db.tx() as con:
        row = con.execute("SELECT state, attempts FROM downloads WHERE cache_key='k-resume'").fetchone()
        con.execute("DELETE FROM downloads WHERE cache_key='k-resume'")
    assert row["state"] == "queued" and row["attempts"] == 0
