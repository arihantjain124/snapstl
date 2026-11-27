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

### 2026-11-20: User logins and roles planned, then put on hold
- Owner asked for per-person logins with Admin and Production roles (logistics page unchanged), Verify tied
  to the signed-in person, only Admin uploading reports, anyone creating batches.
- Owner decided to **skip logins for now**. The plan, proposed access table and the ways it could break the
  system are in `docs/plan-user-logins.md`; build it only when the owner asks.
- Meanwhile the other requested changes go ahead (Print, Printers, Batches, Logistics, Orders) without logins:
  Verify keeps "Checked by".


### 2026-11-20: 0.25.0 easier Print, Printers, Batches, Orders and logistics
Owner's requests after using 0.24 in production (backlog of 500+ orders, customers asking about delays).
Agreed: two releases — 0.25.0 now, 0.25.1 (Verify by name, staff PINs, merging with tick boxes) next.
- **Print:** top line with printers free for the next print (colours loaded), beds to clear, busy / offline;
  **Refresh from Bambuddy**. *Names* renamed **Letters**. Bases has *To print* / *Printed* (with letters
  printed x of y); Letters is split into *Base printed — print these letters now* / *Base still to print*.
  Both have a search (name, order ID, batch).
- **Verify:** a *Saving…* screen covers the card while a check is saved (fix plates take seconds); the server
  saves one check at a time and refuses a second save of the same plate ("already checked by …"). The live
  records had no double saves (207 checks), so this only stops wasted presses.
- **Batches:** B/L marks per name (base / letters: printed, printing, to print) and a line per batch
  (*base ready, letters pending* …) that filters the names.
- **Printers:** what it prints (which names), **Ready at** clock time, filament, problems; temperatures,
  fans, network, firmware and Bambuddy's queue under *Details*.
- **Orders:** oldest first (Express still on top); *Payment* column (Prepaid / COD / Partial COD, from the
  report; filterable); days waiting (red from 7); **Select oldest N**.
- **Logistics:** statuses renamed (owner approved): *In queue – ready around (date)*, *Printing now*,
  *Printed – being checked*, *Confirm name with customer*, *Ready to pack – make label*, *Packed – waiting
  for courier*, *Shipped with (courier)*, *Returning to us (RTO)*. "Ready around" = place in queue / orders
  finished per day over the last 7 days, for the team only (owner). Order IDs click to copy. Label Generated
  was hard to find: tick boxes + *Paste order IDs…* + the button on rows in a search too. A WhatsApp draft per
  name problem; new ready-made messages offered only where they fit (the old defaults are replaced only if
  never changed — the owner's settings had none saved).
- Not changed: no logins (on hold, `docs/plan-user-logins.md`); only the order date is kept from the report
  (not the time), so same-day orders keep order-ID order.


### 2026-11-20: 0.25.1 Verify by name, staff PINs, merging with tick boxes
- **Verify by name:** one card per order, shown once its letters and its base are both printed (owner: a person
  checks a name with its base, and Verify no longer fills up with half-printed plates). Parts already checked
  show "checked by …". **Confirm – ready to pack** / **Save – send problems for reprint** replace *Save check*.
  The info line says how many names wait for their other part. Search by name or order.
- **Whole plate failed…** moved to the printed plate: on each card (the plate that part was printed on) and in
  Bases → Printed and the new Letters → Printed (so a spoiled letters plate can go back before its bases are
  printed — found while explaining the update to the owner); every unchecked item on that plate goes back to print.
- **Staff PINs** (owner: a check must be saved under the person who really did it, without logins): Settings →
  Staff holds the owner PIN (this computer, `owner-pin.json`; reset with `snapstl-admin reset-owner-pin`), the
  people and their 4-digit PINs (shared with every computer as salted hashes, new columns `staff.pin_hash` /
  `pin_changed`; no schema version bump — older apps leave them alone). On Verify a person types their PIN once
  (30 minutes without a check signs them out). Until anyone has a PIN, Verify keeps the name picker, so the update
  doesn't stop checking. Limit (told to the owner): two people sharing one PIN can't be stopped.
- **Merging with tick boxes:** tick letter plates of one colour (not started; letter **fix** plates too), up to 3
  names; other colours can't be ticked; Split back returns exactly the plates merged (each item records `src`).
- On update day: set the owner PIN and everyone's PIN in Settings → Staff on the farm computer.


### 2026-11-21: 0.26.0 Stats, packing and customer help pages, Print next, fixes for stuck orders
Owner's requests after using 0.25 (backlog, packers mixing up similar names from other days, staff who find
English hard — the language help was skipped by the owner).
- **Found:** 86 orders (11–17 Nov) stuck as "not needed" with website status PRINT: their first upload marked
  them so, and the shared records kept that. Owner: from 13 Nov nothing was made until the website says
  pack / in transit → 77 back to To make / Check name; 9 older ones (and AWB EXPIRED) → Skipped. One-time
  repair at startup (`ingest.repair_026`). 77 orders were in Problems only because the website had packed /
  shipped them: owner — packed means made, wherever → the flag is gone and such orders leave Print, Verify and
  the base / letters marks (`makeit.SHIPPED_SQL`). Practice run on a copy: Problems 164 → 1.
- **Statuses:** To make · Check name · In production (waiting to print + printing) · Ready to pack · Packed ·
  Shipped · Delivered · Returned (RTO) · Cancelled · Problems (real ones only) · Skipped.
- **Stats** page: made per day vs new orders per day (chart), backlog and days to clear, daily target (set on
  the page), per printer (plates, failed, hours printing from 0.26: `print_jobs.started_at`), per checker,
  order → made / shipped days, fixes, cancelled / RTO, COD share. On the owner's data: ~51 made vs ~49 new per
  day; X5 BOB and X2 BOB fail 37% / 45% of prints.
- **Print:** "ready at" clock time under the % of printing plates and in the top line; **Print next** on each
  free printer (fix plates, Express, letters whose base is printed in a loaded colour, bases, then the rest —
  through the usual Start print, bed-clear confirmation included); a sound and a notice on every page when a
  printer finishes or fails (Alerts switch on Print); **Split…** a plate not started (or failed) into smaller
  plates (1 + 1 + 1, 2 + 1 …) to spread a night run; **Join back** while no part is started.
- **Orders:** **Make next batch** (oldest first, Express on top; downloads and builds by itself unless a model
  fails); a new report saved in Downloads is imported by itself (Settings → Folders); red warning when the
  last report is more than a day old.
- **Packing page (8002)** and **customer help (8003, own password and cookie)** — see README.


### 2026-11-21: 0.26.1 the sync never hangs on a leftover connection
- After updating to 0.26.0 the farm computer stayed on "Connecting to the shared order records…": the app was
  restarted in the middle of a sync, Supabase's pooler kept that transaction open (a read of `snapstl.staff`),
  and the new app's setup (which changes that table) waited for it forever. It cleared when Supabase ended the
  leftover transaction; nothing was lost.
- Fix: every shared-records session sets `idle_in_transaction_session_timeout` (2 minutes), and the setup uses
  `lock_timeout` (20 s): when a table is held it says "The shared records are busy … trying again in a minute"
  and the next sync tries again. Test: `test_a_leftover_connection_never_hangs_the_sync`.


### 2026-11-21: 0.27.0 Letters and Bases slice with their own profiles
- Owner: letters are the most important part; their top layers showed print lines. Letters print only on the P1S
  (colours in the AMS), at Standard speed with a better finish; bases faster without losing strength or look;
  0.4 mm nozzle only (no 0.6 for now). Both picked automatically by what is printed.
- Measured (Bambuddy print history): 3 bases about 9.6 h on P1S, 11.7 h on A1; a letter plate about 0.8 h.
- **Slicing → Letters** per printer: a Letters process profile and Letters speed. Letter plates (fix and merged
  letter plates too) slice with it (a separate slice key) and print at that speed; bases keep the printer's
  profiles. Once any printer has a Letters profile, letter plates are offered and pre-selected only on those
  printers, and Start print elsewhere is refused (farm.letters_printers / may_print).
- Profile files for the owner to import in Bambuddy (C:\Users\Admin\SnapSTL\Bambuddy profiles\process, and
  "SnapSTL letters + bases profiles.zip"), made from the current ones:
  * SnapSTL Letters 0.20mm P1S: ironing topmost (12%, 0.10 mm, 30 mm/s), Arachne walls, one wall on top off,
    outer wall 150, top surface 120 mm/s, seam back + scarf seam on outer walls.
  * SnapSTL Bases 0.20mm P1S / A1: same layer height, walls and 15% infill; infill combination on; seam back +
    scarf seam. Expected saving is small (about 5-10%); bigger savings need 0.24-0.28 mm layers or a 0.6 mm
    nozzle, which the owner declined for now (look first).


### 2026-11-21: 0.28.0 No double merges, Remove everywhere, spare pieces, Also pack
- **Found:** a printed merged plate "… (2)" still listed under Letters, so its names showed as not complete. The same
  names were merged twice a few seconds apart (three times so far, two of them printed twice): writing a merged
  plate takes seconds and a second press passed the "already merged" check. Now merge / split back / split / join
  run one at a time (`extras._EDIT`), a merge is refused when any of its names (from the same source plate) is on
  another active merged / split plate, and the page keeps every Merge button off until it reloads.
  `extras.duplicates()` flags a copy (Print → Letters, red) and Start print refuses it. One-time repair at startup
  (`extras.repair_028`): the copy not started is set Removed; copies printed twice put their letters into Spare
  pieces (letters only).
- **Remove** on every plate, merged plates and split parts too (Put back in Removed).
- **Available to merge** moved under the Letters list and offers only plates whose bases are printed (owner: staff
  merged letters before bases, then the letters waited); ticking such a plate asks first.
- **Printers → External spool feeding** per printer (`farm.external_spool_by_printer`): a printer with an AMS leaves
  its external spool out of the filament choices unless on; a printer without an AMS keeps it.
- **Completed → Reprint / problem…**: reprint base, letters (whole name), some letters (tap them), both, wrong
  colour; the order goes back to Printing (only until shipped, as before).
- **Also pack** (packing page and customer help): other products of the same Clubbed Order ID, read from the report
  rows kept on this computer (`shop_lines`); the product name comes from the report's Product Code.
- **Spare pieces** (`spares.py`, shared table `snapstl.spares`, newest `changed` wins, never deleted): exact name +
  colour match like returned pieces; an order sent with one gets `reuse_of = "spare:<uid>"` (the returned-piece
  field, so tabs, batches and the shared records already treat it as made). Complete pieces can be used; letters /
  base only are shown. Make next batch and Create batch leave out orders with a complete piece in stock.

### 2026-11-22: 0.28.1 Spare pieces: bases only, no letters from the update
- Owner: letters printed twice are thrown away; only bases are kept (including bases made before SnapSTL). The
  0.28 repair no longer adds the letters of copies printed twice to Spare pieces (it would also have added them
  once per computer, since the repair is remembered per computer but spares are shared). "Letters only" is gone:
  a spare is a base or a complete piece. A base matches an order by name alone (any letters' colour), so the
  order only needs its letters printed; a complete piece still needs the same colour.
- **Start print** felt like nothing happened: the button greyed for a few seconds, came back, and the print
  started minutes later (slicing in Bambuddy, sending, printer warming up). The worker stored the job as "held"
  for a moment before releasing it, so Print could show Start print again in between; now it is stored held only
  when it really waits for a person. The row shows **Starting print…** (step 1–3 with a bar, slice % when Bambuddy
  reports it), the button reads "Starting…" while the click is sent, and Print refreshes every 5 s while a plate is
  on its way (else once a minute).
- **Printing** tab in Bases and Letters (owner): To print / Printing / Printed, a plate in exactly one. Printing =
  an active job not held for a person (queued, slicing, sending, started, printing); a held plate stays in To print
  with Start print. Done → Printed as before. After Start print a note says where it went.
- **A base sent twice** (found on the farm, 0.28.0): two Start print clicks reached the app in the same second
  (the page re-drew the row while the first click was still being sent, so the button was there again); both
  passed the "already on its way" check and Bambuddy got the plate twice. The copy was cancelled in Bambuddy, and
  as the newest job it made the plate look not started while the first one printed. Now: Start print runs one at
  a time (`makeit._START`) and refuses a plate with any job on its way; the worker never sends a second copy of a
  plate already on its way (cancelled, "pressed twice"); a plate's job is its newest except that a cancelled /
  failed copy never hides one on its way, and a copy never hides the one printing (`farm.replaces`, used by Print,
  Verify, merge checks); the button stays "Starting…" while the click is being sent, even if the page re-draws.
- **Print page blank / app "stuck"** (farm, 0.26.1): `/api/makeit` took 6–12 s of CPU; every open page asks for it
  every minute (and Start print / Print next again), so the requests piled up and the server stayed at 2+ cores
  without answering — Restart then hung too. Causes: `bed_state` ran once per plate × printer (884 times), and each
  `db.tx()` re-ran the whole schema + column checks. Now: bed state once per printer; tables / columns made once
  per database file per run (`db._SCHEMA_DONE`); batch labels read once (`verify._label`); `/api/makeit` works
  one request at a time and requests that arrived meanwhile share its answer. On the farm's data: 9 s → ~1 s.

### 2026-11-22: 0.28.2 Print shows its plates again
- 0.28.1 put a `// comment` in the middle of a line of the page script, which commented out the rest of the line
  (`MKSEL`, `OPTLBL`): Print → Bases / Letters showed the counts but no plates. Fixed; `tests/test_page_script.py`
  now checks the Print page's globals are declared and that no code is glued after a `//` comment.

### 2026-11-24: 0.29.0 Products — name stands listed (step 1 of 4)
- `products.py`: Organizer (`Magicians_35`, `Adhoc`) and the name stands GTA (`Magicians_174`), Harry Potter
  (`Magicians_156`), Barbie (`Magicians_117`). Read from the report ("Write your NAME here.: …", no colour), with
  the same name checks. Orders page **Product** chips (Organizer default, remembered per computer); stand rows show
  the product, both colours and their size; Check name approves a stand without a colour; customer help shows it.
- Settings → **Name stands** (`settings.StandSettings`): size by letter count (starting values: the organizer's
  lengths, 40 mm letter height — to be checked on the first prints), thickness 40 (block 37 + letters 3), font
  size 40, brim offset 6 (owner), per product block / letter colour (GTA black/white, Harry Potter yellow/black,
  Barbie hot pink/white) and letter spacing (25 / 0 / 0, MakerWorld's).
- Not made yet: batches take the organizer only (`products.MAKEABLE`); stand orders are not sent to the shared
  records (`products.SHARED`; a computer before 0.29 would batch them as organizers); spare / returned pieces and
  Stats stay the organizer's.
- MakerWorld (checked in the owner's browser, 2026-11-24): the three models share one form (text, font size,
  letter spacing, back thickness, letter protrusion, brim offset, flat bottom cut, preview colours, place in
  printing position, units per em; GTA also "Use Font Features"); Generate gives one OBJ + MTL, two materials:
  the block (Z −15…15 at the default 30 mm) and the letters on top (3 mm).

### 2026-11-24: 0.30.0 Name stands: batches and downloads (step 2 of 4), helper 2.4.0
- Batches: one product per batch; Make next batch takes the product chosen on Orders (no product = organizer, as
  before). A stand batch downloads and stops at Ready to build ("Plates: next update"); the builder never builds
  it with the organizer's plates.
- Helper 2.4.0: works on every product's MakerWorld page (`/api/helper/config` → `pages`), tells the app which
  page it's on (`design`), opens the page of the next download by itself (`go`); fills the stand form (text,
  font size, letter spacing, back thickness 37 = 40 − 3, letter protrusion 3, brim offset 6, flat bottom cut 0.5,
  place in printing position, GTA's Use Font Features) and sends MakerWorld's one OBJ back. An older helper
  (≤ 2.3) is only given organizer downloads.
- Saved: `<Name> - GTA - <key>/{model.obj, base.stl, letters.stl, info.json}`; checked: two colours, block =
  back thickness sent, letters = protrusion sent and on top of the block (±0.3 mm), else refused and tried again.
- Shared records: a stand order goes there once batched (claimed, so no stand is made twice); a ready one stays
  local (a computer before 0.29 would take it for an organizer). Every computer must be on 0.29+ first.

### 2026-11-24: 0.31.0 Name stands: plates (step 3 of 4)
- `stands.py`: length and letter height from the size table, thickness as made (37 + 3); block and letters
  scaled together; a warning when letters are stretched / squeezed (X/Y ratio outside 0.6–1.6).
- Up to 3 of one product per plate, oldest first; the back 45 mm of the bed kept free for the prime tower.
- `Stands/<Product>/<Product> - A + B + C.3mf`: each stand one object of two parts — Base → filament 1, Letters →
  filament 2 (`threemf.write_3mf_assemblies`, Bambu's model_settings.config fields, as Bambuddy reads them).
  Every file read back and checked. `report.csv` per stand.
- The batch keeps stand plates in `summary.stand_plates` until the Print page handles them (next update); a built
  stand batch shows "Print page: next update" instead of Print. Make next batch of stands builds by itself.
- `threemf.read_3mf` skips objects made of parts (their parts are read); organizer files are unchanged.
- Checked with the farm's Bambuddy (2026-11-24, owner's OK; test files deleted after): a 3-stand plate sliced with
  the P1S profiles and PLA Basic twice (black + white) → filament 1 black 250.6 g (blocks), filament 2 white 35.3 g
  (letters), one AMS change at Z 37.2, prime tower at X 17–34 / Y 220–236 (inside the free back strip; profile
  wipe_tower 15, 220). Bambuddy's pre-slice "filament requirements" of the unsliced file lists no filaments (it
  reads them after slicing) — SnapSTL sends both filament profiles itself.

### 2026-11-24: 0.32.0 Name stands on the Print page (step 4 of 4)
- Print → **Name stands** (shown when there are stand plates): To print / Printing / Printed. Only printers with an
  AMS; the pre-selection fits both colours; a block slot and a letters slot (never the same). Start print slices
  with the printer's filament profile twice (block colour, letter colour) and sends `ams_mapping` [block, letters];
  the letters slot is kept in `print_jobs.ams_tray2`.
- Verify: one check per stand — Good / Stand spoiled (a fix plate with the whole stand, both colours) / Whole plate
  failed. Completed → Reprint the stand. Merge / Split stay organizer-only.
- Stand plates are the batch's `plates` (kind `stands`); batches built by 0.31 are moved once at startup.
- Found in the preview before release: a `// comment` mid-line again hid code (stand rows broke the Print table);
  fixed, and `tests/test_page_script.py` now catches a `const` / `let` after a `//` comment even with a space.

### 2026-11-24: 0.32.1 No part made twice
- Owner asked why stand orders showed 0: the latest report was uploaded about an hour before 0.29 (stands) arrived, so
  its stand rows were skipped; uploading the report again on 0.32 brought them in. No code change.
- Owner saw bases made again for names already made. Found on the production computer's data (583 plates, 697 print
  jobs): one bases plate printed twice: a double-pressed Start print (before 0.28.1) left a cancelled copy that,
  once the first print finished, hid it, so the plate came back to Bases to print. One order made twice
  (base and letters) in two batches: it had been made and checked Good, then came back to To make with its made time
  cleared; the local log doesn't show what released it. Two other double prints were real Reprints; two on the first
  setup day (16 Nov) look like testing.
- Fixed: a cancelled / failed copy never hides a done print; an order made and checked Good in an earlier batch is
  left out of new batches (Orders shows "Already made … Remake"); orders leaving a batch are written to Activities →
  Orders, so the next case can be traced.
- Open: what released the order from its batch (shared records "released" from another computer is the likely path).

### 2026-11-24: 0.32.2 Split letters with their bases; packed orders still checked
- Owner: when a bases plate is split, its letter plate should split the same way with the same names, so the letters
  are printed only once their base is done (good to have, not enforced). Done: splitting a bases plate splits every
  letter plate holding its names (not started yet) by base part; Join back on the bases joins the letters back.
- Owner saw a name whose split base and letters were both printed but never appeared in Verify. Cause: the website
  report already said PACK (label made on the website) before the check, and since 0.26 packed counts as made, so it
  left Verify. On the production data 6 orders were in that state. Now Verify keeps orders that are PACK / HANDOVER
  and printed but not checked, marked "Packed on the website – check before it goes"; once in transit, delivered or
  returning they leave Verify as before.

### 2026-11-24: 0.33.0 Shipping labels from ShopDeck
- Owner: print the shipping label from SnapSTL for verified, completed pieces. Today staff search the order on ShopDeck's
  Process Orders, change the courier every time (Logistic; the new AWB doesn't expire), then press Label (PDF) and print
  it on a 4x6 thermal printer. Courier priority: Bluedart Air, Bluedart Surface, Delhivery Surface, Xpressbees (Bluedart
  Air sometimes only after Show more). Clubbed orders (an organizer with any other product counts) are in the Clubbed
  tab, one label for the group; with other products a person must confirm everything is ready. Expired AWBs: regenerate
  that one order in the Expired AWB tab (no logistics change there), then the usual flow.
- Tried by hand on two real orders (one single, one clubbed): courier changed, new AWB, a one-minute wait, Label → an A4
  PDF in Downloads named SHP-<n>-<order or clubbed ID>-<AWB>.pdf, listing AWB, order ID, number of items and each name.
- Built: Print → Completed → Print label; the helper (2.5) does the ShopDeck steps; SnapSTL checks the page against the
  report and the PDF against the order before printing (SumatraPDF). Login is always done by a person.
- Found while building: Windows Smart App Control started blocking the Python that runs SnapSTL (its database library);
  the owner turned Smart App Control off.
- Printing checked: a real label (A4 PDF) printed through SumatraPDF, fit to 4x6, on the H30C Pro; the owner confirmed it
  scans. Open: the Expired AWB Regenerate path is untested on a real order; no full run from the Print label button yet.

### 2026-11-24: 0.33.1 Labels: Show more, courier order checked, ShopDeck pop-ups
- Owner's first real run printed a correct label, with two faults: the helper didn't open Show more in Edit Logistics
  and picked Bluedart Surface though Bluedart Air was behind it; Chrome blocked the label's pop-up window until the
  owner allowed it by hand.
- Cause: Show more is a button with an icon inside, and the helper only looked at elements with nothing inside them.
- Fixed: the helper presses that button (found by ShopDeck's own `showMoreShippingPartner` marker or its text) and stops
  if it stays closed; it sends every courier listed and SnapSTL refuses a pick that isn't first in Settings' order.
  Checked on the live page without saving: Show more opened, Bluedart Air listed and picked. Helper 2.5.1 allows
  pop-ups for pro.shopdeck.com only (new `contentSettings` permission) before pressing Label.
- Owner: an order already on Bluedart Air still goes through Edit Logistics (Bluedart Air picked again for a fresh
  AWB; not offered → next in the list), and the label is only taken after that change, never the existing one
  (expired AWBs). Already how it worked (every job changes the courier; an unchanged AWB stops it); now covered by a test.

### 2026-11-26: 0.33.2 Labels: the ShopDeck tab in front, timeouts, search proof, a trace
- First full day of staff use (2026-11-25): 23 Print label presses, 5 labels printed (all correct), 14 hung on
  "finding the order on ShopDeck" until a person pressed Stop (one for 81 minutes), 3 clubbed orders stopped with
  "Can't choose Order Id", 1 "Not found". The hung jobs never got a single report from the helper.
- Cause (likely; Chrome reported the test tab as visible, so not reproduced): the ShopDeck tab sat behind SnapSTL's.
  Chrome slows background tabs to about one timer a minute and freezes them later; ShopDeck's dropdowns didn't open in
  time. Every label that printed took about a minute — typically pressed again right after a Stop.
- Also: a search still loading was read as "no results" (it then tried Expired AWB and could say Not found); nothing
  was recorded of what the helper saw (couriers offered, why a job hung).
- Fixed: Print label brings the ShopDeck tab to the front (helper 2.6, new content script on SnapSTL's page) and goes
  back when done; a heartbeat, and a job no tab takes in 30 s or whose tab is quiet for 60 s stops with the reason;
  "not found" only on ShopDeck's own No result found (and "found" only once the list narrowed — checked on the live
  page, both lists, Single and Clubbed); every step kept and shown under the label (Details).

### 2026-11-26: 0.34.0 Pending Orders and Order History; clubbed orders made together
- Owner: old orders were missed because they sat in Check name, Clubbed or Problems while new ones were made; too
  many tabs. Live data: 140 ACCEPTANCE PENDING and 3 never-made AWB EXPIRED orders were in Problems, 49 in Check
  name, and one clubbed order had a name printing while the other was still to make.
- Sidebar: **Pending Orders** (To make · In production · Needs attention only when not empty) and **Order History**
  (Ready to pack, Packed, Shipped, Delivered, Returned, Cancelled, Skipped, All). Sub-tabs Single / Clubbed / Ad-hoc /
  Piece in stock / Express are gone: one To make list, Express on top, then clubbed orders whose other names are in
  production, then the oldest. Name checks are listed in date order without a tick box ("ask support…");
  approving moved to the customer help page only.
- ACCEPTANCE PENDING and AWB EXPIRED orders are to make (new ones, and once the existing ones never made); Print
  label waits for acceptance.
- Clubbed orders: a batch takes every name (or none while one waits for its name check); Select oldest / Make next
  batch never split one.
- Pending Orders hides Phone, Address and Report status by default; views, columns, filters and CSV on one line.
- Includes 0.33.2 (labels: ShopDeck tab in front, timeouts, search proof, trace).

### 2026-11-26: 0.35.0 Print by product, print time estimates, base plates merge, Batches on Print
- Owner: Print should follow the products (Custom Organizer, GTA, Harry Potter, Barbie); fewer sidebar entries
  (Batches into Print, no Activities page); how long each plate takes and when it's done; merge base plates up to 3.
- Print chips: Custom Organizer (Bases / Letters) · GTA · Harry Potter · Barbie · Verify · Completed (product
  chips) · Batches (with the download queue). Batches still downloading / building show at the top as Getting
  ready. Removed plates: a link under To print.
- Estimates from 306 done prints (at 124 %): bases about 42 min + 30 min per letter on P1S (49 + 33 on A1), letters
  about 9 + 3.2 min per letter; usually within ±30 min for a base plate, ±4 min for letters. Shown under the printer
  picked with the ready time after its current print; Stats → Print time per letter per printer. Name stands: not
  enough prints yet.
- Base plates merge (up to 3 bases, no scaling, refused when they don't fit), with suggestions and ≈ time.
- Plates holding a clubbed order whose other names are made come first (print this now).

### 2026-11-26: 0.36.0 Printer roles, AMS colours, the shift and the night
- Owner: two staff 8:00–22:00; every printer should print 3 bases overnight; 2–3 printers for letters only (they need
  watching), the rest on bases. Last 7 days: printers busy 59 % of the time, 57 % at night; bases are about 90 % of the
  printing; about 50 bases made a day against 65 orders.
- Printers → Printer roles and AMS colours: Bases / Letters / Flex / Any per printer, the colour each AMS slot should
  hold, the shift. Print pre-selects by role, Print next follows it, a slot not holding its colour shows in red,
  letter colours no Letters printer has loaded are listed for the Flex printer.
- Base plates say done before 22:00 / overnight plate / ends at night, then idle. Before you leave: a base plate for
  every printer (letters printers too) that ends around the morning, with Start print once it's free, and how many
  plates are short. Morning: what printed overnight and which beds to clear.

### 2026-11-26: 0.36.3 Labels: Print ▾ in Pack opens (Helper 2.7.1)
- First real try (owner): "Expected one label option under Print ▾ in Pack, found 0 (menu: nothing opened)". The menu
  has Label and Invoice; the helper only sent a click. It now sends the whole press (pointer-down … click), then a
  second click, then hover, and judges the menu items by their box on screen. Checked on a stand-in page with menus
  that open on pointer-down, on click, on hover, and on pointer-down closing again on click.

### 2026-11-26: 0.36.2 Labels: Re-Generate AWB confirmed; an old label is fetched again from Pack (Helper 2.7)
- Owner: two labels stopped at ShopDeck's "Re-Generate AWB — New AWB for this order will be created" box (jobs 34,
  38), one printed nothing because the PDF still had the AWB from before the courier change (job 37: Label pressed
  23 s after the change; this computer waited 15 s).
- Regenerate: the helper presses Confirm on exactly that box (SnapSTL checks its words; any other box still stops
  the job), waits 15 s (Settings: Wait after Regenerate), looks in Print again. Already Bluedart Air (first in the
  courier list): no courier change, the regenerated AWB is the new one. Another courier: changed as before.
- A label PDF with an old AWB: set aside, then after 60 s the order is found in Pack, SnapSTL checks the AWB there is
  the new one, the label is downloaded from Pack and checked in full again — at most 3 times (Settings). No courier
  change in Pack. A stopped label that was pressed on ShopDeck has **Get label from Pack**.
- Owner, with screenshots of Pack: Pack works like Print (Print ▾, Logistic; changing the courier there asks
  Yes,Change and sends the order back to Print). Print label now also works for orders in Pack (5 made orders were
  stuck at "PACK on the website"), and the order is looked for in Pack after Print and Expired AWB. In Pack: on
  Bluedart Air → the label from Print ▾; else Logistic: Bluedart Air offered → changed, back to Print, the usual
  flow; not offered → printed on the courier it has.
- Not checked on ShopDeck yet: the options under Print ▾ in Pack (the helper takes exactly one option named like
  "Label" and otherwise stops, listing the menu it saw).

### 2026-11-26: 0.36.1 Getting ready tidied; Create batch builds by itself; Build together
- Owner: the Getting ready strip on Print was cluttered (one row per batch, the names shown as [object Object]).
  Cause: 15 batches of 1–3 names made with Create batch on 25 Nov waited for Queue build (only Make next batch built
  by itself).
- Getting ready is one line (batches, names, ready / downloading / building, failed in red), Show batches for the list.
- Create batch downloads and builds by itself like Make next batch. Build together merges small batches waiting with
  their models ready into one batch per product (fuller plates, nothing downloaded again).

### 2026-11-26: 0.36.4 Less on Print: no AMS banners, no staff PINs, Before you leave on its button
- Owner: the red AMS-slot banners on Print (slot n should be X — Y is loaded) were distracting → removed from Print;
  Printers → Printer roles still marks a slot that doesn't hold its planned colour.
- Owner: remove the staff PIN check everywhere → Verify always uses **Checked by** (choose the name); Settings → Staff
  only adds and hides people; the owner PIN, PIN sign-in and `snapstl-admin reset-owner-pin` are gone. The app's own
  login already guards it. The `pin_hash` column stays (shared with other computers), unused.
- Owner: Before you leave opened by itself every few minutes (from 4 h before the shift ends) → it opens only when
  its button is pressed, and stays until Hide or the button again.

### 2026-11-26: 0.37.0 ShopDeck API — connect and live order status
- ShopDeck published a partner API (OAuth; orders: list, note, accept, print, pack, manifest, cancel; catalogue;
  coupons). Missing for us: the customisation (name, colour), a courier change and Regenerate AWB — the owner has
  asked ShopDeck for them (mail, 2026-11-26). The owner's app "SnapSTL" was made on the partner portal (read orders,
  create/update orders, read customers; redirect to this computer's port 8317).
- The ShopDeck MCP connector (Claude only, not callable by SnapSTL) is ShopDeck's analytics warehouse, ~10 minutes
  behind; it has the customisation. Comparing our last report with it: ShopDeck says `created` for PRINT, AWB
  EXPIRED and ACCEPTANCE PENDING alike; printed = PACK, dispatched = IN TRANSIT, cancel_initiated = CANCELLED
  INITIATED. So the API only moves orders forward to Pack / Handover / Shipped / Delivered / Cancelled / RTO.
- Built: Settings → ShopDeck (App ID, Connect, key renewal, Check now, switch, the latest changes); the poller
  every 5 minutes. Checked against ShopDeck's sandbox. One computer holds the key (owner: the label computer).
- Next (owner to decide): labels through the API for orders already on Bluedart Air; accept / pack / manifest;
  a bridge for the customisation until ShopDeck adds it.

### 2026-11-27: 0.37.1 Names and colours before the report (ShopDeck bridge)
- Test run (26–27 Nov): the ShopDeck connector gave 69 to-make orders of 26 Nov with their customisation, before
  the report. When the report came, all 69 matched it exactly (name, colour, SKU, quantity, clubbed ID).
- Built, with the owner's rule "it must not break anything / get out of sync": bridge files are imported as new
  orders only — known orders are never touched, the lowest status stamp so every report and the API override it,
  no report-upload record, no report rows / shop lines, only after a working sync, only files under 2 hours old,
  only files marked as bridge files; incomplete customisation or not-to-make rows are left for the report.
- The Claude task "SnapSTL – ShopDeck orders bridge" writes the files (manual only until the owner sets a schedule).

### 2026-11-27: 0.38.0 Stats — labels and shipping
- Owner asked for labels printed and items shipped on Stats, both side by side: labels — all on ShopDeck vs from
  SnapSTL; parcels picked up vs pieces, for every product; a list of what shipped each day (name printed and
  customer), kept for looking back (any date, CSV).
- Labels on ShopDeck are counted from the ShopDeck connection on (27 Nov 2026): the API shows no label date, so the
  poller notes the moment it sees an order move. Shipped days come from the report's Pickup Date (history back to
  every report imported), with the live status filling the gap until the next report.
- Products SnapSTL doesn't make show their SKU code (the report has no product name).
- Agreed next (plan 2026-11-27): day rhythm and a staff board in Hindi (English switch) replacing the shift plan
  panel and Before you leave; maintenance tracking; 24 printers.

### 2026-11-27: 0.39.0 आज का काम / Today — the day rhythm and the staff board
- Owner's goal: most output (organizers first) from 13 printers with two staff, 08:00–22:00, without the owner
  starting night plates. Measured: a base ≈ 3.3 h on P1S, ≈ 4.0 h on A1 (linear per base), a letters plate ≈ 0.4 h.
  Printers were busy ~68 %; plates finishing in the evening waited ~100 printer-hours a week.
- Owner's choices: the board's button starts the print; plates are split to size by themselves; a new first page
  (Print stays for the owner); night plates got ready from 20:00. Hindi with an English switch (the person at the
  printers reads little English). Removed (owner: confusing): Before you leave, Good morning, the night pills on
  Print. Print's time estimate is now rough per piece, not per letter (owner: per letter was wrong).

### 2026-11-28: 0.40.0 English board, labels waiting / printed, merge any plate
- Owner: staff said Hindi isn't needed — the board is English only (the switch is gone).
- Labels: the board's labels button opens Print → Completed. Completed lists only names waiting for their label;
  printed ones move to a new **Label printed** tab. New **Printed outside** mark per name (the label was made on the
  ShopDeck website or elsewhere), with Undo for 7 days; a mark the ShopDeck API still shows unlabelled after 6 hours
  is listed in Completed again with a warning (the data is not changed).
- Merging: split parts, merged plates (merged again) and fix plates merge too — bases with bases, letters with
  letters of one colour (owner: same colour). Split back undoes one step at a time; Split back / Join back also
  work on a plate that came back to its list.
