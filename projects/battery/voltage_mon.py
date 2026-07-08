#!/usr/bin/env python3
"""Scheduled low-battery monitor: read system voltage from B-CAN and push an ntfy alert when low.

Source: the passive B-CAN frame 0x46C via bcan_voltage.read_with_wake() --
  * bus ASLEEP   -> briefly TX-wakes it (0x7FF burst), reads 0x46C, restores listen-only;
  * bus ALREADY AWAKE (fob/ignition/ENGINE RUNNING) -> never transmits, just reads what's on the wire.
No wlan0 / ignitionmon involvement (that was the retired ELM327-dongle path).

Alerts go to ntfy (free push, no account): edge-triggered when it first drops below WARN_V, a
throttled re-alert while it stays low, and one 'recovered' note on the way back up. Every message
is datestamped. Set NTFY_URL to override the topic.

CONNECTIVITY GATE: before touching the bus it checks the ntfy host is reachable -- if not, it SKIPS
without waking CAN (no point spending battery to wake the bus if the alert can't be delivered anyway).
--no-notify bypasses the gate so the read path can be tested offline.

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


CCAN_BITRATE = 500000   # if can0 is already here (manual radar/C-CAN work, or a drive logger) read the
                        # C-CAN voltage broadcast passively rather than reconfigure/hijack the bus.


def acquire():
    """Read voltage from whichever bus the PCAN is on. The iface BITRATE declares the bus (bringup.sh sets
    it; the monitor also leaves it on the bus it last read), and we honor that for the TX-wake decision so
    the wake burst NEVER fires on a bus declared C-CAN -- we must not inject onto the 500k powertrain bus.
      - @500k (C-CAN): read the C-CAN broadcast if it's live; NEVER wake. A parked/asleep C-CAN is dark
        (powertrain modules are ignition-switched, unpowered) so it has no readable voltage -- 'no reading'
        is the correct, safe outcome. If a live B-CAN is seen instead (rx-error spike @500k), read it @125k.
      - @125k or down (B-CAN, the deployed default): B-CAN wake-read (read live, or TX-wake a silent body
        bus). If that sees a live C-CAN mis-sampled at 125k ('not B-CAN'), read C-CAN passively @500k.
    So both LIVE buses are read regardless of which is connected, mismatches self-correct WITHOUT waking, and
    the only unread case is a parked C-CAN (nothing to read) -- keep the adapter on B-CAN for parked monitoring.
    When you move the adapter to C-CAN, bring it up @500k (bringup.sh) so the monitor won't try to wake it."""
    if cv.iface_bitrate() == CCAN_BITRATE:
        verdict, detail = cv.classify_bus()
        if verdict == "ccan":
            return cv.read_voltage()
        if verdict == "foreign":                       # live bus but not C-CAN @500k -> B-CAN mis-sampled
            bv.bring_up_passive()
            if bv.classify_bus()[0] == "bcan":
                return bv.read_voltage()
        return None, f"can0 @500k: no live C-CAN voltage ({detail}); not waking (a parked C-CAN is dark)"
    volts, status = bv.read_with_wake()                # declared B-CAN -> TX-wake is safe here
    if "not B-CAN" not in status:
        return volts, status
    if not cv.bring_up_passive():                      # B-CAN saw a live C-CAN @125k -> read it @500k
        return None, "detected C-CAN but could not bring up can0 @500k passive"
    verdict, detail = cv.classify_bus()
    return cv.read_voltage() if verdict == "ccan" else (None, f"bus unrecognized ({detail})")


def have_connectivity(url=NTFY_URL, timeout=6):
    """True if the ntfy host is reachable (DNS + TCP connect). No point waking the CAN bus (which draws
    battery) if we can't deliver the alert anyway. Probes the actual NTFY_URL host, so a custom/self-hosted
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

    # Gate on connectivity BEFORE acquire(): no point waking the bus (draws battery) if we can't alert.
    if allow_send and not have_connectivity():
        log("no internet (ntfy host unreachable) -- skipping; not waking the bus")
        return

    volts, status = acquire()                  # B-CAN wake-read, or C-CAN broadcast if can0 is already there
    bv.append_csv(bv.CSV_PATH, volts if volts is not None else "", status)
    if volts is None:
        log(f"voltage read FAILED: {status}")
        sys.exit(1)
    log(f"battery {volts:.2f} V  (status={status})")
    maybe_alert(volts, allow_send)
    sys.exit(2 if volts < WARN_V else 0)


if __name__ == "__main__":
    main()
