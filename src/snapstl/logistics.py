"""Optional Windows LAN listener. Only authenticated order reads are routed here."""
from __future__ import annotations

import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth, platform_os

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
    return HTMLResponse((STATIC / 'index.html').read_text().replace('const LOGISTICS=false;', 'const LOGISTICS=true;'))


# Explicit route registration: never mount the full app on the LAN listener.
from . import server
app.add_api_route('/login', server.login_page, methods=['GET'])
app.add_api_route('/api/login', server.login, methods=['POST'])
app.add_api_route('/api/orders', server.list_orders, methods=['GET'])
app.add_api_route('/api/orders/facets', server.order_facets, methods=['GET'])
app.add_api_route('/api/orders.csv', server.export_orders, methods=['GET'])


def start():
    if not (platform_os.WINDOWS and server.SETTINGS.lan_access and auth.enabled()):
        return None
    import uvicorn
    listener = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=8002, log_level='warning', access_log=False))
    threading.Thread(target=listener.run, daemon=True).start()
    return listener
