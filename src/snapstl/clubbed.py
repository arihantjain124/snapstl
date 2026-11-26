"""Clubbed orders are made together (0.34, owner): one parcel can only ship once every name in it is made.

* Ticking one name of a clubbed order (or Select oldest / Make next batch) takes **every** name of it that is to
  make — of the same product, since a batch is one product.
* While any name of it still needs a name check (support asks the customer), the whole clubbed order waits:
  none of it goes into a batch (`held`).
* A clubbed order whose other names are already in production or made comes first on Pending Orders, after
  Express (`PARTNER_SQL`): it's the one holding a parcel back.
"""
from __future__ import annotations

from . import db, products, tabs

# the other names of it are in production, made or packed
STARTED = ("ready_to_print", "printing", "pack_it", "ship_it")
_O2 = tabs.case_sql(lambda c: "o2." + c)
PARTNER_SQL = (f"(COALESCE(orders.clubbed_id,'')<>'' AND EXISTS (SELECT 1 FROM orders o2 WHERE "
               f"o2.clubbed_id=orders.clubbed_id AND o2.order_id<>orders.order_id AND {_O2} IN "
               f"({', '.join(repr(s) for s in STARTED)})))")


def groups(con, clubbed_ids) -> dict[str, list[dict]]:
    """clubbed ID → its orders (with their tab), only clubbed orders of more than one line."""
    ids = sorted({c for c in clubbed_ids if c})
    if not ids:
        return {}
    out: dict[str, list[dict]] = {}
    for r in db.rows(con, f"SELECT order_id, clubbed_id, name, raw_name, sku, {tabs.TAB_SQL} AS tab FROM orders "
                          f"WHERE clubbed_id IN ({','.join('?' * len(ids))}) ORDER BY order_id", *ids):
        out.setdefault(r["clubbed_id"], []).append(r)
    return {k: v for k, v in out.items() if len(v) > 1}


def _check(members: list[dict]) -> list[str]:
    return [m["name"] or m["raw_name"] or m["order_id"] for m in members if m["tab"] == "check_name"]


def info(members: list[dict]) -> dict:
    """For a row on Pending Orders: how many names, how many to make, who needs a name check, how many started."""
    check = _check(members)
    return {"size": len(members), "to_make": sum(m["tab"] in ("to_make", "check_name") for m in members),
            "check": check, "held": bool(check), "started": sum(m["tab"] in STARTED for m in members)}


def _clubbed_of(con, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    return {r["order_id"]: r for r in db.rows(con, f"SELECT order_id, clubbed_id, sku FROM orders "
                                                   f"WHERE order_id IN ({','.join('?' * len(ids))})", *ids)}


def whole(con, order_ids: list[str]) -> tuple[list[str], int, dict[str, list[str]]]:
    """The ticked orders with every other name of their clubbed orders that is to make (same product), and the
    clubbed orders held back because a name still needs its check. → (ids, names added, {clubbed ID: names})."""
    rows = _clubbed_of(con, order_ids)
    g = groups(con, [r["clubbed_id"] for r in rows.values()])
    picked = set(order_ids)
    out: list[str] = []
    seen: set[str] = set()
    added = 0
    held: dict[str, list[str]] = {}
    for oid in order_ids:
        if oid in seen:
            continue
        r = rows.get(oid)
        ms = g.get((r or {}).get("clubbed_id") or "")
        if not ms:
            out.append(oid)
            seen.add(oid)
            continue
        cid = r["clubbed_id"]
        check = _check(ms)
        if check:
            held[cid] = check
            seen.update(m["order_id"] for m in ms)
            continue
        key = products.of_order(r).key
        for m in [r] + [m for m in ms if m["order_id"] != oid]:
            if m["order_id"] in seen:
                continue
            if m["order_id"] != oid and (m["tab"] != "to_make" or products.of_order(m).key != key):
                continue
            seen.add(m["order_id"])
            out.append(m["order_id"])
            added += m["order_id"] not in picked
    return out, added, held


def take(con, ids: list[str], n: int) -> tuple[list[str], dict[str, list[str]]]:
    """Up to `n` of `ids` (oldest first), never splitting a clubbed order: its names among `ids` go together, and
    one with a name still to check is left out. The first clubbed order is taken whole even past `n`."""
    rows = _clubbed_of(con, ids)
    g = groups(con, [r["clubbed_id"] for r in rows.values()])
    out: list[str] = []
    seen: set[str] = set()
    held: dict[str, list[str]] = {}
    for oid in ids:
        if oid in seen:
            continue
        cid = (rows.get(oid) or {}).get("clubbed_id") or ""
        ms = g.get(cid)
        if not ms:
            part = [oid]
        else:
            check = _check(ms)
            if check:
                held[cid] = check
                seen.update(i for i in ids if (rows.get(i) or {}).get("clubbed_id") == cid)
                continue
            part = [i for i in ids if (rows.get(i) or {}).get("clubbed_id") == cid]
        seen.update(part)
        if out and len(out) + len(part) > n:
            continue
        out += part
        if len(out) >= n:
            break
    return out, held


def held_words(held: dict[str, list[str]]) -> str:
    if not held:
        return ""
    names = sorted({n for ns in held.values() for n in ns})
    return (f"{len(held)} clubbed order{'s' if len(held) > 1 else ''} left out — waiting for the name check of "
            f"{', '.join(names[:6])}{'…' if len(names) > 6 else ''} (support asks the customer)")
