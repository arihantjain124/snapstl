"""Updating SnapSTL from its git repository (Settings → Updates, or scripts/update.sh).

The project folder is a git clone of the private SnapSTL repository. Updating =
`git pull --ff-only` + `uv sync` + restart the service. Customer data is never in the folder
(it lives in the app data folder and ~/SnapSTL), so pulling can't touch it.

Refuses to update when files in the folder were edited by hand (a pull could clash) or while a
name is being downloaded or a batch is being built (the restart would interrupt it).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the project folder (src/snapstl/updater.py → ../../)


def _git(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)


def uv_path() -> str | None:
    for p in (shutil.which("uv"), os.path.expanduser("~/.local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if p and os.path.exists(p):
            return p
    return None


def is_git() -> bool:
    return (ROOT / ".git").exists() and shutil.which("git") is not None


def _edited() -> list[str]:
    out = _git("status", "--porcelain", "--untracked-files=no").stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def check(fetch: bool = True) -> dict:
    """What's waiting on the server: {git, branch, behind, changes[], edited[], error}."""
    if not is_git():
        return {"git": False, "error": "This SnapSTL folder isn't set up with git yet — see INSTALL.md → Updates"}
    res: dict = {"git": True, "error": ""}
    if _git("remote", "get-url", "origin").returncode != 0:
        res["error"] = "No repository is connected to this folder yet — see INSTALL.md → step 2"
    elif fetch:
        f = _git("fetch", "--quiet", "origin")
        if f.returncode != 0:
            lines = f.stderr.strip().splitlines() or ["unknown error"]
            why = next((x for x in lines if x.startswith(("fatal:", "ERROR:", "ssh:"))), lines[0])
            res["error"] = "Couldn't reach the SnapSTL repository (" + why.removeprefix("fatal: ") + ")"
    res["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    res["current"] = _git("log", "-1", "--format=%h %s").stdout.strip()
    behind = _git("rev-list", "--count", "HEAD..@{u}")
    res["behind"] = int(behind.stdout.strip() or 0) if behind.returncode == 0 else 0
    res["changes"] = _git("log", "--format=%s", "-n", "15", "HEAD..@{u}").stdout.splitlines() if res["behind"] else []
    res["edited"] = _edited()
    return res


def apply() -> dict:
    """Pull and install. Raises RuntimeError with a plain-English reason if it can't."""
    if not is_git():
        raise RuntimeError("This SnapSTL folder isn't set up with git yet — see INSTALL.md → Updates")
    edited = _edited()
    if edited:
        raise RuntimeError("Files in the SnapSTL folder were changed by hand, so it can't update safely: "
                           + ", ".join(edited[:5]))
    before = _git("rev-parse", "HEAD").stdout.strip()
    p = _git("pull", "--ff-only", "--quiet", timeout=120)
    if p.returncode != 0:
        raise RuntimeError("Update failed: " + (p.stderr.strip().splitlines() or ["git pull failed"])[-1])
    after = _git("rev-parse", "HEAD").stdout.strip()
    if after != before:
        uv = uv_path()
        if uv is None:
            raise RuntimeError("Updated the files but couldn't find uv to install them — run scripts/update.sh")
        s = subprocess.run([uv, "sync", "--no-dev", "--quiet"], cwd=ROOT, capture_output=True, text=True, timeout=600)
        if s.returncode != 0:
            raise RuntimeError("Updated the files but installing them failed: " + s.stderr.strip()[-300:])
    return {"updated": after != before, "current": _git("log", "-1", "--format=%h %s").stdout.strip()}
