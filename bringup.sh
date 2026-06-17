#!/bin/bash
# Unified PEAK PCAN-USB bring-up for the Promaster CAN buses.
#
# PASSIVE BY DEFAULT (listen-only ON) -> only sniffs, never transmits / ACKs. This is the
# safe default for watching a bus (incl. while AlfaOBD/MX drives it on the OBD splitter).
# Pass --tx to ARM transmission, which UDS tools (uds_send.py, did_sweep.py, ...) require.
#
#   ./bringup.sh                 HS-CAN 500k, passive sniff            (DEFAULT)
#   ./bringup.sh --tx            HS-CAN 500k, ARMED (can send UDS)
#   ./bringup.sh --bcan          body bus 125k, passive sniff
#   ./bringup.sh --bcan --tx     body bus 125k, ARMED
#   ./bringup.sh --probe         cycle common low-speed rates (passive), report which is live
#   ./bringup.sh --bitrate N     override bitrate (e.g. 250000)
#   IFACE=can1 ./bringup.sh ...  override interface (default: auto-pick the sole can iface)
#
# Verified buses on this van: HS-CAN 500k (OBD 6/14, diagnostics) and body B-CAN 125k.
set -e

BITRATE=500000        # HS-CAN default; --bcan flips to 125k; --bitrate overrides either
LISTEN_ONLY=on        # passive by default; --tx arms (listen-only off)
PROBE=0
# Common low-speed body/comfort CAN rates, most-likely first for Fiat Ducato (--probe).
PROBE_RATES=(125000 50000 100000 250000 83333 33333)

while [ $# -gt 0 ]; do
  case "$1" in
    --bcan)    BITRATE=125000; shift;;
    --tx)      LISTEN_ONLY=off; shift;;
    --probe)   PROBE=1; shift;;
    --bitrate) BITRATE="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0;;
    *) echo "unknown arg: $1 (try --help)" >&2; exit 2;;
  esac
done

if ! lsusb | grep -qi peak; then
  echo "ERROR: PEAK PCAN-USB not on the USB bus. Replug it (powered hub / a direct Pi port,"
  echo "       not behind the HDD hub) and re-run. dmesg hints:"
  dmesg | grep -iE 'peak|usb 1-1' | tail -5
  exit 1
fi

# Pick the interface: explicit IFACE wins; else the sole can* iface; else require IFACE.
if [ -z "$IFACE" ]; then
  mapfile -t CANS < <(ip -o link show 2>/dev/null | grep -oE 'can[0-9]+' | sort -u)
  if [ "${#CANS[@]}" -eq 1 ]; then
    IFACE="${CANS[0]}"
  elif [ "${#CANS[@]}" -eq 0 ]; then
    echo "ERROR: no can* interface present. Is the adapter enumerated? (dmesg | grep peak)"; exit 1
  else
    echo "ERROR: multiple can interfaces (${CANS[*]}). Pick one: IFACE=canN ./bringup.sh ..."; exit 1
  fi
fi

sudo modprobe can-isotp 2>/dev/null || true   # needed for UDS/ISO-TP; harmless when just sniffing

# Bring one rate up passive and count clean frames in `secs`. Echoes the count (-1 = rejected).
liveness() {  # args: bitrate secs
  sudo ip link set "$IFACE" down 2>/dev/null || true
  if ! sudo ip link set "$IFACE" up type can bitrate "$1" listen-only on 2>/dev/null; then
    echo -1; return
  fi
  timeout "$2" candump -n 50 "$IFACE" 2>/dev/null | grep -c "$IFACE" || true
}

if [ "$PROBE" -eq 1 ]; then
  echo "--- bitrate probe on $IFACE (listen-only; bus must be awake) ---"
  best_rate=0; best_n=0
  for r in "${PROBE_RATES[@]}"; do
    n=$(liveness "$r" 2)
    if [ "$n" -lt 0 ]; then printf "  %-7s : rate rejected by adapter\n" "$r"; continue; fi
    printf "  %-7s : %s frames\n" "$r" "$n"
    if [ "$n" -gt "$best_n" ]; then best_n="$n"; best_rate="$r"; fi
  done
  sudo ip link set "$IFACE" down 2>/dev/null || true
  if [ "$best_n" -gt 0 ]; then
    echo "BEST: ${best_rate} (${best_n} frames). Bring it up with: ./bringup.sh --bitrate ${best_rate}"
  else
    echo "No frames at any rate. Bus awake? Right adapter pinout?"
  fi
  exit 0
fi

# ALWAYS down first: setting bitrate on an already-up iface fails ("Device or resource
# busy"). Matters when switching speed/adapter (e.g. 500k HS-CAN <-> 125k body bus).
sudo ip link set "$IFACE" down 2>/dev/null || true
# explicit listen-only state -> the flag is sticky across `up`
sudo ip link set "$IFACE" up type can bitrate "$BITRATE" listen-only "$LISTEN_ONLY"
echo "$IFACE up @ ${BITRATE} (listen-only ${LISTEN_ONLY}):"
ip -details link show "$IFACE" | grep -E 'can.*state'
if [ "$LISTEN_ONLY" = off ]; then
  echo "*** ARMED (--tx): listen-only OFF -> this interface WILL transmit. ***"
else
  echo "SNIFF mode: passive RX only, will not transmit. Pass --tx to arm."
fi

echo "--- 2s liveness check (bus must be live: ignition ON for HS; door/fob to wake body) ---"
if timeout 2 candump -n 5 "$IFACE" 2>/dev/null | grep -q "$IFACE"; then
  echo "OK: bus is live."
else
  echo "WARN: no frames in 2s. Bus asleep/ignition off? Wrong bitrate (try --probe)? Right pins?"
fi
