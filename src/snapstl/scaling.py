"""Scaling Rules v1.1.

Base:  X = L / base_X,  Y = 100 %,       Z = H / base_Z
Name:  X = Base X,      Y = Base Z,      Z = 50 %

Meshes are expected in MakerWorld "NO AMS" orientation:
  base  — upright, X = length, Y = depth, Z = height
  name  — lying flat, X = length, Y = letter height, Z = thickness
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import trimesh

from .meshes import dims, to_origin
from .settings import ScaleBand, ScalingSettings


@dataclass
class ScaleResult:
    letters: int
    target_length: float
    target_height: float
    base_orig: tuple[float, float, float]
    name_orig: tuple[float, float, float]
    base_scale: tuple[float, float, float]
    name_scale: tuple[float, float, float]
    base_final: tuple[float, float, float]
    name_final: tuple[float, float, float]
    warnings: list[str] = field(default_factory=list)


def pick_band(letters: int, bands: list[ScaleBand]) -> ScaleBand:
    for b in sorted(bands, key=lambda b: b.min_letters):
        if b.min_letters <= letters <= b.max_letters:
            return b
    # Out of table: nearest band
    bands = sorted(bands, key=lambda b: b.min_letters)
    return bands[0] if letters < bands[0].min_letters else bands[-1]


def _scale_matrix(sx: float, sy: float, sz: float) -> np.ndarray:
    m = np.eye(4)
    m[0, 0], m[1, 1], m[2, 2] = sx, sy, sz
    return m


def scale_pair(
    base: trimesh.Trimesh,
    name: trimesh.Trimesh,
    letters: int,
    cfg: ScalingSettings,
) -> tuple[trimesh.Trimesh, trimesh.Trimesh, ScaleResult]:
    """Return scaled copies (min corner at origin) and the scaling report."""
    warnings: list[str] = []
    bx, by, bz = dims(base)
    nx, ny, nz = dims(name)

    # Orientation sanity checks (catch a changed model or swapped plates).
    if not (bx > by and bz > 20):
        warnings.append(f"Base orientation looks unusual ({bx:.1f}×{by:.1f}×{bz:.1f} mm)")
    if not (nz < 15 and nx > ny):
        warnings.append(f"Name orientation looks unusual ({nx:.1f}×{ny:.1f}×{nz:.1f} mm)")
    if nx > bx * 1.05:
        warnings.append(f"Name ({nx:.1f} mm) is longer than its base ({bx:.1f} mm)")

    band = pick_band(letters, cfg.bands)
    L = min(band.length, cfg.max_length)
    H = band.height
    if not (cfg.min_height <= H <= cfg.max_height):
        warnings.append(f"Target height {H} mm is outside {cfg.min_height}–{cfg.max_height} mm")

    sx = L / bx
    sz = H / bz
    sy = cfg.base_y_scale
    base_scale = (sx, sy, sz)
    name_scale = (sx, sz, cfg.name_z_scale)

    stretch = sx / sz
    if not (0.6 <= stretch <= 1.6):
        warnings.append(
            f"Letters are distorted: length scale {sx*100:.0f}% vs height scale {sz*100:.0f}% "
            f"(ratio {stretch:.2f}). Consider changing Font size / Thickness."
        )

    b = to_origin(base)
    b.apply_transform(_scale_matrix(*base_scale))
    n = to_origin(name)
    n.apply_transform(_scale_matrix(*name_scale))

    res = ScaleResult(
        letters=letters,
        target_length=L,
        target_height=H,
        base_orig=(bx, by, bz),
        name_orig=(nx, ny, nz),
        base_scale=base_scale,
        name_scale=name_scale,
        base_final=dims(b),
        name_final=dims(n),
        warnings=warnings,
    )
    return b, n, res
