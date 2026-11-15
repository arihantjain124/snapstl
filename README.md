# SnapSTL

SnapSTL turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU
`Magicians_35`) into print-ready Bambu Lab plate files (**.3mf**). It generates each name on
MakerWorld, scales it to the agreed rules, puts 3 per plate and saves dated batch folders. With
**Push to queue** it sends the finished plates to your Bambu printers through **Bambuddy**, the
open-source print-farm manager installed with it.

It runs on **macOS** and **Windows**. You install it with one command, and everything else is set up
in the app.

**Contents:** [1. Install](#1-install) · [2. Add the helper to Chrome](#2-add-the-snapstl-helper-to-chrome) ·
[3. Finish setup in the app](#3-finish-setup-in-the-app) · [4. Your printers](#4-your-printers-print-farm) · [Daily use](#daily-use) ·
[Several computers](#several-computers) · [Updates](#updates) · [Troubleshooting](#troubleshooting) ·
[Uninstall](#uninstall) · [For developers](#for-developers)

---

## What you need

- A Mac (macOS 12 or newer) or a Windows 10/11 PC, with internet.
- **Google Chrome**, signed in to the MakerWorld account you use for the organizer.
- **Bambu Studio**, to print the plates.
- *Optional, to send plates to your printers from SnapSTL:* Bambu Lab printers on the same network,
  each with **LAN Only Mode** and **Developer Mode** turned on.
- The model's **license password** from MakerWorld. SnapSTL won't download without it, so models
  never carry MakerWorld's watermark.

## 1. Install

### Mac

Open **Terminal** (Applications → Utilities), paste this line and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/install-mac.sh | zsh
```

- If the Mac asks to install **command-line developer tools**, click **Install**, wait for it to
  finish, then run the same line again.
- The first install takes a few minutes. When it's done, **SnapSTL** opens. It also lives in
  `~/Applications`; drag it to the Dock if you like.

### Windows

Open **PowerShell** (Start menu → type *PowerShell*), paste this line and press Return:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/install-windows.ps1 | iex"
```

- It installs Git first if the PC doesn't have it.
- The first install takes a few minutes. When it's done, **SnapSTL** opens. It also has shortcuts in
  the Start menu and on the Desktop.
- Windows may ask once whether SnapSTL may use the network. Allow it only if other computers will
  open SnapSTL through its web page (Settings → Other computers).

On both systems:

- SnapSTL starts by itself when you log in, and keeps running in the background.
- Its web page is at **http://127.0.0.1:8317**.
- It lives in the **SnapSTL-app** folder in your home folder. Keep it there.
- The installer also sets up **Bambuddy** (the print-farm manager) in the **SnapSTL-Bambuddy** folder.
  It starts at login too, at **http://127.0.0.1:8000** (this computer only), and SnapSTL connects
  to it by itself. To leave it out, put `SNAPSTL_NO_BAMBUDDY=1` before the install command.

## 2. Add the SnapSTL Helper to Chrome

The helper is a small Chrome extension that makes the names on MakerWorld in your own Chrome.

1. In SnapSTL, go to **Settings → SnapSTL Helper → Show helper folder**. This opens the folder you'll
   need in step 4.
2. In Chrome, type `chrome://extensions` in the address bar and press Return.
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose that **helper** folder (inside `SnapSTL-app`).
5. Click the puzzle-piece icon in Chrome's toolbar and pin **SnapSTL Helper**.
6. Sign in to MakerWorld in Chrome. Then click the helper icon → **Open model page**. Keep that tab
   open, ideally in its own window.

The helper updates itself together with SnapSTL. You only add it once.

## 3. Finish setup in the app

Open **Settings**. The **Setup** list at the top shows what's done (✓) and what's left (!). Each item
has a button that takes you to the right place.

1. **MakerWorld model link and license password.** Check the model page link, then type the
   **license password for this model** → **Save settings**.
2. **Output and downloads folders.** The defaults are `SnapSTL/output` (plates) and
   `SnapSTL/Downloads` (models) in your home folder. Change them if you like → **Save settings**.
3. **SnapSTL Helper.** The list shows ✓ once the helper is connected and on the model page.
4. **Shared order records** (only if you use more than one computer). See below.
5. **Print farm (Bambuddy)** (optional). See [4. Your printers](#4-your-printers-print-farm).

Everything you enter stays on this computer. Nothing goes into the public code repository.

### Shared order records (only for several computers)

With more than one SnapSTL computer, connect them all to one **Supabase** project. It holds the
records of every order taken into a batch, bundled or printed, so no order is made twice. Each
computer still keeps its own database.

**Once, by the owner: create the Supabase project**

1. Sign up at **supabase.com** in the business's name and click **New project**.
   - **Name:** `snapstl`.
   - **Region:** **South Asia (Mumbai)**, so the data stays in India.
   - **Database password:** long, **letters and digits only** (symbols break the connection).
     Keep it in a password manager.
   - **Plan:** **Pro**. The free plan pauses after a week without use.
2. When the project is ready, click **Connect** → **Connection string** → **URI** → **Session pooler**,
   and copy it.

**On each computer: connect it**

1. In SnapSTL, go to **Settings → Shared order records**.
2. Paste the connection string. Replace `[YOUR-PASSWORD]`, brackets included, with the database
   password.
3. Give this computer a name, for example *Packing Mac*. It's shown on its batches.
4. Click **Connect / save**.

SnapSTL checks the connection and creates its tables on first use. It then sends this computer's
batched, bundled and printed orders and loads everyone else's. The sidebar then shows
"*name* · shared records".

The connection string contains the database password. It's kept only on that computer and never
shown again in full. Give it only to computers that run SnapSTL.

## 4. Your printers (print farm)

SnapSTL sends plates to your printers through **Bambuddy**, which the installer put on this computer.
Bambuddy keeps the connection to each printer and a print queue per printer. It starts a print only
after someone confirms the bed is clear.

1. **On each printer:** Settings → Network → turn on **LAN Only Mode**, then **Developer Mode**. Note
   the **IP address** and **access code** on that screen, and the **serial number** under Settings →
   Device. Give each printer a fixed address in your router.
   - Developer Mode cuts the printer off from Bambu Cloud and the Bambu Handy app.
2. **In SnapSTL → Printers → Add a printer:** enter the name, model, IP address, serial number and
   access code, and what it makes: **Bases** or **Names**. For names printers, also list the colours
   loaded (e.g. `pink, blue`). Bambuddy checks it can reach the printer before adding it. The access
   code stays in Bambuddy only.
3. **Keep the colours up to date.** When you change a spool, change that printer's colours on the
   Printers page and click **Save**. Push to queue only offers printers that fit a plate: **Bases**
   printers for base plates, and **Names** printers with that colour for name plates.
4. **Slicing.** Printers only print sliced plates. There are two ways:
   - **By hand:** open the plate in Bambu Studio → **Slice plate** → **Export plate sliced file**. Save
     it in the batch folder's `Sliced` folder under the same path and name, e.g.
     `Sliced/Bases/{Ayra,pink},{Manvi,pink}.gcode.3mf` (next to the plate also works).
   - **Slicer service** (automatic): run Bambuddy's slicer service (Docker), then enter its address on
     the Printers page, e.g. `http://127.0.0.1:3001`, with the Bambu printer, process and filament
     preset names. SnapSTL then slices each plate when you push it. Bambu Studio's own command line
     can't slice on a Mac, so this needs the service.

**Open Bambuddy** (Printers page) shows the printers, cameras, queues and history.

## Daily use

1. **Orders → Upload report (.xlsx).** Upload the webstore *Order Reconciliation Report*.
   - Only `Magicians_35` orders are kept, and uploading again never makes duplicates.
   - A newer report updates the status of orders already known, including orders batched on other
     computers.
   - An older report never undoes a newer status.
2. **Check the tabs:**
   - **Ready** (with sub-tabs **Single** and **Clubbed**), **Needs review**, **In a batch**,
     **Bundled**, **Printed**, **Skipped**.
   - **Delivered**, **RTO**, **In Transit**, **Cancelled**, **Others**, **All**.
   - Type in the boxes under the column names to filter.
   - In **Needs review**, fix the name or colour and click **Approve**.
3. **Make a batch.** Tick orders (or **Select all matching**), then click **Create batch**.
4. **Download the models.** Go to **Batches → Queue download**. The helper makes each name in your
   MakerWorld tab, one at a time; follow it on the **Downloads** page. If MakerWorld shows a security
   check in that tab, complete it and the helper carries on.
5. **Build the plates.** When the batch says **Ready to build**, click **Queue build**.
6. **Check the fit.** The batch should say **Fit check: N/N**. A name that wouldn't fit its base is
   never printed and goes back to Ready.
7. **Print.**
   - **With Bambuddy:** open the batch → **Push to queue**. Each plate gets a suggested printer (the
     right job and colour, the least busy). Change any of them, then click **Push to queue**. The
     **Status** column follows each plate: *Sending* → *In Bambuddy queue* → *Printing* → *Printed*.
     After each print, clear that printer's bed and click **Clear plate** (in Bambuddy, or on SnapSTL's
     Printers page) so its next plate starts.
   - **By hand:** click **Show in Finder** (Windows: **Show in File Explorer**) and open the plates in
     Bambu Studio. Print bases in white, and names in the colour of their folder.
8. **Mark them printed.** When every plate of a pushed batch is printed, its Push to queue panel
   offers **Mark these orders printed**. Otherwise open the **Bundled** tab, tick the orders you printed (or click **Printed** on
   a row), then click **Mark printed**. They move to **Printed** and keep their batch for the record.
   **Back to Bundled** undoes this.

A finished batch can be redone:

- **Re-download** fetches every model again from MakerWorld.
- **Rebuild plates** makes the plates again into a new folder. The old folder stays.

Plates are saved like this:

```
SnapSTL/output/2026-11-16/                 one folder per day
    Batch 1 - 2026-11-16/                  numbering restarts at 1 every day
        Bases/{Ayra,pink},{Manvi,pink},{Riya,pink}.3mf
        Names/pink/{Ayra,pink},{Manvi,pink},{Riya,pink}.3mf
        report.csv                         sizes, scale per axis, warnings
```

Downloaded models are kept in `SnapSTL/Downloads`, one folder per name. Later batches reuse them, and
SnapSTL never deletes them.

The **Help** page in the app explains every tab, status and setting.

## Several computers

When the computers are connected to the shared order records:

- **A batch is made on the computer that created it.** That computer's helper downloads its models,
  and its plates are saved there.
- **No order is made twice.** Creating a batch first claims its orders. Orders another computer already
  took are left out, and show as **in Batch N · computer name**. If the shared records can't be
  reached, no batch is created.
- **Every computer stays up to date.** Bundled, printed and returned orders, and new webstore
  statuses, reach the other computers within about a minute. **Settings → Database → Sync now** does
  it immediately.
- **Any computer can mark orders printed,** including orders made on another computer.
- **Resetting is safe.** **Settings → Database → Reset this computer's database** empties only this
  computer. **Sync now** then loads the shared orders back.
- **Records are kept for good.** SnapSTL never deletes shared records, and Supabase refuses to delete
  them too.
- **Settings stay on each computer.** License password, folders and connection are never shared.

To use one SnapSTL computer from a browser on another computer, turn on **Settings → Other
computers** and set a portal password.

## Updates

SnapSTL checks for updates by itself. When there is one, the sidebar shows **Update available**.
Click it, then **Update now**. SnapSTL installs the update and restarts in a few seconds, and the
helper in Chrome reloads itself. Orders, batches, settings and downloads are kept.

Running the install command again also updates SnapSTL. Bambuddy moves to the version this SnapSTL
was tested with (`scripts/bambuddy-version.txt`) at the same time.

## Troubleshooting

| You see | Do this |
|---|---|
| **Helper not connected** | Open the MakerWorld tab in Chrome (helper icon → **Open model page**); check the helper is turned on in `chrome://extensions` |
| Helper says **Complete the MakerWorld security check** | Click the check in that tab |
| A name shows **Failed** on the Downloads page | Read the detail, then **Retry failed** |
| A model looks wrong | Batches → open the batch → **Objects** → **Re-download**, then **Rebuild plates** |
| "No license password" | Settings → enter the license password for this model → **Save settings** |
| "Can't reach the shared records … no batch was created" | Check the internet, then try again |
| Connect / save says the password or address is wrong | Copy the **Session pooler** string again; put the password in place of `[YOUR-PASSWORD]` with no brackets |
| Settings → Database shows changes waiting | They're sent on the next sync; **Sync now** shows any problem |
| Printers page: **Bambuddy isn't answering** | Run the install command again (it repairs Bambuddy); log: `~/Library/Logs/Bambuddy` (Windows: `%LOCALAPPDATA%\Bambuddy\logs`) |
| Add a printer: **Could not connect to the printer** | Check the IP, serial and access code, and that LAN Only Mode **and** Developer Mode are on |
| A plate **Not sliced** / push fails with "Not sliced yet" | Slice it in Bambu Studio and save it in the batch's `Sliced` folder, or set a slicer service; then **Try again** |
| A printer is greyed out in Push to queue | It makes the other part, or that colour isn't loaded: fix it on the Printers page |
| A pushed plate waits in Bambuddy | Its printer is busy or waiting for the bed to be cleared: **Clear plate** |
| SnapSTL doesn't open | Run the install command again; it repairs and restarts SnapSTL |
| Database getting big | Settings → Database → **Clean up** (orders already made are kept) |

**Where your data is:**

- **Database, settings and backups:**
  - Mac: `~/Library/Application Support/SnapSTL`
  - Windows: `%LOCALAPPDATA%\SnapSTL`
- **Models and plates:** the `SnapSTL` folder in your home folder.
- **Shared order records:** your Supabase project.
- **Bambuddy:** program in `SnapSTL-Bambuddy` in your home folder; its data in
  `~/Library/Application Support/Bambuddy` (Windows: `%LOCALAPPDATA%\Bambuddy`).

**Logs:**

- Mac: `~/Library/Logs/SnapSTL.log`
- Windows: `%LOCALAPPDATA%\SnapSTL\SnapSTL.log`

## Uninstall

Your data folders and the shared records stay in place.

- **Mac** (Terminal):
  `launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist && rm -r ~/Applications/SnapSTL.app`
  - Bambuddy: `launchctl bootout gui/$(id -u)/local.snapmagicians.bambuddy && rm ~/Library/LaunchAgents/local.snapmagicians.bambuddy.plist`
- **Windows** (PowerShell):
  `powershell -ExecutionPolicy Bypass -File $HOME\SnapSTL-app\scripts\windows\uninstall.ps1`
  (removes Bambuddy's task too)

Then remove the helper in `chrome://extensions`.

---

## For developers

- How it works and every file: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
- Rules for changing it (also with an AI agent): **[AGENTS.md](AGENTS.md)**

```bash
uv sync                 # dependencies (incl. dev)
uv run pytest -q        # tests on temporary local databases
SNAPSTL_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:54329/postgres uv run pytest -q
                        # also the shared-records tests, on a throwaway Postgres (its `snapstl` schema is dropped)
SNAPSTL_HOME=/tmp/snapstl-dev SNAPSTL_PORT=8320 uv run uvicorn snapstl.server:app --port 8320
                        # a throwaway instance with its own data
```

- **Publishing an update:** merge into `main` and push. Every installed SnapSTL then shows **Update
  available**.
- **Never commit secrets.** The repository is public: the license passwords, the Supabase connection
  string and the portal password are entered in the app and stay on each computer. For tests and
  development, the `SNAPSTL_DATABASE_URL` and `SNAPSTL_COMPUTER_NAME` environment variables override
  the Settings values.
- **From a terminal:** `uv run snapstl-db check` and `uv run snapstl-db sync` test and run the shared
  records.
