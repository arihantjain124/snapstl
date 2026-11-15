# SnapSTL

Turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU `Magicians_35`)
into print-ready Bambu Lab plate files (**.3mf**): each name is generated on MakerWorld, scaled to the
agreed rules, grouped 3 per plate, and saved in dated batch folders.

Everything runs on one Mac: a background service with a web portal at **http://127.0.0.1:8317**,
the SnapSTL app window onto the same portal, and the **SnapSTL Helper** in Chrome.

- How it works, tech spec, every file explained: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
- Changing the software with an AI agent: **[AGENTS.md](AGENTS.md)**

---

## Install (on the dedicated Mac)

Needs macOS 12+, Google Chrome and internet. Put the project folder somewhere permanent (not
Downloads), then in Terminal inside the folder:

```bash
chmod +x scripts/make_app.sh
./scripts/make_app.sh
```

This installs:
- **SnapSTL service** — starts at login, restarts itself if it stops, keeps the Mac awake.
- **~/Applications/SnapSTL.app** — opens the SnapSTL window (drag it to the Dock).

Re-run the script after moving the folder or updating the code. To uninstall:

```bash
launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist && rm -r ~/Applications/SnapSTL.app
```

## One-time setup

1. **Install the SnapSTL Helper** in Chrome (the Chrome you use for MakerWorld):
   `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → choose the `helper`
   folder in this project.
2. In SnapSTL **Settings**: check the **MakerWorld model page link**, enter the **license password for
   this model** (each model keeps its own), choose the output and downloads folders, check the model
   options and the scaling / compartments table → **Save settings**.
3. In that Chrome, sign in to MakerWorld, click the helper icon → **Open model page** (or open the page
   and click **Use this tab**). The helper only works in that tab and only on that exact page. Keep it
   open, ideally in its own window.

## Daily use

1. **Orders → Upload report (.xlsx)** — the webstore *Order Reconciliation Report*. Only
   `Magicians_35` orders are kept, and re-uploading never creates duplicates.
2. Tabs: **Ready** (sub-tabs **Single** / **Clubbed**), **Needs review**, In a batch, Bundled,
   Skipped, Delivered, RTO, In Transit, Cancelled, Others, All. Use the filter boxes under each
   column heading; filters combine. In *Needs review*, fix the name/colour and **Approve**.
3. Tick orders (or **Select all matching**) → **Create batch**.
4. **Batches** → **Queue download**. The helper generates each name in your MakerWorld tab, one at a
   time; watch it on the **Downloads** page. If MakerWorld shows a security check in that tab,
   complete it — it carries on.
5. When the batch says **Ready to build** → **Queue build**. Nothing starts by itself.
6. Check the batch says **Fit check: N/N** (every name keeps its base's proportions, so it fits after
   printing; a pair that fails is never printed and goes back to Ready).
7. **Show in Finder**, open the plates in Bambu Studio, check, slice, print. Bases in white; names in
   their colour folder's colour.

Finished batches can be redone: **Re-download** fetches every model again from MakerWorld;
**Rebuild plates** makes the plates again from the downloaded models into a new Batch folder (the old
one stays).

Downloaded models are kept in **`~/SnapSTL/Downloads`** (one folder per name, e.g.
`SONAL - 5 comp - …`), reused by later batches, and **never deleted by SnapSTL** — clean up in Finder
(Downloads page → *Show in Finder*). The folder can be changed in Settings.

The app has a side menu — **Orders**, **Batches**, **Downloads**, **Settings**, **Help**. Help explains every tab,
status, file and setting.

Output:

```
<output folder>/2026-11-15/                       one folder per day
    Batch 1 - 2026-11-15/                        numbering restarts at 1 every day
        Bases/{Ayra,pink},{Manvi,pink},{Riya,pink}.3mf
        Names/pink/{Ayra,pink},{Manvi,pink},{Riya,pink}.3mf
        Names/blue/{Kabir,blue}.3mf              one folder per name colour
        report.csv                               sizes, scale % per axis, files, warnings
    Batch 2 - 2026-11-15/
```

Each .3mf is one plate; every base or name is a separate, named object in Bambu Studio. No ZIP files
are created (the *Download ZIP* button is optional and saves nothing).

## Using it from other computers

SnapSTL is a small web server on the main Mac. To use it from other computers on the same network:
main Mac → **Settings → Other computers** → tick *Allow other computers*, set a portal password →
**Save** (allow incoming connections if macOS asks). Open the address shown there (e.g.
`http://your-mac.local:8317`) in any browser and sign in. The main Mac keeps running the service and
the Chrome helper; downloads always happen there. Off by default.

## Troubleshooting

| You see | Do |
|---|---|
| **Helper not connected** | open the MakerWorld model maker tab in Chrome; check the helper is enabled in `chrome://extensions` |
| **Helper tab is not on the model page** | helper icon → **Open model page** (or open it and **Use this tab**) |
| Helper says **Complete the MakerWorld security check** | click the check in that tab |
| A name shows **Failed** in the download queue | read *Detail*; **Retry failed** |
| Batch stays *Downloading* | check the sidebar and the **Downloads** page; **Pause / Resume**; *Build now with what's ready* |
| A model looks wrong | Batches → **Re-download**, then **Rebuild plates** |
| Can't select / copy text in the window | quit and reopen SnapSTL.app |
| Portal doesn't open | `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; see `~/Library/Logs/SnapSTL.log` |
| Database getting big | Settings → Database → **Clean up** (orders already made are kept; a backup is saved first) |

Your data: orders database, settings and backups in `~/Library/Application Support/SnapSTL`;
downloaded models in `~/SnapSTL/Downloads`; plates in `~/SnapSTL/output`. License passwords (per model)
are in SnapSTL's settings on this Mac. SnapSTL never deletes downloaded models or batch folders.

## Development

```bash
uv sync                 # install dependencies (incl. dev)
uv run pytest -q        # 26 tests: scaling, packing, grouping, 3MF, ingest, batches, helper API, maintenance
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway dev instance with its own data
```
