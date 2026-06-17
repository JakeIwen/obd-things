#!/usr/bin/env python3
"""Cron supervisor for hands-off drive logging.

Designed to be fired by cron every minute. Idempotent and lock-guarded, so overlapping/while-
running invocations are no-ops. Trigger is PASSIVE -- it listens for CAN traffic (silent bus =
asleep, busy = running) and never transmits when idle, so it cannot keep the vehicle awake or
drain the 12 V battery. When the bus is active and no logger is already running, it launches
`radar_acc_drive_log.py --quiet --stop-after-idle` writing to tmp/dumps/ (gitignored). The
logger exits on its own when the vehicle sleeps; the next cron tick relaunches on the next drive.

    # crontab -e  (run as the same user that can bring up can0):
    * * * * * /usr/bin/python3 /home/pi/dev/obd-things/projects/radar/auto_drive_logger.py >> /home/pi/dev/obd-things/tmp/auto_drive_logger.log 2>&1

Nothing here is interactive and nothing writes to the vehicle (logger is read-only 22/19/01).
"""
import os, sys, time, glob, fcntl, subprocess

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
ACTIVITY_TIMEOUT = 3        # s to wait for one CAN frame before calling the bus idle
STOP_AFTER_IDLE = 60        # logger self-exits after this many s of no radar response
RETAIN_DAYS = 90            # delete captures older than this
# One-shot raw-CAN burst (to identify the vehicle-speed broadcast frame). Only fires while the
# marker file exists; delete the marker once speed is decoded. Bounded so it can't fill the disk.
RAW_MARKER = os.path.join(REPO, "tmp", "CAPTURE_RAW")
RAW_DIR = os.path.join(REPO, "tmp", "canraw")
RAW_BURST_S = 240


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def iface_up():
    r = subprocess.run(["ip", "-br", "link", "show", CHANNEL], capture_output=True, text=True)
    return r.returncode == 0 and "UP" in r.stdout


def ensure_iface():
    """Bring can0 up (listen-only OFF) only if it is currently down -- never bounce a live link."""
    if iface_up():
        return True
    log("can0 down -> bringing up @500k listen-only off")
    return uds.bring_up_can(CHANNEL)


def bus_active():
    """Passive: return True iff at least one CAN frame arrives within ACTIVITY_TIMEOUT (RX only)."""
    r = subprocess.run(["timeout", str(ACTIVITY_TIMEOUT), "candump", "-n", "1", CHANNEL],
                       capture_output=True)
    return r.returncode == 0


def logger_alive():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)        # raises if not running
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def launch_logger():
    os.makedirs(OUT_DIR, exist_ok=True)
    logpath = os.path.join(TMP_DIR, "drive_logger.out")
    out = open(logpath, "a")
    p = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "radar_acc_drive_log.py"),
         "--quiet", "--out-dir", OUT_DIR, "--stop-after-idle", str(STOP_AFTER_IDLE)],
        stdout=out, stderr=subprocess.STDOUT, start_new_session=True, cwd=REPO)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    log(f"launched logger pid={p.pid} -> {OUT_DIR}")

    # one-shot raw-CAN burst for speed-frame identification (while marker present)
    if os.path.exists(RAW_MARKER):
        os.makedirs(RAW_DIR, exist_ok=True)
        raw = os.path.join(RAW_DIR, f"drive_{time.strftime('%Y%m%d_%H%M%S')}.log")
        rf = open(raw, "w")
        subprocess.Popen(["timeout", str(RAW_BURST_S), "candump", "-ta", CHANNEL],
                         stdout=rf, stderr=subprocess.STDOUT, start_new_session=True, cwd=REPO)
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
        if not ensure_iface():
            log("could not bring up can0; skipping")
            return
        if bus_active():
            launch_logger()
        # else: bus silent (vehicle asleep) -- do nothing, never TX
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
