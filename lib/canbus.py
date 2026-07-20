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

from lib import diagnostic_safety

DEFAULT_CHANNEL = "can0"


class PassiveRestoreError(RuntimeError):
    """An interface-changing helper could not prove that it returned CAN to listen-only mode."""


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


def controller_state(channel=DEFAULT_CHANNEL):
    """Return SocketCAN controller state (for example ERROR-ACTIVE or BUS-OFF), if available."""
    out = subprocess.run(
        ["ip", "-details", "link", "show", channel], capture_output=True, text=True
    ).stdout
    match = re.search(r"can state ([A-Z-]+)", out)
    return match.group(1) if match else None


def rx_errors(channel=DEFAULT_CHANNEL):
    out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True, text=True).stdout
    m = re.search(r"berr-counter\s+tx\s+\d+\s+rx\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def _passive_readback_matches(channel, bitrate):
    """Return whether one fresh ``ip -details`` readback proves the requested passive state.

    A successful configuration command is not sufficient: PCAN's listen-only setting is sticky,
    and an interface can also be left down, at the wrong rate, or BUS-OFF.  Require an
    administratively-UP link flag, the exact requested bitrate, an explicit LISTEN-ONLY CAN flag,
    and a reported controller state other than BUS-OFF.  Missing or unparseable fields fail closed.
    """
    result = subprocess.run(
        ["ip", "-details", "link", "show", channel], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    out = result.stdout

    link_flags = re.search(r"^\s*\d+:\s+[^\n]*<([^>\n]*)>", out, re.MULTILINE)
    if link_flags is None or "UP" not in link_flags.group(1).split(","):
        return False

    rate = re.search(r"\bbitrate\s+(\d+)\b", out)
    if rate is None or int(rate.group(1)) != bitrate:
        return False

    option_groups = re.findall(r"<([^>\n]*)>", out)
    if not any("LISTEN-ONLY" in group.split(",") for group in option_groups):
        return False

    state = re.search(r"\bcan(?:\s+<[^>\n]*>)?\s+state\s+([A-Z-]+)\b", out)
    return state is not None and state.group(1) != "BUS-OFF"


def bring_up_passive(channel, bitrate):
    """Configure and verify ``channel`` as passive at ``bitrate``.

    Returns True only when the configuration command succeeds and a fresh readback confirms the
    interface is UP at the exact bitrate, has listen-only ON, and is not BUS-OFF.  Any command,
    readback, or parsing failure returns False.
    """
    try:
        if not ip_up(channel, bitrate, listen_only=True):
            return False
        return _passive_readback_matches(channel, bitrate)
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
    leaves the iface verified passive @500k -- rouse it with wake(). All bitrate changes and
    probes are serialized under the active-diagnostics channel lock."""
    with diagnostic_safety.interrupt_on_termination() as termination:
        lock_handle = None
        restore_default = False
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(channel)
            # From this point onward an interrupted/failed probe may have changed the interface.
            restore_default = True
            for rate in (BITRATE_CCAN, BITRATE_BCAN):
                if not bring_up_passive(channel, rate):
                    continue
                bus = identify_bus(channel)
                if bus in ("c-can", "b-can"):
                    restore_default = False
                    return bus, rate

            # The second probe leaves a quiet interface at 125k. Honor the documented silent-bus
            # contract with a verified 500k passive restore before reporting the result.
            _require_passive_restore(channel, BITRATE_CCAN)
            restore_default = False
            return "silent", BITRATE_CCAN
        finally:
            termination.begin_cleanup()
            try:
                if lock_handle is not None and restore_default:
                    _require_passive_restore(channel, BITRATE_CCAN)
            finally:
                diagnostic_safety.release_channel_lock(lock_handle)


def restore_passive(channel=DEFAULT_CHANNEL, bitrate=BITRATE_CCAN):
    """Restore the passive default and return True only after verified passive-state readback.

    This has the same fail-closed contract as :func:`bring_up_passive`; callers can persist/report a
    failed cleanup instead of treating successful command submission as proof of safe restoration.
    """
    return bring_up_passive(channel, bitrate)


def _require_passive_restore(channel, bitrate):
    """Restore passive mode or surface the cleanup failure to the interface-changing caller."""
    if not restore_passive(channel, bitrate):
        raise PassiveRestoreError(
            f"could not verify {channel} passive at {bitrate} bit/s after CAN interface use"
        )


def _require_coordinated_passive_restore(channel, bitrate):
    """Take the channel lock and complete a termination-safe verified passive restore."""
    with diagnostic_safety.interrupt_on_termination() as termination:
        lock_handle = None
        restored = False
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(channel)
            termination.begin_cleanup()
            _require_passive_restore(channel, bitrate)
            restored = True
        finally:
            termination.begin_cleanup()
            try:
                if lock_handle is not None and not restored:
                    _require_passive_restore(channel, bitrate)
            finally:
                diagnostic_safety.release_channel_lock(lock_handle)


# --- waking a sleeping bus (ACTIVE -- callers gate these behind a bus check) ---
WAKE_ID = 0x7FF              # benign unused id for a broadcast wake burst (no module actuates on it)
WAKE_N, WAKE_GAP = 75, 0.02  # ~1.5s of activity trips wake-on-activity
RFH_MODULE = "rf_hub"       # KL30-always-awake C-CAN module (lib/modules.py); an addressed read wakes it
RFH_WAKE_DID = 0xF190       # benign identification read; any response = module awake = wake triggered


def tx_wake_burst(channel=DEFAULT_CHANNEL, bitrate=BITRATE_BCAN):
    """ACTIVE: arm the iface and TX a brief benign 0x7FF burst to wake a sleeping bus via wake-on-activity.
    Verified on the legacy observed 125-kbit/s body capture. The helper owns the per-channel
    diagnostic lock and restores verified listen-only mode before returning. It returns True only
    if at least one frame was accepted by the local CAN socket. (Does NOT wake C-CAN -- selective
    wake there; use poke_wake for C-CAN.) A cleanup failure raises PassiveRestoreError."""
    with diagnostic_safety.interrupt_on_termination() as termination:
        lock_handle = None
        sock = None
        sent = 0
        mutation_started = False
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(channel)
            # ``ip_up`` runs multiple commands; assume the interface may need cleanup as soon as
            # the call starts, even if it raises or returns False partway through.
            mutation_started = True
            try:
                armed = ip_up(channel, bitrate, listen_only=False, restart_ms=100)
            except Exception:
                armed = False
            if not armed or is_listen_only(channel):
                return False
            try:
                sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                sock.bind((channel,))
                frame = struct.pack("=IB3x8s", WAKE_ID, 0, b"")  # id=0x7FF, dlc=0
                for _ in range(WAKE_N):
                    try:
                        sock.send(frame)
                        sent += 1
                    except OSError:
                        # An unACKed sleeping bus can temporarily reject a send while restart-ms
                        # recovers the controller. Continue the bounded burst, but never report
                        # success if every send failed.
                        pass
                    time.sleep(WAKE_GAP)
            except OSError:
                return False
            return sent > 0
        finally:
            termination.begin_cleanup()
            try:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            finally:
                try:
                    if lock_handle is not None and mutation_started:
                        _require_passive_restore(channel, bitrate)
                finally:
                    diagnostic_safety.release_channel_lock(lock_handle)


def poke_wake(channel=DEFAULT_CHANNEL, bitrate=BITRATE_CCAN, module_key=RFH_MODULE, did=RFH_WAKE_DID):
    """ACTIVE: wake a bus by sending ONE addressed UDS read to an always-awake module (default rf_hub on
    C-CAN). The diag exchange trips the gateway's network-management wake -> full broadcast for ~15s.
    SELF-VALIDATING: returns True iff the module answered with the requested DID echo or a valid
    negative response for service 22 (= an ECU at the physical address completed the exchange).
    The helper owns the per-channel diagnostic lock and restores verified listen-only mode before
    returning. SIDE EFFECT on this van: also wakes the BCM -> accessory rails briefly power up
    (dashcam). A cleanup failure raises PassiveRestoreError. Lazy-imports isotp so this module stays
    stdlib-only unless a poke is actually requested."""
    from lib import uds                    # lazy: only poke_wake needs isotp
    from lib.modules import get
    m = get(module_key)
    payload = bytes((0x22, did >> 8, did & 0xFF))
    with diagnostic_safety.interrupt_on_termination() as termination:
        lock_handle = None
        sock = None
        mutation_started = False
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(channel)
            mutation_started = True
            try:
                armed = ip_up(channel, bitrate, listen_only=False, restart_ms=100)
            except Exception:
                armed = False
            if not armed or is_listen_only(channel):
                return False
            try:
                sock = uds.open_module_socket(m, channel=channel, timeout=1.0)
                uds.drain(sock)
                resp, _ = uds.request(sock, payload, timeout=1.5, retries=0)
            except OSError:
                return False
            if resp is None:
                return False
            response = bytes(resp)
            return (
                len(response) >= 3
                and (
                    response[:3] == bytes((0x62, did >> 8, did & 0xFF))
                    or response[:2] == bytes.fromhex("7F 22")
                )
            )
        finally:
            termination.begin_cleanup()
            try:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            finally:
                try:
                    if lock_handle is not None and mutation_started:
                        _require_passive_restore(channel, bitrate)
                finally:
                    diagnostic_safety.release_channel_lock(lock_handle)


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
        return "c-can", awake
    if tx_wake_burst(channel, BITRATE_BCAN):
        awake = identify_bus(channel) == "b-can"
        if awake:
            return "b-can", True
    _require_coordinated_passive_restore(channel, BITRATE_CCAN)
    return None, False
