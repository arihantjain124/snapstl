"""The Bambuddy computer, run from SnapSTL (Windows only): Bambuddy's Windows service and its slicer
service in Docker (scripts/windows/bambuddy-server.ps1 installs them once).

Settings → Connections shows whether each part is up; **Start** starts what is down (a click may ask
Windows for admin rights to start Bambuddy), and with "Keep running" on SnapSTL checks every minute and
starts what has stopped. Nothing that is running is ever stopped or restarted by itself — only
**Restart …** (a person's click) does that, and only for the Bambuddy SnapSTL's script installed.
Nothing is installed or downloaded here except slicer images that are missing (as the script does).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from . import platform_os

ROOT = Path(__file__).resolve().parents[2]
REPO_COMPOSE = ROOT / "scripts" / "bambuddy-docker" / "docker-compose.yml"
PROJECT = "snapstl-bambuddy"
SIDECARS = ("snapstl-bambu-studio-api", "snapstl-orca-slicer-api")
SLICER_HEALTH = "http://127.0.0.1:3001/health"
BAMBUDDY_HEALTH = "http://127.0.0.1:8000/api/v1/auth/status"
CHECK_S = 60          # Keep running: how often SnapSTL looks
DOCKER_WAIT_S = 6 * 60  # Docker Desktop's engine can take minutes to come up after sign-in


def setup() -> dict:
    """What bambuddy-server.ps1 remembered ({} = it never ran here)."""
    try:
        return json.loads((platform_os.bambuddy_setup_dir() / "setup.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def version() -> str:
    v = str(setup().get("version") or "")
    if not v:
        try:
            v = (ROOT / "scripts" / "bambuddy-version.txt").read_text().strip()
        except OSError:
            v = ""
    return v.lstrip("v")


def is_here() -> bool:
    """This is the Bambuddy computer: Bambuddy's service or the setup script's folder is here."""
    if not platform_os.WINDOWS or os.environ.get("SNAPSTL_NO_BAMBUDDY") == "1":  # tests: never the real one
        return False
    return platform_os.service_state() is not None or (platform_os.bambuddy_setup_dir() / "setup.json").is_file()


def ours() -> bool:
    """Bambuddy was installed by SnapSTL's script (only that one may be restarted from SnapSTL)."""
    return bool(setup().get("native_installed_by_snapstl"))


def _answers(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — fixed local URLs only
            return 200 <= r.status < 500
    except Exception:  # noqa: BLE001
        return False


def compose_cmd() -> tuple[list[str], Path, dict] | None:
    """`docker compose …` for the slicer service: the setup script's copy (it made the running containers),
    else the one in this repository; pinned to the Bambuddy version. None = Docker isn't installed."""
    docker = platform_os.docker_cli()
    if not docker:
        return None
    d = platform_os.bambuddy_setup_dir()
    f = d / "docker-compose.yml" if (d / "docker-compose.yml").is_file() else REPO_COMPOSE
    return [docker, "compose", "-p", PROJECT, "-f", str(f)], f.parent, {"SIDECAR_TAG": f"bambuddy-{version()}"}


def docker_running() -> bool:
    docker = platform_os.docker_cli()
    return bool(docker) and platform_os.run_quiet([docker, "info", "--format", "{{.ServerVersion}}"], timeout=20)[0] == 0


def sidecars() -> list[dict]:
    """The slicer containers: [{name, state ("running", "exited", "missing"…), health}]."""
    docker = platform_os.docker_cli()
    found: dict[str, dict] = {}
    if docker:
        code, out = platform_os.run_quiet([docker, "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                                           "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], timeout=20)
        if code == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    health = "healthy" if "(healthy)" in parts[2] else "unhealthy" if "unhealthy" in parts[2] else ""
                    found[parts[0]] = {"name": parts[0], "state": parts[1], "health": health, "status": parts[2]}
    return [found.get(n, {"name": n, "state": "missing", "health": "", "status": ""}) for n in SIDECARS]


class Host:
    """Status, Start / Restart and the Keep running check (one action at a time, in the background)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.busy = ""             # what is being done right now ("" = nothing)
        self.log: list[str] = []
        self.last_check = 0.0

    def _log(self, msg: str) -> None:
        self.log = (self.log + [time.strftime("%H:%M:%S ") + msg])[-40:]

    def status(self) -> dict:
        here = is_here()
        out = {"here": here, "busy": self.busy, "log": self.log[-12:], "keep_running": keep_running()}
        if not here:
            return out
        svc = platform_os.service_state()
        dock = docker_running()
        out.update({
            "bambuddy": {"service": svc or "not installed", "answering": _answers(BAMBUDDY_HEALTH),
                         "ours": ours(), "version": version()},
            "docker": {"installed": bool(platform_os.docker_cli() or platform_os.docker_desktop()), "running": dock},
            "slicer": {"containers": sidecars() if dock else [], "answering": _answers(SLICER_HEALTH)},
        })
        out["ok"] = (svc == "running" and out["bambuddy"]["answering"] and dock
                     and all(c["state"] == "running" for c in out["slicer"]["containers"]))
        return out

    # ---- actions (background) ----
    def _run(self, what: str, fn) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = what

        def go():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — shown on the card
                self._log(f"{what}: {e}")
            finally:
                self.busy = ""

        self._thread = threading.Thread(target=go, name="bambuddy-host", daemon=True)
        self._thread.start()
        return True

    def start(self, elevate: bool = False) -> bool:
        """Start whatever is down (Bambuddy's service, Docker Desktop, the slicer containers)."""
        return self._run("Starting", lambda: self._start(elevate))

    def restart_bambuddy(self) -> bool:
        if not ours():
            raise PermissionError("This Bambuddy wasn't installed by SnapSTL — restart it in Windows Services")
        return self._run("Restarting Bambuddy", self._restart_bambuddy)

    def restart_slicer(self) -> bool:
        return self._run("Restarting the slicer service", self._restart_slicer)

    def _start(self, elevate: bool) -> None:
        if not is_here():
            self._log("Bambuddy isn't set up on this computer (scripts\\windows\\bambuddy-server.ps1)")
            return
        svc = platform_os.service_state()
        if svc in ("stopped", "paused"):
            err = platform_os.control_service("start", elevate=elevate)
            self._log(f"Bambuddy: {err}" if err else "Bambuddy: starting" + (" (allow the Windows prompt)" if elevate else ""))
        self._start_slicer()

    def _start_slicer(self) -> None:
        if not docker_running():
            if not platform_os.start_docker_desktop():
                self._log("Docker Desktop isn't installed — run scripts\\windows\\bambuddy-server.ps1 once")
                return
            self._log("Docker Desktop: starting (can take a few minutes)")
            deadline = time.time() + DOCKER_WAIT_S
            while not docker_running():
                if time.time() > deadline:
                    self._log("Docker Desktop didn't start — open it and finish any screens it shows")
                    return
                time.sleep(5)
            self._log("Docker Desktop: running")
        if all(c["state"] == "running" for c in sidecars()):
            return
        cmd = compose_cmd()
        if cmd is None:
            self._log("Docker isn't installed")
            return
        base, cwd, env = cmd
        code, out = platform_os.run_quiet(base + ["up", "-d"], timeout=20 * 60, cwd=cwd, env=env)
        self._log("Slicer service: started" if code == 0 else f"Slicer service didn't start: {out[-300:]}")

    def _restart_bambuddy(self) -> None:
        err = platform_os.control_service("restart", elevate=True)
        self._log(f"Bambuddy: {err}" if err else "Bambuddy: restarting (allow the Windows prompt if one shows)")

    def _restart_slicer(self) -> None:
        if not docker_running():
            self._start_slicer()
            return
        cmd = compose_cmd()
        if cmd is None:
            return
        base, cwd, env = cmd
        code, out = platform_os.run_quiet(base + ["up", "-d", "--force-recreate"], timeout=20 * 60, cwd=cwd, env=env)
        self._log("Slicer service: restarted" if code == 0 else f"Slicer service didn't restart: {out[-300:]}")

    # ---- Keep running ----
    def tick(self) -> None:
        """Every CHECK_S: start what has stopped (never restarts anything running; no admin prompt)."""
        if time.time() - self.last_check < CHECK_S or self.busy or not keep_running() or not is_here():
            return
        self.last_check = time.time()
        svc = platform_os.service_state()
        dock = docker_running()
        down = svc in ("stopped", "paused") or not dock or any(c["state"] != "running" for c in sidecars())
        if down:
            self._log("Keep running: something had stopped — starting it")
            self.start(elevate=False)

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — keep checking
                self._log(f"Keep running: {e}")
            time.sleep(10)


def keep_running() -> bool:
    from . import farm

    return bool(farm.load_config().get("keep_bambuddy_running"))


def set_keep_running(on: bool) -> bool:
    from . import farm

    farm.update_config(keep_bambuddy_running=bool(on))
    if on:
        HOST.last_check = 0.0
    return bool(on)


HOST = Host()
_started = False


def start_watch() -> None:
    """Called once at SnapSTL's start (Windows Bambuddy computer only)."""
    global _started
    if _started or not platform_os.WINDOWS or os.environ.get("SNAPSTL_NO_BAMBUDDY") == "1":
        return
    _started = True
    threading.Thread(target=HOST.loop, name="bambuddy-keep-running", daemon=True).start()
