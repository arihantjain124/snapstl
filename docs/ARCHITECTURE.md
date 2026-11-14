# SnapSTL — Architecture & Decisions

This is the source of truth for **how SnapSTL works and why**. Read it before changing code.
Update it whenever a decision below changes (see `AGENTS.md`).

---

## 1. What the product does

Snap Magicians sells personalised **name desk organizers** (webstore SKU `Magicians_35`).
Each order = one organizer **base** (printed white) + one separate **name** piece (printed in the
customer's colour), glued together after printing.

SnapSTL turns the webstore's order report into print-ready plate files:

```
Order Reconciliation Report (.xlsx)
   → keep SKU Magicians_35, read name + colour, de-duplicate by Order ID      (ingest.py, db.py)
   → you review odd names, then select orders → Create batch                   (server.py, UI)
   → each name is generated on MakerWorld by the SnapSTL Helper in your Chrome  (helper/, batches.py)
   → scale with Scaling Rules v1.1                                             (scaling.py)
   → group into plates of 3 and pack collision-free on a 256×256 bed           (grouping.py, packing.py)
   → merged STL per plate + report.csv per batch folder                         (export.py, batches.py)
```

---

## 2. Runtime shape (local Mac only)

| Piece | What it is |
|---|---|
| **Service** | `snapstl-serve` (FastAPI + uvicorn) on `http://127.0.0.1:8317`, started at login by launchd (`~/Library/LaunchAgents/local.snapmagicians.snapstl.plist`, `KeepAlive`). Runs the web portal, the download queue and the batch builder. Keeps the Mac awake (`caffeinate`) while running. |
| **App** | `~/Applications/SnapSTL.app` → `snapstl` → opens a pywebview window onto the same portal (adds a native folder picker). Closing the window does **not** stop the service. |
| **SnapSTL Helper** | Chrome extension (`helper/`, loaded unpacked) in the user's **normal** Chrome. On the MakerWorld model-maker tab it asks the app for the next queued name, fills the real page, presses Generate and uploads the two model files. No settings or tokens. |
| **Data** | `~/Library/Application Support/SnapSTL/` — `snapstl.db` (SQLite), `cache/` (unscaled meshes per name), `settings.json`, `backups/`. License password in the macOS Keychain. |
| **Output** | Chosen in Settings (default `~/SnapSTL/output`). One folder + one ZIP per batch. |
| **Log** | `~/Library/Logs/SnapSTL.log` |

Decision history (so nobody repeats it):
1. Cloud server (AWS Lightsail) — removed: unreliable; everything runs on one dedicated Mac.
2. Token-based extension talking to the server — removed: setup/sync was fragile.
3. Direct HTTP calls to MakerWorld's generate API with the user's cookie — **does not work**:
   MakerWorld never answers generate requests that don't come from its page.
4. App-owned automated Chrome window (Playwright) — **does not work**: Cloudflare keeps rejecting
   "Verify you are a human" in a browser that is controlled by automation.
5. **Current:** a zero-setup helper extension in the user's own Chrome, talking only to the app on
   this Mac. The user's real browser passes Cloudflare normally; the app keeps all the logic.

---

## 3. Tech stack

| Area | Choice |
|---|---|
| Language / env | Python 3.12, managed by **uv** (`pyproject.toml`, `uv.lock`) |
| Web | FastAPI + uvicorn; UI is one static page `src/snapstl/static/index.html` (vanilla JS, no build step) |
| Window | pywebview (Cocoa/WKWebView) |
| MakerWorld | Chrome extension (Manifest V3, vanilla JS) in `helper/` |
| Geometry | trimesh (meshes, STL), shapely (2-D footprints / collisions), numpy |
| Spreadsheet | python-calamine (the webstore XLSX has a stylesheet openpyxl rejects) |
| Storage | SQLite (WAL) via stdlib `sqlite3`; secrets via `keyring` (macOS Keychain) |
| Tests | pytest (+ httpx TestClient, mapbox-earcut for test meshes) |

---

## 4. Orders (ingest.py, db.py)

* Header row is found by the columns `Order ID, Order Status, SKU Code, Product Customisation`.
* Only `SKU Code == Magicians_35` (case-insensitive). Customisation text is parsed as
  `Enter Your First Name: …`, `Color: …`, `Special Requests: …`. **The customer's
  "Number of Compartments" is ignored** (unreliable) — compartments come from the scaling table.
* **Order ID is the primary key** → re-uploading the same or a newer report never duplicates.
  Later reports only update the report status.
* Stored fields: order id, clubbed id, date (ISO), report status, quantity, name as typed, name to
  print, colour, and — at the owner's request — **customer name, phone and address** (address +
  city + state + pincode), shown in the Orders table. They stay in the local database on this Mac;
  re-uploading a report refreshes them.

Order states:

| State | Meaning |
|---|---|
| `ready` | report status `PRINT`, clean name + colour |
| `needs_review` | `PRINT` but name has a space / removed symbols / non-Latin letters / >15 letters, or no name/colour, or a special request. Edit + Approve in the UI. |
| `in_batch` | selected into a batch that is still downloading / building |
| `bundled` | in a finished batch — never made again unless **Remake** |
| `skipped` | skipped by hand (**Skip**) — kept separate from report statuses |
| `not_needed` | the report says delivered / in transit / packed / cancelled / returned |

Orders screen tabs: **Ready** (sub-tabs **Single** = its clubbed order ID has only this name;
**Clubbed** = one clubbed order ID linked to several names), Needs review, In a batch, Bundled,
Skipped, and the *not needed* orders split by report status: **Delivered**, **RTO** (`RTO…`),
**In Transit**, **Cancelled** (`CANCEL…`), **Others** (e.g. `PACK`), plus All. Every column has a
filter box (text "contains"; status / colour / qty exact); filters combine with AND and also apply to
*Select all matching* and *Export CSV*. Text in the app is selectable / copyable.

Name cleaning: emoji and symbols are dropped silently; other characters removed → review;
colours are lower-cased (`gray` → `grey`). Quantity N → N organizers.

---

## 5. MakerWorld model & the helper (helper/, batches.py)

Model: **Custom Name Desk Organizer v2.34** (`designId=2600234`), Parametric Model Maker.

| Parameter | Value used |
|---|---|
| Print version | **always `NO AMS`** (base and name come out as separate plates) |
| Name line 1 | the order's name; Name line 2 always empty (single line only) |
| Font, Font size (30–120), Thickness (30–120), Outline (4–15), Rotate text | from Settings, same for every name |
| Compartments (1–10) | **from the scaling table by letter count** |
| License password | from Settings (Keychain) — removes MakerWorld's anti-sale markings |
| Text / container colour | irrelevant (geometry only) |

NO AMS output: `plate_1.obj` = base (upright; X length, Y depth, Z height),
`plate_2.obj` = name (flat; X length, Y letter height, Z 4 mm). SnapSTL tells them apart by
height, not by order.

**Helper loop** (`helper/content.js`, one MakerWorld tab, one name at a time):
`GET /api/helper/next` → fill the form (labels located by XPath, React inputs set via the
native value setter, MUI menus opened with `mousedown`) → click Generate → `inject.js` (page
world) copies the `plate_1/plate_2.obj` responses → `POST /api/helper/jobs/{id}/result`.
Failures → `/fail` (one retry, then *Failed*). A reload mid-name → `/release` (back to the queue).
Pause in the app → `next` returns nothing. Pace: pause between names (Settings, default 3 s);
MakerWorld takes ~4–10 s per new name.

**Security checks:** the helper never touches them. If Cloudflare shows a check, the helper
says *Complete the MakerWorld security check in this tab* and waits; the user clicks it.
Never add code that hides automation, solves checks, or spoofs a browser.

**Helper ↔ app trust:** the helper has no token. The app accepts `chrome-extension://` origins
**only** on `/api/helper/*` and only in local mode on `127.0.0.1` — every other cross-site request
is rejected (see §9).

## 6. Scaling Rules v1.1 (scaling.py) — agreed with the business

Full background: `docs/scaling-rules-v1.1-handoff.md`.

Target finished organizer size by letter count (editable in Settings; defaults are picks
inside the agreed ranges):

| Letters | Agreed length | Agreed height | Default L × H | Compartments |
|---|---|---|---|---|
| 1–3 | 190–200 mm | 90 | 195 × 90 | 4 |
| 4 | 195–210 | 89–90 | 200 × 90 | 4 |
| 5 | 200–220 | 88–90 | 210 × 89 | 5 |
| 6–7 | 215–230 | 86–89 | 222 × 87.5 | 5 |
| 8–10 | 225–240 | 85–88 | 232 × 86.5 | 6 |
| 11+ | ≤ 240 | 85–88 | 240 × 86 | 6 |

Axis rules (binding — tested by the business for glue fit):

```
Base X = L / original base X     Base Y = 100 %     Base Z = H / original base Z
Name X = Base X                  Name Y = Base Z    Name Z = 50 %   (4 mm → 2 mm)
```

Hard limits: height 85–90 mm, length ≤ 240 mm. Warnings are raised (not silently fixed) for
odd orientation, name longer than base, or letter distortion (X/Z scale ratio outside 0.6–1.6).
Verified against real MakerWorld output: "Ayra" → base 171.6×51×96.2 mm → 116.6 % / 93.5 %,
matching the handoff's 116.56 % / 93.51 %.

---

## 7. Plates (grouping.py, packing.py, export.py)

* Bed 256 × 256 × 256 mm, 5 mm edge margin, 5 mm gap between parts (Settings).
* Parts are only rotated **about Z** (default 0°/180°) — the build face never changes.
* **Names:** same colour only, never mixed. 3 per plate, dropping to 2, then 1 if they don't fit.
  A name is never shrunk on its own (it must match its base for gluing).
* **Bases:** always 3 per plate (only the very last plate may have fewer). First each colour's
  full groups of 3 (so base plates line up with name plates), then leftovers pooled into mixed plates.
* Packing uses each part's real 2-D footprint (shapely): parts stack along one axis and slide
  back until exactly `gap` mm apart, so flipped names nest. Every layout is verified (inside
  margin, gap respected) and every written STL is reloaded and re-checked.

File names:

```
Bases/{Ayra,pink},{Manvi,pink},{Riya,pink}.stl
Bases/{Kabir,blue},{Zoya,blue},{Tara,green}.stl        mixed colours (bases only)
Names/pink/{Ayra,pink},{Manvi,pink},{Riya,pink}.stl    one folder per name colour
```

Each STL is one merged mesh in bed coordinates (sits on Z = 0) — open in Bambu Studio as is.

---

## 8. Batches & downloads (batches.py)

Batches never start by themselves:

| Status | Meaning | Buttons |
|---|---|---|
| `created` | orders reserved (`in_batch`), nothing downloading | **Start download**, Cancel |
| `waiting` (Downloading) | models queued for the helper | Build plates now (partial), Cancel |
| `models_ready` | every model downloaded (or failed) | **Build plates**, Cancel |
| `build_requested` → `building` | plates being made | — |
| `done` / `done_with_problems` / `failed` / `cancelled` | finished | Open, ZIP, Show in Finder |

1. **Create batch** (selected `ready` orders) → `created`. **Start download** queues every model not
   in the cache in `downloads` (de-duplicated by cache key = name + model options + compartments +
   license-password hash).
2. **The helper** works through the queue (one MakerWorld tab, one name at a time). Failed names
   retry once, then show as *Failed* (Retry button). Pausing, a closed tab or an app restart never
   loses work: interrupted names go back to *Waiting* (claims also expire after 6 minutes).
3. When all models are in, the batch becomes `models_ready`. The **builder thread** builds only after
   **Build plates** (or *Build plates now* with what's ready — orders still missing a model go back
   to `ready`).
4. Output: `<output>/<YYYY-MM-DD>/Batch N - <YYYY-MM-DD>/{Bases/, Names/<colour>/, report.csv}` —
   N restarts at 1 each day (date = the day the plates are built; the app's internal batch # is
   separate and shown in the UI next to the folder name). Plain STL files, **no ZIP is
   written** (owner's request). *Download ZIP* in the UI builds one on demand in a temp file only.
   Orders become `bundled`. Leftover downloads nobody needs are cancelled automatically.

---

## 9. Database maintenance (maintenance.py, Settings → Database)

* **Back up now** — consistent SQLite copy to `backups/` (newest 20 kept).
* **Clean up** (older than N days, default 30) — removes *Not needed* orders, finished batches,
  download history and report-upload records. **Bundled (made) orders are always kept**: their
  Order IDs stop a later report from bringing a printed order back as *Ready*. Backup first.
* **Reset database** — type RESET; empties everything (optionally the model cache). Backup first.
  Batch folders/ZIPs on disk are never deleted by SnapSTL.

## 10. Security

* Local mode (no portal password set): the service only answers `127.0.0.1`/`localhost`, and
  rejects cross-site requests (other websites in the browser) and DNS-rebinding host names.
* Optional password (`uv run snapstl-admin set-password`) enables a login page + signed session
  cookie — only needed if the portal is ever exposed beyond this Mac.
* The helper extension is accepted only on `/api/helper/*` from this Mac.
* Secrets only in the Keychain. Customer name/phone/address are stored only in the local database.

---

## 11. Known limits / open items

* MakerWorld's page is undocumented; a redesign can break `helper/content.js` (form labels,
  `plate_N.obj` file names, the Generate button text). Fix there first, then reload the
  extension in `chrome://extensions`.
* The MakerWorld tab must stay open (ideally in its own window — Chrome slows hidden tabs).
* Bambu Studio open/slice of the generated plates is a manual review step.
