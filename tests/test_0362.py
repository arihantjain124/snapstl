"""0.36.2 (owner): labels — Regenerate's "Re-Generate AWB" box is confirmed and the order looked for in Print
again (no courier change when it is already Bluedart Air); a label PDF with the old AWB is downloaded again
from Pack (no courier change there), checked in full, at most pack_tries times; Get label from Pack by hand."""
import pytest

from snapstl import db
from snapstl.settings import save_settings

from .test_033 import job_of, label_text, made, nxt, report, seen, setup, step  # noqa: F401  (setup: fixture)
from .test_flow import client, wait_for  # noqa: F401  (client is a fixture)

BOX = {"text": "Re-Generate AWB\nNew AWB for this order will be created\nCancel\nConfirm", "button": "Confirm"}


@pytest.fixture
def lb(client, setup):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.wait_after_regenerate_s = 0
    server.SETTINGS.labels.pack_wait_s = 0
    server.SETTINGS.labels.pack_tries = 3
    save_settings(server.SETTINGS)
    yield setup
    server.SETTINGS.labels.pack_tries = 3


def _order(c, oid, status="PRINT", name="Riya"):
    c.post("/api/ingest", files={"file": ("r.csv", report([(oid, "", status, "Magicians_35", name, 1)]), "text/csv")})
    made(oid)
    assert c.post("/api/labels", json={"order_ids": [oid]}).status_code == 200
    return nxt(c)["job"]


def _pdf(lb, oid, awb, name):
    f = f"SHP-1-{oid}-{awb}.pdf"
    lb["text"][f] = label_text(oid, awb, [name])
    (lb["dl"] / f).write_bytes(b"%PDF stand-in")
    return f


def _regenerated(c, j, g):
    assert seen(c, j["id"], where="expired", **g, awb="347911101614003", partner="DELHIVERY SURFACE") == {"do": "regenerate"}
    assert step(c, j["id"], "regenerating") == {"do": "go"}
    assert step(c, j["id"], "confirming", **BOX) == {"do": "go"}
    a = step(c, j["id"], "regenerated")
    assert a["do"] == "find" and a["wait_until"] >= a["now"] - 1


def test_regenerate_confirms_the_box_and_keeps_bluedart_air(client, lb):  # noqa: F811
    j = _order(client, "R1", "AWB EXPIRED")
    g = dict(ids=["R1"], skus=["Magicians_35"])
    _regenerated(client, j, g)
    assert seen(client, j["id"], where="expired", **g, awb="347911101614003")["do"] == "wait"  # not in Print yet
    a = seen(client, j["id"], **g, awb="80204475886", partner="BLUEDART AIR")
    assert a["do"] == "wait"  # already Bluedart Air: no courier change, the regenerated AWB is the new one
    jb = job_of(client, "R1")
    assert jb["awb"] == "80204475886" and jb["courier"] == "BLUEDART AIR"
    assert seen(client, j["id"], **g, awb="80204475886", partner="BLUEDART AIR") == {"do": "label"}
    assert step(client, j["id"], "pressing") == {"do": "go"}
    _pdf(lb, "R1", "80204475886", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "R1") or {}).get("state") == "done")
    assert len(lb["printed"]) == 1


def test_regenerate_on_another_courier_changes_it_as_usual(client, lb):  # noqa: F811
    j = _order(client, "R2", "AWB EXPIRED")
    g = dict(ids=["R2"], skus=["Magicians_35"])
    _regenerated(client, j, g)
    assert seen(client, j["id"], **g, awb="80204400001", partner="DELHIVERY SURFACE") == {"do": "logistic"}
    assert step(client, j["id"], "saving", courier="BLUEDART_AIR", offered=["BLUEDART_AIR"]) == {"do": "go"}
    a = step(client, j["id"], "saved", awb="80204400001", courier="BLUEDART AIR")  # the regenerated AWB again
    assert a["do"] == "stop" and "didn't change" in a["error"]


def test_regenerate_on_bluedart_air_with_the_same_awb_stops(client, lb):  # noqa: F811
    j = _order(client, "R3", "AWB EXPIRED")
    g = dict(ids=["R3"], skus=["Magicians_35"])
    _regenerated(client, j, g)
    a = seen(client, j["id"], **g, awb="347911101614003", partner="BLUEDART AIR")
    assert a["do"] == "stop" and "didn't change after Regenerate" in a["error"]


@pytest.mark.parametrize("box,why", [
    ({"text": "Are you sure you want to cancel this order?", "button": "Confirm"}, "doesn't know"),
    ({"text": BOX["text"], "button": "Yes"}, "no Confirm"),
])
def test_any_other_box_after_regenerate_stops(client, lb, box, why):  # noqa: F811
    j = _order(client, "R4", "AWB EXPIRED")
    seen(client, j["id"], where="expired", ids=["R4"], skus=["Magicians_35"], awb="347911101614001")
    step(client, j["id"], "regenerating")
    a = step(client, j["id"], "confirming", **box)
    assert a["do"] == "stop" and why in a["error"] and job_of(client, "R4")["state"] == "failed"


def _to_label(c, j, oid, old="80204294855", new="80204475886"):
    g = dict(ids=[oid], skus=["Magicians_35"])
    seen(c, j["id"], **g, awb=old, partner="DELHIVERY SURFACE")
    step(c, j["id"], "saving", courier="BLUEDART_AIR", offered=["BLUEDART_AIR"])
    step(c, j["id"], "saved", awb=new, courier="BLUEDART AIR")
    assert seen(c, j["id"], **g, awb=new, partner="BLUEDART AIR") == {"do": "label"}
    step(c, j["id"], "pressing")
    return g


def test_an_old_label_is_downloaded_again_from_pack(client, lb):  # noqa: F811
    j = _order(client, "P1")
    g = _to_label(client, j, "P1")
    old = _pdf(lb, "P1", "80204294855", "Riya")  # ShopDeck handed out the label before the courier change
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P1") or {}).get("step") == "pack")
    jb = job_of(client, "P1")
    assert jb["state"] == "working" and lb["printed"] == [] and not (lb["dl"] / old).exists()  # set aside
    n = nxt(client)["job"]
    assert n["step"] == "pack" and n["new_awb"] == "80204475886"
    # Pack: the same order, the new AWB, the same courier — then its label button
    assert seen(client, j["id"], where="pack", **g, awb="80204475886", partner="BLUEDART AIR") == {"do": "packlabel"}
    assert step(client, j["id"], "pressing", button="Download Label", where="pack") == {"do": "go"}
    _pdf(lb, "P1", "80204475886", "Riya")
    assert step(client, j["id"], "pressed") == {"do": "done"}
    wait_for(lambda: (job_of(client, "P1") or {}).get("state") == "done")
    assert len(lb["printed"]) == 1 and lb["printed"][0][0].name == "P1 - 80204475886.pdf"
    ev = [e["what"] for e in client.get(f"/api/labels/{j['id']}/trace").json()["events"]]
    assert any("getting the label from Pack" in e for e in ev)


@pytest.mark.parametrize("kw,why", [
    (dict(awb="80204294855", partner="BLUEDART AIR"), "Pack shows AWB 80204294855"),
    (dict(awb="80204475886", partner="XPRESSBEES"), "Pack shows XPRESSBEES"),
    (dict(where="print", awb="80204475886", partner="BLUEDART AIR"), "not in Pack"),
    (dict(where="none"), "No result found in Pack"),
])
def test_pack_must_show_the_new_awb(client, lb, kw, why):  # noqa: F811
    j = _order(client, "P2")
    g = _to_label(client, j, "P2")
    _pdf(lb, "P2", "80204294855", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P2") or {}).get("step") == "pack")
    a = seen(client, j["id"], **{"where": "pack", **g, **kw})
    assert a["do"] == "stop" and why in a["error"] and lb["printed"] == []


def test_pack_gives_the_old_label_too_often_then_stops(client, lb):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.pack_tries = 1
    save_settings(server.SETTINGS)
    j = _order(client, "P3")
    g = _to_label(client, j, "P3")
    _pdf(lb, "P3", "80204294855", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P3") or {}).get("step") == "pack")
    seen(client, j["id"], where="pack", **g, awb="80204475886", partner="BLUEDART AIR")
    step(client, j["id"], "pressing")
    with db.tx() as con:  # the file of the second try is newer than the new press
        con.execute("UPDATE label_jobs SET pressed_at=pressed_at-10 WHERE id=?", (j["id"],))
    _pdf(lb, "P3", "80204294855", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P3") or {}).get("state") == "failed")
    jb = job_of(client, "P3")
    assert "old label" in jb["error"] and "also from Pack, 1 time" in jb["error"] and lb["printed"] == []
    assert jb["can_pack"]  # a person may try again later


def test_get_label_from_pack_by_hand(client, lb):  # noqa: F811
    from snapstl import server

    server.SETTINGS.labels.pack_tries = 0  # as before 0.36.2: an old label stops the job
    save_settings(server.SETTINGS)
    j = _order(client, "P4")
    g = _to_label(client, j, "P4")
    _pdf(lb, "P4", "80204294855", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P4") or {}).get("state") == "failed")
    assert job_of(client, "P4")["can_pack"]
    server.SETTINGS.labels.pack_tries = 3
    save_settings(server.SETTINGS)
    r = client.post(f"/api/labels/{j['id']}/from-pack", json={"staff": "Ankur"})
    assert r.status_code == 200 and r.json()["state"] == "queued" and r.json()["step"] == "pack"
    assert client.post(f"/api/labels/{j['id']}/from-pack", json={}).status_code == 409  # not stopped any more
    n = nxt(client)["job"]
    assert n["id"] == j["id"] and n["step"] == "pack"
    assert seen(client, j["id"], where="pack", **g, awb="80204475886", partner="BLUEDART AIR") == {"do": "packlabel"}
    step(client, j["id"], "pressing")
    _pdf(lb, "P4", "80204475886", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "P4") or {}).get("state") == "done")
    assert len(lb["printed"]) == 1


def test_get_label_from_pack_needs_a_pressed_label(client, lb):  # noqa: F811
    j = _order(client, "P5")
    seen(client, j["id"], ids=["P5"], skus=["Magicians_35"], awb="80204294855")
    client.post(f"/api/labels/{j['id']}/cancel")
    r = client.post(f"/api/labels/{j['id']}/from-pack", json={})
    assert r.status_code == 409 and "already pressed" in r.json()["detail"]


# ---------------- an order found in Pack (owner): Bluedart Air → label there; else Air if offered, else as it is
def test_an_order_in_pack_on_bluedart_air_gets_its_label_from_pack(client, lb):  # noqa: F811
    j = _order(client, "K1", "PACK")  # PACK in the report: Print label is allowed now
    g = dict(ids=["K1"], skus=["Magicians_35"])
    assert seen(client, j["id"], where="pack", **g, awb="80204400011", partner="BLUEDART AIR") == {"do": "packlabel"}
    jb = job_of(client, "K1")
    assert jb["awb"] == "80204400011" and jb["courier"] == "BLUEDART AIR"
    assert step(client, j["id"], "pressing", where="pack", option="Label") == {"do": "go"}
    _pdf(lb, "K1", "80204400011", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "K1") or {}).get("state") == "done")
    assert len(lb["printed"]) == 1


def test_an_order_in_pack_switches_to_bluedart_air_when_offered(client, lb):  # noqa: F811
    j = _order(client, "K2", "PACK")
    g = dict(ids=["K2"], skus=["Magicians_35"])
    assert seen(client, j["id"], where="pack", **g, awb="347911102322370", partner="DELHIVERY SURFACE") == {"do": "packlogistic"}
    assert nxt(client)["job"]["step"] == "packlogistic"
    a = step(client, j["id"], "pack_offered", offered=["DELHIVERY_SURFACE", "XPRESSBEES", "BLUEDART_AIR"],
             current="DELHIVERY SURFACE")
    assert a == {"do": "change", "courier": "BLUEDART_AIR"}
    assert step(client, j["id"], "pack_saved")["do"] == "find"
    # back in Print, not changed yet → look again; then the new AWB on Bluedart Air → the usual wait and Label
    assert seen(client, j["id"], **g, awb="347911102322370", partner="DELHIVERY SURFACE")["do"] == "wait"
    assert seen(client, j["id"], **g, awb="80204400022", partner="BLUEDART AIR")["do"] == "wait"
    assert job_of(client, "K2")["awb"] == "80204400022"
    assert seen(client, j["id"], **g, awb="80204400022", partner="BLUEDART AIR") == {"do": "label"}


def test_an_order_in_pack_keeps_its_courier_when_air_isnt_offered(client, lb):  # noqa: F811
    j = _order(client, "K3", "PACK")
    g = dict(ids=["K3"], skus=["Magicians_35"])
    seen(client, j["id"], where="pack", **g, awb="347911102322371", partner="DELHIVERY SURFACE")
    a = step(client, j["id"], "pack_offered", offered=["DELHIVERY_SURFACE", "XPRESSBEES"], current="DELHIVERY SURFACE")
    assert a == {"do": "keep"}
    assert nxt(client)["job"]["step"] == "packlabel"
    assert seen(client, j["id"], where="pack", **g, awb="347911102322371", partner="DELHIVERY SURFACE") == {"do": "packlabel"}
    step(client, j["id"], "pressing", where="pack", option="Label")
    _pdf(lb, "K3", "347911102322371", "Riya")
    step(client, j["id"], "pressed")
    wait_for(lambda: (job_of(client, "K3") or {}).get("state") == "done")
    assert job_of(client, "K3")["awb"] == "347911102322371" and len(lb["printed"]) == 1


def test_pack_logistics_with_couriers_hidden_stops(client, lb):  # noqa: F811
    j = _order(client, "K4", "PACK")
    seen(client, j["id"], where="pack", ids=["K4"], skus=["Magicians_35"], awb="347911102322372", partner="XPRESSBEES")
    a = step(client, j["id"], "pack_offered", offered=["XPRESSBEES"], more_hidden=True)
    assert a["do"] == "stop" and "Show more" in a["error"]


def test_a_reload_during_the_change_in_pack_stops(client, lb):  # noqa: F811
    j = _order(client, "K5", "PACK")
    g = dict(ids=["K5"], skus=["Magicians_35"])
    seen(client, j["id"], where="pack", **g, awb="347911102322373", partner="XPRESSBEES")
    step(client, j["id"], "pack_offered", offered=["BLUEDART_AIR"])
    a = seen(client, j["id"], where="pack", **g, awb="347911102322373", partner="XPRESSBEES")
    assert a["do"] == "stop" and "may not have been saved" in a["error"]
