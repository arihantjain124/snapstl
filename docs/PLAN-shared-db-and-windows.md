# Plan: shared cloud database, and SnapSTL on Windows

Status: **plan, not built yet.** Git-based updates (Settings → Updates) are built; see INSTALL.md.

## 1. Goal

Several SnapSTL computers (Macs and Windows PCs) work on the **same orders and batches**. The
database is one you rent and own. Every feature works as today: upload, review, batches, the
helper, license check and watermark scan, re-download, plates and the LAN portal.

## 2. Database provider

What SnapSTL stores is small: order IDs, the names and colours on the organizers, special requests,
batches, the download queue and settings. That's a few MB a year. There are no phone numbers or
addresses. Downloaded models (about 1–3 MB per name) are files, not database rows.

| Provider | Region near you | Price (check before buying) | Notes |
|---|---|---|---|
| **Supabase (recommended)** | **Mumbai** | Free to try; **Pro about $25/month** | Postgres + **file storage** in one project, daily backups, web dashboard to view and export your data. The free plan pauses after a week without use, so use Pro for real work. |
| DigitalOcean Managed PostgreSQL | **Bangalore** | about **$15/month** (1 GB node), backups included | Plain Postgres, flat price, simple. Files would need a separate store (Spaces). Its firewall works best with fixed IP addresses. |
| AWS RDS for PostgreSQL | Mumbai | from about $15–30/month plus storage | The most control, but the most setup (networking, backups, IAM). Only worth it if you already use AWS. |
| Neon | Singapore (no India region) | usage-based, about $20/month always-on | Good product, but data leaves India and every query goes to Singapore. |

**Why Supabase:** it's in Mumbai, so data stays in India, which also keeps you aligned with India's
data protection law (DPDP Act). It also stores the downloaded model files in the same project,
so any SnapSTL computer can build any batch. It's standard **PostgreSQL**. You own it in the sense
that matters: the account is in your name, you hold the password, and you can export everything
at any time (`pg_dump`) and move to any other Postgres host.

**What you'd buy:** a Supabase account in your name → a new project → region **Mumbai
(ap-south-1)** → a strong database password → Pro plan. Then send me only the project's region.
The connection details stay on your computers, never in git or chat.

## 3. How a shared database changes SnapSTL

### What's shared and what stays on each computer

| Shared (cloud database) | Stays on each computer |
|---|---|
| Orders, upload history, review fixes | Output folder and downloads folder locations |
| Batches, their status and plate list | The plates themselves (written where the batch is built) |
| Download queue and history, watermark results | Local copy of downloaded models (a cache) |
| Model link, model options, scaling table, license passwords | LAN portal on/off and its password |
| Pause between downloads, pause/resume downloads | Database connection details |
| Downloaded model files (Supabase Storage, private bucket) | |

### Working safely from several computers at once

- **Download queue:** each computer claims the next name with a database lock
  (`FOR UPDATE SKIP LOCKED`), so two helpers never generate the same name. A claim from a computer
  that went offline expires and goes back to the queue, as today.
- **One pace for MakerWorld:** the pause between downloads, and the longer pause after a failure,
  are kept in the database, not per computer. Two helpers on one MakerWorld account can't double the
  request rate. A **"Download computer"** setting can pick which computers may download.
- **Builds:** a batch is claimed the same way; it is built on the computer that claimed it, and the
  batch shows that computer's name. **Show in Finder / File Explorer** works on that computer. The
  others can download the plates through the portal.
- **Downloaded models:** after a download passes the license and watermark checks, it's uploaded to
  storage. A computer that needs it fetches it once and keeps a local copy. Re-download replaces it
  everywhere.
- **Live view:** the screens already refresh every 2 seconds, so changes made on one computer show
  up on the others.

### Versions and upgrades

- The database records its schema version. Database changes run under a lock, so two computers
  updating at once can't clash.
- A computer whose SnapSTL is older than the database stops with **"Update SnapSTL"** (one click,
  Settings → Updates) instead of writing old-format data.

### Code changes

- `db.py` gets a Postgres connection (psycopg) next to SQLite. **SQLite stays** for a single
  computer and for the tests, so nothing breaks if the cloud is never set up.
- About 90 SQL statements, mostly in `batches.py`, need small dialect fixes (placeholders,
  `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, auto-numbered IDs).
- Batch folders are stored as computer + path, not a bare Mac path.
- New: **Settings → Database → Connect to cloud database**: paste the connection string, test it,
  then **Move my data to the cloud** copies the current SQLite data across once. A local backup is
  kept.
- Backups: Supabase's daily backups, plus **Back up now** saving a local export.

### Security

- Connection details are saved only in the computer's settings file (never in git), and the
  connection is encrypted (SSL required).
- SnapSTL logs in as its own database user with only the rights it needs, not the admin account.
- Supabase's public web API is switched off for these tables; only SnapSTL's direct connection
  is used.
- License passwords move into the shared database so every computer has them. They're readable by
  anyone with the database password, so keep that password private.
- **This changes a rule in AGENTS.md** ("customer data never leaves the Mac"). It becomes
  "customer data stays in your own database (Mumbai) and on your computers, never in git or logs."

### Trade-off

Without internet, SnapSTL can't open orders or batches. (Downloads already need the internet.)
A read-only offline mode could come later if needed.

## 4. SnapSTL on Windows

One codebase for both systems, not a separate Windows app. Same screens and features, with a
Windows look.

### Reused unchanged (about 95% of the code)

The model engine (scaling, plate layout, grouping, 3MF writer, watermark check, previews), order
import and rules, batches and the download queue, the web UI's screens, the Chrome helper (Chrome is
the same on Windows) and the tests.

### New Windows pieces

| Job | Mac today | Windows |
|---|---|---|
| Data folder | `~/Library/Application Support/SnapSTL` | `%LOCALAPPDATA%\SnapSTL` |
| Start at login, restart if it stops | launchd service | Task Scheduler task |
| App window | pywebview (Safari engine) | pywebview (Edge WebView2, built into Windows 10/11) |
| Status | sidebar only | also a **tray icon** by the clock: status, Open, Show downloads, Restart, Quit |
| Keep awake | macOS stay-awake tool | Windows stay-awake setting |
| Show in Finder | Finder | File Explorer, with the file selected |
| LAN portal name and IP | macOS network tools | Python network calls; Windows Firewall asks once |
| Installer | `scripts/make_app.sh` | `scripts/install.ps1` (later a normal `SnapSTL-Setup.exe`) |
| Updates | `git pull` + restart (built) | the same; the restart goes through Task Scheduler; `scripts/update.ps1` |
| Look | current styling | Windows font (Segoe UI), "File Explorer" and "this PC" wording |

### Windows details to handle

- File names Windows refuses (`CON`, `NUL`, `COM1`, names ending in a dot or space) get a safe form.
- Paths over 260 characters: names are shortened, or a clear error is shown.
- A file open in Bambu Studio can't be replaced: re-download says "close it in Bambu Studio".
- Line endings: the repository keeps scripts in Unix format so git doesn't change them on Windows.

### Windows setup (once)

Install **Git for Windows** and Chrome. Then the same deploy-key setup as on the Mac, in PowerShell.
Then `git clone` and `scripts\install.ps1`. After that, updates are the same one click.

### Size of the work

About 80 lines change in the existing code. About 400 lines are new: the file for Mac/Windows
differences, the tray icon, the PowerShell installer and updater, and tests. All 31 current tests
keep running on both systems.

## 5. Suggested order

1. **Git updates on every computer** (built). Create the private repository and set up each Mac once
   (INSTALL.md).
2. **Cloud database**: you create the Supabase project. SnapSTL gets the Postgres support, shared
   locks and the one-time data move. Test with two Macs on the same database.
3. **Downloaded models in cloud storage**, so any computer can build any batch.
4. **Windows version** on the `port/windows` branch. Its first real run is on a Windows PC.
