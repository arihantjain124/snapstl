"""SnapSTL — batch name-organizer STL pipeline (local app for macOS and Windows)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import platform_os

# Fixed local address for the web portal (and the app window).
PORT = int(os.environ.get("SNAPSTL_PORT", "8317"))
URL = f"http://127.0.0.1:{PORT}/"


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_snapstl(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/meta", timeout=2) as r:
            return "fonts" in json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False


def _keep_awake() -> None:
    """Stop the computer from idle-sleeping while SnapSTL is open (ends when the app quits)."""
    platform_os.keep_awake()


LAUNCH_AGENT = platform_os.SERVICE_LABEL
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_AGENT}.plist")


def _start_service() -> bool:
    """Start the installed background service, if there is one (it keeps running after the window closes)."""
    if platform_os.WINDOWS:
        r = subprocess.run(["schtasks", "/Run", "/TN", platform_os.TASK_NAME], capture_output=True,
                           creationflags=platform_os.NO_WINDOW)
        return r.returncode == 0
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LAUNCH_AGENT}"], check=False)
        return True
    return False


class _WindowApi:
    """Functions the page can call inside the app window (not available in a normal browser)."""

    def choose_folder(self) -> str:
        import webview

        win = webview.windows[0] if webview.windows else None
        if win is None:
            return ""
        res = win.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else ""


def _open_window(url: str) -> None:
    try:
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("SnapSTL", url, width=1320, height=880, min_size=(900, 640), js_api=_WindowApi(),
                              text_select=True)  # allow selecting / copying text (off by default in pywebview)
        webview.start()
    except Exception:  # noqa: BLE001 — no native window available: use the browser
        webbrowser.open(url)
        print(f"SnapSTL running at {url} — press Ctrl+C to quit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def _wait_for_server(seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _is_snapstl(URL):
            return True
        time.sleep(0.3)
    return False


def serve() -> None:
    """Background service (started at login by launchd / Task Scheduler): portal + downloads + builds, no window."""
    if _port_busy(PORT):
        print(f"SnapSTL is already running on port {PORT}")
        return
    import uvicorn

    from .server import app

    from .settings import load_settings

    # Listen on the network only when Settings → Other computers is on (needs a portal password).
    host = "127.0.0.1"
    os.environ["SNAPSTL_BIND"] = host
    _keep_awake()
    uvicorn.run(app, host=host, port=PORT, log_level="warning")


def main() -> None:
    """The app: make sure the service runs, then show the window."""
    if _port_busy(PORT):
        if _is_snapstl(URL):
            _open_window(URL)
            return
        print(f"Port {PORT} is used by another program. Quit it or set SNAPSTL_PORT.", file=sys.stderr)
        raise SystemExit(1)

    if _start_service():
        if _wait_for_server(30):
            _open_window(URL)
            return

    import uvicorn

    from .server import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        if not t.is_alive():
            raise SystemExit("SnapSTL server failed to start")
        time.sleep(0.05)
    _keep_awake()
    _open_window(URL)
    server.should_exit = True
