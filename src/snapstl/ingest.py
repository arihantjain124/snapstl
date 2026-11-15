"""Ingest the webstore 'Order Reconciliation Report' (XLSX or CSV).

* Keeps only SKU `Magicians_35` rows.
* Upserts by Order ID, so the same order in later reports is never duplicated.
* Only orders whose report status is PRINT need making; delivered / in transit /
  packed / cancelled / returned orders are recorded but skipped.
* An order we've already bundled is never bundled again (unless reset by hand).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from datetime import datetime, timezone

from . import db

SKU = "magicians_35"
PRINT_STATUSES = {"PRINT"}
COLOR_ALIASES = {"gray": "grey"}
MAX_CLEAN_LETTERS = 15

REQUIRED = ["Order ID", "Order Status", "SKU Code", "Product Customisation"]


@dataclass
class IngestSummary:
    filename: str
    already_seen_file: bool = False
    rows_total: int = 0
    sku_rows: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    to_print_new: int = 0
    needs_review_new: int = 0
    became_not_needed: int = 0
    kept_newer_status: int = 0  # the database already had a newer webstore status for these orders
    report_date: str = ""       # end of the report's "Date Range" (YYYY-MM-DD), if it has one
    warnings: list[str] = field(default_factory=list)


# ---------------- parsing ----------------
def read_table(data: bytes, filename: str) -> list[list[str]]:
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        from python_calamine import CalamineWorkbook

        wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
        sheet = wb.get_sheet_by_index(0)
        return [[_cell(c) for c in r] for r in sheet.to_python()]
    text = data.decode("utf-8-sig", errors="replace")
    return [r for r in csv.reader(io.StringIO(text))]


def _cell(c) -> str:
    if c is None:
        return ""
    if isinstance(c, float) and c.is_integer():
        return str(int(c))
    return str(c)


def find_header(table: list[list[str]]) -> int:
    for i, r in enumerate(table[:30]):
        cells = {c.strip() for c in r}
        if all(h in cells for h in REQUIRED):
            return i
    raise ValueError(
        "Couldn't find the report header row (need columns: " + ", ".join(REQUIRED) + ")"
    )


def parse_customisation(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _is_emoji_or_symbol(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat.startswith("S") or cat in ("Mn", "Me", "Cf", "Co", "Cs") or ord(ch) in (0xFE0F, 0x200D)


def clean_name(raw: str) -> tuple[str, str | None]:
    """Return (name_to_print, review_reason or None)."""
    s = unicodedata.normalize("NFC", (raw or "").strip())
    if not s:
        return "", "No name in the order"
    reasons = []
    kept = []
    dropped_other = []
    for ch in s:
        if ch.isalpha() or ch == " ":
            kept.append(ch)
        elif _is_emoji_or_symbol(ch):
            continue  # silently drop emoji / hearts / symbols
        else:
            dropped_other.append(ch)
    name = re.sub(r"\s+", " ", "".join(kept)).strip()
    if dropped_other:
        reasons.append("Removed " + " ".join(repr(c) for c in dict.fromkeys(dropped_other)))
    if not name:
        return "", "Name has no letters"
    if " " in name:
        reasons.append("Name has a space — confirm the text to print")
    if any(ord(c) > 0x24F for c in name if c.isalpha()):
        reasons.append("Non-Latin letters — check the font supports them")
    letters = sum(c.isalpha() for c in name)
    if letters > MAX_CLEAN_LETTERS:
        reasons.append(f"Very long name ({letters} letters)")
    return name, ("; ".join(reasons) or None)


def normalise_color(raw: str) -> str:
    c = re.sub(r"\s+", " ", (raw or "").strip().lower())
    return COLOR_ALIASES.get(c, c)


# ---------------- how recent is a status? ----------------
# The webstore report has no "generated at" time. A row's status is dated by, in order:
#   1. the latest of the row's own event dates (pickup, delivered, RTO, return),
#   2. the end of the report's "Date Range: … to DD/MM/YYYY" line,
#   3. when the report was uploaded here.
# These make one sortable text (status_at): a status only replaces a stored one when its stamp is
# at least as high, so uploading an older report never undoes a newer status.
EVENT_COLUMNS = ("Pickup Date", "Delivered Date", "RTO Initiated Date", "RTO Delivered Date",
                 "Return Created Date", "Return Delivered Date")
_NO_DATE = "0000-00-00"


def _date(v: str) -> str:
    """'14/11/2026', '14-11-2026', '2026-11-14', with or without a time → '2026-11-14' ('' if none)."""
    from datetime import datetime

    v = (v or "").strip()
    if not v:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", v)  # e.g. ISO with T / timezone
    return m.group(1) if m else ""


def report_end_date(table: list[list[str]], header_row: int) -> str:
    """'Date Range: 30/10/2026 to 14/11/2026' above the header → '2026-11-14'."""
    for r in table[:header_row]:
        for c in r:
            m = re.search(r"to\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})", c or "", re.I)
            if m:
                return _date(m.group(1))
    return ""


def status_stamp(event_date: str, report_date: str, uploaded: str) -> str:
    return f"{event_date or _NO_DATE}|{report_date or _NO_DATE}|{uploaded}"


# ---------------- ingest ----------------
def ingest(data: bytes, filename: str, db_path: Path | None = None) -> IngestSummary:
    sha = hashlib.sha256(data).hexdigest()
    table = read_table(data, filename)
    h = find_header(table)
    header = [c.strip() for c in table[h]]
    ix = {name: i for i, name in enumerate(header)}

    def get(r, col):
        i = ix.get(col)
        return r[i].strip() if i is not None and i < len(r) else ""

    s = IngestSummary(filename=filename)
    body = [r for r in table[h + 1 :] if any(c.strip() for c in r)]
    s.rows_total = len(body)
    s.report_date = report_end_date(table, h)
    uploaded = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with db.tx(db_path) as con:
        if con.execute("SELECT 1 FROM files WHERE sha256=?", (sha,)).fetchone():
            s.already_seen_file = True
        for r in body:
            if get(r, "SKU Code").lower() != SKU:
                continue
            s.sku_rows += 1
            oid = get(r, "Order ID")
            if not oid:
                s.warnings.append("A Magicians_35 row has no Order ID — skipped")
                continue
            _upsert(con, s, filename, {
                "order_id": oid,
                "clubbed_id": get(r, "Clubbed Order ID"),
                "order_date": _iso_date(get(r, "Order Creation Date")),
                "source_status": get(r, "Order Status").upper(),
                "quantity": _qty(get(r, "Quantity")),
                "cust": parse_customisation(get(r, "Product Customisation")),
                "customer_name": get(r, "Customer Name"),
                "phone": get(r, "Mobile"),
                "address": ", ".join(x for x in (get(r, "Address"), get(r, "City"), get(r, "State"), get(r, "Pincode")) if x),
                "status_at": status_stamp(max((_date(get(r, c)) for c in EVENT_COLUMNS), default=""),
                                          s.report_date, uploaded),
            })
        con.execute(
            "INSERT INTO files(filename,sha256,ingested_at,summary) VALUES (?,?,?,?)",
            (filename, sha, db.now(), json.dumps(s.__dict__)),
        )
    return s


def _iso_date(v: str) -> str:
    """Report dates are DD-MM-YYYY; store ISO so they sort correctly."""
    from datetime import datetime

    v = (v or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return v


def _qty(v: str) -> int:
    try:
        return max(1, int(float(v)))
    except ValueError:
        return 1


def _evaluate(name: str, color: str, review: str | None, source_status: str) -> tuple[str, str | None]:
    if source_status not in PRINT_STATUSES:
        return "not_needed", review
    if not color:
        review = "; ".join(x for x in [review, "No colour in the order"] if x)
    return ("needs_review" if review else "ready"), review


def _upsert(con, s: IngestSummary, filename: str, o: dict) -> None:
    cust = o["cust"]
    raw_name = cust.get("enter your first name", "")
    raw_color = cust.get("color", "") or cust.get("colour", "")
    special = cust.get("special requests", "")
    t = db.now()
    cur = con.execute("SELECT * FROM orders WHERE order_id=?", (o["order_id"],)).fetchone()

    if cur is None:
        name, review = clean_name(raw_name)
        color = normalise_color(raw_color)
        if special:
            review = "; ".join(x for x in [review, f"Special request: {special}"] if x)
        state, review = _evaluate(name, color, review, o["source_status"])
        con.execute(
            """INSERT INTO orders(order_id,clubbed_id,sku,order_date,source_status,quantity,raw_name,raw_color,
               special_request,name,color,review_reason,state,first_file,last_file,first_seen,updated_at,
               customer_name,phone,address,status_at,state_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o["order_id"], o["clubbed_id"], "Magicians_35", o["order_date"], o["source_status"], o["quantity"],
             raw_name, raw_color, special, name, color, review, state, filename, filename, t, t,
             o["customer_name"], o["phone"], o["address"], o["status_at"], t),
        )
        s.new += 1
        if state == "ready":
            s.to_print_new += 1
        elif state == "needs_review":
            s.needs_review_new += 1
        return

    # Existing order: refresh report status if this report is newer; never duplicate, never undo a bundle.
    if (cur["status_at"] or "") > o["status_at"]:
        s.kept_newer_status += 1  # an older report: keep the newer status we already have
        con.execute("UPDATE orders SET last_file=?, customer_name=COALESCE(NULLIF(customer_name,''),?), "
                    "phone=COALESCE(NULLIF(phone,''),?), address=COALESCE(NULLIF(address,''),?) WHERE order_id=?",
                    (filename, o["customer_name"], o["phone"], o["address"], o["order_id"]))
        s.unchanged += 1
        return
    changed = cur["source_status"] != o["source_status"]
    state = cur["state"]
    review = cur["review_reason"]
    if state in ("ready", "needs_review") and o["source_status"] not in PRINT_STATUSES:
        state = "not_needed"
        s.became_not_needed += 1
    elif state == "not_needed" and o["source_status"] in PRINT_STATUSES and not cur["batch_id"]:
        state = "needs_review" if review else "ready"
    con.execute(
        "UPDATE orders SET source_status=?, status_at=?, clubbed_id=?, quantity=?, state=?, last_file=?, updated_at=?, "
        "customer_name=COALESCE(NULLIF(?,''),customer_name), phone=COALESCE(NULLIF(?,''),phone), "
        "address=COALESCE(NULLIF(?,''),address) WHERE order_id=?",
        (o["source_status"], o["status_at"], o["clubbed_id"], o["quantity"], state, filename, t,
         o["customer_name"], o["phone"], o["address"], o["order_id"]),
    )
    if changed or state != cur["state"]:
        s.updated += 1
    else:
        s.unchanged += 1


# ---------------- review actions ----------------
def set_name(order_id: str, name: str, color: str | None = None, db_path: Path | None = None) -> dict:
    name = re.sub(r"\s+", " ", name).strip()
    if not any(c.isalpha() for c in name):
        raise ValueError("Name must contain letters")
    with db.tx(db_path) as con:
        cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None:
            raise KeyError(order_id)
        color = normalise_color(color) if color is not None else cur["color"]
        if not color:
            raise ValueError("Colour is required")
        state = cur["state"]
        if state == "needs_review":
            state = "ready"
        con.execute(
            "UPDATE orders SET name=?, color=?, name_edited=1, review_reason=NULL, state=?, updated_at=? WHERE order_id=?",
            (name, color, state, db.now(), order_id),
        )
        return dict(con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone())


def set_printed(order_ids: list[str], printed: bool = True, db_path: Path | None = None) -> int:
    """A person confirms bundled orders were printed (they move to the Printed tab and keep their batch),
    or undoes that (back to Bundled). Returns how many changed."""
    if not order_ids:
        return 0
    q = ",".join("?" * len(order_ids))
    with db.tx(db_path) as con:
        if printed:
            return con.execute(f"UPDATE orders SET state='printed', printed_at=?, updated_at=? "
                               f"WHERE state='bundled' AND order_id IN ({q})", (db.now(), db.now(), *order_ids)).rowcount
        return con.execute(f"UPDATE orders SET state='bundled', printed_at=NULL, updated_at=? "
                           f"WHERE state='printed' AND order_id IN ({q})", (db.now(), *order_ids)).rowcount


def reset_to_ready(order_id: str, db_path: Path | None = None) -> None:
    """Allow a bundled / printed / skipped order to be made again (e.g. reprint)."""
    with db.tx(db_path) as con:
        cur = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if cur is None:
            raise KeyError(order_id)
        state = "needs_review" if cur["review_reason"] else "ready"
        con.execute(
            "UPDATE orders SET state=?, batch_id=NULL, batch_uid=NULL, batch_label=NULL, batch_computer=NULL, "
            "batch_computer_id=NULL, printed_at=NULL, updated_at=? WHERE order_id=?",
            (state, db.now(), order_id),
        )
