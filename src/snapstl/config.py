"""Secrets and per-computer configuration from the `.env` file in the SnapSTL folder.

`.env` is never in git (.gitignore); `.env.example` shows every key. Values already set in the
environment win over the file (tests and dev runs rely on that).

Keys:
  SNAPSTL_DATABASE_URL   Postgres connection string of the shared order records (Supabase, sync.py).
                         Empty = no sharing (this computer only). The local database is SQLite either way.
  SNAPSTL_COMPUTER_NAME  How this computer is named on batches (default: the computer's name).
  SNAPSTL_PORT           Port of the local web portal (default 8317).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the SnapSTL folder (src/snapstl/config.py → ../../)
ENV_PATH = ROOT / ".env"


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def load(path: Path = ENV_PATH) -> None:
    try:
        values = _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return
    for k, v in values.items():
        os.environ.setdefault(k, v)


load()


def database_url() -> str:
    return os.environ.get("SNAPSTL_DATABASE_URL", "").strip()
