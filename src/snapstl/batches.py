"""Batches of selected orders + the MakerWorld download queue.

Flow:
  1. create_batch(order_ids)  → orders become `in_batch`; every model not already in the
     cache is queued in `downloads` (deduplicated by cache key).
  2. The Downloader thread generates each queued model on MakerWorld with the user's login
     cookie (makerworld.py), one at a time, and caches the base + name meshes.
  3. The manager thread builds a batch's plates as soon as all its models are cached (or the
     user presses "Build now"); missing orders go back to ready.
"""
from __future__ import annotations

import json
import threading
import zipfile
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
ACTIVE = ("waiting", "build_requested", "building")


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
        batch_id = db.create_batch(con, ids, "", json.dumps(settings.to_dict()))
        con.execute(
            f"UPDATE orders SET state='in_batch', batch_id=?, updated_at=? WHERE order_id IN ({','.join('?' * len(ids))})",
            (batch_id, db.now(), *ids),
        )
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
    DOWNLOADER.wake()
    return {"batch_id": batch_id, "orders": len(ids), "skipped": len(order_ids) - len(ids), "downloads_queued": queued}


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
    if b is None or b["status"] != "waiting":
        raise ValueError("Only a batch that is waiting for models can be built early")
    if batch_progress(dict(b))["ready"] == 0:
        raise ValueError("No models are downloaded for this batch yet — wait for some, or cancel it")
    with db.tx() as con:
        con.execute("UPDATE batches SET status='build_requested' WHERE id=? AND status='waiting'", (batch_id,))
    MANAGER.wake()


def cancel_batch(batch_id: int) -> None:
    with db.tx() as con:
        r = con.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if r is None or r["status"] not in ("waiting", "build_requested"):
            raise ValueError("Only a batch that is still waiting can be cancelled")
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
    """Works through the download queue in ONE MakerWorld browser session.

    The session (a Chrome window owned by SnapSTL, signed in with the saved cookie) opens
    when there is work, is reused for every name in the queue — fill the form, press
    Generate, keep the two files — and closes when the queue is empty, downloads are
    paused, or the login needs replacing. Names are done one at a time with a pause
    between them (Settings → Pause between downloads).
    """

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = False
        self.status = "Idle"
        self.needs_login = False
        self.current: str | None = None
        self.done_count = 0
        self.pause_s = 3.0
        self.session_open = False
        self.attention: str = ""        # non-empty: the user must act in the MakerWorld window
        self._front = False             # "Show MakerWorld window" requested from the UI
        self.generate = None  # tests inject a fake: (name, model, compartments, pw) -> (bytes, bytes)
        self._browser = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # resume: anything that was mid-download when SnapSTL stopped goes back in the queue
        with db.tx() as con:
            con.execute("UPDATE downloads SET state='queued', attempts=MAX(attempts-1,0), updated_at=? WHERE state='claimed'",
                        (db.now(),))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._ev.set()

    def setup_changed(self) -> None:
        """Cookie updated or downloads resumed: try again."""
        self.needs_login = False
        self.paused = False
        self.status = "Idle"
        self._ev.set()

    def snapshot(self) -> dict:
        return {"status": self.status, "paused": self.paused, "needs_login": self.needs_login,
                "current": self.current, "done": self.done_count, "session_open": self.session_open,
                "attention": self.attention}

    def show_window(self) -> None:
        self._front = True

    def _take_front(self) -> bool:
        f, self._front = self._front, False
        return f

    def _set_attention(self, on: bool, msg: str = "") -> None:
        self.attention = msg if on else ""

    def _set_status(self, s: str) -> None:
        self.status = s

    # ---------- browser session ----------
    def _gen(self, name, model, compartments, pw):
        if self.generate:
            return self.generate(name, model, compartments, pw)
        if self._browser is None:
            from .browser_gen import BrowserMakerWorld
            from .keychain import get_cookie
            from .makerworld import NotConfigured

            cookie = get_cookie()
            if not cookie:
                raise NotConfigured("Paste your MakerWorld cookie in Settings")
            self._browser = BrowserMakerWorld(cookie, status=self._set_status, attention=self._set_attention,
                                              should_stop=lambda: self.paused, front_requested=self._take_front)
            self._browser.open()
            self.session_open = True
        return self._browser.generate(name, model, compartments, pw)

    def _close_session(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self.session_open = False

    def _loop(self) -> None:
        from .browser_gen import Interrupted
        from .makerworld import LoginExpired, NotConfigured
        from .settings import ModelParams

        while True:
            self._ev.wait(10)
            self._ev.clear()
            while not self.paused and not self.needs_login:
                job = _claim_next()
                if job is None:
                    self.status, self.current = "Idle", None
                    break
                self.current = job["name"]
                self.status = f"Generating {job['name']} ({job['compartments']} compartments)"
                try:
                    model = ModelParams(**json.loads(job["model_json"]))
                    p1, p2 = self._gen(job["name"], model, job["compartments"], get_password())
                    self.status = f"Saving {job['name']}"
                    store_result(job["id"], p1, p2)
                    self.done_count += 1
                    self.status = f"Downloaded {job['name']}"
                except Interrupted:
                    _unclaim(job["id"])  # paused while waiting: same name resumes later
                    self.status = "Paused"
                    break
                except (LoginExpired, NotConfigured) as e:
                    _unclaim(job["id"])
                    self.needs_login = True
                    self.status = str(e)
                    break
                except Exception as e:  # noqa: BLE001
                    store_failure(job["id"], str(e))
                    self.status = f"Failed {job['name']}: {e}"
                    if "Target page, context or browser has been closed" in str(e):
                        self._close_session()  # window was closed by hand: reopen for the next name
                time.sleep(max(0.5, self.pause_s))
            self.current = None
            self.attention = ""
            self._close_session()


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
            p = batch_progress(b)
            pending = p["models"] - p["ready"] - p["failed"]
            if b["status"] == "build_requested" or pending == 0:
                self._build(b)

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
            summary = {"ok": res.ok and not missing, "errors": errors, "warnings": res.warnings,
                       "plates": [asdict(p) for p in res.plates], "orders": len(done_ids), "organizers": len(usable)}
            summary["zip"] = str(make_zip(out))
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


def make_zip(folder: Path) -> Path:
    """<batch folder>.zip next to the folder: bases/, names/ and report.csv."""
    z = folder.with_suffix(".zip")
    tmp = z.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                zf.write(f, f"{folder.name}/{f.relative_to(folder).as_posix()}")
    tmp.replace(z)
    return z


MANAGER = Manager()
