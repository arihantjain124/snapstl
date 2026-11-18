"""Write plate STLs, re-check them from disk, and write the report."""
from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

import numpy as np
import trimesh

from .grouping import Plate
from .packing import placed_footprint, placed_part_meshes
from .threemf import Object3MF, read_3mf, write_3mf
from .scaling import ScaleResult
from .settings import PlateSettings

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME = 200


def clean_part(s: str) -> str:
    """Safe for a file or folder name."""
    return _BAD.sub("", s).strip()


def _clean(s: str) -> str:
    return _BAD.sub("", s).strip() or "_"


def title_color(c: str) -> str:
    """"baby blue" → "Baby Blue" (file names)."""
    return " ".join(w[:1].upper() + w[1:] for w in (c or "").split()) or "No colour"


def plate_filename(plate: Plate) -> str:
    """Easy to read (0.24): "Bases - Ayra + Manvi + Riya.3mf" in Bases/, "Pink - Ayra + Manvi.3mf" in
    Names/<colour>/ (before 0.24: "{Ayra,pink},{Manvi,pink}.3mf")."""
    head = "Bases" if plate.kind == "bases" else _clean(title_color(plate.orders[0].color if plate.orders else ""))
    stem = f"{head} - " + " + ".join(_clean(o.name) for o in plate.orders)
    if len(stem) > MAX_NAME:
        h = hashlib.sha1(stem.encode()).hexdigest()[:8]
        stem = stem[: MAX_NAME - 10] + "~" + h
    return stem + ".3mf"


def _unique(path: Path, used: set[Path]) -> Path:
    p, n = path, 2
    while p in used or p.exists():
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    used.add(p)
    return p


def object_name(part_key: str) -> str:
    """'Ayra - Name (NS0AB…·3)' → 'Ayra - Name · NS0AB…' (what Bambu Studio shows per object)."""
    m = re.match(r"^(.*) \((.*)·\d+\)$", part_key)
    return f"{m.group(1)} · {m.group(2)}" if m else part_key


def write_plate(plate: Plate, folder: Path, cfg: PlateSettings, used: set[Path]) -> tuple[Path, list[str], dict]:
    """One .3mf per plate: every base / name is its own named object, already in position.

    Returns (path, problems, sizes): `sizes` maps part key → (X, Y, Z) extents measured from the
    file read back from disk, un-rotated to the part's own axes (used for the name/base fit check).
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = _unique(folder / plate_filename(plate), used)
    placed = placed_part_meshes(plate.parts, plate.result)
    objects = [Object3MF(object_name(k), m.vertices, m.faces) for k, m in placed]
    write_3mf(path, objects, title=path.stem)
    problems, objs = check_written(path, objects, cfg)
    rot = {pl.key: pl.rotation for pl in plate.result.placements}
    sizes = {}
    for (key, _), obj in zip(placed, objs or []):
        ext = obj.vertices.max(axis=0) - obj.vertices.min(axis=0)
        x, y, z = (float(v) for v in ext)
        sizes[key] = (y, x, z) if rot.get(key, 0) % 180 == 90 else (x, y, z)
    return path, problems, sizes


def check_written(path: Path, expected: list[Object3MF], cfg: PlateSettings) -> tuple[list[str], list[Object3MF]]:
    """Read the 3MF back from disk and confirm it matches what we meant to write."""
    objs, problems = _check_objects(path, expected, cfg)
    return problems, objs


def _check_objects(path: Path, expected: list[Object3MF], cfg: PlateSettings) -> tuple[list[Object3MF], list[str]]:
    problems = []
    try:
        objs = read_3mf(path)
    except Exception as e:  # noqa: BLE001
        return [], [f"{path.name}: can't be read back ({e})"]
    if len(objs) != len(expected):
        problems.append(f"{path.name}: {len(objs)} objects on disk, expected {len(expected)}")
    if sum(len(o.faces) for o in objs) != sum(len(o.faces) for o in expected):
        problems.append(f"{path.name}: triangle count changed on disk")
    if not objs or any(len(o.vertices) == 0 for o in objs):
        return objs, problems + [f"{path.name}: empty object"]
    allv = np.vstack([o.vertices for o in objs])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    tol = 0.01
    if lo[0] < cfg.margin - tol or lo[1] < cfg.margin - tol:
        problems.append(f"{path.name}: geometry inside the edge margin ({lo[0]:.2f}, {lo[1]:.2f})")
    if hi[0] > cfg.bed_x - cfg.margin + tol or hi[1] > cfg.bed_y - cfg.margin + tol:
        problems.append(f"{path.name}: geometry beyond the bed ({hi[0]:.2f}, {hi[1]:.2f})")
    if hi[2] > cfg.bed_z + tol:
        problems.append(f"{path.name}: taller than the bed ({hi[2]:.2f} mm)")
    if abs(lo[2]) > tol:
        problems.append(f"{path.name}: not sitting on the bed (min Z = {lo[2]:.3f})")
    return objs, problems


def preview(plate: Plate, cfg: PlateSettings) -> dict:
    """Simplified placed footprints for the UI's top-down plate drawing."""
    by_key = {p.key: p for p in plate.parts}
    parts = []
    for pl in plate.result.placements:
        fp = placed_footprint(by_key[pl.key], pl).simplify(0.6)
        geoms = getattr(fp, "geoms", [fp])
        rings = []
        for g in geoms:
            rings.append(np.round(np.asarray(g.exterior.coords), 1).tolist())
            for hole in g.interiors:
                rings.append(np.round(np.asarray(hole.coords), 1).tolist())
        parts.append({"key": pl.key, "rotation": pl.rotation, "rings": rings})
    return {"bed": [cfg.bed_x, cfg.bed_y], "margin": cfg.margin, "parts": parts}


REPORT_COLUMNS = [
    "row", "name", "color", "letters",
    "base_orig_x", "base_orig_y", "base_orig_z",
    "name_orig_x", "name_orig_y", "name_orig_z",
    "target_length", "target_height",
    "base_scale_x_%", "base_scale_y_%", "base_scale_z_%",
    "name_scale_x_%", "name_scale_y_%", "name_scale_z_%",
    "base_final_x", "base_final_y", "base_final_z",
    "name_final_x", "name_final_y", "name_final_z",
    "name_vs_base_length_%", "name_vs_base_height_%", "fit_check",
    "base_file", "name_file", "status", "warnings",
]


def write_report(path: Path, rows: list[dict], settings_json: str) -> None:
    with path.open("w", newline="") as f:
        f.write(f"# settings: {settings_json}\n")
        w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def report_row(order, sr: ScaleResult | None) -> dict:
    r = {"row": order.idx + 1, "name": order.name, "color": order.color, "letters": order.letters}
    if sr is None:
        return r
    f = lambda v: round(v, 2)  # noqa: E731
    pct = lambda v: round(v * 100, 2)  # noqa: E731
    for ax, i in zip("xyz", range(3)):
        r[f"base_orig_{ax}"] = f(sr.base_orig[i])
        r[f"name_orig_{ax}"] = f(sr.name_orig[i])
        r[f"base_scale_{ax}_%"] = pct(sr.base_scale[i])
        r[f"name_scale_{ax}_%"] = pct(sr.name_scale[i])
        r[f"base_final_{ax}"] = f(sr.base_final[i])
        r[f"name_final_{ax}"] = f(sr.name_final[i])
    r["target_length"] = sr.target_length
    r["target_height"] = sr.target_height
    if sr.fit:  # name size relative to its base — identical to MakerWorld's own proportions when it fits
        r["name_vs_base_length_%"] = round(sr.fit["length_final"] * 100, 2)
        r["name_vs_base_height_%"] = round(sr.fit["height_final"] * 100, 2)
    return r
