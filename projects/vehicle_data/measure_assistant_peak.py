"""Measure a warning-chat Codex turn's memory peak, read-only (deploy runbook, activation step).

Samples every 200 ms the processes below a unit's main PID (the advisor's Codex child and its
descendants, not the advisor itself) and reports the peak summed RSS, the peak summed ``VmHWM`` and
the lowest ``MemAvailable``. Start it, ask one warning-chat question on the dashboard, and it stops
a few seconds after the child has gone (or at ``--seconds``). It changes nothing.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from projects.vehicle_data.research_memory import MIB, mem_available_bytes, tree, usage_of

SAMPLE_SECONDS = 0.2
SETTLE_SECONDS = 5


def main_pid(unit):
    output = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                            capture_output=True, text=True, check=True).stdout.strip()
    pid = int(output or 0)
    if pid <= 0:
        raise SystemExit(f"{unit} is not running")
    return pid


def sample(pid, seconds, proc_root=Path("/proc"), meminfo=Path("/proc/meminfo"), clock=time.monotonic,
           sleep=time.sleep):
    peak_rss = peak_hwm = 0
    lowest = None
    seen_child = None
    deadline = clock() + seconds
    while clock() < deadline:
        children = [p for p in tree(pid, proc_root) if p != pid]
        rss, hwm = usage_of(children, proc_root)
        available = mem_available_bytes(meminfo)
        lowest = available if lowest is None else min(lowest, available)
        if children:
            seen_child = clock()
            peak_rss, peak_hwm = max(peak_rss, rss), max(peak_hwm, hwm)
        elif seen_child is not None and clock() - seen_child > SETTLE_SECONDS:
            break
        sleep(SAMPLE_SECONDS)
    return {"child_seen": seen_child is not None, "peak_tree_rss_mib": round(peak_rss / MIB, 1),
            "peak_tree_hwm_mib": round(peak_hwm / MIB, 1),
            "lowest_available_mib": None if lowest is None else round(lowest / MIB, 1)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", default="van-telemetry-advisor.service")
    parser.add_argument("--seconds", type=float, default=600)
    args = parser.parse_args(argv)
    result = sample(main_pid(args.unit), args.seconds)
    print(json.dumps(result, indent=1))
    return 0 if result["child_seen"] else 1


if __name__ == "__main__":
    sys.exit(main())
