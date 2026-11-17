"""sync.merge without a Postgres: a shared record written by another computer is applied to this one.

The two-computer round trip through Supabase is in test_sync.py (needs a throwaway Postgres)."""
import csv
import io

from snapstl import db, ingest, sync

T0 = 1_000.0


def _report(rows) -> bytes:
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Product Customisation"])
    for oid, name in rows:
        w.writerow([oid, "", "01-11-2026", "PRINT", "Magicians_35", 1, "Private Person",
                    f"Enter Your First Name: {name}\nColor: Pink\n"])
    return f.getvalue().encode()


def _setup(tmp_path, monkeypatch, rows):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "m.db")
    ingest.ingest(_report(rows), "r.csv")
    with db.tx() as con:  # as if already synced, long ago
        con.execute("UPDATE orders SET in_remote=1, state_at=?", (T0,))
        con.execute("DELETE FROM sync_outbox")


def _order(oid):
    with db.tx() as con:
        return dict(con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone())


def _record(o: dict, **kw) -> dict:
    """The shared record another computer would have written for local order `o`."""
    p = sync._payload(o)
    p.pop("actor")
    p["computer_id"] = p["computer_id"] or None
    return {**p, **kw}


def _merge(r, force=False):
    with db.tx() as con, db.quiet(con):
        sync.merge(con, r, force=force)


def test_skip_and_make_anyway_from_another_computer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("S1", "Asha")])
    o = _order("S1")
    assert o["state"] == "ready"
    _merge(_record(o, app_state="skipped", state_at=T0 + 10))
    s = _order("S1")
    assert s["state"] == "skipped" and s["state_at"] == T0 + 10
    with db.tx() as con:  # applied quietly: not sent back
        assert con.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
    # an older record (e.g. a computer that hadn't heard of the skip) doesn't undo it
    _merge(_record(o, app_state="ready", state_at=T0 + 5))
    assert _order("S1")["state"] == "skipped"
    # Make anyway, later, on the other computer
    _merge(_record(o, app_state="ready", state_at=T0 + 20))
    assert _order("S1")["state"] == "ready"


def test_approve_without_editing_reaches_other_computers(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("R1", "Anu Sharma")])
    o = _order("R1")
    assert o["state"] == "needs_review" and o["review_reason"]
    _merge(_record(o, app_state="ready", review_reason=None, state_at=T0 + 10))
    r = _order("R1")
    assert r["state"] == "ready" and r["review_reason"] is None


def test_skip_from_another_computer_on_a_new_order(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("X1", "Asha")])
    r = _record(_order("X1"), order_id="N9", app_state="skipped", state_at=T0 + 10)
    _merge(r)
    assert _order("N9")["state"] == "skipped"


def test_remade_and_skipped_elsewhere(tmp_path, monkeypatch):
    """Bundled here; another computer pressed Remake, then Skip: it ends skipped here too."""
    _setup(tmp_path, monkeypatch, [("B1", "Asha")])
    with db.tx() as con, db.quiet(con):
        con.execute("UPDATE orders SET state='bundled', batch_uid='u1', batch_computer_id='other', "
                    "batch_computer='PC X', made_at=? WHERE order_id='B1'", (T0,))
        con.execute("UPDATE orders SET state_at=? WHERE order_id='B1'", (T0,))  # the trigger stamped it now
    o = _order("B1")
    _merge(_record(o, state="released", app_state="skipped", batch_uid=None, computer_id=None, made_at=None,
                   state_at=T0 + 10))
    b = _order("B1")
    assert b["state"] == "skipped" and b["batch_uid"] is None and b["made_at"] is None


def test_ready_to_ship_from_the_batch_computer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("M1", "Asha")])
    with db.tx() as con, db.quiet(con):
        con.execute("UPDATE orders SET state='bundled', batch_uid='u1', batch_computer_id='other', "
                    "batch_computer='PC X', state_at=? WHERE order_id='M1'", (T0,))
    o = _order("M1")
    _merge(_record(o, made_at=T0 + 50))  # same state_at: only made_at changed
    assert _order("M1")["made_at"] == T0 + 50
    _merge(_record(o, made_at=None))  # Reprint on the batch computer: back to Bundled
    assert _order("M1")["made_at"] is None


def test_remake_clears_ready_to_ship(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [("M2", "Asha")])
    with db.tx() as con:
        con.execute("UPDATE orders SET state='bundled', made_at=? WHERE order_id='M2'", (T0,))
    ingest.reset_to_ready("M2")
    o = _order("M2")
    assert o["state"] == "ready" and o["made_at"] is None
