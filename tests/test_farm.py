"""Print farm: Make It ↔ Bambuddy — printers, slicing with Bambuddy's slicer, print jobs, Ready to ship.

Bambuddy (its slicer included) is faked at the HTTP layer (`farm._request`); nothing leaves this process.
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
    """Just enough of Bambuddy v1.3's API (its slicer included) for SnapSTL."""

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
        self.files, self.queue, self.keys, self._unused, self.cleared, self.patches = {}, {}, [], [], [], []
        self.short_filament = False
        self.slicer = "unreachable"  # Bambuddy's slicer sidecar: "ok" | "unreachable" | "hang"
        self.slice_jobs, self.slice_bodies, self.imports, self.speeds = {}, [], [], []
        self.presets = {"standard": {
            "printer": [{"id": "Bambu Lab A1 0.4 nozzle", "name": "Bambu Lab A1 0.4 nozzle", "source": "standard"}],
            "process": [{"id": "0.20mm Standard @BBL A1", "name": "0.20mm Standard @BBL A1", "source": "standard"}],
            "filament": [{"id": "Bambu PLA Basic @BBL A1", "name": "Bambu PLA Basic @BBL A1", "source": "standard"}]},
            "local": {"process": [{"id": "7", "name": "Organizer 0.24mm", "source": "local"}]}}
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.calls.append((method, url))
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
        if method == "POST" and path == "/api/v1/local-presets/import":
            self.imports.append(_filename(body))
            return 200, json.dumps({"success": True, "imported": 2, "skipped": 0, "errors": []}).encode()
        m = re.fullmatch(r"/api/v1/printers/(\d+)/print-speed\?mode=(\d)", path)
        if m and method == "POST":
            self.speeds.append((int(m.group(1)), int(m.group(2))))
            return 200, b'{"success": true}'
        if method == "GET" and path == "/api/v1/slicer/presets":
            return 200, json.dumps(self.presets).encode()
        m = re.fullmatch(r"/api/v1/library/files/(\d+)/slice", path)
        if m and method == "POST":
            b = json.loads(body)
            self.slice_bodies.append((int(m.group(1)), b))
            jid = len(self.slice_jobs) + 1
            if self.slicer == "ok":
                fid = len(self.files) + 1
                src = self.files[int(m.group(1))]["filename"]
                self.files[fid] = {"filename": src.replace(".3mf", ".gcode.3mf"), "data": sliced_bytes("bambuddy")}
                self.slice_jobs[jid] = {"job_id": jid, "status": "completed", "result": {"library_file_id": fid}}
            elif self.slicer == "hang":
                self.slice_jobs[jid] = {"job_id": jid, "status": "running", "progress": None}
            else:
                self.slice_jobs[jid] = {"job_id": jid, "status": "failed", "error_status": 503,
                                        "error_detail": "Slicer sidecar unreachable: connection refused"}
            return 202, json.dumps({"job_id": jid, "status": "pending"}).encode()
        m = re.fullmatch(r"/api/v1/slice-jobs/(\d+)", path)
        if m:
            j = self.slice_jobs.get(int(m.group(1)))
            return (200, json.dumps(j).encode()) if j else (404, b'{"detail":"Slice job not found or expired"}')
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
    farm.update_config(auto_slice=False)  # tests that want it turn it on
    farm._status_cache.clear()
    monkeypatch.setattr(farm, "_color_map", (0.0, {}))
    monkeypatch.setattr(farm.WORKER, "_models", (0.0, {}))
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
        con.execute("DELETE FROM slices")
        con.execute("DELETE FROM downloads WHERE batch_id > ?", (first,))
        con.execute("DELETE FROM batches WHERE id > ?", (first,))
        con.execute("DELETE FROM orders WHERE order_id LIKE 'PQ%' OR order_id LIKE 'PC%' OR order_id LIKE 'PB%' OR order_id LIKE 'PS%' OR order_id LIKE 'PM%' OR order_id LIKE 'PK%' OR order_id LIKE 'PV%' OR order_id LIKE 'PO%'")
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
    assert "role" not in prs["Base 1"]  # no Bases / Names setting: matched by filament only
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


def test_colour_matching_by_filament_only():
    plate_pink = {"kind": "names", "colors": ["pink"], "file": "p"}
    plate_base = {"kind": "bases", "colors": ["pink"], "file": "b"}
    ok = {"connected": True}
    white = {"id": 1, "name": "B", "colors": ["jade white"], "status": ok}
    exact = {"id": 2, "name": "N1", "colors": ["blue", "pink"], "status": ok}
    close = {"id": 3, "name": "N2", "colors": ["hot pink"], "status": ok}
    mixed = {"id": 4, "name": "Any", "colors": ["jade white", "pink"], "status": ok}
    assert farm.eligible(plate_base, white) == "" and farm.match_note(plate_base, white) == "loaded: jade white"
    assert "needs pink" in farm.eligible(plate_pink, white)  # only white loaded
    assert "needs white" in farm.eligible(plate_base, exact)
    assert farm.eligible(plate_pink, exact) == "" and farm.match_note(plate_pink, exact) == ""
    assert farm.eligible(plate_pink, close) == "" and farm.match_note(plate_pink, close) == "loaded: hot pink"
    assert "needs blue" in farm.eligible({"kind": "names", "colors": ["blue"]}, close)
    assert farm.eligible(plate_pink, dict(exact, status={"connected": False})) == "offline"
    # any printer with the filament takes either kind: exact colour first, then the least busy
    sug = farm.suggest([plate_pink, dict(plate_pink, file="p2"), plate_base, dict(plate_base, file="b2")],
                       [white, exact, close, mixed], {})
    assert sug["p"] == 4 and sug["p2"] == 2 and sug["b"] == 1 and sug["b2"] == 4


def test_push_to_queue_hand_sliced_and_slicer_service(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PQ")
    base_plate, pink, blue = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf", "Names/blue/{Riya,blue}.3mf"

    v = client.get(f"/api/batches/{bid}/plates").json()
    by = {p["file"]: p for p in v["plates"]}
    assert set(by) == {base_plate, pink, blue}
    assert by[base_plate]["suggested"] == 1 and by[pink]["suggested"] == 2 and by[blue]["suggested"] == 2
    assert by[base_plate]["need"] == "white" and by[pink]["need"] == "pink"
    assert all(p["sliced"] is None for p in v["plates"])
    ch = {c["name"]: c for c in by[pink]["choices"]}
    assert ch["Names A"]["why_not"] == "" and ch["Names B"]["note"] == "loaded: hot pink" and "needs pink" in ch["Base 1"]["why_not"]

    # wrong printer for a plate is refused
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": blue, "printer_id": 3}]})
    assert r.status_code == 400 and "needs blue" in r.json()["detail"]

    # not sliced, and Bambuddy's slicer isn't running: the job fails with what to do (it never hangs)
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    j = wait_for(lambda: (lambda j: j if j["state"] == "failed" else None)(job_of(client, bid, base_plate)))
    assert "Slicing failed" in j["error"] and "Use Slicer API" in j["error"]

    # sliced by hand in Bambu Studio → Sliced/<same path>.gcode.3mf → Try again → in Bambuddy's queue
    hand = folder / "Sliced" / "Bases" / "{Ayra,pink},{Riya,blue}.gcode.3mf"
    hand.parent.mkdir(parents=True)
    hand.write_bytes(sliced_bytes("by hand"))
    assert client.get(f"/api/batches/{bid}/plates").json()["plates"][0]["sliced"]
    assert client.post(f"/api/print-jobs/{j['id']}/retry").status_code == 200
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, base_plate)))
    sent = bb.files[bb.queue[1]["library_file_id"]]
    assert sent["filename"] == f"{folder.name} - Bases - {{Ayra,pink}},{{Riya,blue}}.gcode.3mf"
    assert b"by hand" in sent["data"] and bb.queue[1]["printer_id"] == 1
    assert bb.queue[1]["manual_start"] is True and j["held"]  # never starts by itself
    assert not any("add-to-queue" in u for _, u in bb.calls)
    # pushing the same plate again is skipped while it's on its way
    assert client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]}).json()["skipped"] == 1

    # Bambuddy's slicer running: name plates are sliced by Bambuddy when pushed, then the sliced file is queued
    bb.slicer = "ok"
    picks = [{"file": pink, "printer_id": 2}, {"file": blue, "printer_id": 2}]
    assert client.post(f"/api/batches/{bid}/push", json={"plates": picks}).json()["queued"] == 2
    wait_for(lambda: all(job_of(client, bid, f)["state"] == "sent" for f in (pink, blue)))
    src_id, body = next((fid, b) for fid, b in bb.slice_bodies if "Names pink" in bb.files[fid]["filename"])
    assert bb.files[src_id]["filename"].endswith("{Ayra,pink}.3mf")  # the plain plate went up, Bambuddy sliced it
    assert body["printer_preset"] == {"source": "standard", "id": "Bambu Lab A1 0.4 nozzle"}
    assert body["filament_colours"] == ["#F4A7C0"] and body["export_3mf"] is True
    qi = next(q for q in bb.queue.values() if bb.files[q["library_file_id"]]["filename"].endswith("{Ayra,pink}.gcode.3mf"))
    assert qi["manual_start"] is True and not (folder / "Sliced" / "Names").exists()  # nothing written locally

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
    assert "needs pink" in client.post(f"/api/print-jobs/{j['id']}/print", json={"printer_id": 1}).json()["detail"]
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


def makeit_row(c, section, file):
    return next(r for r in c.get("/api/makeit").json()[section] if r["file"] == file)


def test_make_it_bases_names_start_completed_ready_to_ship(client, bb):
    from snapstl import makeit

    bb.printers[4] = {"id": 4, "name": "Base 2", "model": "A1", "serial_number": "S4", "ip_address": "10.0.0.12",
                      "location": "Bases", "is_active": True}
    bb.status[4] = {"connected": True, "state": "IDLE", "vt_tray": [tray(254, "FFFFFF", remain=95)]}  # more white left
    client.get("/api/farm")
    bid, folder = built_batch(client, "PM")
    base, pink, blue = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf", "Names/blue/{Riya,blue}.3mf"
    for f in (base, pink, blue):
        (folder / (f[:-4] + ".gcode.3mf")).write_bytes(sliced_bytes(f))

    m = client.get("/api/makeit").json()
    assert [r["file"] for r in m["bases"]] == [base] and {r["file"] for r in m["names"]} == {pink, blue}
    assert m["bases"][0]["order_ids"] == ["PM1", "PM2"] and m["completed"] == []
    assert m["bases"][0]["suggested"] == 4  # available base printer with the most white left
    assert makeit_row(client, "names", pink)["suggested"] == 2 and makeit_row(client, "names", blue)["suggested"] is None
    # (Names A has blue too, but it's already picked for pink — only *available* printers are pre-selected)

    # nothing is sent until Start print; then it is sent AND started (that click is the start)
    assert bb.queue == {}
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 4,
                                               "options": {"timelapse": True}})
    assert r.status_code == 200, r.text
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    assert bb.queue[1]["printer_id"] == 4 and bb.queue[1]["timelapse"] is True
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 4}).status_code == 400
    # wrong colour refused
    assert "needs blue" in client.post("/api/makeit/start", json={"batch_id": bid, "file": blue, "printer_id": 3}).json()["detail"]

    # Bambuddy warns on start → stays held; Start print again with force starts it
    bb.short_filament = True
    client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    j = wait_for(lambda: (lambda r: r["job"] if r["job"] and r["job"]["state"] == "sent" and r["job"]["error"] else None)(
        makeit_row(client, "names", pink)))
    assert j["held"] and "anyway" in j["error"]
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    assert r.status_code == 409
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2, "force": True}).status_code == 200
    bb.short_filament = False
    client.post("/api/makeit/start", json={"batch_id": bid, "file": blue, "printer_id": 2})
    wait_for(lambda: len(bb.queue) == 3 and bb.queue[3].get("manual_start") is False)

    # printed: base + pink done → Ayra (PM1) ready to ship; Riya waits for blue
    bb.queue[1]["status"] = bb.queue[2]["status"] = "completed"
    farm.WORKER.refresh()
    m = client.get("/api/makeit").json()
    assert {r["file"] for r in m["completed"]} == {base, pink} and [r["file"] for r in m["names"]] == [blue]
    tab = lambda t: [o["order_id"] for o in client.get(f"/api/orders?tab={t}&limit=50").json()["orders"] if o["order_id"].startswith("PM")]
    assert tab("ready_to_ship") == ["PM1"] and tab("bundled") == ["PM2"]
    bb.queue[3]["status"] = "completed"
    farm.WORKER.refresh()
    assert sorted(tab("ready_to_ship")) == ["PM1", "PM2"] and client.get("/api/makeit").json()["names"] == []

    # Reprint (after a failure is noticed): back to the queue, its orders leave Ready to ship
    assert client.post("/api/makeit/reprint", json={"batch_id": bid, "file": pink}).status_code == 200
    m = client.get("/api/makeit").json()
    assert [r["file"] for r in m["names"]] == [pink] and m["names"][0]["job"]["state"] == "reprint"
    assert tab("ready_to_ship") == ["PM2"] and tab("bundled") == ["PM1"]
    assert client.post("/api/makeit/reprint", json={"batch_id": bid, "file": blue}).status_code == 200  # also done
    assert client.post("/api/makeit/reprint", json={"batch_id": bid, "file": pink}).status_code == 400  # not completed


def test_plate_orders_for_batches_built_before_order_ids():
    from snapstl import makeit

    plates = [{"kind": "bases", "file": "B", "names": ["Ayra", "Ayra", "Riya"], "colors": ["pink", "blue", "pink"]},
              {"kind": "names", "file": "N1", "names": ["Ayra"], "colors": ["pink"]},
              {"kind": "names", "file": "N2", "names": ["Ayra"], "colors": ["blue"]}]
    orders = [{"order_id": "A1", "name": "Ayra", "color": "pink", "quantity": 1},
              {"order_id": "A2", "name": "Ayra", "color": "blue", "quantity": 1},
              {"order_id": "R1", "name": "Riya", "color": "pink", "quantity": 1}]
    on = makeit.plate_order_ids(plates, orders)
    assert sorted(on["B"]) == ["A1", "A2", "R1"] and on["N1"] == ["A1"] and on["N2"] == ["A2"]


def test_make_it_slice_with_bambuddy_and_remove(client, bb, monkeypatch):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PK")
    base, pink = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf"

    # presets come from Bambuddy; Bambu's standard A1 ones are chosen until someone picks others
    p = client.get("/api/makeit/presets").json()
    assert p["names"] == {"printer": "Bambu Lab A1 0.4 nozzle", "process": "0.20mm Standard @BBL A1",
                          "filament": "Bambu PLA Basic @BBL A1"}
    assert [x["key"] for x in p["available"]["process"]] == ["local:7", "standard:0.20mm Standard @BBL A1"]
    r = client.put("/api/makeit/presets", json={**p["chosen"], "process": "local:7"})
    assert r.status_code == 200 and r.json()["names"]["process"] == "Organizer 0.24mm"
    assert client.put("/api/makeit/presets", json={**p["chosen"], "process": "local:99"}).status_code == 400

    # Slice: Bambuddy slices it in the background; the row shows it sliced; nothing is queued to print
    bb.slicer = "ok"
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": base}]}).json()["queued"] == 1
    wait_for(lambda: makeit_row(client, "bases", base)["sliced"])
    assert bb.slice_bodies[-1][1]["process_preset"] == {"source": "local", "id": "7"} and bb.queue == {}
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": base}]}).json()["queued"] == 0
    # Start print then sends Bambuddy's sliced file — no second slice, nothing written locally
    n = len(bb.slice_bodies)
    client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 1})
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    assert len(bb.slice_bodies) == n and bb.files[bb.queue[1]["library_file_id"]]["filename"].endswith(".gcode.3mf")
    assert not (folder / "Sliced").exists()

    # a slice that makes no progress is given up with a clear message (never stuck)
    bb.slicer = "hang"
    monkeypatch.setattr(farm, "SLICE_TIMEOUT_S", 0)
    client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink}]})
    s = wait_for(lambda: (lambda s: s if s and s["state"] == "failed" else None)(makeit_row(client, "names", pink)["slice"]))
    assert "no progress" in s["error"]
    st = client.get("/api/printer-status").json()["slicing"]
    assert st["counts"]["failed"] == 1 and st["counts"]["done"] == 1 and st["presets"]["process"] == "Organizer 0.24mm"

    # Remove takes a plate off Names (nothing deleted); Put back returns it
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": pink}).status_code == 200
    m = client.get("/api/makeit").json()
    assert pink not in [r["file"] for r in m["names"]] and [r["file"] for r in m["removed"]] == [pink]
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": base}).status_code == 400  # on its way
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": pink, "put_back": True}).status_code == 200
    m = client.get("/api/makeit").json()
    assert pink in [r["file"] for r in m["names"]] and m["removed"] == []


def test_no_presets_listed_falls_back_to_standard(client, bb):
    bb.presets = {}  # Bambuddy's slicer isn't running: it lists nothing
    p = client.get("/api/makeit/presets").json()
    assert not p["bambuddy_listed"] and p["chosen"]["printer"] == "standard:Bambu Lab A1 0.4 nozzle"
    assert client.put("/api/makeit/presets", json=p["chosen"]).status_code == 200


def test_presets_per_printer_auto_slice_import_and_speed(client, bb):
    bb.printers[4] = {"id": 4, "name": "P1S names", "model": "P1S", "serial_number": "S4", "ip_address": "10.0.0.31",
                      "location": "", "is_active": True}
    bb.status[4] = {"connected": True, "state": "IDLE", "ams": [{"id": 0, "tray": [tray(0, "F55A74")]}]}  # Pink
    bb.presets["local"]["printer"] = [{"id": "11", "name": "P1S organizers", "source": "local"}]
    bb.slicer = "ok"
    client.get("/api/farm")
    bid, folder = built_batch(client, "PV")
    base, pink = "Bases/{Ayra,pink},{Riya,blue}.3mf", "Names/pink/{Ayra,pink}.3mf"

    # the P1S printer gets its own printer preset; the others keep the default
    p = client.get("/api/makeit/presets?printer_id=4").json()
    assert [x["name"] for x in p["printers"]] == ["Base 1", "Names A", "Names B", "P1S names"] and p["auto_slice"] is False
    assert client.put("/api/makeit/presets", json={**p["chosen"], "printer_id": 4, "printer": "local:11"}).status_code == 200
    p4 = client.get("/api/makeit/presets?printer_id=4").json()
    assert p4["names"]["printer"] == "P1S organizers" and [x["own"] for x in p4["printers"]] == [False, False, False, True]
    assert client.get("/api/makeit/presets?printer_id=2").json()["names"]["printer"] == "Bambu Lab A1 0.4 nozzle"

    # automatic slicing: each waiting plate, for the printer pre-selected for it (and only that one)
    assert client.put("/api/makeit/settings", json={"auto_slice": True}).json()["auto_slice"] is True
    farm.WORKER.auto_slice()
    wait_for(lambda: all(r["sliced"] for r in client.get("/api/makeit").json()["names"] + client.get("/api/makeit").json()["bases"]
                         if r["suggested"]))
    n = len(bb.slice_bodies)
    farm.WORKER.auto_slice()
    assert len(bb.slice_bodies) == n  # nothing sliced twice

    # Start print on the P1S with Sport speed: sends the P1S slice; speed applied once printing
    client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 4, "speed": "sport"})
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    sent = bb.files[bb.queue[1]["library_file_id"]]
    src = next(fid for fid, b in bb.slice_bodies if b["printer_preset"]["id"] == "11" and "Names pink" in bb.files[fid]["filename"])
    assert sent["filename"] == bb.files[src]["filename"].replace(".3mf", ".gcode.3mf")
    assert bb.speeds == []
    bb.queue[1]["status"] = "printing"
    farm.WORKER.refresh()
    assert bb.speeds == [(4, 3)]
    farm.WORKER.refresh()
    assert bb.speeds == [(4, 3)]  # once
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 1,
                                                  "speed": "warp"}).status_code == 400

    # a failed automatic slice is not retried by itself
    bb.slicer = "unreachable"
    farm.request_slices([(bid, base, "bases", "")], [makeit_row(client, "bases", base)["suggested"]], again=True)
    wait_for(lambda: (makeit_row(client, "bases", base)["slice"] or {}).get("state") == "failed")
    bases_sliced = lambda: sum(1 for fid, _ in bb.slice_bodies if " - Bases - " in bb.files[fid]["filename"])  # noqa: E731
    n = bases_sliced()
    farm.WORKER.auto_slice()
    farm.WORKER.slice_next()
    assert bases_sliced() == n

    # preset files go to Bambuddy, one kind at a time
    r = client.post("/api/makeit/presets/import", data={"kind": "filament"},
                    files={"file": ("Silk PLA.bbsflmt", b"PK\x03\x04 bundle", "application/octet-stream")})
    assert r.status_code == 200 and r.json() == {"imported": 2, "skipped": 0, "errors": [], "kind": "filament"}
    assert bb.imports == ["Silk PLA.bbsflmt"]
    bad = client.post("/api/makeit/presets/import", files={"file": ("notes.txt", b"x", "text/plain")})
    assert bad.status_code == 400
