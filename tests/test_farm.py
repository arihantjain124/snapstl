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


def tray(i, hex_, material="PLA Basic", remain=80):
    return {"id": i, "tray_color": hex_ + "FF", "tray_type": "PLA", "tray_sub_brands": material, "remain": remain,
            "state": 11, "exists": True}


class FakeBambuddy:
    """Just enough of Bambuddy v1.3's API (and its slicer sidecar) for SnapSTL."""

    def __init__(self, auth_enabled=False):
        self.auth_enabled = auth_enabled
        self.printers = {
            1: {"id": 1, "name": "Base 1", "model": "A1", "serial_number": "S1", "ip_address": "10.0.0.11",
                "location": "Bases", "is_active": True, "access_code": "12345678"},
            2: {"id": 2, "name": "Names A", "model": "A1", "serial_number": "S2", "ip_address": "10.0.0.21",
                "location": "Names row", "is_active": True},
            3: {"id": 3, "name": "Names B", "model": "A1", "serial_number": "S3", "ip_address": "10.0.0.22",
                "location": "Names row", "is_active": True},
        }
        self.status = {
            1: {"connected": True, "state": "IDLE", "temperatures": {"nozzle": 25.0, "bed": 24.0},
                "vt_tray": [tray(254, "FFFFFF")]},                                   # Jade White, external spool
            2: {"connected": True, "state": "IDLE", "ams": [{"id": 0, "tray": [tray(0, "F55A74"), tray(1, "0A2989"),
                                                                              {"id": 2, "state": 9}]}]},  # Pink, Blue, empty
            3: {"connected": True, "state": "IDLE", "ams": [{"id": 0, "tray": [tray(0, "F5547C")]}]},  # Hot Pink
        }
        self.colors = {"colors": {"ffffff": "Jade White", "f55a74": "Pink", "f5547c": "Hot Pink", "0a2989": "Blue"},
                       "by_material": {}}
        self.files, self.queue, self.keys, self.slices, self.cleared, self.patches = {}, {}, [], [], [], []
        self.short_filament = False
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
        if method == "GET" and path == "/api/v1/inventory/colors/map":
            return 200, json.dumps(self.colors).encode()
        if method == "GET" and path == "/api/v1/printers/":
            return 200, json.dumps(list(self.printers.values())).encode()
        m = re.fullmatch(r"/api/v1/printers/(\d+)(/status|/clear-plate)?", path)
        if m:
            pid = int(m.group(1))
            if m.group(2) == "/status":
                return 200, json.dumps({"id": pid, "name": self.printers[pid]["name"], **self.status[pid]}).encode()
            if m.group(2) == "/clear-plate":
                self.cleared.append(pid)
                return 200, b'{"ok":true}'
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
        if method == "GET" and path == "/api/v1/system/info":
            return 200, b'{"app": {"version": "1.3.1"}, "printers": {"total": 3}}'
        if method == "GET" and path.startswith("/api/v1/queue/?printer_id="):
            pid = int(path.split("=")[1])
            return 200, json.dumps([q for q in self.queue.values() if q.get("printer_id") == pid]).encode()
        if method == "POST" and path == "/api/v1/queue/":
            b = json.loads(body)
            assert "library_file_id" in b
            qid = len(self.queue) + 1
            self.queue[qid] = {"id": qid, "status": "pending", **b}
            return 200, json.dumps(self.queue[qid]).encode()
        m = re.fullmatch(r"/api/v1/queue/(\d+)(/cancel|/start)?(\?.*)?", path)
        if m:
            qid = int(m.group(1))
            if qid not in self.queue:
                return 404, b'{"detail":"Queue item not found"}'
            if m.group(2) == "/start":
                if self.short_filament and "skip_filament_check=true" not in (m.group(3) or ""):
                    return 409, b'{"detail":{"code":"filament_deficit","message":"Not enough filament on AMS 1-1"}}'
                self.queue[qid]["manual_start"] = False
            elif m.group(2):
                self.queue[qid]["status"] = "cancelled"
            elif method == "PATCH":
                if self.queue[qid]["status"] != "pending":
                    return 400, b'{"detail":"Can only update pending items"}'
                patch = json.loads(body)
                self.patches.append((qid, patch))
                self.queue[qid].update(patch)
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
    monkeypatch.setattr(farm, "_color_map", (0.0, {}))
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
        con.execute("DELETE FROM orders WHERE order_id LIKE 'PQ%' OR order_id LIKE 'PC%' OR order_id LIKE 'PB%' OR order_id LIKE 'PS%'")
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


def test_printers_page_reads_printers_and_filament_from_bambuddy(client, bb):
    r = client.get("/api/farm").json()
    assert r["status"]["connected"] and r["status"]["reachable"]
    assert "api_key" not in r["config"] and "bb_secret_key" not in json.dumps(r)  # never shown
    prs = {p["name"]: p for p in r["printers"]}
    assert prs["Base 1"]["role"] == "bases" and prs["Names A"]["role"] == "names"
    assert prs["Base 1"]["colors"] == ["jade white"] and prs["Base 1"]["spools"][0]["slot"] == "External"
    assert prs["Names A"]["colors"] == ["blue", "pink"]  # the empty AMS slot is left out
    sp = prs["Names A"]["spools"][0]
    assert (sp["slot"], sp["name"], sp["material"], sp["hex"], sp["remain"]) == ("AMS 1 · 1", "Pink", "PLA Basic", "#F55A74", 80)
    assert prs["Base 1"]["status"]["nozzle"] == 25.0
    # nothing to set up in SnapSTL any more: no add-printer / role endpoints
    assert client.post("/api/farm/printers", json={"name": "x"}).status_code in (404, 405)

    bb.status[1].update(state="FINISH", awaiting_plate_clear=True)
    farm._status_cache.clear()
    assert client.get("/api/farm").json()["printers"][0]["status"]["awaiting_plate_clear"]
    assert client.post("/api/farm/printers/1/clear-plate").status_code == 200 and bb.cleared == [1]


def test_colour_matching_and_location_roles():
    plate_pink = {"kind": "names", "colors": ["pink"], "file": "p"}
    plate_base = {"kind": "bases", "colors": ["pink"], "file": "b"}
    ok = {"connected": True}
    base = {"id": 1, "name": "B", "role": "bases", "colors": ["jade white"], "status": ok}
    exact = {"id": 2, "name": "N1", "role": "names", "colors": ["blue", "pink"], "status": ok}
    close = {"id": 3, "name": "N2", "role": "names", "colors": ["hot pink"], "status": ok}
    both = {"id": 4, "name": "Any", "role": "", "colors": ["jade white", "pink"], "status": ok}
    assert farm.eligible(plate_base, base) == "" and farm.match_note(plate_base, base) == "loaded: jade white"
    assert "makes bases" in farm.eligible(plate_pink, base)
    assert farm.eligible(plate_pink, exact) == "" and farm.match_note(plate_pink, exact) == ""
    assert farm.eligible(plate_pink, close) == "" and farm.match_note(plate_pink, close) == "loaded: hot pink"
    assert "needs blue" in farm.eligible({"kind": "names", "colors": ["blue"]}, close)
    assert farm.eligible(plate_pink, dict(exact, status={"connected": False})) == "offline"
    # exact colour first, then the least busy; a printer with no Location takes both kinds
    sug = farm.suggest([plate_pink, dict(plate_pink, file="p2"), plate_base], [base, exact, close, both], {})
    assert sug["p"] == 2 and sug["p2"] == 4 and sug["b"] == 1
    assert farm.role_of("Bases – left rack") == "bases" and farm.role_of("names") == "names" and farm.role_of("Shed") == ""


def test_push_to_queue_hand_sliced_and_slicer_service(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PQ")
    base_plate, pink, blue = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf", "Names/blue/{Riya,blue}.3mf"

    v = client.get(f"/api/batches/{bid}/plates").json()
    by = {p["file"]: p for p in v["plates"]}
    assert set(by) == {base_plate, pink, blue}
    assert by[base_plate]["suggested"] == 1 and by[pink]["suggested"] == 2 and by[blue]["suggested"] == 2
    assert by[base_plate]["need"] == "white" and by[pink]["need"] == "pink"
    assert all(p["sliced"] is None for p in v["plates"]) and not v["farm"]["slicer"]
    ch = {c["name"]: c for c in by[pink]["choices"]}
    assert ch["Names A"]["why_not"] == "" and ch["Names B"]["note"] == "loaded: hot pink" and "makes bases" in ch["Base 1"]["why_not"]

    # wrong printer for a plate is refused
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": blue, "printer_id": 3}]})
    assert r.status_code == 400 and "needs blue" in r.json()["detail"]

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
    assert bb.queue[1]["manual_start"] is True and j["held"]  # never starts by itself
    assert not any("add-to-queue" in u for _, u in bb.calls)
    # pushing the same plate again is skipped while it's on its way
    assert client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]}).json()["skipped"] == 1

    # slicer service: the names plates are sliced when pushed, with the name colour as filament colour
    r = client.put("/api/farm/settings", json={"slicer_url": SLICER})
    assert r.status_code == 200 and r.json()["slicer_problem"] == ""
    picks = [{"file": pink, "printer_id": 2}, {"file": blue, "printer_id": 2}]
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


def test_print_button_moves_reprints_and_sets_options(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PB")
    pink = "Names/pink/{Ayra,pink}.3mf"
    (folder / "Names" / "pink" / "{Ayra,pink}.gcode.3mf").write_bytes(sliced_bytes())  # sliced, next to the plate
    client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": pink, "printer_id": 2}]})
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, pink)))

    jobs = client.get("/api/farm").json()["jobs"]
    row = next(x for x in jobs if x["id"] == j["id"])
    assert row["latest"] and {c["name"]: c["why_not"] for c in row["choices"]}["Names B"] == ""

    # waiting in Bambuddy's queue → Move to another printer with options (set on the queue item)
    r = client.post(f"/api/print-jobs/{j['id']}/print", json={"printer_id": 3, "options": {"bed_levelling": "on", "timelapse": True}})
    assert r.status_code == 200 and r.json()["moved"]
    assert bb.queue[1]["printer_id"] == 3 and bb.queue[1]["bed_levelling"] == "on" and bb.queue[1]["timelapse"] is True
    j2 = job_of(client, bid, pink)
    assert j2["printer_name"] == "Names B" and j2["options"] == {"bed_levelling": "on", "timelapse": True}
    # a printer without the colour, a bad option, and a printing job are refused
    assert "makes bases" in client.post(f"/api/print-jobs/{j['id']}/print", json={"printer_id": 1}).json()["detail"]
    assert client.post(f"/api/print-jobs/{j['id']}/print", json={"printer_id": 2, "options": {"flow_cali": "maybe"}}).status_code == 400

    # printed → Print again = a new job with its options, applied after it reaches the queue
    bb.queue[1]["status"] = "completed"
    farm.WORKER.refresh()
    r = client.post(f"/api/print-jobs/{j['id']}/print", json={"printer_id": 2, "options": {"flow_cali": "off"}})
    assert r.status_code == 200 and not r.json()["moved"] and r.json()["job_id"] != j["id"]
    wait_for(lambda: job_of(client, bid, pink)["state"] == "sent")
    assert bb.queue[2]["printer_id"] == 2 and bb.queue[2]["flow_cali"] == "off" and bb.queue[2]["manual_start"] is True
    old = next(x for x in client.get("/api/farm").json()["jobs"] if x["id"] == j["id"])
    assert not old["latest"] and "choices" not in old  # only a plate's newest job has a Print button


def test_prints_start_only_when_start_is_pressed(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PS")
    plate = "Bases/{Ayra,pink},{Riya,blue}.3mf"
    (folder / "Bases" / "{Ayra,pink},{Riya,blue}.gcode.3mf").write_bytes(sliced_bytes())
    client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": plate, "printer_id": 1}]})
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, plate)))
    farm.WORKER.refresh()
    assert bb.queue[1]["manual_start"] is True and job_of(client, bid, plate)["held"]

    bb.short_filament = True  # Bambuddy asks for a confirmation → 409, nothing started
    r = client.post(f"/api/print-jobs/{j['id']}/start")
    assert r.status_code == 409 and "Not enough filament" in r.json()["detail"] and bb.queue[1]["manual_start"] is True
    assert client.post(f"/api/print-jobs/{j['id']}/start", json={"force": True}).status_code == 200
    assert bb.queue[1]["manual_start"] is False and not job_of(client, bid, plate)["held"]
    farm.WORKER.refresh()
    assert not job_of(client, bid, plate)["held"]
    # a job that isn't waiting in Bambuddy can't be started
    bb.queue[1]["status"] = "completed"
    farm.WORKER.refresh()
    assert client.post(f"/api/print-jobs/{j['id']}/start").status_code == 400


def test_cancel_and_login_on_bambuddy(client, bb):
    bb.auth_enabled = True
    r = client.post("/api/farm/connect", json={})
    assert r.status_code == 400 and "API Keys" in r.json()["detail"]  # login on: paste a key made in Bambuddy
    bb.keys.append("bb_made_by_hand")
    r = client.post("/api/farm/connect", json={"api_key": "bb_made_by_hand"})
    assert r.status_code == 200 and r.json()["connected"]
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
    for path in ("/api/farm/connect", "/api/farm/printers/1/clear-plate", "/api/farm/settings"):
        assert server._blocked(_req(path, "192.168.1.30")) == "Only available on the SnapSTL computer"
    assert server._blocked(_req("/api/farm", "192.168.1.30")) is None  # others may look
    assert server._blocked(_req("/api/batches/1/push", "192.168.1.30")) is None  # and push (files stay here)


def test_upload_names_fit_sd_cards():
    n = farm._upload_name("Batch 1 - 2026-11-16", "Names pink", "{" + "A" * 300 + ",pink}.gcode.3mf")
    assert n.endswith(".gcode.3mf") and len(n.encode()) <= 200
    assert farm._upload_name("B", "Bases", 'a:b?c.gcode.3mf') == "B - Bases - a_b_c.gcode.3mf"


def test_printer_status_tab_has_everything_but_secrets(client, bb):
    client.get("/api/farm")
    r = client.get("/api/printer-status").json()
    assert r["system"]["app"]["version"] == "1.3.1" and [p["name"] for p in r["printers"]] == ["Base 1", "Names A", "Names B"]
    p1 = r["printers"][0]
    assert p1["printer"]["access_code"] == "•••" and "12345678" not in json.dumps(r)
    assert p1["status"]["vt_tray"][0]["tray_color"] == "FFFFFFFF" and p1["queue"] == []
    assert r["printers"][1]["status"]["ams"][0]["tray"][2] == {"id": 2, "state": 9}  # every field, empty slots too
    d = client.get("/api/printer-status?download=1")
    assert "attachment" in d.headers["content-disposition"] and json.loads(d.text)["printers"][0]["id"] == 1
