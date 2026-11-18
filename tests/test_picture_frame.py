import math
import os

import numpy as np
import pytest
import shapely
import trimesh
from shapely.geometry import LineString, Point, box

from snapstl import model_studio as studio
from snapstl.settings import ScalingSettings

FONT_NAMES = ["Chewy", "Luckiest Guy", "Titan One", "DynaPuff", "Cherry Bomb One", "Akaya Kanadaka"]


@pytest.fixture
def frame_design(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, "APPROVAL_ROOT", tmp_path)
    model = next(m for m in studio.catalog()["models"] if m["id"] == "picture-frame")
    return model["designs"][0]


def test_frame_catalog(frame_design):
    assert frame_design["id"] == "folding-easel"
    assert frame_design["status"] == "draft"
    assert [p["id"] for p in frame_design["parts"]] == ["frame", "backing", "stand"]
    assert all(p["assembly"] for p in frame_design["parts"])
    fields = {p["id"]: p for p in frame_design["parameters"]}
    assert fields["font"]["choices"] == FONT_NAMES
    assert fields["text_position"]["choices"] == ["Bottom", "Top"]


def preview(design, **values):
    if os.environ.get("SNAPSTL_TEST_OPENSCAD") != "1":
        pytest.skip("Enable SNAPSTL_TEST_OPENSCAD=1 to render and check picture frame geometry")
    request = studio.PreviewRequest(model_id="picture-frame", design_id=design["id"],
                                    revision=design["revision"], values=values)
    result = studio.preview(request, ScalingSettings())
    parts = {}
    for part in result["plates"]:
        mesh = studio._load_stl(studio.artifact(result["key"], part["id"]), part["label"])
        assert mesh.is_volume
        neighbors = [[] for _ in mesh.faces]
        for left, right in mesh.face_adjacency:
            neighbors[left].append(right)
            neighbors[right].append(left)
        visited = {0}
        pending = [0]
        while pending:
            for neighbor in neighbors[pending.pop()]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        assert len(visited) == len(mesh.faces)
        assert mesh.bounds[0] == pytest.approx([0, 0, 0])
        parts[part["id"]] = (mesh, np.array(part["assembly"]))
    return parts


def section(mesh, axis, offset):
    origin = np.zeros(3)
    origin[axis] = offset
    lines = trimesh.intersections.mesh_plane(mesh, np.eye(3)[axis], origin)
    lines = np.delete(lines, axis, axis=2).round(4)
    # Polygonize returns nested rings both as holes and as standalone polygons.
    # Keep only the exterior of each face, then toggle each nested ring once.
    faces = shapely.get_parts(shapely.polygonize([LineString(line) for line in lines if
                                                not np.array_equal(*line)]))
    filled = shapely.Polygon()
    for face in faces:
        filled = filled.symmetric_difference(shapely.Polygon(face.exterior))
    return filled


def test_frame_seat_and_folding_stand(frame_design):
    parts = preview(frame_design)
    frame, _ = parts["frame"]
    backing, backing_transform = parts["backing"]
    stand, open_transform = parts["stand"]

    assert frame.extents == pytest.approx([135.6, 186.4, 7.65])
    assert backing.extents == pytest.approx([131.6, 178.4, 13.4])
    photo = box(17, 17, 118.6, 169.4)
    seat = section(frame, 2, 0.4)
    window = section(frame, 2, 6)
    assert seat.distance(photo) == pytest.approx(0.3, abs=0.002)
    assert window.intersection(photo).area == pytest.approx(101.6 * 152.4 - 94.6 * 145.4, abs=0.02)

    # Each backing through-hole sits concentrically over a pilot hole in the frame.
    frame_rear = section(frame, 2, 1)
    cover_face = section(backing.copy().apply_transform(backing_transform), 2, -1)
    for x in (7, 128.6):
        for y in (7.5, 178.9):
            assert frame_rear.distance(Point(x, y)) == pytest.approx(1.325, abs=0.003)
            assert cover_face.distance(Point(x, y)) == pytest.approx(1.7, abs=0.003)

    center_x = 67.8
    pivot = np.array([center_x, 116, -9])
    cover_world = backing.copy().apply_transform(backing_transform)
    stand_open = stand.copy().apply_transform(open_transform)
    pin = Point(*pivot[1:]).buffer(1.5)
    assert section(stand_open, 0, center_x).distance(pin) == pytest.approx(0.2, abs=0.004)
    for x in [51, 84]:
        assert section(cover_world, 0, x).distance(pin) == pytest.approx(0.2, abs=0.004)
    ear_x = cover_world.vertices[cover_world.vertices[:, 2] < -3.1, 0]
    assert stand_open.bounds[0, 0] - max(ear_x[ear_x < center_x]) == pytest.approx(0.35, abs=0.001)
    assert min(ear_x[ear_x > center_x]) - stand_open.bounds[1, 0] == pytest.approx(0.35, abs=0.001)
    # The stand's full width sweeps between the two ears. Away from the ears
    # the central backing has a constant cross-section throughout that span.
    cover_middle = section(cover_world, 0, center_x)
    for swing in range(31):
        rotate = trimesh.transformations.rotation_matrix(math.radians(swing - 30), [1, 0, 0], pivot)
        current = stand_open.copy().apply_transform(rotate)
        profile = section(current, 0, center_x)
        assert profile.intersection(cover_middle).area < 0.001
        if swing == 0:
            assert 0 < current.bounds[0, 1] < current.bounds[1, 1] < frame.bounds[1, 1]
            assert profile.distance(cover_middle) == pytest.approx(1, abs=0.002)
        if swing == 30:
            assert profile.distance(cover_middle) < 0.002
    past_stop = stand_open.copy().apply_transform(
        trimesh.transformations.rotation_matrix(math.radians(1), [1, 0, 0], pivot))
    assert section(past_stop, 0, center_x).intersection(cover_middle).area > 1e-5

    # The rear bottom edge and the angled foot meet the same desk plane.
    lean = math.radians(17)
    up = np.array([0, math.cos(lean), math.sin(lean)])
    assert min(np.min(mesh.vertices @ up) for mesh in [frame, cover_world]) == pytest.approx(0, abs=0.001)
    heights = stand_open.vertices @ up
    assert heights.min() == pytest.approx(0, abs=0.001)
    assert sum(abs(heights) < 0.001) >= 4
    forward = np.array([0, -math.sin(lean), math.cos(lean)])
    meshes = [frame, cover_world, stand_open]
    center = np.average([mesh.center_mass for mesh in meshes], weights=[mesh.volume for mesh in meshes], axis=0)
    assert min(stand_open.vertices[abs(heights) < 0.001] @ forward) < center @ forward < 0


@pytest.mark.parametrize("font", FONT_NAMES)
@pytest.mark.parametrize("position", ["Bottom", "Top"])
def test_frame_lettering_stays_on_selected_border(frame_design, font, position):
    parts = preview(frame_design, text="Agjpq Good Times 2026!!!", font=font, text_position=position,
                    max_letter_height=11, photo_clearance=0.7, pin_clearance=0.45)
    frame, _ = parts["frame"]
    ink = frame.vertices[np.isclose(frame.vertices[:, 2], 7.65)]
    assert np.ptp(ink[:, 0]) <= 115.68 and np.ptp(ink[:, 1]) <= 11.08
    center_y = 9 if position == "Bottom" else 186.4 - 9
    assert (ink[:, 1].min() + ink[:, 1].max()) / 2 == pytest.approx(center_y, abs=0.08)
    assert (ink[:, 0].min() + ink[:, 0].max()) / 2 == pytest.approx(67.8, abs=0.08)
    unlettered = section(frame, 2, 6)
    assert unlettered.buffer(0.001).covers(studio._plane(frame, 2, 7.65))


def test_frame_can_be_unlettered(frame_design):
    frame, _ = preview(frame_design, personalize=False, photo_clearance=0.2, pin_clearance=0.15)["frame"]
    assert frame.extents == pytest.approx([135.6, 186.4, 7])
