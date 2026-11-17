"""Best-effort memory protection for research CLIs, not isolation.

This Pi's kernel runs with ``cgroup_disable=memory``, so no unit's ``MemoryMax`` is enforced and a
hard bound on a runaway CLI needs the memory controller (an owner decision). What the worker can do
is refuse to launch when ``MemAvailable`` is low, and SIGKILL the attempt's process group at once
when the tree grows past its cap or the Pi falls to critical. Thresholds come from the tracked
policy; until real peaks have been measured for the installed Claude copy, research turns are not
activated (``activation``).
"""

from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024


@dataclass(frozen=True)
class Thresholds:
    launch_floor_mib: int
    tree_cap_mib: int
    critical_mib: int


def thresholds(memory_policy, *, measured):
    """Measured values once activated, else the provisional ones used for the measurement itself."""
    values = memory_policy["measured_values"] if measured else memory_policy["provisional"]
    return Thresholds(int(values["launch_floor_mib"]), int(values["tree_cap_mib"]), int(values["critical_mib"]))


def activation(memory_policy, claude_digest):
    """``(activated, reason)``: measured, and measured against the Claude copy now installed."""
    if not memory_policy.get("measured"):
        return False, "research is not activated on this Pi until its memory use has been measured"
    if not claude_digest or memory_policy.get("cli_digest") != claude_digest:
        return False, "the installed Claude copy differs from the one measured; research needs re-measuring"
    return True, None


def mem_available_bytes(meminfo=Path("/proc/meminfo")):
    for line in Path(meminfo).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise ValueError("MemAvailable missing from /proc/meminfo")


def _children(pid, proc_root):
    found = []
    for task in (proc_root / str(pid) / "task").glob("*"):
        try:
            found.extend(int(child) for child in (task / "children").read_text().split())
        except (OSError, ValueError):
            continue
    return found


def _status_kib(pid, proc_root, field):
    try:
        for line in (proc_root / str(pid) / "status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def tree(pid, proc_root=Path("/proc")):
    """PIDs of ``pid`` and all its descendants that still exist."""
    seen, stack = [], [pid]
    while stack:
        current = stack.pop()
        if current in seen or not (proc_root / str(current)).exists():
            continue
        seen.append(current)
        stack.extend(_children(current, proc_root))
    return seen


def usage_of(pids, proc_root=Path("/proc")):
    """``(rss_bytes, hwm_bytes)`` summed over ``pids`` (``VmHWM`` sums overstate: conservative)."""
    rss = sum(_status_kib(p, proc_root, "VmRSS") for p in pids)
    hwm = sum(_status_kib(p, proc_root, "VmHWM") for p in pids)
    return rss * 1024, hwm * 1024


def tree_usage(pid, proc_root=Path("/proc")):
    return usage_of(tree(pid, proc_root), proc_root)


class Watchdog:
    """Called every tick of a running attempt; returns a stop reason or ``None``, tracks peaks."""

    def __init__(self, limits, pid=None, meminfo=Path("/proc/meminfo"), proc_root=Path("/proc")):
        self.limits, self.pid, self.meminfo, self.proc_root = limits, pid, meminfo, proc_root
        self.peak_rss = self.peak_hwm = 0
        self.lowest_available = None

    def check(self):
        if self.pid is None:
            return None
        rss, hwm = tree_usage(self.pid, self.proc_root)
        self.peak_rss, self.peak_hwm = max(self.peak_rss, rss), max(self.peak_hwm, hwm)
        available = mem_available_bytes(self.meminfo)
        self.lowest_available = available if self.lowest_available is None else min(self.lowest_available, available)
        if rss > self.limits.tree_cap_mib * MIB:
            return "the research process grew past its memory cap"
        if available < self.limits.critical_mib * MIB:
            return "the Pi fell to its critical free-memory level"
        return None

    def figures(self):
        return {"peak_tree_rss_mib": round(self.peak_rss / MIB, 1), "peak_tree_hwm_mib": round(self.peak_hwm / MIB, 1),
                "lowest_available_mib": None if self.lowest_available is None else round(self.lowest_available / MIB, 1)}


def launch_refusal(limits, meminfo=Path("/proc/meminfo")):
    """A sentence when ``MemAvailable`` is under the launch floor, else ``None``."""
    try:
        available = mem_available_bytes(meminfo)
    except (OSError, ValueError):
        return "the Pi's free memory could not be read"
    if available < limits.launch_floor_mib * MIB:
        return "the Pi is short of free memory right now"
    return None
