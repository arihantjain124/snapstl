"""Downloaded MakerWorld models (unscaled), kept on disk until the user deletes them in Finder.

Location: Settings → Downloads folder (default ~/SnapSTL/Downloads). One folder per model, named
for people and ending in the cache key so the app can find it:

    ~/SnapSTL/Downloads/SONAL - 5 comp - 3f9c1a…e2/
        base.stl     the organizer base, exactly as MakerWorld generated it (unscaled)
        name.stl     the name piece, unscaled
        info.json    name, compartments, model options, when it was downloaded

SnapSTL never deletes these. If you delete one in Finder, that name is simply downloaded again the
next time a batch needs it. STL is only this folder's internal format; plates are written as .3mf.

Key = hash of everything that changes the geometry (name + model options + compartments +
license password). The password itself is never written; only a salted hash goes into the key.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path

import trimesh

from .settings import APP_DIR, DEFAULT_DOWNLOADS_DIR, ModelParams

CACHE_VERSION = 2
OLD_CACHE_DIR = APP_DIR / "cache"  # where models lived before v0.3 (hidden folder, hash names)

ROOT: Path = Path(DEFAULT_DOWNLOADS_DIR)  # set from Settings by the server (set_root)
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def set_root(path: str | Path) -> Path:
    global ROOT
    ROOT = Path(path).expanduser()
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


DEFAULT_DESIGN = "2600234"  # Custom Name Desk Organizer — keys for it stay as before (no re-downloads)


def cache_key(name: str, model: ModelParams, compartments: int, password: str, design: str | None = None) -> str:
    pw = hashlib.sha256(("snapstl:" + password).encode()).hexdigest()[:16] if password else ""
    data = {"v": CACHE_VERSION, "name": name, "model": asdict(model), "compartments": int(compartments), "pw": pw}
    if design and design != DEFAULT_DESIGN:
        data["design"] = design  # a different MakerWorld model never shares downloads
    payload = json.dumps(data, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def folder_name(key: str, name: str, compartments: int) -> str:
    label = _BAD.sub("", name).strip()[:60] or "model"
    return f"{label} - {int(compartments)} comp - {key}"


def find(key: str, root: Path | None = None) -> Path | None:
    """The model folder for this key, or None. Matches on the key at the end of the folder name."""
    root = root or ROOT
    if not root.is_dir():
        return None
    for d in root.glob(f"* - {key}"):
        if d.is_dir():
            return d
    legacy = root / key
    return legacy if legacy.is_dir() else None


def load_exists(key: str, root: Path | None = None) -> bool:
    d = find(key, root)
    return bool(d) and (d / "base.stl").exists() and (d / "name.stl").exists()


def load(key: str, root: Path | None = None) -> tuple[trimesh.Trimesh, trimesh.Trimesh] | None:
    d = find(key, root)
    if not d:
        return None
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


def store(key: str, base: trimesh.Trimesh, name: trimesh.Trimesh, meta: dict, root: Path | None = None) -> Path:
    """Save (or overwrite, e.g. after Re-download) the model for this key."""
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True)
    d = find(key, root) or root / folder_name(key, meta.get("name", "model"), meta.get("compartments", 0))
    d.mkdir(parents=True, exist_ok=True)
    base.export(d / "base.stl", file_type="stl")
    name.export(d / "name.stl", file_type="stl")
    (d / "info.json").write_text(json.dumps(meta, indent=2))
    return d


def stats(root: Path | None = None) -> dict:
    root = root or ROOT
    folders = [d for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    size = sum(f.stat().st_size for d in folders for f in d.iterdir() if f.is_file())
    return {"path": str(root), "models": len(folders), "bytes": size}


def migrate_old_cache(root: Path | None = None) -> int:
    """Move models from the old hidden cache (APP_DIR/cache/<key>) into the Downloads folder."""
    root = root or ROOT
    if not OLD_CACHE_DIR.is_dir() or OLD_CACHE_DIR.resolve() == root.resolve():
        return 0
    moved = 0
    for d in list(OLD_CACHE_DIR.iterdir()):
        if not (d.is_dir() and (d / "base.stl").exists() and (d / "name.stl").exists()):
            continue
        key = d.name
        if find(key, root):
            continue
        meta = {}
        for fn in ("meta.json", "info.json"):
            try:
                meta = json.loads((d / fn).read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        target = root / folder_name(key, meta.get("name", "model"), meta.get("compartments", 0))
        root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(target))
        if (target / "meta.json").exists() and not (target / "info.json").exists():
            (target / "meta.json").rename(target / "info.json")
        moved += 1
    try:
        OLD_CACHE_DIR.rmdir()  # only if now empty
    except OSError:
        pass
    return moved
