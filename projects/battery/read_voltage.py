#!/usr/bin/env python3
"""Wake the ACC radar and read its control-module voltage (a 12 V battery proxy).

Single-shot and cron-friendly. Brings up C-CAN (500k) ARMED if asked, enters the radar's
extended diagnostic session, reads DID 0x1006 (control-module voltage, u8 x0.1 V -- VERIFIED
against AlfaOBD's "Control module voltage (+15)" = 12.6 V), prints/logs it, then closes so the
radar falls back to sleep (no tester-present is held).

This is the ACTIVE path: it TRANSMITS one short UDS exchange, which briefly wakes the radar.
Keep the eventual cron interval COARSE (e.g. hourly) -- frequent polling keeps modules awake and
would itself drain the battery you're trying to watch. (The passive-on-wake alternative, which
never transmits, is the other approach; this active poll is what we're building first.)

    python3 projects/battery/read_voltage.py              # read once -> "12.6 V"
    python3 projects/battery/read_voltage.py --quiet       # print just "12.6" (cron/pipe)
    python3 projects/battery/read_voltage.py --csv         # also append a timestamped row
    python3 projects/battery/read_voltage.py --warn 12.0   # exit 2 if below threshold (for the notifier)
    python3 projects/battery/read_voltage.py --no-bringup  # skip the iface arm (bus already 500k armed)

Exit codes (a clean contract for the future cron + notifier):
    0  read OK (and >= --warn threshold, if given)
    1  read failed (no response / bus down / bad reply)
    2  read OK but BELOW the --warn threshold  -> low battery

Read-only on the vehicle (UDS service 22 only). Requires TX, so it auto-arms can0 @500k unless
--no-bringup is passed.
"""
import os
import sys
import csv
import time
import socket
import struct
import argparse
import datetime

# locate repo root (the dir containing lib/) regardless of how deep this lives
_ROOT = os.path.dirname(os.path.abspath(__file__))
while _ROOT != os.path.dirname(_ROOT) and not os.path.isdir(os.path.join(_ROOT, "lib")):
    _ROOT = os.path.dirname(_ROOT)
sys.path.insert(0, _ROOT)
from lib import uds                      # noqa: E402
from lib.modules import get              # noqa: E402

VOLT_DID = 0x1006        # control-module voltage on the radar
SCALE = 0.1              # u8 x 0.1 -> volts
BITRATE = 500000         # radar lives on C-CAN 500k
CSV_PATH = os.path.join(_ROOT, "tmp", "battery", "voltage.csv")
RADAR_RXID = get("radar_acc").rxid       # 0x18DAF12A: radar UDS responses (ECU->tester) ride this id


def passive_read(channel="can0", timeout=3.0):
    """PASSIVE, READ-ONLY catch of the radar's voltage -- no TX, no iface reconfigure, so it is safe
    to run alongside auto_drive_logger on C-CAN. The radar only reports 0x1006 when polled, so this
    yields a value ONLY while something else (e.g. the drive logger, which reads 0x1006 at ~1 Hz) is
    actively reading it: that read comes back as the ISO-TP single frame '04 62 10 06 XX' on
    RADAR_RXID. Returns (volts, status); (None, ...) if nothing is polling it. Never reconfigures the
    bus, so it must be on C-CAN 500k already (caller's job to check)."""
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                     struct.pack("=II", RADAR_RXID | socket.CAN_EFF_FLAG,
                                 socket.CAN_EFF_MASK | socket.CAN_EFF_FLAG))   # only the radar's id
        s.bind((channel,))
    except OSError as e:
        return None, f"cannot sniff {channel} ({e})"
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                _, dlc, data = struct.unpack("=IB3x8s", s.recv(16))
            except (socket.timeout, OSError):
                break
            data = data[:dlc]
            # ISO-TP single frame, positive response to ReadDataByIdentifier 0x1006: 04 62 10 06 XX
            if len(data) >= 5 and data[0] == 0x04 and data[1] == 0x62 \
                    and data[2] == (VOLT_DID >> 8) and data[3] == (VOLT_DID & 0xFF):
                v = round(data[4] * SCALE, 1)
                if 6.0 <= v <= 18.0:
                    return v, "radar 0x1006 [passive C-CAN]"
        return None, "no radar 0x1006 on bus (radar not being polled / asleep)"
    finally:
        s.close()


def read_voltage(module, do_bringup=True):
    """Wake `module` and return (volts_float_or_None, status_str). Closes the session after."""
    if do_bringup and not uds.bring_up_can(module.channel, BITRATE):
        return None, "could not bring up can0 @500k armed (sudo rights? adapter plugged?)"
    try:
        s = uds.open_socket(module.txid, module.rxid, module.channel, timeout=0.6)
    except OSError:
        # interface flapped (USB drop) -> wait for it back, then open
        s = uds.recover_socket(module.txid, module.rxid, module.channel, BITRATE)
    try:
        uds.request(s, [0x10, 0x03], timeout=1.0)            # extended diagnostic session
        resp, status = uds.request(s, [0x22, VOLT_DID >> 8, VOLT_DID & 0xFF],
                                   timeout=0.6, retries=2)
    except OSError as e:
        return None, f"bus error during read ({e})"
    finally:
        try:
            s.close()
        except OSError:
            pass
    if resp and resp[0] == 0x62 and len(resp) >= 4:          # 62 10 06 <byte>
        return round(resp[3] * SCALE, 1), "ok"
    return None, status if resp is None else f"unexpected reply {uds.hx(resp)}"


def append_csv(path, volts, status):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["iso_time", "volts", "status"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), volts, status])


def main():
    ap = argparse.ArgumentParser(description="Wake the ACC radar and read battery voltage.")
    ap.add_argument("--module", default="radar_acc", help="module key from lib/modules.py")
    ap.add_argument("--quiet", action="store_true", help="print just the number (or nothing on failure)")
    ap.add_argument("--csv", action="store_true", help=f"append a timestamped row to {CSV_PATH}")
    ap.add_argument("--csv-path", default=CSV_PATH, help="override the CSV log path")
    ap.add_argument("--warn", type=float, metavar="V", help="exit 2 if voltage is below this threshold")
    ap.add_argument("--no-bringup", action="store_true", help="don't (re)arm can0; assume it's 500k armed")
    args = ap.parse_args()

    module = get(args.module)
    volts, status = read_voltage(module, do_bringup=not args.no_bringup)

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
        print(f"{ts}  {module.key}  {volts:.1f} V{flag}")
    sys.exit(2 if low else 0)


if __name__ == "__main__":
    main()
