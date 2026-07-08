#!/bin/bash
# Stream candump from the active CAN iface to the terminal AND tee it to a timestamped log.
#
# PASSIVE viewer: it shows + records whatever broadcast traffic is on the bus the iface is
# already on. It does NOT bring up or reconfigure the iface and never transmits -- run
# ./bringup.sh first to put can0 on the bus/bitrate you want (passive by default).
#
# SWITCHING BUSES (e.g. C-CAN 500k -> B-CAN 125k): this script never sets bitrate, and its
# only guard is "iface UP", which stays true at the OLD rate. So just re-pinning the adapter
# and re-running dump.sh logs GARBAGE/nothing (bitrate mismatch) while looking fine. You must
# re-run bringup.sh for the new bus in between. Correct sequence:
#     Ctrl-C this dump  ->  move PCAN to the B-CAN pins  ->  ./bringup.sh --bcan  ->  ./tools/dump.sh
# (Passive/listen-only makes the physical switch electrically safe; bringup's liveness check
#  also catches a still-asleep B-CAN -- wake it with a FOB unlock.)
#
#   ./tools/dump.sh                   sniff the sole can iface -> dumps/dump-<ts>.log
#   ./tools/dump.sh --filepath PATH   write to PATH instead of the default
#   ./tools/dump.sh --stdout          stream to the console only, no log file
#   ./tools/dump.sh --timeout SECS    auto-stop after SECS seconds (default: until Ctrl-C)
#   IFACE=can1 ./tools/dump.sh        pick a specific iface when several are present
#
# By default the live dump is shown on the console AND teed to a log. --stdout drops the
# log and streams only to the console (its banner goes to stderr, so stdout stays a clean
# candump stream you can pipe). --stdout and --filepath are mutually exclusive.
#
# --filepath PATH may be absolute or relative; a relative PATH is resolved against your
# current working directory (NOT the repo root), and missing parent dirs are created.
# Only the default output is anchored under the repo's dumps/.
#
# --timeout SECS (positive integer) wraps candump in `timeout`, so it runs for SECS then
# stops cleanly (log flushed/complete); omit it to run until Ctrl-C. A timed-out run exits
# 124, the conventional timeout status.
#
# Ctrl-C to stop (the log is flushed and complete). Uses candump -ta (absolute timestamps),
# so the saved file drops straight into tools/can_field_finder.py.
set -eo pipefail   # pipefail: a --timeout hit propagates candump's 124 through the | tee

# repo root = parent of this script's dir (tools/)
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUT=""        # --filepath overrides; empty -> timestamped default under dumps/
STDOUT_ONLY=0 # --stdout streams to the console only, no log file
TIMEOUT=""    # --timeout SECS auto-stops after N seconds; empty -> run until Ctrl-C
while [ $# -gt 0 ]; do
  case "$1" in
    --filepath) OUT="$2"; shift 2;;
    --stdout)   STDOUT_ONLY=1; shift;;
    --timeout)  TIMEOUT="$2"; shift 2;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \?//'; exit 0;;
    *) echo "unknown arg: $1 (try --help)" >&2; exit 2;;
  esac
done

if [ "$STDOUT_ONLY" -eq 1 ] && [ -n "$OUT" ]; then
  echo "ERROR: --stdout (no log file) and --filepath are mutually exclusive." >&2; exit 2
fi
if [ -n "$TIMEOUT" ] && ! [[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --timeout wants a positive integer (seconds), got: $TIMEOUT" >&2; exit 2
fi

# Pick the interface: explicit IFACE wins; else the sole can* iface; else require IFACE.
# (mirrors bringup.sh so both tools agree on the iface.)
if [ -z "$IFACE" ]; then
  mapfile -t CANS < <(ip -o link show 2>/dev/null | grep -oE 'can[0-9]+' | sort -u)
  if [ "${#CANS[@]}" -eq 1 ]; then
    IFACE="${CANS[0]}"
  elif [ "${#CANS[@]}" -eq 0 ]; then
    echo "ERROR: no can* interface present. Bring one up first: ./bringup.sh" >&2; exit 1
  else
    echo "ERROR: multiple can interfaces (${CANS[*]}). Pick one: IFACE=canN ./tools/dump.sh" >&2; exit 1
  fi
fi

if ! ip link show "$IFACE" 2>/dev/null | grep -qE 'state UP|UP,'; then
  echo "ERROR: $IFACE is down. Bring it up first: ./bringup.sh (passive) or --bcan / --tx." >&2; exit 1
fi

# Build the capture command, optionally bounded by --timeout (wraps candump, not tee).
# --foreground: keep candump in the TTY's foreground process group so Ctrl-C reaches it.
# Without it, `timeout` runs candump in its own process group; on a SILENT bus (no frames,
# so a dying `tee` never sends candump a SIGPIPE) nothing stops candump until the timeout
# expires -- Ctrl-C appears ignored. candump has no children, so --foreground costs nothing.
CAP=(candump -ta "$IFACE")
[ -n "$TIMEOUT" ] && CAP=(timeout --foreground "$TIMEOUT" "${CAP[@]}")
STOP="Ctrl-C to stop"
[ -n "$TIMEOUT" ] && STOP="stops after ${TIMEOUT}s (or Ctrl-C)"

# --stdout: stream to the console only (no file). Banner -> stderr so stdout is a clean
# candump stream that can be piped. Otherwise tee to a log (default or --filepath).
if [ "$STDOUT_ONLY" -eq 1 ]; then
  echo "sniffing $IFACE ($STOP) -> stdout only (no log file)" >&2
  "${CAP[@]}"
else
  # Default output -> dumps/dump-<ts>.log; --filepath takes it anywhere (dir is created).
  [ -n "$OUT" ] || OUT="$REPO/dumps/dump-$(date +%Y%m%d_%H%M%S).log"
  mkdir -p "$(dirname "$OUT")"
  echo "sniffing $IFACE ($STOP) -> $OUT"
  "${CAP[@]}" | tee "$OUT"
fi
