#!/bin/bash
# Stream candump from the active CAN iface to the terminal AND tee it to a timestamped log.
#
# PASSIVE viewer: it shows + records whatever broadcast traffic is on the bus the iface is
# already on. It does NOT bring up or reconfigure the iface and never transmits -- run
# ./bringup.sh first to put can0 on the bus/bitrate you want (passive by default).
#
#   ./tools/sniff.sh              sniff the sole can iface -> dumps/sniff/sniff-<iface>-<ts>.txt
#   IFACE=can1 ./tools/sniff.sh   pick a specific iface when several are present
#
# Ctrl-C to stop (the log is flushed and complete). Uses candump -ta (absolute timestamps),
# so the saved file drops straight into tools/can_field_finder.py.
set -e

# repo root = parent of this script's dir (tools/)
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pick the interface: explicit IFACE wins; else the sole can* iface; else require IFACE.
# (mirrors bringup.sh so both tools agree on the iface.)
if [ -z "$IFACE" ]; then
  mapfile -t CANS < <(ip -o link show 2>/dev/null | grep -oE 'can[0-9]+' | sort -u)
  if [ "${#CANS[@]}" -eq 1 ]; then
    IFACE="${CANS[0]}"
  elif [ "${#CANS[@]}" -eq 0 ]; then
    echo "ERROR: no can* interface present. Bring one up first: ./bringup.sh" >&2; exit 1
  else
    echo "ERROR: multiple can interfaces (${CANS[*]}). Pick one: IFACE=canN ./tools/sniff.sh" >&2; exit 1
  fi
fi

if ! ip link show "$IFACE" 2>/dev/null | grep -qE 'state UP|UP,'; then
  echo "ERROR: $IFACE is down. Bring it up first: ./bringup.sh (passive) or --bcan / --tx." >&2; exit 1
fi

OUT_DIR="$REPO/dumps/sniff"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/sniff-${IFACE}-$(date +%Y%m%d_%H%M%S).txt"

echo "sniffing $IFACE (Ctrl-C to stop) -> $OUT"
candump -ta "$IFACE" | tee "$OUT"
