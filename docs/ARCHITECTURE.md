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

## 2. Runtime (one dedicated Mac, everything local)

| Piece | What / where |
|---|---|
| **Service** | `snapstl-serve` → FastAPI/uvicorn on **http://127.0.0.1:8317** (`SNAPSTL_PORT` to change). Started at login by launchd: `~/Library/LaunchAgents/local.snapmagicians.snapstl.plist` (`RunAtLoad`, `KeepAlive`). Hosts the portal, the helper API, the download queue and the batch builder thread. Runs `caffeinate -i` while alive. |
| **App window** | `~/Applications/SnapSTL.app` → `snapstl` → pywebview window on the same URL (adds native folder picker; text selection on). UI = side navigation: Orders · Batches · Downloads · Settings · Help. If the service isn't up it kickstarts it. Closing the window doesn't stop the service. |
| **SnapSTL Helper** | Chrome extension (`helper/`, *Load unpacked*) in the user's **normal** Chrome, on the MakerWorld model-maker tab. Generates each queued name on the real page and uploads the files. |
| **Data** | `~/Library/Application Support/SnapSTL/` (`APP_DIR`, override with `SNAPSTL_HOME`): `snapstl.db`, `settings.json`, `backups/`, `auth.json` (only if a portal password is set). |
| **Downloaded models** | Settings → Downloads folder (default **`~/SnapSTL/Downloads`**): one readable folder per model, `<Name> - <N> comp - <cache key>/{base.stl, name.stl, info.json}`. **Never deleted by SnapSTL** — the user cleans up in Finder; a deleted model is re-downloaded when a batch needs it. Models from the old hidden `APP_DIR/cache/` are moved here once at startup (`cache.migrate_old_cache`). |
| **Secrets** | MakerWorld license password → macOS Keychain (service `SnapSTL`). |
| **Output** | Settings → output folder (default `~/SnapSTL/output`). |
| **Log** | `~/Library/Logs/SnapSTL.log` |

Installed by `scripts/make_app.sh` (installs `uv` if missing, `uv sync --no-dev`, writes the plist and
the `.app`). The app runs **from the project folder** — moving it means re-running the script.

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
| Secrets | keyring (macOS Keychain) | 25.7.0 |
| Storage | SQLite (WAL) via stdlib `sqlite3` | — |
| Plate format | **3MF core spec**, own writer/reader (`threemf.py`, stdlib zip/xml) | — |
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
│   └── scaling-rules-v1.1-handoff.md   business spec for scaling (historical source)
├── pyproject.toml            deps, console scripts            uv.lock      exact versions
├── .python-version           3.12                             .gitignore   excludes data/reports/output
├── scripts/
│   └── make_app.sh           installer: uv, deps, launchd service, ~/Applications/SnapSTL.app
├── helper/                   SnapSTL Helper — Chrome extension (Manifest V3)
│   ├── manifest.json         permissions: makerworld.com + http://127.0.0.1:8317 only; version
│   ├── background.js         service worker: relays tab ↔ app (/api/helper/*), one active tab
│   ├── content.js            model-maker tab: job loop, form filling, Generate, upload
│   ├── inject.js             page world: copies plate_1/plate_2 .obj responses (read-only)
│   └── popup.html, popup.js  status (app reachable? tab open?) + "Open MakerWorld tab"
├── src/snapstl/
│   ├── __init__.py           entry points: main() (window), serve() (service); port, caffeinate, folder picker
│   ├── server.py             FastAPI app: every HTTP endpoint (§11), cross-site guard, tab/filter SQL
│   ├── settings.py           dataclasses + defaults (§12); APP_DIR, CACHE_DIR, output path, MakerWorld URL
│   ├── db.py                 SQLite schema + MIGRATIONS, tx(), batch helpers (create/finish/release)
│   ├── ingest.py             report parsing (XLSX/CSV), name cleaning, upsert by Order ID, review edits
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
│   ├── keychain.py           license password (Keychain; file fallback when SNAPSTL_HOME is set)
│   ├── auth.py               optional portal password + signed session cookie; `snapstl-admin`
│   └── static/
│       ├── index.html        the whole UI: side navigation (Orders, Batches, Downloads, Settings, Help)
│       └── login.html        used only if a portal password is set
└── tests/
    ├── conftest.py           temp SNAPSTL_HOME, synthetic base/name meshes
    ├── test_engine.py        scaling, packing, grouping, file names, 3MF, per-day folders
    ├── test_ingest.py        report filtering, de-dup, review edits, name cleaning
    └── test_flow.py          HTTP end to end: batch → helper → build; tabs/filters; maintenance; guard
```

Not in git (by design): `.venv/`, customer reports (`*.xlsx`, `*.csv`), `output/`, ZIPs, anything in
`APP_DIR`.

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
| `state` | §6 |
| `batch_id`, `last_error` | current / last batch; last download error |
| `first_file`, `last_file`, `first_seen`, `updated_at` | provenance |

**batches** — `id`, `created_at`, `status` (§8), `order_ids` (JSON), `settings` (JSON snapshot at
creation), `out_dir`, `summary` (JSON: plates + previews, errors, warnings, `folder`, `label`),
`finished_at`.

**downloads** — the helper queue: `cache_key`, `name`, `compartments`, `model_json`, `state`
(`queued | claimed | done | failed | cancelled`), `attempts`, `claimed_at`, `error`, `batch_id` (batch
that queued it), timestamps.

**files** — each report upload: `filename`, `sha256`, `ingested_at`, `summary` (JSON).

---

## 6. Orders & ingest (`ingest.py`)

* Header row found by `Order ID, Order Status, SKU Code, Product Customisation` (first 30 rows).
* Only `SKU Code == Magicians_35` (case-insensitive). Customisation lines parsed as
  `Enter Your First Name:`, `Color:`, `Special Requests:`. **The customer's "Number of
  Compartments" is ignored** — compartments come from the scaling table.
* **Upsert by Order ID**: re-uploads never duplicate; later reports refresh status, quantity, clubbed
  ID and customer fields. A `bundled` or `skipped` order is never moved back by a report.
* Quantity N → N organizers in a batch.

Name cleaning (`clean_name`): emoji/symbols dropped silently; other non-letters removed → review;
spaces → review; non-Latin letters → review; more than 15 letters → review. A missing name/colour
or a special request → review.

| State | Set when | UI tab |
|---|---|---|
| `ready` | report `PRINT`, clean | **Ready** → sub-tabs **Single** / **Clubbed** |
| `needs_review` | `PRINT` with a review reason | Needs review (edit + Approve) |
| `in_batch` | selected into an unfinished batch | In a batch |
| `bundled` | in a finished batch | Bundled (Remake → back to ready) |
| `skipped` | user pressed Skip | Skipped (Make anyway) |
| `not_needed` | report status isn't `PRINT` | **Delivered** / **RTO** (`RTO…`) / **In Transit** / **Cancelled** (`CANCEL…`) / **Others** |

*Single* = the order's clubbed ID has only this line; *Clubbed* = one clubbed ID with several lines.
Tab SQL lives in `server.TABS`; column filters in `server.FILTERS` (text = contains; status, colour,
qty = exact; combined with AND; also used by *Select all matching* and *Export CSV*).

---

## 7. MakerWorld & the SnapSTL Helper (`helper/`, `batches.py`)

Model: **Custom Name Desk Organizer v2.34** (`designId=2600234`) on MakerWorld's Parametric Model
Maker (URL in `settings.MAKERWORLD_URL` and `helper/background.js`).

| Parameter | Value |
|---|---|
| Print version | **always `NO AMS`** → base and name come out as separate plates |
| Name line 1 / 2 | the order's name / always empty (single line only) |
| Font, Font size 30–120, Thickness 30–120, Outline 4–15, Rotate text | Settings, same for every name |
| Compartments 1–10 | scaling table, by letter count |
| License password | Settings → Keychain (removes anti-sale markings) |

NO AMS output: `plate_1.obj` = base (upright: X length, Y depth, Z height); `plate_2.obj` = name
(flat: X length, Y letter height, Z 4 mm). The app tells them apart **by height**, not by order.

**Helper protocol** (one MakerWorld tab, one name at a time):

```
content.js ──message──► background.js ──HTTP──► app (127.0.0.1:8317)
  GET  /api/helper/next?status=…  → {job: {job_id, name, compartments, params, license_password, pause_after_s} | null, paused}
  fill form (XPath on labels; native value setter for React inputs; MUI menus via mousedown) → click Generate
  inject.js copies plate_1/plate_2 .obj → POST /api/helper/jobs/{id}/result   (multipart plate_1, plate_2)
  error            → POST /api/helper/jobs/{id}/fail {error}       (1 retry, then failed)
  reload mid-name  → POST /api/helper/jobs/{id}/release            (back to queue, attempt not counted)
  status           → POST /api/helper/ping {status}                (every ~5 s while generating; popup sends {probe:true})
```

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
* Downloads no active batch needs are cancelled automatically (startup and after each build/cancel).
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
        Bases/{Ayra,pink},{Manvi,pink},{Riya,blue}.3mf
        Names/pink/{Ayra,pink},{Manvi,pink}.3mf
        Names/blue/{Riya,blue}.3mf            one folder per name colour
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
| Orders | `POST /api/ingest` (multipart `file`), `GET /api/orders`, `GET /api/orders/ids`, `GET /api/orders/facets`, `GET /api/orders.csv`, `POST /api/orders/{id}/name` `{name,color}`, `POST /api/orders/{id}/reset`, `POST /api/orders/{id}/skip` |
| Batches | `POST /api/batches` `{order_ids}`, `POST /api/batches/{id}/download` (`{force:true}` = Re-download), `POST /api/batches/{id}/build` (also Rebuild), `POST /api/batches/{id}/cancel`, `GET /api/batches`, `GET /api/batches/{id}`, `GET /api/batches/{id}/file?path=`, `POST /api/batches/{id}/reveal` (local only), `GET /api/batches/{id}/zip` |
| Downloads | `GET /api/downloads` (queue + Downloads folder stats), `POST /api/downloads/retry`, `POST /api/downloads/reveal` (local only), `POST /api/downloader/pause`, `POST /api/downloader/resume` |
| Helper | `GET /api/helper/next`, `POST /api/helper/ping`, `POST /api/helper/jobs/{id}/result`, `…/fail`, `…/release`, `GET /api/helper.zip` |
| Settings | `GET` / `PUT /api/settings`, `POST /api/settings/reset`, `POST /api/license` `{password}` |
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
| top level | `output_dir`, `downloads_dir` (both editable in local mode only), `delay_between_downloads_s` | ~/SnapSTL/output, ~/SnapSTL/Downloads, 3 |

Unknown keys are ignored on load, so older settings files keep working.

---

## 13. Security & privacy

* Local mode (no portal password): only `127.0.0.1` / `localhost` host headers are accepted;
  requests with a foreign `Origin` are rejected (CSRF / DNS-rebinding guard). `chrome-extension://`
  origins are accepted **only** on `/api/helper/*`.
* Optional portal password (`uv run snapstl-admin set-password`): scrypt hash + signed 30-day cookie
  in `APP_DIR/auth.json`. Only needed if the portal is ever exposed beyond this Mac.
* Customer name / phone / address are stored only in the local database (owner's request) — never
  sent anywhere, never in git. License password only in the Keychain.

---

## 14. Maintenance (`maintenance.py`, Settings → Database)

**Back up now** (SQLite online backup → `backups/`, newest 20 kept). **Clean up** records older than
N days (not-needed orders, finished batches, download history, upload records) — **bundled orders
are always kept** so printed orders can never come back as ready. **Reset** (type RESET). A backup is
taken first every time. **None of these delete files**: batch folders and downloaded models are only
ever removed by the user in Finder.

---

## 15. Testing

`uv run pytest -q` — 25 tests, ~3 s, no network, temporary `SNAPSTL_HOME`:

* `test_engine.py` — scaling, fit check (pass, mismatch detected, failing pair held back), packing/nesting,
  grouping, file names, 3MF round trip, per-day folders
* `test_ingest.py` — SKU/status filtering, de-dup across reports, review edits, name cleaning
* `test_flow.py` — over HTTP: create → start download → helper → models_ready → build; failures and
  retries; pause/release/cancel; resume after restart; tabs/filters/customer fields; maintenance;
  cross-site guard; Rebuild (new folder, old kept); Re-download (+ Stop); readable Downloads folders;
  move from the old cache; reset keeps models

The helper is exercised by calling `/api/helper/*` directly — tests never contact MakerWorld.
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

---

## 17. Known limits

* MakerWorld's page is undocumented: a redesign can break `helper/content.js` (form labels,
  `plate_N.obj` names, the "Generate" text). Fix there, then reload the extension in `chrome://extensions`.
* The MakerWorld tab must stay open, ideally in its own window (Chrome slows hidden tabs).
* Opening and slicing in Bambu Studio is a manual review step; it isn't automated or tested here.
* The app runs from the project folder; moving it requires re-running `scripts/make_app.sh`.

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
| Add an endpoint | `server.py`; anything the helper calls must live under `/api/helper/` |
