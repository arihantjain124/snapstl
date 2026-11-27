"""0.41: printer care — maintenance set up in Bambuddy, shown on Today's work, ticked there; why a print failed."""
import re
from datetime import datetime

import pytest

from snapstl import db, farm, printercare

from .test_flow import client  # noqa: F401  (client is a fixture)


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m).timestamp()


class FakeCare:
    """Bambuddy's /maintenance/* and /archives/* as they answered on the owner's Bambuddy (16–28 Nov 2026)."""

    SYSTEM = {"Clean Carbon Rods": 100, "Lubricate Linear Rails": 50, "Clean Linear Rails": 100,
              "Clean Nozzle/Hotend": 100, "Check Belt Tension": 200, "Clean Build Plate": 25, "Check PTFE Tube": 500}

    def __init__(self):
        self.calls = []
        self.types = [{"id": i + 1, "name": n, "default_interval_hours": h, "is_system": True}
                      for i, (n, h) in enumerate(self.SYSTEM.items())]
        self.printers = {1: {"name": "X8 BOB", "model": "P1S", "hours": 223.8},
                         2: {"name": "A5", "model": "A1", "hours": 131.2},
                         3: {"name": "X3 BOB", "model": "P1S", "hours": 213.5}}
        self.items = []
        for pid, p in self.printers.items():
            names = (["Clean Carbon Rods"] if p["model"] == "P1S" else ["Lubricate Linear Rails", "Clean Linear Rails"]) + [
                "Clean Nozzle/Hotend", "Check Belt Tension", "Clean Build Plate", "Check PTFE Tube"]
            for n in names:
                self._assign(pid, next(t for t in self.types if t["name"] == n))
        self.archives = []
        self.patched = {}

    def _assign(self, pid, t):
        self.items.append({"id": len(self.items) + 1, "printer_id": pid, "type": t, "custom": None, "enabled": True,
                           "since": self.printers[pid]["hours"], "performed": None})

    def _view(self, it):
        h = it["custom"] or it["type"]["default_interval_hours"]
        return {"id": it["id"], "printer_id": it["printer_id"], "maintenance_type_name": it["type"]["name"],
                "enabled": it["enabled"], "interval_hours": h, "interval_type": "hours",
                "hours_until_due": h - it["since"], "is_due": it["since"] >= h, "last_performed_at": it["performed"],
                "description": it["type"].get("description")}

    def _printer(self, pid):
        p = self.printers[pid]
        return {"printer_id": pid, "printer_name": p["name"], "printer_model": p["model"], "total_print_hours": p["hours"],
                "maintenance_items": [self._view(i) for i in self.items if i["printer_id"] == pid]}

    def api(self, method, path, *, cfg=None, url=None, key=None, json_body=None, files=None, timeout=20):
        self.calls.append((method, path, json_body))
        if (method, path) == ("GET", "/maintenance/types"):
            return self.types
        if (method, path) == ("POST", "/maintenance/types"):
            t = {"id": len(self.types) + 1, "is_system": False, **json_body}
            self.types.append(t)
            return t
        if (method, path) == ("GET", "/maintenance/overview"):
            return [self._printer(pid) for pid in self.printers]
        if m := re.fullmatch(r"/maintenance/printers/(\d+)/assign/(\d+)", path):
            self._assign(int(m[1]), next(t for t in self.types if t["id"] == int(m[2])))
            return {}
        if m := re.fullmatch(r"/maintenance/printers/(\d+)", path):
            return self._printer(int(m[1]))
        if m := re.fullmatch(r"/maintenance/items/(\d+)", path):
            it = self.items[int(m[1]) - 1]
            if "custom_interval_hours" in json_body:
                it["custom"] = json_body["custom_interval_hours"]
            if json_body.get("enabled") is not None:
                it["enabled"] = json_body["enabled"]
            return {}
        if m := re.fullmatch(r"/maintenance/items/(\d+)/perform", path):
            it = self.items[int(m[1]) - 1]
            it["since"], it["performed"] = 0.0, "2026-11-28T10:00:00"
            return {}
        if method == "GET" and path.startswith("/archives/?"):
            return self.archives
        if m := re.fullmatch(r"/archives/(\d+)", path):
            self.patched[int(m[1])] = json_body
            return {}
        raise AssertionError(f"unexpected Bambuddy call {method} {path}")

    def task_names(self, pid, enabled_only=True):
        return {i["type"]["name"]: (i["custom"] or i["type"]["default_interval_hours"])
                for i in self.items if i["printer_id"] == pid and (i["enabled"] or not enabled_only)}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeCare()
    monkeypatch.setattr(farm, "api", f.api)
    monkeypatch.setattr(printercare, "_FILE", tmp_path / "printercare.json")
    printercare._CACHE.clear()
    with db.tx() as con:
        con.execute("DELETE FROM care_log")
        con.execute("DELETE FROM print_failures")
    yield f
    printercare._CACHE.clear()


def test_setup_puts_our_tasks_and_intervals_in_bambuddy_once(client, fake):  # noqa: F811
    assert not printercare.is_set_up()
    r = printercare.setup()
    assert sorted(r["made"]) == ["Check Heatbed Cable", "Clean Extruder Gears", "Clean Fans and Filter",
                                 "Grease Z Lead Screws"]
    assert fake.task_names(1) == {"Clean Carbon Rods": 100, "Clean Nozzle/Hotend": 100, "Check Belt Tension": 200,
                                  "Check PTFE Tube": 500, "Clean Extruder Gears": 200, "Grease Z Lead Screws": 500,
                                  "Clean Fans and Filter": 500}  # Clean Build Plate off
    a1 = fake.task_names(2)
    assert a1["Lubricate Linear Rails"] == 500 and a1["Check Heatbed Cable"] == 200 and "Clean Carbon Rods" not in a1
    assert "Clean Fans and Filter" not in a1 and "Clean Build Plate" not in a1
    assert printercare.is_set_up()
    n = len(fake.calls)
    again = printercare.setup()
    assert again == {"made": [], "assigned": 0, "changed": 0}
    assert all(m == "GET" for m, _, _ in fake.calls[n:])  # nothing changed the second time


def test_a_full_service_first_one_printer_a_day_worst_first(client, fake):  # noqa: F811
    printercare.setup()
    ov = printercare.overview()
    # X8 and A5 free: A5 is first in line (most failures), only one full service a day
    c = printercare.cards({1, 2, 3}, ov, now=at(28, 10))
    assert [(x["printer"], x["full"]) for x in c] == [("A5", True)]  # the others wait for their full service
    assert c[0]["minutes"] == sum(i["minutes"] for i in c[0]["items"]) and len(c[0]["items"]) == 8 and c[0]["minutes"] == 43
    assert printercare.waiting_full(ov) == ["A5", "X8 BOB", "X3 BOB"]
    with pytest.raises(printercare.CareError):
        printercare.done(2, [999], False)
    printercare.done(2, None, True, staff="Ravi")
    assert all(i["since"] == 0.0 for i in fake.items if i["printer_id"] == 2 and i["enabled"])
    ov = printercare.overview()
    assert printercare.waiting_full(ov) == ["X8 BOB", "X3 BOB"]
    # the same day: no second full service
    with db.tx() as con:
        con.execute("UPDATE care_log SET at=?", (at(28, 10, 30),))
    assert printercare.cards({1, 3}, ov, now=at(28, 15)) == []
    # the next day: X8 (before X3)
    assert [(x["printer"], x["full"]) for x in printercare.cards({1, 3}, ov, now=at(29, 9))] == [("X8 BOB", True)]


def test_after_the_full_service_due_tasks_and_one_long_card_a_day(client, fake):  # noqa: F811
    printercare.setup()
    for pid in (1, 2):
        printercare.done(pid, None, True)
    with db.tx() as con:
        con.execute("UPDATE care_log SET at=?", (at(26, 10),))
    for it in fake.items:  # 120 h printed since on X8, 520 h on A5
        it["since"] = 120.0 if it["printer_id"] == 1 else 520.0
    printercare._CACHE.clear()
    c = {x["printer"]: x for x in printercare.cards({1, 2}, now=at(28, 11))}
    assert {i["name"] for i in c["X8 BOB"]["items"]} == {"Clean Carbon Rods", "Clean Nozzle/Hotend"}
    assert not c["X8 BOB"]["full"]
    assert "Grease Z Lead Screws" in {i["name"] for i in c["A5"]["items"]}  # the long ones on one printer
    a5 = c["A5"]
    printercare.done(2, [i["id"] for i in a5["items"]], False, staff="Ravi")
    with db.tx() as con:
        con.execute("UPDATE care_log SET at=? WHERE kind='tasks'", (at(28, 11, 30),))
        assert tuple(con.execute("SELECT staff, kind FROM care_log WHERE kind='tasks'").fetchone()) == ("Ravi", "tasks")
    for it in fake.items:
        if it["printer_id"] == 1:
            it["since"] = 520.0
    printercare._CACHE.clear()
    c = {x["printer"]: x for x in printercare.cards({1, 2}, now=at(28, 16))}
    assert "A5" not in c and not any(i["long"] for i in c["X8 BOB"]["items"])  # a long card was done today


def test_why_a_print_failed_one_tap(client, fake):  # noqa: F811
    fake.archives = [
        {"id": 724, "printer_id": 1, "status": "failed", "print_name": "Bases - A + B", "completed_at": "2026-11-28T09:10:00",
         "actual_time_seconds": 3 * 3600},
        {"id": 725, "printer_id": 2, "status": "completed", "print_name": "Bases - C", "completed_at": "2026-11-28T09:20:00"},
        {"id": 700, "printer_id": 2, "status": "aborted", "print_name": "Bases - D", "completed_at": "2026-11-26T09:00:00"},
    ]
    f = printercare.failures(now=at(28, 12))
    assert [(x["archive_id"], x["hours"]) for x in f] == [(724, 3.0)]  # older than 24 h / completed: not asked
    with pytest.raises(printercare.CareError):
        printercare.note_failure(724, "bad")
    r = printercare.note_failure(724, "edges", staff="Ravi", printer="X8 BOB", printer_id=1)
    assert r["bambuddy"] and fake.patched[724] == {"failure_reason": "Edges lifted (warping)"}
    assert printercare.failures(now=at(28, 12)) == []
    with db.tx() as con:
        st = printercare.printer_stats(con, now=at(28, 12))
    x8 = next(p for p in st["printers"] if p["printer"] == "X8 BOB")
    assert x8["reasons"] == {"Edges lifted (warping)": 1}


def test_stats_per_printer_week(client, fake):  # noqa: F811
    with db.tx() as con:
        con.execute("DELETE FROM print_jobs WHERE printer_name='Z9 test'")
        for st, day in (("done", 27), ("failed", 27), ("done", 20), ("cancelled", 27)):
            con.execute("INSERT INTO print_jobs(batch_id, plate, kind, printer_id, printer_name, state, created_at, "
                        "updated_at) VALUES (1, 'p', 'bases', 99, 'Z9 test', ?, ?, ?)", (st, at(day, 10), at(day, 10)))
        st = printercare.printer_stats(con, now=at(28, 12))
        con.execute("DELETE FROM print_jobs WHERE printer_name='Z9 test'")
    z = next(p for p in st["printers"] if p["printer"] == "Z9 test")
    assert st["weeks"][-1] == "2026-11-23" and z["weeks"][-1] == {"prints": 2, "failed": 1}
    assert z["weeks"][-2] == {"prints": 1, "failed": 0}


def test_the_board_carries_care_and_the_page_has_the_sound(client):  # noqa: F811
    b = client.get("/api/today").json()
    assert b["care"] == [] and b["failures"] == [] and "edges" in b["reasons"]
    page = client.get("/").text
    assert 'id="tdsound"' in page and "Why did it fail?" in page and 'id="stcare"' in page
    assert "care" in client.get("/api/stats").json()
