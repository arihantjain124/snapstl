"""Batches of selected orders + the MakerWorld download queue.

Flow:
  1. create_batch(order_ids)  → orders become `in_batch`; every model not already in the
     cache is queued in `downloads` (deduplicated by cache key).
  2. The SnapSTL Helper extension (in the user's own Chrome, on a MakerWorld tab) asks for
     the next queued name, generates it on the real page and uploads the base + name files.
  3. The manager thread builds a batch's plates as soon as all its models are cached (or the
     user presses "Build now"); missing orders go back to ready.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
import time
import traceback
from dataclasses import asdict

from . import cache, db
from .keychain import get_password
from .meshes import dims, obj_to_mesh
from .orders import Order
from .pipeline import build, new_batch_dir
from .scaling import pick_band
from .settings import Settings

CLAIM_TIMEOUT_S = 6 * 60
MAX_ATTEMPTS = 2
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
        for _ in range(max(1, int(r["quantity"]))):
            orders.append(Order(len(orders), r["name"], r["color"], r["order_id"], compartments_for(letters, settings)))
    return orders


def _needed(orders: list[Order], settings: Settings, pw: str) -> dict[str, tuple[str, int]]:
    """cache_key → (name, compartments) for every distinct model the orders need."""
    out = {}
    for o in orders:
        out[cache.cache_key(o.name, settings.model, o.compartments, pw)] = (o.name, o.compartments)
    return out


# ---------------- batch lifecycle ----------------
def create_batch(order_ids: list[str], settings: Settings) -> dict:
    if not order_ids:
        raise ValueError("Select at least one order")
    pw = get_password()
    with db.tx() as con:
        q = ",".join("?" * len(order_ids))
        rows = db.rows(con, f"SELECT * FROM orders WHERE state='ready' AND order_id IN ({q}) ORDER BY order_date, order_id", *order_ids)
        if not rows:
            raise ValueError("None of the selected orders are ready (they may need review or be in another batch)")
        orders = _expand(rows, settings)
        ids = [r["order_id"] for r in rows]
        batch_id = db.create_batch(con, ids, "", json.dumps(settings.to_dict()), status="created")
        con.execute(
            f"UPDATE orders SET state='in_batch', batch_id=?, updated_at=? WHERE order_id IN ({','.join('?' * len(ids))})",
            (batch_id, db.now(), *ids),
        )
    return {"batch_id": batch_id, "orders": len(ids), "skipped": len(order_ids) - len(ids),
            "models": len(_needed(orders, settings, pw))}


def start_download(batch_id: int) -> dict:
    """Queue every model this batch still needs; the helper then downloads them."""
    pw = get_password()
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b is None or b["status"] not in ("created", "waiting", "models_ready"):
            raise ValueError("This batch can't start downloading (it's finished or being built)")
        settings = Settings.from_dict(json.loads(b["settings"] or "{}"))
        ids = json.loads(b["order_ids"] or "[]")
        rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
        orders = _expand(rows, settings)
        con.execute("UPDATE batches SET status='waiting' WHERE id=?", (batch_id,))
        queued = 0
        for key, (name, comps) in _needed(orders, settings, pw).items():
            if cache.load_exists(key):
                continue
            live = con.execute(
                "SELECT id FROM downloads WHERE cache_key=? AND state IN ('queued','claimed')", (key,)
            ).fetchone()
            if live:
                continue
            con.execute(
                "INSERT INTO downloads(cache_key,name,compartments,model_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (key, name, comps, json.dumps(asdict(settings.model)), "queued", db.now(), db.now()),
            )
            queued += 1
    MANAGER.wake()
    return {"batch_id": batch_id, "downloads_queued": queued}


def batch_progress(batch: dict) -> dict:
    """How many of this batch's models are cached / failed / pending."""
    settings = Settings.from_dict(json.loads(batch.get("settings") or "{}"))
    pw = get_password()
    ids = json.loads(batch["order_ids"] or "[]")
    if not ids:
        return {"models": 0, "ready": 0, "failed": 0}
    with db.tx() as con:
        rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
        need = _needed(_expand(rows, settings), settings, pw)
        failed = 0
        ready = 0
        for key in need:
            if cache.load_exists(key):
                ready += 1
            elif con.execute("SELECT 1 FROM downloads WHERE cache_key=? AND state='failed' "
                             "AND id=(SELECT MAX(id) FROM downloads WHERE cache_key=?)", (key, key)).fetchone():
                failed += 1
    return {"models": len(need), "ready": ready, "failed": failed}


def request_build(batch_id: int) -> None:
    with db.tx() as con:
        b = con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b is None or b["status"] not in ("waiting", "models_ready"):
        raise ValueError("Start the download first — plates are built from downloaded models")
    if batch_progress(dict(b))["ready"] == 0:
        raise ValueError("No models are downloaded for this batch yet — wait for some, or cancel it")
    with db.tx() as con:
        con.execute("UPDATE batches SET status='build_requested' WHERE id=? AND status IN ('waiting','models_ready')",
                    (batch_id,))
    MANAGER.wake()


def cancel_batch(batch_id: int) -> None:
    with db.tx() as con:
        r = con.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if r is None or r["status"] not in ("created", "waiting", "models_ready"):
            raise ValueError("Only a batch that hasn't been built yet can be cancelled")
        con.execute("UPDATE batches SET status='cancelled', finished_at=? WHERE id=?", (db.now(), batch_id))
        db.release_batch_orders(con, batch_id)
        _cancel_unneeded_downloads(con)


def _cancel_unneeded_downloads(con) -> None:
    """Drop queued downloads that no active batch needs any more."""
    pw = get_password()
    needed: set[str] = set()
    for b in db.rows(con, f"SELECT * FROM batches WHERE status IN ({','.join('?' * len(ACTIVE))})", *ACTIVE):
        settings = Settings.from_dict(json.loads(b.get("settings") or "{}"))
        ids = json.loads(b["order_ids"] or "[]")
        if ids:
            rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))})", *ids)
            needed |= set(_needed(_expand(rows, settings), settings, pw))
    for d in db.rows(con, "SELECT id, cache_key FROM downloads WHERE state='queued'"):
        if d["cache_key"] not in needed:
            con.execute("UPDATE downloads SET state='cancelled', updated_at=? WHERE id=?", (db.now(), d["id"]))


# ---------------- download queue ----------------
def _claim_next() -> dict | None:
    t = db.now()
    with db.tx() as con:
        # re-queue claims left behind by a crash / restart
        con.execute("UPDATE downloads SET state='queued', updated_at=? WHERE state='claimed' AND claimed_at < ?",
                    (t, t - CLAIM_TIMEOUT_S))
        d = con.execute("SELECT * FROM downloads WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if d is None:
            return None
        con.execute("UPDATE downloads SET state='claimed', claimed_at=?, attempts=attempts+1, updated_at=? WHERE id=?",
                    (t, t, d["id"]))
        return dict(d)


def _unclaim(job_id: int) -> None:
    """Put a job back without counting the attempt (e.g. login expired)."""
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='queued', attempts=MAX(attempts-1,0), updated_at=? WHERE id=?",
                    (db.now(), job_id))


def store_result(job_id: int, plate_1: bytes, plate_2: bytes) -> dict:
    with db.tx() as con:
        d = con.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if d is None:
        raise KeyError(job_id)
    meshes = sorted([obj_to_mesh(plate_1), obj_to_mesh(plate_2)], key=lambda m: dims(m)[2], reverse=True)
    base, name = meshes
    if dims(name)[2] > 15 or dims(base)[2] < 20:
        raise ValueError(f"Unexpected parts {dims(base)} / {dims(name)} — was Print version NO AMS?")
    cache.store(d["cache_key"], base, name, {"name": d["name"], "compartments": d["compartments"],
                                             "model": json.loads(d["model_json"]), "source": "makerworld"})
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='done', error=NULL, updated_at=? WHERE id=?", (db.now(), job_id))
    DOWNLOADER.done_count += 1
    DOWNLOADER.current = None
    MANAGER.wake()
    return {"base": [round(v, 1) for v in dims(base)], "name": [round(v, 1) for v in dims(name)]}


def store_failure(job_id: int, error: str) -> None:
    with db.tx() as con:
        d = con.execute("SELECT attempts FROM downloads WHERE id=?", (job_id,)).fetchone()
        if d is None:
            raise KeyError(job_id)
        state = "queued" if d["attempts"] < MAX_ATTEMPTS else "failed"
        con.execute("UPDATE downloads SET state=?, error=?, updated_at=? WHERE id=?",
                    (state, error[:500], db.now(), job_id))
    MANAGER.wake()


def retry_failed() -> int:
    with db.tx() as con:
        n = con.execute("UPDATE downloads SET state='queued', attempts=0, error=NULL, updated_at=? WHERE state='failed'",
                        (db.now(),)).rowcount
    DOWNLOADER.wake()
    return n


def queue_counts() -> dict:
    with db.tx() as con:
        return {r["state"]: r["n"] for r in db.rows(con, "SELECT state, COUNT(*) n FROM downloads GROUP BY state")}


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
        self.last_seen = 0.0
        self.claimed_at = 0.0

    def start(self) -> None:
        # resume: anything that was mid-download when SnapSTL stopped goes back in the queue
        with db.tx() as con:
            con.execute("UPDATE downloads SET state='queued', attempts=MAX(attempts-1,0), updated_at=? WHERE state='claimed'",
                        (db.now(),))

    def wake(self) -> None:  # kept for callers; the helper polls
        pass

    def seen(self, status: str = "") -> None:
        self.last_seen = time.time()
        if status:
            self.status = status[:200]

    @property
    def online(self) -> bool:
        # a name being generated counts as connected (MakerWorld can take a while)
        busy = self.current is not None and time.time() - self.claimed_at < CLAIM_TIMEOUT_S
        return busy or time.time() - self.last_seen < self.ONLINE_S

    def snapshot(self) -> dict:
        return {"status": self.status if self.online else "Helper not connected", "paused": self.paused,
                "current": self.current, "done": self.done_count, "online": self.online,
                "last_seen": self.last_seen}


def helper_next() -> dict | None:
    """Claim the next queued model for the helper (None when paused or nothing to do)."""
    if DOWNLOADER.paused:
        return None
    job = _claim_next()
    if job is None:
        DOWNLOADER.current = None
        return None
    DOWNLOADER.current = job["name"]
    DOWNLOADER.claimed_at = time.time()
    model = json.loads(job["model_json"])
    return {
        "job_id": job["id"], "name": job["name"], "compartments": job["compartments"],
        "params": {"name": job["name"], "font": model["Font"], "font_size": model["Font_size"],
                   "thickness": model["Thickness"], "outline": model["Outline"],
                   "compartments": job["compartments"], "rotate_text": model["Rotate_text"]},
        "license_password": get_password(),
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
            con.execute("UPDATE batches SET status='waiting' WHERE status='building'")
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
            batches = db.rows(con, "SELECT * FROM batches WHERE status IN ('waiting','build_requested') ORDER BY id")
        for b in batches:
            if b["status"] == "build_requested":
                self._build(b)  # only when the user pressed Build plates
                continue
            p = batch_progress(b)
            if p["models"] - p["ready"] - p["failed"] == 0:
                with db.tx() as con:  # all downloaded (or failed): wait for "Build plates"
                    con.execute("UPDATE batches SET status='models_ready' WHERE id=? AND status='waiting'", (b["id"],))

    def _build(self, b: dict) -> None:
        batch_id = b["id"]
        settings = Settings.from_dict(json.loads(b.get("settings") or "{}"))
        settings.output_dir = self.output_dir or settings.output_dir  # server's folder, not the snapshot's
        self.building = batch_id
        with db.tx() as con:
            con.execute("UPDATE batches SET status='building' WHERE id=?", (batch_id,))
        try:
            pw = get_password()
            ids = json.loads(b["order_ids"] or "[]")
            with db.tx() as con:
                rows = db.rows(con, f"SELECT * FROM orders WHERE order_id IN ({','.join('?' * len(ids))}) "
                                    "ORDER BY order_date, order_id", *ids)
            orders = _expand(rows, settings)
            meshes = {}
            for o in orders:
                got = cache.load(cache.cache_key(o.name, settings.model, o.compartments, pw))
                if got:
                    meshes[o.idx] = got
            usable = [o for o in orders if o.idx in meshes]
            missing = sorted({o.name for o in orders if o.idx not in meshes})
            if not usable:
                raise RuntimeError("None of this batch's models were downloaded")
            out = new_batch_dir(settings.output_dir, batch_id)
            with db.tx() as con:
                con.execute("UPDATE batches SET out_dir=? WHERE id=?", (str(out), batch_id))
            self._log(f"Building batch #{batch_id} ({len(usable)} organizers)")
            res = build(usable, meshes, settings, out, self._log)
            incomplete = {o.order_id for o in orders if not res.complete(o.idx)}
            done_ids = sorted({o.order_id for o in usable} - incomplete)
            errors = res.errors + [f"{n}: model not downloaded — order returned to Ready" for n in missing]
            fit_ok = sum(1 for v in res.fit.values() if v == "ok")
            summary = {"ok": res.ok and not missing, "errors": errors, "warnings": res.warnings,
                       "fit": {"checked": len(res.fit), "ok": fit_ok},
                       "plates": [asdict(p) for p in res.plates], "orders": len(done_ids), "organizers": len(usable)}
            summary["folder"] = str(out)  # plain .3mf files — no ZIP is written
            summary["label"] = out.name   # "Batch 1 - 2026-11-15" (numbering restarts every day)
            with db.tx() as con:
                db.finish_batch(con, batch_id, "done" if summary["ok"] else "done_with_problems", summary, done_ids)
            self._log(f"Batch #{batch_id} done: {len(res.plates)} plates")
        except Exception as e:  # noqa: BLE001
            self._log(f"Batch #{batch_id} failed: {e}")
            self._log(traceback.format_exc().splitlines()[-1])
            with db.tx() as con:
                db.finish_batch(con, batch_id, "failed", {"errors": [str(e)]}, [])
        finally:
            self.building = None
            with db.tx() as con:
                _cancel_unneeded_downloads(con)  # failed / missing models of a finished batch


MANAGER = Manager()
