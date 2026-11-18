from __future__ import annotations

import io
import json

import pytest
import trimesh
from fastapi.testclient import TestClient

from snapstl import model_studio, server
from snapstl.settings import FONTS, ModelParams, ScalingSettings


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
    model, = model_studio.catalog(ModelParams(Font="DynaPuff", Thickness=65))["models"]
    design, = model["designs"]
    controls = {p["id"]: p for p in design["parameters"]}
    assert design["id"] == "letter-contour"
    assert controls["font"]["choices"] == FONTS
    assert controls["font"]["default"] == "DynaPuff" and controls["thickness"]["default"] == 65
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
