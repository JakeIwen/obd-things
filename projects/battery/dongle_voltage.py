#!/usr/bin/env python3
"""Read 12 V battery voltage from the Wi-Fi OBD dongle (ELM327 `AT RV`) -- works while PARKED.

Unlike the radar UDS path (read_voltage.py), which is dead when the van sleeps (the radar is on
switched +15 power), the ELM327 dongle is powered from OBD pin 16 = PERMANENT battery, so its
`AT RV` reports battery voltage with NO CAN traffic, even with the vehicle fully asleep.

VERIFIED 2026-06-21: dongle "V-LINK" (ELM327 v2.1) reachable while parked at 192.168.0.10:35000
over wlan0; raw read under-read 0.93 V, calibrated in-device with `AT CV 1193` to match an 11.93 V
multimeter reading -- and the calibration persisted across a reset.

Cron-friendly: single shot, optional CSV log, --warn threshold with exit codes for a notifier.

    python3 projects/battery/dongle_voltage.py                  # -> "11.9 V"
    python3 projects/battery/dongle_voltage.py --quiet           # -> "11.9"
    python3 projects/battery/dongle_voltage.py --csv --warn 11.8  # log + flag low
    python3 projects/battery/dongle_voltage.py --connect          # best-effort bring up wlan0->dongle first
    python3 projects/battery/dongle_voltage.py --calibrate 12.55  # one-time: AT CV from a multimeter reading

Exit codes (match read_voltage.py so one notifier wrapper handles both sources):
    0  read OK and >= --warn        1  read failed        2  read OK but BELOW --warn (low battery)

Read-only on the vehicle (AT RV touches no CAN bus). Assumes wlan0 is associated to the dongle's
open AP (NetworkManager profile 'vlink-obd'); pass --connect to have it ensure that first.
"""
import os
import re
import sys
import time
import socket
import argparse
import datetime
import subprocess

HOST, PORT = "192.168.0.10", 35000        # ELM327 Wi-Fi dongle default endpoint
NM_PROFILE = "vlink-obd"                   # NetworkManager connection for the V-LINK AP
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)
from lib.canbus import append_csv          # noqa: E402,F401  shared CSV appender
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "dongle_voltage.csv")
_VOLT_RE = re.compile(r"([0-9]{1,2}\.[0-9]+)\s*V", re.I)


def _elm(sock, cmd, settle=0.5, timeout=2.0):
    """Send one ELM327 command, return the text up to the '>' prompt."""
    sock.sendall((cmd + "\r").encode())
    time.sleep(settle)
    sock.settimeout(timeout)
    buf = b""
    try:
        while True:
            d = sock.recv(256)
            if not d:
                break
            buf += d
            if b">" in buf:
                break
    except socket.timeout:
        pass
    return buf.decode(errors="replace").replace("\r", " ").replace("\n", " ").strip()


def ensure_connected():
    """Best-effort: make sure wlan0 is on the dongle AP. Returns True on success-ish.
    All subprocess calls are time-bounded so a stuck nmcli can't hang a cron run."""
    def _cur():
        try:
            return subprocess.run(["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", "wlan0"],
                                  capture_output=True, text=True, timeout=10).stdout
        except subprocess.SubprocessError:
            return ""
    if NM_PROFILE in _cur():
        return True
    try:
        subprocess.run(["sudo", "nmcli", "connection", "up", NM_PROFILE],
                       capture_output=True, timeout=45)
    except subprocess.SubprocessError:
        pass
    time.sleep(2)
    return NM_PROFILE in _cur()


def read_voltage(host=HOST, port=PORT, retries=2):
    """Return (volts_float_or_None, status_str)."""
    last = "no attempt"
    for _ in range(retries + 1):
        try:
            with socket.create_connection((host, port), timeout=6) as s:
                time.sleep(0.3)
                s.settimeout(1.0)
                try:
                    s.recv(256)          # drain power-on banner
                except socket.timeout:
                    pass
                _elm(s, "ATE0")          # echo off -> clean replies
                resp = _elm(s, "ATRV")
            m = _VOLT_RE.search(resp)
            if m:
                return float(m.group(1)), "ok"
            last = f"no voltage in reply {resp!r}"
        except OSError as e:
            last = f"connect/io error ({e})"
        time.sleep(0.5)
    return None, last


def calibrate(actual_v, host=HOST, port=PORT):
    """One-time: tell the ELM327 the true voltage so AT RV matches (AT CV xxxx)."""
    xxxx = int(round(actual_v * 100))
    with socket.create_connection((host, port), timeout=6) as s:
        time.sleep(0.3)
        try:
            s.recv(256)
        except OSError:
            pass
        _elm(s, "ATE0")
        ok = _elm(s, f"ATCV {xxxx:04d}")
        after = _elm(s, "ATRV")
    return ok, after


def main():
    ap = argparse.ArgumentParser(description="Read battery voltage from the ELM327 Wi-Fi dongle.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--quiet", action="store_true", help="print just the number (nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH)
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if below this threshold")
    ap.add_argument("--connect", action="store_true", help="ensure wlan0 is on the dongle AP first")
    ap.add_argument("--calibrate", type=float, metavar="ACTUAL_V",
                    help="one-time: send AT CV using a multimeter reading, then exit")
    args = ap.parse_args()

    if args.calibrate is not None:
        ok, after = calibrate(args.calibrate, args.host, args.port)
        print(f"AT CV -> {ok}   ATRV now -> {after}")
        return

    if args.connect and not ensure_connected():
        if not args.quiet:
            print("could not bring up wlan0 -> dongle AP", file=sys.stderr)
        sys.exit(1)

    volts, status = read_voltage(args.host, args.port)
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
        print(f"{ts}  dongle  {volts:.1f} V" + (f"  ** LOW (< {args.warn} V) **" if low else ""))
    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
