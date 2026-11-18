# SnapSTL — discussion log

A short, running record of what the owner and AI agents discussed and decided. Newest entry at the
bottom. Each entry: date, what was asked, what was found, what was decided, and what is still open.
Binding rules still live in `AGENTS.md` and `docs/ARCHITECTURE.md`; this file explains the *why* and
the open threads between sessions.

**Privacy:** this repository is public. Never write customer names, phones, addresses or order IDs
here, only counts, column names and status names.

---

## Current understanding: Order Reconciliation Report

The owner uploads the webstore's *Order Reconciliation Report* (.xlsx) via **Orders → Upload report**.
It is read by `src/snapstl/ingest.py`.

**File layout** (reference copy: `Order_Reconciliation_Report_JNEZSLf9AG.xlsx`, kept outside git):
- First sheet only. Title row, then `Date Range: DD/MM/YYYY to DD/MM/YYYY`, then the header row.
- 65 columns. SnapSTL uses: Order ID, Clubbed Order ID, Order Creation Date, Order Status, SKU Code,
  Quantity, Customer Name, Mobile, Address/City/State/Pincode, Product Customisation (name, colour,
  special request), and the event dates (Pickup, Delivered, RTO Initiated/Delivered, Return
  Created/Delivered). All other columns are stored for other tools (`report.py`, `shop.py`).
- Every product's rows go to the Supabase `shop` schema; only SKU `Magicians_35` becomes app orders.

**Webstore `Order Status` values (owner's definitions, 2026-11-18)**

| Status | Meaning |
|---|---|
| `PRINT` | Order successfully registered on the website; it **should be made**. |
| `PACK` | Shipping label printed; the parcel is ready to go. |
| `HANDOVER` | Handed over to the courier. |
| `IN-TRANSIT` | With the courier, on the way. |
| `DELIVERED` | Delivered to the customer. |
| `CANCELLED INITIATED` | Cancelled. |
| `RTO_INITIATED` / `RTO_ACKNOWLEDGED` / `RTO_DELIVERED` | Return to origin started / acknowledged / back with us. |
| `AWB EXPIRED` | The shipping label expired; a new label must be generated and the order goes back to PRINT on the website. Can follow PRINT or PACK. |

`CANCELLED INITIATED` counts as **fully cancelled**.

**How SnapSTL reads it today**
- Order ID is the de-duplication key; re-uploads update, never duplicate.
- A status only replaces a stored one if its stamp is at least as new (`@file moment|event date|upload`),
  so an older file never undoes a newer status.
- New order with `PRINT` → **Ready** (or **Needs review** for odd names / missing colour / special
  request). Any other status → **not_needed**.
- Once an order is in a batch (in_batch / bundled / printed) or skipped, the app's own state wins; the
  report only updates its `source_status` and never moves it to another tab.

**App states (what we do in the app)**: `ready`, `needs_review`, `in_batch`, `bundled`
(Ready to ship when `made_at` is set), `printed`, `skipped`, `not_needed`.

**Orders page tabs**: Ready, Needs review, In a batch, Bundled, Ready to ship, Printed, Skipped, and,
for `not_needed` orders only, split by webstore status: Delivered, RTO, In Transit, Cancelled,
**Others** (anything else, e.g. PACK, HANDOVER, AWB EXPIRED). Defined in `server.TABS` and mirrored by
the Supabase `set_tab()` trigger.

---

## Log

### 2026-11-17: How the report is read
- Asked: how does SnapSTL read the Order Reconciliation Report?
- Explained the `ingest.py` steps: first sheet, header found by the 4 required columns, Magicians_35
  filter, name/colour cleaning, PRINT → Ready / Needs review, de-duplication and status stamps.

### 2026-11-17: Why "Others" shows only 3 orders
- Found: Others = `not_needed` orders whose status isn't Delivered/RTO/In Transit/Cancelled. In the
  local database that was 2 PACK + 1 HANDOVER.
- The other PACK orders (17 bundled, 7 skipped) show in **Bundled** / **Skipped**, because the app's
  state decides the tab first. Working as designed, but it hides where batched orders are in shipping.

### 2026-11-18: Status definitions and plan to rework statuses
- Owner defined PRINT, PACK, HANDOVER (above); other statuses are self-explanatory.
- Owner will upload a fresh report **daily, covering the last 7 days** (the reference file covers
  30 days: 2026-10-18 to 2026-11-17, 5,287 rows, 1,654 Magicians_35).
  Magicians_35 statuses in it: DELIVERED 754, PRINT 683, IN-TRANSIT 152, CANCELLED INITIATED 27,
  PACK 26, RTO_INITIATED 4, RTO_DELIVERED 4, RTO_ACKNOWLEDGED 3, HANDOVER 1.
- Goal: plan changes to the order statuses tracked from the report **and** from in-app actions
  (and therefore the Orders tabs).
- Noted consequence of 7-day files: an order older than 7 days drops out of the report, so its stored
  webstore status stops updating (e.g. stays IN-TRANSIT). The plan must decide how to treat that.
- **Open**: owner to confirm the target tabs/statuses and the questions asked in this session
  (see the next entry once answered). No code changed yet.

### 2026-11-18: Answers and the new order flow (draft, not built)
- Answers: AWB EXPIRED = label expired, order back to PRINT for a new label (from PRINT or PACK).
  CANCELLED INITIATED = fully cancelled. Stale statuses: the owner will upload a **30-day** file when
  needed to refresh older orders.
- Conflicts (report says PACK/DELIVERED but we never made it; cancelled after we batched it): **flag
  them**, the team handles them by hand. These mostly come from ad-hoc / bulk orders made in the app
  and registered on the website by hand; the ad-hoc feature will be updated later to link them.
- Owner wants fewer statuses. Proposed flow for employees:
  report PRINT → **Ready** (to make) → Create batch → **Ready to print** → printing started →
  **In production** → names and bases all printed → **Ready to pack** → button **Label Generated**
  (or report says PACK) → **Ready to ship** → then the report's HANDOVER / IN-TRANSIT / DELIVERED / RTO /
  CANCELLED take over.
- Drop the **Bundled** and **Printed** tabs. Keep **Skipped** for exceptions; keep Delivered, RTO,
  In Transit, Cancelled as records for customer queries. Rename **Others** and **Ready** etc. into
  plain words for staff with little formal education.
- Packing staff search the order ID in **Ready to pack** to print the label on the website.
- **Open**: confirm PRINT (not PACK) is the "registered" status; where HANDOVER goes; tab names;
  whether Hindi labels are wanted; clubbed orders in Ready to pack; what the truncated "Needs …" was
  about (Needs review?). Agent's suggestions and risks are in the session reply; record the decisions
  here once agreed.

### 2026-11-18: Agreed brief for the new Orders tabs (approved to plan, not built yet)
Answers: "registered" = **PRINT**. HANDOVER has no tab of its own: it joins **Label done – ship it**.
Tab names as suggested. Label Generated can be undone. "Needs" was a typo (Needs review stays as
**Check name first**). Problems handled as suggested; allow going back a step wherever it makes sense.
Every computer must see the same tab for an order (shared records in Supabase).

**Tabs, in three groups** (each order is in exactly one):

| Group | Tab | An order is here when |
|---|---|---|
| Make | New orders – to make | report PRINT, name OK, not batched |
| Make | Check name first | report PRINT, name/colour needs a person |
| Make | Ready to print | in a batch, no plate of it started printing yet |
| Make | Printing | a plate holding it was started, not all its plates printed |
| Pack & ship | Printed – pack it | all its name and base plates printed, no label yet |
| Pack & ship | Label done – ship it | **Label Generated** clicked, or report PACK / HANDOVER |
| Records | On the way | report IN-TRANSIT |
| Records | Delivered | report DELIVERED |
| Records | Coming back (RTO) | report RTO_* |
| Records | Cancelled | report CANCELLED* (and nothing of it in production) |
| Records | Problems – check | a flag (below) or a status SnapSTL doesn't know |
| Records | Skipped | a person clicked Skip |

**Rules**
- A report status never moves an order backwards in the flow (e.g. PRINT after Label Generated). One
  exception: **AWB EXPIRED** on an order we've made → back to *Printed – pack it* with the note
  "label expired – make a new label"; on an order not made yet → stays in the Make tabs.
- Report PACK/HANDOVER/IN-TRANSIT/DELIVERED/RTO on a made order moves it forward to that tab.
- Flags → *Problems*: report past PRINT for an order SnapSTL listed to make but never printed;
  cancelled while in *Ready to print* / *Printing* / *Printed – pack it* ("Cancelled – don't pack").
  Orders first seen already past PRINT are history, never flagged. A person clears a flag.
- Clubbed orders: in *Printed – pack it* the group is marked "waiting for N more" until every order of
  the group is printed (one label per group).
- Going back: Undo Label Generated; Skip ↔ Unskip; Remake (back to New orders); Reprint keeps the order
  in *Printing*; a cleared flag returns the order to the tab its state says.
- *Printed – pack it* gets a **Copy order IDs** button for the website.
- Removed: tabs Bundled, Printed, In a batch, Ready to ship (old), Others; the **Mark printed** button.
  Existing orders: Bundled → Ready to print / Printing / Printed – pack it by their plates;
  Printed (by hand) → Printed – pack it.

**Build notes (for the agent)**
- Keep `orders.state` values (never rename/drop); add columns via `db.MIGRATIONS`: `print_started_at`,
  `label_at`, `flag` (+ reason). Tabs are worked out from state + these + `made_at` + `source_status`.
- Same new columns in Supabase `order_records` (`sync._SETUP_SQL`, `_COLS`, merge rules); rewrite the
  `set_tab()` trigger to the new tabs and keep its `COMMENT`s true; regenerate
  `docs/supabase-schema.sql`; run tests with a throwaway Postgres.
- Every computer must run the new version together (old versions would write old tab rules); bump
  the version and update via the in-app updater.
- Update `server.TABS`, `index.html` (`TABS`, groups, buttons, Help page), README, ARCHITECTURE,
  AGENTS §3 rows, tests.

### 2026-11-18: Logistics page for the label / customer-care team (proposal, not built)
- Today: `logistics.py` serves the full Orders page on port 8002 (password, LAN only, **read only**):
  every tab and every column (incl. scaling size, compartments, raw names), no row actions.
- Owner wants it trimmed to what the labelling and customer-care team need: print labels, monitor
  statuses, answer customer queries.
- Proposal (agent): two simple screens, **Labels to make** (= *Printed – pack it*) and **Find an
  order** (search by order ID / phone / customer name → one plain-language status line + key dates);
  tabs limited to Being made (one combined count), Printed – pack it, Label done – ship it, On the way,
  Delivered, Coming back (RTO), Cancelled, Problems; columns limited to order ID, date, customer, phone,
  city/pincode, name to print, colour, qty, status in words, days since order, and from the report
  (already stored in `report.py`): courier, AWB, payment method, pickup / delivered dates.
- Needs an owner decision because it changes a binding rule (AGENTS: logistics = order reads only):
  may the team click **Label Generated** / Undo on the logistics page?
- **Open**: that rule change; columns; whether the team sees full address; "last report uploaded"
  banner; late-order highlight threshold.

### 2026-11-18: Logistics decisions and Express delivery (agreed to plan, not built)
- **Label Generated** (and Undo) lives **only on the logistics page**: the label team uses it. This
  changes the binding rule "logistics = order reads only": the logistics page may now make exactly
  these writes: Label Generated / Undo, and Express set / change / clear. Nothing else.
- Logistics team sees the **full address**. An order shows **red 7 days after its order date** while it
  is not yet in *Label done – ship it* (or later).
- **Express delivery** (new): on the logistics page the team marks an order *Express* with the
  **date the customer needs it by** (+ optional short note). Change or remove it later.
  - Synced to every computer and Supabase (new order columns: needed-by date, note, set at).
  - Production sees it everywhere the order appears: an **EXPRESS · needed by <date>** badge, express
    orders **sorted first** in New orders / Check name first / Ready to print / Printing / Printed –
    pack it, an **Express** filter for Create batch, and the Print tab lists plates holding an
    express order first, with the badge.
  - Only for orders not yet shipped; ignored once the order is in *Label done* or later; can't be set
    on Cancelled / Skipped.
- **Open**: the owner's message had an unfinished line "Also they should have …"; how many days
  before the needed-by date an express order turns red; whether Express also needs a "how it ships"
  choice (e.g. faster courier) or only the date.

### 2026-11-18: More logistics decisions (agreed to plan, not built)
- Unfinished line: ignore. Express turns **red 3 days before** the needed-by date. Express = priority
  only (date + note), no courier choice.
- **Copy tracking link** button on the logistics page, built from the report's Shipping Partner + AWB.
  Couriers in the reference file: Blue Dart (AIR/SURFACE), Delhivery (AIR/SURFACE), Xpressbees, Amazon,
  and `-` (no courier yet). AWBs are 11–15 digits. Proposal: one link pattern per courier, editable in
  Settings, so a changed courier site needs no code change.
- Logistics team may **edit name / colour** of orders in *Check name first*, then press **Checked with
  customer**, which moves the order to *New orders – to make*. (Production keeps its own edit too.)
  Logistics writes allowed now: Label Generated / Undo, Express set / change / clear, name/colour edit +
  Checked with customer in *Check name first*.
- Reference data: Magicians_35 RTO is low (~1.5 % of finished orders); payment mix ONLINE / PARTIAL-COD
  mostly, so a COD-confirmation call step is not worth adding now.
- **Open**: tracking link source (courier sites vs the webstore's own tracking page); which extra
  logistics features from the agent's list to include.

### 2026-11-18: Tracking page, name edits, all logistics features, usage counts (agreed, not built)
- Tracking link goes to the **webstore's own tracking page**, no per-courier links. The report has no
  tracking-URL column (checked all 65 columns), so the link is built from a pattern the owner pastes in
  Settings, e.g. `https://<store>/track?awb={awb}` or `…{order_id}`. **Open**: the owner to send one
  real tracking link (from a customer SMS/email or the website) so the pattern can be set.
- Logistics team may also edit name / colour in **New orders – to make**, only after ticking
  "Confirmed with customer" and giving a **reason** (kept on the order, synced). Not allowed once the
  order is in a batch (plates may be built); then it needs Remake.
- **All 8 suggested features are in**: customer search by phone (all their orders), copy-ready reply
  messages, WhatsApp button (opens WhatsApp with text; a person presses Send), call notes, customer
  wants to cancel (→ Problems for production), remake request (→ New orders, linked to the original),
  stuck shipments (On the way > 5 days, default), label-expired list at the top of Labels to make.
- **Usage counts**: every logistics button/feature use is counted per feature per day (no customer
  data), synced to Supabase, shown in Settings → Usage, to reorganise the layout later.
- Layout: see the agreed logistics layout (next entry, once confirmed).

### 2026-11-18: Tracking pattern, layout approved, reuse of returned (RTO) items (agreed, not built)
- Webstore tracking page (ClickPost): `https://shopdeck.clickpost.ai/en?waybill={awb}&cp_id=105`.
  The owner's two examples (an 11-digit and a 15-digit AWB) both use `cp_id=105`, so it is treated as
  fixed; kept as an editable pattern in Settings. The example AWBs are not in the reference file, so
  **Open**: confirm `cp_id=105` works for Blue Dart, Delhivery, Xpressbees and Amazon AWBs alike.
- Logistics layout approved: top bar (search + "last report uploaded"); left menu in three groups:
  **Labels** (Labels to make with label-expired first, Label done – ship it), **Customer help**
  (Check name first, Cancel requests & problems, Remake requests), **Tracking** (Being made, On the way
  with stuck > 5 days red, Delivered, Coming back (RTO), Cancelled); clicking an order opens one panel
  with rows **Share** (tracking link, message, WhatsApp), **Change** (Label Generated/Undo, edit name,
  Express, cancel request, remake request) and **Notes**.
- **Reuse returned items (owner idea)**: in *New orders – to make*, highlight an order when an RTO order
  has exactly the same printed name (**same letter case**) and colour, so the returned piece can be sent
  instead of making a new one. Agent's proposal: RTO_DELIVERED (piece is back with us) = "Returned piece
  available"; RTO_INITIATED / RTO_ACKNOWLEDGED = "Returned piece on its way back". A **Use returned piece**
  button (after a person checks it's not damaged) links the two orders, moves the new order straight
  to *Printed – pack it* and marks the returned piece as used, so it is never matched twice.
  On today's data: 12 RTO orders, 0 exact matches with a Ready order.
- **Open**: confirm the Use returned piece flow and who may press it (production, logistics, or both).

### 2026-11-18: Build plan written
- `cp_id=105` is the same for every courier (owner). **Use returned piece**: both production and
  logistics may press it.
- Full build plan: `docs/build-plan-order-flow-and-logistics.md` (7 phases, version 0.23.0).
- **Open**: owner approval of the plan before any code.

### 2026-11-18: Plan approved, phase 1 (data + sync) built, not committed
- Owner approved the plan. Phase 1 adds, with no screen change: `first_status`, `print_started_at`,
  and "field groups" (Label Generated, Express, flag, name-change reason, remake, returned piece),
  each with its own change time so the newest change wins on every computer; call notes and usage
  counts tables; all in Supabase too. Supabase schema version 2: a computer on an older SnapSTL stops
  syncing until updated (the existing version guard, used instead of a new min-version key).
- Small plan change: "customer wants to cancel" is stored as the flag `cancel_request` (+ note), not
  separate columns; remake keeps the same order ID (no `remake_of`).
- Owner allowed downloading `postgres:16-alpine` for a throwaway test database (Docker, port 54329,
  removed after the run). All 103 tests pass, including the 20 shared-records tests.
- Phase 1 committed on the work branch (not merged to `main`, not pushed). Next: phase 2 (tab rule).

### 2026-11-18: Phase 2 (one tab rule, report rules, Printing) built and committed
- One rule decides each order's tab (`tabs.py`), used by the app and by Supabase; tested to agree
  on every combination.
- Report rules: never backwards; AWB EXPIRED on a made order clears the label ("Label expired – make a
  new label") → Printed – pack it; AWB EXPIRED on an order not made yet keeps it to make. Flags:
  website past PRINT but not made; cancelled while being made; back to PRINT after it was packed
  outside SnapSTL (new flag `back_to_print`, the agent's addition: otherwise such an order would be
  made twice). Orders first seen already past PRINT are history and never flagged.
- A plate started in Print moves its orders to *Printing*; existing batches are filled in once from
  their print jobs.
- Dry run on a copy of the owner's database (deleted afterwards): 1,670 orders → New orders 582,
  Check name 67, Ready to print 2, Printing 20, Printed – pack it 13, Label done – ship it 20, On the
  way 151, Delivered 767, Coming back 12, Cancelled 27, Problems 0, Skipped 9.
- 110 tests pass (incl. shared records). The Orders page still shows the old tabs until phase 3.

### 2026-11-18: Phase 3 (production Orders page) built and committed
- Orders page: the 12 tabs in three groups (Make / Pack & ship / Records) with counts; a **Stage**
  column in plain words; New orders sub-tabs Single, Clubbed, Ad-hoc, **Returned piece available**,
  **Express**. Mark printed / Back to Bundled removed. Only New orders can be ticked for a batch.
- Express orders first everywhere (Orders and Print), badge "EXPRESS · needed by …", red from 3 days
  before. Production can see Express but not change it (logistics sets it, phase 4).
- Returned piece: green "available" (RTO_DELIVERED) or amber "on its way back"; **Use returned piece**
  (asks to check the piece) → Printed – pack it; **Undo returned piece**; each piece used once.
- Problems – check shows the reason in words and **Clear flag**. Remake / Make anyway work from any
  tab (a delivered order made again shows in New orders).
- Checked in the browser on a copy of the owner's database (deleted afterwards); 116 tests pass.
- Not built yet: anything on the logistics page (Label Generated, Express, notes …) = phase 4.

### 2026-11-18: Phase 4 (logistics page) built and committed
- Port 8002 now serves the logistics page instead of a read-only Orders page. Layout as agreed:
  search (order ID, phone any way it's written, customer name) + "Last report uploaded"; menus
  **Labels** (Labels to make, Label done – ship it), **Customer help** (Check name first, Cancel
  requests & problems, Remake requests), **Tracking** (Being made, On the way, Delivered, Coming back,
  Cancelled); a panel per order with **Share** (tracking link, ready-made messages, WhatsApp),
  **Change** (Label Generated / Undo, Edit name, Express, Customer wants to cancel, Remake request, Use
  returned piece) and **Notes**.
- Rules: Label Generated covers a whole clubbed order and waits until every name in it is printed;
  Express also applies to the whole clubbed order; name edits need "Confirmed with customer" + a reason
  and only before a batch; red = 7 days since ordering and no label / stuck On the way > 5 days /
  Express ≤ 3 days. Tracking link = ClickPost pattern (editable later in Settings, phase 6).
- Port 8002 refuses everything else (checked: settings, batches, skip, reset, clear flag, Orders list …).
  Changes there need the shared records online, like everywhere else.
- Every action and click is counted per day (feature names only).
- Checked in the browser on a throwaway copy bound to this computer only (127.0.0.1), deleted
  afterwards; no password typed (a script-made sign-in). AGENTS.md rule for port 8002 updated.
- Open for phase 5/6: Settings → Usage screen; Settings fields for the tracking link, day limits and
  message texts (defaults are in place).

### 2026-11-18: Phase 5 (Settings → Usage) built and committed
- Settings has a new **Usage** section: each feature in words, times used and on how many days, for
  the last 7 / 30 / 90 days or a year, all computers together (from Supabase; this computer only if
  it can't be reached). Production's Use / Undo returned piece and Clear flag are counted too.
- Checked in the browser on a fresh throwaway instance with sample counts (no customer data).

### 2026-11-18: Phase 6 (Logistics settings, version 0.23.0) built and committed
- Settings → **Logistics**: tracking link (must start with https:// and contain {awb} or {order_id};
  empty hides the button), the three red limits (0–60 days) and the ready-made messages (add, edit,
  remove; up to 20). Saved with Save settings on the computer that hosts the logistics page. The
  production Express badge follows the same "red N days before" setting.
- Version 0.23.0. Docs (README, ARCHITECTURE, AGENTS, Help) brought up to date.
- Next: phase 7 — practice run on a copy of the database with the reference report, then the update
  day (backups, every computer updated together, 30-day report uploaded).

### 2026-11-18: Phase 7, practice run (passed)
Run on copies only (this computer's database + its computer ID as "A", an empty second computer "B")
and a throwaway Postgres in place of Supabase; copies deleted afterwards.
1. Today's version (0.22.23) on A filled the shared records: 1,670 orders, schema 1.
2. A updated to 0.23.0: schema 2, every record's tab worked out again, Printing filled from the print
   jobs of 25 batches. A: New orders 582, Check name 67, Ready to print 2, Printing 19, Printed – pack
   it 14, Label done – ship it 20, On the way 151, Delivered 767, Coming back 12, Cancelled 27,
   Problems 0, Skipped 9. Old tabs: Bundled → Printing 19 / pack it 14 / ship it 17 / On the way 12;
   In a batch → Ready to print 2.
3. B (0.23.0) joined: all 1,670 orders, every one in the same tab as on A and in the shared records,
   same batch owner for every order.
4. A uploaded the reference report (30 days): nothing lost, nothing flagged, no order went back to
   New orders, B and the shared records still identical to A.
5. A computer still on 0.22.23 is refused: "The shared records were set up by a newer SnapSTL —
   update this computer (Settings → Updates)".
- Found while setting up: a database copy that remembers the real Supabase empties itself against a
  fresh database (the existing "Supabase was cleared" safety, working as designed). Not a problem for
  the real update (same Supabase), only for test copies.
- Printing 19 / pack it 14 here vs 20 / 13 in the earlier dry run: production marked 2 orders printed
  on the real computer in between (live work), not a difference in the new version.
- Not covered by this run: a report with *new* statuses (the reference file was already uploaded);
  those rules are covered by the automated tests (AWB EXPIRED, flags, never backwards).
- **Next (needs the owner's go-ahead): update day** — merge to main and push, back up every computer,
  update all computers the same day, upload a 30-day report.

### 2026-11-18: 0.23.0 shipped; 0.23.1 small-first-letter check
- 0.23.0 merged and pushed to `main` (merge made without touching the folder the running SnapSTL uses;
  every computer updates through Settings → Updates).
- Owner (0.23.1): a name whose first letter is small (e.g. "riya") goes to **Check name first** — it's
  almost always a typing slip; the name still prints as typed or as approved. New reports and orders
  from the shared records get the check; once after updating, Ready orders not yet checked by a person
  are re-checked (5 such orders on the owner's database when this was made). Orders a person already
  approved or edited are left alone.

### 2026-11-18: next update planned — Batches, Print → Verify, clean-up (0.24.0)
- Owner's problems: Batches table cluttered (Computer column useless with one computer), names not
  shown, list only grows, no archive or date filter; Print needs merging of same-colour single-name
  plates across batches, a manual check step after printing (spoiled letters today mean digging out
  the file and reprinting by hand), clearer Completed / Removed; .3mf names hard to read; Refresh
  button on Orders. Printers part of Print stays as it is.
- Found: downloaded name models usually have one piece per letter (an "i" dot is its own piece,
  touching letters are one piece), so a single-letter fix plate can be made inside SnapSTL.
- Decisions: shipped = On the way or later; Recycle Bin, not permanent; batch files 10 days after
  shipped, models after 15 days unused; merge any plates of a colour up to 3 names, suggest only;
  verify bases and names with a staff name from a list; everything printed before counts as
  verified; new file names `Pink – A + B + C.3mf`; checks sync through Supabase; logistics sees
  "waiting for check" and only checked orders in Labels to make; Activity log and download queue move
  to a new Activities tab; packing slip only for Completed items; fewer hints, auto-build with a
  "build plate clean" tick, print by colour, disk space — yes; Hindi — no; home screen — not chosen.
- Plan: `docs/build-plan-batches-print-verify.md` (7 phases). Waiting for the owner's approval.
- Owner (later the same day): "build plate clean" means no print may start while a finished print is
  still on the printer's bed; approved all 7 phases and asked to build, push and write a brief.

### 2026-11-18: 0.24.0 built and pushed
- Built as planned (`docs/build-plan-batches-print-verify.md`), brief for the owner:
  `docs/release-0.24.0-brief.md`.
- **Print → Verify**: an order is made (Printed – pack it) only when its base and name are checked good
  by a named staff member; fix plates for spoiled letters / a name / wrong colour / a broken base, cut
  from the plate's own file at the same size; whole plate failed → back to the list. Completed lists
  checked names with who checked them, packing slip, Found a problem.
- **Bed clear**: Start print asks to confirm the bed is empty when the printer shows a finished print; a
  busy printer keeps the plate held. Queue build stays manual.
- **Merge** (suggest only, ≤ 3 names, same colour, not started) and **Split back**; group by colour.
- **Batches**: names, progress, status in words, day groups, filters, Archive; folders to the Recycle
  Bin 10 days after all orders are on the way, models unused 15 days too.
- **Activities** tab (download queue moved there), Orders **Refresh**, explanations behind **?**,
  logistics shows *Printed – waiting for check*.
- Checks: 137 automated tests pass (sync on a throwaway Postgres). Practice run on a copy of the owner's
  database: 116 printed items counted as checked "Before 0.24", all 1,670 orders in the same tab on the
  copy, on an empty second computer and in the shared records (schema 3); a 0.23 computer is refused.
  Letters of real names split correctly (e.g. SuriPari 8, Ronika 6, ANIKA 5).
- **Update day**: every computer must update the same day (0.23 computers stop syncing).


### 2026-11-18: 0.24.1 Slicing page fix
- Owner reported: Slicing → Bambuddy profiles per printer stuck on "Loading…" after updating to 0.24.0.
- Cause: the page lines declaring the Slicing helpers (`SLPR`, `SLP`, `slName`, `SLOTS`, `SRC_LABEL`, `CLOUD_WHY`,
  `putJSON`) were removed by mistake in 0.22.22. Saved profiles were not affected.
- Fix: lines restored; `tests/test_page_script.py` fails if they go missing again. Checked against a stand-in
  Bambuddy: load, save, switch printer, reload, print defaults.


### 2026-11-18: 0.24.2 Bambuddy profile list and logistics search
- Owner reported: Slicing showed every saved profile as "no longer in Bambuddy", with an Internal Server Error, while
  the profiles were still in Bambuddy.
- Cause: Bambuddy 1.3.1's combined profile list crashes when it is signed in to Bambu Cloud and SnapSTL's key may use
  the cloud. SnapSTL treated the failed list as profiles that had gone.
- Fix: when that list crashes, SnapSTL reads the owner's profiles from Bambuddy's local list and says the standard
  profiles are hidden. If nothing can be listed, saved picks show "(saved)" and no false warning appears.
- Logistics search also finds the name to print, as the customer typed it or as corrected.
- Checks: automated tests and a stand-in Bambuddy that fails the same way. Not yet run against the real Bambuddy.

### 2026-11-18: Branch selector for testing
- Owner asked for just the branch picker on main so testers can switch to the published
  local parametric model branch from Settings → Maintenance → Updates and return to main.
- The selector shows branches from origin. Branch switching requires confirmation and keeps the
  existing dirty-file, unpublished-commit and busy batch checks. Models stays on its test branch.
