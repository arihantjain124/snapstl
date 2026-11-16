"""The rest of the webstore report, kept for reading and querying (Supabase: snapstl.report_rows).

The app itself uses only a few report columns (ingest.py). Every other column of each Magicians_35
order — channel, money, COD, shipping, dates, coupon … — is kept here, typed, so another tool or agent
can query it (snapstl.report_rows, joined with the order in snapstl.orders_full).

* On upload, each Magicians_35 row is stored locally (`report_rows`, JSON) and marked unsent; the sync
  sends it (sync._push_reports). Uploads themselves are recorded too (`files.sent` → report_uploads).
* The newest file wins, as for statuses: a row is replaced only by a file whose stamp (ingest
  status_stamp: event date | newest entry in the file | upload time) is at least as high, and a blank
  cell never wipes a stored value — the same rule runs locally and in the database.
* Online / refund transaction IDs are left out on purpose (payment identifiers nobody needs here).
"""
from __future__ import annotations

import json
import re

# (report header, column, SQL type, description). Columns the orders table already has (order ID,
# clubbed ID, order date, status, SKU, quantity, customer name, mobile, customisation) are not repeated.
COLUMNS: list[tuple[str, str, str, str]] = [
    ("Channel", "channel", "text", "Sales channel"),
    ("Product Code", "product_code", "text", "Webstore product code"),
    ("Address", "address_line", "text", "Delivery address line (city, state and pincode are separate)"),
    ("City", "city", "text", "Delivery city"),
    ("State", "delivery_state", "text", "Delivery state (Indian state)"),
    ("Pincode", "pincode", "text", "Delivery pincode"),
    ("Payment Method", "payment_method", "text", "e.g. COD or prepaid"),
    ("Product Amount (Incl. Tax)", "product_amount_incl_tax", "numeric", "₹"),
    ("Product Tax (%)", "product_tax_pct", "numeric", "%"),
    ("Product Tax Amount", "product_tax_amount", "numeric", "₹"),
    ("Product Amount (Excl. Tax)", "product_amount_excl_tax", "numeric", "₹"),
    ("COD Charges (Incl. Tax)", "cod_charges_incl_tax", "numeric", "₹"),
    ("COD Charges Tax (%)", "cod_charges_tax_pct", "numeric", "%"),
    ("COD Charges Tax Amount", "cod_charges_tax_amount", "numeric", "₹"),
    ("COD Charges (Excl. Tax)", "cod_charges_excl_tax", "numeric", "₹"),
    ("Prepaid Discount", "prepaid_discount", "numeric", "₹"),
    ("Delivery Fees (Incl. Tax)", "delivery_fees_incl_tax", "numeric", "₹"),
    ("Delivery Fees Tax (%)", "delivery_fees_tax_pct", "numeric", "%"),
    ("Delivery Fees Tax Amount", "delivery_fees_tax_amount", "numeric", "₹"),
    ("Delivery Fees (Excl. Tax)", "delivery_fees_excl_tax", "numeric", "₹"),
    ("Total Amount (Excl. Tax)", "total_amount_excl_tax", "numeric", "₹"),
    ("Total Tax Amount", "total_tax_amount", "numeric", "₹"),
    ("Invoice Total (Incl. Tax)", "invoice_total_incl_tax", "numeric", "₹ the customer pays"),
    ("Shopdeck Service Fees", "shopdeck_service_fees", "numeric", "₹"),
    ("Cancellation/RTO Revenue", "cancellation_rto_revenue", "numeric", "₹"),
    ("Weight (kg)", "weight_kg", "numeric", "Parcel weight (kg)"),
    ("Dimensions (cm)", "dimensions_cm", "text", "Parcel dimensions (cm)"),
    ("Initial Weight Slab", "initial_weight_slab", "text", "Courier weight slab"),
    ("Final Weight Slab", "final_weight_slab", "text", "Courier weight slab after weighing"),
    ("Shipping Partner", "shipping_partner", "text", "Courier"),
    ("AWB", "awb", "text", "Courier tracking number"),
    ("Pickup Address", "pickup_address", "text", "Where the courier picks up"),
    ("Pickup Date", "pickup_date", "timestamp", "Picked up by the courier"),
    ("Delivered Date", "delivered_date", "timestamp", "Delivered to the customer"),
    ("RTO Initiated Date", "rto_initiated_date", "timestamp", "Return to origin started"),
    ("RTO Delivered Date", "rto_delivered_date", "timestamp", "Returned to origin"),
    ("Return Created Date", "return_created_date", "timestamp", "Customer return created"),
    ("Return Delivered Date", "return_delivered_date", "timestamp", "Customer return delivered back"),
    ("COD Remittance Status", "cod_remittance_status", "text", "COD money paid out?"),
    ("COD Remittance Date", "cod_remittance_date", "timestamp", "When COD money was paid out"),
    ("COD Payable Amount", "cod_payable_amount", "numeric", "₹ COD to be paid out"),
    ("Remittance ID", "remittance_id", "text", "COD payout reference"),
    ("Customer Invoice ID", "customer_invoice_id", "text", "Invoice number"),
    ("Initial Shipping Charge", "initial_shipping_charge", "numeric", "₹"),
    ("Weight Delta", "weight_delta", "numeric", "₹ extra for weight"),
    ("RTO Delta", "rto_delta", "numeric", "₹ extra for return to origin"),
    ("Clubbing Delta", "clubbing_delta", "numeric", "₹ change for clubbed shipping"),
    ("Final Shipping Charge", "final_shipping_charge", "numeric", "₹"),
    ("Billing Invoice ID", "billing_invoice_id", "text", "Shopdeck billing invoice"),
    ("Coupon Code", "coupon_code", "text", "Coupon used"),
    ("Coupon Type", "coupon_type", "text", "Coupon kind"),
    ("Coupon Discount", "coupon_discount", "numeric", "₹"),
    ("Customisation Applied", "customisation_applied", "text", "Whether the order has a customisation"),
    ("Return Condition", "return_condition", "text", "Return policy / condition"),
]
COLS = [c for _, c, _, _ in COLUMNS]
KINDS = {c: t for _, c, t, _ in COLUMNS}


def _number(v: str):
    s = re.sub(r"[₹,\s]", "", v or "")
    if not s or s in ("-", "NA", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _moment(v: str):
    from .ingest import _when

    w = _when(v)
    return w.replace("~", " ") if w else None


def parse(get) -> dict:
    """One report row → {column: value} (only cells that have something). `get(header)` reads a cell."""
    out = {}
    for header, col, kind, _ in COLUMNS:
        raw = (get(header) or "").strip()
        if not raw:
            continue
        val = _number(raw) if kind == "numeric" else _moment(raw) if kind == "timestamp" else raw
        if val is not None:
            out[col] = val
    return out


def store(con, order_id: str, data: dict, stamp: str, filename: str) -> None:
    """Keep this order's report row here (sent by the next sync). Newer file wins; blanks never erase."""
    cur = con.execute("SELECT data, status_at FROM report_rows WHERE order_id=?", (order_id,)).fetchone()
    if cur:
        old = json.loads(cur[0] or "{}")
        newer = stamp >= (cur[1] or "")
        merged = {**old, **data} if newer else {**data, **old}
        if merged == old and not newer:
            return
        stamp = max(stamp, cur[1] or "")
    else:
        merged = data
    con.execute("INSERT OR REPLACE INTO report_rows(order_id, data, status_at, file, sent) VALUES (?,?,?,?,0)",
                (order_id, json.dumps(merged, sort_keys=True), stamp, filename))


# ---------------- in the shared records ----------------
def setup_sql(schema: str) -> str:
    cols = ",\n    ".join(f"{c:<26}{t}" for _, c, t, _ in COLUMNS)
    comments = "\n".join(
        f"COMMENT ON COLUMN {schema}.report_rows.{c} IS '{(h + ' — ' + d).replace(chr(39), chr(39) * 2)}';"
        for h, c, _, d in COLUMNS)
    adds = "\n".join(f"ALTER TABLE {schema}.report_rows ADD COLUMN IF NOT EXISTS {c} {t};" for _, c, t, _ in COLUMNS)
    view_cols = ", ".join(f"r.{c}" for c in COLS)
    return f"""
-- The rest of the webstore report for every Magicians_35 order (report.py), for reading and querying.
CREATE TABLE IF NOT EXISTS {schema}.report_rows (
    order_id                  text PRIMARY KEY,
    {cols},
    status_at                 text,
    report_file               text,
    updated_by                text,
    updated_at                timestamptz NOT NULL DEFAULT clock_timestamp()
);
{adds}
CREATE TABLE IF NOT EXISTS {schema}.report_uploads (
    uid            text PRIMARY KEY,
    filename       text,
    sha256         text,
    computer_name  text,
    uploaded_at    timestamptz,
    as_of          text,
    report_date    text,
    rows_total     integer,
    magicians_rows integer,
    new_orders     integer,
    updated_orders integer
);
CREATE OR REPLACE TRIGGER report_rows_no_delete BEFORE DELETE ON {schema}.report_rows
    FOR EACH ROW EXECUTE FUNCTION {schema}.refuse_delete();
CREATE OR REPLACE TRIGGER report_rows_no_truncate BEFORE TRUNCATE ON {schema}.report_rows
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.refuse_delete();
CREATE OR REPLACE TRIGGER report_uploads_no_delete BEFORE DELETE ON {schema}.report_uploads
    FOR EACH ROW EXECUTE FUNCTION {schema}.refuse_delete();
CREATE OR REPLACE TRIGGER report_uploads_no_truncate BEFORE TRUNCATE ON {schema}.report_uploads
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.refuse_delete();
DROP VIEW IF EXISTS {schema}.orders_full;  -- made again: it lists the orders table's columns as they are now
CREATE VIEW {schema}.orders_full WITH (security_invoker = true) AS
    SELECT o.*, {view_cols}, r.status_at AS report_status_at, r.report_file, r.updated_at AS report_updated_at
      FROM {schema}.order_records o LEFT JOIN {schema}.report_rows r USING (order_id);
COMMENT ON TABLE {schema}.report_rows IS 'Webstore report columns for each Magicians_35 order (one row per order_id), from the newest uploaded report. Join with order_records on order_id, or read orders_full. Read-only for anyone but SnapSTL.';
COMMENT ON TABLE {schema}.report_uploads IS 'Every report uploaded on any SnapSTL computer: when, by which computer, and how recent the file is (as_of = newest date/time in it).';
COMMENT ON VIEW {schema}.orders_full IS 'Each order (order_records: tab, ready_type, who is making it, name, colour …) with its report columns (report_rows). The easiest table to query.';
COMMENT ON COLUMN {schema}.report_rows.status_at IS 'How recent this row is: event date | newest entry in its file | upload time (a newer file replaces it; blanks never erase).';
{comments}
"""


def upsert_sql(schema: str) -> str:
    """Newer file (status_at) wins column by column; a blank (NULL) never erases a stored value."""
    allc = ["order_id", *COLS, "status_at", "report_file", "updated_by"]
    newer = "COALESCE(EXCLUDED.status_at, '') >= COALESCE(r.status_at, '')"
    sets = ", ".join(f"{c} = CASE WHEN {newer} THEN COALESCE(EXCLUDED.{c}, r.{c}) ELSE COALESCE(r.{c}, EXCLUDED.{c}) END"
                     for c in COLS)
    return (f"INSERT INTO {schema}.report_rows AS r ({', '.join(allc)}) VALUES "
            f"({', '.join('%(' + c + ')s' for c in allc)}) ON CONFLICT (order_id) DO UPDATE SET {sets}, "
            f"report_file = CASE WHEN {newer} THEN EXCLUDED.report_file ELSE r.report_file END, "
            f"status_at = GREATEST(r.status_at, EXCLUDED.status_at), updated_by = EXCLUDED.updated_by, "
            "updated_at = clock_timestamp() RETURNING order_id")


def payload(row: dict, computer_name: str) -> dict:
    data = json.loads(row["data"] or "{}")
    p = {c: data.get(c) for c in COLS}
    return p | {"order_id": row["order_id"], "status_at": row["status_at"], "report_file": row["file"],
                "updated_by": computer_name}
