#!/bin/bash
# Install the position monitor as a launchd job: weekdays at 15:00 and 16:15.
# Run from anywhere; it resolves the repository path itself.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.wheelscan.positions"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"
sed "s|__REPO__|$REPO|g" "$REPO/scripts/$LABEL.plist" > "$TARGET"

launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Installed $LABEL"
echo "  runs   : weekdays 15:00 and 16:15 (local time)"
echo "  logs   : $REPO/logs/positions.log"
echo "  remove : launchctl unload $TARGET && rm $TARGET"
echo
echo "Alerts only - it stays silent unless a position trips a threshold."
