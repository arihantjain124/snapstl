"""This computer's identity on batches (shared database).

A batch belongs to the computer that created it: only that computer downloads its models, builds
its plates and keeps its files. The id is generated once and saved in the app folder; the name comes
from SNAPSTL_COMPUTER_NAME in .env, else the computer's own name.
"""
from __future__ import annotations

import json
import os
import uuid

from .platform_os import computer_name
from .settings import APP_DIR

_PATH = APP_DIR / "computer.json"


def _load() -> tuple[str, str]:
    try:
        d = json.loads(_PATH.read_text())
        cid = str(d["id"])
    except (OSError, ValueError, KeyError):
        cid = uuid.uuid4().hex[:12]
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps({"id": cid}))
    name = os.environ.get("SNAPSTL_COMPUTER_NAME", "").strip() or computer_name()
    return cid, name[:60]


ID, NAME = _load()
