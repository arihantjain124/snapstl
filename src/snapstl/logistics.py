"""Optional LAN listeners for Mac and Windows (0.26: two pages, each with its own password).

* **Packing page** — port 8002 (`PACK`, Settings → Connections → Packing page, the existing password):
  only orders made and checked, oldest first, with Label Generated (`logistics_api.pack_list`).
* **Customer help** — port 8003 (`HELP`, its own password): search, status, tracking, notes, WhatsApp,
  name check, Express, cancel and remake requests (the logistics page of 0.23–0.25).

Signed-in only. Each page may read and change only what its lists say; everything else is refused, and
changes need the shared records reachable (0.23, owner decision)."""
from __future__ import annotations

import re
import socket
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth

PACK_READS = re.compile(r"^/(api/pack/list|api/logistics/meta)?$")
PACK_WRITES = re.compile(r"^/api/(logistics/order/[^/]+/label|usage)$")
HELP_READS = re.compile(r"^/(api/logistics/(orders|counts|meta|orders\.csv|order/[^/]+))?$")
HELP_WRITES = re.compile(r"^/api/(logistics/order/[^/]+/(label|express|name|note|cancel-request|remake)"
                         r"|orders/[^/]+/(use-returned|undo-returned)|usage)$")
READS, WRITES = HELP_READS, HELP_WRITES  # (0.23 names)

PORT = 8002
HELP_PORT = 8003


class Site:
    """One page on its own port, password and cookie."""

    def __init__(self, mode: str, label: str, keys: auth.Keys, setting: str, reads, writes, port_attr: str) -> None:
        self.mode, self.label, self.keys, self.setting = mode, label, keys, setting
        self.reads, self.writes, self.port_attr = reads, writes, port_attr
        self.listener = self.thread = None
        self.error = ""
        self.lock = threading.Lock()
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.middleware("http")(self.guard)
        self.app.add_api_route("/", self.page, methods=["GET"])
        self.app.add_api_route("/login", self.login_page, methods=["GET"])
        self.app.add_api_route("/api/login", self.login, methods=["POST"])

    @property
    def port(self) -> int:
        return globals()[self.port_attr]

    def on(self) -> bool:
        from . import server
        return bool(getattr(server.SETTINGS, self.setting)) and self.keys.enabled()

    async def guard(self, request: Request, call_next):
        if not self.on():
            return JSONResponse({"detail": f"The {self.label} page is disabled"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        path = request.url.path
        if path in ("/login", "/api/login"):
            return await call_next(request)
        if not self.keys.valid_cookie(request.cookies.get(self.keys.cookie)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            return RedirectResponse("/login")
        if request.method == "GET" and self.reads.match(path):
            pass
        elif request.method == "POST" and self.writes.match(path):
            from . import sync
            if sync.REQUIRED and not sync.online():
                return JSONResponse({"detail": sync.offline_reason()}, status_code=503)
        else:
            return JSONResponse({"detail": f"Not available on the {self.label} page"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def page(self):
        from .server import STATIC
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8")
                            .replace("const LOGISTICS=false;", f"const LOGISTICS='{self.mode}';"))

    def login_page(self):
        from .server import STATIC
        return FileResponse(STATIC / "login.html")

    async def login(self, req: Request):
        from .server import SECURE_COOKIE
        body = await req.json()
        if not self.keys.check_password(str(body.get("password", ""))):
            raise HTTPException(401, "Wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(self.keys.cookie, self.keys.make_cookie(), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, secure=SECURE_COOKIE and req.url.scheme == "https", samesite="lax")
        return resp

    # ---- listener ----
    def status(self) -> dict:
        running = bool(self.listener and self.thread and self.thread.is_alive() and self.listener.started
                       and not self.listener.should_exit)
        return {"listening_on_network": running, "error": self.error}

    def stop(self) -> None:
        with self.lock:
            if self.listener:
                self.listener.should_exit = True
            if self.thread:
                self.thread.join(timeout=3)
            self.listener = self.thread = None

    def start(self):
        """Start on Save as well as at startup; report bind failures to local Settings."""
        if not self.on():
            return None
        import uvicorn
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.listener
            self.error = ""
            port = self.port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("0.0.0.0", port))
                sock.listen(128)
                sock.setblocking(False)
            except OSError:
                sock.close()
                self.error = (f"Cannot open port {port} for the {self.label} page. Another program may be using it, "
                              "or Windows may be blocking it.")
                return None
            listener = uvicorn.Server(uvicorn.Config(self.app, host="0.0.0.0", port=port, log_level="warning",
                                                     access_log=False))

            def run():
                try:
                    listener.run(sockets=[sock])
                except (Exception, SystemExit):
                    self.error = f"The {self.label} listener stopped. Try disabling and enabling it again."
                finally:
                    sock.close()
            self.listener = listener
            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()
            deadline = time.monotonic() + 2
            while not listener.started and self.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            return listener


PACK = Site("pack", "packing", auth.MAIN, "lan_access", PACK_READS, PACK_WRITES, "PORT")
HELP = Site("help", "customer help", auth.HELP, "help_access", HELP_READS, HELP_WRITES, "HELP_PORT")
app = PACK.app  # (tests, 0.23 name)

# Explicit route registration: never mount the full app on a LAN listener.
from . import server  # noqa: E402
from . import logistics_api  # noqa: E402

for _site in (PACK, HELP):
    _site.app.add_api_route("/api/orders/{order_id}/use-returned", server.use_returned, methods=["POST"])
    _site.app.add_api_route("/api/orders/{order_id}/undo-returned", server.undo_returned, methods=["POST"])
    logistics_api.register(_site.app)


def status():
    return PACK.status()


def start():
    HELP.start()
    return PACK.start()


def stop():
    PACK.stop()
    HELP.stop()
