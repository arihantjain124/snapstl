"""Stats page (0.26): how much is made per day against what comes in, the backlog, printers, checkers.

Everything is worked out from what SnapSTL already keeps on this computer: orders (`made_at` = checked
good, since 0.24 / 18 Nov 2026), print jobs, checks and the uploaded report (payment, pickup dates).
Days are this computer's local days. Nothing here changes data.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from . import products, db, tabs
from .verify import BEFORE, FIX_RESULTS

_T = tabs.TAB_SQL
WAITING = ("to_make", "check_name", "ready_to_print", "printing")


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).date().isoformat() if ts else ""


def _days(n: int) -> list[str]:
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


ORG = products.organizer_sql()  # 0.29: Stats are the organizer's until name stands are made in SnapSTL


def _pace(con, days: int = 7) -> float:
    """Orders made (checked good) per day over the last `days` days (fewer when checks started later)."""
    since = time.time() - days * 86400
    n, first = con.execute("SELECT COUNT(*), MIN(made_at) FROM orders WHERE made_at >= ?", (since,)).fetchone()
    span = min(float(days), max(1.0, (time.time() - first) / 86400)) if first else float(days)
    return n / span


def stats(days: int = 30, target: int = 0) -> dict:
    days = max(7, min(int(days), 365))
    since = time.time() - days * 86400
    first_day = _days(days)[0]
    with db.tx() as con:
        made = {r[0]: r[1] for r in con.execute(
            "SELECT date(made_at,'unixepoch','localtime'), COUNT(*) FROM orders WHERE made_at >= ? GROUP BY 1", (since,))}
        new = {r[0]: r[1] for r in con.execute(
            f"SELECT order_date, COUNT(*) FROM orders WHERE order_date >= ? AND COALESCE(sku,'')<>'Adhoc' AND {ORG} GROUP BY 1",
            (first_day,))}
        plates = {r[0]: r[1] for r in con.execute(
            "SELECT date(updated_at,'unixepoch','localtime'), COUNT(*) FROM print_jobs WHERE state='done' "
            "AND updated_at >= ? AND printer_id<>0 GROUP BY 1", (since,))}
        tab_n = dict(con.execute(f"SELECT {_T}, COUNT(*) FROM orders WHERE {ORG} GROUP BY 1").fetchall())
        oldest = con.execute(f"SELECT MIN(order_date) FROM orders WHERE {ORG} AND {_T} IN ({','.join('?' * len(WAITING))})",
                             WAITING).fetchone()[0]
        pace7, pace30 = _pace(con, 7), _pace(con, 30)
        printers = db.rows(con, "SELECT printer_name AS name, SUM(state='done') AS done, SUM(state='failed') AS failed, "
                                "SUM(state='cancelled') AS cancelled, "
                                "SUM(CASE WHEN state='done' AND started_at IS NOT NULL THEN updated_at-started_at ELSE 0 END) AS secs, "
                                "MAX(updated_at) AS last FROM print_jobs WHERE printer_id<>0 AND created_at >= ? "
                                "GROUP BY printer_name ORDER BY done DESC", since)
        checkers = db.rows(con, f"SELECT staff AS name, SUM(result='good') AS good, "
                                f"SUM(result IN ({','.join('?' * len(FIX_RESULTS))}, 'plate_failed')) AS problems, "
                                "COUNT(DISTINCT order_id) AS orders FROM plate_checks WHERE at >= ? AND staff<>? "
                                "GROUP BY staff ORDER BY good DESC", *FIX_RESULTS, since, BEFORE)
        to_made = con.execute("SELECT order_date, made_at FROM orders WHERE made_at >= ? AND order_date IS NOT NULL",
                              (since,)).fetchall()
        rep = {r[0]: json.loads(r[1] or "{}") for r in con.execute(
            "SELECT r.order_id, r.data FROM report_rows r JOIN orders o USING(order_id) WHERE o.order_date >= ?",
            (first_day,))}
        period = db.rows(con, f"SELECT order_id, order_date, {_T} AS tab FROM orders WHERE order_date >= ? "
                              f"AND COALESCE(sku,'')<>'Adhoc' AND {ORG}", first_day)
    # waiting times
    wait_made = [(made_at - datetime.fromisoformat(d[:10]).timestamp()) / 86400 for d, made_at in to_made if d]
    ship = []
    for o in period:
        p = (rep.get(o["order_id"]) or {}).get("pickup_date") or ""
        if p[:10] and o["order_date"]:
            try:
                ship.append((date.fromisoformat(p[:10]) - date.fromisoformat(o["order_date"][:10])).days)
            except ValueError:
                pass
    pay = [str((rep.get(o["order_id"]) or {}).get("payment_method") or "").upper() for o in period]
    n_period = len(period) or 1
    waiting = sum(tab_n.get(t, 0) for t in WAITING)
    today = date.today().isoformat()
    for p in printers:
        p["hours"] = round((p.pop("secs") or 0) / 3600, 1)
        p["fail_pct"] = round(100 * (p["failed"] or 0) / max(1, (p["done"] or 0) + (p["failed"] or 0)))
    for c in checkers:
        c["problem_pct"] = round(100 * (c["problems"] or 0) / max(1, (c["good"] or 0) + (c["problems"] or 0)))
    checks_good = sum(c["good"] or 0 for c in checkers)
    checks_bad = sum(c["problems"] or 0 for c in checkers)
    return {
        "days": [{"day": d, "made": made.get(d, 0), "new": new.get(d, 0), "plates": plates.get(d, 0)} for d in _days(days)],
        "today": {"made": made.get(today, 0), "new": new.get(today, 0), "plates": plates.get(today, 0), "target": target},
        "pace7": round(pace7, 1), "pace30": round(pace30, 1),
        "new7": round(sum(new.get(d, 0) for d in _days(7)) / 7, 1),
        "backlog": {"waiting": waiting, "to_make": tab_n.get("to_make", 0), "check_name": tab_n.get("check_name", 0),
                    "in_production": tab_n.get("ready_to_print", 0) + tab_n.get("printing", 0),
                    "to_pack": tab_n.get("pack_it", 0), "oldest": oldest,
                    "days_to_clear": round(waiting / pace7, 1) if pace7 else None,
                    "days_at_target": round(waiting / target, 1) if target else None},
        "printers": printers, "checkers": checkers,
        "times": {"order_to_made": round(sum(wait_made) / len(wait_made), 1) if wait_made else None,
                  "order_to_shipped": round(sum(ship) / len(ship), 1) if ship else None},
        "rates": {"orders": len(period), "cancelled": sum(o["tab"] == "cancelled" for o in period),
                  "rto": sum(o["tab"] == "rto" for o in period),
                  "cod_pct": round(100 * sum("COD" in p for p in pay) / n_period),
                  "fix_pct": round(100 * checks_bad / max(1, checks_good + checks_bad))},
        "checks_since": "2026-11-18",
    }
