"""Storefront test server: a local stand-in for the VPS render service.

Serves the storefront test page and a small public API in front of SnapSTL's Models renderer
(change/local-parametric-model). Customers can set only the text and the colours; every other
parameter stays at the design's own defaults (or the approved values once a design is developed).

Run with ./run.sh on the Mac. Never exposes the SnapSTL portal or its data.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("SNAPSTL_MODELS_DIR", str(HERE / "branch" / "models"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from snapstl import model_studio  # noqa: E402
from snapstl.settings import load_settings  # noqa: E402

# Filament colours SnapSTL already knows (farm.COLOR_HEX), the ones offered to customers.
PALETTE = {"pink": "#F4A7C0", "hot pink": "#FF4FA0", "baby pink": "#F9C9D9", "lavender": "#B9A3E3",
           "red": "#E5534B", "orange": "#F39A3D", "yellow": "#F2D04B", "green": "#5FB36A",
           "blue": "#4A86D9", "sky blue": "#7CC4F0", "purple": "#9A6BD1", "black": "#000000",
           "white": "#FFFFFF", "grey": "#999999"}

# What the store tests for now (the organizer only), mapped to the Models package, design and the only inputs a customer sets.
PRODUCTS = {
    "organizer": {"label": "Name desk organizer", "model": "name-desk-organizer", "design": "letter-contour",
                  "max": 15, "colors": {"text": ["Name color", "text_color", "pink"]}},
}

SETTINGS = load_settings()
ORDERS = HERE / "test-orders.jsonl"
app = FastAPI(title="Snap Magicians storefront test", docs_url=None, redoc_url=None, openapi_url=None)


class PreviewBody(BaseModel):
    product: str
    text: str = Field(min_length=1, max_length=28)
    text_color: str = ""
    base_color: str = ""


class OrderBody(PreviewBody):
    quantity: int = Field(default=1, ge=1, le=20)
    preview_key: str = Field(default="", max_length=24)


def _revision(model_id: str, design_id: str) -> str:
    for model in model_studio.catalog(SETTINGS.model)["models"]:
        if model["id"] == model_id:
            for design in model["designs"]:
                if design["id"] == design_id:
                    return design["revision"]
    raise HTTPException(404, "This product's model is not in the catalog.")


def _values(body: PreviewBody) -> tuple[dict, dict]:
    product = PRODUCTS.get(body.product)
    if product is None:
        raise HTTPException(404, "Unknown product.")
    text = body.text.strip()
    if not text or len(text) > product["max"] or any(ord(c) < 32 for c in text):
        raise HTTPException(400, f"Text must be 1 to {product['max']} characters.")
    values = {"text": text}
    for key, (_, param, default) in product["colors"].items():
        name = (getattr(body, f"{key}_color") or default).lower()
        if name not in PALETTE:
            raise HTTPException(400, "Pick one of the listed colors.")
        values[param] = PALETTE[name].lower()
    return product, values


@app.get("/")
def page():
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def config():
    cat = model_studio.catalog(SETTINGS.model)
    return {"renderer": cat["renderer"], "renderer_error": cat["renderer_error"], "palette": PALETTE,
            "products": [{"key": k, "label": p["label"], "max": p["max"],
                          "colors": {c: {"label": v[0], "default": v[2]} for c, v in p["colors"].items()}}
                         for k, p in PRODUCTS.items()]}


@app.post("/api/preview")
async def preview(body: PreviewBody):
    product, values = _values(body)
    request = model_studio.PreviewRequest(model_id=product["model"], design_id=product["design"],
                                          revision=_revision(product["model"], product["design"]), values=values)
    started = time.monotonic()
    try:
        result = await run_in_threadpool(model_studio.preview, request, SETTINGS.scaling, SETTINGS.model)
    except model_studio.ModelError as error:
        busy = "rendering another" in str(error)
        raise HTTPException(409 if busy else 400, str(error)) from error
    for plate in result["plates"]:
        plate["url"] = f"/api/stl/{result['key']}/{plate['id']}.stl"
    return {"key": result["key"], "plates": result["plates"], "seconds": round(time.monotonic() - started, 1)}


@app.get("/api/stl/{key}/{part}.stl")
def stl(key: str, part: str):
    data = model_studio.artifact(key, part)
    if data is None:
        raise HTTPException(404, "This preview expired. Type again to rebuild it.")
    return Response(data, media_type="model/stl", headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/test-order")
def test_order(body: OrderBody):
    product, values = _values(body)
    order = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "product": body.product, "model": product["model"],
             "design": product["design"], "values": values, "quantity": body.quantity, "preview_key": body.preview_key}
    with ORDERS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(order) + "\n")
    return {"ok": True, "order": order}


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--lan", action="store_true", help="also reachable from phones on the same Wi-Fi")
    parser.add_argument("--port", type=int, default=8400)
    args = parser.parse_args()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    print(f"\nStorefront test: http://localhost:{args.port}")
    if args.lan:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            print(f"On your phone (same Wi-Fi): http://{s.getsockname()[0]}:{args.port}")
        except OSError:
            pass
        finally:
            s.close()
    print("Test orders are saved to storefront-test/test-orders.jsonl. Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
