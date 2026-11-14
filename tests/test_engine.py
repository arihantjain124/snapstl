from pathlib import Path

import pytest
import trimesh

from snapstl.export import plate_filename
from snapstl.grouping import group_bases, group_names
from snapstl.orders import Order, parse_orders
from snapstl.packing import make_part, pack, placed_footprint
from snapstl.pipeline import build
from snapstl.scaling import scale_pair
from snapstl.settings import PlateSettings, ScalingSettings, Settings

from .conftest import base_mesh, name_mesh


def test_parse_orders_header_and_normalise():
    orders, warns = parse_orders("﻿Name,Color\nAyra, Pink \n,blue\nManvi,pink\n")
    assert [(o.name, o.color) for o in orders] == [("Ayra", "pink"), ("Manvi", "pink")]
    assert any("empty name" in w for w in warns)


def test_parse_orders_no_header():
    orders, _ = parse_orders("Ayra,pink\nKabir,blue\n")
    assert [o.name for o in orders] == ["Ayra", "Kabir"]


def test_scaling_rules_v11():
    base, name = base_mesh(), name_mesh(4)
    b, n, r = scale_pair(base, name, 4, ScalingSettings())
    bx, by, bz = b.extents
    assert bx == pytest.approx(200, abs=0.01)
    assert by == pytest.approx(51, abs=0.01)  # Base Y 100 %
    assert bz == pytest.approx(90, abs=0.01)
    sx, sy, sz = r.base_scale
    assert r.name_scale == pytest.approx((sx, sz, 0.5))  # Name X=Base X, Name Y=Base Z, Name Z=50 %
    assert n.extents[2] == pytest.approx(2.0, abs=0.01)  # 4 mm → 2 mm
    assert b.bounds[0][2] == pytest.approx(0) and n.bounds[0][2] == pytest.approx(0)


def test_long_name_capped_at_240():
    _, _, r = scale_pair(base_mesh(length=600), name_mesh(12), 12, ScalingSettings())
    assert r.base_final[0] == pytest.approx(240, abs=0.01)
    assert 85 <= r.base_final[2] <= 88


def test_three_names_pack_without_collision():
    cfg = PlateSettings()
    parts = []
    for i, k in enumerate([4, 5, 9]):
        _, n, _ = scale_pair(base_mesh(length=48 * k + 10), name_mesh(k), k, ScalingSettings())
        parts.append(make_part(f"n{i}", n))
    res = pack(parts, cfg)
    assert res.ok, res.reason
    fps = [placed_footprint(p, pl) for p in parts for pl in res.placements if pl.key == p.key]
    for i in range(3):
        for j in range(i + 1, 3):
            assert fps[i].distance(fps[j]) >= cfg.gap - 0.05
    for fp in fps:
        x0, y0, x1, y1 = fp.bounds
        assert x0 >= cfg.margin - 0.01 and y0 >= cfg.margin - 0.01
        assert x1 <= cfg.bed_x - cfg.margin + 0.01 and y1 <= cfg.bed_y - cfg.margin + 0.01


def test_nesting_beats_bounding_boxes():
    """Bumpy names flipped 180° should nest tighter than bbox stacking."""
    cfg = PlateSettings(gap=3)
    parts = [make_part(f"n{i}", name_mesh(4, h=82)) for i in range(3)]
    bbox_depth = 3 * 82 + 2 * 3  # 252 > usable 246: only fits if the bumps nest
    res = pack(parts, cfg)
    assert res.ok, res.reason
    assert res.extent[1] < bbox_depth - 5


def test_too_big_drops_to_two():
    cfg = PlateSettings()
    huge = [make_part(f"n{i}", name_mesh(4, h=110)) for i in range(3)]
    assert not pack(huge, cfg).ok
    assert pack(huge[:2], cfg).ok


def _orders(spec):
    return [Order(i, n, c) for i, (n, c) in enumerate(spec)]


def test_grouping_by_color(meshes_for):
    spec = [("Ayra", "pink"), ("Kabir", "blue"), ("Manvi", "pink"), ("Riya", "pink"),
            ("Zoya", "blue"), ("Tara", "green"), ("Isha", "pink")]
    orders = _orders(spec)
    meshes = meshes_for([n for n, _ in spec])
    sc = ScalingSettings()
    bparts, nparts = {}, {}
    for o in orders:
        b, n, _ = scale_pair(*meshes[o.idx], o.letters, sc)
        bparts[o.idx] = make_part(o.name + " - Base", b)
        nparts[o.idx] = make_part(o.name + " - Name", n)
    cfg = PlateSettings()

    nplates, errs = group_names(orders, nparts, cfg)
    assert not errs
    assert all(len(set(p.colors)) == 1 for p in nplates)
    by_color = {}
    for p in nplates:
        by_color.setdefault(p.colors[0], []).append(len(p.orders))
    assert by_color == {"pink": [3, 1], "blue": [2], "green": [1]}

    bplates, errs = group_bases(orders, bparts, cfg)
    assert not errs
    sizes = [len(p.orders) for p in bplates]
    assert sizes == [3, 3, 1]
    assert set(bplates[0].colors) == {"pink"}  # full colour group first
    assert sum(sizes) == len(orders)
    names = [plate_filename(p) for p in bplates]
    assert names[0] == "{Ayra,pink},{Manvi,pink},{Riya,pink}.stl"
    assert names[1] == "{Kabir,blue},{Zoya,blue},{Tara,green}.stl"
    assert plate_filename(nplates[0]) == "{Ayra,pink},{Manvi,pink},{Riya,pink}.stl"


def test_build_writes_valid_files(tmp_path: Path, meshes_for):
    spec = [("Ayra", "pink"), ("Manvi", "pink"), ("Aishwarya", "pink"), ("Kabir", "blue")]
    orders = _orders(spec)
    res = build(orders, meshes_for([n for n, _ in spec]), Settings(), tmp_path)
    assert res.ok, res.errors
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.stl"))
    assert files == [
        "Bases/{Ayra,pink},{Manvi,pink},{Aishwarya,pink}.stl",
        "Bases/{Kabir,blue}.stl",
        "Names/blue/{Kabir,blue}.stl",
        "Names/pink/{Ayra,pink},{Manvi,pink},{Aishwarya,pink}.stl",
    ]
    m = trimesh.load(tmp_path / files[2], force="mesh")
    lo, hi = m.bounds
    assert lo[0] >= 5 - 0.01 and hi[0] <= 251.01 and lo[2] == pytest.approx(0, abs=1e-3)
    assert (tmp_path / "report.csv").exists()


def test_duplicate_names_and_copies(tmp_path: Path, meshes_for):
    spec = [("Anaya", "pink"), ("Anaya", "pink"), ("Anaya", "pink"), ("Anaya", "blue")]
    orders = [Order(i, n, c, order_id=f"O{i // 2}") for i, (n, c) in enumerate(spec)]
    res = build(orders, meshes_for([n for n, _ in spec]), Settings(), tmp_path)
    assert res.ok, res.errors
    assert all(res.complete(o.idx) for o in orders)
    names = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.stl"))
    assert "Names/pink/{Anaya,pink},{Anaya,pink},{Anaya,pink}.stl" in names


def test_batch_folders_number_per_day(tmp_path, monkeypatch):
    import datetime as dt

    from snapstl import pipeline

    class D(dt.datetime):
        day = "2026-11-15"

        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(cls.day + "T10:00:00")

    monkeypatch.setattr(pipeline, "datetime", D)
    a = pipeline.new_batch_dir(tmp_path)
    b = pipeline.new_batch_dir(tmp_path)
    D.day = "2026-11-16"
    c = pipeline.new_batch_dir(tmp_path)
    rel = [p.relative_to(tmp_path).as_posix() for p in (a, b, c)]
    assert rel == ["2026-11-15/Batch 1 - 2026-11-15", "2026-11-15/Batch 2 - 2026-11-15", "2026-11-16/Batch 1 - 2026-11-16"]
