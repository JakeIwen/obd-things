#!/usr/bin/env python3
"""TPMS drive logger: poll the RF Hub and timestamp per-wheel dropouts to catch the
intermittent C1504/C1512 in the act.

Polls every CYCLE_S seconds over UDS (module 'rf_hub', C-CAN via the SGW-bypass tap):
  * 31D0-31D3  per-slot pressure, raw x 0.1 kPa  (slot->wheel map verified 2026-07-07
                by deflate test: 1=FL, 2=FR, 3=RR(physical), 4=RL(physical) -- the hub
                believes 3=RL/4=RR, i.e. its rear positions are MIRRORED; we log physical)
  * 301E-3021  per-slot last-RX records [04 | 3-byte timestamp | age] (trigger not yet
                fully characterized -- logged raw for offline analysis)
  * 19 02 0D   DTC status: a flip of testFailedThisOperationCycle on C1504/C1512/B1040
                pinpoints fault onset to the cycle

Appends CSV to tmp/tpms/tpms_drive_log.csv; prints changes to stdout. Read-only UDS
(22 / 19), no writes. Survives socket drops (uds.recover_socket) and ignition state
changes (the RFH answers on battery). Ctrl-C to stop.

    ./bringup.sh --tx                       # iface must be ARMED for UDS
    python3 projects/tpms/tpms_logger.py    # run for the duration of the drive

AUTO MODE (systemd service tpms-logger.service runs this):

    python3 projects/tpms/tpms_logger.py --auto

Battery-safe unattended operation for a van that is lived in: IDLE = pure-RX watch for
the ignition-only broadcast 0x2EF (2 s filtered listen every 30 s, transmits NOTHING, so
a parked/asleep bus stays asleep); 0x2EF present -> poll/log as above; 0x2EF gone
(ignition off) -> session ends within ~12 s and the bus is released to sleep. Gate is
0x2EF, NOT raw frame count, because our own diag polling holds network management awake.
Manages the iface itself (500k, armed). Stop before manual bus work:
sudo systemctl stop tpms-logger
"""
import os
import csv
import sys
import time
import socket
import struct
import argparse
import datetime
import subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from lib import uds
from lib.modules import get

CYCLE_S = 10
CSV_PATH = os.path.join(REPO, "tmp", "tpms", "tpms_drive_log.csv")
# physical wheel names for slots 1-4 (deflate-test map, NOT the hub's own belief)
WHEELS = ("FL", "FR", "RR", "RL")
PRESS_DIDS = (0x31D0, 0x31D1, 0x31D2, 0x31D3)
LASTRX_DIDS = (0x301E, 0x301F, 0x3020, 0x3021)
DTC_NAMES = {b"\x90\x40\x64": "B1040-64", b"\x55\x12\x88": "C1512-88",
             b"\x55\x10\x08": "C1504-08", b"\x55\x10\x07": "C1504-07"}


def read_did(s, did):
    r, _ = uds.request(s, [0x22, did >> 8, did & 0xFF], timeout=0.6, retries=1)
    return r[3:] if r and r[0] == 0x62 else None


def read_dtcs(s):
    """-> {'C1512-88': status_byte, ...} for DTCs reported by 19 02 0D."""
    r, _ = uds.request(s, [0x19, 0x02, 0x0D], timeout=0.8, retries=1)
    out = {}
    if r and r[0] == 0x59:
        for i in range(3, len(r) - 3, 4):
            dtc, status = bytes(r[i:i + 3]), r[i + 3]
            out[DTC_NAMES.get(dtc, dtc.hex().upper())] = status
    return out


def psi(raw):
    return round(int.from_bytes(raw, "big") * 0.1 * 0.145038, 1) if raw else None


def ensure_iface(channel="can0", bitrate=500000):
    """Make sure the iface is UP, at `bitrate`, and ARMED (not listen-only)."""
    sudo = [] if os.geteuid() == 0 else ["sudo"]
    show = subprocess.run(["ip", "-details", "link", "show", channel],
                          capture_output=True, text=True).stdout
    if f"bitrate {bitrate}" in show and "LISTEN-ONLY" not in show and ",UP" in show:
        return
    subprocess.run(sudo + ["ip", "link", "set", channel, "down"], check=False)
    subprocess.run(sudo + ["ip", "link", "set", channel, "up", "type", "can",
                           "bitrate", str(bitrate), "listen-only", "off"], check=True)
    print(f"iface {channel} (re)configured: {bitrate} armed", flush=True)


IGN_BCAST = 0x2EF   # broadcast only present with ignition ON (see ccan_voltage.py).
                    # Gating on it (not raw frame count) matters: our own diag polling
                    # holds FCA network management awake, so a frame-count gate would
                    # never see the bus go quiet and would drain the battery
                    # (verified 2026-07-07: polling stopped -> bus asleep in 60 s).


def ignition_on(channel="can0", window=2.0):
    """True if the ignition-only broadcast is on the wire. Pure RX -- never transmits."""
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        flt = struct.pack("=II", IGN_BCAST, 0x1FFFFFFF)
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
        s.bind((channel,))
        s.settimeout(window)
    except OSError:
        return False                   # iface missing/down; caller re-ensures
    try:
        s.recv(16)
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def log_session(auto=False):
    """Poll/log until Ctrl-C (manual) or until the bus goes quiet (auto). """
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    m = get("rf_hub")
    s = uds.open_socket(m.txid, m.rxid, m.channel, timeout=0.8)
    f = open(CSV_PATH, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(["time"] + [f"psi_{x}" for x in WHEELS]
                   + [f"lastrx_{x}" for x in WHEELS] + ["dtcs"])
    prev = None
    print(f"logging to {CSV_PATH} every {CYCLE_S}s", flush=True)
    try:
        while True:
            try:
                press = [read_did(s, d) for d in PRESS_DIDS]
                lastrx = [read_did(s, d) for d in LASTRX_DIDS]
                dtcs = read_dtcs(s)
            except OSError as e:
                print(f"! socket error ({e}); recovering", flush=True)
                try:
                    s.close()
                except Exception:
                    pass
                if auto:
                    ensure_iface(m.channel)
                s = uds.recover_socket(m.txid, m.rxid, m.channel)
                continue
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row = ([psi(p) for p in press]
                   + [x.hex() if x else "" for x in lastrx]
                   + [";".join(f"{k}={v:02X}" for k, v in sorted(dtcs.items()))])
            w.writerow([now] + row)
            f.flush()
            if row != prev:
                missing = [WHEELS[i] for i, p in enumerate(press) if p is None]
                tag = f"  << NO REPLY: {','.join(missing)}" if missing else ""
                print(f"{now}  psi={row[0:4]}  dtc={row[8]}{tag}", flush=True)
                prev = row
            if auto and not ignition_on(m.channel, 2.0):
                print("ignition off (0x2EF gone) -> session end, releasing bus to sleep",
                      flush=True)
                return
            time.sleep(CYCLE_S)
    finally:
        f.close()
        try:
            s.close()
        except Exception:
            pass


def auto_loop():
    """IDLE (pure-RX watch, no TX, lets the bus sleep) <-> logging sessions."""
    m = get("rf_hub")
    print("auto mode: watching for ignition (0x2EF), no TX while idle", flush=True)
    while True:
        ensure_iface(m.channel)
        if ignition_on(m.channel, 2.0):
            print("ignition on (0x2EF seen) -> logging session", flush=True)
            log_session(auto=True)
        else:
            time.sleep(28)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="unattended: log only while the bus is awake (systemd mode)")
    args = ap.parse_args()
    try:
        auto_loop() if args.auto else log_session()
    except KeyboardInterrupt:
        print("\nstopped")
