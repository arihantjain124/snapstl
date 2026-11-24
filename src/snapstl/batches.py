"""Batches of selected orders + the MakerWorld download queue.

Flow:
  1. create_batch(order_ids)  → orders become `in_batch`; every model not already in the
     cache is queued in `downloads` (deduplicated by cache key).
  2. The SnapSTL Helper extension (in the user's own Chrome, on a MakerWorld tab) asks for
     the next queued name, generates it on the real page and uploads the base + name files.
  3. The manager thread builds a batch's plates as soon as all its models are cached (or the
     user presses "Build now"); missing orders go back to ready.

Several computers: a batch is made entirely on the computer that created it (its helper, its
Downloads folder, its plates). With the shared order records (sync.py) its orders are claimed
there when the batch is created, so they leave Ready on every computer and no order is made twice.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
import time
import traceback
from dataclasses import asdict

from . import cache, computer, db, products, spares, verify, watermark
from .meshes import dims, obj_by_material, obj_to_mesh
from .orders import Order
from .pipeline import build, new_batch_dir
from .scaling import pick_band
from .settings import Settings, load_settings

CLAIM_TIMEOUT_S = 6 * 60
MAX_ATTEMPTS = 3
NO_PASSWORD = ("Add the license password for this model first (Settings → MakerWorld model → License password). "
               "SnapSTL only downloads with the password so models come without MakerWorld's watermark.")
# Batch lifecycle:
#   created  --Start download-->  waiting (downloading)  --all models in-->  models_ready
#   models_ready / waiting  --Build plates-->  build_requested  -->  building  -->  done | done_with_problems | failed
#   created / waiting / models_ready  --Cancel-->  cancelled
ACTIVE = ("waiting", "models_ready", "build_requested", "building")   # need their models kept / downloaded
OPEN = ("created", "waiting", "models_ready", "build_requested", "building")  # not finished yet


def compartments_for(letters: int, settings: Settings) -> int:
    return int(pick_band(letters, settings.scaling.bands).compartments)


def _expand(rows: list[dict], settings: Settings) -> list[Order]:
    orders: list[Order] = []
    for r in rows:
        letters = sum(c.isalpha() for c in r["name"])
        prod = products.of_order(r)
        comps = compartments_for(letters, settings) if prod.kind == "organizer" else 0
        for _ in range(max(1, int(r["quantity"]))):
            orders.append(Order(len(orders), r["name"], r["color"], r["order_id"], comps, prod.key))
    return orders


def stand_job(o: Order, settings: Settings) -> dict:
    """What the helper fills in on the product's MakerWorld page for this name stand (0.30)."""
    return products.stand_job(o.name, products.BY_KEY[o.product], settings.stands)


def model_key(o: Order, settings: Settings) -> str:
    """Download key: an organizer — name + model options + compartments + model link + its password;
    a name stand (0.30) — name + product + every value sent to MakerWorld."""
    if o.product != products.ORGANIZER.key:
        j = stand_job(o, settings)
        return cache.stand_key(o.name, o.product, j["design"], j["fields"])
    return cache.cache_key(o.name, settings.model, o.compartments, settings.license_password,
                           design=settings.design_id)


def _needed(orders: list[Order], settings: Settings) -> dict[str, tuple[str, int]]:
    """cache_key → (name, compartments) for every distinct model the orders need."""
    return {model_key(o, settings): (o.name, o.compartments) for o in orders}


def _stand_jobs(orders: list[Order], settings: Settings) -> dict[str, dict]:
    """cache_key → MakerWorld form, for the name stands among the orders."""
    return {model_key(o, settings): stand_job(o, settings) for o in orders if o.product != products.ORGANIZER.key}


def batch_product(con, b) -> products.Product:
    """A batch's product (one per batch since 0.30; older batches are the organizer's)."""
    ids = json.loads(b["order_ids"] or "[]")
    r = con.execute("SELECT sku FROM orders WHERE order_id=?", (ids[0],)).fetchone() if ids else None
    return products.of_order({"sku": r[0]} if r else {})


# ---------------- batch lifecycle ----------------
class NotOwner(ValueError):
    """The batch belongs to another computer."""


def _mine(b) -> bool:
    return b is not None and (b["computer_id"] or computer.ID) == computer.ID


def _require_mine(b) -> None:
    if b is not None and not _mine(b):
        raise NotOwner(f"Batch #{b['id']} belongs to {b['computer_name'] or 'another computer'} — its models are "
                       "downloaded and its plates built there. Open SnapSTL on that computer to change it.")


def create_batch(order_ids: list[str], settings: Settings) -> dict:
    """Take ready orders into a new batch of this computer.

    With the shared records (Supabase) set up, the orders are claimed there first: an order another
    computer has already taken is left out (and leaves Ready here). If the shared records can't be
    reached, no batch is created — so no order is ever made twice."""
    from . import sync

    if not order_ids:
        raise ValueError("Select at least one order")
    with db.tx() as con:
        q = ",".join("?" * len(order_ids))
        rows = db.rows(con, f"SELECT * FROM orders WHERE state='ready' AND reuse_of IS NULL AND flag IS NULL "
                            f"AND order_id IN ({q}) ORDER BY order_date, order_id", *order_ids)
        # 0.28: an order with a complete spare piece in stock is sent with it, not printed again
        in_stock = {r[0] for r in con.execute(f"SELECT order_id FROM orders WHERE order_id IN ({q}) "
                                              f"AND {spares.match_sql(complete=True)}", order_ids)}
        # 0.32.1: an order already made and checked Good in an earlier batch is never made again by itself
        # (only after Remake) — e.g. one that came back to To make after it was made
        made = verify.made_before(con, [r["order_id"] for r in rows])
    kinds = {products.of_order(r).key for r in rows if products.of_order(r).key in products.MAKEABLE}
    if len(kinds) > 1:
        raise ValueError("One product per batch: " + ", ".join(sorted(products.BY_KEY[k].label for k in kinds))
                         + " are selected — pick one product with the Product chips")
    later = [r for r in rows if products.of_order(r).key not in products.MAKEABLE]
    rows = [r for r in rows if products.of_order(r).key in products.MAKEABLE]
    if later and not rows:
        raise ValueError(f"{products.of_order(later[0]).label} orders can't be made in SnapSTL yet (a later update) — "
                         "they are listed so none is missed")
    rows = [r for r in rows if r["order_id"] not in in_stock]
    if in_stock and not rows:
        raise ValueError(f"{len(in_stock)} selected order{'s have' if len(in_stock) > 1 else ' has'} a spare piece in "
                         "stock — use it (Orders → To make → Piece in stock) instead of making it again")
    if made:
        rows = [r for r in rows if r["order_id"] not in made]
        if not rows:
            raise ValueError(_made_words(made) + " — not made again. If it really must be made again, "
                             "press Remake on it first")
    if not rows:
        raise ValueError("None of the selected orders are ready (they may need review or be in another batch)")
    uid = uuid.uuid4().hex
    state_at = None
    if sync.enabled():
        try:
            claimed, state_at = sync.claim(rows, uid)
        except sync.SyncError as e:
            raise ValueError(f"{e} — no batch was created, so no order can be made twice. Try again in a moment.") from e
        taken = len(rows) - len(claimed)
        rows = [r for r in rows if r["order_id"] in set(claimed)]
        if not rows:
            raise ValueError("Every selected order was already taken into a batch on another computer — "
                             "they've left Ready here")
    else:
        taken = 0
    with db.tx() as con:
        orders = _expand(rows, settings)
        ids = [r["order_id"] for r in rows]
        batch_id = db.create_batch(con, ids, "", json.dumps(settings.to_dict()), status="created", uid=uid)
        n = con.execute(
            f"UPDATE orders SET state='in_batch', batch_id=?, updated_at=?, batch_uid=?, batch_label=NULL, "
            f"batch_computer=?, batch_computer_id=?, in_remote=?, state_at=COALESCE(?, state_at) "
            f"WHERE state='ready' AND order_id IN ({','.join('?' * len(ids))})",
            (batch_id, db.now(), uid, computer.NAME, computer.ID, 1 if state_at else 0, state_at, *ids),
        ).rowcount
        if n != len(ids):  # changed meanwhile (e.g. a report upload): undo this batch entirely
            raise ValueError("Some of these orders changed a moment ago — refresh and try again")
        if state_at:  # already in the shared records exactly like this: nothing to push
            con.execute(f"DELETE FROM sync_outbox WHERE order_id IN ({','.join('?' * len(ids))})", ids)
    return {"batch_id": batch_id, "orders": len(ids), "skipped": len(order_ids) - len(ids),
            "taken_elsewhere": taken, "models": len(_needed(orders, settings)), "in_stock": len(in_stock),
            "already_made": len(made), "already_made_note": _made_words(made) if made else ""}


def _made_words(made: dict[str, str]) -> str:
    """'2 selected orders were already made and checked Good (Batch 10 - 2026-11-19)'."""
    n = len(made)
    where = ", ".join(sorted(set(made.values())))
    return (f"{n} selected order{'s were' if n > 1 else ' was'} already made and checked Good ({where})")


FINISHED = ("done", "done_with_problems", "failed")


def _was_built(b) -> bool:
    return bool(b["out_dir"])


def start_download(batch_id: int, force: bool = False) -> dict:
    """Queue this batch's models for the helper.

    force=False (Queue download): only models that aren't in the Downloads folder yet.
    force=True  (Re-download):    every model again — the new download replaces the saved one.
    Allowed before building and, for Re-download, on finished batches too.
    """
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        _require_mine(b)
        allowed = ("created", "waiting", "models_ready") + (FINISHED if force else ())
        if b is None or b["status"] not in allowed:
            raise ValueError("This batch can't be queued for download right now"
                             + ("" if force else " — use Re-download for a finished batch"))
        settings = _with_current_passwords(con, b)
        organizer = batch_product(con, b) is products.ORGANIZER
        if organizer and not settings.license_password:  # name stands need no password (0.30)
            raise ValueError(NO_PASSWORD)
        ids = json.loads(b["order_ids"] or "[]")
        rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
        orders = _expand(rows, settings)
        con.execute("UPDATE batches SET status='waiting' WHERE id=?", (batch_id,))
        queued = 0
        stands = _stand_jobs(orders, settings)
        for key, (name, comps) in _needed(orders, settings).items():
            if not force and cache.load_exists(key):
                continue
            queued += _queue(con, key, name, comps, settings, batch_id, stand=stands.get(key))
    MANAGER.wake()
    return {"batch_id": batch_id, "downloads_queued": queued}


def _with_current_passwords(con, b) -> Settings:
    """The batch's settings, with the license passwords from Settings as they are now.

    A batch keeps the settings it was created with, but the password may have been added later —
    downloads always use the current one (the cache key includes it, so old watermarked
    downloads are never reused).
    """
    settings = Settings.from_dict(json.loads(b["settings"] or "{}"))
    current = load_settings().license_passwords
    if current != settings.license_passwords:
        settings.license_passwords = dict(current)
        con.execute("UPDATE batches SET settings=? WHERE id=?", (json.dumps(settings.to_dict()), b["id"]))
    return settings


def _queue(con, key: str, name: str, comps: int, settings: Settings, batch_id: int, manual: bool = False,
           stand: dict | None = None) -> int:
    """Queue one model unless it's already queued / being downloaded. Returns 1 if queued.
    `stand`: a name stand's MakerWorld form (0.30) instead of the organizer's options."""
    if con.execute("SELECT 1 FROM downloads WHERE cache_key=? AND computer_id=? AND state IN ('queued','claimed')",
                   (key, computer.ID)).fetchone():
        return 0
    if stand:
        model = {"_kind": "stand", "_design": stand["design"], "_product": stand["product"], "_url": stand["url"],
                 "_fields": stand["fields"]}
    else:
        model = {**asdict(settings.model), "_license": settings.license_password, "_design": settings.design_id}
    if manual:
        model["_manual"] = True  # re-download of one object: kept even if its batch is finished
    con.execute(
        "INSERT INTO downloads(cache_key,name,compartments,model_json,state,created_at,updated_at,batch_id,computer_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, name, comps, json.dumps(model), "queued", db.now(), db.now(), batch_id, computer.ID),
    )
    return 1


def _batch_orders(con, b) -> tuple[Settings, list[Order]]:
    settings = Settings.from_dict(json.loads(b["settings"] or "{}"))
    ids = json.loads(b["order_ids"] or "[]")
    rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))}) "
                        "ORDER BY order_date, order_id", *ids) if ids else []
    return settings, _expand(rows, settings)


def redownload_model(batch_id: int, key: str) -> dict:
    """Re-download ONE object of a batch (Batches → batch → Objects → Re-download).

    The batch's status doesn't change for a built batch; press Rebuild plates once it's in.
    """
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        _require_mine(b)
        if b is None or b["status"] in ("cancelled", "building", "build_requested"):
            raise ValueError("This batch's objects can't be re-downloaded right now")
        settings = _with_current_passwords(con, b)
        if batch_product(con, b) is products.ORGANIZER and not settings.license_password:
            raise ValueError(NO_PASSWORD)
        _, orders = _batch_orders(con, b)
        needed = _needed(orders, settings)
        if key not in needed:
            raise ValueError("That object isn't part of this batch (was the license password changed? "
                             "use Re-download for the whole batch)")
        name, comps = needed[key]
        queued = _queue(con, key, name, comps, settings, batch_id, manual=True,
                        stand=_stand_jobs(orders, settings).get(key))
        if b["status"] in ("created", "models_ready"):
            con.execute("UPDATE batches SET status='waiting' WHERE id=?", (batch_id,))
    MANAGER.wake()
    return {"queued": queued, "name": name}


def _info(key: str) -> dict:
    d = cache.find(key)
    if not d:
        return {}
    try:
        return json.loads((d / "info.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


_SCAN: dict[tuple[str, int], bool] = {}  # (key, base.stl mtime) → watermark found


def scanned_watermark(key: str) -> bool | None:
    """Watermark on a saved model: from info.json, else scanned now (older downloads) and remembered."""
    info = _info(key)
    if isinstance(info.get("watermark"), bool):
        return info["watermark"]
    d = cache.find(key)
    if not d or not (d / "base.stl").exists():
        return None
    k = (key, int((d / "base.stl").stat().st_mtime))
    if k not in _SCAN:
        got = cache.load(key)
        if not got:
            return None
        _SCAN[k] = watermark.detect(got[0]).present
    return _SCAN[k]


def batch_models(batch_id: int) -> list[dict]:
    """Every distinct object of a batch with its download + watermark state (for the Objects list)."""
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b is None:
            raise KeyError(batch_id)
        mine = _mine(b)
        settings, orders = _batch_orders(con, b)
        out = []
        by_key: dict[str, list[Order]] = {}
        for o in orders:
            by_key.setdefault(model_key(o, settings), []).append(o)
        for key, os_ in by_key.items():
            last = con.execute("SELECT * FROM downloads WHERE cache_key=? AND computer_id=? ORDER BY id DESC LIMIT 1",
                               (key, b["computer_id"] or computer.ID)).fetchone()
            # the files are on the batch's computer: elsewhere, go by its download records
            have = cache.load_exists(key) if mine else bool(last and last["state"] == "done")
            state = last["state"] if last else None
            if state in ("queued", "claimed"):
                status = "downloading" if state == "claimed" else "queued"
            elif have:
                status = "ready"
            elif state == "failed":
                status = "failed"
            else:
                status = "not downloaded"
            info = _info(key) if have else {}
            stand = os_[0].product != products.ORGANIZER.key
            out.append({
                "key": key, "name": os_[0].name, "compartments": os_[0].compartments,
                "copies": len(os_), "colors": sorted({o.color for o in os_}),
                "status": status, "downloaded": info.get("downloaded"),
                "watermark": (scanned_watermark(key) if mine else (bool(last["watermark"]) if last and
                              last["watermark"] is not None else None)) if have and not stand else None,
                "product": os_[0].product,
                "pictures": mine,
                "license_check": info.get("license_check"),
                "error": last["error"] if last and state in ("failed", "queued") else None,
            })
    return sorted(out, key=lambda m: m["name"].lower())


def batch_progress(batch: dict) -> dict:
    """How many of this batch's models are downloaded / failed / still downloading.

    Only the batch's own computer can tell (the models are in its Downloads folder); it saves the
    numbers in batches.progress, which other computers show."""
    if not _mine(batch):
        try:
            return json.loads(batch.get("progress") or "") or {"models": 0, "ready": 0, "failed": 0}
        except ValueError:
            return {"models": 0, "ready": 0, "failed": 0}
    settings = Settings.from_dict(json.loads(batch.get("settings") or "{}"))
    ids = json.loads(batch["order_ids"] or "[]")
    if not ids:
        return {"models": 0, "ready": 0, "failed": 0}
    with db.tx() as con:
        rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
        need = _needed(_expand(rows, settings), settings)
        failed = 0
        ready = 0
        for key in need:
            live = con.execute("SELECT 1 FROM downloads WHERE cache_key=? AND computer_id=? AND state IN ('queued','claimed')",
                               (key, computer.ID)).fetchone()
            if live:
                continue  # being (re)downloaded right now
            if cache.load_exists(key):
                ready += 1
            elif con.execute("SELECT 1 FROM downloads WHERE cache_key=? AND state='failed' AND id=(SELECT MAX(id) "
                             "FROM downloads WHERE cache_key=? AND computer_id=?)", (key, key, computer.ID)).fetchone():
                failed += 1
        prog = {"models": len(need), "ready": ready, "failed": failed}
        if batch.get("id") is not None:
            con.execute("UPDATE batches SET progress=? WHERE id=?", (json.dumps(prog), batch["id"]))
    return prog


def request_build(batch_id: int) -> None:
    """Queue build: also Rebuild for a finished batch (writes a new Batch N folder; the old one stays)."""
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    _require_mine(b)
    if b is None or b["status"] not in ("waiting", "models_ready") + FINISHED:
        raise ValueError("Queue the download first — plates are built from downloaded models")
    with db.tx() as con:
        prod = batch_product(con, b)
    if prod.key not in products.BUILDABLE:
        raise ValueError(f"Plates for {prod.label} name stands come in the next update — their models are downloaded "
                         "and kept in the Downloads folder until then")
    prog = batch_progress(dict(b))
    if b["status"] in FINISHED and prog["models"] - prog["ready"] - prog["failed"] > 0:
        raise ValueError("Some of this batch's objects are still being re-downloaded — "
                         "rebuild once they're in, so the plates use the fresh downloads")
    if prog["ready"] == 0:
        raise ValueError("None of this batch's models are in the Downloads folder — queue a download first")
    with db.tx() as con:
        con.execute("UPDATE batches SET status='build_requested' WHERE id=? AND status IN (?,?,?,?,?)",
                    (batch_id, "waiting", "models_ready", *FINISHED))
    MANAGER.wake()


def cancel_batch(batch_id: int) -> None:
    """Cancel a batch that was never built, or stop a Re-download of a built one (it goes back to done)."""
    with db.tx() as con:
        r = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        _require_mine(r)
        if r is None or r["status"] not in ("created", "waiting", "models_ready"):
            raise ValueError("Only a batch that is waiting to download or build can be cancelled")
        if _was_built(r):
            ok = json.loads(r["summary"] or "{}").get("ok")
            con.execute("UPDATE batches SET status=? WHERE id=?", ("done" if ok else "done_with_problems", batch_id))
        else:
            con.execute("UPDATE batches SET status='cancelled', finished_at=? WHERE id=?", (db.now(), batch_id))
            db.release_batch_orders(con, batch_id)
        _cancel_unneeded_downloads(con)
    _kick_sync()


def _cancel_unneeded_downloads(con) -> None:
    """Drop this computer's queued downloads that none of its active batches needs any more."""
    needed: set[str] = set()
    for b in db.rows(con, f"SELECT * FROM batches WHERE computer_id=? AND status IN ({','.join('?' * len(ACTIVE))})",
                     computer.ID, *ACTIVE):
        settings = Settings.from_dict(json.loads(b.get("settings") or "{}"))
        ids = json.loads(b["order_ids"] or "[]")
        if ids:
            rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
            needed |= set(_needed(_expand(rows, settings), settings))
    for d in db.rows(con, "SELECT d.id, d.cache_key, d.model_json, b.status AS bstatus FROM downloads d "
                          "LEFT JOIN batches b ON b.id=d.batch_id WHERE d.state='queued' AND d.computer_id=?",
                     computer.ID):
        manual = json.loads(d["model_json"] or "{}").get("_manual")
        if manual and d["bstatus"] in FINISHED:
            continue  # a single object re-downloaded from a built batch
        if d["cache_key"] not in needed:
            con.execute("UPDATE downloads SET state='cancelled', updated_at=? WHERE id=?", (db.now(), d["id"]))


def _kick_sync() -> None:
    from . import sync

    sync.LOOP.kick()  # bundled / released orders go to the shared records soon


# ---------------- download queue ----------------
def _design_of(d) -> str:
    """The MakerWorld designId a queued download is made on (organizer jobs: the Settings model)."""
    m = json.loads(d["model_json"] or "{}")
    return str(m.get("_design") or "")


def _is_stand(d) -> bool:
    return json.loads(d["model_json"] or "{}").get("_kind") == "stand"


def _claim_next(design: str | None = None) -> dict | None:
    """The oldest queued download — on the page `design` if given (helper 2.4 tells the page it is on);
    without it (an older helper) only organizer downloads, never a name stand."""
    t = db.now()
    with db.tx() as con:
        # re-queue claims left behind by a crash / restart
        con.execute("UPDATE downloads SET state='queued', updated_at=? WHERE state='claimed' AND claimed_at < ? "
                    "AND computer_id=?", (t, t - CLAIM_TIMEOUT_S, computer.ID))
        d = None
        for row in con.execute("SELECT * FROM downloads WHERE state='queued' AND computer_id=? ORDER BY id", (computer.ID,)):
            if design is None and not _is_stand(row):
                d = row
                break
            if design is not None and (_design_of(row) == design or (not _is_stand(row) and design == _organizer_design())):
                d = row
                break
        if d is None:
            return None
        con.execute("UPDATE downloads SET state='claimed', claimed_at=?, attempts=attempts+1, updated_at=? WHERE id=?",
                    (t, t, d["id"]))
        return dict(d)


def _unclaim(job_id: int) -> None:
    """Put a job back without counting the attempt (e.g. login expired)."""
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='queued', attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END, updated_at=? WHERE id=?",
                    (db.now(), job_id))


class Rejected(ValueError):
    """A download that came back but must not be used (no confirmed password / watermark found)."""


def check_license(model: dict, check: dict | None) -> str:
    """Was the license password really in place when Generate was pressed? Raises Rejected if not.

    `check` comes from the helper (2.2.1+):
      password_set — the License password box held exactly the password right before Generate
                     was pressed (2.2.1+; kept_after says whether it was still there just after,
                     informational only — MakerWorld may clear the box once Generate starts);
      request      — "sent" / "missing" / "unknown": whether the page's Generate request carried it
                     (the helper only watches the page's own requests; "missing" is reported only
                     when the request plainly carried the name but not the password).
    """
    if not model.get("_license"):
        raise Rejected("No license password for this model — not saved (downloads need the password)")
    if not check:
        raise Rejected("This SnapSTL Helper is older than 2.2 and can't confirm the license password — not saved. "
                       "Reload the helper in chrome://extensions")
    if check.get("password_set") is not True:
        raise Rejected("The helper didn't confirm the license password was filled in when Generate was pressed — "
                       "not saved, trying again (update the SnapSTL Helper if this keeps happening)")
    if check.get("request") == "missing":
        raise Rejected("MakerWorld's Generate request didn't include the license password — not saved, trying again")
    return "confirmed" + (" (sent with Generate)" if check.get("request") == "sent" else "")


def store_result(job_id: int, plate_1: bytes, plate_2: bytes, check: dict | None = None) -> dict:
    with db.tx() as con:
        d = con.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if d is None:
        raise KeyError(job_id)
    model = json.loads(d["model_json"])
    lic = check_license(model, check)
    meshes = sorted([obj_to_mesh(plate_1), obj_to_mesh(plate_2)], key=lambda m: dims(m)[2], reverse=True)
    base, name = meshes
    if dims(name)[2] > 15 or dims(base)[2] < 20:
        raise ValueError(f"Unexpected parts {dims(base)} / {dims(name)} — was Print version NO AMS?")
    wm = watermark.detect(base)
    if wm.present:
        with db.tx() as con:
            con.execute("UPDATE downloads SET watermark=1 WHERE id=?", (job_id,))
        raise Rejected(f"MakerWorld watermark still on the base ({wm.summary()}) — the license password didn't "
                       "apply. Not saved, trying again")
    cache.store(d["cache_key"], base, name, {"name": d["name"], "compartments": d["compartments"],
                                             "model": {k: v for k, v in model.items()
                                                       if not k.startswith("_")},  # never write the password
                                             "source": "makerworld",
                                             "downloaded": time.strftime("%Y-%m-%d %H:%M:%S"),
                                             "batch": d["batch_id"] if "batch_id" in d.keys() else None,
                                             "watermark": False, "license_check": lic})
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='done', error=NULL, watermark=0, license_check=?, updated_at=? "
                    "WHERE id=?", (lic, db.now(), job_id))
    DOWNLOADER.done_count += 1
    DOWNLOADER.current = None
    DOWNLOADER.next_after = time.time() + DOWNLOADER.pause_s
    MANAGER.wake()
    return {"base": [round(v, 1) for v in dims(base)], "name": [round(v, 1) for v in dims(name)]}


STAND_TOL = 0.3  # mm


def store_stand_result(job_id: int, obj_text: str) -> dict:
    """A name stand from MakerWorld (0.30): one OBJ, two materials. The lower one is the coloured block, the
    one on top the raised letters; checked against the sizes sent (back thickness, letter protrusion)."""
    with db.tx() as con:
        d = con.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if d is None:
        raise KeyError(job_id)
    model = json.loads(d["model_json"])
    if model.get("_kind") != "stand":
        raise ValueError("This download is an organizer, not a name stand")
    parts = obj_by_material(obj_text)
    if len(parts) != 2:
        raise ValueError(f"Expected 2 colours in the MakerWorld model, got {len(parts)} — has the model changed?")
    block, letters = sorted(parts.values(), key=lambda m: (float(m.bounds[0][2]), -float(m.bounds[1][2])))
    sent = {f[0]: f[2] for f in model.get("_fields") or []}
    back, lp = float(sent.get("back thickness", 0)), float(sent.get("letter protrusion", 0))
    bz = float(block.bounds[1][2] - block.bounds[0][2])
    lz = float(letters.bounds[1][2] - letters.bounds[0][2])
    gap = float(letters.bounds[0][2] - block.bounds[1][2])
    if abs(bz - back) > STAND_TOL or abs(lz - lp) > STAND_TOL or abs(gap) > STAND_TOL:
        raise ValueError(f"Unexpected name stand: block {bz:.1f} mm (sent {back:g}), letters {lz:.1f} mm "
                         f"(sent {lp:g}), letters {gap:+.1f} mm from the block's top — not saved")
    label = products.BY_KEY[model["_product"]].label if model.get("_product") in products.BY_KEY else "stand"
    cache.store_stand(d["cache_key"], block, letters, obj_text, {
        "name": d["name"], "kind": "stand", "product": model.get("_product"), "product_label": label,
        "design": model.get("_design"), "fields": model.get("_fields"), "source": "makerworld",
        "downloaded": time.strftime("%Y-%m-%d %H:%M:%S"), "batch": d["batch_id"],
        "block": [round(v, 2) for v in dims(block)], "letters": [round(v, 2) for v in dims(letters)]})
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='done', error=NULL, license_check=?, updated_at=? WHERE id=?",
                    ("not needed (name stand)", db.now(), job_id))
    DOWNLOADER.done_count += 1
    DOWNLOADER.current = None
    DOWNLOADER.next_after = time.time() + DOWNLOADER.pause_s
    MANAGER.wake()
    return {"block": [round(v, 1) for v in dims(block)], "letters": [round(v, 1) for v in dims(letters)]}


def store_failure(job_id: int, error: str) -> None:
    with db.tx() as con:
        d = con.execute("SELECT attempts FROM downloads WHERE id=?", (job_id,)).fetchone()
        if d is None:
            raise KeyError(job_id)
        state = "queued" if d["attempts"] < MAX_ATTEMPTS else "failed"
        # a failure waits longer before the next name: Time break × tries so far (1×, 2×, 3× …)
        DOWNLOADER.next_after = time.time() + DOWNLOADER.pause_s * max(1, d["attempts"])
        con.execute("UPDATE downloads SET state=?, error=?, updated_at=? WHERE id=?",
                    (state, error[:500], db.now(), job_id))
    MANAGER.wake()


def retry_failed() -> int:
    with db.tx() as con:
        n = con.execute("UPDATE downloads SET state='queued', attempts=0, error=NULL, updated_at=? "
                        "WHERE state='failed' AND computer_id=?", (db.now(), computer.ID)).rowcount
    DOWNLOADER.wake()
    return n


def queue_counts() -> dict:
    with db.tx() as con:
        return {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) AS n FROM downloads WHERE computer_id=? "
                                                         "GROUP BY state", computer.ID)}


class Downloader:
    """State of the download side.

    Models are generated by the **SnapSTL Helper** Chrome extension in the user's own
    Chrome (a MakerWorld tab). The helper asks the app for the next queued name
    (`/api/helper/next`), fills the real model-maker page, presses Generate and uploads
    the two files (`/api/helper/jobs/{id}/result`). This class only tracks pause state and
    what the helper last reported; the queue itself lives in the `downloads` table.
    """

    ONLINE_S = 25  # helper counts as connected if it called in within this many seconds

    def __init__(self) -> None:
        self.paused = False
        self.status = "Idle"
        self.current: str | None = None
        self.done_count = 0
        self.pause_s = 3.0
        self.next_after = 0.0  # no name is handed out before this time (Time break; longer after a failure)
        self.last_seen = 0.0
        self.claimed_at = 0.0
        self.on_page = False  # helper's tab is on the configured MakerWorld model page

    def start(self) -> None:
        # resume: anything that was mid-download when SnapSTL stopped goes back in the queue
        with db.tx() as con:
            con.execute("UPDATE downloads SET state='queued', attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END, updated_at=? "
                        "WHERE state='claimed' AND computer_id=?", (db.now(), computer.ID))

    def wake(self) -> None:  # kept for callers; the helper polls
        pass

    def seen(self, status: str = "", on_page: bool | None = None) -> None:
        self.last_seen = time.time()
        if status:
            self.status = status[:200]
        if on_page is not None:
            self.on_page = on_page

    @property
    def online(self) -> bool:
        # a name being generated counts as connected (MakerWorld can take a while)
        busy = self.current is not None and time.time() - self.claimed_at < CLAIM_TIMEOUT_S
        return busy or time.time() - self.last_seen < self.ONLINE_S

    def snapshot(self) -> dict:
        return {"status": self.status if self.online else "Helper not connected", "paused": self.paused,
                "current": self.current, "done": self.done_count, "online": self.online,
                "on_page": self.online and self.on_page,
                "last_seen": self.last_seen}


def _organizer_design() -> str:
    return load_settings().design_id or cache.DEFAULT_DESIGN


def helper_go(design: str | None) -> str | None:
    """Helper 2.4 on page `design` with nothing to do there: the page of the oldest queued download elsewhere
    (it opens it in the working tab), else None."""
    if DOWNLOADER.paused or design is None:
        return None
    with db.tx() as con:
        for row in con.execute("SELECT * FROM downloads WHERE state='queued' AND computer_id=? ORDER BY id", (computer.ID,)):
            m = json.loads(row["model_json"] or "{}")
            if m.get("_kind") == "stand":
                if str(m.get("_design")) != design:
                    return m.get("_url") or None
            elif design != _organizer_design():
                return load_settings().makerworld_url
            return None
    return None


def helper_next(design: str | None = None) -> dict | None:
    """Claim the next queued model for the helper (None when paused or nothing to do on this page)."""
    if DOWNLOADER.paused:
        return None
    if time.time() < DOWNLOADER.next_after:  # enforced here too, so retries keep the gap
        return None
    while True:
        job = _claim_next(design)
        if job is None:
            DOWNLOADER.current = None
            return None
        model = json.loads(job["model_json"])
        if model.get("_kind") == "stand":  # a name stand (0.30): no password, its own form
            DOWNLOADER.current = job["name"]
            DOWNLOADER.claimed_at = time.time()
            label = products.BY_KEY[model["_product"]].label if model.get("_product") in products.BY_KEY else "stand"
            return {"job_id": job["id"], "kind": "stand", "name": job["name"], "product": label,
                    "design": model.get("_design"), "fields": model.get("_fields") or [],
                    "pause_after_s": DOWNLOADER.pause_s}
        if model.get("_license"):
            break
        with db.tx() as con:  # queued before the password rule: never generate without it
            con.execute("UPDATE downloads SET state='failed', error=?, updated_at=? WHERE id=?",
                        (NO_PASSWORD, db.now(), job["id"]))
    DOWNLOADER.current = job["name"]
    DOWNLOADER.claimed_at = time.time()
    return {
        "job_id": job["id"], "name": job["name"], "compartments": job["compartments"],
        "params": {"name": job["name"], "font": model["Font"], "font_size": model["Font_size"],
                   "thickness": model["Thickness"], "outline": model["Outline"],
                   "compartments": job["compartments"], "rotate_text": model["Rotate_text"]},
        "license_password": model.get("_license", ""),  # this batch's model password (Settings, per model)
        "pause_after_s": DOWNLOADER.pause_s,
    }


def helper_release(job_id: int) -> None:
    """The helper stopped mid-name (paused / tab closed): put it back without counting a try."""
    _unclaim(job_id)
    DOWNLOADER.current = None


DOWNLOADER = Downloader()


# ---------------- builder thread ----------------
class Manager:
    def __init__(self) -> None:
        self._ev = threading.Event()
        self._thread: threading.Thread | None = None
        self.log: list[str] = []
        self.building: int | None = None
        self.output_dir: str = ""  # set by the server at startup

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # a build interrupted by a restart goes back to waiting; drop downloads nobody needs
        with db.tx() as con:
            con.execute("UPDATE batches SET status='waiting' WHERE status='building' AND computer_id=?", (computer.ID,))
            _cancel_unneeded_downloads(con)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._ev.set()

    def _log(self, msg: str) -> None:
        self.log.append(time.strftime("%H:%M:%S ") + msg)
        self.log = self.log[-300:]

    def _loop(self) -> None:
        while True:
            self._ev.wait(5)
            self._ev.clear()
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"Builder error: {e}")

    def _tick(self) -> None:
        with db.tx() as con:
            batches = db.rows(con, "SELECT * FROM batches WHERE status IN ('waiting','build_requested') AND computer_id=? "
                                   "ORDER BY id", computer.ID)
        for b in batches:
            if b["status"] == "build_requested":
                with db.tx() as con:
                    buildable = batch_product(con, b).key in products.BUILDABLE
                if not buildable:  # never build a product with the organizer's plates (name stands: next update)
                    with db.tx() as con:
                        con.execute("UPDATE batches SET status='models_ready' WHERE id=?", (b["id"],))
                    continue
                self._build(b)  # only when the user pressed Build plates
                continue
            p = batch_progress(b)
            if p["models"] - p["ready"] - p["failed"] == 0:
                with db.tx() as con:
                    if b.get("auto_build") and p["ready"] and not p["failed"]:  # Make next batch: build by itself
                        con.execute("UPDATE batches SET status='build_requested' WHERE id=? AND status='waiting'", (b["id"],))
                        self._ev.set()
                    else:  # all downloaded (or some failed): wait for "Build plates"
                        con.execute("UPDATE batches SET status='models_ready' WHERE id=? AND status='waiting'", (b["id"],))

    def _build(self, b: dict) -> None:
        batch_id = b["id"]
        settings = Settings.from_dict(json.loads(b.get("settings") or "{}"))
        settings.output_dir = self.output_dir or settings.output_dir  # server's folder, not the snapshot's
        self.building = batch_id
        with db.tx() as con:
            con.execute("UPDATE batches SET status='building' WHERE id=?", (batch_id,))
        try:
            ids = json.loads(b["order_ids"] or "[]")
            with db.tx() as con:
                rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))}) "
                                    "ORDER BY order_date, order_id", *ids)
            orders = _expand(rows, settings)
            if orders and orders[0].product != products.ORGANIZER.key:  # name stands (0.31): their own plates
                self._build_stands(b, settings, orders)
                return
            meshes = {}
            marked: dict[str, bool] = {}  # cache key → watermark found
            for o in orders:
                key = model_key(o, settings)
                got = cache.load(key)
                if got:
                    if key not in marked:
                        marked[key] = watermark.detect(got[0]).present
                    if not marked[key]:
                        meshes[o.idx] = got
            watermarked = sorted({o.name for o in orders if marked.get(model_key(o, settings))})
            usable = [o for o in orders if o.idx in meshes]
            missing = sorted({o.name for o in orders if o.idx not in meshes} - set(watermarked))
            if not usable:
                raise RuntimeError("None of this batch's models are usable"
                                   + (f" — watermark found on: {', '.join(watermarked)}" if watermarked else
                                      " (none were downloaded)"))
            out = new_batch_dir(settings.output_dir, batch_id)
            with db.tx() as con:
                con.execute("UPDATE batches SET out_dir=? WHERE id=?", (str(out), batch_id))
            self._log(f"Building batch #{batch_id} ({len(usable)} organizers)")
            res = build(usable, meshes, settings, out, self._log,
                        preview_dir=PREVIEW_DIR / f"batch-{batch_id}" / out.name)
            incomplete = {o.order_id for o in orders if not res.complete(o.idx)}
            done_ids = sorted({o.order_id for o in usable} - incomplete)
            errors = res.errors + [f"{n}: model not in the Downloads folder — left out of these plates" for n in missing]
            errors += [f"{n}: MakerWorld watermark found on the downloaded base — held back, order returned to Ready. "
                       "Re-download it (Objects list), then Rebuild plates" for n in watermarked]
            fit_ok = sum(1 for v in res.fit.values() if v == "ok")
            summary = {"ok": res.ok and not missing and not watermarked,
                       "watermark": {"checked": len(marked), "found": sum(marked.values())}, "errors": errors, "warnings": res.warnings,
                       "fit": {"checked": len(res.fit), "ok": fit_ok},
                       "plates": [asdict(p) for p in res.plates], "orders": len(done_ids), "organizers": len(usable)}
            summary["folder"] = str(out)  # plain .3mf files — no ZIP is written
            summary["label"] = out.name   # "Batch 1 - 2026-11-15" (numbering restarts every day)
            old = json.loads(b.get("summary") or "{}")  # a Rebuild keeps the earlier folders on disk
            summary["previous_folders"] = old.get("previous_folders", []) + ([old["folder"]] if old.get("folder") else [])
            with db.tx() as con:
                db.finish_batch(con, batch_id, "done" if summary["ok"] else "done_with_problems", summary, done_ids)
            self._log(f"Batch #{batch_id} done: {len(res.plates)} plates")
            _kick_sync()
        except Exception as e:  # noqa: BLE001
            self._log(f"Batch #{batch_id} failed: {e}")
            self._log(traceback.format_exc().splitlines()[-1])
            with db.tx() as con:
                db.finish_batch(con, batch_id, "failed", {"errors": [str(e)]}, [])
            _kick_sync()
        finally:
            self.building = None
            with db.tx() as con:
                _cancel_unneeded_downloads(con)  # failed / missing models of a finished batch


def _build_stands(self, b: dict, settings: Settings, orders: list[Order]) -> None:
    """A name stand batch (0.31, stands.py): scale, up to 3 per plate, two-colour .3mf files. Its plates are
    the batch's "plates" with kind "stands" (0.32; 0.31 kept them in "stand_plates" — stands.repair_031)."""
    from . import stands

    batch_id = b["id"]
    meshes = {}
    for o in orders:
        got = cache.load(model_key(o, settings))
        if got:
            meshes[o.idx] = got
    missing = sorted({o.name for o in orders if o.idx not in meshes})
    if not meshes:
        raise RuntimeError("None of this batch's models are in the Downloads folder")
    out = new_batch_dir(settings.output_dir, batch_id)
    with db.tx() as con:
        con.execute("UPDATE batches SET out_dir=? WHERE id=?", (str(out), batch_id))
    usable = [o for o in orders if o.idx in meshes]
    self._log(f"Building batch #{batch_id} ({len(usable)} name stands)")
    res = stands.build(usable, meshes, settings, out, self._log)
    done_ids = sorted({o.order_id for o in usable} - {o.order_id for o in orders if not res.complete(o.idx)})
    errors = res.errors + [f"{n}: model not in the Downloads folder — left out of these plates" for n in missing]
    summary = {"ok": res.ok and not missing, "errors": errors, "warnings": res.warnings,
               "plates": [asdict(p) for p in res.plates], "orders": len(done_ids), "stands": len(usable),
               "product": usable[0].product, "folder": str(out), "label": out.name}
    old = json.loads(b.get("summary") or "{}")
    summary["previous_folders"] = old.get("previous_folders", []) + ([old["folder"]] if old.get("folder") else [])
    with db.tx() as con:
        db.finish_batch(con, batch_id, "done" if summary["ok"] else "done_with_problems", summary, done_ids)
    self._log(f"Batch #{batch_id} done: {len(res.plates)} name stand plates")
    _kick_sync()


Manager._build_stands = _build_stands
MANAGER = Manager()
PREVIEW_DIR = Path(cache.APP_DIR) / "previews"  # rendered plate / object pictures (safe to delete)
