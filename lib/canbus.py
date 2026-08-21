"""Shared SocketCAN state, passive-readback, and bus-identification plumbing.

Used by passive readers to inspect already-configured serial-role interfaces.

  plumbing:  interface_state / ip_up / rx_errors / probe_ids / append_csv
             (the STICKY listen-only flag is handled explicitly).
  identity:  identify_bus() -- passive signature evidence at the current bitrate.
             Signature id sets are bus facts sourced from docs/bus-map.md.

Interface mutation remains available only through explicit-channel primitives;
role-bound callers supply the safety/ownership capability around them.
"""
import os
import re
import csv
import time
import socket
import struct
import datetime
import subprocess
from dataclasses import dataclass

CLASSICAL_CAN_MTU = 16
CAN_FD_MTU = 72


class PassiveRestoreError(RuntimeError):
    """An interface-changing helper could not prove that it returned CAN to listen-only mode."""


@dataclass(frozen=True)
class InterfaceState:
    """One atomic ``ip -details`` snapshot of a SocketCAN interface."""

    channel: str
    present: bool
    up: bool
    bitrate: int | None
    listen_only: bool
    controller_state: str | None
    restart_ms: int | None
    # Linux exposes classical CAN as MTU 16 and CAN FD as MTU 72.  ``None``
    # means the readback could not prove either mode and must fail closed in
    # code which may transmit or restore an interface.
    fd_enabled: bool | None = None

    def same_configuration(self, other):
        """Whether two snapshots prove the same safety-relevant link configuration."""
        return (
            isinstance(other, InterfaceState)
            and self.channel == other.channel
            and self.present == other.present
            and self.up == other.up
            and self.bitrate == other.bitrate
            and self.listen_only == other.listen_only
            and self.controller_state == other.controller_state
            and self.restart_ms == other.restart_ms
            and self.fd_enabled == other.fd_enabled
        )


def interface_state(channel):
    """Return a fail-closed interface snapshot from one command/readback.

    Unlike the older scalar helpers, this captures link flags, bitrate, listen-only,
    controller state, and restart timing from the same output. Callers that may mutate
    the interface can compare a later snapshot with :meth:`InterfaceState.same_configuration`.
    """
    result = subprocess.run(
        ["ip", "-details", "link", "show", channel],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return InterfaceState(channel, False, False, None, False, None, None, None)
    out = result.stdout
    link_flags = re.search(r"^\s*\d+:\s+[^\n]*<([^>\n]*)>", out, re.MULTILINE)
    flags = set(link_flags.group(1).split(",")) if link_flags else set()
    bitrate_match = re.search(r"\bbitrate\s+(\d+)\b", out)
    state_match = re.search(
        r"\bcan(?:\s+<[^>\n]*>)?\s+state\s+([A-Z-]+)\b", out
    )
    restart_match = re.search(r"\brestart-ms\s+(\d+)\b", out)
    option_groups = re.findall(r"<([^>\n]*)>", out)
    mtu_match = re.search(
        r"^\s*\d+:\s+[^\n]*\bmtu\s+(\d+)\b", out, re.MULTILINE
    )
    mtu = int(mtu_match.group(1)) if mtu_match else None
    can_options_match = re.search(r"\bcan\s+<([^>\n]*)>", out)
    can_options = (
        set(can_options_match.group(1).split(",")) if can_options_match else set()
    )
    fd_enabled = (
        True
        if mtu == CAN_FD_MTU or "FD" in can_options
        else False if mtu == CLASSICAL_CAN_MTU else None
    )
    return InterfaceState(
        channel=channel,
        present=True,
        up="UP" in flags,
        bitrate=int(bitrate_match.group(1)) if bitrate_match else None,
        listen_only=any(
            "LISTEN-ONLY" in group.split(",") for group in option_groups
        ),
        controller_state=state_match.group(1) if state_match else None,
        restart_ms=int(restart_match.group(1)) if restart_match else None,
        fd_enabled=fd_enabled,
    )


def ip_up(channel, bitrate, listen_only, restart_ms=None, *, noninteractive=False):
    """Down then bring up ``channel`` as classical CAN at ``bitrate``.

    CAN FD is explicitly disabled and listen-only is set explicitly both ways
    because controller modes may otherwise remain sticky across link changes.
    ``restart_ms>0`` enables bounded bus-off recovery. ``noninteractive`` adds
    ``sudo -n`` for service helpers. Returns True only when command submission
    succeeds; transmitting callers must also verify a fresh state readback.
    """
    if subprocess.run(["ip", "link", "show", channel], capture_output=True).returncode != 0:
        return False
    sudo = ["sudo", "-n"] if noninteractive else ["sudo"]
    subprocess.run(sudo + ["ip", "link", "set", channel, "down"], capture_output=True)
    cmd = sudo + [
        "ip",
        "link",
        "set",
        channel,
        "up",
        "type",
        "can",
        "bitrate",
        str(bitrate),
        "fd",
        "off",
        "listen-only",
        "on" if listen_only else "off",
    ]
    cmd += ["restart-ms", str(0 if restart_ms is None else restart_ms)]
    r = subprocess.run(cmd, capture_output=True)
    time.sleep(0.3)
    return r.returncode == 0


def rx_errors(channel):
    out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True, text=True).stdout
    m = re.search(r"berr-counter\s+tx\s+\d+\s+rx\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def probe_ids(channel, probe=2.0):
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
BITRATE_BCAN = 125000        # B-CAN body bus
# High-rate frames unique to each bus (present ignition-on AND in parked wakes). Source: docs/bus-map.md.
CCAN_SIG = {0x100, 0x101, 0x103, 0x104, 0x10F, 0x110, 0x116, 0x0EA, 0x0EE, 0x0FA, 0x0FE, 0x2EF, 0x41A}
BCAN_SIG = {0x46C, 0x0A0, 0x0E0, 0x2EA, 0x3DC, 0x3DE, 0x3E0, 0x3E2, 0x3E4, 0x3E6, 0x354, 0x356}
# CAN-CH shares some gateway-forwarded identifiers with ordinary C-CAN. These seven high-rate
# identifiers were all present on pins 12/13 and absent from the same campaign's pins-6/14
# reference capture. Require several of them together instead of treating one identifier as proof.
CANCH_SIG = {0x0DA, 0x0DC, 0x0F1, 0x106, 0x10E, 0x117, 0x1F6}
# A captured physical request or response for an installed grey-routed ECU is independently decisive.
CANCH_DIAG_SIG = {
    0x18DA28F1, 0x18DAF128,  # ABS
    0x18DA30F1, 0x18DAF130,  # EPS
    0x18DA31F1, 0x18DAF131,  # HALF
    0x18DAC0F1, 0x18DAF1C0,  # ORC
}
CANCH_MIN_BROADCAST_HITS = 3
RX_ERR_ABORT = 200           # rx-error climb over a probe -> a bus sampled at the WRONG bitrate


def identify_bus(channel, probe=2.0):
    """PASSIVE (no TX): which physical bus is `channel` on, AT ITS CURRENT BITRATE? Returns one of
    'c-can' | 'b-can' | 'can-ch' | 'silent' | 'wrong-rate' | 'unknown'. The caller must already
    own the serial-resolved role and prove its exact passive interface state.
      wrong-rate = traffic present but mis-sampled (rx-errors climb) -> the bus runs at the OTHER bitrate.
      silent     = no traffic (asleep) -- passive traffic cannot establish bus identity."""
    try:
        ids, rxd = probe_ids(channel, probe)
    except OSError:
        return "unknown"
    if ids & CANCH_DIAG_SIG or len(ids & CANCH_SIG) >= CANCH_MIN_BROADCAST_HITS:
        return "can-ch"
    if ids & CCAN_SIG:
        return "c-can"
    if (ids & BCAN_SIG) or any((c & 0x1FFF0000) == 0x1E340000 for c in ids):   # 0x1E34xxxx = B-CAN NM
        return "b-can"
    if rxd > RX_ERR_ABORT:
        return "wrong-rate"
    return "silent" if not ids else "unknown"


def restore_interface_state(state, *, noninteractive=False):
    """Restore and verify one previously captured UP classical-CAN configuration.

    This is intentionally narrower than a general network configurator. It accepts
    only an :class:`InterfaceState` that proves the adapter was present, UP, had
    a readable bitrate, and had CAN FD disabled. ``restart-ms`` is set explicitly
    (including zero) so an active helper cannot leave behind its temporary
    bus-off recovery setting.
    """
    if (
        not isinstance(state, InterfaceState)
        or not state.present
        or not state.up
        or state.bitrate is None
        or state.fd_enabled is not False
    ):
        return False
    kwargs = {
        "listen_only": state.listen_only,
        "restart_ms": state.restart_ms if state.restart_ms is not None else 0,
    }
    if noninteractive:
        kwargs["noninteractive"] = True
    if not ip_up(state.channel, state.bitrate, **kwargs):
        return False
    return state.same_configuration(interface_state(state.channel))
