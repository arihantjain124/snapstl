"""The logistics page's data and actions (0.23): the label and customer-care team.

Served on the logistics listener (port 8002, `logistics.py`, password) and on the main app. Reads give
only what that team needs; the only changes it can make are listed in `logistics.WRITES`:
Label Generated / Undo, Express, name change confirmed with the customer, call notes, customer wants
to cancel, remake request, use a returned piece. Every action is counted (Settings → Usage).
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from datetime import date, datetime, timedelta

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from . import db, sync, tabs
from .ingest import SKU, reset_to_ready, set_name

BY = "logistics"
MAKING = ("to_make", "check_name", "ready_to_print", "printing")
NOT_SHIPPED = (*MAKING, "pack_it")
_T = tabs.TAB_SQL


def _settings():
    from . import server

    return server.SETTINGS.logistics


# ---------------- views (left menu) ----------------
VIEWS: dict[str, tuple[str, str]] = {  # key → (SQL where, order by)
    "labels": (f"{_T}='pack_it'", "(label_note IS NULL), (express_by IS NULL), express_by, order_date, order_id"),
    "ship_it": (f"{_T}='ship_it'", "COALESCE(label_at, 0) DESC, order_date DESC, order_id"),
    "check_name": (f"{_T}='check_name'", "(express_by IS NULL), express_by, order_date, order_id"),
    "problems": (f"{_T}='problems'", "(flag <> 'cancel_request'), order_date DESC, order_id"),
    "remakes": (f"remake_at IS NOT NULL AND {_T} IN ({', '.join(repr(t) for t in NOT_SHIPPED)})",
                "remake_at DESC"),
    "being_made": (f"{_T} IN ({', '.join(repr(t) for t in MAKING)})",
                   "(express_by IS NULL), express_by, order_date, order_id"),
    "on_the_way": (f"{_T}='on_the_way'", "order_date, order_id"),
    "delivered": (f"{_T}='delivered'", "order_date DESC, order_id"),
    "rto": (f"{_T}='rto'", "order_date DESC, order_id"),
    "cancelled": (f"{_T}='cancelled'", "order_date DESC, order_id"),
    # 0.26: waiting a week or more and not shipped — message them before they ask (customer help)
    "late": (f"{_T} IN ({', '.join(repr(t) for t in NOT_SHIPPED)}) AND order_date <= date('now','localtime','-7 days')",
             "order_date, order_id"),
}
FLAG_WORDS = {
    "past_print_not_made": "Website moved it on, but it was never made",
    "cancelled_in_production": "Cancelled – don't pack",
    "cancel_request": "Customer wants to cancel",
    "back_to_print": "Back to print after it was packed – check whether it is made",
    "unknown_status": "Status SnapSTL doesn't know",
}


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _search(q: str) -> tuple[str, list]:
    """Order ID, customer name, the name to print (as it will print or as the customer typed it) or phone (any
    way it's written: the last 10 digits are compared)."""
    q = q.strip()
    d = _digits(q)[-10:]
    parts = [f"COALESCE({c},'') LIKE ?" for c in ("order_id", "clubbed_id", "customer_name", "name", "raw_name")]
    args = [f"%{q}%"] * len(parts)
    if len(d) >= 4:
        phone = "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(phone,''),' ',''),'+',''),'-',''),'(',''),')','')"
        parts.append(f"{phone} LIKE ?")
        args.append(f"%{d}%")
    return "(" + " OR ".join(parts) + ")", args


def _report(con, ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT order_id, data FROM report_rows WHERE order_id IN ({','.join('?' * len(part))})",
                             part):
            out[r[0]] = json.loads(r[1] or "{}")
    return out


def _days_since(d: str | None) -> int | None:
    try:
        return (date.today() - date.fromisoformat((d or "")[:10])).days
    except ValueError:
        return None


def tracking_link(o: dict, rep: dict) -> str:
    awb = str(rep.get("awb") or "").strip()
    pattern = _settings().tracking_url
    if not pattern or ("{awb}" in pattern and not awb):
        return ""
    return pattern.replace("{awb}", awb).replace("{order_id}", o["order_id"])


QUEUED = ("to_make", "check_name", "ready_to_print")  # waiting for its turn to print
PACE_DAYS = 7


def pace(con) -> dict:
    """For "ready around": each waiting order's place in the queue (1 = next; Express first, then the oldest, as
    production takes them) and how many orders were finished (checked good) per day over the last week."""
    pos = {r[0]: i + 1 for i, r in enumerate(con.execute(
        f"SELECT order_id FROM orders WHERE {_T} IN ({', '.join(repr(t) for t in QUEUED)}) "
        "ORDER BY (express_by IS NULL), express_by, order_date, COALESCE(clubbed_id,''), order_id"))}
    since = time.time() - PACE_DAYS * 86400
    made, first = con.execute("SELECT COUNT(*), MIN(made_at) FROM orders WHERE made_at >= ?", (since,)).fetchone()
    days = min(float(PACE_DAYS), max(1.0, (time.time() - first) / 86400)) if first else float(PACE_DAYS)
    return {"pos": pos, "per_day": made / days}


def eta(o: dict, tab: str, pc: dict | None) -> str:
    """The day a waiting order should be printed and checked (ISO date), from its place in the queue and the
    recent pace; "" when there's nothing to go on. For the team only, not for customers (owner, 2026-11-20)."""
    if not pc or tab not in QUEUED or pc["per_day"] <= 0 or o["order_id"] not in pc["pos"]:
        return ""
    return (date.today() + timedelta(days=math.ceil(pc["pos"][o["order_id"]] / pc["per_day"]))).isoformat()


def _short(d: str) -> str:
    try:
        x = date.fromisoformat(d[:10])
    except ValueError:
        return d
    return f"{x.day} {x.strftime('%b')}"


def words(o: dict, tab: str, rep: dict, ready_by: str = "") -> str:
    """Where the order is, in the words customer care says on the phone (0.25: agreed with the owner)."""
    if tab == "printing" and o.get("print_done_at") and not o.get("checked_at"):
        return "Printed – being checked"  # Print → Verify (0.24): packed only after a person checks it
    if tab == "printing":
        return "Printing now"
    if tab == "check_name":
        return "Confirm name with customer"
    if tab in MAKING:
        return "In queue" + (f" – ready around {_short(ready_by)}" if ready_by else "")
    if tab == "pack_it":
        return o.get("label_note") or "Ready to pack – make label"
    if tab == "ship_it":
        return "Packed – waiting for courier"
    if tab == "on_the_way":
        return "Shipped" + (f" with {rep['shipping_partner']}" if rep.get("shipping_partner") else "")
    if tab == "delivered":
        d = (rep.get("delivered_date") or "")[:10]
        return "Delivered" + (f" on {_short(d)}" if d else "")
    if tab == "rto":
        return "Returning to us (RTO)"
    if tab == "cancelled":
        return "Cancelled"
    if tab == "skipped":
        return "Skipped (not being made)"
    return FLAG_WORDS.get(o.get("flag") or "", "Needs checking")


def view_of(o: dict, rep: dict, pc: dict | None = None) -> dict:
    lg = _settings()
    tab = tabs.tab_of(o)
    ready_by = eta(o, tab, pc)
    days = _days_since(o.get("order_date"))
    exp = o.get("express_by")
    exp_days = (date.fromisoformat(exp) - date.today()).days if exp and re.match(r"\d{4}-\d{2}-\d{2}$", exp) else None
    picked = _days_since(rep.get("pickup_date"))
    return {
        "order_id": o["order_id"], "clubbed_id": o.get("clubbed_id") or "", "order_date": o.get("order_date"),
        "days": days, "customer_name": o.get("customer_name") or "", "phone": o.get("phone") or "",
        "address": o.get("address") or "", "name": o.get("name") or "", "raw_name": o.get("raw_name") or "",
        "color": o.get("color") or "", "quantity": o.get("quantity") or 1, "special_request": o.get("special_request") or "",
        "review_reason": o.get("review_reason") or "", "source_status": o.get("source_status") or "",
        "tab": tab, "words": words(o, tab, rep, ready_by), "ready_by": ready_by, "adhoc": o.get("sku") == "Adhoc",
        **{c: o.get(c) for g, cs in db.FIELD_GROUPS.items() for c in cs},
        "courier": rep.get("shipping_partner") or "", "awb": rep.get("awb") or "",
        "payment": rep.get("payment_method") or "", "pickup_date": (rep.get("pickup_date") or "")[:10],
        "delivered_date": (rep.get("delivered_date") or "")[:10], "tracking": tracking_link(o, rep),
        "late": bool(days is not None and days >= lg.late_days and tab in NOT_SHIPPED),
        "stuck": bool(tab == "on_the_way" and picked is not None and picked > lg.stuck_days),
        "express_days": exp_days, "express_red": bool(exp_days is not None and exp_days <= lg.express_red_days),
    }


def _rows(con, where: str, args: list, order: str, limit: int, offset: int) -> tuple[int, list[dict]]:
    total = con.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", args).fetchone()[0]
    rs = db.rows(con, f"SELECT * FROM orders WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", *args, limit, offset)
    rep = _report(con, [r["order_id"] for r in rs])
    pc = pace(con)
    return total, [view_of(r, rep.get(r["order_id"], {}), pc) for r in rs]


def list_orders(request: Request, view: str = "labels", q: str = "", limit: int = 100, offset: int = 0):
    """A left-menu list, or a search over every order (q: order ID, phone, customer name or name to print)."""
    limit = max(1, min(int(limit), 500))
    with db.tx() as con:
        if q.strip():
            w, args = _search(q)
            total, rows = _rows(con, w, args, "order_date DESC, order_id", limit, offset)
        else:
            if view not in VIEWS:
                raise HTTPException(400, f"Unknown list {view!r}")
            w, order = VIEWS[view]
            total, rows = _rows(con, w, [], order, limit, offset)
    return {"total": total, "orders": rows}


def counts():
    with db.tx() as con:
        out = {k: con.execute(f"SELECT COUNT(*) FROM orders WHERE {w}").fetchone()[0] for k, (w, _) in VIEWS.items()}
        out["labels_expired"] = con.execute(f"SELECT COUNT(*) FROM orders WHERE {_T}='pack_it' "
                                            "AND label_note IS NOT NULL").fetchone()[0]
        out["express"] = con.execute(f"SELECT COUNT(*) FROM orders WHERE express_by IS NOT NULL AND {_T} IN "
                                     f"({', '.join(repr(t) for t in NOT_SHIPPED)})").fetchone()[0]
    return out


def meta():
    """Settings the page needs and how fresh the statuses are."""
    lg = _settings()
    with db.tx() as con:
        stamps = [r[0] for r in con.execute("SELECT DISTINCT status_at FROM orders WHERE status_at LIKE '@%'")]
    uploaded = max((s.rsplit("|", 1)[-1] for s in stamps), default="")
    as_of = max((s[1:].split("|", 1)[0] for s in stamps), default="")
    return {"last_upload": uploaded, "report_as_of": as_of.replace("~", " "), "late_days": lg.late_days,
            "express_red_days": lg.express_red_days, "stuck_days": lg.stuck_days, "messages": lg.messages,
            "tracking": bool(lg.tracking_url)}


def order_detail(order_id: str):
    """One order with everything the panel shows: notes, its clubbed group, the customer's other orders,
    returned pieces that would fit."""
    from .server import returned_pieces

    with db.tx() as con:
        cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None:
            raise HTTPException(404, "No such order")
        o = dict(cur)
        group = db.rows(con, "SELECT * FROM orders WHERE COALESCE(clubbed_id,'')<>'' AND clubbed_id=? "
                             "AND order_id<>? ORDER BY order_id", o.get("clubbed_id") or "", order_id)
        d = _digits(o.get("phone") or "")[-10:]
        same = []
        if len(d) >= 7:
            w, args = _search(d)
            same = db.rows(con, f"SELECT * FROM orders WHERE {w} AND order_id<>? ORDER BY order_date DESC LIMIT 20",
                           *args, order_id)
        rep = _report(con, [order_id, *(g["order_id"] for g in group), *(s["order_id"] for s in same)])
        notes = db.rows(con, "SELECT uid, at, text, author FROM order_notes WHERE order_id=? ORDER BY at DESC", order_id)
        returned = returned_pieces(con, o) if o["state"] in ("ready", "needs_review") else []
        pc = pace(con)
        also = also_pack(con, [o.get("clubbed_id")]).get(o.get("clubbed_id") or "", [])
    v = view_of(o, rep.get(order_id, {}), pc)
    return {**v, "notes": notes, "returned": returned, "also": also,
            "group": [view_of(g, rep.get(g["order_id"], {}), pc) for g in group],
            "other_orders": [view_of(s, rep.get(s["order_id"], {}), pc) for s in same]}


CSV_COLS = ("order_id", "clubbed_id", "order_date", "customer_name", "phone", "address", "name", "color",
            "quantity", "words", "courier", "awb", "payment", "pickup_date", "delivered_date", "express_by",
            "express_note", "tracking")


def export_csv(request: Request, view: str = "labels", q: str = ""):
    data = list_orders(request, view, q, limit=500)["orders"]
    f = io.StringIO()
    w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(data)
    return StreamingResponse(iter([f.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=snapstl-{view}.csv"})


# ---------------- actions ----------------
def _order(con, order_id: str) -> dict:
    cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if cur is None:
        raise HTTPException(404, "No such order")
    return dict(cur)


def _group(con, o: dict) -> list[dict]:
    """The order and the others shipped with it (same clubbed order ID)."""
    if not o.get("clubbed_id"):
        return [o]
    return db.rows(con, "SELECT * FROM orders WHERE clubbed_id=? ORDER BY order_id", o["clubbed_id"])


def _done(feature: str) -> dict:
    with db.tx() as con:
        db.count_usage(con, feature)
    sync.LOOP.kick()
    return {"ok": True}


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except ValueError:
        b = {}
    return b if isinstance(b, dict) else {}


async def label(order_id: str, req: Request):
    """Label Generated (the label was made on the website) → Label done – ship it; `undo` takes it back.
    One label covers a clubbed group: every order of it must be printed first."""
    undo = bool((await _body(req)).get("undo"))
    with db.tx() as con:
        o = _order(con, order_id)
        grp = _group(con, o)
        if undo:
            done = [g for g in grp if g.get("label_at") is not None]
            if not done:
                raise HTTPException(409, "No label to undo for this order")
            for g in done:
                db.set_group(con, g["order_id"], "label")
        else:
            if tabs.tab_of(o) != "pack_it":
                raise HTTPException(409, "Only a printed order waiting for its label (Labels to make)")
            waiting = [g["order_id"] for g in grp if tabs.tab_of(g) in MAKING]
            if waiting:
                raise HTTPException(409, f"One label covers the whole clubbed order: waiting for "
                                         f"{len(waiting)} more ({', '.join(waiting)})")
            now = db.now()
            for g in grp:
                if tabs.tab_of(g) == "pack_it":
                    db.set_group(con, g["order_id"], "label", label_at=now, label_by=BY)
    return _done("label_undo" if undo else "label_generated")


async def express(order_id: str, req: Request):
    """Express: the date the customer needs it by (+ note); empty date = not express. Applies to the
    whole clubbed group; only orders not shipped yet."""
    b = await _body(req)
    by = str(b.get("needed_by") or "").strip()
    note = str(b.get("note") or "").strip()[:300]
    if by:
        try:
            datetime.strptime(by, "%Y-%m-%d")
        except ValueError as e:
            raise HTTPException(400, "Pick the date the customer needs it by") from e
    with db.tx() as con:
        o = _order(con, order_id)
        if tabs.tab_of(o) not in NOT_SHIPPED:
            raise HTTPException(409, "Express only for orders not shipped yet")
        for g in _group(con, o):
            if tabs.tab_of(g) in NOT_SHIPPED:
                if by:
                    db.set_group(con, g["order_id"], "express", express_by=by, express_note=note or None)
                else:
                    db.set_group(con, g["order_id"], "express")
    return _done("express_set" if by else "express_clear")


async def change_name(order_id: str, req: Request):
    """Name / colour changed while talking to the customer (New orders or Check name first only, never
    once it's in a batch): needs "Confirmed with customer" and a reason. A Check-name order goes to New
    orders (Checked with customer)."""
    b = await _body(req)
    reason = str(b.get("reason") or "").strip()[:300]
    if b.get("confirmed") is not True:
        raise HTTPException(400, "Tick “Confirmed with customer” first")
    if not reason:
        raise HTTPException(400, "Write why the name is changed")
    with db.tx() as con:
        o = _order(con, order_id)
        if o["state"] not in ("ready", "needs_review") or tabs.tab_of(o) not in ("to_make", "check_name"):
            raise HTTPException(409, "The name can only be changed before the order is in a batch (use Remake request)")
    try:
        set_name(order_id, str(b.get("name") or ""), b.get("color"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    with db.tx() as con:
        db.set_group(con, order_id, "name_note", name_change_reason=reason, name_confirmed_at=db.now())
    return _done("name_changed")


async def note(order_id: str, req: Request):
    text = str((await _body(req)).get("text") or "")
    with db.tx() as con:
        _order(con, order_id)
        try:
            db.add_note(con, order_id, text, BY)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _done("note_added")


async def cancel_request(order_id: str, req: Request):
    """The customer wants to cancel: Problems – check for production at once (the website shows it
    only in the next report). Not for orders already with the courier."""
    why = str((await _body(req)).get("note") or "").strip()[:300]
    with db.tx() as con:
        o = _order(con, order_id)
        if tabs.tab_of(o) not in (*NOT_SHIPPED, "ship_it", "problems"):
            raise HTTPException(409, "Already with the courier or finished: handle it with the courier / website")
        db.set_group(con, order_id, "flag", flag="cancel_request",
                     flag_note="Customer wants to cancel" + (f": {why}" if why else ""))
    return _done("cancel_request")


async def remake(order_id: str, req: Request):
    """Remake request (e.g. arrived broken or with a wrong name): back to New orders with the reason."""
    reason = str((await _body(req)).get("reason") or "").strip()[:300]
    if not reason:
        raise HTTPException(400, "Write why it must be made again")
    with db.tx() as con:
        o = _order(con, order_id)
        if tabs.tab_of(o) in MAKING:
            raise HTTPException(409, "It's being made already")
    reset_to_ready(order_id)
    with db.tx() as con:
        db.set_group(con, order_id, "remake", remake_at=db.now(), remake_reason=reason)
        if o.get("reuse_of"):
            db.set_group(con, order_id, "reuse")
    return _done("remake_request")


async def use(req: Request):
    """+1 use of a page feature (screens, copy buttons, WhatsApp): Settings → Usage."""
    f = str((await _body(req)).get("feature") or "")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", f):
        raise HTTPException(400, "Unknown feature")
    with db.tx() as con:
        db.count_usage(con, f)
    return {"ok": True}


def usage(days: int = 30):
    """Settings → Usage: every computer's counts (shared records), else this computer's."""
    days = max(1, min(int(days), 365))
    if sync.enabled():
        try:
            return {"rows": sync.usage(days), "shared": True}
        except sync.SyncError:
            pass
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with db.tx() as con:
        rows = db.rows(con, "SELECT feature, SUM(count) AS count, COUNT(DISTINCT day) AS days FROM usage_counts "
                            "WHERE day >= ? GROUP BY feature ORDER BY 2 DESC, 1", since)
    return {"rows": rows, "shared": False}


# ---------------- packing page (port 8002, 0.26) ----------------
def product_name(code: str) -> str:
    """'Shopdeck-3D-Decor-Radium-Boo-Ghosts-Peace-Boo' → '3D Decor Radium Boo Ghosts Peace Boo'."""
    s = re.sub(r"^shopdeck-", "", (code or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", s.replace("-", " ")).strip()


def also_pack(con, clubbed_ids) -> dict[str, list[dict]]:
    """0.28: other products ordered in the same parcel (same Clubbed Order ID in the report, not the name
    organizer, not cancelled): clubbed id → [{product, quantity}]. From the report rows kept on this computer."""
    ids = sorted({c for c in clubbed_ids if c})
    if not ids:
        return {}
    out: dict[str, dict[str, int]] = {}
    for (data,) in con.execute(f"SELECT data FROM shop_lines WHERE json_extract(data, '$.\"Clubbed Order ID\"') "
                               f"IN ({','.join('?' * len(ids))})", ids):
        d = json.loads(data or "{}")
        if (d.get("SKU Code") or "").lower() == SKU or "CANCEL" in (d.get("Order Status") or "").upper():
            continue
        name = product_name(d.get("Product Code") or "") or d.get("SKU Code") or "Other product"
        try:
            q = int(float(d.get("Quantity") or 1))
        except ValueError:
            q = 1
        box = out.setdefault(d["Clubbed Order ID"], {})
        box[name] = box.get(name, 0) + q
    return {k: [{"product": p, "quantity": q} for p, q in sorted(v.items())] for k, v in out.items()}


def _groups(con, rows: list[dict]) -> list[dict]:
    """Orders shipped together (same clubbed order ID) as one group, oldest first."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("clubbed_id") or r["order_id"], []).append(r)
    out = []
    for k, os_ in by.items():
        os_.sort(key=lambda o: o["order_id"])
        out.append({"key": k, "clubbed_id": os_[0].get("clubbed_id") or "", "order_date": min(o["order_date"] or "" for o in os_),
                    "express_by": min((o["express_by"] for o in os_ if o.get("express_by")), default=None),
                    "label_at": max((o.get("label_at") or 0) for o in os_) or None, "label_note": os_[0].get("label_note") or "",
                    "orders": [{"order_id": o["order_id"], "name": o.get("name") or "", "color": o.get("color") or "",
                                "quantity": o.get("quantity") or 1} for o in os_]})
    also = also_pack(con, [g["clubbed_id"] for g in out])
    for g in out:
        g["also"] = also.get(g["clubbed_id"], [])
    out.sort(key=lambda g: (g["express_by"] is None, g["express_by"] or "", g["order_date"], g["key"]))
    return out


def _name_search(q: str) -> tuple[str, list]:
    """Packing page (owner, 0.26): only the name to print — what the packer reads on the piece — as it will
    print or as the customer typed it."""
    q = f"%{q.strip()}%"
    return "(COALESCE(name,'') LIKE ? OR COALESCE(raw_name,'') LIKE ?)", [q, q]


def pack_list(request: Request, view: str = "ready", q: str = ""):
    """Packing page: only orders made and checked (Ready to pack), a clubbed order only when every name of it is
    made; or the ones labelled today (Packed today, to undo a mistake). A search shows the whole clubbed order;
    an order that isn't ready says where it is instead."""
    start = datetime.combine(date.today(), datetime.min.time()).timestamp()
    with db.tx() as con:
        made = db.rows(con, f"SELECT *, {_T} AS tab FROM orders WHERE {_T}='pack_it'")
        clubs = {r["clubbed_id"] for r in made if r.get("clubbed_id")}
        partners = db.rows(con, f"SELECT *, {_T} AS tab FROM orders WHERE clubbed_id IN ({','.join('?' * len(clubs))})",
                           *clubs) if clubs else []
        waiting = {r["clubbed_id"] for r in partners if r["tab"] in MAKING}  # a name of it isn't made yet
        ready = [r for r in made if not (r.get("clubbed_id") and r["clubbed_id"] in waiting)]
        today = db.rows(con, f"SELECT *, {_T} AS tab FROM orders WHERE label_at >= ? AND {_T}='ship_it'", start)
        groups = {"ready": _groups(con, ready), "today": _groups(con, today)}
        held = len({r["clubbed_id"] for r in made if r.get("clubbed_id") in waiting})
        out = {"counts": {k: len(v) for k, v in groups.items()}, "held_clubbed": held, "not_ready": []}
        if q.strip():
            w, args = _name_search(q)
            hit = {r["order_id"] for r in db.rows(con, f"SELECT order_id FROM orders WHERE {w}", *args)}
            shown = [g for g in groups["ready"] + groups["today"] if any(o["order_id"] in hit for o in g["orders"])]
            seen = {o["order_id"] for g in shown for o in g["orders"]}
            rest = db.rows(con, f"SELECT *, {_T} AS tab FROM orders WHERE {w} ORDER BY order_date DESC LIMIT 20", *args)
            out["not_ready"] = [{"order_id": r["order_id"], "name": r.get("name") or "", "color": r.get("color") or "",
                                 "words": words(r, r["tab"], {})} for r in rest if r["order_id"] not in seen][:10]
            out["groups"] = shown
        else:
            out["groups"] = groups["today" if view == "today" else "ready"]
    return out


ROUTES = [  # (method, path, endpoint) — registered on the main app and on the logistics listener
    ("GET", "/api/pack/list", pack_list),
    ("GET", "/api/logistics/orders", list_orders),
    ("GET", "/api/logistics/counts", counts),
    ("GET", "/api/logistics/meta", meta),
    ("GET", "/api/logistics/orders.csv", export_csv),
    ("GET", "/api/logistics/order/{order_id}", order_detail),
    ("POST", "/api/logistics/order/{order_id}/label", label),
    ("POST", "/api/logistics/order/{order_id}/express", express),
    ("POST", "/api/logistics/order/{order_id}/name", change_name),
    ("POST", "/api/logistics/order/{order_id}/note", note),
    ("POST", "/api/logistics/order/{order_id}/cancel-request", cancel_request),
    ("POST", "/api/logistics/order/{order_id}/remake", remake),
    ("POST", "/api/usage", use),
]


def register(app) -> None:
    for method, path, fn in ROUTES:
        app.add_api_route(path, fn, methods=[method])
