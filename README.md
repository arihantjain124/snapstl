# SnapSTL

SnapSTL turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU
`Magicians_35`) into print-ready Bambu Lab plate files (**.3mf**). It generates each name on
MakerWorld, scales it to the agreed rules, puts 3 per plate and saves dated batch folders. With
**Make It** it sends the finished plates to your Bambu printers through **Bambuddy**, the
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
5. **Print farm (Bambuddy)** (optional). Connected by itself; **Settings → Print farm** has the
   address (slicing presets are chosen in Make It → Slice). See [4. Your printers](#4-your-printers-print-farm).

Everything you enter stays on this computer. Nothing goes into the public code repository.

### Shared order records (only for several computers)

With more than one SnapSTL computer, connect them all to one **Supabase** project. It holds the
records of every order taken into a batch, bundled or printed, so no order is made twice. Each
computer still keeps its own database.

**Once, by the owner: create the Supabase project**

1. Sign up at **supabase.com** in the business's name and click **New project**.
   - **Name:** `snapstl`.
   - **Region:** **South Asia (Mumbai)**, so the data stays in India.
   - **Database password:** long. Symbols are fine: SnapSTL encodes them itself. Keep it in a
     password manager.
   - **Plan:** **Pro**. The free plan pauses after a week without use.
2. When the project is ready, click **Connect** → **Connection string** → **URI** → **Session pooler**,
   and copy it.

**On each computer: connect it**

1. In SnapSTL, go to **Settings → Shared order records**.
2. Paste the connection string. Replace `[YOUR-PASSWORD]`, brackets included, with the database
   password, typed exactly as it is, symbols included.
   - SnapSTL percent-encodes the password before connecting (`#` → `%23`, `)` → `%29`, `@` → `%40`,
     `/` → `%2F`, `?` → `%3F`, `%` → `%25`, `:` → `%3A`). A password you already encoded is left alone.
   - Elsewhere, e.g. `psql`, use the encoded form: password `ab#c)d` →
     `postgresql://postgres:ab%23c%29d@<server>:5432/postgres`.
   - Prefer the **Session pooler** address (`…pooler.supabase.com`). The direct one
     (`db.<project>.supabase.co`) only works on networks with IPv6 unless the project has the IPv4
     add-on.
3. Give this computer a name, for example *Packing Mac*. It's shown on its batches.
4. Click **Connect / save**.

SnapSTL checks the connection and creates its tables on first use. To create them yourself
beforehand, open Supabase → **SQL Editor**, paste [`docs/supabase-schema.sql`](docs/supabase-schema.sql)
and click **Run**. No password is needed for that, and it's safe to run again. It makes the schema
`snapstl` with the tables `order_records`, `batch_records` and `meta`, and records are never deleted
(deleting is refused). It then sends this computer's
batched, bundled and printed orders and loads everyone else's. The sidebar then shows
"*name* · shared records".

The connection string contains the database password. It's kept only on that computer and never
shown again in full. Give it only to computers that run SnapSTL.

## 4. Your printers (print farm)

SnapSTL sends plates to your printers through **Bambuddy**, which the installer put on this computer.
Printers, filament and printer settings are all managed **in Bambuddy** (Make It → **Open
Bambuddy**). SnapSTL only reads them.

1. **On each printer:** Settings → Network → turn on **LAN Only Mode**, then **Developer Mode**.
   Developer Mode cuts the printer off from Bambu Cloud and the Bambu Handy app.
2. **In Bambuddy:** add each printer (IP address, serial number, access code). Any printer can print
   bases or names. What matters is the filament loaded in it.
3. **Filament:** nothing to enter. SnapSTL reads what's loaded in each AMS slot and external spool from
   Bambuddy, with Bambu's colour names (e.g. *Hot Pink*, *Jade White*). Base plates need white loaded;
   a name plate needs its colour. A close colour (e.g. *Hot Pink* for a *pink* order) is offered, but
   marked *loaded: hot pink*. The exact colour is suggested first.
4. **Slicing** is done on this computer by **OrcaSlicer**, which the installer puts in Applications.
   It's built from Bambu Studio, with the same engine and Bambu presets. Bambu Studio's own command
   line can't slice on a Mac, which is why SnapSTL uses OrcaSlicer's. In **Make It → Slice**:
   - **Presets** come from Bambu Studio and OrcaSlicer on this computer, including **your own saved
     presets**. Save a preset in Bambu Studio and it shows up in the lists by itself. Choose the
     printer, process and filament presets → **Save presets**.
   - **Presets for** lets each printer model (A1, P1S, …) have its own presets. A model without its
     own uses the default set, or else its own Bambu defaults. Print speeds, layer height and infill
     are part of the **process** preset.
   - **Add printer / process / filament preset…** takes preset files exported from another
     computer (.json, .bbscfg, .bbsflmt).
   - **Slicer**: OrcaSlicer on this computer (the default), or Bambuddy's slicer if you run that
     instead (Bambuddy → Settings → Slicer, Docker).

**Make It** (sidebar) is where plates are printed. It has four parts:

- **Bases**: every base plate of your built batches that still has to be printed. A printer is
  already chosen: the next **available** printer (connected, idle, bed cleared, nothing of yours on
  its way) that has white loaded, with the **most filament left**. You can pick any
  other printer that fits.
- **Names**: every name plate with its colour. You can only pick printers that have that colour
  loaded, as Bambuddy reports it. One is pre-selected when such a printer is available.
- **Slice**: **Slice new plates automatically** (on by default: every new plate is sliced for each
  printer model in the farm; a failed slice isn't retried by itself), the presets per printer model,
  adding preset files to Bambuddy, **Slice all waiting plates**, and every plate's slicing state. Each plate
  is sliced by Bambuddy, one at a time in the background, and shows *Waiting to slice* →
  *Slicing %* → *Sliced*. If it fails it shows why. A slice that makes no progress for 20 minutes
  is stopped with a message, so nothing stays stuck.
- **Completed**: printed plates. **Reprint** is there only for a print that failed: it asks first,
  then the plate goes back to Bases or Names.
- **Removed**: plates you took off Bases or Names with **Remove** (for example printed by hand).
  Nothing is deleted. **Put back** returns a plate.
- **Printers**: each printer's state and the filament in every slot, with **Clear plate** when a bed
  must be cleared.

Each plate has a **Slice** button, which slices it now, and **Remove**.

**Nothing prints until you click Start print.** That click sends the sliced plate to the chosen
printer and starts it. A plate that isn't sliced yet is sliced first and sent when that's done. Bambuddy then prints it as soon as the
printer is free and its bed has been cleared. A plate is always sent sliced for the model of the
printer you chose; if it isn't sliced for that model yet, it's sliced first. Each row also has
**Options**: **Print speed** (Silent / Standard / Sport / Ludicrous, applied as the print starts),
bed levelling, flow calibration and timelapse (default = Bambuddy's settings). If Bambuddy warns, for example about too
little filament, SnapSTL asks before starting anyway. When a plate is printed it moves to
**Completed**. Once an order's base **and** its name are both printed, the order appears in
**Orders → Ready to ship**.

**Printer status** (sidebar) explains each printer in plain words: its state and current print
(progress, time left, layer, speed), temperatures and fans, the filament in each slot and the AMS
humidity, any errors, network, SD card, door, nozzle and firmware, and its queue in Bambuddy. At the
top, **Slicing** shows what Bambuddy is slicing for SnapSTL, what's waiting and what failed. Each
printer card ends with **All fields**, everything Bambuddy reports as it comes, and **Download JSON**
keeps a copy. It's read only, and access codes and keys are hidden.

## Daily use

1. **Orders → Upload report (.xlsx).** Upload the webstore *Order Reconciliation Report*.
   - Only `Magicians_35` orders are kept, and uploading again never makes duplicates.
   - A newer report updates the status of orders already known, including orders batched on other
     computers.
   - An older report never undoes a newer status.
2. **Check the tabs:**
   - **Ready** (with sub-tabs **Single** and **Clubbed**), **Needs review**, **In a batch**,
     **Bundled** (made into plates, not printed yet), **Ready to ship** (base and name both printed
     in Make It), **Printed**, **Skipped**.
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
   - **With Bambuddy:** go to **Make It**. In **Bases** and **Names**, check the printer on each plate
     (one is pre-selected when available), then click **Start print** on each plate you want printed
     now. The **Status** column follows it: *Sending* → *Started · in queue* → *Printing* →
     *Printed*. After each print, clear that printer's bed and click **Clear plate** (in Bambuddy, or
     Make It → Printers) so its next plate starts. Printed plates move to **Completed**. If a print
     failed, click **Reprint** there and start it again from Bases or Names.
   - **By hand:** click **Show in Finder** (Windows: **Show in File Explorer**) and open the plates in
     Bambu Studio. Print bases in white, and names in the colour of their folder.
8. **Ship and mark them printed.** **Orders → Ready to ship** lists the orders whose base and name are
   both printed. Tick them (or click **Printed** on a row), then click **Mark printed**. They move to
   **Printed** and keep their batch for the record. **Back to Bundled** undoes this. Orders printed by
   hand are marked the same way from the **Bundled** tab.

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
| Make It: **Bambuddy isn't answering** | Run the install command again (it repairs Bambuddy); log: `~/Library/Logs/Bambuddy` (Windows: `%LOCALAPPDATA%\Bambuddy\logs`) |
| Adding a printer in Bambuddy fails | Check the IP, serial and access code, and that LAN Only Mode **and** Developer Mode are on |
| A plate shows **Slicing failed** | Read the message. "OrcaSlicer isn't installed" → run the install command again. "isn't made for the printer preset" → choose a process preset for that printer in Make It → Slice. Then click **Slice** again |
| A plate stays **Slicing** | It's stopped after 20 minutes without progress, with a message. Check Bambuddy → Settings → Slicer, then **Slice** again |
| A printer is greyed out in Make It | It's offline, or that colour isn't loaded (white for bases) — load the spool; Make It → Printers shows what Bambuddy sees |
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
