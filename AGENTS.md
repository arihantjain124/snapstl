# AGENTS.md — instructions for AI coding agents (Claude, Codex, Cursor, …)

You are working on **SnapSTL**: a local app (macOS and Windows) that turns webstore orders into print-ready
name-organizer plate files (.3mf). The owner asks for changes in plain language (UI tweaks, new
columns, rule changes). Your job: **build forward on the existing design** — make the change safely,
keep the docs true, and commit. Don't rewrite or replace working parts.

## 1. Read first (in this order)

1. `docs/ARCHITECTURE.md` — runtime, tech stack, **file-by-file map**, data model, API, flows, the
   decision log (§16: things that were tried and **don't work**) and *How to extend safely* (§18).
2. `docs/scaling-rules-v1.1-handoff.md` — the business's scaling spec.
3. `README.md` — how the owner installs and uses it.
4. `documentation.md` — the running log of discussions with the owner: current understanding of the
   Order Reconciliation Report (webstore statuses and what they mean), app states, and open questions.
5. The files you'll touch. Read them fully before editing.

## 2. Binding rules (change only if the owner explicitly asks)

**Product rules**
- Scaling v1.1 axes: Base X = Name X, Base Z = Name Y, Base Y = 100 %, Name Z = 50 %; height 85–90 mm, length ≤ 240 mm.
- Print version is always **NO AMS**; names are **single line** (Name line 2 empty).
- **Compartments come from the scaling table**, never from the customer's order text.
- Names never mix colours on a plate (3 → 2 → 1 by fit). Bases 3 per plate (only the last may have fewer).
- Parts rotate only about Z; the build face never changes. A name is never scaled separately from its base.
- **Name/base fit check stays on**: every pair is checked after scaling and again from the written
  .3mf files; failing pairs are never printed (ARCHITECTURE §9). Don't loosen `FIT_TOL` or skip it.
- **Order ID is the de-duplication key**; a `bundled` order is never re-made unless the user clicks Remake.
- **Batches never auto-start, auto-download or auto-build**: Create → *Queue download* → *Queue build*;
  finished batches offer *Re-download* and *Rebuild plates* (new folder; old one kept).
- **Never delete downloaded models or batch folders** in code (cleanup, reset, migrations included) —
  the user cleans `~/SnapSTL/Downloads` and the output folder in Finder.
- Output: `<output>/<YYYY-MM-DD>/Batch N - <YYYY-MM-DD>/{Bases/, Names/<colour>/, report.csv}`, one
  **.3mf** per plate, file names `{name,color},{name2,color2}.3mf`, **never write ZIP files**. Sliced
  plates (print farm) go only in that batch's `Sliced/` folder, same path + `.gcode.3mf`.
- **Print farm (Bambuddy):** plates reach printers only through Bambuddy on this computer, only after a
  person presses **Start print** in Print (never automatically after a build). Any printer and any
  loaded filament slot may be chosen (owner decision 0.22.20): SnapSTL pre-selects the best colour fit
  (`farm.best_tray`) and sends the chosen slot as Bambuddy's `ams_mapping`; nothing is greyed out by colour. Printers have no Bases / Names
  setting: any printer can print either. Never skip Bambuddy's "Clear plate" step. SnapSTL never talks to printers directly.
- **No print starts by itself:** every queue item is created with `manual_start: true` (held) and is
  released only by a person's click (**Start print** → `autostart`, or `farm.start_job`). Never create
  a queue item that can start on its own, and never start one from a code path a person didn't click.
  Reprint only after a person confirms a failure.
- Printers, filament and printer settings are managed in Bambuddy; the Printers page stays read only
  (no adding printers, filament or roles in SnapSTL). The "prints as …" colour mapping was removed
  (0.22.20): the colour is chosen per print by its filament slot. **Refresh from Bambuddy**
  (`farm.refresh_from_bambuddy`) only asks printers to report their status again.

**Technical rules**
- **Day plan (0.43, `today.plan`)**: day plates end by 21:30 (`LEAVE_MIN` 30) — never plan one past it; every board action (start, split, merge, Make next batch) asks for a confirmation first (owner).
- **Spare base (0.42, `spares.use_base`)**: a spare base is a `good` base check (note `spare base`) plus the base taken off its plate — never edit a plate file in place, never touch a plate that is started; refuse rather than guess (merged plates, other batches).
- **Printer care (0.41, `printercare.py`)**: Bambuddy is the maintenance record — never keep a second one here; SnapSTL only sets up our tasks / intervals (`TASKS`), shows what is due on a printer that is free anyway, and performs it in Bambuddy on Done. Never tick a task the person did not confirm. Times per task are rough minutes for the card.
- **Today's work (0.39, `today.py`)**: the staff board is for a person with little English — one action per card, plain short English words (0.40: Hindi removed, staff said it isn't needed); strings live in `TD.en`. Times are rough per piece (`today.per_piece`), never per letter. The board's Start only splits a plate after the bed is confirmed clear, and starts through `makeit.start_print`.
- **ShopDeck API (0.37, `shopdeck.py`)**: the keys live only in `shopdeck.json` on the connected computer — never in the repo, a log, a page or the shared records. Live statuses move an order forward only and only through `ingest.live_status` (the report's stamp, state and flag rules); never apply `created` / `enqueued` (they mean PRINT, AWB EXPIRED and ACCEPTANCE PENDING alike). Read-only calls only until the owner agrees to a write (accept, print, pack, cancel). The customisation bridge (0.37.1, `ingest.ingest_bridge`, `reportwatch.import_bridge`) only ever adds orders SnapSTL doesn't have, with `BRIDGE_STAMP` (lowest), and never writes `files`, report rows or shop lines — keep it that way; bridge files hold customer names and stay on the computer.
- **Shipping labels (0.33, `labels.py`, `helper/shopdeck.js`)**: the helper never signs in to ShopDeck (a person does, then presses Continue) and uses only the page's own buttons, never ShopDeck's API. A label is pressed only after SnapSTL matched the page to the report's parcel exactly, the courier change gave a new AWB and the wait passed; it prints only after the PDF's AWB, order ID, item count, quantity and names matched. Every check fails closed; never retry a stopped job by itself, never loosen a check to make a job pass. A search counts as "not found" only on ShopDeck's own *No result found* (0.33.2); a quiet ShopDeck tab stops the job (never wait forever).
- **Clubbed orders (0.34, `clubbed.py`)**: batched whole, and not at all while a name of it waits for its check — never batch one name of a clubbed order alone (one parcel ships only when every name is made).
- MakerWorld models come **only** from the SnapSTL Helper (`helper/`) in the user's own Chrome.
  Direct API calls and app-controlled browsers were tried and **don't work** (ARCHITECTURE §16) — don't retry them.
- Never add code that hides automation, spoofs browser fingerprints, or solves CAPTCHAs/Cloudflare
  checks. Security checks are completed by the human; the helper waits.
- The app accepts extension calls **only** on `/api/helper/*` from 127.0.0.1. Don't widen that.
- The helper must keep checking, before every name, that its working tab is on the configured model
  page (designId); the app must never hand out work when `on_page` is false.
- The full app is localhost only. Optional Mac/Windows logistics listener on port 8002 requires `lan_access` + password. It permits only the logistics page's reads (`logistics.READS`) and, since 0.23 (owner decision), exactly these changes (`logistics.WRITES`, only while the shared records are reachable): Label Generated / Undo, Express, name change confirmed with the customer (before a batch only), call notes, customer wants to cancel, remake request, use / undo a returned piece, usage counts. Don't add more without the owner asking. Never expose editing, helper, settings, batches or printer controls there.
- Installers must **never run a second Bambuddy or stop one they didn't install**: detect an existing
  Bambuddy (service, data folder, launchd job, or the port answering) and use it; stop only processes
  started from SnapSTL's own Bambuddy folder; never delete Bambuddy data.
- **One Bambuddy computer** on the owner's network serves all SnapSTL computers: Bambuddy runs
  **natively** (Windows: Bambuddy's own installer, a Windows service — so printer discovery works) and
  **only the slicer sidecars run in Docker** (owner decision; never put Bambuddy itself back in Docker).
  Bambuddy scripts are **Windows only** (owner decision): `scripts/windows/bambuddy-server.ps1`, compose file
  `scripts/bambuddy-docker/docker-compose.yml` (sidecars only, 127.0.0.1:3001/3003). Firewall: TCP 8000
  + UDP 2021 for the local subnet only. Re-running the scripts applies settings only (setup.json);
  downloads/installs happen only when missing or with `BAMBUDDY_UPGRADE=1`. SnapSTL installers no
  longer install Bambuddy unless asked (Windows: `SNAPSTL_BAMBUDDY=server|native`); an older native
  install keeps being updated on Windows. SnapSTL itself stays Mac + Windows; a Mac only connects
  (`SNAPSTL_BAMBUDDY_URL` / Settings → Connections). Images pinned to `scripts/bambuddy-version.txt` (`bambuddy:X`, `*-api:bambuddy-X`); bump it
  only after checking `farm.py`'s calls against that release. The setup scripts must keep running
  under `irm … | iex` (Windows PowerShell 5.1, ASCII only, no `exit`). The Bambuddy computer script also
  installs the **OctoEverywhere Plugin Manager** (Microsoft Store, remote access to Bambuddy;
  `BAMBUDDY_OCTOEVERYWHERE=0` skips it); never uninstall it, and never script its plugin setup or account
  linking (the owner does that in its UI). SnapSTL itself never talks to OctoEverywhere. Slicing on this computer is gone for good (#43).
- Database: add columns **only** via `MIGRATIONS` in `db.py`; never drop/rename columns or tables.
- **The tab rule lives only in `tabs.py`** (0.23): `tab_of` (Python) and `case_sql` (SQL, used for the
  Orders page and by Supabase's `set_tab()` trigger) must stay identical — `tests/test_tabs.py` checks
  every combination. A report never moves an order backwards (`ingest.status_state`); doubtful cases
  get a flag (Problems – check) instead of a guess.
- `order_records.tab` / `ready_type` are set by the `set_tab()` trigger from the row (state, app_state,
  made_at, source_status, clubbed_id, sku, and the 0.23 fields): when the Orders page's tabs change, change
  that trigger the same way, and keep the column `COMMENT`s true — other agents read the table by them.
- Shared-records schema changes go in `sync._SETUP_SQL`; regenerate `docs/supabase-schema.sql`
  (`uv run snapstl-db schema > docs/supabase-schema.sql`) — a test checks they match.
- Shared order records (`sync.py`, Supabase): **never delete** there — no DELETE/TRUNCATE in code, keep
  the `refuse_delete` triggers. The only exception is `sync.clear` (owner-requested, 0.19.1): a person
  types CLEAR, every record is saved to a JSON file in the backups folder first, then the schema is
  dropped and created again empty. It is called only from there and from Maintenance → Reset with
  "also clear Supabase" ticked (`maintenance.reset(shared=True)`, typed RESET ALL, 0.22.6). Never call it from anywhere else. Settings never go there. Conflict rules live in the SQL (`_WIN`,
  `_CLAIM`); keep a batch's claim before its local creation, and refuse to create a batch when the
  records can't be reached. Local changes reach the outbox only through the triggers in
  `db.POST_SCHEMA`; writes coming from the records go inside `db.quiet()`. Run the tests with a
  throwaway Postgres too (README → Development) when touching sync, ingest or batch states.
- A webstore status only replaces a stored one when its `status_at` stamp is at least as high
  (`ingest.status_stamp` = `@file moment|event date|upload`; the file moment is the newest entry in the
  file, `ingest.file_as_of`, and comes first: a newer export updates every order in it); never let an
  older report undo a newer status.
- **Every computer needs the shared records** (owner decision 0.22.8): with `sync.REQUIRED`, no change
  (non-GET `/api`) is accepted while `sync.online()` is false, except `server.WITHOUT_SHARED`. Don't add
  ways to work offline. Settings → Database **Sync** and **Clean up** (`sync.match`) run without
  confirmation; Clean up never deletes in Supabase and makes this computer hold exactly what Supabase
  holds (`sync._apply_match`: orders, batches and their download / print / slice rows; never files on
  disk). A cleared Supabase (new `meta.instance`) is followed by every computer at its next sync
  (`sync._follow_reset`) — never re-send old rows after a clear.
- OS differences go only in `platform_os.py` (Mac and Windows); never call `open`, `launchctl`,
  `scutil` or Windows commands elsewhere.
- Clean-up must keep bundled orders. Every destructive maintenance action takes a backup first.
- No new runtime dependencies, frameworks or build steps without the owner asking. The UI stays one
  file (`static/index.html`, vanilla JS).

**Privacy**
- Customer name/phone/address live only in the local database and the owner's shared order records
  (Supabase, Mumbai). Never send them anywhere else, log them, or put them in git. **The repository is public:** every secret — license passwords, the Supabase connection string (`config.py`, `shared-records.json`, mode 600), the portal password — is entered in the app's Settings and stays in the app folder; never add a secrets file to the repo or ask users to edit one. License passwords live only in `settings.json` (per model) — never in model folders
  (`info.json`), logs, reports or git. The helper's `check` carries only booleans/"sent" — never the password.
- SnapSTL's Bambuddy API key lives only in `farm.json` (mode 600) and is never sent to the UI; SnapSTL
  never handles printer access codes. Plate files go only to this
  computer's Bambuddy, never to a cloud print service.
- **Slicing**: only Bambuddy's slicer (its sidecar), with the Bambuddy profiles picked per printer
  (`farm.target`); bases sliced white, names in their colour. SnapSTL keeps **no slicing
  settings**: only references (source + id + name) to Bambuddy profiles in `presets_by_printer`
  (`presets_by_model` = fallback from before 0.22.7). Never
  add a slicer on this computer, a preset store/import, or use of hand-made sliced files again
  (owner decision, ARCHITECTURE #43). **No cloud profiles** (owner decision 0.22.19, #62): only
  Bambuddy's local and standard profiles (`farm.PRESET_TIERS`); never patch Bambuddy's program. Profiles are added in Bambuddy, never by SnapSTL.
  Slicing never blocks the worker: one slice at a time, followed by polling, stopped after
  `SLICE_TIMEOUT_S` without progress. **No automatic slicing** (owner decision 0.22.22): a plate is
  sliced only when a person presses Slice / Slice all waiting (or Start print), always with the profiles
  of the printer selected for that plate — never a default or "main" printer; a failed slice is never
  retried by itself. A print always uses the slice for its printer's profiles.
- Never weaken the license guard: no download without the model's password, no saving a download the
  helper didn't confirm or that still has the watermark (decision log #18).
- Never commit customer files (`*.xlsx`, `*.csv` reports), outputs, or anything in
  `~/Library/Application Support/SnapSTL`.

## 3. Where to change what

| Request | Edit |
|---|---|
| UI text, layout, buttons, columns, tabs, side navigation | `src/snapstl/static/index.html` |
| In-app Help page | `<section id="t-help">` in `index.html` (update with README when behaviour changes) |
| Fit check | `scaling.fit_metrics` / `fit_problems`, `pipeline.build`, `export.write_plate` |
| New order field / column | `db.py` (MIGRATIONS) → `ingest.py` → `server._order_view` / `FILTERS` → `index.html` |
| Order tabs & filters | `tabs.py` (the rule, 0.23), `server.TABS`, `server.FILTERS` (+ `TABS` array in `index.html`) |
| Report status rules, Problems flags, label expired | `ingest.status_state`, `ingest._status_flags`; tests in `tests/test_tabs.py` |
| New setting / default | `settings.py` (+ `fillSettings` / `readSettings` in `index.html`) |
| Scaling targets / compartments | `settings.default_bands()`; math in `scaling.py` |
| Plate grouping / packing | `grouping.py`, `packing.py` |
| Output folders / file names | `pipeline.new_batch_dir`, `pipeline.plate_folder`, `export.plate_filename` |
| 3MF writing | `threemf.py`, `export.write_plate` |
| Report import / review rules / ad-hoc orders | `ingest.py` |
| Batch lifecycle / download queue / Re-download / Rebuild | `batches.py` (+ endpoints in `server.py`) |
| Model link / per-model license passwords | `settings.py` (`makerworld_url`, `license_passwords`, `design_id`) + Settings card in `index.html` |
| Watermark check / license confirmation | `watermark.py`, `batches.check_license` / `store_result`, `helper/content.js` (`runJob`), `helper/inject.js` |
| Plate / object pictures | `render.py`, `pipeline._plate_picture`, `server.model_view` |
| Updates (git pull, helper self-reload) | `updater.py`, `server.update_*`, `scripts/update.sh`, `helper/background.js` (`reloadIfUpdated`) |
| Logistics page (label + customer-care team): lists, search, actions, tracking link, messages | `logistics_api.py` (`VIEWS`, `ROUTES`), `logistics.py` (`READS` / `WRITES`), `settings.LogisticsSettings`, `<section id="t-desk">` + `dLoad` / `dOpen` in `index.html`; `tests/test_logistics_page.py` |
| Access control / other computers | `server._blocked`, `server.access_control`, `/api/portal`, `__init__.serve` (bind address) |
| Download queue + folder, batches | Print → Batches (`<div id="mk-batches">` in `index.html`, `loadQueue`, `loadBatches`; 0.35: no Activities page), `GET /api/activity` in `server.py` (still used), `db.add_activity`; `cache.py` for the folder |
| Print time estimates | `estimate.py` (from done prints), `makeit.board` (`est`, `free_in` per printer choice), `/api/stats/print-times` |
| Printer roles, AMS slot plan, shift | `farm.py` (`role_by_printer`, `slots_by_printer`, `shift`, `describe_roles`), `makeit.choose` (`ROLE_RANK`), Printers → Printer roles, Print → Before you leave |
| Print → Verify: checks, staff names, order made only when checked | `verify.py` (items, carriers, `update_orders`, `submit`, `backfill`), `makeit.update_made` (calls it), `vRender` / `cRender` in `index.html`; `tests/test_verify.py` |
| Fix plates (letters / name / base) and merged plates | `extras.py` (`pieces`, `make_fixes`, `merge`, `split`, `suggestions`), `makeit.board` (lists them); `tests/test_verify.py` |
| Bed clear before a print | `makeit.bed_state` / `confirm_bed_clear` / `start_print`, `farm.Worker` (never releases onto a busy printer) |
| Batches page, Archive, clean-up to the Recycle Bin, disk space | `archive.py` (`batch_rows`, `update_archive`, `clean_up`, `disk`, `CLEANER`), `platform_os.to_trash`, `cache.touch`; `bRender` in `index.html`; `tests/test_archive.py` |
| MakerWorld page interaction | `helper/content.js`, `helper/inject.js` (bump `helper/manifest.json` version; reload extension) |
| Backups / clean-up / reset | `maintenance.py` |
| Install / service / window | Mac `scripts/make_app.sh`; Windows `scripts/windows/*.ps1`; `src/snapstl/__init__.py` |
| Mac vs Windows (folders, Finder/Explorer, keep awake, restart, LAN name) | `platform_os.py` |
| Supabase `shop` schema for questions (every product: orders, customers, finance, shipments, views, glossary) | `shop.py` (SQL: tidy rules in `apply_line`, views, glossary — keep `COMMENT`s true); `shop.store_line` in ingest; `sync._push_lines`. shop triggers must never raise into SnapSTL's writes |
| Report columns kept for other agents (Supabase `report_rows`, `report_uploads`, `orders_full`) | `report.py` (`COLUMNS`: add a report column there, with its type and description); `ingest.ingest` stores rows; `sync._push_reports` |
| Shared order records (Supabase): connection in Settings, sync, claims | `sync.py`, `config.py`, `computer.py`, `server.shared_records_*`, `server.setup_status`, `dbtool.py` (`snapstl-db`), triggers in `db.POST_SCHEMA` |
| Print farm: Bambuddy connection, printers, Push to queue, slicing | `farm.py` (+ `/api/farm*`, `/api/batches/{id}/plates` and `/push`, `/api/print-jobs` in `server.py`); Printers page + Push to queue panel in `index.html`; `tests/test_farm.py` |
| Print tab (Bases / Names / Completed / Removed), Printing / Printed – pack it, Express plates first | `makeit.py` (`board`, `update_made`), `/api/makeit*` in `server.py`, `tabs.py` (`printing`, `pack_it`), `<section id="t-makeit">` in `index.html`; `tests/test_farm.py` |
| Slicing tab (Bambuddy profiles and print defaults per printer) | `farm.py` (`bambuddy_presets`, `chosen_presets`, `set_presets`, `missing_presets`, `target`, `print_defaults`, `forget_local_slicing`), `/api/makeit/presets` `/print-defaults`; `<section id="t-slicing">`; `tests/test_farm.py` |
| Printers page, sidebar status | `farm.everything` / `summarize`, `/api/printer-status`, `/api/health`; `<section id="t-printers">`, `loadHealth` in `index.html` |
| Settings sections (Setup · Product · Plates · Folders · Connections · Maintenance · Usage) | `data-sec` blocks + `setSec` in `index.html`; checklist from `/api/setup` |
| Bambuddy computer run from SnapSTL — external services: Bambuddy, Docker, slicer, OctoEverywhere (status, Start / Restart / Restart all, Keep running) | `bambuddy_host.py`, `platform_os` (services, Docker Desktop, OctoEverywhere Plugin Manager), `/api/bambuddy-host*` in `server.py`, `#hostcard` in `index.html`; `tests/test_bambuddy_host.py` |
| Bambuddy computer (Docker: Bambuddy + slicer sidecars) | `scripts/windows/bambuddy-server.ps1`, `scripts/bambuddy-docker/docker-compose.yml`, `scripts/bambuddy-version.txt` |
| SnapSTL installers and Bambuddy (`SNAPSTL_BAMBUDDY`, `SNAPSTL_BAMBUDDY_URL`) | `scripts/windows/common.ps1` (`Install-Bambuddy`, `Connect-Bambuddy`), `scripts/make_app.sh` (Mac: connect only); older native install (Windows): `scripts/windows/bambuddy.ps1`, `platform_os.update_bambuddy` |
| Sidebar "This computer" address | `server._network` (in `/api/health`), `platform_os.lan_ips`, `#hip` in `index.html` |
| Report status timestamps | `ingest.status_stamp`, `ingest.file_as_of` (Mark printed / `set_printed` stays only for old data, no button) |
| Logistics settings (tracking link, red limits, messages), Usage screen | `settings.LogisticsSettings`, `data-sec="logistics"` / `"usage"` + `lgMsgs` / `loadUsage` in `index.html` |

## 4. How to work

```bash
uv sync                     # Python 3.12 deps via uv
uv run pytest -q            # all tests must pass before every commit
```

- Add or update a test in `tests/` for every logic change. Engine tests use synthetic meshes
  (`tests/conftest.py`); `tests/test_flow.py` plays the helper over HTTP — tests never contact MakerWorld.
- Try changes without touching the owner's data:
  `SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320`
- Apply to the running app: `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`
  (UI-only changes just need a page reload). Logs: `~/Library/Logs/SnapSTL.log`.
- Never restart the service while a batch is downloading or building unless the owner agrees
  (interrupted names are re-queued, but it's disruptive).

## 5. Git workflow

`main` is the stable branch the owner runs.

```bash
git status                                   # start clean
git switch -c change/<short-name>            # one branch per change request
# edit → uv run pytest -q → update docs
git add -A && git commit -m "<what changed and why>"
git switch main && git merge --no-ff change/<short-name> && git branch -d change/<short-name>
```

- Small, focused commits with plain-English messages ("Batches: show folder name in table").
- Never commit failing tests. Never rewrite `main`'s history (no force-push, no rebase of `main`).
- Undo with `git revert <commit>`, not `git reset --hard`.
- Handing the project on: commit everything, then copy the folder **including `.git`**
  (`git clone /path/to/Snapstl SnapSTL-copy`) so history travels with it. Leave out `.venv/`,
  customer reports and local data. The owner zips it themselves.

## 6. Keep the docs true (same commit as the change)

- `docs/ARCHITECTURE.md` — repository map (§4), data model (§5), states, API table (§11), settings
  (§12), decision log (§16) when a decision changes.
- `README.md` and the in-app **Help** page — when the owner's steps or the output change.
- `AGENTS.md` — when rules or workflow change.
- `documentation.md` — at the end of every session with the owner that explains, clarifies or decides
  something (not only code changes):
  - Add a short dated entry under **Log**: what was asked, what was found, what was decided, what is
    still open. A few bullets, not a transcript; newest at the bottom.
  - Keep **Current understanding** true: webstore status meanings, how the report is read, app states
    and Orders tabs. Change it in the same commit as code that changes any of them.
  - Mark open questions **Open** and resolve them in a later entry when the owner answers. Never
    assume a status meaning the owner hasn't confirmed: ask.
  - The repository is public: counts, column and status names only, never customer names, phones,
    addresses or order IDs.

Orders pagination includes a Page selector to jump directly to any page. Logistics uses the same control. Logistics hosting is available on both Mac and Windows; Settings reports whether port 8002 is actually listening. Allow incoming connections in the host firewall for LAN access. The UTF-8 interface is decoded explicitly on Windows.
