"""Build stage: cached meshes → scaled parts → plates → one .3mf per plate + report.csv.

Input is the unscaled base/name meshes per order (filled into the cache by the SnapSTL
Helper via batches.py), so plates can be rebuilt without downloading again.
Output layout: <output>/<YYYY-MM-DD>/Batch N - <YYYY-MM-DD>/{Bases/, Names/<colour>/, report.csv}.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import trimesh

from . import export
from .grouping import Plate, group_bases, group_names
from .orders import Order
from .packing import make_part
from .scaling import ScaleResult, fit_metrics, fit_problems, scale_pair
from .settings import Settings

Progress = Callable[[str], None]


@dataclass
class PlateOut:
    kind: str
    file: str
    names: list[str]
    colors: list[str]
    preview: dict
    warnings: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    out_dir: Path
    plates: list[PlateOut]
    scale: dict[int, ScaleResult]
    errors: list[str]
    warnings: list[str]
    ok: bool
    files: dict[tuple[str, int], str] = field(default_factory=dict)  # (kind, order idx) → file
    fit: dict[int, str] = field(default_factory=dict)  # order idx → "ok" or why the pair won't fit

    def complete(self, idx: int) -> bool:
        """True when this order copy has its base and its name in the output AND they fit each other."""
        return ("bases", idx) in self.files and ("names", idx) in self.files and self.fit.get(idx) == "ok"


BATCH_RE = re.compile(r"^Batch (\d+) - ")


def new_batch_dir(root: str | Path, batch_id: int | None = None) -> Path:
    """<root>/<YYYY-MM-DD>/Batch N - <YYYY-MM-DD>; N starts again at 1 every day.

    `batch_id` (the app's internal number) is not part of the folder name.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    day_dir = Path(root).expanduser() / day
    day_dir.mkdir(parents=True, exist_ok=True)
    taken = [int(m.group(1)) for p in day_dir.iterdir() if (m := BATCH_RE.match(p.name))]
    n = max(taken, default=0) + 1
    while True:
        d = day_dir / f"Batch {n} - {day}"
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def plate_folder(kind: str, color: str) -> str:
    """Bases/ for all bases; Names/<colour>/ for names (one folder per colour)."""
    if kind == "bases":
        return "Bases"
    return f"Names/{export.clean_part(color) or 'no colour'}"


def build(
    orders: list[Order],
    meshes: dict[int, tuple[trimesh.Trimesh, trimesh.Trimesh]],
    settings: Settings,
    out_dir: Path,
    progress: Progress = lambda s: None,
) -> BuildResult:
    errors: list[str] = []
    warnings: list[str] = []
    scale: dict[int, ScaleResult] = {}
    base_parts, name_parts = {}, {}
    fit: dict[int, str] = {}
    held: set[int] = set()  # pairs that failed the fit check: never written to a plate

    for o in orders:
        if o.idx not in meshes:
            errors.append(f"{o.name}: no model downloaded — left out of this batch")
            continue
        base, name = meshes[o.idx]
        progress(f"Scaling {o.name}")
        b, n, sr = scale_pair(base, name, o.letters, settings.scaling)
        scale[o.idx] = sr
        warnings.extend(f"{o.name}: {w}" for w in sr.warnings)
        if sr.fit_errors:  # name would not fit its base → don't print either part
            fit[o.idx] = "; ".join(sr.fit_errors)
            held.add(o.idx)
            errors.append(f"{o.name}: fit check failed ({fit[o.idx]}) — held back, order returned to Ready")
            continue
        tag = o.order_id or f"#{o.idx + 1}"
        base_parts[o.idx] = make_part(f"{o.name} - Base ({tag}·{o.idx + 1})", b)
        name_parts[o.idx] = make_part(f"{o.name} - Name ({tag}·{o.idx + 1})", n)

    progress("Arranging name plates")
    name_plates, errs = group_names([o for o in orders if o.idx not in held], name_parts, settings.plate)
    errors.extend(errs)
    progress("Arranging base plates")
    base_plates, errs = group_bases([o for o in orders if o.idx not in held], base_parts, settings.plate)
    errors.extend(errs)

    used: set[Path] = set()
    file_sizes: dict[tuple[str, int], tuple | None] = {}
    plates_out: list[PlateOut] = []
    file_of: dict[tuple[str, int], str] = {}
    for plate in base_plates + name_plates:
        rel = plate_folder(plate.kind, plate.colors[0])
        progress(f"Writing {plate.kind} plate: {', '.join(o.name for o in plate.orders)}")
        path, problems, sizes = export.write_plate(plate, out_dir / rel, settings.plate, used)
        for o in plate.orders:
            part = (base_parts if plate.kind == "bases" else name_parts)[o.idx]
            file_sizes[(plate.kind, o.idx)] = sizes.get(part.key)
        errors.extend(problems)
        for o in plate.orders:
            file_of[(plate.kind, o.idx)] = f"{rel}/{path.name}"
        plates_out.append(
            PlateOut(
                kind=plate.kind,
                file=f"{rel}/{path.name}",
                names=[o.name for o in plate.orders],
                colors=plate.colors,
                preview=export.preview(plate, settings.plate),
                warnings=plate.warnings + problems,
            )
        )
        warnings.extend(plate.warnings)

    # Fit check on the files themselves: measure each pair as written to disk.
    for o in orders:
        if o.idx not in scale or o.idx in held:
            continue
        b_sz, n_sz = file_sizes.get(("bases", o.idx)), file_sizes.get(("names", o.idx))
        if not b_sz or not n_sz:
            continue  # reported as missing below
        sr = scale[o.idx]
        probs = fit_problems(fit_metrics(sr.base_orig, sr.name_orig, b_sz, n_sz))
        fit[o.idx] = "ok" if not probs else "; ".join(probs)
        if probs:
            errors.append(f"{o.name}: fit check on the written files failed ({fit[o.idx]}) — don't print this pair")

    # Completeness: every order with meshes must appear exactly once per kind.
    for o in orders:
        if o.idx not in meshes or o.idx in held:
            continue
        for kind in ("bases", "names"):
            if (kind, o.idx) not in file_of:
                errors.append(f"{o.name}: {kind[:-1]} is missing from the output")
    _check_name_colors(name_plates, errors)

    rows = []
    for o in orders:
        r = export.report_row(o, scale.get(o.idx))
        r["base_file"] = file_of.get(("bases", o.idx), "")
        r["name_file"] = file_of.get(("names", o.idx), "")
        mine = [e for e in errors if e.startswith(o.name + ":") or o.name in e]
        r["fit_check"] = fit.get(o.idx, "")
        r["status"] = "ok" if r["base_file"] and r["name_file"] and fit.get(o.idx) == "ok" and not mine else "check"
        r["warnings"] = " | ".join(scale[o.idx].warnings if o.idx in scale else [])
        rows.append(r)
    export.write_report(out_dir / "report.csv", rows, json.dumps(settings.to_dict()))

    return BuildResult(
        out_dir=out_dir,
        plates=plates_out,
        scale=scale,
        errors=errors,
        warnings=warnings,
        ok=not errors,
        files=file_of,
        fit=fit,
    )


def _check_name_colors(plates: list[Plate], errors: list[str]) -> None:
    for p in plates:
        if len(set(p.colors)) != 1:
            errors.append(f"Name plate mixes colours: {p.colors}")
