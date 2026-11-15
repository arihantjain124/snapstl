# Installing SnapSTL on a new Mac

This guide sets up SnapSTL on a new Mac from the shared **SnapSTL zip**. It takes about 10 minutes.

## What you need

- A Mac with **macOS 12 or newer**.
- **Google Chrome**, signed in to the MakerWorld account you use for the name organizer.
- **Bambu Studio**, to open and print the plates.
- An internet connection (the first install downloads Python and SnapSTL's parts).
- The **license password** for the MakerWorld model. Without it SnapSTL won't download anything, so
  the models never carry MakerWorld's watermark.

---

## 1. Unzip into a permanent folder

1. Copy the zip to the new Mac (AirDrop, USB stick or a shared drive).
2. Double-click it. You get a folder named **SnapSTL-app**.
3. Move that folder into your **home folder** (in Finder: Go → Home), so it sits at `~/SnapSTL-app`.

Don't leave it in **Downloads** or on the **Desktop**: the service runs from this folder, and it stops
working if the folder is moved or deleted.

If you put it somewhere else, use that folder in place of `~/SnapSTL-app` in the commands below.

## 2. Install the app and its background service

Open **Terminal** (Applications → Utilities → Terminal). Then run these two commands, one at a time.

Clear the "downloaded from the internet" flag that macOS puts on unzipped files:

```bash
xattr -dr com.apple.quarantine ~/SnapSTL-app
```

Install SnapSTL:

```bash
cd ~/SnapSTL-app && zsh scripts/make_app.sh
```

The first run takes a few minutes. It installs these:

- **uv**, the Python tool manager, if the Mac doesn't have it yet.
- The **SnapSTL service**, which starts at login, restarts itself if it stops and keeps the Mac awake.
- **SnapSTL.app** in `~/Applications`. Drag it to the Dock if you like.

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
4. Check that **SnapSTL Helper** shows the version that came with the zip (2.2.1 or later).
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

## Updating to a newer zip

1. Quit SnapSTL.app.
2. Unzip the new zip and replace the old folder with it. Run this in Terminal, using the folder the
   zip produced:

```bash
rm -rf ~/SnapSTL-app && mv ~/Downloads/SnapSTL-app ~/SnapSTL-app && xattr -dr com.apple.quarantine ~/SnapSTL-app
```

3. Re-run the installer:

```bash
cd ~/SnapSTL-app && zsh scripts/make_app.sh
```

4. In `chrome://extensions`, press the reload ↻ button on **SnapSTL Helper**.

Your orders, batches, settings and downloaded models are kept: they live outside this folder.

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
| "zsh: permission denied" or "operation not permitted" | Run the `xattr` command from step 2 again, then the installer. |
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
