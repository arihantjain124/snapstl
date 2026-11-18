"""Local catalog and validated STL previews, separate from production downloads and batches."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock
from typing import Literal

import numpy as np
import shapely
import trimesh
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from shapely import affinity
from shapely.geometry import Polygon

from . import platform_os
from .config import ROOT
from .meshes import dims, to_origin
from .scaling import fit_metrics, fit_problems, pick_band, scale_pair
from .settings import APP_DIR, ModelParams, PlateSettings, ScalingSettings

SLUG = r"^[a-z][a-z0-9-]{0,47}$"
VARIABLE = r"^[a-z][a-z0-9_]{0,47}$"
MAX_STL = 8_000_000
MAX_JOB = 12_000_000
RENDER_TIMEOUT = 30
CATALOG_ROOT = Path(os.environ.get("SNAPSTL_MODELS_DIR", ROOT / "models")).expanduser()
APPROVAL_ROOT = APP_DIR / "model-approvals"
RENDER_LOCK = Lock()
PREVIEW_LOCK = Lock()
APPROVAL_LOCK = Lock()
PREVIEWS: OrderedDict[str, tuple[dict, dict[str, bytes], dict]] = OrderedDict()


class ModelError(ValueError):
    """A catalog, input, or render error safe to show in the UI."""


class ManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Parameter(ManifestItem):
    id: str = Field(pattern=VARIABLE)
    label: str = Field(min_length=1, max_length=80)
    type: Literal["text", "integer", "number", "checkbox", "choice", "color"]
    group: Literal["production", "development"] = "production"
    default: str | int | float | bool
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    max_length: int = Field(default=50, ge=1, le=100)
    unit: str = Field(default="", max_length=16)
    choices: list[str] = Field(default_factory=list, max_length=20)

    def value(self, value):
        match self.type:
            case "text":
                valid = (isinstance(value, str) and 0 < len(value) <= self.max_length
                         and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value))
            case "choice":
                valid = isinstance(value, str) and value in self.choices
            case "checkbox":
                valid = type(value) is bool
            case "color":
                valid = isinstance(value, str) and bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value))
            case "number" | "integer":
                valid = (
                    (type(value) is int or (self.type == "number" and type(value) is float))
                    and -1_000_000 <= value <= 1_000_000 and math.isfinite(value)
                    and (self.minimum is None or value >= self.minimum)
                    and (self.maximum is None or value <= self.maximum)
                )
        if not valid:
            raise ModelError(f"{self.label} has an invalid value.")
        return value


class Part(ManifestItem):
    id: str = Field(pattern=SLUG)
    label: str = Field(min_length=1, max_length=80)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    color_parameter: str | None = Field(default=None, pattern=VARIABLE)
    upper_color_parameter: str | None = Field(default=None, pattern=VARIABLE, exclude_if=lambda value: value is None)
    color_change_at: str | None = Field(default=None, pattern=VARIABLE, exclude_if=lambda value: value is None)
    assembly: tuple[tuple[float, float, float, float], tuple[float, float, float, float],
                    tuple[float, float, float, float], tuple[float, float, float, float]] | None = None


class Design(ManifestItem):
    id: str = Field(pattern=SLUG)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=240)
    status: Literal["draft", "developed"] = "draft"
    source: str
    assets: list[str] = Field(default_factory=list, max_length=20)
    parameters: list[Parameter] = Field(max_length=20)
    parts: list[Part] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_design(self):
        ids = [p.id for p in self.parameters]
        if len(set(ids)) != len(ids) or "part" in ids:
            raise ValueError("Parameter IDs must be unique and part is reserved")
        if len({p.id for p in self.parts}) != len(self.parts):
            raise ValueError("Part IDs must be unique")
        color_ids = {p.id for p in self.parameters if p.type == "color"}
        height_ids = {p.id for p in self.parameters if p.type in ("number", "integer")}
        if any(part.color_parameter and part.color_parameter not in color_ids for part in self.parts):
            raise ValueError("A part's colour control must name a color parameter")
        for part in self.parts:
            if part.upper_color_parameter or part.color_change_at:
                if part.upper_color_parameter not in color_ids or part.color_change_at not in height_ids:
                    raise ValueError("A layer colour needs a color parameter and a numeric change height parameter")
        for param in self.parameters:
            param.value(param.default)
        return self


class Model(ManifestItem):
    schema_version: Literal[1]
    id: str = Field(pattern=SLUG)
    title: str = Field(min_length=1, max_length=80)
    adapter: Literal["generic", "organizer-v1"] = "generic"
    designs: list[Design] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_model(self):
        if len({d.id for d in self.designs}) != len(self.designs):
            raise ValueError("Design IDs must be unique")
        if self.adapter == "organizer-v1":
            for design in self.designs:
                if [p.id for p in design.parts] != ["base", "name"] or not any(
                    p.id == "text" and p.type == "text" for p in design.parameters
                ):
                    raise ValueError("Organizer designs need text and ordered base/name parts")
        return self


class PreviewRequest(ManifestItem):
    model_id: str = Field(pattern=SLUG)
    design_id: str = Field(pattern=SLUG)
    revision: str = Field(pattern=r"^[0-9a-f]{16}$")
    values: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ApprovalRequest(ManifestItem):
    model_id: str = Field(pattern=SLUG)
    design_id: str = Field(pattern=SLUG)
    action: Literal["promote", "reopen"]
    preview_key: str = Field(default="", pattern=r"^(?:[0-9a-f]{24})?$")


class PrintRequest(ManifestItem):
    key: str = Field(pattern=r"^[0-9a-f]{24}$")
    part: str = Field(pattern=SLUG)
    printer_id: int = Field(strict=True, gt=0)
    tray: int = Field(strict=True, ge=0)
    bed_clear: bool = Field(default=False, strict=True)
    force: bool = Field(default=False, strict=True)
    reprint_failed: bool = Field(default=False, strict=True)


def _read(root: Path, relative: str, limit: int) -> bytes:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ModelError("Model files must be inside their package.")
    path = root / path
    if not path.is_file() or path.resolve() != path:
        raise ModelError("A model file is missing or is a symbolic link.")
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise ModelError("A model file exceeds the preview size limit.")
    return data


def _model(root: Path) -> Model:
    try:
        manifest = Model.model_validate_json(_read(root, "model.json", 250_000))
    except (ValidationError, OSError) as exc:
        raise ModelError("Invalid model.json. Check its fields and parameter defaults.") from exc
    if root.name != manifest.id:
        raise ModelError("The package directory must match its model ID.")
    return manifest


def _selection(root: Path, design_id: str) -> tuple[Model, Design]:
    model = _model(root)
    design = next((item for item in model.designs if item.id == design_id), None)
    if design is None:
        raise ModelError("That design is unavailable. Refresh Models.")
    return model, design


def _snapshot(package: Path, design: Design) -> tuple[str, dict[str, bytes]]:
    digest = hashlib.sha256(design.model_dump_json().encode())
    files = {}
    if Path(design.source).suffix.lower() != ".scad":
        raise ModelError("The design entry point must be a .scad file.")
    for relative in dict.fromkeys([design.source, *design.assets]):
        data = _read(package, relative, 2_000_000)
        files[relative] = data
        digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()[:16], files


def _defaults(model: Model, design: Design, product: ModelParams | None, tuned: dict | None = None) -> dict:
    settings = {k.lower(): v for k, v in asdict(product).items()} if product and model.adapter == "organizer-v1" else {}
    values = {p.id: settings.get(p.id, p.default) for p in design.parameters}
    for p in design.parameters:
        if p.group == "development" and tuned and p.id in tuned:
            try:
                values[p.id] = p.value(tuned[p.id])
            except ModelError:
                if design.status == "developed":
                    raise
    return values


def _approval(model_id: str, design_id: str) -> dict:
    path = APPROVAL_ROOT / model_id / design_id / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ModelError("Could not read the model's local approval record.") from exc


def _effective(package: Path, design: Design) -> tuple[Path, Design, dict]:
    state = _approval(package.name, design.id)
    if state.get("stage") == "production":
        snapshot = APPROVAL_ROOT.resolve() / package.name / design.id / state["snapshot"]
        pinned = Design.model_validate_json(_read(snapshot, "design.json", 250_000))
        return snapshot, pinned, state.get("values", {})
    if state.get("stage") == "development":
        design = design.model_copy(update={"status": "draft"})
    return package, design, state.get("values", {})


def set_approval(request: ApprovalRequest) -> dict:
    package = CATALOG_ROOT.resolve() / request.model_id
    _, design = _selection(package, request.design_id)
    folder = APPROVAL_ROOT / request.model_id / request.design_id
    with APPROVAL_LOCK:
        state = _approval(request.model_id, request.design_id)
        if request.action == "reopen":
            _, effective, _ = _effective(package, design)
            if effective.status != "developed":
                raise ModelError("This design is already in development.")
            state["stage"] = "development"
        else:
            root, effective, _ = _effective(package, design)
            if effective.status == "developed":
                raise ModelError("This design is already in production.")
            revision, files = _snapshot(root, effective)
            with PREVIEW_LOCK:
                preview = PREVIEWS.get(request.preview_key)
            if not preview or preview[2]["identity"] != (request.model_id, request.design_id, revision):
                raise ModelError("Generate and review a current preview before moving to production.")
            tuned = {p.id: preview[2]["values"][p.id] for p in design.parameters if p.group == "development"}
            snapshot_id = request.preview_key
            snapshot = folder / snapshot_id
            snapshot.mkdir(parents=True, exist_ok=True)
            for relative, content in files.items():
                path = snapshot / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            pinned = effective.model_copy(update={"status": "developed"})
            (snapshot / "design.json").write_text(pinned.model_dump_json(), encoding="utf-8")
            state = {"stage": "production", "snapshot": snapshot_id, "values": tuned}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as file:
                json.dump(state, file)
            Path(file.name).replace(folder / "state.json")
        except OSError as exc:
            raise ModelError("Could not save the model's approval record.") from exc
    return {"stage": state["stage"]}


def catalog(product: ModelParams | None = None) -> dict:
    models, errors = [], []
    root = CATALOG_ROOT.resolve()
    for package in sorted(root.iterdir()) if root.is_dir() else ():
        if not re.fullmatch(SLUG, package.name) or package.is_symlink() or not package.is_dir():
            continue
        try:
            model = _model(package)
        except ModelError as exc:
            errors.append(f"{package.name}: {exc}")
            continue
        designs = []
        for design in model.designs:
            try:
                source, effective, tuned = _effective(package, design)
                revision, _ = _snapshot(source, effective)
                item = effective.model_dump(exclude={"source", "assets"})
                defaults = _defaults(model, effective, product, tuned)
                item["parameters"] = [p for p in item["parameters"]
                                      if effective.status != "developed" or p["group"] == "production"]
                for param in item["parameters"]:
                    param["default"] = defaults[param["id"]]
                designs.append(item | {"revision": revision, "adapter": model.adapter})
            except (ModelError, OSError, ValidationError) as exc:
                reason = str(exc) if isinstance(exc, ModelError) else "A model file could not be read."
                errors.append(f"{model.title} / {design.label}: {reason}")
        models.append({"id": model.id, "title": model.title, "designs": designs})
    installed = bool(platform_os.openscad_cli())
    problem = platform_os.openscad_launch_problem() if installed else None
    return {"models": models, "errors": errors, "renderer": installed and problem is None,
            "renderer_error": problem}


def _engine() -> tuple[str, str]:
    binary = platform_os.openscad_cli()
    if binary is None:
        raise ModelError("Install an OpenSCAD development build with textmetrics support, then refresh Models.")
    if problem := platform_os.openscad_launch_problem():
        raise ModelError(problem)
    code, version = platform_os.run_quiet([binary, "--version"], timeout=5)
    if code:
        raise ModelError("OpenSCAD could not start. Check the installation and launch environment.")
    return binary, version


def _export(binary: str, work: Path, design: Design, params: dict, part: Part):
    out = work / "preview.stl"
    out.unlink(missing_ok=True)
    cmd = [binary, "--hardwarnings", "--export-format", "binstl", "-o", str(out)]
    for key, value in (params | {"part": part.id}).items():
        cmd += ["-D", f"{key}={json.dumps(value, ensure_ascii=False)}"]
    code, _ = platform_os.run_quiet([*cmd, str(work / design.source)], timeout=RENDER_TIMEOUT, cwd=work,
                                    env={"OPENSCADPATH": str(work / "_libraries")})
    if code == 124:
        raise ModelError(f"{part.label} exceeded the OpenSCAD render time limit.")
    if code or not out.is_file() or out.stat().st_size > MAX_STL:
        raise ModelError(f"OpenSCAD could not export {part.label}. Check its source and parameters.")
    return _load_stl(out.read_bytes(), part.label)


def _load_stl(data: bytes, label: str):
    try:
        mesh = trimesh.load(io.BytesIO(data), file_type="stl", force="mesh")
        if (mesh.is_empty or not np.isfinite(mesh.vertices).all() or not mesh.is_volume
                or min(mesh.extents) <= 0 or np.max(np.abs(mesh.vertices)) > 10_000):
            raise ValueError("Invalid solid")
    except (ValueError, IndexError, TypeError, OverflowError) as exc:
        raise ModelError(f"{label} did not export as a closed, positive-volume solid.") from exc
    return mesh


def _plane(mesh, axis: int, offset: float):
    triangles = mesh.triangles
    faces = triangles[np.all(np.abs(triangles[:, :, axis] - offset) < 0.003, axis=1)]
    faces = np.delete(faces, axis, axis=2)
    joined = shapely.union_all([poly for face in faces if (poly := Polygon(face)).area > 1e-8])
    # Close sub-micron seams introduced when STL triangulation rounds points on a common edge.
    return joined.buffer(0.001).buffer(-0.001)


def _organizer(meshes, scaling: ScalingSettings, text: str, compartments: int):
    base, name = meshes
    x, z = name.bounds[0][:2] - base.bounds[0][[0, 2]]
    b, n, result = scale_pair(base, name, len(text), scaling)
    if result.fit_errors:
        raise ModelError("The name and base scales did not preserve their fit.")
    sx, sy, sz = result.base_scale
    x, z = x * sx, z * sz
    recess_depth = sy
    pocket = _plane(b, 1, recess_depth)
    name_face = _plane(n, 2, 0)
    seated = affinity.translate(name_face, x, z)
    floor = _plane(b, 2, 3 * sz)
    openings = sum(poly.area > 1 for poly in shapely.get_parts(floor))
    if seated.is_empty or not pocket.buffer(0.015).covers(seated) or openings != compartments:
        raise ModelError("The insert did not seat in the recess or the compartment count did not match.")
    clearance = seated.boundary.distance(pocket.boundary)
    if clearance < 0.12:
        raise ModelError("The insert has less than 0.12 mm clearance in part of the pocket.")
    matrix = [[1, 0, 0, float(x)], [0, 0, -1, recess_depth], [0, 1, 0, float(z)], [0, 0, 0, 1]]
    return [b, n], matrix, {
        "compartments": openings, "minimum_clearance": round(clearance, 3),
        "compartment_widths": [round(p.bounds[2] - p.bounds[0], 1) for p in
                               sorted((p for p in shapely.get_parts(floor) if p.area > 1),
                                      key=lambda p: p.bounds[0])],
        "recess_depth": round(recess_depth, 3),
        "checks": ["Scale proportions preserved", "Insert seats in the full recess profile",
                   "Compartment count matches the scaling band"],
    }, result


def preview(request: PreviewRequest, scaling: ScalingSettings, product: ModelParams | None = None) -> dict:
    package = CATALOG_ROOT.resolve() / request.model_id
    model, design = _selection(package, request.design_id)
    root, design, tuned = _effective(package, design)
    revision, files = _snapshot(root, design)
    if request.revision != revision:
        raise ModelError("The design changed on disk. Refresh Models and try again.")
    defaults = _defaults(model, design, product, tuned)
    editable = {p.id for p in design.parameters if design.status != "developed" or p.group == "production"}
    if request.values.keys() - editable:
        if request.values.keys() & (defaults.keys() - editable):
            raise ModelError("Development settings are locked in production. Move the model back to Development to edit them.")
        raise ModelError("Some inputs are no longer part of this design. Refresh Models.")
    values = {p.id: p.value(request.values.get(p.id, defaults[p.id])) for p in design.parameters}
    params = {p.id: values[p.id] for p in design.parameters if p.type != "color"}
    if model.adapter == "organizer-v1":
        if not re.fullmatch(r"[A-Za-z]{1,15}", values["text"]):
            raise ModelError("Use one line of 1–15 letters A–Z for this draft.")
        band = pick_band(len(values["text"]), scaling.bands)
        params.update(base_length=min(band.length, scaling.max_length), base_height=band.height,
                      compartments=band.compartments)
    if not RENDER_LOCK.acquire(blocking=False):
        raise ModelError("OpenSCAD is rendering another preview. Try again in a moment.")
    try:
        return _render(model, design, revision, files, values, params, scaling)
    finally:
        RENDER_LOCK.release()


def _render(model, design, revision, files, values, params, scaling):
    binary, engine = _engine()
    key_data = [model.id, design.id, revision, values, engine, asdict(scaling)]
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()[:24]
    with PREVIEW_LOCK:
        if key in PREVIEWS:
            PREVIEWS.move_to_end(key)
            return PREVIEWS[key][0]
    with TemporaryDirectory(prefix="snapstl-model-") as directory:
        work = Path(directory)
        for relative, data in files.items():
            path = work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if model.adapter == "organizer-v1":
            base_part, name_part = design.parts
            name = _export(binary, work, design, params, name_part)
            x, y = name.bounds[0][:2].tolist()
            width, height = dims(name)[:2]
            bounds = {"label_left": x, "label_bottom": y, "label_width": width, "label_height": height}
            meshes = [_export(binary, work, design, params | bounds, base_part), name]
        else:
            meshes = [_export(binary, work, design, params, part) for part in design.parts]

    checks = {"checks": ["Closed, positive-volume parts"]}
    transforms = [part.assembly for part in design.parts]
    fit = None
    if model.adapter == "organizer-v1":
        meshes, insert, checks, fit = _organizer(meshes, scaling, values["text"], params["compartments"])
        transforms = [np.eye(4).tolist(), insert]
    else:
        meshes = [to_origin(mesh) for mesh in meshes]

    plates, data = [], {}
    for part, mesh, transform in zip(design.parts, meshes, transforms, strict=True):
        data[part.id] = mesh.export(file_type="stl")
        if sum(map(len, data.values())) > MAX_JOB:
            raise ModelError("The preview exceeds the STL size limit.")
        actual = _load_stl(data[part.id], part.label)
        plate = {"id": part.id, "label": part.label,
                 "color": values.get(part.color_parameter, part.color), "assembly": transform,
                 "dimensions": list(dims(actual)), "url": f"/api/model-studio/previews/{key}/{part.id}.stl"}
        if part.upper_color_parameter:
            height = values[part.color_change_at]
            if not 0 < height < actual.extents[2]:
                raise ModelError("The filament change height must be inside the preview plate.")
            plate.update(upper_color=values[part.upper_color_parameter], color_change_at=height)
        plates.append(plate)
    if fit and fit_problems(fit_metrics(fit.base_orig, fit.name_orig, plates[0]["dimensions"], plates[1]["dimensions"])):
        raise ModelError("The exported preview files did not preserve the name/base proportions.")
    result = {"key": key, "revision": revision, "engine": engine, "plates": plates, **checks}
    with PREVIEW_LOCK:
        PREVIEWS[key] = (result, data, {"identity": (model.id, design.id, revision), "values": values})
        while len(PREVIEWS) > 8:
            PREVIEWS.popitem(last=False)
    return result


def artifact(key: str, part: str) -> bytes | None:
    with PREVIEW_LOCK:
        entry = PREVIEWS.get(key)
        return entry[1].get(part) if entry else None


def print_file(key: str, part: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{24}", key) or not re.fullmatch(SLUG, part):
        raise ModelError("Choose a valid preview plate.")
    return f"{key}/{part}.3mf"


def write_print_plate(key: str, part: str, cfg: PlateSettings, root: Path) -> str:
    from . import export, packing, threemf

    relative = print_file(key, part)
    path = root / relative
    if path.is_file():
        return relative  # a held preview can be resumed even after SnapSTL restarted
    with PREVIEW_LOCK:
        preview = PREVIEWS.get(key)
        data = preview[1].get(part) if preview else None
        plate = next((plate for plate in preview[0]["plates"] if plate["id"] == part), None) if preview else None
    if data is None:
        raise ModelError("This preview expired. Generate it again before printing.")
    if plate and "upper_color" in plate and plate["upper_color"].lower() != plate["color"].lower():
        raise ModelError("Download the STL and set the filament change in the slicer for a two-colour print.")
    mesh = _load_stl(data, part)
    packed_part = packing.make_part(part, mesh)
    layout = packing.pack([packed_part], cfg)
    if not layout.ok:
        raise ModelError(layout.reason)
    _, placed = packing.placed_part_meshes([packed_part], layout)[0]
    # Put the single part the configured margin from the origin. The selected
    # printer's Bambuddy profile validates its own bed during slicing.
    placed.apply_translation((cfg.margin - placed.bounds[0][0], cfg.margin - placed.bounds[0][1], 0))
    objects = [threemf.Object3MF(f"Model – {part}", placed.vertices, placed.faces)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as folder:
        staged = Path(folder) / path.name
        threemf.write_3mf(staged, objects)
        problems, _ = export.check_written(staged, objects, cfg)
        if problems:
            raise ModelError("The print plate failed verification. " + "; ".join(problems))
        staged.replace(path)
    return relative
