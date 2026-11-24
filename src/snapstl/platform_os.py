"""Everything that differs between macOS and Windows, in one place.

The rest of SnapSTL calls these functions and never checks the operating system itself.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
SERVICE_LABEL = "local.snapmagicians.snapstl"  # launchd label (Mac)
TASK_NAME = "SnapSTL"  # Task Scheduler task (Windows)
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW: no console flashing up


def app_dir() -> Path:
    """Settings, local database, backups, previews."""
    if os.environ.get("SNAPSTL_HOME"):
        return Path(os.environ["SNAPSTL_HOME"])
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SnapSTL"
    return Path.home() / "Library" / "Application Support" / "SnapSTL"


def file_manager() -> str:
    return "File Explorer" if WINDOWS else "Finder"


def open_folder(path: Path) -> None:
    path = Path(path)
    if WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif MAC:
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def reveal(path: Path) -> None:
    """Show a file selected in its folder (or open a folder)."""
    path = Path(path)
    if path.is_dir():
        open_folder(path)
    elif WINDOWS:
        subprocess.run(["explorer", "/select,", str(path)], check=False, creationflags=NO_WINDOW)
    elif MAC:
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        open_folder(path.parent)


def to_trash(path: Path) -> bool:
    """Move a file or folder to the Recycle Bin (Windows) / Trash (Mac, Linux) — never deleted for good.
    Returns True when it's gone from its place."""
    path = Path(path)
    if not path.exists():
        return True
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path.resolve()) + "\0", None,
                             FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not op.fAnyOperationsAborted and not path.exists()
    trash = Path.home() / (".Trash" if MAC else ".local/share/Trash/files")
    trash.mkdir(parents=True, exist_ok=True)
    dest, n = trash / path.name, 2
    while dest.exists():
        dest = trash / f"{path.name} {n}"
        n += 1
    import shutil

    shutil.move(str(path), str(dest))
    return not path.exists()


_awake_proc = None


def keep_awake() -> None:
    """Stop the computer from idle-sleeping while SnapSTL runs."""
    global _awake_proc
    try:
        if WINDOWS:
            import ctypes

            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        elif MAC and _awake_proc is None:
            _awake_proc = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    except OSError:
        pass


def computer_name() -> str:
    if MAC:
        try:
            n = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3).stdout.strip()
            if n:
                return n
        except (OSError, subprocess.SubprocessError):
            pass
    return os.environ.get("COMPUTERNAME") or socket.gethostname().split(".")[0] or "This computer"


def lan_name() -> str:
    """Name other computers on the network can use (Mac: Bonjour .local name)."""
    if MAC:
        try:
            n = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
            if n:
                return n + ".local"
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname()


def lan_ips() -> list[str]:
    """This computer's address on the local network (the one used to reach the internet)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def running_as_service() -> bool:
    if WINDOWS:
        return os.environ.get("SNAPSTL_SERVICE") == "1"  # set by the scheduled task (scripts/install.ps1)
    return os.environ.get("XPC_SERVICE_NAME") == SERVICE_LABEL


def bambuddy_dir() -> Path:
    """Where the installers put Bambuddy (older installs, Windows: scripts/windows/bambuddy.ps1)."""
    return Path(os.environ.get("BAMBUDDY_DIR") or Path.home() / "SnapSTL-Bambuddy")


def update_bambuddy(root: Path) -> bool:
    """After an in-app update: bring Bambuddy to the version this SnapSTL was tested with, in the
    background (it survives SnapSTL restarting). Only on Windows, where an older SnapSTL installer put
    Bambuddy next to SnapSTL; Bambuddy scripts are Windows only (a Mac's old copy is left as it is)."""
    if not WINDOWS or os.environ.get("SNAPSTL_NO_BAMBUDDY") == "1" or not (bambuddy_dir() / ".git").is_dir():
        return False
    log = app_dir() / "bambuddy-update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(root / "scripts" / "windows" / "bambuddy.ps1")], stdout=out, stderr=out,
                         creationflags=NO_WINDOW | 0x00000008 | 0x00000200)  # DETACHED, NEW_PROCESS_GROUP
    return True


# ---- the Bambuddy computer (Windows only): Bambuddy's Windows service and Docker Desktop ----
BAMBUDDY_SERVICE = "Bambuddy"  # Bambuddy's own Windows installer names its service so
_SC_STATES = {"1": "stopped", "2": "starting", "3": "stopping", "4": "running", "5": "starting", "6": "stopping",
              "7": "paused"}


def bambuddy_setup_dir() -> Path:
    """Where scripts/windows/bambuddy-server.ps1 keeps setup.json and the slicer's docker-compose.yml."""
    return Path(os.environ.get("ProgramData") or "C:/ProgramData") / "SnapSTL-Bambuddy"


def service_state(name: str = BAMBUDDY_SERVICE) -> str | None:
    """running / stopped / starting / stopping / paused; None = no such service (or not Windows)."""
    if not WINDOWS:
        return None
    try:
        r = subprocess.run(["sc.exe", "query", name], capture_output=True, text=True, timeout=10,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        if "STATE" in line and ":" in line:
            return _SC_STATES.get(line.split(":", 1)[1].split()[0], "unknown")
    return None


def control_service(action: str, name: str = BAMBUDDY_SERVICE, elevate: bool = False) -> str:
    """Start or restart a Windows service. Returns "" when done or asked for, else why not.
    `elevate` (only from a person's click): when Windows refuses (SnapSTL runs without admin rights),
    ask with Windows' own admin prompt (UAC) — the person approves it there."""
    if not WINDOWS:
        return "Only on the Windows Bambuddy computer"
    if action not in ("start", "restart"):
        raise ValueError(action)
    cmds = [["sc.exe", "start", name]] if action == "start" else [["sc.exe", "stop", name], ["sc.exe", "start", name]]
    denied = False
    for i, cmd in enumerate(cmds):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        if r.returncode == 5:  # ERROR_ACCESS_DENIED
            denied = True
            break
        if r.returncode not in (0, 1056, 1062):  # 1056 already running, 1062 not started
            return (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else f"code {r.returncode}"
        if cmd[1] == "stop":  # wait until it has stopped before starting it again
            for _ in range(60):
                if service_state(name) == "stopped":
                    break
                time.sleep(1)
    if not denied:
        return ""
    if not elevate:
        return "Windows needs admin rights to start Bambuddy — press Start in SnapSTL and allow the prompt"
    verb = "Start-Service" if action == "start" else "Restart-Service -Force"
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          f"Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList "
                          f"'-NoProfile -Command {verb} -Name {name}'"], creationflags=NO_WINDOW)
    except OSError as e:
        return str(e)
    return ""


def docker_cli() -> str | None:
    """The docker command (Docker Desktop's, when it isn't on PATH yet)."""
    import shutil

    found = shutil.which("docker")
    if found:
        return found
    if WINDOWS:
        p = Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        if p.is_file():
            return str(p)
    return None


def docker_desktop() -> Path | None:
    if WINDOWS:
        p = Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "Docker" / "Docker" / "Docker Desktop.exe"
        return p if p.is_file() else None
    return None


def start_docker_desktop() -> bool:
    """Open Docker Desktop (its engine starts with it); False = not installed."""
    exe = docker_desktop()
    if exe is None:
        return False
    subprocess.Popen([str(exe)], creationflags=NO_WINDOW | 0x00000008 | 0x00000200, close_fds=True)
    return True


def restart_docker_desktop() -> bool:
    """Restart Docker Desktop (its engine and every container with it); False = not installed.
    Docker Desktop's own `docker desktop restart` when it has it, else close it and open it again."""
    docker, exe = docker_cli(), docker_desktop()
    if docker and run_quiet([docker, "desktop", "restart"], timeout=5 * 60)[0] == 0:
        return True
    if exe is None:
        return False
    run_quiet(["taskkill", "/IM", "Docker Desktop.exe", "/F"], timeout=30)
    time.sleep(5)
    return start_docker_desktop()


# OctoEverywhere Plugin Manager (Microsoft Store app; its plugins run in Docker)
OCTO_PACKAGE = "9020QuinnDamerell.1221C557A868_m9y180tpb7212"
OCTO_EXE = "OctoEverywherePluginManager.exe"


def octoeverywhere_manager() -> dict:
    """{installed, running} for the OctoEverywhere Plugin Manager app (Windows only)."""
    if not WINDOWS:
        return {"installed": False, "running": False}
    installed = (Path(os.environ.get("LOCALAPPDATA") or "") / "Packages" / OCTO_PACKAGE).is_dir()
    code, out = run_quiet(["tasklist", "/FI", f"IMAGENAME eq {OCTO_EXE}", "/FO", "CSV", "/NH"], timeout=15)
    return {"installed": installed, "running": code == 0 and OCTO_EXE.lower() in out.lower()}


def open_octoeverywhere_manager() -> bool:
    """Open the OctoEverywhere Plugin Manager (opening it also updates its plugins); False = not installed."""
    if not octoeverywhere_manager()["installed"]:
        return False
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + OCTO_PACKAGE + "!App"], creationflags=NO_WINDOW)
    return True


def run_quiet(cmd: list[str], timeout: float = 60, cwd: Path | None = None, env: dict | None = None):
    """A command with no console window: (exit code, output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, creationflags=NO_WINDOW,
                           env={**os.environ, **(env or {})}, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def printers() -> list[str]:
    """The printers installed on this computer (names as the system shows them)."""
    if WINDOWS:
        code, out = run_quiet(["powershell", "-NoProfile", "-Command",
                               "Get-Printer | Select-Object -ExpandProperty Name"], timeout=30)
    else:
        code, out = run_quiet(["lpstat", "-e"], timeout=30)
    return [x.strip() for x in out.splitlines() if x.strip()] if code == 0 else []


SUMATRA_PLACES = [Path(os.environ.get("LOCALAPPDATA", "")) / "SumatraPDF" / "SumatraPDF.exe",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SumatraPDF" / "SumatraPDF.exe",
                  Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "SumatraPDF" / "SumatraPDF.exe"]


def pdf_printer_tool(configured: str = "") -> str | None:
    """What prints a PDF without opening it: SumatraPDF on Windows (owner's OK, 0.33), lp on a Mac."""
    if not WINDOWS:
        return "lp"
    for c in ([Path(configured)] if configured else []) + SUMATRA_PLACES:
        if str(c) and c.is_file():
            return str(c)
    return None


def print_pdf(path: Path, printer: str, configured_tool: str = "") -> None:
    """Print a PDF on this printer, scaled to fit its paper (the label printer's 4x6 paper is set in its
    driver, as for a print from Chrome). Raises RuntimeError with the reason when it didn't print."""
    tool = pdf_printer_tool(configured_tool)
    if tool is None:
        raise RuntimeError("SumatraPDF isn't installed — it prints the labels (install it, or set its path in "
                           "Settings → Logistics → Shipping labels)")
    if WINDOWS:
        cmd = [tool, "-print-to", printer, "-print-settings", "fit", "-silent", str(path)]
    else:
        cmd = [tool, "-d", printer, "-o", "fit-to-page", str(path)]
    code, out = run_quiet(cmd, timeout=90)
    if code != 0:
        raise RuntimeError(f"The printer didn't take the label ({printer}): {out[:200] or f'exit {code}'}")


def restart_service() -> None:
    """Restart the background service (it is started again by launchd / Task Scheduler)."""
    if WINDOWS:
        # The task restarts SnapSTL when it exits (scripts/install.ps1: restart on failure + a watchdog
        # loop in the launcher), so exiting is enough.
        os._exit(3)
    subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"])
