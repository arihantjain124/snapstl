"""Shipping labels from ShopDeck (0.33, owner 2026-11-24): Print → Completed → **Print label**.

Who does what:

* **SnapSTL** decides. It knows what must be in the parcel (the report's lines: every sub-order of a clubbed
  order, SnapSTL's products and others), refuses a parcel that isn't ready, checks every page the helper reads
  against that, checks the label PDF, prints it and marks the orders *Label done – ship it*.
* **The SnapSTL Helper** (the Chrome extension, `helper/shopdeck.js`) only operates ShopDeck's Process Orders
  page in the person's own signed-in Chrome: search the order, Regenerate an expired AWB, change the courier,
  press Label. It reports what it sees; SnapSTL answers what to do next or stops the job. It never types a
  password or OTP and never clicks on the login pages: a job waits until a person signs in and presses Continue.

One wrong label can send customised pieces to the wrong person, so every check fails closed: anything
unexpected stops the job with the reason, and a stopped job is never retried by itself.

Steps of a job (`label_jobs.step`): find → (regenerate → find) → logistic → saving → wait → check → label →
pressing → pdf → done. States: queued, working, login (waiting for a person to sign in), printing, done,
failed, cancelled.

0.33.2 (a day of staff use: 14 of 23 presses hung while "finding the order"): the helper brings the ShopDeck
tab to the front for each job (Chrome slows and freezes background tabs) and sends a heartbeat; a job no tab
takes within `pickup_timeout_s`, or whose tab goes quiet for `stall_timeout_s`, stops with the reason. Every
look and step is kept in `label_events` and shown under the job.

0.36.2 (owner): Expired AWB → Regenerate asks "Re-Generate AWB — New AWB for this order will be created":
the helper presses Confirm on exactly that box (SnapSTL checks its words), waits `wait_after_regenerate_s`
and looks in Print again (every so often until `regenerate_timeout_s`). Already on the first courier of
Settings (Bluedart Air) after Regenerate: no courier change — the regenerated AWB is the new one. Else the
usual courier change. A label PDF with an old AWB (ShopDeck hands out the old label for a while after a
change) isn't the end any more: after `pack_wait_s` the helper finds the order in **Pack** (no courier change
there), SnapSTL checks it shows the new AWB, the helper downloads the label there and the PDF is checked in
full again — at most `pack_tries` times. A stopped job whose label was pressed can do the same by hand:
**Get label from Pack**.

Pack (owner, 0.36.2): the order is also looked for in Pack (after Print and Expired AWB), and Print label works for
orders the report shows as PACK. In Pack: on the first courier of Settings (Bluedart Air) → the label is downloaded
there (Print ▾ → label). Else Logistic is opened: the first courier offered → it is changed (Save → Yes,Change), the
order goes back to Print and the usual Print flow follows (new AWB, wait, Label); not offered → the panel is closed
unsaved and the label is downloaded on the courier it has.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, platform_os, products

ACTIVE = ("queued", "working", "login", "printing")
OK_STATUSES = ("PRINT", "AWB-EXPIRED", "PACK")  # a label can be made / fetched (0.36.2: PACK too, from Pack)
ORGANIZER_FIELD = "ENTERYOURFIRSTNAME:"   # how the label prints the organizer's name (seen 2026-11-24)
_LOCK = threading.RLock()


class LabelError(ValueError):
    pass


# ---------------- the parcel ----------------
def _norm_status(s: str | None) -> str:
    return (s or "").upper().replace("_", "-").replace(" ", "-")


def norm(s: str | None) -> str:
    """Text compared without case, spaces or punctuation (PDF text and page text break lines differently)."""
    return re.sub(r"[^\w:]|_", "", (s or "").upper())


def courier_key(s: str | None) -> str:
    """'BLUEDART_AIR' / 'Bluedart Air' / 'BLUE DART AIR' → 'BLUEDARTAIR'."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def parcel(con, order_id: str) -> dict:
    """What ShopDeck must show for this order's parcel, from the report lines kept on this computer:
    {key, clubbed, search, lines: [{order_id, sku, quantity, status, ours, name, made, label_at}], others: [...]}.
    A clubbed order is every line with its Clubbed Order ID (cancelled lines left out)."""
    o = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if o is None:
        raise LabelError("No such order")
    o = dict(o)
    if (o.get("sku") or "").lower() == "adhoc":
        raise LabelError("An ad-hoc order isn't on ShopDeck — no label to print")
    key = (o.get("clubbed_id") or "").strip() or order_id
    clubbed = key != order_id
    if clubbed:
        rows = db.rows(con, "SELECT order_id, data FROM shop_lines WHERE json_extract(data, '$.\"Clubbed Order ID\"')=?",
                       key)
    else:
        rows = db.rows(con, "SELECT order_id, data FROM shop_lines WHERE order_id=?", order_id)
    if not rows:
        raise LabelError("This order's report line isn't on this computer — upload the latest report first")
    lines = []
    for r in rows:
        d = json.loads(r["data"] or "{}")
        status = _norm_status(d.get("Order Status"))
        if "CANCEL" in status:
            continue
        sku = (d.get("SKU Code") or "").strip()
        prod = products.by_sku(sku)
        mine = con.execute("SELECT * FROM orders WHERE order_id=?", (r["order_id"],)).fetchone() if prod else None
        try:
            qty = max(1, int(float(d.get("Quantity") or 1)))
        except ValueError:
            qty = 1
        lines.append({"order_id": r["order_id"], "sku": sku, "quantity": qty, "status": status,
                      "ours": prod is not None, "kind": prod.kind if prod else "",
                      "name": (mine["raw_name"] or "") if mine else "", "shown": (mine["name"] or "") if mine else "",
                      "made": bool(mine and mine["made_at"] and mine["state"] == "bundled"),
                      "label_at": mine["label_at"] if mine else None,
                      "product": _product_name(d.get("Product Code") or "") or sku})
    lines.sort(key=lambda x: x["order_id"])
    if not any(x["order_id"] == order_id for x in lines):
        raise LabelError("This order is cancelled in the report")
    ours = [x for x in lines if x["ours"]]
    return {"key": key, "clubbed": clubbed, "search": ours[0]["order_id"] if ours else order_id, "lines": lines,
            "others": [x for x in lines if not x["ours"]]}


def _product_name(code: str) -> str:
    from .logistics_api import product_name  # the packing page's words for other products

    return product_name(code) if code else ""


def problems(p: dict) -> list[str]:
    """Why this parcel's label can't be made yet (empty = it can)."""
    out = []
    for x in p["lines"]:
        if x["ours"] and not x["made"]:
            out.append(f"{x['shown'] or x['order_id']} isn't made and checked yet")
        if x["status"] == "ACCEPTANCE-PENDING":  # 0.34: made before it's accepted — no label until then
            out.append(f"{x['order_id']} is still waiting for acceptance — accept it on ShopDeck first")
        elif x["status"] not in OK_STATUSES:
            out.append(f"{x['order_id']} is {x['status'] or 'unknown'} on the website (not waiting for a label)")
    return out


# ---------------- Print label (a person's click) ----------------
def request(order_ids: list[str], staff: str = "", items_ready: bool = False, settings=None) -> list[dict]:
    """Print label for these orders (one parcel each). Refused unless every SnapSTL piece of the parcel is made
    and checked, and — when the parcel holds other products — a person confirmed they're all ready."""
    st = settings or _settings()
    if not st.labels.enabled:
        raise LabelError("Shipping labels are off (Settings → Logistics → Shipping labels)")
    ids = list(dict.fromkeys(o for o in order_ids if o))
    if not ids:
        raise LabelError("Choose an order")
    with _LOCK, db.tx() as con:
        parcels = {}
        for oid in ids:
            p = parcel(con, oid)
            parcels.setdefault(p["key"], p)
        if st.labels.one_at_a_time and len(parcels) > 1:
            raise LabelError("One label at a time (Settings → Logistics → Shipping labels)")
        busy = con.execute(f"SELECT key FROM label_jobs WHERE state IN ({','.join('?' * len(ACTIVE))})",
                           ACTIVE).fetchall()
        if st.labels.one_at_a_time and busy:
            raise LabelError("A label is on its way already — wait for it to finish")
        busy_keys = {r[0] for r in busy}
        out = []
        for key, p in parcels.items():
            if key in busy_keys:
                raise LabelError(f"{key}: a label is on its way already")
            why = problems(p)
            if why:
                raise LabelError(f"{key}: " + "; ".join(why))
            if p["others"] and not items_ready:
                raise LabelError(f"{key}: the parcel also holds " + ", ".join(x["product"] for x in p["others"])
                                 + " — confirm every item is ready first")
            expected = {"lines": [{k: x[k] for k in ("order_id", "sku", "quantity", "ours", "kind", "name")}
                                  for x in p["lines"]]}
            now = db.now()
            cur = con.execute("INSERT INTO label_jobs(key, search, clubbed, expected, state, step, items_ready_by, "
                              "staff, created_at, updated_at) VALUES (?,?,?,?, 'queued', 'find', ?,?,?,?)",
                              (key, p["search"], int(p["clubbed"]), json.dumps(expected),
                               (staff or "someone") if p["others"] else None, staff or None, now, now))
            db.add_activity(con, "label", f"Print label: {key}" + (f" (all items ready — {staff})" if p["others"] else ""))
            _trace(con, int(cur.lastrowid), f"Print label pressed by {staff or 'someone'}"
                   + (" (all items ready)" if p["others"] else ""))
            out.append(_job(con, int(cur.lastrowid)))
    WATCHER.kick()
    return out


def _settings():
    from .settings import load_settings

    return load_settings()


def _job(con, job_id: int) -> dict:
    r = con.execute("SELECT * FROM label_jobs WHERE id=?", (job_id,)).fetchone()
    if r is None:
        raise LabelError("No such label job")
    j = dict(r)
    j["expected"] = json.loads(j["expected"] or "{}")
    return j


def _set(con, job_id: int, **values) -> None:
    values["updated_at"] = db.now()
    con.execute(f"UPDATE label_jobs SET {', '.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), job_id))


def _trace(con, job_id: int, what: str) -> None:
    con.execute("INSERT INTO label_events(job_id, at, what) VALUES (?,?,?)", (job_id, db.now(), what[:1000]))


def trace(job_id: int) -> list[dict]:
    """What the job saw and did, oldest first (for the page)."""
    with db.tx() as con:
        return [{"at": r[0], "what": r[1]} for r in
                con.execute("SELECT at, what FROM label_events WHERE job_id=? ORDER BY id", (job_id,))]


def _fail(con, job: dict, why: str) -> dict:
    _trace(con, job["id"], f"Stopped: {why}")
    _set(con, job["id"], state="failed", error=why[:500])
    db.add_activity(con, "label", f"Label for {job['key']} stopped: {why}"[:500])
    return {"do": "stop", "error": why}


def cancel(job_id: int) -> dict:
    """A person stops a job (before its label is pressed). Safe at any step: nothing is undone on ShopDeck."""
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        if j["step"] in ("pressing", "pdf"):
            raise LabelError("Label was already pressed on ShopDeck — wait for the PDF")
        _trace(con, job_id, f"Stopped by a person (at: {STEP_WORDS.get(j['step'], j['step'])})")
        _set(con, job_id, state="cancelled", error="Stopped by a person")
        return _job(con, job_id)


def signed_in(job_id: int) -> dict:
    """A person signed in to ShopDeck again and pressed Continue."""
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] == "login":
            _trace(con, job_id, "Continue pressed after signing in")
            _set(con, job_id, state="working", error=None)
            _ALIVE[job_id] = time.time()
        out = _job(con, job_id)
    WATCHER.kick()
    return out


# ---------------- the helper's side (helper/shopdeck.js) ----------------
_SEEN = {"at": 0.0}  # the ShopDeck tab's last call (the page says when no ShopDeck tab is open)
_ALIVE: dict[int, float] = {}  # job → the ShopDeck tab's last word about it (heartbeat, look or step)
_VIS: dict[int, str] = {}      # job → the tab's last visibility ("visible" / "hidden")
_FRONT: dict[int, float] = {}  # queued job → since when it is first in line with nothing else working
_BOOT = time.time()


def _last_word(j: dict) -> float:
    return max(_ALIVE.get(j["id"], _BOOT), j["updated_at"] or 0)


def helper_next(settings=None) -> dict:
    """The job the ShopDeck tab works on now (oldest first, one at a time), or None."""
    st = settings or _settings()
    _SEEN["at"] = time.time()
    with _LOCK, db.tx() as con:
        r = con.execute("SELECT id FROM label_jobs WHERE state IN ('working','login') ORDER BY id LIMIT 1").fetchone()
        if r is None:
            r = con.execute("SELECT id FROM label_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                return {"job": None}
            _set(con, r[0], state="working")
            _trace(con, r[0], "ShopDeck tab took the job")
        j = _job(con, r[0])
        if j["state"] == "working":
            _ALIVE[j["id"]] = time.time()
        if j["state"] == "login":
            return {"job": None, "login": True, "key": j["key"]}
        if j["step"] in ("pressing", "pdf"):
            return {"job": None, "waiting": "pdf", "key": j["key"]}  # nothing for the page to do
        lb = st.labels
        return {"job": {"id": j["id"], "key": j["key"], "search": j["search"], "clubbed": bool(j["clubbed"]),
                        "step": j["step"], "old_awb": j["old_awb"], "new_awb": j["new_awb"], "courier": j["courier"],
                        "couriers": list(lb.couriers), "wait_until": j["wait_until"], "now": time.time(),
                        "regenerate_timeout_s": lb.regenerate_timeout_s}}  # wait_until: also after Regenerate / before Pack


def _check_group(job: dict, seen: dict) -> str | None:
    """None when the page shows exactly this parcel, else what differs."""
    lines = job["expected"]["lines"]
    want_ids = sorted(x["order_id"] for x in lines)
    got_ids = sorted(set(seen.get("ids") or []))
    if str(seen.get("count") or "").lstrip("0") != "1":
        return f"the search showed {seen.get('count') or 'no'} results, not exactly one"
    if job["clubbed"]:
        if (seen.get("club") or "") != job["key"]:
            return f"ShopDeck shows clubbed order {seen.get('club') or '(none)'}, not {job['key']}"
        if str(seen.get("sub") or "").lstrip("0") != str(len(lines)):
            return f"ShopDeck shows {seen.get('sub')} sub-orders, the report has {len(lines)}"
    elif seen.get("club"):
        return f"ShopDeck shows it inside clubbed order {seen.get('club')}, the report doesn't"
    if got_ids != want_ids:
        extra = sorted(set(got_ids) - set(want_ids))
        missing = sorted(set(want_ids) - set(got_ids))
        return ("the orders on ShopDeck differ from the report"
                + (f" — not in the report: {', '.join(extra)}" if extra else "")
                + (f" — missing on ShopDeck: {', '.join(missing)}" if missing else ""))
    if sorted(s.upper() for s in (seen.get("skus") or [])) != sorted(x["sku"].upper() for x in lines):
        return "the products (SKU IDs) on ShopDeck differ from the report"
    if not re.fullmatch(r"\d{6,20}", seen.get("awb") or ""):
        return "can't read the AWB on ShopDeck"
    return None


def helper_seen(job_id: int, seen: dict) -> dict:
    """The page found the order: `seen` = {where: print|expired|none, tab, count, club, sub, ids, skus, awb, partner}.
    Answers {do: regenerate|logistic|label|stop|wait}."""
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] != "working":
            return {"do": "stop", "error": f"This job is {j['state']}"}
        _ALIVE[job_id] = time.time()
        _trace(con, job_id, "Saw: " + _seen_words(seen))
        where = seen.get("where")
        if where == "none" and j["step"] in PACK_STEPS:
            return _fail(con, j, "ShopDeck said No result found in Pack ("
                         + ("Clubbed" if j["clubbed"] else "Single") + " tab) — look for the order on ShopDeck by hand")
        if where == "none":
            return _fail(con, j, "ShopDeck said No result found in Print and in Expired AWB ("
                         + ("Clubbed" if j["clubbed"] else "Single")
                         + " tab) or Pack — it may be handed over, cancelled or older than the Expired AWB date range")
        if (seen.get("tab") or "") != ("clubbed" if j["clubbed"] else "single"):
            return _fail(con, j, f"Found in the {seen.get('tab')} tab, expected {'Clubbed' if j['clubbed'] else 'Single'}")
        why = _check_group(j, seen)
        if why:
            return _fail(con, j, why)
        awb = seen["awb"]
        st = _settings().labels
        first = (st.couriers or [""])[0]
        if j["step"] == "packsaving":
            return _fail(con, j, "The courier change in Pack may not have been saved — check the order on ShopDeck by hand")
        if j["step"] in PACK_STEPS:  # 0.36.2: the label from Pack
            if where != "pack":
                return _fail(con, j, f"Found in {where}, not in Pack")
            want = j["old_awb"] if j["step"] == "packlogistic" else j["new_awb"]
            if awb != want:
                return _fail(con, j, f"Pack shows AWB {awb}, expected {want} — check it by hand")
            if j["step"] != "packlogistic" and j["courier"] and courier_key(seen.get("partner")) != courier_key(j["courier"]):
                return _fail(con, j, f"Pack shows {seen.get('partner')}, expected {j['courier']}")
            if j["step"] == "pack":
                return _pack_rule(con, j, seen, first)
            return {"do": j["step"]}
        if j["step"] == "moved":  # changed to the first courier in Pack: the order goes back to Print
            if where == "print" and awb != j["old_awb"]:
                if courier_key(seen.get("partner")) != courier_key(j["courier"]):
                    return _fail(con, j, f"ShopDeck shows {seen.get('partner')} after the change in Pack, expected {j['courier']}")
                return _saved(con, j, awb, seen.get("partner") or j["courier"])
            if where in ("pack", "print") and j["regen_at"] and time.time() - j["regen_at"] < st.regenerate_timeout_s:
                wait_until = time.time() + max(5, st.wait_after_regenerate_s)  # not moved / not changed yet
                _set(con, job_id, wait_until=wait_until)
                return {"do": "wait", "wait_until": wait_until, "now": time.time()}
            return _fail(con, j, f"After the courier change in Pack the order is in {where} with AWB {awb} — check it by hand")
        if j["step"] in ("find", "found_again"):
            if where == "pack" and j["step"] == "find":  # 0.36.2: already in Pack (a label was made before)
                return _pack_rule(con, j, seen, first)
            if where == "expired":
                if j["step"] == "found_again":
                    if j["regen_at"] and time.time() - j["regen_at"] < st.regenerate_timeout_s:
                        wait_until = time.time() + max(5, st.wait_after_regenerate_s)  # not back in Print yet
                        _set(con, job_id, wait_until=wait_until)
                        return {"do": "wait", "wait_until": wait_until, "now": time.time()}
                    return _fail(con, j, "Still in Expired AWB after Regenerate — regenerate it by hand on ShopDeck")
                _set(con, job_id, step="regenerate", old_awb=awb)
                return {"do": "regenerate"}
            if where != "print":
                return _fail(con, j, f"Found in {where}, not in Print")
            if j["step"] == "found_again" and courier_key(seen.get("partner")) == courier_key(first):
                # 0.36.2 (owner): regenerated on the first courier already — the new AWB needs no courier change
                _trace(con, job_id, f"Already {seen.get('partner')} after Regenerate — no courier change")
                return _saved(con, j, awb, seen.get("partner") or first, wait_s=st.wait_after_regenerate_s)
            _set(con, job_id, step="logistic", old_awb=awb)
            return {"do": "logistic"}
        if j["step"] == "saving":  # the page reloaded during Save: did it go through?
            if where == "print" and awb != j["old_awb"]:
                return _saved(con, j, awb, seen.get("partner") or j["courier"] or "")
            return _fail(con, j, "The courier change may not have been saved — check the order on ShopDeck by hand")
        if j["step"] in ("wait", "check", "label"):  # label: the page reloaded before pressing it
            if where != "print":
                return _fail(con, j, f"Found in {where} after the courier change, not in Print")
            if awb != j["new_awb"]:
                return _fail(con, j, f"AWB changed again on ShopDeck ({awb}, expected {j['new_awb']}) — check it by hand")
            if j["courier"] and courier_key(seen.get("partner")) != courier_key(j["courier"]):
                return _fail(con, j, f"ShopDeck shows {seen.get('partner')}, expected {j['courier']}")
            if (j["wait_until"] or 0) > time.time():
                _set(con, job_id, step="wait")
                return {"do": "wait", "wait_until": j["wait_until"], "now": time.time()}
            _set(con, job_id, step="label")
            return {"do": "label"}
        return _fail(con, j, f"Unexpected step {j['step']}")


def _saved(con, j: dict, awb: str, courier: str, settings=None, wait_s: int | None = None) -> dict:
    st = settings or _settings()
    what = "Regenerate" if wait_s is not None else "the courier change"
    if not re.fullmatch(r"\d{6,20}", awb or ""):
        return _fail(con, j, f"Can't read the new AWB after {what}")
    if awb == j["old_awb"]:
        return _fail(con, j, f"The AWB didn't change after {what} — not printing an old label")
    wait_until = time.time() + (st.labels.wait_after_logistics_s if wait_s is None else wait_s)
    _set(con, j["id"], step="wait", new_awb=awb, courier=courier, wait_until=wait_until)
    db.add_activity(con, "label", f"{j['key']}: courier {courier}, new AWB {awb} (old {j['old_awb']})")
    return {"do": "wait", "wait_until": wait_until, "now": time.time()}


def helper_step(job_id: int, step: str, data: dict) -> dict:
    """The page did something: regenerating / regenerated / saving {courier} / saved {awb, courier} /
    pressing / pressed {awb}."""
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] != "working":
            return {"do": "stop", "error": f"This job is {j['state']}"}
        _ALIVE[job_id] = time.time()
        _trace(con, job_id, f"Step: {step}" + ("" if not data else " " + json.dumps(data, ensure_ascii=False)[:600]))
        allowed = {"regenerating": ("regenerate",), "confirming": ("regenerating",), "regenerated": ("regenerating",),
                   "pack_offered": ("packlogistic",), "pack_saved": ("packsaving",),
                   "saving": ("logistic",), "saved": ("saving",), "pressing": ("label", "packlabel"),
                   "pressed": ("pressing",)}
        if j["step"] not in allowed.get(step, ()):
            return _fail(con, j, f"{step} at step {j['step']} — out of order")
        if step == "regenerating":
            _set(con, job_id, step="regenerating")
            return {"do": "go"}
        if step == "confirming":  # 0.36.2: the Re-Generate AWB box — Confirm only exactly that one
            why = _regen_box(data)
            if why:
                return _fail(con, j, why)
            return {"do": "go"}
        if step == "pack_offered":  # 0.36.2: Logistic opened in Pack — change only to the first courier
            first = (_settings().labels.couriers or [""])[0]
            offered = [str(o) for o in (data.get("offered") or [])]
            if data.get("more_hidden"):
                return _fail(con, j, "Show more is still closed in the Edit Logistics panel — couriers may be hidden")
            pick = next((o for o in offered if courier_key(o) == courier_key(first)), None)
            if pick:
                _set(con, job_id, step="packsaving", courier=pick)
                _trace(con, job_id, f"Pack: {j['courier'] or '?'} → {pick} (the order goes back to Print)")
                return {"do": "change", "courier": pick}
            _set(con, job_id, step="packlabel", new_awb=j["old_awb"])  # the label on the courier it has
            _trace(con, job_id, f"Pack: {first} not offered ({', '.join(offered) or 'none'}) — label on {j['courier'] or '?'}")
            return {"do": "keep"}
        if step == "pack_saved":
            wait_until = time.time() + _settings().labels.wait_after_regenerate_s
            _set(con, job_id, step="moved", regen_at=time.time(), wait_until=wait_until)
            db.add_activity(con, "label", f"{j['key']}: courier changed to {j['courier']} in Pack — back to Print")
            return {"do": "find", "wait_until": wait_until, "now": time.time()}
        if step == "regenerated":
            wait_until = time.time() + _settings().labels.wait_after_regenerate_s
            _set(con, job_id, step="found_again", regen_at=time.time(), wait_until=wait_until)
            db.add_activity(con, "label", f"{j['key']}: AWB regenerated on ShopDeck")
            return {"do": "find", "wait_until": wait_until, "now": time.time()}
        if step == "saving":
            courier = str(data.get("courier") or "")
            couriers = _settings().labels.couriers
            if courier_key(courier) not in {courier_key(c) for c in couriers}:
                return _fail(con, j, f"{courier or 'No courier'} isn't one of the couriers in Settings")
            # 0.33.1: the page lists every courier it offers (after Show more); the pick must be the first of
            # Settings' order among them — a missed Show more once picked Bluedart Surface over Bluedart Air
            offered = {courier_key(str(o)) for o in (data.get("offered") or [])}
            if data.get("more_hidden"):
                return _fail(con, j, "Show more is still closed in the Edit Logistics panel — couriers may be hidden")
            best = next((c for c in couriers if courier_key(c) in offered), None)
            if best is None or courier_key(best) != courier_key(courier):
                return _fail(con, j, f"{courier} picked, but {best or 'nothing'} comes first of the couriers offered "
                                     f"({', '.join(sorted(offered)) or 'none listed'})")
            _set(con, job_id, step="saving", courier=courier)
            return {"do": "go"}
        if step == "saved":
            return _saved(con, j, str(data.get("awb") or ""), j["courier"] or str(data.get("courier") or ""))
        if step == "pressing":
            _set(con, job_id, step="pressing", pressed_at=time.time())
            return {"do": "go"}
        # pressed: the PDF is on its way to Downloads
        _set(con, job_id, step="pdf")
    WATCHER.kick()
    return {"do": "done"}


PACK_STEPS = ("pack", "packlabel", "packlogistic")


def _pack_rule(con, j: dict, seen: dict, first: str) -> dict:
    """Owner, 0.36.2: in Pack on the first courier → download the label there; else look at Logistic first."""
    awb, partner = seen["awb"], seen.get("partner") or ""
    if courier_key(partner) == courier_key(first):
        _set(con, j["id"], step="packlabel", new_awb=j["new_awb"] or awb, old_awb=j["old_awb"] or awb, courier=partner)
        return {"do": "packlabel"}
    _set(con, j["id"], step="packlogistic", old_awb=awb, courier=partner)
    return {"do": "packlogistic"}


REGEN_TITLE = "Re-Generate AWB"
REGEN_TEXT = "New AWB for this order will be created"


def _regen_box(data: dict) -> str | None:
    """None when the box the helper is about to confirm is ShopDeck's Re-Generate AWB box (seen 2026-11-26)."""
    text = " ".join(str(data.get("text") or "").split())
    if REGEN_TITLE.lower() not in text.lower() or REGEN_TEXT.lower() not in text.lower():
        return f"Regenerate asked something SnapSTL doesn't know ({text[:160] or 'no text'}) — finish it by hand on ShopDeck"
    if str(data.get("button") or "") != "Confirm":
        return f"The Re-Generate AWB box has no Confirm button ({data.get('button') or 'none'}) — finish it by hand"
    return None


def helper_fail(job_id: int, error: str) -> dict:
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] in ("working", "queued"):
            if j["step"] in ("pressing", "pdf"):  # Label may be pressed: the PDF watcher decides
                return {"ok": True}
            _fail(con, j, error or "The ShopDeck page didn't do what was expected")
        else:
            _trace(con, job_id, f"ShopDeck tab reported (job already {j['state']}): {error}")
        return {"ok": True}


def helper_alive(job_id: int, info: dict) -> dict:
    """Heartbeat from the ShopDeck tab while it works on a job (every few seconds, also while waiting).
    `visible`: the tab's visibility; `note`: something worth keeping (tab brought to the front, …)."""
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if j["state"] in ("working", "login"):
            _ALIVE[job_id] = time.time()
        vis = str(info.get("visible") or "")
        if vis and vis != _VIS.get(job_id):
            if vis != "visible" or job_id in _VIS:
                _trace(con, job_id, "ShopDeck tab is " + ("in front" if vis == "visible" else
                                                          "in the background — Chrome slows it down"))
            _VIS[job_id] = vis
        if info.get("note"):
            _trace(con, job_id, str(info["note"])[:300])
        return {"state": j["state"]}


def _seen_words(s: dict) -> str:
    if s.get("where") == "none":
        return "not on ShopDeck" + (f" ({s['why']})" if s.get("why") else "")
    out = f"{s.get('where')} list, {s.get('tab')} tab, {s.get('count') or 0} result(s)"
    if s.get("club"):
        out += f", clubbed {s['club']} ({s.get('sub') or '?'} sub-orders)"
    return out + (f", orders {', '.join(s.get('ids') or []) or '-'}, SKUs {', '.join(s.get('skus') or []) or '-'}, "
                  f"AWB {s.get('awb') or '-'}, courier {s.get('partner') or '-'}")


def helper_login(job_id: int | None) -> dict:
    """The ShopDeck tab isn't signed in: the job waits for a person (never the helper) to sign in."""
    with _LOCK, db.tx() as con:
        r = con.execute("SELECT id FROM label_jobs WHERE state='working' ORDER BY id LIMIT 1").fetchone()
        jid = job_id or (r[0] if r else None)
        if jid:
            j = _job(con, jid)
            if j["state"] == "working" and j["step"] not in ("pressing", "pdf"):
                _trace(con, jid, "ShopDeck asked for a sign-in")
                _set(con, jid, state="login", error="Sign in to ShopDeck in Chrome, then press Continue")
    return {"ok": True}


# ---------------- the label PDF ----------------
def downloads() -> Path:
    from .reportwatch import folder

    return folder()


_PDF_NAME = re.compile(r"^SHP-\d+-(?P<key>[A-Za-z0-9]+)-(?P<awb>\d+)(?: \(\d+\))?\.pdf$", re.I)


def find_pdf(key: str, since: float) -> Path | None:
    """The newest label PDF for this parcel saved since `since` (ShopDeck names it SHP-<n>-<order>-<AWB>.pdf)."""
    best = None
    d = downloads()
    if not d.is_dir():
        return None
    for f in d.iterdir():
        m = _PDF_NAME.match(f.name)
        if m and m.group("key").upper() == key.upper():
            try:
                t = f.stat().st_mtime
            except OSError:
                continue
            if t >= since - 5 and (best is None or t > best[0]):
                best = (t, f)
    return best[1] if best else None


def pdf_text(path: Path) -> str:
    """The label's text (every page)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


def check_pdf(job: dict, path: Path) -> str | None:
    """None when the label is this parcel's with the new AWB, else what's wrong."""
    m = _PDF_NAME.match(path.name)
    if not m or m.group("awb") != job["new_awb"]:
        return f"The label file has AWB {m.group('awb') if m else '?'}, expected {job['new_awb']} — an old label"
    try:
        raw = pdf_text(path).replace("￾", " ").replace("\r", "\n")
    except Exception as e:  # noqa: BLE001  (a broken or unreadable file)
        return f"Can't read the label PDF ({e})"
    lines = job["expected"]["lines"]

    def field(pattern: str) -> list[str]:
        return re.findall(pattern, raw, flags=re.I)

    awbs = field(r"AWB\s*:\s*(\d+)")
    if awbs != [job["new_awb"]]:
        return f"The label shows AWB {', '.join(awbs) or '(none)'}, expected {job['new_awb']}"
    ids = [x.upper() for x in field(r"Order\s*Id\s*:\s*([A-Za-z0-9]+)")]
    if ids != [job["key"].upper()]:
        return f"The label is for order {', '.join(ids) or '(none)'}, not {job['key']}"
    skus = field(r"Number\s+of\s+Skus\s*:\s*(\d+)")
    if skus != [str(len(lines))]:
        return f"The label lists {', '.join(skus) or 'no'} item(s), the parcel has {len(lines)}"
    qty = sum(int(x.get("quantity") or 1) for x in lines)
    qtys = field(r"Total\s+Quantity\s*:\s*(\d+)")
    if qtys != [str(qty)]:
        return f"The label's total quantity is {', '.join(qtys) or '(none)'}, the parcel has {qty}"
    t = norm(raw)
    for x in lines:
        if not x["ours"]:
            continue
        name = norm(x["name"])
        if not name:
            return f"{x['order_id']}: no name to compare on this computer"
        # the organizer's line reads "Enter Your First Name : <name>" then "Color : <colour>"
        want = (ORGANIZER_FIELD + name + "COLOR:") if x["kind"] == "organizer" else name
        if want not in t:
            return f"The label doesn't show the name {x['name']!r} ({x['order_id']})"
    return None


_OLD_LABEL = ("The label file has AWB", "The label shows AWB")  # check_pdf's words for a label with another AWB


def _set_aside(path: Path) -> None:
    """An old label out of Downloads (kept for a look, never printed)."""
    try:
        d = labels_dir() / "Old labels"
        d.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), d / path.name)
    except OSError:
        pass


def _to_pack(con, j: dict, path: Path, why: str, st) -> None:
    """0.36.2: the PDF had an old AWB — get the label again from Pack after a wait (no courier change there)."""
    _set_aside(path)
    n = (j["pack_tries"] or 0) + 1
    _set(con, j["id"], step="pack", pack_tries=n, pressed_at=None, wait_until=time.time() + st.labels.pack_wait_s)
    _ALIVE[j["id"]] = time.time()
    _trace(con, j["id"], f"{why} — getting the label from Pack in {st.labels.pack_wait_s} s "
                         f"(try {n} of {st.labels.pack_tries})")


def from_pack(job_id: int, staff: str = "") -> dict:
    """Get label from Pack (0.36.2): a stopped job whose Label was pressed on ShopDeck (so the order is in Pack
    with the new AWB) fetches its label there — no courier change. Every check as before."""
    st = _settings()
    if not st.labels.enabled:
        raise LabelError("Shipping labels are off (Settings → Logistics → Shipping labels)")
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        if not _can_pack(j):
            raise LabelError("Get label from Pack is for a stopped label that was already pressed on ShopDeck")
        busy = con.execute(f"SELECT key FROM label_jobs WHERE state IN ({','.join('?' * len(ACTIVE))})",
                           ACTIVE).fetchall()
        if any(r[0] == j["key"] for r in busy) or (st.labels.one_at_a_time and busy):
            raise LabelError("A label is on its way already — wait for it to finish")
        if con.execute("SELECT 1 FROM label_jobs WHERE key=? AND state='done' AND id>?", (j["key"], job_id)).fetchone():
            raise LabelError("This parcel's label was printed since")
        _trace(con, job_id, f"Get label from Pack pressed by {staff or 'someone'}")
        _set(con, job_id, state="queued", step="pack", error=None, pack_tries=0, pressed_at=None, wait_until=None)
        db.add_activity(con, "label", f"Get label from Pack: {j['key']}")
        out = _job(con, job_id)
    WATCHER.kick()
    return out


def _can_pack(j: dict) -> bool:
    return j["state"] == "failed" and bool(j["new_awb"]) and j["step"] in ("pressing", "pdf")


def labels_dir() -> Path:
    from .settings import load_settings

    return Path(load_settings().output_dir) / "Labels" / datetime.now().strftime("%Y-%m-%d")


def _finish(job_id: int, path: Path) -> None:
    """Check, keep, print and mark the parcel's orders Label done (or stop the job with the reason)."""
    st = _settings()
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        why = check_pdf(j, path)
        if why and why.startswith(_OLD_LABEL) and (j["pack_tries"] or 0) < st.labels.pack_tries:
            _to_pack(con, j, path, why, st)
            return
        if why:
            if why.startswith(_OLD_LABEL) and j["pack_tries"]:
                why += f" (also from Pack, {j['pack_tries']} time{'s' if j['pack_tries'] > 1 else ''})"
            _fail(con, j, why + " — nothing printed")
            return
        dest_dir = labels_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{j['key']} - {j['new_awb']}.pdf"
        shutil.move(str(path), dest)  # out of Downloads, kept with the batches' output
        _set(con, job_id, state="printing", step="print", pdf=str(dest))
    try:
        platform_os.print_pdf(dest, st.labels.printer, st.labels.sumatra_path)
        err = None
    except RuntimeError as e:
        err = str(e)
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
        now = db.now()
        for x in j["expected"]["lines"]:
            if x["ours"]:  # Label done – ship it, on every computer (shared records)
                db.set_group(con, x["order_id"], "label", label_at=now, label_by="SnapSTL label")
        if err:
            _set(con, job_id, state="done", step="done", error=f"Label made but not printed: {err} — press Reprint")
        else:
            _set(con, job_id, state="done", step="done", error=None, printed_at=now, prints=(j["prints"] or 0) + 1)
        _trace(con, job_id, f"Label PDF checked, kept as {dest.name}; " + (f"not printed: {err}" if err else
                                                                           f"printed on {st.labels.printer}"))
        db.add_activity(con, "label", f"Label {j['key']} AWB {j['new_awb']} ({j['courier'] or ''})"
                        + (f" — not printed: {err}" if err else f" printed on {st.labels.printer}"))


def reprint(job_id: int) -> dict:
    st = _settings()
    with _LOCK, db.tx() as con:
        j = _job(con, job_id)
    if j["state"] != "done" or not j["pdf"] or not Path(j["pdf"]).is_file():
        raise LabelError("No saved label to print again")
    try:
        platform_os.print_pdf(Path(j["pdf"]), st.labels.printer, st.labels.sumatra_path)
    except RuntimeError as e:
        raise LabelError(str(e)) from e
    with _LOCK, db.tx() as con:
        _set(con, job_id, error=None, printed_at=db.now(), prints=(j["prints"] or 0) + 1)
        db.add_activity(con, "label", f"Label {j['key']} printed again")
        return _job(con, job_id)


class Watcher:
    """Waits for the label PDF of a pressed job (one thread, woken by `kick`)."""

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def kick(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="snapstl-labels")
            self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=2)
            self._wake.clear()
            try:
                if not self.tick():
                    return  # nothing waiting: the thread ends until the next kick
            except Exception:  # noqa: BLE001  (never let one odd file stop the watcher)
                time.sleep(2)

    def tick(self) -> bool:
        """One look at Downloads and the clock. Returns True while some job is queued or working."""
        st = _settings()
        busy = self._timeouts(st)
        with _LOCK, db.tx() as con:
            jobs = [_job(con, r[0]) for r in con.execute(
                "SELECT id FROM label_jobs WHERE state='working' AND step IN ('pressing','pdf')")]
        waiting = busy
        for j in jobs:
            f = find_pdf(j["key"], j["pressed_at"] or 0)
            if f is not None:
                time.sleep(0.5)  # let Chrome finish writing it
                _finish(j["id"], f)
                continue
            if time.time() - (j["pressed_at"] or 0) > st.labels.pdf_timeout_s:
                with _LOCK, db.tx() as con:
                    _fail(con, _job(con, j["id"]), "No label PDF arrived in Downloads — look on ShopDeck (Pack) "
                                                   "and in Downloads before trying again")
                continue
            waiting = True
        return waiting


    @staticmethod
    def _timeouts(st) -> bool:
        """0.33.2: stop a job no ShopDeck tab takes, or whose tab went quiet (Chrome slows background tabs
        down and freezes them; a closed tab says nothing). Label pressed (pressing / pdf): the PDF decides."""
        now = time.time()
        lb = st.labels
        with _LOCK, db.tx() as con:
            rows = [_job(con, r[0]) for r in con.execute(
                "SELECT id FROM label_jobs WHERE state IN ('queued','working','login') ORDER BY id")]
            working = [j for j in rows if j["state"] in ("working", "login")]
            for j in working:
                if j["state"] != "working" or j["step"] in ("pressing", "pdf"):
                    continue
                last = _last_word(j)
                quiet = now - last
                limit = lb.stall_timeout_s + (lb.regenerate_timeout_s if j["step"] in ("regenerate", "regenerating")
                                              else 0)
                if j["step"] in ("wait", "found_again", "pack", "moved") and j["wait_until"]:
                    limit += max(0.0, j["wait_until"] - last)
                if quiet > limit:
                    hidden = _VIS.get(j["id"]) == "hidden"
                    _fail(con, j, f"The ShopDeck tab stopped answering ({int(quiet)} s) while "
                                  f"{STEP_WORDS.get(j['step'], j['step'])}"
                                  + (" — it was in the background" if hidden else "")
                                  + _STALL_NOTE.get(j["step"], "")
                                  + ". Keep ShopDeck's tab open; check the order on ShopDeck before trying again")
            queued = [j for j in rows if j["state"] == "queued"]
            if queued and not working:
                j = queued[0]
                since = _FRONT.setdefault(j["id"], max(now, j["created_at"]))
                if now - since > lb.pickup_timeout_s:
                    _fail(con, j, f"No ShopDeck tab took this label in {lb.pickup_timeout_s} s — open ShopDeck's "
                                  "Process Orders in Chrome (signed in, SnapSTL Helper on), then press Print label again")
            left = con.execute("SELECT COUNT(*) FROM label_jobs WHERE state IN ('queued','working')").fetchone()[0]
            return bool(left)


_STALL_NOTE = {"saving": " — the courier may have been changed on ShopDeck",
               "regenerating": " — Regenerate may have run on ShopDeck",
               "packlabel": " — the label may have been downloaded from Pack",
               "packsaving": " — the courier may have been changed in Pack",
               "regenerate": " — Regenerate may have run on ShopDeck"}

WATCHER = Watcher()


# ---------------- for the page ----------------
def state(order_ids: list[str] | None = None) -> dict:
    """Jobs to show (active ones and the last ones per parcel) and whether the ShopDeck tab is there."""
    with db.tx() as con:
        rows = db.rows(con, "SELECT * FROM label_jobs WHERE state IN ('queued','working','login','printing') "
                            "OR updated_at > ? ORDER BY id DESC LIMIT 200", time.time() - 14 * 86400)
        by_order: dict[str, dict] = {}
        for r in rows:
            j = {**r, "expected": json.loads(r["expected"] or "{}")}
            for x in j["expected"].get("lines", []):
                by_order.setdefault(x["order_id"], _brief(j))
        parcels = {}
        for oid in order_ids or []:
            try:
                p = parcel(con, oid)
            except LabelError as e:
                parcels[oid] = {"error": str(e)}
                continue
            parcels[oid] = {"key": p["key"], "clubbed": p["clubbed"], "problems": problems(p),
                            "others": [{"product": x["product"], "quantity": x["quantity"]} for x in p["others"]],
                            "names": [x["shown"] for x in p["lines"] if x["ours"]]}
    active = [_brief({**r, "expected": {}}) for r in rows if r["state"] in ACTIVE]
    return {"jobs": by_order, "active": active, "parcels": parcels,
            "tab_seen": time.time() - _SEEN["at"] < 15}


STEP_WORDS = {"find": "finding the order on ShopDeck", "regenerate": "regenerating the expired AWB",
              "regenerating": "regenerating the expired AWB", "found_again": "finding the order again",
              "logistic": "changing the courier", "saving": "saving the courier change",
              "wait": "waiting for the new label", "check": "checking the order again", "label": "pressing Label",
              "pressing": "pressing Label", "pdf": "waiting for the label PDF", "print": "printing",
              "pack": "getting the label again from Pack", "packlabel": "getting the label from Pack",
              "packlogistic": "looking at the couriers in Pack", "packsaving": "changing the courier in Pack",
              "moved": "finding the order in Print again",
              "done": "done"}


def _brief(j: dict) -> dict:
    return {"id": j["id"], "key": j["key"], "state": j["state"], "step": j["step"],
            "doing": STEP_WORDS.get(j["step"], j["step"]), "awb": j["new_awb"], "courier": j["courier"],
            "error": j["error"], "printed_at": j["printed_at"], "prints": j["prints"], "wait_until": j["wait_until"],
            "has_pdf": bool(j["pdf"]), "can_pack": _can_pack(j)}
