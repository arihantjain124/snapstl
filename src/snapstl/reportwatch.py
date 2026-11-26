"""Report import by itself (0.26): a new *Order Reconciliation Report* saved in the Downloads folder is
uploaded like Orders → Upload report, within a minute. Each file is imported once (name, size and time are
remembered); only files saved in the last two days are looked at. Turn it off in Settings → Folders.

0.37.1: the same loop imports *bridge files* (`shopdeck-bridge-*.csv`) from the `bridge-inbox` folder in SnapSTL's
data folder — written by a scheduled Claude task from ShopDeck's warehouse (Claude's ShopDeck connector) — as new
orders only (ingest.ingest_bridge). Only after a sync worked (so orders other computers already have are here
first), only files under BRIDGE_MAX_AGE_S old, only files that say they are bridge files; each is then moved to
`done/` (or `rejected/`).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import db

NAME = re.compile(r"order.?reconciliation.?report.*\.(xlsx|csv)$", re.I)
BRIDGE_NAME = re.compile(r"^shopdeck-bridge-.*\.csv$", re.I)
BRIDGE_MAX_AGE_S = 2 * 3600  # an older snapshot may hold orders cancelled since: left to the report
BRIDGE_KEEP = 200  # files kept in done/ and rejected/
RECENT_S = 2 * 86400
KEY = "report_import_seen"


def folder() -> Path:
    return Path(os.environ.get("SNAPSTL_REPORT_DIR") or (Path.home() / "Downloads"))


def _seen(con) -> dict:
    r = con.execute("SELECT value FROM sync_state WHERE key=?", (KEY,)).fetchone()
    try:
        return json.loads(r[0]) if r else {}
    except ValueError:
        return {}


def new_files() -> list[Path]:
    d = folder()
    if not d.is_dir():
        return []
    now = time.time()
    with db.tx() as con:
        seen = _seen(con)
    out = []
    for f in d.iterdir():
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_file() and NAME.search(f.name) and now - st.st_mtime < RECENT_S \
                and seen.get(f.name) != f"{int(st.st_mtime)}|{st.st_size}" and now - st.st_mtime > 5:
            out.append(f)  # (5 s: the browser may still be writing it)
    return sorted(out, key=lambda f: f.stat().st_mtime)


def import_new() -> list[dict]:
    """Import every new report in the Downloads folder (oldest first). Returns what each import did."""
    from . import sync
    from .ingest import ingest

    done = []
    for f in new_files():
        st = f.stat()
        res: dict = {"file": f.name}
        try:
            if sync.enabled():
                sync.sync_now()
            s = ingest(f.read_bytes(), f.name)
            res |= {"ok": True, "new": s.new, "updated": s.updated}
            with db.tx() as con:
                db.add_activity(con, "download", f"Report imported by itself from Downloads: {f.name} "
                                                 f"({s.new} new, {s.updated} updated)")
            if sync.enabled():
                sync.sync_now()
        except (ValueError, OSError) as e:
            res |= {"ok": False, "error": str(e)}
            with db.tx() as con:
                db.add_activity(con, "download", f"Report in Downloads couldn't be imported: {f.name}: {e}")
        with db.tx() as con:  # once per file version, also when it failed (a broken file isn't retried)
            seen = _seen(con)
            seen[f.name] = f"{int(st.st_mtime)}|{st.st_size}"
            con.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, ?)", (KEY, json.dumps(seen)))
        done.append(res)
    return done


def bridge_folder() -> Path:
    from .settings import APP_DIR

    return Path(os.environ.get("SNAPSTL_BRIDGE_DIR") or (APP_DIR / "bridge-inbox"))


def _file_away(f: Path, sub: str) -> None:
    d = f.parent / sub
    d.mkdir(exist_ok=True)
    os.replace(f, d / f.name)
    old = sorted(d.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    for p in old[:-BRIDGE_KEEP]:
        p.unlink(missing_ok=True)


def import_bridge() -> list[dict]:
    """Import every waiting bridge file (oldest first), new orders only. Returns what each did."""
    from . import sync
    from .ingest import find_header, ingest, is_bridge, read_table

    d = bridge_folder()
    if not d.is_dir():
        return []
    now = time.time()
    files = sorted((f for f in d.iterdir() if f.is_file() and BRIDGE_NAME.search(f.name)
                    and now - f.stat().st_mtime > 5), key=lambda f: f.stat().st_mtime)
    done: list[dict] = []
    for f in files:
        res: dict = {"file": f.name, "at": time.time()}
        if now - f.stat().st_mtime > BRIDGE_MAX_AGE_S:
            res |= {"ok": False, "error": "older than 2 hours — left for the report"}
            _file_away(f, "rejected")
            done.append(res)
            continue
        if sync.enabled():
            sync.sync_now()
            if not sync.online():  # everyone's orders must be here first; the file waits for the next minute
                break
        try:
            data = f.read_bytes()
            table = read_table(data, f.name)
            if not is_bridge(table, find_header(table)):
                raise ValueError("not a SnapSTL bridge file")
            s = ingest(data, f.name)
            res |= {"ok": True, "new": s.new, "known": s.unchanged, "left": len(s.warnings),
                    "to_print": s.to_print_new, "check": s.needs_review_new}
            with db.tx() as con:
                db.add_activity(con, "download", f"ShopDeck bridge: {s.new} new order(s) added before the report "
                                                 f"({s.to_print_new} ready, {s.needs_review_new} to check) — {f.name}")
            _file_away(f, "done")
            if sync.enabled():
                sync.sync_now()
        except (ValueError, OSError) as e:
            res |= {"ok": False, "error": str(e)[:200]}
            with db.tx() as con:
                db.add_activity(con, "download", f"ShopDeck bridge file couldn't be imported: {f.name}: {e}")
            try:
                _file_away(f, "rejected")
            except OSError:
                pass
        done.append(res)
    return done


class Watcher:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self.last: list[dict] = []
        self.bridge_last: list[dict] = []

    def start(self) -> None:
        if os.environ.get("SNAPSTL_NO_REPORT_WATCH") or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-reportwatch")
        self._thread.start()

    def _run(self) -> None:
        from . import server

        while True:
            try:
                if server.SETTINGS.report_import:
                    r = import_new()
                    if r:
                        self.last = r
            except Exception as e:  # noqa: BLE001
                print(f"SnapSTL: report import: {e}")
            try:
                b = import_bridge()
                if b:
                    self.bridge_last = (b + self.bridge_last)[:20]
            except Exception as e:  # noqa: BLE001
                print(f"SnapSTL: bridge import: {e}")
            time.sleep(60)


WATCHER = Watcher()
