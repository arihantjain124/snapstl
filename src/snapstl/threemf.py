"""Minimal 3MF (3D Manufacturing Format, core spec) writer and reader.

SnapSTL writes one .3mf per plate. Each part (a base or a name) is its own named
object, in millimetres, already placed at its plate position — Bambu Studio and other
slicers show every part individually. Geometry is core 3MF only; the one slicer-specific bit
is a small names-only config (Metadata/model_settings.config, Metadata/Slic3r_PE_model.config)
because Bambu Studio ignores the core object name and shows the file name instead. No slicer
settings, so files stay valid across Bambu Studio updates.

Dependency-free on purpose (stdlib zipfile + xml); `read_3mf` is used to re-check files
after writing.

Name stands (0.31, `write_3mf_assemblies`): each stand is one object made of two parts (core 3MF
components), and model_settings.config gives each part its filament — Bambu Studio's own fields
(`<object><part subtype="normal_part"><metadata key="extruder" …>`), which Bambuddy's slicer and its
filament mapping read.
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
BAMBU_CONFIG = "Metadata/model_settings.config"  # object names for Bambu Studio
PRUSA_CONFIG = "Metadata/Slic3r_PE_model.config"  # same, for PrusaSlicer/OrcaSlicer

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
    names = _names_config(objects)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(MODEL_PATH, "\n".join(parts))
        # Bambu Studio (and PrusaSlicer) ignore the core `name` attribute and call every object after
        # the file; they take object names from their own config file instead. Names only — no
        # slicer settings — so the file stays a plain geometry 3MF.
        z.writestr(BAMBU_CONFIG, names)
        z.writestr(PRUSA_CONFIG, names)
    tmp.replace(path)


def write_3mf_assemblies(path: Path, assemblies: list[tuple[str, list[tuple]]], title: str = "") -> None:
    """One object per entry, made of parts: [(object name, [(part name, vertices, faces, filament), …]), …].
    Parts are in bed coordinates (no transforms). Filament numbers start at 1."""
    if not assemblies:
        raise ValueError("Nothing to write")
    res, items, conf = [], [], []
    nid = 1
    for name, parts in assemblies:
        ids = []
        for pname, v, f, _ in parts:
            res.append(_mesh_xml(Object3MF(pname, v, f), nid))
            ids.append(nid)
            nid += 1
        aid = nid
        nid += 1
        comps = "".join(f'<component objectid="{i}"/>' for i in ids)
        res.append(f'<object id="{aid}" name={quoteattr(name)} type="model"><components>{comps}</components></object>')
        items.append(f'<item objectid="{aid}"/>')
        rows = [f'  <object id="{aid}">', f'    <metadata key="name" value="{_attr(name)}"/>',
                f'    <metadata key="extruder" value="{int(parts[0][3])}"/>']
        for i, (pname, _, _, ext) in zip(ids, parts):
            rows += [f'    <part id="{i}" subtype="normal_part">', f'      <metadata key="name" value="{_attr(pname)}"/>',
                     f'      <metadata key="extruder" value="{int(ext)}"/>', "    </part>"]
        rows.append("  </object>")
        conf.append("\n".join(rows))
    model = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">',
             '<metadata name="Application">SnapSTL</metadata>']
    if title:
        model.append(f"<metadata name=\"Title\">{_esc(title)}</metadata>")
    model += ["<resources>", *res, "</resources><build>", *items, "</build></model>"]
    config = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "\n".join(conf) + "\n</config>\n"
    tmp = path.with_suffix(path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(MODEL_PATH, "\n".join(model))
        z.writestr(BAMBU_CONFIG, config)
    tmp.replace(path)


def read_parts_config(path: Path) -> list[tuple[str, list[tuple[str, int]]]]:
    """model_settings.config of a stands plate: [(object name, [(part name, filament), …]), …]."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(BAMBU_CONFIG))
    out = []
    for obj in root.findall("object"):
        name = next((m.get("value") for m in obj.findall("metadata") if m.get("key") == "name"), "")
        parts = []
        for p in obj.findall("part"):
            meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
            parts.append((meta.get("name", ""), int(meta.get("extruder") or 0)))
        out.append((name or "", parts))
    return out


def _names_config(objects: list[Object3MF]) -> str:
    rows = [f'  <object id="{i + 1}">\n    <metadata type="object" key="name" value="{_attr(o.name)}"/>\n  </object>'
            for i, o in enumerate(objects)]
    return '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "\n".join(rows) + "\n</config>\n"


def _attr(s: str) -> str:  # for a double-quoted attribute
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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
        if obj.find("m:mesh", ns) is None:
            continue  # an object made of parts (components): its parts are read as their own objects
        vs = obj.findall("m:mesh/m:vertices/m:vertex", ns)
        ts = obj.findall("m:mesh/m:triangles/m:triangle", ns)
        v = np.array([[float(e.get("x")), float(e.get("y")), float(e.get("z"))] for e in vs])
        f = np.array([[int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))] for e in ts], dtype=np.int64)
        out.append(Object3MF(obj.get("name") or "", v, f))
    if root.get("unit", "millimeter") != "millimeter":
        raise ValueError("3MF is not in millimetres")
    return out
