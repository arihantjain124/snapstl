"""Everything that differs between macOS and Windows, in one place.

The rest of SnapSTL calls these functions and never checks the operating system itself.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
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


def orca_candidates() -> list[Path]:
    """Where OrcaSlicer's program may be (SNAPSTL_ORCA overrides: tests, unusual installs)."""
    if os.environ.get("SNAPSTL_ORCA") is not None:
        return [Path(os.environ["SNAPSTL_ORCA"])] if os.environ["SNAPSTL_ORCA"] else []
    if WINDOWS:
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")]
        return [Path(p) / "OrcaSlicer" / "orca-slicer.exe" for p in pf if p] + \
               [Path(p) / "Programs" / "OrcaSlicer" / "orca-slicer.exe" for p in pf[1:] if p]
    return [Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
            Path.home() / "Applications" / "OrcaSlicer.app" / "Contents" / "MacOS" / "OrcaSlicer"]


def bambu_candidates() -> list[Path]:
    """Where Bambu Studio's program may be (SNAPSTL_BAMBU_STUDIO overrides: tests, unusual installs)."""
    if os.environ.get("SNAPSTL_BAMBU_STUDIO") is not None:
        return [Path(os.environ["SNAPSTL_BAMBU_STUDIO"])] if os.environ["SNAPSTL_BAMBU_STUDIO"] else []
    if WINDOWS:
        out = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Bambu Studio" / "bambu-studio.exe"]
        if os.environ.get("LOCALAPPDATA"):
            out.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Bambu Studio" / "bambu-studio.exe")
        return out
    return [Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
            Path.home() / "Applications" / "BambuStudio.app" / "Contents" / "MacOS" / "BambuStudio"]


def slicer_data_dir(app: str) -> Path | None:
    """A slicer app's settings folder ("OrcaSlicer" / "BambuStudio"): its system/ and user/ presets.
    SNAPSTL_SLICER_HOME replaces the base folder (tests)."""
    base = os.environ.get("SNAPSTL_SLICER_HOME")
    if base is not None:
        return Path(base) / app if base else None
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / app
    return Path.home() / "Library" / "Application Support" / app


def slicer_resource_dirs() -> list[Path]:
    """Preset folders inside the installed slicer apps (Bambu Studio keeps them as plain files)."""
    if os.environ.get("SNAPSTL_SLICER_HOME") is not None:
        return []
    if WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [Path(pf) / "Bambu Studio" / "resources" / "profiles"]
    return [Path("/Applications/BambuStudio.app/Contents/Resources/profiles"),
            Path.home() / "Applications" / "BambuStudio.app" / "Contents" / "Resources" / "profiles"]


def bambuddy_dir() -> Path:
    """Where the installers put Bambuddy (the print-farm manager, scripts/bambuddy-mac.sh / bambuddy.ps1)."""
    return Path(os.environ.get("BAMBUDDY_DIR") or Path.home() / "SnapSTL-Bambuddy")


def update_bambuddy(root: Path) -> bool:
    """After an in-app update: bring Bambuddy to the version this SnapSTL was tested with, in the
    background (it survives SnapSTL restarting). Only where the installer put Bambuddy."""
    if os.environ.get("SNAPSTL_NO_BAMBUDDY") == "1" or not (bambuddy_dir() / ".git").is_dir():
        return False
    log = app_dir() / "bambuddy-update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        if WINDOWS:
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(root / "scripts" / "windows" / "bambuddy.ps1")], stdout=out, stderr=out,
                             creationflags=NO_WINDOW | 0x00000008 | 0x00000200)  # DETACHED, NEW_PROCESS_GROUP
        else:
            subprocess.Popen(["/bin/zsh", str(root / "scripts" / "bambuddy-mac.sh")], stdout=out, stderr=out,
                             start_new_session=True)
    return True


def restart_service() -> None:
    """Restart the background service (it is started again by launchd / Task Scheduler)."""
    if WINDOWS:
        # The task restarts SnapSTL when it exits (scripts/install.ps1: restart on failure + a watchdog
        # loop in the launcher), so exiting is enough.
        os._exit(3)
    subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SERVICE_LABEL}"])
