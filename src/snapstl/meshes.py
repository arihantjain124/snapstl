"""Mesh loading helpers (OpenSCAD OBJ → trimesh)."""
from __future__ import annotations

import numpy as np
import trimesh


def obj_to_mesh(data: bytes | str) -> trimesh.Trimesh:
    """Parse an OpenSCAD-exported OBJ (v / f lines, polygons fan-triangulated)."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    verts: list[list[float]] = []
    faces: list[list[int]] = []
    for line in data.splitlines():
        if line.startswith("v "):
            p = line.split()
            verts.append([float(p[1]), float(p[2]), float(p[3])])
        elif line.startswith("f "):
            idx = [int(tok.split("/")[0]) for tok in line.split()[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            for k in range(1, len(idx) - 1):
                faces.append([idx[0], idx[k], idx[k + 1]])
    if not verts or not faces:
        raise ValueError("OBJ contains no geometry")
    mesh = trimesh.Trimesh(np.asarray(verts), np.asarray(faces), process=True)
    return mesh


def dims(mesh: trimesh.Trimesh) -> tuple[float, float, float]:
    ext = mesh.bounds[1] - mesh.bounds[0]
    return float(ext[0]), float(ext[1]), float(ext[2])


def to_origin(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Copy translated so the bounding-box min corner is at (0,0,0)."""
    m = mesh.copy()
    m.apply_translation(-m.bounds[0])
    return m
