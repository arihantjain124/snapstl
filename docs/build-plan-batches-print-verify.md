# Build plan: Batches, Print → Verify, merged plates, clean-up, Activities (0.24.0)

Agreed with the owner on 2026-11-18 (discussion: `documentation.md`). **Status:** all 7 phases built,
tested and pushed to `main` as 0.24.0 on 2026-11-18 (owner approved all phases and asked for the push).
Changes from this plan, decided while building: Queue build stays a button (the owner's "build plate
clean" meant: no print may start on a bed with a finished print — see decision 78 in ARCHITECTURE);
file names use a plain hyphen (`Pink - Ayra + Riya.3mf`, safe on SD cards); Hindi and the home screen
were not built (owner).
Each phase ends with passing tests and can be committed on its own on the work branch.
The **Printers** part of Print (printer, slot, slice, options for bases and names) is not changed.

---

## Plain-language summary (for the owner)

1. **Verify and Proceed**: a plate Bambu reports as finished goes to a new **Verify** tab. A staff
   member (picked from a list) checks every name and base: Good, letter spoiled, whole name spoiled,
   base broken, wrong colour, whole plate failed. Only checked-good orders reach *Printed – pack it*.
   Spoiled items get a small **Fix** plate (just those letters / that name / that base, same size).
2. **Completed** lists checked-good names, with who checked them; **packing slip** for these.
3. **Merge**: SnapSTL suggests merging same-colour name plates (up to 3 names) across batches; you
   press Merge; Split back before printing.
4. **Batches** page: names as chips, progress (printed / packed / shipped), grouped by day, date /
   status / name filters, no Computer column, **Archive** tab.
5. **Clean-up** to the Recycle Bin: a batch folder 10 days after all its orders are on the way;
   downloaded models not used for 15 days.
6. **Activities** tab (after Settings): activity log, download queue, prints, checks, clean-ups — in
   plain sentences with filters.
7. **Smaller things**: Refresh button on Orders, easier .3mf names, Print by colour, disk space in
   Settings, hints behind a **?**, build plate clean tick, start build by itself after download.
8. **Logistics page** shows *Printed – waiting for check* under Being made; *Labels to make* only
   gets checked-good orders.
9. **Every computer stays in step** through the shared records (Supabase). Version 0.24.0, all
   computers update the same day (older versions are blocked from syncing).

---

## Decisions (owner, 2026-11-18)

| # | Topic | Decision |
|---|---|---|
| 1 | Batch counts as shipped | When every order is **On the way** or later (Delivered, Coming back) or Cancelled |
| 2 | Deleting files | **Recycle Bin**, never permanent |
| 3 | Downloaded models | Kept for reuse up to **15 days** after last use, then Recycle Bin |
| 4 | Merge | Any plates of one colour, total **≤ 3 names** |
| 5 | Merge | **Suggest only**; a person presses Merge |
| 6 | Verify | **Bases and names**, by a named **staff member** (dropdown, new names can be added) |
| 7 | Before this update | Everything already printed counts as **verified** ("Before 0.24") |
| 8 | File names | `Pink – Urvashi + Anaya + NIMMI.3mf`, `Bases – …`, `Fix – …`, `Merged – …` |
| 9 | Extra ideas | Yes: fewer hints (?), auto-build with build-plate check, Print by colour, disk space. Packing slip for **Completed** items only. No: Hindi. Not chosen: home screen |
| 10 | Sync | Yes — checks, staff list, merges, archive all go through Supabase |
| 11 | Logistics | Sees "waiting for check"; *Labels to make* only lists checked-good orders |
| 12 | Activity log / download queue | Move to a new **Activities** tab after Settings |

---

## Phase 1: Data (local database + shared records)

- **Staff list**: table `staff` (name, added_at, hidden). Synced (Supabase `staff`, no delete —
  a name is hidden, never removed, so old checks keep their name).
- **Checks**: table `print_checks`, one row per item checked: batch, plate, order_id, part
  (base / name), result (good, letters, name, base, colour, plate_failed, printed_by_hand,
  cancelled), letters (which ones), staff name, note, at, fix plate made. Append-only, synced like
  `order_notes`.
- **Order fields** (new field group `check`, newest change wins): `checked_at`, `checked_by`.
  `made_at` keeps its meaning "made and fit to pack" — from now on it is set only when both parts are
  checked good. The tab rule (`tabs.py`) does not change.
- **Plates of a batch** gain: printed-by-Bambu state is read from print jobs as today; new records for
  **fix plates** and **merged plates** (`extra_plates`: uid, kind, colour, file, made from which
  plates / orders / letters, state, created_at). Synced.
- **Batches** gain `archived_at`, `files_cleaned_at`. Synced with the batch record.
- **Downloaded models** gain a "last used" time (from batches that used them).
- **Backfill once** (decision 7): every order already in *Printed – pack it* or later, and every plate
  already printed, gets checks "good" by **"Before 0.24"**. Nothing moves tab.
- Supabase schema **3**; computers on 0.23.x are told to update. Tests: sync of every new table and
  field between two computers, backfill moves nothing.

## Phase 2: Print → Verify, Fix plates, Completed, Removed

- Tabs on Print: **Bases · Names · Verify · Completed · Removed** (Bases and Names lists unchanged
  apart from Fix plates shown first with a red **Fix** label).
- **Verify**: one card per printed plate; staff dropdown at the top (remembered on this computer,
  **+ Add name**); each item: **Good** / **Problem ▾**; **All good** for the whole plate. Problems:
  - *Letter spoiled* → letter buttons (the name split into its pieces, left to right; an "i" dot goes
    with its letter; letters that touch come as one piece) → Fix plate with only those pieces.
  - *Whole name spoiled* / *Wrong colour* → Fix plate with that name alone, same size, right colour.
  - *Base broken or warped* → Fix plate with that base.
  - *Whole plate failed* → whole plate back to Bases / Names (today's Reprint).
  - A plate Bambu reports **failed** never reaches Verify — it comes back to its list with the reason.
  - An order cancelled meanwhile shows **Cancelled – don't pack** and needs no check.
  - A third fix for the same item shows a warning: check the model.
- Fix plates are cut from the plate's own .3mf (already scaled), saved in the batch folder under
  `Fixes/`, and verified like any plate; when good, the original item counts as good.
- An order goes to *Printed – pack it* only when its base and name (and fixes) are checked good.
  Orders whose plates are printed but not checked stay in **Printing**, shown as
  *Printed – waiting for check* (Orders page and logistics page).
- **Completed**: one row per checked-good name — name + colour chip, order, batch, printer, checked by,
  when, "had a fix". Date filter; rows hide once the order is on the way.
- **Packing slip**: from Completed (select or per batch) — a printable page: name, colour, order,
  base plate / name plate, checked by.
- **Removed**: **Mark printed by hand** sends a plate to Verify; rows hide once their orders are
  shipped or cancelled; **Put back** stays.
- **Build plate clean**: *Start print* asks for a tick "Build plate is clean" (once per press).
- Tests: every problem path, fix plate content (only the chosen pieces, same size), order reaches
  pack it only after both checks, cancelled while printing.

## Phase 3: Merge plates, new file names

- Names tab shows a banner when ≥ 2 not-started plates of one colour have room
  ("3 pink plates have 1 name each → Merge"). Tick up to 3 names; SnapSTL packs them at their current
  size (no scaling) and says so if they don't fit.
- The merged plate is saved in the newest batch folder under `Merged/`; the single plates leave the
  list (marked *merged into …*, not deleted). **Split back** while the merged plate is not started.
  Printing / checking the merged plate counts for every order's own batch.
- New plate file names for new builds, fixes and merges (decision 8); old plates keep theirs.
- **Print by colour**: Names tab can be grouped by colour across all batches.
- Tests: merge across batches, too big to fit, split back, made / checked flows back to each batch.

## Phase 4: Batches page

- Table: **Batch · Date · Names** (coloured chips, "+9" opens all) **· Progress** (printed / checked /
  packed / shipped) **· Status** in words (Downloading → Building → Ready to print → Printing →
  Checking → Packing → All shipped). Grouped by day, newest first.
- Filters: date (Today, Last 7 days, This month, pick dates), status, search name / order.
- Computer column hidden unless more than one computer made batches.
- **Archive** tab: batches whose orders are all on the way or later (or cancelled); shows when its
  files go to the Recycle Bin.
- **Start build by itself** after the download finishes (tick in Settings, on by default; today's
  Queue build button stays).
- Download queue and Activity log leave this page (phase 6).

## Phase 5: Clean-up and disk space

- Daily, and on **Clean up now**: batch folders whose batch was archived 10+ days ago → Recycle Bin
  (Windows and Mac); record kept, marked *files cleaned*. Downloaded models not used by any batch for
  15 days and not needed by any open order → Recycle Bin (downloaded again if ever needed).
- Never while a batch is downloading or building; never a folder of an unshipped order.
- Settings → Maintenance: space used by batch folders, models, backups; what the next clean-up will
  move.
- Tests: only archived + 10 days, models in use are kept, nothing permanent.

## Phase 6: Activities tab, Orders Refresh, fewer hints, logistics

- **Activities** tab after Settings: one list in plain sentences ("Riya checked plate Pink – Urvashi +
  Anaya: all good", "Batch 3 built: 4 plates", "Batch 1 files moved to Recycle Bin"), filters by type
  (Downloads, Builds, Prints, Checks, Clean-up, Sync) and date; the download queue as its own section
  with Pause / Retry failed.
- **Refresh** button after *Add Ad-hoc order*: sync now with the shared records, then reload.
- Long hint paragraphs move behind a **?** next to each heading (Batches, Print, Orders, Settings).
- Logistics page: *Printed – waiting for check* in Being made; Labels to make = checked-good only.
- Help page and glossary updated.

## Phase 7: Version 0.24.0, practice run, update day

- README, ARCHITECTURE decision log, AGENTS §3, `documentation.md`, Help.
- Practice run on copies (as for 0.23): schema 2 → 3, two computers agree, backfill moves nothing,
  0.23.x blocked.
- Ship **only on the owner's go-ahead**; every computer updates the same day, after a backup, with no
  batch downloading or building.

---

## Risks and how they are handled

- *Orders stop reaching Printed – pack it* (new check step): backfill marks everything printed before
  as checked; Printing shows "waiting for check" so nothing looks lost.
- *Files deleted too early*: only archived batches (all orders on the way) after 10 days; Recycle Bin.
  A remake after that makes a new batch (models re-downloaded if cleaned).
- *Merged plate across batches*: originals are never deleted, only marked; Split back before printing.
- *Old versions*: schema 3 blocks 0.23.x from syncing until updated.
