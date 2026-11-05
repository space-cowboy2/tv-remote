#!/bin/bash
# Build TVRemoteHub.app and install it as the hub's launchd agent on macOS.
#
#   deploy/macos/build-app.sh [path/to/venv]     (default: <repo>/.venv)
#
# Why: macOS applies Local Network permission only to apps. Python started
# directly by launchd is blocked from reaching the TV (errno 65) even after
# you click Allow. The hub runs inside this small app so the permission sticks.
#
# Needs the Xcode Command Line Tools (installed with Homebrew). Safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$(cd "${1:-$REPO/.venv}" && pwd)"
HUB_BIN="$VENV/bin/tvremote-hub"
APP="$HOME/Applications/TVRemoteHub.app"
LABEL="com.tvremote.hub"
BUNDLE_ID="com.tvremote.hub"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/tvremote-hub.log"
DOMAIN="gui/$(id -u)"

[ -x "$HUB_BIN" ] || { echo "tvremote-hub not found in $VENV (run: pip install -e .)" >&2; exit 1; }
command -v clang >/dev/null || { echo "clang missing: run xcode-select --install" >&2; exit 1; }

# Stop the running agent (if any) before replacing its files.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

echo "Building $APP"
mkdir -p "$APP/Contents/MacOS"
clang -O2 -Wall -o "$APP/Contents/MacOS/TVRemoteHub" "$REPO/deploy/macos/launcher.c"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>TV Remote Hub</string>
  <key>CFBundleDisplayName</key><string>TV Remote Hub</string>
  <key>CFBundleExecutable</key><string>TVRemoteHub</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>TV Remote Hub talks to your Samsung TV on your home network.</string>
  <key>NSBonjourServices</key>
  <array><string>_samsungmsf._tcp</string></array>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist"
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --verbose=1 "$APP"

echo "Installing launchd agent $AGENT"
mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>AssociatedBundleIdentifiers</key><string>$BUNDLE_ID</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/TVRemoteHub</string>
    <string>$HUB_BIN</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TVREMOTE_BIND</key><string>127.0.0.1</string>
    <key>TVREMOTE_PORT</key><string>8080</string>
    <key>TVREMOTE_SECURE_COOKIE</key><string>1</string>
    <key>TVREMOTE_WEB_DIR</key><string>$REPO/web</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
plutil -lint "$AGENT"

launchctl bootstrap "$DOMAIN" "$AGENT"
echo
echo "Started. macOS should ask to let \"TV Remote Hub\" find devices on your local network: click Allow."
echo "If no prompt appears, open System Settings > Privacy & Security > Local Network, turn on"
echo "TV Remote Hub, then run: launchctl kickstart -k $DOMAIN/$LABEL"
echo "Check: curl -s localhost:8080/api/health   Logs: $LOG"
