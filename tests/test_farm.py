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

from snapstl import db, farm

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


STD = {"printer": {"source": "standard", "id": "Bambu Lab A1 0.4 nozzle", "name": "Bambu Lab A1 0.4 nozzle"},
       "process": {"source": "standard", "id": "0.20mm Standard @BBL A1", "name": "0.20mm Standard @BBL A1"},
       "filament": {"source": "standard", "id": "Bambu PLA Basic @BBL A1", "name": "Bambu PLA Basic @BBL A1"}}


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
        self.refreshed = []
        self.short_filament = False
        self.slicer = "unreachable"  # Bambuddy's slicer sidecar: "ok" | "unreachable" | "hang"
        self.slice_jobs, self.slice_bodies, self.imports, self.speeds = {}, [], [], []
        self.speed_ignored = False  # a printer that doesn't take the speed command
        self.cloud_keys, self.cloud_signed_in = [], True  # keys with "Allow cloud access"; the owner's sign-in
        self.presets = {"standard": {
            "printer": [{"id": "Bambu Lab A1 0.4 nozzle", "name": "Bambu Lab A1 0.4 nozzle", "source": "standard"}],
            "process": [{"id": "0.20mm Standard @BBL A1", "name": "0.20mm Standard @BBL A1", "source": "standard"}],
            "filament": [{"id": "Bambu PLA Basic @BBL A1", "name": "Bambu PLA Basic @BBL A1", "source": "standard"}]},
            "local": {"process": [{"id": "7", "name": "Organizer 0.24mm", "source": "local"}]}}
        self.calls = []
        self.preset_reads = []
        self.key_requests = []

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
            if json.loads(body).get("can_access_cloud") and not self.auth_enabled:  # as Bambuddy 1.3.1
                return 400, b'{"detail":"can_access_cloud requires authentication to be enabled (per-user cloud tokens)"}'
            self.key_requests.append(json.loads(body))
            self.keys.append("bb_secret_key")
            return 200, json.dumps({"id": 1, "key": "bb_secret_key"}).encode()
        if method == "GET" and path == "/api/v1/cloud/status":
            if self.auth_enabled and headers.get("X-API-Key") not in self.cloud_keys:
                return 403, b'{"detail":"API key does not have cloud access"}'
            return 200, json.dumps({"is_authenticated": self.cloud_signed_in, "sign_in_expired": False}).encode()
        if method == "GET" and path == "/api/v1/inventory/colors/map":
            return 200, json.dumps(self.colors).encode()
        if method == "GET" and path == "/api/v1/printers/":
            return 200, json.dumps(list(self.printers.values())).encode()
        m = re.fullmatch(r"/api/v1/printers/(\d+)(/status|/clear-plate|/refresh-status)?", path)
        if m:
            pid = int(m.group(1))
            if m.group(2) == "/refresh-status":
                self.refreshed.append(pid)
                return 200, b'{"success": true}'
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
            if not self.speed_ignored:  # Bambuddy answers "sent"; the printer reports the new level later
                self.status.setdefault(int(m.group(1)), {})["speed_level"] = int(m.group(2))
            return 200, b'{"success": true}'
        if method == "GET" and path.split("?")[0] == "/api/v1/slicer/presets":
            self.preset_reads.append(path)
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
    farm.update_config(presets_by_model={"A1": STD},  # A1's presets, as set before 0.22.7
                       presets_by_printer={}, )
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
        for t in ("plate_checks", "extra_plates", "extra_items", "staff", "bed_clears", "activity"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM downloads WHERE batch_id > ?", (first,))
        con.execute("DELETE FROM batches WHERE id > ?", (first,))
        con.execute("DELETE FROM orders WHERE order_id LIKE 'PQ%' OR order_id LIKE 'PC%' OR order_id LIKE 'PB%' OR order_id LIKE 'PS%' OR order_id LIKE 'PM%' OR order_id LIKE 'PK%' OR order_id LIKE 'PV%' OR order_id LIKE 'PO%' OR order_id LIKE 'PN%'")
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


def _sp(tray, color, hexv, remain=50):
    return {"slot": f"AMS 1 · {tray % 4 + 1}" if tray < 254 else "External", "tray": tray, "color": color,
            "name": color.title(), "hex": hexv, "material": "PLA Basic", "remain": remain}


def test_any_printer_and_best_filament_slot():
    """Nothing is ruled out by colour (owner decision 0.22.20): the slot closest to the colour is suggested."""
    plate_pink = {"kind": "names", "colors": ["pink"], "file": "p"}
    plate_base = {"kind": "bases", "colors": ["pink"], "file": "b"}
    ok = {"connected": True}
    white = {"id": 1, "name": "B", "colors": ["jade white"], "status": ok, "spools": [_sp(254, "jade white", "#FFFFFF")]}
    exact = {"id": 2, "name": "N1", "colors": ["blue", "pink"], "status": ok,
             "spools": [_sp(0, "blue", "#0A2989"), _sp(1, "pink", "#F55A74", 20), _sp(2, "pink", "#F55A74", 90)]}
    close = {"id": 3, "name": "N2", "colors": ["hot pink"], "status": ok, "spools": [_sp(4, "hot pink", "#F5547C")]}
    red = {"id": 5, "name": "R", "colors": ["red"], "status": ok, "spools": [_sp(0, "red", "#E5534B"), _sp(1, "black", "#000000")]}
    for pr in (white, exact, close, red, dict(exact, status={"connected": False})):
        assert farm.eligible(plate_pink, pr) == "" and farm.eligible(plate_base, pr) == ""  # never greyed out
    assert farm.best_tray("pink", exact) == 2          # exact colour, most filament left
    assert farm.best_tray("pink", close) == 4 and farm.match_note(plate_pink, close) == "close pink: hot pink loaded"
    assert farm.best_tray("pink", red) == 0            # no pink: the nearest colour (red, not black)
    assert farm.match_note(plate_pink, red) == "no pink: red loaded"
    assert farm.best_tray("white", white) == 254 and farm.match_note(plate_base, white) == "close white: jade white loaded"
    assert farm.best_tray("pink", {"spools": []}) is None
    assert [t["tray"] for t in farm.tray_choices("pink", exact)] == [1, 2, 0]  # matches first
    # a person may pick any loaded slot; one that isn't loaded is refused
    assert farm.pick_tray(plate_pink, red, "1") == 1 and farm.pick_tray(plate_pink, red, None) == 0
    with pytest.raises(farm.FarmError, match="no filament in that slot"):
        farm.pick_tray(plate_pink, red, 7)
    # global tray ids: AMS n slot k = n*4+k, AMS HT = its id, external = 254
    assert farm.tray_id({"id": 1}, {"id": 2}) == 6 and farm.tray_id({"id": 128}, {"id": 0}) == 128
    assert farm.tray_id(None, {"id": 254}) == 254


def test_push_to_queue_hand_sliced_and_slicer_service(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PQ")
    base_plate, pink, blue = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf", "Names/blue/Blue - Riya.3mf"

    v = client.get(f"/api/batches/{bid}/plates").json()
    by = {p["file"]: p for p in v["plates"]}
    assert set(by) == {base_plate, pink, blue}
    assert by[base_plate]["suggested"] == 1 and by[pink]["suggested"] == 2 and by[blue]["suggested"] == 2
    assert by[base_plate]["need"] == "white" and by[pink]["need"] == "pink"
    assert all(p["sliced"] is None for p in v["plates"])
    ch = {c["name"]: c for c in by[pink]["choices"]}
    assert all(c["why_not"] == "" for c in ch.values())  # any printer may print any plate (0.22.20)
    assert ch["Names A"]["note"] == "" and ch["Names B"]["note"] == "close pink: hot pink loaded"

    # not sliced, and Bambuddy's slicer isn't running: the job fails with what to do (it never hangs)
    r = client.post(f"/api/batches/{bid}/push", json={"plates": [{"file": base_plate, "printer_id": 1}]})
    assert r.status_code == 200 and r.json()["queued"] == 1
    j = wait_for(lambda: (lambda j: j if j["state"] == "failed" else None)(job_of(client, bid, base_plate)))
    assert "Slicing failed" in j["error"] and "Use Slicer API" in j["error"]

    # only Bambuddy's slicer slices: a sliced file put in the batch folder by hand is never used
    hand = folder / "Sliced" / "Bases" / "Bases - Ayra + Riya.gcode.3mf"
    hand.parent.mkdir(parents=True)
    hand.write_bytes(sliced_bytes("by hand"))
    assert not client.get(f"/api/batches/{bid}/plates").json()["plates"][0]["sliced"]
    bb.slicer = "ok"  # Bambuddy's slicer running → Try again → sliced there → in Bambuddy's queue
    assert client.post(f"/api/print-jobs/{j['id']}/retry").status_code == 200
    j = wait_for(lambda: (lambda j: j if j["state"] == "sent" else None)(job_of(client, bid, base_plate)))
    sent = bb.files[bb.queue[1]["library_file_id"]]
    assert sent["filename"] == f"{folder.name} - Bases - Bases - Ayra + Riya.gcode.3mf"
    assert b"by hand" not in sent["data"] and bb.queue[1]["printer_id"] == 1
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
    assert bb.files[src_id]["filename"].endswith("Pink - Ayra.3mf")  # the plain plate went up, Bambuddy sliced it
    assert body["printer_preset"] == {"source": "standard", "id": "Bambu Lab A1 0.4 nozzle"}
    assert body["filament_colours"] == ["#F4A7C0"] and body["export_3mf"] is True
    qi = next(q for q in bb.queue.values() if bb.files[q["library_file_id"]]["filename"].endswith("Pink - Ayra.gcode.3mf"))
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
    pink = "Names/pink/Pink - Ayra.3mf"
    bb.slicer = "ok"  # Bambuddy's slicer slices it when pushed
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
    # a slot with no filament, a bad option, and a printing job are refused
    assert "no filament in that slot" in client.post(f"/api/print-jobs/{j['id']}/print",
                                                     json={"printer_id": 1, "tray": 3}).json()["detail"]
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
    plate = "Bases/Bases - Ayra + Riya.3mf"
    bb.slicer = "ok"
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
    plate = "Bases/Bases - Ayra + Riya.3mf"
    bb.slicer = "ok"
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
        assert server._blocked(_req(path, "192.168.1.30")) is not None
    assert server._blocked(_req("/api/farm", "192.168.1.30")) is not None  # logistics never exposes farm data
    assert server._blocked(_req("/api/batches/1/push", "192.168.1.30")) is not None  # logistics never pushes plates


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
    base, pink, blue = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf", "Names/blue/Blue - Riya.3mf"
    bb.slicer = "ok"  # sliced by Bambuddy when started

    m = client.get("/api/makeit").json()
    assert [r["file"] for r in m["bases"]] == [base] and {r["file"] for r in m["names"]} == {pink, blue}
    assert m["bases"][0]["order_ids"] == ["PM1", "PM2"] and m["completed"] == []
    assert m["bases"][0]["suggested"] == 4  # available base printer with the most white left
    assert makeit_row(client, "names", pink)["suggested"] == 2
    # (Names A has blue too, but it's already picked for pink — another *available* printer is pre-selected,
    # with its slot closest to blue; nothing is ruled out by colour)
    blue_row = makeit_row(client, "names", blue)
    assert blue_row["suggested"] not in (None, 2)
    ch = {c["id"]: c for c in blue_row["choices"]}
    assert all(c["why_not"] == "" for c in ch.values()) and ch[2]["best_tray"] == 1  # Names A: blue in AMS 1 slot 2
    assert [t["tray"] for t in ch[2]["trays"]][0] == 1 and ch[2]["trays"][0]["match"] == 2

    # nothing is sent until Start print; then it is sent AND started (that click is the start)
    assert bb.queue == {}
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 4,
                                               "options": {"timelapse": True}})
    assert r.status_code == 200, r.text
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    assert bb.queue[1]["printer_id"] == 4 and bb.queue[1]["timelapse"] is True
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 4}).status_code == 400
    assert bb.queue[1]["ams_mapping"] == [254]  # the base printer's white spool (external), sent to Bambuddy
    # a slot that isn't loaded in that printer is refused
    assert "no filament in that slot" in client.post("/api/makeit/start", json={
        "batch_id": bid, "file": blue, "printer_id": 3, "tray": 9}).json()["detail"]

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

    # printed: base + pink done → Verify; nothing is packed before a person checks it
    bb.queue[1]["status"] = bb.queue[2]["status"] = "completed"
    farm.WORKER.refresh()
    m = client.get("/api/makeit").json()
    from .test_verify import by_plate
    assert {f for _, f in by_plate(m["verify"])} == {base, pink} and [r["file"] for r in m["names"]] == [blue]
    assert [c["order_id"] for c in m["verify"]] == ["PM1"] and m["verify_waiting_other"] == 1  # PM2: blue not printed
    tab = lambda t: sorted(o["order_id"] for o in client.get(f"/api/orders?tab={t}&limit=50").json()["orders"] if o["order_id"].startswith("PM"))
    assert tab("pack_it") == [] and tab("printing") == ["PM1", "PM2"]
    o1 = next(o for o in client.get("/api/orders?tab=printing&limit=50").json()["orders"] if o["order_id"] == "PM1")
    assert o1["print_done_at"] and not o1["checked_at"]  # Printed – waiting for check
    card = {f: c for (_, f), c in by_plate(m["verify"]).items()}
    good = lambda f: [{"batch_id": i["batch_id"], "plate": i["plate"], "idx": i["idx"], "result": "good"} for i in card[f]["items"]]
    check = lambda f, res, staff="Asha": client.post("/api/verify", json={"carrier": {"batch_id": bid, "file": f},
                                                                          "staff": staff, "results": res})
    assert check(base, good(base)).status_code == 409  # who is checking? (no staff names yet)
    assert client.post("/api/staff", json={"name": "Asha"}).status_code == 200
    assert check(base, good(base)).status_code == 200 and check(pink, good(pink)).status_code == 200
    assert tab("pack_it") == ["PM1"] and tab("printing") == ["PM2"]
    m = client.get("/api/makeit").json()
    assert m["verify"] == [] and [(r["order_id"], r["checked_by"]) for r in m["completed"]] == [("PM1", "Asha")]
    assert m["staff"] == ["Asha"]

    # blue printed but the whole plate failed → back to Names to print again; PM2 stays in Printing
    bb.queue[3]["status"] = "completed"
    farm.WORKER.refresh()
    card = {f: c for (_, f), c in by_plate(client.get("/api/makeit").json()["verify"]).items()}
    assert set(card) == {base, blue}  # PM2's base and its blue letters: one card for Riya
    r = client.post("/api/verify/plate-failed", json={"carrier": {"batch_id": bid, "file": blue}, "staff": "Asha"})
    assert r.status_code == 200 and r.json()["failed"]
    m = client.get("/api/makeit").json()
    assert [r["file"] for r in m["names"]] == [blue] and m["names"][0]["job"]["state"] == "reprint"
    assert m["verify"] == [] and tab("printing") == ["PM2"]


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
    base, pink = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf"

    # presets come from Bambuddy's list, set per printer (the A1s still use the pick made for their model)
    p = client.get("/api/makeit/presets?printer=1").json()
    assert p["names"] == {"printer": "Bambu Lab A1 0.4 nozzle", "process": "0.20mm Standard @BBL A1",
                          "filament": "Bambu PLA Basic @BBL A1"}
    assert [x["key"] for x in p["available"]["process"]] == ["local:7", "standard:0.20mm Standard @BBL A1"]
    r = client.put("/api/makeit/presets", json={**p["chosen"], "printer_id": 1, "process": "local:7"})
    assert r.status_code == 200 and r.json()["names"]["process"] == "Organizer 0.24mm"
    assert client.put("/api/makeit/presets", json={**p["chosen"], "printer_id": 1, "process": "local:99"}).status_code == 400
    assert client.get("/api/makeit/presets?printer=2").json()["names"]["process"] == "0.20mm Standard @BBL A1"  # only printer 1

    # Slice: Bambuddy slices it in the background; the row shows it sliced; nothing is queued to print
    bb.slicer = "ok"
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": base, "printer_id": 1}]}).json()["queued"] == 1
    wait_for(lambda: makeit_row(client, "bases", base)["sliced"])
    assert bb.slice_bodies[-1][1]["process_preset"] == {"source": "local", "id": "7"} and bb.queue == {}
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": base, "printer_id": 1}]}).json()["queued"] == 0
    # Start print then sends Bambuddy's sliced file — no second slice, nothing written locally
    n = len(bb.slice_bodies)
    # the printer's print defaults (Slicing) apply; what's set on the plate wins
    r = client.put("/api/makeit/print-defaults", json={"printer_id": 1, "bed_levelling": "off", "timelapse": True})
    assert r.json() == {"speed": "", "bed_levelling": "off", "timelapse": True}
    assert client.put("/api/makeit/print-defaults", json={"printer_id": 1, "speed": "warp"}).status_code == 400
    # plate type: sliced in as Bambuddy's bed_type, part of the slice key (another plate = a new slice)
    assert client.put("/api/makeit/print-defaults", json={"printer_id": 1, "plate_type": "Glass"}).status_code == 400
    key0 = farm.target(1)[0]
    r = client.put("/api/makeit/print-defaults", json={"printer_id": 1, "bed_levelling": "off", "timelapse": True,
                                                       "plate_type": "Textured PEI Plate"})
    assert r.json()["plate_type"] == "Textured PEI Plate" and farm.target(1)[0] != key0
    assert farm.slice_request(farm.target(1)[1], "white")["bed_type"] == "Textured PEI Plate"
    assert "Textured PEI Plate" in client.get("/api/makeit/presets?printer=1").json()["plates"]
    r = client.put("/api/makeit/print-defaults", json={"printer_id": 1, "bed_levelling": "off", "timelapse": True})
    assert farm.target(1)[0] == key0 and "bed_type" not in farm.slice_request(farm.target(1)[1], None)
    client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 1, "options": {"timelapse": False}})
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    assert bb.queue[1]["bed_levelling"] == "off" and bb.queue[1]["timelapse"] is False
    assert len(bb.slice_bodies) == n and bb.files[bb.queue[1]["library_file_id"]]["filename"].endswith(".gcode.3mf")
    assert not (folder / "Sliced").exists()

    # a slice that makes no progress is given up with a clear message (never stuck)
    bb.slicer = "hang"
    monkeypatch.setattr(farm, "SLICE_TIMEOUT_S", 0)
    client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 2}]})
    s = wait_for(lambda: (lambda s: s if s and s["state"] == "failed" else None)(makeit_row(client, "names", pink)["slice"]))
    assert "no progress" in s["error"]
    st = client.get("/api/printer-status").json()["slicing"]
    assert st["counts"]["failed"] == 1 and st["counts"]["done"] == 1 and st["presets_by_printer"]["1"]["process"] == "Organizer 0.24mm"

    # Remove takes a plate off Names (nothing deleted); Put back returns it
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": pink}).status_code == 200
    m = client.get("/api/makeit").json()
    assert pink not in [r["file"] for r in m["names"]] and [r["file"] for r in m["removed"]] == [pink]
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": base}).status_code == 400  # on its way
    assert client.post("/api/makeit/remove", json={"batch_id": bid, "file": pink, "put_back": True}).status_code == 200
    m = client.get("/api/makeit").json()
    assert pink in [r["file"] for r in m["names"]] and m["removed"] == []


def test_no_built_in_presets_each_printer_needs_its_own(client, bb):
    farm.update_config(presets_by_model={})  # nothing set up yet
    client.get("/api/farm")
    bid, folder = built_batch(client, "PN")
    pink = "Names/pink/Pink - Ayra.3mf"
    p = client.get("/api/makeit/presets").json()
    assert p["printer"] == 1 and p["set_for"] == {"1": False, "2": False, "3": False} and p["chosen"] == {}
    sl = makeit_row(client, "names", pink)["slice"]
    assert sl["state"] == "needs_presets" and sl["printer"] in ("Names A", "Names B")
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    assert r.status_code == 400 and "slicing profiles for Names A" in r.json()["detail"]
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 2}]}).json()["queued"] == 0
    h = client.get("/api/health").json()
    assert h["presets_missing"] == ["Base 1", "Names A", "Names B"] and h["bambuddy"]["connected"] and h["printers"]["total"] == 3
    keys = {slot: f"standard:{v['id']}" for slot, v in STD.items()}
    assert client.put("/api/makeit/presets", json=keys).status_code == 400  # for which printer?
    assert client.put("/api/makeit/presets", json={"printer_id": 2, **keys}).status_code == 200
    assert client.get("/api/makeit/presets").json()["set_for"] == {"1": False, "2": True, "3": False}
    assert client.get("/api/health").json()["presets_missing"] == ["Base 1", "Names B"]  # saving refreshes the sidebar
    s = client.get("/api/setup").json()["slicing"]
    assert s["presets_missing"] == ["Base 1", "Names B"] and s["printers"]["total"] == 3
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 2}]}).json()["queued"] == 1


def test_presets_per_printer_manual_slice_import_and_speed(client, bb):
    bb.printers[4] = {"id": 4, "name": "P1S names", "model": "P1S", "serial_number": "S4", "ip_address": "10.0.0.31",
                      "location": "", "is_active": True}
    bb.status[4] = {"connected": True, "state": "IDLE", "ams": [{"id": 0, "tray": [tray(0, "F55A74")]}]}  # Pink
    bb.presets["local"]["printer"] = [{"id": "11", "name": "P1S organizers", "source": "local"}]
    bb.slicer = "ok"
    client.get("/api/farm")
    bid, folder = built_batch(client, "PV")
    base, pink = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf"

    # the P1S gets its own presets; the A1s keep theirs
    p = client.get("/api/makeit/presets?printer=4").json()
    assert [x["name"] for x in p["printers"]] == ["Base 1", "Names A", "Names B", "P1S names"] and "auto_slice" not in p
    assert p["printer"] == 4 and p["set_for"] == {"1": True, "2": True, "3": True, "4": False}
    keys = {slot: f"standard:{v['id']}" for slot, v in STD.items()}
    assert client.put("/api/makeit/presets", json={**keys, "printer_id": 4, "printer": "local:11"}).status_code == 200
    assert client.get("/api/makeit/presets?printer=4").json()["names"]["printer"] == "P1S organizers"
    assert client.get("/api/makeit/presets?printer=1").json()["names"]["printer"] == "Bambu Lab A1 0.4 nozzle"

    # nothing is sliced by itself (owner decision 0.22.22): the worker runs, no slice is asked for
    farm.WORKER.slice_next()
    assert bb.slice_bodies == [] and client.put("/api/makeit/settings", json={"auto_slice": True}).status_code in (404, 405)
    # Slice needs the printer chosen for the plate: no default printer
    r = client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink}]})
    assert r.status_code == 400 and "Choose the printer" in r.json()["detail"]
    # Slice for the P1S queues it with the P1S's profiles only; the row shows each printer's own slice
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 4}]}).json()["queued"] == 1
    wait_for(lambda: next(c for c in makeit_row(client, "names", pink)["choices"] if c["id"] == 4)["slice"]["state"] == "done")
    row = makeit_row(client, "names", pink)
    assert [b["printer_preset"]["id"] for _, b in bb.slice_bodies] == ["11"]
    assert next(c for c in row["choices"] if c["id"] == 1)["slice"] is None  # the A1s' profiles: not sliced
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 4}]}).json()["queued"] == 0

    # Start print on the P1S with Sport speed: sends the P1S slice; speed applied once printing
    client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 4, "speed": "sport"})
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    sent = bb.files[bb.queue[1]["library_file_id"]]
    src = next(fid for fid, b in bb.slice_bodies if b["printer_preset"]["id"] == "11" and "Names pink" in bb.files[fid]["filename"])
    assert sent["filename"] == bb.files[src]["filename"].replace(".3mf", ".gcode.3mf")
    assert bb.speeds == []
    bb.queue[1]["status"] = "printing"  # Bambuddy's queue says printing while the printer still prepares …
    bb.status[4] |= {"state": "PREPARE", "layer_num": 0, "speed_level": 2}
    farm.WORKER.refresh()
    assert bb.speeds == []  # … a speed sent now would be lost when the print begins
    bb.status[4] |= {"state": "RUNNING", "layer_num": 1}
    farm.WORKER.refresh()
    assert bb.speeds == [(4, 3)] and bb.status[4]["speed_level"] == 3
    farm.WORKER.refresh()  # the printer reports Sport: done, never sent again
    farm.WORKER.refresh()
    assert bb.speeds == [(4, 3)]
    with db.tx() as con:
        j = dict(con.execute("SELECT * FROM print_jobs WHERE printer_id=4").fetchone())
    assert j["speed_set"] == 1 and j["speed_tries"] == 1 and not j["error"]
    # a printer that doesn't take it: sent again each round, then the job says so
    with db.tx() as con:
        con.execute("UPDATE print_jobs SET speed=4, speed_set=0, speed_tries=0 WHERE id=?", (j["id"],))
    bb.speed_ignored = True
    for _ in range(farm.SPEED_TRIES + 2):
        farm.WORKER.refresh()
    assert bb.speeds[1:] == [(4, 4)] * farm.SPEED_TRIES
    with db.tx() as con:
        j = dict(con.execute("SELECT * FROM print_jobs WHERE id=?", (j["id"],)).fetchone())
    assert j["speed_set"] == 1 and "stayed at Sport" in j["error"] and "Ludicrous" in j["error"]
    assert client.post("/api/makeit/start", json={"batch_id": bid, "file": base, "printer_id": 1,
                                                  "speed": "warp"}).status_code == 400

    # a failed slice is not retried by itself
    bb.slicer = "unreachable"
    farm.request_slices([(bid, base, "bases", "")], [1], again=True)
    wait_for(lambda: (next(c for c in makeit_row(client, "bases", base)["choices"] if c["id"] == 1)["slice"] or {}).get("state") == "failed")
    n = len(bb.slice_bodies)
    farm.WORKER.slice_next()
    farm.WORKER.poll_slices()
    assert len(bb.slice_bodies) == n

    # SnapSTL never adds profiles (to Bambuddy or itself), and has no slicer of its own to test or switch to
    r = client.post("/api/makeit/presets/import", data={"kind": "filament"},
                    files={"file": ("Silk PLA.bbsflmt", b"PK\x03\x04 bundle", "application/octet-stream")})
    assert r.status_code in (404, 405) and bb.imports == []
    assert client.post("/api/makeit/test-slice", json={"model": "A1"}).status_code in (404, 405)
    assert client.put("/api/makeit/settings", json={"slicer": "orca"}).status_code in (404, 405)


def test_any_bambuddy_profile_and_nothing_kept_by_snapstl(client, bb):
    from snapstl.platform_os import app_dir

    # left behind by the slicers on this computer before 0.21: dropped when the worker starts
    raw = {**farm.load_config(), "slicer": "orca", "orca_presets_by_model": {"A1": {"printer": "x"}}}
    farm.CONFIG_PATH.write_text(json.dumps(raw))
    (app_dir() / "slicer-presets" / "process").mkdir(parents=True, exist_ok=True)
    (app_dir() / "slicer-presets" / "process" / "mine.json").write_text("{}")
    farm.forget_local_slicing()
    kept = json.loads(farm.CONFIG_PATH.read_text())
    assert "slicer" not in kept and "orca_presets_by_model" not in kept and not (app_dir() / "slicer-presets").exists()

    # only Bambuddy's local and standard profiles can be picked — never cloud ones (ARCHITECTURE #62)
    bb.presets["cloud"] = {"filament": [{"id": "PFUS1", "name": "My Silk PLA", "source": "cloud"}]}
    bb.presets["orca_cloud"] = {"process": [{"id": "oc9", "name": "Orca fine", "source": "orca_cloud"}]}
    bb.presets["cloud_status"] = "ok"
    client.get("/api/farm")
    p = client.get("/api/makeit/presets?printer=1&refresh=1").json()
    assert bb.preset_reads[-1].endswith("?refresh=true") and p["error"] == ""
    assert {x["source"] for sl in p["available"].values() for x in sl} == {"local", "standard"}
    assert p["cloud_problem"] == ""
    cloudy = {"printer_id": 1, "printer": "standard:Bambu Lab A1 0.4 nozzle", "process": "local:7", "filament": "cloud:PFUS1"}
    assert client.put("/api/makeit/presets", json=cloudy).status_code == 400
    keys = {"printer_id": 1, "printer": "standard:Bambu Lab A1 0.4 nozzle", "process": "local:7",
            "filament": "standard:Bambu PLA Basic @BBL A1"}
    assert client.put("/api/makeit/presets", json=keys).json()["names"] == {
        "printer": "Bambu Lab A1 0.4 nozzle", "process": "Organizer 0.24mm", "filament": "Bambu PLA Basic @BBL A1"}
    # only references to Bambuddy's profiles are kept — never their content
    assert farm.load_config()["presets_by_printer"]["1"]["process"] == {"source": "local", "id": "7", "name": "Organizer 0.24mm"}
    # a profile that isn't in Bambuddy can't be picked
    bad = client.put("/api/makeit/presets", json={**keys, "process": "local:999"})
    assert bad.status_code == 400 and "from Bambuddy's list" in bad.json()["detail"]
    # Bambuddy can't list them: the page still opens and says why
    bb.presets = ["not", "a", "profile list"]  # not the shape Bambuddy answers with
    p = client.get("/api/makeit/presets").json()
    assert p["error"] and p["available"] == {"printer": [], "process": [], "filament": []}


def test_bambuddy_on_another_computer_and_this_computers_address(client, bb, monkeypatch):
    from snapstl import platform_os, server

    monkeypatch.setattr(platform_os, "lan_ips", lambda: ["192.168.1.20"])
    monkeypatch.setattr(platform_os, "lan_name", lambda: "farm-pc")
    # SnapSTL connects to a Bambuddy on another computer by its address and makes its own key
    farm.update_config(api_key="")
    r = client.post("/api/farm/connect", json={"url": "http://192.168.1.50:8000/"})
    assert r.status_code == 200 and r.json()["connected"]
    assert farm.load_config()["bambuddy_url"] == "http://192.168.1.50:8000"
    assert "can_access_cloud" not in bb.key_requests[-1]  # login off: Bambuddy refuses it (and serves cloud anyway)
    server._health_stale()
    n = client.get("/api/health").json()["network"]
    assert n == {"ip": "192.168.1.20", "name": "farm-pc", "portal": "http://192.168.1.20:8317",
                 "bambuddy_url": "http://192.168.1.50:8000", "bambuddy_here": False,
                 "bambuddy_share": "http://192.168.1.50:8000"}
    # Bambuddy on this computer: the sidebar offers its address for the other SnapSTL computers
    farm.update_config(bambuddy_url="http://127.0.0.1:8000")
    server._health_stale()
    n = client.get("/api/health").json()["network"]
    assert n["bambuddy_here"] and n["bambuddy_share"] == "http://192.168.1.20:8000"


def test_refresh_from_bambuddy_and_chosen_slot(client, bb):
    """Printers → Refresh from Bambuddy asks every printer to report again; Print sends the chosen slot."""
    farm.printers()
    bb.status[3] = {"connected": True, "state": "IDLE", "ams": [{"id": 0, "tray": [tray(0, "F5547C"), tray(1, "0A2989")]}]}
    assert len(next(p for p in farm.printers() if p["id"] == 3)["spools"]) == 1  # still the cached status
    r = client.post("/api/farm/refresh")
    assert r.status_code == 200 and r.json() == {"asked": 3, "failed": []}
    assert sorted(bb.refreshed) == [1, 2, 3]
    p3 = next(p for p in farm.printers() if p["id"] == 3)
    assert [(s["slot"], s["tray"], s["color"]) for s in p3["spools"]] == [("AMS 1 · 1", 0, "hot pink"), ("AMS 1 · 2", 1, "blue")]
    assert "key" not in p3["spools"][0] and "mapped" not in p3["spools"][0]  # no "prints as" mapping any more
    assert client.put("/api/farm/printers/3/color", json={"key": "x|y", "color": "red"}).status_code in (404, 405)


def test_snapstl_key_never_asks_for_cloud(client, bb):
    """SnapSTL uses no cloud profiles, so its key never asks for cloud access."""
    farm.update_config(api_key="")
    assert client.post("/api/farm/connect", json={}).status_code == 200  # login off: SnapSTL makes its key
    assert "can_access_cloud" not in bb.key_requests[-1]
    bb.auth_enabled = True
    farm.update_config(api_key="")
    r = client.post("/api/farm/connect", json={})
    assert r.status_code == 400 and "not Allow cloud access" in r.json()["detail"]


def test_queue_panel_lists_downloading_building_slicing_printing(client, bb):
    bb.slicer = "ok"
    client.get("/api/farm")
    bid, folder = built_batch(client, "PQ")
    pink = "Names/pink/Pink - Ayra.3mf"
    q = client.get("/api/queues").json()
    assert set(q["counts"]) == {"downloading", "building", "slicing", "printing"}
    assert any(r["state"] == "done" for r in q["downloading"])  # the batch's models, finished
    assert any(b["id"] == bid and b["status"] == "done" for b in q["building"])
    client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink, "printer_id": 2}]})
    wait_for(lambda: any(i["plate"] == pink and i["state"] == "done" for i in client.get("/api/queues").json()["slicing"]))
    client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    q = wait_for(lambda: (lambda q: q if any(j["plate"] == pink for j in q["printing"]) else None)(client.get("/api/queues").json()))
    j = next(j for j in q["printing"] if j["plate"] == pink)
    assert j["printer_name"] == "Names A" and j["batch"]


def test_letters_slice_with_their_own_profile_and_only_on_letter_printers(client, bb):
    """0.27: Slicing → Letters: letter plates slice with the printer's Letters process profile and print at the
    Letters speed; bases keep the printer's profiles. With any Letters profile set, letters go only to those printers."""
    client.get("/api/farm")
    bid, folder = built_batch(client, "PL")
    base, pink = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf"
    assert client.put("/api/makeit/letters", json={"printer_id": 1, "process": "local:99"}).status_code == 400
    r = client.put("/api/makeit/letters", json={"printer_id": 1, "process": "local:7", "speed": "standard"})
    assert r.status_code == 200 and r.json()["process"]["name"] == "Organizer 0.24mm"
    p = client.get("/api/makeit/presets?printer=1").json()
    assert p["letters"] == {"process": "local:7", "name": "Organizer 0.24mm", "speed": "standard"} and p["letters_printers"] == [1]
    assert farm.target(1, kind="names")[0] != farm.target(1)[0]  # letters and bases: separate slices

    bb.slicer = "ok"
    for f in (pink, base):
        assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": f, "printer_id": 1}]}).json()["queued"] == 1
        wait_for(lambda f=f: makeit_row(client, "names" if f == pink else "bases", f)["sliced"])
    procs = [b[1]["process_preset"] for b in bb.slice_bodies[-2:]]
    assert procs[0] == {"source": "local", "id": "7"} and procs[1] != procs[0]  # letters: Letters; base: the printer's

    m = client.get("/api/makeit").json()
    row = next(r for r in m["names"] if r["file"] == pink)
    assert {c["id"] for c in row["choices"]} == {1}  # letters only on the printer with a Letters profile
    assert len(next(r for r in m["bases"] if r["file"] == base)["choices"]) > 1
    assert next(x for x in m["printers"] if x["id"] == 1)["defaults_letters"]["speed"] == "standard"
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    assert r.status_code in (400, 409) and "Letters profile" in r.json()["detail"]
    # removing it: letters may print anywhere again, with the printer's profiles
    assert client.put("/api/makeit/letters", json={"printer_id": 1, "process": ""}).json()["process"] is None
    assert farm.target(1, kind="names")[0] == farm.target(1)[0] and farm.letters_printers() == set()
