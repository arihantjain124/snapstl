import os
import tempfile

# Isolate every test run from the real SnapSTL data folder (must happen before snapstl is imported).
os.environ["SNAPSTL_HOME"] = tempfile.mkdtemp(prefix="snapstl-test-")
os.environ["SNAPSTL_SECURE_COOKIE"] = "0"  # test client talks plain HTTP
# Never the real shared database from .env: SQLite, or a throwaway Postgres given in SNAPSTL_TEST_DATABASE_URL
# (its `snapstl` schema is dropped first).
os.environ["SNAPSTL_DATABASE_URL"] = os.environ.get("SNAPSTL_TEST_DATABASE_URL", "")
os.environ["SNAPSTL_COMPUTER_NAME"] = "Test computer"
if os.environ["SNAPSTL_DATABASE_URL"]:
    import psycopg

    with psycopg.connect(os.environ["SNAPSTL_DATABASE_URL"], autocommit=True) as _c:
        _c.execute("DROP SCHEMA IF EXISTS snapstl CASCADE")

import numpy as np
import pytest
import trimesh
from shapely.geometry import Point, box
from shapely.ops import unary_union


def name_mesh(letters: int, letter_w: float = 45.0, h: float = 63.7, t: float = 4.0) -> trimesh.Trimesh:
    """Flat 'name' piece: bumpy outline (one blob per letter) on a strip, lying on Z=0."""
    blobs = []
    for i in range(letters):
        cx = letter_w * (i + 0.5)
        r = h / 2 if i % 2 == 0 else h / 2.6
        blobs.append(Point(cx, h / 2).buffer(r, 32))
    strip = box(0, h * 0.25, letter_w * letters, h * 0.75)
    shape = unary_union(blobs + [strip])
    return trimesh.creation.extrude_polygon(shape, t)


def base_mesh(length: float = 193.0, depth: float = 51.0, height: float = 75.7) -> trimesh.Trimesh:
    """Upright organizer base: a box with a raised back so it's not a plain cuboid."""
    body = trimesh.creation.box([length, depth * 0.8, height * 0.6])
    body.apply_translation([length / 2, depth * 0.4, height * 0.3])
    back = trimesh.creation.box([length * 0.9, depth * 0.2, height])
    back.apply_translation([length / 2, depth * 0.9, height / 2])
    return trimesh.util.concatenate([body, back])


@pytest.fixture
def meshes_for():
    def make(names):
        out = {}
        for i, n in enumerate(names):
            k = sum(c.isalpha() for c in n)
            out[i] = (base_mesh(length=48.0 * max(k, 3) + 10), name_mesh(max(k, 1)))
        return out

    return make
