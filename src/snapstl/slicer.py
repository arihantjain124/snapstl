"""Slicing on this computer with OrcaSlicer's or Bambu Studio's command line (Slicing tab).

Both share one engine (OrcaSlicer is built from Bambu Studio) and the same Bambu printer / process /
filament presets. Bambu Studio's command line works on Windows; on macOS it crashes (ARCHITECTURE decision
log #28), so there OrcaSlicer is the one. SnapSTL runs the chosen one headless, one plate at a time in a
background thread:

    <slicer> --arrange 0 --orient 0 --slice 0 --load-settings "printer.json;process.json"
             --load-filaments filament.json --outputdir <tmp> --export-3mf plate.gcode.3mf <plate>.3mf

`--arrange 0 --orient 0` keep every part exactly where SnapSTL placed and checked it.

Presets: OrcaSlicer's command line needs *complete* preset files, so SnapSTL reads the presets where the
slicer apps keep them and resolves their `inherits` chain itself:
  * presets added in SnapSTL (Slicing → Add preset…): APP_DIR/slicer-presets/<machine|process|filament>
  * your own presets saved in OrcaSlicer or Bambu Studio (their `user` folders)
  * the system presets (Bambu Lab printers, processes, filaments) from OrcaSlicer's or Bambu Studio's
    folders, or from inside the Bambu Studio app.
The same name in several places: the first in that order wins. A printer model without chosen presets
uses its printer preset's own defaults ("Bambu Lab P1S 0.4 nozzle" → its default process and filament).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from . import platform_os
from .platform_os import app_dir

CATS = {"printer": "machine", "process": "process", "filament": "filament"}
IMPORT_DIR = app_dir() / "slicer-presets"
MODEL_NAMES = {"X1C": "X1 Carbon", "A1M": "A1 mini", "A1 MINI": "A1 mini"}  # Bambuddy short names → Bambu's


class SlicerError(Exception):
    """Something the person can fix, in plain English."""


NAMES = {"orca": "OrcaSlicer", "bambu": "Bambu Studio"}


def exe_path(engine: str = "orca") -> Path | None:
    """The slicer's program ("orca" | "bambu"), or None when it isn't installed."""
    cands = platform_os.orca_candidates() if engine == "orca" else platform_os.bambu_candidates()
    for p in cands:
        if p and Path(p).is_file():
            return Path(p)
    return None


def orca_path() -> Path | None:
    return exe_path("orca")


def works_here(engine: str) -> bool:
    """Bambu Studio's command line crashes on macOS; everything else is fine."""
    return not (engine == "bambu" and platform_os.MAC)


def _system_root(chain: list[dict]) -> str:
    """Name of the first Bambu system preset in a preset's chain (itself if it is one)."""
    for d in chain:
        frm = str(d.get("from", "")).lower()
        if frm == "system" or d.get("setting_id") or (frm != "user" and str(d.get("instantiation", "")).lower() == "true"):
            return str(d["name"])  # a pickable system preset — never an abstract base like fdm_machine_common
    return str(chain[0]["name"]) if chain else ""


def preset_roots() -> list[tuple[str, Path]]:
    """(label, folder with machine/ process/ filament/) in priority order."""
    roots: list[tuple[str, Path]] = [("added in SnapSTL", IMPORT_DIR)]
    for app in ("OrcaSlicer", "BambuStudio"):
        base = platform_os.slicer_data_dir(app)
        if base:
            roots += [(f"yours ({'Orca' if app == 'OrcaSlicer' else 'Bambu Studio'})", d)
                      for d in sorted((base / "user").glob("*")) if d.is_dir()]
    for app in ("OrcaSlicer", "BambuStudio"):
        base = platform_os.slicer_data_dir(app)
        if base and (base / "system" / "BBL").is_dir():
            roots.append(("Bambu Lab", base / "system" / "BBL"))
    for res in platform_os.slicer_resource_dirs():
        if (res / "BBL" / "machine").is_dir():
            roots.append(("Bambu Lab", res / "BBL"))
    return roots


_idx: tuple[float, dict] = (0.0, {})


def index(refresh: bool = False) -> dict[tuple[str, str], dict]:
    """(category, name) → {"data", "source", "path"}; first root wins. Cached for a minute."""
    global _idx
    if not refresh and time.time() - _idx[0] < 60 and _idx[1]:
        return _idx[1]
    out: dict[tuple[str, str], dict] = {}
    for label, root in preset_roots():
        for cat in CATS.values():
            for f in sorted((root / cat).rglob("*.json")) if (root / cat).is_dir() else []:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and d.get("name"):
                    out.setdefault((cat, str(d["name"])), {"data": d, "source": label, "path": str(f)})
    _idx = (time.time(), out)
    return out


def list_presets() -> dict[str, list[dict]]:
    """For the Slice menu: {printer|process|filament: [{key, name, source}]} — only presets one can pick."""
    idx = index()
    out: dict[str, list[dict]] = {slot: [] for slot in CATS}
    for slot, cat in CATS.items():
        for (c, name), v in idx.items():
            if c != cat:
                continue
            system = v["source"] == "Bambu Lab"
            if system and str(v["data"].get("instantiation", "true")).lower() != "true":
                continue  # an abstract base preset (e.g. "fdm_machine_common")
            out[slot].append({"key": name, "name": name, "source": v["source"]})
        out[slot].sort(key=lambda p: (p["source"] == "Bambu Lab", p["name"].lower()))
    return out


def flatten(slot: str, name: str) -> dict:
    """The complete preset: its whole `inherits` chain merged (child wins)."""
    cat = CATS[slot]
    idx = index()
    chain, seen, cur = [], set(), name
    while cur:
        if cur in seen:
            raise SlicerError(f"The {slot} preset “{name}” inherits from itself")
        seen.add(cur)
        hit = idx.get((cat, cur))
        if hit is None:
            if cur == name:
                raise SlicerError(f"The {slot} preset “{name}” isn't on this computer any more — choose another in "
                                  "Slicing")
            break  # parent not found: use what we have
        chain.append(hit["data"])
        cur = hit["data"].get("inherits") or ""
    out: dict = {}
    for d in reversed(chain):
        out.update(d)
    out.pop("inherits", None)
    out["name"] = name
    out["_system"] = _system_root(chain)
    return out


def for_engine(d: dict, engine: str) -> dict:
    """A flattened preset as the slicer wants it. Bambu Studio checks printer / process compatibility by
    the *system* presets they come from, so its copy is a user preset (named apart) that inherits that
    system preset and carries every value; OrcaSlicer takes the flat preset as it is."""
    d = dict(d)
    root = d.pop("_system", "") or d["name"]
    if engine == "bambu":
        d.update({"from": "User", "inherits": root, "name": f"SnapSTL {d['name']}"[:120]})
    return d


def model_name(model: str) -> str:
    m = (model or "A1").strip()
    m = MODEL_NAMES.get(m.upper(), m)
    return m if m.lower().startswith("bambu lab") else f"Bambu Lab {m}"


def defaults_for(model: str) -> dict[str, str]:
    """A printer model's own default presets (from its Bambu Lab printer preset)."""
    printer = f"{model_name(model)} 0.4 nozzle"
    idx = index()
    hit = idx.get(("machine", printer))
    if hit is None:
        return {}
    m = flatten("printer", printer)
    fil = m.get("default_filament_profile") or []
    out = {"printer": printer, "process": str(m.get("default_print_profile") or ""),
           "filament": str(fil[0] if isinstance(fil, list) and fil else fil or "")}
    return {k: v for k, v in out.items() if v and (CATS[k], v) in idx}


def slice_plate(src: Path, dst: Path, presets: dict[str, str], color_hex: str | None = None,
                timeout: float = 900, engine: str = "orca") -> Path:
    """Slice one plate with OrcaSlicer or Bambu Studio (parts stay where they are). Raises SlicerError."""
    app = NAMES.get(engine, engine)
    if not works_here(engine):
        raise SlicerError("Bambu Studio's command line can't slice on macOS — choose OrcaSlicer in Slicing")
    exe = exe_path(engine)
    if exe is None:
        raise SlicerError(f"{app} isn't installed on this computer — install it, or choose the other slicer in "
                          "Slicing")
    missing = [s for s in CATS if not presets.get(s)]
    if missing:
        raise SlicerError(f"Choose the {', '.join(missing)} preset in Slicing")
    with tempfile.TemporaryDirectory(prefix="snapstl-slice-") as tmp:
        t = Path(tmp)
        files = {}
        for slot in CATS:
            d = for_engine(flatten(slot, presets[slot]), engine)
            if slot == "filament" and color_hex:
                d["filament_colour"] = [color_hex]
            files[slot] = t / f"{slot}.json"
            files[slot].write_text(json.dumps(d), encoding="utf-8")
        out = t / "plate.gcode.3mf"  # --export-3mf is relative to --outputdir (where OrcaSlicer also writes its log)
        cmd = [str(exe), *(["--debug", "2"] if engine == "bambu" else []), "--outputdir", str(t),
               "--arrange", "0", "--orient", "0", "--slice", "0",
               "--load-settings", f"{files['printer']};{files['process']}", "--load-filaments", str(files["filament"]),
               "--export-3mf", out.name, str(src)]
        try:
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout,
                               creationflags=platform_os.NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise SlicerError(f"{app} didn't finish in {int(timeout // 60)} min") from e
        except OSError as e:
            raise SlicerError(f"Couldn't start {app}: {e}") from e
        if r.returncode != 0 or not out.is_file():
            log = "".join(f.read_text(errors="replace") for f in sorted(t.glob("*.log")))  # the slicer's own log
            why = _why(log + r.stdout + r.stderr) or f"exit code {r.returncode}"
            if "not compatible with printer" in why:
                why = (f"the process preset “{presets['process']}” isn't made for the printer preset "
                       f"“{presets['printer']}” — choose a matching pair in Slicing")
            raise SlicerError(f"{app} couldn't slice it: {why}")
        data = out.read_bytes()
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        if not any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in names):
            raise SlicerError(f"{app}'s file has no sliced plate inside")
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        shutil.copyfile(out, part)
        os.replace(part, dst)
    return dst


def _why(log: str) -> str:
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    errs = [ln for ln in lines if re.search(r"error|failed|not compatible|invalid", ln, re.I)
            and "run found error" not in ln]
    return re.sub(r"^\[[^\]]*\]\s*(\[[^\]]*\]\s*)*", "", (errs or lines or [""])[-1])[:300]


def import_preset(filename: str, data: bytes, kind: str = "") -> dict:
    """Add preset files (a preset .json, or a .bbscfg / .bbsflmt / .zip bundle of them) for SnapSTL's
    slicer. Each lands in APP_DIR/slicer-presets/<machine|process|filament>/ by its own "type"."""
    items: list[tuple[str, bytes]] = []
    low = filename.lower()
    if low.endswith(".json"):
        items.append((filename, data))
    elif low.endswith((".bbscfg", ".bbsflmt", ".zip", ".orca_filament")):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise SlicerError("That bundle couldn't be opened") from e
        items += [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".json")]
    else:
        raise SlicerError("Choose a preset exported from Bambu Studio or OrcaSlicer (.json, .bbscfg, .bbsflmt or .zip)")
    added, skipped, errors = [], 0, []
    want = CATS.get(kind, "")
    for name, raw in items:
        try:
            d = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            skipped += 1
            continue
        cat = {"machine": "machine", "printer": "machine", "process": "process", "print": "process",
               "filament": "filament"}.get(str(d.get("type", "")).lower()) if isinstance(d, dict) else None
        if not cat or not d.get("name"):
            skipped += 1  # bundle info files and the like
            continue
        if want and cat != want and low.endswith(".json"):  # a bundle brings all its presets
            errors.append(f"“{d['name']}” is a {'printer' if cat == 'machine' else cat} preset, not a {kind} preset")
            continue
        folder = IMPORT_DIR / cat
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", str(d["name"]))[:150]
        (folder / f"{safe}.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
        added.append(str(d["name"]))
    index(refresh=True)
    for n in added:  # a user preset needs its parents: say so now rather than at slicing time
        cat = next(c for (c, nm) in index() if nm == n)
        slot = {v: k for k, v in CATS.items()}[cat]
        try:
            flatten(slot, n)
        except SlicerError as e:
            errors.append(str(e))
    return {"imported": len(added), "skipped": skipped, "errors": errors, "names": added}


def test_slice(engine: str, presets: dict[str, str]) -> dict:
    """Slicing → Test slicer: slice a small test plate (a 20 mm cube) with these presets."""
    import numpy as np

    from .threemf import Object3MF, write_3mf

    v = np.array([[x, y, z] for z in (0, 20) for y in (118, 138) for x in (118, 138)], float)
    f = np.array([[0, 2, 1], [1, 2, 3], [4, 5, 6], [5, 7, 6], [0, 1, 5], [0, 5, 4], [2, 6, 7], [2, 7, 3],
                  [0, 4, 6], [0, 6, 2], [1, 3, 7], [1, 7, 5]])
    with tempfile.TemporaryDirectory(prefix="snapstl-test-slice-") as tmp:
        src = Path(tmp) / "test cube.3mf"
        write_3mf(src, [Object3MF("Test cube", v, f)])
        t = time.time()
        out = slice_plate(src, Path(tmp) / "test cube.gcode.3mf", presets, "#FFFFFF", timeout=300, engine=engine)
        g = zipfile.ZipFile(out).read("Metadata/plate_1.gcode").decode(errors="replace")
    model = re.search(r"^; printer_model = (.*)$", g, re.M)
    return {"seconds": round(time.time() - t, 1), "printer_model": model.group(1).strip() if model else ""}


_running = threading.Lock()


def busy() -> bool:
    return _running.locked()
