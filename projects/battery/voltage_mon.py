#!/usr/bin/env python3
"""Scheduled passive battery monitor using the serial-resolved C-CAN role.

Acquisition delegates to a running ``projects/vehicle_data`` broker when
present.  Otherwise it uses the same read-only role manager in-process.  Both
paths resolve the installed adapter by USB serial plus controller ``dev_id``,
hold shared logical-role and current-channel locks, require the exact passive
classical-CAN state, and never reconfigure or transmit.  A silent/asleep bus is
therefore an expected unavailable sample; this scheduled monitor does not wake
vehicle modules merely to obtain a voltage reading.

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
import pathlib
import socket
import datetime
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bcan_voltage as bv            # sibling module owns CSV path/root and append helper
from projects.vehicle_data.api import TelemetryClient
from projects.vehicle_data.broker import DEFAULT_SOCKET
from projects.vehicle_data.can_interfaces import PassiveInterfaceManager
from projects.vehicle_data.can_runtime import RoleAwareVoltageAcquirer

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
        return {"low": False, "last_alert": None}


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


BROKER_SOCKET = pathlib.Path(DEFAULT_SOCKET)
_VOLTAGE_ACQUIRER = RoleAwareVoltageAcquirer(PassiveInterfaceManager())


def _monitor_result(
    *,
    available,
    value,
    bus,
    acquisition,
    source,
    quality,
    detail,
):
    if not available:
        return None, detail
    if acquisition != "passive":
        return None, (
            "telemetry acquisition returned a non-passive result; "
            "scheduled voltage monitoring withheld it"
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "telemetry acquisition returned an invalid voltage value"
    bus_label = {"c-can": "C-CAN", "b-can": "B-CAN"}.get(
        bus, bus or "unknown bus"
    )
    return (
        value,
        f"{detail} [passive {bus_label}; source={source}; "
        f"quality={quality}]",
    )


def acquire():
    """Use an authoritative running broker, otherwise its shared acquirer in-process."""
    if BROKER_SOCKET.exists():
        if not BROKER_SOCKET.is_socket():
            return None, (
                f"telemetry broker path {BROKER_SOCKET} is not a Unix socket; "
                "direct CAN fallback withheld"
            )
        try:
            status_code, payload = TelemetryClient(
                str(BROKER_SOCKET), timeout=30.0
            ).request(
                "POST",
                "/v1/acquisitions/battery.voltage",
                {"mode": "passive"},
            )
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            return None, (
                f"telemetry broker unavailable ({exc}); "
                "direct CAN fallback withheld"
            )
        detail = str(
            payload.get("detail")
            or payload.get("reason")
            or f"broker returned HTTP {status_code}"
        )
        return _monitor_result(
            available=bool(payload.get("available")),
            value=payload.get("value"),
            bus=payload.get("bus"),
            acquisition=payload.get("acquisition"),
            source=payload.get("source"),
            quality=payload.get("quality"),
            detail=detail,
        )

    result = _VOLTAGE_ACQUIRER.acquire("passive")
    return _monitor_result(
        available=result.available,
        value=result.value,
        bus=result.bus,
        acquisition=result.acquisition,
        source=result.source,
        quality=result.quality,
        detail=result.detail,
    )


def have_connectivity(url=NTFY_VOLTAGE_URL, timeout=6):
    """True if the ntfy host is reachable (DNS + TCP connect). No point taking a passive sample
    if we can't deliver the alert anyway. Probes the actual NTFY_VOLTAGE_URL host, so a custom/self-hosted
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

    # Gate on connectivity before opening a passive CAN observer.
    if allow_send and not have_connectivity():
        log("no internet (ntfy host unreachable) -- skipping passive CAN read")
        return

    volts, status = acquire()
    bv.append_csv(bv.CSV_PATH, volts if volts is not None else "", status)
    if volts is None:
        log(f"voltage read FAILED: {status}")
        sys.exit(1)
    log(f"battery {volts:.2f} V  (status={status})")
    maybe_alert(volts, allow_send)
    sys.exit(2 if volts < WARN_V else 0)


if __name__ == "__main__":
    main()
