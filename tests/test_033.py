"""0.33: shipping labels from ShopDeck. The helper's page (helper/shopdeck.js) is played over HTTP; ShopDeck,
the PDF's text and the printer are stand-ins. Every check fails closed: a wrong parcel, an unchanged AWB or a
label that isn't this parcel's stops the job and nothing is printed."""
import csv
import io
import time
from pathlib import Path

import pytest

from snapstl import db, labels, platform_os
from snapstl.settings import save_settings

from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)
from .test_ingest import HEAD


def report(rows) -> bytes:
    """rows: (order_id, clubbed_id, status, sku, name, qty)."""
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["Order Reconciliation Report"])
    w.writerow(["Date Range: x"])
    w.writerow(HEAD)
    for oid, club, status, sku, name, qty in rows:
        cust = f"Enter Your First Name: {name}\nColor: Pink\n" if name else ""
        w.writerow([oid, club, "01-11-2026", status, sku, qty, "Private Person", cust])
    return f.getvalue().encode()


@pytest.fixture
def setup(client, monkeypatch, tmp_path):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.enabled = True
    server.SETTINGS.labels.wait_after_logistics_s = 0
    server.SETTINGS.labels.pdf_timeout_s = 20
    server.SETTINGS.labels.one_at_a_time = True
    save_settings(server.SETTINGS)
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("SNAPSTL_REPORT_DIR", str(dl))
    printed = []
    monkeypatch.setattr(platform_os, "print_pdf", lambda path, printer, tool="": printed.append((Path(path), printer)))
    text = {}
    monkeypatch.setattr(labels, "pdf_text", lambda path: text[Path(path).name])
    yield {"dl": dl, "printed": printed, "text": text}
    server.SETTINGS.labels.enabled = False
    save_settings(server.SETTINGS)
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 200


def made(*ids):
    """As if built, printed and checked Good (Print → Verify)."""
    with db.tx() as con:
        for oid in ids:
            con.execute("UPDATE orders SET state='bundled', made_at=?, batch_id=1 WHERE order_id=?", (db.now(), oid))


def label_text(key, awb, names, qty=None):
    items = "\n".join(f"Customized Name Desk Organizer | Personalized (20-24cm)\nEnter Your First Name : {n}\nColor : Pink"
                      for n in names)
    return (f"To:\nCustomer\nAWB: {awb}\nRouting Code: X\nPREPAID BLUE DART SURFACE\nNumber of Skus: {qty or len(names)} "
            f"Total Quantity: {qty or len(names)} Order Id: {key}\nFrom:\nSNAP MAGICIANS\n{items}\nNOTE: …")


def job_of(c, oid):
    return c.get(f"/api/labels?ids={oid}").json()["jobs"].get(oid)


def nxt(c):
    return c.get("/api/helper/shopdeck/next").json()


def seen(c, jid, **kw):
    s = {"where": "print", "tab": "single", "count": "01", "club": "", "sub": "", "ids": [], "skus": [], "awb": "",
         "partner": ""}
    s.update(kw)
    return c.post(f"/api/helper/shopdeck/{jid}/seen", json=s).json()


def step(c, jid, name, **data):
    return c.post(f"/api/helper/shopdeck/{jid}/step", json={"step": name, **data}).json()


def test_a_single_order_gets_a_new_awb_a_checked_label_and_is_printed(client, setup):  # noqa: F811
    assert client.post("/api/ingest", files={"file": ("r.csv", report([("Z1", "", "PRINT", "Magicians_35", "Tara", 1)]),
                                                         "text/csv")}).status_code == 200
    r = client.post("/api/labels", json={"order_ids": ["Z1"], "staff": "Asha"})
    assert r.status_code == 409 and "isn't made" in r.json()["detail"]  # not made and checked yet
    made("Z1")
    assert client.post("/api/labels", json={"order_ids": ["Z1"], "staff": "Asha"}).status_code == 200
    assert client.post("/api/labels", json={"order_ids": ["Z1"]}).status_code == 409  # one at a time
    j = nxt(client)["job"]
    assert (j["key"], j["search"], j["clubbed"], j["step"]) == ("Z1", "Z1", False, "find")
    assert j["couriers"][0] == "BLUEDART_AIR"
    a = seen(client, j["id"], ids=["Z1"], skus=["Magicians_35"], awb="111111111111102", partner="DELHIVERY SURFACE")
    assert a == {"do": "logistic"}
    assert step(client, j["id"], "saving", courier="DELHIVERY_SURFACE") == {"do": "go"}
    a = step(client, j["id"], "saved", awb="111111111111135", courier="DELHIVERY SURFACE")
    assert a["do"] == "wait"
    a = seen(client, j["id"], ids=["Z1"], skus=["Magicians_35"], awb="111111111111135", partner="DELHIVERY SURFACE")
    assert a == {"do": "label"}
    assert step(client, j["id"], "pressing") == {"do": "go"}
    name = "SHP-10000003-Z1-111111111111135.pdf"
    setup["text"][name] = label_text("Z1", "111111111111135", ["Tara"])
    (setup["dl"] / name).write_bytes(b"%PDF-1.4 stand-in")
    assert step(client, j["id"], "pressed") == {"do": "done"}
    wait_for(lambda: (job_of(client, "Z1") or {}).get("state") == "done")
    jb = job_of(client, "Z1")
    assert jb["awb"] == "111111111111135" and not jb["error"] and jb["prints"] == 1
    assert len(setup["printed"]) == 1 and setup["printed"][0][1] == "iDPRT SP410BT"
    assert setup["printed"][0][0].name == "Z1 - 111111111111135.pdf" and not (setup["dl"] / name).exists()
    with db.tx() as con:
        assert con.execute("SELECT label_by FROM orders WHERE order_id='Z1'").fetchone()[0] == "SnapSTL label"
    assert nxt(client)["job"] is None
    assert client.post(f"/api/labels/{jb['id']}/reprint").status_code == 200 and len(setup["printed"]) == 2


def _clubbed(client, *extra):  # noqa: F811
    rows = [("C1", "NCW1", "PRINT", "Magicians_35", "Mina", 1), ("C2", "NCW1", "PRINT", "Magicians_35", "Rina", 1),
            *extra]
    assert client.post("/api/ingest", files={"file": ("r.csv", report(rows), "text/csv")}).status_code == 200
    made("C1", "C2")


def test_a_clubbed_parcel_with_other_products_needs_a_person_and_must_match_exactly(client, setup):  # noqa: F811
    _clubbed(client, ("C3", "NCW1", "PRINT", "Magicians_176", None, 1))
    st = client.get("/api/labels?ids=C1").json()["parcels"]["C1"]
    assert st["key"] == "NCW1" and st["clubbed"] and st["problems"] == [] and st["others"][0]["quantity"] == 1
    r = client.post("/api/labels", json={"order_ids": ["C1"]})
    assert r.status_code == 409 and "confirm every item is ready" in r.json()["detail"]
    assert client.post("/api/labels", json={"order_ids": ["C2"], "items_ready": True, "staff": "Asha"}).status_code == 200
    j = nxt(client)["job"]
    assert j["key"] == "NCW1" and j["clubbed"] and j["search"] == "C1"
    # ShopDeck shows a fourth sub-order the report doesn't have: stop, nothing is changed
    a = seen(client, j["id"], tab="clubbed", club="NCW1", sub="04", ids=["C1", "C2", "C3", "C4"],
             skus=["Magicians_35", "Magicians_35", "Magicians_176", "Magicians_35"], awb="44444444435")
    assert a["do"] == "stop" and "04 sub-orders" in a["error"]
    assert job_of(client, "C1")["state"] == "failed" and nxt(client)["job"] is None


@pytest.mark.parametrize("bad", [
    {"club": "NCW9"},                                 # another clubbed order
    {"ids": ["C1", "C9", "C3"]},                      # another sub-order
    {"skus": ["Magicians_35", "Magicians_35", "Magicians_35"]},  # another product
    {"count": "02"},                                  # the search showed two parcels
    {"tab": "single"},                                # found in the Single tab
    {"awb": ""},                                      # AWB unreadable
])
def test_any_difference_stops_the_job(client, setup, bad):  # noqa: F811
    _clubbed(client, ("C3", "NCW1", "PRINT", "Magicians_176", None, 1))
    client.post("/api/labels", json={"order_ids": ["C1"], "items_ready": True})
    j = nxt(client)["job"]
    good = dict(tab="clubbed", club="NCW1", sub="03", ids=["C3", "C1", "C2"],
                skus=["Magicians_176", "Magicians_35", "Magicians_35"], awb="44444444435")
    assert seen(client, j["id"], **{**good, **bad})["do"] == "stop"
    assert job_of(client, "C1")["state"] == "failed"


def test_an_unchanged_awb_or_a_wrong_label_prints_nothing(client, setup):  # noqa: F811
    _clubbed(client)
    client.post("/api/labels", json={"order_ids": ["C1"]})
    j = nxt(client)["job"]
    g = dict(tab="clubbed", club="NCW1", sub="02", ids=["C1", "C2"], skus=["Magicians_35"] * 2)
    assert seen(client, j["id"], **g, awb="222222222222292")["do"] == "logistic"
    step(client, j["id"], "saving", courier="BLUEDART_SURFACE")
    a = step(client, j["id"], "saved", awb="222222222222292")
    assert a["do"] == "stop" and "didn't change" in a["error"]
    # again: the label PDF names another person
    client.post("/api/labels", json={"order_ids": ["C1"]})
    j = nxt(client)["job"]
    seen(client, j["id"], **g, awb="222222222222292")
    step(client, j["id"], "saving", courier="BLUEDART_SURFACE")
    step(client, j["id"], "saved", awb="33333333396", courier="BLUEDART SURFACE")
    assert seen(client, j["id"], **g, awb="33333333396", partner="BLUEDART SURFACE")["do"] == "label"
    step(client, j["id"], "pressing")
    name = "SHP-10000002-NCW1-33333333396.pdf"
    setup["text"][name] = label_text("NCW1", "33333333396", ["Mina", "Someone"])
    (setup["dl"] / name).write_bytes(b"%PDF-1.4 stand-in")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "C1") or {}).get("state") == "failed")
    assert "Rina" in job_of(client, "C1")["error"] and setup["printed"] == []
    with db.tx() as con:
        assert con.execute("SELECT label_at FROM orders WHERE order_id='C2'").fetchone()[0] is None


def test_an_old_label_file_is_refused(client, setup):  # noqa: F811
    client.post("/api/ingest", files={"file": ("r.csv", report([("Z2", "", "PRINT", "Magicians_35", "Zoya", 1)]), "text/csv")})
    made("Z2")
    client.post("/api/labels", json={"order_ids": ["Z2"]})
    j = nxt(client)["job"]
    seen(client, j["id"], ids=["Z2"], skus=["Magicians_35"], awb="111111")
    step(client, j["id"], "saving", courier="XPRESSBEES")
    step(client, j["id"], "saved", awb="222222", courier="XPRESSBEES")
    seen(client, j["id"], ids=["Z2"], skus=["Magicians_35"], awb="222222", partner="XPRESSBEES")
    step(client, j["id"], "pressing")
    name = "SHP-1-Z2-111111.pdf"  # the label before the courier change
    setup["text"][name] = label_text("Z2", "111111", ["Zoya"])
    (setup["dl"] / name).write_bytes(b"%PDF")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "Z2") or {}).get("state") == "failed")
    assert "old label" in job_of(client, "Z2")["error"] and setup["printed"] == []


def test_expired_awb_is_regenerated_first_then_the_usual_flow(client, setup):  # noqa: F811
    client.post("/api/ingest", files={"file": ("r.csv", report([("E1", "", "AWB EXPIRED", "Magicians_35", "Esha", 1)]),
                                                 "text/csv")})
    made("E1")
    assert client.post("/api/labels", json={"order_ids": ["E1"]}).status_code == 200
    j = nxt(client)["job"]
    g = dict(ids=["E1"], skus=["Magicians_35"])
    assert seen(client, j["id"], where="expired", **g, awb="111111111111122") == {"do": "regenerate"}
    assert step(client, j["id"], "regenerating") == {"do": "go"}
    assert step(client, j["id"], "regenerated") == {"do": "find"}
    assert nxt(client)["job"]["step"] == "found_again"
    assert seen(client, j["id"], **g, awb="111111111111199") == {"do": "logistic"}
    # still in Expired AWB after a Regenerate: stop
    client.post(f"/api/labels/{j['id']}/cancel")
    client.post("/api/labels", json={"order_ids": ["E1"]})
    j = nxt(client)["job"]
    seen(client, j["id"], where="expired", **g, awb="1234567")
    step(client, j["id"], "regenerating")
    step(client, j["id"], "regenerated")
    a = seen(client, j["id"], where="expired", **g, awb="1234567")
    assert a["do"] == "stop" and "Still in Expired AWB" in a["error"]


def test_not_signed_in_waits_for_a_person(client, setup):  # noqa: F811
    client.post("/api/ingest", files={"file": ("r.csv", report([("L1", "", "PRINT", "Magicians_35", "Lina", 1)]), "text/csv")})
    made("L1")
    client.post("/api/labels", json={"order_ids": ["L1"]})
    j = nxt(client)["job"]
    client.post("/api/helper/shopdeck/login", json={"job_id": j["id"]})
    assert job_of(client, "L1")["state"] == "login"
    assert nxt(client) == {"job": None, "login": True, "key": "L1"}  # the page does nothing meanwhile
    assert client.post(f"/api/labels/{j['id']}/continue").json()["state"] == "working"
    assert nxt(client)["job"]["step"] == "find"


def test_no_pdf_in_time_stops_and_label_pressed_cant_be_cancelled(client, setup):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.pdf_timeout_s = 20
    save_settings(server.SETTINGS)
    client.post("/api/ingest", files={"file": ("r.csv", report([("P1", "", "PRINT", "Magicians_35", "Pia", 1)]), "text/csv")})
    made("P1")
    client.post("/api/labels", json={"order_ids": ["P1"]})
    j = nxt(client)["job"]
    seen(client, j["id"], ids=["P1"], skus=["Magicians_35"], awb="5555555")
    step(client, j["id"], "saving", courier="BLUEDART_AIR")
    step(client, j["id"], "saved", awb="6666666", courier="BLUEDART AIR")
    a = seen(client, j["id"], ids=["P1"], skus=["Magicians_35"], awb="6666666", partner="BLUEDART AIR")
    assert a == {"do": "label"}, (a, job_of(client, "P1"))
    assert step(client, j["id"], "pressing") == {"do": "go"}
    assert client.post(f"/api/labels/{j['id']}/cancel").status_code == 409
    with db.tx() as con:  # as if pressed long ago
        con.execute("UPDATE label_jobs SET pressed_at=? WHERE id=?", (time.time() - 60, j["id"]))
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P1") or {}).get("state") == "failed")
    assert "No label PDF arrived" in job_of(client, "P1")["error"]


def test_labels_off_means_no_work(client, setup):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.enabled = False
    assert nxt(client) == {"job": None, "off": True}
    assert client.post("/api/labels", json={"order_ids": ["X"]}).status_code == 409


def test_settings_validate_labels():
    from snapstl.settings import Settings

    s = Settings()
    assert not s.validate()
    s.labels.couriers = []
    s.labels.wait_after_logistics_s = -1
    errs = " ".join(s.validate())
    assert "courier" in errs and "wait after logistics" in errs
