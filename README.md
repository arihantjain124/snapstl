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

## One-time setup (Settings tab)

1. **MakerWorld login cookie** — in Chrome on makerworld.com (signed in): View → Developer →
   Developer Tools → **Network** → reload → click any `api/v1/…` request → under *Request Headers*
   copy the whole **cookie** value → paste → **Save**. SnapSTL checks it and keeps it in the Keychain.
2. **MakerWorld license password** (removes anti-sale markings) → Save.
3. **Output folder** → *Choose…* (app window) — each batch is saved here.
4. Check the model options (font, sizes) and the scaling / compartments table.

## Daily use

1. **Orders → Upload report (.xlsx)** — the webstore *Order Reconciliation Report*. Only new
   `Magicians_35` orders are added; re-uploading never duplicates. Orders already delivered,
   in transit, packed or cancelled are recorded as *Not needed*.
2. **Needs review** — fix names (spaces, symbols, missing colour) and **Approve**.
3. Tick orders (or **Select all matching** a colour) → **Create batch**.
4. SnapSTL opens its **MakerWorld window** and generates each name, one at a time.
   If MakerWorld shows a security check, the header says **Action needed** — click
   **Show MakerWorld window**, complete the check, and it carries on by itself.
5. **Batches** tab: progress per batch and the **Download queue**. When a batch is done:
   **Show in Finder** or **Download ZIP**.
6. Open the plates in Bambu Studio, check, slice, print. Bases in white; names in their colour.

Output per batch:

```
<output folder>/batch-001_2026-11-14_231500/
    bases/bases-(Ayra,Manvi,Riya,pink).stl
    names/(Ayra,Manvi,Riya)-(pink).stl
    report.csv               original sizes, target sizes, scale % per axis, files, warnings
<output folder>/batch-001_2026-11-14_231500.zip
```

## Troubleshooting

| You see | Do |
|---|---|
| **Action needed: MakerWorld security check** | *Show MakerWorld window* → complete the check |
| **MakerWorld login needed** | paste a fresh cookie in Settings |
| A name shows **Failed** in the download queue | read *Detail*; **Retry failed** |
| Batch stuck *Waiting for models* | check the header / download queue; **Pause/Resume**; *Build now* builds with what's ready |
| Portal doesn't open | `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; see `~/Library/Logs/SnapSTL.log` |

Data lives in `~/Library/Application Support/SnapSTL` (database `snapstl.db`, model cache,
settings). Back it up to keep order history.

## Development

```bash
uv sync                 # install deps (incl. dev)
uv run pytest           # 20 tests: scaling, packing, grouping, ingest, batches, downloader (stubbed)
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway dev instance with its own data
```
