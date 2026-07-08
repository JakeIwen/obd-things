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
BITRATE = 125000          # B-CAN body bus
VOLT_ID = 0x46C           # BCM broadcast frame carrying system voltage
SFF_MASK = 0x7FF          # standard 11-bit id mask
DIVISOR = 400.0           # voltage = (word & VOLT_MASK) / 400 (verified 2026-06-26; recal vs multimeter)
VOLT_MASK = 0x1FFF        # 0x46C byte[4] HIGH bits are status flags (saw bit6=0x4000 set -> phantom +51 V);
                          # the voltage is the LOW 13 bits of the bytes[4:5] BE word
V_SANE = (6.0, 18.0)      # plausible 12 V-system rail; frames decoding outside this are dropped as corrupt
WAKE_ID = 0x7FF           # benign unused id for the --wake burst (no module actuates on it)
WAKE_N, WAKE_GAP = 75, 0.02   # ~1.5s of bus activity trips wake-on-activity; bus then stays up ~10s

# Bus-identity guard: refuse to read/TX if the adapter is on C-CAN (500k powertrain/diag) not B-CAN.
# Signature ids observed in captures (see [[promaster-dasm-comms]] / bcan-bringup): high-rate C-CAN
# powertrain frames vs the always-present B-CAN body frames (0x0A0 @40Hz, 0x46C, 0x3E0..).
CCAN_IDS = {0x100, 0x101, 0x103, 0x104, 0x10F, 0x110, 0x116, 0x160, 0x0FE, 0x0FA, 0x0EE, 0x0EA}
BCAN_IDS = {0x46C, 0x0A0, 0x0E0, 0x2EA, 0x3DC, 0x3DE, 0x3E0, 0x3E2, 0x3E4, 0x3E6, 0x354, 0x356}
RX_ERR_ABORT = 200        # rx-error climb during the probe -> 500k bus sampled at 125k = wrong bus


def classify_bus(channel=CHANNEL, probe=2.0):
    """PASSIVE bus-identity check. Never transmits. Returns (verdict, detail):
      'silent'  -- no traffic (asleep): safe to TX-wake.
      'bcan'    -- B-CAN confirmed (body signature ids present): safe to read.
      'foreign' -- C-CAN signature ids, OR rx-errors climbing (500k bus at 125k), OR an ACTIVE bus
                   with NO B-CAN signature -> almost certainly the wrong bus. ABORT: do not read/TX.
    On a live B-CAN, 0x0A0 (40 Hz) etc. always appear within the probe, so an active-but-unrecognized
    bus is treated as foreign rather than risk reading/injecting on C-CAN."""
    try:
        ids, rxd = canbus.probe_ids(channel, probe)
    except OSError as e:
        return "foreign", f"cannot open/bind {channel} ({e})"

    ccan_hit = sorted(ids & CCAN_IDS)
    if ccan_hit:
        return "foreign", f"C-CAN id(s) seen ({', '.join(hex(c) for c in ccan_hit)})"
    if rxd > RX_ERR_ABORT:
        return "foreign", f"{rxd} rx errors in {probe:.0f}s -> 500k C-CAN sampled at 125k?"
    bcan_hit = [c for c in ids if c in BCAN_IDS or (c & 0x1FFF0000) == 0x1E340000]
    if bcan_hit:
        return "bcan", f"B-CAN confirmed ({len(ids)} ids incl. {', '.join(hex(c) for c in sorted(bcan_hit)[:3])})"
    if ids:
        return "foreign", f"active bus, no B-CAN signature ({len(ids)} unrecognized ids)"
    return "silent", "no traffic"


def bring_up_passive(channel=CHANNEL, bitrate=BITRATE):
    """Ensure `channel` is UP at `bitrate`, listen-only ON (passive, never TX/ACK)."""
    return canbus.bring_up_passive(channel, bitrate)


def restore_passive(channel=CHANNEL, bitrate=BITRATE):
    """Put the iface back to the safe passive default (listen-only ON) after an active --wake."""
    canbus.bring_up_passive(channel, bitrate)


def wake_bus(channel=CHANNEL, bitrate=BITRATE):
    """ACTIVE: arm the iface and TX a brief benign burst to wake a sleeping body bus.
    Verified 2026-06-26: ~1.5s of 0x7FF/0-length frames wakes B-CAN, which then carries normal
    traffic (incl. 0x46C) for ~10s before re-sleeping -- ample to read voltage. Leaves the iface
    ARMED for the immediate read; the caller must restore_passive() afterward. Returns True on
    success. 0x7FF/DLC0 is an unused id no module acts on; we only need bus activity for the wake."""
    try:
        if not canbus.ip_up(channel, bitrate, listen_only=False, restart_ms=100):
            return False
    except Exception:
        return False
    if canbus.is_listen_only(channel):     # sticky-flag guard: must be cleared or we can't TX
        return False
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((channel,))
        frame = struct.pack("=IB3x8s", WAKE_ID, 0, b"")   # id=0x7FF, dlc=0
        for _ in range(WAKE_N):
            try:
                s.send(frame)
            except OSError:
                pass        # unACKed on a still-sleeping bus -> restart-ms recovers; keep poking
            time.sleep(WAKE_GAP)
        s.close()
        return True
    except OSError:
        return False


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
    if not wake_bus(channel):
        return None, "bus silent and could not arm/wake (sudo? adapter? listen-only stuck?)"
    try:
        verdict2, detail2 = classify_bus(channel)
        if verdict2 == "foreign":
            return None, f"ABORT post-wake: not B-CAN -- {detail2} (woke a non-B-CAN bus; discarding)"
        v, s = read_voltage(channel, timeout, divisor)
    finally:
        restore_passive(channel)                  # always hand the iface back to passive
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
