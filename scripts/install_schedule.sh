#!/bin/bash
# Install every wheelscan launchd job. Run from anywhere; it resolves the
# repository path itself. Safe to re-run - each job is unloaded first.
#
#   positions  weekdays 15:00 and 16:15  monitor open positions, alerts only
#   scan       weekdays 15:45            build tomorrow's watchlist
#   requote    weekdays 10:30            re-check it before you trade
#
# The scan runs at 15:45 rather than after the close because the scanner needs
# live quotes: option spreads blow out once market makers step back, and an
# after-hours scan rejects most of what it would otherwise surface. 15:45 is
# late enough to reflect the day and still inside the session.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
JOBS=("com.wheelscan.positions" "com.wheelscan.scan" "com.wheelscan.requote")

mkdir -p "$REPO/logs" "$AGENTS"

for label in "${JOBS[@]}"; do
    target="$AGENTS/$label.plist"
    sed "s|__REPO__|$REPO|g" "$REPO/scripts/$label.plist" > "$target"
    launchctl unload "$target" 2>/dev/null || true
    launchctl load "$target"
    echo "Installed $label"
done

cat <<EOF

  10:30  requote    re-quotes last night's scan, flags anything that gapped
  15:00  positions  open-position alerts
  15:45  scan       builds the watchlist for tomorrow
  16:15  positions  open-position alerts, marks settled

  Weekdays only, in this machine's local time ($(date +%Z)) - which tracks the
  US market only while the Mac stays on Eastern time.

  logs   : $REPO/logs/{positions,scan,requote}.{log,err}
  remove : for j in ${JOBS[*]}; do launchctl unload $AGENTS/\$j.plist && rm $AGENTS/\$j.plist; done

The 10:30 run pushes its verdict, so a gap reaches you before you trade.
Confirm delivery works first:  python wheel_positions.py --notify-test
EOF
