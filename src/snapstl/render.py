"""Small dependency-free renderer for previews (PNG bytes).

Points are sampled on the mesh surface and splatted into an orthographic height map; each pixel
shows the sample nearest the viewer, shaded by height and slope. Faces flagged in `marks` (e.g. the
MakerWorld watermark) are painted red so they're easy to spot.

Used for: base plate previews seen from the top, and per-model back-view thumbnails.
"""
from __future__ import annotations

import struct
import zlib

import numpy as np
import trimesh

BG = np.array([236, 235, 230], dtype=float)
PART = np.array([250, 250, 248], dtype=float)
MARK = np.array([220, 40, 40], dtype=float)


def png_rgb(img: np.ndarray) -> bytes:
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].astype(np.uint8).tobytes() for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(parts: list[tuple[trimesh.Trimesh, np.ndarray | None]], view: str = "top",
           bounds: tuple[float, float, float, float] | None = None, px: float = 0.4,
           max_samples: int = 4_000_000) -> bytes:
    """Render meshes (with optional boolean face masks to paint red) to a PNG.

    view="top":  looking down −Z; image X = world X, image up = world +Y.
    view="back": looking at the +Y face (from behind); image X = world −X, image up = world +Z.
    bounds: (u_min, u_max, v_min, v_max) of the image in world units (default: fit the parts).
    """
    us, vs, ds, mk, lt = [], [], [], [], []
    total_area = sum(m.area for m, _ in parts) or 1.0
    per_mm2 = 3.0 / (px * px)  # ~3 samples per pixel of surface
    for mesh, mask in parts:
        n = int(min(max_samples * mesh.area / total_area, max(20_000, mesh.area * per_mm2)))
        pts, fi = trimesh.sample.sample_surface(mesh, n, seed=1)
        nrm = mesh.face_normals[fi]
        if view == "top":
            u, v, d, facing = pts[:, 0], pts[:, 1], pts[:, 2], nrm[:, 2]
        else:
            u, v, d, facing = -pts[:, 0], pts[:, 2], pts[:, 1], nrm[:, 1]
        us.append(u); vs.append(v); ds.append(d); lt.append(np.abs(facing))
        mk.append(mask[fi] if mask is not None else np.zeros(len(fi), bool))
    u = np.concatenate(us); v = np.concatenate(vs); d = np.concatenate(ds); m = np.concatenate(mk)
    f = np.concatenate(lt)
    if bounds is None:
        pad = 2.0
        bounds = (u.min() - pad, u.max() + pad, v.min() - pad, v.max() + pad)
    u0, u1, v0, v1 = bounds
    W = max(1, int((u1 - u0) / px)); H = max(1, int((v1 - v0) / px))
    xi = np.clip(((u - u0) / px).astype(int), 0, W - 1)
    yi = np.clip(((v1 - v) / px).astype(int), 0, H - 1)
    flat = yi * W + xi
    order = np.argsort(d)  # nearest to the viewer written last → wins
    top = np.full(W * H, -1, dtype=np.int64)
    top[flat[order]] = order
    img = np.tile(BG, (H * W, 1))
    hit = top >= 0
    dd = np.full(W * H, np.nan)
    dd[hit] = d[top[hit]]
    lo, hi = np.nanmin(dd), np.nanmax(dd)
    shade = np.zeros(W * H)
    shade[hit] = (dd[hit] - lo) / max(1e-6, hi - lo)
    light = np.ones(W * H)
    light[hit] = 0.55 + 0.45 * f[top[hit]]  # surfaces facing the viewer are brighter
    base_col = PART * (0.6 + 0.4 * shade[:, None]) * light[:, None]
    img[hit] = base_col[hit]
    marked = np.zeros(W * H, bool)
    marked[hit] = m[top[hit]]
    img[marked] = MARK
    return png_rgb(np.clip(img, 0, 255).reshape(H, W, 3))
