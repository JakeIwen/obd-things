#!/usr/bin/env python3
"""Read 12 V system voltage passively from B-CAN broadcast frame 0x46C.

The CLI resolves the stable B-CAN role by USB serial/controller id and holds
shared role plus current-channel observer locks. It requires the interface to
already be exact passive classical CAN and never changes a link or transmits.

VERIFIED 2026-06-26: voltage lives in **0x46C bytes[4:5] big-endian / 400 = volts** (~0.0025 V/LSB).
Confirmed by an engine ON->OFF transition (14.24 V charging -> settles 12.48-12.80 V resting, a clean
alternator-drop + surface-charge decay). `0x46C` broadcasts ~2 Hz while the bus is awake.

B-CAN carries traffic after a key-fob unlock or with ignition awake. A fully
asleep bus returns an expected unavailable result; the retired fixed-channel
autonomous-wake CLI is intentionally not reproduced here.

    python3 projects/battery/bcan_voltage.py                 # -> "12.5 V" (passive; needs an awake bus)
    python3 projects/battery/bcan_voltage.py --quiet          # -> "12.5"
    python3 projects/battery/bcan_voltage.py --csv --warn 12.0 # log + flag low
    python3 projects/battery/bcan_voltage.py --timeout 10      # wait up to 10s for a frame (a wake window)

Exit codes (shared by the passive voltage reader CLIs):
    0  read OK and >= --warn        1  read failed (incl. bus asleep)        2  read OK but BELOW --warn

The low-level decoder accepts an explicit resolved channel so the broker can
reuse it while holding its own role lease; no ephemeral channel is a default.
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
from lib import can_runtime_route, canbus             # noqa: E402
from lib.canbus import append_csv                     # noqa: E402,F401  re-exported for callers
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "bcan_voltage.csv")

BUS = "b-can"
VOLT_ID = 0x46C           # BCM broadcast frame carrying system voltage
SFF_MASK = 0x7FF          # standard 11-bit id mask
DIVISOR = 400.0           # voltage = (word & VOLT_MASK) / 400 (verified 2026-06-26; recal vs multimeter)
VOLT_MASK = 0x1FFF        # 0x46C byte[4] HIGH bits are status flags (saw bit6=0x4000 set -> phantom +51 V);
                          # the voltage is the LOW 13 bits of the bytes[4:5] BE word
V_SANE = (6.0, 18.0)      # plausible 12 V-system rail; frames decoding outside this are dropped as corrupt
# Bus identity is a second passive evidence check after stable role resolution.


def classify_bus(channel, probe=2.0):
    """Map the generic lib.canbus.identify_bus() to this reader's verdict: 'bcan' (safe to read), 'silent'
    (asleep), or 'foreign' (unexpected traffic -> abort the read)."""
    bus = canbus.identify_bus(channel, probe)
    if bus == "b-can":
        return "bcan", "B-CAN confirmed"
    if bus == "silent":
        return "silent", "no traffic"
    return "foreign", f"not B-CAN (identify_bus={bus})"


def read_voltage(channel, timeout=4.0, divisor=DIVISOR):
    """Camp on `channel` listen-only and decode 0x46C bytes[4:5] BE / divisor.
    Reads several frames within `timeout` and returns the median (resting is steady, charging jitters).
    Returns (volts_float_or_None, status_str)."""
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    except OSError as e:
        return None, f"cannot open CAN socket ({e})"
    try:
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                     struct.pack("=II", VOLT_ID, SFF_MASK))   # only 0x46C reaches us
        try:
            s.bind((channel,))
        except OSError as e:
            return None, f"cannot bind {channel} (is it up? {e})"
        deadline = time.time() + timeout
        volts = []
        while time.time() < deadline and len(volts) < 7:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                frame = s.recv(16)
            except socket.timeout:
                break
            except OSError as e:
                if e.errno == errno.ENETDOWN:
                    return None, f"{channel} went down mid-read"
                return None, f"recv error ({e})"
            can_id, dlc, data = struct.unpack("=IB3x8s", frame)
            data = data[:dlc]
            if len(data) >= 6:
                v = (((data[4] << 8) | data[5]) & VOLT_MASK) / divisor   # mask off byte[4] status bits
                if V_SANE[0] <= v <= V_SANE[1]:                          # drop corrupt/out-of-range frames
                    volts.append(v)
    finally:
        s.close()
    if not volts:
        return None, "no 0x46C in window (bus asleep? fob-unlock to wake the body bus)"
    volts.sort()
    return round(volts[len(volts) // 2], 2), f"ok ({len(volts)} frame{'s' if len(volts) != 1 else ''})"


def main():
    ap = argparse.ArgumentParser(description="Passively read system voltage from B-CAN frame 0x46C.")
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--timeout", type=float, default=4.0, help="seconds to wait for a 0x46C frame")
    ap.add_argument("--divisor", type=float, default=DIVISOR, help="raw/divisor = volts (cal; default 400)")
    args = ap.parse_args()

    try:
        ownership = can_runtime_route.acquire_passive_bus_route(BUS)
    except (OSError, RuntimeError, ValueError) as exc:
        if not args.quiet:
            print(f"passive {BUS} role unavailable: {exc}", file=sys.stderr)
        sys.exit(1)
    with ownership:
        channel = ownership.route.channel
        verdict, detail = classify_bus(channel)
        if verdict == "foreign":                   # refuse to read off the wrong bus
            if not args.quiet:
                print(f"ABORT: not B-CAN -- {detail}", file=sys.stderr)
            sys.exit(1)
        volts, status = read_voltage(channel, args.timeout, args.divisor)

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
        flag = f"  ** LOW (< {args.warn} V) **" if low else ""
        print(f"{ts}  bcan  {volts:.2f} V{flag}")
    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
