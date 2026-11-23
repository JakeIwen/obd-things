"""Read-only check for leftover link mutators in the broker's service cgroup."""

import os
from pathlib import Path

from projects.vehicle_data.usb_recovery_contract import (
    HELPER_MODULES,
    PROCESS_CHECK_ROUNDS,
    SERVICE_CGROUP_SUFFIX,
)
from projects.vehicle_data.usb_recovery_model import ProcessCheck

_PID_READ_ATTEMPTS = 3
_DEAD_STATES = {b"Z", b"X", b"x"}
_PROCESS_STATES = frozenset(bytes([state]) for state in b"RSDZTtXxKWPI")
_HELPER_ARGV = frozenset(module.encode("ascii") for module in HELPER_MODULES)


def _membership_file(proc_root: Path, cgroup_root: Path) -> Path:
    groups = [line[3:] for line in (proc_root / "self/cgroup").read_text().splitlines()
              if line.startswith("0::")]
    if len(groups) != 1:
        raise ValueError("expected one unified cgroup")
    group = groups[0]
    if (not group.startswith("/") or not group.endswith(SERVICE_CGROUP_SUFFIX)
            or any(part in ("", ".", "..") for part in group[1:].split("/"))):
        raise ValueError("not in the broker service cgroup")
    return cgroup_root / group[1:] / "cgroup.procs"


def _listed_pids(membership: Path, self_pid: int) -> list[int]:
    rows = membership.read_bytes().splitlines()
    if any(not row.isdigit() for row in rows):
        raise ValueError("invalid cgroup PID")
    pids = {int(row) for row in rows}
    if 0 in pids or self_pid not in pids:
        raise ValueError("cgroup listing does not include the broker or contains PID 0")
    return sorted(pids - {self_pid})


def _stat(process: Path) -> tuple[int, bytes]:
    # The kernel's parenthesized comm can contain spaces, newlines and ')'.
    prefix, opened, remainder = (process / "stat").read_bytes().partition(b" (")
    _, closed, tail = remainder.rpartition(b") ")
    fields = tail.split()
    if (not opened or not closed or prefix != process.name.encode("ascii")
            or len(fields) < 20 or fields[0] not in _PROCESS_STATES
            or not all(field.removeprefix(b"-").isdigit() for field in fields[1:19])
            or not fields[19].isdigit()):
        raise ValueError("invalid process stat")
    return int(fields[19]), fields[0]


def _uids(process: Path) -> tuple[int, ...]:
    lines = [line[4:].split() for line in (process / "status").read_bytes().splitlines()
             if line.startswith(b"Uid:")]
    if len(lines) != 1 or len(lines[0]) != 4 or not all(uid.isdigit() for uid in lines[0]):
        raise ValueError("invalid process Uids")
    return tuple(map(int, lines[0]))


def _offender_class(uids: tuple[int, ...], argv: list[bytes], program: bytes) -> str | None:
    if 0 in uids:
        return "root"
    if program == b"sudo":
        return "sudo"
    if _HELPER_ARGV.intersection(argv):
        return "helper"
    return None


def _inspect_pid(process: Path, start: int) -> tuple[int, str | None, str]:
    evidence = None
    comm = ""
    for attempt in range(_PID_READ_ATTEMPTS):
        if attempt:
            current, _ = _stat(process)
            if current != start:
                evidence = None
            start = current
        before = _uids(process)
        cmdline = (process / "cmdline").read_bytes()
        after = _uids(process)
        end, state = _stat(process)
        if start != end:
            continue
        if cmdline and (not cmdline.endswith(b"\0") or cmdline.startswith(b"\0")):
            raise ValueError("invalid process cmdline")
        argv = cmdline.split(b"\0")[:-1]
        program = argv[0].rsplit(b"/", 1)[-1] if argv else b""
        comm = "".join(ch if ch.isprintable() else "?"
                       for ch in program.decode("utf-8", errors="replace")[:32])
        evidence = _offender_class(before + after, argv, program) or evidence
        if before == after and (argv or state in _DEAD_STATES):
            return start, evidence, comm
    return start, "changing", comm


def inspect(
    *,
    proc_root: str | Path = "/proc",
    cgroup_root: str | Path = "/sys/fs/cgroup",
    self_pid: int | None = None,
    rounds: int = PROCESS_CHECK_ROUNDS,
) -> ProcessCheck:
    """Converge on inspected process identities, or return a non-quiet result."""
    proc_root = Path(proc_root)
    self_pid = os.getpid() if self_pid is None else self_pid
    seen: set[tuple[int, int]] = set()
    offenders: list[tuple[int, str, str]] = []
    try:
        membership = _membership_file(proc_root, Path(cgroup_root))
        for _ in range(rounds):
            converged = True
            for pid in _listed_pids(membership, self_pid):
                process = proc_root / str(pid)
                try:
                    start, _ = _stat(process)
                    if (pid, start) in seen:
                        continue
                    converged = False
                    start, kind, comm = _inspect_pid(process, start)
                except (FileNotFoundError, ProcessLookupError):
                    # Re-list after a vanished PID: it may have forked a child. A
                    # missing file in a surviving directory is not proof of exit.
                    try:
                        process.stat()
                    except FileNotFoundError:
                        converged = False
                        continue
                    raise
                seen.add((pid, start))
                if kind is not None:
                    offenders.append((pid, kind, comm))
            if converged:
                return ProcessCheck(quiet=not offenders, offenders=tuple(offenders))
    except (OSError, ValueError):
        return ProcessCheck(quiet=False, unverifiable=True, offenders=tuple(offenders))
    return ProcessCheck(quiet=False, unstable=True, offenders=tuple(offenders))
