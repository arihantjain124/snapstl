"""The `shop` schema in Supabase: the whole webstore report, organised for answering questions.

SnapSTL's own tables (`snapstl.*`, sync.py) run the app. `shop.*` is for reading and querying — e.g. by
an agent over the Supabase MCP — and holds every order of every product, not only what SnapSTL makes:

* `shop.report_lines`  the report row as uploaded (all columns but the two transaction IDs), merged
  across uploads: a newer file (status_at) wins key by key, an older one or a blank never erases.
  Written by `shop.put_line()` — by SnapSTL on upload (sync._push_lines, all SKUs, since 0.22.17) and
  by triggers on snapstl.order_records / report_rows (`shop.bridge`, Magicians_35, any version).
* A trigger turns each line into typed, tidy rows: `shop.orders`, `shop.customers`, `shop.order_finance`,
  `shop.shipments`, `shop.products`, `shop.status_history`. The tidying runs in the database, so it can
  be improved without a new SnapSTL.
* Views: `shop.production` / `shop.batches` / `shop.uploads` show SnapSTL's own records live;
  `daily_sales`, `production_queue`, `cod_outstanding`, `courier_performance`, `rto_by_state` answer
  common questions; `shop.glossary` explains every code.

Nothing in `shop` may block SnapSTL: the triggers catch their own errors and only warn. Same lock-down
as `snapstl`: row level security on, Supabase's API roles get nothing.
"""
from __future__ import annotations

from . import report

S = "shop"
DROPPED = ("Online Transaction ID", "Refund Transaction ID")  # payment identifiers: left in the xlsx

# (term, kind, meaning) — shop.glossary
GLOSSARY = [
    ("print", "status", "Order placed and waiting to be made / packed (webstore status PRINT)"),
    ("pack", "status", "Being packed (PACK)"),
    ("handover", "status", "Handed over to the courier, not yet moving (HANDOVER) — grouped as in_transit"),
    ("in_transit", "status", "Handed to the courier, on its way (IN-TRANSIT)"),
    ("delivered", "status", "Delivered to the customer (DELIVERED)"),
    ("cancelled_initiated", "status", "Cancellation started (CANCELLED INITIATED)"),
    ("rto_initiated", "status", "Return to origin started — the customer didn't take it (RTO_INITIATED)"),
    ("rto_acknowledged", "status", "Return to origin acknowledged by the courier (RTO_ACKNOWLEDGED)"),
    ("rto_delivered", "status", "Parcel came back to us (RTO_DELIVERED)"),
    ("awb_expired", "status", "The courier label expired before pickup (AWB EXPIRED)"),
    ("processing", "status_group", "print or pack: not shipped yet"),
    ("in_transit", "status_group", "With the courier: handover, in_transit (also picked_up, shipped, out_for_delivery)"),
    ("delivered", "status_group", "Delivered"),
    ("cancelled", "status_group", "Cancelled"),
    ("rto", "status_group", "Returned to origin, or on its way back"),
    ("other", "status_group", "Any other status (e.g. awb_expired)"),
    ("online", "payment_method", "Paid online in full"),
    ("cod", "payment_method", "Cash on delivery"),
    ("partial_cod", "payment_method", "Part paid online, the rest cash on delivery"),
    ("prepaid", "payment_method", "Prepaid"),
    ("to_make", "tab", "SnapSTL (New orders – to make): webstore PRINT, ready to be taken into a batch"),
    ("check_name", "tab", "SnapSTL (Check name first): a person must check the name first (see review_reason)"),
    ("ready_to_print", "tab", "SnapSTL (Ready to print): in a batch, no plate of it started yet (made_on)"),
    ("printing", "tab", "SnapSTL (Printing): a plate holding it was started (print_started_at); print_done_at set = printed, waiting for a person's check (Print → Verify)"),
    ("pack_it", "tab", "SnapSTL (Printed – pack it): base and name printed and checked good (checked_at, checked_by), no label yet"),
    ("ship_it", "tab", "SnapSTL (Label done – ship it): Label Generated (label_at), or webstore PACK / HANDOVER"),
    ("on_the_way", "tab", "SnapSTL (On the way): webstore IN-TRANSIT"),
    ("delivered", "tab", "SnapSTL (Delivered): webstore DELIVERED"),
    ("rto", "tab", "SnapSTL (Coming back): webstore RTO_*"),
    ("cancelled", "tab", "SnapSTL (Cancelled): webstore CANCELLED*"),
    ("problems", "tab", "SnapSTL (Problems – check): has a flag (see flag, flag_note) or a status SnapSTL doesn't know"),
    ("skipped", "tab", "SnapSTL (Skipped): skipped by a person"),
    # tabs before 0.23 (kept so old answers still make sense; no order has them any more)
    ("ready", "tab", "Before SnapSTL 0.23: now to_make"),
    ("needs_review", "tab", "Before SnapSTL 0.23: now check_name"),
    ("in_batch", "tab", "Before SnapSTL 0.23: now ready_to_print"),
    ("bundled", "tab", "Before SnapSTL 0.23: now ready_to_print or printing"),
    ("ready_to_ship", "tab", "Before SnapSTL 0.23: now pack_it"),
    ("printed", "tab", "Before SnapSTL 0.23: now pack_it"),
    ("delivered / rto / in_transit / cancelled / others", "tab",
     "Before SnapSTL 0.23: now delivered / rto / on_the_way / cancelled / problems or ship_it"),
    ("past_print_not_made", "flag", "Website moved it past PRINT (PACK …) but SnapSTL hasn't printed it"),
    ("cancelled_in_production", "flag", "Cancelled while being made or after it was printed: don't pack"),
    ("cancel_request", "flag", "Customer asked to cancel (logistics page), before the website shows it"),
    ("back_to_print", "flag", "Back to PRINT / AWB EXPIRED after it was packed outside SnapSTL: check whether it is made"),
    ("unknown_status", "flag", "A webstore status SnapSTL doesn't know"),
    ("single", "ready_type", "Ready order on its own"),
    ("clubbed", "ready_type", "Ready order shipped together with others (same clubbed_id)"),
    ("adhoc", "ready_type", "Ready order typed in by hand in SnapSTL (sku Adhoc)"),
    ("Magicians_35", "sku", "The name organizer SnapSTL makes; the only product in shop.production"),
    ("status_at", "column", "How recent a row is: @newest entry in its report file | event date | upload time. A newer file wins."),
    ("orders_full", "table", "snapstl.orders_full: SnapSTL's order with its report columns (Magicians_35 only)"),
    ("shop.orders", "table", "Every webstore order of every product, one row per order_id. Join customers, order_finance, shipments on order_id / customer_id."),
]


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _bridge_sql() -> str:
    """shop.bridge(order_id): an order as the current SnapSTL keeps it (order_records + report_rows), in
    report header names, handed to put_line — so shop fills even from SnapSTLs before 0.22.17."""
    rep = [f"{_q(h)}, r.{c}::text" for h, c, _, _ in report.COLUMNS if c != "address_line"]
    half = len(rep) // 2
    parts = ", ".join(rep[:half]), ", ".join(rep[half:])
    return f"""
CREATE OR REPLACE FUNCTION {S}.bridge(p_id text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE o record; r record; d jsonb;
BEGIN
  SELECT * INTO o FROM snapstl.order_records WHERE order_id = p_id;
  IF NOT FOUND THEN RETURN; END IF;
  SELECT * INTO r FROM snapstl.report_rows WHERE order_id = p_id;
  d := jsonb_build_object('Order ID', o.order_id, 'Clubbed Order ID', o.clubbed_id,
         'Order Creation Date', o.order_date, 'Order Status', o.source_status, 'SKU Code', o.sku,
         'Quantity', o.quantity::text, 'Customer Name', o.customer_name, 'Mobile', o.phone,
         'Address', coalesce(r.address_line, o.address),
         'Customisation Applied', CASE WHEN coalesce(o.raw_name, '') <> '' THEN 'true' END,
         'Product Customisation', nullif(concat_ws(E'\\n',
             CASE WHEN coalesce(o.raw_name, '') <> '' THEN 'Enter Your First Name: ' || o.raw_name END,
             CASE WHEN coalesce(o.raw_color, '') <> '' THEN 'Color: ' || o.raw_color END,
             CASE WHEN coalesce(o.special_request, '') <> '' THEN 'Special Requests: ' || o.special_request END), ''));
  IF r.order_id IS NOT NULL THEN
    d := d || jsonb_build_object({parts[0]}) || jsonb_build_object({parts[1]});
  END IF;
  PERFORM {S}.put_line(p_id, d, greatest(coalesce(o.status_at, ''), coalesce(r.status_at, '')),
                       coalesce(r.report_file, 'shared records'), o.updated_by);
END $$;
CREATE OR REPLACE FUNCTION {S}.bridge_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM {S}.bridge(NEW.order_id);
  RETURN NULL;
EXCEPTION WHEN others THEN
  RAISE WARNING 'shop.bridge(%) failed: %', NEW.order_id, SQLERRM;  -- never block SnapSTL
  RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER shop_bridge AFTER INSERT OR UPDATE ON snapstl.order_records
    FOR EACH ROW EXECUTE FUNCTION {S}.bridge_trigger();
CREATE OR REPLACE TRIGGER shop_bridge AFTER INSERT OR UPDATE ON snapstl.report_rows
    FOR EACH ROW EXECUTE FUNCTION {S}.bridge_trigger();
"""


def setup_sql() -> str:
    gloss = ",\n  ".join(f"({_q(t)}, {_q(k)}, {_q(m)})" for t, k, m in GLOSSARY)
    drop = ", ".join(_q(x) for x in DROPPED)
    return f"""
-- ======================= shop: the whole report, for questions (shop.py) =======================
CREATE SCHEMA IF NOT EXISTS {S};
COMMENT ON SCHEMA {S} IS 'Every webstore order (all products) from the uploaded reports, organised for reading and querying, plus live views of SnapSTL''s own records. Written only by SnapSTL; read-only for everyone else. Start with shop.glossary and shop.orders.';

-- --- tidy-up helpers ---
CREATE OR REPLACE FUNCTION {S}.txt(t text) RETURNS text LANGUAGE sql IMMUTABLE AS
  $$ SELECT nullif(nullif(btrim(t), ''), '-') $$;
CREATE OR REPLACE FUNCTION {S}.num(t text) RETURNS numeric LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v text := regexp_replace(coalesce(t, ''), '[₹,[:space:]]', '', 'g');
BEGIN
  IF v ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN RETURN v::numeric; END IF;
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION {S}.ts(t text) RETURNS timestamp LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE v text := btrim(coalesce(t, '')); m text[];
BEGIN
  IF v = '' OR v = '-' THEN RETURN NULL; END IF;
  m := regexp_match(v, '^([0-9]{{4}})-([0-9]{{2}})-([0-9]{{2}})(?:[T ]([0-9]{{1,2}}):([0-9]{{2}})(?::([0-9]{{2}}))?)?');
  IF m IS NULL THEN
    m := regexp_match(v, '^([0-9]{{1,2}})[-/]([0-9]{{1,2}})[-/]([0-9]{{4}})(?:[ T]([0-9]{{1,2}}):([0-9]{{2}})(?::([0-9]{{2}}))?)?');
    IF m IS NULL THEN RETURN NULL; END IF;
    m := ARRAY[m[3], m[2], m[1], m[4], m[5], m[6]];
  END IF;
  RETURN make_timestamp(m[1]::int, m[2]::int, m[3]::int, coalesce(m[4], '0')::int, coalesce(m[5], '0')::int,
                        coalesce(m[6], '0')::double precision);
EXCEPTION WHEN others THEN RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION {S}.code(t text) RETURNS text LANGUAGE sql IMMUTABLE AS
  $$ SELECT nullif(lower(regexp_replace(btrim(coalesce(t, '')), '[[:space:]-]+', '_', 'g')), '') $$;
CREATE OR REPLACE FUNCTION {S}.status_group(c text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN c IS NULL THEN NULL WHEN c IN ('print', 'pack') THEN 'processing'
              WHEN c IN ('in_transit', 'handover', 'picked_up', 'shipped', 'dispatched', 'out_for_delivery') THEN 'in_transit'
              WHEN c = 'delivered' THEN 'delivered' WHEN c LIKE 'cancel%' THEN 'cancelled' WHEN c LIKE 'rto%' THEN 'rto'
              ELSE 'other' END $$;
CREATE OR REPLACE FUNCTION {S}.customisation(t text) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
  SELECT jsonb_object_agg(lower(btrim(split_part(l, ':', 1))), btrim(substr(l, strpos(l, ':') + 1)))
    FROM regexp_split_to_table(coalesce(t, ''), E'\\r?\\n') AS l WHERE strpos(l, ':') > 1 $$;

-- --- the report as uploaded, merged across uploads ---
CREATE TABLE IF NOT EXISTS {S}.report_lines (
    order_id     text PRIMARY KEY,
    data         jsonb NOT NULL,
    status_at    text NOT NULL DEFAULT '',
    report_file  text,
    uploaded_by  text,
    updated_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE {S}.report_lines IS 'The report row of every order (all products) as uploaded, keyed by its column headers; merged across uploads (newer file wins, blanks never erase). The tidy tables below are made from it.';

CREATE TABLE IF NOT EXISTS {S}.products (
    sku              text PRIMARY KEY,
    product_code     text,
    family           text,
    made_by_snapstl  boolean NOT NULL DEFAULT false,
    first_seen       date,
    notes            text
);
COMMENT ON TABLE {S}.products IS 'Every product (SKU) seen in a report. made_by_snapstl = the name organizer SnapSTL makes (Magicians_35). notes is free for people.';

CREATE TABLE IF NOT EXISTS {S}.customers (
    customer_id       text PRIMARY KEY,
    name              text,
    mobile            text,
    address_line      text,
    city              text,
    state             text,
    pincode           text,
    first_order_date  date,
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE {S}.customers IS 'One row per customer, by mobile number (last 10 digits); customer_id = md5 of it. Name and address from their newest order.';

CREATE TABLE IF NOT EXISTS {S}.orders (
    order_id              text PRIMARY KEY,
    clubbed_id            text,
    order_date            date,
    channel               text,
    sku                   text,
    product_code          text,
    quantity              integer,
    status                text,
    status_group          text,
    payment_method        text,
    is_cod                boolean,
    customer_id           text,
    customised            boolean,
    customisation         jsonb,
    name_on_product       text,
    colour                text,
    return_condition      text,
    customer_invoice_id   text,
    status_at             text,
    report_file           text,
    updated_at            timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS shop_orders_date ON {S}.orders (order_date);
CREATE INDEX IF NOT EXISTS shop_orders_sku ON {S}.orders (sku);
CREATE INDEX IF NOT EXISTS shop_orders_status ON {S}.orders (status_group, status);
CREATE INDEX IF NOT EXISTS shop_orders_customer ON {S}.orders (customer_id);
COMMENT ON TABLE {S}.orders IS 'Every webstore order of every product (one row per order_id), from the newest report. Join customers (customer_id), order_finance and shipments (order_id), products (sku); SnapSTL''s making state is in shop.production.';
COMMENT ON COLUMN {S}.orders.status IS 'Webstore status, tidied: print | pack | handover | in_transit | delivered | cancelled_initiated | rto_initiated | rto_acknowledged | rto_delivered | awb_expired … (see shop.glossary).';
COMMENT ON COLUMN {S}.orders.status_group IS 'processing | in_transit | delivered | cancelled | rto | other.';
COMMENT ON COLUMN {S}.orders.payment_method IS 'online | cod | partial_cod | prepaid.';
COMMENT ON COLUMN {S}.orders.customisation IS 'What the customer typed, by field (e.g. "enter your first name", "color", "number of compartments", "special requests").';
COMMENT ON COLUMN {S}.orders.name_on_product IS 'customisation "enter your first name" (as typed; SnapSTL''s cleaned name is shop.production.name_to_print).';
COMMENT ON COLUMN {S}.orders.clubbed_id IS 'Orders with the same clubbed_id ship together.';

CREATE TABLE IF NOT EXISTS {S}.order_finance (
    order_id                  text PRIMARY KEY,
    product_amount_incl_tax   numeric,
    product_amount_excl_tax   numeric,
    product_tax_pct           numeric,
    product_tax_amount        numeric,
    cod_charges_incl_tax      numeric,
    cod_charges_excl_tax      numeric,
    prepaid_discount          numeric,
    coupon_code               text,
    coupon_type               text,
    coupon_discount           numeric,
    total_amount_excl_tax     numeric,
    total_tax_amount          numeric,
    invoice_total             numeric,
    shopdeck_service_fees     numeric,
    cancellation_rto_revenue  numeric,
    cod_remittance_status     text,
    cod_remittance_date       date,
    cod_payable_amount        numeric,
    remittance_id             text
);
COMMENT ON TABLE {S}.order_finance IS 'Money per order in ₹ (one row per order_id): amounts with/without tax, discounts, invoice total (what the customer pays), Shopdeck fee, COD remittance (pending / remitted).';

CREATE TABLE IF NOT EXISTS {S}.shipments (
    order_id                 text PRIMARY KEY,
    courier                  text,
    mode                     text,
    shipping_partner         text,
    awb                      text,
    weight_kg                numeric,
    length_cm                numeric,
    width_cm                 numeric,
    height_cm                numeric,
    initial_weight_slab      numeric,
    final_weight_slab        numeric,
    pickup_address           text,
    picked_up_at             timestamp,
    delivered_at             timestamp,
    rto_initiated_at         timestamp,
    rto_delivered_at         timestamp,
    return_created_at        timestamp,
    return_delivered_at      timestamp,
    days_to_deliver          integer,
    initial_shipping_charge  numeric,
    weight_delta             numeric,
    rto_delta                numeric,
    clubbing_delta           numeric,
    final_shipping_charge    numeric
);
COMMENT ON TABLE {S}.shipments IS 'Courier details per order (one row per order_id): courier and mode (air / surface), AWB tracking number, parcel weight and size, pickup / delivery / RTO / return times, days from order to delivery, shipping charges in ₹.';

CREATE TABLE IF NOT EXISTS {S}.status_history (
    id            bigserial PRIMARY KEY,
    order_id      text NOT NULL,
    status        text,
    status_group  text,
    status_at     text,
    report_file   text,
    seen_at       timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS shop_status_history_order ON {S}.status_history (order_id, seen_at);
COMMENT ON TABLE {S}.status_history IS 'Each webstore status an order was seen with, in order (one row per change between uploads).';

CREATE TABLE IF NOT EXISTS {S}.glossary (term text, kind text, meaning text, PRIMARY KEY (term, kind));
COMMENT ON TABLE {S}.glossary IS 'What every code in shop and snapstl means (statuses, groups, payment methods, SnapSTL tabs, tables).';
INSERT INTO {S}.glossary(term, kind, meaning) VALUES
  {gloss}
ON CONFLICT (term, kind) DO UPDATE SET meaning = EXCLUDED.meaning;

-- --- one line → tidy rows ---
CREATE OR REPLACE FUNCTION {S}.put_line(p_id text, p_data jsonb, p_stamp text, p_file text, p_by text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE clean jsonb;
BEGIN
  SELECT coalesce(jsonb_object_agg(k, btrim(v)), '{{}}') INTO clean FROM jsonb_each_text(coalesce(p_data, '{{}}')) AS e(k, v)
   WHERE {S}.txt(v) IS NOT NULL AND k NOT IN ({drop});
  INSERT INTO {S}.report_lines AS l (order_id, data, status_at, report_file, uploaded_by)
       VALUES (p_id, clean, coalesce(p_stamp, ''), p_file, p_by)
  ON CONFLICT (order_id) DO UPDATE SET
       data = CASE WHEN EXCLUDED.status_at >= l.status_at THEN l.data || EXCLUDED.data ELSE EXCLUDED.data || l.data END,
       report_file = CASE WHEN EXCLUDED.status_at >= l.status_at THEN EXCLUDED.report_file ELSE l.report_file END,
       uploaded_by = EXCLUDED.uploaded_by, status_at = greatest(l.status_at, EXCLUDED.status_at),
       updated_at = clock_timestamp()
  WHERE (CASE WHEN EXCLUDED.status_at >= l.status_at THEN l.data || EXCLUDED.data ELSE EXCLUDED.data || l.data END)
        IS DISTINCT FROM l.data OR EXCLUDED.status_at > l.status_at;
END $$;

CREATE OR REPLACE FUNCTION {S}.apply_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d jsonb := NEW.data; oid text := NEW.order_id; st text; prev text; mob text; cid text; cus jsonb;
        partner text; dims text[]; odate date; deliv timestamp;
BEGIN
  st := {S}.code(d->>'Order Status');
  odate := {S}.ts(d->>'Order Creation Date')::date;
  mob := right(regexp_replace(coalesce(d->>'Mobile', ''), '[^0-9]', '', 'g'), 10);
  cid := CASE WHEN mob <> '' THEN md5(mob) END;
  cus := {S}.customisation(d->>'Product Customisation');
  IF cid IS NOT NULL THEN
    INSERT INTO {S}.customers AS c (customer_id, name, mobile, address_line, city, state, pincode, first_order_date)
    VALUES (cid, {S}.txt(d->>'Customer Name'), mob, {S}.txt(d->>'Address'), {S}.txt(d->>'City'), {S}.txt(d->>'State'),
            {S}.txt(d->>'Pincode'), odate)
    ON CONFLICT (customer_id) DO UPDATE SET
      name = coalesce(EXCLUDED.name, c.name), address_line = coalesce(EXCLUDED.address_line, c.address_line),
      city = coalesce(EXCLUDED.city, c.city), state = coalesce(EXCLUDED.state, c.state),
      pincode = coalesce(EXCLUDED.pincode, c.pincode),
      first_order_date = least(c.first_order_date, EXCLUDED.first_order_date), updated_at = clock_timestamp();
  END IF;
  IF {S}.txt(d->>'SKU Code') IS NOT NULL THEN
    INSERT INTO {S}.products AS p (sku, product_code, family, made_by_snapstl, first_seen)
    VALUES (d->>'SKU Code', {S}.txt(d->>'Product Code'), split_part(d->>'SKU Code', '_', 1),
            d->>'SKU Code' = 'Magicians_35', odate)
    ON CONFLICT (sku) DO UPDATE SET product_code = coalesce(EXCLUDED.product_code, p.product_code),
      first_seen = least(p.first_seen, EXCLUDED.first_seen);
  END IF;
  SELECT status INTO prev FROM {S}.orders WHERE order_id = oid;
  INSERT INTO {S}.orders AS o (order_id, clubbed_id, order_date, channel, sku, product_code, quantity, status, status_group,
       payment_method, is_cod, customer_id, customised, customisation, name_on_product, colour, return_condition,
       customer_invoice_id, status_at, report_file, updated_at)
  VALUES (oid, {S}.txt(d->>'Clubbed Order ID'), odate, {S}.code(d->>'Channel'), {S}.txt(d->>'SKU Code'),
       {S}.txt(d->>'Product Code'), {S}.num(d->>'Quantity')::integer, st, {S}.status_group(st),
       {S}.code(d->>'Payment Method'), {S}.code(d->>'Payment Method') IN ('cod', 'partial_cod'), cid,
       lower(coalesce(d->>'Customisation Applied', '')) IN ('true', 'yes', '1') OR cus IS NOT NULL, cus,
       cus->>'enter your first name', lower(cus->>'color'), {S}.txt(d->>'Return Condition'),
       {S}.txt(d->>'Customer Invoice ID'), NEW.status_at, NEW.report_file, clock_timestamp())
  ON CONFLICT (order_id) DO UPDATE SET clubbed_id = EXCLUDED.clubbed_id, order_date = EXCLUDED.order_date,
       channel = EXCLUDED.channel, sku = EXCLUDED.sku, product_code = EXCLUDED.product_code, quantity = EXCLUDED.quantity,
       status = EXCLUDED.status, status_group = EXCLUDED.status_group, payment_method = EXCLUDED.payment_method,
       is_cod = EXCLUDED.is_cod, customer_id = EXCLUDED.customer_id, customised = EXCLUDED.customised,
       customisation = EXCLUDED.customisation, name_on_product = EXCLUDED.name_on_product, colour = EXCLUDED.colour,
       return_condition = EXCLUDED.return_condition, customer_invoice_id = EXCLUDED.customer_invoice_id,
       status_at = EXCLUDED.status_at, report_file = EXCLUDED.report_file, updated_at = clock_timestamp();
  IF st IS NOT NULL AND st IS DISTINCT FROM prev THEN
    INSERT INTO {S}.status_history(order_id, status, status_group, status_at, report_file)
    VALUES (oid, st, {S}.status_group(st), NEW.status_at, NEW.report_file);
  END IF;
  INSERT INTO {S}.order_finance AS f VALUES (oid,
       {S}.num(d->>'Product Amount (Incl. Tax)'), {S}.num(d->>'Product Amount (Excl. Tax)'), {S}.num(d->>'Product Tax (%)'),
       {S}.num(d->>'Product Tax Amount'), {S}.num(d->>'COD Charges (Incl. Tax)'), {S}.num(d->>'COD Charges (Excl. Tax)'),
       {S}.num(d->>'Prepaid Discount'), {S}.txt(d->>'Coupon Code'), {S}.code(d->>'Coupon Type'), {S}.num(d->>'Coupon Discount'),
       {S}.num(d->>'Total Amount (Excl. Tax)'), {S}.num(d->>'Total Tax Amount'), {S}.num(d->>'Invoice Total (Incl. Tax)'),
       {S}.num(d->>'Shopdeck Service Fees'), {S}.num(d->>'Cancellation/RTO Revenue'), {S}.code(d->>'COD Remittance Status'),
       {S}.ts(d->>'COD Remittance Date')::date, {S}.num(d->>'COD Payable Amount'), {S}.txt(d->>'Remittance ID'))
  ON CONFLICT (order_id) DO UPDATE SET product_amount_incl_tax = EXCLUDED.product_amount_incl_tax,
       product_amount_excl_tax = EXCLUDED.product_amount_excl_tax, product_tax_pct = EXCLUDED.product_tax_pct,
       product_tax_amount = EXCLUDED.product_tax_amount, cod_charges_incl_tax = EXCLUDED.cod_charges_incl_tax,
       cod_charges_excl_tax = EXCLUDED.cod_charges_excl_tax, prepaid_discount = EXCLUDED.prepaid_discount,
       coupon_code = EXCLUDED.coupon_code, coupon_type = EXCLUDED.coupon_type, coupon_discount = EXCLUDED.coupon_discount,
       total_amount_excl_tax = EXCLUDED.total_amount_excl_tax, total_tax_amount = EXCLUDED.total_tax_amount,
       invoice_total = EXCLUDED.invoice_total, shopdeck_service_fees = EXCLUDED.shopdeck_service_fees,
       cancellation_rto_revenue = EXCLUDED.cancellation_rto_revenue, cod_remittance_status = EXCLUDED.cod_remittance_status,
       cod_remittance_date = EXCLUDED.cod_remittance_date, cod_payable_amount = EXCLUDED.cod_payable_amount,
       remittance_id = EXCLUDED.remittance_id;
  partner := {S}.txt(d->>'Shipping Partner');
  dims := regexp_match(coalesce(d->>'Dimensions (cm)', ''), '([0-9.]+)[^0-9.]+([0-9.]+)[^0-9.]+([0-9.]+)');
  deliv := {S}.ts(d->>'Delivered Date');
  INSERT INTO {S}.shipments AS s VALUES (oid,
       nullif(btrim(regexp_replace(coalesce(partner, ''), '([[:space:]]+(air|surface))+$', '', 'i')), ''),
       CASE WHEN partner ~* 'air$' THEN 'air' WHEN partner ~* 'surface$' THEN 'surface' END, partner,
       {S}.txt(d->>'AWB'), {S}.num(d->>'Weight (kg)'), {S}.num(dims[1]), {S}.num(dims[2]), {S}.num(dims[3]),
       {S}.num(d->>'Initial Weight Slab'), {S}.num(d->>'Final Weight Slab'), {S}.txt(d->>'Pickup Address'),
       {S}.ts(d->>'Pickup Date'), deliv, {S}.ts(d->>'RTO Initiated Date'), {S}.ts(d->>'RTO Delivered Date'),
       {S}.ts(d->>'Return Created Date'), {S}.ts(d->>'Return Delivered Date'), deliv::date - odate,
       {S}.num(d->>'Initial Shipping Charge'), {S}.num(d->>'Weight Delta'), {S}.num(d->>'RTO Delta'),
       {S}.num(d->>'Clubbing Delta'), {S}.num(d->>'Final Shipping Charge'))
  ON CONFLICT (order_id) DO UPDATE SET courier = EXCLUDED.courier, mode = EXCLUDED.mode,
       shipping_partner = EXCLUDED.shipping_partner, awb = EXCLUDED.awb, weight_kg = EXCLUDED.weight_kg,
       length_cm = EXCLUDED.length_cm, width_cm = EXCLUDED.width_cm, height_cm = EXCLUDED.height_cm,
       initial_weight_slab = EXCLUDED.initial_weight_slab, final_weight_slab = EXCLUDED.final_weight_slab,
       pickup_address = EXCLUDED.pickup_address, picked_up_at = EXCLUDED.picked_up_at, delivered_at = EXCLUDED.delivered_at,
       rto_initiated_at = EXCLUDED.rto_initiated_at, rto_delivered_at = EXCLUDED.rto_delivered_at,
       return_created_at = EXCLUDED.return_created_at, return_delivered_at = EXCLUDED.return_delivered_at,
       days_to_deliver = EXCLUDED.days_to_deliver, initial_shipping_charge = EXCLUDED.initial_shipping_charge,
       weight_delta = EXCLUDED.weight_delta, rto_delta = EXCLUDED.rto_delta, clubbing_delta = EXCLUDED.clubbing_delta,
       final_shipping_charge = EXCLUDED.final_shipping_charge;
  RETURN NULL;
EXCEPTION WHEN others THEN
  RAISE WARNING 'shop.apply_line(%) failed: %', oid, SQLERRM;  -- the line is kept; tidy rows catch up next time
  RETURN NULL;
END $$;
-- rows tidied before a grouping rule changed follow it (cheap: only rows whose group differs)
UPDATE {S}.orders SET status_group = {S}.status_group(status) WHERE status_group IS DISTINCT FROM {S}.status_group(status);
UPDATE {S}.status_history SET status_group = {S}.status_group(status) WHERE status_group IS DISTINCT FROM {S}.status_group(status);
CREATE OR REPLACE TRIGGER report_lines_apply AFTER INSERT OR UPDATE ON {S}.report_lines
    FOR EACH ROW EXECUTE FUNCTION {S}.apply_line();
{_bridge_sql()}
-- --- SnapSTL's own records, live ---
CREATE OR REPLACE VIEW {S}.production WITH (security_invoker = true) AS
  SELECT o.order_id, o.tab, o.ready_type, o.review_reason, o.name AS name_to_print, o.raw_name, o.color AS colour,
         o.special_request, o.state AS making_state, o.computer_name AS made_on, o.batch_label,
         to_timestamp(o.bundled_at) AS bundled_at, to_timestamp(o.printed_at) AS printed_at,
         to_timestamp(o.made_at) AS ready_to_ship_at, o.source_status, o.clubbed_id, o.sku, o.quantity, o.updated_at
    FROM snapstl.order_records o;
COMMENT ON VIEW {S}.production IS 'SnapSTL''s Magicians_35 orders exactly as the app shows them: Orders page tab, ready_type, review_reason, name to print, colour, which computer is making it, batch, bundled / printed / ready-to-ship times.';
CREATE OR REPLACE VIEW {S}.batches WITH (security_invoker = true) AS
  SELECT uid, computer_name, label, status, to_timestamp(created_at) AS created_at, to_timestamp(finished_at) AS finished_at,
         orders, plates, folder FROM snapstl.batch_records;
COMMENT ON VIEW {S}.batches IS 'Every SnapSTL batch on every computer.';
CREATE OR REPLACE VIEW {S}.uploads WITH (security_invoker = true) AS
  SELECT filename, computer_name, uploaded_at, as_of, report_date, rows_total, magicians_rows, new_orders, updated_orders
    FROM snapstl.report_uploads;
COMMENT ON VIEW {S}.uploads IS 'Every report uploaded on any computer: when, by which, and how recent the file was (as_of).';

-- --- ready-made answers ---
CREATE OR REPLACE VIEW {S}.daily_sales WITH (security_invoker = true) AS
  SELECT o.order_date, o.sku, count(*) AS orders, sum(o.quantity) AS units, sum(f.invoice_total) AS revenue,
         count(*) FILTER (WHERE o.is_cod) AS cod_orders, count(*) FILTER (WHERE o.status_group = 'cancelled') AS cancelled,
         count(*) FILTER (WHERE o.status_group = 'rto') AS rto
    FROM {S}.orders o LEFT JOIN {S}.order_finance f USING (order_id) GROUP BY 1, 2;
COMMENT ON VIEW {S}.daily_sales IS 'Per order date and SKU: orders, units, revenue (invoice total, ₹), COD orders, cancelled, RTO.';
CREATE OR REPLACE VIEW {S}.production_queue WITH (security_invoker = true) AS
  SELECT tab, ready_type, count(*) AS orders FROM snapstl.order_records GROUP BY 1, 2;
COMMENT ON VIEW {S}.production_queue IS 'How many Magicians_35 orders are in each SnapSTL Orders page tab right now.';
CREATE OR REPLACE VIEW {S}.cod_outstanding WITH (security_invoker = true) AS
  SELECT o.order_id, o.order_date, o.sku, o.status, f.cod_payable_amount, f.cod_remittance_status, s.delivered_at
    FROM {S}.orders o JOIN {S}.order_finance f USING (order_id) LEFT JOIN {S}.shipments s USING (order_id)
   WHERE o.is_cod AND o.status = 'delivered' AND coalesce(f.cod_remittance_status, 'pending') <> 'remitted';
COMMENT ON VIEW {S}.cod_outstanding IS 'Delivered COD orders whose money hasn''t been paid out yet.';
CREATE OR REPLACE VIEW {S}.courier_performance WITH (security_invoker = true) AS
  SELECT s.courier, s.mode, count(*) AS shipments, count(*) FILTER (WHERE o.status = 'delivered') AS delivered,
         count(*) FILTER (WHERE o.status_group = 'rto') AS rto,
         round(100.0 * count(*) FILTER (WHERE o.status_group = 'rto') / nullif(count(*), 0), 1) AS rto_pct,
         round(avg(s.days_to_deliver), 1) AS avg_days_to_deliver
    FROM {S}.shipments s JOIN {S}.orders o USING (order_id) WHERE s.courier IS NOT NULL GROUP BY 1, 2;
COMMENT ON VIEW {S}.courier_performance IS 'Per courier and mode: shipments, delivered, RTO count and %, average days from order to delivery.';
CREATE OR REPLACE VIEW {S}.rto_by_state WITH (security_invoker = true) AS
  SELECT c.state, count(*) AS orders, count(*) FILTER (WHERE o.status_group = 'rto') AS rto,
         round(100.0 * count(*) FILTER (WHERE o.status_group = 'rto') / nullif(count(*), 0), 1) AS rto_pct
    FROM {S}.orders o JOIN {S}.customers c USING (customer_id) GROUP BY 1;
COMMENT ON VIEW {S}.rto_by_state IS 'Per delivery state: orders, RTO count and %.';

-- --- lock-down: only SnapSTL (the owner) and whoever has the database password ---
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['report_lines', 'products', 'customers', 'orders', 'order_finance', 'shipments',
                           'status_history', 'glossary'] LOOP
    EXECUTE format('ALTER TABLE {S}.%I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA {S} FROM anon, authenticated;
    REVOKE ALL ON ALL TABLES IN SCHEMA {S} FROM anon, authenticated;
  END IF;
END $$;
"""


def backfill_sql() -> str:
    """Fill shop from what SnapSTL already keeps (direct one-off, or after a reset)."""
    return f"SELECT count({S}.bridge(order_id)) FROM snapstl.order_records;"


# ---------------- on this computer ----------------
def store_line(con, order_id: str, row: dict, stamp: str, filename: str) -> None:
    """Keep an uploaded report row (any product) here until the sync sends it to shop.report_lines.
    Newer file wins key by key; a blank never erases (the same rule as shop.put_line)."""
    import json

    data = {k: v.strip() for k, v in row.items() if k and k not in DROPPED and (v or "").strip() not in ("", "-")}
    cur = con.execute("SELECT data, status_at FROM shop_lines WHERE order_id=?", (order_id,)).fetchone()
    if cur:
        old = json.loads(cur[0] or "{}")
        newer = stamp >= (cur[1] or "")
        merged = {**old, **data} if newer else {**data, **old}
        if merged == old and stamp <= (cur[1] or ""):
            return
        stamp = max(stamp, cur[1] or "")
    else:
        merged = data
    con.execute("INSERT OR REPLACE INTO shop_lines(order_id, data, status_at, file, sent) VALUES (?,?,?,?,0)",
                (order_id, json.dumps(merged, sort_keys=True), stamp, filename))


PUT_LINE = f"SELECT {S}.put_line(%(order_id)s, %(data)s::jsonb, %(status_at)s, %(file)s, %(by)s) AS ok"
