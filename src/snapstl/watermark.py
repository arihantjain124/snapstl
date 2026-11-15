"""Detect MakerWorld's anti-sale watermark on a downloaded base.

What it is (measured on real downloads made without a license password): a raised text strip on
the BACK face of the base — 1 mm proud of the flat back wall, ~91 mm wide, centred, 2.7–8.7 mm up
from the bottom, the same for every name. A correct license password removes it.

How we detect it: find the main back wall (the +Y-facing plane with the most area) and look for
geometry standing out beyond it. Works on the unscaled model (MakerWorld orientation) and on the
scaled base (Base Y is always 100 %, so the 1 mm stays 1 mm).

Thresholds are deliberately loose on area and tight on height: a real watermark is ~770 mm² of
surface ~1 mm proud; a clean base has nothing beyond its back wall.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import trimesh

PROUD_MM = 0.3  # geometry this far beyond the back wall counts
MIN_AREA_MM2 = 20.0  # ignore specks


@dataclass
class WatermarkResult:
    present: bool
    area_mm2: float = 0.0
    width_mm: float = 0.0
    height_mm: float = 0.0
    faces: np.ndarray | None = None  # boolean mask of watermark faces (for previews)

    def summary(self) -> str:
        if not self.present:
            return "no watermark"
        return f"watermark found on the back ({self.width_mm:.0f} × {self.height_mm:.0f} mm)"


def detect(base: trimesh.Trimesh, axis: int = 1, sign: int = 1) -> WatermarkResult:
    """Look for the watermark on the face pointing along `sign`·axis (default +Y = back)."""
    n = base.face_normals
    a = base.area_faces
    c = base.triangles_center
    facing = n[:, axis] * sign > 0.9
    if not facing.any():
        return WatermarkResult(False)
    coord = np.round(c[facing, axis] * sign, 2)
    vals, inv = np.unique(coord, return_inverse=True)
    wall = vals[np.argmax(np.bincount(inv, a[facing]))]  # main back wall = most area
    beyond = c[:, axis] * sign > wall + PROUD_MM
    area = float(a[beyond].sum())
    if area < MIN_AREA_MM2:
        return WatermarkResult(False, area)
    other = [i for i in range(3) if i != axis]
    pts = c[beyond]
    w = float(pts[:, other[0]].max() - pts[:, other[0]].min())
    h = float(pts[:, other[1]].max() - pts[:, other[1]].min())
    return WatermarkResult(True, area, w, h, beyond)
