"""End-to-end: report → select orders → batch → helper downloads → plates → ZIP; plus DB maintenance."""
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from snapstl import auth, db
from snapstl.batches import DOWNLOADER

from .conftest import base_mesh, name_mesh
from .test_ingest import report


@pytest.fixture
def client():
    from snapstl import server

    auth.set_password("test-password-123")
    c = TestClient(server.app)
    c.__enter__()
    DOWNLOADER.paused = False
    DOWNLOADER.pause_s = 0
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


def obj(mesh) -> bytes:
    return mesh.export(file_type="obj").encode()


def helper_work(c, fail_names=(), max_jobs=50):
    """Act like the Chrome helper until the queue is empty. Returns [(name, compartments)]."""
    seen = []
    for _ in range(max_jobs):
        job = c.get("/api/helper/next", params={"status": "Idle"}).json()["job"]
        if job is None:
            break
        seen.append((job["name"], job["compartments"]))
        assert job["params"]["name"] == job["name"]
        if job["name"] in fail_names:
            c.post(f"/api/helper/jobs/{job['job_id']}/fail", json={"error": "MakerWorld said no"})
            continue
        k = sum(ch.isalpha() for ch in job["name"])
        # plate order deliberately swapped: the app must tell base from name by shape
        files = {"plate_1": ("plate_1.obj", obj(name_mesh(k)), "text/plain"),
                 "plate_2": ("plate_2.obj", obj(base_mesh(length=48 * k + 10)), "text/plain")}
        assert c.post(f"/api/helper/jobs/{job['job_id']}/result", files=files).status_code == 200
    return seen


def done(c, bid):
    return wait_for(lambda: (lambda d: d if d["status"].startswith("done") else None)(c.get(f"/api/batches/{bid}").json()))


def test_batch_with_helper_builds_and_zips(client):
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

    seen = helper_work(client)
    assert ("Ayra", 4) in seen and ("Manvi", 5) in seen  # compartments from the scaling table
    assert client.get("/api/state").json()["downloader"]["online"]

    b = done(client, bid)
    assert b["status"] == "done", b["summary"].get("errors")
    assert sorted(p["file"] for p in b["summary"]["plates"]) == [
        "bases/bases-(Ayra,Manvi,Riya,pink,pink,blue).stl",
        "names/(Ayra,Manvi)-(pink).stl",
        "names/(Riya)-(blue).stl",
    ]
    z = Path(b["summary"]["zip"])
    assert z.is_file() and z.name.startswith(f"batch-{bid:03d}_")
    names = zipfile.ZipFile(z).namelist()
    assert any(n.endswith("/report.csv") for n in names) and len([n for n in names if n.endswith(".stl")]) == 3
    assert client.get(f"/api/batches/{bid}/zip").status_code == 200
    states = {o["order_id"]: o["state"] for o in client.get("/api/orders?limit=10").json()["orders"]}
    assert states["F1"] == "bundled" and states["F4"] == "ready"
    q = client.get("/api/downloads").json()["items"]
    assert {x["name"] for x in q if x["state"] == "done"} >= {"Ayra", "Manvi", "Riya"}


def test_failed_download_retries_then_batch_builds_without_it(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("G1", "PRINT", "Magicians_35", "Broken", "Red", 1),
        ("G2", "PRINT", "Magicians_35", "Kabir", "Red", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["G1", "G2"]}).json()["batch_id"]
    seen = helper_work(client, fail_names=("Broken",))
    assert [n for n, _ in seen].count("Broken") == 2  # one retry, then failed
    b = done(client, bid)
    assert b["status"] == "done_with_problems" and any("Broken" in e for e in b["summary"]["errors"])
    o = {x["order_id"]: x["state"] for x in client.get("/api/orders", params={"q": "G"}).json()["orders"]}
    assert o["G1"] == "ready" and o["G2"] == "bundled"


def test_pause_release_and_cancel(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([("H1", "PRINT", "Magicians_35", "Tara", "Green", 1)]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["H1"]}).json()["batch_id"]
    client.post("/api/downloader/pause")
    assert client.get("/api/helper/next").json() == {"job": None, "paused": True}
    client.post("/api/downloader/resume")
    job = client.get("/api/helper/next").json()["job"]
    assert job["name"] == "Tara"
    client.post(f"/api/helper/jobs/{job['job_id']}/release")  # e.g. tab reloaded mid-name
    assert client.get("/api/helper/next").json()["job"]["job_id"] == job["job_id"]
    client.post(f"/api/helper/jobs/{job['job_id']}/release")
    assert client.post(f"/api/batches/{bid}/build").status_code == 400  # nothing downloaded yet
    assert client.post(f"/api/batches/{bid}/cancel").status_code == 200
    assert client.get("/api/orders", params={"q": "H1"}).json()["orders"][0]["state"] == "ready"
    assert client.get("/api/helper/next").json()["job"] is None  # its download was dropped


def test_interrupted_download_resumes_after_restart(client):
    from snapstl.batches import Downloader

    with db.tx() as con:
        con.execute("INSERT INTO downloads(cache_key,name,compartments,model_json,state,attempts,claimed_at,created_at,updated_at)"
                    " VALUES ('k-resume','Resumo',4,'{}','claimed',1,?,?,?)", (db.now(), db.now(), db.now()))
    Downloader().start()
    with db.tx() as con:
        row = con.execute("SELECT state, attempts FROM downloads WHERE cache_key='k-resume'").fetchone()
        con.execute("DELETE FROM downloads WHERE cache_key='k-resume'")
    assert row["state"] == "queued" and row["attempts"] == 0


def test_cleanup_keeps_made_orders_and_reset_empties(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("M1", "DELIVERED", "Magicians_35", "Old", "Red", 1),
        ("M2", "PRINT", "Magicians_35", "Made", "Red", 1),
    ]), "text/csv")})
    with db.tx() as con:
        con.execute("UPDATE orders SET state='bundled' WHERE order_id='M2'")
        con.execute("UPDATE orders SET updated_at=0 WHERE order_id IN ('M1','M2')")
    r = client.post("/api/maintenance/cleanup", json={"older_than_days": 30}).json()
    assert r["orders_removed"] >= 1 and Path(r["backup"]).is_file()
    ids = {o["order_id"] for o in client.get("/api/orders", params={"q": "M", "limit": 100}).json()["orders"]}
    assert "M1" not in ids and "M2" in ids  # made orders are kept so they never come back
    assert client.post("/api/maintenance/reset", json={"confirm": "nope"}).status_code == 400
    r = client.post("/api/maintenance/reset", json={"confirm": "RESET"})
    assert r.status_code == 200 and Path(r.json()["backup"]).is_file()
    assert client.get("/api/orders").json()["total"] == 0
    assert client.get("/api/maintenance").json()["backups"] >= 2


def _req(path, origin=None, host="127.0.0.1:8317"):
    headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""})


def test_helper_endpoints_only_from_this_mac(monkeypatch):
    from snapstl import server

    monkeypatch.setattr(auth, "enabled", lambda: False)  # local desktop mode
    ext = "chrome-extension://abcdef"
    assert not server._cross_site(_req("/api/helper/next", ext))         # helper allowed
    assert server._cross_site(_req("/api/settings", ext))                 # but nothing else
    assert server._cross_site(_req("/api/helper/next", "https://evil.example"))  # other websites blocked
    assert server._cross_site(_req("/api/helper/next", None, host="evil.example:8317"))  # DNS rebinding
    assert not server._cross_site(_req("/api/state", "http://127.0.0.1:8317"))  # the app's own page
