#!/usr/bin/env python3
"""Read 12 V system voltage PASSIVELY from the C-CAN (500k powertrain) bus.

The powertrain bus DOES broadcast system voltage (found 2026-06-28 from tmp/captures/ccan analysis;
see memory battery-monitor-passive-plan). Two broadcast fields (default mode is passive/no-TX; --wake pokes once):

  * 0x2EF  bytes[0:1] little-endian uint16 / DIVISOR  -- FINE. Only present with IGNITION ON / running.
           ratio engine/ignition = 1.17 (alternator); same 2-byte ÷~400 family as B-CAN 0x46C.
  * 0x41A  byte0 / COARSE_DIVISOR                      -- COARSE. Present even on a parked WAKE
           (~12.5 V resting), so it's the C-CAN analogue of B-CAN 0x46C for parked reads.

Reads whichever is on the wire, PREFERRING 0x2EF (finer). So: connected with ignition on -> 0x2EF;
catching a parked wake -> 0x41A. A fully-asleep van is silent -> use --wake (or "bus asleep" failure).

--wake (ACTIVE parked wake, verified 2026-07-08; see docs/bus-map.md): a raw 0x7FF broadcast burst does NOT
wake C-CAN (selective wake -- junk frames aren't a wake reason), but ONE addressed UDS read to the RF Hub
(rf_hub: KL30-powered, always-awake RKE receiver) DOES -- the diag exchange trips the gateway's network-
management wake -> full C-CAN broadcast incl. 0x41A @10 Hz for ~15 s, re-sleeps ~30 s later. So --wake pokes
rf_hub, then reads 0x41A. SIDE EFFECT: the wake also powers the BCM's accessory rails (dash USB / dashcam
boots) for the awake window -- owner OK'd unprompted parked TX; use a COARSE cadence (battery). Only fires on
a SILENT bus (never active/foreign); the poke is self-validating -- if rf_hub doesn't answer we're not on C-CAN.

SCALE NOT YET PINNED -- the FIELDS are confirmed voltage (range/ratio/load-response exclude temp+checksum)
but the exact divisor needs ONE ground-truth reading. `--calibrate V` reads the live raw and prints the
divisor to use (ignition on, against a multimeter or the radar's 0x1006). Defaults: 0x2EF /400 (->~11.5/13.5,
consistent with the ~11.9 V battery), 0x41A /14.2.

    python3 projects/battery/ccan_voltage.py                  # -> "12.0 V" (passive; needs an awake bus)
    python3 projects/battery/ccan_voltage.py --quiet
    python3 projects/battery/ccan_voltage.py --csv --warn 12.0
    python3 projects/battery/ccan_voltage.py --no-bringup     # assume can0 already up @500k passive
    python3 projects/battery/ccan_voltage.py --calibrate 12.4 # ignition on: print the divisor that fits
    python3 projects/battery/ccan_voltage.py --wake           # parked: poke rf_hub to wake C-CAN, read 0x41A

Exit codes (match the other readers so one notifier handles all sources):
    0  read OK and >= --warn        1  read failed (incl. bus asleep / wrong bus)        2  below --warn

SAFETY: a passive bus-identity guard (classify_bus) runs first and ABORTS without reading OR poking if the
adapter looks like it's on B-CAN (body signature ids) or a mis-sampled bus (rx-error spike) rather than
C-CAN. Default mode is read-only (listen-only); --wake sends one addressed UDS read to rf_hub (arms the
iface briefly), then restores passive. Needs sudo to bring up / arm the iface.
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
BITRATE = canbus.BITRATE_CCAN  # C-CAN / HS-CAN powertrain bus (500k)
FINE_ID = 0x2EF               # bytes[0:1] LE uint16 / DIVISOR (ignition-on, fine)
COARSE_ID = 0x41A             # byte0 / COARSE_DIVISOR (parked-wake-readable, coarse)
SFF_MASK = 0x7FF              # 11-bit id mask
DIVISOR = 400.0              # 0x2EF: raw/400 = volts (default; pin with --calibrate vs a multimeter)
COARSE_DIVISOR = 14.2        # 0x41A: raw/14.2 = volts (coarse; likely small offset -- recal near threshold)
V_SANE = (6.0, 18.0)         # plausible 12 V rail; frames decoding outside are dropped as corrupt
# Bus identity + wake now live in lib/canbus (identify_bus / poke_wake); classify_bus below just maps them.


def bring_up_passive(channel=CHANNEL, bitrate=BITRATE):
    """Ensure `channel` is UP @bitrate, listen-only ON (passive, never TX/ACK)."""
    return canbus.bring_up_passive(channel, bitrate)


def classify_bus(channel=CHANNEL, probe=2.0):
    """Map the generic lib.canbus.identify_bus() to this reader's verdict: 'ccan' (safe to read), 'silent'
    (asleep -> poke to wake), or 'foreign' (B-CAN / wrong-rate / unknown -> abort; don't read or poke)."""
    bus = canbus.identify_bus(channel, probe)
    if bus == "c-can":
        return "ccan", "C-CAN confirmed"
    if bus == "silent":
        return "silent", "no traffic (ignition off / bus asleep)"
    return "foreign", f"not C-CAN (identify_bus={bus})"


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


def read_with_wake(channel=CHANNEL, timeout=6.0, bringup=True):
    """Read C-CAN voltage, waking a SILENT bus with an rf_hub poke first. If the bus is already awake
    (ignition/fob) we never poke -- just read passively. Returns (volts, status). ABORTS without poking if
    the bus classifies 'foreign' (B-CAN / wrong bus)."""
    if bringup and not bring_up_passive(channel):
        return None, "could not bring up can0 @500k passive (sudo rights? adapter plugged?)"
    verdict, detail = classify_bus(channel)
    if verdict == "foreign":                       # wrong bus -> never read or poke here
        return None, f"ABORT: not C-CAN -- {detail} (adapter on B-CAN/wrong bus? refusing to read or poke)"
    if verdict == "ccan":                          # already awake -> read passively, NEVER poke
        v, s = read_voltage(channel, timeout)
        return v, (s + " [passive: bus already awake]" if v is not None else s)
    # silent -> poke rf_hub to wake the broadcast, then read 0x41A on the (armed) iface
    if not canbus.poke_wake(channel, BITRATE):
        return None, "C-CAN silent and rf_hub wake-poke got no response (on C-CAN? rf_hub reachable?)"
    try:
        v, s = read_voltage(channel, timeout)
    finally:
        canbus.restore_passive(channel, BITRATE)  # always hand the iface back to passive
    return v, (s + " [rfh-waked]" if v is not None else s)


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
    ap.add_argument("--wake", action="store_true",
                    help="parked: poke rf_hub (one UDS read) to wake a SILENT C-CAN, then read 0x41A")
    args = ap.parse_args()

    if args.wake:                                  # ACTIVE: rf_hub poke wakes the bus, then read
        volts, status = read_with_wake(args.channel, max(args.timeout, 6.0), bringup=not args.no_bringup)
    else:
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
