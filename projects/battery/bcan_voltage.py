#!/usr/bin/env python3
"""Read 12 V system voltage PASSIVELY from the B-CAN body bus (broadcast frame 0x46C).

Unlike read_voltage.py (active UDS poll of the radar, dead when parked) and the ELM327 dongle
(dongle_voltage.py, permanent-power but flaky auto-sleep), this never transmits: it camps
listen-only on the 125k body bus and decodes the system-voltage field that the BCM broadcasts.

VERIFIED 2026-06-26: voltage lives in **0x46C bytes[4:5] big-endian / 400 = volts** (~0.0025 V/LSB).
Confirmed by an engine ON->OFF transition (14.24 V charging -> settles 12.48-12.80 V resting, a clean
alternator-drop + surface-charge decay). `0x46C` broadcasts ~2 Hz while the bus is awake.

Two modes:
  * DEFAULT (passive-on-wake): camp listen-only and read whatever 0x46C is on the wire. B-CAN only
    carries traffic when something woke it -- a **key-fob UNLOCK** (~95 s; a door-open does NOT) or
    ignition. Fully asleep -> silent -> returns a "bus asleep" failure (expected, not a bug). Captures
    a reading whenever the owner approaches/uses the van, but can't poll a sleeping van on its own.
  * --wake (autonomous): FIRST probes passively (listen-only) -- if the bus is ALREADY awake
    (fob/ignition/ENGINE RUNNING) it just reads, and NEVER transmits onto the live bus. Only when the
    bus is genuinely silent does it TX a brief benign burst (0x7FF/DLC0) that wakes it via
    wake-on-activity; the bus then carries traffic ~10 s -- enough to read 0x46C -- then re-sleeps.
    Verified 2026-06-26. This is the only way to poll a parked, untouched van. After an active wake it
    restores the iface to passive. Use a COARSE cron cadence (hourly) -- each wake briefly powers the
    body modules. The unused id is never actuated on.

    python3 projects/battery/bcan_voltage.py                 # -> "12.5 V" (passive; needs an awake bus)
    python3 projects/battery/bcan_voltage.py --quiet          # -> "12.5"
    python3 projects/battery/bcan_voltage.py --csv --warn 12.0 # log + flag low
    python3 projects/battery/bcan_voltage.py --no-bringup      # assume can0 already up @125k passive
    python3 projects/battery/bcan_voltage.py --timeout 10      # wait up to 10s for a frame (a wake window)
    python3 projects/battery/bcan_voltage.py --wake           # AUTONOMOUS: wake the bus, then read

Exit codes (match read_voltage.py / dongle_voltage.py so one notifier wrapper handles all sources):
    0  read OK and >= --warn        1  read failed (incl. bus asleep)        2  read OK but BELOW --warn

SAFETY: both modes first run a passive bus-identity check (classify_bus) and ABORT without reading or
transmitting if the adapter looks like it's on C-CAN (500k powertrain/diag) rather than B-CAN -- detected
via C-CAN signature ids, rx-error spikes (500k sampled at 125k), or an active bus with no B-CAN signature.
This guarantees the wake burst is never injected onto the powertrain bus.

Default mode is read-only (listen-only); --wake transmits a benign burst. Needs sudo to bring up the iface.
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
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "bcan_voltage.csv")

CHANNEL = "can0"
BITRATE = canbus.BITRATE_BCAN  # B-CAN body bus (125k)
VOLT_ID = 0x46C           # BCM broadcast frame carrying system voltage
SFF_MASK = 0x7FF          # standard 11-bit id mask
DIVISOR = 400.0           # voltage = (word & VOLT_MASK) / 400 (verified 2026-06-26; recal vs multimeter)
VOLT_MASK = 0x1FFF        # 0x46C byte[4] HIGH bits are status flags (saw bit6=0x4000 set -> phantom +51 V);
                          # the voltage is the LOW 13 bits of the bytes[4:5] BE word
V_SANE = (6.0, 18.0)      # plausible 12 V-system rail; frames decoding outside this are dropped as corrupt
# Bus identity + wake now live in lib/canbus (identify_bus / tx_wake_burst); classify_bus below just maps them.


def classify_bus(channel=CHANNEL, probe=2.0):
    """Map the generic lib.canbus.identify_bus() to this reader's verdict: 'bcan' (safe to read), 'silent'
    (asleep -> safe to TX-wake), or 'foreign' (C-CAN / wrong-rate / unknown -> ABORT: do not read or TX)."""
    bus = canbus.identify_bus(channel, probe)
    if bus == "b-can":
        return "bcan", "B-CAN confirmed"
    if bus == "silent":
        return "silent", "no traffic"
    return "foreign", f"not B-CAN (identify_bus={bus})"


def bring_up_passive(channel=CHANNEL, bitrate=BITRATE):
    """Ensure `channel` is UP at `bitrate`, listen-only ON (passive, never TX/ACK)."""
    return canbus.bring_up_passive(channel, bitrate)


def read_voltage(channel=CHANNEL, timeout=4.0, divisor=DIVISOR):
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


def read_with_wake(channel=CHANNEL, timeout=4.0, divisor=DIVISOR, bringup=True):
    """Read 0x46C, transmitting a wake burst ONLY if the bus is currently silent. If the bus is
    already active (fob/ignition/ENGINE RUNNING) we never transmit -- we just read what's on the
    wire. Brings the iface up passive first (unless bringup=False) so the probe + any read are
    listen-only. Returns (volts, status). This is what the autonomous monitor calls."""
    if bringup and not bring_up_passive(channel):
        return None, "could not bring up can0 @125k passive (sudo rights? adapter plugged?)"
    verdict, detail = classify_bus(channel)
    if verdict == "foreign":                       # wrong bus -> never read or TX here
        return None, f"ABORT: not B-CAN -- {detail} (adapter on C-CAN/wrong bus? refusing to read or TX)"
    if verdict == "bcan":                          # already awake -> read passively, NEVER TX
        v, s = read_voltage(channel, timeout, divisor)
        return v, (s + " [passive: bus already awake]" if v is not None else s)
    # verdict == "silent": asleep -> safe to TX-wake, then RE-VERIFY before trusting/reading
    if not canbus.tx_wake_burst(channel, BITRATE):
        return None, "bus silent and could not arm/wake (sudo? adapter? listen-only stuck?)"
    try:
        verdict2, detail2 = classify_bus(channel)
        if verdict2 == "foreign":
            return None, f"ABORT post-wake: not B-CAN -- {detail2} (woke a non-B-CAN bus; discarding)"
        v, s = read_voltage(channel, timeout, divisor)
    finally:
        canbus.restore_passive(channel, BITRATE)  # always hand the iface back to passive
    return v, (s + " [tx-waked]" if v is not None else s)


def main():
    ap = argparse.ArgumentParser(description="Passively read system voltage from B-CAN frame 0x46C.")
    ap.add_argument("--channel", default=CHANNEL)
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--timeout", type=float, default=4.0, help="seconds to wait for a 0x46C frame")
    ap.add_argument("--divisor", type=float, default=DIVISOR, help="raw/divisor = volts (cal; default 400)")
    ap.add_argument("--no-bringup", action="store_true",
                    help="don't (re)bring-up the iface; assume it's already up @125k passive")
    ap.add_argument("--wake", action="store_true",
                    help="ACTIVE: TX a benign burst to wake a sleeping bus, then read (autonomous mode)")
    args = ap.parse_args()

    if args.wake:
        # autonomous: probe passively, and only TX a wake burst if the bus is actually silent
        volts, status = read_with_wake(args.channel, args.timeout, args.divisor,
                                       bringup=not args.no_bringup)
    else:
        if not args.no_bringup and not bring_up_passive(args.channel):
            if not args.quiet:
                print(f"could not bring up {args.channel} @125k passive (sudo rights? adapter plugged?)",
                      file=sys.stderr)
            sys.exit(1)
        verdict, detail = classify_bus(args.channel)
        if verdict == "foreign":                   # refuse to read off the wrong bus
            if not args.quiet:
                print(f"ABORT: not B-CAN -- {detail} (adapter on C-CAN/wrong bus?)", file=sys.stderr)
            sys.exit(1)
        volts, status = read_voltage(args.channel, args.timeout, args.divisor)

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
