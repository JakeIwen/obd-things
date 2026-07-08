#!/usr/bin/env python3
"""Read 12 V system voltage PASSIVELY from the C-CAN (500k powertrain) bus.

The powertrain bus DOES broadcast system voltage (found 2026-06-28 from tmp/captures/ccan analysis;
see memory battery-monitor-passive-plan). Two fields, both pure broadcast -- this NEVER transmits:

  * 0x2EF  bytes[0:1] little-endian uint16 / DIVISOR  -- FINE. Only present with IGNITION ON / running.
           ratio engine/ignition = 1.17 (alternator); same 2-byte ÷~400 family as B-CAN 0x46C.
  * 0x41A  byte0 / COARSE_DIVISOR                      -- COARSE. Present even on a parked fob/door WAKE
           (~12.5 V resting), so it's the C-CAN analogue of B-CAN 0x46C for parked reads.

Reads whichever is on the wire, PREFERRING 0x2EF (finer). So: connected with ignition on -> 0x2EF;
catching a parked fob/door wake -> 0x41A. A fully-asleep van is silent -> "bus asleep" failure (expected).

NO --wake / TX-wake: VERIFIED 2026-07-07 that a 0x7FF burst @500k does NOT wake a parked C-CAN (no 0x41A;
a 5s/~490-frame burst drew only a lone 0x200, never the voltage). The fob/door/ignition wake is gateway-
orchestrated (a real NM wake), NOT replicable by raw bus activity -- so C-CAN voltage is readable ONLY when
something else wakes the bus (ignition on, or a fob/door event). For autonomous PARKED monitoring the Pi can
only wake B-CAN (bcan_voltage.py --wake); keep the adapter there for parked coverage.

SCALE NOT YET PINNED -- the FIELDS are confirmed voltage (range/ratio/load-response exclude temp+checksum)
but the exact divisor needs ONE ground-truth reading. `--calibrate V` reads the live raw and prints the
divisor to use (ignition on, against a multimeter or the radar's 0x1006). Defaults: 0x2EF /400 (->~11.5/13.5,
consistent with the ~11.9 V battery), 0x41A /14.2.

    python3 projects/battery/ccan_voltage.py                  # -> "12.0 V" (passive; needs an awake bus)
    python3 projects/battery/ccan_voltage.py --quiet
    python3 projects/battery/ccan_voltage.py --csv --warn 12.0
    python3 projects/battery/ccan_voltage.py --no-bringup     # assume can0 already up @500k passive
    python3 projects/battery/ccan_voltage.py --calibrate 12.4 # ignition on: print the divisor that fits

Exit codes (match the other readers so one notifier handles all sources):
    0  read OK and >= --warn        1  read failed (incl. bus asleep / wrong bus)        2  below --warn

SAFETY: a passive bus-identity guard (classify_bus) runs first and ABORTS without reading if the adapter
looks like it's on B-CAN (body signature ids) or a mis-sampled bus (rx-error spike) rather than C-CAN.
Read-only (listen-only); needs sudo only to bring the iface up.
"""
import os
import sys
import time
import errno
import socket
import struct
import argparse
import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)
from lib import canbus                                # noqa: E402  shared CAN-iface plumbing
from lib.canbus import iface_bitrate, append_csv      # noqa: E402,F401  re-exported for callers
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "ccan_voltage.csv")

CHANNEL = "can0"
BITRATE = 500000              # C-CAN / HS-CAN powertrain bus
FINE_ID = 0x2EF               # bytes[0:1] LE uint16 / DIVISOR (ignition-on, fine)
COARSE_ID = 0x41A             # byte0 / COARSE_DIVISOR (parked-wake-readable, coarse)
SFF_MASK = 0x7FF              # 11-bit id mask
DIVISOR = 400.0              # 0x2EF: raw/400 = volts (default; pin with --calibrate vs a multimeter)
COARSE_DIVISOR = 14.2        # 0x41A: raw/14.2 = volts (coarse; likely small offset -- recal near threshold)
V_SANE = (6.0, 18.0)         # plausible 12 V rail; frames decoding outside are dropped as corrupt

# Bus-identity guard. C-CAN powertrain signature ids (high-rate; present ignition-on AND in parked wakes),
# vs B-CAN body signature ids (abort -- wrong bus). Verified against tmp/captures/{ccan,bcan}.
CCAN_IDS = {0x100, 0x101, 0x103, 0x104, 0x10F, 0x110, 0x116, 0x0EA, 0x0EE, 0x0FA, 0x0FE,
            FINE_ID, COARSE_ID}
BCAN_IDS = {0x46C, 0x0A0, 0x2EA, 0x3DC, 0x3DE, 0x3E0, 0x3E2, 0x3E4, 0x3E6, 0x354, 0x356}
RX_ERR_ABORT = 200           # rx-error climb during the probe -> a 125k bus mis-sampled at 500k


def bring_up_passive(channel=CHANNEL, bitrate=BITRATE):
    """Ensure `channel` is UP @bitrate, listen-only ON (passive, never TX/ACK)."""
    return canbus.bring_up_passive(channel, bitrate)


def classify_bus(channel=CHANNEL, probe=2.0):
    """PASSIVE identity check (never transmits). Returns (verdict, detail):
      'ccan'    -- C-CAN confirmed (powertrain signature ids present): safe to read.
      'foreign' -- B-CAN signature ids, OR rx-errors climbing (wrong-bitrate sampling), OR an ACTIVE bus
                   with no C-CAN signature -> wrong bus. ABORT.
      'silent'  -- no traffic (asleep / ignition off): nothing to read passively."""
    try:
        ids, rxd = canbus.probe_ids(channel, probe)
    except OSError as e:
        return "foreign", f"cannot open/bind {channel} ({e})"

    bcan_hit = sorted(ids & BCAN_IDS)
    if bcan_hit:
        return "foreign", f"B-CAN id(s) seen ({', '.join(hex(c) for c in bcan_hit)}) -- adapter on body bus?"
    if rxd > RX_ERR_ABORT:
        return "foreign", f"{rxd} rx errors in {probe:.0f}s -> wrong-bitrate bus?"
    ccan_hit = sorted(ids & CCAN_IDS)
    if ccan_hit:
        return "ccan", f"C-CAN confirmed ({len(ids)} ids incl. {', '.join(hex(c) for c in ccan_hit[:3])})"
    if ids:
        return "foreign", f"active bus, no C-CAN signature ({len(ids)} unrecognized ids)"
    return "silent", "no traffic (ignition off / bus asleep)"


def _decode(can_id, data):
    """Return volts for a 0x2EF/0x41A frame, or None."""
    if can_id == FINE_ID and len(data) >= 2:
        return ((data[0] | (data[1] << 8)) & 0x1FFF) / DIVISOR     # bytes[0:1] LE, low 13 bits
    if can_id == COARSE_ID and len(data) >= 1:
        return data[0] / COARSE_DIVISOR
    return None


def read_voltage(channel=CHANNEL, timeout=4.0, raw=False):
    """Camp listen-only and decode 0x2EF (preferred) or 0x41A. Returns (volts, status); with raw=True
    returns (volts, status, fine_raw_list, coarse_raw_list) for --calibrate."""
    fine, coarse, fine_raw, coarse_raw = [], [], [], []
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    except OSError as e:
        r = (None, f"cannot open CAN socket ({e})")
        return r + ([], []) if raw else r
    try:
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                     struct.pack("=IIII", FINE_ID, SFF_MASK, COARSE_ID, SFF_MASK))  # only our 2 ids
        try:
            s.bind((channel,))
        except OSError as e:
            r = (None, f"cannot bind {channel} (is it up? {e})")
            return r + ([], []) if raw else r
        deadline = time.time() + timeout
        while time.time() < deadline and len(fine) < 7 and len(coarse) < 15:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                frame = s.recv(16)
            except socket.timeout:
                break
            except OSError as e:
                if e.errno == errno.ENETDOWN:
                    r = (None, f"{channel} went down mid-read")
                    return r + ([], []) if raw else r
                break
            can_id, dlc, dat = struct.unpack("=IB3x8s", frame)
            can_id &= 0x7FF
            dat = dat[:dlc]
            v = _decode(can_id, dat)
            if v is None or not (V_SANE[0] <= v <= V_SANE[1]):
                continue
            if can_id == FINE_ID:
                fine.append(v); fine_raw.append(dat[0] | (dat[1] << 8))
            else:
                coarse.append(v); coarse_raw.append(dat[0])
    finally:
        s.close()
    if fine:
        fine.sort(); v = round(fine[len(fine) // 2], 2)
        r = (v, f"ok 0x2EF [fine, {len(fine)} frames]")
    elif coarse:
        coarse.sort(); v = round(coarse[len(coarse) // 2], 2)
        r = (v, f"ok 0x41A [coarse, {len(coarse)} frames]")
    else:
        r = (None, "no 0x2EF/0x41A in window (bus asleep / ignition off?)")
    return r + (fine_raw, coarse_raw) if raw else r


def main():
    ap = argparse.ArgumentParser(description="Passively read system voltage from C-CAN (0x2EF / 0x41A).")
    ap.add_argument("--channel", default=CHANNEL)
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--timeout", type=float, default=4.0, help="seconds to wait for a frame")
    ap.add_argument("--no-bringup", action="store_true",
                    help="don't (re)bring-up the iface; assume it's already up @500k passive")
    ap.add_argument("--calibrate", type=float, metavar="ACTUAL_V",
                    help="ignition on: read live raw and print the divisor that fits ACTUAL_V")
    args = ap.parse_args()

    if not args.no_bringup and not bring_up_passive(args.channel):
        if not args.quiet:
            print(f"could not bring up {args.channel} @500k passive (sudo? adapter?)", file=sys.stderr)
        sys.exit(1)
    verdict, detail = classify_bus(args.channel)
    if verdict == "foreign":
        if not args.quiet:
            print(f"ABORT: not C-CAN -- {detail}", file=sys.stderr)
        sys.exit(1)

    if args.calibrate is not None:
        v, status, fine_raw, coarse_raw = read_voltage(args.channel, max(args.timeout, 6.0), raw=True)
        if fine_raw:
            fine_raw.sort(); rw = fine_raw[len(fine_raw) // 2]
            print(f"0x2EF raw median={rw}  -> for {args.calibrate} V use  --divisor {rw / args.calibrate:.1f}"
                  f"  (current /{DIVISOR:.0f} = {rw / DIVISOR:.2f} V)")
        elif coarse_raw:
            coarse_raw.sort(); rw = coarse_raw[len(coarse_raw) // 2]
            print(f"0x41A raw median={rw}  -> for {args.calibrate} V set COARSE_DIVISOR = {rw / args.calibrate:.2f}"
                  f"  (current /{COARSE_DIVISOR} = {rw / COARSE_DIVISOR:.2f} V)")
        else:
            print(f"calibrate: no 0x2EF/0x41A seen ({status}) -- ignition on?", file=sys.stderr)
            sys.exit(1)
        return

    volts, status = read_voltage(args.channel, args.timeout)
    if args.csv:
        append_csv(args.csv_path, volts if volts is not None else "", status)
    if volts is None:
        if not args.quiet:
            print(f"voltage read FAILED: {status}", file=sys.stderr)
        sys.exit(1)
    low = args.warn is not None and volts < args.warn
    if args.quiet:
        print(volts)
    else:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts}  ccan  {volts:.2f} V  [{status}]" + ("  ** LOW **" if low else ""))
    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
