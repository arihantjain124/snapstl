"""Which Orders tab an order is in (0.23): one rule, three places.

`tab_of(order)` (Python), `case_sql()` (the same rule as SQL, for the Orders page in SQLite and for the
`set_tab()` trigger in Supabase). tests/test_tabs.py checks that Python and SQL always agree.
Every order is in exactly one tab; the first matching rule wins (docs/build-plan-order-flow-and-logistics.md):

 1 skipped by a person                                   → skipped
 2 has a flag (db.FLAGS)                                 → problems
 3 webstore CANCELLED*                                   → cancelled
 4 webstore DELIVERED                                    → delivered      ┐ the report counts unless a remake
 5 webstore RTO_*                                        → rto            │ was asked for and no new label
 6 webstore IN-TRANSIT                                   → on_the_way     │ has been made since
 7 Label Generated, or webstore PACK / HANDOVER          → ship_it        ┘
 8 made: all plates printed, marked printed, or sent
   with a returned piece                                 → pack_it
 9 in a batch and a plate started                        → printing
10 in a batch                                            → ready_to_print
11 needs a name check                                    → check_name
12 ready to make                                         → to_make
13 anything else (e.g. a status SnapSTL doesn't know)    → problems
"""
from __future__ import annotations

from typing import Callable

# (key, words on the Orders page, group)
TABS = (
    ("to_make", "To make", "make"),
    ("check_name", "Check name", "make"),
    ("ready_to_print", "In production – waiting to print", "make"),
    ("printing", "In production – printing", "make"),
    ("pack_it", "Ready to pack", "pack"),
    ("ship_it", "Packed", "pack"),
    ("on_the_way", "Shipped", "records"),
    ("delivered", "Delivered", "records"),
    ("rto", "Returned (RTO)", "records"),
    ("cancelled", "Cancelled", "records"),
    ("problems", "Problems", "records"),
    ("skipped", "Skipped", "records"),
)
KEYS = tuple(k for k, _, _ in TABS)
MAKING = ("in_batch", "bundled", "printed")


def norm_status(s: str | None) -> str:
    """'RTO_INITIATED', 'Cancelled Initiated', 'in transit' → 'RTO-INITIATED', 'CANCELLED-INITIATED', 'IN-TRANSIT'."""
    return (s or "").upper().replace("_", "-").replace(" ", "-")


def tab_of(o: dict) -> str:
    """The tab of a local order row (`orders` table)."""
    s = o.get("state") or ""
    st = norm_status(o.get("source_status"))
    label = o.get("label_at")
    remake = o.get("remake_at")
    rep = remake is None or float(label or 0) >= float(remake)
    if s == "skipped":
        return "skipped"
    if o.get("flag") is not None:
        return "problems"
    if st.startswith("CANCEL"):
        return "cancelled"
    if rep and st == "DELIVERED":
        return "delivered"
    if rep and st.startswith("RTO"):
        return "rto"
    if rep and st == "IN-TRANSIT":
        return "on_the_way"
    if label is not None or (rep and st in ("PACK", "HANDOVER")):
        return "ship_it"
    if o.get("made_at") is not None or s == "printed" or o.get("reuse_of") is not None:
        return "pack_it"
    if s in MAKING:
        return "printing" if o.get("print_started_at") is not None else "ready_to_print"
    if s == "needs_review":
        return "check_name"
    if s == "ready":
        return "to_make"
    return "problems"


def case_sql(col: Callable[[str], str] = lambda c: c, state: str | None = None) -> str:
    """The same rule as one SQL CASE (SQLite and Postgres). `col(name)` gives the SQL for a column
    (e.g. `lambda c: 'NEW.' + c` in a trigger); `state` overrides the expression for the local state."""
    s = state or col("state")
    st = f"REPLACE(REPLACE(UPPER(COALESCE({col('source_status')}, '')), '_', '-'), ' ', '-')"
    rep = f"({col('remake_at')} IS NULL OR COALESCE({col('label_at')}, 0) >= {col('remake_at')})"
    making = ", ".join(f"'{m}'" for m in MAKING)
    return (f"(CASE WHEN {s} = 'skipped' THEN 'skipped'"
            f" WHEN {col('flag')} IS NOT NULL THEN 'problems'"
            f" WHEN {st} LIKE 'CANCEL%' THEN 'cancelled'"
            f" WHEN {rep} AND {st} = 'DELIVERED' THEN 'delivered'"
            f" WHEN {rep} AND {st} LIKE 'RTO%' THEN 'rto'"
            f" WHEN {rep} AND {st} = 'IN-TRANSIT' THEN 'on_the_way'"
            f" WHEN {col('label_at')} IS NOT NULL OR ({rep} AND {st} IN ('PACK', 'HANDOVER')) THEN 'ship_it'"
            f" WHEN {col('made_at')} IS NOT NULL OR {s} = 'printed' OR {col('reuse_of')} IS NOT NULL THEN 'pack_it'"
            f" WHEN {s} IN ({making}) AND {col('print_started_at')} IS NOT NULL THEN 'printing'"
            f" WHEN {s} IN ({making}) THEN 'ready_to_print'"
            f" WHEN {s} = 'needs_review' THEN 'check_name'"
            f" WHEN {s} = 'ready' THEN 'to_make'"
            " ELSE 'problems' END)")


TAB_SQL = case_sql()  # for `orders` in SQLite: WHERE {TAB_SQL} = ?
