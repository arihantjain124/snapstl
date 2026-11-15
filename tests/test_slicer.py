"""Slicing with OrcaSlicer on this computer: presets from the slicer apps' folders, inheritance, model
defaults, preset import, and the whole Make It path (Orca slices → Bambuddy prints).

OrcaSlicer is a small stand-in program that reads the same command line and writes a sliced 3MF (or a log
saying why not), so the tests never need the real app.
"""
import io
import json
import os
import stat
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from snapstl import farm, slicer

FAKE_ORCA = textwrap.dedent('''\
    #!{py}
    import json, sys, zipfile, pathlib
    a = sys.argv[1:]
    opt = lambda k: a[a.index(k) + 1]
    assert opt("--arrange") == "0" and opt("--orient") == "0" and opt("--slice") == "0"
    out = pathlib.Path(opt("--outputdir"))
    machine, process = [json.load(open(p)) for p in opt("--load-settings").split(";")]
    filament = json.load(open(opt("--load-filaments")))
    if machine["name"] not in process.get("compatible_printers", [machine["name"]]):
        (out / "00001.log").write_text("[error] run 3010: process not compatible with printer.\\n")
        print("run found error, exit"); sys.exit(243)
    with zipfile.ZipFile(out / opt("--export-3mf"), "w") as z:
        z.writestr("Metadata/plate_1.gcode", "; printer_model = %s\\n; process = %s\\n; filament_colour = %s\\n; plate = %s\\n"
                   % (machine.get("printer_model"), process["name"], filament.get("filament_colour"), a[-1]))
    ''')


def write(p: Path, d: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


@pytest.fixture
def orca(tmp_path, monkeypatch):
    """A fake OrcaSlicer and preset folders like Bambu Studio's (system) with one user preset."""
    exe = tmp_path / "OrcaSlicer"
    exe.write_text(FAKE_ORCA.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "slicer-home"
    sysd = home / "BambuStudio" / "system" / "BBL"
    write(sysd / "machine" / "fdm_machine_common.json", {"type": "machine", "name": "fdm_machine_common",
                                                         "instantiation": "false", "nozzle_diameter": ["0.4"]})
    for model, proc, fil in (("A1", "0.20mm Standard @BBL A1", "Bambu PLA Basic @BBL A1"),
                             ("P1S", "0.20mm Standard @BBL X1C", "Bambu PLA Basic @BBL P1S 0.4 nozzle")):
        write(sysd / "machine" / f"Bambu Lab {model} 0.4 nozzle.json",
              {"type": "machine", "name": f"Bambu Lab {model} 0.4 nozzle", "inherits": "fdm_machine_common",
               "instantiation": "true", "printer_model": f"Bambu Lab {model}", "default_print_profile": proc,
               "default_filament_profile": [fil]})
        write(sysd / "process" / f"{proc}.json", {"type": "process", "name": proc, "instantiation": "true",
                                                 "layer_height": "0.2",
                                                 "compatible_printers": [f"Bambu Lab {model} 0.4 nozzle"]})
        write(sysd / "filament" / f"{fil}.json", {"type": "filament", "name": fil, "instantiation": "true"})
    write(home / "BambuStudio" / "user" / "123" / "process" / "Organizer fast.json",
          {"type": "process", "name": "Organizer fast", "from": "User", "inherits": "0.20mm Standard @BBL A1",
           "outer_wall_speed": ["300"]})
    monkeypatch.setenv("SNAPSTL_ORCA", str(exe))
    monkeypatch.setenv("SNAPSTL_SLICER_HOME", str(home))
    monkeypatch.setattr(slicer, "_idx", (0.0, {}))
    monkeypatch.setattr(slicer, "IMPORT_DIR", tmp_path / "imported")
    return exe


def test_presets_inheritance_and_model_defaults(orca):
    L = slicer.list_presets()
    assert [p["name"] for p in L["printer"]] == ["Bambu Lab A1 0.4 nozzle", "Bambu Lab P1S 0.4 nozzle"]  # no abstract base
    assert L["process"][0] == {"key": "Organizer fast", "name": "Organizer fast", "source": "yours (Bambu Studio)"}
    f = slicer.flatten("process", "Organizer fast")
    assert f["layer_height"] == "0.2" and f["outer_wall_speed"] == ["300"] and "inherits" not in f
    assert slicer.flatten("printer", "Bambu Lab A1 0.4 nozzle")["nozzle_diameter"] == ["0.4"]
    assert slicer.defaults_for("P1S") == {"printer": "Bambu Lab P1S 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                                          "filament": "Bambu PLA Basic @BBL P1S 0.4 nozzle"}
    assert slicer.defaults_for("X9") == {}


def test_slice_plate_and_why_it_fails(orca, tmp_path):
    src = tmp_path / "plate.3mf"
    src.write_bytes(b"plain")
    dst = tmp_path / "out" / "plate.gcode.3mf"
    slicer.slice_plate(src, dst, slicer.defaults_for("A1") | {"process": "Organizer fast"}, "#F4A7C0")
    g = zipfile.ZipFile(dst).read("Metadata/plate_1.gcode").decode()
    assert "printer_model = Bambu Lab A1" in g and "process = Organizer fast" in g and "#F4A7C0" in g
    with pytest.raises(slicer.SlicerError, match="isn't made for the printer preset"):
        slicer.slice_plate(src, dst, slicer.defaults_for("P1S") | {"process": "0.20mm Standard @BBL A1"})
    with pytest.raises(slicer.SlicerError, match="isn't on this computer"):
        slicer.slice_plate(src, dst, slicer.defaults_for("A1") | {"process": "Gone"})


def test_import_preset_files(orca):
    one = json.dumps({"type": "filament", "name": "Silk Pink", "inherits": "Bambu PLA Basic @BBL A1",
                      "filament_colour": ["#FF00AA"]}).encode()
    r = slicer.import_preset("Silk Pink.json", one, "filament")
    assert r["imported"] == 1 and r["errors"] == []
    assert any(p["name"] == "Silk Pink" and p["source"] == "added in SnapSTL" for p in slicer.list_presets()["filament"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bundle_structure.json", json.dumps({"version": 1}))
        z.writestr("process/Bases strong.json", json.dumps({"type": "process", "name": "Bases strong",
                                                             "inherits": "0.20mm Standard @BBL A1", "wall_loops": "4"}))
    r = slicer.import_preset("my.bbscfg", buf.getvalue(), "process")
    assert r["imported"] == 1 and r["skipped"] == 1 and slicer.flatten("process", "Bases strong")["wall_loops"] == "4"
    r = slicer.import_preset("x.json", one, "printer")  # wrong button: said so, not added
    assert r["imported"] == 0 and "not a printer preset" in r["errors"][0]
    with pytest.raises(slicer.SlicerError):
        slicer.import_preset("notes.txt", b"hi")


def test_make_it_slices_with_orca_then_prints_through_bambuddy(orca, client, bb):  # noqa: F811
    from .test_farm import built_batch, makeit_row, wait_for

    client.get("/api/farm")
    p = client.get("/api/makeit/presets?model=A1").json()
    assert p["engine"] == "orca" and p["orca"] == str(orca) and p["chosen"]["process"] == "0.20mm Standard @BBL A1"
    assert client.put("/api/makeit/presets", json={"model": "A1", **p["chosen"], "process": "Organizer fast"}).status_code == 200
    assert client.put("/api/makeit/presets", json={"model": "A1", **p["chosen"], "process": "Nope"}).status_code == 400

    bid, folder = built_batch(client, "PO")
    pink = "Names/pink/{Ayra,pink}.3mf"
    assert client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": pink}]}).json()["queued"] == 1
    wait_for(lambda: makeit_row(client, "names", pink)["sliced"])
    out = folder / "Sliced" / "A1" / "Names" / "pink" / "{Ayra,pink}.gcode.3mf"
    g = zipfile.ZipFile(out).read("Metadata/plate_1.gcode").decode()
    assert "process = Organizer fast" in g and "#F4A7C0" in g and bb.slice_bodies == []  # Orca, not Bambuddy
    base = "Bases/{Ayra,pink},{Riya,blue}.3mf"
    client.post("/api/makeit/slice", json={"plates": [{"batch_id": bid, "file": base}]})
    wait_for(lambda: makeit_row(client, "bases", base)["sliced"])
    gb = zipfile.ZipFile(folder / "Sliced" / "A1" / "Bases" / "{Ayra,pink},{Riya,blue}.gcode.3mf").read("Metadata/plate_1.gcode")
    assert b"#FFFFFF" in gb  # bases are sliced white, whatever names are on them

    client.post("/api/makeit/start", json={"batch_id": bid, "file": pink, "printer_id": 2})
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    sent = bb.files[bb.queue[1]["library_file_id"]]
    assert sent["filename"].endswith("{Ayra,pink}.gcode.3mf") and b"Organizer fast" in sent["data"]

    # switching to Bambuddy's slicer is one setting
    r = client.put("/api/makeit/settings", json={"slicer": "bambuddy"}).json()
    assert r["engine"] == "bambuddy" and client.get("/api/makeit/presets").json()["engine"] == "bambuddy"


from .test_farm import bb, client  # noqa: E402,F401  (fixtures)
