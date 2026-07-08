"""Shared CAN-interface plumbing for the passive voltage readers (bcan_voltage / ccan_voltage).

Generic and module-agnostic (same role as lib/uds.py): bring the iface up/down at a bitrate with the
STICKY listen-only flag handled explicitly, introspect bitrate / rx-errors, do a passive id-probe, and
append a reading to CSV. Bus-specific policy -- which ids mean which bus, and the verdict labels -- stays
in each reader's classify_bus, which calls probe_ids() here for the (identical) socket loop.

Nothing here transmits except ip_up(listen_only=False), which the callers gate behind their own bus checks.
"""
import os
import re
import csv
import time
import socket
import struct
import datetime
import subprocess

DEFAULT_CHANNEL = "can0"


def ip_up(channel, bitrate, listen_only, restart_ms=None):
    """down then up `channel` as CAN @bitrate. listen-only is set EXPLICITLY both ways because the flag
    is STICKY on PCAN/SocketCAN -- omitting it leaves the previous mode, which silently breaks TX (frames
    go nowhere). restart_ms>0 lets the controller auto-recover from a bus-off (an unACKed wake frame on a
    still-sleeping bus drives toward bus-off). Returns True on success."""
    if subprocess.run(["ip", "link", "show", channel], capture_output=True).returncode != 0:
        return False
    subprocess.run(["sudo", "ip", "link", "set", channel, "down"], capture_output=True)
    cmd = ["sudo", "ip", "link", "set", channel, "up", "type", "can", "bitrate", str(bitrate),
           "listen-only", "on" if listen_only else "off"]
    if restart_ms is not None:
        cmd += ["restart-ms", str(restart_ms)]
    r = subprocess.run(cmd, capture_output=True)
    time.sleep(0.3)
    return r.returncode == 0


def is_listen_only(channel=DEFAULT_CHANNEL):
    out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True, text=True).stdout
    return "<LISTEN-ONLY>" in out


def iface_bitrate(channel=DEFAULT_CHANNEL):
    """Current CAN bitrate of `channel` if it's UP, else None. Used to detect which bus the adapter is on
    (e.g. a live C-CAN 500k session we must not reconfigure)."""
    out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True, text=True).stdout
    if not re.search(r"state UP|UP,", out):
        return None
    m = re.search(r"bitrate (\d+)", out)
    return int(m.group(1)) if m else None


def rx_errors(channel=DEFAULT_CHANNEL):
    out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True, text=True).stdout
    m = re.search(r"berr-counter\s+tx\s+\d+\s+rx\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def bring_up_passive(channel, bitrate):
    """Ensure `channel` is UP @bitrate, listen-only ON (passive, never TX/ACK). Returns True on success."""
    try:
        return ip_up(channel, bitrate, listen_only=True)
    except Exception:
        return False


def probe_ids(channel=DEFAULT_CHANNEL, probe=2.0):
    """PASSIVE (never transmits): return (ids:set, rx_delta:int) -- the CAN ids seen in `probe` seconds and
    the rx-error climb over that window. Raises OSError if the socket can't be opened/bound (the caller maps
    that to its own verdict). The 11/29-bit split matches SocketCAN framing. Always closes the socket."""
    rx0 = rx_errors(channel)
    ids = set()
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        s.bind((channel,))
        deadline = time.time() + probe
        while time.time() < deadline:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                cid_raw = struct.unpack("=IB3x8s", s.recv(16))[0]
            except (socket.timeout, OSError):
                break
            cid = (cid_raw & 0x1FFFFFFF) if (cid_raw & 0x80000000) else (cid_raw & 0x7FF)
            ids.add(cid)
    finally:
        s.close()
    return ids, rx_errors(channel) - rx0


def append_csv(path, volts, status):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["iso_time", "volts", "status"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), volts, status])
