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
4. The files you'll touch. Read them fully before editing.

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
  person presses **Start print** in Print (never automatically after a build), and only on a
  printer that has the plate's colour loaded (white for bases). Printers have no Bases / Names
  setting: any printer can print either. Never skip Bambuddy's "Clear plate" step. SnapSTL never talks to printers directly.
- **No print starts by itself:** every queue item is created with `manual_start: true` (held) and is
  released only by a person's click (**Start print** → `autostart`, or `farm.start_job`). Never create
  a queue item that can start on its own, and never start one from a code path a person didn't click.
  Reprint only after a person confirms a failure.
- Printers, filament and printer settings are managed in Bambuddy; the Printers page stays read only
  (no adding printers, filament or roles in SnapSTL).

**Technical rules**
- MakerWorld models come **only** from the SnapSTL Helper (`helper/`) in the user's own Chrome.
  Direct API calls and app-controlled browsers were tried and **don't work** (ARCHITECTURE §16) — don't retry them.
- Never add code that hides automation, spoofs browser fingerprints, or solves CAPTCHAs/Cloudflare
  checks. Security checks are completed by the human; the helper waits.
- The app accepts extension calls **only** on `/api/helper/*` from 127.0.0.1. Don't widen that.
- The helper must keep checking, before every name, that its working tab is on the configured model
  page (designId); the app must never hand out work when `on_page` is false.
- Other computers: off by default; only with `lan_access` + portal password; they must sign in and can
  never use Mac-only actions (`MAC_ONLY_PREFIXES`, `…/reveal`, folder / model link / password changes,
  `/api/farm/*`).
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
  under `irm … | iex` (Windows PowerShell 5.1, ASCII only, no `exit`). Slicing on this computer is gone for good (#43).
- Database: add columns **only** via `MIGRATIONS` in `db.py`; never drop/rename columns or tables.
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
  (`ingest.status_stamp`); never let an older report undo a newer status.
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
  model (`farm.target`); bases sliced white, names in their colour. SnapSTL keeps **no slicing
  settings**: only references (source + id + name) to Bambuddy profiles in `presets_by_model`. Never
  add a slicer on this computer, a preset store/import, or use of hand-made sliced files again
  (owner decision, ARCHITECTURE #43). Profiles are added in Bambuddy, never by SnapSTL.
  Slicing never blocks the worker: one slice at a time, followed by polling, stopped after
  `SLICE_TIMEOUT_S` without progress. Automatic slicing may run on its own (it never prints) and
  never retries a failed slice; a print always uses the slice for its printer's model.
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
| Order tabs & filters | `server.TABS`, `server.FILTERS` (+ `TABS` array in `index.html`) |
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
| Access control / other computers | `server._blocked`, `server.access_control`, `/api/portal`, `__init__.serve` (bind address) |
| Download queue + folder (on Batches) | `<section id="t-batches">` in `index.html`; `cache.py` for the folder |
| MakerWorld page interaction | `helper/content.js`, `helper/inject.js` (bump `helper/manifest.json` version; reload extension) |
| Backups / clean-up / reset | `maintenance.py` |
| Install / service / window | Mac `scripts/make_app.sh`; Windows `scripts/windows/*.ps1`; `src/snapstl/__init__.py` |
| Mac vs Windows (folders, Finder/Explorer, keep awake, restart, LAN name) | `platform_os.py` |
| Shared order records (Supabase): connection in Settings, sync, claims | `sync.py`, `config.py`, `computer.py`, `server.shared_records_*`, `server.setup_status`, `dbtool.py` (`snapstl-db`), triggers in `db.POST_SCHEMA` |
| Print farm: Bambuddy connection, printers, Push to queue, slicing | `farm.py` (+ `/api/farm*`, `/api/batches/{id}/plates` and `/push`, `/api/print-jobs` in `server.py`); Printers page + Push to queue panel in `index.html`; `tests/test_farm.py` |
| Print tab (Bases / Names / Completed / Removed), Ready to ship | `makeit.py`, `/api/makeit*` in `server.py`, `server.TABS` (`ready_to_ship`), `<section id="t-makeit">` in `index.html`; `tests/test_farm.py` |
| Slicing tab (Bambuddy profiles per printer model, print defaults per printer) | `farm.py` (`bambuddy_presets`, `chosen_presets`, `set_presets`, `missing_presets`, `target`, `print_defaults`, `forget_local_slicing`), `/api/makeit/presets` `/print-defaults`; `<section id="t-slicing">`; `tests/test_farm.py` |
| Printers page, sidebar status | `farm.everything` / `summarize`, `/api/printer-status`, `/api/health`; `<section id="t-printers">`, `loadHealth` in `index.html` |
| Settings sections (Setup · Product · Plates · Folders · Connections · Maintenance) | `data-sec` blocks + `setSec` in `index.html`; checklist from `/api/setup` |
| Bambuddy computer (Docker: Bambuddy + slicer sidecars) | `scripts/windows/bambuddy-server.ps1`, `scripts/bambuddy-docker/docker-compose.yml`, `scripts/bambuddy-version.txt` |
| SnapSTL installers and Bambuddy (`SNAPSTL_BAMBUDDY`, `SNAPSTL_BAMBUDDY_URL`) | `scripts/windows/common.ps1` (`Install-Bambuddy`, `Connect-Bambuddy`), `scripts/make_app.sh` (Mac: connect only); older native install (Windows): `scripts/windows/bambuddy.ps1`, `platform_os.update_bambuddy` |
| Sidebar "This computer" address | `server._network` (in `/api/health`), `platform_os.lan_ips`, `#hip` in `index.html` |
| Printed tab / report status timestamps | `ingest.set_printed`, `ingest.status_stamp`, `server.TABS`, `index.html` (`SELKIND`) |

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
