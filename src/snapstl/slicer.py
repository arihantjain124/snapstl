"""Slicing on this computer with OrcaSlicer's command line (Make It → Slice).

Why OrcaSlicer: it's built from Bambu Studio's code (same engine, same Bambu printer / process /
filament presets), and its command line works on macOS — Bambu Studio's own crashes there (ARCHITECTURE
decision log #28). SnapSTL runs it headless, one plate at a time in a background thread:

    OrcaSlicer --arrange 0 --orient 0 --slice 0 --load-settings "printer.json;process.json"
               --load-filaments filament.json --export-3mf <out>.gcode.3mf <plate>.3mf

`--arrange 0 --orient 0` keep every part exactly where SnapSTL placed and checked it.

Presets: OrcaSlicer's command line needs *complete* preset files, so SnapSTL reads the presets where the
slicer apps keep them and resolves their `inherits` chain itself:
  * presets added in SnapSTL (Make It → Slice → Add preset…): APP_DIR/slicer-presets/<machine|process|filament>
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


def orca_path() -> Path | None:
    """OrcaSlicer's program, or None when it isn't installed."""
    for p in platform_os.orca_candidates():
        if p and Path(p).is_file():
            return Path(p)
    return None


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
                                  "Make It → Slice")
            break  # parent not found: use what we have
        chain.append(hit["data"])
        cur = hit["data"].get("inherits") or ""
    out: dict = {}
    for d in reversed(chain):
        out.update(d)
    out.pop("inherits", None)
    out["name"] = name
    return out


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
                timeout: float = 900) -> Path:
    """Slice one plate with OrcaSlicer (parts stay where they are). Raises SlicerError with the reason."""
    exe = orca_path()
    if exe is None:
        raise SlicerError("OrcaSlicer isn't installed on this computer — install it (the SnapSTL installer does), "
                          "then slice again")
    missing = [s for s in CATS if not presets.get(s)]
    if missing:
        raise SlicerError(f"Choose the {', '.join(missing)} preset in Make It → Slice")
    with tempfile.TemporaryDirectory(prefix="snapstl-slice-") as tmp:
        t = Path(tmp)
        files = {}
        for slot in CATS:
            d = flatten(slot, presets[slot])
            if slot == "filament" and color_hex:
                d["filament_colour"] = [color_hex]
            files[slot] = t / f"{slot}.json"
            files[slot].write_text(json.dumps(d), encoding="utf-8")
        out = t / "plate.gcode.3mf"  # --export-3mf is relative to --outputdir (where OrcaSlicer also writes its log)
        cmd = [str(exe), "--outputdir", str(t), "--arrange", "0", "--orient", "0", "--slice", "0",
               "--load-settings", f"{files['printer']};{files['process']}", "--load-filaments", str(files["filament"]),
               "--export-3mf", out.name, str(src)]
        try:
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout,
                               creationflags=platform_os.NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise SlicerError(f"OrcaSlicer didn't finish in {int(timeout // 60)} min") from e
        except OSError as e:
            raise SlicerError(f"Couldn't start OrcaSlicer: {e}") from e
        if r.returncode != 0 or not out.is_file():
            log = "".join(f.read_text(errors="replace") for f in sorted(t.glob("*.log")))  # OrcaSlicer's own log
            why = _why(log + r.stdout + r.stderr) or f"exit code {r.returncode}"
            if "not compatible with printer" in why:
                why = (f"the process preset “{presets['process']}” isn't made for the printer preset "
                       f"“{presets['printer']}” — choose a matching pair in Make It → Slice")
            raise SlicerError(f"OrcaSlicer couldn't slice it: {why}")
        data = out.read_bytes()
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        if not any(n.startswith("Metadata/plate_") and n.endswith(".gcode") for n in names):
            raise SlicerError("OrcaSlicer's file has no sliced plate inside")
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
        if want and cat != want:
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


_running = threading.Lock()


def busy() -> bool:
    return _running.locked()
