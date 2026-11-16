#!/bin/zsh
# Installs (or updates) Bambuddy on this Mac, next to SnapSTL: the open-source print-farm manager
# (AGPL-3.0, https://github.com/maziggy/bambuddy) that SnapSTL's "Push to queue" sends plates to.
#   • the version in scripts/bambuddy-version.txt (tested with this SnapSTL), in ~/SnapSTL-Bambuddy
#   • a background service (starts at login, restarts if it stops): http://127.0.0.1:8000
#     (this computer only; SnapSTL connects to it by itself)
#   • its data in ~/Library/Application Support/Bambuddy, log in ~/Library/Logs/Bambuddy
# Run by make_app.sh (install) and update.sh; safe to run again. Skip Bambuddy: SNAPSTL_NO_BAMBUDDY=1.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${BAMBUDDY_VERSION:-$(tr -d '[:space:]' < "$PROJECT/scripts/bambuddy-version.txt")}"
REPO="https://github.com/maziggy/bambuddy.git"
DIR="${BAMBUDDY_DIR:-$HOME/SnapSTL-Bambuddy}"
PORT="${BAMBUDDY_PORT:-8000}"
DATA="$HOME/Library/Application Support/Bambuddy"
LOGS="$HOME/Library/Logs/Bambuddy"
LABEL="local.snapmagicians.bambuddy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
changed=0

# Never a second Bambuddy next to one installed another way (Bambuddy's own installer: launchd
# com.bambuddy.app, ~/bambuddy): SnapSTL uses that one. Only SnapSTL's own copy is ever stopped.
other=""
launchctl print "gui/$(id -u)/com.bambuddy.app" >/dev/null 2>&1 && other="launchd com.bambuddy.app"
[ -z "$other" ] && [ -f "$HOME/Library/LaunchAgents/com.bambuddy.app.plist" ] && other="com.bambuddy.app"
if [ -n "$other" ]; then
  if [ -d "$DIR/.git" ] || [ -f "$PLIST" ]; then
    echo "Found your own Bambuddy ($other) — SnapSTL's extra copy is stopped and yours is started again (nothing deleted)."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"   # SnapSTL's own service file only; its Bambuddy folder and data stay
    launchctl kickstart -k "gui/$(id -u)/com.bambuddy.app" 2>/dev/null || true
  else
    echo "Found Bambuddy already installed ($other) — SnapSTL uses it (nothing installed or changed)."
  fi
  exit 0
fi
if [ ! -d "$DIR/.git" ] && curl -fs -m 3 "http://127.0.0.1:$PORT/api/v1/auth/status" >/dev/null 2>&1; then
  echo "A Bambuddy is already answering on port $PORT — SnapSTL uses it (nothing installed)."
  exit 0
fi

if [ -d "$DIR/.git" ]; then
  current="$(git -C "$DIR" describe --tags --exact-match 2>/dev/null || echo none)"
  if [ "$current" != "$VERSION" ]; then
    echo "Updating Bambuddy $current → $VERSION…"
    git -C "$DIR" fetch -q --depth 1 origin tag "$VERSION"
    git -C "$DIR" -c advice.detachedHead=false checkout -q "$VERSION"
    changed=1
  fi
else
  echo "Downloading Bambuddy $VERSION into $DIR…"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$VERSION" "$REPO" "$DIR"
  changed=1
fi

if [ "$changed" = 1 ] || [ ! -x "$DIR/venv/bin/uvicorn" ]; then
  echo "Installing Bambuddy's Python parts (first time: a few minutes)…"
  "$UV" venv -q --allow-existing --python 3.12 "$DIR/venv"
  "$UV" pip install -q --python "$DIR/venv/bin/python" -r "$DIR/requirements.lock"
  changed=1
fi

mkdir -p "$DATA" "$LOGS" "$HOME/Library/LaunchAgents"
NEW_PLIST="$(cat <<PLISTXML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/venv/bin/uvicorn</string><string>backend.app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>
    <string>--loop</string><string>asyncio</string>
    <string>--timeout-graceful-shutdown</string><string>5</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>DATA_DIR</key><string>$DATA</string>
    <key>LOG_DIR</key><string>$LOGS</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/bambuddy.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/bambuddy.out.log</string>
</dict></plist>
PLISTXML
)"
if [ ! -f "$PLIST" ] || [ "$(cat "$PLIST")" != "$NEW_PLIST" ]; then
  print -r -- "$NEW_PLIST" > "$PLIST"
  changed=1
fi
if [ "$changed" = 1 ] || ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
fi

for _ in {1..45}; do
  if curl -fs "http://127.0.0.1:$PORT/api/v1/auth/status" >/dev/null 2>&1; then
    echo "Bambuddy $VERSION is running: http://127.0.0.1:$PORT (SnapSTL → Printers to add your printers)"
    exit 0
  fi
  sleep 2
done
echo "Bambuddy didn't answer yet — log: $LOGS/bambuddy.out.log"
