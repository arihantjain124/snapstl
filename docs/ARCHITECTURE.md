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
   → each name is generated on MakerWorld (NO AMS → base + name separately)   (browser_gen.py)
   → scale with Scaling Rules v1.1                                             (scaling.py)
   → group into plates of 3 and pack collision-free on a 256×256 bed           (grouping.py, packing.py)
   → merged STL per plate + report.csv + ZIP per batch                         (export.py, batches.py)
```

---

## 2. Runtime shape (local Mac only)

| Piece | What it is |
|---|---|
| **Service** | `snapstl-serve` (FastAPI + uvicorn) on `http://127.0.0.1:8317`, started at login by launchd (`~/Library/LaunchAgents/local.snapmagicians.snapstl.plist`, `KeepAlive`). Runs the web portal, the download worker and the batch builder. Keeps the Mac awake (`caffeinate`) while running. |
| **App** | `~/Applications/SnapSTL.app` → `snapstl` → opens a pywebview window onto the same portal (adds a native folder picker). Closing the window does **not** stop the service. |
| **MakerWorld window** | A separate Chrome window owned by SnapSTL (Playwright, persistent profile `~/Library/Application Support/SnapSTL/chrome-profile`), signed in with the user's pasted cookie. Opened per download run, reused for every name, closed when the queue is empty. |
| **Data** | `~/Library/Application Support/SnapSTL/` — `snapstl.db` (SQLite), `cache/` (unscaled meshes per name), `settings.json`. Secrets (MakerWorld cookie, license password) in the macOS Keychain. |
| **Output** | Chosen in Settings (default `~/SnapSTL/output`). One folder + one ZIP per batch. |
| **Log** | `~/Library/Logs/SnapSTL.log` |

Decision history: a cloud server (AWS Lightsail) and a Chrome extension were both tried and
**removed** — the server was unreliable and the extension/server sync was fragile. Everything
now runs on one dedicated Mac.

---

## 3. Tech stack

| Area | Choice |
|---|---|
| Language / env | Python 3.12, managed by **uv** (`pyproject.toml`, `uv.lock`) |
| Web | FastAPI + uvicorn; UI is one static page `src/snapstl/static/index.html` (vanilla JS, no build step) |
| Window | pywebview (Cocoa/WKWebView) |
| Browser automation | Playwright driving **Google Chrome** (visible, not headless) |
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
* Stored fields are minimal: order id, clubbed id, date (ISO), report status, quantity, name as
  typed, name to print, colour. **No customer contact details are stored.**

Order states:

| State | Meaning |
|---|---|
| `ready` | report status `PRINT`, clean name + colour |
| `needs_review` | `PRINT` but name has a space / removed symbols / non-Latin letters / >15 letters, or no name/colour, or a special request. Edit + Approve in the UI. |
| `in_batch` | selected into a batch that is still downloading / building |
| `bundled` | in a finished batch — never made again unless **Remake** |
| `not_needed` | delivered / in transit / packed / cancelled / returned (or skipped by hand) |

Name cleaning: emoji and symbols are dropped silently; other characters removed → review;
colours are lower-cased (`gray` → `grey`). Quantity N → N organizers.

---

## 5. MakerWorld model (settings.py, browser_gen.py)

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

**Why a real browser:** MakerWorld only processes *Generate* requests that come from its web
page — the identical request from a program (Python, curl, HTTP/1.1 or 2) never gets an answer.
So SnapSTL fills the real page in its own Chrome window. It **never** tries to get around
Cloudflare: when a security check appears, the download pauses, the UI shows *Action needed*
+ *Show MakerWorld window*, and the user completes it; then the same name is retried.

Login: the user pastes the `cookie` request header from a signed-in Chrome tab (DevTools →
Network). It is checked against MakerWorld's account API and stored in the Keychain; SnapSTL
never handles the MakerWorld password.

Pace: one name at a time, a pause between names (Settings, default 3 s). MakerWorld itself
takes ~4–10 s per new name.

---

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
names/(Ayra,Manvi,Riya)-(pink).stl
bases/bases-(Ayra,Manvi,Riya,pink).stl                 all three same colour
bases/bases-(Kabir,Zoya,Tara,blue,blue,green).stl      mixed colours
```

Each STL is one merged mesh in bed coordinates (sits on Z = 0) — open in Bambu Studio as is.

---

## 8. Batches & downloads (batches.py)

1. **Create batch** (selected `ready` orders) → orders become `in_batch`; every model not in the
   cache is queued in `downloads` (de-duplicated by cache key = name + model options + compartments
   + license-password hash).
2. **Downloader thread** opens one MakerWorld session and works through the queue. Failed names
   retry once, then show as *Failed* (Retry button). Pausing, a closed window or a restart never
   loses work: interrupted names go back to *Waiting*.
3. **Builder thread** builds a batch as soon as all its models are cached (or on **Build now** with
   what's ready — orders still missing a model go back to `ready`).
4. Output: `<output>/batch-NNN_<date>/{bases/, names/, report.csv}` + `batch-NNN_<date>.zip`.
   Orders become `bundled`. Leftover downloads nobody needs are cancelled automatically.

---

## 9. Security

* Local mode (no portal password set): the service only answers `127.0.0.1`/`localhost`, and
  rejects cross-site requests (other websites in the browser) and DNS-rebinding host names.
* Optional password (`uv run snapstl-admin set-password`) enables a login page + signed session
  cookie — only needed if the portal is ever exposed beyond this Mac.
* Secrets only in the Keychain; the order report's customer contact data is never stored.

---

## 10. Known limits / open items

* MakerWorld's page and request formats are undocumented; a site redesign can break
  `browser_gen.py` (form labels, `plate_N.obj` file names). Fix there first.
* The cookie expires periodically → header shows *MakerWorld login needed* → paste a new one.
* Bambu Studio open/slice of the generated plates is a manual review step.
