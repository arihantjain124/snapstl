# SnapSTL

Turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU `Magicians_35`)
into print-ready Bambu Lab plate STLs — automatically generated on MakerWorld, scaled to the
agreed rules, grouped 3 per plate, and zipped per batch.

Runs entirely on one Mac: a background service with a web portal at **http://127.0.0.1:8317**,
plus a SnapSTL app window onto the same portal.

> How it works and why: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.
> Working on the code with an AI agent: **[AGENTS.md](AGENTS.md)**.

---

## Install (on the dedicated Mac)

Requirements: macOS 12+, Google Chrome, internet. Everything else is installed by the script.

```bash
./scripts/make_app.sh
```

This installs:
- **SnapSTL service** — starts at login, restarts itself if it stops, keeps the Mac awake.
- **~/Applications/SnapSTL.app** — opens the SnapSTL window (drag it to the Dock).

Re-run the script after moving the folder or updating the code. Uninstall the service with
`launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist`.

## One-time setup

1. **Install the SnapSTL Helper** in Chrome (the browser where you use MakerWorld):
   `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → choose the
   `helper` folder in this project (or download it from Settings → SnapSTL Helper).
2. In Chrome, sign in to MakerWorld and open the **model maker** page (link in Settings).
   Leave that tab open — ideally in its own window.
3. In SnapSTL **Settings**: MakerWorld license password → Save; output folder → *Choose…*;
   check the model options and the scaling / compartments table.

## Daily use

1. **Orders → Upload report (.xlsx)** — the webstore *Order Reconciliation Report*. Only new
   `Magicians_35` orders are added; re-uploading never duplicates. Orders already delivered,
   in transit, packed, returned or cancelled go to those tabs and are never made.
2. **Needs review** — fix names (spaces, symbols, missing colour) and **Approve**.
   Tabs: Ready (Single / Clubbed), Needs review, In a batch, Bundled, Skipped, Delivered, RTO,
   In Transit, Cancelled, Others, All. Filter any column with the boxes under its heading.
3. Tick orders (or **Select all matching** your filters) → **Create batch**.
   On **Batches**: press **Start download**; when it says *Ready to build*, press **Build plates**.
4. The helper (in your MakerWorld tab) generates each name, one at a time — the header shows what
   it's doing. If MakerWorld shows a security check in that tab, just complete it; it carries on.
5. **Batches** tab: progress per batch and the **Download queue**. When a batch is done:
   **Show in Finder** or **Download ZIP**.
6. Open the plates in Bambu Studio, check, slice, print. Bases in white; names in their colour.

Output per batch:

```
<output folder>/batch-001_2026-11-14_231500/
    bases/{Ayra,pink},{Manvi,pink},{Riya,pink}.stl
    names/{Ayra,pink},{Manvi,pink},{Riya,pink}.stl
    report.csv               original sizes, target sizes, scale % per axis, files, warnings
<output folder>/batch-001_2026-11-14_231500.zip
```

## Troubleshooting

| You see | Do |
|---|---|
| **Helper not connected** | open the MakerWorld model maker tab in Chrome; check the helper is enabled in `chrome://extensions` |
| Helper says **Complete the MakerWorld security check** | click the check in that tab |
| A name shows **Failed** in the download queue | read *Detail*; **Retry failed** |
| Batch stuck *Waiting for models* | check the header / download queue; **Pause/Resume**; *Build now* builds with what's ready |
| Portal doesn't open | `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; see `~/Library/Logs/SnapSTL.log` |
| Database getting big | Settings → Database → **Clean up** (made orders are kept; a backup is saved first) |

Data lives in `~/Library/Application Support/SnapSTL` (database `snapstl.db`, model cache,
settings, `backups/`). Settings → Database has Back up / Clean up / Reset.

## Development

```bash
uv sync                 # install deps (incl. dev)
uv run pytest           # 20 tests: scaling, packing, grouping, ingest, batches, helper API, DB maintenance
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway dev instance with its own data
```
