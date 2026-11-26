"""0.37: the ShopDeck partner API — connect (OAuth), renew the key, live order status. ShopDeck is a stand-in
(`shopdeck._http`); statuses move forward only, through the report's own rules, and never undo newer news."""
import json
import time
import urllib.parse

import pytest

from snapstl import db, ingest, shopdeck

from .test_033 import report
from .test_flow import client  # noqa: F401  (client is a fixture)


class FakeShopDeck:
    def __init__(self):
        self.calls = []
        self.orders = []
        self.refresh_fail = None
        self.n = 0

    def __call__(self, method, url, body=None, key="", timeout=30):
        self.calls.append((method, url, body, key))
        if url.endswith("/oauth/access-token"):
            assert body["code"] == "CODE1" and body["redirection_url"].endswith("/api/shopdeck/callback")
            return self._tokens()
        if url.endswith("/oauth/refresh-access-token"):
            if self.refresh_fail:
                e = shopdeck.ShopDeckError("ShopDeck 401: Invalid or expired refresh token.")
                e.status, e.code = 401, "UNAUTHENTICATED"
                raise e
            return self._tokens()
        if "/orders?" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert key.startswith("KEY") and q["fields"] == [shopdeck.FIELDS]
            page, size = int(q["page_no"][0]), int(q["page_size"][0])
            rows = self.orders if q["end_date"][0] >= "2026" else []
            got = rows[(page - 1) * size: page * size] if q["start_date"][0] == self.first_start(q) else []
            return {"data": got, "page_info": {"has_next_page": len(got) == size}}
        raise AssertionError(url)

    @staticmethod
    def first_start(q):
        return q["start_date"][0] if q["end_date"][0] == shopdeck._ist_today().isoformat() else None

    def _tokens(self, days=90):
        self.n += 1
        exp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + days * 86400))
        return {"data": {"key_secret": f"KEY{self.n}", "refresh_token": f"REF{self.n}", "expires_at": exp,
                         "refresh_expires_at": exp, "scope": list(shopdeck.SCOPES)}}


@pytest.fixture
def sd(monkeypatch):
    fake = FakeShopDeck()
    monkeypatch.setattr(shopdeck, "_http", fake)
    shopdeck.CONF_FILE.unlink(missing_ok=True)
    shopdeck.POLLER.n, shopdeck.POLLER.last, shopdeck.POLLER.changes = 0, {}, []
    yield fake
    shopdeck.CONF_FILE.unlink(missing_ok=True)
    with db.tx() as con:
        con.execute("DELETE FROM shopdeck_live")


def _connect(client):  # noqa: F811
    assert client.post("/api/shopdeck", json={"app_id": "0123456789abcdef01234567"}).status_code == 200
    r = client.get("/api/shopdeck/connect", follow_redirects=False)
    assert r.status_code == 303
    loc = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(loc.query)
    assert loc.netloc == "integrations.shopdeck.com" and q["scope"] == ["read_orders,write_orders,read_customers"]
    assert q["redirection_url"][0].startswith("http://127.0.0.1:") and q["app_id"] == ["0123456789abcdef01234567"]
    r = client.get("/api/shopdeck/callback", params={"code": "CODE1", "state": q["state"][0]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#settings:shopdeck"
    return q["state"][0]


def test_forward_only():
    f = shopdeck.forward
    assert f("PRINT", "PACK") and f("AWB EXPIRED", "IN-TRANSIT") and f("ACCEPTANCE_PENDING", "CANCELLED INITIATED")
    assert f("PACK", "HANDOVER") and f("IN-TRANSIT", "DELIVERED") and f("IN-TRANSIT", "RTO_INITIATED")
    assert f("CANCELLED INITIATED", "CANCELLED") and f("RTO_INITIATED", "RTO_DELIVERED")
    assert not f("IN-TRANSIT", "PACK") and not f("DELIVERED", "IN-TRANSIT") and not f("PACK", "PACK")
    assert not f("IN-TRANSIT", "CANCELLED") and not f("CANCELLED", "PACK") and not f("CANCELLED", "CANCELLED INITIATED")
    assert not f("", "PACK") and not f("SOMETHING NEW", "PACK")
    assert "created" not in shopdeck.STATUS and "enqueued" not in shopdeck.STATUS  # PRINT / AWB EXPIRED / ACCEPTANCE: report


def test_connect_keeps_the_key_private_and_checks_state(client, sd):  # noqa: F811
    assert client.post("/api/shopdeck", json={"app_id": "not an id!"}).status_code == 400
    assert client.get("/api/shopdeck/connect", follow_redirects=False).status_code == 400  # no App ID yet
    state = _connect(client)
    s = client.get("/api/shopdeck").json()
    assert s["connected"] and "key_secret" not in json.dumps(s) and "KEY1" not in json.dumps(s)
    saved = json.loads(shopdeck.CONF_FILE.read_text())
    assert saved["key_secret"] == "KEY1" and "pending_state" not in saved
    # the same link again (or someone else's) can't be used
    r = client.get("/api/shopdeck/callback", params={"code": "CODE1", "state": state}, follow_redirects=False)
    assert r.status_code == 303 and "isn't the one SnapSTL started" in client.get("/api/shopdeck").json()["last_error"]
    assert shopdeck.load()["key_secret"] == "KEY1"
    client.post("/api/shopdeck/disconnect")
    assert not client.get("/api/shopdeck").json()["connected"]


def test_key_is_renewed_before_it_expires_and_a_dead_refresh_asks_to_connect(client, sd):  # noqa: F811
    _connect(client)
    assert not shopdeck.renew_if_due()  # 90 days left
    shopdeck._update(expires_at="2026-01-01T00:00:00.000Z")
    assert shopdeck.renew_if_due() and shopdeck.load()["key_secret"] == "KEY2"
    assert sd.calls[-1][2] == {"refresh_token": "REF1"}
    sd.refresh_fail = True
    with pytest.raises(shopdeck.ShopDeckError):
        shopdeck.renew_if_due(force=True)
    s = client.get("/api/shopdeck").json()
    assert not s["connected"] and "Connect ShopDeck again" in s["last_error"]


def test_live_status_moves_orders_forward_through_the_report_rules(client, sd):  # noqa: F811
    ingest.ingest(report([("SD1", "", "PRINT", "Magicians_35", "Asha", 1),
                          ("SD2", "", "PRINT", "Magicians_35", "Ravi", 1),
                          ("SD3", "", "IN-TRANSIT", "Magicians_35", "Meena", 1),
                          ("SD4", "", "ACCEPTANCE_PENDING", "Magicians_35", "Tara", 1),
                          ("SD5", "", "PRINT", "Magicians_35", "Zoya", 1)]), "r.csv")
    with db.tx() as con:  # SD2 is being made
        con.execute("UPDATE orders SET state='bundled', made_at=?, batch_id=1 WHERE order_id='SD2'", (db.now(),))
    _connect(client)
    sd.orders = [{"id": "SD1", "order_status": "printed", "awb_no": "A1", "courier_name": "Bluedart Air"},
                 {"id": "SD2", "order_status": "cancel_initiated"},
                 {"id": "SD3", "order_status": "printed"},       # behind the report: kept
                 {"id": "SD4", "order_status": "created"},       # created: the report decides
                 {"id": "SD5", "order_status": "dispatched"},
                 {"id": "OTHER", "order_status": "delivered"}]  # not one of ours: only kept
    last = shopdeck.POLLER.once()
    assert last["ok"] and (last["seen"], last["known"], last["changed"]) == (6, 5, 3), last
    with db.tx() as con:
        o = {r["order_id"]: r for r in db.rows(con, "SELECT * FROM orders WHERE order_id LIKE 'SD%'")}
        live = db.rows(con, "SELECT order_id, status, awb_no FROM shopdeck_live WHERE order_id='SD1'")
    assert o["SD1"]["source_status"] == "PACK" and o["SD1"]["state"] == "not_needed"  # packed outside: not to make
    assert o["SD2"]["source_status"] == "CANCELLED INITIATED" and o["SD2"]["flag"] == "cancelled_in_production"
    assert o["SD3"]["source_status"] == "IN-TRANSIT" and o["SD4"]["source_status"] == "ACCEPTANCE_PENDING"
    assert o["SD4"]["state"] == "ready" and o["SD5"]["source_status"] == "IN-TRANSIT"
    assert live == [{"order_id": "SD1", "status": "printed", "awb_no": "A1"}]
    assert [c["order_id"] for c in client.get("/api/shopdeck").json()["changes"]] == ["SD5", "SD2", "SD1"]
    # an older report doesn't undo it; nothing new moves twice
    ingest.ingest(report([("SD1", "", "PRINT", "Magicians_35", "Asha", 1)]), "old.csv")
    with db.tx() as con:
        assert con.execute("SELECT source_status FROM orders WHERE order_id='SD1'").fetchone()[0] == "PACK"
    assert shopdeck.POLLER.once()["changed"] == 0


def test_switch_off_only_records(client, sd):  # noqa: F811
    ingest.ingest(report([("SD9", "", "PRINT", "Magicians_35", "Asha", 1)]), "r.csv")
    _connect(client)
    client.post("/api/shopdeck", json={"apply_status": False})
    sd.orders = [{"id": "SD9", "order_status": "dispatched"}]
    assert shopdeck.POLLER.once()["changed"] == 0
    with db.tx() as con:
        assert con.execute("SELECT source_status FROM orders WHERE order_id='SD9'").fetchone()[0] == "PRINT"
        assert con.execute("SELECT status FROM shopdeck_live WHERE order_id='SD9'").fetchone()[0] == "dispatched"


def test_pages_and_windows(client, sd):  # noqa: F811
    _connect(client)
    sd.orders = [{"id": f"P{i}", "order_status": "created"} for i in range(230)]
    assert len(shopdeck.fetch(7)) == 230
    starts = {urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["start_date"][0] for _, u, _, _ in sd.calls if "/orders?" in u}
    assert len(starts) == 1
    sd.calls.clear()
    shopdeck.fetch(35)
    qs = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query) for _, u, _, _ in sd.calls if "/orders?" in u]
    wins = [w for w in qs if w["page_no"] == ["1"]]
    assert len({w["start_date"][0] for w in wins}) == 5  # 5 windows of 7 days, each at most 7 days long
    assert not client.post("/api/shopdeck", json={"poll_s": 10}).status_code == 200


# ---------------- 0.37.1: the customisation bridge (new orders only) ----------------
def bridge(rows, title="Order Reconciliation Report (SnapSTL bridge from the ShopDeck connector)") -> bytes:
    """rows: (order_id, clubbed_id, status, sku, customisation, qty)."""
    import csv
    import io

    f = io.StringIO()
    w = csv.writer(f)
    w.writerow([title])
    w.writerow(["Date Range: 26/11/2026 to 27/11/2026"])
    w.writerow(["Order ID", "Clubbed Order ID", "Order Creation Date", "Order Status", "SKU Code", "Quantity",
                "Customer Name", "Product Customisation"])
    for oid, club, st, sku, cust, qty in rows:
        w.writerow([oid, club, "26-11-2026", st, sku, qty, "Someone", cust])
    return f.getvalue().encode()


ORG = "Enter Your First Name: {}\nColor: Pink"


def _o(oid):
    with db.tx() as con:
        r = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
        return dict(r) if r else None


def test_bridge_adds_new_orders_only_and_never_changes_known_ones(client):  # noqa: F811
    ingest.ingest(report([("BK1", "", "IN-TRANSIT", "Magicians_35", "Asha", 1)]), "r.csv")
    before = _o("BK1")
    s = ingest.ingest(bridge([
        ("BK1", "", "PRINT", "Magicians_35", ORG.format("Other"), 1),         # known: untouched
        ("BN1", "C1", "PRINT", "Magicians_35", ORG.format("Kavya"), 2),     # new
        ("BN2", "", "PRINT", "Magicians_174", "Write your NAME here.: ROCKET", 1),  # new stand
        ("BN3", "", "PRINT", "Magicians_35", "Enter Your First Name: Ravi", 1),     # no colour: left
        ("BN4", "", "PRINT", "Magicians_35", "", 1),                                 # nothing: left
        ("BN5", "", "CANCELLED", "Magicians_35", ORG.format("Zoya"), 1),             # not to make: left
        ("BN6", "", "PRINT", "Some_Other_SKU", ORG.format("X"), 1)]), "shopdeck-bridge-1.csv")
    assert (s.new, s.unchanged, len(s.warnings)) == (2, 1, 3)
    after = _o("BK1")
    assert {k: after[k] for k in after if k != "updated_at"} == {k: before[k] for k in before if k != "updated_at"}
    assert after["updated_at"] == before["updated_at"]
    n1, n2 = _o("BN1"), _o("BN2")
    assert (n1["name"], n1["color"], n1["quantity"], n1["clubbed_id"], n1["state"]) == ("Kavya", "pink", 2, "C1", "ready")
    assert (n2["name"], n2["sku"], n2["state"]) == ("ROCKET", "Magicians_174", "ready")
    assert n1["status_at"] == ingest.BRIDGE_STAMP and n1["first_file"] == "shopdeck-bridge-1.csv"
    assert all(_o(x) is None for x in ("BN3", "BN4", "BN5", "BN6"))
    with db.tx() as con:  # not a report: no upload record, no report rows / shop lines from it
        assert not con.execute("SELECT 1 FROM files WHERE filename='shopdeck-bridge-1.csv'").fetchone()
        assert not con.execute("SELECT 1 FROM report_rows WHERE order_id LIKE 'BN%'").fetchone()
        assert not con.execute("SELECT 1 FROM shop_lines WHERE order_id LIKE 'BN%'").fetchone()
    # the same file again (or Orders → Upload report with it) adds nothing twice
    assert ingest.ingest(bridge([("BN1", "C1", "PRINT", "Magicians_35", ORG.format("Kavya"), 2)]), "x.csv").new == 0


def test_any_report_and_the_api_override_a_bridge_order(client, sd):  # noqa: F811
    from snapstl import logistics_api

    ingest.ingest(report([("BK9", "", "PRINT", "Magicians_35", "Asha", 1)]), "r.csv")
    as_of = logistics_api.meta()["report_as_of"], logistics_api.meta()["last_upload"]
    ingest.ingest(bridge([("BO1", "", "PRINT", "Magicians_35", ORG.format("Meena"), 1),
                          ("BO2", "", "PRINT", "Magicians_35", ORG.format("Tara"), 1)]), "shopdeck-bridge-2.csv")
    assert (logistics_api.meta()["report_as_of"], logistics_api.meta()["last_upload"]) == as_of  # freshness unchanged
    # a report (even a dated one) wins: status moves, the name and colour stay as the bridge had them
    ingest.ingest(report([("BO1", "", "CANCELLED INITIATED", "Magicians_35", "Meena", 1)]), "r2.csv")
    o = _o("BO1")
    assert o["source_status"] == "CANCELLED INITIATED" and o["state"] == "not_needed" and o["name"] == "Meena"
    assert o["status_at"] > ingest.BRIDGE_STAMP
    # and so does the ShopDeck API
    _connect(client)
    sd.orders = [{"id": "BO2", "order_status": "printed"}]
    assert shopdeck.POLLER.once()["changed"] == 1 and _o("BO2")["source_status"] == "PACK"


def test_bridge_inbox(client, monkeypatch, tmp_path):  # noqa: F811
    import os

    from snapstl import reportwatch, sync

    monkeypatch.setenv("SNAPSTL_BRIDGE_DIR", str(tmp_path))
    old = time.time() - 10

    def put(name, data, mtime=old):
        p = tmp_path / name
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))
        return p

    put("shopdeck-bridge-a.csv", bridge([("BI1", "", "PRINT", "Magicians_35", ORG.format("Anu"), 1)]))
    put("shopdeck-bridge-b.csv", report([("BI2", "", "PRINT", "Magicians_35", "Real", 1)]))  # not a bridge file
    put("shopdeck-bridge-c.csv", bridge([("BI3", "", "PRINT", "Magicians_35", ORG.format("Old"), 1)]),
        time.time() - 3 * 3600)  # too old
    put("notes.csv", b"x")  # not ours
    # shared records on but not reachable: nothing is imported, the files wait
    monkeypatch.setattr(sync, "enabled", lambda: True)
    monkeypatch.setattr(sync, "sync_now", lambda: {})
    monkeypatch.setattr(sync, "online", lambda: False)
    res = reportwatch.import_bridge()
    assert [r["file"] for r in res] == ["shopdeck-bridge-c.csv"] and not res[0]["ok"]  # only the old one, rejected
    assert (tmp_path / "shopdeck-bridge-a.csv").exists() and _o("BI1") is None
    monkeypatch.setattr(sync, "online", lambda: True)
    res = {r["file"]: r for r in reportwatch.import_bridge()}
    assert res["shopdeck-bridge-a.csv"]["ok"] and res["shopdeck-bridge-a.csv"]["new"] == 1
    assert not res["shopdeck-bridge-b.csv"]["ok"] and _o("BI2") is None and _o("BI3") is None
    assert sorted(p.name for p in (tmp_path / "done").iterdir()) == ["shopdeck-bridge-a.csv"]
    assert sorted(p.name for p in (tmp_path / "rejected").iterdir()) == ["shopdeck-bridge-b.csv", "shopdeck-bridge-c.csv"]
    assert (tmp_path / "notes.csv").exists() and _o("BI1")["name"] == "Anu"
