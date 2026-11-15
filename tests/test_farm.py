"""Print farm: Printers page ↔ Bambuddy, Push to queue (sliced by hand or by the slicer service), job tracking.

Bambuddy and the slicer service are faked at the HTTP layer (`farm._request`); nothing leaves this process.
"""
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from starlette.requests import Request

from snapstl import farm

from .test_flow import build_and_wait, client, helper_work, wait_for  # noqa: F401  (client is a fixture)
from .test_ingest import report

SLICER = "http://slicer.test:3001"


def sliced_bytes(note: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", f"; sliced {note}\nG28\n")
        z.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _filename(body: bytes, field: str = "file") -> str:
    m = re.search(rb'name="' + field.encode() + rb'"; filename="([^"]*)"', body)
    return m.group(1).decode() if m else ""


def _part(body: bytes, field: str) -> bytes:
    m = re.search(rb'name="' + field.encode() + rb'"; filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n(.*?)\r\n--snapstl',
                  body, re.S)
    return m.group(1) if m else b""


class FakeBambuddy:
    """Just enough of Bambuddy v1.3's API (and its slicer sidecar) for SnapSTL."""

    def __init__(self, auth_enabled=False):
        self.auth_enabled = auth_enabled
        self.printers = {1: {"id": 1, "name": "Base 1", "model": "A1", "serial_number": "S1", "ip_address": "10.0.0.11",
                             "location": None, "is_active": True}}
        self.status = {1: {"connected": True, "state": "IDLE"}}
        self.files, self.queue, self.keys, self.slices, self.cleared = {}, {}, [], [], []
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.calls.append((method, url))
        if url.startswith(SLICER):
            if path == "/health":
                return 200, b'{"ok":true}'
            assert path == "/slice" and b'name="exportType"' in body
            prof = json.loads(_part(body, "filamentProfile"))
            self.slices.append({"file": _filename(body), "printer": json.loads(_part(body, "printerProfile")),
                                "filament": prof})
            return 200, sliced_bytes(_filename(body))
        key_ok = (not self.auth_enabled) or headers.get("X-API-Key") in self.keys
        if path == "/api/v1/auth/status":
            return 200, json.dumps({"auth_enabled": self.auth_enabled}).encode()
        if not key_ok:
            return 401, b'{"detail":"Not authenticated"}'
        if method == "POST" and path == "/api/v1/api-keys/":
            self.keys.append("bb_secret_key")
            return 200, json.dumps({"id": 1, "key": "bb_secret_key"}).encode()
        if method == "GET" and path == "/api/v1/printers/":
            return 200, json.dumps(list(self.printers.values())).encode()
        if method == "POST" and path == "/api/v1/printers/":
            b = json.loads(body)
            if b["ip_address"].startswith("192.0.2."):
                return 400, json.dumps({"detail": {"code": "printer_connection_failed",
                                                   "message": "Could not connect to the printer."}}).encode()
            pid = max(self.printers) + 1
            self.printers[pid] = {"id": pid, "name": b["name"], "model": b["model"], "serial_number": b["serial_number"],
                                  "ip_address": b["ip_address"], "location": b["location"], "is_active": True}
            self.status[pid] = {"connected": True, "state": "IDLE"}
            assert "access_code" in b
            return 200, json.dumps(self.printers[pid]).encode()
        m = re.fullmatch(r"/api/v1/printers/(\d+)(/status|/clear-plate)?", path)
        if m:
            pid = int(m.group(1))
            if m.group(2) == "/status":
                return 200, json.dumps({"id": pid, "name": self.printers[pid]["name"], **self.status[pid]}).encode()
            if m.group(2) == "/clear-plate":
                self.cleared.append(pid)
                return 200, b'{"ok":true}'
            if method == "PATCH":
                self.printers[pid].update(json.loads(body))
                return 200, json.dumps(self.printers[pid]).encode()
        if method == "POST" and path.startswith("/api/v1/library/files") and path.endswith("files"):
            fid = len(self.files) + 1
            self.files[fid] = {"filename": _filename(body), "data": _part(body, "file")}
            return 200, json.dumps({"id": fid, "filename": self.files[fid]["filename"]}).encode()
        if method == "POST" and path == "/api/v1/library/files/add-to-queue":
            b = json.loads(body)
            qid = len(self.queue) + 1
            self.queue[qid] = {"id": qid, "status": "pending", "printer_id": b["printer_id"], "library_file_id": b["file_ids"][0]}
            return 200, json.dumps({"added": [{"file_id": b["file_ids"][0], "filename": "x", "queue_item_id": qid}],
                                    "errors": []}).encode()
        m = re.fullmatch(r"/api/v1/queue/(\d+)(/cancel)?", path)
        if m:
            qid = int(m.group(1))
            if qid not in self.queue:
                return 404, b'{"detail":"Queue item not found"}'
            if m.group(2):
                self.queue[qid]["status"] = "cancelled"
            return 200, json.dumps(self.queue[qid]).encode()
        return 404, b'{"detail":"Not Found"}'


@pytest.fixture
def bb(monkeypatch, tmp_path):
    """Fake Bambuddy; this test's batches, models and plates are kept apart and removed afterwards, so the
    other test files still start from an empty database and an empty Downloads folder."""
    from snapstl import cache, db
    from snapstl.batches import MANAGER

    fake = FakeBambuddy()
    monkeypatch.setattr(farm, "_request", fake)
    farm.CONFIG_PATH.unlink(missing_ok=True)
    farm._status_cache.clear()
    old_out, old_cache = MANAGER.output_dir, cache.ROOT
    MANAGER.output_dir = str(tmp_path / "output")
    cache.set_root(str(tmp_path / "Downloads"))
    with db.tx() as con:
        first = con.execute("SELECT COALESCE(MAX(id), 0) FROM batches").fetchone()[0]
    yield fake
    MANAGER.output_dir = old_out
    cache.set_root(str(old_cache))
    with db.tx() as con:
        con.execute("DELETE FROM print_jobs")
        con.execute("DELETE FROM downloads WHERE batch_id > ?", (first,))
        con.execute("DELETE FROM batches WHERE id > ?", (first,))
        con.execute("DELETE FROM orders WHERE order_id LIKE 'PQ%' OR order_id LIKE 'PC%'")
    farm.CONFIG_PATH.unlink(missing_ok=True)


def built_batch(c, tag):
    data = report([
        (tag + "1", "PRINT", "Magicians_35", "Ayra", "Pink", 1),
        (tag + "2", "PRINT", "Magicians_35", "Riya", "Blue", 1),
    ])
    assert c.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    bid = c.post("/api/batches", json={"order_ids": [tag + "1", tag + "2"]}).json()["batch_id"]
    c.post(f"/api/batches/{bid}/download")
    helper_work(c)
    b = build_and_wait(c, bid)
    assert b["status"] == "done", b["summary"].get("errors")
    return bid, Path(b["summary"]["folder"])


def job_of(c, bid, plate):
    return next(j for j in c.get(f"/api/print-jobs?batch_id={bid}").json()["jobs"][::-1] if j["plate"] == plate)


def test_printers_page_connects_and_adds_printers(client, bb):
    r = client.get("/api/farm").json()
    assert r["status"]["connected"] and r["status"]["reachable"]
    assert "api_key" not in r["config"] and "bb_secret_key" not in json.dumps(r)  # never shown
    assert [p["name"] for p in r["printers"]] == ["Base 1"] and r["printers"][0]["role"] == ""

    # a printer Bambuddy can't reach is refused with its own words
    bad = client.post("/api/farm/printers", json={"name": "X", "ip": "192.0.2.5", "serial": "s9", "access_code": "1234",
                                                  "model": "A1", "role": "names", "colors": "pink"})
    assert bad.status_code == 400 and "Could not connect to the printer" in bad.json()["detail"]
    ok = client.post("/api/farm/printers", json={"name": "Names 1", "ip": "10.0.0.21", "serial": "s2", "access_code": "1234",
                                                 "model": "A1", "role": "names", "colors": "Pink, blue"})
    assert ok.status_code == 200, ok.text
    assert client.post("/api/farm/printers/1", json={"role": "bases", "colors": ""}).status_code == 200
    prs = {p["name"]: p for p in client.get("/api/farm").json()["printers"]}
    assert prs["Names 1"]["role"] == "names" and prs["Names 1"]["colors"] == ["blue", "pink"]
    assert prs["Base 1"]["role"] == "bases" and bb.printers[1]["location"] == "Bases"
    assert "1234" not in farm.CONFIG_PATH.read_text()  # access codes are never kept by SnapSTL
    assert oct(farm.CONFIG_PATH.stat().st_mode & 0o777) == "0o600"

    bb.status[1] = {"connected": True, "state": "FINISH", "awaiting_plate_clear": True}
    farm._status_cache.clear()
    assert client.get("/api/farm").json()["printers"][0]["status"]["awaiting_plate_clear"]
    assert client.post("/api/farm/printers/1/clear-plate").status_code == 200 and bb.cleared == [1]


def test_push_to_queue_hand_sliced_and_slicer_service(client, bb):
    client.get("/api/farm")
    client.post("/api/farm/printers/1", json={"role": "bases"})
    client.post("/api/farm/printers", json={"name": "Names pink", "ip": "10.0.0.21", "serial": "s2", "access_code": "1",
                                            "model": "A1", "role": "names", "colors": "pink"})
    client.post("/api/farm/printers", json={"name": "Names blue", "ip": "10.0.0.22", "serial": "s3", "access_code": "1",
                                            "model": "A1", "role": "names", "colors": "blue"})
    bid, folder = built_batch(client, "PQ")
    base_plate, pink, blue = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf", "Names/blue/{Riya,blue}.3mf"

    v = client.get(f"/api/batches/{bid}/plates").json()
    by = {p["file"]: p for p in v["plates"]}
    names = {p["id"]: p["name"] for p in client.get("/api/farm").json()["printers"]}
    assert set(by) == {base_plate, pink, blue}
    assert names[by[base_plate]["suggested"]] == "Base 1"
    assert names[by[pink]["suggested"]] == "Names pink" and names[by[blue]["suggested"]] == "Names blue"
    assert all(p["sliced"] is None for p in v["plates"]) and not v["farm"]["slicer"]
    why = {c["name"]: c["why_not"] for c in by[pink]["choices"]}
    assert why["Names pink"] == "" and "needs Names" in why["Base 1"] and "needs pink" in why["Names blue"]

    # wrong printer for a plate is refused
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": pink, "printer_id": by[blue]["suggested"]}]})
    assert r.status_code == 400 and "needs pink" in r.json()["detail"]

    # not sliced and no slicer service: the job fails with what to do
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    j = wait_for(lambda: (lambda j: j if j["state"] == "failed" else None)(job_of(client, bid, base_plate)))
    assert "Export plate sliced file" in j["error"]

    # sliced by hand in Bambu Studio → Sliced/<same path>.gcode.3mf → Try again → in Bambuddy's queue
    hand = folder / "Sliced" / "Bases" / "{Ayra,pink},{Riya,blue}.gcode.3mf"
    hand.parent.mkdir(parents=True)
    hand.write_bytes(sliced_bytes("by hand"))
    assert client.get(f"/api/batches/{bid}/plates").json()["plates"][0]["sliced"]
    assert client.post(f"/api/print-jobs/{j['id']}/retry").status_code == 200
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, base_plate)))
    sent = bb.files[1]
    assert sent["filename"] == f"{folder.name} - Bases - {{Ayra,pink}},{{Riya,blue}}.gcode.3mf"
    assert b"by hand" in sent["data"] and bb.queue[1]["printer_id"] == 1
    # pushing the same plate again is skipped while it's on its way
    assert client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]}).json()["skipped"] == 1

    # slicer service: the names plates are sliced when pushed, with the name colour as filament colour
    r = client.put("/api/farm/settings", json={"slicer_url": SLICER})
    assert r.status_code == 200 and r.json()["slicer_problem"] == ""
    picks = [{"file": pink, "printer_id": by[pink]["suggested"]}, {"file": blue, "printer_id": by[blue]["suggested"]}]
    assert client.post(f"/api/batches/{bid}/push", json={"plates": picks}).json()["queued"] == 2
    wait_for(lambda: all(job_of(client, bid, f)["state"] == "sent" for f in (pink, blue)))
    assert (folder / "Sliced" / "Names" / "pink" / "{Ayra,pink}.gcode.3mf").is_file()
    s = next(x for x in bb.slices if "Ayra" in x["file"])
    assert s["printer"]["inherits"] == "Bambu Lab A1 0.4 nozzle" and s["filament"]["filament_colour"] == ["#F4A7C0"]

    # Bambuddy prints them: pending → printing → completed
    for q in bb.queue.values():
        q["status"] = "printing"
    farm.WORKER.refresh()
    assert job_of(client, bid, pink)["state"] == "printing"
    for q in bb.queue.values():
        q["status"] = "completed"
    farm.WORKER.refresh()
    v = client.get(f"/api/batches/{bid}/plates").json()
    assert all(p["job"]["state"] == "done" for p in v["plates"]) and v["all_printed"]


def test_cancel_and_login_on_bambuddy(client, bb):
    bb.auth_enabled = True
    r = client.post("/api/farm/connect", json={})
    assert r.status_code == 400 and "API Keys" in r.json()["detail"]  # login on: paste a key made in Bambuddy
    bb.keys.append("bb_made_by_hand")
    r = client.post("/api/farm/connect", json={"api_key": "bb_made_by_hand"})
    assert r.status_code == 200 and r.json()["connected"]
    client.post("/api/farm/printers/1", json={"role": "bases"})
    bid, folder = built_batch(client, "PC")
    plate = "Bases/{Ayra,pink},{Riya,blue}.3mf"
    (folder / "Bases" / "{Ayra,pink},{Riya,blue}.gcode.3mf").write_bytes(sliced_bytes())  # next to the plate works too
    client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": plate, "printer_id": 1}]})
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, plate)))
    assert client.post(f"/api/print-jobs/{j['id']}/cancel").status_code == 200
    assert job_of(client, bid, plate)["state"] == "cancelled" and bb.queue[1]["status"] == "cancelled"


def _req(path, client_ip):
    scope = {"type": "http", "method": "POST", "path": path, "headers": [(b"host", b"snapstl.local:8317")],
             "client": (client_ip, 5000), "query_string": b""}
    return Request(scope)


def test_farm_setup_only_on_this_computer(monkeypatch):
    from snapstl import auth, server

    monkeypatch.setattr(server.SETTINGS, "lan_access", True)
    monkeypatch.setattr(auth, "enabled", lambda: True)
    for path in ("/api/farm/connect", "/api/farm/printers", "/api/farm/printers/1/clear-plate", "/api/farm/settings"):
        assert server._blocked(_req(path, "192.168.1.30")) == "Only available on the SnapSTL computer"
    assert server._blocked(_req("/api/farm", "192.168.1.30")) is None  # others may look
    assert server._blocked(_req("/api/batches/1/push", "192.168.1.30")) is None  # and push (files stay here)


def test_upload_names_fit_sd_cards():
    n = farm._upload_name("Batch 1 - 2026-11-16", "Names pink", "{" + "A" * 300 + ",pink}.gcode.3mf")
    assert n.endswith(".gcode.3mf") and len(n.encode()) <= 200
    assert farm._upload_name("B", "Bases", 'a:b?c.gcode.3mf') == "B - Bases - a_b_c.gcode.3mf"
