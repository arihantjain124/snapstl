#!/bin/zsh
# Installs SnapSTL on this Mac:
#   • a background service (starts at login, restarts if it stops) serving the web portal
#     at http://127.0.0.1:8317, running downloads and building batches
#   • ~/Applications/SnapSTL.app — opens the SnapSTL window
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "Installing uv (Python tool manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
APP="$HOME/Applications/SnapSTL.app"
LABEL="local.snapmagicians.snapstl"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/SnapSTL.log"

echo "Installing SnapSTL's Python dependencies…"
(cd "$PROJECT" && "$UV" sync --no-dev --quiet)

# ---- background service ----
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLISTXML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$UV</string><string>run</string><string>--no-dev</string><string>--quiet</string>
    <string>--project</string><string>$PROJECT</string><string>snapstl-serve</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLISTXML
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# ---- app (window) ----
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>SnapSTL</string>
  <key>CFBundleDisplayName</key><string>SnapSTL</string>
  <key>CFBundleIdentifier</key><string>local.snapmagicians.snapstl.app</string>
  <key>CFBundleVersion</key><string>0.2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>SnapSTL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/SnapSTL" <<LAUNCH
#!/bin/zsh
cd "$PROJECT"
exec "$UV" run --no-dev --quiet snapstl >> "$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/SnapSTL"

# ---- Bambuddy (print farm): runs on the Windows Bambuddy computer (scripts/windows/bambuddy-server.ps1),
#      never on a Mac. SNAPSTL_BAMBUDDY_URL=http://<Bambuddy computer>:8000 → connect SnapSTL to it.
BAMBUDDY="on your Windows Bambuddy computer — enter its address in Settings → Connections"
if [ -n "${SNAPSTL_BAMBUDDY_URL:-}" ]; then
  for _ in $(seq 1 45); do curl -fs -o /dev/null --max-time 2 http://127.0.0.1:8317/api/auth-check && break; sleep 2; done
  if curl -fs -o /dev/null --max-time 30 -X POST -H 'Content-Type: application/json' \
       -d "{\"url\": \"$SNAPSTL_BAMBUDDY_URL\"}" http://127.0.0.1:8317/api/farm/connect; then
    BAMBUDDY="connected to $SNAPSTL_BAMBUDDY_URL"
  else
    BAMBUDDY="couldn't connect to $SNAPSTL_BAMBUDDY_URL yet — set it in SnapSTL: Settings → Connections"
  fi
fi

echo
echo "Installed:"
echo "  • Background service — starts at login; web portal: http://127.0.0.1:8317"
echo "  • $APP — opens the SnapSTL window (drag it to the Dock)"
echo "  • Bambuddy (print farm) — $BAMBUDDY"
echo "To uninstall the service: launchctl bootout gui/\$(id -u)/$LABEL && rm $PLIST"
