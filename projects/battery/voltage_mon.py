#!/usr/bin/env python3
"""Scheduled low-battery monitor: passively read system voltage and push an ntfy alert when low.

This scheduler-facing monitor is passive unless every autonomous-wake gate succeeds:
  * awake recognized buses are read without interface changes or transmission;
  * a silent bus may be woken only while holding the exclusive can0 diagnostics lock, with no
    same-boot external-operation inhibit and an explicit same-boot C-CAN/B-CAN topology record;
  * the interface and passive-silence conditions are rechecked under the lock immediately before TX;
  * it skips silent buses unless all wake gates pass, plus every unknown, wrong-rate, armed, down,
    and BUS-OFF interface;
  * CAN-CH/grey is never woken; it sends one grey-adapter notice and exits untouched.

The cooperative lock excludes participating Pi tools. External tools such as AlfaOBD must hold an
explicit campaign inhibit; controller diagnostic actions create it automatically and only an explicit
campaign-end removes it. Physical adapter changes must invalidate/set topology through
tools/can_operation_state.py. Missing, stale-boot, malformed, unknown, or CAN-CH topology fails closed.

Alerts go to ntfy (free push, no account): edge-triggered when it first drops below WARN_V, a
throttled re-alert while it stays low, and one 'recovered' note on the way back up. Every message
is datestamped. NTFY_VOLTAGE_URL sets the topic (defined in ~/secrets/.bash_variables, kept out of git).

CONNECTIVITY GATE: before opening a passive CAN socket it checks the ntfy host is reachable -- if not,
it skips. --no-notify bypasses the gate so the passive classification/read path can be tested offline.

    python3 projects/battery/voltage_mon.py             # one run (pushes ntfy if low)
    python3 projects/battery/voltage_mon.py --no-notify  # one run, never pushes (test the read path)

cron (installed alongside):
    0 10-22/2 * * *  timeout 90 python3 .../voltage_mon.py >> .../voltage_mon.log 2>&1
"""
import os
import sys
import json
import fcntl
import socket
import datetime
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bcan_voltage as bv            # sibling reader: read_with_wake, append_csv, CSV_PATH, _ROOT
import ccan_voltage as cv            # C-CAN voltage BROADCAST reader (0x2EF/0x41A); stdlib-only, no isotp
from lib import can_operation_state, diagnostic_safety

NTFY_VOLTAGE_URL = os.environ.get("NTFY_VOLTAGE_URL", "")  # topic in ~/secrets/.bash_variables (sourced by .bashrc + cron BASH_ENV); never hardcode
WARN_V    = 12.0                 # alert below this resting voltage (tune to taste)
HYST_V    = 0.3                  # must rise this far above WARN to count as "recovered"
REALERT_H = 12                   # while still low, re-push at most every this many hours
STATE = os.path.join(bv._ROOT, "tmp", "battery", "mon_state.json")
LOCK  = os.path.join(bv._ROOT, "tmp", "battery", "voltage_mon.lock")


def log(m):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {m}", flush=True)


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"low": False, "last_alert": None, "grey_connected": False}


def _save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(s, f)


def notify(msg, allow_send, title="Van battery"):
    """Push a datestamped message to ntfy. Datestamp is in the body per request."""
    stamped = f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {msg}"
    log("ALERT: " + stamped)
    if not allow_send:
        return
    try:
        subprocess.run(["curl", "-fsS", "-m", "20", "-H", f"Title: {title}",
                        "-d", stamped, NTFY_VOLTAGE_URL], capture_output=True, timeout=25)
    except subprocess.SubprocessError as e:
        log(f"ntfy send failed: {e}")


def maybe_alert(volts, allow_send):
    """Edge-triggered low alert + throttled re-alert; one 'recovered' note on the way back up."""
    st = _load_state()
    now = datetime.datetime.now()
    if volts < WARN_V:
        last = st.get("last_alert")
        due = last is None or (now - datetime.datetime.fromisoformat(last)).total_seconds() > REALERT_H * 3600
        if not st.get("low") or due:
            notify(f"Van battery LOW: {volts:.2f} V (below {WARN_V} V). Charge soon.", allow_send)
            st["last_alert"] = now.isoformat()
        st["low"] = True
    elif volts >= WARN_V + HYST_V:
        if st.get("low"):
            notify(f"Van battery recovered: {volts:.2f} V.", allow_send)
        st["low"] = False
        st["last_alert"] = None
    _save_state(st)


CCAN_BITRATE = 500000
BCAN_BITRATE = 125000
CAN_CH_STATUS = "CAN-CH confirmed: grey adapter connected"
CHANNEL = "can0"
PAIR_BY_BUS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}


def _passive_read(bus, bitrate, *, suffix):
    if bus == "c-can" and bitrate == CCAN_BITRATE:
        volts, status = cv.read_voltage(timeout=2.0)
        return volts, f"{status} [passive C-CAN; {suffix}]"
    if bus == "b-can" and bitrate == BCAN_BITRATE:
        volts, status = bv.read_voltage(timeout=2.0)
        return volts, f"{status} [passive B-CAN; {suffix}]"
    return None, f"bus/bitrate mismatch ({bus} at {bitrate}); skipped unchanged"


def _record_observed_topology(bus):
    try:
        can_operation_state.set_topology(
            CHANNEL,
            bus,
            pair=PAIR_BY_BUS[bus],
            source="voltage_mon_passive_signature",
            note="passively identified before any wake decision",
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        log(f"could not persist passively observed topology: {exc}")


def _interface_gate():
    bitrate = cv.iface_bitrate()
    if bitrate is None:
        return None, "can0 is down or bitrate is unavailable; skipped without interface changes"
    if not cv.canbus.is_listen_only():
        return None, "can0 is armed (listen-only off); skipped without touching an active operation"
    state = cv.canbus.controller_state()
    if state != "ERROR-ACTIVE":
        return None, f"can0 controller state is {state or 'unavailable'}; skipped without interface changes"
    if bitrate not in (CCAN_BITRATE, BCAN_BITRATE):
        return None, f"can0 is at unsupported bitrate {bitrate}; skipped without interface changes"
    return bitrate, ""


def _autonomous_wake(bitrate):
    lock_handle = None
    try:
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(CHANNEL)
        except diagnostic_safety.ChannelLockError:
            return None, "bus silent; another participating CAN operation holds can0"

        topology = can_operation_state.load_topology(CHANNEL)
        if topology.bus == "can-ch":
            return None, CAN_CH_STATUS

        # CAN-CH is a terminal no-TX status, so report it even while another
        # diagnostic campaign is inhibiting ordinary-bus wake. For every
        # wake-capable topology, the external-operation inhibit still wins.
        inhibits = can_operation_state.active_inhibits(CHANNEL)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            return None, f"bus silent; autonomous wake inhibited by {names}"

        if not topology.usable or topology.bus not in ("c-can", "b-can"):
            return None, f"bus silent; autonomous wake denied: {topology.reason}"

        expected_bitrate = (
            CCAN_BITRATE if topology.bus == "c-can" else BCAN_BITRATE
        )
        if bitrate != expected_bitrate:
            return None, (
                f"bus silent; topology {topology.bus} expects {expected_bitrate}, "
                f"interface is {bitrate}; refusing reconfiguration"
            )

        # Close the time-of-check/time-of-use window under the exclusive lock.
        checked_bitrate, failure = _interface_gate()
        if checked_bitrate is None:
            return None, failure
        bus = cv.canbus.identify_bus(probe=1.0)
        if bus in ("c-can", "b-can", "can-ch"):
            _record_observed_topology(bus)
            if bus == "can-ch":
                return None, CAN_CH_STATUS
            return _passive_read(
                bus, checked_bitrate, suffix="became active under wake lock"
            )
        if bus != "silent":
            return None, f"wake recheck found {bus}; refusing TX"

        try:
            if topology.bus == "c-can":
                woke = cv.canbus.poke_wake(
                    CHANNEL,
                    CCAN_BITRATE,
                    lock_handle=lock_handle,
                )
            else:
                woke = cv.canbus.tx_wake_burst(
                    CHANNEL,
                    BCAN_BITRATE,
                    lock_handle=lock_handle,
                )
        except cv.canbus.PassiveRestoreError as exc:
            return None, f"wake cleanup failed: {exc}"
        if not woke:
            return None, f"{topology.bus} autonomous wake produced no validated wake"

        verified = cv.canbus.identify_bus(probe=1.0)
        if verified != topology.bus:
            return None, (
                f"post-wake topology mismatch: expected {topology.bus}, got {verified}"
            )
        volts, status = _passive_read(
            verified,
            checked_bitrate,
            suffix=f"autonomous wake; topology source={topology.source}",
        )
        return volts, status
    finally:
        diagnostic_safety.release_channel_lock(lock_handle)


def acquire():
    """Read passively, or wake a silent known-safe topology behind every coordination gate."""
    bitrate, failure = _interface_gate()
    if bitrate is None:
        return None, failure

    bus = cv.canbus.identify_bus(probe=1.0)
    if bus == "can-ch":
        _record_observed_topology(bus)
        return None, CAN_CH_STATUS
    if bus in ("c-can", "b-can"):
        _record_observed_topology(bus)
        return _passive_read(bus, bitrate, suffix="interface unchanged")
    if bus == "silent":
        return _autonomous_wake(bitrate)
    return None, f"bus/topology not safely recognized ({bus} at {bitrate}); skipped unchanged"


def handle_grey_adapter(status, allow_send):
    """Edge-trigger one notice while a live CAN-CH signature remains connected."""
    st = _load_state()
    if status == CAN_CH_STATUS:
        if not st.get("grey_connected"):
            notify(
                "Grey adapter / CAN-CH is connected. Voltage monitor skipped CAN without "
                "reconfiguring the interface or transmitting.",
                allow_send,
                title="Van CAN monitor",
            )
        st["grey_connected"] = True
        _save_state(st)
        return True
    # Only a positively identified ordinary bus proves that grey is no longer selected.
    if "passive C-CAN" in status or "passive B-CAN" in status:
        if st.get("grey_connected"):
            st["grey_connected"] = False
            _save_state(st)
    return False


def have_connectivity(url=NTFY_VOLTAGE_URL, timeout=6):
    """True if the ntfy host is reachable (DNS + TCP connect). No point waking the CAN bus (which draws
    battery) if we can't deliver the alert anyway. Probes the actual NTFY_VOLTAGE_URL host, so a custom/self-hosted
    topic is tracked too."""
    try:
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, (u.port or (443 if u.scheme == "https" else 80))
        if not host:
            return False
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except (OSError, ValueError):        # ValueError: urlparse().port raises on a non-numeric port
        return False


def main():
    allow_send = not ("--no-notify" in sys.argv or "--no-sms" in sys.argv)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another voltage_mon instance is running; skipping this tick")
        return

    # Config gate: without a topic there's nothing to deliver to (fail loud, not as "no internet").
    if allow_send and not NTFY_VOLTAGE_URL:
        log("NTFY_VOLTAGE_URL unset (define it in ~/secrets/.bash_variables) -- skipping notify path")
        return

    # Gate on connectivity BEFORE acquire(): no point waking the bus (draws battery) if we can't alert.
    if allow_send and not have_connectivity():
        log("no internet (ntfy host unreachable) -- skipping; not waking the bus")
        return

    volts, status = acquire()
    bv.append_csv(bv.CSV_PATH, volts if volts is not None else "", status)
    if handle_grey_adapter(status, allow_send):
        log(f"{status}; battery read intentionally skipped")
        return
    if volts is None:
        log(f"voltage read FAILED: {status}")
        sys.exit(1)
    log(f"battery {volts:.2f} V  (status={status})")
    maybe_alert(volts, allow_send)
    sys.exit(2 if volts < WARN_V else 0)


if __name__ == "__main__":
    main()
