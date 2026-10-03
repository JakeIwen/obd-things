"""Loss-accounted candump capture and zstd chunk primitives.

Entry points own their distinct route, mount, and admission policies. The
compressed recorder keeps one candump child across rotations; the independent
raw three-bus workers share only argv construction, not its lifecycle policy.
"""

from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable, Sequence

from lib.timeutil import utc_now


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


CANDUMP_RE = re.compile(
    rb"^\((?P<timestamp>[^)]+)\)\s+\S+\s+(?P<can_id>[0-9A-Fa-f]{3,8})#"
)


DROP_RE = re.compile(
    rb"^DROPCOUNT:\s+dropped\s+(?P<frames>\d+)\s+CAN frames?.*"
    rb"\(total drops\s+(?P<total>\d+)\)"
)


class CaptureError(RuntimeError):
    """A fail-closed capture or preflight failure."""


class RecoverableCaptureError(CaptureError):
    """An owner may retry this failure only after capture cleanup succeeds."""


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


def parse_interface_state(
    details: str,
    *,
    channel: str,
) -> InterfaceState:
    """Parse ``ip -details link show CHANNEL`` without consulting live state."""

    flags_match = re.search(
        rf"^\d+:\s+{re.escape(channel)}:\s+<([^>]*)>",
        details,
        re.MULTILINE,
    )
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
    channel: str,
    bitrate: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> InterfaceState:
    errors: list[str] = []
    try:
        details_result = runner(
            ["ip", "-details", "-statistics", "link", "show", channel],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"{channel} state query failed: {type(exc).__name__}: {exc}")
        state = InterfaceState(False, None, False, None)
    else:
        if details_result.returncode != 0:
            errors.append(f"{channel} is missing or unreadable")
            state = InterfaceState(False, None, False, None)
        else:
            state = parse_interface_state(details_result.stdout, channel=channel)
            if not state.up:
                errors.append(f"{channel} is not UP")
            if state.bitrate != bitrate:
                errors.append(f"{channel} bitrate is {state.bitrate}, expected {bitrate}")
            if not state.listen_only:
                errors.append(f"{channel} is not LISTEN-ONLY")
            if state.controller_state != "ERROR-ACTIVE":
                errors.append(
                    f"{channel} controller state is {state.controller_state}, "
                    "expected ERROR-ACTIVE"
                )
            if state.rx_dropped is None or state.rx_missed is None:
                errors.append(
                    f"{channel} RX dropped/missed counters are unavailable"
                )

    if errors:
        raise CaptureError("runtime safety check failed:\n- " + "\n- ".join(errors))
    return state


def preflight(
    out_root: Path,
    policy: DiskPolicy,
    *,
    channel: str,
    bitrate: int,
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
        state = runtime_safety_check(
            channel=channel,
            bitrate=bitrate,
            runner=runner,
        )
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


def candump_command(
    executable: str,
    channel: str,
    *,
    receive_buffer_bytes: int | None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    command = [executable, "-L", *extra_args, "-d"]
    if receive_buffer_bytes is not None:
        command.extend(("-r", str(receive_buffer_bytes)))
    command.append(channel)
    return command



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
        required_start_id: int | None = None,
        required_start_id_timeout_seconds: float | None = None,
        *,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        disk_free: Callable[[Path], int] = available_bytes,
        safety_check: Callable[[], InterfaceState] | None = None,
        mount_check: Callable[[], None] | None = None,
        zstd: str = "zstd",
        candump: str = "candump",
        candump_extra_args: Sequence[str] = (),
        external_stop_requested: Callable[[], bool] | None = None,
        health_check: Callable[[], None] | None = None,
        started_callback: Callable[[], None] | None = None,
        install_signal_handlers: bool = True,
        channel: str,
        bitrate: int,
        sequence_start: int = 0,
    ) -> None:
        self.run_dir = run_dir
        self.priority_ids = priority_ids
        self.rotation_seconds = rotation_seconds
        self.duration_seconds = duration_seconds
        self.policy = policy
        self.stop_after_id = stop_after_id
        self.stop_after_id_absence_seconds = stop_after_id_absence_seconds
        self.required_start_id = required_start_id
        self.required_start_id_timeout_seconds = required_start_id_timeout_seconds
        if (required_start_id is None) != (
            required_start_id_timeout_seconds is None
        ):
            raise ValueError(
                "required_start_id and required_start_id_timeout_seconds "
                "must be configured together"
            )
        if (
            required_start_id_timeout_seconds is not None
            and required_start_id_timeout_seconds <= 0
        ):
            raise ValueError(
                "required_start_id_timeout_seconds must be positive"
            )
        if (
            required_start_id_timeout_seconds is not None
            and required_start_id_timeout_seconds >= duration_seconds
        ):
            raise ValueError(
                "required_start_id_timeout_seconds must be shorter than "
                "duration_seconds"
            )
        if not isinstance(channel, str) or not re.fullmatch(r"can[0-9]+", channel):
            raise ValueError("recorder channel must be a resolved kernel canN")
        if not isinstance(bitrate, int) or isinstance(bitrate, bool) or bitrate <= 0:
            raise ValueError("recorder bitrate must be a positive integer")
        if (
            not isinstance(sequence_start, int)
            or isinstance(sequence_start, bool)
            or sequence_start < 0
        ):
            raise ValueError("sequence_start must be a non-negative integer")
        self.channel = channel
        self.bitrate = bitrate
        # A later segment appended to the same directory continues the chunk
        # numbering so it can never reopen an earlier chunk or partial.
        self.sequence_start = sequence_start
        self.popen = popen
        self.runner = runner
        self.disk_free = disk_free
        self.safety_check = safety_check or (
            lambda: runtime_safety_check(
                channel=self.channel,
                bitrate=self.bitrate,
                runner=self.runner,
            )
        )
        self.mount_check = mount_check or (lambda: None)
        self.zstd = zstd
        self.candump = candump
        self.candump_extra_args = tuple(candump_extra_args)
        self.external_stop_requested = external_stop_requested
        self.health_check = health_check
        self.started_callback = started_callback
        self.install_signal_handlers = install_signal_handlers
        self.manifest = run_dir / "manifest.jsonl"
        self.checkpoint = run_dir / "checkpoint.json"
        self.stop_requested = False

    @staticmethod
    def _new_chunk(*args, **kwargs) -> Chunk:
        return Chunk(*args, **kwargs)

    def _candump_command(self) -> list[str]:
        return candump_command(
            self.candump,
            self.channel,
            receive_buffer_bytes=RECEIVE_BUFFER,
            extra_args=self.candump_extra_args,
        )

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
        sequence = self.sequence_start
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
        required_start_id_seen_at: float | None = None

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
            nonlocal detected_drops
            nonlocal tracked_id_first_seen, tracked_id_last_seen
            nonlocal required_start_id_seen_at
            if chunk is None:
                raise CaptureError("received candump data without an active chunk")
            chunk.write(line, self.priority_ids)
            _, can_id = parse_candump_line(line)
            if self.stop_after_id is not None and can_id == self.stop_after_id:
                observed = time.monotonic()
                if tracked_id_first_seen is None:
                    tracked_id_first_seen = observed
                tracked_id_last_seen = observed
            if (
                self.required_start_id is not None
                and can_id == self.required_start_id
                and required_start_id_seen_at is None
            ):
                required_start_id_seen_at = time.monotonic()
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

        old_handlers = (
            {
                signum: signal.signal(signum, request_stop)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            }
            if self.install_signal_handlers
            else {}
        )
        try:
            candump_command = self._candump_command()
            process = self.popen(
                candump_command,
                stdout=subprocess.PIPE,
                stderr=runtime_log,
                bufsize=0,
                start_new_session=True,
            )
            if process.stdout is None:
                raise CaptureError("candump stdout pipe was not created")
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            chunk = self._new_chunk(
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
                    "candump_command": candump_command,
                    "full_enabled": full_enabled,
                    "priority_ids": [f"0x{value:X}" for value in sorted(self.priority_ids)],
                    "free_bytes": free,
                },
            )
            if self.started_callback is not None:
                self.started_callback()

            while True:
                if self.health_check is not None:
                    self.health_check()
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
                if (
                    self.external_stop_requested is not None
                    and self.external_stop_requested()
                ):
                    reason = "external_stop"
                    break
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
                    self.required_start_id is not None
                    and required_start_id_seen_at is None
                    and now - started
                    >= self.required_start_id_timeout_seconds
                ):
                    reason = "required_start_id_missing"
                    fatal = CaptureError(
                        "required start CAN ID "
                        f"0x{self.required_start_id:X} was not observed within "
                        f"{self.required_start_id_timeout_seconds} seconds"
                    )
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
                        chunk = self._new_chunk(
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
                        chunk = self._new_chunk(
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
                    if fatal is None or isinstance(fatal, RecoverableCaptureError):
                        fatal = exc
                        reason = "error"
            if storage_available:
                try:
                    current_interface = self.safety_check()
                    self._assert_no_new_interface_drops(
                        baseline_interface, current_interface
                    )
                except Exception as exc:
                    if fatal is None or isinstance(fatal, RecoverableCaptureError):
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
                    if not record["complete"] and (
                        fatal is None or isinstance(fatal, RecoverableCaptureError)
                    ):
                        fatal = CaptureError("final zstd chunk failed validation")
                        reason = "error"
                except Exception as exc:
                    if fatal is None or isinstance(fatal, RecoverableCaptureError):
                        fatal = exc
                        reason = "error"
                if pending_error is not None and (
                    fatal is None or isinstance(fatal, RecoverableCaptureError)
                ):
                    fatal = pending_error
                    reason = "error"
            else:
                try:
                    harvest_chunks(wait=True)
                except Exception as exc:
                    if fatal is None or isinstance(fatal, RecoverableCaptureError):
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
            "required_start_id": (
                f"0x{self.required_start_id:X}"
                if self.required_start_id is not None
                else None
            ),
            "required_start_id_timeout_seconds": (
                self.required_start_id_timeout_seconds
            ),
            "required_start_id_seen_elapsed_seconds": (
                required_start_id_seen_at - started
                if required_start_id_seen_at is not None
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
            # Preserve typed capture failures after successful cleanup. Owners
            # may recover a specific boundary loss without treating unrelated
            # storage, transport, or interface errors as recoverable.
            if isinstance(fatal, CaptureError):
                raise fatal
            raise CaptureError(str(fatal)) from fatal
        return 0
