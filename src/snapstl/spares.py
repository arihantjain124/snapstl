"""Spare pieces (0.28): name pieces printed by mistake (a duplicate, an extra) kept in stock to send instead of
making the same name again — like a returned (RTO) piece.

* A spare is a name exactly as printed (same letter case, as on the piece) and what it is: `bases` (base only —
  the usual one: letters are thrown away, bases are kept) or `both` (base + letters, a complete piece, with the
  letters' colour). A base fits an order of that name whatever its letters' colour (0.28.1).
* A new order with exactly that name and colour is highlighted (Orders → To make → Piece in stock, the Print
  page). A complete piece can be **used**: the order goes to Ready to pack (`reuse_of = "spare:<uid>"`, the same
  field a returned piece uses, so tabs, batches and the shared records already treat it as made) and the piece
  is marked used. A base-only piece is highlighted (print the letters only).
* Batches leave out an order that has a complete piece in stock, so it isn't printed by mistake.
* Rows are shared with every computer (`snapstl.spares`); the newest `changed` wins. Never deleted: a piece
  taken out of stock gets `removed_at`.
"""
from __future__ import annotations

import uuid

from . import db

PARTS = {"bases": "Base only", "both": "Complete (base + letters)"}  # letters aren't kept (owner, 0.28.1)
PREFIX = "spare:"
COLS = ("uid", "name", "color", "part", "note", "staff", "added_at", "used_by", "used_at", "removed_at", "changed",
        "computer")

# a piece in stock for this order (same name, same letter case; a complete piece also the same colour; not used,
# not taken out)
_FREE = ("s.name = {t}.name AND (s.part = 'bases' OR COALESCE(s.color,'') = COALESCE({t}.color,'')) "
         "AND s.used_by IS NULL "
         "AND s.removed_at IS NULL AND COALESCE({t}.name,'') <> ''")


def match_sql(t: str = "orders", complete: bool = False) -> str:
    """SQL condition: order `t` has a piece in stock (`complete`: a base + letters one)."""
    return (f"EXISTS (SELECT 1 FROM spares s WHERE {_FREE.format(t=t)}"
            + (" AND s.part = 'both'" if complete else "") + ")")


class SpareError(ValueError):
    pass


def _clean(name: str, color: str, part: str) -> tuple[str, str, str]:
    name, color = (name or "").strip(), (color or "").strip().lower()
    if not name:
        raise SpareError("Type the name exactly as it is printed on the piece")
    if part not in PARTS:
        raise SpareError("Choose what the piece is: base only or complete (base + letters)")
    return name[:60], ("" if part == "bases" else color[:30]), part


def _touch(con, uid: str, **values) -> None:
    sets = ", ".join(f"{k}=?" for k in values)
    con.execute(f"UPDATE spares SET {sets}, changed=?, sent=0 WHERE uid=?", (*values.values(), db.now(), uid))


def add(name: str, color: str, part: str, note: str = "", staff: str = "", count: int = 1) -> list[dict]:
    from . import computer

    name, color, part = _clean(name, color, part)
    out = []
    with db.tx() as con:
        for _ in range(max(1, min(int(count or 1), 20))):
            r = {"uid": uuid.uuid4().hex, "name": name, "color": color, "part": part, "note": (note or "").strip()[:200]
                 or None, "staff": staff or None, "added_at": db.now(), "changed": db.now(), "computer": computer.NAME}
            con.execute("INSERT INTO spares(uid, name, color, part, note, staff, added_at, changed, computer, sent) "
                        "VALUES (?,?,?,?,?,?,?,?,?,0)", (r["uid"], name, color, part, r["note"], r["staff"],
                                                         r["added_at"], r["changed"], r["computer"]))
            out.append(r)
        db.add_activity(con, "spare", f"Spare piece added: {name} ({color or 'no colour'}, {PARTS[part].lower()})"
                        + (f" ×{len(out)}" if len(out) > 1 else ""))
    return out


def edit(uid: str, name: str, color: str, part: str, note: str = "") -> None:
    name, color, part = _clean(name, color, part)
    with db.tx() as con:
        r = con.execute("SELECT used_by FROM spares WHERE uid=?", (uid,)).fetchone()
        if r is None:
            raise SpareError("No such spare piece")
        if r[0]:
            raise SpareError(f"This piece is already used for {r[0]} — undo that first")
        _touch(con, uid, name=name, color=color, part=part, note=(note or "").strip()[:200] or None)


def remove(uid: str, put_back: bool = False) -> None:
    """Take a piece out of stock (broken, given away…) — or put it back. Never deleted."""
    with db.tx() as con:
        r = con.execute("SELECT name, used_by FROM spares WHERE uid=?", (uid,)).fetchone()
        if r is None:
            raise SpareError("No such spare piece")
        if r[1] and not put_back:
            raise SpareError(f"This piece is used for {r[1]} — undo that on the order first")
        _touch(con, uid, removed_at=None if put_back else db.now())
        db.add_activity(con, "spare", f"Spare piece {'put back' if put_back else 'taken out of stock'}: {r[0]}")


def listing(con=None) -> dict:
    """Orders → Spare pieces: in stock (with the waiting orders each fits), used and taken out (newest first)."""
    from .tabs import TAB_SQL

    def run(con):
        rows = db.rows(con, "SELECT * FROM spares ORDER BY COALESCE(removed_at, used_at, added_at) DESC")
        waiting = db.rows(con, f"SELECT order_id, name, color FROM orders WHERE {TAB_SQL} IN ('to_make','check_name') "
                               f"AND COALESCE(name,'')<>''")
        fits: dict[tuple, list[str]] = {}
        for o in waiting:
            fits.setdefault((o["name"], (o["color"] or "").lower()), []).append(o["order_id"])
        lower = {o["name"].lower(): o["name"] for o in waiting}
        near = {}
        by_name: dict[str, list[str]] = {}
        for o in waiting:
            by_name.setdefault(o["name"], []).append(o["order_id"])
        for r in rows:
            hit = by_name.get(r["name"], []) if r["part"] == "bases" else fits.get((r["name"], (r["color"] or "").lower()), [])
            r["fits"] = hit if not (r["used_by"] or r["removed_at"]) else []
            if not r["fits"] and lower.get(r["name"].lower(), r["name"]) != r["name"]:
                near[r["name"]] = lower[r["name"].lower()]  # same letters, other capitals: not matched
        return {"stock": [r for r in rows if not r["used_by"] and not r["removed_at"]],
                "used": [r for r in rows if r["used_by"]][:200],
                "removed": [r for r in rows if r["removed_at"] and not r["used_by"]][:200],
                "parts": PARTS, "near": near}
    if con is not None:
        return run(con)
    with db.tx() as con:
        return run(con)


def for_order(con, o: dict) -> list[dict]:
    """Pieces in stock that fit this order (complete ones first)."""
    if not o.get("name") or o.get("reuse_of"):
        return []
    rs = db.rows(con, "SELECT uid, name, color, part, note FROM spares s WHERE s.name=? "
                      "AND (s.part='bases' OR COALESCE(s.color,'')=?) "
                      "AND s.used_by IS NULL AND s.removed_at IS NULL", o["name"], o.get("color") or "")
    for r in rs:
        r["complete"] = r["part"] == "both"
        r["what"] = PARTS.get(r["part"], r["part"])
    return sorted(rs, key=lambda r: (not r["complete"], r["uid"]))


def waiting_matches(con) -> int:
    """How many orders to make have a piece in stock (Orders page notice, e.g. after a report import)."""
    from .tabs import TAB_SQL

    return con.execute(f"SELECT COUNT(*) FROM orders WHERE {TAB_SQL} IN ('to_make','check_name') "
                       f"AND {match_sql()}").fetchone()[0]


def in_stock_orders(con, ids) -> set[str]:
    """Of these orders, the ones with a piece in stock (Print page tag)."""
    ids = list({i for i in ids if i})
    if not ids:
        return set()
    return {r[0] for r in con.execute(f"SELECT order_id FROM orders WHERE order_id IN ({','.join('?' * len(ids))}) "
                                      f"AND {match_sql()}", ids)}


def use(order_id: str, uid: str) -> None:
    """Send this order with a complete spare piece: it goes to Ready to pack; the piece is marked used."""
    with db.tx() as con:
        cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None:
            raise SpareError("No such order")
        o = dict(cur)
        if o["state"] not in ("ready", "needs_review") or o.get("reuse_of"):
            raise SpareError("Only an order waiting to be made (not in a batch yet) can use a spare piece")
        hit = next((s for s in for_order(con, o) if s["uid"] == uid), None)
        if hit is None:
            raise SpareError("That piece doesn't match this order (same name, and colour for a complete piece) "
                             "or is already used")
        if not hit["complete"]:
            raise SpareError("Only a complete piece (base + letters) can be sent instead of making the order")
        db.set_group(con, order_id, "reuse", reuse_of=PREFIX + uid)
        _touch(con, uid, used_by=order_id, used_at=db.now())
        db.add_activity(con, "spare", f"Spare piece used: {o['name']} for {order_id}")
        db.count_usage(con, "use_spare")


def release(con, order_id: str, reuse_of: str | None) -> None:
    """The order no longer uses its spare piece (Undo): the piece is in stock again."""
    if reuse_of and reuse_of.startswith(PREFIX):
        _touch(con, reuse_of[len(PREFIX):], used_by=None, used_at=None)


def describe(reuse_of: str | None) -> str:
    """'spare:…' → 'a spare piece'; an order ID → that returned order."""
    if reuse_of and reuse_of.startswith(PREFIX):
        return "a spare piece from stock"
    return f"the returned piece of {reuse_of}" if reuse_of else ""


# ---------------- shared records (sync.py calls these) ----------------
PG_SQL = """
CREATE TABLE IF NOT EXISTS {S}.spares (
    uid        text PRIMARY KEY,
    name       text NOT NULL,
    color      text,
    part       text NOT NULL,
    note       text,
    staff      text,
    added_at   double precision,
    used_by    text,
    used_at    double precision,
    removed_at double precision,
    changed    double precision,
    computer   text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE {S}.spares IS 'Spare name pieces in stock (0.28): part both | names | bases; used_by = the order sent with it; removed_at = taken out of stock. Never deleted; newest `changed` wins.';
CREATE OR REPLACE TRIGGER spares_no_delete BEFORE DELETE ON {S}.spares
    FOR EACH ROW EXECUTE FUNCTION {S}.refuse_delete();
"""


def push(rc, schema: str, many) -> None:
    with db.tx() as con:
        rows = db.rows(con, "SELECT * FROM spares WHERE COALESCE(sent,0)=0")
    if not rows:
        return
    many(rc, f"INSERT INTO {schema}.spares AS s({', '.join(COLS)}) VALUES ({', '.join('%(' + c + ')s' for c in COLS)}) "
             f"ON CONFLICT (uid) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in COLS if c != 'uid')}, "
             "updated_at = clock_timestamp() WHERE COALESCE(EXCLUDED.changed, 0) > COALESCE(s.changed, 0) RETURNING uid",
         [{c: r.get(c) for c in COLS} for r in rows])
    with db.tx() as con:
        for r in rows:  # only if it wasn't changed again meanwhile
            con.execute("UPDATE spares SET sent=1 WHERE uid=? AND changed IS ?", (r["uid"], r["changed"]))


def pull(rc, schema: str) -> None:
    rows = rc.execute(f"SELECT {', '.join(COLS)} FROM {schema}.spares").fetchall()
    with db.tx() as con:
        for r in rows:
            con.execute(f"INSERT INTO spares({', '.join(COLS)}, sent) VALUES ({', '.join('?' * len(COLS))}, 1) "
                        f"ON CONFLICT(uid) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in COLS if c != 'uid')}, "
                        "sent=1 WHERE COALESCE(excluded.changed, 0) > COALESCE(spares.changed, 0)",
                        tuple(r[c] for c in COLS))
