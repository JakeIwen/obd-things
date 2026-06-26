#!/usr/bin/env python3
"""Scheduled low-battery monitor: read system voltage from B-CAN and push an ntfy alert when low.

Source: the passive B-CAN frame 0x46C via bcan_voltage.read_with_wake() --
  * bus ASLEEP   -> briefly TX-wakes it (0x7FF burst), reads 0x46C, restores listen-only;
  * bus ALREADY AWAKE (fob/ignition/ENGINE RUNNING) -> never transmits, just reads what's on the wire.
No wlan0 / ignitionmon involvement (that was the retired ELM327-dongle path).

Alerts go to ntfy (free push, no account): edge-triggered when it first drops below WARN_V, a
throttled re-alert while it stays low, and one 'recovered' note on the way back up. Every message
is datestamped. Set NTFY_URL to override the topic.

    python3 projects/battery/voltage_mon.py             # one run (pushes ntfy if low)
    python3 projects/battery/voltage_mon.py --no-notify  # one run, never pushes (test the read path)

cron (installed alongside):
    0 10-22/2 * * *  timeout 90 python3 .../voltage_mon.py >> .../voltage_mon.log 2>&1
"""
import os
import sys
import json
import fcntl
import datetime
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bcan_voltage as bv            # sibling reader: read_with_wake, append_csv, CSV_PATH, _ROOT

NTFY_URL  = os.environ.get("NTFY_URL", "https://ntfy.sh/promaster_ncn")
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


def notify(msg, allow_send):
    """Push a datestamped message to ntfy. Datestamp is in the body per request."""
    stamped = f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {msg}"
    log("ALERT: " + stamped)
    if not allow_send:
        return
    try:
        subprocess.run(["curl", "-fsS", "-m", "20", "-H", "Title: Van battery",
                        "-d", stamped, NTFY_URL], capture_output=True, timeout=25)
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


def main():
    allow_send = not ("--no-notify" in sys.argv or "--no-sms" in sys.argv)
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another voltage_mon instance is running; skipping this tick")
        return

    volts, status = bv.read_with_wake()        # wakes the bus only if it's silent; never TX if awake
    bv.append_csv(bv.CSV_PATH, volts if volts is not None else "", status)
    if volts is None:
        log(f"voltage read FAILED: {status}")
        sys.exit(1)
    log(f"battery {volts:.2f} V  (status={status})")
    maybe_alert(volts, allow_send)
    sys.exit(2 if volts < WARN_V else 0)


if __name__ == "__main__":
    main()
