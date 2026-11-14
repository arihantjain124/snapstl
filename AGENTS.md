# AGENTS.md — instructions for AI coding agents (Claude, Codex, Cursor, …)

You are working on **SnapSTL**, a local Mac app that turns webstore orders into print-ready
name-organizer STLs. The owner will describe changes in plain language (UI tweaks, new
settings, rule changes). Your job: make the change safely, keep the docs true, and commit.

## Read first (in this order)

1. `docs/ARCHITECTURE.md` — what every module does and **the decisions you must not break**.
2. `docs/scaling-rules-v1.1-handoff.md` — the business's original scaling spec.
3. `README.md` — how the owner installs and uses it.

## Ground rules (do not change without the owner explicitly asking)

- **Scaling axes:** Base X = Name X, Base Z = Name Y, Base Y = 100 %, Name Z = 50 %. Height 85–90 mm, length ≤ 240 mm.
- **Print version is always NO AMS**; names are **single line** (Name line 2 empty).
- **Compartments come from the scaling table**, never from the customer's order text.
- **Names never mix colours on a plate**; 3 → 2 → 1 by fit. **Bases are 3 per plate** (last may be fewer).
- **Parts only rotate about Z**; the build face never changes. A name is never scaled separately from its base.
- **Order ID is the de-duplication key**; a `bundled` order is never re-made unless the user clicks Remake.
- **MakerWorld:** generation happens in SnapSTL's own visible Chrome window, signed in with the user's cookie.
  Never add code that hides automation, spoofs browser fingerprints, solves CAPTCHAs/Cloudflare checks, or
  launches Chrome with `--no-sandbox`. Security checks are completed by the human; the app waits.
- **Privacy:** never store customer phone/address from the report; secrets only via `keychain.py`.
- Never commit customer files (`*.xlsx`, `*.csv` reports) or anything under `~/Library/Application Support/SnapSTL`.

## Where things live

| Change request | Edit |
|---|---|
| UI text, layout, buttons, new screen | `src/snapstl/static/index.html` (single file, vanilla JS, no build) |
| New setting / default value | `src/snapstl/settings.py` (+ `fillSettings`/`readSettings` in index.html) |
| Scaling targets / compartments per letter count | `default_bands()` in `settings.py` (users can also edit in the UI) |
| Scaling math | `src/snapstl/scaling.py` |
| Plate grouping rules / file names | `grouping.py`, `export.py` |
| Packing / collision logic | `packing.py` |
| Report columns / review rules | `ingest.py` |
| Batch / download queue behaviour | `batches.py` |
| MakerWorld page interaction | `browser_gen.py` (form labels, `DRIVER_JS`) |
| HTTP API | `server.py` |
| Install / service | `scripts/make_app.sh`, `src/snapstl/__init__.py` |

## How to work

```bash
uv sync                     # dependencies (Python 3.12 via uv)
uv run pytest -q            # must pass before every commit
```

- Add or update a test in `tests/` for any logic change (engine tests use synthetic meshes in `tests/conftest.py`;
  the downloader is stubbed via `DOWNLOADER.generate` in `tests/test_flow.py` — tests never contact MakerWorld).
- Try a change without touching the owner's real data:
  `SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320`
- Apply a change to the running app: `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`
  (UI-only changes just need a page reload). Logs: `~/Library/Logs/SnapSTL.log`.
- Database changes: add new columns via `MIGRATIONS` in `db.py` (never drop or rename existing columns —
  the owner's order history lives there).

## Git workflow

The repo uses `main` as the stable branch.

```bash
git status                                  # see what changed
git switch -c change/<short-name>           # one branch per change request
# … edit, run: uv run pytest -q …
git add -A && git commit -m "<what changed and why>"
git switch main && git merge --no-ff change/<short-name>   # when the owner is happy
git tag v0.X.Y                              # optional: tag releases the owner installs
```

- Small, focused commits with plain-English messages ("Bases: allow 2 per plate when …").
- Never commit failing tests. Never rewrite history on `main` (no force-push, no rebase of `main`).
- To undo a bad change: `git revert <commit>` (keeps history), not `git reset --hard`.
- Before handing the project on as a ZIP: commit everything, then `git archive -o snapstl.zip HEAD`
  (or zip the folder **including** `.git` so history travels with it). Exclude `.venv/`.

## Keep the docs true

When you change behaviour, update in the same commit:
- `docs/ARCHITECTURE.md` (decisions, tables, state names),
- `README.md` (if the owner's steps change),
- this file (if the workflow or the rules change).
