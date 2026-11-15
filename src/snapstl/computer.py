"""This computer's identity on batches and in the shared order records.

A batch belongs to the computer that created it: only that computer downloads its models, builds
its plates and keeps its files. The id is generated once and saved in the app folder (computer.json)
with the name set in Settings (default: the computer's own name; SNAPSTL_COMPUTER_NAME overrides it
in tests).
"""
from __future__ import annotations

import json
import os
import uuid

from .platform_os import computer_name
from .settings import APP_DIR

_PATH = APP_DIR / "computer.json"


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(d))


def _load() -> tuple[str, str]:
    d = _read()
    cid = str(d.get("id") or "")
    if not cid:
        cid = uuid.uuid4().hex[:12]
        _write({**d, "id": cid})
    name = (os.environ.get("SNAPSTL_COMPUTER_NAME", "").strip() or str(d.get("name") or "").strip()
            or computer_name())
    return cid, name[:60]


def set_name(name: str) -> str:
    """Settings → Shared order records → This computer's name (blank = the computer's own name)."""
    global NAME
    name = " ".join(name.split())[:60]
    _write({**_read(), "id": ID, "name": name})
    NAME = name or computer_name()[:60]
    return NAME


ID, NAME = _load()
