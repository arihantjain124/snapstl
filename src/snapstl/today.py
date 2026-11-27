"""Today's work (0.39, owner; English only since 0.40): one page that tells the person at the printers what to do next.

The day rhythm (agreed 2026-11-27): a plate should end while someone is there (the shift, default 08:00–22:00),
and every printer gets a long base plate for the night. Times are rough, per piece, measured from the prints
already done (`per_piece`: hours per base / per letters plate / per stand, by printer model) — not per letter.

For each free printer the board picks the next plate (the Print page's order: fix plates, Express, clubbed, then
oldest batches first) and, for bases, **how many bases** (`best_size`): the size that prints the most bases per
hour once the time nobody is there to take it off is counted. A bigger plate is split by itself (extras.divide)
when its button is pressed; the print starts like Print → Start print (the bed check included).
Letters printers get letter plates in the day, a base plate for the night; X1 (Flex) gets name stands first.
From 20:00 (two hours before the shift ends) `NIGHT` merges small base plates into 3-base ones, once a day.

0.43 (owner): the whole day per printer (`plan`): what it prints now, then the plates sized to end by 21:30 (no day
plate runs after 21:30, so every printer is free for its night plate by 22:00; a plate ending later is a night plate
and may start earlier — an A1 at 20:00 prints 3 bases till 08:00), then its night plate. Re-planned on every refresh.
When the plan needs more bases than are on plates, the board offers Make next batch; it also offers letter merges
(day) and 3-base night merges (from 20:00) — every action with a confirmation. A split puts Express and clubbed
names first (`extras.divide`); the rest of a split plate comes next (`_rank`).
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from datetime import datetime, timedelta

from . import db, estimate, extras, farm, makeit

REACT_H = 0.25     # someone takes a finished plate off within ~15 minutes while they are here
LEAVE_MIN = 30     # no day plate runs in the last half hour (0.43, owner: every printer free 21:30–22:00)
ROUND_MIN = 60     # the night round: the last hour of the shift
PREP_MIN = 120     # night plates are got ready from two hours before the shift ends
DEFAULT_H = {"bases": {"A1": 4.0, "P1S": 3.3, "": 3.5}, "names": {"": 0.35}, farm.STANDS: {"": 4.0}}
_TTL = 600
_CACHE: dict = {"at": 0.0, "v": None}
_LOCK = threading.Lock()


# ---------------- rough time per piece ----------------
def _family(model: str) -> str:
    m = (model or "").upper().replace(" ", "")
    return "A1" if m.startswith("A1") else "P1S" if m.startswith(("P1", "X1")) else m


def per_piece(prs: list[dict] | None = None) -> dict:
    """{kind: {model family: hours per piece}} from the prints of the last 30 days (median of time ÷ pieces);
    a kind or model with fewer than 5 prints uses DEFAULT_H. Pieces: bases, names (a letters plate), stands."""
    with _LOCK:
        if _CACHE["v"] is not None and time.time() - _CACHE["at"] < _TTL:
            return _CACHE["v"]
    fam = {int(p["id"]): _family(str(p.get("model") or "")) for p in prs or [] if p.get("id") is not None}
    pts: dict[str, dict[str, list[float]]] = {}
    with db.tx() as con:
        plates = estimate._plates(con)
        for bid, plate, kind, pid, st, up, sp in con.execute(
                "SELECT batch_id, plate, kind, printer_id, started_at, updated_at, speed FROM print_jobs "
                "WHERE state='done' AND started_at IS NOT NULL AND updated_at > started_at AND started_at > ?",
                (time.time() - 30 * 86400,)):
            n = len(plates.get((bid, plate)) or [])
            h = (up - st) / 3600
            if not n or h < 0.1 or h > 20:
                continue
            k = pts.setdefault(kind, {})
            k.setdefault(fam.get(pid, ""), []).append(h / n)
            k.setdefault("", []).append(h / n)
    out = {k: dict(v) for k, v in DEFAULT_H.items()}
    for kind, by in pts.items():
        for f, v in by.items():
            if len(v) >= 5:
                out.setdefault(kind, {})[f] = round(statistics.median(v), 2)
    with _LOCK:
        _CACHE.update(at=time.time(), v=out)
    return out


def hours(pp: dict, kind: str, model: str, n: int) -> float:
    k = pp.get(kind) or {}
    return round((k.get(_family(model)) or k.get("") or 3.5) * max(1, n), 2)


# ---------------- the shift ----------------
def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def shift_window(sh: dict) -> tuple[int, int]:
    return _hm(sh.get("start") or "08:00"), _hm(sh.get("end") or "22:00")


def staffed(t: float, sh: dict) -> bool:
    """Someone is there to take a plate off at t (the shift, less the last LEAVE_MIN minutes)."""
    st, en = shift_window(sh)
    d = datetime.fromtimestamp(t)
    m = d.hour * 60 + d.minute
    return st <= m < en - LEAVE_MIN if st < en else (m >= st or m < en - LEAVE_MIN)


def next_staffed(t: float, sh: dict) -> float:
    if staffed(t, sh):
        return t
    st, _ = shift_window(sh)
    d = datetime.fromtimestamp(t)
    start = d.replace(hour=st // 60, minute=st % 60, second=0, microsecond=0)
    if start.timestamp() <= t:
        start += timedelta(days=1)
    return start.timestamp()


def phase(t: float, sh: dict) -> str:
    """day | prep (night plates being got ready) | round (the night round: start the night plates) | night."""
    st, en = shift_window(sh)
    d = datetime.fromtimestamp(t)
    m = d.hour * 60 + d.minute
    if not (st <= m < en):
        return "night"
    if m >= en - ROUND_MIN:
        return "round"
    if m >= en - PREP_MIN:
        return "prep"
    return "day"


def best_size(t: float, per_h: float, most: int, sh: dict) -> tuple[int, float]:
    """How many bases (1..most) to put on a printer free at t: the most bases per hour, counting the hours the
    printer then waits for someone. Returns (bases, idle hours after it)."""
    best = (0.0, 1, 0.0)
    for n in range(1, max(1, most) + 1):
        end = t + n * per_h * 3600
        idle = REACT_H if staffed(end, sh) else (next_staffed(end, sh) - end) / 3600
        score = n / (n * per_h + idle)
        if score >= best[0] - 1e-9:
            best = (score, n, round(idle, 1))
    return best[1], best[2]


# ---------------- the board ----------------
def _plate_n(r: dict) -> int:
    return len(r.get("open_items") or [])


def _startable(r: dict) -> bool:
    j = r.get("job") or {}
    return not r.get("job_active") and j.get("state") not in ("merged", "divided", "done")


def _splittable(r: dict) -> bool:
    j = r.get("job") or {}
    return (not r.get("extra") and len(r.get("names") or []) == _plate_n(r)
            and (not j or j.get("state") in ("reprint", "unmerged", "failed", "cancelled")))


def _free(p: dict, active: dict[int, int]) -> str | None:
    """'clear' / 'check' when a plate can go on this printer now, else None (printing, offline, a plate waiting)."""
    st = p.get("status") or {}
    if not p.get("active", True) or not st.get("connected") or active.get(p["id"], 0):
        return None
    bed = makeit.bed_state(p)
    return None if bed == "busy" else bed


def build(mk: dict, prs: list[dict], now: float | None = None, sh: dict | None = None,
          pp: dict | None = None) -> dict:
    """The board from Print's own board (`mk` = makeit.board + printers): tasks for the free printers, what ends
    when, and the other work (checking prints, labels)."""
    now = now or time.time()
    sh = sh or farm.shift()
    pp = pp or per_piece(prs)
    ph = phase(now, sh)
    st, en = shift_window(sh)
    day_end = datetime.fromtimestamp(now).replace(hour=en // 60, minute=en % 60, second=0, microsecond=0).timestamp()
    cut = day_end - LEAVE_MIN * 60  # a day plate (letters) must be off by then
    active = farm.active_counts()
    by_id = {p["id"]: p for p in prs}
    free = {p["id"]: b for p in prs if (b := _free(p, active))}
    tasks: list[dict] = []
    used: set[tuple] = set()

    def task(p, r, kind, n=None, split=None, why=""):
        n = n or max(1, _plate_n(r))
        h = hours(pp, kind, str(p.get("model") or ""), n if kind != "names" else 1)
        end = now + h * 3600
        used.add((r["batch_id"], r["file"]))
        tasks.append({"type": "start", "printer_id": p["id"], "printer": p["name"], "role": p.get("role") or "any",
                      "bed": free[p["id"]], "kind": kind, "batch_id": r["batch_id"], "file": r["file"],
                      "names": r.get("names") or [], "colors": r.get("colors") or [], "n": n,
                      "split": split, "hours": h, "ends_at": end, "night": end > cut or not staffed(end, sh),
                      "idle_after": 0 if staffed(end, sh) else round((next_staffed(end, sh) - end) / 3600, 1),
                      "product": r.get("product") or "", "express": bool(r.get("express_by")),
                      "fix": r.get("extra") == "fix", "why": why})
        del free[p["id"]]

    day_letters = ph in ("day", "prep")
    # name stands first on Flex (X1), then Letters printers; letters on Letters printers (colour fit), day only
    for kind, roles in ((farm.STANDS, ("flex", "letters")), ("names", ("letters", "flex", "any"))):
        rows = [r for r in mk.get("stands" if kind == farm.STANDS else "names") or [] if _startable(r)]
        for role in roles:
            cand = [dict(by_id[i], status={**(by_id[i].get("status") or {}), "awaiting_plate_clear": False,
                                           "state": "IDLE"})
                    for i in list(free) if (by_id[i].get("role") or "any") == role
                    and (kind != farm.STANDS or farm.has_ams(by_id[i]))]
            if not cand or not rows:
                continue
            pick = makeit.choose(rows, cand, {})
            for r in rows:
                pid = pick.get(r["file"])
                if pid is None or pid not in free or (r["batch_id"], r["file"]) in used:
                    continue
                h = hours(pp, kind, str(by_id[pid].get("model") or ""), 1 if kind == "names" else _plate_n(r))
                if kind == "names" and (not day_letters or now + h * 3600 > cut):
                    continue  # nobody to take letters off at night
                task(by_id[pid], r, kind)
            rows = [r for r in rows if (r["batch_id"], r["file"]) not in used]
    # bases: every other free printer; letters printers only from the night round on (or with nothing else to do)
    bases = sorted((r for r in mk.get("bases") or [] if _startable(r)), key=_rank)
    letters_waiting = any(_startable(r) for r in mk.get("names") or [])
    for pid in sorted(free, key=lambda i: ((by_id[i].get("role") or "any") == "letters", by_id[i]["name"])):
        p = by_id[pid]
        role = p.get("role") or "any"
        left = [r for r in bases if (r["batch_id"], r["file"]) not in used]
        if not left:
            break
        if role == "letters" and ph not in ("round", "night") and letters_waiting:
            continue
        per_h = hours(pp, "bases", str(p.get("model") or ""), 1)
        most = max(_plate_n(r) for r in left)
        if role == "letters" and ph not in ("round", "night"):
            most = 1  # a short base plate in the day keeps the letters printer free for letters
        n, _ = best_size(now, per_h, min(3, most), sh)
        exact = next((r for r in left if _plate_n(r) == n), None)
        bigger = next((r for r in left if _plate_n(r) > n and _splittable(r)), None)
        if exact is not None and (bigger is None or left.index(exact) <= left.index(bigger) + 2):
            task(p, exact, "bases", n)
        elif bigger is not None:
            task(p, bigger, "bases", n, split=[n, _plate_n(bigger) - n])
        else:
            r = next((r for r in left if _plate_n(r) < n), left[0])
            task(p, r, "bases", _plate_n(r))
    # printers with nothing to start, and what's printing
    printing = []
    for p in prs:
        s = p.get("status") or {}
        if not p.get("active", True):
            continue
        if not s.get("connected"):
            printing.append({"printer": p["name"], "state": "offline"})
            continue
        if makeit.bed_state(p) == "busy":
            rem = s.get("remaining_time")
            end = now + float(rem) * 60 if rem is not None else None
            printing.append({"printer": p["name"], "state": "printing", "ends_at": end, "progress": s.get("progress"),
                             "night": bool(end and not staffed(end, sh)), "role": p.get("role") or "any"})
        elif p["id"] in free:
            printing.append({"printer": p["name"], "state": "idle", "bed": free[p["id"]]})
        elif active.get(p["id"]):
            printing.append({"printer": p["name"], "state": "starting"})
    printing.sort(key=lambda x: (x.get("ends_at") is None, x.get("ends_at") or 0))
    next_end = next((x["ends_at"] for x in printing if x.get("ends_at") and staffed(x["ends_at"], sh)), None)
    tasks.sort(key=lambda t: (not t["fix"], not t["express"], t["kind"] != farm.STANDS, t["printer"]))
    three = sum(1 for r in bases if _plate_n(r) >= 3 and (r["batch_id"], r["file"]) not in used)
    ready = sum(_plate_n(r) for r in bases if (r["batch_id"], r["file"]) not in used)
    return {"now": now, "phase": ph, "shift": sh, "tasks": tasks, "printing": printing, "next_end": next_end,
            "plan": plan(prs, tasks, now, sh, pp, ready),
            "verify": len(mk.get("verify") or []), "per_piece": pp,
            "night": {"three_base_plates": three, "printers": sum(1 for p in prs if p.get("active", True)
                                                                    and (p.get("status") or {}).get("connected")),
                      "prep_from": day_end - PREP_MIN * 60, "round_from": day_end - ROUND_MIN * 60,
                      "merged_today": NIGHT.merged_on == datetime.fromtimestamp(now).date().isoformat(),
                      "merged": NIGHT.merged}}


def _rank(r: dict) -> tuple:
    """Base plates in the order they are started: fix plates, Express, clubbed orders waiting for this name, the
    rest of a split plate (0.43: what a split left for later comes next), then Print's own order."""
    return (r.get("extra") != "fix", r.get("express_by") is None, r.get("express_by") or "", not r.get("club_wait"),
            r.get("extra") != "divided")


def plan(prs: list[dict], tasks: list[dict], now: float, sh: dict, pp: dict, ready: int) -> dict:
    """The rest of the day per printer (0.43): now → plates sized to end by 21:30 → the night plate. Rough: a
    letters printer prints letters through the day; every plate is base plates. `need` = bases the plan still
    has to find (beyond the plates already started now), `ready` = bases on plates not started."""
    _, en = shift_window(sh)
    day_end = datetime.fromtimestamp(now).replace(hour=en // 60, minute=en % 60, second=0, microsecond=0).timestamp()
    if day_end <= now - 12 * 3600:
        day_end += 86400
    last = day_end - LEAVE_MIN * 60
    by_task = {t["printer_id"]: t for t in tasks}
    rows, need = [], 0
    for p in sorted(prs, key=lambda p: p["name"]):
        s = p.get("status") or {}
        if not p.get("active", True) or not s.get("connected"):
            continue
        role = p.get("role") or "any"
        per_h = hours(pp, "bases", str(p.get("model") or ""), 1)
        slots: list[dict] = []
        t = now
        tk = by_task.get(p["id"])
        if tk is not None:
            slots.append({"kind": tk["kind"], "n": tk["n"], "start": now, "end": tk["ends_at"], "night": tk["night"],
                          "now": True})
            t = tk["ends_at"]
        elif makeit.bed_state(p) == "busy":
            rem = s.get("remaining_time")
            t = now + float(rem) * 60 if rem is not None else now
            slots.append({"kind": "printing", "start": now, "end": t, "night": t > last, "now": True})
        if slots and slots[-1]["night"]:
            rows.append({"printer": p["name"], "role": role, "slots": slots})
            continue
        # someone takes it off ~15 min after it ends while they are here, else when the shift starts
        t = max((t + REACT_H * 3600 if staffed(t, sh) else next_staffed(t, sh)) if slots else next_staffed(t, sh), now)
        if role == "letters" and t < last:
            slots.append({"kind": "names", "start": t, "end": last, "night": False})
            t = last
        while t < day_end:
            n, _ = best_size(t, per_h, 3, sh)
            end = t + n * per_h * 3600
            night = end > last or not staffed(end, sh)  # runs past 21:30: the night plate
            slots.append({"kind": "bases", "n": n, "start": t, "end": end, "night": night})
            need += n
            if night:
                break
            t = end + REACT_H * 3600
        rows.append({"printer": p["name"], "role": role, "slots": slots})
    return {"printers": rows, "need": need, "ready": ready, "short": max(0, need - ready), "last": last}


def start(mk_prs: list[dict], batch_id: int, file: str, printer_id: int, split: list[int] | None = None,
          bed_clear: bool = False) -> dict:
    """The board's button: split the plate first when the board said so (`split` = sizes, the first part is
    printed; extras.divide), then Start print (the bed check included)."""
    p = next((x for x in mk_prs if x["id"] == int(printer_id)), None)
    if p is None:
        raise farm.FarmError("Choose a printer")
    if makeit.bed_state(p) == "check" and not bed_clear:
        raise makeit.BedNotClear(f"{p['name']} may still have a finished print on its bed. Take it off and clean "
                                 "the plate, then confirm.")
    parts = None
    if split and len(split) >= 2:
        res = extras.divide(batch_id, file, [int(x) for x in split], staff="Today's work")
        file, parts = res["files"][0], res["files"]
    out = makeit.start_print(batch_id, file, int(printer_id), {}, mk_prs, bed_clear=bed_clear)
    return {**out, "file": file, "split": parts}


# ---------------- night plates: from 20:00, small base plates merged into 3-base ones (once a day) ----------------
class NightPrep:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self.merged_on = ""
        self.merged: list[str] = []
        self.board_fn = None  # set by the server: () -> the Print board (with printers)

    def start(self, board_fn) -> None:
        self.board_fn = board_fn
        if os.environ.get("SNAPSTL_NO_NIGHTPREP") or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-nightprep")
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.once()
            except Exception as e:  # noqa: BLE001
                print(f"SnapSTL: night plates: {e}")
            time.sleep(300)

    def once(self, now: float | None = None, mk: dict | None = None) -> list[str]:
        now = now or time.time()
        today = datetime.fromtimestamp(now).date().isoformat()
        sh = farm.shift()
        if phase(now, sh) not in ("prep", "round") or self.merged_on == today:
            return []
        mk = mk or (self.board_fn() if self.board_fn else None)
        if not mk:
            return []
        prs = mk.get("printers") or []
        need = sum(1 for p in prs if p.get("active", True) and (p.get("status") or {}).get("connected"))
        have = sum(1 for r in mk.get("bases") or [] if _startable(r) and _plate_n(r) >= 3)
        done = []
        for s in extras.base_suggestions(mk.get("bases") or [], estimate.model(prs)):
            if have >= need:
                break
            if s["names"] < 3:
                continue
            try:
                r = extras.merge([{"batch_id": x["batch_id"], "file": x["file"]} for x in s["plates"]],
                                 staff="Today's work")
                done.append(r.get("file") or "")
                have += 1
            except extras.ExtraError:
                continue
        self.merged_on, self.merged = today, done
        return done


NIGHT = NightPrep()
