#!/usr/bin/env python3
"""Bounded, passive SocketCAN drive recorder.

The default invocation is a plan: it performs no subprocess calls and writes no
files.  Live recording requires ``--execute --confirm-passive --conditions``.

This tool never configures CAN, controls a service, or transmits.  It accepts
only an already-UP, 500 kbit/s, LISTEN-ONLY, ERROR-ACTIVE ``can0`` and then
runs one persistent ``candump`` process.  Its text stream is compressed into
bounded zstd chunks.  Selected CAN IDs may also be duplicated into a much
smaller priority stream which can continue after the disk soft floor disables
the full-bus stream.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import dataclasses
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import diagnostic_safety
from lib.modules import MODULES


CHANNEL = "can0"
BITRATE = 500_000
# Keep enough kernel-side backlog for transient EXFAT/zstd stalls.  A 4 MiB
# reserve overflowed once during an otherwise healthy 46-minute C-CAN drive;
# the loss gate worked, but that leg could not be treated as complete evidence.
RECEIVE_BUFFER = 16_777_216
RMEM_MAX_PATH = Path("/proc/sys/net/core/rmem_max")
DEFAULT_ROTATION_SECONDS = 600
DEFAULT_DURATION_SECONDS = 22 * 60 * 60
DEFAULT_STOP_ID_ABSENCE_SECONDS = 20.0
MAX_PENDING_FINALIZATION_SECONDS = 120
DEFAULT_SOFT_FREE_BYTES = 30 * 1024**3
DEFAULT_HARD_FREE_BYTES = 25 * 1024**3
SERVICE_BLOCKLIST = ("tpms-logger", "tpms-drivesniff")
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
CANDUMP_RE = re.compile(
    rb"^\((?P<timestamp>[^)]+)\)\s+\S+\s+(?P<can_id>[0-9A-Fa-f]{3,8})#"
)
DROP_RE = re.compile(
    rb"^DROPCOUNT:\s+dropped\s+(?P<frames>\d+)\s+CAN frames?.*"
    rb"\(total drops\s+(?P<total>\d+)\)"
)
CCAN_CORRELATION_BROADCAST_IDS = frozenset(
    {
        0x0EA,
        0x0EE,
        0x0FA,
        0x0FC,
        0x0FE,
        0x100,
        0x101,
        0x103,
        0x104,
        0x10F,
        0x110,
        0x116,
        0x1F1,
        0x1FA,
        0x2ED,
        0x2EF,
        0x412,
        0x417,
        0x419,
        0x41A,
        0x41B,
        0x41D,
        0x4B1,
        0x5A8,
        0x5BE,
    }
)


class CaptureError(RuntimeError):
    """A fail-closed capture or preflight failure."""


@dataclasses.dataclass(frozen=True)
class InterfaceState:
    up: bool
    bitrate: int | None
    listen_only: bool
    controller_state: str | None
    rx_dropped: int | None = None
    rx_missed: int | None = None


@dataclasses.dataclass(frozen=True)
class DiskPolicy:
    soft_free_bytes: int
    hard_free_bytes: int

    def __post_init__(self) -> None:
        if self.hard_free_bytes < 0:
            raise ValueError("hard disk floor cannot be negative")
        if self.soft_free_bytes <= self.hard_free_bytes:
            raise ValueError("soft disk floor must be greater than hard disk floor")

    def action(self, available_bytes: int) -> str:
        if available_bytes <= self.hard_free_bytes:
            return "stop"
        if available_bytes <= self.soft_free_bytes:
            return "priority-only"
        return "full"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def campaign_stamp() -> str:
    return dt.datetime.now().strftime("drive_%Y%m%d_%H%M%S")


def parse_can_id(value: str) -> int:
    text = value.strip().lower()
    base = 16 if text.startswith("0x") or re.fullmatch(r"[0-9a-f]+", text) else 10
    try:
        parsed = int(text, base)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid CAN ID: {value!r}") from exc
    if not 0 <= parsed <= 0x1FFFFFFF:
        raise argparse.ArgumentTypeError("CAN ID must be between 0 and 0x1FFFFFFF")
    return parsed


def resolved_priority_ids(args: argparse.Namespace) -> frozenset[int]:
    selected = set(args.priority_id)
    if args.priority_profile == "ccan-correlation":
        selected.update(CCAN_CORRELATION_BROADCAST_IDS)
        for module in MODULES.values():
            if module.bus == "c-can":
                selected.update((module.txid, module.rxid))
    return frozenset(selected)


def parse_interface_state(details: str) -> InterfaceState:
    """Parse ``ip -details link show can0`` without consulting live state."""

    flags_match = re.search(r"^\d+:\s+can0:\s+<([^>]*)>", details, re.MULTILINE)
    flags = set(flags_match.group(1).split(",")) if flags_match else set()
    bitrate_match = re.search(r"\bbitrate\s+(\d+)\b", details)
    state_match = re.search(
        r"\bcan(?:\s+<[^>]*>)?\s+state\s+([A-Z-]+)\b", details
    )
    rx_match = re.search(
        r"RX:\s+bytes\s+packets\s+errors\s+dropped\s+missed\b[^\n]*\n"
        r"\s*\d+\s+\d+\s+\d+\s+(?P<dropped>\d+)\s+(?P<missed>\d+)",
        details,
    )
    return InterfaceState(
        up="UP" in flags,
        bitrate=int(bitrate_match.group(1)) if bitrate_match else None,
        listen_only="<LISTEN-ONLY>" in details.upper(),
        controller_state=state_match.group(1) if state_match else None,
        rx_dropped=int(rx_match.group("dropped")) if rx_match else None,
        rx_missed=int(rx_match.group("missed")) if rx_match else None,
    )


def parse_candump_line(line: bytes) -> tuple[float | None, int | None]:
    match = CANDUMP_RE.match(line)
    if not match:
        return None, None
    try:
        timestamp = float(match.group("timestamp"))
        can_id = int(match.group("can_id"), 16)
    except ValueError:
        return None, None
    return timestamp, can_id


def parse_drop_line(line: bytes) -> tuple[int, int] | None:
    match = DROP_RE.match(line)
    if not match:
        return None
    return int(match.group("frames")), int(match.group("total"))


def is_priority_line(line: bytes, priority_ids: frozenset[int]) -> bool:
    _, can_id = parse_candump_line(line)
    return can_id in priority_ids if can_id is not None else False


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP):
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        fsync_directory(path.parent)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def append_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def campaign_file_lock(run_dir: Path):
    """Exclude recovery from a live writer for this exact campaign directory."""
    lock_path = run_dir / "capture.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError(f"campaign is already active: {run_dir}") from None
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def available_bytes(path: Path) -> int:
    anchor = path
    while not anchor.exists():
        if anchor.parent == anchor:
            raise CaptureError(f"no existing parent filesystem for {path}")
        anchor = anchor.parent
    return shutil.disk_usage(anchor).free


def require_writable_mount(
    out_root: Path,
    required_mount: Path,
    *,
    is_mount: Callable[[Path], bool] = os.path.ismount,
    statvfs: Callable[[Path], os.statvfs_result] = os.statvfs,
    stat: Callable[[Path], os.stat_result] = os.stat,
    expected_device: int | None = None,
) -> int:
    """Require output to resolve below the explicitly named writable mount."""
    if not required_mount.is_absolute():
        raise CaptureError("--require-mount must be an absolute path")
    try:
        resolved_mount = required_mount.resolve(strict=True)
    except OSError as exc:
        raise CaptureError(f"required mount is unavailable: {required_mount}") from exc
    resolved_output = out_root.resolve(strict=False)
    try:
        inside = os.path.commonpath((resolved_mount, resolved_output)) == str(
            resolved_mount
        )
    except ValueError:
        inside = False
    if not inside:
        raise CaptureError(
            f"--out-root must resolve below required mount {resolved_mount}"
        )
    if not is_mount(resolved_mount):
        raise CaptureError(f"required path is not a mount point: {resolved_mount}")
    flags = statvfs(resolved_mount).f_flag
    if flags & getattr(os, "ST_RDONLY", 1):
        raise CaptureError(f"required mount is read-only: {resolved_mount}")
    device = stat(resolved_mount).st_dev
    if expected_device is not None and device != expected_device:
        raise CaptureError(
            f"required mount device changed: {device} != {expected_device}"
        )
    return device


def read_rmem_max(path: Path = RMEM_MAX_PATH) -> int:
    try:
        text = path.read_text(encoding="ascii").strip()
        value = int(text)
    except (OSError, ValueError) as exc:
        raise CaptureError(f"cannot read socket receive-buffer limit {path}") from exc
    if value <= 0:
        raise CaptureError(f"invalid socket receive-buffer limit {value}")
    return value


def strip_partial_suffix(path: Path) -> Path:
    suffix = ".partial"
    text = str(path)
    if not text.endswith(suffix):
        raise ValueError(f"not a partial path: {path}")
    return Path(text[: -len(suffix)])


def verify_zstd_file(
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    zstd: str = "zstd",
) -> bool:
    try:
        result = runner(
            [zstd, "-t", "-q", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(
            f"zstd verification failed for {path}: {type(exc).__name__}: {exc}"
        ) from exc
    return result.returncode == 0


def recover_partials(
    root: Path,
    verifier: Callable[[Path], bool],
    guard: Callable[[], None] = lambda: None,
) -> list[dict]:
    """Recover complete zstd frames left with a .partial suffix.

    Invalid/truncated files are retained in place for later salvage.
    """

    recovered: list[dict] = []
    guard()
    if not root.exists():
        return recovered
    for partial in sorted(root.rglob("*.zst.partial")):
        guard()
        if not verifier(partial):
            continue
        guard()
        final = strip_partial_suffix(partial)
        if final.exists():
            base = final.with_name(
                f"{final.stem}.recovered-{int(time.time())}{final.suffix}"
            )
            final = base
            collision = 1
            while final.exists():
                final = base.with_name(
                    f"{base.stem}-{collision}{base.suffix}"
                )
                collision += 1
        os.replace(partial, final)
        fsync_directory(final.parent)
        record = {
            "type": "partial_recovery",
            "time_utc": utc_now(),
            "path": str(final),
            "compressed_bytes": final.stat().st_size,
            "sha256": sha256_file(final),
        }
        guard()
        append_manifest(final.parent / "manifest.jsonl", record)
        recovered.append(record)
    return recovered


def runtime_safety_check(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> InterfaceState:
    errors: list[str] = []
    try:
        details_result = runner(
            ["ip", "-details", "-statistics", "link", "show", CHANNEL],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"{CHANNEL} state query failed: {type(exc).__name__}: {exc}")
        state = InterfaceState(False, None, False, None)
    else:
        if details_result.returncode != 0:
            errors.append(f"{CHANNEL} is missing or unreadable")
            state = InterfaceState(False, None, False, None)
        else:
            state = parse_interface_state(details_result.stdout)
            if not state.up:
                errors.append(f"{CHANNEL} is not UP")
            if state.bitrate != BITRATE:
                errors.append(f"{CHANNEL} bitrate is {state.bitrate}, expected {BITRATE}")
            if not state.listen_only:
                errors.append(f"{CHANNEL} is not LISTEN-ONLY")
            if state.controller_state != "ERROR-ACTIVE":
                errors.append(
                    f"{CHANNEL} controller state is {state.controller_state}, "
                    "expected ERROR-ACTIVE"
                )
            if state.rx_dropped is None or state.rx_missed is None:
                errors.append(
                    f"{CHANNEL} RX dropped/missed counters are unavailable"
                )

    for service in SERVICE_BLOCKLIST:
        try:
            result = runner(
                ["systemctl", "is-active", service],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(
                f"cannot establish {service} state: {type(exc).__name__}: {exc}"
            )
            continue
        service_state = result.stdout.strip()
        if result.returncode == 0 and service_state == "active":
            errors.append(f"{service} is active; stop it before this dedicated capture")
        elif not (
            service_state in {"inactive", "failed", "unknown"}
            and result.returncode in {3, 4}
        ):
            errors.append(
                f"cannot establish {service} state: rc={result.returncode}, "
                f"stdout={service_state!r}, stderr={result.stderr.strip()!r}"
            )
    if errors:
        raise CaptureError("runtime safety check failed:\n- " + "\n- ".join(errors))
    return state


def preflight(
    out_root: Path,
    policy: DiskPolicy,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    disk_free: Callable[[Path], int] = available_bytes,
    rmem_max: Callable[[], int] = read_rmem_max,
) -> tuple[InterfaceState, int]:
    errors: list[str] = []
    for executable in ("candump", "zstd"):
        if which(executable) is None:
            errors.append(f"required executable is missing: {executable}")
    try:
        maximum_receive_buffer = rmem_max()
        if maximum_receive_buffer < RECEIVE_BUFFER:
            errors.append(
                "net.core.rmem_max is too small for the requested candump buffer: "
                f"{maximum_receive_buffer} < {RECEIVE_BUFFER}; before a long capture run "
                f"'sudo sysctl -w net.core.rmem_max={RECEIVE_BUFFER}'"
            )
    except CaptureError as exc:
        errors.append(str(exc))

    try:
        state = runtime_safety_check(runner=runner)
    except CaptureError as exc:
        errors.append(str(exc))
        state = InterfaceState(False, None, False, None)

    free = disk_free(out_root)
    if policy.action(free) == "stop":
        errors.append(
            f"only {free} bytes free, at/below hard floor {policy.hard_free_bytes}"
        )
    if errors:
        raise CaptureError("preflight failed:\n- " + "\n- ".join(errors))
    return state, free


class ZstdStream:
    def __init__(
        self,
        partial_path: Path,
        stderr_handle,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        zstd: str = "zstd",
    ) -> None:
        self.partial_path = partial_path
        self.uncompressed_bytes = 0
        self.lines = 0
        self.finished = False
        output = partial_path.open("wb")
        try:
            self.process = popen(
                [zstd, "-1", "-T1", "-q", "-c"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=stderr_handle,
                bufsize=0,
                start_new_session=True,
            )
        finally:
            output.close()
        if self.process.stdin is None:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait()
            raise CaptureError("zstd stdin pipe was not created")

    def write(self, line: bytes) -> None:
        if self.finished:
            raise CaptureError(f"write attempted after finalization: {self.partial_path}")
        if self.process.poll() is not None:
            raise CaptureError(f"zstd exited early for {self.partial_path}")
        try:
            self.process.stdin.write(line)
        except (BrokenPipeError, OSError) as exc:
            raise CaptureError(f"zstd pipe failed for {self.partial_path}: {exc}") from exc
        self.uncompressed_bytes += len(line)
        self.lines += 1

    def finish(
        self,
        verifier: Callable[[Path], bool],
        timeout: float = 30.0,
    ) -> dict:
        if self.finished:
            raise CaptureError(f"stream already finalized: {self.partial_path}")
        self.finished = True
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            returncode = -signal.SIGKILL

        record = {
            "partial_path": str(self.partial_path),
            "uncompressed_bytes": self.uncompressed_bytes,
            "frames": self.lines,
            "zstd_exit": returncode,
            "complete": False,
        }
        if self.partial_path.exists():
            with self.partial_path.open("rb+") as handle:
                os.fsync(handle.fileno())
        if returncode != 0 or not self.partial_path.exists() or not verifier(self.partial_path):
            return record

        final_path = strip_partial_suffix(self.partial_path)
        if final_path.exists():
            raise CaptureError(f"refusing to overwrite completed chunk: {final_path}")
        os.replace(self.partial_path, final_path)
        fsync_directory(final_path.parent)
        record.update(
            {
                "path": str(final_path),
                "compressed_bytes": final_path.stat().st_size,
                "sha256": sha256_file(final_path),
                "complete": True,
            }
        )
        return record

    def abort(self) -> None:
        """Idempotently close and terminate a compressor without deleting evidence."""
        if not self.finished:
            self.finished = True
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class Chunk:
    def __init__(
        self,
        run_dir: Path,
        sequence: int,
        full_enabled: bool,
        priority_enabled: bool,
        stderr_handle,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        zstd: str = "zstd",
    ) -> None:
        self.sequence = sequence
        self.started_utc = utc_now()
        self.started_monotonic = time.monotonic()
        self.first_frame_timestamp: float | None = None
        self.last_frame_timestamp: float | None = None
        prefix = f"chunk_{sequence:06d}"
        self.full = None
        self.priority = None
        try:
            if full_enabled:
                self.full = ZstdStream(
                    run_dir / f"{prefix}_full.candump.zst.partial",
                    stderr_handle,
                    popen=popen,
                    zstd=zstd,
                )
            if priority_enabled:
                self.priority = ZstdStream(
                    run_dir / f"{prefix}_priority.candump.zst.partial",
                    stderr_handle,
                    popen=popen,
                    zstd=zstd,
                )
        except BaseException:
            if self.full is not None:
                self.full.abort()
            if self.priority is not None:
                self.priority.abort()
            raise

    def write(self, line: bytes, priority_ids: frozenset[int]) -> None:
        timestamp, can_id = parse_candump_line(line)
        if timestamp is not None:
            self.first_frame_timestamp = self.first_frame_timestamp or timestamp
            self.last_frame_timestamp = timestamp
        if self.full is not None:
            self.full.write(line)
        if self.priority is not None and can_id in priority_ids:
            self.priority.write(line)

    def finish(self, verifier: Callable[[Path], bool]) -> dict:
        streams: dict[str, dict] = {}
        for name, stream in (("full", self.full), ("priority", self.priority)):
            if stream is None:
                continue
            try:
                streams[name] = stream.finish(verifier)
            except BaseException as exc:
                stream.abort()
                streams[name] = {
                    "partial_path": str(stream.partial_path),
                    "uncompressed_bytes": stream.uncompressed_bytes,
                    "frames": stream.lines,
                    "complete": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return {
            "type": "chunk",
            "sequence": self.sequence,
            "started_utc": self.started_utc,
            "ended_utc": utc_now(),
            "elapsed_seconds": time.monotonic() - self.started_monotonic,
            "first_frame_timestamp": self.first_frame_timestamp,
            "last_frame_timestamp": self.last_frame_timestamp,
            "streams": streams,
            "complete": all(item["complete"] for item in streams.values()),
        }

    def abort(self) -> None:
        for stream in (self.full, self.priority):
            if stream is not None:
                stream.abort()


class Recorder:
    def __init__(
        self,
        run_dir: Path,
        priority_ids: frozenset[int],
        rotation_seconds: int,
        duration_seconds: int,
        policy: DiskPolicy,
        stop_after_id: int | None = None,
        stop_after_id_absence_seconds: float | None = None,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        disk_free: Callable[[Path], int] = available_bytes,
        safety_check: Callable[[], InterfaceState] | None = None,
        mount_check: Callable[[], None] | None = None,
        zstd: str = "zstd",
        candump: str = "candump",
    ) -> None:
        self.run_dir = run_dir
        self.priority_ids = priority_ids
        self.rotation_seconds = rotation_seconds
        self.duration_seconds = duration_seconds
        self.policy = policy
        self.stop_after_id = stop_after_id
        self.stop_after_id_absence_seconds = stop_after_id_absence_seconds
        self.popen = popen
        self.runner = runner
        self.disk_free = disk_free
        self.safety_check = safety_check or (
            lambda: runtime_safety_check(runner=self.runner)
        )
        self.mount_check = mount_check or (lambda: None)
        self.zstd = zstd
        self.candump = candump
        self.manifest = run_dir / "manifest.jsonl"
        self.checkpoint = run_dir / "checkpoint.json"
        self.stop_requested = False

    def _verifier(self, path: Path) -> bool:
        return verify_zstd_file(path, runner=self.runner, zstd=self.zstd)

    def _checkpoint(self, payload: dict) -> None:
        atomic_write_json(self.checkpoint, payload)

    @staticmethod
    def _assert_no_new_interface_drops(
        baseline: InterfaceState,
        current: InterfaceState,
    ) -> None:
        changes: list[str] = []
        for field in ("rx_dropped", "rx_missed"):
            before = getattr(baseline, field)
            after = getattr(current, field)
            if before is None or after is None:
                changes.append(f"{field} counter unavailable")
            elif after < before:
                changes.append(f"{field} counter reset from {before} to {after}")
            elif after > before:
                changes.append(f"{field} increased from {before} to {after}")
        if changes:
            raise CaptureError(
                "SocketCAN interface loss accounting changed: " + "; ".join(changes)
            )

    def _stop_process(
        self,
        process: subprocess.Popen,
        consume: Callable[[bytes], None],
    ) -> None:
        """Signal, fully drain, and reap candump; defer callback errors until cleanup."""
        callback_error: Exception | None = None
        forced_action: str | None = None

        def drain_available() -> bool:
            nonlocal callback_error
            drained = False
            while True:
                try:
                    data = os.read(process.stdout.fileno(), 64 * 1024)
                except BlockingIOError:
                    return drained
                if not data:
                    return drained
                drained = True
                try:
                    consume(data)
                except Exception as exc:
                    if callback_error is None:
                        callback_error = exc

        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            drained = drain_available()
            if not drained:
                time.sleep(0.01)

        if process.poll() is None:
            forced_action = "SIGTERM"
            process.terminate()
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            if not drain_available():
                time.sleep(0.01)

        if process.poll() is None:
            forced_action = "SIGKILL"
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            if callback_error is not None:
                raise CaptureError(
                    "candump could not be reaped after SIGKILL; stream callback also failed: "
                    f"{callback_error}"
                ) from exc
            raise CaptureError("candump could not be reaped after SIGKILL") from exc

        # The child is reaped, so a final nonblocking pass reaches every byte it
        # placed in the pipe before exit.
        drain_available()

        failures: list[str] = []
        if callback_error is not None:
            failures.append(f"stream callback failed while draining: {callback_error}")
        if forced_action is not None:
            failures.append(
                f"candump required {forced_action}; final tail integrity is not guaranteed"
            )
        if failures:
            raise CaptureError("; ".join(failures)) from callback_error

    def run(self) -> int:
        self.mount_check()
        baseline_interface = self.safety_check()
        free = self.disk_free(self.run_dir)
        mode = self.policy.action(free)
        if mode == "stop":
            raise CaptureError("disk is already at/below the hard floor")
        full_enabled = mode == "full"
        full_stream_complete = full_enabled
        if not full_enabled and not self.priority_ids:
            raise CaptureError("disk is below soft floor and no priority IDs were configured")

        runtime_log = (self.run_dir / "runtime.stderr.log").open("ab", buffering=0)
        process = None
        selector = selectors.DefaultSelector()
        chunk: Chunk | None = None
        finalizer = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="capture-finalize"
        )
        pending: list[
            tuple[concurrent.futures.Future, float, int]
        ] = []
        sequence = 0
        buffer = bytearray()
        started = time.monotonic()
        next_disk_check = started
        reason = "duration_complete"
        fatal: Exception | None = None
        detected_drops = 0
        storage_available = True
        stop_signal: int | None = None
        stop_requested_at: float | None = None
        tracked_id_first_seen: float | None = None
        tracked_id_last_seen: float | None = None

        def request_stop(signum, _frame) -> None:
            nonlocal stop_requested_at, stop_signal
            if stop_requested_at is None:
                stop_requested_at = time.monotonic()
                stop_signal = signum
            self.stop_requested = True

        def submit_chunk(finished: Chunk) -> None:
            pending.append(
                (
                    finalizer.submit(finished.finish, self._verifier),
                    time.monotonic(),
                    finished.sequence,
                )
            )

        def harvest_chunks(*, wait: bool) -> None:
            for item in list(pending):
                future, submitted_at, submitted_sequence = item
                if not wait and not future.done():
                    elapsed = time.monotonic() - submitted_at
                    if elapsed > MAX_PENDING_FINALIZATION_SECONDS:
                        raise CaptureError(
                            "chunk finalization exceeded "
                            f"{MAX_PENDING_FINALIZATION_SECONDS}s for sequence "
                            f"{submitted_sequence}; stopping before the active chunk "
                            "can grow without a bound"
                        )
                    continue
                pending.remove(item)
                record = future.result()
                append_manifest(self.manifest, record)
                if not record["complete"]:
                    raise CaptureError("one or more zstd chunks failed validation")

        def write_line(line: bytes) -> CaptureError | None:
            nonlocal detected_drops, tracked_id_first_seen, tracked_id_last_seen
            if chunk is None:
                raise CaptureError("received candump data without an active chunk")
            chunk.write(line, self.priority_ids)
            _, can_id = parse_candump_line(line)
            if self.stop_after_id is not None and can_id == self.stop_after_id:
                observed = time.monotonic()
                if tracked_id_first_seen is None:
                    tracked_id_first_seen = observed
                tracked_id_last_seen = observed
            dropped = parse_drop_line(line)
            if dropped is not None:
                frames, total = dropped
                detected_drops = max(detected_drops + frames, total)
                append_manifest(
                    self.manifest,
                    {
                        "type": "socket_drop",
                        "time_utc": utc_now(),
                        "dropped_frames": frames,
                        "total_drops": total,
                    },
                )
                return CaptureError(
                    f"candump reported {frames} dropped frames ({total} total)"
                )
            return None

        def consume(data: bytes) -> None:
            buffer.extend(data)
            first_error: Exception | None = None
            while True:
                newline_at = buffer.find(b"\n")
                if newline_at < 0:
                    break
                newline = newline_at + 1
                line = bytes(buffer[:newline])
                # Consume the prefix before dispatching it. If a compressor or
                # manifest write fails, cleanup can continue with later bytes
                # without replaying this line.
                del buffer[:newline]
                try:
                    line_error = write_line(line)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    if line_error is not None and first_error is None:
                        first_error = line_error
            if first_error is not None:
                raise first_error

        old_handlers = {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            process = self.popen(
                [
                    self.candump,
                    "-L",
                    "-d",
                    "-r",
                    str(RECEIVE_BUFFER),
                    CHANNEL,
                ],
                stdout=subprocess.PIPE,
                stderr=runtime_log,
                bufsize=0,
                start_new_session=True,
            )
            if process.stdout is None:
                raise CaptureError("candump stdout pipe was not created")
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            chunk = Chunk(
                self.run_dir,
                sequence,
                full_enabled,
                bool(self.priority_ids),
                runtime_log,
                popen=self.popen,
                zstd=self.zstd,
            )
            append_manifest(
                self.manifest,
                {
                    "type": "capture_start",
                    "time_utc": utc_now(),
                    "candump_command": [
                        self.candump,
                        "-L",
                        "-d",
                        "-r",
                        str(RECEIVE_BUFFER),
                        CHANNEL,
                    ],
                    "full_enabled": full_enabled,
                    "priority_ids": [f"0x{value:X}" for value in sorted(self.priority_ids)],
                    "free_bytes": free,
                },
            )

            while True:
                harvest_chunks(wait=False)
                for key, _ in selector.select(timeout=1.0):
                    try:
                        data = os.read(key.fd, 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not data:
                        if process.poll() is not None:
                            raise CaptureError(
                                f"candump exited unexpectedly with status {process.returncode}"
                            )
                        continue
                    consume(data)

                if process.poll() is not None:
                    raise CaptureError(
                        f"candump exited unexpectedly with status {process.returncode}"
                    )
                now = time.monotonic()
                if self.stop_requested:
                    signal_time = (
                        stop_requested_at
                        if stop_requested_at is not None
                        else now
                    )
                    if signal_time - started < self.duration_seconds:
                        reason = "signal"
                        fatal = CaptureError(
                            "capture interrupted by signal "
                            f"{stop_signal} before the requested duration completed"
                        )
                        break
                if now - started >= self.duration_seconds:
                    break
                if (
                    self.stop_after_id is not None
                    and self.stop_after_id_absence_seconds is not None
                    and tracked_id_last_seen is not None
                    and now - tracked_id_last_seen
                    >= self.stop_after_id_absence_seconds
                ):
                    reason = "tracked_id_absent"
                    break
                if now - chunk.started_monotonic >= self.rotation_seconds:
                    harvest_chunks(wait=False)
                    if not pending:
                        self.mount_check()
                        finished_chunk = chunk
                        sequence += 1
                        chunk = Chunk(
                            self.run_dir,
                            sequence,
                            full_enabled,
                            bool(self.priority_ids),
                            runtime_log,
                            popen=self.popen,
                            zstd=self.zstd,
                        )
                        submit_chunk(finished_chunk)
                if now >= next_disk_check:
                    self.mount_check()
                    current_interface = self.safety_check()
                    self._assert_no_new_interface_drops(
                        baseline_interface, current_interface
                    )
                    free = self.disk_free(self.run_dir)
                    action = self.policy.action(free)
                    self._checkpoint(
                        {
                            "status": "running",
                            "time_utc": utc_now(),
                            "sequence": sequence,
                            "full_enabled": full_enabled,
                            "free_bytes": free,
                            "disk_action": action,
                        }
                    )
                    next_disk_check = now + 10
                    if action == "stop":
                        reason = "disk_hard_floor"
                        fatal = CaptureError(
                            "required output disk reached the hard free-space floor "
                            "before the requested duration completed"
                        )
                        break
                    if action == "priority-only" and full_enabled:
                        harvest_chunks(wait=False)
                        if pending:
                            continue
                        if not self.priority_ids:
                            reason = "disk_soft_floor_no_priority"
                            fatal = CaptureError(
                                "output disk reached the soft free-space floor and no "
                                "priority IDs were configured"
                            )
                            break
                        self.mount_check()
                        finished_chunk = chunk
                        full_enabled = False
                        full_stream_complete = False
                        append_manifest(
                            self.manifest,
                            {
                                "type": "mode_change",
                                "time_utc": utc_now(),
                                "mode": "priority-only",
                                "free_bytes": free,
                            },
                        )
                        sequence += 1
                        chunk = Chunk(
                            self.run_dir,
                            sequence,
                            False,
                            True,
                            runtime_log,
                            popen=self.popen,
                            zstd=self.zstd,
                        )
                        submit_chunk(finished_chunk)
        except Exception as exc:
            fatal = exc
            reason = "error"
            try:
                self.mount_check()
            except Exception:
                storage_available = False
        finally:
            if process is not None:
                try:
                    self._stop_process(process, consume)
                except Exception as exc:
                    if fatal is None:
                        fatal = exc
                        reason = "error"
            if storage_available:
                try:
                    current_interface = self.safety_check()
                    self._assert_no_new_interface_drops(
                        baseline_interface, current_interface
                    )
                except Exception as exc:
                    if fatal is None:
                        fatal = exc
                        reason = "error"
            if not storage_available:
                if chunk is not None:
                    chunk.abort()
                for future, _submitted_at, _submitted_sequence in pending:
                    try:
                        future.result()
                    except Exception:
                        pass
                pending.clear()
            elif chunk is not None:
                pending_error: Exception | None = None
                try:
                    harvest_chunks(wait=True)
                except Exception as exc:
                    pending_error = exc
                try:
                    if buffer:
                        consume(b"\n")
                    record = chunk.finish(self._verifier)
                    append_manifest(self.manifest, record)
                    if not record["complete"] and fatal is None:
                        fatal = CaptureError("final zstd chunk failed validation")
                        reason = "error"
                except Exception as exc:
                    if fatal is None:
                        fatal = exc
                        reason = "error"
                if pending_error is not None and fatal is None:
                    fatal = pending_error
                    reason = "error"
            else:
                try:
                    harvest_chunks(wait=True)
                except Exception as exc:
                    if fatal is None:
                        fatal = exc
                        reason = "error"
            finalizer.shutdown(wait=True, cancel_futures=False)
            selector.close()
            runtime_log.close()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)

        if not storage_available:
            if fatal is None:
                fatal = CaptureError("required output mount became unavailable")
            raise CaptureError(str(fatal)) from fatal

        self.mount_check()
        end_record = {
            "type": "capture_end",
            "time_utc": utc_now(),
            "reason": reason,
            "success": fatal is None,
            "duration_complete": reason == "duration_complete",
            "tracked_id": (
                f"0x{self.stop_after_id:X}"
                if self.stop_after_id is not None
                else None
            ),
            "tracked_id_first_seen_elapsed_seconds": (
                tracked_id_first_seen - started
                if tracked_id_first_seen is not None
                else None
            ),
            "tracked_id_last_seen_elapsed_seconds": (
                tracked_id_last_seen - started
                if tracked_id_last_seen is not None
                else None
            ),
            "tracked_id_absence_seconds": (
                self.stop_after_id_absence_seconds
                if reason == "tracked_id_absent"
                else None
            ),
            "signal_number": stop_signal if reason == "signal" else None,
            "signal_elapsed_seconds": (
                stop_requested_at - started
                if reason == "signal" and stop_requested_at is not None
                else None
            ),
            "full_stream_complete": full_stream_complete,
            "requested_duration_seconds": self.duration_seconds,
            "elapsed_seconds": time.monotonic() - started,
            "error": str(fatal) if fatal else None,
            "free_bytes": self.disk_free(self.run_dir),
            "detected_socket_drops": detected_drops,
        }
        append_manifest(self.manifest, end_record)
        self._checkpoint({"status": "complete" if fatal is None else "error", **end_record})
        if fatal is not None:
            raise CaptureError(str(fatal)) from fatal
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-root",
        required=True,
        type=Path,
        help="explicit parent for the campaign directory; parents are created only with --execute",
    )
    parser.add_argument(
        "--require-mount",
        type=Path,
        help="exact writable mount point that must contain --out-root (required with --execute)",
    )
    parser.add_argument("--campaign", help="safe directory name; default is timestamped")
    parser.add_argument("--execute", action="store_true", help="perform passive recording")
    parser.add_argument(
        "--recover-partials",
        action="store_true",
        help="verify and finalize complete .zst.partial files in one named campaign",
    )
    parser.add_argument(
        "--confirm-recovery",
        action="store_true",
        help="confirm explicit rename/manifest writes for --recover-partials",
    )
    parser.add_argument(
        "--confirm-passive",
        action="store_true",
        help="confirm that PCAN must remain listen-only and this tool must never transmit",
    )
    parser.add_argument(
        "--conditions",
        default="",
        help="vehicle/topology conditions recorded in run metadata (required with --execute)",
    )
    parser.add_argument(
        "--priority-id",
        action="append",
        default=[],
        type=parse_can_id,
        help="CAN ID to duplicate into priority chunks; repeat as needed",
    )
    parser.add_argument(
        "--priority-profile",
        choices=("none", "ccan-correlation"),
        default="ccan-correlation",
        help="bounded built-in priority-ID set; explicit --priority-id values are added",
    )
    parser.add_argument(
        "--rotation-seconds",
        type=int,
        default=DEFAULT_ROTATION_SECONDS,
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--stop-after-id",
        type=parse_can_id,
        help=(
            "cleanly finish after this CAN ID has been observed and then remains "
            "absent for --stop-after-id-absence-seconds"
        ),
    )
    parser.add_argument(
        "--stop-after-id-absence-seconds",
        type=float,
        default=DEFAULT_STOP_ID_ABSENCE_SECONDS,
        help="absence grace period used with --stop-after-id (default: 20 seconds)",
    )
    parser.add_argument(
        "--soft-free-gib",
        type=float,
        default=DEFAULT_SOFT_FREE_BYTES / 1024**3,
    )
    parser.add_argument(
        "--hard-free-gib",
        type=float,
        default=DEFAULT_HARD_FREE_BYTES / 1024**3,
    )
    return parser


def validate_args(args: argparse.Namespace) -> DiskPolicy:
    if not args.out_root.is_absolute():
        raise CaptureError("--out-root must be an absolute path")
    if args.campaign and not CAMPAIGN_RE.fullmatch(args.campaign):
        raise CaptureError("--campaign must contain only letters, digits, dot, underscore, or dash")
    if args.rotation_seconds < 10:
        raise CaptureError("--rotation-seconds must be at least 10")
    if args.duration_seconds < 1:
        raise CaptureError("--duration-seconds must be positive")
    if args.duration_seconds > 48 * 60 * 60:
        raise CaptureError("--duration-seconds cannot exceed 48 hours")
    if (
        not math.isfinite(args.stop_after_id_absence_seconds)
        or not 1 <= args.stop_after_id_absence_seconds <= 3600
    ):
        raise CaptureError(
            "--stop-after-id-absence-seconds must be between 1 and 3600"
        )
    policy = DiskPolicy(
        soft_free_bytes=int(args.soft_free_gib * 1024**3),
        hard_free_bytes=int(args.hard_free_gib * 1024**3),
    )
    if args.recover_partials:
        if not args.campaign:
            raise CaptureError("--recover-partials requires an exact --campaign")
        if args.execute:
            if not args.confirm_recovery:
                raise CaptureError(
                    "--execute --recover-partials requires --confirm-recovery"
                )
            if args.require_mount is None:
                raise CaptureError(
                    "--execute --recover-partials requires --require-mount"
                )
        return policy
    if args.execute:
        if not args.confirm_passive:
            raise CaptureError("--execute requires --confirm-passive")
        if not args.conditions.strip():
            raise CaptureError("--execute requires non-empty --conditions")
        if args.require_mount is None:
            raise CaptureError("--execute requires --require-mount")
    return policy


def plan(args: argparse.Namespace, policy: DiskPolicy) -> dict:
    campaign = args.campaign or "<drive_TIMESTAMP>"
    priority_ids = resolved_priority_ids(args)
    if args.recover_partials:
        return {
            "mode": "recovery_execute" if args.execute else "recovery_plan_only",
            "interaction": "offline_partial_verification_and_rename",
            "target": str(args.out_root / campaign),
            "required_mount": str(args.require_mount) if args.require_mount else None,
            "pattern": "*.zst.partial",
            "invalid_partials": "retained unchanged",
            "live_gates": [
                "--execute",
                "--recover-partials",
                "--confirm-recovery",
                "--campaign NAME",
                "--require-mount PATH",
            ],
            "does_not": [
                "open or configure CAN",
                "control services",
                "transmit CAN",
                "delete invalid partial files",
                "change network or proxy settings",
            ],
        }
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "passive_receive_only",
        "interface_requirement": {
            "channel": CHANNEL,
            "up": True,
            "bitrate": BITRATE,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
        },
        "blocked_services": list(SERVICE_BLOCKLIST),
        "candump_command": [
            "candump",
            "-L",
            "-d",
            "-r",
            str(RECEIVE_BUFFER),
            CHANNEL,
        ],
        "output": str(args.out_root / campaign),
        "required_mount": str(args.require_mount) if args.require_mount else None,
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "stop_after_id": (
            f"0x{args.stop_after_id:X}"
            if args.stop_after_id is not None
            else None
        ),
        "stop_after_id_absence_seconds": (
            args.stop_after_id_absence_seconds
            if args.stop_after_id is not None
            else None
        ),
        "priority_profile": args.priority_profile,
        "priority_ids": [f"0x{value:X}" for value in sorted(priority_ids)],
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "minimum_net_core_rmem_max": RECEIVE_BUFFER,
        "live_gates": ["--execute", "--confirm-passive", "--conditions TEXT"],
        "does_not": [
            "configure CAN",
            "control services",
            "transmit CAN",
            "change network or proxy settings",
        ],
    }


def execute(args: argparse.Namespace, policy: DiskPolicy) -> int:
    campaign = args.campaign or campaign_stamp()
    capture_root = args.out_root
    priority_ids = resolved_priority_ids(args)
    mount_device = require_writable_mount(capture_root, args.require_mount)
    state, free = preflight(capture_root, policy)
    try:
        lock_handle = diagnostic_safety.acquire_channel_observer_lock(CHANNEL)
    except diagnostic_safety.ChannelLockError as exc:
        raise CaptureError(str(exc)) from exc
    try:
        # Close the service/interface race after reserving the participating channel.
        require_writable_mount(
            capture_root,
            args.require_mount,
            expected_device=mount_device,
        )
        state, free = preflight(capture_root, policy)
        capture_root.mkdir(parents=True, exist_ok=True)
        run_dir = capture_root / campaign
        if run_dir.exists():
            raise CaptureError(f"campaign directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)

        metadata = {
            "type": "run_metadata",
            "created_utc": utc_now(),
            "campaign": campaign,
            "conditions": args.conditions.strip(),
            "interaction": "passive_receive_only",
            "interface": dataclasses.asdict(state),
            "required_mount": str(args.require_mount.resolve()),
            "free_bytes_at_preflight": free,
            "rotation_seconds": args.rotation_seconds,
            "duration_seconds": args.duration_seconds,
            "stop_after_id": (
                f"0x{args.stop_after_id:X}"
                if args.stop_after_id is not None
                else None
            ),
            "stop_after_id_absence_seconds": (
                args.stop_after_id_absence_seconds
                if args.stop_after_id is not None
                else None
            ),
            "priority_profile": args.priority_profile,
            "priority_ids": [f"0x{value:X}" for value in sorted(priority_ids)],
            "soft_free_bytes": policy.soft_free_bytes,
            "hard_free_bytes": policy.hard_free_bytes,
            "net_core_rmem_max": read_rmem_max(),
        }
        atomic_write_json(run_dir / "run.json", metadata)
        zstd = shutil.which("zstd") or "zstd"
        recorder = Recorder(
            run_dir,
            priority_ids,
            args.rotation_seconds,
            args.duration_seconds,
            policy,
            stop_after_id=args.stop_after_id,
            stop_after_id_absence_seconds=(
                args.stop_after_id_absence_seconds
                if args.stop_after_id is not None
                else None
            ),
            mount_check=lambda: require_writable_mount(
                capture_root,
                args.require_mount,
                expected_device=mount_device,
            ),
            zstd=zstd,
            candump=shutil.which("candump") or "candump",
        )
        with campaign_file_lock(run_dir):
            return recorder.run()
    finally:
        diagnostic_safety.release_channel_lock(lock_handle)


def execute_recovery(args: argparse.Namespace) -> int:
    """Recover only valid zstd frames inside one explicitly named campaign."""
    capture_root = args.out_root
    mount_device = require_writable_mount(capture_root, args.require_mount)
    run_dir = capture_root / args.campaign
    if not run_dir.is_dir():
        raise CaptureError(f"campaign directory does not exist: {run_dir}")
    zstd = shutil.which("zstd")
    if zstd is None:
        raise CaptureError("required executable is missing: zstd")
    with campaign_file_lock(run_dir):
        records = recover_partials(
            run_dir,
            lambda path: verify_zstd_file(path, zstd=zstd),
            guard=lambda: require_writable_mount(
                capture_root,
                args.require_mount,
                expected_device=mount_device,
            ),
        )
    require_writable_mount(
        capture_root,
        args.require_mount,
        expected_device=mount_device,
    )
    print(
        json.dumps(
            {
                "mode": "recovery_complete",
                "campaign": args.campaign,
                "recovered": len(records),
                "records": records,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = validate_args(args)
        if not args.execute:
            print(json.dumps(plan(args, policy), indent=2, sort_keys=True))
            return 0
        if args.recover_partials:
            return execute_recovery(args)
        return execute(args, policy)
    except (CaptureError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
