# SnapSTL

Turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU `Magicians_35`)
into print-ready Bambu Lab plate files (**.3mf**): each name is generated on MakerWorld, scaled to the
agreed rules, grouped 3 per plate, and saved in dated batch folders.

Runs on **macOS and Windows**. Each computer runs a background service with a web page at
**http://127.0.0.1:8317**, the SnapSTL app window onto that page, and the **SnapSTL Helper** in Chrome.
Several computers can share one database (Supabase) and work on the same orders.

- How it works, every file explained: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
- Changing the software with an AI agent: **[AGENTS.md](AGENTS.md)**

**Contents:** [What you need](#what-you-need) · [1. Repository access](#1-give-the-computer-read-only-access-to-the-repository) ·
[2a. Install on a Mac](#2a-install-on-a-mac) · [2b. Install on Windows](#2b-install-on-windows) ·
[3. The .env file and shared database](#3-the-env-file-and-the-shared-database) · [4. Helper and settings](#4-helper-and-settings) ·
[Updates](#updates) · [Daily use](#daily-use) · [Several computers](#several-computers) · [Troubleshooting](#troubleshooting)

---

## What you need

- **macOS 12+** or **Windows 10/11**, with an internet connection.
- **Google Chrome**, signed in to the MakerWorld account you use for the organizer.
- **Bambu Studio**, to open and print the plates.
- The model's **license password**. SnapSTL refuses to download without it, so models never carry
  MakerWorld's watermark.
- On Windows: **Git for Windows** (https://git-scm.com/download/win, install with the default options).
- The repository owner, for one minute in step 1.

## 1. Give the computer read-only access to the repository

Each computer gets its own **deploy key**: it can download SnapSTL but not change it, and it can be
removed on its own if the computer is lost. No GitHub account is needed on the computer.

**Mac:** open **Terminal**. Install Apple's command-line tools (includes git; click **Install** if a
window opens, or carry on if they're already there):

```bash
xcode-select --install
```

**Windows:** open **Git Bash** (Start menu, installed with Git for Windows).

Then, on either, create this computer's key and copy it to the clipboard:

```bash
mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N "" -C "snapstl $(hostname)" -f ~/.ssh/snapstl_deploy && printf '\nHost github-snapstl\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/snapstl_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' >> ~/.ssh/config && (pbcopy 2>/dev/null || clip) < ~/.ssh/snapstl_deploy.pub && echo "Key copied to the clipboard"
```

The owner adds it on GitHub (send them the copied text; it isn't secret): the repository →
**Settings → Deploy keys → Add deploy key** → title = the computer's name, paste the key, leave
**Allow write access** off → **Add key**.

Check it works (prints `successfully authenticated`):

```bash
ssh -T github-snapstl 2>&1 | grep -o "successfully authenticated"
```

## 2a. Install on a Mac

In **Terminal**:

```bash
git clone github-snapstl:arihantjain124/snapstl.git ~/SnapSTL-app && cd ~/SnapSTL-app && zsh scripts/make_app.sh
```

This installs uv (the Python tool manager) if needed, the **SnapSTL service** (starts at login,
restarts itself if it stops, keeps the Mac awake) and **SnapSTL.app** in `~/Applications`.

Already have SnapSTL from a zip in `~/SnapSTL-app`? Turn that folder into a git copy in place (your
orders, settings and downloads aren't in it and are kept):

```bash
cd ~/SnapSTL-app && git init -q -b main && git remote add origin github-snapstl:arihantjain124/snapstl.git && git fetch -q origin && git reset -q --hard origin/main && git branch -q --set-upstream-to=origin/main main && zsh scripts/make_app.sh
```

Check it's running (prints `200`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8317/
```

## 2b. Install on Windows

In **Git Bash**, download SnapSTL:

```bash
git clone github-snapstl:arihantjain124/snapstl.git ~/SnapSTL-app
```

Then open **PowerShell** (Start menu) and install it:

```powershell
cd $HOME\SnapSTL-app; powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

This installs uv if needed, a background task **SnapSTL** (starts when you log in, restarts SnapSTL if
it stops) and **SnapSTL** shortcuts in the Start menu and on the Desktop. Windows may ask once whether
to allow SnapSTL on the network: allow it only if you'll use the web portal from other computers.

Keep the folder at `C:\Users\<you>\SnapSTL-app`: the task and the Chrome helper run from it.
SnapSTL's own files: `%LOCALAPPDATA%\SnapSTL` (settings, local database, backups, log `SnapSTL.log`).

## 3. The .env file and the shared database

Each computer has a `.env` file in its SnapSTL folder for things that must never go into git: the
shared database's address and password, and the computer's name. **Skip this section** to keep the
computer on its own local database (as before).

### Once: create the database on Supabase (owner)

1. Sign up at **supabase.com** in the business's name → **New project**.
   - Name: `snapstl`. Region: **South Asia (Mumbai)** (keeps the data in India).
   - **Database password**: long, **letters and digits only** (symbols like `@ : / # ? %` break the
     address). Keep it in a password manager.
   - Plan: **Pro** for real use (the free plan pauses after a week without use).
2. In the project, click **Connect** → **Connection string** → type **URI** → **Session pooler**
   (works on every network). Copy it; it looks like
   `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
   Replace `[YOUR-PASSWORD]` (including the brackets) with the database password.
3. Nothing else to set up: SnapSTL creates its tables itself, in their own schema `snapstl`, which
   Supabase's public web API doesn't expose. Supabase keeps daily backups (Pro).

The database holds orders, including the customer names, phone numbers and addresses from the
report. Share the connection string only with computers that run SnapSTL, and never paste it into
chat, email or git.

### On every computer: the .env file

**Mac** (Terminal):

```bash
cd ~/SnapSTL-app && cp .env.example .env && open -e .env
```

**Windows** (PowerShell):

```powershell
cd $HOME\SnapSTL-app; copy .env.example .env; notepad .env
```

Fill in and save:

```ini
SNAPSTL_DATABASE_URL=postgresql://postgres.abcdefghijkl:YourPassword123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
SNAPSTL_COMPUTER_NAME=Packing Mac
```

`SNAPSTL_COMPUTER_NAME` is how the computer appears on its batches (default: the computer's own name).

Check the connection, then restart SnapSTL so it uses it:

```bash
cd ~/SnapSTL-app && ~/.local/bin/uv run snapstl-db check
```

```bash
launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl
```

On Windows (PowerShell):

```powershell
cd $HOME\SnapSTL-app; uv run snapstl-db check; powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1
```

The sidebar then shows the computer's name and "shared database".

### Once: move the existing orders into the shared database

On the computer that has been running SnapSTL so far (it has the orders), after its `.env` is set and
**before** other computers start making batches:

```bash
cd ~/SnapSTL-app && ~/.local/bin/uv run snapstl-db move-local
```

It copies orders, uploads, batches and the download history. Its batches stay that computer's
batches, and its local database stays as a backup. It refuses to run twice, or if the shared database
already has orders.

## 4. Helper and settings

On every computer:

1. **SnapSTL Helper** in Chrome: `chrome://extensions` → turn on **Developer mode** → **Load
   unpacked** → choose the `helper` folder in `SnapSTL-app` (Mac: press **Cmd+Shift+G** and type
   `~/SnapSTL-app/helper`). Pin it from the puzzle-piece icon.
2. Open **SnapSTL** (Mac: `~/Applications`; Windows: Start menu) → **Settings**: check the
   **MakerWorld model page link**, enter the **license password for this model**, choose the output
   and downloads folders, check model options and the scaling table → **Save settings**. Settings and
   license passwords are kept per computer.
3. In Chrome, sign in to MakerWorld → helper icon → **Open model page**. Keep that tab open, ideally
   in its own window; the helper only works there.

## Updates

SnapSTL checks for updates when it opens and every 6 hours. When one is waiting, the sidebar shows
**Update available** → **Settings → Updates** lists the changes → **Update now**. SnapSTL installs it
and restarts, and the Chrome helper reloads itself. Orders, batches, settings, downloads and `.env`
are kept.

From a terminal instead — Mac: `cd ~/SnapSTL-app && zsh scripts/update.sh` · Windows:
`powershell -ExecutionPolicy Bypass -File scripts\windows\update.ps1` (in `SnapSTL-app`).

It won't update while a name is downloading or a batch is building, or if files in the folder were
edited by hand (undo with `git -C ~/SnapSTL-app checkout -- .`). With the shared database, update
every computer: one that is older than the database asks to be updated before it continues.

**Publishing an update (owner, development computer):** merge into `main`, then `git push origin main`.

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
7. **Show in Finder** (Windows: **Show in File Explorer**), open the plates in Bambu Studio, check, slice, print. Bases in white; names in
   their colour folder's colour.

Finished batches can be redone: **Re-download** fetches every model again from MakerWorld;
**Rebuild plates** makes the plates again from the downloaded models into a new Batch folder (the old
one stays).

Downloaded models are kept in **`~/SnapSTL/Downloads`** (Windows: `C:\Users\<you>\SnapSTL\Downloads`) (one folder per name, e.g.
`SONAL - 5 comp - …`), reused by later batches, and **never deleted by SnapSTL** — clean up in Finder / File Explorer
(Downloads page → *Show in …*). The folder can be changed in Settings.

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

## Several computers

With the shared database every SnapSTL computer sees the same orders and batches, live:

- **A batch belongs to the computer that created it.** Its orders leave **Ready** on every computer
  at once and show **in batch #N · computer name**. Two computers can't take the same order: the
  second one gets "Some of these orders were just put in a batch on another computer".
- **That computer does all the work for its batch:** its helper downloads the models into its
  Downloads folder, it builds the plates into its output folder, and only it can queue, build,
  rebuild, re-download, cancel or open the files.
- **Other computers** see the batch in **Batches** with its computer, status and download progress
  (greyed out), but not its pictures or files.
- **Downloads** on each computer show only that computer's own queue.
- Uploading the order report works from any computer; uploads take turns, so nothing is counted twice.

The **web portal** is separate: it lets a browser on another computer use one SnapSTL computer
(Settings → **Other computers** → allow, set a portal password, then open the address shown, e.g.
`http://packing-mac.local:8317`). Everything done there happens on that SnapSTL computer.

## Troubleshooting

| You see | Do |
|---|---|
| **Helper not connected** | open the MakerWorld model maker tab in Chrome; check the helper is enabled in `chrome://extensions` |
| **Helper tab is not on the model page** | helper icon → **Open model page** (or open it and **Use this tab**) |
| Helper says **Complete the MakerWorld security check** | click the check in that tab |
| A name shows **Failed** in the download queue | read *Detail*; **Retry failed** |
| Batch stays *Downloading* | check the sidebar and the **Downloads** page; **Pause / Resume**; *Build now with what's ready* |
| A model looks wrong | Batches → **Re-download**, then **Rebuild plates** |
| "belongs to *another computer*" | that batch is done on its own computer: open SnapSTL there |
| `snapstl-db check` can't connect | check the `.env` line (password in place of `[YOUR-PASSWORD]`, no brackets, Session pooler address) and the internet |
| "set up by a newer SnapSTL" | update this computer (Settings → Updates) |
| "Permission denied (publickey)" | the deploy key isn't on GitHub yet: redo step 1 |
| Portal or page doesn't open (Mac) | `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; log `~/Library/Logs/SnapSTL.log` |
| Portal or page doesn't open (Windows) | `scripts\windows\restart.ps1`; log `%LOCALAPPDATA%\SnapSTL\SnapSTL.log` |
| Database getting big | Settings → Database → **Clean up** (orders already made are kept; a backup is saved first) |

Your data — Mac: database, settings and backups in `~/Library/Application Support/SnapSTL`.
Windows: `%LOCALAPPDATA%\SnapSTL`. Both: downloaded models in `~/SnapSTL/Downloads`, plates in
`~/SnapSTL/output`. With the shared database, orders and batches are in Supabase and each computer's
**Back up now** saves a local copy. SnapSTL never deletes downloaded models or batch folders.

**Uninstall** — Mac:
`launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist && rm -r ~/Applications/SnapSTL.app`
· Windows: `powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1`. Data folders and
`.env` are left in place.

## Development

```bash
uv sync                 # install dependencies (incl. dev)
uv run pytest -q        # tests on a temporary local database
SNAPSTL_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:54329/postgres uv run pytest -q
                        # the same tests on a throwaway Postgres (its `snapstl` schema is dropped first)
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway dev instance with its own data
```

Tests never use the `.env` database.
