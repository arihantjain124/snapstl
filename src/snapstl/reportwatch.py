"""Report import by itself (0.26): a new *Order Reconciliation Report* saved in the Downloads folder is
uploaded like Orders → Upload report, within a minute. Each file is imported once (name, size and time are
remembered); only files saved in the last two days are looked at. Turn it off in Settings → Folders.
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


class Watcher:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self.last: list[dict] = []

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
            time.sleep(60)


WATCHER = Watcher()
