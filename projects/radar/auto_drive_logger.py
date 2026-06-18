#!/usr/bin/env python3
"""Cron supervisor for hands-off drive logging.

Designed to be fired by cron every minute. Idempotent and lock-guarded, so overlapping/while-
running invocations are no-ops. Trigger is PASSIVE -- it listens for CAN traffic (silent bus =
asleep, busy = running) and never transmits when idle, so it cannot keep the vehicle awake or
drain the 12 V battery. When the bus is active and no logger is already running, it launches
`radar_acc_drive_log.py --quiet --stop-after-idle` writing to tmp/dumps/ (gitignored). The
logger exits on its own when the vehicle sleeps; the next cron tick relaunches on the next drive.

Bus-aware (bringup.sh is passive-by-default + supports the 125k B-CAN body bus):
  * Only operates on the radar's C-CAN (500k). If can0 is at another bitrate (e.g. 125k B-CAN),
    it SKIPS entirely -- never hijacks your body-bus work.
  * The logger must transmit (UDS reads), so on an active C-CAN drive it AUTO-ARMS can0
    (listen-only off) even if you left it passive; from a down iface it brings up C-CAN armed.
    (Trade-off accepted: it will TX during an active bus even if you'd set passive for AlfaOBD.)

    # crontab -e  (run as the same user that can bring up can0):
    * * * * * /usr/bin/python3 /home/pi/dev/obd-things/projects/radar/auto_drive_logger.py >> /home/pi/dev/obd-things/tmp/auto_drive_logger.log 2>&1

Nothing here is interactive and nothing writes to the vehicle (logger is read-only 22/19/01).
"""
import os, sys, time, glob, fcntl, subprocess, re

# locate repo root (dir containing lib/) regardless of how deep this script lives
REPO = os.path.dirname(os.path.abspath(__file__))
while REPO != os.path.dirname(REPO) and not os.path.isdir(os.path.join(REPO, "lib")):
    REPO = os.path.dirname(REPO)
sys.path.insert(0, REPO)
from lib import uds  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))   # this script's dir (projects/radar/)

CHANNEL = "can0"
OUT_DIR = os.path.join(REPO, "tmp", "dumps")
TMP_DIR = os.path.join(REPO, "tmp")
SUP_LOCK = os.path.join(TMP_DIR, "auto_drive_logger.lock")   # one supervisor at a time
PIDFILE = os.path.join(TMP_DIR, "drive_logger.pid")          # pid of the launched logger
RAW_PIDFILE = os.path.join(TMP_DIR, "raw_burst.pid")         # pid of the in-flight raw-CAN burst
ACTIVITY_TIMEOUT = 3        # s to wait for one CAN frame before calling the bus idle
STOP_AFTER_IDLE = 60        # logger self-exits after this many s of no radar response
RETAIN_DAYS = 90            # delete captures older than this
# One-shot raw-CAN burst (to identify the vehicle-speed broadcast frame). Only fires while the
# marker file exists; delete the marker once speed is decoded. Bounded so it can't fill the disk.
RAW_MARKER = os.path.join(REPO, "tmp", "CAPTURE_RAW")
RAW_DIR = os.path.join(REPO, "tmp", "canraw")
RAW_BURST_S = 240
# While this marker exists, log EVERY readable radar DID (did_hunt_log.py) instead of just the
# angle logger -- to find the DID that tracks vehicle speed. Remove once the speed DID is found.
HUNT_MARKER = os.path.join(REPO, "tmp", "HUNT_DIDS")


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


CCAN_BITRATE = 500000   # the radar lives on C-CAN 500k; B-CAN body bus is 125k (skip it)


def iface_details():
    r = subprocess.run(["ip", "-details", "link", "show", CHANNEL], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def iface_up(details):
    return "state UP" in details or "UP," in details   # ip -details header carries the UP flag


def iface_bitrate(details):
    m = re.search(r"bitrate (\d+)", details)
    return int(m.group(1)) if m else None


def iface_passive(details):
    return "<LISTEN-ONLY>" in details   # armed (listen-only off) -> token absent


def bus_active():
    """Passive: return True iff at least one CAN frame arrives within ACTIVITY_TIMEOUT (RX only)."""
    r = subprocess.run(["timeout", str(ACTIVITY_TIMEOUT), "candump", "-n", "1", CHANNEL],
                       capture_output=True)
    return r.returncode == 0


def _pid_alive(pidfile):
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)        # raises if not running
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def logger_alive():
    return _pid_alive(PIDFILE)


def launch_logger():
    os.makedirs(OUT_DIR, exist_ok=True)
    logpath = os.path.join(TMP_DIR, "drive_logger.out")
    out = open(logpath, "a")
    # while the hunt marker exists, log ALL readable DIDs (find the speed DID) instead of angles
    script = "did_hunt_log.py" if os.path.exists(HUNT_MARKER) else "radar_acc_drive_log.py"
    p = subprocess.Popen(
        [sys.executable, os.path.join(HERE, script),
         "--quiet", "--out-dir", OUT_DIR, "--stop-after-idle", str(STOP_AFTER_IDLE)],
        stdout=out, stderr=subprocess.STDOUT, start_new_session=True, cwd=REPO)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    log(f"launched {script} pid={p.pid} -> {OUT_DIR}")

    # raw-CAN burst for speed-frame identification (while marker present), one at a time
    if os.path.exists(RAW_MARKER) and not _pid_alive(RAW_PIDFILE):
        os.makedirs(RAW_DIR, exist_ok=True)
        raw = os.path.join(RAW_DIR, f"drive_{time.strftime('%Y%m%d_%H%M%S')}.log")
        rf = open(raw, "w")
        bp = subprocess.Popen(["timeout", str(RAW_BURST_S), "candump", "-ta", CHANNEL],
                              stdout=rf, stderr=subprocess.STDOUT, start_new_session=True, cwd=REPO)
        with open(RAW_PIDFILE, "w") as f:
            f.write(str(bp.pid))
        log(f"raw CAN burst ({RAW_BURST_S}s) -> {raw}")


def sweep_old():
    cutoff = time.time() - RETAIN_DAYS * 86400
    for path in glob.glob(os.path.join(OUT_DIR, "radar_acc_drive_*.csv")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                log(f"retention: removed {os.path.basename(path)}")
        except OSError:
            pass


def main():
    os.makedirs(TMP_DIR, exist_ok=True)
    lock = open(SUP_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another supervisor instance is mid-run; skip this tick silently

    try:
        if logger_alive():
            return  # a drive is already being recorded; do nothing
        sweep_old()

        d = iface_details()
        if not iface_up(d):                       # down (e.g. after reboot) -> bring up armed C-CAN
            log("can0 down -> bringing up C-CAN 500k armed")
            if not uds.bring_up_can(CHANNEL):
                log("could not bring up can0; skipping")
                return
            d = iface_details()

        br = iface_bitrate(d)
        if br is not None and br != CCAN_BITRATE:  # 125k B-CAN or other -> user's bus work; stay out
            log(f"can0 @ {br} != C-CAN {CCAN_BITRATE} (B-CAN / other bus work) -- skipping")
            return

        if not bus_active():
            return  # silent: parked / asleep -- never arm or TX

        # active C-CAN drive: the logger must TX, so ensure ARMED (listen-only off). Per the
        # chosen policy we auto-arm even if the user left it passive.
        if iface_passive(d):
            log("can0 passive -> arming (listen-only off) to log this drive")
            if not uds.bring_up_can(CHANNEL):
                log("could not arm can0; skipping")
                return

        launch_logger()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
