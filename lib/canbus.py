"""Shared CAN-interface plumbing + bus detection/wake -- generic, module-agnostic (same role as lib/uds.py).
Used by the voltage readers and any project that needs to find and rouse whichever bus (B-CAN 125k /
C-CAN 500k) the PCAN is physically on.

  plumbing:  ip_up / bring_up_passive / iface_bitrate / rx_errors / probe_ids / append_csv
             (the STICKY listen-only flag is handled explicitly).
  identity:  identify_bus() -- which bus at the current bitrate; detect_bus() -- auto-try both bitrates.
             Signature id sets are bus facts sourced from docs/bus-map.md.
  wake:      tx_wake_burst() -- B-CAN 0x7FF broadcast burst (wake-on-activity);
             poke_wake()     -- C-CAN addressed UDS read to an always-awake module (rf_hub), self-validating;
             wake()          -- detect + wake, the "keep a parked bus awake" primitive.

Transmits ONLY in ip_up(listen_only=False) and the wake helpers -- callers gate those behind a bus check.
On this van a C-CAN wake also powers the BCM accessory rails briefly (dashcam) -- see docs/bus-map.md.
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


# --- bus identity (signature id sets are bus facts: docs/bus-map.md) ----------
BITRATE_CCAN = 500000        # C-CAN / HS-CAN powertrain bus
BITRATE_BCAN = 125000        # B-CAN body bus
# High-rate frames unique to each bus (present ignition-on AND in parked wakes). Source: docs/bus-map.md.
CCAN_SIG = {0x100, 0x101, 0x103, 0x104, 0x10F, 0x110, 0x116, 0x0EA, 0x0EE, 0x0FA, 0x0FE, 0x2EF, 0x41A}
BCAN_SIG = {0x46C, 0x0A0, 0x0E0, 0x2EA, 0x3DC, 0x3DE, 0x3E0, 0x3E2, 0x3E4, 0x3E6, 0x354, 0x356}
RX_ERR_ABORT = 200           # rx-error climb over a probe -> a bus sampled at the WRONG bitrate


def identify_bus(channel=DEFAULT_CHANNEL, probe=2.0):
    """PASSIVE (no TX): which physical bus is `channel` on, AT ITS CURRENT BITRATE? Returns one of
    'c-can' | 'b-can' | 'silent' | 'wrong-rate' | 'unknown'. Caller brings the iface up first.
      wrong-rate = traffic present but mis-sampled (rx-errors climb) -> the bus runs at the OTHER bitrate.
      silent     = no traffic (asleep) -- can't tell which bus passively; rouse it with wake()."""
    try:
        ids, rxd = probe_ids(channel, probe)
    except OSError:
        return "unknown"
    if ids & CCAN_SIG:
        return "c-can"
    if (ids & BCAN_SIG) or any((c & 0x1FFF0000) == 0x1E340000 for c in ids):   # 0x1E34xxxx = B-CAN NM
        return "b-can"
    if rxd > RX_ERR_ABORT:
        return "wrong-rate"
    return "silent" if not ids else "unknown"


def detect_bus(channel=DEFAULT_CHANNEL):
    """Auto-detect the connected bus: bring the iface up PASSIVE and probe at 500k (C-CAN) then 125k (B-CAN).
    Returns (bus, bitrate) with bus in 'c-can'/'b-can'/'silent'. 'silent' = both rates quiet (bus asleep);
    leaves the iface @500k -- rouse it with wake()."""
    for rate in (BITRATE_CCAN, BITRATE_BCAN):
        if not bring_up_passive(channel, rate):
            continue
        bus = identify_bus(channel)
        if bus in ("c-can", "b-can"):
            return bus, rate
    return "silent", BITRATE_CCAN


def restore_passive(channel=DEFAULT_CHANNEL, bitrate=BITRATE_CCAN):
    """Put the iface back to the safe passive default (listen-only ON) after an active wake."""
    bring_up_passive(channel, bitrate)


# --- waking a sleeping bus (ACTIVE -- callers gate these behind a bus check) ---
WAKE_ID = 0x7FF              # benign unused id for a broadcast wake burst (no module actuates on it)
WAKE_N, WAKE_GAP = 75, 0.02  # ~1.5s of activity trips wake-on-activity
RFH_MODULE = "rf_hub"       # KL30-always-awake C-CAN module (lib/modules.py); an addressed read wakes it
RFH_WAKE_DID = 0xF190       # benign identification read; any response = module awake = wake triggered


def tx_wake_burst(channel=DEFAULT_CHANNEL, bitrate=BITRATE_BCAN):
    """ACTIVE: arm the iface and TX a brief benign 0x7FF burst to wake a sleeping bus via wake-on-activity.
    Verified on B-CAN (~1.5s burst -> ~10s awake). Leaves the iface ARMED for an immediate read; the caller
    must restore_passive(). Returns True if it armed and sent. (Does NOT wake C-CAN -- selective wake there;
    use poke_wake for C-CAN.)"""
    try:
        if not ip_up(channel, bitrate, listen_only=False, restart_ms=100):
            return False
    except Exception:
        return False
    if is_listen_only(channel):            # sticky-flag guard: must be cleared or we can't TX
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


def poke_wake(channel=DEFAULT_CHANNEL, bitrate=BITRATE_CCAN, module_key=RFH_MODULE, did=RFH_WAKE_DID):
    """ACTIVE: wake a bus by sending ONE addressed UDS read to an always-awake module (default rf_hub on
    C-CAN). The diag exchange trips the gateway's network-management wake -> full broadcast for ~15s.
    SELF-VALIDATING: returns True iff the module answered (= we're on that module's bus, and it's waking).
    Leaves the iface ARMED; caller must restore_passive(). SIDE EFFECT on this van: also wakes the BCM ->
    accessory rails briefly power up (dashcam). Lazy-imports isotp so this module stays stdlib-only unless a
    poke is actually requested."""
    from lib import uds                    # lazy: only poke_wake needs isotp
    from lib.modules import get
    m = get(module_key)
    if not ip_up(channel, bitrate, listen_only=False, restart_ms=100):
        return False
    if is_listen_only(channel):
        return False
    try:
        s = uds.open_socket(m.txid, m.rxid, channel, timeout=1.0)
        resp, _ = uds.request(s, [0x22, did >> 8, did & 0xFF], timeout=1.5, retries=1)
        s.close()
    except OSError:
        return False
    return resp is not None                # any response (even a 7F negative) = exchange happened = wake


def wake(channel=DEFAULT_CHANNEL):
    """Detect the connected bus and WAKE it if silent -- the 'keep the bus awake' primitive. Returns
    (bus, awake): bus in 'c-can'/'b-can'/None, awake True if it's (now) broadcasting. Leaves the iface passive.
      * already awake -> (bus, True), no TX.
      * silent        -> try the self-validating C-CAN rf_hub poke (only wakes if we're on C-CAN), else the
                         B-CAN 0x7FF burst; whichever produces traffic is the connected bus.
    Call on a cadence to hold a parked bus awake. (For a tight loop, prefer detect_bus() ONCE then repeat the
    matching wake primitive -- wake() re-detects every call.) NOTE: each C-CAN wake blips accessory rails."""
    bus, _ = detect_bus(channel)
    if bus in ("c-can", "b-can"):
        return bus, True
    # silent: rouse-and-identify. C-CAN poke first (self-validating), then B-CAN burst.
    if poke_wake(channel, BITRATE_CCAN):
        awake = identify_bus(channel) == "c-can"
        restore_passive(channel, BITRATE_CCAN)
        return "c-can", awake
    if tx_wake_burst(channel, BITRATE_BCAN):
        awake = identify_bus(channel) == "b-can"
        restore_passive(channel, BITRATE_BCAN)
        if awake:
            return "b-can", True
    restore_passive(channel, BITRATE_CCAN)
    return None, False
