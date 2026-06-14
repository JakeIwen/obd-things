#!/bin/bash
# Reliable can0 bring-up for the PEAK PCAN-USB -> Promaster HS-CAN (500k).
# Encodes the two gotchas we hit: explicit `listen-only off`, and isotp module load.
set -e

if ! lsusb | grep -qi peak; then
  echo "ERROR: PEAK PCAN-USB not on the USB bus. Replug it (ideally a powered hub /"
  echo "       a direct Pi port, not behind the HDD hub) and re-run. dmesg hints:"
  dmesg | grep -iE 'peak|usb 1-1' | tail -5
  exit 1
fi

sudo modprobe can-isotp 2>/dev/null || true
sudo ip link set can0 down 2>/dev/null || true
# explicit listen-only off -> the flag is sticky across `up`
sudo ip link set can0 up type can bitrate 500000 listen-only off
echo "can0 up @ 500k:"
ip -details link show can0 | grep -E 'can.*state'

# quick liveness: should print a few powertrain frames if ignition is ON
echo "--- 2s liveness check (need ignition ON) ---"
if timeout 2 candump -n 5 can0 2>/dev/null | grep -q can0; then
  echo "OK: bus is live."
else
  echo "WARN: no frames in 2s. Ignition on? Right OBD pins (6/14)?"
fi
