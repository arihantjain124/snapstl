"""0.43.1: MakerWorld busy ("server is experiencing some issues") doesn't use up a name's tries; the queue waits
longer each time in a row and says so; a real failure still counts."""
import time

import pytest

from snapstl import batches as B
from snapstl import computer, db

from .test_flow import client  # noqa: F401  (client is a fixture)


@pytest.fixture
def job():
    with db.tx() as con:
        cur = con.execute("INSERT INTO downloads(cache_key,name,compartments,model_json,state,attempts,created_at,"
                          "updated_at,batch_id,computer_id) VALUES ('busy-test','Shambhavi',5,'{}','claimed',2,?,?,NULL,?)",
                          (db.now(), db.now(), computer.ID))
        jid = cur.lastrowid
    B.DOWNLOADER.busy_streak, B.DOWNLOADER.busy_until, B.DOWNLOADER.next_after = 0, 0.0, 0.0
    yield jid
    with db.tx() as con:
        con.execute("DELETE FROM downloads WHERE id=?", (jid,))
    B.DOWNLOADER.busy_streak, B.DOWNLOADER.busy_until, B.DOWNLOADER.next_after = 0, 0.0, 0.0


def _row(jid):
    with db.tx() as con:
        return dict(con.execute("SELECT state, attempts, error FROM downloads WHERE id=?", (jid,)).fetchone())


def test_busy_goes_back_without_using_a_try_and_waits_longer_each_time(client, job):  # noqa: F811
    msg = "MakerWorld busy: Sorry, it seems that the server is experiencing some issues. Please try again later."
    r = client.post(f"/api/helper/jobs/{job}/fail", json={"error": msg})
    assert r.status_code == 200
    assert _row(job)["state"] == "queued" and _row(job)["attempts"] == 1  # the claim's try given back
    assert 55 < B.DOWNLOADER.busy_until - time.time() <= 60 and B.DOWNLOADER.next_after >= B.DOWNLOADER.busy_until
    snap = client.get("/api/downloads").json()["downloader"]
    assert snap["busy_streak"] == 1 and snap["busy_until"]
    with db.tx() as con:
        con.execute("UPDATE downloads SET state='claimed', attempts=attempts+1 WHERE id=?", (job,))
    B.store_failure(job, msg)
    assert 115 < B.DOWNLOADER.busy_until - time.time() <= 120 and B.DOWNLOADER.busy_streak == 2
    for _ in range(6):
        B.store_failure(job, msg)
    assert B.DOWNLOADER.busy_until - time.time() <= 600  # never more than 10 minutes
    assert _row(job)["state"] == "queued"


def test_a_real_failure_still_counts(client, job):  # noqa: F811
    with db.tx() as con:
        con.execute("UPDATE downloads SET attempts=3 WHERE id=?", (job,))
    B.store_failure(job, "MakerWorld: Invalid license password")
    assert _row(job)["state"] == "failed" and B.DOWNLOADER.busy_until == 0.0


def test_the_helper_knows_the_busy_message():
    js = open("helper/content.js", encoding="utf-8").read()
    assert "experiencing" in js and "MakerWorld busy" in js and "location.reload()" in js
    assert '"version": "2.7.2"' in open("helper/manifest.json", encoding="utf-8").read()


def test_reprint_label_from_label_printed(client, tmp_path, monkeypatch):  # noqa: F811
    import json

    from snapstl import labels, platform_os

    pdf = tmp_path / "NS0TEST.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    printed = []
    monkeypatch.setattr(platform_os, "print_pdf", lambda path, printer, tool: printed.append(path))
    with db.tx() as con:
        cur = con.execute("INSERT INTO label_jobs(key, search, clubbed, expected, state, step, pdf, created_at, updated_at, "
                          "printed_at, prints) VALUES ('RPX1','RPX1',0,?, 'done','done',?,?,?,?,1)",
                          (json.dumps({"lines": [{"order_id": "RPX1"}]}), str(pdf), db.now(), db.now(), db.now()))
        jid = cur.lastrowid
    try:
        r = client.post("/api/labels/reprint-order", json={"order_id": "RPX1"})
        assert r.status_code == 200, r.text
        assert printed == [pdf] and r.json()["prints"] == 2
        r = client.post("/api/labels/reprint-order", json={"order_id": "RPX2"})  # made outside SnapSTL: no PDF
        assert r.status_code == 409 and "ShopDeck" in r.json()["detail"]
        assert labels.reprint_order  # noqa: B018
    finally:
        with db.tx() as con:
            con.execute("DELETE FROM label_jobs WHERE id=?", (jid,))
