"""0.24: Print → Verify (checks by a named person, fix plates), merged plates, printed by hand, bed clear.

Uses the fake Bambuddy and real built batches of tests/test_farm.py.
"""
import json
from pathlib import Path

import numpy as np
import trimesh

from snapstl import db, extras, farm, makeit, verify
from snapstl.threemf import read_3mf

from .test_farm import bb, built_batch  # noqa: F401  (bb is a fixture)
from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)
from .test_ingest import report
from snapstl.ingest import ingest

BASE, PINK, BLUE = "Bases/Bases - Ayra + Riya.3mf", "Names/pink/Pink - Ayra.3mf", "Names/blue/Blue - Riya.3mf"


def _tab(c, prefix):
    out = {}
    for t in ("printing", "pack_it", "ready_to_print"):
        for o in c.get(f"/api/orders?tab={t}&limit=100").json()["orders"]:
            if o["order_id"].startswith(prefix):
                out[o["order_id"]] = t
    return out


def _check(c, bid, file, results, staff="Asha"):
    return c.post("/api/verify", json={"carrier": {"batch_id": bid, "file": file}, "staff": staff, "results": results})


def _res(card, how):
    return [{"batch_id": i["batch_id"], "plate": i["plate"], "idx": i["idx"], "result": how(i)} for i in card["items"]]


def _cards(c):
    return {(x["batch_id"], x["file"]): x for x in c.get("/api/makeit").json()["verify"]}


def test_plate_files_have_easy_names(client, bb):
    bid, folder = built_batch(client, "PQ")
    assert all((folder / f).is_file() for f in (BASE, PINK, BLUE))


def test_fix_plates_for_a_broken_base_and_a_wrong_colour(client, bb):
    bid, folder = built_batch(client, "PV")
    assert client.post("/api/staff", json={"name": "Asha"}).status_code == 200
    for f in (BASE, PINK, BLUE):  # printed by hand (e.g. Removed → Mark printed by hand)
        assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": f}).status_code == 200
    m = client.get("/api/makeit").json()
    assert m["bases"] == [] and m["names"] == [] and {c["file"] for c in m["verify"]} == {BASE, PINK, BLUE}
    assert _tab(client, "PV") == {"PV1": "printing", "PV2": "printing"}
    cards = _cards(client)

    # Riya's base broke; Ayra's is fine → a fix plate with only Riya's base, same size as on the plate
    r = _check(client, bid, BASE, _res(cards[(bid, BASE)], lambda i: "good" if i["name"] == "Ayra" else "base"))
    assert r.status_code == 200, r.text
    fix_base = r.json()["fixes"][0]
    assert fix_base == "Fixes/Fix - Bases - Riya.3mf" and (folder / fix_base).is_file()
    fixed = read_3mf(folder / fix_base)
    orig = [o for o in read_3mf(folder / BASE) if o.name.startswith("Riya - Base")]
    assert len(fixed) == 1 and fixed[0].name.startswith("Riya - Base")
    ext = lambda o: sorted(np.ptp(o.vertices, axis=0).round(2))  # noqa: E731
    assert ext(fixed[0]) == ext(orig[0])
    # Ayra's name came out in the wrong colour → reprinted in pink; Riya's name is good
    r = _check(client, bid, PINK, _res(cards[(bid, PINK)], lambda i: "colour"))
    assert r.json()["fixes"] == ["Fixes/Fix - Pink - Ayra.3mf"]
    assert _check(client, bid, BLUE, _res(cards[(bid, BLUE)], lambda i: "good")).status_code == 200

    m = client.get("/api/makeit").json()
    assert m["verify"] == []
    assert [(r["file"], r["extra"]) for r in m["bases"]] == [(fix_base, "fix")]
    assert [(r["file"], r["extra"]) for r in m["names"]] == [("Fixes/Fix - Pink - Ayra.3mf", "fix")]
    assert _tab(client, "PV") == {"PV1": "printing", "PV2": "printing"}  # each waits for its fix
    # fix plates print and get checked like any plate
    for f in (fix_base, "Fixes/Fix - Pink - Ayra.3mf"):
        assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": f}).status_code == 200
    cards = _cards(client)
    assert set(cards) == {(bid, fix_base), (bid, "Fixes/Fix - Pink - Ayra.3mf")}
    assert cards[(bid, fix_base)]["extra"] == "fix" and cards[(bid, fix_base)]["items"][0]["fixes"] == 1
    for (b, f), card in cards.items():
        assert _check(client, b, f, _res(card, lambda i: "good")).status_code == 200
    assert _tab(client, "PV") == {"PV1": "pack_it", "PV2": "pack_it"}
    done = {r["order_id"]: r for r in client.get("/api/makeit").json()["completed"]}
    assert done["PV1"]["had_fix"] == 1 and done["PV2"]["checked_by"] == "Asha"

    # found later while packing: Completed → Found a problem → a fix plate, the order goes back to Printing
    it = next(i for i in done["PV1"]["items"] if i["part"] == "names")
    r = _check(client, it["batch_id"], it["plate"], [{**it, "result": "name"}])
    assert r.json()["fixes"][0].startswith("Fixes/Fix - Pink - Ayra")
    assert _tab(client, "PV")["PV1"] == "printing"
    # only real results; a staff name is needed
    assert _check(client, bid, BLUE, [{**it, "result": "letters"}]).status_code == 409  # no letters chosen
    assert _check(client, bid, BLUE, [{**it, "result": "good"}], staff="Nobody").status_code == 409


def test_spoiled_letters_only(client, bb, monkeypatch):
    """A name's letters are its separate pieces; only the chosen ones go on the fix plate."""
    def three_letters(b, it):
        boxes = []
        for i, w in enumerate((20, 18, 8)):
            m = trimesh.creation.box([w, 30, 4])
            m.apply_translation([i * 30, 0, 2])
            boxes.append(m)
        dot = trimesh.creation.box([4, 4, 4])
        dot.apply_translation([60, 22, 2])  # the dot of the i
        return trimesh.util.concatenate(boxes + [dot]), 0

    monkeypatch.setattr(extras, "item_mesh", three_letters)
    bid, folder = built_batch(client, "PS")
    client.post("/api/staff", json={"name": "Asha"})
    client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": PINK})
    card = _cards(client)[(bid, PINK)]
    it = card["items"][0]
    p = client.get(f"/api/verify/letters?batch_id={bid}&plate={PINK}&idx=0").json()["pieces"]
    assert [x["n"] for x in p] == [0, 1, 2] and [x["label"] for x in p] == ["1", "2", "3"]  # "Ayra" has 4 letters
    r = _check(client, bid, PINK, [{**{k: it[k] for k in ("batch_id", "plate", "idx")}, "result": "letters",
                                    "pieces": [2]}])
    assert r.status_code == 200, r.text
    f = r.json()["fixes"][0]
    objs = read_3mf(folder / f)
    assert len(objs) == 1 and "letter 3" in objs[0].name
    assert sorted(np.ptp(objs[0].vertices, axis=0).round(1))[-2:] == [8.0, 39.0]  # the i with its dot, not the others
    with db.tx() as con:
        assert con.execute("SELECT letters FROM plate_checks WHERE result='letters'").fetchone()[0] == "3"


def test_merge_name_plates_across_batches_and_split_back(client, bb):
    b1, f1 = built_batch(client, "PO")
    b2, f2 = built_batch(client, "PN")
    m = client.get("/api/makeit").json()
    sug = {s["color"]: s for s in m["merge"]}
    assert set(sug) == {"pink", "blue"} and sug["pink"]["names"] == 2
    plates = [{"batch_id": b1, "file": PINK}, {"batch_id": b2, "file": PINK}]
    assert client.post("/api/makeit/merge", json={"plates": plates + [{"batch_id": b1, "file": BLUE}]}).status_code == 409
    r = client.post("/api/makeit/merge", json={"plates": plates})
    assert r.status_code == 200, r.text
    merged = r.json()["file"]
    assert r.json()["batch_id"] == b2 and merged == "Merged/Merged - Pink - Ayra + Ayra.3mf"
    assert len(read_3mf(f2 / merged)) == 2
    names = [(r["batch_id"], r["file"], r["extra"]) for r in client.get("/api/makeit").json()["names"]]
    assert (b2, merged, "merged") in names and (b1, PINK, None) not in names and (b2, PINK, None) not in names
    # Split back while not started → the single plates return
    assert client.post("/api/makeit/split", json={"batch_id": b2, "file": merged}).status_code == 200
    names = [(r["batch_id"], r["file"]) for r in client.get("/api/makeit").json()["names"]]
    assert (b1, PINK) in names and (b2, PINK) in names and (b2, merged) not in names
    # merge again and print it: one check covers the orders of both batches
    merged = client.post("/api/makeit/merge", json={"plates": plates}).json()["file"]
    assert merged == "Merged/Merged - Pink - Ayra + Ayra (2).3mf"
    assert client.post("/api/makeit/by-hand", json={"batch_id": b2, "file": merged}).status_code == 200
    assert client.post("/api/makeit/split", json={"batch_id": b2, "file": merged}).status_code == 409  # printed
    client.post("/api/staff", json={"name": "Asha"})
    card = _cards(client)[(b2, merged)]
    assert card["extra"] == "merged" and sorted(i["order_id"] for i in card["items"]) == ["PN1", "PO1"]
    assert _check(client, b2, merged, _res(card, lambda i: "good")).status_code == 200
    assert _tab(client, "PO")["PO1"] == "printing"  # its base isn't printed yet
    for b in (b1, b2):
        client.post("/api/makeit/by-hand", json={"batch_id": b, "file": BASE})
        _check(client, b, BASE, _res(_cards(client)[(b, BASE)], lambda i: "good"))
    assert _tab(client, "PO")["PO1"] == "pack_it" and _tab(client, "PN")["PN1"] == "pack_it"


def test_bed_must_be_clear_before_a_print_starts(client, bb):
    client.get("/api/farm")
    bid, folder = built_batch(client, "PB")
    bb.slicer = "ok"
    # a finished print may still be on the bed → asked first; confirming clears the plate in Bambuddy
    bb.status[2].update(state="FINISH", awaiting_plate_clear=True)
    farm._status_cache.clear()
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": PINK, "printer_id": 2})
    assert r.status_code == 428 and "bed" in r.json()["detail"]
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": PINK, "printer_id": 2, "bed_clear": True})
    assert r.status_code == 200 and bb.cleared == [2]
    wait_for(lambda: bb.queue and bb.queue[1].get("manual_start") is False)
    # a printer busy with another print: the plate waits held in its queue (never starts on a full bed)
    bb.status[3].update(state="RUNNING")
    farm._status_cache.clear()
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": BLUE, "printer_id": 3})
    assert r.status_code == 200 and r.json()["held"]
    wait_for(lambda: len(bb.queue) == 2)
    assert bb.queue[2]["manual_start"] is True
    r = client.post("/api/makeit/start", json={"batch_id": bid, "file": BLUE, "printer_id": 3})
    assert r.status_code == 400 and "still printing" in r.json()["detail"]
    assert bb.queue[2]["manual_start"] is True


def test_printed_before_0_24_counts_as_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    ingest(report([("S1", "PRINT", "Magicians_35", "Ayra", "Pink", 1),
                   ("S2", "PRINT", "Magicians_35", "Riya", "Pink", 1)]), "a.csv")
    plates = [{"file": "Bases/b1.3mf", "kind": "bases", "order_ids": ["S1", "S2"], "names": ["Ayra", "Riya"]},
              {"file": "Names/pink/n1.3mf", "kind": "names", "order_ids": ["S1"], "names": ["Ayra"]},
              {"file": "Names/pink/n2.3mf", "kind": "names", "order_ids": ["S2"], "names": ["Riya"]}]
    with db.tx() as con:
        bid = db.create_batch(con, ["S1", "S2"], str(tmp_path), status="done")
        con.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps({"plates": plates}), bid))
        con.execute("UPDATE orders SET state='bundled', batch_id=? WHERE order_id IN ('S1','S2')", (bid,))
        for f in ("Bases/b1.3mf", "Names/pink/n1.3mf"):
            con.execute("INSERT INTO print_jobs(batch_id, plate, kind, printer_id, state, created_at, updated_at) "
                        "VALUES (?,?,?,1,'done',?,?)", (bid, f, f.split("/")[0].lower(), db.now(), db.now()))
        con.execute("UPDATE orders SET made_at=? WHERE order_id='S1'", (db.now(),))  # made before 0.24
    assert verify.backfill() == 3  # S1's base + name, S2's base
    with db.tx() as con:
        o = {r["order_id"]: r for r in db.rows(con, "SELECT * FROM orders")}
        who = {r[0] for r in con.execute("SELECT staff FROM plate_checks")}
    assert o["S1"]["made_at"] and o["S1"]["checked_by"] == verify.BEFORE and who == {verify.BEFORE}
    assert o["S2"]["made_at"] is None and o["S2"]["print_started_at"]  # its name still to print
    assert verify.backfill() == 0  # once
    makeit.update_made(bid)
    with db.tx() as con:
        assert con.execute("SELECT made_at FROM orders WHERE order_id='S1'").fetchone()[0]


def test_staff_names_are_kept_hidden_not_removed(client):
    assert client.post("/api/staff", json={"name": "  Ravi   K "}).json()["name"] == "Ravi K"
    assert client.post("/api/staff", json={"name": ""}).status_code == 400
    client.post("/api/staff", json={"name": "Ravi K", "hidden": True})
    assert "Ravi K" not in [s["name"] for s in client.get("/api/staff").json()["staff"]]
    assert {"name": "Ravi K", "hidden": True} in client.get("/api/staff?all=1").json()["staff"]
    client.post("/api/staff", json={"name": "Ravi K"})  # added again → shown again
    assert "Ravi K" in [s["name"] for s in client.get("/api/staff").json()["staff"]]
    with db.tx() as con:
        con.execute("DELETE FROM staff")


def test_base_and_letters_status_and_no_double_save(client, bb):
    """0.25: Letters splits by "base printed"; Bases → Printed; Batches shows base / letters per name; a check
    saved twice (button pressed again) is refused, not saved twice."""
    bid, folder = built_batch(client, "PD")
    assert client.post("/api/staff", json={"name": "Asha"}).status_code == 200
    m = client.get("/api/makeit").json()
    assert all(r["other_ready"] == 0 for r in m["names"]) and m["printed_bases"] == []
    assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": BASE}).status_code == 200
    m = client.get("/api/makeit").json()
    assert {r["file"]: r["other_ready"] == len(r["open_items"]) for r in m["names"]} == {PINK: True, BLUE: True}
    assert [(r["file"], r["other_ready"]) for r in m["printed_bases"]] == [(BASE, 0)]  # no letters printed yet
    names = {n["name"]: n for b in client.get("/api/batches").json()["batches"] if b["id"] == bid for n in b["names"]}
    assert {k: (n["base"], n["letters"]) for k, n in names.items()} == {"Ayra": ("waiting", "todo"), "Riya": ("waiting", "todo")}

    assert client.post("/api/makeit/by-hand", json={"batch_id": bid, "file": PINK}).status_code == 200
    card = _cards(client)[(bid, PINK)]
    assert _check(client, bid, PINK, _res(card, lambda i: "good")).status_code == 200
    again = _check(client, bid, PINK, _res(card, lambda i: "good"))
    assert again.status_code == 409 and "already checked by Asha" in again.json()["detail"]
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM plate_checks WHERE batch_id=? AND plate=?", (bid, PINK)).fetchone()[0] == 1
    m = client.get("/api/makeit").json()
    assert [(r["file"], r["other_ready"]) for r in m["printed_bases"]] == [(BASE, 1)]  # Ayra's letters printed
