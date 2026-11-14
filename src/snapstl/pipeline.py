"""Build stage: cached meshes → scaled parts → plates → STL files + report.

The download stage (makerworld.py) fills the cache; this module only needs the
unscaled base/name meshes for each order, so a batch can be rebuilt instantly
after changing plate or scaling settings.
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
from .scaling import ScaleResult, scale_pair
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

    def complete(self, idx: int) -> bool:
        """True when this order copy has both its base and its name in the output."""
        return ("bases", idx) in self.files and ("names", idx) in self.files


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

    for o in orders:
        if o.idx not in meshes:
            errors.append(f"{o.name}: no model downloaded — left out of this batch")
            continue
        base, name = meshes[o.idx]
        progress(f"Scaling {o.name}")
        b, n, sr = scale_pair(base, name, o.letters, settings.scaling)
        scale[o.idx] = sr
        warnings.extend(f"{o.name}: {w}" for w in sr.warnings)
        tag = o.order_id or f"#{o.idx + 1}"
        base_parts[o.idx] = make_part(f"{o.name} - Base ({tag}·{o.idx + 1})", b)
        name_parts[o.idx] = make_part(f"{o.name} - Name ({tag}·{o.idx + 1})", n)

    progress("Arranging name plates")
    name_plates, errs = group_names(orders, name_parts, settings.plate)
    errors.extend(errs)
    progress("Arranging base plates")
    base_plates, errs = group_bases(orders, base_parts, settings.plate)
    errors.extend(errs)

    used: set[Path] = set()
    plates_out: list[PlateOut] = []
    file_of: dict[tuple[str, int], str] = {}
    for plate in base_plates + name_plates:
        rel = plate_folder(plate.kind, plate.colors[0])
        progress(f"Writing {plate.kind} plate: {', '.join(o.name for o in plate.orders)}")
        path, problems = export.write_plate(plate, out_dir / rel, settings.plate, used)
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

    # Completeness: every order with meshes must appear exactly once per kind.
    for o in orders:
        if o.idx not in meshes:
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
        r["status"] = "ok" if r["base_file"] and r["name_file"] and not mine else "check"
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
    )


def _check_name_colors(plates: list[Plate], errors: list[str]) -> None:
    for p in plates:
        if len(set(p.colors)) != 1:
            errors.append(f"Name plate mixes colours: {p.colors}")
