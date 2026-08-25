#!/usr/bin/env python3
"""Scheduled battery monitor using the broker's fixed B-CAN wake profile.

The local Unix broker first checks the permanent C-CAN and B-CAN roles
passively. Only when no already-awake voltage is available may it run the
fixed, role-owned B-CAN wake, validate 0x46C, restore the exact passive
baseline, and publish the sample. There is no direct active CAN fallback when
the authoritative broker is absent or unhealthy.

Alerts go to ntfy (free push, no account): edge-triggered when it first drops below WARN_V, a
throttled re-alert while it stays low, and one 'recovered' note on the way back up. Every message
is datestamped. NTFY_VOLTAGE_URL sets the topic (defined in ~/secrets/.bash_variables, kept out of git).

Connectivity gates notification delivery only. Sampling and CSV history
continue while ntfy is unreachable, and the alert edge remains pending.

    python3 projects/battery/voltage_mon.py             # one run (pushes ntfy if low)
    python3 projects/battery/voltage_mon.py --no-notify  # one run, never pushes (test the read path)
    python3 projects/battery/voltage_mon.py --no-notify --passive-only
                                                        # cache/web-safe; never wakes

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
    if acquisition not in ("passive", "wake_assisted"):
        return None, "telemetry acquisition returned an unapproved acquisition class"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "telemetry acquisition returned an invalid voltage value"
    bus_label = {"c-can": "C-CAN", "b-can": "B-CAN"}.get(
        bus, bus or "unknown bus"
    )
    return (
        value,
        f"{detail} [{acquisition.replace('_', '-')} {bus_label}; source={source}; "
        f"quality={quality}]",
    )


def acquire(*, passive_only=False):
    """Use only the authoritative local broker for wake-capable acquisition."""
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
                {"mode": "passive" if passive_only else "wake_if_asleep"},
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

    return None, (
        f"telemetry broker socket {BROKER_SOCKET} is absent; "
        "direct CAN fallback withheld"
    )


def have_connectivity(url=NTFY_VOLTAGE_URL, timeout=6):
    """True if the configured ntfy host is currently reachable."""
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

    notification_reachable = True
    if allow_send and not NTFY_VOLTAGE_URL:
        notification_reachable = False
        log("NTFY_VOLTAGE_URL unset -- sampling continues; notification withheld")
    elif allow_send and not have_connectivity():
        notification_reachable = False
        log("no internet (ntfy host unreachable) -- sampling continues; notification withheld")

    volts, status = acquire(passive_only="--passive-only" in sys.argv)
    bv.append_csv(bv.CSV_PATH, volts if volts is not None else "", status)
    if volts is None:
        log(f"voltage read FAILED: {status}")
        sys.exit(1)
    log(f"battery {volts:.2f} V  (status={status})")
    if not allow_send:
        log("notification disabled; alert state left unchanged")
    elif not notification_reachable:
        log("battery alert state retained until ntfy is reachable")
    else:
        maybe_alert(volts, True)
    sys.exit(2 if volts < WARN_V else 0)


if __name__ == "__main__":
    main()
