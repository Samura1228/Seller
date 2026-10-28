#!/bin/bash
# Starts the helper automatically when you log in to the Mac, and restarts it if it stops.
# Usage (from the helper folder):  bash install_autostart.sh      Remove: bash install_autostart.sh remove
set -e
LABEL="cy.limassol-seller.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"

if [ "$1" = "remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Автозапуск выключен."
  exit 0
fi

[ -x "$PY" ] || { echo "Сначала создай окружение: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.limassol-helper"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/helper.py</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.limassol-helper/helper.log</string>
  <key>StandardErrorPath</key><string>$HOME/.limassol-helper/helper.log</string>
</dict></plist>
PLIST
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Готово: помощник запущен и будет стартовать сам. Лог: ~/.limassol-helper/helper.log"
