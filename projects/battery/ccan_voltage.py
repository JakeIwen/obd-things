#!/usr/bin/env python3
"""Read 12 V system voltage passively from C-CAN frame 0x41A.

The verified decode is ``4.0 + byte0 * 0.05 V``. A controlled charger transition on 2026-07-25
produced ``BE, BC, BA, ... B0`` while BCM Status stepped from 13.50 V toward its 12.80 V
maintenance plateau; a fresh later BCM snapshot reported 12.70 V (+30) / 12.80 V (ADC) while
0x41A was AE/B0. These observations fit the affine decode exactly.

Frame 0x2EF remains a verified ignition-on presence gate, but its payload is NOT an approved voltage
source. The current ``FF 21`` payload disproved the former low-13-bit /400 interpretation and suggests
mode/multiplex semantics that remain unresolved. This reader deliberately ignores 0x2EF so it cannot
override the verified 0x41A value.

The CLI resolves the stable C-CAN role by USB serial/controller id and holds
shared role plus current-channel observer locks. It requires exact passive
classical CAN and never changes a link or transmits. The former fixed-channel
parked-wake path is retired; its verified behavior remains in ``docs/bus-map.md``.

    python3 projects/battery/ccan_voltage.py                  # -> "12.0 V" (passive; needs an awake bus)
    python3 projects/battery/ccan_voltage.py --quiet
    python3 projects/battery/ccan_voltage.py --csv --warn 12.0

Exit codes (match the other readers so one notifier handles all sources):
    0  read OK and >= --warn        1  read failed (incl. bus asleep / wrong bus)        2  below --warn

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
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "ccan_voltage.csv")

BUS = "c-can"
VOLT_ID = 0x41A               # byte0 * 0.05 V + 4.0 V
SFF_MASK = 0x7FF              # 11-bit id mask
VOLT_SCALE = 0.05
VOLT_OFFSET = 4.0
V_SANE = (6.0, 18.0)         # plausible 12 V rail; frames decoding outside are dropped as corrupt
# Bus identity is a second passive evidence check after stable role resolution.


def classify_bus(channel, probe=2.0):
    """Map the generic lib.canbus.identify_bus() to this reader's verdict: 'ccan' (safe to read), 'silent'
    (asleep), or 'foreign' (unexpected traffic -> abort the read)."""
    bus = canbus.identify_bus(channel, probe)
    if bus == "c-can":
        return "ccan", "C-CAN confirmed"
    if bus == "silent":
        return "silent", "no traffic (ignition off / bus asleep)"
    return "foreign", f"not C-CAN (identify_bus={bus})"


def _decode(can_id, data):
    """Return volts for a verified 0x41A frame, or None."""
    if can_id == VOLT_ID and len(data) >= 1:
        return VOLT_OFFSET + data[0] * VOLT_SCALE
    return None


def read_voltage(channel, timeout=4.0):
    """Camp listen-only and decode 0x41A. Returns ``(volts, status)``."""
    samples = []
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    except OSError as e:
        return None, f"cannot open CAN socket ({e})"
    try:
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                     struct.pack("=II", VOLT_ID, SFF_MASK))
        try:
            s.bind((channel,))
        except OSError as e:
            return None, f"cannot bind {channel} (is it up? {e})"
        deadline = time.time() + timeout
        while time.time() < deadline and len(samples) < 15:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                frame = s.recv(16)
            except socket.timeout:
                break
            except OSError as e:
                if e.errno == errno.ENETDOWN:
                    return None, f"{channel} went down mid-read"
                break
            can_id, dlc, dat = struct.unpack("=IB3x8s", frame)
            can_id &= 0x7FF
            dat = dat[:dlc]
            v = _decode(can_id, dat)
            if v is None or not (V_SANE[0] <= v <= V_SANE[1]):
                continue
            samples.append(v)
    finally:
        s.close()
    if not samples:
        return None, "no 0x41A in window (bus asleep / ignition off?)"
    samples.sort()
    v = round(samples[len(samples) // 2], 2)
    return v, f"ok 0x41A [verified affine, {len(samples)} frames]"


def main():
    ap = argparse.ArgumentParser(description="Passively read system voltage from C-CAN frame 0x41A.")
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--timeout", type=float, default=4.0, help="seconds to wait for a frame")
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
        if verdict == "foreign":
            if not args.quiet:
                print(f"ABORT: not C-CAN -- {detail}", file=sys.stderr)
            sys.exit(1)
        volts, status = read_voltage(channel, args.timeout)
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
