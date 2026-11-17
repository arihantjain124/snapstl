"""Optional LAN listener for Mac and Windows: the logistics page (label and customer-care team).

Signed-in only. Reads: the logistics lists, search and order details (`logistics_api`). Changes: only
the ones in WRITES (Label Generated / Undo, Express, name change confirmed with the customer, call
notes, customer wants to cancel, remake request, use a returned piece, usage counts), and only while
the shared records are reachable. Everything else is refused here (0.23, owner decision)."""
from __future__ import annotations

import re
import threading
import socket
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
READS = re.compile(r"^/(api/logistics/(orders|counts|meta|orders\.csv|order/[^/]+))?$")
WRITES = re.compile(r"^/api/(logistics/order/[^/]+/(label|express|name|note|cancel-request|remake)"
                    r"|orders/[^/]+/(use-returned|undo-returned)|usage)$")


@app.middleware('http')
async def guard(request: Request, call_next):
    from . import server
    if not (server.SETTINGS.lan_access and auth.enabled()):
        return JSONResponse({'detail': 'Logistics is disabled'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin.split('://', 1)[-1] != request.headers.get('host'):
        return JSONResponse({'detail': 'Cross-site request blocked'}, status_code=403)
    path = request.url.path
    if path in ('/login', '/api/login'):
        return await call_next(request)
    if not auth.valid_cookie(request.cookies.get(auth.COOKIE)):
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Sign in required'}, status_code=401)
        return RedirectResponse('/login')
    if request.method == 'GET' and READS.match(path):
        pass
    elif request.method == 'POST' and WRITES.match(path):
        from . import sync
        if sync.REQUIRED and not sync.online():
            return JSONResponse({'detail': sync.offline_reason()}, status_code=503)
    else:
        return JSONResponse({'detail': 'Not available on the logistics page'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def page():
    from .server import STATIC
    return HTMLResponse((STATIC / 'index.html').read_text(encoding="utf-8").replace('const LOGISTICS=false;', 'const LOGISTICS=true;'))


# Explicit route registration: never mount the full app on the LAN listener.
from . import server
app.add_api_route('/login', server.login_page, methods=['GET'])
app.add_api_route('/api/login', server.login, methods=['POST'])
app.add_api_route('/api/orders/{order_id}/use-returned', server.use_returned, methods=['POST'])
app.add_api_route('/api/orders/{order_id}/undo-returned', server.undo_returned, methods=['POST'])
from . import logistics_api  # noqa: E402
logistics_api.register(app)


PORT = 8002
_LOCK = threading.Lock()
_LISTENER = None
_THREAD = None
_ERROR = ""


def status():
    running = bool(_LISTENER and _THREAD and _THREAD.is_alive() and _LISTENER.started
                   and not _LISTENER.should_exit)
    return {"listening_on_network": running, "error": _ERROR}


def stop():
    global _LISTENER, _THREAD
    with _LOCK:
        if _LISTENER:
            _LISTENER.should_exit = True
        if _THREAD:
            _THREAD.join(timeout=3)
        _LISTENER = _THREAD = None


def start():
    """Start on Save as well as startup; report bind failures to local Settings."""
    global _LISTENER, _THREAD, _ERROR
    if not (server.SETTINGS.lan_access and auth.enabled()):
        return None
    import uvicorn
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return _LISTENER
        _ERROR = ""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('0.0.0.0', PORT))
            sock.listen(128)
            sock.setblocking(False)
        except OSError:
            sock.close()
            _ERROR = "Cannot open logistics port 8002. Another program may be using it, or Windows may be blocking it."
            return None
        listener = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=PORT,
                                                 log_level='warning', access_log=False))
        def run():
            global _ERROR
            try:
                listener.run(sockets=[sock])
            except (Exception, SystemExit):
                _ERROR = "The logistics listener stopped. Try disabling and enabling logistics again."
            finally:
                sock.close()
        _LISTENER = listener
        _THREAD = threading.Thread(target=run, daemon=True)
        _THREAD.start()
        deadline = time.monotonic() + 2
        while not listener.started and _THREAD.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        return listener
