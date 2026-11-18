"""0.24: Batches page (names, progress, status words), Archive, and the clean-up to the Recycle Bin."""
import json
import time

from snapstl import archive, cache, db, platform_os
from snapstl.ingest import ingest

from .test_ingest import report


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    moved = []

    def fake_trash(p):  # never the real Recycle Bin in tests
        import shutil
        moved.append(p.name)
        shutil.rmtree(p)
        return True

    monkeypatch.setattr(platform_os, "to_trash", fake_trash)
    old = cache.ROOT
    cache.set_root(str(tmp_path / "Downloads"))
    ingest(report([("A1", "PRINT", "Magicians_35", "Ayra", "Pink", 1), ("A2", "PRINT", "Magicians_35", "Riya", "Blue", 1),
                   ("A3", "PRINT", "Magicians_35", "Tara", "Pink", 1)]), "a.csv")
    folder = tmp_path / "Batch 1"
    (folder / "Bases").mkdir(parents=True)
    (folder / "Bases" / "Bases - Ayra + Riya.3mf").write_bytes(b"x" * 100)
    with db.tx() as con:
        bid = db.create_batch(con, ["A1", "A2"], str(folder), status="done")
        con.execute("UPDATE batches SET summary=? WHERE id=?", (json.dumps({"label": "Batch 1 - 2026-11-18", "plates": []}), bid))
        con.execute("UPDATE orders SET state='bundled', batch_id=?, batch_uid=(SELECT uid FROM batches WHERE id=?) "
                    "WHERE order_id IN ('A1','A2')", (bid, bid))
    return bid, folder, moved, old


def _status(oid, s):
    with db.tx() as con:
        con.execute("UPDATE orders SET source_status=? WHERE order_id=?", (s, oid))


def test_batch_rows_names_progress_and_archive(tmp_path, monkeypatch):
    bid, folder, moved, old = _setup(tmp_path, monkeypatch)
    try:
        r = archive.batch_rows()
        b = next(x for x in r["batches"] if x["id"] == bid)
        assert [n["name"] for n in b["names"]] == ["Riya", "Ayra"] and b["word"] == "Ready to print"
        assert r["computers"] == 1 and b["archived_at"] is None
        with db.tx() as con:
            con.execute("UPDATE orders SET print_started_at=1, print_done_at=1 WHERE order_id='A1'")
        assert archive.batch_rows()["batches"][0]["word"] == "Checking"
        with db.tx() as con:
            con.execute("UPDATE orders SET made_at=1, checked_at=1 WHERE order_id IN ('A1','A2')")
        b = archive.batch_rows()["batches"][0]
        assert b["word"] == "Packing" and b["count"]["checked"] == 2
        _status("A1", "IN_TRANSIT")
        _status("A2", "CANCELLED")
        b = archive.batch_rows()["batches"][0]
        assert b["word"] == "All shipped" and b["archived_at"] and b["count"]["shipped"] == 1
        assert b["clean_on"] == b["archived_at"] + 10 * 86400
        # its label expired → back to Printed – pack it: leaves Archive (files still there)
        _status("A1", "PRINT")
        assert archive.batch_rows()["batches"][0]["archived_at"] is None
    finally:
        cache.set_root(str(old))


def test_clean_up_moves_old_files_to_the_recycle_bin(tmp_path, monkeypatch):
    bid, folder, moved, old = _setup(tmp_path, monkeypatch)
    try:
        _status("A1", "DELIVERED")
        _status("A2", "IN_TRANSIT")
        archive.batch_rows()  # archived now
        models = tmp_path / "Downloads"
        for n in ("Ayra - 3 comp - k1", "Tara - 3 comp - k2", "Zoya - 3 comp - k3"):
            (models / n).mkdir(parents=True)
            (models / n / "base.stl").write_bytes(b"x" * 10)
        now = time.time()
        assert archive.clean_up(now)["batches"] == 0 and folder.exists()  # archived today: kept 10 days
        assert archive.clean_up(now + 9 * 86400) == {"batches": 0, "models": 0, "bytes": 0, "failed": []}
        r = archive.clean_up(now + 16 * 86400)
        # the batch folder (all shipped 10+ days ago) and Ayra / Zoya's models (unused 15 days) go;
        # Tara's model stays: order A3 (Tara) is still waiting to be made
        assert (r["batches"], r["models"]) == (1, 2) and sorted(moved) == ["Ayra - 3 comp - k1", "Batch 1", "Zoya - 3 comp - k3"]
        assert (models / "Tara - 3 comp - k2").exists()
        b = archive.batch_rows()["batches"][0]
        assert b["files_cleaned_at"] and b["archived_at"]
        _status("A1", "PRINT")  # files are gone: it stays archived
        assert archive.batch_rows()["batches"][0]["archived_at"]
        with db.tx() as con:
            assert "Recycle Bin" in con.execute("SELECT text FROM activity WHERE kind='cleanup'").fetchone()[0]
        # a model a batch used recently stays
        (models / "Mia - 3 comp - k4").mkdir()
        cache.touch(models / "Mia - 3 comp - k4")
        with db.tx() as con:
            assert con.execute("SELECT last_used FROM model_use WHERE folder='Mia - 3 comp - k4'").fetchone()[0] >= now
            con.execute("UPDATE model_use SET last_used=? WHERE folder='Mia - 3 comp - k4'", (now + 10 * 86400,))
        assert archive.clean_up(now + 17 * 86400)["models"] == 0  # used 7 "days" ago
        # never while a batch is downloading or building
        with db.tx() as con:
            db.create_batch(con, ["A3"], str(tmp_path / "B2"), status="building")
        assert "waits" in archive.clean_up(now + 40 * 86400)["skipped"]
    finally:
        cache.set_root(str(old))


def test_disk_space(tmp_path, monkeypatch):
    bid, folder, moved, old = _setup(tmp_path, monkeypatch)
    try:
        d = archive.disk()
        assert d["batch_bytes"] == 100 and d["batch_folders"] == 1 and d["due_batches"] == 0
    finally:
        cache.set_root(str(old))


def test_recycle_bin_never_fails_on_a_missing_path(tmp_path):
    assert platform_os.to_trash(tmp_path / "not-there") is True
