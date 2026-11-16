# SnapSTL

SnapSTL turns Snap Magicians webstore orders for the **Custom Name Desk Organizer** (SKU
`Magicians_35`) into print-ready Bambu Lab plate files (**.3mf**). It generates each name on
MakerWorld, scales it to the agreed rules, puts 3 per plate and saves dated batch folders. With
**Print** it sends the finished plates to your Bambu printers through **Bambuddy**, the
open-source print-farm manager, which slices them with its slicer service. Bambuddy runs on one computer
of your network for every SnapSTL computer; only its slicer service runs in Docker.

It runs on **macOS** and **Windows**. You install it with one command, and everything else is set up
in the app.

**Contents:** [0. The Bambuddy computer](#0-the-bambuddy-computer-print-farm--slicing) · [1. Install](#1-install) · [2. Add the helper to Chrome](#2-add-the-snapstl-helper-to-chrome) ·
[3. Finish setup in the app](#3-finish-setup-in-the-app) · [4. Your printers](#4-your-printers-print-farm) · [Daily use](#daily-use) ·
[Several computers](#several-computers) · [Updates](#updates) · [Troubleshooting](#troubleshooting) ·
[Uninstall](#uninstall) · [For developers](#for-developers)

---

## What you need

- A Mac (macOS 12 or newer) or a Windows 10/11 PC, with internet.
- **Google Chrome**, signed in to the MakerWorld account you use for the organizer.
- *With the print farm:* **Bambuddy** with its slicer service (sidecar, e.g. in Docker on another
  computer), and your slicing profiles imported into Bambuddy.
- *Optional, to send plates to your printers from SnapSTL:* Bambu Lab printers on the same network,
  each with **LAN Only Mode** and **Developer Mode** turned on.
- The model's **license password** from MakerWorld. SnapSTL won't download without it, so models
  never carry MakerWorld's watermark.

## 0. The Bambuddy computer (print farm + slicing)

One **Windows** computer of your network runs **Bambuddy** for every SnapSTL computer (it can also be a SnapSTL
computer):

- **Bambuddy** itself is installed with its own Windows installer, as a **Windows service**: it starts
  with Windows (no sign-in needed) and finds your printers on the network by itself.
- Only its **slicer service** (Bambu Studio + OrcaSlicer) runs in **Docker**, on the same computer.

It needs a 64-bit Intel/AMD Windows 10/11 PC with virtualization on (standard on modern PCs) and about
5 GB free disk.

The Bambuddy computer is always a Windows PC (there are no Bambuddy scripts for a Mac); SnapSTL itself
runs on Mac and Windows computers alike and connects to it over the network.

1. Start menu → type **PowerShell** → right-click **Windows PowerShell** → **Run as administrator**.
2. Paste this line and press Enter:

   ```powershell
   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/bambuddy-server.ps1 | iex"
   ```

3. **First time only:**
   - If WSL 2 isn't on the PC yet, the script turns it on and asks you to **restart**. Restart, then
     run steps 1–2 again.
   - It downloads and installs **Docker Desktop** (about 600 MB) and accepts Docker's license for
     you (free for personal use and small businesses; see docker.com/pricing). If it then says
     *Docker isn't running yet*: open **Docker Desktop**, click through its first screens (signing in
     is optional) until it says **Engine running** — or sign out and back in once — then run steps
     1–2 again.
4. It installs **Bambuddy** (the version this SnapSTL was tested with) as a Windows service, starts the
   **slicer service** in Docker (about 330 MB the first time), points Bambuddy at it (**Use Slicer
   API**, Bambu Studio), opens Windows Firewall for your local network only (TCP 8000 for Bambuddy, UDP
   2021 for printer discovery), and ends with:

   ```
   In SnapSTL on every computer: Settings -> Connections -> Bambuddy address:
       http://192.168.1.50:8000
   ```

5. Open that address in a browser:
   - **Printers → Add printer**: Bambuddy finds the printers on the network; enter each one's access
     code.
   - **Profiles → Local Profiles → Import Profiles**: your slicing presets exported from Bambu Studio
     (**File → Export → Export Preset Bundle**).
   - Optional, recommended when others share your network: turn on Bambuddy's login (**Settings →
     Authentication**) and give each SnapSTL an API key (**Settings → API keys**: Queue, Read status,
     Library, Control printer, Allow cloud access).

**Running it again** changes nothing that's installed: it only applies settings (Bambuddy's slicer
settings, firewall) and downloads only what's missing. Settings are remembered in
`C:\ProgramData\SnapSTL-Bambuddy\setup.json`; set one before the command to change it:

| Before the command | What it does |
|---|---|
| `$env:BAMBUDDY_SLICER="orcaslicer"` | Bambuddy slices with OrcaSlicer (`"bambu_studio"` = back to Bambu Studio) |
| `$env:BAMBUDDY_UPGRADE="1"` | Also moves Bambuddy and the slicer service to the version a newer SnapSTL was tested with (printers, profiles and history are kept) |

Already installed Bambuddy yourself on that PC? The script uses it as it is (never reinstalls, upgrades
or stops it) and adds the slicer service. Ran the earlier 0.22.0 script, which put Bambuddy itself in
Docker? Running the new one removes that container (its data stays in a Docker volume) and installs the
Windows service; add the printers again there.

The slicer service runs while someone is signed in to that PC (Docker Desktop); Bambuddy itself runs
from boot.

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

Open **PowerShell** (Start menu → type *PowerShell*), paste these two lines and press Return — the first
one tells the installer your Bambuddy computer's address from [step 0](#0-the-bambuddy-computer-print-farm--slicing)
(leave it out to set it later in the app):

```powershell
$env:SNAPSTL_BAMBUDDY_URL="http://192.168.1.50:8000"
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/windows/install-windows.ps1 | iex"
```

- It installs Git first if the PC doesn't have it.
- The first install takes a few minutes. When it's done, **SnapSTL** opens. It also has shortcuts in
  the Start menu and on the Desktop.
- Windows may ask once whether SnapSTL may use the network. Allow it only if other computers will
  open SnapSTL through its web page (Settings → Connections → Other computers).

On both systems:

- SnapSTL starts by itself when you log in, and keeps running in the background.
- Its web page is at **http://127.0.0.1:8317**.
- It lives in the **SnapSTL-app** folder in your home folder. Keep it there.
- **Bambuddy** isn't installed with SnapSTL: it runs on your Bambuddy computer
  ([step 0](#0-the-bambuddy-computer-print-farm--slicing)). With `SNAPSTL_BAMBUDDY_URL` set (Mac:
  `SNAPSTL_BAMBUDDY_URL=http://192.168.1.50:8000 curl …`) the installer connects SnapSTL to it;
  otherwise enter the address in **Settings → Connections → Bambuddy address**.
  - Windows only: to make this same PC the Bambuddy computer, run `$env:SNAPSTL_BAMBUDDY="server"`
    first: the installer then runs step 0's setup here (it asks for administrator rights).
  - A Bambuddy installed next to SnapSTL by an older version (folder **SnapSTL-Bambuddy**) keeps
    working; on Windows it's still updated, on a Mac it's left as it is (remove it with the command in
    [Uninstall](#uninstall) once the Windows Bambuddy computer is set up).
- The sidebar's bottom line **IP address** shows this computer's address on the network; click it
  to copy. On the Bambuddy computer it copies Bambuddy's address (`http://<ip>:8000`) to paste into the
  other SnapSTL computers.
- No slicer is installed: plates are sliced by Bambuddy's slicer service. After installing, pick
  the Bambuddy profiles per printer model in the **Slicing** tab (step 4 of
  [4. Your printers](#4-your-printers-print-farm)). SnapSTL has no slicing settings of its own.

## 2. Add the SnapSTL Helper to Chrome

The helper is a small Chrome extension that makes the names on MakerWorld in your own Chrome.

1. In SnapSTL, go to **Settings → Connections → SnapSTL Helper → Show helper folder**. This opens the folder you'll
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
5. **Print farm (Bambuddy)** (optional). Connected by itself; **Settings → Connections** has the
   address. See [4. Your printers](#4-your-printers-print-farm).
6. **Slicing profiles for every printer model** (with the print farm). Picked in the **Slicing** tab;
   see step 4 of [4. Your printers](#4-your-printers-print-farm).

Settings is split into **Setup** (this checklist) · **Product** (model link, license password, name
style, size per letter count) · **Plates** · **Folders** · **Connections** (helper, Bambuddy,
shared records, web portal) · **Maintenance** (updates, database).

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

1. In SnapSTL, go to **Settings → Connections → Shared order records**.
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
(deleting row by row is refused). It then sends this computer's batched, bundled and printed orders
and loads everyone else's, and says how many it sent and loaded. If that first sync fails, it says
why instead of "connected". The sidebar then shows "*name* · shared records".

**What is shared, and how to check it.** Only orders taken into a batch (*In a batch*, *Bundled*,
*Printed*, and orders released back to Ready), with their newest webstore status, plus every
computer's batches. Orders that are only *Ready* or *Needs review* stay on the computer that
uploaded the report, so upload the report on each computer. The same card shows the last sync,
changes waiting, any error, **Sync now** and **Show what's there** (how many orders per state and
batches per computer are in Supabase right now). In Supabase they're in **Table Editor → schema
`snapstl`** (switch the schema from `public`): `order_records` and `batch_records`.

**Clearing the shared records** (e.g. after testing): **Clear shared records…** on the same card,
then type `CLEAR`. It empties them for **every** computer. First it saves a copy of every record in
this computer's backups folder (`shared-records-<date>.json`). Orders this computer is still making
are sent again at the next sync. To start completely fresh, also reset each computer's database
(**Settings → Maintenance → Database**).

**Copies and privacy:** once a day each computer saves a copy of all shared records in its backups
folder (`shared-records-daily-<date>.json`, the last 30 days kept); the card shows when. `uv run
snapstl-db export` saves one now. Only SnapSTL can read the records: row level security is on and
Supabase's public API (anon / authenticated keys) has no access to schema `snapstl`.

The connection string contains the database password. It's kept only on that computer and never
shown again in full. Give it only to computers that run SnapSTL.

## 4. Your printers (print farm)

SnapSTL sends plates to your printers through **Bambuddy** on your Bambuddy computer
([step 0](#0-the-bambuddy-computer-print-farm--slicing)), connected in **Settings → Connections**.
Printers, filament and printer settings are all managed **in Bambuddy** (Printers → **Open
Bambuddy**). SnapSTL only reads them.

1. **On each printer:** Settings → Network → turn on **LAN Only Mode**, then **Developer Mode**.
   Developer Mode cuts the printer off from Bambu Cloud and the Bambu Handy app.
2. **In Bambuddy:** add each printer (IP address, serial number, access code). Any printer can print
   bases or names. What matters is the filament loaded in it.
3. **Filament:** nothing to enter. SnapSTL reads what's loaded in each AMS slot and external spool from
   Bambuddy, with Bambu's colour names (e.g. *Hot Pink*, *Jade White*). Base plates need white loaded;
   a name plate needs its colour. A close colour (e.g. *Hot Pink* for a *pink* order) is offered, but
   marked *loaded: hot pink*. The exact colour is suggested first.
4. **Slicing** is done only by **Bambuddy's slicer**, its slicer service (sidecar), which can run in
   Docker on another computer. Set it up in Bambuddy: **Settings → Slicer** → turn on **Use Slicer
   API** and set the sidecar URL (or `SLICER_API_URL` / `BAMBU_STUDIO_API_URL` in its Docker setup).
   Its **Preferred Slicer** decides Bambu Studio or OrcaSlicer. SnapSTL has no slicer and keeps **no
   slicing settings**: in the **Slicing** tab (sidebar) you only pick which **Bambuddy profiles** each
   printer model uses. The sliced file stays in Bambuddy's library. Until a printer model has its
   profiles picked, its plates aren't sliced or printed and Print says *No profiles for A1*.
   - **Bambuddy profiles per printer model**: pick the model (A1, P1S, …), then its printer /
     process / filament profile → **Save**. Every printer of that model slices with them. Every
     profile Bambuddy has is listed: yours in Bambuddy, Orca Cloud, Bambu Cloud and the slicer's
     standard ones. Layer height, walls, infill and speeds are part of the **process** profile.
     **Reload from Bambuddy** reads the lists again.
   - **Adding profiles** happens in Bambuddy, never in SnapSTL: export them from Bambu Studio
     (**File → Export → Export Preset Bundle**, .bbscfg / .bbsflmt, or a preset's .json) and import
     them in Bambuddy under **Profiles → Local Profiles → Import Profiles**. Or sign in to Bambu Cloud
     / Orca Cloud in Bambuddy; for Bambu Cloud presets the API key SnapSTL uses needs **Allow cloud
     access** (Bambuddy → Settings → API keys).
   - **Print defaults per printer**: speed (Silent / Standard / Sport / Ludicrous), bed levelling,
     flow calibration and timelapse each printer uses at Start print. *Bambuddy's default* leaves it
     to Bambuddy.
   - **Slice new plates automatically** (on by default) slices each new plate for the printer
     pre-selected for it. **Slicing activity** lists what was sliced, what's waiting and what failed.

**Print** (sidebar) is where plates are printed. It has four lists:

- **Bases**: every base plate of your built batches that still has to be printed. A printer is
  already chosen: the next **available** printer (connected, idle, bed cleared, nothing of yours on
  its way) that has white loaded, with the **most filament left**. You can pick any
  other printer that fits.
- **Names**: every name plate with its colour. You can only pick printers that have that colour
  loaded, as Bambuddy reports it. One is pre-selected when such a printer is available.
- **Completed**: printed plates. **Reprint** is there only for a print that failed: it asks first,
  then the plate goes back to Bases or Names.
- **Removed**: plates you took off Bases or Names with **Remove** (for example printed by hand).
  Nothing is deleted. **Put back** returns a plate.

Each plate row shows its **Slice** state for the chosen printer's model (*Sliced*, *Waiting to
slice*, *Slicing %*, *Slicing failed* with the reason, or *No profiles for A1*), a **Slice** button,
and **Options**, which start from the printer's print defaults and can be changed for that plate
only. **Slice all waiting** (top) slices every plate that isn't sliced yet. A slice that makes no
progress for 20 minutes is stopped with a message, so nothing stays stuck.

**Nothing prints until you click Start print.** That click sends the plate, sliced for the model of
the printer you chose (sliced first if needed), and starts it. Bambuddy prints it as soon as the
printer is free and its bed has been cleared. If Bambuddy warns, for example about too little
filament, SnapSTL asks before starting anyway. When a plate is printed it moves to **Completed**.
Once an order's base **and** its name are both printed, the order appears in **Orders → Ready to
ship**.

**Printers** (sidebar) explains each printer in plain words: its state and current print (progress,
time left, layer, speed), temperatures and fans, the filament in each slot and the AMS humidity, any
errors, network, SD card, door, nozzle and firmware, and its queue in Bambuddy, with **Clear plate**
when a bed must be cleared. Each card ends with **All fields**, everything Bambuddy reports as it
comes; **Download JSON** keeps a copy; **Farm activity** lists what SnapSTL sent and started. It's
read only, and access codes and keys are hidden.

The bottom of the sidebar shows at a glance whether the **Helper**, **Bambuddy**, the **Slicer**
(and its presets) and the **Printers** are ready; click a line to go there.

## Daily use

1. **Orders → Upload report (.xlsx).** Upload the webstore *Order Reconciliation Report*.
   - Only `Magicians_35` orders are kept, and uploading again never makes duplicates.
   - A newer report updates the status of orders already known, including orders batched on other
     computers.
   - An older report never undoes a newer status.
   - **Ad-hoc request?** Click **Add ad-hoc order** and type the name to print, mobile and colour
     (quantity and a note are optional). It goes straight to **Ready → Ad-hoc** with today's date and
     time and an `AH-…` order ID, and from there is batched and printed like any other order.
2. **Check the tabs:**
   - **Ready** (with sub-tabs **Single**, **Clubbed** and **Ad-hoc**), **Needs review**, **In a batch**,
     **Bundled** (made into plates, not printed yet), **Ready to ship** (base and name both printed
     in Print), **Printed**, **Skipped**.
   - **Delivered**, **RTO**, **In Transit**, **Cancelled**, **Others**, **All**.
   - Type in the boxes under the column names to filter.
   - In **Needs review**, fix the name or colour and click **Approve**.
3. **Make a batch.** Tick orders (or **Select all matching**), then click **Create batch**.
4. **Download the models.** Go to **Batches → Queue download**. The helper makes each name in your
   MakerWorld tab, one at a time; follow it in the **Download queue** on the Batches page. If MakerWorld shows a security
   check in that tab, complete it and the helper carries on.
5. **Build the plates.** When the batch says **Ready to build**, click **Queue build**.
6. **Check the fit.** The batch should say **Fit check: N/N**. A name that wouldn't fit its base is
   never printed and goes back to Ready.
7. **Print.**
   - **With Bambuddy:** go to **Print**. In **Bases** and **Names**, check the printer on each plate
     (one is pre-selected when available), then click **Start print** on each plate you want printed
     now. The **Status** column follows it: *Sending* → *Started · in queue* → *Printing* →
     *Printed*. After each print, clear that printer's bed and click **Clear plate** (in Bambuddy, or
     on the Printers page) so its next plate starts. Printed plates move to **Completed**. If a print
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
  statuses, reach the other computers within about a minute. **Settings → Connections → Sync now**
  does it immediately.
- **Any computer can mark orders printed,** including orders made on another computer.
- **Resetting is safe.** **Settings → Maintenance → Reset this computer's database** empties only this
  computer. **Sync now** then loads the shared orders back.
- **Records are kept for good.** SnapSTL never deletes single shared records, and Supabase refuses to
  delete them too. Only **Clear shared records** (typed confirmation, copy saved first) empties them.
- **Settings stay on each computer.** License password, folders and connection are never shared.

To use one SnapSTL computer from a browser on another computer, turn on **Settings → Connections →
Other computers** and set a portal password.

## Updates

SnapSTL checks for updates by itself. When there is one, the sidebar shows **Update available**.
Click it, then **Update now**. SnapSTL installs the update and restarts in a few seconds, and the
helper in Chrome reloads itself. Orders, batches, settings and downloads are kept.

Running the install command again also updates SnapSTL.
Bambuddy on the Bambuddy computer is updated by running its [step-0](#0-the-bambuddy-computer-print-farm--slicing)
command there again (to the version this SnapSTL was tested with, `scripts/bambuddy-version.txt`). A
Bambuddy an older SnapSTL installed next to itself is still updated with SnapSTL.

**Updating to 0.22 from an older version:** SnapSTL no longer installs Bambuddy with itself. Set up the
Bambuddy computer (step 0), then on each SnapSTL computer enter its address in **Settings →
Connections → Bambuddy address**. Printers and profiles are added in that Bambuddy; an older Bambuddy
next to SnapSTL keeps running until you remove it.

**Updating to 0.21 from an older version:**

- **Only Bambuddy's slicer slices now.** OrcaSlicer / Bambu Studio on this computer, Test slice,
  Add preset files and sliced files put in the batch folder by hand are gone. SnapSTL keeps no slicing
  settings: the update removes the presets picked for those slicers and SnapSTL's own copy of
  imported presets. Open **Slicing** once and pick the **Bambuddy profiles** for each printer model
  (import your Bambu Studio presets into Bambuddy first: Profiles → Local Profiles → Import
  Profiles). Profiles already picked for Bambuddy's slicer are kept. OrcaSlicer stays installed;
  remove it yourself if nothing else uses it.

**Updating to 0.19 from an older version:**

- The sidebar is now Orders · Batches · Print · Printers · Slicing · Settings · Help. Downloads is at
  the bottom of Batches, Printer status is Printers, Make It is Print.
- **Slicing has no default presets any more.** Open **Slicing** and save presets for each printer
  model (A1, P1S…) once; until then those printers' plates aren't sliced or printed. Presets set for
  single printers in 0.18 aren't used.
- Settings is split into Setup · Product · Plates · Folders · Connections · Maintenance; nothing you
  saved changes.

## Troubleshooting

| You see | Do this |
|---|---|
| **Helper not connected** | Open the MakerWorld tab in Chrome (helper icon → **Open model page**); check the helper is turned on in `chrome://extensions` |
| Helper says **Complete the MakerWorld security check** | Click the check in that tab |
| A name shows **Failed** in the Download queue | Read the detail, then **Retry failed** |
| A model looks wrong | Batches → open the batch → **Objects** → **Re-download**, then **Rebuild plates** |
| "No license password" | Settings → Product → enter the license password for this model → **Save settings** |
| "Can't reach the shared records … no batch was created" | Check the internet, then try again |
| Connect / save says the password or address is wrong | Copy the **Session pooler** string again; put the password in place of `[YOUR-PASSWORD]` with no brackets |
| Settings → Connections shows changes waiting | They're sent on the next sync; **Sync now** shows any problem |
| Shared records: Supabase looks empty / another computer doesn't see orders | Only orders taken into a batch are shared — upload the report on each computer. In Supabase look in **Table Editor → schema `snapstl`**, not `public`. **Settings → Connections → Show what's there** shows what's stored |
| "Connected, but sync fails" | Read the message under it. Usually the password or the address: copy the **Session pooler** string again (the direct `db.….supabase.co` one needs IPv6), then **Connect / save** |
| Bambuddy lost its printers after installing SnapSTL (0.17 or older) | SnapSTL's installer had started a second, empty Bambuddy next to yours. Run the SnapSTL install command again: it now stops SnapSTL's copy and starts yours again; your printers and data were never deleted |
| **Bambuddy isn't answering** | On the Bambuddy computer: is it on? Services → **Bambuddy** running? Run the step-0 command again (it starts it and re-applies the firewall rules). Logs: `C:\ProgramData\Bambuddy\logs`. From a SnapSTL computer, open `http://<Bambuddy computer>:8000` in a browser: if it doesn't load, check both are on the same network and the firewall rule *SnapSTL Bambuddy (TCP 8000)* exists |
| Bambuddy doesn't find a printer | Printer and Bambuddy computer on the same network; printer in LAN Only + Developer Mode; the rule *SnapSTL Bambuddy printer discovery (UDP 2021)* exists (run step 0 again). Or add it by IP address (printer screen: Settings → Network) |
| Slicing fails with "sidecar unreachable" | On the Bambuddy computer someone must be signed in and Docker Desktop running (the slicer service runs in it). Run step 0 again; logs: `docker compose -f C:\ProgramData\SnapSTL-Bambuddy\docker-compose.yml logs` |
| Its address changed (e.g. `192.168.1.50` → `.51`) | Give the Bambuddy computer a fixed address in your router (DHCP reservation), then enter it again in **Settings → Connections** on each SnapSTL computer |
| Adding a printer in Bambuddy fails | Check the IP, serial and access code, and that LAN Only Mode **and** Developer Mode are on |
| A plate shows **Slicing failed** | Read the message. "Bambuddy's slicer isn't running" → in Bambuddy: Settings → Slicer → **Use Slicer API** on, and the slicer container running and reachable from Bambuddy. "isn't made for the printer preset" → pick a process profile made for that printer profile in **Slicing**. Then click **Slice** again |
| A plate shows **No profiles for A1** (or P1S) | **Slicing → Bambuddy profiles per printer model**: choose that model's printer, process and filament profiles → **Save** |
| Slicing lists none of my profiles / a picked profile is "no longer in Bambuddy" | Import them in Bambuddy (Profiles → Local Profiles → Import Profiles), then **Reload from Bambuddy**. Cloud profiles need Bambuddy signed in to Bambu Cloud / Orca Cloud, and for Bambu Cloud an API key with **Allow cloud access** |
| A plate stays **Slicing** | It's stopped after 20 minutes without progress, with a message. Check Bambuddy → Settings → Slicer, then **Slice** again |
| A printer is greyed out in Print | It's offline, or that colour isn't loaded (white for bases) — load the spool; the Printers page shows what Bambuddy sees |
| A pushed plate waits in Bambuddy | Its printer is busy or waiting for the bed to be cleared: **Clear plate** |
| SnapSTL doesn't open | Run the install command again; it repairs and restarts SnapSTL |
| Database getting big | Settings → Maintenance → **Clean up** (orders already made are kept) |

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
  - A Bambuddy an older SnapSTL installed on this Mac: `launchctl bootout gui/$(id -u)/local.snapmagicians.bambuddy && rm ~/Library/LaunchAgents/local.snapmagicians.bambuddy.plist`
- **Windows** (PowerShell):
  `powershell -ExecutionPolicy Bypass -File $HOME\SnapSTL-app\scripts\windows\uninstall.ps1`
  (removes Bambuddy's task too)
- **The Bambuddy computer**: Bambuddy → Windows **Settings → Apps → Bambuddy → Uninstall** (its data in
  `C:\ProgramData\Bambuddy` stays); slicer service:
  `docker compose -f C:\ProgramData\SnapSTL-Bambuddy\docker-compose.yml down`.

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
  records; `uv run snapstl-db export` saves a copy of them in the backups folder.
