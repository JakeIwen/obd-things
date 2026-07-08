#!/bin/bash
# Cron kickoff for the battery voltage monitor -- keeps the crontab line to a single path.
# Ensures the log dir, bounds the run with `timeout`, and appends all output to the log. Any extra
# args (e.g. --no-notify) pass through to voltage_mon.py. For interactive testing run voltage_mon.py
# directly -- this wrapper sends output to the log, not your terminal.
#
#   crontab:  0 10-22/2 * * *  /home/pi/dev/obd-things/projects/battery/voltage_mon.sh
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # projects/battery/ -> repo root
LOG="$REPO/tmp/battery/voltage_mon.log"
mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
exec /usr/bin/timeout 90 /usr/bin/python3 "$REPO/projects/battery/voltage_mon.py" "$@"
