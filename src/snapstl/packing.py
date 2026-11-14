"""Plate packing on real 2-D footprints.

Each part is reduced to its footprint (projection onto the bed, XY). Parts are
only rotated about Z (the build face never changes). Parts are stacked along
one bed axis; every new part starts clear of the others and slides back
towards them until it is exactly `gap` mm away, so letter bumps can nest.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
import trimesh
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from .settings import PlateSettings

SIMPLIFY_TOL = 0.3  # mm; packing polygons are simplified then grown by this much (conservative)
EPS = 1e-3


@dataclass
class Part:
    key: str  # e.g. "Ayra - Name"
    mesh: trimesh.Trimesh  # centred: XY bbox centre at origin, min Z = 0
    footprint: Polygon | MultiPolygon  # exact projection, same frame as mesh
    pack_poly: Polygon | MultiPolygon  # simplified + grown copy used while searching

    @property
    def height(self) -> float:
        return float(self.mesh.bounds[1][2])


@dataclass
class Placement:
    key: str
    rotation: int  # degrees about Z
    tx: float
    ty: float


@dataclass
class PackResult:
    ok: bool
    placements: list[Placement] = field(default_factory=list)
    extent: tuple[float, float] = (0.0, 0.0)  # used width (X), depth (Y) incl. parts only
    reason: str = ""


def footprint(mesh: trimesh.Trimesh) -> Polygon | MultiPolygon:
    """Exact XY projection of a mesh (union of all triangles seen from above)."""
    tris = mesh.vertices[mesh.faces][:, :, :2]
    # drop triangles that project to (near) zero area — vertical walls
    a = tris[:, 1] - tris[:, 0]
    b = tris[:, 2] - tris[:, 0]
    area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) * 0.5
    tris = tris[area > 1e-9]
    polys = shapely.polygons(np.concatenate([tris, tris[:, :1]], axis=1))
    fp = shapely.union_all(polys)
    if fp.geom_type == "MultiPolygon":
        # close hairline cracks between triangles without growing the outline
        fp = fp.buffer(0.01, join_style="mitre").buffer(-0.01, join_style="mitre")
    fp = fp.buffer(0)
    if fp.is_empty:
        raise ValueError("Mesh has an empty footprint")
    return fp


def make_part(key: str, mesh: trimesh.Trimesh) -> Part:
    m = mesh.copy()
    lo, hi = m.bounds
    m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    fp = footprint(m)
    pp = fp.simplify(SIMPLIFY_TOL, preserve_topology=True).buffer(SIMPLIFY_TOL, join_style="mitre")
    return Part(key=key, mesh=m, footprint=fp, pack_poly=pp)


def _rot(poly, deg: int):
    return affinity.rotate(poly, deg, origin=(0, 0)) if deg % 360 else poly


def _slide(placed, poly, axis: int, gap: float):
    """Place `poly` after the already-placed union along `axis` (0=X, 1=Y).

    Returns the offset along `axis`; the cross-axis is centred at 0.
    """
    pb = placed.bounds  # minx, miny, maxx, maxy
    qb = poly.bounds
    hi_placed = pb[2 + axis]
    lo_q = qb[axis]
    q_len = qb[2 + axis] - qb[axis]

    def moved(t):
        return affinity.translate(poly, xoff=t if axis == 0 else 0, yoff=t if axis == 1 else 0)

    start = hi_placed - lo_q + gap + EPS  # bbox-clear: always valid
    lowest = pb[axis] - lo_q  # never slide past the start of the placed block
    step = 1.0
    t_ok = start
    t = start - step
    while t >= lowest:
        if placed.distance(moved(t)) >= gap:
            t_ok = t
            t -= step
        else:
            break
    else:
        return t_ok
    # refine between t (invalid) and t_ok (valid)
    lo, hi = t, t_ok
    for _ in range(8):
        mid = (lo + hi) / 2
        if placed.distance(moved(mid)) >= gap:
            hi = mid
        else:
            lo = mid
    return hi


def _layout(polys, rotations, axis: int, gap: float):
    """Place rotated polys in sequence; returns (offsets[(dx,dy)], union)."""
    offsets = []
    placed = None
    for poly, rot in zip(polys, rotations):
        p = _rot(poly, rot)
        b = p.bounds
        cross = -(b[1 - axis] + b[3 - axis]) / 2  # centre on the cross axis
        if placed is None:
            along = -b[axis]
        else:
            pc = affinity.translate(p, xoff=cross if axis == 1 else 0, yoff=cross if axis == 0 else 0)
            along = _slide(placed, pc, axis, gap)
        dx, dy = (along, cross) if axis == 0 else (cross, along)
        offsets.append((dx, dy))
        moved = affinity.translate(p, dx, dy)
        placed = moved if placed is None else unary_union([placed, moved])
    return offsets, placed


def pack(parts: list[Part], cfg: PlateSettings, try_orders: bool = True) -> PackResult:
    """Find a collision-free arrangement of all `parts` on one plate."""
    if not parts:
        return PackResult(ok=True)
    tall = [p.key for p in parts if p.height > cfg.bed_z]
    if tall:
        return PackResult(ok=False, reason=f"Taller than the bed: {', '.join(tall)}")

    usable_x = cfg.bed_x - 2 * cfg.margin
    usable_y = cfg.bed_y - 2 * cfg.margin
    rots = sorted({r % 360 for r in cfg.rotations}) or [0]
    orders = list(itertools.permutations(range(len(parts)))) if try_orders else [tuple(range(len(parts)))]

    best = None  # (score, order, rotations, axis, offsets, union)
    for axis in (1, 0):
        for order in orders:
            polys = [parts[i].pack_poly for i in order]
            for rot_combo in itertools.product(rots, repeat=len(parts)):
                offsets, union = _layout(polys, rot_combo, axis, cfg.gap)
                minx, miny, maxx, maxy = union.bounds
                w, d = maxx - minx, maxy - miny
                if w > usable_x + EPS or d > usable_y + EPS:
                    continue
                slack = min(usable_x - w, usable_y - d)
                score = (-slack, w * d)
                if best is None or score < best[0]:
                    best = (score, order, rot_combo, axis, offsets, union)
        if best is not None and len(parts) == 1:
            break

    if best is None:
        return PackResult(ok=False, reason=f"{len(parts)} part(s) do not fit on {cfg.bed_x:g}×{cfg.bed_y:g} mm")

    _, order, rot_combo, axis, offsets, union = best
    minx, miny, maxx, maxy = union.bounds
    # centre the block on the bed
    cx = cfg.bed_x / 2 - (minx + maxx) / 2
    cy = cfg.bed_y / 2 - (miny + maxy) / 2
    placements = [
        Placement(key=parts[i].key, rotation=int(r), tx=dx + cx, ty=dy + cy)
        for i, r, (dx, dy) in zip(order, rot_combo, offsets)
    ]
    # report front → back (ascending Y), then left → right
    placements.sort(key=lambda p: (round(p.ty, 1), p.tx))
    res = PackResult(ok=True, placements=placements, extent=(maxx - minx, maxy - miny))
    problems = verify(parts, res, cfg)
    if problems:
        return PackResult(ok=False, reason="; ".join(problems))
    return res


def placed_footprint(part: Part, pl: Placement):
    return affinity.translate(_rot(part.footprint, pl.rotation), pl.tx, pl.ty)


def transform_matrix(pl: Placement) -> np.ndarray:
    m = trimesh.transformations.rotation_matrix(math.radians(pl.rotation), [0, 0, 1])
    m[0, 3], m[1, 3] = pl.tx, pl.ty
    return m


def verify(parts: list[Part], res: PackResult, cfg: PlateSettings) -> list[str]:
    """Check exact footprints: inside bed margins and ≥ gap apart."""
    by_key = {p.key: p for p in parts}
    problems = []
    if sorted(pl.key for pl in res.placements) != sorted(by_key):
        problems.append("placement list does not match parts")
        return problems
    area = box(cfg.margin, cfg.margin, cfg.bed_x - cfg.margin, cfg.bed_y - cfg.margin)
    fps = {pl.key: placed_footprint(by_key[pl.key], pl) for pl in res.placements}
    for k, fp in fps.items():
        if not area.buffer(EPS * 10).contains(fp):
            problems.append(f"{k} is outside the bed margin")
    keys = list(fps)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            d = fps[keys[i]].distance(fps[keys[j]])
            if d < cfg.gap - 0.05:
                problems.append(f"{keys[i]} and {keys[j]} are {d:.2f} mm apart (< {cfg.gap} mm)")
    return problems


def build_plate_mesh(parts: list[Part], res: PackResult) -> trimesh.Trimesh:
    by_key = {p.key: p for p in parts}
    meshes = []
    for pl in res.placements:
        m = by_key[pl.key].mesh.copy()
        m.apply_transform(transform_matrix(pl))
        meshes.append(m)
    return trimesh.util.concatenate(meshes)
