"""Plates for name stands (0.31): GTA, Harry Potter, Barbie — one piece in two colours, printed in one go.

From the Downloads folder each stand is two meshes in MakerWorld's printing position (`cache.load`): the
coloured **block** at the bottom and the raised **letters** on top (Z = block thickness … + protrusion).

* **Size** (Settings → Name stands): length (X) and letter height (Y) from the table by letter count; the
  thickness (Z) is already right from MakerWorld (back thickness + letter protrusion = 40 mm) and is never
  scaled. Block and letters get exactly the same factors in the same frame, so the letters stay on the block.
* **Plates:** up to 3 stands of one product per plate (a batch is one product); packed like the organizer's
  parts (real footprints, `packing.pack`), with a strip at the back of the bed kept free for the slicer's
  prime tower (one colour change per plate).
* **Files:** `Stands/<Product>/<Product> - A + B + C.3mf`. Each stand is ONE object made of two parts —
  "Base" → filament 1, "Letters" → filament 2 (`threemf.write_3mf_assemblies`, Bambu Studio's
  model_settings.config) — so the slicer prints the letters on the block in the second colour.
  Every file is read back: object / part count, triangles, on the bed, letters on the block, sizes, filaments.

The organizer's plates (pipeline.py) are not touched by any of this.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import trimesh

from . import export, products
from .grouping import Plate
from .meshes import dims
from .orders import Order
from .packing import make_part, pack, transform_matrix
from .pipeline import BuildResult, PlateOut, Progress
from .settings import Settings
from .threemf import read_3mf, read_parts_config, write_3mf_assemblies

PER_PLATE = 3
PRIME_TOWER_MM = 45.0  # back of the bed kept free for the prime tower (colour change)
SIZE_TOL = 0.5         # mm, size of a written stand vs its target
Z_TOL = 0.05           # mm, letters on the block / on the bed
DISTORT = (0.6, 1.6)   # X / Y scale ratio outside this = letters look squeezed or stretched: warning


def band(letters: int, settings: Settings):
    bs = sorted(settings.stands.bands, key=lambda b: b.min_letters)
    return next((b for b in bs if b.min_letters <= letters <= b.max_letters), bs[-1])


def scale_stand(block: trimesh.Trimesh, letters: trimesh.Trimesh, n_letters: int, settings: Settings):
    """(block, letters, info): sized to the table, block on Z = 0, both in the same frame."""
    b = band(n_letters, settings)
    bx, by, bz = dims(block)
    sx, sy = b.length / bx, b.height / by
    m = np.diag([sx, sy, 1.0, 1.0])
    blk, let = block.copy(), letters.copy()
    blk.apply_transform(m)
    let.apply_transform(m)
    lo = blk.bounds[0]
    shift = [-lo[0], -lo[1], -lo[2]]
    blk.apply_translation(shift)
    let.apply_translation(shift)
    warnings = []
    r = sx / sy
    if not (DISTORT[0] <= r <= DISTORT[1]):
        warnings.append(f"letters {'stretched' if r > 1 else 'squeezed'} (length ×{sx:.2f}, height ×{sy:.2f})")
    total = float(let.bounds[1][2] - blk.bounds[0][2])  # block + letters (Z is never scaled)
    info = {"target": [b.length, b.height, round(total, 2)], "orig": [round(bx, 2), round(by, 2), round(bz, 2)],
            "scale": [round(sx * 100, 2), round(sy * 100, 2), 100.0], "warnings": warnings}
    return blk, let, info


def _names(o: Order, label: str) -> str:
    return f"{o.name} - {label} · {o.order_id or '#' + str(o.idx + 1)}"


def build(orders: list[Order], meshes: dict[int, tuple[trimesh.Trimesh, trimesh.Trimesh]], settings: Settings,
          out_dir: Path, progress: Progress = lambda s: None) -> BuildResult:
    errors: list[str] = []
    warnings: list[str] = []
    prod = products.BY_KEY[orders[0].product] if orders else None
    label = prod.label if prod else "Stand"
    colors = settings.stands.products.get(prod.key, {}) if prod else {}
    pair = [colors.get("base_color", ""), colors.get("letter_color", "")]
    cfg = settings.plate
    pack_cfg = replace(cfg, bed_y=cfg.bed_y - PRIME_TOWER_MM)  # parts stay off the back strip
    parts, pieces, info = {}, {}, {}
    for o in orders:
        if o.idx not in meshes:
            errors.append(f"{o.name}: no model downloaded — left out of this batch")
            continue
        progress(f"Scaling {o.name}")
        blk, let, inf = scale_stand(*meshes[o.idx], o.letters, settings)
        info[o.idx] = inf
        warnings.extend(f"{o.name}: {w}" for w in inf["warnings"])
        whole = trimesh.util.concatenate([blk, let])
        lo, hi = whole.bounds
        centre = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
        for m in (blk, let):
            m.apply_translation(centre)
        whole.apply_translation(centre)
        key = _names(o, label)
        parts[o.idx] = make_part(key, whole)
        pieces[o.idx] = (blk, let)

    # up to 3 per plate, oldest first; fewer when they don't fit
    queue = [o for o in orders if o.idx in parts]
    plates: list[Plate] = []
    while queue:
        placed = None
        for k in range(min(PER_PLATE, len(queue)), 0, -1):
            res = pack([parts[o.idx] for o in queue[:k]], pack_cfg)
            if res.ok:
                placed = (queue[:k], res)
                break
        if not placed:
            errors.append(f"{queue[0].name}: doesn't fit on a plate ({pack([parts[queue[0].idx]], pack_cfg).reason})")
            queue = queue[1:]
            continue
        subset, res = placed
        plates.append(Plate("stands", subset, [parts[o.idx] for o in subset], res))
        queue = queue[len(subset):]

    rel = f"Stands/{export.clean_part(label) or 'Stand'}"
    used: set[Path] = set()
    out, files, fit = [], {}, {}
    for plate in plates:
        progress(f"Writing {label} plate: {', '.join(o.name for o in plate.orders)}")
        folder = out_dir / rel
        folder.mkdir(parents=True, exist_ok=True)
        path = export._unique(folder / filename(label, plate.orders), used)
        by_key = {pl.key: pl for pl in plate.result.placements}
        assemblies = []
        for o in plate.orders:
            t = transform_matrix(by_key[parts[o.idx].key])
            blk, let = (m.copy() for m in pieces[o.idx])
            blk.apply_transform(t)
            let.apply_transform(t)
            assemblies.append((export.object_name(parts[o.idx].key), [("Base", blk.vertices, blk.faces, 1),
                                                                     ("Letters", let.vertices, let.faces, 2)]))
        write_3mf_assemblies(path, assemblies, title=path.stem)
        rots = {o.idx: by_key[parts[o.idx].key].rotation for o in plate.orders}
        problems = check(path, plate.orders, assemblies, info, settings, rots)
        errors.extend(problems)
        for o in plate.orders:
            files[("stands", o.idx)] = f"{rel}/{path.name}"
            fit[o.idx] = "ok" if not [p for p in problems if o.name in p or path.name in p] else "check"
        out.append(PlateOut(kind="stands", file=f"{rel}/{path.name}", names=[o.name for o in plate.orders],
                            colors=pair, order_ids=[o.order_id for o in plate.orders],
                            preview=export.preview(plate, cfg), warnings=problems))

    rows = []
    for o in orders:
        inf = info.get(o.idx) or {}
        rows.append({"row": o.idx + 1, "name": o.name, "product": label, "letters": o.letters,
                     "target_length": (inf.get("target") or [""])[0], "target_height": (inf.get("target") or ["", ""])[1],
                     "thickness": (inf.get("target") or ["", "", ""])[2],
                     "orig_x": (inf.get("orig") or [""])[0], "orig_y": (inf.get("orig") or ["", ""])[1],
                     "scale_x_%": (inf.get("scale") or [""])[0], "scale_y_%": (inf.get("scale") or ["", ""])[1],
                     "file": files.get(("stands", o.idx), ""),
                     "status": "ok" if fit.get(o.idx) == "ok" else "check",
                     "warnings": " | ".join(inf.get("warnings") or [])})
    write_report(out_dir / "report.csv", rows, json.dumps(settings.to_dict()))
    return BuildResult(out_dir=out_dir, plates=out, scale={}, errors=errors, warnings=warnings, ok=not errors,
                       files=files, fit=fit)


def filename(label: str, orders: list[Order]) -> str:
    stem = f"{export._clean(label)} - " + " + ".join(export._clean(o.name) for o in orders)
    if len(stem) > export.MAX_NAME:
        import hashlib

        stem = stem[: export.MAX_NAME - 10] + "~" + hashlib.sha1(stem.encode()).hexdigest()[:8]
    return stem + ".3mf"


def check(path: Path, orders: list[Order], assemblies: list, info: dict, settings: Settings,
          rots: dict[int, int] | None = None) -> list[str]:
    """Read the plate back from disk: every stand there, on the bed, letters on the block, its size, filaments."""
    cfg = settings.plate
    try:
        objs = read_3mf(path)
        conf = read_parts_config(path)
    except Exception as e:  # noqa: BLE001
        return [f"{path.name}: can't be read back ({e})"]
    problems = []
    want = [p for _, ps in assemblies for p in ps]
    if len(objs) != len(want):
        return [f"{path.name}: {len(objs)} parts on disk, expected {len(want)}"]
    if sum(len(o.faces) for o in objs) != sum(len(p[2]) for p in want):
        problems.append(f"{path.name}: triangle count changed on disk")
    allv = np.vstack([o.vertices for o in objs])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    if lo[0] < cfg.margin - 0.01 or lo[1] < cfg.margin - 0.01 or hi[0] > cfg.bed_x - cfg.margin + 0.01 \
            or hi[1] > cfg.bed_y - PRIME_TOWER_MM - cfg.margin + 0.01:
        problems.append(f"{path.name}: outside the bed margins or on the prime tower strip")
    if abs(lo[2]) > Z_TOL:
        problems.append(f"{path.name}: not sitting on the bed (min Z = {lo[2]:.3f})")
    if [len(ps) for _, ps in conf] != [2] * len(orders) or any([e for _, e in ps] != [1, 2] for _, ps in conf):
        problems.append(f"{path.name}: filaments not set as Base → 1, Letters → 2")
    for i, o in enumerate(orders):
        blk, let = objs[2 * i], objs[2 * i + 1]
        bmin, bmax = blk.vertices.min(axis=0), blk.vertices.max(axis=0)
        lmin, lmax = let.vertices.min(axis=0), let.vertices.max(axis=0)
        if abs(lmin[2] - bmax[2]) > Z_TOL:
            problems.append(f"{o.name}: letters not on the block ({lmin[2] - bmax[2]:+.2f} mm) in {path.name}")
        if not (bmin[0] - 0.01 <= lmin[0] and lmax[0] <= bmax[0] + 0.01 and bmin[1] - 0.01 <= lmin[1]
                and lmax[1] <= bmax[1] + 0.01):
            problems.append(f"{o.name}: letters stick out of the block in {path.name}")
        tl, th, tz = info[o.idx]["target"]
        size = bmax - bmin
        x, y = (size[1], size[0]) if (rots or {}).get(o.idx, 0) % 180 == 90 else (size[0], size[1])
        z = lmax[2] - bmin[2]
        if abs(x - tl) > SIZE_TOL or abs(y - th) > SIZE_TOL or abs(z - tz) > SIZE_TOL:
            problems.append(f"{o.name}: written {x:.1f} × {y:.1f} × {z:.1f} mm, wanted {tl:g} × {th:g} × {tz:g} "
                            f"in {path.name}")
    return problems


def repair_031() -> int:
    """Once: batches built by 0.31 kept their stand plates in summary["stand_plates"]; since 0.32 they are the
    batch's "plates" (kind "stands"), so Print / Verify see them. Returns how many batches changed."""
    from . import db

    n = 0
    with db.tx() as con:
        for b in db.rows(con, "SELECT id, summary FROM batches WHERE summary LIKE '%stand_plates%'"):
            s = json.loads(b["summary"] or "{}")
            sp = s.pop("stand_plates", None)
            if not sp:
                continue
            have = {p["file"] for p in s.get("plates") or []}
            s["plates"] = (s.get("plates") or []) + [p for p in sp if p["file"] not in have]
            con.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps(s), b["id"]))
            n += 1
    return n


# ---------------- Verify: a spoiled stand is printed again on a fix plate (0.32) ----------------
def stand_meshes(plate_path: Path, idx: int) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """The block and letters of the `idx`-th stand on a written plate (parts are written in stand order)."""
    objs = read_3mf(plate_path)
    if len(objs) < 2 * (idx + 1):
        raise ValueError(f"Stand {idx + 1} isn't on {plate_path.name}")
    blk, let = objs[2 * idx], objs[2 * idx + 1]
    return (trimesh.Trimesh(np.asarray(blk.vertices), np.asarray(blk.faces), process=True),
            trimesh.Trimesh(np.asarray(let.vertices), np.asarray(let.faces), process=True))


def write_fix(folder: Path, stem: str, stands_: list[tuple[str, trimesh.Trimesh, trimesh.Trimesh]],
              settings: Settings) -> tuple[str, dict]:
    """A fix plate of whole stands (as printed, same size): [(object name, block, letters)]. Returns (file, preview)."""
    cfg = settings.plate
    pack_cfg = replace(cfg, bed_y=cfg.bed_y - PRIME_TOWER_MM)
    parts, pieces = [], {}
    for name, blk, let in stands_:
        blk, let = blk.copy(), let.copy()
        whole = trimesh.util.concatenate([blk, let])
        lo, hi = whole.bounds
        c = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
        for m in (blk, let, whole):
            m.apply_translation(c)
        p = make_part(name, whole)
        parts.append(p)
        pieces[name] = (blk, let)
    res = pack(parts, pack_cfg, try_orders=len(parts) <= 4)
    if not res.ok:
        raise ValueError(f"Doesn't fit on one plate: {res.reason}")
    folder.mkdir(parents=True, exist_ok=True)
    path = export._unique(folder / f"{export._clean(stem)[:180]}.3mf", set())
    by_key = {pl.key: pl for pl in res.placements}
    assemblies = []
    for p in parts:
        t = transform_matrix(by_key[p.key])
        blk, let = (m.copy() for m in pieces[p.key])
        blk.apply_transform(t)
        let.apply_transform(t)
        assemblies.append((p.key, [("Base", blk.vertices, blk.faces, 1), ("Letters", let.vertices, let.faces, 2)]))
    write_3mf_assemblies(path, assemblies, title=path.stem)
    objs = read_3mf(path)
    if len(objs) != 2 * len(parts) or abs(np.vstack([o.vertices for o in objs]).min(axis=0)[2]) > Z_TOL:
        path.unlink(missing_ok=True)
        raise ValueError(f"{path.name}: didn't read back right")
    return path.name, export.preview(Plate("stands", [], parts, res), cfg)


REPORT = ["row", "name", "product", "letters", "target_length", "target_height", "thickness", "orig_x", "orig_y",
          "scale_x_%", "scale_y_%", "file", "status", "warnings"]


def write_report(path: Path, rows: list[dict], settings_json: str) -> None:
    import csv

    with path.open("w", newline="") as f:
        f.write(f"# settings: {settings_json}\n")
        w = csv.DictWriter(f, fieldnames=REPORT, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
