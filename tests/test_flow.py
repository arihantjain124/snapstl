"""End-to-end: report → select orders → batch → helper downloads → plates → ZIP; plus DB maintenance."""
import io
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
    """Acts as the SnapSTL Mac itself (127.0.0.1): no login needed, helper allowed."""
    from snapstl import server

    from snapstl.settings import save_settings

    c = TestClient(server.app, base_url="http://127.0.0.1:8317", client=("127.0.0.1", 50000))
    c.__enter__()
    server.SETTINGS.license_passwords = {server.SETTINGS.design_id: TEST_PW}  # downloads need the password
    save_settings(server.SETTINGS)
    DOWNLOADER.paused = False
    DOWNLOADER.pause_s = 0
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


TEST_PW = "test-license"
CONFIRMED = '{"password_set": true, "request": "sent"}'


def watermarked(base: "trimesh.Trimesh") -> "trimesh.Trimesh":
    """Add a MakerWorld-like raised text strip 1 mm proud of the back wall."""
    import trimesh

    lo, hi = base.bounds
    strip = trimesh.creation.box([91, 1, 6])
    strip.apply_translation([(lo[0] + hi[0]) / 2, hi[1] + 0.5, lo[2] + 5.7])
    return trimesh.util.concatenate([base, strip])


def helper_work(c, fail_names=(), max_jobs=50, watermark_names=(), check=CONFIRMED):
    """Act like the Chrome helper until the queue is empty. Returns [(name, compartments)]."""
    seen = []
    for _ in range(max_jobs):
        job = c.get("/api/helper/next", params={"status": "Idle"}).json()["job"]
        if job is None:
            break
        seen.append((job["name"], job["compartments"]))
        assert job["params"]["name"] == job["name"]
        assert job["license_password"] == TEST_PW
        if job["name"] in fail_names:
            c.post(f"/api/helper/jobs/{job['job_id']}/fail", json={"error": "MakerWorld said no"})
            continue
        k = sum(ch.isalpha() for ch in job["name"])
        # plate order deliberately swapped: the app must tell base from name by shape
        base = base_mesh(length=48 * k + 10)
        if job["name"] in watermark_names:
            base = watermarked(base)
        files = {"plate_1": ("plate_1.obj", obj(name_mesh(k)), "text/plain"),
                 "plate_2": ("plate_2.obj", obj(base), "text/plain")}
        r = c.post(f"/api/helper/jobs/{job['job_id']}/result", files=files, data={"check": check} if check else None)
        assert r.status_code == (400 if job["name"] in watermark_names or check != CONFIRMED else 200), r.text
    return seen


def status(c, bid):
    return c.get(f"/api/batches/{bid}").json()["status"]


def build_and_wait(c, bid):
    """Downloads finished → wait for 'models_ready' → press Build plates → wait for done."""
    wait_for(lambda: status(c, bid) == "models_ready")
    assert c.post(f"/api/batches/{bid}/build").status_code == 200
    return wait_for(lambda: (lambda d: d if d["status"].startswith("done") else None)(c.get(f"/api/batches/{bid}").json()))


def test_batch_with_helper_builds_plates(client):
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
    states = {o["order_id"]: o["state"] for o in client.get("/api/orders?tab=all&limit=10").json()["orders"]}
    assert states == {"F1": "in_batch", "F2": "in_batch", "F3": "in_batch", "F4": "ready"}
    assert client.post("/api/batches", json={"order_ids": ["F1"]}).status_code == 400

    # a new batch does nothing until Start download
    assert status(client, bid) == "created"
    assert client.get("/api/helper/next").json()["job"] is None
    assert client.post(f"/api/batches/{bid}/build").status_code == 400  # can't build before downloading
    assert client.post(f"/api/batches/{bid}/download").json()["downloads_queued"] == 3
    seen = helper_work(client)
    assert ("Ayra", 4) in seen and ("Manvi", 5) in seen  # compartments from the scaling table
    assert client.get("/api/state").json()["downloader"]["online"]

    time.sleep(1)
    assert status(client, bid) == "models_ready"  # downloaded, but plates wait for the button
    b = build_and_wait(client, bid)
    assert b["status"] == "done", b["summary"].get("errors")
    assert sorted(p["file"] for p in b["summary"]["plates"]) == [
        "Bases/{Ayra,pink},{Manvi,pink},{Riya,blue}.3mf",
        "Names/blue/{Riya,blue}.3mf",
        "Names/pink/{Ayra,pink},{Manvi,pink}.3mf",
    ]
    folder = Path(b["summary"]["folder"])
    today = time.strftime("%Y-%m-%d")
    assert folder.is_dir() and folder.parent.name == today and folder.name == f"Batch 1 - {today}"
    assert b["summary"]["label"] == folder.name
    assert (folder / "report.csv").is_file() and len(list(folder.rglob("*.3mf"))) == 3
    assert not list(folder.parent.parent.rglob("*.zip"))  # no ZIP written anywhere
    r = client.get(f"/api/batches/{bid}/zip")  # optional on-demand download still works
    assert r.status_code == 200 and len([n for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist() if n.endswith(".3mf")]) == 3
    states = {o["order_id"]: o["state"] for o in client.get("/api/orders?tab=all&limit=10").json()["orders"]}
    assert states["F1"] == "bundled" and states["F4"] == "ready"
    q = client.get("/api/downloads").json()["items"]
    assert {x["name"] for x in q if x["state"] == "done"} >= {"Ayra", "Manvi", "Riya"}


def test_failed_download_retries_then_batch_builds_without_it(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("G1", "PRINT", "Magicians_35", "Broken", "Red", 1),
        ("G2", "PRINT", "Magicians_35", "Kabir", "Red", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["G1", "G2"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download")
    seen = helper_work(client, fail_names=("Broken",))
    assert [n for n, _ in seen].count("Broken") == 3  # two retries, then failed
    b = build_and_wait(client, bid)
    assert b["status"] == "done_with_problems" and any("Broken" in e for e in b["summary"]["errors"])
    o = {x["order_id"]: x["state"] for x in client.get("/api/orders", params={"q": "G", "tab": "all"}).json()["orders"]}
    assert o["G1"] == "ready" and o["G2"] == "bundled"


def test_pause_release_and_cancel(client):
    client.post("/api/ingest", files={"file": ("r.csv", report([("H1", "PRINT", "Magicians_35", "Tara", "Green", 1)]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["H1"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download")
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
    assert client.get("/api/orders", params={"q": "H1", "tab": "all"}).json()["orders"][0]["state"] == "ready"
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
    ids = {o["order_id"] for o in client.get("/api/orders", params={"q": "M", "limit": 100, "tab": "all"}).json()["orders"]}
    assert "M1" not in ids and "M2" in ids  # made orders are kept so they never come back
    assert client.post("/api/maintenance/reset", json={"confirm": "nope"}).status_code == 400
    r = client.post("/api/maintenance/reset", json={"confirm": "RESET"})
    assert r.status_code == 200 and Path(r.json()["backup"]).is_file()
    assert client.get("/api/orders?tab=all").json()["total"] == 0
    assert client.get("/api/maintenance").json()["backups"] >= 2


def test_other_computers_need_portal_and_login(client):
    from snapstl import server

    remote = TestClient(server.app, base_url="http://192.168.1.20:8317", client=("192.168.1.50", 50001))
    # portal off → other computers refused
    assert remote.get("/api/state").status_code == 403
    # turning it on needs a password; only this Mac can change it
    assert client.post("/api/portal", json={"enabled": True}).status_code in (200, 400)
    assert remote.post("/api/portal", json={"enabled": True, "password": "x" * 12}).status_code == 403
    r = client.post("/api/portal", json={"enabled": True, "password": "portal-pass-123"})
    assert r.status_code == 200 and r.json()["lan_access"] and r.json()["password_set"]
    try:
        assert remote.get("/api/state").status_code == 401  # must sign in
        assert remote.get("/", follow_redirects=False).status_code == 307
        assert remote.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert remote.post("/api/login", json={"password": "portal-pass-123"}).status_code == 200
        assert remote.get("/api/state").status_code == 200
        assert remote.get("/api/meta").json()["local"] is False
        # never from another computer: the helper, Finder, portal setup, reset
        assert remote.get("/api/helper/next").status_code == 403
        assert remote.post("/api/downloads/reveal").status_code == 403
        assert remote.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 403
        # this Mac never needs to sign in
        assert client.get("/api/state").status_code == 200 and client.get("/api/meta").json()["local"] is True
    finally:
        client.post("/api/portal", json={"enabled": False})
    assert remote.get("/api/state").status_code == 403


def _req(path, origin=None, host="127.0.0.1:8317", client_ip="127.0.0.1"):
    headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b"",
                    "client": (client_ip, 1234)})


def test_helper_endpoints_only_from_this_mac():
    from snapstl import server

    ext = "chrome-extension://abcdef"
    assert server._blocked(_req("/api/helper/next", ext)) is None                    # helper allowed on this Mac
    assert server._blocked(_req("/api/settings", ext))                              # but nothing else
    assert server._blocked(_req("/api/helper/next", "https://evil.example"))        # other websites blocked
    assert server._blocked(_req("/api/helper/next", None, host="evil.example:8317"))  # DNS rebinding
    assert server._blocked(_req("/api/state", "http://127.0.0.1:8317")) is None      # the app's own page
    assert server._blocked(_req("/api/helper/next", ext, host="192.168.1.20:8317", client_ip="192.168.1.50"))


def test_rebuild_and_redownload_finished_batch(client):
    from snapstl import cache

    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("R1", "PRINT", "Magicians_35", "Neha", "Pink", 1),
        ("R2", "PRINT", "Magicians_35", "Ritu", "Pink", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["R1", "R2"]}).json()["batch_id"]
    assert client.post(f"/api/batches/{bid}/download").json()["downloads_queued"] == 2
    helper_work(client)
    first = build_and_wait(client, bid)
    folder1 = first["summary"]["folder"]

    # Rebuild: new Batch folder from the saved models, old folder kept
    assert client.post(f"/api/batches/{bid}/build").status_code == 200
    again = wait_for(lambda: (lambda d: d if d["status"].startswith("done") and d["summary"]["folder"] != folder1 else None)(
        client.get(f"/api/batches/{bid}").json()))
    assert Path(folder1).is_dir() and Path(again["summary"]["folder"]).is_dir()
    assert again["summary"]["previous_folders"] == [folder1]
    assert client.get("/api/helper/next").json()["job"] is None  # rebuild downloads nothing

    # Re-download: every model queued again even though it's saved; progress counts it as not ready
    assert client.post(f"/api/batches/{bid}/download").status_code == 400  # plain queue refused on a finished batch
    assert client.post(f"/api/batches/{bid}/download", json={"force": True}).json()["downloads_queued"] == 2
    b = client.get(f"/api/batches/{bid}").json()
    assert b["status"] == "waiting" and b["progress"]["ready"] == 0
    q = client.get("/api/downloads").json()["items"]
    assert {x["batch_id"] for x in q if x["state"] == "queued"} == {bid}
    # Stop the re-download: batch goes back to done, plates untouched
    assert client.post(f"/api/batches/{bid}/cancel").status_code == 200
    assert client.get(f"/api/batches/{bid}").json()["status"] == "done"
    assert client.get("/api/helper/next").json()["job"] is None

    # models live in readable folders in the Downloads folder
    names = sorted(p.name.split(" - ")[0] for p in cache.ROOT.iterdir() if p.is_dir())
    assert "Neha" in names and "Ritu" in names
    neha = next(p for p in cache.ROOT.iterdir() if p.name.startswith("Neha - 4 comp - "))
    assert (neha / "base.stl").is_file() and (neha / "name.stl").is_file() and (neha / "info.json").is_file()
    # reset never deletes downloaded models
    client.post("/api/maintenance/reset", json={"confirm": "RESET"})
    assert neha.is_dir()


def test_old_cache_is_moved_to_downloads_folder(tmp_path, monkeypatch):
    import json as _json

    from snapstl import cache

    from .conftest import base_mesh as bm, name_mesh as nm

    old = tmp_path / "oldcache"
    key = "abc123def456abc123def456"
    (old / key).mkdir(parents=True)
    bm().export(old / key / "base.stl")
    nm(4).export(old / key / "name.stl")
    (old / key / "meta.json").write_text(_json.dumps({"name": "Ayra", "compartments": 4}))
    monkeypatch.setattr(cache, "OLD_CACHE_DIR", old)
    root = tmp_path / "Downloads"
    assert cache.migrate_old_cache(root) == 1
    d = root / f"Ayra - 4 comp - {key}"
    assert (d / "base.stl").is_file() and (d / "info.json").is_file()
    assert cache.load_exists(key, root) and not old.exists()


def _report2(rows) -> bytes:
    import csv as _csv
    import io as _io

    f = _io.StringIO()
    w = _csv.writer(f)
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Mobile", "Address", "City", "State", "Pincode", "Product Customisation"])
    for oid, club, status, name, color, cust, phone in rows:
        w.writerow([oid, club, "14-11-2026", status, "Magicians_35", 1, cust, phone, "12 Main Rd", "Pune", "MH", "411001",
                    f"Enter Your First Name: {name}\nColor: {color}\n"])
    return f.getvalue().encode()


def test_tabs_filters_clubbed_and_customer_fields(client):
    client.post("/api/maintenance/reset", json={"confirm": "RESET"})
    data = _report2([
        ("T1", "", "PRINT", "Ayra", "Pink", "Asha K", "9000000001"),
        ("T2", "C9", "PRINT", "Manvi", "Pink", "Ravi P", "9000000002"),
        ("T3", "C9", "PRINT", "Riya", "Blue", "Ravi P", "9000000002"),
        ("T4", "", "DELIVERED", "Zoya", "Red", "Meena", "9000000004"),
        ("T5", "", "RTO_DELIVERED", "Kabir", "Red", "Dev", "9000000005"),
        ("T6", "", "IN-TRANSIT", "Tara", "Red", "Sam", "9000000006"),
        ("T7", "", "CANCELLED INITIATED", "Isha", "Red", "Neha", "9000000007"),
        ("T8", "", "PACK", "Om", "Red", "Raj", "9000000008"),
        ("T9", "", "PRINT", "Kiara", "Green", "Lata", "9000000009"),
    ])
    assert client.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    client.post("/api/orders/T9/skip")
    ids = lambda tab, **f: sorted(o["order_id"] for o in client.get("/api/orders", params={"tab": tab, **f}).json()["orders"])  # noqa: E731
    assert ids("ready") == ["T1", "T2", "T3"]
    assert ids("ready_single") == ["T1"] and ids("ready_clubbed") == ["T2", "T3"]
    assert ids("skipped") == ["T9"]
    assert ids("delivered") == ["T4"] and ids("rto") == ["T5"] and ids("in_transit") == ["T6"]
    assert ids("cancelled") == ["T7"] and ids("others") == ["T8"]
    # per-column filters combine (AND)
    assert ids("ready", f_color="pink") == ["T1", "T2"]
    assert ids("ready", f_color="pink", f_customer="ravi") == ["T2"]
    assert ids("all", f_phone="0004") == ["T4"]
    assert ids("all", f_address="pune", f_status="PACK") == ["T8"]
    assert client.get("/api/orders/ids", params={"tab": "ready", "f_color": "pink"}).json() == ["T1", "T2"]
    o = client.get("/api/orders", params={"tab": "ready_single"}).json()["orders"][0]
    assert o["customer_name"] == "Asha K" and o["phone"] == "9000000001" and o["address"] == "12 Main Rd, Pune, MH, 411001"
    facets = client.get("/api/orders/facets", params={"tab": "all"}).json()
    assert facets["tabs"]["ready_clubbed"] == 2 and "PACK" in facets["statuses"] and "pink" in facets["colors"]


# ---------------- license password + watermark ----------------
def test_download_needs_license_password(client):
    from snapstl import server
    from snapstl.settings import save_settings

    client.post("/api/ingest", files={"file": ("r.csv", report([("P1", "PRINT", "Magicians_35", "Tara", "Pink", 1)]),
                                               "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["P1"]}).json()["batch_id"]
    server.SETTINGS.license_passwords = {}
    save_settings(server.SETTINGS)
    r = client.post(f"/api/batches/{bid}/download")
    assert r.status_code == 400 and "license password" in r.json()["detail"]
    # added later in Settings: the existing batch picks it up
    server.SETTINGS.license_passwords = {server.SETTINGS.design_id: TEST_PW}
    save_settings(server.SETTINGS)
    assert client.post(f"/api/batches/{bid}/download").json()["downloads_queued"] == 1
    assert helper_work(client) == [("Tara", 4)]


def test_unconfirmed_or_watermarked_download_is_not_saved(client):
    from snapstl import cache

    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("W1", "PRINT", "Magicians_35", "Ishaan", "Blue", 1),
        ("W2", "PRINT", "Magicians_35", "Mira", "Blue", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["W1", "W2"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download")
    # old helper (no confirmation) → rejected every time → failed
    seen = helper_work(client, check="")
    assert len(seen) == 6  # 2 names × 3 tries
    items = client.get("/api/downloads").json()["items"]
    assert all(x["state"] == "failed" and "license password" in x["error"] for x in items if x["batch_id"] == bid)
    assert not any(cache.load_exists(m["key"]) for m in client.get(f"/api/batches/{bid}/models").json()["items"])

    # watermark still on the base → rejected and retried; Mira comes in clean
    client.post("/api/downloads/retry")
    seen = helper_work(client, watermark_names=("Ishaan",))
    assert [n for n, _ in seen].count("Ishaan") == 3
    models = {m["name"]: m for m in client.get(f"/api/batches/{bid}/models").json()["items"]}
    assert models["Mira"]["status"] == "ready" and models["Mira"]["watermark"] is False
    assert models["Mira"]["license_check"].startswith("confirmed")
    assert models["Ishaan"]["status"] == "failed" and "watermark" in models["Ishaan"]["error"]
    wm = [x for x in client.get("/api/downloads").json()["items"] if x["name"] == "Ishaan"]
    assert wm and wm[0]["watermark"] == 1
    info = (cache.find(models["Mira"]["key"]) / "info.json").read_text()
    assert TEST_PW not in info and '"watermark": false' in info

    # pictures of the downloaded base
    for side in ("back", "top"):
        r = client.get(f"/api/models/{models['Mira']['key']}/view.png", params={"side": side})
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/api/models/../view.png").status_code == 404


def test_build_holds_back_watermarked_model_and_object_redownload(client):
    from snapstl import cache

    client.post("/api/ingest", files={"file": ("r.csv", report([
        ("H1", "PRINT", "Magicians_35", "Dev", "Pink", 1),
        ("H2", "PRINT", "Magicians_35", "Anu", "Pink", 1),
    ]), "text/csv")})
    bid = client.post("/api/batches", json={"order_ids": ["H1", "H2"]}).json()["batch_id"]
    client.post(f"/api/batches/{bid}/download")
    helper_work(client)
    models = {m["name"]: m for m in client.get(f"/api/batches/{bid}/models").json()["items"]}
    # simulate an old download that still carries the watermark (saved before this check existed)
    d = cache.find(models["Dev"]["key"])
    base, name = cache.load(models["Dev"]["key"])
    watermarked(base).export(d / "base.stl")

    b = build_and_wait(client, bid)
    s = b["summary"]
    assert b["status"] == "done_with_problems"
    assert s["watermark"] == {"checked": 2, "found": 1}
    assert any("Dev" in e and "watermark" in e for e in s["errors"])
    assert all("Dev" not in n for p in s["plates"] for n in p["names"])
    bases = [p for p in s["plates"] if p["kind"] == "bases"]
    assert bases and all(p["image"] for p in bases)
    r = client.get(f"/api/batches/{bid}/plate-image", params={"f": bases[0]["image"]})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/batches/{bid}/plate-image", params={"f": "../../snapstl.db"}).status_code == 404
    o = {x["order_id"]: x["state"] for x in client.get("/api/orders", params={"tab": "all"}).json()["orders"]}
    assert o["H1"] == "ready" and o["H2"] == "bundled"

    # re-download just Dev from the batch's Objects list; rebuild waits for it
    r = client.post(f"/api/batches/{bid}/models/redownload", json={"key": models["Dev"]["key"]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    assert status(client, bid) == "done_with_problems"  # a built batch keeps its status
    assert client.post(f"/api/batches/{bid}/build").status_code == 400  # still downloading
    assert client.post(f"/api/batches/{bid}/models/redownload", json={"key": "nope"}).status_code == 400
    assert helper_work(client) == [("Dev", 4)]
    fresh = {m["name"]: m for m in client.get(f"/api/batches/{bid}/models").json()["items"]}["Dev"]
    assert fresh["status"] == "ready" and fresh["watermark"] is False
    assert client.post(f"/api/batches/{bid}/build").status_code == 200
    b = wait_for(lambda: (lambda d: d if d["status"].startswith("done") and d["summary"].get("label") != s["label"]
                          else None)(client.get(f"/api/batches/{bid}").json()))
    assert b["summary"]["watermark"] == {"checked": 2, "found": 0}
    assert any("Dev" in n for p in b["summary"]["plates"] for n in p["names"])
