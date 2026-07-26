#!/usr/bin/env python3
"""Read 12 V system voltage PASSIVELY from C-CAN frame 0x41A.

The verified decode is ``4.0 + byte0 * 0.05 V``. A controlled charger transition on 2026-07-25
produced ``BE, BC, BA, ... B0`` while BCM Status stepped from 13.50 V toward its 12.80 V
maintenance plateau; a fresh later BCM snapshot reported 12.70 V (+30) / 12.80 V (ADC) while
0x41A was AE/B0. These observations fit the affine decode exactly.

Frame 0x2EF remains a verified ignition-on presence gate, but its payload is NOT an approved voltage
source. The current ``FF 21`` payload disproved the former low-13-bit /400 interpretation and suggests
mode/multiplex semantics that remain unresolved. This reader deliberately ignores 0x2EF so it cannot
override the verified 0x41A value.

--wake (ACTIVE parked wake, verified 2026-07-08; see docs/bus-map.md): a raw 0x7FF broadcast burst does NOT
wake C-CAN (selective wake -- junk frames aren't a wake reason), but ONE addressed UDS read to the RF Hub
(rf_hub: KL30-powered, always-awake RKE receiver) DOES -- the diag exchange trips the gateway's network-
management wake -> full C-CAN broadcast incl. 0x41A @10 Hz for ~15 s, re-sleeps ~30 s later. So --wake pokes
rf_hub, then reads 0x41A. SIDE EFFECT: the wake also powers the BCM's accessory rails (dash USB / dashcam
boots) for the awake window -- owner OK'd unprompted parked TX; use a COARSE cadence (battery). Only fires on
a SILENT bus (never active/foreign); the poke is self-validating -- if rf_hub doesn't answer we're not on C-CAN.

    python3 projects/battery/ccan_voltage.py                  # -> "12.0 V" (passive; needs an awake bus)
    python3 projects/battery/ccan_voltage.py --quiet
    python3 projects/battery/ccan_voltage.py --csv --warn 12.0
    python3 projects/battery/ccan_voltage.py --no-bringup     # assume can0 already up @500k passive
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
VOLT_ID = 0x41A               # byte0 * 0.05 V + 4.0 V
SFF_MASK = 0x7FF              # 11-bit id mask
VOLT_SCALE = 0.05
VOLT_OFFSET = 4.0
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
    """Return volts for a verified 0x41A frame, or None."""
    if can_id == VOLT_ID and len(data) >= 1:
        return VOLT_OFFSET + data[0] * VOLT_SCALE
    return None


def read_voltage(channel=CHANNEL, timeout=4.0):
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
    ap = argparse.ArgumentParser(description="Passively read system voltage from C-CAN frame 0x41A.")
    ap.add_argument("--channel", default=CHANNEL)
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--timeout", type=float, default=4.0, help="seconds to wait for a frame")
    ap.add_argument("--no-bringup", action="store_true",
                    help="don't (re)bring-up the iface; assume it's already up @500k passive")
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
