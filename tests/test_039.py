"""0.39: Today's work — plates sized to end while someone is there, the board's tasks, its Start button."""
from datetime import datetime

import pytest

from snapstl import extras, farm, makeit, today

from .test_flow import client  # noqa: F401  (client is a fixture: a fresh database)

SH = {"start": "08:00", "end": "22:00"}


def at(h, m=0):
    return datetime(2026, 11, 28, h, m).timestamp()


def test_phase_and_staffed():
    assert today.phase(at(9), SH) == "day" and today.phase(at(20, 5), SH) == "prep"
    assert today.phase(at(21, 10), SH) == "round" and today.phase(at(23), SH) == "night"
    assert today.phase(at(7, 59), SH) == "night"
    assert today.staffed(at(21, 44), SH) and not today.staffed(at(21, 46), SH) and not today.staffed(at(3), SH)
    assert today.next_staffed(at(23), SH) == datetime(2026, 11, 29, 8, 0).timestamp()
    assert today.next_staffed(at(5), SH) == at(8)


@pytest.mark.parametrize("hm,per,want", [
    ((14, 3), 3.3, 2),   # P1S free after lunch: 2 bases end ~20:40, then the night plate
    ((21, 10), 3.3, 3),  # night round: 3 bases through the night
    ((21, 50), 4.0, 3),  # A1 at 21:50: 3 bases end ~09:50, someone is there
    ((17, 54), 3.3, 1),  # late afternoon: 1 base ends ~21:12 (2 would end at night and wait ~6.6 h)
])
def test_best_size(hm, per, want):
    n, idle = today.best_size(at(*hm), per, 3, SH)
    assert n == want, (hm, n, idle)


def _printer(pid, name, role="bases", model="P1S", state="FINISH", connected=True, rem=None, clear=False):
    return {"id": pid, "name": name, "role": role, "model": model, "active": True,
            "status": {"connected": connected, "state": state, "remaining_time": rem, "awaiting_plate_clear": clear},
            "spools": [{"tray": 0, "color": "Pink", "hex": "#F5A9B8"}]}


def _row(bid, file, kind, names, colors=None):
    return {"batch_id": bid, "file": file, "kind": kind, "names": names, "open_items": [f"{file}{i}" for i in
                                                                                        range(len(names))],
            "colors": colors or [], "job": None, "job_active": False, "extra": None, "express_by": None}


def test_board_gives_free_printers_a_plate_sized_to_the_shift(client, monkeypatch):  # noqa: F811 (client: fresh db)
    monkeypatch.setattr(makeit, "bed_state", lambda p: "busy" if p["status"]["state"] == "RUNNING" else "check")
    monkeypatch.setattr(farm, "active_counts", lambda: {})
    monkeypatch.setattr(farm, "has_ams", lambda p: True)
    prs = [_printer(1, "X4"), _printer(2, "X6", role="letters"), _printer(3, "A1", model="A1", state="RUNNING", rem=120),
           _printer(4, "X2", connected=False), _printer(5, "X1", role="flex")]
    mk = {"bases": [_row(1, "B1.3mf", "bases", ["Asha", "Ravi", "Tara"]), _row(1, "B2.3mf", "bases", ["Meena"])],
          "names": [_row(1, "N1.3mf", "names", ["Asha"], ["pink"])], "stands": [_row(2, "S1.3mf", "stands", ["ROCKET"])],
          "verify": [{}, {}]}
    pp = {"bases": {"A1": 4.0, "P1S": 3.3, "": 3.5}, "names": {"": 0.4}, "stands": {"": 4.0}}
    b = today.build(mk, prs, now=at(14, 3), sh=SH, pp=pp)
    t = {x["printer"]: x for x in b["tasks"]}
    assert set(t) == {"X4", "X6", "X1"}
    assert t["X1"]["kind"] == "stands"                                   # Flex: name stands first
    assert t["X6"]["kind"] == "names" and t["X6"]["bed"] == "check"      # Letters printer: letters in the day
    assert t["X4"]["kind"] == "bases" and t["X4"]["n"] == 2 and t["X4"]["split"] == [2, 1]
    assert not t["X4"]["night"] and t["X4"]["file"] == "B1.3mf"
    st = {x["printer"]: x["state"] for x in b["printing"]}
    assert st["A1"] == "printing" and st["X2"] == "offline" and b["verify"] == 2 and b["phase"] == "day"
    # the night round: the letters printer gets a base plate, every plate is a night plate
    three = [_row(1, f"T{i}.3mf", "bases", ["A", "B", "C"]) for i in range(3)]
    b = today.build({**mk, "bases": three, "names": [], "stands": []}, prs, now=at(21, 10), sh=SH, pp=pp)
    t = {x["printer"]: x for x in b["tasks"]}
    assert b["phase"] == "round" and t["X4"]["n"] == 3 and t["X4"]["split"] is None and t["X4"]["night"]
    assert t["X6"]["kind"] == "bases"
    # letters at night: never (nobody to take them off)
    b = today.build({**mk, "bases": []}, prs, now=at(21, 50), sh=SH, pp=pp)
    assert all(x["kind"] != "names" for x in b["tasks"])


def test_start_splits_then_starts(monkeypatch):
    calls = []
    p = _printer(7, "X5")
    monkeypatch.setattr(makeit, "bed_state", lambda pr: "check")
    monkeypatch.setattr(extras, "divide", lambda bid, f, sizes, staff="": calls.append(("divide", f, sizes))
                        or {"files": ["Split/a.3mf", "Split/b.3mf"]})
    monkeypatch.setattr(makeit, "start_print", lambda bid, f, pid, opts, prs, bed_clear=False:
                        calls.append(("start", f, pid, bed_clear)) or {"job_id": 1, "started": True})
    with pytest.raises(makeit.BedNotClear):
        today.start([p], 1, "B1.3mf", 7, [2, 1])
    assert calls == []  # nothing split while the bed isn't confirmed clear
    out = today.start([p], 1, "B1.3mf", 7, [2, 1], bed_clear=True)
    assert calls == [("divide", "B1.3mf", [2, 1]), ("start", "Split/a.3mf", 7, True)] and out["file"] == "Split/a.3mf"


def test_night_prep_merges_once_a_day(monkeypatch):
    monkeypatch.setattr(farm, "shift", lambda cfg=None: SH)
    merged = []
    monkeypatch.setattr(extras, "base_suggestions", lambda rows, est=None: [
        {"names": 3, "plates": [{"batch_id": 1, "file": "a"}, {"batch_id": 1, "file": "b"}]},
        {"names": 2, "plates": [{"batch_id": 1, "file": "c"}, {"batch_id": 1, "file": "d"}]}])
    monkeypatch.setattr(extras, "merge", lambda plates, staff="": merged.append(plates) or {"file": "Merged/x.3mf"})
    monkeypatch.setattr(today.estimate, "model", lambda prs: {})
    mk = {"printers": [_printer(1, "X4"), _printer(2, "X5")], "bases": [_row(1, "B1.3mf", "bases", ["A", "B", "C"])]}
    n = today.NightPrep()
    assert n.once(now=at(15), mk=mk) == []  # the day: nothing
    assert n.once(now=at(20, 10), mk=mk) == ["Merged/x.3mf"] and len(merged) == 1  # 2 printers, 1 ready → 1 merge
    assert n.once(now=at(21, 0), mk=mk) == []  # once a day
