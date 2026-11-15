# AGENTS.md — instructions for AI coding agents (Claude, Codex, Cursor, …)

You are working on **SnapSTL**: a local Mac app that turns webstore orders into print-ready
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
  **.3mf** per plate, file names `{name,color},{name2,color2}.3mf`, **never write ZIP files**.

**Technical rules**
- MakerWorld models come **only** from the SnapSTL Helper (`helper/`) in the user's own Chrome.
  Direct API calls and app-controlled browsers were tried and **don't work** (ARCHITECTURE §16) — don't retry them.
- Never add code that hides automation, spoofs browser fingerprints, or solves CAPTCHAs/Cloudflare
  checks. Security checks are completed by the human; the helper waits.
- The app accepts extension calls **only** on `/api/helper/*` from 127.0.0.1. Don't widen that.
- The helper must keep checking, before every name, that its working tab is on the configured model
  page (designId); the app must never hand out work when `on_page` is false.
- Other computers: off by default; only with `lan_access` + portal password; they must sign in and can
  never use Mac-only actions (`MAC_ONLY_PREFIXES`, `…/reveal`, folder / model link / password changes).
- Database: add columns **only** via `MIGRATIONS` in `db.py`; never drop/rename columns or tables.
- Clean-up must keep bundled orders. Every destructive maintenance action takes a backup first.
- No new runtime dependencies, frameworks or build steps without the owner asking. The UI stays one
  file (`static/index.html`, vanilla JS).

**Privacy**
- Customer name/phone/address live only in the local database. Never send them anywhere, log them, or
  put them in git. License passwords live only in `settings.json` (per model) — never in model folders
  (`info.json`), logs, reports or git. The helper's `check` carries only booleans/"sent" — never the password.
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
| Report import / review rules | `ingest.py` |
| Batch lifecycle / download queue / Re-download / Rebuild | `batches.py` (+ endpoints in `server.py`) |
| Model link / per-model license passwords | `settings.py` (`makerworld_url`, `license_passwords`, `design_id`) + Settings card in `index.html` |
| Watermark check / license confirmation | `watermark.py`, `batches.check_license` / `store_result`, `helper/content.js` (`runJob`), `helper/inject.js` |
| Plate / object pictures | `render.py`, `pipeline._plate_picture`, `server.model_view` |
| Access control / other computers | `server._blocked`, `server.access_control`, `/api/portal`, `__init__.serve` (bind address) |
| Downloads page (queue + folder) | `<section id="t-downloads">` in `index.html`; `cache.py` for the folder |
| MakerWorld page interaction | `helper/content.js`, `helper/inject.js` (bump `helper/manifest.json` version; reload extension) |
| Backups / clean-up / reset | `maintenance.py` |
| Install / service / window | `scripts/make_app.sh`, `src/snapstl/__init__.py` |

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
