# SnapSTL

Turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU `Magicians_35`)
into print-ready Bambu Lab plate files (**.3mf**). Each name is generated on MakerWorld, scaled to the
agreed rules, grouped 3 per plate, and saved in dated batch folders.

Runs on **macOS and Windows**. On each computer:

- a background service with a web page at **http://127.0.0.1:8317**,
- the SnapSTL app window onto that page,
- the **SnapSTL Helper** in Chrome.

Several computers can share **order records** in Supabase, so they never make the same order twice.

- How it works, every file explained: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
- Changing the software with an AI agent: **[AGENTS.md](AGENTS.md)**

**Contents:**
[What you need](#what-you-need) ·
[1. Repository access](#1-give-the-computer-read-only-access-to-the-repository) ·
[2a. Install on a Mac](#2a-install-on-a-mac) ·
[2b. Install on Windows](#2b-install-on-windows) ·
[3. Shared order records (.env)](#3-shared-order-records-supabase-and-the-env-file) ·
[4. Helper and settings](#4-helper-and-settings) ·
[Updates](#updates) ·
[Daily use](#daily-use) ·
[Several computers](#several-computers) ·
[Troubleshooting](#troubleshooting) ·
[Development](#development)

---

## What you need

- **macOS 12+** or **Windows 10/11**, with an internet connection.
- **Google Chrome**, signed in to the MakerWorld account you use for the organizer.
- **Bambu Studio**, to open and print the plates.
- The model's **license password**. SnapSTL refuses to download without it, so models never carry
  MakerWorld's watermark.
- On Windows: **Git for Windows** (https://git-scm.com/download/win), installed with the default options.
- The repository owner, for one minute in step 1.

## 1. Give the computer read-only access to the repository

Each computer gets its own **deploy key**. The key can download SnapSTL but can't change it, and it
can be removed on its own if the computer is lost. The computer doesn't need a GitHub account.

**Mac:** open **Terminal**, then install Apple's command-line tools, which include git. If a window
opens, click **Install**. If it says they're already installed, carry on.

```bash
xcode-select --install
```

**Windows:** open **Git Bash** (Start menu; it comes with Git for Windows).

Then, on either system, create this computer's key and copy it to the clipboard:

```bash
mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N "" -C "snapstl $(hostname)" -f ~/.ssh/snapstl_deploy && printf '\nHost github-snapstl\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/snapstl_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' >> ~/.ssh/config && (pbcopy 2>/dev/null || clip) < ~/.ssh/snapstl_deploy.pub && echo "Key copied to the clipboard"
```

Send the copied text to the owner; it isn't secret. The owner adds it on GitHub:

1. Open the repository → **Settings → Deploy keys → Add deploy key**.
2. Title: the computer's name. Key: paste the text.
3. Leave **Allow write access** off → **Add key**.

Check it works. It should print `successfully authenticated`:

```bash
ssh -T github-snapstl 2>&1 | grep -o "successfully authenticated"
```

## 2a. Install on a Mac

In **Terminal**:

```bash
git clone github-snapstl:arihantjain124/snapstl.git ~/SnapSTL-app && cd ~/SnapSTL-app && zsh scripts/make_app.sh
```

This installs:

- **uv**, the Python tool manager, if the Mac doesn't have it yet.
- The **SnapSTL service**: it starts at login, restarts itself if it stops, and keeps the Mac awake.
- **SnapSTL.app** in `~/Applications`.

If SnapSTL is already in `~/SnapSTL-app` from a zip, use this instead. It turns that folder into a
git copy in place. Your orders, settings and downloads aren't in that folder, so they're kept.

```bash
cd ~/SnapSTL-app && git init -q -b main && git remote add origin github-snapstl:arihantjain124/snapstl.git && git fetch -q origin && git reset -q --hard origin/main && git branch -q --set-upstream-to=origin/main main && zsh scripts/make_app.sh
```

Check it's running. It should print `200`:

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

This installs:

- **uv**, the Python tool manager, if the PC doesn't have it yet.
- A background task called **SnapSTL**: it starts when you log in and restarts SnapSTL if it stops.
- **SnapSTL** shortcuts in the Start menu and on the Desktop.

Windows may ask once whether to let SnapSTL use the network. Allow it only if you'll open SnapSTL
from other computers (the web portal).

Keep the folder at `C:\Users\<you>\SnapSTL-app`: the task and the Chrome helper run from it.
SnapSTL's own files are in `%LOCALAPPDATA%\SnapSTL`: settings, the local database, backups and the
log `SnapSTL.log`.

## 3. Shared order records (Supabase) and the .env file

**Skip this section if you use only one computer.**

Every computer keeps its own **local database**. The shared order records in Supabase hold every
order that has been taken into a batch, bundled or printed, on any computer, with the latest
webstore status. With them:

- **No order is made twice.** A batch can only take an order that no other computer has taken.
- **Any computer can load everything.** **Sync now** (Settings → Database) fetches the current state of
  all orders.
- **Records are kept for good.** SnapSTL only adds and updates records, never deletes them. The
  tables also refuse deletes. Resetting a computer's database doesn't touch them.
- **Settings stay on each computer.** They are never stored in Supabase.

The connection details go in a `.env` file in the SnapSTL folder of each computer. That file is
never put in git.

### Once: create the Supabase project (owner)

1. Sign up at **supabase.com** in the business's name, then click **New project**.
   - **Name:** `snapstl`.
   - **Region:** South Asia (Mumbai), so the data stays in India.
   - **Database password:** long, with **letters and digits only**. Symbols like `@ : / # ? %` break
     the connection address. Keep it in a password manager.
   - **Plan:** **Pro** for real use. The free plan pauses after a week without use.
2. In the project, click **Connect** → **Connection string** → type **URI** → **Session pooler** (it
   works on every network). Copy the address. It looks like
   `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
   Replace `[YOUR-PASSWORD]`, brackets included, with the database password.
3. That's all. On first use SnapSTL creates its tables in their own schema, `snapstl`:
   `order_records` and `batch_records`. Supabase's public web API doesn't expose that schema, and
   Supabase keeps daily backups (Pro plan).

The records include customer names, phone numbers and addresses from the report. Give the
connection address only to computers that run SnapSTL. Never paste it into chat, email or git.

### On every computer: the .env file

**Mac** (Terminal):

```bash
cd ~/SnapSTL-app && cp .env.example .env && open -e .env
```

**Windows** (PowerShell):

```powershell
cd $HOME\SnapSTL-app; copy .env.example .env; notepad .env
```

Fill it in and save:

```ini
SNAPSTL_DATABASE_URL=postgresql://postgres.abcdefghijkl:YourPassword123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
SNAPSTL_COMPUTER_NAME=Packing Mac
```

`SNAPSTL_COMPUTER_NAME` is how this computer appears on its batches. It defaults to the computer's
own name.

Check the connection, then restart SnapSTL so it uses it. On a Mac:

```bash
cd ~/SnapSTL-app && ~/.local/bin/uv run snapstl-db check
```

```bash
launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl
```

On Windows (PowerShell, in `SnapSTL-app`):

```powershell
uv run snapstl-db check; powershell -ExecutionPolicy Bypass -File scripts\windows\restart.ps1
```

The sidebar then shows the computer's name followed by "shared records". Orders this computer had
already batched, bundled or printed are sent automatically with the first sync.

## 4. Helper and settings

On every computer:

1. **Add the SnapSTL Helper to Chrome:**
   - Open `chrome://extensions` and turn on **Developer mode**.
   - Click **Load unpacked** and choose the `helper` folder in `SnapSTL-app`. On a Mac, press
     **Cmd+Shift+G** in the file picker and type `~/SnapSTL-app/helper`.
   - Pin it from the puzzle-piece icon.
2. **Fill in Settings.** Open **SnapSTL** (Mac: `~/Applications`; Windows: Start menu) → **Settings**:
   - Check the **MakerWorld model page link**.
   - Enter the **license password for this model**.
   - Choose the output and downloads folders.
   - Check the model options and the scaling table.
   - Click **Save settings**. Settings and license passwords are kept separately on each computer.
3. **Open the model page.** In Chrome, sign in to MakerWorld, then click the helper icon → **Open
   model page**. Keep that tab open, ideally in its own window; the helper only works there.

## Updates

SnapSTL checks for updates when it opens and every 6 hours. When one is waiting:

1. The sidebar shows **Update available**.
2. **Settings → Updates** lists what changed. Click **Update now**.
3. SnapSTL installs the update and restarts, and the Chrome helper reloads itself.

Orders, batches, settings, downloads and `.env` are kept.

You can also update from a terminal:

- **Mac:** `cd ~/SnapSTL-app && zsh scripts/update.sh`
- **Windows** (in `SnapSTL-app`): `powershell -ExecutionPolicy Bypass -File scripts\windows\update.ps1`

SnapSTL won't update while a name is downloading or a batch is building. It also won't update if
files in the folder were edited by hand; undo those edits with `git -C ~/SnapSTL-app checkout -- .`.
Keep every computer updated: a computer running an older SnapSTL than the shared records stops
syncing and asks to be updated.

**Publishing an update (owner, on the development computer):** merge into `main`, then run
`git push origin main`.

## Daily use

1. **Orders → Upload report (.xlsx).** Upload the webstore *Order Reconciliation Report*. Only
   `Magicians_35` orders are kept, and uploading again never creates duplicates.
   - With shared records, SnapSTL syncs first, then updates every known order with its new webstore
     status, including orders batched on other computers.
   - **Order dates:** a status only replaces the stored one if the report is newer. SnapSTL decides
     this by the row's own dates (pickup, delivered, RTO, return), then the report's
     "Date Range … to" date, then the upload time. Uploading an older report never undoes a newer
     status.
2. **Tabs:**
   - **Ready** (with sub-tabs **Single** and **Clubbed**), **Needs review**, **In a batch**.
   - **Bundled**, **Printed**, **Skipped**.
   - **Delivered**, **RTO**, **In Transit**, **Cancelled**, **Others**, **All**.

   Type in the boxes under the column names to filter; filters combine. In **Needs review**, fix the
   name or colour and click **Approve**.
3. Tick orders (or **Select all matching**), then click **Create batch**.
4. **Batches → Queue download.** The helper generates each name in your MakerWorld tab, one at a time;
   follow it on the **Downloads** page. If MakerWorld shows a security check in that tab, complete
   it and the helper carries on.
5. When the batch says **Ready to build**, click **Queue build**. Nothing starts by itself.
6. Check the batch says **Fit check: N/N**. Every name then keeps its base's proportions, so it fits
   after printing. A pair that fails is never printed and goes back to Ready.
7. Click **Show in Finder** (Windows: **Show in File Explorer**). Open the plates in Bambu Studio,
   check, slice and print. Print bases in white and names in the colour of their folder.
8. **After printing:** go to **Bundled**, tick the printed orders (or click **Printed** on a row),
   then click **Mark printed**. They move to **Printed** and keep their batch for the record.
   **Back to Bundled** undoes this.

Finished batches can be redone:

- **Re-download** fetches every model again from MakerWorld.
- **Rebuild plates** makes the plates again from the downloaded models into a new Batch folder. The
  old folder stays.

Downloaded models are kept in **`~/SnapSTL/Downloads`** (Windows: `C:\Users\<you>\SnapSTL\Downloads`),
one folder per name, for example `SONAL - 5 comp - …`. Later batches reuse them, and SnapSTL **never
deletes them**. Clean them up yourself from the Downloads page → **Show in …**. You can change the
folder in Settings.

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

Each .3mf is one plate, and every base or name shows as its own named object in Bambu Studio.
SnapSTL saves no ZIP files. The **Download ZIP** button is optional and saves nothing.

The **Help** page in the app explains every tab, status, file and setting.

## Several computers

With the shared order records:

- **Batches are made where they're created.** A batch belongs to the computer that created it. That
  computer's helper downloads its models into its Downloads folder, it builds the plates into its
  output folder, and only it can queue, build, rebuild, re-download, cancel or open the files.
- **No order is batched twice.** Creating a batch first claims its orders in the shared records.
  Orders another computer already has are left out, with "already taken on another computer". They
  leave Ready and show as **in Batch N · computer name**. If the shared records can't be reached,
  no batch is created.
- **Changes reach the other computers within a minute.** Bundled, printed and released orders, and
  newer webstore statuses, go to the shared records right after each change and every minute. Other
  computers pick them up on their next sync. **Sync now** does it immediately.
- **Any computer can mark orders printed,** including orders made on another computer.
- **Resetting a computer's database** (Settings → Database) empties only that computer. It waits
  until that computer's changes have been sent. Afterwards, **Sync now** loads the batched, bundled
  and printed orders back. A batch that was still in progress there is released, so its orders can
  be made again.
- **Records are never deleted.** SnapSTL only adds and updates records, and the tables refuse deletes
  (even from Supabase's dashboard). If the owner really needs to delete something, they can first
  remove the `…_no_delete` triggers in Supabase's SQL editor.

The **web portal** is separate. It lets a browser on another computer use one SnapSTL computer:

1. On that computer: Settings → **Other computers** → allow, set a portal password, save.
2. On the other computer: open the address shown there (e.g. `http://packing-mac.local:8317`) and
   sign in.

Everything done through the portal happens on the SnapSTL computer it points to.

## Troubleshooting

| You see | Do |
|---|---|
| **Helper not connected** | Open the MakerWorld model maker tab in Chrome; check the helper is enabled in `chrome://extensions` |
| **Helper tab is not on the model page** | Helper icon → **Open model page** (or open it and **Use this tab**) |
| Helper says **Complete the MakerWorld security check** | Click the check in that tab |
| A name shows **Failed** in the download queue | Read *Detail*; **Retry failed** |
| Batch stays *Downloading* | Check the sidebar and the **Downloads** page; **Pause / Resume**; *Build now with what's ready* |
| A model looks wrong | Batches → **Re-download**, then **Rebuild plates** |
| "Can't reach the shared records … no batch was created" | Check the internet and the `.env` line, then try again |
| Settings → Database shows changes waiting | They're sent on the next sync; **Sync now** shows any error |
| `snapstl-db check` can't connect | Check the `.env` line: your password in place of `[YOUR-PASSWORD]` with no brackets, and the Session pooler address |
| "set up by a newer SnapSTL" | Update this computer (Settings → Updates) |
| "Permission denied (publickey)" | The deploy key isn't on GitHub yet: redo step 1 |
| Page doesn't open (Mac) | `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; log `~/Library/Logs/SnapSTL.log` |
| Page doesn't open (Windows) | `scripts\windows\restart.ps1`; log `%LOCALAPPDATA%\SnapSTL\SnapSTL.log` |
| Database getting big | Settings → Database → **Clean up** (orders already made are kept; a backup is saved first) |

**Where your data is:**

- **Local database, settings and backups:** Mac `~/Library/Application Support/SnapSTL`; Windows
  `%LOCALAPPDATA%\SnapSTL`.
- **Downloaded models:** `~/SnapSTL/Downloads`. **Plates:** `~/SnapSTL/output`.
- **Shared order records:** in your Supabase project.

SnapSTL never deletes downloaded models or batch folders.

**Uninstall.** Data folders, `.env` and the shared records are left in place.

- **Mac:**
  `launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist && rm -r ~/Applications/SnapSTL.app`
- **Windows:** `powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1`

## Development

```bash
uv sync                 # install dependencies (incl. dev)
uv run pytest -q        # tests on temporary local databases (shared-record tests are skipped)
SNAPSTL_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:54329/postgres uv run pytest -q
                        # also the shared-record tests, on a throwaway Postgres (its `snapstl` schema is dropped)
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway dev instance with its own data
```

Tests never use the `.env` records. `uv run snapstl-db check` and `uv run snapstl-db sync` work with
the shared records from a terminal.
