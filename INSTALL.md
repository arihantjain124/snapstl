# Installing SnapSTL on a new Mac

This guide sets up SnapSTL on a Mac from its private git repository. After this one-time setup,
updates are **one click**: SnapSTL shows **Update available** in its sidebar → **Settings → Updates →
Update now**. Nothing to copy, unzip or reinstall.

## What you need

- A Mac with **macOS 12 or newer**.
- **Google Chrome**, signed in to the MakerWorld account you use for the name organizer.
- **Bambu Studio**, to open and print the plates.
- An internet connection.
- The **license password** for the MakerWorld model. Without it SnapSTL won't download anything, so
  the models never carry MakerWorld's watermark.
- Someone who can open the SnapSTL repository on GitHub (the owner) for step 1, about a minute.

Open **Terminal** (Applications → Utilities → Terminal) for the commands below. Paste each block and
press Return.

---

## 1. Give this Mac read-only access to the repository

Each Mac gets its own **deploy key**: it can download SnapSTL but can't change it, and it can be
removed on its own if the Mac is lost. No GitHub account is needed on this Mac.

Install Apple's command-line tools (includes git). If a window opens, click **Install** and wait for
it to finish. If it says they're already installed, carry on.

```bash
xcode-select --install
```

Create this Mac's key and copy it to the clipboard:

```bash
mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N "" -C "snapstl $(scutil --get ComputerName)" -f ~/.ssh/snapstl_deploy && printf '\nHost github-snapstl\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/snapstl_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' >> ~/.ssh/config && pbcopy < ~/.ssh/snapstl_deploy.pub && echo "Key copied to the clipboard"
```

The owner then adds it on GitHub (send them the clipboard text; it's not a secret):

1. Open the repository → **Settings** → **Deploy keys** → **Add deploy key**.
2. Title: the Mac's name. Key: paste. Leave **Allow write access** off.
3. **Add key**.

Check it works. It should print `successfully authenticated`:

```bash
ssh -T github-snapstl 2>&1 | grep -o "successfully authenticated"
```

## 2. Download and install SnapSTL

**A new Mac** (SnapSTL was never installed):

```bash
git clone github-snapstl:arihantjain124/snapstl.git ~/SnapSTL-app && cd ~/SnapSTL-app && zsh scripts/make_app.sh
```

**A Mac that already has SnapSTL from a zip** in `~/SnapSTL-app`: this turns that folder into a git
copy in place, so the helper and the service keep their folder. Orders, batches, settings and
downloads aren't in this folder and are kept.

```bash
cd ~/SnapSTL-app && git init -q -b main && git remote add origin github-snapstl:arihantjain124/snapstl.git && git fetch -q origin && git reset -q --hard origin/main && git branch -q --set-upstream-to=origin/main main && zsh scripts/make_app.sh
```

The first install takes a few minutes. It installs:

- **uv**, the Python tool manager, if the Mac doesn't have it yet.
- The **SnapSTL service**, which starts at login, restarts itself if it stops and keeps the Mac awake.
- **SnapSTL.app** in `~/Applications`. Drag it to the Dock if you like.

Keep the folder at `~/SnapSTL-app`: the service and the Chrome helper run from it.

Check that it's running. This should print `200`:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8317/
```

## 3. Add the SnapSTL Helper to Chrome

The helper is the Chrome extension that generates each name on MakerWorld.

1. In Chrome, open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the **helper** folder inside `SnapSTL-app`. In the file
   chooser, press **Cmd + Shift + G**, type `~/SnapSTL-app/helper` and press Return.
4. Check that **SnapSTL Helper** shows version 2.3.0 or later.
5. Click the puzzle-piece icon in Chrome's toolbar and pin **SnapSTL Helper**.

Keep the `helper` folder where it is: Chrome loads the extension from it.

## 4. Set up SnapSTL

Open **SnapSTL** from `~/Applications` and go to **Settings**.

1. **MakerWorld model page link**: check it points to the name organizer model.
2. **License password for this model**: enter the password.
3. **Output folder** and **Downloads folder**: the defaults are `~/SnapSTL/output` and
   `~/SnapSTL/Downloads`. Change them if you want.
4. **Pause between downloads (s)**: leave the default unless MakerWorld complains. After a failure,
   SnapSTL waits longer before trying again.
5. Check the model options and the scaling / compartments table.
6. Click **Save settings**.

## 5. Open the MakerWorld page for the helper

1. In Chrome, sign in to **MakerWorld**.
2. Click the **SnapSTL Helper** icon → **Open model page**.
3. Keep that tab open, ideally in its own Chrome window. The helper works only in that tab, and only
   on that exact page.

If MakerWorld shows a security check in that tab, complete it yourself. The helper waits, then
carries on.

## 6. First test

1. **Orders** → **Upload report (.xlsx)**, and choose the webstore *Order Reconciliation Report*.
2. Tick one or two orders → **Create batch**.
3. **Batches** → **Queue download**. Watch progress on the **Downloads** page.
4. In the batch, open **Objects**. Each base should show **no watermark**. Click a picture to see
   the back and top views.
5. **Queue build**, then **Show in Finder**. Open a plate in Bambu Studio: each object should be
   listed by its name (for example *Asha - base*).

---

## Updates

SnapSTL checks for updates when it opens and every 6 hours.

- **In the app:** the sidebar shows **Update available**. Click it → **Settings → Updates** lists
  what changed → **Update now**. SnapSTL installs it and restarts (a few seconds), and the page reloads.
- **The Chrome helper** reloads itself within a minute and refreshes the MakerWorld tab. If its
  popup says to reload it, do so once in `chrome://extensions`.
- **From Terminal**, the same thing:

```bash
cd ~/SnapSTL-app && zsh scripts/update.sh
```

SnapSTL won't update while a name is downloading or a batch is building (wait or pause downloads),
or if files in `~/SnapSTL-app` were edited by hand. To undo hand edits:

```bash
git -C ~/SnapSTL-app checkout -- .
```

Orders, batches, settings and downloaded models live outside this folder, so updates never touch them.

### For the owner: publishing an update

Changes are made and tested on the development Mac, merged into `main` and pushed:

```bash
git push origin main
```

Every SnapSTL then shows **Update available**.

## Moving your data from the old Mac (optional)

A new install starts empty. To bring over orders, batches, settings and downloaded models, copy these
two folders from the old Mac to the same places on the new one **before step 2**:

- `~/Library/Application Support/SnapSTL`: orders, batches and settings (including the license password).
- `~/SnapSTL`: downloaded models and output plates.

These hold customer names. Copy them directly (USB stick or AirDrop), and don't upload them anywhere.

## If something goes wrong

| Problem | What to do |
|---|---|
| `curl` doesn't print `200` | Wait 30 seconds and try again. Still nothing: re-run `zsh scripts/make_app.sh` and check the log at `~/Library/Logs/SnapSTL.log`. |
| "Permission denied (publickey)" | The deploy key isn't on GitHub yet, or was added to another repository. Redo step 1. |
| Settings → Updates says "Couldn't reach the SnapSTL repository" | Check the internet connection and the `ssh -T` check in step 1. |
| "Not possible to fast-forward" | Someone committed on this Mac. Ask the owner; to throw those changes away: `git -C ~/SnapSTL-app reset --hard origin/main` |
| Helper says **Not running** | SnapSTL isn't running. Open SnapSTL.app, or re-run the installer. |
| Helper says **Not on the model page** | Click the helper icon → **Open model page**. |
| Downloads fail with "No license password" | Enter the license password in **Settings** and save. Then **Retry failed** on Downloads. |
| "didn't confirm the license password" | Reload the helper in `chrome://extensions`, then **Retry failed**. |
| Bambu Studio shows the file name instead of the names | Plates built with an older SnapSTL: open the batch → **Rebuild plates**. |

Every screen is explained in SnapSTL's **Help** page.

## Uninstalling

```bash
launchctl bootout gui/$(id -u)/local.snapmagicians.snapstl && rm ~/Library/LaunchAgents/local.snapmagicians.snapstl.plist && rm -r ~/Applications/SnapSTL.app
```

This leaves your data folders (above) in place. Remove the helper in `chrome://extensions`.
