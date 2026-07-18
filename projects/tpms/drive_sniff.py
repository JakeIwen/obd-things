#!/usr/bin/env python3
"""PASSIVE drive sniffer — capture C-CAN broadcasts during drives to identify the frame
that carries per-wheel tire pressure (so the TPMS logger can eventually go zero-TX).

WHY: the IPC displays real per-wheel psi, so the RF Hub *must* broadcast pressures. Finding
that frame lets tpms_logger stop polling the hub over UDS — removing the only reason it
transmits, and with it any observer effect on the very fault we're trying to catch.

WHY A DRIVE (and not the parked capture we already have): parked pressures are static and form
only TWO levels (fronts ~55, rears ~75 psi), which cannot identify an offset-encoded field — any
affine map fits two levels. Driving warms each tire along its OWN curve (F 55->68, R 75->90 psi),
giving four independent signals to correlate against. Pair this capture with the ground truth in
tmp/tpms/tpms_drive_log.csv (same wall clock) and feed both to tools/can_field_finder.py /
tools/signal_correlate.py.

**PURE RX. This script NEVER transmits** — it only opens a raw CAN socket and reads, so it adds
no bus traffic and no observer effect of its own. It also never reconfigures the interface
(tpms-logger owns that); if the iface is down or at the wrong bitrate it just waits.

DECIMATION: keeps at most one frame per CAN ID per DECIM_S (default 2 s). Pressure is a slow
signal, so this loses nothing that matters while keeping a 90-minute drive around ~10 MB instead
of ~90 MB. Output is candump-compatible: "(epoch) can0 ID#HEX".

    python3 projects/tpms/drive_sniff.py --auto     # systemd mode: capture each drive
    python3 projects/tpms/drive_sniff.py            # capture now until Ctrl-C
"""
import os
import sys
import time
import errno
import socket
import struct
import argparse
import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(REPO, "tmp", "tpms", "captures")
CHANNEL = "can0"
IGN_BCAST = 0x2EF          # ignition-on gate; same one tpms_logger uses
DECIM_S = 2.0              # keep <=1 frame per CAN id per this many seconds
MIN_KEEP_MIN = 10          # discard a session shorter than this (not enough thermal rise)
CAP_BYTES = 400 * 1024**2  # stop starting NEW sessions once captures/ exceeds this


def open_rx(filter_id=None):
    """Raw CAN RX socket. Never sends."""
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    if filter_id is not None:
        s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                     struct.pack("=II", filter_id, 0x1FFFFFFF))
    s.bind((CHANNEL,))
    return s


def ignition_on(window=2.0):
    try:
        s = open_rx(IGN_BCAST)
        s.settimeout(window)
    except OSError:
        return False
    try:
        s.recv(16)
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


def dir_bytes():
    if not os.path.isdir(OUT_DIR):
        return 0
    return sum(os.path.getsize(os.path.join(OUT_DIR, f)) for f in os.listdir(OUT_DIR))


def capture(auto):
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUT_DIR, f"drive_{stamp}.log")
    try:
        s = open_rx()
        s.settimeout(1.0)
    except OSError as e:
        print(f"cannot open {CHANNEL} ({e}); waiting", flush=True)
        return
    last = {}                       # can_id -> last kept timestamp
    kept = seen = 0
    t0 = time.time()
    quiet_since = None
    print(f"capturing -> {path}", flush=True)
    with open(path, "w") as f:
        while True:
            try:
                frame = s.recv(16)
            except socket.timeout:
                frame = None
            except OSError as e:
                if e.errno in (errno.ENETDOWN, errno.ENODEV):
                    print("iface went down; ending session", flush=True)
                    break
                continue
            now = time.time()
            if frame:
                seen += 1
                cid = struct.unpack("<I", frame[:4])[0] & 0x1FFFFFFF
                dlc = frame[4]
                data = frame[8:8 + dlc]
                if now - last.get(cid, 0) >= DECIM_S:
                    last[cid] = now
                    f.write(f"({now:.6f}) {CHANNEL} {cid:03X}#{data.hex().upper()}\n")
                    kept += 1
                    if kept % 2000 == 0:
                        f.flush()
            if auto:
                # end the session when the ignition broadcast stops appearing
                if not frame:
                    quiet_since = quiet_since or now
                    if now - quiet_since > 8:
                        break
                elif cid == IGN_BCAST:
                    quiet_since = None
    s.close()
    mins = (time.time() - t0) / 60
    if auto and mins < MIN_KEEP_MIN:
        os.remove(path)
        print(f"session {mins:.1f} min < {MIN_KEEP_MIN} -> discarded", flush=True)
    else:
        print(f"session {mins:.1f} min: kept {kept} of {seen} frames -> {path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="wait for ignition, capture each drive")
    a = ap.parse_args()
    if not a.auto:
        capture(auto=False)
        return
    print("drive_sniff auto: waiting for ignition (0x2EF). PURE RX, never transmits.", flush=True)
    while True:
        if dir_bytes() > CAP_BYTES:
            print("capture dir over cap; sleeping (delete old captures to resume)", flush=True)
            time.sleep(600)
            continue
        if ignition_on(2.0):
            capture(auto=True)
        else:
            time.sleep(20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
