"""Per-computer connection details, entered in the app (Settings → Shared order records).

The shared order records' connection string holds the Supabase database password, so it is kept
only on this computer: `shared-records.json` in the app folder, readable by this user only. It is
never shown back in full, never logged and never in git (the repository is public).

For tests and development an environment variable overrides it:
  SNAPSTL_DATABASE_URL   connection string ("" = no shared records)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .platform_os import app_dir

ROOT = Path(__file__).resolve().parents[2]  # the SnapSTL folder (src/snapstl/config.py → ../../)
SECRETS_PATH = app_dir() / "shared-records.json"


def _read() -> dict:
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def database_url() -> str:
    if "SNAPSTL_DATABASE_URL" in os.environ:  # tests / development
        return os.environ["SNAPSTL_DATABASE_URL"].strip()
    return str(_read().get("database_url", "")).strip()


def set_database_url(url: str) -> None:
    """Save (or with "" remove) the connection string, readable by this user only."""
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _read()
    data["database_url"] = url.strip()
    tmp = SECRETS_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SECRETS_PATH)


def check_url(url: str) -> str:
    """A plain-English problem with a pasted connection string, or ""."""
    url = url.strip()
    if not url:
        return "Paste the connection string from Supabase"
    if "YOUR-PASSWORD" in url:
        return "Replace [YOUR-PASSWORD] (brackets included) with your database password"
    try:
        u = urlsplit(url)
        _ = u.port
    except ValueError:
        return ("The connection string isn't valid — if your database password has symbols "
                "(@ : / # ? %), change it in Supabase to letters and digits only")
    if u.scheme not in ("postgres", "postgresql"):
        return "It should start with postgresql:// (Supabase → Connect → Connection string → URI)"
    if not u.password:
        return "The database password is missing from the connection string"
    if not u.hostname:
        return "The server address is missing from the connection string"
    return ""


def describe(url: str | None = None) -> str:
    """Where it connects, without the password (e.g. 'postgres.abcd @ aws-0-ap-south-1.pooler.supabase.com')."""
    url = database_url() if url is None else url
    if not url:
        return ""
    try:
        u = urlsplit(url)
        return f"{u.username or '?'} @ {u.hostname or '?'}"
    except ValueError:
        return "(not valid)"
