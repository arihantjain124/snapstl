"""Merged plates and fix plates (0.24), made from the batch's own plate files — no download, no scaling.

* **Fix plate** (Print → Verify → Problem): only the spoiled letters of a name, a whole name, or a base,
  cut from the plate it was printed on (already scaled), packed onto a small new plate in the same
  batch folder under `Fixes/`. A name's letters are the separate pieces of its mesh, left to right; a
  small piece (the dot of an i) goes with the nearest letter; letters that touch are one piece.
* **Merged plate** (Print → Letters → tick plates → Merge): name plates of one colour, not started yet, up to 3
  names in all — also letter **fix** plates (0.25.1: their pieces are taken as they are on the fix plate) —
  packed onto one plate saved in the newest of their batch folders under `Merged/`. Each item records the
  plate it came from (`src`, `src_batch`) so Split back returns exactly those plates. The single
  plates leave the list (print job state `merged`, nothing deleted); **Split back** returns them while
  the merged plate isn't started.

Both are rows of `extra_plates` (see verify.py for how items, checks and orders follow them).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import trimesh

from . import computer, db, verify
from .export import _unique, check_written, clean_part, preview
from .grouping import Plate
from .packing import make_part, pack, placed_part_meshes
from .settings import PlateSettings, Settings, load_settings
from .threemf import Object3MF, read_3mf, write_3mf

MAX_MERGE = 3
DOT_SHARE = 0.25  # a piece smaller than this share of the median letter is a dot / accent


class ExtraError(Exception):
    pass


# ---------------- reading items back from their plate ----------------
def _plate_cfg(b: dict) -> PlateSettings:
    try:
        return Settings.from_dict(json.loads(b.get("settings") or "{}")).plate
    except (TypeError, ValueError):
        return load_settings().plate


def _plates(b: dict) -> list[dict]:
    return json.loads(b.get("summary") or "{}").get("plates") or []


def _word(part: str) -> str:
    return "Base" if part == "bases" else "Name"


def item_mesh(b: dict, it: dict) -> tuple[trimesh.Trimesh, int]:
    """The item's mesh as written on its own plate (scaled, in plate position) and its rotation there."""
    path = Path(b.get("out_dir") or "") / it["plate"]
    if not path.is_file():
        raise ExtraError(f"The plate file is gone ({it['plate']}) — make a new batch for this order")
    objs = read_3mf(path)
    word = _word(it["part"])
    plate = next((p for p in _plates(b) if p["file"] == it["plate"]), {})
    ids = list(plate.get("order_ids") or [])
    k = sum(1 for i in ids[: int(it["idx"])] if i == it.get("order_id"))  # copy number of this order on the plate
    oid = it.get("order_id") or ""
    same = [o for o in objs if oid and o.name.endswith(f"· {oid}") and f" - {word} ·" in o.name]
    if not same:  # older plates: match by name
        same = [o for o in objs if o.name.startswith(f"{it.get('name')} - {word}")]
    if not same:
        raise ExtraError(f"{it.get('name')}: not found on {Path(it['plate']).name}")
    o = same[min(k, len(same) - 1)]
    rot = 0
    for part in (plate.get("preview") or {}).get("parts", []):
        if f" - {word} (" in part["key"] and (f"({oid}·" in part["key"] or part["key"].startswith(f"{it.get('name')} - ")):
            rot = int(part.get("rotation") or 0)
            break
    return trimesh.Trimesh(np.asarray(o.vertices), np.asarray(o.faces), process=True), rot


def _components(m: trimesh.Trimesh) -> list[trimesh.Trimesh]:
    """Separate bodies of a mesh (letters), without scipy / networkx."""
    m = m.copy()
    m.merge_vertices()
    parent = np.arange(len(m.vertices))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b_, c in m.faces:
        ra, rb, rc = find(a), find(b_), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    roots = np.array([find(v) for v in range(len(m.vertices))])
    face_root = roots[m.faces[:, 0]]
    out = []
    for r in np.unique(face_root):
        sub = m.submesh([np.nonzero(face_root == r)[0]], append=True)
        out.append(sub)
    return out


def pieces(b: dict, it: dict) -> list[dict]:
    """A name's letters as the person sees them from the top, left to right: [{n, label, mesh, rings}].
    Small pieces (dots, accents) join the nearest letter; letters that touch are one piece."""
    m, rot = item_mesh(b, it)
    m = m.copy()
    c = m.bounds.mean(axis=0)
    m.apply_translation([-c[0], -c[1], 0])
    if rot % 360:
        m.apply_transform(trimesh.transformations.rotation_matrix(np.radians(-rot), [0, 0, 1]))
    comps = _components(m)
    area = [float(np.prod(x.extents[:2])) for x in comps]
    med = float(np.median(area)) if area else 0.0
    big = [i for i, a in enumerate(area) if a >= DOT_SHARE * med] or list(range(len(comps)))
    groups: dict[int, list[int]] = {i: [i] for i in big}
    for i in range(len(comps)):
        if i in groups:
            continue
        cx = comps[i].bounds.mean(axis=0)[0]
        near = min(big, key=lambda j: abs(comps[j].bounds.mean(axis=0)[0] - cx))
        groups[near].append(i)
    order = sorted(groups, key=lambda i: comps[i].bounds[0][0])
    letters = [ch for ch in (it.get("name") or "") if not ch.isspace()]
    out = []
    for n, g in enumerate(order):
        mesh = trimesh.util.concatenate([comps[i] for i in groups[g]])
        out.append({"n": n, "label": letters[n] if len(letters) == len(order) else str(n + 1), "mesh": mesh,
                    "rings": _rings(mesh)})
    return out


def _rings(mesh: trimesh.Trimesh) -> list[list[list[float]]]:
    from .packing import footprint

    fp = footprint(mesh).simplify(0.4)
    rings = []
    for g in getattr(fp, "geoms", [fp]):
        rings.append(np.round(np.asarray(g.exterior.coords), 1).tolist())
        for h in g.interiors:
            rings.append(np.round(np.asarray(h.coords), 1).tolist())
    return rings


# ---------------- writing a plate ----------------
def _title(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in (s or "").split()) or "No colour"


def _write(folder: Path, stem: str, parts, cfg: PlateSettings, kind: str) -> tuple[str, dict]:
    res = pack(parts, cfg, try_orders=len(parts) <= 4)
    if not res.ok:
        raise ExtraError(f"Doesn't fit on one plate: {res.reason}")
    folder.mkdir(parents=True, exist_ok=True)
    stem = clean_part(stem)[:180] or "plate"
    path = _unique(folder / f"{stem}.3mf", set())
    objs = [Object3MF(k, m.vertices, m.faces) for k, m in placed_part_meshes(parts, res)]
    write_3mf(path, objs, title=path.stem)
    problems, _ = check_written(path, objs, cfg)
    if problems:
        path.unlink(missing_ok=True)
        raise ExtraError("; ".join(problems))
    return path.name, preview(Plate(kind, [], parts, res), cfg)


def _record(con, batch_id: int, file: str, kind: str, part: str, color: str, items: list[dict], prev: dict,
            names: str, staff: str = "", at: float | None = None) -> int:
    cur = con.execute("INSERT INTO extra_plates(uid, batch_id, file, kind, part, color, names, items, preview, created_at, "
                      "state, staff) VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?)",
                      (uuid.uuid4().hex, batch_id, file, kind, part, color, names, json.dumps(items), json.dumps(prev),
                       at if at is not None else db.now(), staff or None))
    pid = int(cur.lastrowid)
    con.executemany("INSERT INTO extra_items(plate_id, batch_id, plate, idx) VALUES (?,?,?,?)",
                    [(pid, int(i["batch_id"]), i["plate"], int(i["idx"])) for i in items])
    return pid


def _own(con, batch_id: int) -> dict:
    b = con.execute("SELECT * FROM batches WHERE id=? AND COALESCE(computer_id, ?)=?",
                    (batch_id, computer.ID, computer.ID)).fetchone()
    if b is None:
        raise ExtraError("This batch isn't on this computer")
    return dict(b)


def make_fixes(con, folder_batch: int, wanted: list[dict], staff: str, at: float) -> list[str]:
    """Fix plates for spoiled items: wanted = [{item, pieces: [n…] | None}]. Items of one part and colour
    share a plate when they fit. Returns the files made (relative to the folder batch)."""
    fb = _own(con, folder_batch)
    cfg = _plate_cfg(fb)
    groups: dict[tuple[str, str], list[dict]] = {}
    for w in wanted:
        it = w["item"]
        groups.setdefault((it["part"], (it.get("color") or "").lower() if it["part"] == "names" else ""), []).append(w)
    files = []
    for (part, color), ws in groups.items():
        built = []  # (wanted, parts)
        for w in ws:
            it = w["item"]
            ib = _own(con, int(it["batch_id"]))
            tag = it.get("order_id") or ""
            if w.get("pieces"):
                ps = [p for p in pieces(ib, it) if p["n"] in set(w["pieces"])]
                if not ps:
                    raise ExtraError(f"{it.get('name')}: choose the spoiled letters")
                w["letters"] = "".join(p["label"] for p in ps)
                parts = [make_part(f"{it.get('name')} - letter {p['label']} · {tag} #{p['n'] + 1}", p["mesh"]) for p in ps]
            else:
                m, _ = item_mesh(ib, it)
                parts = [make_part(f"{it.get('name')} - {_word(part)} · {tag}", m)]
            built.append((w, parts))
        # one plate for the group if it fits, else one per item
        made = None
        if len(built) > 1:
            try:
                made = [(built, _write_fix(fb, cfg, part, color, built))]
            except ExtraError:
                made = None
        if made is None:
            made = [([x], _write_fix(fb, cfg, part, color, [x])) for x in built]
        for chunk, (file, prev, names) in made:
            items = [{**w["item"], "letters": w.get("letters") or ""} for w, _ in chunk]
            _record(con, folder_batch, file, "fix", part, color or (chunk[0][0]["item"].get("color") or ""), items,
                    prev, names, staff, at)
            files.append(file)
    return files


def _write_fix(fb: dict, cfg: PlateSettings, part: str, color: str, chunk) -> tuple[str, dict, str]:
    parts = [p for _, ps in chunk for p in ps]
    labels = []
    for w, _ in chunk:
        nm = w["item"].get("name") or "?"
        labels.append(f"{nm} ({w['letters']})" if w.get("letters") else nm)
    names = " + ".join(labels)
    head = "Bases" if part == "bases" else _title(color or chunk[0][0]["item"].get("color") or "")
    file, prev = _write(Path(fb["out_dir"]) / "Fixes", f"Fix - {head} - {names}", parts, cfg, part)
    return f"Fixes/{file}", prev, names


# ---------------- merge ----------------
def _latest_state(con, batch_id: int, file: str) -> str | None:
    r = con.execute("SELECT state FROM print_jobs WHERE batch_id=? AND plate=? AND computer_id=? ORDER BY id DESC LIMIT 1",
                    (batch_id, file, computer.ID)).fetchone()
    return r[0] if r else None


MERGEABLE = (None, "reprint", "unmerged")  # not started (or back in the list after a failed print)


def merge(plates: list[dict], staff: str = "") -> dict:
    """Merge letter plates [{batch_id, file}] of one colour (≤ 3 names in all; letter fix plates too) onto one plate."""
    from .makeit import _batch_orders, plate_order_ids

    if len(plates) < 2:
        raise ExtraError("Choose at least two plates to merge")
    with db.tx() as con:
        items, colors, batches, parts = [], set(), {}, []
        for p in plates:
            b = _own(con, int(p["batch_id"]))
            batches[b["id"]] = b
            pl = next((x for x in _plates(b) if x["file"] == p["file"]), None)
            fix = None
            if pl is None:
                fix = con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND file=? AND state='active'",
                                  (b["id"], p["file"])).fetchone()
            if fix is not None and (fix["kind"] != "fix" or fix["part"] != "names"):
                fix = None
            if (pl is None or pl["kind"] != "names") and fix is None:
                raise ExtraError("Only letter plates (and letter fix plates) can be merged")
            file = pl["file"] if pl else fix["file"]
            if _latest_state(con, b["id"], file) not in MERGEABLE:
                raise ExtraError(f"{Path(file).name} is already started, printed, removed or merged")
            src = {"src": file, "src_batch": b["id"]}
            if fix is not None:
                colors.add((fix["color"] or "").lower())
                fitems = json.loads(fix["items"] or "[]")
                state = {r[0]: r[1] for r in con.execute(
                    f"SELECT order_id, state FROM orders WHERE order_id IN ({','.join('?' * len(fitems))})",
                    [i.get("order_id") for i in fitems])} if fitems else {}
                fitems = [i for i in fitems if state.get(i.get("order_id")) == "bundled"]
                ids = {i.get("order_id") for i in fitems}
                path = Path(b.get("out_dir") or "") / file
                if not path.is_file():
                    raise ExtraError(f"The fix plate file is gone ({Path(file).name})")
                for o in read_3mf(path):  # the fix plate's pieces as they are (already cut and scaled)
                    if any(f"· {oid}" in o.name for oid in ids):
                        parts.append(make_part(o.name, trimesh.Trimesh(np.asarray(o.vertices), np.asarray(o.faces),
                                                                       process=True)))
                items += [{**i, **src} for i in fitems]
                continue
            colors.add(((pl.get("colors") or [""])[0] or "").lower())
            orders = _batch_orders(con, b)
            state = {o["order_id"]: o["state"] for o in orders}
            on = plate_order_ids(_plates(b), orders).get(pl["file"], [])
            for it in verify.plate_items(b["id"], pl, on):
                if state.get(it["order_id"]) == "bundled":
                    m, _ = item_mesh(b, it)
                    parts.append(make_part(f"{it['name']} - Name · {it['order_id']} #{len(parts) + 1}", m))
                    items.append({**it, **src})
        if len(colors) != 1:
            raise ExtraError("Only plates of the same colour can be merged")
        if not items:
            raise ExtraError("Nothing left to print on these plates")
        if len(items) > MAX_MERGE:
            raise ExtraError(f"At most {MAX_MERGE} names on one plate ({len(items)} chosen)")
        color = colors.pop()
        home = batches[max(batches)]
        names = " + ".join(it["name"] + (f" ({it['letters']})" if it.get("letters") else "") for it in items)
        file, prev = _write(Path(home["out_dir"]) / "Merged", f"Merged - {_title(color)} - {names}", parts,
                            _plate_cfg(home), "names")
        file = f"Merged/{file}"
        _record(con, home["id"], file, "merged", "names", color, items, prev, names, staff)
        now = db.now()
        for p in plates:
            con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                        "updated_at,computer_id) VALUES (?,?, 'names', ?, 0, '', 'merged', ?, ?, ?)",
                        (int(p["batch_id"]), p["file"], color, now, now, computer.ID))
        db.add_activity(con, "merge", f"Merged {len(plates)} {color} plates onto one: {names}")
    return {"batch_id": home["id"], "file": file, "names": names}


def split(batch_id: int, file: str) -> dict:
    """Split back a merged plate that isn't started: its single plates return to Names."""
    with db.tx() as con:
        x = con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND file=? AND kind='merged' AND state='active'",
                        (batch_id, file)).fetchone()
        if x is None:
            raise ExtraError("Not a merged plate")
        if _latest_state(con, batch_id, file) not in (None, "failed", "cancelled", "removed"):
            raise ExtraError("This merged plate is started or printed — it can't be split back")
        con.execute("UPDATE extra_plates SET state='split' WHERE id=?", (x["id"],))
        now = db.now()
        plates = {(int(i.get("src_batch") or i["batch_id"]), i.get("src") or i["plate"]) for i in json.loads(x["items"])}
        for bid, pf in plates:
            if _latest_state(con, bid, pf) == "merged":
                con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                            "updated_at,computer_id) VALUES (?,?, 'names', ?, 0, '', 'unmerged', ?, ?, ?)",
                            (bid, pf, x["color"], now, now, computer.ID))
        db.add_activity(con, "merge", f"Split back {Path(file).name} into {len(plates)} plates")
    return {"ok": True, "plates": len(plates)}


# ---------------- divide (0.26): one plate into smaller ones, e.g. to spread a night run ----------------
def divide(batch_id: int, file: str, sizes: list[int], staff: str = "") -> dict:
    """A batch's own plate (bases or letters), not started, into smaller plates: `sizes` = how many items on
    each (e.g. [1, 1, 1] or [2, 1]), in plate order. Same size, no scaling, saved under `Split/`. The plate
    leaves its list (print job state `divided`); **Join back** returns it while no part is started."""
    from .makeit import _batch_orders, plate_order_ids

    with db.tx() as con:
        b = _own(con, batch_id)
        pl = next((x for x in _plates(b) if x["file"] == file), None)
        if pl is None:
            raise ExtraError("Only a batch's own plate can be split (use Split back for a merged plate)")
        if _latest_state(con, b["id"], file) not in MERGEABLE + ("failed", "cancelled"):  # a failed print may be split too
            raise ExtraError(f"{Path(file).name} is already started, printed, removed, merged or split")
        orders = _batch_orders(con, b)
        state = {o["order_id"]: o["state"] for o in orders}
        on = plate_order_ids(_plates(b), orders).get(file, [])
        items = [it for it in verify.plate_items(b["id"], pl, on) if state.get(it["order_id"]) == "bundled"]
        sizes = [int(s) for s in sizes if int(s) > 0]
        if len(items) < 2:
            raise ExtraError("This plate has only one item left to print")
        if len(sizes) < 2 or sum(sizes) != len(items):
            raise ExtraError(f"The parts must add up to the {len(items)} items on the plate")
        part, color = pl["kind"], ((pl.get("colors") or [""])[0] or "") if pl["kind"] == "names" else ""
        head = "Bases" if part == "bases" else _title(color)
        made, k = [], 0
        for n in sizes:
            chunk = [{**it, "src": file, "src_batch": b["id"]} for it in items[k:k + n]]
            k += n
            parts = []
            for it in chunk:
                m, _ = item_mesh(b, it)
                parts.append(make_part(f"{it['name']} - {_word(part)} · {it['order_id']} #{len(parts) + 1}", m))
            names = " + ".join(it["name"] for it in chunk)
            f, prev = _write(Path(b["out_dir"]) / "Split", f"Part - {head} - {names}", parts, _plate_cfg(b), part)
            _record(con, b["id"], f"Split/{f}", "divided", part, color, chunk, prev, names, staff)
            made.append(f"Split/{f}")
        now = db.now()
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                    "updated_at,computer_id) VALUES (?,?,?,?,0,'','divided',?,?,?)",
                    (b["id"], file, part, color, now, now, computer.ID))
        db.add_activity(con, "merge", f"Split {Path(file).name} into {len(made)} plates ({' + '.join(map(str, sizes))})")
    return {"files": made}


def join(batch_id: int, file: str) -> dict:
    """Join back the parts of a split plate (any one of them given) while none of them is started."""
    with db.tx() as con:
        x = con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND file=? AND kind='divided' AND state='active'",
                        (batch_id, file)).fetchone()
        if x is None:
            raise ExtraError("Not a part of a split plate")
        src = json.loads(x["items"])[0].get("src")
        parts = [dict(p) for p in con.execute("SELECT * FROM extra_plates WHERE batch_id=? AND kind='divided' "
                                              "AND state='active'", (batch_id,))
                 if json.loads(p["items"])[0].get("src") == src]
        for p in parts:
            if _latest_state(con, batch_id, p["file"]) not in (None, "failed", "cancelled", "removed"):
                raise ExtraError(f"{Path(p['file']).name} is started or printed — the plate can't be joined back")
        for p in parts:
            con.execute("UPDATE extra_plates SET state='joined' WHERE id=?", (p["id"],))
        now = db.now()
        con.execute("INSERT INTO print_jobs(batch_id,plate,kind,color,printer_id,printer_name,state,created_at,"
                    "updated_at,computer_id) VALUES (?,?,?,?,0,'','unmerged',?,?,?)",
                    (batch_id, src, x["part"], x["color"], now, now, computer.ID))
        db.add_activity(con, "merge", f"Joined {len(parts)} parts back into {Path(src).name}")
    return {"ok": True, "file": src}


def suggestions(names_rows: list[dict]) -> list[dict]:
    """Merge ideas for Print → Names: same-colour plates not started, ≤ 3 names in all (fewest names first)."""
    by: dict[str, list[dict]] = {}
    for r in names_rows:
        if r.get("extra") or r.get("job_active") or r.get("done"):
            continue
        if ((r.get("job") or {}).get("state")) not in MERGEABLE:
            continue
        n = len(r.get("open_items") or r.get("names") or [])
        if 0 < n < MAX_MERGE:
            by.setdefault(((r.get("colors") or [""])[0] or "").lower(), []).append({**r, "_n": n})
    out = []
    for color, rs in by.items():
        rs.sort(key=lambda r: (r["_n"], r["batch_id"]))
        while len(rs) >= 2:
            pick, total = [rs[0]], rs[0]["_n"]
            for r in rs[1:]:
                if total + r["_n"] <= MAX_MERGE:
                    pick.append(r)
                    total += r["_n"]
            if len(pick) < 2:
                break
            out.append({"color": color, "names": total,
                        "plates": [{"batch_id": r["batch_id"], "file": r["file"], "names": r.get("names", []),
                                    "batch": r.get("batch")} for r in pick]})
            rs = [r for r in rs if r not in pick]
    return out
