#!/bin/zsh
# Installs (or updates) SnapSTL on this Mac in one go. In Terminal:
#   curl -fsSL https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/install-mac.sh | zsh
# Puts SnapSTL in ~/SnapSTL-app (a copy of the public repository, so the app can update itself),
# installs the background service and ~/Applications/SnapSTL.app, then opens the app.
# Your orders, settings and downloads live elsewhere and are never touched.
set -euo pipefail
REPO="https://github.com/arihantjain124/snapstl.git"
DIR="$HOME/SnapSTL-app"

if ! xcode-select -p >/dev/null 2>&1; then
  echo "SnapSTL needs Apple's command-line tools (they include git)."
  echo "A window opens now: click Install, wait until it finishes, then run the same command again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

if [ -d "$DIR/.git" ]; then
  echo "Updating SnapSTL in $DIR…"
  git -C "$DIR" pull --ff-only --quiet
elif [ -d "$DIR" ]; then
  echo "Turning the existing $DIR into an updatable copy…"
  git -C "$DIR" init -q -b main
  git -C "$DIR" remote add origin "$REPO" 2>/dev/null || git -C "$DIR" remote set-url origin "$REPO"
  git -C "$DIR" fetch -q origin
  git -C "$DIR" reset -q --hard origin/main
  git -C "$DIR" branch -q --set-upstream-to=origin/main main
else
  echo "Downloading SnapSTL into $DIR…"
  git clone -q "$REPO" "$DIR"
fi

zsh "$DIR/scripts/make_app.sh"
open "$HOME/Applications/SnapSTL.app" || true
echo
echo "SnapSTL is installed. Next, in the app: Settings → Setup (add the helper to Chrome, enter the license password), then Printers (add your printers to Bambuddy)."
