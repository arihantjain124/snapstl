"""How long a plate takes to print (0.35, owner): learned from the prints already done.

For each finished print (`print_jobs` done, with `started_at`), minutes = finish − start, the plate's letters are
counted from its names, and the time is brought to Sport speed (124 %, how the farm runs). A straight line
**minutes = a + b × letters** is fitted per plate kind (bases / letters / stands) and per printer (once it has
`PER_PRINTER` prints), else per printer model (A1, P1S …), else for every printer; prints far off the line
(a plate stopped and resumed, a print left on the bed) are dropped once and the line fitted again.

The Print page shows, for the printer picked: ≈ duration at the plate's speed, and when it would be ready if
started now (after that printer's current print). Stats shows minutes per letter for each printer.
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import db

# Bambu speed levels → % of standard; the farm prints at Sport (124 %)
SPEED_PCT = {1: 50, 2: 100, 3: 124, 4: 166, "silent": 50, "standard": 100, "sport": 124, "ludicrous": 166}
BASE_PCT = 124
PER_PRINTER = 10   # a printer's own line once it has this many prints of a kind
PER_MODEL = 5      # else its model's
MIN_FIT = 5        # else every printer's; fewer than this → no estimate
_TTL = 600
_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "model": None}


def letters(names) -> int:
    """Letters on a plate: every non-space character of its names (a fix plate's '(abc)' = only those)."""
    n = 0
    for x in names or []:
        x = x or ""
        m = re.search(r"\(([^)]*)\)\s*$", x)
        n += len(re.sub(r"\s", "", m.group(1) if m else x))
    return n


def _fit(pts: list[tuple[int, float]]) -> tuple[float, float] | None:
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return (0.0, my / mx) if mx else None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def _robust(pts: list[tuple[int, float]]) -> dict | None:
    f = _fit(pts)
    if f is None:
        return None
    res = sorted(abs(y - (f[0] + f[1] * x)) for x, y in pts)
    mad = res[len(res) // 2] or 1.0
    keep = [(x, y) for x, y in pts if abs(y - (f[0] + f[1] * x)) <= 4 * mad]
    f2 = _fit(keep) or f
    a, b = f2
    if b <= 0:  # too few letters' spread: a plain average per letter
        tot = sum(x for x, _ in keep)
        a, b = 0.0, (sum(y for _, y in keep) / tot if tot else 0.0)
    err = sorted(abs(y - (a + b * x)) for x, y in keep)
    return {"a": round(a, 1), "b": round(b, 2), "n": len(keep), "err": round(err[len(err) // 2], 1) if err else 0.0}


def _plates(con) -> dict[tuple[int, str], list[str]]:
    out: dict[tuple[int, str], list[str]] = {}
    for bid, s in con.execute("SELECT id, summary FROM batches"):
        try:
            for p in json.loads(s or "{}").get("plates") or []:
                out[(bid, p["file"])] = p.get("names") or []
        except (TypeError, ValueError):
            continue
    for bid, f, items in con.execute("SELECT batch_id, file, items FROM extra_plates"):
        try:
            out[(bid, f)] = [(i.get("name") or "") + (f" ({i['letters']})" if i.get("letters") else "")
                             for i in json.loads(items or "[]")]
        except (TypeError, ValueError):
            continue
    return out


def build(con, printers: dict[int, str] | None = None) -> dict:
    """The fitted lines: {kind: {"all": line, "model": {model: line}, "printer": {id: line}}}."""
    plates = _plates(con)
    printers = printers or {}
    pts: dict[str, dict] = {}
    for bid, plate, kind, pid, st, up, sp in con.execute(
            "SELECT batch_id, plate, kind, printer_id, started_at, updated_at, speed FROM print_jobs "
            "WHERE state='done' AND started_at IS NOT NULL AND updated_at > started_at"):
        names = plates.get((bid, plate))
        if not names:
            continue
        n = letters(names)
        mins = (up - st) / 60 * SPEED_PCT.get(sp or 3, BASE_PCT) / BASE_PCT
        if n <= 0 or mins < 3 or mins > 30 * 60:
            continue
        k = pts.setdefault(kind, {"all": [], "model": {}, "printer": {}})
        k["all"].append((n, mins))
        k["printer"].setdefault(pid, []).append((n, mins))
        if printers.get(pid):
            k["model"].setdefault(printers[pid], []).append((n, mins))
    out: dict[str, dict] = {}
    for kind, k in pts.items():
        out[kind] = {
            "all": _robust(k["all"]) if len(k["all"]) >= MIN_FIT else None,
            "model": {m: f for m, v in k["model"].items() if len(v) >= PER_MODEL and (f := _robust(v))},
            "printer": {p: f for p, v in k["printer"].items() if len(v) >= PER_PRINTER and (f := _robust(v))},
        }
    return out


def model(printers: list[dict] | None = None) -> dict:
    """The lines, kept for 10 minutes (reading every batch's plates takes a moment)."""
    with _LOCK:
        if _CACHE["model"] is not None and time.time() - _CACHE["at"] < _TTL:
            return _CACHE["model"]
    pm = {int(p["id"]): str(p.get("model") or "") for p in (printers or []) if p.get("id") is not None}
    with db.tx() as con:
        m = build(con, pm)
    with _LOCK:
        _CACHE.update(at=time.time(), model=m)
    return m


def forget() -> None:
    with _LOCK:
        _CACHE["model"] = None


def minutes(m: dict, kind: str, n: int, printer_id=None, printer_model: str = "") -> dict | None:
    """≈ minutes at Sport speed for `n` letters on this printer: {min, err, basis} or None (not enough prints)."""
    k = m.get(kind) or {}
    line, basis = None, ""
    if printer_id is not None and printer_id in (k.get("printer") or {}):
        line, basis = k["printer"][printer_id], "this printer"
    elif printer_model and printer_model in (k.get("model") or {}):
        line, basis = k["model"][printer_model], f"{printer_model} printers"
    elif k.get("all"):
        line, basis = k["all"], "all printers"
    if not line or n <= 0:
        return None
    return {"min": round(line["a"] + line["b"] * n), "err": line["err"], "basis": basis, "prints": line["n"]}


def table(printers: list[dict]) -> list[dict]:
    """Stats: each printer's minutes per letter (bases / letters), with how many prints it's from."""
    m = model(printers)
    out = []
    for p in printers:
        row = {"printer": p.get("name") or str(p.get("id")), "model": p.get("model") or ""}
        for kind in ("bases", "names", "stands"):
            k = m.get(kind) or {}
            line = (k.get("printer") or {}).get(int(p["id"]))
            row[kind] = line
        out.append(row)
    return out
