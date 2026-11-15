"""On-disk cache of unscaled meshes downloaded from MakerWorld.

Layout: APP_DIR/cache/<key>/{base.stl, name.stl, meta.json}. STL is only the cache's internal
format (exact, simple); plate output is .3mf (export.py).

Key = hash of everything that changes the geometry (name + model params +
whether/which license password). The password itself is never written; only a
salted hash of it goes into the key.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import trimesh

from .settings import CACHE_DIR, ModelParams

CACHE_VERSION = 2


def cache_key(name: str, model: ModelParams, compartments: int, password: str) -> str:
    pw = hashlib.sha256(("snapstl:" + password).encode()).hexdigest()[:16] if password else ""
    payload = json.dumps(
        {"v": CACHE_VERSION, "name": name, "model": asdict(model), "compartments": int(compartments), "pw": pw}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def cache_dir(key: str, root: Path = CACHE_DIR) -> Path:
    return root / key


def load_exists(key: str, root: Path = CACHE_DIR) -> bool:
    d = cache_dir(key, root)
    return (d / "base.stl").exists() and (d / "name.stl").exists()


def load(key: str, root: Path = CACHE_DIR) -> tuple[trimesh.Trimesh, trimesh.Trimesh] | None:
    d = cache_dir(key, root)
    b, n = d / "base.stl", d / "name.stl"
    if not (b.exists() and n.exists()):
        return None
    try:
        return (
            trimesh.load(b, file_type="stl", force="mesh"),
            trimesh.load(n, file_type="stl", force="mesh"),
        )
    except Exception:
        return None


def store(key: str, base: trimesh.Trimesh, name: trimesh.Trimesh, meta: dict, root: Path = CACHE_DIR) -> Path:
    d = cache_dir(key, root)
    d.mkdir(parents=True, exist_ok=True)
    base.export(d / "base.stl", file_type="stl")
    name.export(d / "name.stl", file_type="stl")
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    return d
