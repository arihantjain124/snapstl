"""Minimal 3MF (3D Manufacturing Format, core spec) writer and reader.

SnapSTL writes one .3mf per plate. Each part (a base or a name) is its own named
object, in millimetres, already placed at its plate position — Bambu Studio and other
slicers show every part individually. Only the core 3MF spec is used (no slicer-specific
project data), so files stay valid across Bambu Studio updates.

Dependency-free on purpose (stdlib zipfile + xml); `read_3mf` is used to re-check files
after writing.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_PATH = "3D/3dmodel.model"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/{MODEL_PATH}" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


@dataclass
class Object3MF:
    name: str
    vertices: np.ndarray  # (N, 3) float, millimetres
    faces: np.ndarray  # (M, 3) int, counter-clockwise seen from outside


def _mesh_xml(obj: Object3MF, object_id: int) -> str:
    v = np.asarray(obj.vertices, dtype=float)
    f = np.asarray(obj.faces, dtype=np.int64)
    verts = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v)
    tris = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    return (f'<object id="{object_id}" name={quoteattr(obj.name)} type="model">'
            f"<mesh><vertices>\n{verts}\n</vertices><triangles>\n{tris}\n</triangles></mesh></object>")


def write_3mf(path: Path, objects: list[Object3MF], title: str = "") -> None:
    if not objects:
        raise ValueError("Nothing to write")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">',
        '<metadata name="Application">SnapSTL</metadata>',
    ]
    if title:
        parts.append(f"<metadata name=\"Title\">{_esc(title)}</metadata>")
    parts.append("<resources>")
    parts += [_mesh_xml(o, i + 1) for i, o in enumerate(objects)]
    parts.append("</resources><build>")
    parts += [f'<item objectid="{i + 1}"/>' for i in range(len(objects))]
    parts.append("</build></model>")
    tmp = path.with_suffix(path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(MODEL_PATH, "\n".join(parts))
    tmp.replace(path)


def _esc(s: str) -> str:
    return quoteattr(s)[1:-1]


def read_3mf(path: Path) -> list[Object3MF]:
    """Read back the objects of a core-spec 3MF (used to verify what was written)."""
    with zipfile.ZipFile(path) as z:
        data = z.read(MODEL_PATH)
    ns = {"m": CORE_NS}
    root = ET.parse(io.BytesIO(data)).getroot()
    out = []
    for obj in root.findall("m:resources/m:object", ns):
        vs = obj.findall("m:mesh/m:vertices/m:vertex", ns)
        ts = obj.findall("m:mesh/m:triangles/m:triangle", ns)
        v = np.array([[float(e.get("x")), float(e.get("y")), float(e.get("z"))] for e in vs])
        f = np.array([[int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))] for e in ts], dtype=np.int64)
        out.append(Object3MF(obj.get("name") or "", v, f))
    if root.get("unit", "millimeter") != "millimeter":
        raise ValueError("3MF is not in millimetres")
    return out
