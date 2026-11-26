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
     Library, Control printer — not Allow cloud access: SnapSTL uses no cloud profiles).
6. **Remote access with OctoEverywhere** (optional, free): the script also installs the
   **OctoEverywhere Plugin Manager** from the Microsoft Store (it runs OctoEverywhere's plugins in the
   same Docker Desktop). Once:
   - Start menu → **OctoEverywhere Plugin Manager** → **Create New Plugin Instance** for each printer:
     type **Bambu Lab**, then its IP address, access code and serial number (Bambuddy → **Printers**
     shows them). Click the button to **link it to your OctoEverywhere account** in the browser.
   - Then turn on Bambuddy remote access at <https://octoeverywhere.com/bambuddy> with Bambuddy's
     address from step 4.
   - Keep Bambuddy's login on (step 5) once it's reachable from outside your network: remote access
     can start prints. The plugins keep running while Docker Desktop runs; opening the Plugin Manager
     updates them. SnapSTL itself still talks only to Bambuddy on your network.

**Running it again** changes nothing that's installed: it only applies settings (Bambuddy's slicer
settings, firewall) and downloads only what's missing. Settings are remembered in
`C:\ProgramData\SnapSTL-Bambuddy\setup.json`; set one before the command to change it:

| Before the command | What it does |
|---|---|
| `$env:BAMBUDDY_SLICER="orcaslicer"` | Bambuddy slices with OrcaSlicer (`"bambu_studio"` = back to Bambu Studio) |
| `$env:BAMBUDDY_UPGRADE="1"` | Also moves Bambuddy and the slicer service to the version a newer SnapSTL was tested with (printers, profiles and history are kept) |
| `$env:BAMBUDDY_OCTOEVERYWHERE="0"` | Don't install the OctoEverywhere Plugin Manager (`"1"` = install it again if missing; it's never uninstalled by the script) |

Already installed Bambuddy yourself on that PC? The script uses it as it is (never reinstalls, upgrades
or stops it) and adds the slicer service. Ran the earlier 0.22.0 script, which put Bambuddy itself in
Docker? Running the new one removes that container (its data stays in a Docker volume) and installs the
Windows service; add the printers again there.

The slicer service runs while someone is signed in to that PC (Docker Desktop); Bambuddy itself runs
from boot.

**If Bambuddy or the slicer service stops** (after a restart, Docker Desktop closed, …) you don't need to
run the script again: in SnapSTL on the Bambuddy computer, **Settings → Connections → This Bambuddy
computer — external services** checks every outside service SnapSTL relies on: **Bambuddy**, **Docker
Desktop**, the **slicer service** and **OctoEverywhere** (its plugins, and whether the Plugin Manager is
installed). **Check now** looks again, **Start** starts whatever is down (Windows may ask to allow changes
for Bambuddy — choose Yes), each service has its own **Restart**, and **Restart all** restarts Bambuddy,
the slicer and OctoEverywhere together (not Docker, which has its own Restart: it takes the slicer and
OctoEverywhere away for a few minutes). **Open Plugin Manager** opens OctoEverywhere's app (it also
updates its plugins). With **Keep running** on (default) SnapSTL checks every minute and starts what has
stopped by itself; it never restarts anything that's running. Prints on the printers keep going through
any restart; a slice in progress fails and has to be sliced again.
Running the script once more (version 0.22.19 or newer) lets SnapSTL start Bambuddy without the Windows
prompt.

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
  the Bambuddy profiles per printer in the **Slicing** tab (step 4 of
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
6. **Slicing profiles for every printer** (with the print farm). Picked in the **Slicing** tab;
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

**What is shared, and how to check it.** Every order. Uploading a report on any computer sends its
orders to Supabase straight away (the upload says "Shared with the other computers"); the other
computers get them at their next sync, or at once with **Settings → Database → Sync**.
Shared with each order: who is making it (*In a batch*, *Bundled*, *Printed*), the newest webstore
status and name fixes; plus every computer's batches. A report never undoes an order another
computer is already making. The same card shows the last sync,
changes waiting, any error, **Sync now** and **Show what's there** (how many orders per state and
batches per computer are in Supabase right now). In Supabase they're in **Table Editor → schema
`snapstl`** (switch the schema from `public`): `order_records` and `batch_records`.

**Clearing the shared records** (e.g. after testing): **Clear shared records…** on the same card,
then type `CLEAR`. It empties them for **every** computer. First it saves a copy of every record in
this computer's backups folder (`shared-records-<date>.json`). Orders this computer is still making
are sent again at the next sync. To start completely fresh, also reset each computer's database
(**Settings → Maintenance → Database**). Or do both at once: tick **also clear Supabase** next to
**Reset this computer's database…** and type `RESET ALL` — this computer's database and the shared
records are emptied together (a backup of each is saved first) and nothing is sent back. Other
computers keep their own databases; reset them too for a fresh start everywhere.

**Daily handling (Settings → Maintenance → Database).** **Sync** sends this computer's changes and
loads every computer's; **Clean up** syncs and then makes this computer match Supabase exactly
(orders and batches Supabase doesn't have are removed here, with their download and print rows —
batch folders and models on disk are kept; old history is tidied; a backup is saved first). When
Supabase is cleared on one computer (Clear shared records, or Reset with *also clear Supabase*),
every other computer empties itself to match at its next sync, so nothing comes back. Both run at once, without questions.
Only **Reset** asks (RESET, or RESET ALL with *also clear Supabase*). A report's statuses count as
recent as the **newest date or time in that file**, so uploading an older file never undoes a newer
status, on any computer.

**Supabase is required.** Every SnapSTL needs a working connection (the Session pooler address).
Without it a red bar says why and nothing can be changed — orders, batches, printing — until it's
back; Settings still works so you can fix it. Downloads already running finish.

**Reading the orders from another tool or agent (e.g. the Supabase MCP).** Use the **`shop`** schema —
built for questions, holding **every product's** orders from the uploaded reports, not only what
SnapSTL makes. Start with `shop.glossary` (what every code means) and `shop.orders`:

| | |
|---|---|
| `shop.orders` | every order: date, channel, SKU, quantity, status and status group, payment method, COD, customisation, customer |
| `shop.customers` | one per mobile number: name, address, city, state, pincode, first order |
| `shop.order_finance` | money per order (₹): amounts, tax, discounts, coupon, invoice total, Shopdeck fee, COD remittance |
| `shop.shipments` | courier and mode, AWB, weight and size, pickup / delivery / RTO / return times, days to deliver, shipping charges |
| `shop.status_history` | every status an order was seen with, in order |
| `shop.products` | every SKU; `made_by_snapstl` marks Magicians_35 |
| `shop.production`, `shop.batches`, `shop.uploads` | SnapSTL's own records, live: Orders page tab, review reason, who's making it … |
| `daily_sales`, `production_queue`, `cod_outstanding`, `courier_performance`, `rto_by_state` | ready-made answers |

The report is tidied inside Supabase (dates and ₹ typed, statuses like `in_transit`, couriers split into
courier + mode); a newer file wins, an older one or a blank never erases. Online / refund transaction
IDs stay only in the xlsx. Every table and column has a description. Read only — SnapSTL writes it.
Example: `select courier, mode, rto_pct, avg_days_to_deliver from shop.courier_performance;`
(SnapSTL's own tables, `snapstl.*`, keep working as before; `snapstl.orders_full` still joins its
orders with their report columns.) After updating to 0.22.17, upload the latest report once so
`shop` gets every product.

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
   printer uses. The sliced file stays in Bambuddy's library. Until a printer has its profiles
   picked, its plates aren't sliced or printed and Print says *No profiles for <printer name>*.
   - **Bambuddy profiles per printer**: pick the printer by name, then its printer / process /
     filament profile → **Save**. Printers with the same profiles share one slice. Profiles picked
     per model before 0.22.7 keep applying to that model's printers until you pick their own. Every
     profile Bambuddy has is listed: yours in Bambuddy, Orca Cloud, Bambu Cloud and the slicer's
     standard ones. Layer height, walls, infill and speeds are part of the **process** profile.
     **Reload from Bambuddy** reads the lists again. Each list has a **search box**: type any words
     from the profile's name or source (e.g. `a1 0.4`, `0.20 strength`, `pla matte`, `bambu cloud`);
     Enter picks the first match.
   - **Adding profiles** happens in Bambuddy, never in SnapSTL: export them from Bambu Studio
     (**File → Export → Export Preset Bundle**, .bbscfg / .bbsflmt, or a preset's .json) and import
     them in Bambuddy under **Profiles → Local Profiles → Import Profiles**. Or sign in to Bambu Cloud
     / Orca Cloud in Bambuddy; for Bambu Cloud presets the API key SnapSTL uses needs **Allow cloud
     access** (Bambuddy → Settings → API keys).
   - **Print defaults per printer**: the **plate type** on that printer (Textured PEI, Cool,
     Engineering, High Temp, Supertack, Textured Cool — its plates are sliced for it, so the bed
     temperature is right and Bambuddy shows that plate; *Profile's plate* = what the process profile
     says, often Cool Plate), speed (Silent / Standard / Sport / Ludicrous), bed levelling, flow
     calibration and timelapse each printer uses at Start print. *Bambuddy's default* leaves it to
     Bambuddy.
   - **Nothing is sliced by itself** (since 0.22.22): plates are sliced only when you press **Slice**
     in Print (or Start print slices first). **Slicing activity** lists what was sliced, what's
     waiting and what failed.

**Print** (sidebar) is where plates are printed. On top it shows the printers **free for the next
print** (with the colours loaded) and beds to clear, and has **Refresh from Bambuddy**. Its lists:

- **Bases** → *To print*: every base plate of your built batches that still has to be printed. A printer is
  already chosen: the next **available** printer (connected, idle, bed cleared, nothing of yours on
  its way) that has white loaded, with the **most filament left**. You can pick any
  other printer that fits. *Printed*: base plates already printed whose letters aren't all printed yet,
  with how many are. Search by name, order ID or batch.
- **Letters** (the names; called *Names* before 0.25): every letter plate with its colour, in two groups —
  **Base printed — print these letters now**, then **Base still to print**. You can only pick printers that
  have that colour loaded, as Bambuddy reports it. One is pre-selected when such a printer is available.
  Search by name, order ID or batch.
- **Verify** (0.25.1): one card per name, once its letters **and** its base are printed — put them together,
  mark each part, then **Confirm – ready to pack** (or **Save – send problems for reprint**). A *Saving…* screen
  covers the card until it's done; pressing again saves nothing twice. People choose their name in **Checked by** and
  every check is saved under it. **Whole
  plate failed…** is on each card and in Bases → Printed / Letters → Printed (a letters plate can be sent back
  before its bases are printed).
- **Merge** (Letters): tick letter plates of one colour (fix plates too), not started, up to 3 names, then
  **Merge onto one plate**; **Split back** returns exactly those plates while it isn't started.
- **Products** (0.29): the name stands — GTA (`Magicians_174`), Harry Potter (`Magicians_156`), Barbie
  (`Magicians_117`) — are read from the report and listed on Orders under **Product** chips (Organizer is the
  default). Since 0.30 they're batched (one product per batch; Make next batch uses the product chosen) and the
  helper (2.4) downloads them from their own MakerWorld page — it opens the right page by itself; no license
  password. Since 0.31 **Queue build** makes their plates: sized by Settings → Name stands, up to 3 of one
  product per plate, two-colour files `Stands/<Product>/<Product> - A + B + C.3mf` (each stand: Base → filament
  1, Letters → filament 2; the back 45 mm of the bed left for the prime tower). Since 0.32 **Print → Name stands**:
  only printers with an AMS, a block slot and a letters slot (picked by colour), sliced with two filaments and
  sent with both slots; Verify has one check per stand (Good / Stand spoiled → fix plate / Whole plate failed);
  Completed → Reprint the stand. Settings → **Name stands**:
  size by letter count, thickness 40 mm (37 block + 3 letters), font size, brim offset 6, and per product the
  block / letter colours and letter spacing.
- **Spare pieces** (0.28, Orders → **Spare pieces**): bases kept in stock (extras, mistakes, bases made before
  SnapSTL) and complete pieces; letters aren't kept (0.28.1). Name exactly as on the base. A new order with that
  name shows **In stock** (on its row in Pending Orders → To make, a notice there, a tag on Print) — a base fits
  whatever the letters' colour; a complete piece needs the same colour and is sent with **Use spare piece** (the
  order goes to Ready to pack; **Undo spare piece** puts it back; batches leave such orders out). Print → Removed →
  **Add to spare pieces** on a bases plate that was printed anyway. Shared with every computer.
- **Start print** (0.28.1): the plate moves to **Printing** (Bases and Letters each have To print / **Printing** /
  Printed — a plate is in one of them) with **Starting print…** and a progress bar (slicing → sending → printer
  warming up), then the print's %. It takes a few minutes; no need to press again. A plate held because the
  printer was busy stays in To print with Start print.
- **Merging** (0.28): one merge at a time — a second press is refused, so the same names are never on two plates.
  **Available to merge** (under Letters) suggests only plates whose bases are printed. **Remove** works on every
  plate (merged plates and split parts too); a copy left by an older double press is flagged in red.
- **Completed → Reprint / problem…** (0.28): reprint base, letters (whole name), some letters (tap them), both,
  or wrong colour — until the order is shipped.
- **Printers → External spool feeding** (0.28): a printer with an AMS offers its external spool only when this is on.
- **Packing / customer help pages** (0.28): **Also pack:** the other products ordered in the same parcel (same
  Clubbed Order ID in the report), e.g. a 3D decor piece with the name organizer.
- **Slicing → Letters** (0.27): per printer, a Letters process profile and speed. Letter plates slice and print with
  them; bases use the printer's own profiles. Once any printer has a Letters profile, letters go only to those
  printers (e.g. the P1S with colours in the AMS) — Print and Print next pick it by themselves.
- **Settings → ShopDeck** (0.37): paste the App ID from ShopDeck's partner portal (Apps → SnapSTL), press **Connect
  ShopDeck** and allow it on ShopDeck's page. Every 5 minutes orders that ShopDeck shows as printed, packed, shipped,
  delivered, cancelled or returned move forward here (never backward). Names, colours, AWB EXPIRED and Acceptance
  pending still come from the report. Only the connected computer holds the key; it renews itself.
  **Names and colours before the report** (0.37.1): a scheduled Claude task drops new orders with their names and
  colours from ShopDeck into SnapSTL's `bridge-inbox` folder; SnapSTL adds only orders it doesn't have yet (still to
  make, name and colour complete) and never changes one that is already here — the next report always wins.
- **Settings → Staff**: the people who check prints (shared with every computer); add or hide them. No PINs
  (removed in 0.36.4).
- **Completed**: printed plates. **Reprint** is there only for a print that failed: it asks first,
  then the plate goes back to Bases or Names.
- **Removed**: plates you took off Bases or Names with **Remove** (for example printed by hand).
  Nothing is deleted. **Put back** returns a plate.

Each plate row shows its **Slice** state for the printer selected on that row (*Sliced*, *Waiting to
slice*, *Slicing %*, *Slicing failed* with the reason, or *No profiles for A1*, with *for <printer>*
under it), a **Slice** button, and **Options**, which start from the printer's print defaults and can
be changed for that plate only. **Slice** puts the plate in the slicing queue with the profiles of the
printer selected for it — choose the printer first; there is no default printer. Change the printer and
the row shows the slice for that one. **Slice all waiting** (top) queues every plate that has a printer
chosen and isn't sliced for it yet. Plates are sliced one at a time; a slice that makes no progress for
20 minutes is stopped with a message, so nothing stays stuck. Nothing is sliced by itself.

The **Queues** panel (bottom right of Print) shows what's going on, in four tabs: **Downloading**
(models from MakerWorld), **Building** (batches), **Slicing** (with the printer profile each plate is
sliced with) and **Printing** (with the printer). Running and waiting items come first, then the last
few finished; it refreshes every 5 seconds. **Hide** shrinks it to one line with the counts.

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
     (quantity and a note are optional). It goes straight to **Pending Orders → To make** with today's date and
     time and an `AH-…` order ID, and from there is batched and printed like any other order.
2. **Check the lists** (0.34; every order is in exactly one):
   - **Pending Orders:** **To make** — one list: PRINT, **AWB EXPIRED** and **ACCEPTANCE PENDING** orders,
     single, clubbed and ad-hoc, Express on top, then clubbed orders whose other names are already in
     production (*make this now*), then the oldest. Orders whose name support must check with the customer
     are listed in date order but can't be ticked. **In production** (in a batch / printing).
     **Needs attention** shows only when something needs a decision (sort it out, then **Clear flag**).
   - **Order History:** **Ready to pack**, **Packed**, **Shipped**, **Delivered**, **Returned (RTO)**,
     **Cancelled**, **Skipped**, **All**.
   - **Clubbed orders are made together:** ticking one name ticks the others; a batch takes every name of it,
     and none while one name still waits for its check.
   - **EXPRESS · needed by …** orders are always at the top (red from 3 days before the date).
   - **Returned piece available:** a returned order has exactly the same name and colour; check the
     piece, then **Use returned piece** sends it instead of making a new one (**Undo returned piece**
     to make it after all).
   - Type in the boxes under the column names to filter. Choose columns under **Columns** (Pending Orders hides
     Phone, Address and Report status); **Save view** remembers the columns, category and filters in this browser. Use the controls below the table to change pages and rows per page.
   - A name to check is confirmed by support on the customer help page (port 8003). Names starting with a small
     letter (e.g. *riya*) need a check too: usually a typing slip, so check it with the customer.
3. **Make a batch.** In **Pending Orders → To make** (oldest first, Express on top; each shows how many days it
   has waited and its **Payment**: Prepaid / COD / Partial COD), tick orders — or type how many and press
   **Select oldest**, or **Select all matching** — then click **Create batch**. They move to **Ready to print**.
   On **Batches**, each name shows **B** (base) and **L** (letters): ✓ printed, ◐ printing, ○ to print, and
   each batch says e.g. *4 base ready, letters pending* — click it to see just those names.
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
     failed, click **Reprint** there and start it again from Bases or Names. Plates with an
     **EXPRESS** order are listed first. Starting a plate moves its orders to **Printing**.
   - **By hand:** click **Show in Finder** (Windows: **Show in File Explorer**) and open the plates in
     Bambu Studio. Print bases in white, and names in the colour of their folder.
8. **Pack and ship.** When an order's base and name are both printed it moves to **Orders → Printed –
   pack it** by itself. The labelling team makes the label on the website and presses **Label
   Generated**: the order moves to **Label done – ship it**, then follows the website's statuses
   (On the way, Delivered …). A report never moves an order backwards.

A finished batch can be redone:

- **Re-download** fetches every model again from MakerWorld.
- **Rebuild plates** makes the plates again into a new folder. The old folder stays.

Plates are saved like this:

```
SnapSTL/output/2026-11-16/                 one folder per day
    Batch 1 - 2026-11-16/                  numbering restarts at 1 every day
        Bases/Bases - Ayra + Manvi + Riya.3mf
        Names/pink/Pink - Ayra + Manvi + Riya.3mf
        Fixes/Fix - Pink - Riya (r).3mf      only what was spoiled, printed again (Print → Verify)
        Merged/Merged - Pink - Ayra + Tara.3mf
        report.csv                         sizes, scale per axis, warnings
```

Downloaded models are kept in `SnapSTL/Downloads`, one folder per name, and later batches reuse them.
Nothing is ever deleted for good: a model no batch used for 15 days, and the folder of a batch whose
orders were all on the way 10 days ago, go to the Recycle Bin (Trash on a Mac).

Since 0.24 a printed plate goes to **Print → Verify**: a staff member checks every base and name; spoiled
letters, a spoiled name, a wrong colour or a broken base get a small fix plate, and an order is packed only
after both its parts are checked good. Batches show their names and progress and move to **Archive** once
shipped.

Since 0.35 **Print** is by product: **Custom Organizer** (Bases / Letters, each To print · Printing · Printed),
**GTA**, **Harry Potter**, **Barbie**, **Verify** and **Completed** (product chips), and **Batches** (with the
download queue; a batch still getting ready shows at the top of Print). Each plate shows ≈ how long it prints on
the printer picked and when it would be ready, learned from the prints already done (Stats → Print time per
letter). Base plates merge like letters (up to 3 bases, no scaling). There is no Activities page; the activity log
is still kept.

Since 0.36 each printer has a **role** (Printers → Printer roles and AMS colours): **Bases** (white), **Letters** (the
common letter colours kept in its AMS slots, watched by staff), **Flex** (bases and the rare colours) or **Any**, and
the **shift** (default 8:00–22:00). Print pre-selects printers by role, warns when an AMS slot doesn't hold its
planned colour, marks base plates *done before 22:00* / *overnight plate* / *ends at night, then idle*, and
**Before you leave** gives every printer an overnight base plate that ends around the morning.

The **Help** page in the app explains every tab, status and setting.

### Shipping labels (ShopDeck, 0.33)

1. Install **SumatraPDF** (free, prints the labels without opening them) and check the label printer's paper is 4×6
   (100 × 150 mm) in its Windows printer settings.
2. Settings → Logistics → **Shipping labels**: tick *Shipping labels on*, pick the label printer, keep the courier
   order and the 60-second wait. Save settings.
3. In Chrome (with the SnapSTL Helper 2.6+), sign in to ShopDeck and leave **Process Orders** open. On Print label
   the Helper brings that tab to the front (Chrome slows tabs in the background) and goes back to SnapSTL when the
   label is done; don't switch away meanwhile. A stopped label shows why — **Details** lists every step.
4. Print → **Completed** → **Print label** on a checked order. A parcel with other products asks first whether every
   item is ready. The helper changes the courier, waits a minute, presses Label; SnapSTL checks the label and prints
   it, and the order goes to *Label done – ship it*. If ShopDeck asks you to sign in, do it, then press **Continue**.
   Anything unexpected stops with the reason — finish that order by hand on ShopDeck.

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

To browse orders from another computer, enable **Settings → Connections → Logistics access** on the host, set a password and press Save, then open `http://HOST-IP:8002`. This is the **logistics page** for the labelling and customer-care team (below).

## Updates

SnapSTL checks for updates by itself. When there is one, the sidebar shows **Update available**.
Click it, then **Update now**. SnapSTL installs the update and restarts in a few seconds, and the
helper in Chrome reloads itself. Orders, batches, settings and downloads are kept.

To test a published branch, open **Settings → Maintenance → Updates**, click **Check for updates**,
choose its name under **Update branch**, and click **Switch branch**. Use the same picker to return
to **main (production)**. Updates follow the branch checked out on this computer. Only branches
published to the configured `origin` appear. A tester needs a SnapSTL build with the branch picker
before using it, and the branches they switch between should contain the picker for an in-app return.
Use test branches with shared production records only after checking schema and workflow compatibility.
Switching code does not roll back database changes. Test branches skip the in-app Bambuddy update.

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
  imported presets. Open **Slicing** once and pick the **Bambuddy profiles** for each printer
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
| Shared records: Supabase looks empty / another computer doesn't see orders | Press **Settings → Database → Sync** on both computers; an upload that couldn't reach Supabase says so and is sent automatically later. In Supabase look in **Table Editor → schema `snapstl`**, not `public`. **Settings → Connections → Show what's there** shows what's stored |
| "Connected, but sync fails" | Read the message under it. Usually the password or the address: copy the **Session pooler** string again (the direct `db.….supabase.co` one needs IPv6), then **Connect / save** |
| Bambuddy lost its printers after installing SnapSTL (0.17 or older) | SnapSTL's installer had started a second, empty Bambuddy next to yours. Run the SnapSTL install command again: it now stops SnapSTL's copy and starts yours again; your printers and data were never deleted |
| **Bambuddy isn't answering** | On the Bambuddy computer: is it on? In SnapSTL there: **Settings → Connections → This Bambuddy computer → Start** (or run the step-0 command again: it also re-applies the firewall rules). Logs: `C:\ProgramData\Bambuddy\logs`. From a SnapSTL computer, open `http://<Bambuddy computer>:8000` in a browser: if it doesn't load, check both are on the same network and the firewall rule *SnapSTL Bambuddy (TCP 8000)* exists |
| Bambuddy doesn't find a printer | Printer and Bambuddy computer on the same network; printer in LAN Only + Developer Mode; the rule *SnapSTL Bambuddy printer discovery (UDP 2021)* exists (run step 0 again). Or add it by IP address (printer screen: Settings → Network) |
| Slicing fails with "sidecar unreachable" | On the Bambuddy computer someone must be signed in and Docker Desktop running (the slicer service runs in it). In SnapSTL there: **Settings → Connections → This Bambuddy computer → Start**; logs: `docker compose -f C:\ProgramData\SnapSTL-Bambuddy\docker-compose.yml logs` |
| Its address changed (e.g. `192.168.1.50` → `.51`) | Give the Bambuddy computer a fixed address in your router (DHCP reservation), then enter it again in **Settings → Connections** on each SnapSTL computer |
| Adding a printer in Bambuddy fails | Check the IP, serial and access code, and that LAN Only Mode **and** Developer Mode are on |
| A plate shows **Slicing failed** | Read the message. "Bambuddy's slicer isn't running" → in Bambuddy: Settings → Slicer → **Use Slicer API** on, and the slicer container running and reachable from Bambuddy. "isn't made for the printer preset" → pick a process profile made for that printer profile in **Slicing**. Then click **Slice** again |
| A plate shows **No profiles for** a printer | **Slicing → Bambuddy profiles per printer**: pick that printer, then its printer, process and filament profiles → **Save** |
| Slicing lists none of my profiles / a picked profile is "no longer in Bambuddy" | SnapSTL lists Bambuddy's **local** profiles and the slicer's standard ones (no cloud profiles). Import yours in Bambuddy (**Profiles → Local Profiles → Import Profiles**: a Bambu Studio export, `.json` or `.zip`), then **Reload from Bambuddy** |
| Slicing fails with **'NoneType' object has no attribute 'get'** | Bambuddy fails reading Bambu Cloud's profile list (a Bambuddy bug). SnapSTL uses no cloud profiles: in Bambuddy sign out of Bambu Cloud (**Settings → Bambu Cloud**), import your profiles under **Profiles → Local Profiles → Import**, pick them in **Slicing**, then **Slice** again |
| A terminal window opens with SnapSTL, and closing it stops SnapSTL | Fixed in 0.22.19: SnapSTL's background task runs with no window. An install made as administrator keeps the old window until you run the install command again (as administrator); others switch by themselves at their next start |
| A plate stays **Slicing** | It's stopped after 20 minutes without progress, with a message. Check Bambuddy → Settings → Slicer, then **Slice** again |
| Print suggests the wrong filament slot / an old spool | Printers → **Refresh from Bambuddy** (every printer reports its filament again), then choose the slot under the plate's printer — any printer and any loaded slot can be chosen |
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
- **Updated with `git pull` by hand?** The running SnapSTL keeps the old code until it restarts; **Settings → Updates** then shows **Restart SnapSTL**. (Versions before 0.22.13 don't have that button: restart once by hand — Mac: `launchctl kickstart -k gui/$(id -u)/local.snapmagicians.snapstl`; Windows PowerShell: `Stop-ScheduledTask SnapSTL; Start-ScheduledTask SnapSTL`, or sign out and in.)
- **Never commit secrets.** The repository is public: the license passwords, the Supabase connection
  string and the portal password are entered in the app and stay on each computer. For tests and
  development, the `SNAPSTL_DATABASE_URL` and `SNAPSTL_COMPUTER_NAME` environment variables override
  the Settings values.
- **From a terminal:** `uv run snapstl-db check` and `uv run snapstl-db sync` test and run the shared
  records; `uv run snapstl-db export` saves a copy of them in the backups folder.


### Packing page (port 8002) and customer help page (port 8003) — 0.26

**Packing page** — open `http://HOST-IP:8002` and sign in with the packing password (Settings → Connections →
Packing page). It shows only orders **made and checked**, oldest first (Express on top); a clubbed order only
when every name of it is made. Each row: the names with colour and quantity, the order IDs (click to copy) and
**Label Generated**. Tick several (or **Paste order IDs…**) to do many at once; **Packed today** lists today's
labels with **Undo**. The search looks only at the **name to print** (what the packer reads on the piece); it
shows the whole clubbed order, and an order not ready says where it is.

**Customer help** — open `http://HOST-IP:8003` and sign in with its own password (Settings → Connections →
Customer help page). Everything below (search, status, tracking, notes, WhatsApp, name check, Express, cancel,
remake), plus **Late – message first**: orders waiting a week or more, each with **WhatsApp: sorry** (a note
is saved).

- **Search** (top): order ID, phone (any way it's written), customer name or the name to print (as
  the customer typed it or as corrected) — a phone search shows all of that customer's orders. "Last report uploaded" says how fresh the statuses are.
- **Labels:** *Ready to pack – make label* (printed orders; "label expired" and Express first, **Copy
  order IDs** for the website; tick orders — or **Paste order IDs…** — and press **Label Generated**;
  each row also has its own **Label Generated**) and *Packed – waiting for courier*.
- **Customer help:** *Confirm name with customer* (its WhatsApp message asks exactly what is unclear),
  *Cancel requests & problems*, *Remake requests*.
- **Tracking:** *In queue & printing*, *Shipped* (red when stuck more than 5 days), *Delivered*,
  *Returning to us (RTO)*, *Cancelled*. Orders not labelled 7 days after ordering are red.
- **Status words:** *In queue – ready around 28 Nov* (its place in the queue — Express first, then the
  oldest — divided by the orders finished per day over the last 7 days; for the team, not for customers),
  *Printing now*, *Printed – being checked*, *Ready to pack – make label*, *Packed – waiting for courier*,
  *Shipped with (courier)*, *Delivered on (date)*, *Returning to us (RTO)*.
- Click an order ID to copy it.
- Ready-made messages are offered only where they fit the order's status (e.g. *Sorry for the wait*,
  *Printing now*, *Shipped – tracking*, *Delivered – review*, *Couldn't deliver*, *Confirm COD order*).
- Click an order: full details, its clubbed orders and the customer's other orders, then
  - **Share:** Copy tracking link (the webstore's ClickPost page), Copy message (ready-made replies),
    WhatsApp (opens WhatsApp with the message; you press Send);
  - **Change:** Label Generated / Undo, Edit name (only before it's in a batch; tick *Confirmed with
    customer* and say why), Express (needed-by date + note; red from 3 days before), Customer wants to
    cancel (production sees it at once in Problems), Remake request (arrived broken …), Use returned piece;
  - **Notes:** call notes, shared with everyone.
- Nothing else of SnapSTL can be changed from this page. Every button is counted per day: see
  **Settings → Usage** on any SnapSTL computer (all computers together, last 7 / 30 / 90 days), to
  improve the layout later.

**Settings → Logistics** (on the computer that hosts the logistics page): the tracking link (the
webstore's page with `{awb}` or `{order_id}`), after how many days an order turns red (not labelled,
Express, stuck On the way) and the ready-made messages. Press **Save settings**.

### Logistics host (Mac or Windows)

The full app now listens only on localhost on Mac and Windows. On Mac or Windows, open Settings → Connections → Logistics access, enable hosting, set a password and press Save. Save starts or stops the listener immediately without restarting the app. Employees open `http://HOST-IP:8002` and sign in. Permit TCP 8002 for the local subnet in Windows Firewall if needed. This supersedes the former remote-control portal.

`logistics.py` hosts an explicit allowlist: login, orders, facets and CSV export. No edit, upload, batch, helper, printer or settings route is exposed. The shared orders table supports copying, filters, categories, pagination and browser-local saved column views. Refresh orders reloads the host’s local synchronized records, also refreshed every minute; it never pushes changes or runs cleanup. Main Orders has no Supabase refresh button; use Settings → Database → Sync or Clean up.

Orders pagination includes a Page selector to jump directly to any page. Logistics uses the same control. Logistics hosting is available on both Mac and Windows; Settings reports whether port 8002 is actually listening. Allow incoming connections in the host firewall for LAN access. The UTF-8 interface is decoded explicitly on Windows.

Settings → Connections → Logistics access shows the current host LAN IP addresses as clickable links with Copy address buttons (port 8002). Use a numeric IP link from another computer on the same network.

The logistics password field is labelled **Set or change logistics password** in Settings → Connections → Logistics access. Show reveals only the newly typed password. Saved passwords are hashed and cannot be displayed; leaving the field blank keeps the existing password.
