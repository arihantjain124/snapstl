# Build plan: new order flow, logistics page, Express, reuse of returned pieces (0.23.0)

Agreed with the owner on 2026-11-18 (discussion: `documentation.md`). Nothing here is built yet.
Each phase ends with passing tests and can be committed on its own. Phases 1–2 change no screen;
the owner sees the change from phase 3.

---

## Plain-language summary (for the owner)

1. **New tabs** in three groups. *Make*: New orders – to make, Check name first, Ready to print,
   Printing. *Pack & ship*: Printed – pack it, Label done – ship it. *Records*: On the way, Delivered,
   Coming back (RTO), Cancelled, Problems – check, Skipped. Bundled, Printed, In a batch, Others and
   the Mark printed button go away.
2. **Report rules**: a report never moves an order backwards; AWB EXPIRED sends a made order back to
   *Printed – pack it* for a new label; strange cases go to *Problems – check*.
3. **Logistics page** (label and customer-care team) rebuilt: Labels / Customer help / Tracking
   menus, one order panel with Share / Change / Notes, Label Generated, Express, name edits with a
   reason, tracking link, ready-made messages, WhatsApp, call notes, cancel and remake requests.
4. **Express**: needed-by date + note, sorted first and badged for production, red 3 days before.
5. **Returned pieces**: a New order with the exact same name (same letter case) and colour as an RTO
   order is highlighted; production or logistics can click *Use returned piece*.
6. **Usage counts** of every logistics feature, per day, in Settings → Usage.
7. **Every computer stays in step** through the shared records (Supabase); all computers update the
   same day.

---

## Phase 1: Data (local database + shared records)

**New order columns** (`db.MIGRATIONS` only; nothing renamed or dropped):

| Column | Meaning |
|---|---|
| `first_status` | webstore status when SnapSTL first saw the order (old history is never flagged) |
| `print_started_at` | first time a plate holding the order was started → *Printing* |
| `label_at`, `label_by` | Label Generated clicked (by "logistics") → *Label done – ship it*; cleared by Undo or AWB EXPIRED |
| `flag`, `flag_note`, `flag_at` | why it is in *Problems* (codes below); cleared by a person |
| `express_by`, `express_note`, `express_at` | Express: needed-by date and note |
| `name_change_reason`, `name_confirmed_at` | logistics name edit after confirming with the customer |
| `remake_at`, `remake_reason` | a remake request (same order ID, back to making) |
| `reuse_of` / `reused_by` | Use returned piece: new order ↔ RTO order (each piece used once) |

"Customer wants to cancel" is the flag `cancel_request` with its note. Each people-set group
(label, express, flag, name note, remake, reuse) has a `<group>_changed` time: the newest wins.

Flag codes: `past_print_not_made` (report past PRINT for an order we listed to make but never
printed), `cancelled_in_production`, `cancel_request`, `unknown_status`.

**New local tables**: `order_notes` (order, time, text, who), `usage_counts` (day, feature, computer, count).

**Shared records (Supabase)**, in `sync._SETUP_SQL` / `sync._COLS` / merge rules:
- the same columns on `order_records`; new tables `order_notes`, `usage_counts` (insert/update only,
  `refuse_delete` triggers like the rest);
- the order outbox triggers in `db.POST_SCHEMA` watch the new columns, so every change is sent;
- newest change wins per field (as today); notes are append-only;
- **minimum version** in `meta` (`min_app_version = 0.23.0`): a computer on an older SnapSTL stops
  writing and asks to update, so old tab rules can never mix with new ones;
- regenerate `docs/supabase-schema.sql`.

Tests: migrations on a copy of an old database; sync round trip of every new field with a throwaway
Postgres; old version refused by `min_app_version`.

## Phase 2: One tab rule, used everywhere

A single function `tabs.tab_of(order)` (new `tabs.py`), and the same rule in SQL for `server.TABS`
and the Supabase `set_tab()` trigger (with true `COMMENT`s). First match wins:

| # | Rule | Tab |
|---|---|---|
| 1 | Skipped by a person | Skipped |
| 2 | has a flag (incl. cancel request) | Problems – check |
| 3 | report CANCELLED* | Cancelled (flag `cancelled_in_production` instead if in a batch or made) |
| 4 | report DELIVERED | Delivered |
| 5 | report RTO_* | Coming back (RTO) |
| 6 | report IN-TRANSIT | On the way |
| 7 | Label Generated, or report PACK / HANDOVER | Label done – ship it |
| 8 | all plates printed, marked printed (old), or uses a returned piece | Printed – pack it |
| 9 | in a batch and a plate started | Printing |
| 10 | in a batch | Ready to print |
| 11 | needs a name check | Check name first |
| 12 | ready to make | New orders – to make |
| 13 | anything else | Problems – check (`unknown_status`) |

**Report rules in `ingest.py`** (on top of today's status stamps):
- a status never moves an order backwards: PRINT after PACK / label keeps the order where it is;
- **AWB EXPIRED**: made order → `label_at` cleared, note "Label expired – make a new label" →
  *Printed – pack it* (top of Labels to make); not made → stays in the Make tabs;
- a To-make / Check-name order that the report shows past PRINT (PACK … DELIVERED) without being
  printed → flag `past_print_not_made`; orders first seen already past PRINT are history, no flag;
- CANCELLED while in a batch or made → flag `cancelled_in_production` ("Cancelled – don't pack").

**Moving existing orders** (one-time, with a backup first): Bundled → Ready to print / Printing /
Printed – pack it by their plates and print jobs; Printed (by hand) → Printed – pack it;
`first_status` filled from the oldest report seen.

Tests: a table test of `tab_of` for every state × status; the Python rule and the SQL rule give the
same tab for the same rows; ingest cases (backwards, AWB EXPIRED both ways, flags, history).

## Phase 3: Production app (main SnapSTL window)

- Orders page: the 12 tabs in three groups with counts; old tabs and **Mark printed** removed.
- **Express**: badge "EXPRESS · needed by 25 Nov" (+ note on hover), red from 3 days before; express
  orders first in every Make / Pack tab (earliest date first); **Express** filter for Create batch;
  Print tab lists plates holding an express order first, with the badge. Production can see but not
  change Express.
- **Printing** starts when a person starts a plate (`farm.start_job` / Start print) → `print_started_at`.
- **Returned pieces**: in *New orders – to make*, green "Returned piece available" (RTO_DELIVERED) or
  amber "Returned piece on its way back" (RTO_INITIATED / ACKNOWLEDGED) when an RTO order has exactly
  the same printed name (same letter case) and colour and its piece isn't used yet. **Use returned
  piece** (production and logistics, asks "piece checked, not damaged?") links both orders and moves the new
  one to *Printed – pack it*; Undo puts it back.
- **Problems – check**: the reason in words, **Clear flag** (asks to confirm), and for cancel requests
  a **Skip** shortcut.
- Name changes made by logistics show their reason; remake requests show "Remake of <order>: reason".
- Going back: Undo Label Generated (logistics), Unskip, Remake, Reprint, Clear flag, Undo returned piece.
- Help page and simple wording throughout.

## Phase 4: Logistics page (port 8002)

**Access rule changes** (`logistics.py` allowlist; everything else stays refused):
reads as today, plus these writes only: Label Generated / Undo; Express set / change / clear; name and
colour edit with "Confirmed with customer" + reason (only in *New orders – to make* and *Check name
first*, never once in a batch); Checked with customer; call notes; customer wants to cancel; remake
request; Use returned piece / Undo (same highlight as production, in *Being made*); usage count. Recorded as "logistics" with the time.

**Layout** (approved):
- Top bar: one search box (order ID, phone or customer name; phone search shows all that customer's
  orders) and "Last report uploaded: <date, time>".
- Left menu: **Labels** (Labels to make, label-expired first and express first; Label done – ship
  it), **Customer help** (Check name first, Cancel requests & problems, Remake requests),
  **Tracking** (Being made, On the way, Delivered, Coming back (RTO), Cancelled).
- Columns: order ID, order date + days since, customer, phone, full address, name to print, colour,
  quantity, status in words, courier, AWB, payment, pickup / delivered dates. Red when 7 days old and
  not yet in *Label done* or later; On the way red after 5 days (stuck).
- **Copy order IDs** on Labels to make.
- Click an order → one panel:
  - **Share**: Copy tracking link (`https://shopdeck.clickpost.ai/en?waybill={awb}&cp_id=105`,
    pattern editable in Settings; "No tracking yet" without an AWB) · Copy message (ready-made replies
    filled with the order's details) · WhatsApp (opens WhatsApp with the number and message; a person
    presses Send).
  - **Change**: Label Generated / Undo · Edit name (confirm + reason) · Express (date + note) ·
    Customer wants to cancel · Remake request (reason).
  - **Notes**: call notes, newest first.
- Bigger text; works on a phone; CSV download limited to these columns.

## Phase 5: Usage counts

Every logistics screen and button sends a count (feature name only, never customer data) →
`usage_counts` per day per computer → shared records. **Settings → Usage**: a table of features by
count for the last 7 / 30 days, to reorganise the layout later.

## Phase 6: Settings and documents

- Settings: tracking link pattern; red after 7 days; Express red 3 days before; stuck after 5 days;
  ready-made message texts.
- AGENTS.md: logistics write allowlist (replaces "order reads only"), the tab rule and where it
  lives (`tabs.py` + SQL, kept identical), "never backwards", privacy note for WhatsApp links (opened
  by a person in their own browser; the app sends nothing).
- ARCHITECTURE: data model, states and tabs, API table, logistics section, decision log entry.
- README and in-app Help in simple words; `documentation.md` entry.

## Phase 7: Test and roll out

- `uv run pytest -q` plus the sync tests against a throwaway Postgres.
- Dry run on a copy of the owner's database (`SNAPSTL_HOME` dev folder) with the reference report:
  check counts per tab before/after and that no order is lost or made twice.
- Roll out on one day: backup on every computer → update the logistics host and every production
  computer (in-app updater) → upload a 30-day report → spot-check tabs on two computers.

## Risks and how they're handled

| Risk | Handling |
|---|---|
| An old SnapSTL writes old-style records | `min_app_version` in shared records; old versions stop writing |
| Report undoes staff work | never-backwards rule; only AWB EXPIRED moves a made order back |
| Cancelled order gets packed | flag "Cancelled – don't pack" in Problems; customer cancel requests flagged at once |
| Same returned piece used twice | `reused_by` set once; matching ignores used pieces |
| Name edited after plates are built | edits blocked once in a batch; use Remake |
| Logistics host off | page unreachable; Settings on the host shows listening status (as today) |
| Who did it | shared password → recorded as "logistics" + time; per-person sign-in is a later option |
