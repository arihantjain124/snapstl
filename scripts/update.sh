#!/bin/zsh
# Updates SnapSTL from its git repository and restarts it. Same as Settings → Updates → Update now.
#   cd ~/SnapSTL-app && zsh scripts/update.sh
# Orders, batches, settings and downloaded models live outside this folder and are kept.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
LABEL="local.snapmagicians.snapstl"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Files in $PROJECT were changed by hand:"; git status --short --untracked-files=no
  echo "Undo them with:  git -C \"$PROJECT\" checkout -- .   then run this again."; exit 1
fi
before="$(git rev-parse HEAD)"
git pull --ff-only --quiet
bambuddy() {  # the Bambuddy version this SnapSTL was tested with (scripts/bambuddy-version.txt)
  # only a Bambuddy an older SnapSTL installed next to it; Docker Bambuddy: run bambuddy-server.sh again
  if [ "${SNAPSTL_NO_BAMBUDDY:-0}" != 1 ] && [ -d "$HOME/SnapSTL-Bambuddy/.git" ]; then
    zsh "$PROJECT/scripts/bambuddy-mac.sh" || echo "Bambuddy update failed (SnapSTL still works)."
  fi
}
if [ "$before" = "$(git rev-parse HEAD)" ]; then echo "SnapSTL is already up to date ($(git log -1 --format='%h %s'))."; bambuddy; exit 0; fi
echo "Updated:"; git log --format='  • %s' "$before"..HEAD
"$UV" sync --no-dev --quiet
if [ -f "$HOME/Library/LaunchAgents/$LABEL.plist" ]; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "SnapSTL restarted."
  bambuddy
else
  zsh "$PROJECT/scripts/make_app.sh"
fi
echo "The SnapSTL Helper reloads itself in Chrome within a minute."
