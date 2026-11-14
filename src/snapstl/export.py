"""Write plate STLs, re-check them from disk, and write the report."""
from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

import numpy as np
import trimesh

from .grouping import Plate
from .packing import build_plate_mesh, placed_footprint
from .scaling import ScaleResult
from .settings import PlateSettings

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME = 200


def _clean(s: str) -> str:
    return _BAD.sub("", s).strip() or "_"


def plate_filename(plate: Plate) -> str:
    names = [_clean(o.name) for o in plate.orders]
    colors = [_clean(c) for c in plate.colors]
    if plate.kind == "names":
        stem = f"({','.join(names)})-({colors[0]})"
    else:
        tail = [colors[0]] if len(set(colors)) == 1 else colors
        stem = f"bases-({','.join(names + tail)})"
    if len(stem) > MAX_NAME:
        h = hashlib.sha1(stem.encode()).hexdigest()[:8]
        stem = stem[: MAX_NAME - 10] + "~" + h
    return stem + ".stl"


def _unique(path: Path, used: set[Path]) -> Path:
    p, n = path, 2
    while p in used or p.exists():
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    used.add(p)
    return p


def write_plate(plate: Plate, folder: Path, cfg: PlateSettings, used: set[Path]) -> tuple[Path, list[str]]:
    folder.mkdir(parents=True, exist_ok=True)
    path = _unique(folder / plate_filename(plate), used)
    mesh = build_plate_mesh(plate.parts, plate.result)
    mesh.export(path, file_type="stl")
    return path, check_written(path, mesh, cfg)


def check_written(path: Path, expected: trimesh.Trimesh, cfg: PlateSettings) -> list[str]:
    """Reload the STL from disk and confirm it matches what we meant to write."""
    problems = []
    m = trimesh.load(path, file_type="stl", force="mesh", process=False)
    if len(m.faces) != len(expected.faces):
        problems.append(f"{path.name}: triangle count changed on disk")
    lo, hi = m.bounds
    tol = 0.01
    if lo[0] < cfg.margin - tol or lo[1] < cfg.margin - tol:
        problems.append(f"{path.name}: geometry inside the edge margin ({lo[0]:.2f}, {lo[1]:.2f})")
    if hi[0] > cfg.bed_x - cfg.margin + tol or hi[1] > cfg.bed_y - cfg.margin + tol:
        problems.append(f"{path.name}: geometry beyond the bed ({hi[0]:.2f}, {hi[1]:.2f})")
    if hi[2] > cfg.bed_z + tol:
        problems.append(f"{path.name}: taller than the bed ({hi[2]:.2f} mm)")
    if abs(lo[2]) > tol:
        problems.append(f"{path.name}: not sitting on the bed (min Z = {lo[2]:.3f})")
    return problems


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
    return r
