# SnapSTL — Architecture, Tech Spec & Decisions

**Source of truth** for how SnapSTL works, where everything lives, and which decisions are
binding. Read it fully before changing code; update it in the same commit as any behaviour change
(see `AGENTS.md`). Business scaling spec: `docs/scaling-rules-v1.1-handoff.md`.

Contents: 1 Product · 2 Runtime · 3 Tech stack · 4 Repository map · 5 Data model · 6 Orders &
ingest · 7 MakerWorld & the helper · 8 Batches & queue · 9 Scaling · 10 Plates & output ·
11 HTTP API · 12 Settings · 13 Security & privacy · 14 Maintenance · 15 Testing ·
16 Decision log · 17 Known limits · 18 How to extend safely

---

## 1. Product

Snap Magicians sells personalised **name desk organizers** (webstore SKU `Magicians_35`). Each order
is one organizer **base** (printed white, has compartments) plus a separate **name** piece (printed
in the customer's colour), glued together after printing.

```
Webstore "Order Reconciliation Report" (.xlsx)
  → keep SKU Magicians_35, read name/colour/customer, de-duplicate by Order ID    ingest.py, db.py
  → review odd names; select orders → Create batch                                 server.py, index.html
  → Queue download: each name generated on MakerWorld by the SnapSTL Helper        helper/, batches.py
  → Queue build: scale (Rules v1.1) → group 3 per plate → pack on 256×256 bed       scaling, grouping, packing
  → <output>/<date>/Batch N - <date>/{Bases/, Names/<colour>/, report.csv} (.3mf)  pipeline.py, export.py
```

---

## 2. Runtime (each computer, Mac or Windows; optional shared order records)

| Piece | What / where |
|---|---|
| **Service** | `snapstl-serve` → FastAPI/uvicorn on **http://127.0.0.1:8317** (`SNAPSTL_PORT` to change); always localhost; optional Mac/Windows logistics uses `0.0.0.0:8002` (see §13). Started at login by launchd: `~/Library/LaunchAgents/local.snapmagicians.snapstl.plist` (`RunAtLoad`, `KeepAlive`). Hosts the portal, the helper API, the download queue and the batch builder thread. Runs `caffeinate -i` while alive. |
| **App window** | `~/Applications/SnapSTL.app` → `snapstl` → pywebview window on the same URL (adds native folder picker; text selection on). UI = side navigation: Orders · Batches · Downloads · Make It · Slicing · Printer status · Settings · Help. If the service isn't up it kickstarts it. Closing the window doesn't stop the service. |
| **SnapSTL Helper** | Chrome extension (`helper/`, *Load unpacked*) in the user's **normal** Chrome, on the MakerWorld model-maker tab. Generates each queued name on the real page and uploads the files. |
| **Data** | `~/Library/Application Support/SnapSTL/` (`APP_DIR`, override with `SNAPSTL_HOME`): `snapstl.db`, `settings.json`, `backups/`, `auth.json` (only if a portal password is set). |
| **Downloaded models** | Settings → Downloads folder (default **`~/SnapSTL/Downloads`**): one readable folder per model, `<Name> - <N> comp - <cache key>/{base.stl, name.stl, info.json}`. **Never deleted by SnapSTL** — the user cleans up in Finder; a deleted model is re-downloaded when a batch needs it. Models from the old hidden `APP_DIR/cache/` are moved here once at startup (`cache.migrate_old_cache`). |
| **Secrets** | MakerWorld license password **per model** (by designId) in `settings.json` (mode 600), editable in Settings; portal password hash in `auth.json`. Never written to downloaded model folders, logs or git. |
| **Output** | Settings → output folder (default `~/SnapSTL/output`). |
| **Log** | Mac `~/Library/Logs/SnapSTL.log` · Windows `%LOCALAPPDATA%\SnapSTL\SnapSTL.log` |
| **Windows** | Same, with `APP_DIR = %LOCALAPPDATA%\SnapSTL`, a Task Scheduler task "SnapSTL" instead of launchd and Start-menu shortcuts instead of the `.app` (see *Windows* below). |
| **Bambuddy (print farm)** | Open-source print-farm manager ([maziggy/bambuddy](https://github.com/maziggy/bambuddy), AGPL-3.0), since 0.22 runs on **one Bambuddy computer** for every SnapSTL computer: **natively** (Windows: Bambuddy's own installer `bambuddy-X-windows-x64-setup.exe`, run silently by `scripts/windows/bambuddy-server.ps1` — a Windows service on 0.0.0.0:8000 from boot, data in `C:\ProgramData\Bambuddy`, so SSDP printer discovery works), with **only its slicer sidecars in Docker** (Bambu Studio + OrcaSlicer, `scripts/bambuddy-docker/docker-compose.yml`, 127.0.0.1:3001 / 3003; Docker Desktop + WSL 2 installed by the script when missing). The script points Bambuddy at `http://localhost:3001` / `:3003` with *Use Slicer API* while its login is off, opens TCP 8000 + UDP 2021 for the local subnet, prints `http://<ip>:8000` and remembers its choices in `C:\ProgramData\SnapSTL-Bambuddy\setup.json`: a re-run only applies settings (installs / downloads only what's missing, or with `BAMBUDDY_UPGRADE=1` to the pinned version). Bambuddy scripts are Windows only (0.22.3; the Mac ones were removed): a Mac runs SnapSTL and connects. SnapSTL connects with that address (Settings → Connections, or `SNAPSTL_BAMBUDDY_URL` at install). The older install next to SnapSTL (`~/SnapSTL-Bambuddy`, http://127.0.0.1:8000) is made with `SNAPSTL_BAMBUDDY=native` (Windows) and keeps being updated on Windows; a Mac's old copy is left as it is. |
| **Shared order records** | Optional Supabase project connected in Settings (connection string in `APP_DIR/shared-records.json`, mode 600): batched / bundled / printed orders of every computer, add-and-update only (see *Shared order records* below). |

Installed by `scripts/make_app.sh` on a Mac (installs `uv` if missing, `uv sync --no-dev`, writes the
plist and the `.app`) or `scripts/windows/install.ps1` on Windows. The app runs **from the project
folder** (a git clone, updated in-app) — moving it means re-running the installer.

---

## 3. Tech stack (exact versions pinned in `uv.lock`)

| Area | Choice | Version |
|---|---|---|
| Language / env | Python, managed by **uv** (`.python-version` = 3.12) | 3.12.x |
| Web server | FastAPI + uvicorn[standard] | 0.162.7 / 0.54.0 |
| Uploads | python-multipart | 0.0.33 |
| UI | One static page, vanilla JS + CSS, no build step (`static/index.html`) | — |
| Window | pywebview (Cocoa / WKWebView) | 6.2.1 |
| Meshes | trimesh (OBJ parsing done in-house in `meshes.py`) | 5.2.1 |
| 2-D geometry | shapely (footprints, collisions) | 2.2.0 |
| Numerics | numpy | 2.5.4 |
| Spreadsheets | python-calamine (the webstore XLSX has a stylesheet openpyxl rejects) | 0.9.1 |
| Storage | SQLite (WAL) via stdlib `sqlite3` | — |
| Plate format | **3MF core spec**, own writer/reader (`threemf.py`, stdlib zip/xml), plus a names-only `Metadata/model_settings.config` (+ `Slic3r_PE_model.config`) because Bambu Studio ignores the core object name | — |
| Browser side | Chrome extension, Manifest V3, vanilla JS | helper 2.0.1 |
| Tests | pytest, httpx (FastAPI TestClient), mapbox-earcut (test meshes) | 9.2.0 / 0.28.1 / 2.1.0 |
| Packaging | `uv_build` backend; console scripts `snapstl`, `snapstl-serve`, `snapstl-admin` | — |

No other runtime dependencies. Don't add Playwright/Selenium, ORMs, JS frameworks or build tools
without the owner asking (§16 explains why browser automation was removed).

---

## 4. Repository map

```
Snapstl/
├── AGENTS.md                 rules + workflow for AI agents (read first)
├── CLAUDE.md                 points Claude to AGENTS.md
├── README.md                 owner-facing install / daily use / troubleshooting
├── docs/
│   ├── ARCHITECTURE.md       this file
│   ├── parametric-model-pipeline.md   local organizer reference survey and CAD workflow proposal
│   └── scaling-rules-v1.1-handoff.md   business spec for scaling (historical source)
├── pyproject.toml            deps, console scripts            uv.lock      exact versions
├── .python-version           3.12                             .gitignore   excludes data/reports/output
├── .gitattributes            .sh stays LF, .ps1 CRLF
├── scripts/
│   ├── make_app.sh           Mac installer: uv, deps, launchd service, ~/Applications/SnapSTL.app
│   ├── update.sh             Mac: git pull + uv sync + restart (same as Settings → Updates) + Bambuddy
│   ├── bambuddy-docker/      docker-compose.yml (slicer sidecars only) used by both bambuddy-server scripts
│   ├── bambuddy-version.txt  the Bambuddy release this SnapSTL is tested with (both installers read it)
│   └── windows/              install / run-service / update / restart / uninstall (.ps1) + common.ps1;
│                             bambuddy-server.ps1 (the Bambuddy computer: Bambuddy's Windows installer, WSL 2,
│                             Docker Desktop + slicer sidecars, firewall, address, OctoEverywhere Plugin
│                             Manager from the Microsoft Store); bambuddy.ps1 + run-bambuddy.ps1 (older native install)
├── helper/                   SnapSTL Helper — Chrome extension (Manifest V3)
│   ├── manifest.json         permissions: makerworld.com + http://127.0.0.1:8317 only; version
│   ├── background.js         service worker: relays tab ↔ app (/api/helper/*); working tab; page check
│   ├── content.js            on makerworld.com: works only in the working tab on the configured model page
│   ├── inject.js             page world: copies plate_1/plate_2 .obj responses (read-only)
│   └── popup.html, popup.js  app status, model designId, working tab on/off the page; "Open model page", "Use this tab"
├── src/snapstl/
│   ├── __init__.py           entry points: main() (window), serve() (service); port, keep awake, folder picker
│   ├── __main__.py           `python -m snapstl` (Windows shortcuts)
│   ├── config.py             shared-records connection string (entered in Settings; env override for tests)
│   ├── platform_os.py        every Mac/Windows difference (folders, Finder/Explorer, keep awake, restart, LAN name,
│   │                         Windows services + Docker Desktop for the Bambuddy computer)
│   ├── bambuddy_host.py      the Bambuddy computer from SnapSTL (Windows): status, Start / Restart Bambuddy's service,
│   │                         Docker Desktop, the slicer containers and OctoEverywhere's plugin containers,
│   │                         Restart all, Keep running (starts what stopped, every minute)
│   ├── computer.py           this computer's id + name (on batches and in the shared records)
│   ├── sync.py               shared order records in Supabase: claim, push, pull, merge, background loop
│   ├── report.py             the rest of each Magicians_35 report row (typed) → snapstl.report_rows / orders_full
│   ├── shop.py               the `shop` schema: every product's report rows, tidied in SQL, views for questions
│   ├── farm.py               print farm: Bambuddy client (stdlib HTTP), printers + their job (farm.json),
│   │                         slicing with Bambuddy's slicer only, print jobs + background worker
│   ├── makeit.py             Print tab: Bases / Names queues across batches, printer pre-selection,
│   │                         Start print, Completed + Reprint, Ready to ship (orders.made_at)
│   ├── dbtool.py             `snapstl-db check | sync | export | schema`
│   ├── updater.py            Settings → Updates (git fetch / pull --ff-only / uv sync)
│   ├── watermark.py, render.py   watermark detection; plate / object pictures
│   ├── logistics.py          optional Mac/Windows port-8002 listener: the logistics page (READS / WRITES)
│   ├── logistics_api.py      logistics page: lists, search, order panel, its actions, usage (0.23)
│   ├── tabs.py               the Orders tab rule (Python + SQL), 0.23
│   ├── server.py             FastAPI app: every HTTP endpoint (§11), access control (§13), tab/filter SQL
│   ├── settings.py           dataclasses + defaults (§12); APP_DIR, CACHE_DIR, output path, MakerWorld URL
│   ├── db.py                 local SQLite schema + MIGRATIONS + sync triggers, tx(), quiet(), batch helpers
│   ├── ingest.py             report parsing (XLSX/CSV), name cleaning, upsert by Order ID, review edits, ad-hoc orders
│   ├── batches.py            batch lifecycle, download queue, helper claim/result/fail, builder thread
│   ├── cache.py              downloaded models in the visible Downloads folder (readable names, never auto-deleted);
│   │                         key = name + options + compartments + license hash; one-time move from old cache
│   ├── meshes.py             OBJ → trimesh, dims, to_origin
│   ├── scaling.py            Scaling Rules v1.1 (pick_band, scale_pair)
│   ├── grouping.py           names by colour 3→2→1; bases 3 per plate (colour groups first)
│   ├── packing.py            2-D footprint packing, Z rotation only, gap/margin verification
│   ├── pipeline.py           build(): scale → group → pack → write; dated "Batch N - date" folders
│   ├── export.py             file names {name,color},…; write + verify .3mf; previews; report.csv
│   ├── threemf.py            core-spec 3MF writer + reader
│   ├── orders.py             `Order` dataclass (one organizer copy inside a batch)
│   ├── maintenance.py        backups, clean-up (keeps made orders), reset
│   ├── auth.py               portal password (other computers) + signed session cookie; `snapstl-admin`
│   └── static/
│       ├── index.html        the whole UI: side navigation (Orders, Batches, Downloads, Settings, Help)
│       └── login.html        used only if a portal password is set
└── tests/
    ├── conftest.py           temp SNAPSTL_HOME, synthetic base/name meshes
    ├── test_engine.py        scaling, packing, grouping, file names, 3MF, per-day folders
    ├── test_ingest.py        report filtering, de-dup, review edits, name cleaning
    ├── test_flow.py          HTTP end to end: batch → helper → build; tabs/filters; printed; maintenance; guard
    ├── test_farm.py          print farm + Print against a fake Bambuddy, its slicer included (HTTP layer)
    └── test_sync.py          two simulated computers on a throwaway Postgres (skipped without one)
```

Not in git (by design; the repository is public): `.venv/`, any secret, customer reports (`*.xlsx`, `*.csv`), `output/`, ZIPs,
anything in `APP_DIR`.

---

## 5. Data model (`db.py`, SQLite at `APP_DIR/snapstl.db`)

New columns are added **only** through `MIGRATIONS` (applied on every connect). Never drop or rename
columns — the owner's order history lives here.

**orders** — one row per webstore Order ID

| Column | Notes |
|---|---|
| `order_id` (PK) | webstore "Order ID" (unique per line) — the de-duplication key |
| `clubbed_id` | webstore "Clubbed Order ID", shared by several lines of one customer order |
| `sku`, `order_date` (ISO), `source_status`, `quantity` | from the report |
| `customer_name`, `phone`, `address` | from the report (address = address, city, state, pincode) |
| `raw_name`, `raw_color`, `special_request` | as typed in *Product Customisation* |
| `name`, `color`, `name_edited` | what gets printed; colour lower-cased, gray → grey |
| `review_reason` | why it needs a human (NULL = fine) |
| `state` | §6: ready · needs_review · in_batch · bundled · **printed** · skipped · not_needed |
| `batch_id`, `last_error` | this computer's batch (NULL when made on another computer); last download error |
| `first_file`, `last_file`, `first_seen`, `updated_at` | provenance |
| `state_at` | when `state` last changed (trigger) |
| `status_at` | how recent `source_status` is: `@newest entry in the file|event date|upload time` (`ingest.status_stamp`; before 0.22.21 `event|file|upload`, rewritten once by `db.upgrade_stamp`) |
| `in_remote`, `batch_uid`, `batch_label`, `batch_computer`, `batch_computer_id`, `bundled_at`, `printed_at` | shared order records: its batch (also on other computers), when bundled / printed |
| `made_at` | base and name both printed in Print (`makeit.update_made`) → tab **Ready to ship** (`state='bundled' AND made_at IS NOT NULL`; **Bundled** = not made yet). Set by the batch's computer, mirrored on the others through the shared records (0.22.21) |
| `first_status` | webstore status when SnapSTL first saw the order (0.23; older orders filled once: PRINT if listed to make, else their status). First seen past PRINT = history, never flagged |
| `print_started_at` | first time a plate holding the order was started (0.23) → tab *Printing*. Like `made_at`: set by the batch's computer, mirrored on the others |
| field groups (`db.FIELD_GROUPS`, 0.23) | things people set, each group with its own `<group>_changed` time; the newest change of a group wins on every computer, whatever the state: **label** (`label_at`, `label_by`: Label Generated), **express** (`express_by` date, `express_note`), **flag** (`flag` ∈ `db.FLAGS`, `flag_note`: Problems – check), **name_note** (`name_change_reason`, `name_confirmed_at`), **remake** (`remake_at`, `remake_reason`), **reuse** (`reuse_of`, `reused_by`: returned RTO piece). Written only through `db.set_group` |

**order_notes** (0.23) — call notes: `uid`, `order_id`, `at`, `text`, `author`, `sent`. Append-only,
shared (`snapstl.order_notes`). **usage_counts** — `day`, `feature`, `count`, `sent`: feature use on
this computer (`db.count_usage`), shared per computer (`snapstl.usage_counts`, read by `sync.usage`).

**batches** — `id`, `created_at`, `status` (§8), `order_ids` (JSON), `settings` (JSON snapshot at
creation), `out_dir`, `summary` (JSON: plates + previews, errors, warnings, `folder`, `label`),
`finished_at`, `computer_id`, `computer_name`, `progress`, `uid` (id in the shared records),
`synced`.

**sync_outbox / sync_state / sync_flags** — orders waiting to be sent to the shared records, the
last pull time, and the "quiet" flag for writes that came from the records (see *Shared order
records*).

**downloads** — the helper queue: `cache_key`, `name`, `compartments`, `model_json`, `state`
(`queued | claimed | done | failed | cancelled`), `attempts`, `claimed_at`, `error`, `batch_id` (batch
that queued it), timestamps.

**files** — each report upload: `filename`, `sha256`, `ingested_at`, `summary` (JSON).

**print_jobs** — one row per plate pushed to a printer (`farm.py`): `batch_id`, `plate` (path in the
batch folder), `kind`, `color`, `printer_id` / `printer_name` (Bambuddy's), `state` (`queued | slicing
| uploading | sent | printing | done | failed | cancelled`), `sliced` (file used), `file_id` /
`queue_item_id` (Bambuddy's), `progress`, `error`, `options` (JSON print options, sent with the queue item), `held` (1 = waiting in Bambuddy for Start),
`autostart` (1 = made by Print → Start print: released right after it's queued), state `reprint` (a
person sent a printed plate back to the queue; no printer yet),
timestamps, `computer_id`. Local only (never in the
shared records); Clean up removes finished ones, Reset empties it.

---

## 6. Orders & ingest (`ingest.py`)

* Header row found by `Order ID, Order Status, SKU Code, Product Customisation` (first 30 rows).
* Only `SKU Code == Magicians_35` (case-insensitive). Customisation lines parsed as
  `Enter Your First Name:`, `Color:`, `Special Requests:`. **The customer's "Number of
  Compartments" is ignored** — compartments come from the scaling table.
* **Upsert by Order ID**: re-uploads never duplicate; later reports refresh status, quantity, clubbed
  ID and customer fields. A `bundled` or `skipped` order is never moved back by a report.
* Quantity N → N organizers in a batch.
* **Ad-hoc orders** (`add_adhoc`, `POST /api/orders/adhoc`, Orders → **Add ad-hoc order**): typed in
  by hand with only the name to print, mobile and colour (plus optional quantity and note). Stored in
  the same `orders` table with `order_id = AH-YYYYMMDD-HHMMSS-xxxx`, `sku = 'Adhoc'`,
  `source_status = 'PRINT'`, `order_date` = today, `first_seen` = when added, `customer_name` = the
  name typed, note → `special_request`; address, clubbed ID and report file stay empty. They go
  straight to `ready` (the person typed the name, so no review) and from there are batched, printed,
  marked printed and shared exactly like report orders. No report ever touches them (their IDs can't
  match a webstore Order ID).

Name cleaning (`clean_name`): emoji/symbols dropped silently; other non-letters removed → review;
first letter small (e.g. `riya`, 0.23.1; printed as typed) → review; spaces → review; non-Latin letters → review; more than 15 letters → review. A missing name/colour
or a special request → review.

| State | Set when | UI tab |
|---|---|---|
| `ready` | report `PRINT`, clean; or added by hand | **Ready** → sub-tabs **Single** / **Clubbed** / **Ad-hoc** |
| `needs_review` | `PRINT` with a review reason | Needs review (edit + Approve) |
| `in_batch` | selected into an unfinished batch | In a batch |
| `bundled` | in a finished batch | Bundled (Remake → back to ready) |
| `skipped` | user pressed Skip | Skipped (Make anyway) |
| `not_needed` | report status isn't `PRINT` | **Delivered** / **RTO** (`RTO…`) / **In Transit** / **Cancelled** (`CANCEL…`) / **Others** |

*Single* = the order's clubbed ID has only this line (ad-hoc orders excluded); *Clubbed* = one clubbed ID with
several lines; *Ad-hoc* = `sku = 'Adhoc'`.

**New Orders tabs (0.23, `tabs.py`)** — one rule, written once as Python (`tabs.tab_of`) and once as SQL
(`tabs.case_sql`, used for SQLite and by Supabase's `set_tab()`); `tests/test_tabs.py` checks they
always agree. First match wins: skipped → **problems** (any `flag`) → **cancelled** (CANCEL…) →
**delivered** / **rto** / **on_the_way** (IN-TRANSIT) → **ship_it** (`label_at`, or PACK / HANDOVER) →
**pack_it** (`made_at`, state `printed`, or `reuse_of`) → **printing** (in a batch + `print_started_at`)
→ **ready_to_print** (in a batch) → **check_name** (`needs_review`) → **to_make** (`ready`) → else
**problems**. Report statuses 4–7 are ignored while a remake is pending (`remake_at` newer than
`label_at`). The Orders page (`server.TABS`, 0.23 phase 3) uses the same rule: one key per tab plus
`to_make_single` / `_clubbed` / `_adhoc` / `_returned` (a returned RTO piece with exactly the same name,
case-sensitive, and colour, not used yet), `express` and `all`; rows sorted Express first
(`server._ORDER`). Only `to_make` orders can be ticked / batched (`batches.create_batch` also skips
flagged and reused ones). Mark printed / Back to Bundled buttons are gone (the endpoint stays for old
data).

**Report rules (0.23, `ingest.status_state` / `_status_flags`; `sync.merge` uses `status_state` too)**:
a report never moves an order backwards. Ready / needs review stay to make on PRINT and AWB EXPIRED;
not needed goes back to making only on PRINT and never after PACK / HANDOVER / IN-TRANSIT /
DELIVERED / RTO / AWB EXPIRED. Flags (`db.set_group(..., "flag")`, only by the uploading computer):
`past_print_not_made` (listed to make or in a batch, not made, report moves past PRINT; not for
history or ad-hoc), `cancelled_in_production` (cancelled while in a batch or made, before it shipped),
`back_to_print` (packed outside SnapSTL, then AWB EXPIRED / PRINT). AWB EXPIRED on a made / in-batch /
labelled order clears `label_at` with `label_note` "Label expired – make a new label" → pack_it.
Printing: `makeit.update_made` also sets `print_started_at` for orders on any plate with a print job
(not Removed); called from Start print; `makeit.backfill_started` once at startup (`started_v1`).
The Orders table offers browser-local saved views (column visibility, tab and filters) and a selectable page size; these preferences do not change order records. Tab SQL lives in `server.TABS`; column filters in `server.FILTERS` (text = contains; status, colour,
qty = exact; combined with AND; also used by *Select all matching* and *Export CSV*).

---

## 7. MakerWorld & the SnapSTL Helper (`helper/`, `batches.py`)

Model: **Custom Name Desk Organizer v2.34** (`designId=2600234`) on MakerWorld's Parametric Model
Maker. The link is a setting (`makerworld_url`, default `settings.MAKERWORLD_URL`); `settings.design_id()`
validates it (https://makerworld.com…/makerlab/parametricModelMaker?designId=N). A different model must
have the same parameters. Download keys include the designId for any model other than 2600234, so
models never share downloads (existing 2600234 keys unchanged).

| Parameter | Value |
|---|---|
| Print version | **always `NO AMS`** → base and name come out as separate plates |
| Name line 1 / 2 | the order's name / always empty (single line only) |
| Font, Font size 30–120, Thickness 30–120, Outline 4–15, Rotate text | Settings, same for every name |
| Compartments 1–10 | scaling table, by letter count |
| License password | Settings, **per model** (`license_passwords[designId]`); **required to download** (`NO_PASSWORD`); a batch takes the current passwords whenever a download is queued (`_with_current_passwords`); sent to the helper with each job; removes the watermark |

NO AMS output: `plate_1.obj` = base (upright: X length, Y depth, Z height); `plate_2.obj` = name
(flat: X length, Y letter height, Z 4 mm). The app tells them apart **by height**, not by order.

**Working tab & page check.** The content script loads on every `makerworld.com` page (the site changes
pages without reloading). Each loop it fetches `/api/helper/config` (model link + designId) and checks
the tab is on exactly that model page. Only the **working tab** works: the one chosen with **Use this
tab** in the popup, or else the first tab found on the model page. If the working tab is elsewhere it
reports `on_page=false` and gets no work (`/api/helper/next?on_page=0` always returns no job); the app
sidebar shows "Helper tab is not on the model page". A name interrupted by navigating away is released
back to the queue.

**Helper protocol** (working tab, one name at a time):

```
content.js ──message──► background.js ──HTTP──► app (127.0.0.1:8317)
  GET  /api/helper/config          → {model_url, design_id}
  GET  /api/helper/next?status=…&on_page=1  → {job: {job_id, name, compartments, params, license_password, pause_after_s} | null, paused}
  fill form (XPath on labels; native value setter for React inputs; MUI menus via mousedown) → click Generate
  inject.js copies plate_1/plate_2 .obj → POST /api/helper/jobs/{id}/result   (multipart plate_1, plate_2, check)
  error            → POST /api/helper/jobs/{id}/fail {error}       (3 tries, then failed)
  reload mid-name  → POST /api/helper/jobs/{id}/release            (back to queue, attempt not counted)
  status           → POST /api/helper/ping {status, on_page}       (every ~5 s while generating; popup sends {probe:true})
```

**License password confirmation (helper 2.2.1).** The helper refuses a job without a password. It sets
the password first and again last, waits 800 ms, re-sets it until the box holds it, and never presses
Generate unless the box holds it right then. Whether the box still holds it just after the click is
recorded as `kept_after` but not used to reject: MakerWorld clears or redraws the box once Generate
starts (2.2.0 rejected on it and refused good, watermark-free downloads). `inject.js` passively watches the page's own
non-GET request bodies during the job (never changes them) so content.js can tell whether the password
was sent (`request`: sent / missing / unknown — "missing" only when a body carried the name but no body
carried the password). The result carries `check = {"password_set": bool, "kept_after": bool, "request": …, "helper": ver}`
(no password inside). No `check` at all = a helper older than 2.2 → rejected with "reload the helper". `batches.check_license` rejects (not saved, retried) unless `password_set` is true
and `request` isn't "missing". Then `watermark.detect(base)` scans the base: a watermark → rejected,
`downloads.watermark=1`, retried. Saved models get `"watermark": false, "license_check": …` in
`info.json`. Rejections are `batches.Rejected` (a `ValueError`) → `store_failure`.

**Watermark (`watermark.py`).** Measured on real 2600234 downloads: raised "SALE NOT PERMITTED" text on
the base's back face (+Y), 1 mm proud of the main back wall, ~91 × 6 mm, z 2.7–8.7, centred; none on
names. Detection: the +Y plane with the most area is the wall; ≥ 20 mm² of surface > 0.3 mm beyond it =
watermark. Works on unscaled and scaled bases (Base Y is always 100 %). Other models may watermark
differently — prevention above is the main guard; the scan is the backstop.

Security checks: the helper never touches them — it reports *Complete the MakerWorld security
check in this tab* and waits for the person. Claims expire after 6 min (`CLAIM_TIMEOUT_S`);
`Downloader.start()` re-queues claims left by a restart. The helper counts as *connected* if it
called in within 25 s or a name is being generated.

---

## 8. Batches & download queue (`batches.py`)

Batches **never start, download or build by themselves** — every step is a button (a manual queue):

| Status | Meaning | UI buttons |
|---|---|---|
| `created` ("Created — not queued") | orders reserved (`in_batch`) | **Queue download**, Cancel |
| `waiting` ("Downloading") | its missing models are in the download queue | Build now with what's ready, Cancel / Stop |
| `models_ready` ("Ready to build") | every model downloaded (or failed) | **Queue build**, Cancel / Stop |
| `build_requested` → `building` ("Queued to build") | builder thread, one batch at a time | — |
| `done` / `done_with_problems` / `failed` | finished | Open, Show in Finder, Download ZIP (on demand), **Re-download**, **Rebuild plates** |
| `cancelled` | never built and cancelled | — |

* `create_batch` → `created`. `start_download(force=False)` (**Queue download**) queues only models not
  in the Downloads folder; downloads are processed oldest first (de-duplicated by cache key).
* **Re-download** = `start_download(force=True)`: queues **every** model of a finished batch again; the
  new result overwrites the saved model folder. Progress counts models with a live download as not ready.
* **Rebuild plates** = `request_build` on a finished batch: builds again from the saved models into a
  **new** `Batch N - <date>` folder; the old folder stays (`summary.previous_folders`). No downloads.
* **Cancel** a never-built batch → `cancelled`, orders back to `ready`. **Stop** (same endpoint) on a
  batch that was built before (re-download in progress) → back to `done`, plates untouched.
* `Manager` thread: `waiting` with nothing pending → `models_ready`; it builds only `build_requested`.
* First build: orders with a missing model or a failed fit check go back to `ready`; complete → `bundled`.
* Downloads no active batch needs are cancelled automatically (startup and after each build/cancel) —
  except single-object re-downloads (`model_json._manual`) of a finished batch.
* **Objects list** (`batch_models`, `GET /api/batches/{id}/models`): every distinct object with download
  state, watermark and license check. **Re-download one** = `redownload_model` (`POST …/models/redownload
  {key}`): a built batch keeps its status; `request_build` refuses a Rebuild while any of the batch's
  models is queued/downloading, so the rebuild uses the fresh files.
* **Build-time watermark check:** `Manager._build` runs `watermark.detect` on every model's base; a hit
  is held back (error, order → Ready); `summary.watermark = {checked, found}`.
* **Pictures** (`render.py`, dependency-free height-map PNG; watermark faces red): base plates are drawn
  from the top at build (`APP_DIR/previews/batch-<id>/<Batch folder>/bases-N.png`, `PlateOut.image`,
  `GET /api/batches/{id}/plate-image?f=`); per object `GET /api/models/{key}/view.png?side=back|top`
  (cached in `APP_DIR/previews/models`). Names keep the SVG outline. Previews are never in the output.
* Each batch snapshots the settings at creation (model options, scaling, plate rules).

## 9. Scaling Rules v1.1 (`scaling.py`; defaults in `settings.default_bands`) — binding

| Letters | Agreed length | Agreed height | Default L × H (mm) | Compartments |
|---|---|---|---|---|
| 1–3 | 190–200 | 90 | 195 × 90 | 4 |
| 4 | 195–210 | 89–90 | 200 × 90 | 4 |
| 5 | 200–220 | 88–90 | 210 × 89 | 5 |
| 6–7 | 215–230 | 86–89 | 222 × 87.5 | 5 |
| 8–10 | 225–240 | 85–88 | 232 × 86.5 | 6 |
| 11+ | ≤ 240 | 85–88 | 240 × 86 | 6 |

```
Base X = L / original base X     Base Y = 100 %      Base Z = H / original base Z
Name X = Base X                  Name Y = Base Z     Name Z = 50 %   (4 mm → 2 mm)
```

Height 85–90 mm, length ≤ 240 mm. Warnings, never silent fixes: unusual orientation, name longer
or taller than base, letter distortion (X/Z scale ratio outside 0.6–1.6).

**Name ↔ base fit check (binding).** The name is glued onto its base, so its size *relative to the
base* must stay exactly MakerWorld's: `name length / base length` and `name height (Y) / base
height (Z)`. `scale_pair` builds the name scale from the base factors (`name_scale = (base_x,
base_z, 0.5)`), and `scaling.fit_metrics` / `fit_problems` prove it per order (tolerance
`FIT_TOL` = 0.2 %), twice:
1. **After scaling** (`pipeline.build`): a failing pair is **held back** — neither part is packed or
   written, the order returns to `ready`, the batch is *done with problems*.
2. **From the written files**: `export.write_plate` reads each .3mf back and returns every part's
   measured size (un-rotated); `pipeline.build` re-checks each pair from those sizes. A failure is an
   error and the order is **not** marked bundled.

`BuildResult.complete(idx)` is true only when both parts are written **and** the fit check is `ok`.
`report.csv` adds `name_vs_base_length_%`, `name_vs_base_height_%`, `fit_check`; the batch summary
adds `fit: {checked, ok}` and the UI shows "Fit check: N/N". Verified on the 8 real MakerWorld pairs:
proportions identical before/after (e.g. SONAL 94.67 % length, 84.74 % height). A name and its base
are always built in the same batch with the same settings snapshot. Verified on real output: "Ayra"
116.6 % / 93.5 % (handoff: 116.56 / 93.51); "SONAL" → base 210 × 51 × 89 mm, name 198.8 × 75.4 × 2 mm.

---

## 10. Plates & output (`grouping.py`, `packing.py`, `pipeline.py`, `export.py`, `threemf.py`)

* Bed 256 × 256 × 256 mm, 5 mm edge margin, 5 mm gap (Settings). Rotation **only about Z**
  (default 0°/180°); the build face never changes.
* **Names:** one colour per plate, 3 → 2 → 1 by fit; never scaled separately from their base.
* **Bases:** 3 per plate (only the last may have fewer): each colour's full groups of 3 first, then
  leftovers pooled into mixed plates.
* Packing uses real 2-D footprints (shapely): parts stack and slide back to exactly `gap` mm, so
  flipped names nest. Every layout is verified, and every written file is read back and re-checked
  (object count, triangle count, bounds, sitting on Z = 0).

Output — **no ZIP is ever written** (*Download ZIP* builds a temporary file on demand only):

```
<output>/2026-11-15/                          one folder per build date (YYYY-MM-DD)
    Batch 1 - 2026-11-15/                     N restarts at 1 each day (pipeline.new_batch_dir)
        Bases/Bases - Ayra + Manvi + Riya.3mf  (before 0.24: {Ayra,pink},{Manvi,pink},{Riya,blue}.3mf)
        Names/pink/Pink - Ayra + Manvi.3mf
        Names/blue/Blue - Riya.3mf            one folder per name colour
        Fixes/Fix - Pink - Riya (r).3mf       fix plates (Print → Verify, extras.py)
        Merged/Merged - Pink - Ayra + Tara.3mf  merged name plates (extras.py)
        report.csv                            sizes, scale % per axis, files, warnings, settings
    Batch 2 - 2026-11-15/
```

Each `.3mf` holds one plate; every part is its own object named like `Ayra - Name · NS0AB…`, in mm,
placed in bed coordinates. Core 3MF only (no Bambu project data), so Bambu Studio updates don't break
the files. Clashing file names get ` (2)`; names over 200 characters are shortened with a hash.

---

## 11. HTTP API (`server.py`)

| Area | Endpoints |
|---|---|
| Pages | `GET /`, `GET /login` |
| Auth (optional) | `POST /api/login`, `POST /api/logout`, `GET /api/auth-check` |
| State | `GET /api/meta`, `GET /api/state` (counts, downloader, queue, builder log) |
| Orders | `POST /api/ingest` (multipart `file`), `GET /api/orders`, `GET /api/orders/ids`, `GET /api/orders/facets`, `GET /api/orders.csv`, `POST /api/orders/adhoc` `{name,phone,color,quantity?,note?}`, `POST /api/orders/{id}/name` `{name,color}`, `POST /api/orders/{id}/reset` (Remake / Make anyway; records a remake when the webstore status would keep it elsewhere), `POST /api/orders/{id}/skip`, `POST /api/orders/{id}/clear-flag`, `POST /api/orders/{id}/use-returned` `{rto_order_id}`, `POST /api/orders/{id}/undo-returned` |
| Batches | `POST /api/batches` `{order_ids}`, `POST /api/batches/{id}/download` (`{force:true}` = Re-download), `POST /api/batches/{id}/build` (also Rebuild), `POST /api/batches/{id}/cancel`, `GET /api/batches`, `GET /api/batches/{id}`, `GET /api/batches/{id}/file?path=`, `POST /api/batches/{id}/reveal` (local only), `GET /api/batches/{id}/zip` |
| Downloads | `GET /api/downloads` (queue + Downloads folder stats), `POST /api/downloads/retry`, `POST /api/downloads/reveal` (local only), `POST /api/downloader/pause`, `POST /api/downloader/resume` |
| Helper | `GET /api/helper/config`, `GET /api/helper/next`, `POST /api/helper/ping`, `POST /api/helper/jobs/{id}/result`, `…/fail`, `…/release`, `GET /api/helper.zip` |
| Settings | `GET` / `PUT /api/settings` (includes `makerworld_url`, `license_passwords`), `POST /api/settings/reset` |
| Portal | `GET /api/portal` (on/off, password set, addresses), `POST /api/portal` `{enabled, password}` — this Mac only |
| Print farm | `GET /api/farm` (read only: Bambuddy status, printers with live state and filament per slot, recent jobs with printer choices; connects by itself on a fresh install), `POST /api/farm/connect` `{url, api_key}`, `GET /api/bambuddy-host` (this Windows Bambuddy computer: service, Docker, slicer containers; `here:false` elsewhere), `POST /api/bambuddy-host/start|restart-bambuddy|restart-slicer`, `POST /api/bambuddy-host/keep-running` `{on}`, `POST /api/farm/printers/{id}/clear-plate`, `POST /api/farm/refresh` (Refresh from Bambuddy) — `/api/farm/*` this computer only. Printers and filament are set up in Bambuddy, never in SnapSTL |
| Print (was Make It) | `GET /api/makeit` (Bases / Names plates still to print with printer choices + pre-selection, Completed, printers, options), `POST /api/makeit/start` `{batch_id, file, printer_id, tray, options, force}` (`tray` = filament slot) (Start print: send + start, or start / move a held one; 409 = Bambuddy's warning → `force`), `POST /api/makeit/reprint` `{batch_id, file}` (Completed → back to the queue), `GET` / `PUT /api/makeit/presets` (Bambuddy's slicer profiles / the chosen printer, process, filament as `source:id`), `GET /api/bambuddy-host` + `POST /api/bambuddy-host/{start, restart-bambuddy, restart-slicer, restart-octoeverywhere, restart-docker, restart-all, open-octoeverywhere, keep-running}` (Settings → external services), `GET /api/queues` (Print's Queues panel: `downloading`, `building`, `slicing`, `printing` — live first, then the last 15 finished — and `counts`; `makeit.queues`), `POST /api/makeit/slice` `{plates:[{batch_id, file, printer_id}], again}` (queue for Bambuddy's slicer with that printer's profiles; `printer_id` required, one slice at a time), `POST /api/makeit/remove` `{batch_id, file, put_back}`, `GET /api/makeit/presets?model=&refresh=1` (models, `set_for`, printers with their print defaults, every Bambuddy profile by tier + `status` of its cloud tiers, `error`, slicing activity; `refresh` = Bambuddy re-reads its cloud lists) / `PUT` `{model, printer, process, filament, clear}` (model required), `PUT /api/makeit/print-defaults` `{printer_id, speed, bed_levelling, flow_cali, timelapse, plate_type}`, `POST /api/makeit/test-slice` `{model}`, `GET /api/health` (incl. `network`: this computer's IP, name, portal URL, Bambuddy URL / whether it runs here / the address to share) (sidebar status: Bambuddy, slicer, printers online, models without presets; cached 10 s), `POST /api/makeit/presets/import` (multipart `file`, `kind`) (`PUT /api/makeit/settings` removed in 0.22.22); `POST /api/makeit/start` also takes `speed` (silent/standard/sport/ludicrous) |
| Push to queue (API only, no UI since 0.13) | `GET /api/batches/{id}/plates` (plates, sliced file, latest job, suggested printer, choices with why-not), `POST /api/batches/{id}/push` `{plates:[{file, printer_id}], again}`, `GET /api/print-jobs?batch_id=`, `POST /api/print-jobs/{id}/print` `{printer_id, options:{bed_levelling, flow_cali, timelapse}}` (Printers → Print jobs: change a waiting job, move one waiting in Bambuddy's queue, or print a failed/cancelled/printed plate again as a new job), `POST /api/print-jobs/{id}/start` `{force}` (the only way a print starts; 409 = Bambuddy's warning, e.g. filament short → `force`), `POST /api/print-jobs/{id}/retry`, `POST /api/print-jobs/{id}/cancel` |
| Printers | `GET /api/printer-status` (`?download=1` = JSON file): every printer's Bambuddy record, full live status and queue + `/system/info`, secrets redacted (`farm.redact`) |
| Maintenance | `GET /api/maintenance`, `POST /api/maintenance/backup`, `POST /api/maintenance/cleanup` `{older_than_days}`, `POST /api/maintenance/reset` `{confirm:"RESET", clear_cache}` |

Order list parameters: `tab` (`ready ready_single ready_clubbed needs_review in_batch bundled skipped
delivered rto in_transit cancelled others all`), filters `f_order f_date f_customer f_phone f_address
f_status f_name f_color f_qty`, `q`, `limit`, `offset`.

---

## 12. Settings (`settings.py` → `APP_DIR/settings.json`)

| Group | Field | Default |
|---|---|---|
| model | `Font` (Chewy, Luckiest Guy, Titan One, DynaPuff, Cherry Bomb One, Akaya Kanadaka) | Chewy |
| model | `Font_size` 30–120 / `Thickness` 30–120 / `Outline` 4–15 / `Rotate_text` | 60 / 50 / 6 / no |
| scaling | `bands` (table §9), `base_y_scale`, `name_z_scale`, `min_height`, `max_height`, `max_length` | 1.0, 0.5, 85, 90, 240 |
| plate | `bed_x/y/z`, `margin`, `gap`, `max_names_per_plate`, `bases_per_plate`, `rotations` | 256, 5, 5, 3, 3, [0, 180] |
| top level | `output_dir`, `downloads_dir`, `makerworld_url`, `license_passwords` (editable on this Mac only), `delay_between_downloads_s` | ~/SnapSTL/output, ~/SnapSTL/Downloads, MakerWorld model link, {}, 3 |
| top level | `lan_access` (changed only via `/api/portal`) | false |
| logistics (0.23, Settings → Logistics) | `tracking_url` (`https://`, with `{awb}` and/or `{order_id}`; empty = no button), `late_days`, `express_red_days`, `stuck_days` (0–60), `messages` (name → text, ≤ 20; `{customer}` `{order_id}` `{name}` `{colour}` `{tracking}` `{address}` `{needed_by}`; empty → defaults) | ClickPost link, 7, 3, 5, 5 messages |

Unknown keys are ignored on load, so older settings files keep working.

Print farm settings are separate: `APP_DIR/farm.json` (mode 600, `farm.py`): `bambuddy_url`
(default `http://127.0.0.1:8000`, env `SNAPSTL_BAMBUDDY_URL`), `api_key` (SnapSTL's Bambuddy key — never
sent to the UI), `keep_bambuddy_running` (Bambuddy computer: start what stopped, default on),
`presets_by_printer` (which Bambuddy profile each printer uses — references only:
`{printer_id: {printer|process|filament: {source, id, name}}}`; `presets_by_model`, the per-model picks
before 0.22.7, still applies to a printer with none of its own) and
`print_defaults` (`{printer_id: {speed, bed_levelling, flow_cali, timelapse, plate_type}}`), all set in the
Slicing tab. SnapSTL keeps no slicing settings (since 0.21 `forget_local_slicing` drops the old `slicer` /
`orca_presets_by_model` keys and `APP_DIR/slicer-presets` at start). **No built-in profiles**: a printer
without all three isn't sliced or printed
(`farm.missing_presets`). Settings → Defaults doesn't touch it. Printers, what they make and their
filament are not stored by SnapSTL — they're read from Bambuddy.

---

## 13. Security, privacy & other computers (`server._blocked`, `server.access_control`)

* **This Mac** (client 127.0.0.1/::1): no login. Host header must be `127.0.0.1`/`localhost`
  (DNS-rebinding guard).
* **Other websites** in any browser: rejected — a request's `Origin` must match its host.
  `chrome-extension://` origins are accepted **only** on `/api/helper/*` and only from this Mac.
* **Other computers:** the full app refuses every remote request. Optional Mac/Windows logistics
  hosting uses a separate listener on port 8002, password sign-in and an explicit order-read route
  allowlist (`logistics.py`). `lan_access` retains its stored name but now enables logistics only.
  Save starts/stops hosting immediately; Settings reports the actual listener status or bind error. No worker runs in the
  logistics listener; the host's normal shared-records sync supplies its local read data.
* Bambuddy listens on the owner's local network (port 8000; the Windows firewall rule allows the local
  subnet only). Its login is off by default — anyone on that network can use it; the README recommends
  turning it on and giving each SnapSTL an API key. The slicer sidecars have no login and are never
  published beyond 127.0.0.1. SnapSTL's Bambuddy API key lives only in `farm.json` (mode 600); the key
  SnapSTL makes for itself asks for cloud access (Bambu Cloud slicer profiles). SnapSTL never handles
  printer access codes. Plates (file names carry the printed first names) go only to the owner's
  Bambuddy on their network, never to a cloud print service.
* Customer name / phone / address are stored only in the local database (owner's request) — never sent
  anywhere, never in git. License passwords live only in `settings.json` (mode 600); `info.json` in
  model folders never contains them.

## 14. Maintenance (`maintenance.py`, Settings → Maintenance → Database)

**Back up now** (SQLite online backup → `backups/`, newest 20 kept). **Clean up** records older than
N days (not-needed orders, finished batches, download history, upload records, finished print jobs) — **bundled orders
are always kept** so printed orders can never come back as ready. **Reset** (type RESET). A backup is
taken first every time. **None of these delete files**: batch folders and downloaded models are only
ever removed by the user in Finder.

---

## 15. Testing

`uv run pytest -q` — ~40 tests, ~9 s, no network, temporary `SNAPSTL_HOME`. With
`SNAPSTL_TEST_DATABASE_URL=postgresql://…` (a throwaway Postgres; its `snapstl` schema is dropped) the
shared-record tests run too (`test_sync.py`: claims between two computers, bundled/printed/released,
newer vs older report, refused deletes, local reset + reload, offline = no batch, reset waits for
unsent changes):

* `test_engine.py` — scaling, fit check (pass, mismatch detected, failing pair held back), packing/nesting,
  grouping, file names, 3MF round trip, per-day folders
* `test_ingest.py` — SKU/status filtering, de-dup across reports, review edits, name cleaning
* `test_flow.py` — over HTTP: create → start download → helper → models_ready → build; failures and
  retries; pause/release/cancel; resume after restart; tabs/filters/customer fields; maintenance;
  cross-site guard; Rebuild (new folder, old kept); Re-download (+ Stop); readable Downloads folders;
  move from the old cache; reset keeps models; other computers refused until the portal is on, must sign
  in, and can never reach the helper / Finder / portal / reset

* `test_farm.py` (Print) — Bases pre-select the available base printer with most white left; Names
  pre-select only an available printer with the colour; nothing sent before Start print; Start print
  sends + starts with options; warning → held → force; done → Completed; base + name printed → Ready to
  ship (Bundled tab excludes it); Reprint → back to queue and out of Ready to ship; old batches'
  plate → order matching
* `test_config.py` — passwords with symbols are encoded (already-encoded left alone), schema file =
  `sync.schema_sql()`
* `test_farm.py` — every print waits held until Start (409 warning → force), Printers page has every
  field but no secrets, JSON download; Printers page connects (own API key, never shown) and reads printers
  and filament per AMS slot / external spool (colour names from Bambuddy's catalogue), no setup
  endpoints, clear plate; colour matching (exact / same family / none) and suggestions; Print button
  (move a queued job with options, print again as a new job, refusals); Push to queue suggestions and why-not, wrong printer
  refused, not sliced → clear error, hand-sliced file → Bambuddy library + queue, no double push,
  Bambuddy slicing (slicer not running → clear failure; sliced file queued, nothing written locally), pending → printing → printed, cancel, Bambuddy login
  on (pasted key), `/api/farm/*` this computer only, SD-card-safe file names

The helper is exercised by calling `/api/helper/*` directly — tests never contact MakerWorld. Bambuddy
is faked at `farm._request`; tests point `SNAPSTL_BAMBUDDY_URL` at a closed port so a real Bambuddy is
never touched.
Dev instance with its own data: `SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn
snapstl.server:app --port 8320`.

---

## 16. Decision log (don't redo what was ruled out)

| # | Decision | Why |
|---|---|---|
| 1 | Local Mac only; AWS Lightsail server removed | server unreliable; simpler to run and maintain |
| 2 | Token-based extension + server sync removed | setup and sync too fragile |
| 3 | Direct HTTP calls to MakerWorld's generate API — **don't work** | MakerWorld never answers generate requests that don't come from its page |
| 4 | App-controlled Chrome (Playwright) — **doesn't work** | Cloudflare keeps rejecting "Verify you are a human" in an automated browser |
| 5 | **Zero-setup helper in the user's own Chrome** (current) | a real browser passes Cloudflare normally; the app keeps all logic |
| 6 | No local CAD model generation for now | owner paused it (an original OpenSCAD design was proposed) |
| 7 | 3MF per plate instead of STL; no ZIP output | named parts, ~5× smaller; the owner zips by hand when needed |
| 8 | Customer contact fields stored locally | owner wants them in the Orders table |
| 9 | Batches: Create → Queue download → Queue build (nothing automatic) | owner wants explicit control |
| 10 | Output `<date>/Batch N - <date>/Bases, Names/<colour>` | owner's folder structure; N restarts daily |
| 11 | Name/base fit check, twice (after scaling + from written files); failing pairs never printed | owner must be sure names fit bases after printing |
| 12 | Side navigation (Orders, Batches, Downloads, Settings, Help) + in-app Help page | owner's request; Help explains the workflow for staff |
| 13 | Downloaded models in a visible folder (`~/SnapSTL/Downloads`), readable names, never auto-deleted | owner wants to see and clean them in Finder |
| 14 | Re-download / Rebuild for finished batches; Queue download / Queue build buttons | owner wants to redo batches and queue steps manually |
| 15 | Model link + license password per model in Settings (Keychain storage removed) | different models have different passwords; owner wants them editable |
| 16 | Helper works only in its working tab on the exact configured model page (designId) | it used to keep "working" after the tab moved to another MakerWorld page |
| 17 | Optional web portal for other computers (off by default, password, Mac-only actions) | staff can use SnapSTL from other computers; the main Mac runs service + helper |
| 18 | No license password → no download; helper must confirm the password at Generate; every download and build scans for the watermark | watermarked models slipped through when Generate was pressed before the page took the password; a watermark may differ per model, so prevention (password confirmed) comes first and the scan (`watermark.py`) is the backstop |
| 19 | Per-object Re-download in a batch's Objects list; Rebuild refused while a re-download is pending | fix one bad object without re-fetching the batch, and rebuild only from fresh files |
| 21 | Updates by git from the public repository (Settings → Updates, helper reloads itself); one-command installers `scripts/install-mac.sh`, `scripts/windows/install-windows.ps1` | sharing zips and reinstalling took too long |
| 22 | Each computer keeps its own local SQLite; Supabase holds only **order records** (batched/bundled/printed/released), add-and-update only, deletes refused by triggers; settings never shared | owner wants to own the order data, keep records for good, and reset a computer without losing them (0.7.0 used Supabase as the live database — replaced) |
| 23 | Creating a batch claims its orders in the shared records first; unreachable records = no batch | no order may be made twice across computers |
| 24 | A batch is made entirely on the computer that created it | its models, plates and files live there |
| 25 | Printed is a separate state set by a person (Bundled → Printed, undoable), keeping the batch | owner tracks what was actually printed |
| 26 | Report status updates are ordered by row event dates, then the report's date range end, then upload time | the report has no "generated at" time; an older report must never undo a newer status |
| 27 | Print farm through **Bambuddy** (open source, self-hosted), installed with SnapSTL at a pinned version, on 127.0.0.1 only; SnapSTL never talks to printers itself. Each plate is pushed to a printer the person picks (pre-filled suggestion); Bambuddy waits for "Clear plate" before every next print | owner wants plates to go to a chosen printer automatically without a cloud service (SimplyPrint/Printago would send plate files with customer first names off the computer; Bambu Farm Manager has no API) |
| 28 | Slicing: by hand in Bambu Studio (Export plate sliced file → `Sliced/…`) or Bambuddy's slicer service (Docker HTTP wrapper around the CLI). **Bambu Studio's macOS command line can't slice** — tested 02.05.00.66: it segfaults in `PartPlateList::generate_print_polygon` (calls the GUI `plater()` in CLI mode); the same code is in 2.6–2.9 | don't retry the local Bambu Studio CLI on a Mac |
| 29 | Printers page is read only: printers, filament and settings live in Bambuddy. Printers have no Bases / Names setting (0.15: the Location rule was removed — any printer prints either); colours come from the filament Bambuddy reports per AMS slot / external spool, named by its colour catalogue. A plate needs its colour loaded (bases: white); exact name before same family ("Hot Pink" for pink); wrong printer/colour refused | owner manages printers and filament in Bambuddy only, with no second place to keep in step (0.10.0 kept roles/colours in SnapSTL — replaced) |
| 30 | **No print starts by itself**: every plate goes to Bambuddy held (`manual_start`) and only a person's **Start** releases it; plates reach Bambuddy only on Push to queue / Print | owner wants every print clicked by a person |
| 31 | Printer status tab shows Bambuddy's raw data for every printer (record, live status, queue, system info), redacted, downloadable as JSON | owner wants all available information visible for future use |
| 32 | Shared-records passwords may contain symbols: `config.encode_password` percent-encodes the part between the user's ':' and the last '@'; schema also as `docs/supabase-schema.sql` (`snapstl-db schema`, test keeps them equal) | owner's Supabase password has symbols; tables can be made in the SQL Editor without giving SnapSTL the password |
| 33 | **Make It** replaces the Printers page and the batch's Push to queue panel: Bases / Names queues across all built batches (plates whose orders are still Bundled, or with a print on its way); pre-selection only of an *available* printer (connected, idle, bed cleared, none of our jobs on it), exact colour → most filament left; **Start print** (one click) sends and starts; printed → Completed (Reprint only after confirming a failure); both parts printed → order Ready to ship. Plates record `order_ids` (new builds; older matched by name/colour) | owner's workflow: one place per part, pick printer, click to print, see what can ship |
| 34 | **Slicing only by Bambuddy's slicer** (`POST /library/files/{id}/slice` with presets from Bambuddy's list, chosen in Make It → Slice); SnapSTL's own slicer settings and sidecar client removed. Slices run in the background one at a time, polled (`/slice-jobs/{id}`), stopped after 20 min without progress; Start print slices first if needed. Plates can be **Removed** from Make It and put back | owner wants one slicer (Bambuddy's), a Slice menu in SnapSTL, and no plate stuck "slicing" (the 0.12 client sliced inline on the worker thread, up to 15 min per plate, holding everything behind it) |
| 35 | Printer status page explains each printer in plain words (`farm.summarize`) + slicing status; raw fields only under "All fields" | owner found the raw dump hard to read |
| 36 | Slicing presets per printer model (`presets_by_model`, default `presets`); slices are per (plate, model) and a print always gets the slice for its printer's model (sliced first if missing). **Automatic slicing** (`auto_slice`, default on) slices every waiting plate for each model every 30 s, never retrying a failure (removed in 0.22.22, #67). Preset files are added to Bambuddy (`POST /local-presets/import`). **Print speed** per print (1–4) is set with `POST /printers/{id}/print-speed` once the print is running | owner wants slicing ready without clicks, model-specific settings, and speed control; printing still only on Start print |
| 37 | **Slicing with OrcaSlicer's command line on this computer** (default when installed; the installers install it): presets read from Bambu Studio / OrcaSlicer (system + the owner's own) and SnapSTL-added files, `inherits` resolved by SnapSTL, `--arrange 0 --orient 0`, `--outputdir` (its log names the failure); one plate at a time in its own thread; output `Sliced/<model>/…gcode.3mf` uploaded to Bambuddy at Start print. Bambuddy's slicer stays selectable | Bambuddy's slicer needs Docker and wasn't running on the farm; Bambu Studio's Mac CLI crashes; Orca's (2.5.1) slices real SnapSTL plates in ~1 s, layout kept |
| 38 | **Slicing tab** (its own sidebar page); slicer = OrcaSlicer **or Bambu Studio** on this computer (Bambu Studio's CLI is refused on macOS only; on Windows it gets user presets that `inherits` the Bambu system preset, which its compatibility check needs) or Bambuddy's; **Test slicer** slices a 20 mm cube. **Presets per printer** (`orca_presets_by_printer` / `presets_by_printer`, then per model, then default); a plate is sliced for the printer it goes to: slices are keyed by slicer + presets (`farm.target`), so printers with the same presets share a slice; automatic slicing slices for the pre-selected printer only. Preset bundles add all their presets | owner: each printer its own settings, slice for the selected printer, Bambu Studio as an option on Windows |
| 39 | **Installers never run a second Bambuddy and only stop their own**: an existing Bambuddy (its own installer's Windows service / `C:\ProgramData\Bambuddy`, Mac launchd `com.bambuddy.app`, or anything answering on the port) is used as is; if SnapSTL's copy was already put next to it, the installer stands SnapSTL's copy down and starts the other again (nothing deleted). `Stop-Bambuddy` matches SnapSTL's own folder, never "whatever listens on 8000" | 0.10–0.17 installed a second, empty Bambuddy and on Windows stopped the existing one on port 8000 — it looked as if Bambuddy had lost its printers |
| 40 | **UI 0.19**: sidebar Orders · Batches · Print · Printers · Slicing · Settings · Help. Downloads merged into Batches (download queue + folder), Printer status into Printers (with Clear plate), Make It renamed Print (Bases · Names · Completed · Removed; slicing state inline per plate). Settings split into Setup · Product · Plates · Folders · Connections · Maintenance; a sidebar status (Helper · Bambuddy · Slicer · Printers, `/api/health`). **No default slicing presets**: presets only per printer model (all printers of a model share them; the per-printer presets of 0.18 are gone); a model without its presets isn't sliced or printed and the UI says so. **Print defaults per printer** (Slicing) with a per-plate override in Print. "Printed" keeps its name; bed size stays in Settings (256 mm on A1 and P1S) | owner: good quality comes from the owner's own tuned Bambu Studio presets, never guessed ones; clearer grouping of pages and settings |
| 41 | **Clear shared records** (0.19.1): typed CLEAR, copy of every record saved locally first, schema dropped and recreated; sync status, Sync now and "what's in Supabase" sit with the connection (Settings → Connections), with what is and isn't shared spelled out | owner asked for a way to empty Supabase when needed and couldn't tell whether sync worked (only batched orders are shared; tables are in schema `snapstl`) |
| 42 | **Ad-hoc orders** (0.20.0): orders typed in by hand (name to print, mobile, colour; optional qty and note) live in the same `orders` table with `sku='Adhoc'`, an `AH-…` ID and today's date/time; missing report columns stay empty. They go straight to Ready (sub-tab **Ad-hoc**) and use the normal batch → plates → Print → Printed flow and shared records | owner takes ad-hoc requests outside the webstore and wants them made in the same workflow without a report |
| 43 | **Only Bambuddy's slicer** (0.21.0): OrcaSlicer / Bambu Studio on this computer (`slicer.py`, decisions #37–#38), Test slice, preset import (to SnapSTL or into Bambuddy), the slicer switch and hand-made sliced files in the batch folder are removed; the installers no longer install OrcaSlicer. The Slicing tab offers every profile Bambuddy lists (local, Orca cloud, Bambu cloud, standard) per printer model and stores only references; old local-slicer settings, SnapSTL's preset folder and unfinished local slices are dropped at start | owner runs Bambuddy's slicer sidecar in Docker on another system and wants no slicing settings created, built or kept by SnapSTL |
| 44 | **One Bambuddy computer: Bambuddy native, only the slicer in Docker** (0.22.2): `bambuddy-server.ps1` installs Bambuddy's own Windows installer (a service; printer discovery works) and runs the Bambu Studio + OrcaSlicer sidecars from a sidecar-only compose file pinned to the tested release; re-runs only apply settings (`setup.json`), upgrades only with `BAMBUDDY_UPGRADE=1`. SnapSTL installers stop installing Bambuddy by default (`SNAPSTL_BAMBUDDY=server` makes that computer the Bambuddy computer, `SNAPSTL_BAMBUDDY_URL` connects to it, older native installs keep updating). The sidebar shows this computer's network address (`/api/health` → `network`) and copies Bambuddy's address for other computers when it runs here. Supersedes the 127.0.0.1-only part of #27. 0.22.0 briefly ran Bambuddy itself in Docker: Docker Desktop can't pass SSDP multicast (its host networking is layer 4 only and needs a Docker sign-in), so printers weren't found | owner runs one Bambuddy computer for all SnapSTL computers and chose: only the slicer in Docker |
| 45 | **Bambuddy scripts are Windows only** (0.22.3): `bambuddy-server.sh` and `bambuddy-mac.sh` removed; the Mac installer / updater only connect SnapSTL to the Windows Bambuddy computer (`SNAPSTL_BAMBUDDY_URL`), `update_bambuddy` runs on Windows only. SnapSTL itself stays on Mac + Windows | owner: the Bambuddy computer is a Windows PC; no Bambuddy or slicer scripts needed for a Mac |
| 46 | **Batched orders always name their batch's owner** (0.22.1): opening the local database copies a batch's `uid` and computer onto its orders that lack them and queues them for the shared records; `_WIN` lets a push take an `in_batch` record that has no owner | batches made before 0.8.0 were sent to Supabase without `computer_id`, so the owner's own *Bundled* was refused and `merge(force=True)` pulled the orders out of their batch |
| 47 | **Only SnapSTL reaches the shared records; a daily copy on every computer** (0.22.4): the setup SQL turns on row level security (no policies) on the `snapstl` tables and revokes `anon` / `authenticated` (Supabase's API roles) from the schema on every start; SnapSTL connects as the owner, which bypasses RLS. After a good sync, once a day, each computer saves every record to `backups/shared-records-daily-<date>.json` (mode 600, newest 30 kept); `snapstl-db export` saves one on demand | the records hold customer names, phones and addresses; Supabase's free plan keeps no backups the owner can download |
| 48 | **Every uploaded order is shared** (0.22.5): orders the records don't have are offered as `released` (`sync._OFFER`: inserts, or only takes a newer status and missing details — never changes a stored state; a stored batched/bundled/printed state is merged back here). Orders first seen through a pull get the report's review rules (`sync._review`); a name/colour fixed on one computer reaches the others while nobody makes the order. Upload syncs before and after ingesting; Settings → Database → Sync runs Sync now. Pushes are pipelined (`executemany`, 1000 per round). Supersedes the "only batched orders" part of #22 | owner wanted a report uploaded on any computer to update Supabase and every computer, and a refresh on the Orders page |
| 49 | **Reset can also clear Supabase** (0.22.6): Maintenance → *also clear Supabase* + typed RESET ALL → `maintenance.reset(shared=True)`: local backup, then `sync.clear` (copy of every record) and the local database emptied under `sync._lock`, so the background sync can't send this computer's orders back in between; the unsynced-changes guard doesn't apply (everything is emptied) | owner wanted one action to reset both this computer and the shared records |
| 50 | **Slicing profiles per printer; a spool can print as another colour** (0.22.7): `presets_by_printer` keyed by Bambuddy printer id replaces `presets_by_model` (kept as the fallback for printers with no pick of their own; *Remove* on one printer hands the model's pick to its other printers first). The slice key still hashes the profiles, so printers with the same profiles share a slice. Printers page: each spool has *prints as …* (`color_overrides`, keyed by slot + hex so it lapses with the spool); it changes only how SnapSTL reads the spool, nothing is sent to Bambuddy. Supersedes #36 | owner's printers of the same model need different profiles, and Bambuddy's filament names don't always match the order colours |
| 51 | **Daily handling: Sync, Clean up, Reset; Supabase required** (0.22.8): a report's statuses are dated by the newest entry in the file (`ingest.file_as_of`), not the upload time. Settings → Database: **Sync** (`/api/sync`) and **Clean up** (`sync.match`, `/api/maintenance/match`) run without confirmation — Clean up syncs, backs up, re-reads every shared record with `merge(force=True)`, removes local orders the records no longer have (except this computer's own batched / bundled / printed ones, which are sent again) and tidies 30-day history without deleting orders. Only Reset asks (RESET / RESET ALL). With `sync.REQUIRED` (default; `SNAPSTL_REQUIRE_SHARED=0` in tests) every non-GET `/api` call except `server.WITHOUT_SHARED` (connection, settings, sync, helper, updates, sign-in, backup) answers 503 while `sync.online()` is false; the UI shows a red bar and greys out every page but Settings and Help | owner wanted simple daily handling where every computer matches Supabase, and no computer working without it |
| 52 | **Print speed is checked, not assumed** (0.22.9): `FarmWorker._apply_speed` waits until the printer reports `RUNNING` from layer 1 (Bambuddy's queue says *printing* while the printer still heats and calibrates, and a speed sent then is lost when the print begins), sends `print-speed`, and marks `speed_set` only when the printer's `speed_level` matches; otherwise it resends each round, `SPEED_TRIES` = 5 at most (`print_jobs.speed_tries`), then puts the reason on the job | owner: the speed chosen in SnapSTL wasn't applied on the printer — it was sent once during preparation and taken as done because Bambuddy only confirms that the command was sent |
| 53 | ~~Bambu Cloud profiles: say why they're missing~~ (0.22.11) — superseded by #62: SnapSTL uses no cloud profiles | — |
| 54 | **Clean up and resets match Supabase strictly** (0.22.12): `sync._apply_match` makes the local database hold exactly the shared records — orders not there are removed (no exception for this computer's own), and this computer's batches the records lack (with their downloads / print jobs / slices) unless a shared order still names them, in which case the batch is kept and sent again. `snapstl.meta` gets an `instance` id (new each time the schema is created); every sync first checks it (`_follow_reset`) and, if it changed, backs up and matches, so a Clear / Reset-all on one computer empties every computer within a minute and nothing is sent back. `clear()` no longer marks this computer's orders to be re-sent. Supersedes the keep-own-work part of #51 | owner: batches and other rows weren't cleaned and came back after a reset |
| 55 | **Updated by hand → Restart** (0.22.13; the Restart button is always shown since 0.22.19): `updater.STARTED_AT` is the commit SnapSTL started from; `check()` adds `restart_needed` (and `running`) when the folder's HEAD has moved since (a `git pull` by hand, or an update whose restart didn't happen), and Settings → Updates shows **Restart SnapSTL** (`POST /api/update/restart`, refused while a name downloads or a batch builds) instead of "up to date" | owner ran `git pull` by hand; the app then said up to date and offered nothing, while still running the old code |
| 56 | **Plate type per printer** (0.22.14): Slicing → Print defaults has a *Plate type* per printer (`print_defaults[id].plate_type`, one of `farm.PLATE_TYPES` = Bambuddy 1.3.1's `BED_TYPES`; empty = the process profile's `curr_bed_type`). It is sent as `bed_type` on `POST /library/files/{id}/slice`, stored with the slice's profiles (`presets.plate`) and part of the slice key (`farm.target`), so another plate means a new slice; no plate type keeps the old key. Not a queue print option — the plate Bambuddy and the printer show comes from the sliced file | Bambuddy showed every print as Cool Plate (the profiles' default) while the printers have other plates on |
| 57 | **Orders page tabs in Supabase** (0.22.15): `order_records` gets `app_state`, `review_reason`, `made_at` (sent by the app) and `tab`, `ready_type` (set by the `set_tab` trigger with the Orders page's rules), with column comments, so another tool or agent can read and query the orders by tab. The local outbox trigger (`orders_outbox_upd2`) also sends review-reason and Ready-to-ship changes | owner wants the Supabase table usable read-only by another agent |
| 58 | **Report columns in Supabase, Magicians_35 only** (0.22.16): `report.py` maps ~55 report columns (channel, amounts, COD, shipping, dates, coupon, delivery city / state / pincode …; not the online / refund transaction IDs) to typed columns. Ingest keeps each Magicians_35 row locally (`report_rows`, JSON, stamped like statuses: newer file wins column by column, blanks never erase) and the sync sends unsent ones (`_push_reports`, pipelined) to `snapstl.report_rows`, and each upload (`files.sent`) to `report_uploads`. `orders_full` (security_invoker view, dropped and made again at setup) joins `order_records` + `report_rows`. Both tables have RLS, no-delete triggers and column comments; Clean up / resets / daily copies include them | owner wants the Supabase tables useful to another agent (via the Supabase MCP) for reading and querying; other SKUs left out on purpose |
| 59 | **`shop` schema for questions; review view fix** (0.22.17): every uploaded row of every product is kept locally (`shop_lines`, newer file wins key by key) and sent with `shop.put_line()` to `shop.report_lines`; a trigger (`shop.apply_line`) tidies each line into `shop.orders` / `customers` / `order_finance` / `shipments` / `products` / `status_history` — the tidying lives in the database, so it can change without an app release. `shop.bridge` (triggers on `snapstl.order_records` / `report_rows`) fills it from SnapSTL's own records too, so older versions feed it. Views: `production`, `batches`, `uploads` (live SnapSTL records), `daily_sales`, `production_queue`, `cod_outstanding`, `courier_performance`, `rto_by_state`; `glossary`. shop's triggers catch their own errors (warn only) so they never block SnapSTL. RLS on, API roles revoked. Clear drops `shop` too; Clean up / daily copies include it. Also: `app_state` / `review_reason` of an order nobody makes apply from any computer (`_VIEW`), and `_WIN` allows 1 ms of clock noise — 27 orders showed *ready* in Supabase though *needs review* in the app. Settings → Connections gets Clean up next to Sync now | owner wants a database an MCP agent can answer questions from, with the whole report, matching the app |
| 60 | **HANDOVER counts as in transit** (0.22.18): `shop.status_group` puts handover (and picked_up / shipped / dispatched / out_for_delivery) in `in_transit`; glossary entry added; setup regroups rows tidied under the old rule | the report gained a HANDOVER status (61 orders) that fell into *other* |
| 61 | **The Bambuddy computer is run from SnapSTL** (0.22.19): Settings → Connections → *This Bambuddy computer* (`bambuddy_host.py`, Windows only, shown where Bambuddy's service or `setup.json` is) starts what is down — Bambuddy's service (`sc start`; refused without admin → a click asks Windows' UAC prompt), Docker Desktop, the slicer containers (`docker compose up -d` with the script's compose file) — and *Keep running* (`keep_bambuddy_running`, default on) does the same every minute without any prompt. Nothing running is ever stopped or restarted by itself; Restart Bambuddy only for the one SnapSTL's script installed. `bambuddy-server.ps1` grants the signed-in user start/stop on the Bambuddy service. The script is still how it's installed | owner: restarting it shouldn't need re-running the install script |
| 62 | **No cloud profiles** (0.22.19): since 17 Nov 2026 Bambu Cloud's profile *list* answers with an empty (null) printer section; Bambuddy 1.3.1 crashes on it (`'NoneType' object has no attribute 'get'`) in its profile list and in slices with a Bambu Cloud process profile. Owner decision: no Bambu Cloud / Orca Cloud in SnapSTL — `farm.PRESET_TIERS` lists only Bambuddy's local and standard profiles, `cloud_problem` is gone, SnapSTL's key never asks for cloud access. Printers slice with self-contained "SnapSTL …" profiles built from the local Bambu Studio's system profiles (P1S: GM014 / GP004 / GFSA00_34, A1: GM030 / GP079 / GFSA00_04; inherits + includes resolved) and imported by the owner in Bambuddy → Local Profiles. Bambuddy stays the unmodified release (a local patch was tried and reverted at the owner's request); with its cloud sign-in on, its own profile list still fails — sign Bambuddy out of Bambu Cloud | slicing must not depend on Bambu Cloud |
| 63 | **The SnapSTL task runs with no window** (0.22.19): the task starts `conhost.exe --headless powershell … run-service.ps1` (`Register-SnapSTLTask` in `common.ps1`). On Windows 11 consoles open in Windows Terminal, which ignores `-WindowStyle Hidden`: a terminal stayed open and closing it stopped SnapSTL (task result 0xC000013A). Older installs switch at their next start (`Repair-SnapSTLTask` in run-service / update) | owner saw a terminal open with the app; closing it took SnapSTL down |
| 64 | **Filament slot per print, any printer** (0.22.20): Print lists every printer (none greyed out by colour) and, under the chosen one, its loaded slots best fit first (`farm.tray_choices`: exact colour name, then same family, then nearest hex; then most filament left). The chosen slot (Bambu global tray id: AMS n·4+slot, AMS HT id, 254 external) is stored in `print_jobs.ams_tray` and sent as Bambuddy's `ams_mapping` (queue item create / PATCH). The Printers page's "prints as …" mapping (`color_overrides`, #0.22.7) is removed; **Refresh from Bambuddy** (`POST /api/farm/refresh`) asks each printer for a full status report (`/printers/{id}/refresh-status`) and clears SnapSTL's caches | owner: pick the colour by AMS slot at print time, any printer allowed |
| 65 | **Skip, Make anyway, Approve and Ready to ship reach every computer** (0.22.21): `sync.merge` applies the record's `app_state` / `review_reason` (newer `state_at` wins) and `made_at`; `_UPSERT` keeps a skip unless a newer change undoes it; Remake clears `made_at`; once after updating each computer re-sends its orders and pulls every record again (`sync.VIEW_SENT`, see #66) | owner: orders skipped or bundled/ready to ship on one computer stayed that way only on that computer — Supabase had the skip (tab) but other computers never applied it, and could overwrite it |
| 68 | **External services in Settings** (0.22.23, owner request): *This Bambuddy computer — external services* checks and restarts Bambuddy, Docker Desktop (`platform_os.restart_docker_desktop`: `docker desktop restart`, else close + open), the slicer service and OctoEverywhere (containers of image `octoeverywhere/octoeverywhere`, `bambuddy_host.octo_plugins`; `docker restart`; Plugin Manager installed/open via `platform_os.octoeverywhere_manager`, *Open Plugin Manager*). `POST /api/bambuddy-host/{restart-octoeverywhere, restart-docker, restart-all, open-octoeverywhere}`. **Restart all** = Bambuddy (only if SnapSTL's script installed it) + slicer + OctoEverywhere, never Docker. Keep running also starts stopped OctoEverywhere plugins (start, never restart). SnapSTL and the database (Supabase) are not on this card | one place to check and fix every outside service |
| 67 | **Slicing only on a person's click, for the chosen printer** (0.22.22, owner decision): automatic slicing (`auto_slice`, #36) removed — it sliced only plates with a pre-selected (free) printer, so with printers waiting for Clear plate most plates never sliced. **Slice** / **Slice all waiting** require the printer selected on the row. A **Queues** panel (Print, bottom right, hideable) shows downloading / building / slicing / printing from `GET /api/queues` (`POST /api/makeit/slice` refuses a plate without `printer_id`; no default printer); each printer choice in `GET /api/makeit` carries its own `slice`, and the row shows the selected printer's. Start print still slices first when needed. **OctoEverywhere** (remote access to Bambuddy): `bambuddy-server.ps1` installs the OctoEverywhere Plugin Manager (Microsoft Store `9N9JNFCTJP7L`, via winget; `BAMBUDDY_OCTOEVERYWHERE=0` = skip); plugin instances and account linking are done by the owner in its UI. SnapSTL never talks to OctoEverywhere | owner wants to decide what is sliced and for which printer; remote access to the farm |
| 66 | **The newest export wins for every order in it** (0.22.21): `status_at` puts the file's moment (`file_as_of`: newest order / date in the file) before the row's event date (`@file|event|upload`); old stamps rewritten once locally and in Supabase. Also: once after updating, each computer sends **every** order again and reads every record (`sync.VIEW_SENT`); `_VIEW` (`app_state`, `review_reason`) now follows only the newer change (`_WIN`) or fills a record that has none | owner: some orders kept an old status after uploading a newer ShopDeck export (their row had no event date while the stored one did), and the host's changes had to reach Supabase and every computer |
| 69 | **Order-flow data, phase 1** (0.23, owner-approved plan `docs/build-plan-order-flow-and-logistics.md`): orders get `first_status`, `print_started_at` and the field groups (`db.FIELD_GROUPS`, each with `<group>_changed`; newest change per group wins locally in `sync.merge` and in Supabase via `_GROUPS_SQL` in `_UPSERT` / `_CLAIM` / `_OFFER`); new shared tables `order_notes` (append-only) and `usage_counts` (per computer/day/feature), both with RLS and no-delete triggers. The outbox trigger is now `orders_outbox_upd3` (also watches the new columns). `SCHEMA_VERSION` 2: an older SnapSTL stops syncing (update every computer the same day). No screen change yet | per-field change times keep people's actions (label, express, flags) independent of the who-makes-it state rules (`_WIN`) |
| 70 | **One tab rule, report rules, Printing** (0.23 phase 2): `tabs.py` holds the new 12 tabs as Python and as one SQL CASE shared by SQLite and the Supabase `set_tab()` trigger (old `tab` values are worked out again once at setup; the shop glossary keeps the old names marked "before 0.23"). Ingest flags problems instead of guessing (see §6); AWB EXPIRED clears a label; `print_started_at` from print jobs. Label group gets `label_note`; flag `back_to_print` added. `db.connect` tolerates a column added by another thread at the same moment (first start after updating). Dry run on the owner's database: all 1,670 orders in exactly one tab, none in Problems | owner's process: To make → Ready to print → Printing → Printed – pack it → Label done – ship it; a report must never undo work done in the app |
| 71 | **Production Orders page** (0.23 phase 3): 12 tabs in Make / Pack & ship / Records groups, Stage column with the tab's words, Express badge (red ≤ 3 days) and Express-first sorting (Orders and Print: `makeit.board` sorts plates by their orders' earliest `express_by`, so free printers are pre-selected for them first), returned-piece highlight + Use / Undo, Clear flag, reasons shown in words (flag, label expired, name change, remake). Saved views moved to `snapstl-order-views-v2` (old tab names). Make anyway on a delivered order records a remake so it shows in New orders | owner: simple words for staff, express orders prioritised, reuse returned pieces |
| 72 | **Logistics page** (0.23 phase 4, owner decision): port 8002 no longer shows the Orders page read-only; it serves the label / customer-care page and allows exactly the writes in `logistics.WRITES` (was: order reads only). Label Generated lives only there (production sees the result). WhatsApp opens `wa.me` in the person's own browser with the text filled in; SnapSTL sends nothing. Copy buttons fall back to `execCommand` because port 8002 is plain HTTP (no clipboard API) | the team that talks to customers needs to act, not just read; one place for labels, Express and customer questions |
| 73 | **Settings → Usage** (0.23 phase 5): counts per feature for the last 7 / 30 / 90 / 365 days from `GET /api/usage` (all computers via Supabase `usage_counts`, else this computer), in words (`FEATWORD` in `index.html`). Counted: every logistics screen, search, copy and WhatsApp click and action; production Use / Undo returned piece and Clear flag. Feature names only | owner: reorganise the layout later from real usage |
| 74 | **Logistics settings, 0.23.0** (phase 6): Settings → Logistics edits the tracking link, the red limits and the ready-made messages (saved with Save settings, per computer: the logistics host's apply to its page); the Orders page's Express badge uses the same `express_red_days`. Version 0.23.0; shared-records schema 2 (older SnapSTLs stop syncing) | owner can change wording and limits without a release |
| 75 | **Small first letter → Check name first** (0.23.1, owner): a name starting with a small letter is almost always a typing slip, so it needs a person (`ingest.SMALL_FIRST`); the name is still printed as typed or as approved. Once after updating, `ingest.recheck_small_first` (key `small_first_v1`) moves Ready orders not yet checked by a person to Check name first | real orders rarely start with a small letter |
| 76 | **Print → Verify** (0.24, owner, `verify.py`): a printed plate is checked by a named staff member before its orders are packed. Items are keyed by their own plate (batch, plate file, position) and never change; the plate they're printed on now (own, merged or fix) is their *carrier*. `made_at` is set only when every item of an order is checked Good (newest check per item counts); printed-but-unchecked = `print_done_at`. New field group `check` (`print_done_at`, `checked_at`, `checked_by`), tables `staff` and `plate_checks` (shared as `staff` / `print_checks`, append-only, no delete), shared-records schema **3**. Once after updating, everything already printed is checked by "Before 0.24" (`verify.backfill`, key `checks_v1`, runs before the 0.23 Printing backfill) — dry run on the owner's database: no order changed tab | the owner wants a quality check with a name on it; spoiled letters used to mean digging out files in Bambu Studio |
| 77 | **Fix and merged plates** (0.24, `extras.py`): cut from the plate's own .3mf (already scaled, no download): letters are the separate bodies of the name mesh (union-find, no scipy), small pieces join the nearest letter, ordered left to right after undoing the plate rotation; a fix group shares one plate when it fits. Merge: same colour, not started, ≤ 3 names, suggest only; originals get print-job state `merged` (hidden), Split back writes `unmerged`. Rows in `extra_plates` / `extra_items` (local: the files are on this computer) | owner decisions 2026-11-18: merge suggest-only, up to 3 names, no scaling |
| 78 | **Bed clear before a print** (0.24, owner): Start print refuses (HTTP 428) when the printer may hold a finished print (Bambuddy's `awaiting_plate_clear`, or FINISH / FAILED with no confirmation since our last job there) until the person confirms the bed is empty (`bed_clears`; also clears the plate in Bambuddy). A busy printer gets the plate held (`autostart` 0), and the worker never releases a held plate onto a busy printer | a print must never start on top of a finished one |
| 79 | **Batches page, Archive, Recycle Bin** (0.24, `archive.py`): names, progress (printed / checked / packed / shipped) and status in words, grouped by day, filters; Computer column only with more than one computer. Archived when every order is On the way / Delivered / Coming back or Cancelled; files to the Recycle Bin 10 days later; models unused 15 days (and no waiting order with that name) too — `platform_os.to_trash`, never permanent, never while downloading / building, daily thread (off with `SNAPSTL_NO_CLEANUP`) | owner: one production computer, clean list, keep disk small without losing anything |
| 80 | **Plate file names** (0.24): `Bases - A + B + C.3mf`, `Pink - A + B.3mf` (plain hyphen: safe on printers' SD cards); old plates keep their names. **Activities** tab (download queue and helper log moved from Batches, plus prints / checks / builds / merges / clean-ups from `activity`, print jobs, batches, downloads). Orders → **Refresh** syncs and reloads; long explanations sit behind a **?** per card | owner: easier for everyone to read, cleaner pages |
| 20 | Base plate previews are top-view renders (`render.py`, PNG in `APP_DIR/previews`, not in the output folder); per-object back/top pictures | the owner checks the watermark visually; the output folder stays only .3mf + report.csv |

---

## 17. Known limits

* MakerWorld's page is undocumented: a redesign can break `helper/content.js` (form labels,
  `plate_N.obj` names, the "Generate" text). Fix there, then reload the extension in `chrome://extensions`.
* The MakerWorld tab must stay open, ideally in its own window (Chrome slows hidden tabs).
* Slicing is Bambuddy's slicer only (sidecar, e.g. Docker on another computer). Its API calls are tested
  against a fake of Bambuddy's API (checked against Bambuddy's source), not yet against a real sidecar.
* The print farm was tested against a real Bambuddy v1.3.1 with a simulated printer (upload, queue,
  status, cancel) — not yet with a real printer; Bambuddy's API paths change between versions, so bump
  `scripts/bambuddy-version.txt` only after checking `farm.py` against the new release.
* The app runs from the project folder; moving it requires re-running the installer.
* The report has no "generated at" time: two reports with the same row dates and date range are
  ordered by upload time.
* The Windows version was written and tested on macOS only (scripts and OS functions); its first
  run on a real Windows PC may need fixes.
* Creating a batch needs the shared records reachable (by design). Other changes wait in the outbox
  while offline and are sent later.

---

## 18. How to extend safely

| Want to… | Do this |
|---|---|
| Add an order column | `MIGRATIONS` (db.py) → fill in `ingest._upsert` → `server._order_view` (+ `FILTERS` if filterable) → column + filter cell in `index.html` → test in `test_flow.py` |
| Add a setting | field with a default in the right dataclass (`settings.py`) → `validate()` if constrained → `fillSettings` / `readSettings` in `index.html` |
| Add a page / nav item | a `<section id="t-…">` + a button in `nav#tabs` + the list in `show()` and the startup allow-list (`index.html`) |
| Update in-app Help | the `<section id="t-help">` cards in `index.html` — keep it in step with README/this file |
| Add an order tab | `server.TABS` + the `TABS` array in `index.html` + a test |
| Change scaling targets | `default_bands()` (the owner can also edit them in Settings); keep the §9 axis rules |
| Change grouping / packing | `grouping.py` / `packing.py`; keep the §10 rules; extend `test_engine.py` |
| Change output layout / names | `pipeline.new_batch_dir`, `pipeline.plate_folder`, `export.plate_filename`; update §10 and README |
| Change where / how models are saved | `cache.py` (keep: readable folder names ending in the key; never auto-delete) |
| Change MakerWorld interaction | `helper/content.js` / `inject.js`; bump `helper/manifest.json` version |
| Change the print farm | `farm.py` (Bambuddy calls, worker), `makeit.py` (queues, pre-selection, Start print, Ready to ship) + Print tab in `index.html`; new Bambuddy version → check its routes, then `scripts/bambuddy-version.txt`; extend the fake in `test_farm.py` |
| Add an endpoint | `server.py`; anything the helper calls must live under `/api/helper/`; anything that touches this Mac's files or setup must be Mac-only (`MAC_ONLY_PREFIXES` / `is_local`) |

## Updates (git)

Each SnapSTL folder (`~/SnapSTL-app`) is a git clone of the **public** repository over HTTPS (no keys),
made by the one-command installers (`scripts/install-mac.sh`, `scripts/windows/install-windows.ps1`,
which also turn an older zip copy into a clone and run the platform installer). `updater.py`: `check()` = `git fetch` + how many commits `main` is
behind; `apply()` = refuse if tracked files were edited by hand → `git pull --ff-only` → `uv sync`.
`GET/POST /api/update` (this computer only) wrap them. POST refuses while a name is claimed or a
batch is building, then restarts the service (launchd / the Windows task). The UI checks at start and every 6 h and shows
**Update available** in the sidebar. The helper's config response carries the helper version on disk.
When it differs from the running one, background.js calls `chrome.runtime.reload()` once per version
(never mid-name). `onInstalled` reloads open model-maker tabs so they run the new content script.
`scripts/update.sh` does the same from Terminal. Customer data is outside the folder, so a pull
can't touch it. Shared order records and Windows: the sections below.

## Print farm — Bambuddy (0.10.0)

```
Batch built → Print → Bases / Names (per plate: printer pre-selected if one is available) → Start print
  → print_jobs row 'queued' → farm.Worker (one at a time, oldest first):
      sliced by Bambuddy (slices row done) → its library file; or a sliced file in the batch folder → upload;
      else → slice now (job waits as 'slicing') and continue when the slice is done
      → POST /api/v1/queue/ {library_file_id, printer_id, manual_start: true, options} → 'sent', held
  → autostart (the Start print click) → POST /api/v1/queue/{id}/start (409 → stays held; the person
    confirms → ?skip_filament_check=true). Jobs from the API's Push to queue stay held until Start.
  → every 15 s GET /api/v1/queue/{id}: pending → sent, printing → printing (+ %), completed → done,
    failed → failed, cancelled/skipped → cancelled
Bambuddy: one queue per printer; a held item never starts; after Start it waits for the printer to be
free and for "Clear plate" (a person).
```

* **Connecting:** `GET /api/v1/auth/status`. Login off (fresh install) → SnapSTL creates its own API key
  (`POST /api/v1/api-keys/`: queue, read status, library, control printer) and keeps it in `farm.json`;
  the worker does this at startup, so the Printers page is connected the first time it opens. Login on →
  the owner pastes a key made in Bambuddy → Settings → API Keys.
* **Printers (read only):** `GET /api/v1/printers/` + `/{id}/status` (state, %, time left, layer,
  temperatures, HMS errors, `awaiting_plate_clear`, `ams[].tray[]`, `vt_tray[]`); colour names from
  `GET /api/v1/inventory/colors/map` (`by_material["PLA Basic|hex"]`, then `colors[hex]`; cached 10 min;
  no name → nearest SnapSTL colour, shown "≈").
* **Suggestion** (`farm.suggest`, Print: `makeit.choose`): any available printer, ranked by how well its best
  filament slot fits the colour (white for bases; `farm.best_tray`: exact, family, nearest hex), then (Print) most filament left / fewest
  SnapSTL jobs on their way + busy/awaiting clear, spread across plates.
* **Print jobs → Print** (`farm.print_job`): queued → printer/options changed; sent (pending in
  Bambuddy) → `PATCH /api/v1/queue/{id}` `{printer_id, bed_levelling, flow_cali, timelapse}`;
  failed / cancelled / done → new job (queued held, like every job). Options go in the create call
  (`POST /queue/` takes them and `manual_start` in one go — no window where Bambuddy could start it;
  `add-to-queue` can't do either with Bambuddy's login off).
* **Slicing** (`Worker.slice_next` / `poll_slices`, table `slices`): one plate at a time — upload the plain
  plate (`POST /api/v1/library/files`), `POST /api/v1/library/files/{id}/slice` `{printer_preset,
  process_preset, filament_presets:[…], filament_colours:[name colour], plate: 1, export_3mf: true}` (no
  arrange: parts stay where SnapSTL put them) → 202 `{job_id}`; poll `GET /api/v1/slice-jobs/{id}` every
  ~2 s: completed → `result.library_file_id` (the sliced file, queued as is), failed → `error_detail`
  (`farm.slicer_hint` explains "sidecar unreachable"), any change of `progress` = still working, none
  for `SLICE_TIMEOUT_S` → failed. Profiles: `GET /api/v1/slicer/presets[?refresh=true]` (tiers local, orca_cloud,
  cloud, standard, all offered; `cloud_status` / `orca_cloud_status` say why a cloud tier is empty). Print jobs waiting for a slice sit in state `slicing` (`check_waiting`).
* **Printers page** (`farm.everything` → `summarize`): per printer the state, current print, temps,
  fans, spools + AMS humidity, HMS errors, nozzle, network, SD card, door, AI checks and its Bambuddy
  queue, plus `makeit.slicing_summary()`; the raw record / status / queue stay under "All fields".
* A restart re-queues jobs and slices cut off while uploading. Bambuddy is installed/updated by
  `scripts/windows/bambuddy.ps1` on Windows (from the installers, `update.sh` /
  `update.ps1`, and in the background after an in-app update via `platform_os.update_bambuddy`).

## Shared order records (Supabase) and computers (0.8.0)

Each computer works in its **own local SQLite** database (`db.py`). `sync.py` keeps the **shared
order records** in Supabase in step. The connection string is entered in **Settings → Connections →
Shared order records** (`/api/shared-records`: format check, connection test and table creation before saving) and kept
in `APP_DIR/shared-records.json` (mode 600, never returned in full). Without it there is no sync.
`SNAPSTL_DATABASE_URL` overrides it for tests and development.

- **What's shared:** `snapstl.order_records` holds one row per order that ever entered making, with
  state `in_batch`, `bundled`, `printed` or `released`. It also stores the batch (uid, label), the
  owning computer, the order fields, the webstore status and two clocks: `state_at` (epoch seconds)
  and `status_at` (text, see below). `snapstl.batch_records` holds every computer's batches.
  Since 0.22.15 each record also says where the Orders page shows it: the app sends `app_state` (its
  own state), `review_reason` and `made_at` (both in `_STATE_GROUP`, so `_WIN` decides), and the
  trigger `snapstl.set_tab()` sets `tab` (the `server.TABS` key) and `ready_type` (single / clubbed /
  adhoc) on every insert and update, so they're right whichever computer or version wrote the row;
  `retab_club()` marks a clubbed group's partners when a new one arrives. Columns carry `COMMENT`s for
  anyone reading the table. Each computer sends every order once after updating (`sync.TABS_SENT`).
  Settings are never shared.
- **Never deleted:** the app has no DELETE on these tables, and `BEFORE DELETE` / `BEFORE TRUNCATE`
  triggers (`refuse_delete`) reject deletes from anyone. Local reset and clean-up only touch SQLite.
  The one way to empty them is **Clear shared records** (`sync.clear`, `POST /api/shared-records/clear`
  `{confirm:"CLEAR"}`, this computer only): every row is first saved to
  `backups/shared-records-<stamp>.json` (mode 600), then `DROP SCHEMA snapstl CASCADE` and the setup
  runs again; this computer marks its orders and batches unsent and forgets its pull point, so the
  next sync sends what it's still making.
  Reset refuses while `sync_outbox` isn't empty. With *also clear Supabase* (RESET ALL) it empties the shared records
  too, through `sync.clear`, with no sync in between (decision #49).
- **Who can read them:** row level security is on (no policies) and Supabase's API roles (`anon`,
  `authenticated`) have no rights on schema `snapstl`; the setup SQL re-applies both on every start.
  SnapSTL connects as the table owner, which bypasses RLS.
- **Daily copy:** `sync.daily_copy` (after a good sync in `sync.LOOP`, once per day) saves every record
  to `backups/shared-records-daily-<YYYY-MM-DD>.json` (mode 600, newest `KEEP_DAILY` = 30 kept) on each
  computer; `snapstl-db export` saves a timestamped one. The sync status carries `last_copy` and
  `copy_error`.
- **Claim:** `batches.create_batch` → `sync.claim`. `INSERT … ON CONFLICT DO UPDATE … WHERE
  state='released'` (or this computer's own claim whose batch uid no longer exists locally)
  `RETURNING order_id`. Orders not returned were taken elsewhere: they're merged locally and left out.
  If the shared records are unreachable, `SyncError` means no batch is created.
- **Local bookkeeping by triggers** (`db.POST_SCHEMA`, so no code path can skip them):
  - `orders.state_at` is set on every state change.
  - `sync_outbox` gets every order whose state, status or name changed and which is in the records
    (or now `in_batch`/`bundled`/`printed`).
  - `batches.synced` becomes 0 when a batch changes.
  - Writes made by the sync itself run inside `db.quiet()` and are not queued again.
- **Offer:** every local order the records don't have (`in_remote = 0`, not batched) is sent with
  `_OFFER` as `released`: new rows are inserted; for an existing row only a newer webstore status and
  missing customer details are taken, never the state. If the stored row is batched / bundled /
  printed, it is merged here (`merge(force=True)`). An upload syncs before and right after ingesting.
- **Push:** outbox orders are upserted with rules in SQL (`_WIN`):
  - A state change applies only if it is at least as new (`state_at`), and only if one of these
    holds: the record is `released`; the writer owns it; it is `in_batch` with no owner (sent by an
    older SnapSTL, see #46); it marks printed or unprinted; or it sends a made order back (`released`).
  - The webstore status applies only if `status_at` is higher.
  - The first-written order fields are kept.
  - `RETURNING *` shows when a change was refused; the stored row is then merged locally
    (`merge(force=True)`).
- **Pull:** rows with `updated_at` after the last pull, minus a 120 s overlap, are merged locally
  (`merge`). Orders unknown here are inserted. Another computer's orders show as `in_batch`,
  `bundled` or `printed` with `batch_id NULL` plus `batch_label` and `batch_computer`. A claim of
  this computer whose batch is gone (e.g. after a reset) is released. An order with a pending local
  change keeps it until the push has run.
  Since 0.22.21 a merge also takes `app_state` / `review_reason` of an order nobody makes (Skip,
  Make anyway, Approve pressed on another computer, newer `state_at` wins; `sync._view`) and `made_at`
  (Ready to ship, worked out by the batch's computer) whenever both sides agree on the state. In
  `_UPSERT` a skip is set or undone only by a newer change, so a computer that hasn't pulled the skip
  can't overwrite it; a local skip the records lack goes through `_UPSERT`, not `_OFFER`.
- **When it runs:** `sync.LOOP` runs every 60 s and is woken right after batches finish, orders are
  marked printed, reset or skipped, and after a report upload. An upload syncs before ingesting too.
  **Settings → Connections → Sync now** runs `/api/sync`. `GET /api/shared-records?check=1` adds
  what the records hold (orders per state, batches per computer: **Show what's there**). Connecting
  runs a first sync and reports its error instead of "connected" when it fails. `snapstl-db check | sync` does the same from a
  terminal.
- **Report timestamps:** the webstore report has no "generated at" time, so
  `ingest.status_stamp(event, report, upload)` builds a sortable `status_at`, `@file|event|upload`:
  - first, how recent the whole file is (`ingest.file_as_of`, since 0.22.8): the later of the "Date
    Range … to DD/MM/YYYY" end above the header and the newest date / date-time in any of its date
    columns (the newest order's creation time, pickup, delivered, RTO, return …). The ShopDeck export
    holds every order's status as of that moment, so since 0.22.21 a newer export updates **every**
    order in it, also rows without an event date (before, the row's event date came first and a row
    without one lost to an older file's row that had one). With a time it's written
    `YYYY-MM-DD~HH:MM:SS`; `~` sorts after `|`, so the same day with a time ranks higher;
  - then the latest of the row's event dates (Pickup / Delivered / RTO Initiated / RTO Delivered /
    Return Created / Return Delivered) — only between rows of files with the same moment;
  - the upload time (only breaks ties).

  The leading `@` sorts above every older stamp. Older stamps are rewritten once into this order:
  locally by `db._upgrade_stamps` (orders, report_rows, shop_lines; key `stamps_v2`), in Supabase by
  `snapstl.stamp_v2()` in the setup SQL (order_records, report_rows, shop.report_lines; again whenever a
  not-yet-updated computer writes an old one; a failure only warns) and in `sync.merge` for pulled rows.

  An existing order's status changes only if the new stamp is at least as high (`kept_newer_status`
  counts the others), both locally and in the records.
- **Printed:** a person marks bundled orders printed (`ingest.set_printed`, `/api/orders/printed`)
  and can undo it. Printed orders keep their batch and get their own tab.
- **Computers:** `computer.py` gives this computer an id (`computer.json`) and a name
  (`SNAPSTL_COMPUTER_NAME` or the system name). Batches are always this computer's. Download and
  build queries filter on `computer.ID`, and the `mine` checks in batches.py / server.py stay as
  guards.

## Windows

`platform_os.py` holds every OS difference: the app folder (`%LOCALAPPDATA%\SnapSTL`), open/reveal
(Explorer `/select,`), keep awake (`SetThreadExecutionState`), computer and LAN names, and service
restart. `scripts/windows/install.ps1` installs uv and runs `uv sync`. It registers the scheduled task
**SnapSTL** (at log-on, hidden, restart on failure) running `run-service.ps1`, a loop around
`uv run snapstl-serve`, and creates Start-menu and Desktop shortcuts (`pythonw -m snapstl`).
`update.ps1`, `restart.ps1` and `uninstall.ps1` handle the rest. A restart from the app exits with
code 3 and the loop starts it again. In-app updates on Windows only `git pull`: the loop's `uv run`
installs the new packages before starting, since Windows can't replace files Python has open.
`.gitattributes` keeps `.sh` files LF and `.ps1` files CRLF.


### Logistics page (0.23)

`logistics_api.py`: lists (`VIEWS`: labels, ship_it, check_name, problems, remakes, being_made,
on_the_way, delivered, rto, cancelled), search (order ID, customer, phone by its last 10 digits), order
detail (notes, clubbed group, the customer's other orders, returned pieces), CSV with the page's columns
only, and the actions (label, express, name, note, cancel-request, remake; use / undo returned piece via
`/api/orders/{id}/…`). Label Generated covers the whole clubbed group and is refused until every order of
it is printed; Express applies to the group's unshipped orders. `view_of` adds words for the phone
(`words`), the tracking link (`settings.logistics.tracking_url` with `{awb}` / `{order_id}`), and red
flags (late: `late_days` since ordering and not labelled; stuck: On the way `stuck_days` after pickup;
Express red `express_red_days` before). Every action counts itself in `usage_counts`; the page counts
screens and copy / WhatsApp clicks via `POST /api/usage`. `logistics.py` serves the page (`#t-desk`) and
only `READS` / `WRITES`; writes need the shared records online. The main app registers the same routes
(local use) plus `GET /api/usage` (Settings → Usage).

### Logistics host (Mac or Windows)

The full app now listens only on localhost on Mac and Windows. On Mac or Windows, open Settings → Connections → Logistics access, enable hosting, set a password and press Save. Save starts or stops the listener immediately without restarting the app. Employees open `http://HOST-IP:8002` and sign in. Permit TCP 8002 for the local subnet in Windows Firewall if needed. This supersedes the former remote-control portal.

`logistics.py` hosts an explicit allowlist: login, orders, facets and CSV export. No edit, upload, batch, helper, printer or settings route is exposed. The shared orders table supports copying, filters, categories, pagination and browser-local saved column views. Refresh orders reloads the host’s local synchronized records, also refreshed every minute; it never pushes changes or runs cleanup. Main Orders has no Supabase refresh button; use Settings → Database → Sync or Clean up.

Orders pagination includes a Page selector to jump directly to any page. Logistics uses the same control. Logistics hosting is available on both Mac and Windows; Settings reports whether port 8002 is actually listening. Allow incoming connections in the host firewall for LAN access. The UTF-8 interface is decoded explicitly on Windows.

Settings → Connections → Logistics access shows the current host LAN IP addresses as clickable links with Copy address buttons (port 8002). Use a numeric IP link from another computer on the same network.

The logistics password field is labelled **Set or change logistics password** in Settings → Connections → Logistics access. Show reveals only the newly typed password. Saved passwords are hashed and cannot be displayed; leaving the field blank keeps the existing password.

Listener lifecycle tests bind a temporary OS-assigned port so an active logistics host on port 8002 is never affected.
