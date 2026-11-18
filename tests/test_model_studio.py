from __future__ import annotations

import io
import json
import os

import pytest
import trimesh
from fastapi.testclient import TestClient

from snapstl import model_studio, platform_os, server
from snapstl.settings import FONTS, ModelParams, PlateSettings, ScalingSettings


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(model_studio, "CATALOG_ROOT", tmp_path)
    monkeypatch.setattr(model_studio, "PREVIEWS", model_studio.OrderedDict())
    root = tmp_path / "fixture"
    root.mkdir()
    (root / "main.scad").write_text("cube(5);")
    (root / "model.json").write_text(json.dumps({
        "schema_version": 1, "id": "fixture", "title": "Fixture",
        "designs": [{
            "id": "draft", "label": "Draft", "source": "main.scad",
            "parameters": [{"id": "width", "label": "Width", "type": "integer", "default": 5,
                            "minimum": 2, "maximum": 8}],
            "parts": [{"id": "body", "label": "Body", "color": "#abcdef"}],
        }],
    }))
    return root


def test_catalog_reloads_and_rejects_missing_sources(package):
    revision = model_studio.catalog()["models"][0]["designs"][0]["revision"]
    source = package / "main.scad"
    source.write_text("cube(7);")
    assert model_studio.catalog()["models"][0]["designs"][0]["revision"] != revision
    source.unlink()
    catalog = model_studio.catalog()
    assert catalog["models"][0]["designs"] == [] and "missing" in catalog["errors"][0]


def test_apple_silicon_preflight_blocks_renderer_when_cpu_probe_is_denied(package, monkeypatch):
    monkeypatch.setattr(platform_os, "MAC", True)
    monkeypatch.setattr(platform_os.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform_os, "openscad_cli", lambda: "/test/OpenSCAD")

    def run_quiet(command, **kwargs):
        assert command == ["/usr/sbin/sysctl", "-n", "hw.optional.neon"]
        return 1, "Operation not permitted"

    monkeypatch.setattr(platform_os, "run_quiet", run_quiet)
    platform_os.openscad_launch_problem.cache_clear()
    try:
        catalog = model_studio.catalog()
        assert not catalog["renderer"] and "CPU feature detection" in catalog["renderer_error"]
        with pytest.raises(model_studio.ModelError, match="CPU feature detection"):
            model_studio._engine()
    finally:
        platform_os.openscad_launch_problem.cache_clear()


def test_preview_serves_validated_stl_and_rejects_stale_requests(package, monkeypatch):
    def export(_binary, _work, _design, params, _part):
        return trimesh.creation.box(extents=(params["width"], 3, 2))

    monkeypatch.setattr(model_studio, "_engine", lambda: ("fixture", "fixture-version"))
    monkeypatch.setattr(model_studio, "_export", export)
    client = TestClient(server.app, base_url="http://127.0.0.1:8317", client=("127.0.0.1", 50000))
    model, = client.get("/api/model-studio/catalog").json()["models"]
    design, = model["designs"]
    body = {"model_id": model["id"], "design_id": design["id"], "revision": design["revision"],
            "values": {"width": 7}}
    response = client.post("/api/model-studio/preview", json=body)
    assert response.status_code == 200
    plate, = response.json()["plates"]
    artifact = client.get(plate["url"])
    assert artifact.status_code == 200
    assert artifact.headers["cache-control"] == "private, no-store"
    mesh = trimesh.load(io.BytesIO(artifact.content), file_type="stl", force="mesh")
    assert tuple(mesh.extents) == tuple(plate["dimensions"]) == (7, 3, 2)
    assert client.post("/api/model-studio/preview", json=body | {"values": {"width": 9}}).status_code == 400
    (package / "main.scad").write_text("cube(6);")
    stale = client.post("/api/model-studio/preview", json=body)
    assert stale.status_code == 400 and "changed on disk" in stale.json()["detail"]
    assert client.get("/api/model-studio/catalog", headers={"Origin": "https://example.com"}).status_code == 403


def test_organizer_uses_product_font_options_and_scales_the_same_pair(monkeypatch):
    model = next(model for model in model_studio.catalog(ModelParams(Font="DynaPuff", Thickness=65))["models"]
                 if model["id"] == "name-desk-organizer")
    design, = model["designs"]
    controls = {p["id"]: p for p in design["parameters"]}
    assert design["id"] == "letter-contour"
    assert controls["font"]["choices"] == FONTS
    assert controls["font"]["default"] == "DynaPuff" and controls["thickness"]["default"] == 65
    angle = controls["rotation_angle"]
    assert (angle["default"], angle["minimum"], angle["maximum"]) == (6, -30, 30)
    observed = {}

    def export(_binary, _work, _design, params, part):
        observed[part.id] = params
        if part.id == "name":
            mesh = trimesh.creation.box(extents=(101, 41, 4))
            mesh.apply_translation((100.5, 30.5, 2))
            return mesh
        return trimesh.creation.box(extents=(140, 65, 66))

    def organizer(meshes, scaling, text, compartments):
        assert text == "AaLiQ" and compartments == 5
        b, n, result = model_studio.scale_pair(*meshes, len(text), scaling)
        return [b, n], None, {"compartments": compartments}, result

    monkeypatch.setattr(model_studio, "_engine", lambda: ("fixture", "fake-organizer"))
    monkeypatch.setattr(model_studio, "_export", export)
    monkeypatch.setattr(model_studio, "_organizer", organizer)
    request = model_studio.PreviewRequest(model_id=model["id"], design_id=design["id"], revision=design["revision"])
    result = model_studio.preview(request, ScalingSettings(), ModelParams(Font="DynaPuff", Thickness=65))
    assert observed["name"]["font"] == "DynaPuff"
    assert observed["base"]["thickness"] == 65
    assert observed["base"]["label_left"] == 50 and observed["base"]["label_width"] == 101
    assert result["plates"][0]["dimensions"] == [210, 65, 89]
    assert result["plates"][1]["dimensions"][2] == 2
    rotated = request.model_copy(update={"values": {"rotate_text": "yes", "rotation_angle": -12.5}})
    model_studio.preview(rotated, ScalingSettings(), ModelParams(Font="DynaPuff", Thickness=65))
    assert all(params["rotation_angle"] == -12.5 and params["rotate_text"] == "yes"
               for params in observed.values())
    invalid = request.model_copy(update={"values": {"rotate_text": "yes", "rotation_angle": 31}})
    with pytest.raises(model_studio.ModelError, match="Rotation angle"):
        model_studio.preview(invalid, ScalingSettings())


def test_approval_pins_geometry_and_tolerance_until_reopened(package, tmp_path, monkeypatch):
    monkeypatch.setattr(model_studio, "APPROVAL_ROOT", tmp_path / "approvals")
    manifest = package / "model.json"
    model = json.loads(manifest.read_text())
    model["designs"][0]["parameters"].append({
        "id": "tolerance", "label": "Tolerance", "type": "number", "group": "development",
        "default": 0.3, "minimum": 0.2, "maximum": 0.6,
    })
    manifest.write_text(json.dumps(model))
    monkeypatch.setattr(model_studio, "_engine", lambda: ("fixture", "fixture-version"))

    def export(_binary, work, design, params, _part):
        height = int((work / design.source).read_text().split("(")[1].split(")")[0])
        return trimesh.creation.box(extents=(params["width"], height, params["tolerance"]))

    monkeypatch.setattr(model_studio, "_export", export)
    client = TestClient(server.app, base_url="http://127.0.0.1:8317", client=("127.0.0.1", 50000))
    path = "/api/model-studio"

    def design():
        return client.get(path + "/catalog").json()["models"][0]["designs"][0]

    identity = {"model_id": "fixture", "design_id": "draft"}
    body = identity | {"revision": design()["revision"], "values": {"tolerance": 0.5}}
    preview = client.post(path + "/preview", json=body).json()
    reviewed = identity | {"preview_key": preview["key"], "action": "promote"}
    assert client.post(path + "/approval", json=reviewed).json()["stage"] == "production"
    (package / "main.scad").write_text("cube(6);")
    assert design()["status"] == "developed"
    assert [p["id"] for p in design()["parameters"]] == ["width"]
    pinned = identity | {"revision": design()["revision"], "values": {"width": 6}}
    assert client.post(path + "/preview", json=pinned).json()["plates"][0]["dimensions"] == [6, 5, 0.5]
    invalid = pinned | {"values": {"tolerance": 0.2}}
    assert "locked" in client.post(path + "/preview", json=invalid).json()["detail"]
    assert client.post(path + "/approval", json=identity | {"action": "reopen"}).json()["stage"] == "development"
    open_design = design()
    assert open_design["status"] == "draft" and open_design["parameters"][1]["default"] == 0.5
    reopened = client.post(path + "/preview", json=identity | {"revision": open_design["revision"]}).json()
    assert reopened["plates"][0]["dimensions"] == [5, 6, 0.5]


def test_aaliq_rim_matches_reference_height_when_real_cad_is_enabled():
    if os.environ.get("SNAPSTL_TEST_OPENSCAD") != "1":
        pytest.skip("Enable SNAPSTL_TEST_OPENSCAD=1 to check the real OpenSCAD draft")
    model = next(model for model in model_studio.catalog()["models"] if model["id"] == "name-desk-organizer")
    design, = model["designs"]
    values = {"text": "AaLiq"}
    req = model_studio.PreviewRequest(model_id=model["id"], design_id=design["id"],
                                      revision=design["revision"], values=values)
    result = model_studio.preview(req, ScalingSettings())
    assert result["compartment_widths"] == [35.6] * 5
    base = model_studio._load_stl(model_studio.artifact(result["key"], "base"), "Base")
    section = trimesh.intersections.mesh_plane(base, [0, 1, 0], [0, 49, 0])
    x = 126.5  # the flat low section between L and i on the downloaded reference
    top = max(a[2] + (x - a[0]) * (b[2] - a[2]) / (b[0] - a[0])
              for a, b in section if min(a[0], b[0]) < x < max(a[0], b[0]))
    assert top == pytest.approx(58.74, abs=0.04)


def test_bookmark_catalog_styles_share_customization_controls():
    model = next(model for model in model_studio.catalog()["models"] if model["id"] == "personalized-bookmark")
    designs = model["designs"]
    assert [design["id"] for design in designs] == ["classic-round", "ribbon-tail", "open-arch", "faceted-tab"]
    assert all(design["parameters"] == designs[0]["parameters"] for design in designs)
    for design in designs:
        plate, = design["parts"]
        assert plate["id"] == "bookmark"


@pytest.mark.parametrize("design_id", ["classic-round", "ribbon-tail", "open-arch", "faceted-tab"])
def test_bookmark_exports_flat_single_solid_with_a_layer_colour_boundary(design_id, tmp_path):
    if os.environ.get("SNAPSTL_TEST_OPENSCAD") != "1":
        pytest.skip("Enable SNAPSTL_TEST_OPENSCAD=1 to check the real OpenSCAD draft")
    model = next(model for model in model_studio.catalog()["models"] if model["id"] == "personalized-bookmark")
    design = next(design for design in model["designs"] if design["id"] == design_id)
    cases = [
        ({}, [28, 120, 1.6], 1.0),
        ({"text": "Élodie, just one more page!", "length": 85, "width": 22, "base_thickness": 0.8,
          "letter_height": 12, "letter_relief": 0.4}, [22, 85, 1.2], 0.8),
        ({"text": "i", "length": 160, "width": 40, "base_thickness": 1.6,
          "letter_height": 12, "letter_relief": 0.8, "base_color": "#877802",
          "letter_color": "#897700"}, [40, 160, 2.4], 1.6),
    ]
    for values, dimensions, boundary in cases:
        request = model_studio.PreviewRequest(model_id=model["id"], design_id=design_id,
                                              revision=design["revision"], values=values)
        result = model_studio.preview(request, ScalingSettings())
        plate, = result["plates"]
        assert plate["dimensions"] == pytest.approx(dimensions)
        assert plate["color"] == values.get("base_color", "#305460")
        assert plate["upper_color"] == values.get("letter_color", "#ffe4b0")
        assert plate["color_change_at"] == boundary
        with pytest.raises(model_studio.ModelError, match="set the filament change"):
            model_studio.write_print_plate(result["key"], "bookmark", PlateSettings(), tmp_path)
        mesh = model_studio._load_stl(model_studio.artifact(result["key"], "bookmark"), "Bookmark")
        neighbours = [[] for _ in mesh.faces]
        for a, b in mesh.face_adjacency:
            neighbours[a].append(b)
            neighbours[b].append(a)
        seen, pending = {0}, [0]
        while pending:
            for face in neighbours[pending.pop()]:
                if face not in seen:
                    seen.add(face)
                    pending.append(face)
        assert len(seen) == len(mesh.faces)

        assert mesh.bounds[0] == pytest.approx([0, 0, 0])
        assert mesh.is_winding_consistent
        bed = model_studio._plane(mesh, 2, 0)
        base_face = model_studio._plane(mesh, 2, boundary)
        letters = model_studio._plane(mesh, 2, dimensions[2])
        assert len(bed.interiors) == (1 if design_id == "open-arch" else 0)
        assert letters.area > 1 and bed.contains(letters)
        assert base_face.intersection(letters).area < 0.01
        assert bed.symmetric_difference(base_face.union(letters)).area < 0.01
        assert all(z == pytest.approx(boundary) or z == pytest.approx(dimensions[2])
                   for z in mesh.vertices[:, 2] if z > 0.01)

    matching = request.model_copy(update={"values": {"base_color": "#FFE4B0"}})
    result = model_studio.preview(matching, ScalingSettings())
    assert model_studio.write_print_plate(result["key"], "bookmark", PlateSettings(), tmp_path)
