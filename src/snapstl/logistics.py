"""Optional LAN listener for Mac and Windows. Only authenticated order reads are routed here."""
from __future__ import annotations

import threading
import socket
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


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
    if request.method != 'GET' or path not in ('/', '/api/orders', '/api/orders/facets', '/api/orders.csv'):
        return JSONResponse({'detail': 'Logistics access is read only'}, status_code=403)
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
app.add_api_route('/api/orders', server.list_orders, methods=['GET'])
app.add_api_route('/api/orders/facets', server.order_facets, methods=['GET'])
app.add_api_route('/api/orders.csv', server.export_orders, methods=['GET'])


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
            sock.bind(('0.0.0.0', 8002))
            sock.listen(128)
            sock.setblocking(False)
        except OSError:
            sock.close()
            _ERROR = "Cannot open logistics port 8002. Another program may be using it, or Windows may be blocking it."
            return None
        listener = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=8002,
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
