"""0.35: Print — base plates merge (up to 3 bases), print time estimates from done prints, products on every plate,
clubbed orders whose other names are made come first; Stats print times."""
import json
import sqlite3
from pathlib import Path

import pytest

from snapstl import db, estimate
from snapstl.threemf import read_3mf

from .test_farm import bb, build_and_wait, helper_work, report  # noqa: F401  (bb is a fixture)
from .test_flow import client  # noqa: F401  (client is a fixture)


@pytest.fixture(autouse=True)
def _clean_after(client):  # noqa: F811
    yield
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def one_batch(c, tag, name):
    data = report([(tag + "1", "PRINT", "Magicians_35", name, "Pink", 1)])
    assert c.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    bid = c.post("/api/batches", json={"order_ids": [tag + "1"]}).json()["batch_id"]
    c.post(f"/api/batches/{bid}/download")
    helper_work(c)
    b = build_and_wait(c, bid)
    assert b["status"] == "done", b["summary"].get("errors")
    return bid, Path(b["summary"]["folder"])


def base_of(c, bid):
    return next(r for r in c.get("/api/makeit").json()["bases"] if r["batch_id"] == bid and not r["extra"])


def test_base_plates_merge_and_split_back(client, bb):  # noqa: F811
    b1, _ = one_batch(client, "MB", "Ayra")
    b2, f2 = one_batch(client, "MC", "Riya")
    p1, p2 = base_of(client, b1), base_of(client, b2)
    assert p1["product"] == "organizer" and p1["footprints"] and p1["bed"]
    ideas = client.get("/api/makeit").json()["merge_bases"]
    assert len(ideas) == 1 and {p["batch_id"] for p in ideas[0]["plates"]} == {b1, b2} and ideas[0]["names"] == 2
    # bases merge with bases only
    letters = next(r for r in client.get("/api/makeit").json()["names"] if r["batch_id"] == b1)
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": b1, "file": p1["file"]},
                                                          {"batch_id": b1, "file": letters["file"]}]})
    assert r.status_code == 409 and "base plates with base plates" in r.json()["detail"]
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": b1, "file": p1["file"]},
                                                          {"batch_id": b2, "file": p2["file"]}]})
    assert r.status_code == 200, r.text
    merged = r.json()["file"]
    assert merged.startswith("Merged/Merged - Bases - ") and len(read_3mf(f2 / merged)) == 2
    bases = [(x["batch_id"], x["file"], x["extra"]) for x in client.get("/api/makeit").json()["bases"]]
    assert (b2, merged, "merged") in bases and (b1, p1["file"], None) not in bases
    assert client.post("/api/makeit/split", json={"batch_id": b2, "file": merged}).status_code == 200
    bases = [(x["batch_id"], x["file"]) for x in client.get("/api/makeit").json()["bases"]]
    assert (b1, p1["file"]) in bases and (b2, p2["file"]) in bases and (b2, merged) not in bases


def test_at_most_three_bases(client, bb):  # noqa: F811
    from .test_farm import built_batch

    b1, _ = built_batch(client, "MD")  # 2 bases on one plate
    b2, _ = built_batch(client, "ME")
    p1, p2 = base_of(client, b1), base_of(client, b2)
    assert client.get("/api/makeit").json()["merge_bases"] == []  # 2 + 2 > 3
    r = client.post("/api/makeit/merge", json={"plates": [{"batch_id": b1, "file": p1["file"]},
                                                          {"batch_id": b2, "file": p2["file"]}]})
    assert r.status_code == 409 and "At most 3 bases" in r.json()["detail"]


def test_clubbed_plate_first_and_choices_carry_estimates(client, bb):  # noqa: F811
    one_batch(client, "MF", "Ayra")
    b2, _ = one_batch(client, "MG", "Riya")
    data = report([("MG2", "PRINT", "Magicians_35", "Lia", "Pink", 1)])
    assert client.post("/api/ingest", files={"file": ("r.csv", data, "text/csv")}).status_code == 200
    with db.tx() as con:  # MG1's clubbed partner is already made
        con.execute("UPDATE orders SET clubbed_id='CLX' WHERE order_id IN ('MG1','MG2')")
        con.execute("UPDATE orders SET state='bundled', made_at=1.0 WHERE order_id='MG2'")
    bases = client.get("/api/makeit").json()["bases"]
    assert bases[0]["batch_id"] == b2 and bases[0]["club_wait"] == ["MG1"]
    c0 = bases[0]["choices"][0]
    assert "est" in c0 and c0["est"] is None and c0["free_in"] == 0  # no prints done yet: no guess


def _fake_history(path, rows):
    """rows: (kind, printer_id, names, minutes, speed)"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY, summary TEXT)")
    con.execute("CREATE TABLE extra_plates (batch_id INT, file TEXT, items TEXT)")
    con.execute("CREATE TABLE print_jobs (batch_id INT, plate TEXT, kind TEXT, printer_id INT, started_at REAL, "
                "updated_at REAL, speed INT, state TEXT)")
    plates = []
    for i, (kind, pid, names, mins, speed) in enumerate(rows):
        plates.append({"file": f"p{i}.3mf", "names": names})
        con.execute("INSERT INTO print_jobs VALUES (1,?,?,?,?,?,?, 'done')", (f"p{i}.3mf", kind, pid, 1000.0,
                                                                              1000.0 + mins * 60, speed))
    con.execute("INSERT INTO batches VALUES (1, ?)", (json.dumps({"plates": plates}),))
    con.execute("INSERT INTO extra_plates VALUES (1, 'fix.3mf', ?)", (json.dumps([{"name": "Ayra", "letters": "ra"}]),))
    con.commit()
    return con


def test_estimate_lines_and_fallbacks(tmp_path):
    assert estimate.letters(["Ayra", "Riya Sen"]) == 11 and estimate.letters(["Ayra (ra)"]) == 2
    rows = []
    for i in range(12):  # printer 1: 70 + 30 / letter at Sport
        n = 4 + i % 6
        rows.append(("bases", 1, ["x" * n], 70 + 30 * n, 3))
    for i in range(6):  # printer 2 (another A1), printed at Standard: 124/100 slower
        n = 5 + i % 3
        rows.append(("bases", 2, ["y" * n], (80 + 32 * n) * 124 / 100, 2))
    rows.append(("bases", 1, ["z" * 6], 2000, 3))  # left on the bed for a day: dropped
    con = _fake_history(tmp_path / "h.db", rows)
    m = estimate.build(con, {1: "P1S", 2: "A1"})
    own = estimate.minutes(m, "bases", 10, 1, "P1S")
    assert own["basis"] == "this printer" and abs(own["min"] - 370) <= 3
    a1 = estimate.minutes(m, "bases", 6, 2, "A1")
    assert a1["basis"] == "A1 printers" and abs(a1["min"] - (80 + 32 * 6)) <= 3  # brought to Sport
    assert estimate.minutes(m, "bases", 6, 9, "X1")["basis"] == "all printers"
    assert estimate.minutes(m, "names", 6, 1, "P1S") is None  # no letter prints: no guess
