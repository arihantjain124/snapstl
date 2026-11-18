"""Updating SnapSTL from its git repository (Settings → Updates, or scripts/update.sh).

The project folder is a git clone of the public SnapSTL repository. Updating follows a branch
from origin, runs `uv sync`, and restarts the service. Customer data is never in the folder
(it lives in the app data folder and ~/SnapSTL), so pulling can't touch it.

Refuses to update when files in the folder were edited by hand (a pull could clash) or while a
name is being downloaded or a batch is being built (the restart would interrupt it).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from threading import Lock

from .config import ROOT  # the SnapSTL folder

_APPLY_LOCK = Lock()


def _git(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout,
                          creationflags=0x08000000 if os.name == "nt" else 0)  # no console window on Windows


def uv_path() -> str | None:
    for p in (shutil.which("uv"), os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.local/bin/uv.exe"),
              "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if p and os.path.exists(p):
            return p
    return None


def is_git() -> bool:
    return (ROOT / ".git").exists() and shutil.which("git") is not None


def _edited() -> list[str]:
    status = _git("status", "--porcelain", "--untracked-files=no")
    if status.returncode:
        raise RuntimeError("Couldn't check the SnapSTL folder for local edits")
    out = status.stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def head() -> str:
    """The commit the folder is on now ("" outside git)."""
    if not is_git():
        return ""
    return _git("rev-parse", "HEAD").stdout.strip()


# The commit this SnapSTL started from: after a `git pull` by hand the folder moves on, but the running
# app keeps the old code until it restarts — check() then says so instead of "up to date".
STARTED_AT = head()


def _fetch():
    return _git("fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=120)


def _branches() -> list[str]:
    refs = _git("for-each-ref", "--format=%(refname) %(symref)", "refs/remotes/origin/")
    return [line.rstrip().removeprefix("refs/remotes/origin/") for line in refs.stdout.splitlines()
            if " " not in line.rstrip()]


def check(fetch: bool = True) -> dict:
    """What's waiting on the server: {git, branch, behind, changes[], edited[], error}."""
    if not is_git():
        return {"git": False, "error": "This SnapSTL folder isn't set up with git yet — see README → Install"}
    res: dict = {"git": True, "error": ""}
    if _git("remote", "get-url", "origin").returncode != 0:
        res["error"] = "No repository is connected to this folder yet — see README → Install"
    elif fetch:
        f = _fetch()
        if f.returncode != 0:
            lines = f.stderr.strip().splitlines() or ["unknown error"]
            why = next((x for x in lines if x.startswith(("fatal:", "ERROR:", "ssh:"))), lines[0])
            res["error"] = "Couldn't reach the SnapSTL repository (" + why.removeprefix("fatal: ") + ")"
    res["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    res["branches"] = _branches()
    res["current"] = _git("log", "-1", "--format=%h %s").stdout.strip()
    behind = _git("rev-list", "--count", "HEAD..@{u}")
    res["behind"] = int(behind.stdout.strip() or 0) if behind.returncode == 0 else 0
    res["changes"] = _git("log", "--format=%s", "-n", "15", "HEAD..@{u}").stdout.splitlines() if res["behind"] else []
    try:
        res["edited"] = _edited()
    except RuntimeError as e:
        res["error"] = str(e)
    now = head()
    res["restart_needed"] = bool(STARTED_AT and now and now != STARTED_AT)
    if res["restart_needed"]:
        res["running"] = _git("log", "-1", "--format=%h %s", STARTED_AT).stdout.strip()
    return res


def apply(branch: str | None = None) -> dict:
    """Pull and install. Raises RuntimeError with a plain-English reason if it can't."""
    if not _APPLY_LOCK.acquire(blocking=False):
        raise RuntimeError("An update is already running")
    try:
        return _apply(branch)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("The update timed out. Check Updates before trying again.") from e
    finally:
        _APPLY_LOCK.release()


def _apply(branch: str | None) -> dict:
    if not is_git():
        raise RuntimeError("This SnapSTL folder isn't set up with git yet — see README → Install")
    edited = _edited()
    if edited:
        raise RuntimeError("Files in the SnapSTL folder were changed by hand, so it can't update safely: "
                           + ", ".join(edited[:5]))
    before = head()
    current_branch = _git("branch", "--show-current").stdout.strip()
    switched = branch is not None and branch != current_branch
    if branch is None:
        p = _git("pull", "--ff-only", "--quiet", timeout=120)
    else:
        fetched = _fetch()
        if fetched.returncode:
            raise RuntimeError("Couldn't refresh branches from the SnapSTL repository")
        if branch.startswith("-") or branch not in _branches():
            raise RuntimeError("Choose a branch published in the SnapSTL repository")
        target = f"refs/remotes/origin/{branch}"
        local = f"refs/heads/{branch}"
        if _git("show-ref", "--verify", "--quiet", local).returncode == 0:
            if _git("merge-base", "--is-ancestor", local, target).returncode:
                raise RuntimeError(f"Local branch {branch} has commits outside origin/{branch}. "
                                   "Use Git to review it before updating.")
        if switched:
            p = _git("switch", "--quiet", "--no-overwrite-ignore", "--track", f"--force-create={branch}", target)
        else:
            p = _git("merge", "--ff-only", "--quiet", "--no-overwrite-ignore", target)
    if p.returncode != 0:
        raise RuntimeError("Update failed: " + (p.stderr.strip().splitlines() or ["git pull failed"])[-1])
    after = head()
    if after != before and os.name == "nt":
        # Windows can't replace files of the running Python: the service launcher (scripts/windows/
        # run-service.ps1) runs `uv run`, which installs the update before SnapSTL starts again.
        pass
    elif after != before:
        uv = uv_path()
        if uv is None:
            raise RuntimeError("Updated the files but couldn't find uv to install them — run scripts/update.sh")
        s = subprocess.run([uv, "sync", "--no-dev", "--quiet"], cwd=ROOT, capture_output=True, text=True, timeout=600)
        if s.returncode != 0:
            raise RuntimeError("Updated the files but installing them failed: " + s.stderr.strip()[-300:])
    active_branch = branch if branch is not None else current_branch
    if after != before and active_branch == "main":
        from . import platform_os

        platform_os.update_bambuddy(ROOT)  # the Bambuddy version in scripts/bambuddy-version.txt (background)
    return {"updated": after != before, "switched": switched, "branch": active_branch,
            "current": _git("log", "-1", "--format=%h %s").stdout.strip()}
