#!/usr/bin/env python3
"""Independent, cooperative, receive-only capture of three ProMaster buses.

One worker owns each logical role: ``c-can``, ``b-can``, and ``can-ch``.  A
worker resolves its adapter by USB serial plus ``dev_id``, acquires only that
role's shared logical-role and resolved-channel observer locks, revalidates the
exact lease, and starts a one-interface ``candump`` child.  A missing adapter,
down interface, child exit, or USB reset therefore restarts only that role;
healthy bus recorders keep their children, leases, and current chunks.

The tool has no interface-configuration capability.  It requires every role it
records to already be UP in the exact classical-CAN listen-only baseline.  It
never runs ``ip``, touches the spare channel, opens a transmitting CAN socket,
or changes another role as cleanup.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.vehicle_can_roles import CAN_BUS_ROLES
from projects.vehicle_data.can_interfaces import (
    PassiveInterfaceLease,
    PassiveInterfaceManager,
    PassiveInterfaceUnavailable,
)


DEFAULT_CAPTURE_ROOT = REPO / "tmp" / "captures" / "three_bus_drive"
DEFAULT_CHUNK_SECONDS = 3600
DEFAULT_RETRY_SECONDS = 2
DEFAULT_MAX_SESSION_SECONDS = 6 * 3600
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SESSION_SECONDS = 24 * 3600
MAX_CHUNK_SECONDS = 6 * 3600
MAX_RETRY_SECONDS = 300
DEFAULT_RCVBUF_BYTES = 16 * 1024 * 1024
CHILD_WAIT_POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 10.0
TERMINATE_GRACE_SECONDS = 5.0
SINGLETON_PATH = REPO / "tmp" / "locks" / "three-bus-capture.lock"
_DROP_NOTICE = re.compile(r"Dropped\s+(\d+)\s+CAN frame", re.IGNORECASE)
_INTERFACE_LOSS = re.compile(
    r"network is down|no such device|device.*disappear|interface.*down",
    re.IGNORECASE,
)


class CaptureError(RuntimeError):
    """A capture precondition or local recorder operation failed."""


@dataclass(frozen=True)
class CaptureConfig:
    capture_root: Path = DEFAULT_CAPTURE_ROOT
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS
    retry_seconds: int = DEFAULT_RETRY_SECONDS
    max_session_seconds: int = DEFAULT_MAX_SESSION_SECONDS
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    ignition_state: str = "unknown"
    wake_condition: str = "not asserted; receive-only observer"
    conditions: str = "unspecified"
    candump: str = "/usr/bin/candump"
    requested_rcvbuf_bytes: int = DEFAULT_RCVBUF_BYTES

    def validate(self) -> None:
        if not 1 <= self.chunk_seconds <= MAX_CHUNK_SECONDS:
            raise CaptureError(
                f"chunk seconds must be between 1 and {MAX_CHUNK_SECONDS}"
            )
        if not 1 <= self.retry_seconds <= MAX_RETRY_SECONDS:
            raise CaptureError(
                f"retry seconds must be between 1 and {MAX_RETRY_SECONDS}"
            )
        if not 1 <= self.max_session_seconds <= MAX_SESSION_SECONDS:
            raise CaptureError(
                f"maximum session must be between 1 and {MAX_SESSION_SECONDS} seconds"
            )
        if self.min_free_bytes < 0:
            raise CaptureError("minimum free bytes cannot be negative")
        if self.requested_rcvbuf_bytes < 0:
            raise CaptureError("receive buffer size cannot be negative")
        if not Path(self.candump).is_absolute():
            raise CaptureError("candump path must be absolute")


@dataclass(frozen=True)
class SessionPaths:
    root: Path
    session: Path
    events: Path
    summary: Path
    session_id: str
    role_dirs: Mapping[str, Path]


@dataclass(frozen=True)
class ChunkOutcome:
    role: str
    reason: str
    returncode: int | None
    byte_count: int
    chunk_path: Path
    dropped_frames: int
    interface_loss_indicated: bool


@dataclass
class RoleWorkerStats:
    role: str
    lease_attempts: int = 0
    chunks_started: int = 0
    chunks_finished: int = 0
    retryable_errors: int = 0
    bytes: int = 0
    dropped_frames: int = 0
    last_error: str | None = None
    finished: bool = False


class StopController:
    """Concurrency-safe global stop state and per-role child registry."""

    def __init__(self) -> None:
        self._event = threading.Event()
        # A Python signal handler can run on the main thread between any two
        # bytecodes, including while that thread reads stop metadata. RLock
        # prevents a same-thread signal reentry from deadlocking the latch.
        self._lock = threading.RLock()
        self._children: dict[str, object] = {}
        self._signal_number: int | None = None
        self._reason: str | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def signal_number(self) -> int | None:
        with self._lock:
            return self._signal_number

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def request(
        self,
        signum: int | None = None,
        _frame=None,
        *,
        reason: str | None = None,
    ) -> None:
        """Latch a global stop and interrupt every currently registered child."""

        with self._lock:
            first_request = not self._event.is_set()
            if self._signal_number is None and signum is not None:
                self._signal_number = signum
            if self._reason is None:
                self._reason = reason or (
                    "signal" if signum is not None else "requested"
                )
            self._event.set()
            children = tuple(self._children.values()) if first_request else ()
        for child in children:
            try:
                child.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass

    def register_child(self, role: str, child) -> None:
        with self._lock:
            if role in self._children:
                raise CaptureError(f"{role} already has a registered candump child")
            self._children[role] = child
            requested = self._event.is_set()
        if requested:
            try:
                child.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError):
                pass

    def clear_child(self, role: str, child) -> None:
        with self._lock:
            if self._children.get(role) is child:
                del self._children[role]

    def wait(self, timeout: float) -> bool:
        return self._event.wait(max(0.0, timeout))


class EventLog:
    """Concurrency-safe append-only JSONL log; child stderr remains separate."""

    def __init__(
        self,
        path: Path,
        *,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stderr=None,
    ) -> None:
        self.path = path
        self.wall_clock = wall_clock
        self.stderr = stderr if stderr is not None else sys.stderr
        self._lock = threading.Lock()

    def write(self, event: str, **fields: object) -> None:
        payload = {
            "time": self.wall_clock().astimezone(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            print(line, file=self.stderr, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def create_session(
    root: Path,
    *,
    wall_clock: Callable[[], datetime] = utc_now,
) -> SessionPaths:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    session_id = f"session_{_stamp(wall_clock())}_{os.getpid()}"
    session = root / session_id
    session.mkdir(mode=0o755)
    role_dirs = {}
    for role in CAN_BUS_ROLES:
        role_dir = session / role
        role_dir.mkdir(mode=0o755)
        role_dirs[role] = role_dir
    events = session / "events.jsonl"
    events.touch(mode=0o644)
    summary = session / "session.json"
    current_tmp = root / f".current_session.{os.getpid()}.tmp"
    current_tmp.write_text(str(session) + "\n", encoding="utf-8")
    os.replace(current_tmp, root / "current_session")
    return SessionPaths(
        root=root,
        session=session,
        events=events,
        summary=summary,
        session_id=session_id,
        role_dirs=role_dirs,
    )


@contextmanager
def singleton_lock(path: Path = SINGLETON_PATH) -> Iterator[None]:
    """Exclude only a duplicate recorder, never a different CAN role owner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError(f"another three-bus recorder holds {path}") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_epoch={time.time():.6f}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _lease_identity(lease: PassiveInterfaceLease) -> tuple[object, ...]:
    """Return identity local to one physical bus route.

    ``topology_generation`` deliberately is not part of this comparison.  It
    fingerprints the complete adapter topology, so unrelated churn on another
    role (or the spare channel) must not invalidate a healthy role's held
    lease.  The generation is still recorded as chunk provenance.
    """

    return (
        lease.role,
        lease.channel,
        lease.usb_serial,
        lease.dev_id,
        lease.bitrate,
        lease.pair,
    )


def revalidate_exact_lease(
    manager: PassiveInterfaceManager,
    lease: PassiveInterfaceLease,
) -> None:
    """Re-resolve and recheck one held lease immediately before child spawn."""

    with manager.observe(lease.role) as checked:
        if _lease_identity(checked) != _lease_identity(lease):
            raise CaptureError(
                f"{lease.role} changed while validating passive ownership"
            )


def read_rmem_max(path: Path = Path("/proc/sys/net/core/rmem_max")) -> int | None:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def selected_rcvbuf(requested: int, kernel_max: int | None) -> int | None:
    if requested <= 0 or kernel_max is None:
        return None
    return min(requested, kernel_max)


def candump_command(
    executable: str,
    lease: PassiveInterfaceLease,
    *,
    receive_buffer_bytes: int | None,
) -> list[str]:
    command = [executable, "-L", "-d"]
    if receive_buffer_bytes is not None:
        command.extend(("-r", str(receive_buffer_bytes)))
    command.append(lease.channel)
    return command


def available_bytes(
    path: Path,
    *,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> int:
    try:
        return int(disk_usage(path).free)
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise CaptureError(f"cannot determine free space at {path}: {exc}") from exc


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(
        path.name + f".{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def summarize_candump_stderr(path: Path) -> dict[str, object]:
    summary: dict[str, object] = {
        "bytes": 0,
        "lines": 0,
        "drop_notices": 0,
        "dropped_frames": 0,
        "interface_loss_indicated": False,
    }
    try:
        summary["bytes"] = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                summary["lines"] = int(summary["lines"]) + 1
                matches = _DROP_NOTICE.findall(line)
                if matches:
                    summary["drop_notices"] = int(summary["drop_notices"]) + len(
                        matches
                    )
                    summary["dropped_frames"] = int(
                        summary["dropped_frames"]
                    ) + sum(int(value) for value in matches)
                if _INTERFACE_LOSS.search(line):
                    summary["interface_loss_indicated"] = True
    except OSError as exc:
        summary["read_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def _stop_child(process) -> int | None:
    if process.poll() is not None:
        return process.returncode
    try:
        process.send_signal(signal.SIGINT)
    except (OSError, ProcessLookupError):
        pass
    try:
        return process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
    try:
        return process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        return process.wait(timeout=TERMINATE_GRACE_SECONDS)


def _wait_for_child(
    process,
    *,
    duration_seconds: float,
    stop: StopController,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, int | None]:
    """Wait for one child while keeping global stop latency bounded."""

    deadline = monotonic() + max(0.001, duration_seconds)
    while True:
        if stop.requested:
            return "stop-requested", _stop_child(process)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "chunk-rotate", _stop_child(process)
        try:
            returncode = process.wait(
                timeout=min(CHILD_WAIT_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            continue
        return (
            "stop-requested" if stop.requested else "process-exit",
            returncode,
        )


def run_role_chunk(
    *,
    config: CaptureConfig,
    paths: SessionPaths,
    chunk_number: int,
    lease: PassiveInterfaceLease,
    duration_seconds: float,
    stop: StopController,
    event_log: EventLog,
    process_factory: Callable[..., object] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = utc_now,
    kernel_rmem_max: int | None = None,
) -> ChunkOutcome:
    role = lease.role
    started = wall_clock()
    stamp = _stamp(started)
    chunk_id = f"{chunk_number:04d}"
    role_dir = paths.role_dirs[role]
    prefix = f"chunk_{chunk_id}_{stamp}"
    chunk_path = role_dir / f"{prefix}.candump"
    stderr_path = role_dir / f"{prefix}.stderr.log"
    metadata_path = role_dir / f"{prefix}.metadata.json"
    rcvbuf = selected_rcvbuf(config.requested_rcvbuf_bytes, kernel_rmem_max)
    command = candump_command(
        config.candump,
        lease,
        receive_buffer_bytes=rcvbuf,
    )
    metadata: dict[str, object] = {
        "schema": "obd-things.independent-bus-capture.v2",
        "session": paths.session_id,
        "role": role,
        "chunk": chunk_number,
        "start_time": started.astimezone(timezone.utc).isoformat(),
        "end_time": None,
        "status": "running",
        "mode": "receive_only_classical_can",
        "configures_interfaces": False,
        "transmits_can_frames": False,
        "ignition_state": config.ignition_state,
        "wake_condition": config.wake_condition,
        "conditions": config.conditions,
        "route": lease.as_dict(),
        "candump_command": command,
        "candump_stderr": stderr_path.name,
        "requested_receive_buffer_bytes": config.requested_rcvbuf_bytes,
        "kernel_rmem_max": kernel_rmem_max,
        "selected_receive_buffer_bytes": rcvbuf,
        "planned_duration_seconds": duration_seconds,
        "loss_reporting": {
            "candump_drop_monitor": True,
            "summary": None,
        },
    }
    _write_json(metadata_path, metadata)
    event_log.write(
        "role-chunk-start",
        role=role,
        chunk=chunk_number,
        channel=lease.channel,
        topology_generation=lease.topology_generation,
        path=str(chunk_path),
    )

    process = None
    returncode: int | None = None
    reason = "process-exit"
    try:
        with (
            chunk_path.open("wb") as capture_handle,
            stderr_path.open("wb") as stderr_handle,
        ):
            try:
                process = process_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=capture_handle,
                    stderr=stderr_handle,
                    close_fds=True,
                )
            except OSError as exc:
                reason = "spawn-error"
                metadata["error"] = f"{type(exc).__name__}: {exc}"
            else:
                registered = False
                try:
                    stop.register_child(role, process)
                    registered = True
                    reason, returncode = _wait_for_child(
                        process,
                        duration_seconds=duration_seconds,
                        stop=stop,
                        monotonic=monotonic,
                    )
                finally:
                    if registered:
                        stop.clear_child(role, process)
                    if process.poll() is None:
                        returncode = _stop_child(process)
    finally:
        ended = wall_clock()
        try:
            byte_count = chunk_path.stat().st_size
        except OSError:
            byte_count = 0
        stderr_summary = summarize_candump_stderr(stderr_path)
        metadata.update(
            end_time=ended.astimezone(timezone.utc).isoformat(),
            status=reason,
            candump_returncode=returncode,
            bytes=byte_count,
        )
        metadata["loss_reporting"] = {
            "candump_drop_monitor": True,
            "summary": stderr_summary,
        }
        _write_json(metadata_path, metadata)
        event_log.write(
            "role-chunk-end",
            role=role,
            chunk=chunk_number,
            reason=reason,
            returncode=returncode,
            bytes=byte_count,
            dropped_frames=stderr_summary["dropped_frames"],
            interface_loss_indicated=stderr_summary[
                "interface_loss_indicated"
            ],
        )
    return ChunkOutcome(
        role=role,
        reason=reason,
        returncode=returncode,
        byte_count=byte_count,
        chunk_path=chunk_path,
        dropped_frames=int(stderr_summary["dropped_frames"]),
        interface_loss_indicated=bool(
            stderr_summary["interface_loss_indicated"]
        ),
    )


def run_role_worker(
    role: str,
    *,
    config: CaptureConfig,
    paths: SessionPaths,
    manager: PassiveInterfaceManager,
    stop: StopController,
    stats: RoleWorkerStats,
    session_start: float,
    event_log: EventLog,
    process_factory: Callable[..., object] = subprocess.Popen,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = utc_now,
    kernel_rmem_max: int | None = None,
) -> None:
    """Record one role independently until global stop or the session bound."""

    event_log.write("role-worker-start", role=role)
    try:
        while not stop.requested:
            elapsed = max(0.0, monotonic() - session_start)
            remaining = config.max_session_seconds - elapsed
            if remaining <= 0:
                stop.request(reason="max-session")
                break
            try:
                free = available_bytes(paths.root, disk_usage=disk_usage)
            except CaptureError as exc:
                stats.last_error = str(exc)
                event_log.write(
                    "global-disk-check-failed", role=role, detail=str(exc)
                )
                stop.request(reason="disk-check-failed")
                break
            if free < config.min_free_bytes:
                event_log.write(
                    "global-low-disk",
                    role=role,
                    available_bytes=free,
                    required_bytes=config.min_free_bytes,
                )
                stop.request(reason="low-disk")
                break

            outcome = None
            try:
                stats.lease_attempts += 1
                with manager.observe(role) as lease:
                    revalidate_exact_lease(manager, lease)
                    stats.chunks_started += 1
                    outcome = run_role_chunk(
                        config=config,
                        paths=paths,
                        chunk_number=stats.chunks_started,
                        lease=lease,
                        duration_seconds=min(float(config.chunk_seconds), remaining),
                        stop=stop,
                        event_log=event_log,
                        process_factory=process_factory,
                        monotonic=monotonic,
                        wall_clock=wall_clock,
                        kernel_rmem_max=kernel_rmem_max,
                    )
                    stats.chunks_finished += 1
                    stats.bytes += outcome.byte_count
                    stats.dropped_frames += outcome.dropped_frames
            except (PassiveInterfaceUnavailable, CaptureError, OSError) as exc:
                stats.retryable_errors += 1
                stats.last_error = f"{type(exc).__name__}: {exc}"
                event_log.write(
                    "role-retry",
                    role=role,
                    exception=type(exc).__name__,
                    detail=str(exc),
                )
            except Exception as exc:  # fail contained to this physical role
                stats.retryable_errors += 1
                stats.last_error = f"{type(exc).__name__}: {exc}"
                event_log.write(
                    "role-worker-error",
                    role=role,
                    exception=type(exc).__name__,
                    detail=str(exc),
                )

            if stop.requested:
                break
            # Normal rotation immediately reacquires this role. Adapter/child
            # failure and unavailable passive state use a bounded retry delay.
            if outcome is not None and outcome.reason == "chunk-rotate":
                continue
            remaining = config.max_session_seconds - max(
                0.0, monotonic() - session_start
            )
            if remaining <= 0:
                stop.request(reason="max-session")
                break
            stop.wait(min(float(config.retry_seconds), remaining))
    finally:
        stats.finished = True
        event_log.write(
            "role-worker-stop",
            role=role,
            chunks_started=stats.chunks_started,
            chunks_finished=stats.chunks_finished,
            retryable_errors=stats.retryable_errors,
            bytes=stats.bytes,
            dropped_frames=stats.dropped_frames,
        )


def run_role_worker_guarded(**kwargs) -> None:
    """Turn an unexpected worker failure into one explicit global fault."""

    role = str(kwargs["role"])
    stop: StopController = kwargs["stop"]
    stats: RoleWorkerStats = kwargs["stats"]
    event_log: EventLog = kwargs["event_log"]
    try:
        run_role_worker(**kwargs)
    except Exception as exc:
        stats.finished = True
        stats.last_error = f"{type(exc).__name__}: {exc}"
        try:
            event_log.write(
                "role-worker-fatal",
                role=role,
                exception=type(exc).__name__,
                detail=str(exc),
            )
        except Exception:
            pass
        stop.request(reason="worker-fatal")


def _session_payload(
    *,
    config: CaptureConfig,
    paths: SessionPaths,
    stats: Mapping[str, RoleWorkerStats],
    started_at: datetime,
    ended_at: datetime | None,
    stop: StopController,
) -> dict[str, object]:
    return {
        "schema": "obd-things.independent-three-bus-session.v2",
        "session": paths.session_id,
        "start_time": started_at.astimezone(timezone.utc).isoformat(),
        "end_time": (
            ended_at.astimezone(timezone.utc).isoformat()
            if ended_at is not None
            else None
        ),
        "mode": "three_independent_receive_only_workers",
        "configures_interfaces": False,
        "transmits_can_frames": False,
        "roles": list(CAN_BUS_ROLES),
        "stop_reason": stop.reason,
        "signal": stop.signal_number,
        "config": {
            "chunk_seconds": config.chunk_seconds,
            "retry_seconds": config.retry_seconds,
            "max_session_seconds": config.max_session_seconds,
            "min_free_bytes": config.min_free_bytes,
            "ignition_state": config.ignition_state,
            "wake_condition": config.wake_condition,
            "conditions": config.conditions,
            "requested_rcvbuf_bytes": config.requested_rcvbuf_bytes,
        },
        "role_stats": {role: asdict(stats[role]) for role in CAN_BUS_ROLES},
    }


def run_supervisor(
    config: CaptureConfig,
    *,
    manager: PassiveInterfaceManager | None = None,
    stop: StopController | None = None,
    process_factory: Callable[..., object] = subprocess.Popen,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = utc_now,
    kernel_rmem_max_reader: Callable[[], int | None] = read_rmem_max,
) -> int:
    config.validate()
    manager = manager or PassiveInterfaceManager()
    stop = stop or StopController()
    try:
        paths = create_session(config.capture_root, wall_clock=wall_clock)
    except OSError as exc:
        raise CaptureError(f"cannot create capture session: {exc}") from exc
    events = EventLog(paths.events, wall_clock=wall_clock)
    session_start = monotonic()
    started_at = wall_clock()
    kernel_rmem_max = kernel_rmem_max_reader()
    stats = {role: RoleWorkerStats(role=role) for role in CAN_BUS_ROLES}
    try:
        _write_json(
            paths.summary,
            _session_payload(
                config=config,
                paths=paths,
                stats=stats,
                started_at=started_at,
                ended_at=None,
                stop=stop,
            ),
        )
        events.write(
            "supervisor-start",
            session=paths.session_id,
            worker_model="independent_per_role",
            max_session_seconds=config.max_session_seconds,
            chunk_seconds=config.chunk_seconds,
            min_free_bytes=config.min_free_bytes,
            kernel_rmem_max=kernel_rmem_max,
        )
    except OSError as exc:
        raise CaptureError(f"cannot initialize capture metadata: {exc}") from exc

    threads = []
    try:
        for role in CAN_BUS_ROLES:
            thread = threading.Thread(
                name=f"three-bus-capture-{role}",
                target=run_role_worker_guarded,
                kwargs={
                    "role": role,
                    "config": config,
                    "paths": paths,
                    "manager": manager,
                    "stop": stop,
                    "stats": stats[role],
                    "session_start": session_start,
                    "event_log": events,
                    "process_factory": process_factory,
                    "disk_usage": disk_usage,
                    "monotonic": monotonic,
                    "wall_clock": wall_clock,
                    "kernel_rmem_max": kernel_rmem_max,
                },
            )
            thread.start()
            threads.append(thread)
    except Exception as exc:
        stop.request(reason="worker-start-failed")
        for thread in threads:
            thread.join()
        raise CaptureError(f"cannot start per-role recorder workers: {exc}") from exc

    while True:
        alive = [thread for thread in threads if thread.is_alive()]
        if not alive:
            break
        if (
            not stop.requested
            and monotonic() - session_start >= config.max_session_seconds
        ):
            stop.request(reason="max-session")
        for thread in alive:
            thread.join(timeout=0.1)

    if not stop.requested:
        stop.request(reason="workers-finished")
    ended_at = wall_clock()
    try:
        events.write(
            "supervisor-stop",
            reason=stop.reason,
            signal=stop.signal_number,
            role_stats={role: asdict(stats[role]) for role in CAN_BUS_ROLES},
        )
        _write_json(
            paths.summary,
            _session_payload(
                config=config,
                paths=paths,
                stats=stats,
                started_at=started_at,
                ended_at=ended_at,
                stop=stop,
            ),
        )
    except OSError as exc:
        raise CaptureError(f"cannot finalize capture metadata: {exc}") from exc
    return (
        1
        if stop.reason
        in (
            "disk-check-failed",
            "worker-start-failed",
            "worker-fatal",
            "workers-finished",
        )
        else 0
    )


@contextmanager
def installed_signal_handlers(stop: StopController) -> Iterator[None]:
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, stop.request)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _env(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    argument_parser.add_argument(
        "--check",
        "--resolve-only",
        dest="check",
        action="store_true",
        help="independently report each role's passive lease; start no candump",
    )
    argument_parser.add_argument(
        "--capture-root",
        type=Path,
        default=Path(_env("CAN_CAPTURE_ROOT", str(DEFAULT_CAPTURE_ROOT))),
    )
    argument_parser.add_argument(
        "--chunk-seconds",
        type=int,
        default=_env("CAN_CAPTURE_CHUNK_SECONDS", str(DEFAULT_CHUNK_SECONDS)),
    )
    argument_parser.add_argument(
        "--retry-seconds",
        type=int,
        default=_env("CAN_CAPTURE_WAIT_SECONDS", str(DEFAULT_RETRY_SECONDS)),
    )
    argument_parser.add_argument(
        "--max-session-seconds",
        type=int,
        default=_env(
            "CAN_CAPTURE_MAX_SESSION_SECONDS", str(DEFAULT_MAX_SESSION_SECONDS)
        ),
    )
    argument_parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=_env("CAN_CAPTURE_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES)),
    )
    argument_parser.add_argument(
        "--ignition-state",
        choices=("off", "on", "engine-running", "unknown"),
        default="unknown",
    )
    argument_parser.add_argument(
        "--wake-condition",
        default="not asserted; receive-only observer",
    )
    argument_parser.add_argument("--conditions", default="unspecified")
    return argument_parser


def _config_from_args(args: argparse.Namespace, candump: str) -> CaptureConfig:
    return CaptureConfig(
        capture_root=args.capture_root,
        chunk_seconds=args.chunk_seconds,
        retry_seconds=args.retry_seconds,
        max_session_seconds=args.max_session_seconds,
        min_free_bytes=args.min_free_bytes,
        ignition_state=args.ignition_state,
        wake_condition=args.wake_condition,
        conditions=args.conditions,
        candump=candump,
    )


def check_roles(manager: PassiveInterfaceManager | None = None) -> dict[str, object]:
    """Independently check all roles so one failure does not hide healthy buses."""

    manager = manager or PassiveInterfaceManager()
    roles: dict[str, object] = {}
    for role in CAN_BUS_ROLES:
        try:
            with manager.observe(role) as lease:
                revalidate_exact_lease(manager, lease)
                roles[role] = {
                    "passive_ready": True,
                    "lease": lease.as_dict(),
                }
        except (PassiveInterfaceUnavailable, CaptureError, OSError) as exc:
            roles[role] = {
                "passive_ready": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "mode": "independent_receive_only_roles",
        "configures_interfaces": False,
        "transmits_can_frames": False,
        "passive_ready": all(
            bool(roles[role]["passive_ready"]) for role in CAN_BUS_ROLES
        ),
        "roles": roles,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.check:
        payload = check_roles()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["passive_ready"] else 1

    candump = shutil.which("candump")
    if candump is None:
        print("ERROR: candump is not installed", file=sys.stderr)
        return 2
    config = _config_from_args(args, candump)
    stop = StopController()
    try:
        config.validate()
        with singleton_lock(), installed_signal_handlers(stop):
            return run_supervisor(config, stop=stop)
    except CaptureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
