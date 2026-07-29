#!/usr/bin/env python3
"""Bounded drive logger for reviewed cluster or TCM ReadDataByIdentifier profiles.

The default invocation is an inert plan. Live mode is deliberately narrow:

* one verified ECU endpoint from ``lib/modules.py``;
* one fixed, reviewed physical-``22`` request profile;
* no DiagnosticSessionControl, TesterPresent, DTC, routine, write, security, reset,
  functional, wake, retry, interface-recovery, or re-arm traffic;
* at most five request attempts per second;
* an integrated receive-only full-bus ``candump`` child started before the first read;
* append-only samples plus an atomic summary on an explicitly required external mount.

The process must be started while parked. It can then run unattended during ordinary
driving, stopping on its duration bound, a sustained loss of the verified ignition frame,
the external-disk soft floor, an error, or a signal. It always attempts to close the ISO-TP
socket and raw observer, restore ``can0`` to verified listen-only mode, and release the
exclusive channel lock. It never tries to recover a dropped adapter while the vehicle moves.

Example plan (no CAN, mount, service, subprocess, or output access):

    python3 projects/ecu_mapping/cluster_drive_log.py \
      --out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/cluster-drive \
      --raw-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/cluster-drive \
      --require-mount /mnt/EXFAT512 --duration-seconds 72000

Live execution, started while parked before the drive:

    ./bringup.sh --tx
    python3 projects/ecu_mapping/cluster_drive_log.py \
      --out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/cluster-drive \
      --raw-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/cluster-drive \
      --require-mount /mnt/EXFAT512 --duration-seconds 72000 \
      --execute --confirm-driving-read-only --confirm-started-parked \
      --confirm-no-other-diagnostics --pair 6/14 \
      --conditions "ordinary driving; AlfaOBD closed; PCAN on C-CAN 6/14"

The opt-in TCM thermal profile reads only gearbox-oil DID ``04FE`` and TCU-chip
temperature DID ``0301``. It exists to exact-link both controls to the same
loss-accounted C-CAN stream during a broad cold-start drive:

    ./bringup.sh --tx
    python3 projects/ecu_mapping/cluster_drive_log.py \
      --profile tcm-thermal \
      --out-root /mnt/EXFAT512/obd-things/tmp/ecu_mapping/tcm-thermal-drive \
      --raw-root /mnt/EXFAT512/obd-things/tmp/captures/ccan/tcm-thermal-drive \
      --require-mount /mnt/EXFAT512 --duration-seconds 10800 \
      --execute --confirm-driving-read-only --confirm-started-parked \
      --confirm-no-other-diagnostics --confirm-tcm-thermal-correlation \
      --pair 6/14 \
      --conditions "cold-start drive; AlfaOBD closed; PCAN C-CAN 6/14"
"""

from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import ctypes
import dataclasses
from decimal import Decimal, InvalidOperation
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import canbus, diagnostic_safety
from lib.modules import MODULES
from tools.passive_drive_capture import (
    Chunk,
    InterfaceState,
    RECEIVE_BUFFER,
    append_manifest,
    atomic_write_json,
    available_bytes,
    fsync_directory,
    parse_candump_line,
    parse_drop_line,
    parse_interface_state,
    read_rmem_max,
    require_writable_mount,
    verify_zstd_file,
)

try:
    from lib import uds
except SystemExit as exc:
    # Planning is deliberately dependency-free. Live preflight reports this before
    # output, locks, sockets, interface changes, or transmission are attempted.
    uds = None
    UDS_IMPORT_ERROR = str(exc)
else:
    UDS_IMPORT_ERROR = None


def diagnostic_preflight(channel: str, bitrate: int) -> list[str]:
    """Load the active diagnostic preflight only for live execution."""
    from tools.ecu_discover import preflight

    return list(preflight(channel, bitrate))


CHANNEL = "can0"
BITRATE = 500_000
PAIR = "6/14"
REQUEST_RATE_HZ = 5.0
REQUEST_INTERVAL_S = 1.0 / REQUEST_RATE_HZ
REQUEST_TIMEOUT_S = 0.75
DEFAULT_DURATION_SECONDS = 20 * 60 * 60
MAX_DURATION_SECONDS = 22 * 60 * 60
DEFAULT_SOFT_FREE_BYTES = 30 * 1024**3
DEFAULT_HARD_FREE_BYTES = 25 * 1024**3
RUNTIME_CHECK_INTERVAL_S = 10.0
CHECKPOINT_INTERVAL_S = 10.0
RAW_ROTATION_SECONDS = 10 * 60
MAX_PENDING_FINALIZATION_SECONDS = 120.0
IGNITION_CAN_ID = 0x2EF
IGNITION_START_TIMEOUT_S = 15.0
IGNITION_LOSS_TIMEOUT_S = 10.0
MAX_CONSECUTIVE_DID_FAILURES = 3
DEFAULT_TELEMETRY_SOCKET = "/run/van-telemetry/api.sock"
TELEMETRY_TIMEOUT_S = 0.25
TELEMETRY_MAX_PENDING = 8
TELEMETRY_CLOSE_TIMEOUT_S = 2.0
TELEMETRY_IGNITION_INTERVAL_S = 1.0
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
BLOCKED_SERVICES = ("tpms-drivesniff",)
BLOCKED_PROCESS_BASENAMES = frozenset(
    {
        "auto_drive_logger.py",
        "cluster_live.py",
        "did_hunt_log.py",
        "radar_acc_drive_log.py",
        "radar_acc_sda_drive.py",
        "signal_correlate.py",
    }
)

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_SFF_MASK = 0x000007FF
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
PR_SET_PDEATHSIG = 1
WIRE_RE = re.compile(
    rb"^\((?P<timestamp>[^)]+)\)\s+\S+\s+"
    rb"(?P<can_id>[0-9A-Fa-f]{3,8})#(?P<data>[0-9A-Fa-f]*)\s*$"
)


@dataclasses.dataclass(frozen=True)
class DriveProfile:
    key: str
    module_key: str
    txid: int
    rxid: int
    dids: tuple[int, ...]
    expected_data_lengths: dict[int, int]
    request_rate_hz: float
    wire_stem: str
    purpose: str


DRIVE_PROFILES = {
    "cluster": DriveProfile(
        key="cluster",
        module_key="cluster",
        txid=0x18DA60F1,
        rxid=0x18DAF160,
        dids=(0x1000, 0x1002, 0x0107, 0x1004, 0x1005),
        expected_data_lengths={
            0x1000: 2,
            0x1002: 1,
            0x0107: 1,
            0x1004: 1,
            0x1005: 1,
        },
        request_rate_hz=5.0,
        wire_stem="cluster_wire",
        purpose="cluster scaling and state correlation",
    ),
    "tcm-thermal": DriveProfile(
        key="tcm-thermal",
        module_key="tcm",
        txid=0x18DA18F1,
        rxid=0x18DAF118,
        dids=(0x04FE, 0x0301),
        expected_data_lengths={0x04FE: 1, 0x0301: 1},
        request_rate_hz=2.0,
        wire_stem="tcm_thermal_wire",
        purpose=(
            "broad-range gearbox-oil carrier challenge with TCU-chip "
            "temperature negative control"
        ),
    ),
}

ACTIVE_PROFILE = DRIVE_PROFILES["cluster"]
MODULE = MODULES[ACTIVE_PROFILE.module_key]
CLUSTER_DIDS = ACTIVE_PROFILE.dids
EXPECTED_DATA_LENGTHS = ACTIVE_PROFILE.expected_data_lengths


def select_drive_profile(name: str) -> DriveProfile:
    """Select one reviewed profile before planning or opening live resources."""
    global ACTIVE_PROFILE, MODULE, CLUSTER_DIDS, EXPECTED_DATA_LENGTHS
    try:
        profile = DRIVE_PROFILES[name]
    except KeyError:
        choices = ", ".join(sorted(DRIVE_PROFILES))
        raise DriveLogError(
            f"unknown drive profile {name!r}; choose {choices}"
        ) from None
    ACTIVE_PROFILE = profile
    MODULE = MODULES[profile.module_key]
    CLUSTER_DIDS = profile.dids
    EXPECTED_DATA_LENGTHS = profile.expected_data_lengths
    return profile


class DriveLogError(RuntimeError):
    """A bounded capture or safety invariant failed."""


class CampaignLimitReached(RuntimeError):
    """The duration boundary was reached before another request could be sent."""


TELEMETRY_RAW_DIDS = {
    0x1000: ("diagnostics.cluster.did.1000.raw", "raw_u16_be"),
    0x1002: ("diagnostics.cluster.did.1002.raw", "raw_u8"),
    0x0107: ("diagnostics.cluster.did.0107.raw", "raw_u8"),
    0x1005: ("diagnostics.cluster.did.1005.raw", "raw_u8"),
}
TELEMETRY_METRICS = frozenset(
    {
        "battery.voltage",
        "vehicle.ignition_on",
        *(name for name, _unit in TELEMETRY_RAW_DIDS.values()),
    }
)


def telemetry_metrics_for_profile() -> frozenset[str]:
    if ACTIVE_PROFILE.key == "cluster":
        return TELEMETRY_METRICS
    return frozenset({"vehicle.ignition_on"})


def telemetry_observation_for_did(
    did: int, data: bytes
) -> tuple[str, dict[str, object]] | None:
    """Return one allowlisted dashboard observation for an exact positive DID."""
    expected_length = EXPECTED_DATA_LENGTHS.get(did)
    if expected_length is None or len(data) != expected_length:
        raise ValueError(
            f"DID {did:04X} telemetry payload length {len(data)} != {expected_length}"
        )
    raw = int.from_bytes(data, "big")
    if did == 0x1004:
        return (
            "battery.voltage",
            {
                "value": raw / 10.0,
                "unit": "V",
                "source": "cluster.did.1004",
                "bus": "c-can",
                "quality": "observed_alfa_scale",
            },
        )
    definition = TELEMETRY_RAW_DIDS.get(did)
    if definition is None:
        return None
    metric, unit = definition
    return (
        metric,
        {
            "value": raw,
            "unit": unit,
            "source": f"cluster.did.{did:04x}",
            "bus": "c-can",
            "quality": "candidate",
        },
    )


def ignition_telemetry_observation() -> tuple[str, dict[str, object]]:
    """Describe fresh presence of the verified ignition-on broadcast frame."""
    return (
        "vehicle.ignition_on",
        {
            "value": True,
            "unit": "boolean",
            "source": "ccan.broadcast.0x2ef",
            "bus": "c-can",
            "quality": "verified",
        },
    )


class BestEffortTelemetryPublisher:
    """Bounded latest-value publisher that can never stall the CAN owner.

    The fixed metric allowlist caps memory independently of broker health.
    Publication happens on one daemon thread with a short Unix-socket timeout;
    failures are counted in evidence but never change diagnostic pacing or
    campaign success.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        enabled: bool = True,
        timeout: float = TELEMETRY_TIMEOUT_S,
        max_pending: int = TELEMETRY_MAX_PENDING,
        client_factory=None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("telemetry max_pending must be positive")
        self.socket_path = socket_path
        self.enabled = enabled
        self.timeout = timeout
        self.max_pending = min(max_pending, len(TELEMETRY_METRICS))
        self.client_factory = client_factory or self._default_client_factory
        self._condition = threading.Condition()
        self._pending: dict[str, dict[str, object]] = {}
        self._thread: threading.Thread | None = None
        self._closing = False
        self._submitted = 0
        self._superseded = 0
        self._overflow_dropped = 0
        self._attempts = 0
        self._published = 0
        self._rejected = 0
        self._errors = 0
        self._last_error: str | None = None

    @staticmethod
    def _default_client_factory(socket_path: str, timeout: float):
        from projects.vehicle_data.api import TelemetryClient

        return TelemetryClient(socket_path, timeout=timeout)

    def start(self) -> None:
        if not self.enabled:
            return
        with self._condition:
            if self._thread is not None:
                return
            try:
                self._thread = threading.Thread(
                    target=self._run,
                    name="cluster-telemetry-publisher",
                    daemon=True,
                )
                self._thread.start()
            except Exception as exc:
                self._thread = None
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"

    def submit(self, metric: str, payload: dict[str, object]) -> None:
        if not self.enabled:
            return
        if metric not in TELEMETRY_METRICS:
            raise ValueError(f"refusing non-allowlisted telemetry metric {metric!r}")
        with self._condition:
            if self._closing:
                self._overflow_dropped += 1
                return
            self._submitted += 1
            if metric in self._pending:
                self._superseded += 1
            elif len(self._pending) >= self.max_pending:
                oldest = next(iter(self._pending))
                del self._pending[oldest]
                self._overflow_dropped += 1
            self._pending[metric] = dict(payload)
            self._condition.notify()

    def _run(self) -> None:
        try:
            client = self.client_factory(self.socket_path, self.timeout)
        except Exception as exc:
            with self._condition:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._pending.clear()
            return
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if not self._pending:
                    return
                metric = next(iter(self._pending))
                payload = self._pending.pop(metric)
            try:
                status, response = client.publish(metric, **payload)
                with self._condition:
                    self._attempts += 1
                    if 200 <= status < 300:
                        self._published += 1
                        self._last_error = None
                    else:
                        self._rejected += 1
                        self._last_error = (
                            f"HTTP {status}: "
                            f"{response.get('reason', 'publication_rejected')}"
                        )
            except Exception as exc:
                with self._condition:
                    self._attempts += 1
                    self._errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "enabled": self.enabled,
                "socket": self.socket_path,
                "timeout_seconds": self.timeout,
                "max_pending": self.max_pending,
                "submitted": self._submitted,
                "superseded": self._superseded,
                "overflow_dropped": self._overflow_dropped,
                "attempts": self._attempts,
                "published": self._published,
                "rejected": self._rejected,
                "errors": self._errors,
                "pending": len(self._pending),
                "thread_alive": bool(
                    self._thread is not None and self._thread.is_alive()
                ),
                "last_error": self._last_error,
            }

    def close(
        self, timeout: float = TELEMETRY_CLOSE_TIMEOUT_S
    ) -> dict[str, object]:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.snapshot()


class EvidenceMountGuard:
    """Permanently disable path publication after the external mount is lost."""

    def __init__(
        self,
        out_root: Path,
        raw_root: Path,
        required_mount: Path,
        expected_device: int,
        *,
        checker: Callable[..., int] | None = None,
    ) -> None:
        self.out_root = out_root
        self.raw_root = raw_root
        self.required_mount = required_mount
        self.expected_device = expected_device
        self.checker = checker or require_writable_mount
        self.lost_reason: str | None = None

    def check(self) -> None:
        if self.lost_reason is not None:
            raise DriveLogError(self.lost_reason)
        try:
            device = self.checker(
                self.out_root,
                self.required_mount,
                expected_device=self.expected_device,
            )
            if device != self.expected_device:
                raise DriveLogError(
                    f"external evidence mount device changed: "
                    f"{device} != {self.expected_device}"
                )
            raw_device = self.checker(
                self.raw_root,
                self.required_mount,
                expected_device=self.expected_device,
            )
            if raw_device != self.expected_device:
                raise DriveLogError(
                    f"external raw evidence mount device changed: "
                    f"{raw_device} != {self.expected_device}"
                )
        except BaseException as exc:
            self.lost_reason = (
                "external evidence mount is unavailable; all later path publication "
                f"is disabled: {type(exc).__name__}: {exc}"
            )
            raise DriveLogError(self.lost_reason) from exc


def guarded_atomic_write_json(
    guard: EvidenceMountGuard,
    path: Path,
    payload: dict[str, object],
) -> None:
    """Write JSON only while the original external mount identity is present."""
    guard.check()
    atomic_write_json(path, payload)


class RequestPacer:
    """Place the rate boundary immediately before the transport request call."""

    def __init__(
        self,
        deadline: float,
        health_check: Callable[[], None],
        *,
        interval: float = REQUEST_INTERVAL_S,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.deadline = deadline
        self.health_check = health_check
        self.interval = interval
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.last_request_call: float | None = None

    def __call__(self) -> None:
        self.health_check()
        if self.last_request_call is not None:
            not_before = self.last_request_call + self.interval
            while True:
                now = self.monotonic()
                remaining = not_before - now
                if remaining <= 0:
                    break
                deadline_remaining = self.deadline - now
                if deadline_remaining <= 0:
                    raise CampaignLimitReached(
                        "duration boundary reached before the next request"
                    )
                self.sleep(min(remaining, deadline_remaining))
        if self.monotonic() >= self.deadline:
            raise CampaignLimitReached(
                "duration boundary reached before the next request"
            )
        self.health_check()
        if self.monotonic() >= self.deadline:
            raise CampaignLimitReached(
                "duration boundary reached during the final health check"
            )
        self.last_request_call = self.monotonic()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def campaign_stamp() -> str:
    prefix = ACTIVE_PROFILE.key.replace("-", "_")
    return dt.datetime.now().strftime(f"{prefix}_drive_%Y%m%d_%H%M%S")


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def validate_profile_invariants() -> None:
    expected = (
        CHANNEL,
        BITRATE,
        "normal_29bits",
        ACTIVE_PROFILE.txid,
        ACTIVE_PROFILE.rxid,
    )
    observed = (
        MODULE.channel,
        MODULE.bitrate,
        MODULE.addressing_mode,
        MODULE.txid,
        MODULE.rxid,
    )
    if observed != expected:
        raise DriveLogError(
            f"{ACTIVE_PROFILE.key} registry no longer matches the reviewed drive profile: "
            f"observed={observed!r}, expected={expected!r}"
        )
    if set(EXPECTED_DATA_LENGTHS) != set(CLUSTER_DIDS):
        raise DriveLogError("reviewed DID lengths no longer match the fixed DID profile")
    if (
        not math.isfinite(ACTIVE_PROFILE.request_rate_hz)
        or ACTIVE_PROFILE.request_rate_hz <= 0
        or ACTIVE_PROFILE.request_rate_hz > 5
    ):
        raise DriveLogError("reviewed profile request rate must be within (0, 5] Hz")


def blocked_processes(proc_root: Path = Path("/proc")) -> list[str]:
    """Return known legacy/non-cooperating diagnostic programs already running."""
    matches: list[str] = []
    own_pid = os.getpid()
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise DriveLogError(f"cannot inspect running processes: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        except OSError as exc:
            raise DriveLogError(f"cannot inspect process {entry.name}: {exc}") from exc
        arguments = [item for item in raw.split(b"\0") if item]
        for argument in arguments:
            name = os.path.basename(os.fsdecode(argument))
            if name in BLOCKED_PROCESS_BASENAMES:
                matches.append(f"pid {entry.name}: {name}")
                break
    return sorted(matches)


def _service_active(
    name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    try:
        result = runner(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveLogError(
            f"cannot establish {name} state: {type(exc).__name__}: {exc}"
        ) from exc
    state = result.stdout.strip()
    if result.returncode == 0 and state == "active":
        return True
    if result.returncode in (3, 4) and state in {"inactive", "failed", "unknown"}:
        return False
    raise DriveLogError(
        f"cannot establish {name} state: rc={result.returncode}, "
        f"stdout={state!r}, stderr={result.stderr.strip()!r}"
    )


def query_interface_state(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> InterfaceState:
    try:
        result = runner(
            ["ip", "-details", "-statistics", "link", "show", CHANNEL],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveLogError(
            f"{CHANNEL} state query failed: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise DriveLogError(f"{CHANNEL} is missing or unreadable")
    return parse_interface_state(result.stdout)


def validate_active_interface(
    state: InterfaceState,
    *,
    baseline: InterfaceState | None = None,
) -> None:
    problems: list[str] = []
    if not state.up:
        problems.append(f"{CHANNEL} is not UP")
    if state.bitrate != BITRATE:
        problems.append(f"{CHANNEL} bitrate is {state.bitrate}, expected {BITRATE}")
    if state.listen_only:
        problems.append(f"{CHANNEL} became LISTEN-ONLY during active logging")
    if state.controller_state != "ERROR-ACTIVE":
        problems.append(
            f"{CHANNEL} controller state is {state.controller_state}, expected ERROR-ACTIVE"
        )
    for field in ("rx_dropped", "rx_missed"):
        value = getattr(state, field)
        if value is None:
            problems.append(f"{field} counter is unavailable")
        if baseline is not None:
            before = getattr(baseline, field)
            if before is None:
                problems.append(f"baseline {field} counter is unavailable")
            elif value is not None and value != before:
                problems.append(f"{field} changed from {before} to {value}")
    if problems:
        raise DriveLogError("interface safety check failed: " + "; ".join(problems))


def preflight_live(
    out_root: Path,
    raw_root: Path,
    required_mount: Path,
    soft_free_bytes: int,
    *,
    expected_device: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[int, InterfaceState, int]:
    """Verify all temporal live prerequisites without changing any of them."""
    validate_profile_invariants()
    errors: list[str] = []
    if uds is None:
        errors.append(UDS_IMPORT_ERROR or "can-isotp dependency is unavailable")
    else:
        errors.extend(diagnostic_preflight(CHANNEL, BITRATE))
    for executable in ("candump", "zstd"):
        if which(executable) is None:
            errors.append(f"required executable is missing: {executable}")
    try:
        maximum_receive_buffer = read_rmem_max()
        if maximum_receive_buffer < RECEIVE_BUFFER:
            errors.append(
                "net.core.rmem_max is too small for loss-accounted candump: "
                f"{maximum_receive_buffer} < {RECEIVE_BUFFER}; run "
                f"'sudo sysctl -w net.core.rmem_max={RECEIVE_BUFFER}' before the drive"
            )
    except Exception as exc:
        errors.append(str(exc))
    for service in BLOCKED_SERVICES:
        try:
            if _service_active(service, runner=runner):
                errors.append(f"{service} is active; stop it before this dedicated capture")
        except DriveLogError as exc:
            errors.append(str(exc))
    try:
        running = blocked_processes()
        if running:
            errors.append(
                "legacy/non-cooperating diagnostic process is running: "
                + ", ".join(running)
            )
    except DriveLogError as exc:
        errors.append(str(exc))
    try:
        device = require_writable_mount(
            out_root,
            required_mount,
            expected_device=expected_device,
        )
    except Exception as exc:
        errors.append(str(exc))
        device = -1
    try:
        require_writable_mount(
            raw_root,
            required_mount,
            expected_device=device if device >= 0 else expected_device,
        )
    except Exception as exc:
        errors.append(str(exc))
    try:
        free = min(available_bytes(out_root), available_bytes(raw_root))
        if free <= soft_free_bytes:
            errors.append(
                f"only {free} bytes free; live start requires more than the "
                f"{soft_free_bytes}-byte soft floor"
            )
    except Exception as exc:
        errors.append(str(exc))
        free = -1
    try:
        state = query_interface_state(runner=runner)
        validate_active_interface(state)
    except DriveLogError as exc:
        errors.append(str(exc))
        state = InterfaceState(False, None, False, None)
    if errors:
        raise DriveLogError("preflight failed:\n- " + "\n- ".join(errors))
    return device, state, free


class IgnitionWatcher:
    """Receive-only observer for the verified C-CAN ignition-presence frame."""

    def __init__(
        self,
        channel: str = CHANNEL,
        *,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self.channel = channel
        self.socket_factory = socket_factory
        self.sock: socket.socket | None = None
        self.last_seen_monotonic: float | None = None

    def open(self) -> None:
        sock = self.socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        try:
            can_filter = struct.pack(
                "=II",
                IGNITION_CAN_ID,
                CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_SFF_MASK,
            )
            sock.setsockopt(SOL_CAN_RAW, CAN_RAW_FILTER, can_filter)
            sock.bind((self.channel,))
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.sock = sock

    def poll(self) -> bool:
        if self.sock is None:
            raise DriveLogError("ignition observer is not open")
        seen = False
        while True:
            try:
                frame = self.sock.recv(16)
            except BlockingIOError:
                break
            except OSError as exc:
                raise DriveLogError(f"ignition observer failed: {exc}") from exc
            if not frame:
                break
            if len(frame) < 8:
                raise DriveLogError("ignition observer received a short CAN frame")
            can_id = struct.unpack_from("=I", frame)[0]
            if (
                not can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG)
                and can_id & CAN_SFF_MASK == IGNITION_CAN_ID
            ):
                self.last_seen_monotonic = time.monotonic()
                seen = True
        return seen

    def wait_for_first(self, timeout: float = IGNITION_START_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.last_seen_monotonic is not None:
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise DriveLogError(
            f"no 0x{IGNITION_CAN_ID:03X} ignition frame arrived within {timeout:g}s; "
            "start only while parked with ignition/engine on"
        )

    def ignition_lost(self, timeout: float = IGNITION_LOSS_TIMEOUT_S) -> bool:
        self.poll()
        return (
            self.last_seen_monotonic is not None
            and time.monotonic() - self.last_seen_monotonic >= timeout
        )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


def _set_parent_death_signal() -> None:
    """Ask Linux to terminate a recorder child if this Python owner disappears."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        os._exit(127)
    # Close the fork/prctl race: the parent could have died just before prctl.
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


class RawCapture:
    """Integrated loss-accounted full-bus recorder with bounded zstd chunks."""

    def __init__(
        self,
        run_dir: Path,
        *,
        rotation_seconds: int = RAW_ROTATION_SECONDS,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        candump: str | None = None,
        zstd: str | None = None,
        path_guard: Callable[[], None] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.rotation_seconds = rotation_seconds
        self.popen = popen
        self.runner = runner
        self.candump = candump or shutil.which("candump") or "candump"
        self.zstd = zstd or shutil.which("zstd") or "zstd"
        self.path_guard = path_guard or (lambda: None)
        self.stderr_path = run_dir / "runtime.stderr.log"
        self.manifest_path = run_dir / "manifest.jsonl"
        self.wire_partial_path = run_dir / f"{ACTIVE_PROFILE.wire_stem}.jsonl.partial"
        self.wire_path = run_dir / f"{ACTIVE_PROFILE.wire_stem}.jsonl"
        self.owner_path = run_dir / "owner.json"
        self.command = [
            self.candump,
            "-L",
            "-d",
            "-r",
            str(RECEIVE_BUFFER),
            CHANNEL,
        ]
        self.process: subprocess.Popen | None = None
        self.chunk: Chunk | None = None
        self.sequence = 0
        self.buffer = bytearray()
        self._stderr_handle = None
        self._wire_handle = None
        self.frame_count = 0
        self.first_timestamp: float | None = None
        self.last_timestamp: float | None = None
        self.detected_drops = 0
        self.chunk_records: list[dict[str, object]] = []
        self.wire_request_counts: Counter[str] = Counter()
        self.wire_positive_counts: Counter[str] = Counter()
        self.wire_classification_counts: Counter[str] = Counter()
        self.wire_negative_responses = 0
        self.wire_pending_responses = 0
        self.wire_other_endpoint_frames = 0
        self.wire_frame_sequence = 0
        self.ingest_stopped = False
        self.ingest_result: dict[str, object] | None = None
        self._finalizer: concurrent.futures.ThreadPoolExecutor | None = None
        self._pending_finalization: concurrent.futures.Future | None = None
        self._pending_submitted_at: float | None = None
        self._pending_sequence: int | None = None

    def _spawn(self, *args, **kwargs):
        # Keep recorder children out of Python's foreground process group. Python owns
        # Ctrl-C/TERM cleanup and signals/reaps them deliberately; PDEATHSIG still covers
        # an abrupt owner death.
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _set_parent_death_signal
        return self.popen(*args, **kwargs)

    def _verifier(self, path: Path) -> bool:
        self.path_guard()
        return verify_zstd_file(path, runner=self.runner, zstd=self.zstd)

    def _new_chunk(self) -> Chunk:
        self.path_guard()
        if self._stderr_handle is None:
            raise DriveLogError("raw stderr evidence is not open")
        return Chunk(
            self.run_dir,
            self.sequence,
            True,
            False,
            self._stderr_handle,
            popen=self._spawn,
            zstd=self.zstd,
        )

    def _write_owner(self, stage: str) -> None:
        self.path_guard()
        compressor_pid = (
            self.chunk.full.process.pid
            if self.chunk is not None and self.chunk.full is not None
            else None
        )
        atomic_write_json(
            self.owner_path,
            {
                "owner_pid": os.getpid(),
                "candump_pid": (
                    self.process.pid if self.process is not None else None
                ),
                "compressor_pid": compressor_pid,
                "active_chunk_sequence": (
                    self.chunk.sequence if self.chunk is not None else None
                ),
                "pending_finalization_sequence": self._pending_sequence,
                "parent_death_signal": "SIGTERM",
                "stage": stage,
                "updated_utc": utc_now(),
            },
        )

    def start(self) -> None:
        self.path_guard()
        self.run_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory(self.run_dir.parent)
        self._stderr_handle = self.stderr_path.open("xb", buffering=0)
        self._wire_handle = self.wire_partial_path.open("x", encoding="utf-8")
        try:
            self.process = self._spawn(
                self.command,
                stdout=subprocess.PIPE,
                stderr=self._stderr_handle,
                bufsize=0,
            )
            if self.process.stdout is None:
                raise DriveLogError("candump stdout pipe was not created")
            os.set_blocking(self.process.stdout.fileno(), False)
            self.chunk = self._new_chunk()
            self._write_owner("ingesting")
            self.path_guard()
            append_manifest(
                self.manifest_path,
                {
                    "type": "capture_start",
                    "time_utc": utc_now(),
                    "candump_command": self.command,
                    "rotation_seconds": self.rotation_seconds,
                    "compression": "zstd -1 -T1",
                    "scope": "full can0 including local TX loopback",
                },
            )
            time.sleep(0.05)
            self.assert_alive()
        except BaseException:
            self._abort_children()
            self._close_handles()
            raise

    def _decode_wire(
        self,
        timestamp_text: str,
        can_id: int,
        data: bytes,
    ) -> None:
        if self._wire_handle is None or can_id not in (MODULE.txid, MODULE.rxid):
            return
        direction = (
            f"tester_to_{MODULE.key}"
            if can_id == MODULE.txid
            else f"{MODULE.key}_to_tester"
        )
        classification = "non_single_frame"
        payload = b""
        did: int | None = None
        if data and data[0] & 0xF0 == 0:
            declared = data[0] & 0x0F
            if declared <= len(data) - 1:
                payload = data[1 : 1 + declared]
                classification = "other_single_frame"
                if (
                    can_id == MODULE.txid
                    and len(payload) == 3
                    and payload[0] == 0x22
                ):
                    did = int.from_bytes(payload[1:3], "big")
                    if did in EXPECTED_DATA_LENGTHS:
                        classification = "exact_request"
                        self.wire_request_counts[f"{did:04X}"] += 1
                elif (
                    can_id == MODULE.rxid
                    and len(payload) >= 3
                    and payload[0] == 0x62
                ):
                    did = int.from_bytes(payload[1:3], "big")
                    if (
                        did in EXPECTED_DATA_LENGTHS
                        and len(payload) == 3 + EXPECTED_DATA_LENGTHS[did]
                    ):
                        classification = "exact_positive_response"
                        self.wire_positive_counts[f"{did:04X}"] += 1
                    else:
                        classification = "malformed_positive_response"
                elif (
                    can_id == MODULE.rxid
                    and len(payload) == 3
                    and payload[:2] == bytes.fromhex("7F 22")
                ):
                    if payload[2] == 0x78:
                        classification = "response_pending"
                        self.wire_pending_responses += 1
                    else:
                        classification = "ambiguous_negative_response"
                        self.wire_negative_responses += 1
        if classification in {"non_single_frame", "other_single_frame"}:
            self.wire_other_endpoint_frames += 1
        self.wire_classification_counts[classification] += 1
        try:
            timestamp_epoch_us = int(
                Decimal(timestamp_text) * Decimal(1_000_000)
            )
        except (InvalidOperation, ValueError) as exc:
            raise DriveLogError(
                f"candump emitted invalid timestamp {timestamp_text!r}"
            ) from exc
        _append_jsonl(
            self._wire_handle,
            {
                "schema_version": 1,
                "type": "wire_frame",
                "sequence": self.wire_frame_sequence,
                "raw_line_sequence": self.frame_count - 1,
                "chunk_sequence": (
                    self.chunk.sequence if self.chunk is not None else None
                ),
                "timestamp_epoch_us": timestamp_epoch_us,
                "timestamp_text": timestamp_text,
                "timestamp_source": "candump_kernel",
                "can_id": f"{can_id:X}",
                "direction": direction,
                "module": MODULE.key,
                "can_data_hex": data.hex(" ").upper(),
                "isotp_payload_hex": payload.hex(" ").upper() if payload else None,
                "classification": classification,
                "did": f"{did:04X}" if did is not None else None,
            },
            fsync=False,
        )
        self.wire_frame_sequence += 1

    def _consume_line(self, line: bytes, *, fail_on_drop: bool) -> None:
        dropped = parse_drop_line(line)
        if dropped is not None:
            count, total = dropped
            self.detected_drops = max(self.detected_drops + count, total)
            self.path_guard()
            append_manifest(
                self.manifest_path,
                {
                    "type": "socket_drop",
                    "time_utc": utc_now(),
                    "dropped_frames": count,
                    "total_drops": total,
                },
            )
            if fail_on_drop:
                raise DriveLogError(
                    f"candump reported {count} dropped frames ({total} total)"
                )
            return
        timestamp, can_id = parse_candump_line(line)
        match = WIRE_RE.match(line)
        if timestamp is None or can_id is None or match is None:
            if line.strip():
                raise DriveLogError("candump emitted a malformed nonempty line")
            return
        try:
            data = bytes.fromhex(match.group("data").decode("ascii"))
        except ValueError as exc:
            raise DriveLogError("candump emitted malformed frame data") from exc
        if self.chunk is None:
            raise DriveLogError("candump data arrived without an active raw chunk")
        self.chunk.write(line, frozenset())
        self.frame_count += 1
        self.first_timestamp = timestamp if self.first_timestamp is None else self.first_timestamp
        self.last_timestamp = timestamp
        self._decode_wire(
            match.group("timestamp").decode("ascii"),
            can_id,
            data,
        )

    def _consume_bytes(self, data: bytes, *, fail_on_drop: bool) -> None:
        self.buffer.extend(data)
        while True:
            newline_at = self.buffer.find(b"\n")
            if newline_at < 0:
                return
            newline = newline_at + 1
            line = bytes(self.buffer[:newline])
            del self.buffer[:newline]
            self._consume_line(line, fail_on_drop=fail_on_drop)

    def drain(
        self,
        *,
        require_alive: bool = True,
        fail_on_drop: bool = True,
        allow_rotate: bool = True,
    ) -> int:
        if self.process is None or self.process.stdout is None:
            raise DriveLogError("candump was not started")
        consumed = 0
        while True:
            try:
                data = os.read(self.process.stdout.fileno(), 64 * 1024)
            except BlockingIOError:
                break
            if not data:
                break
            consumed += len(data)
            self._consume_bytes(data, fail_on_drop=fail_on_drop)
        if require_alive and self.process.poll() is not None:
            raise DriveLogError(
                f"candump exited unexpectedly with status {self.process.returncode}"
            )
        if allow_rotate:
            self._rotate_if_due()
        return consumed

    def _finish_chunk(self, chunk: Chunk) -> dict[str, object]:
        record = chunk.finish(self._verifier)
        return self._accept_chunk_record(record)

    def _accept_chunk_record(self, record: dict[str, object]) -> dict[str, object]:
        self.path_guard()
        append_manifest(self.manifest_path, record)
        self.chunk_records.append(record)
        if not record.get("complete"):
            raise DriveLogError(
                f"raw chunk {record.get('sequence')} failed compression validation"
            )
        return record

    def _validate_internal_accounting(self) -> dict[str, object]:
        sequences: list[int] = []
        chunk_frames = 0
        for record in self.chunk_records:
            try:
                sequence = int(record["sequence"])
                streams = record["streams"]
                if not isinstance(streams, dict):
                    raise TypeError("streams is not a mapping")
                full = streams["full"]
                if not isinstance(full, dict):
                    raise TypeError("full stream is not a mapping")
                frames = int(full["frames"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DriveLogError(
                    f"raw chunk accounting record is malformed: {record!r}"
                ) from exc
            if frames < 0:
                raise DriveLogError(
                    f"raw chunk {sequence} has negative frame count {frames}"
                )
            sequences.append(sequence)
            chunk_frames += frames
        expected_sequences = list(range(len(self.chunk_records)))
        if sequences != expected_sequences:
            raise DriveLogError(
                f"raw chunk sequences are not contiguous: "
                f"{sequences!r} != {expected_sequences!r}"
            )
        if chunk_frames != self.frame_count:
            raise DriveLogError(
                f"raw chunk frames {chunk_frames} != ingested frames {self.frame_count}"
            )
        classified_wire_frames = sum(self.wire_classification_counts.values())
        if classified_wire_frames != self.wire_frame_sequence:
            raise DriveLogError(
                f"{ACTIVE_PROFILE.key} wire classification count "
                f"{classified_wire_frames} != wire rows {self.wire_frame_sequence}"
            )
        return {
            "complete": True,
            "chunk_sequences": sequences,
            "chunk_full_stream_frames": chunk_frames,
            "ingested_frames": self.frame_count,
            "classified_wire_frames": classified_wire_frames,
            "wire_rows": self.wire_frame_sequence,
        }

    def _submit_chunk(self, chunk: Chunk) -> None:
        if self._pending_finalization is not None or self._finalizer is not None:
            raise DriveLogError("a raw chunk finalization is already pending")
        self._finalizer = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{ACTIVE_PROFILE.key}-raw-finalize",
        )
        self._pending_finalization = self._finalizer.submit(
            chunk.finish,
            self._verifier,
        )
        self._pending_submitted_at = time.monotonic()
        self._pending_sequence = chunk.sequence

    def _harvest_pending(self, *, wait: bool) -> None:
        future = self._pending_finalization
        if future is None:
            return
        submitted = self._pending_submitted_at
        elapsed = (
            time.monotonic() - submitted if submitted is not None else 0.0
        )
        if not wait and not future.done():
            if elapsed > MAX_PENDING_FINALIZATION_SECONDS:
                raise DriveLogError(
                    "raw chunk finalization exceeded "
                    f"{MAX_PENDING_FINALIZATION_SECONDS:g}s for sequence "
                    f"{self._pending_sequence}"
                )
            return
        timeout = max(0.0, MAX_PENDING_FINALIZATION_SECONDS - elapsed)
        try:
            record = future.result(timeout=timeout if wait else 0)
        except concurrent.futures.TimeoutError as exc:
            raise DriveLogError(
                "raw chunk finalization exceeded "
                f"{MAX_PENDING_FINALIZATION_SECONDS:g}s for sequence "
                f"{self._pending_sequence}"
            ) from exc
        finally:
            if future.done() and self._finalizer is not None:
                self._finalizer.shutdown(wait=True, cancel_futures=True)
                self._finalizer = None
        if not future.done():
            return
        self._pending_finalization = None
        self._pending_submitted_at = None
        self._pending_sequence = None
        self._accept_chunk_record(record)
        self._write_owner("ingest_stopped" if self.ingest_stopped else "ingesting")

    def _rotate_if_due(self) -> None:
        self._harvest_pending(wait=False)
        if self.chunk is None:
            return
        if time.monotonic() - self.chunk.started_monotonic < self.rotation_seconds:
            return
        if self._pending_finalization is not None:
            raise DriveLogError(
                "previous raw chunk is still finalizing at the next rotation"
            )
        finished = self.chunk
        self.sequence += 1
        self.chunk = self._new_chunk()
        try:
            self._submit_chunk(finished)
        except BaseException:
            finished.abort()
            raise
        self._write_owner("ingesting")

    def assert_alive(self) -> None:
        if self.process is None:
            raise DriveLogError("candump was not started")
        if self.process.poll() is not None:
            raise DriveLogError(
                f"candump exited unexpectedly with status {self.process.returncode}"
            )
        if (
            self.chunk is None
            or self.chunk.full is None
            or self.chunk.full.process.poll() is not None
        ):
            raise DriveLogError("active zstd raw compressor exited unexpectedly")
        self.drain()

    def wait_for_first_frame(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.assert_alive()
            if self.frame_count:
                return
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        raise DriveLogError(
            "integrated raw recorder did not persist a CAN frame before diagnostics"
        )

    def checkpoint(self) -> None:
        self.assert_alive()
        self._harvest_pending(wait=False)
        if self._wire_handle is not None:
            self._wire_handle.flush()
            os.fsync(self._wire_handle.fileno())

    def _abort_children(self) -> None:
        if self.chunk is not None:
            try:
                self.chunk.abort()
            except BaseException:
                pass
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except BaseException:
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except BaseException:
                    pass

    def _close_handles(self) -> None:
        if self.process is not None and self.process.stdout is not None:
            try:
                self.process.stdout.close()
            except BaseException:
                pass
        if self._wire_handle is not None:
            try:
                self._wire_handle.close()
            finally:
                self._wire_handle = None
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            finally:
                self._stderr_handle = None

    def stop_ingest(self) -> dict[str, object]:
        """Stop/reap candump and drain its tail without doing chunk verification."""
        if self.ingest_stopped:
            if self.ingest_result is None:
                raise DriveLogError("raw ingest stop state is inconsistent")
            return self.ingest_result
        if self.process is None:
            raise DriveLogError("candump was not started")
        process = self.process
        was_running = process.poll() is None
        forced: str | None = None
        try:
            if was_running:
                try:
                    process.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if not self.drain(
                    require_alive=False,
                    fail_on_drop=False,
                    allow_rotate=False,
                ):
                    time.sleep(0.01)
            if process.poll() is None:
                forced = "SIGTERM"
                process.terminate()
                deadline = time.monotonic() + 2
                while process.poll() is None and time.monotonic() < deadline:
                    if not self.drain(
                        require_alive=False,
                        fail_on_drop=False,
                        allow_rotate=False,
                    ):
                        time.sleep(0.01)
            if process.poll() is None:
                forced = "SIGKILL"
                process.kill()
            process.wait(timeout=2)
            while self.drain(
                require_alive=False,
                fail_on_drop=False,
                allow_rotate=False,
            ):
                pass
            if self.buffer:
                raise DriveLogError("candump ended with an unterminated raw line")
            if not was_running:
                raise DriveLogError(
                    f"candump had already exited unexpectedly with status {process.returncode}"
                )
            if forced is not None:
                raise DriveLogError(
                    f"candump required {forced}; raw tail integrity is not guaranteed"
                )
            if process.returncode not in (
                0,
                -signal.SIGINT,
                128 + signal.SIGINT,
            ):
                raise DriveLogError(
                    f"candump stopped with unexpected status {process.returncode}"
                )
            if self.detected_drops:
                raise DriveLogError(
                    f"candump reported {self.detected_drops} dropped frames"
                )
            if self.frame_count == 0:
                raise DriveLogError("candump captured no frames")
            if self._wire_handle is not None:
                self._wire_handle.flush()
                os.fsync(self._wire_handle.fileno())
                self._wire_handle.close()
                self._wire_handle = None
            self.ingest_stopped = True
            self.ingest_result = {
                "started": True,
                "returncode": process.returncode,
                "forced": False,
                "frames": self.frame_count,
                "detected_socket_drops": self.detected_drops,
                "first_timestamp": self.first_timestamp,
                "last_timestamp": self.last_timestamp,
            }
            # The local ``process`` reference remains available for the final pipe close,
            # but owner metadata must not advertise a reaped candump PID.
            self.process = None
            self._write_owner("ingest_stopped")
            return self.ingest_result
        except BaseException:
            self._abort_children()
            self._close_handles()
            raise
        finally:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except BaseException:
                    pass
            self.process = None

    def finalize(self) -> dict[str, object]:
        """Finalize/hash only after CAN has been restored passive and the lock released."""
        if not self.ingest_stopped or self.ingest_result is None:
            raise DriveLogError("raw ingest must stop cleanly before finalization")
        try:
            self.path_guard()
            self._harvest_pending(wait=True)
            if self.chunk is None:
                raise DriveLogError("raw capture ended without a current chunk")
            self._finish_chunk(self.chunk)
            self.chunk = None
            internal_accounting = self._validate_internal_accounting()
            self.path_guard()
            append_manifest(
                self.manifest_path,
                {
                    "type": "capture_stop",
                    "time_utc": utc_now(),
                    "frames": self.frame_count,
                    "chunks": len(self.chunk_records),
                    "detected_socket_drops": self.detected_drops,
                    "first_timestamp": self.first_timestamp,
                    "last_timestamp": self.last_timestamp,
                },
            )
            if self._stderr_handle is not None:
                self._stderr_handle.flush()
                os.fsync(self._stderr_handle.fileno())
                self._stderr_handle.close()
                self._stderr_handle = None
            if not self.wire_partial_path.is_file():
                raise DriveLogError(
                    f"raw wire partial is missing: {self.wire_partial_path}"
                )
            if self.wire_path.exists():
                raise DriveLogError(
                    f"refusing to overwrite raw wire evidence {self.wire_path}"
                )
            self.path_guard()
            os.replace(self.wire_partial_path, self.wire_path)
            fsync_directory(self.run_dir)
            self._write_owner("finalized")
            self.path_guard()
            return {
                **self.ingest_result,
                "complete": all(
                    bool(record.get("complete")) for record in self.chunk_records
                ),
                "run_dir": str(self.run_dir),
                "manifest": str(self.manifest_path),
                "manifest_sha256": sha256_file(self.manifest_path),
                "wire_jsonl": str(self.wire_path),
                "wire_sha256": sha256_file(self.wire_path),
                "stderr_log": str(self.stderr_path),
                "stderr_sha256": sha256_file(self.stderr_path),
                "chunks": len(self.chunk_records),
                "wire_request_counts": dict(sorted(self.wire_request_counts.items())),
                "wire_positive_counts": dict(sorted(self.wire_positive_counts.items())),
                "wire_classification_counts": dict(
                    sorted(self.wire_classification_counts.items())
                ),
                "wire_negative_responses": self.wire_negative_responses,
                "wire_pending_responses": self.wire_pending_responses,
                "wire_other_endpoint_frames": self.wire_other_endpoint_frames,
                "internal_accounting": internal_accounting,
            }
        except BaseException:
            self._abort_children()
            raise
        finally:
            self._close_handles()

    def close_after_ingest_failure(self) -> list[str]:
        """Best-effort offline cleanup after the interface is already passive."""
        errors: list[str] = []
        try:
            self._harvest_pending(wait=True)
        except BaseException as exc:
            errors.append(
                f"pending raw finalization cleanup failed: {type(exc).__name__}: {exc}"
            )
        if self.chunk is not None:
            try:
                self.chunk.abort()
            except BaseException as exc:
                errors.append(
                    f"active raw chunk abort failed: {type(exc).__name__}: {exc}"
                )
            self.chunk = None
        if self._finalizer is not None:
            self._finalizer.shutdown(wait=False, cancel_futures=True)
            self._finalizer = None
        self._close_handles()
        return errors

    def stop(self) -> dict[str, object]:
        """Convenience combined stop for tests; live cleanup uses the two phases."""
        self.stop_ingest()
        return self.finalize()


def classify_did_response(did: int, response: bytes | None) -> str:
    if response is None:
        return "timeout"
    response = bytes(response)
    expected = bytes((0x62, did >> 8, did & 0xFF))
    if len(response) >= 3 and response[:3] == expected:
        expected_length = EXPECTED_DATA_LENGTHS[did]
        return "positive" if len(response) == 3 + expected_length else "invalid_length"
    if len(response) == 3 and response[:2] == bytes.fromhex("7F 22"):
        return f"negative_{response[2]:02X}_ambiguous"
    return "unexpected"


def query_did(
    sock,
    did: int,
    timeout: float = REQUEST_TIMEOUT_S,
    *,
    before_request: Callable[[], None] | None = None,
) -> dict[str, object]:
    if uds is None:
        raise DriveLogError(UDS_IMPORT_ERROR or "can-isotp dependency is unavailable")
    request = bytes((0x22, did >> 8, did & 0xFF))
    started_epoch_us = time.time_ns() // 1000
    started = time.monotonic()
    uds.drain(sock)
    if before_request is not None:
        before_request()
    request_call_epoch_us = time.time_ns() // 1000
    transport_error = None
    try:
        response, status = uds.request(sock, request, timeout=timeout, retries=0)
    except Exception as exc:
        response = None
        status = "TRANSPORT_EXCEPTION"
        transport_error = f"{type(exc).__name__}: {exc}"
    completed_epoch_us = time.time_ns() // 1000
    response = bytes(response) if response else None
    category = (
        "transport_exception"
        if transport_error is not None
        else classify_did_response(did, response)
    )
    data = response[3:] if category in {"positive", "invalid_length"} else None
    return {
        "did": f"{did:04X}",
        "expected_data_length": EXPECTED_DATA_LENGTHS[did],
        "request_hex": uds.hx(request),
        "response_hex": uds.hx(response) if response else None,
        "data_hex": uds.hx(data) if data is not None else None,
        "category": category,
        "negative_response_assignment": (
            "ambiguous_after_pre_send_drain"
            if category.startswith("negative_")
            else None
        ),
        "transport_status": status,
        "transport_error": transport_error,
        "attempt_started_epoch_us": started_epoch_us,
        "request_call_epoch_us": request_call_epoch_us,
        "attempt_completed_epoch_us": completed_epoch_us,
        "timestamp_authority": "userspace_attempt_envelope; use raw wire timestamp",
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--profile",
        choices=sorted(DRIVE_PROFILES),
        default="cluster",
        help="fixed reviewed diagnostic profile (default: cluster)",
    )
    parser.add_argument(
        "--out-root",
        required=True,
        type=Path,
        help="absolute parent for DID evidence campaign output",
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        help="absolute parent for canonical tmp/captures/ccan campaign output",
    )
    parser.add_argument(
        "--require-mount",
        type=Path,
        help="exact writable mount containing --out-root (required live)",
    )
    parser.add_argument("--campaign", help="safe output directory name; default is timestamped")
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--soft-free-gib",
        type=float,
        default=DEFAULT_SOFT_FREE_BYTES / 1024**3,
        help="stop cleanly when available space reaches this floor",
    )
    parser.add_argument(
        "--hard-free-gib",
        type=float,
        default=DEFAULT_HARD_FREE_BYTES / 1024**3,
        help="fail if available space reaches this floor",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-driving-read-only", action="store_true")
    parser.add_argument("--confirm-started-parked", action="store_true")
    parser.add_argument("--confirm-no-other-diagnostics", action="store_true")
    parser.add_argument(
        "--confirm-tcm-thermal-correlation",
        action="store_true",
        help="required live acknowledgement for the opt-in TCM 04FE/0301 profile",
    )
    parser.add_argument("--pair")
    parser.add_argument("--conditions")
    parser.add_argument(
        "--telemetry-socket",
        default=DEFAULT_TELEMETRY_SOCKET,
        help=(
            "local telemetry-broker Unix socket for bounded best-effort dashboard "
            "publication"
        ),
    )
    parser.add_argument(
        "--no-telemetry-publish",
        action="store_true",
        help="disable best-effort publication without changing captured evidence",
    )
    return parser


def validate_args(args: argparse.Namespace) -> tuple[int, int]:
    if not args.out_root.is_absolute():
        raise DriveLogError("--out-root must be an absolute path")
    if args.raw_root is not None and not args.raw_root.is_absolute():
        raise DriveLogError("--raw-root must be an absolute path")
    if args.campaign and not CAMPAIGN_RE.fullmatch(args.campaign):
        raise DriveLogError(
            "--campaign must contain only letters, digits, dot, underscore, or dash"
        )
    if (
        not args.no_telemetry_publish
        and not Path(args.telemetry_socket).is_absolute()
    ):
        raise DriveLogError("--telemetry-socket must be an absolute Unix-socket path")
    if not 1 <= args.duration_seconds <= MAX_DURATION_SECONDS:
        raise DriveLogError(
            f"--duration-seconds must be between 1 and {MAX_DURATION_SECONDS}"
        )
    for label, value in (
        ("--soft-free-gib", args.soft_free_gib),
        ("--hard-free-gib", args.hard_free_gib),
    ):
        if not math.isfinite(value) or value < 0:
            raise DriveLogError(f"{label} must be finite and non-negative")
    soft = int(args.soft_free_gib * 1024**3)
    hard = int(args.hard_free_gib * 1024**3)
    if soft <= hard:
        raise DriveLogError("--soft-free-gib must be greater than --hard-free-gib")
    if args.execute:
        if not args.confirm_driving_read_only:
            raise DriveLogError("--execute requires --confirm-driving-read-only")
        if not args.confirm_started_parked:
            raise DriveLogError("--execute requires --confirm-started-parked")
        if not args.confirm_no_other_diagnostics:
            raise DriveLogError("--execute requires --confirm-no-other-diagnostics")
        if (
            ACTIVE_PROFILE.key == "tcm-thermal"
            and not args.confirm_tcm_thermal_correlation
        ):
            raise DriveLogError(
                "--profile tcm-thermal live use requires "
                "--confirm-tcm-thermal-correlation"
            )
        if (
            ACTIVE_PROFILE.key != "tcm-thermal"
            and args.confirm_tcm_thermal_correlation
        ):
            raise DriveLogError(
                "--confirm-tcm-thermal-correlation applies only to "
                "--profile tcm-thermal"
            )
        if args.pair != PAIR:
            raise DriveLogError(f"--execute requires the fixed verified --pair {PAIR}")
        if not args.conditions or not args.conditions.strip():
            raise DriveLogError("--execute requires non-empty --conditions")
        if args.require_mount is None:
            raise DriveLogError("--execute requires --require-mount")
        if args.raw_root is None:
            raise DriveLogError("--execute requires --raw-root")
    return soft, hard


def plan(args: argparse.Namespace, soft: int, hard: int) -> dict[str, object]:
    campaign = args.campaign or f"<{ACTIVE_PROFILE.key.replace('-', '_')}_drive_TIMESTAMP>"
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "active physical ReadDataByIdentifier plus same-owner raw observation",
        "profile": ACTIVE_PROFILE.key,
        "purpose": ACTIVE_PROFILE.purpose,
        "module": {
            "key": MODULE.key,
            "txid": f"{MODULE.txid:08X}",
            "rxid": f"{MODULE.rxid:08X}",
            "channel": MODULE.channel,
            "bitrate": MODULE.bitrate,
        },
        "diagnostic_session_policy": (
            "inherited/unknown; no DiagnosticSessionControl or TesterPresent"
        ),
        "request_payloads": [f"22 {did >> 8:02X} {did & 0xFF:02X}" for did in CLUSTER_DIDS],
        "maximum_total_request_rate_hz": ACTIVE_PROFILE.request_rate_hz,
        "duration_seconds": args.duration_seconds,
        "maximum_request_attempts": math.ceil(
            args.duration_seconds * ACTIVE_PROFILE.request_rate_hz
        ),
        "raw_capture": {
            "scope": "full can0 including local TX loopback",
            "format": (
                f"{RAW_ROTATION_SECONDS}-second zstd chunks with validated hashes, "
                f"manifest, and {MODULE.key}-endpoint wire JSONL"
            ),
            "command": [
                "candump",
                "-L",
                "-d",
                "-r",
                str(RECEIVE_BUFFER),
                CHANNEL,
            ],
        },
        "output": str(args.out_root / campaign),
        "raw_output": str(args.raw_root / campaign) if args.raw_root else None,
        "required_mount": str(args.require_mount) if args.require_mount else None,
        "soft_free_bytes": soft,
        "hard_free_bytes": hard,
        "telemetry_publication": {
            "enabled": not args.no_telemetry_publish,
            "socket": args.telemetry_socket,
            "policy": (
                "bounded best-effort latest-value publication; broker failure "
                "never stalls or fails CAN evidence capture"
            ),
            "metrics": sorted(telemetry_metrics_for_profile()),
        },
        "stop_policy": (
            f"duration, signal, disk floor, error, or {IGNITION_LOSS_TIMEOUT_S:g}s "
            f"without verified 0x{IGNITION_CAN_ID:03X}"
        ),
        "does_not": [
            (
                "send DiagnosticSessionControl or TesterPresent "
                "(the repeated 22 reads may refresh an inherited S3 timer)"
            ),
            "send DTC, routine, write, security, reset, IO-control, or functional requests",
            "retry requests",
            "wake, re-arm, or recover the interface",
            "control AlfaOBD, services, network, or proxy configuration",
        ],
    }


def _append_jsonl(handle, payload: dict[str, object], *, fsync: bool = False) -> None:
    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    handle.flush()
    if fsync:
        os.fsync(handle.fileno())


def _running_summary(
    *,
    campaign: str,
    args: argparse.Namespace,
    samples_path: Path,
    raw_capture: RawCapture,
    started_utc: str,
    baseline: InterfaceState,
    free_bytes: int,
    soft: int,
    hard: int,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "tool": "projects/ecu_mapping/cluster_drive_log.py",
        "status": "running",
        "campaign": campaign,
        "profile": ACTIVE_PROFILE.key,
        "purpose": ACTIVE_PROFILE.purpose,
        "started_utc": started_utc,
        "completed_utc": None,
        "interaction": "active fixed physical 22 reads plus integrated raw observation",
        "module": {
            "key": MODULE.key,
            "name": MODULE.name,
            "channel": MODULE.channel,
            "bus": MODULE.bus,
            "bitrate": MODULE.bitrate,
            "addressing_mode": MODULE.addressing_mode,
            "txid": f"{MODULE.txid:X}",
            "rxid": f"{MODULE.rxid:X}",
        },
        "physical_pair": args.pair,
        "conditions": args.conditions.strip(),
        "started_parked_asserted": args.confirm_started_parked,
        "driving_read_only_confirmed": args.confirm_driving_read_only,
        "no_other_diagnostics_confirmed": args.confirm_no_other_diagnostics,
        "diagnostic_session_policy": "inherited/unknown; no 10 or 3E sent",
        "dids": [f"{did:04X}" for did in CLUSTER_DIDS],
        "request_rate_limit_hz": ACTIVE_PROFILE.request_rate_hz,
        "duration_limit_seconds": args.duration_seconds,
        "request_limit": math.ceil(
            args.duration_seconds * ACTIVE_PROFILE.request_rate_hz
        ),
        "samples_jsonl": str(samples_path.name),
        "sample_timestamp_semantics": (
            f"userspace attempt envelopes; raw {MODULE.key} wire JSONL carries authoritative "
            "candump kernel timestamps"
        ),
        "raw_run_dir": str(raw_capture.run_dir),
        "raw_manifest": str(raw_capture.manifest_path),
        "raw_wire_jsonl": str(raw_capture.wire_path),
        "raw_command": raw_capture.command,
        "initial_interface": dataclasses.asdict(baseline),
        "free_bytes_at_start": free_bytes,
        "soft_free_bytes": soft,
        "hard_free_bytes": hard,
        "sample_cycles": 0,
        "request_attempts": 0,
        "responses_received": 0,
        "category_counts": {},
        "per_did_category_counts": {
            f"{did:04X}": {} for did in CLUSTER_DIDS
        },
        "startup_profile_validated": False,
        "stop_reason": None,
        "duration_complete": False,
        "ignition_seen": False,
        "interrupted": False,
        "fatal_errors": [],
        "raw_capture": None,
        "samples_sha256": None,
        "restored_passive": False,
        "telemetry_publication": {
            "enabled": not args.no_telemetry_publish,
            "socket": args.telemetry_socket,
            "submitted": 0,
            "published": 0,
            "errors": 0,
        },
    }


def validate_wire_evidence(
    raw_result: dict[str, object],
    attempt_counts: Counter[int],
    positive_counts: Counter[int],
    *,
    negative_count: int = 0,
    interrupted: bool = False,
    inflight_did: int | None = None,
) -> dict[str, object]:
    """Count-reconcile userspace outcomes with exact endpoint frames seen by candump."""
    wire_requests = dict(raw_result.get("wire_request_counts") or {})
    wire_positives = dict(raw_result.get("wire_positive_counts") or {})
    mismatches: list[str] = []
    for did in CLUSTER_DIDS:
        key = f"{did:04X}"
        attempted = attempt_counts[did]
        positive = positive_counts[did]
        observed_requests = int(wire_requests.get(key, 0))
        observed_positives = int(wire_positives.get(key, 0))
        request_delta = observed_requests - attempted
        positive_delta = observed_positives - positive
        inflight_allowance = interrupted and did == inflight_did
        request_ok = request_delta == 0 or (
            inflight_allowance and request_delta == 1
        )
        positive_ok = positive_delta == 0 or (
            inflight_allowance and positive_delta == 1
        )
        if not request_ok:
            mismatches.append(
                f"{key} request frames {observed_requests} != attempts {attempted}"
            )
        if not positive_ok:
            mismatches.append(
                f"{key} positive frames {observed_positives} != outcomes {positive}"
            )
        if positive_delta > request_delta:
            mismatches.append(
                f"{key} has more unpaired positive frames than request frames"
            )
    if not raw_result.get("complete"):
        mismatches.append("raw recorder did not report complete")
    if int(raw_result.get("detected_socket_drops", -1)) != 0:
        mismatches.append("raw recorder reported socket drops")
    if int(raw_result.get("chunks", 0)) < 1:
        mismatches.append("raw recorder finalized no chunks")
    observed_negatives = int(raw_result.get("wire_negative_responses", 0))
    negative_delta = observed_negatives - negative_count
    if negative_delta != 0 and not (
        interrupted and inflight_did is not None and negative_delta == 1
    ):
        mismatches.append(
            f"negative response frames {observed_negatives} != outcomes {negative_count}"
        )
    other_endpoint_frames = int(raw_result.get("wire_other_endpoint_frames", 0))
    if other_endpoint_frames:
        mismatches.append(
            f"raw recorder saw {other_endpoint_frames} unexplained "
            f"{MODULE.key} endpoint frames"
        )
    classifications = dict(raw_result.get("wire_classification_counts") or {})
    unexpected_classifications = {
        key: int(value)
        for key, value in classifications.items()
        if key
        not in {
            "exact_request",
            "exact_positive_response",
            "ambiguous_negative_response",
            "response_pending",
        }
        and int(value)
    }
    if unexpected_classifications:
        mismatches.append(
            f"unexpected {MODULE.key} wire classifications: "
            + ", ".join(
                f"{key}={value}"
                for key, value in sorted(unexpected_classifications.items())
            )
        )
    return {
        "complete": not mismatches,
        "mismatches": mismatches,
        "high_level_attempt_counts": {
            f"{did:04X}": attempt_counts[did] for did in CLUSTER_DIDS
        },
        "high_level_positive_counts": {
            f"{did:04X}": positive_counts[did] for did in CLUSTER_DIDS
        },
        "wire_request_counts": wire_requests,
        "wire_positive_counts": wire_positives,
        "high_level_negative_count": negative_count,
        "wire_negative_response_count": observed_negatives,
        "wire_pending_response_count": int(
            raw_result.get("wire_pending_responses", 0)
        ),
        "wire_other_endpoint_frames": other_endpoint_frames,
        "wire_classification_counts": classifications,
        "interrupted_inflight_allowance": (
            f"{inflight_did:04X}"
            if interrupted and inflight_did is not None
            else None
        ),
    }


def enforce_did_health(
    did: int,
    category: str,
    *,
    startup_profile_validated: bool,
    consecutive_failures: dict[int, int],
) -> None:
    """Update one DID's health and fail closed at the reviewed thresholds."""
    if category == "positive":
        consecutive_failures[did] = 0
        return
    consecutive_failures[did] += 1
    if category in {"unexpected", "invalid_length", "transport_exception"}:
        raise DriveLogError(
            f"DID {did:04X} produced fail-closed outcome {category}"
        )
    if not startup_profile_validated:
        raise DriveLogError(
            f"startup profile was not all-positive: DID {did:04X} returned {category}"
        )
    if consecutive_failures[did] >= MAX_CONSECUTIVE_DID_FAILURES:
        raise DriveLogError(
            f"DID {did:04X} failed "
            f"{MAX_CONSECUTIVE_DID_FAILURES} consecutive attempts"
        )


def execute(args: argparse.Namespace, soft: int, hard: int) -> int:
    campaign = args.campaign or campaign_stamp()
    out_root = args.out_root
    raw_root = args.raw_root
    required_mount = args.require_mount
    assert required_mount is not None and raw_root is not None
    telemetry = BestEffortTelemetryPublisher(
        args.telemetry_socket,
        enabled=not args.no_telemetry_publish,
    )

    mount_device, baseline, free = preflight_live(
        out_root,
        raw_root,
        required_mount,
        soft,
    )
    mount_guard = EvidenceMountGuard(
        out_root,
        raw_root,
        required_mount,
        mount_device,
    )
    try:
        lock_handle = diagnostic_safety.acquire_channel_lock(CHANNEL)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DriveLogError(f"refusing to start drive logger: {exc}") from None

    run_dir: Path | None = None
    raw_run_dir: Path | None = None
    samples_path: Path | None = None
    samples_handle = None
    sock = None
    watcher: IgnitionWatcher | None = None
    raw_capture: RawCapture | None = None
    report: dict[str, object] | None = None
    categories: Counter[str] = Counter()
    per_did_categories = {did: Counter() for did in CLUSTER_DIDS}
    attempt_counts: Counter[int] = Counter()
    positive_counts: Counter[int] = Counter()
    consecutive_failures = {did: 0 for did in CLUSTER_DIDS}
    request_attempts = 0
    responses_received = 0
    sample_cycles = 0
    request_records = 0
    startup_profile_validated = False
    last_runtime_check: float | None = None
    last_checkpoint: float | None = None
    stop_reason: str | None = None
    interrupted = False
    fatal_errors: list[str] = []
    restored_passive = False
    raw_started = False
    raw_ingest_result: dict[str, object] | None = None
    raw_result: dict[str, object] | None = None
    wire_validation: dict[str, object] | None = None
    final_interface: InterfaceState | None = None
    current_did: int | None = None
    lock_released = False
    samples_hash: str | None = None
    telemetry_report = telemetry.snapshot()
    last_ignition_publish_s: float | None = None
    last_published_ignition_seen_s: float | None = None
    started_utc = utc_now()

    def record_fatal(exc: BaseException | str) -> None:
        message = str(exc)
        if not message:
            message = type(exc).__name__ if isinstance(exc, BaseException) else "unknown error"
        fatal_errors.append(message)

    with diagnostic_safety.interrupt_on_termination() as termination:
        try:
            # Close the mount/service/interface race after reserving the channel.
            mount_device, baseline, free = preflight_live(
                out_root,
                raw_root,
                required_mount,
                soft,
                expected_device=mount_device,
            )
            mount_guard.expected_device = mount_device
            mount_guard.check()
            out_root.mkdir(parents=True, exist_ok=True)
            mount_guard.check()
            raw_root.mkdir(parents=True, exist_ok=True)
            run_dir = out_root / campaign
            raw_run_dir = raw_root / campaign
            if run_dir.exists():
                raise DriveLogError(f"campaign directory already exists: {run_dir}")
            if raw_run_dir.exists():
                raise DriveLogError(
                    f"raw campaign directory already exists: {raw_run_dir}"
                )
            mount_guard.check()
            run_dir.mkdir()
            fsync_directory(out_root)
            samples_path = run_dir / "samples.jsonl"
            raw_capture = RawCapture(raw_run_dir, path_guard=mount_guard.check)
            report = _running_summary(
                campaign=campaign,
                args=args,
                samples_path=samples_path,
                raw_capture=raw_capture,
                started_utc=started_utc,
                baseline=baseline,
                free_bytes=free,
                soft=soft,
                hard=hard,
            )
            guarded_atomic_write_json(mount_guard, run_dir / "run.json", report)
            guarded_atomic_write_json(mount_guard, run_dir / "summary.json", report)

            mount_guard.check()
            samples_handle = samples_path.open("x", encoding="utf-8")
            telemetry.start()
            raw_capture.start()
            raw_started = True
            watcher = IgnitionWatcher()
            watcher.open()
            watcher.wait_for_first()
            raw_capture.wait_for_first_frame()
            report["ignition_seen"] = True
            ignition_metric, ignition_payload = ignition_telemetry_observation()
            telemetry.submit(ignition_metric, ignition_payload)
            last_ignition_publish_s = time.monotonic()
            last_published_ignition_seen_s = watcher.last_seen_monotonic
            sock = uds.open_module_socket(MODULE, timeout=REQUEST_TIMEOUT_S)

            started = time.monotonic()
            deadline = started + args.duration_seconds
            last_runtime_check = started
            last_checkpoint = started
            request_limit = int(report["request_limit"])

            def raw_health_check() -> None:
                if raw_capture is None:
                    raise DriveLogError("raw capture disappeared before a request")
                raw_capture.assert_alive()

            pace_request = RequestPacer(
                deadline,
                raw_health_check,
                interval=1.0 / ACTIVE_PROFILE.request_rate_hz,
            )

            while time.monotonic() < deadline and request_attempts < request_limit:
                if raw_capture is None or watcher is None:
                    raise DriveLogError("internal capture components disappeared")
                raw_capture.assert_alive()
                if watcher.ignition_lost():
                    stop_reason = "ignition_frame_absent"
                    break
                now = time.monotonic()
                if (
                    watcher.last_seen_monotonic is not None
                    and watcher.last_seen_monotonic
                    != last_published_ignition_seen_s
                    and (
                        last_ignition_publish_s is None
                        or now - last_ignition_publish_s
                        >= TELEMETRY_IGNITION_INTERVAL_S
                    )
                ):
                    ignition_metric, ignition_payload = (
                        ignition_telemetry_observation()
                    )
                    telemetry.submit(ignition_metric, ignition_payload)
                    last_ignition_publish_s = now
                    last_published_ignition_seen_s = (
                        watcher.last_seen_monotonic
                    )

                cycle_sequence = sample_cycles
                cycle_started_us = time.time_ns() // 1000
                cycle_results = 0
                cycle_positives: list[str] = []
                boundary_reached = False
                for cycle_position, did in enumerate(CLUSTER_DIDS):
                    if time.monotonic() >= deadline or request_attempts >= request_limit:
                        boundary_reached = True
                        break
                    if watcher.ignition_lost():
                        stop_reason = "ignition_frame_absent"
                        break
                    current_did = did
                    try:
                        result = query_did(
                            sock,
                            did,
                            before_request=pace_request,
                        )
                    except CampaignLimitReached:
                        current_did = None
                        boundary_reached = True
                        break
                    attempt_sequence = request_attempts
                    request_attempts += 1
                    request_records += 1
                    cycle_results += 1
                    attempt_counts[did] += 1
                    result.update(
                        {
                            "schema_version": 2,
                            "type": "did_attempt",
                            "attempt_sequence": attempt_sequence,
                            "did_attempt_ordinal": attempt_counts[did] - 1,
                            "cycle_sequence": cycle_sequence,
                            "cycle_position": cycle_position,
                            "cycle_started_epoch_us": cycle_started_us,
                            "wire_join": (
                                "ordinal candidate only; final validation is count-level; "
                                "authoritative payloads/timestamps are in raw "
                                f"{ACTIVE_PROFILE.wire_stem}.jsonl"
                            ),
                        }
                    )
                    _append_jsonl(samples_handle, result, fsync=False)
                    raw_capture.assert_alive()
                    category = str(result["category"])
                    categories[category] += 1
                    per_did_categories[did][category] += 1
                    if result["response_hex"] is not None:
                        responses_received += 1
                    if category == "positive":
                        positive_counts[did] += 1
                        cycle_positives.append(f"{did:04X}")
                        data_hex = result["data_hex"]
                        if not isinstance(data_hex, str):
                            raise DriveLogError(
                                f"DID {did:04X} positive response lost its data"
                            )
                        observation = telemetry_observation_for_did(
                            did, bytes.fromhex(data_hex)
                        )
                        if observation is not None:
                            metric, payload = observation
                            telemetry.submit(metric, payload)
                    enforce_did_health(
                        did,
                        category,
                        startup_profile_validated=startup_profile_validated,
                        consecutive_failures=consecutive_failures,
                    )
                    current_did = None

                if cycle_results == len(CLUSTER_DIDS):
                    _append_jsonl(
                        samples_handle,
                        {
                            "schema_version": 2,
                            "type": "cycle_complete",
                            "cycle_sequence": cycle_sequence,
                            "cycle_started_epoch_us": cycle_started_us,
                            "cycle_completed_epoch_us": time.time_ns() // 1000,
                            "positive_dids": cycle_positives,
                        },
                        fsync=False,
                    )
                    sample_cycles += 1
                    if not startup_profile_validated:
                        if len(cycle_positives) != len(CLUSTER_DIDS):
                            raise DriveLogError(
                                "startup profile did not complete with "
                                f"{len(CLUSTER_DIDS)} positives"
                            )
                        startup_profile_validated = True
                        report["startup_profile_validated"] = True
                if stop_reason is not None:
                    break
                if boundary_reached:
                    stop_reason = (
                        "request_limit"
                        if request_attempts >= request_limit
                        else "duration_limit"
                    )
                    break

                now = time.monotonic()
                if now - last_runtime_check >= RUNTIME_CHECK_INTERVAL_S:
                    mount_guard.check()
                    current = query_interface_state()
                    validate_active_interface(current, baseline=baseline)
                    free = min(available_bytes(run_dir), available_bytes(raw_run_dir))
                    if free <= hard:
                        raise DriveLogError(
                            f"external disk reached hard free-space floor: {free} <= {hard}"
                        )
                    if free <= soft:
                        stop_reason = "disk_soft_free_floor"
                        break
                    raw_capture.assert_alive()
                    last_runtime_check = now

                if now - last_checkpoint >= CHECKPOINT_INTERVAL_S:
                    os.fsync(samples_handle.fileno())
                    raw_capture.checkpoint()
                    report.update(
                        {
                            "sample_cycles": sample_cycles,
                            "request_records": request_records,
                            "request_attempts": request_attempts,
                            "responses_received": responses_received,
                            "category_counts": dict(sorted(categories.items())),
                            "per_did_category_counts": {
                                f"{did:04X}": dict(
                                    sorted(per_did_categories[did].items())
                                )
                                for did in CLUSTER_DIDS
                            },
                            "startup_profile_validated": startup_profile_validated,
                            "telemetry_publication": telemetry.snapshot(),
                        }
                    )
                    guarded_atomic_write_json(
                        mount_guard,
                        run_dir / "summary.json",
                        report,
                    )
                    last_checkpoint = now

            if stop_reason is None:
                if time.monotonic() >= deadline:
                    stop_reason = "duration_limit"
                elif request_attempts >= request_limit:
                    stop_reason = "request_limit"
                else:
                    stop_reason = "complete"
        except KeyboardInterrupt:
            interrupted = True
            stop_reason = "interrupted"
        except BaseException as exc:
            stop_reason = "failed"
            record_fatal(f"{type(exc).__name__}: {exc}")
        finally:
            termination.begin_cleanup()
            if sock is not None:
                try:
                    sock.close()
                except BaseException as exc:
                    record_fatal(f"ISO-TP close failed: {type(exc).__name__}: {exc}")
            if watcher is not None:
                try:
                    watcher.close()
                except BaseException as exc:
                    record_fatal(
                        f"ignition observer close failed: {type(exc).__name__}: {exc}"
                    )
            if raw_capture is not None and raw_started:
                try:
                    raw_ingest_result = raw_capture.stop_ingest()
                except BaseException as exc:
                    record_fatal(
                        f"raw ingest stop failed: {type(exc).__name__}: {exc}"
                    )
            try:
                final_interface = query_interface_state()
                validate_active_interface(final_interface, baseline=baseline)
            except BaseException as exc:
                record_fatal(
                    f"final active interface check failed: {type(exc).__name__}: {exc}"
                )
            if samples_handle is not None:
                try:
                    samples_handle.flush()
                    os.fsync(samples_handle.fileno())
                except BaseException as exc:
                    record_fatal(f"sample fsync failed: {type(exc).__name__}: {exc}")
                finally:
                    try:
                        samples_handle.close()
                    except BaseException as exc:
                        record_fatal(
                            f"sample close failed: {type(exc).__name__}: {exc}"
                        )
            try:
                restored_passive = bool(canbus.restore_passive(CHANNEL, BITRATE))
            except BaseException as exc:
                record_fatal(f"passive restore failed: {type(exc).__name__}: {exc}")
            if not restored_passive:
                record_fatal("passive restoration could not be verified")
            try:
                diagnostic_safety.release_channel_lock(lock_handle)
                lock_released = True
            except BaseException as exc:
                record_fatal(
                    f"diagnostic lock release failed: {type(exc).__name__}: {exc}"
                )
            telemetry_report = telemetry.close()

            if termination.received_signal is not None:
                interrupted = True
                if stop_reason not in {"failed"}:
                    stop_reason = "interrupted"

            # The CAN interface is now safe and the lock outcome is known. Publish a small
            # checkpoint before the remaining bounded offline hashes/cross-checks.
            if run_dir is not None and report is not None:
                report.update(
                    {
                        "status": "finalizing",
                        "restored_passive": restored_passive,
                        "lock_released": lock_released,
                        "fatal_errors": fatal_errors,
                        "raw_ingest_stop": raw_ingest_result,
                    }
                )
                try:
                    guarded_atomic_write_json(
                        mount_guard,
                        run_dir / "summary.json",
                        report,
                    )
                except BaseException as exc:
                    record_fatal(
                        f"finalizing summary publication failed: "
                        f"{type(exc).__name__}: {exc}"
                    )

            if raw_capture is not None and raw_ingest_result is not None:
                try:
                    raw_result = raw_capture.finalize()
                except BaseException as exc:
                    record_fatal(
                        f"raw capture finalization failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            elif raw_capture is not None and raw_started:
                for cleanup_error in raw_capture.close_after_ingest_failure():
                    record_fatal(cleanup_error)
            if raw_result is not None:
                try:
                    wire_validation = validate_wire_evidence(
                        raw_result,
                        attempt_counts,
                        positive_counts,
                        negative_count=sum(
                            count
                            for category, count in categories.items()
                            if category.startswith("negative_")
                        ),
                        interrupted=interrupted,
                        inflight_did=current_did,
                    )
                    if not wire_validation["complete"]:
                        record_fatal(
                            "raw/high-level evidence mismatch: "
                            + "; ".join(wire_validation["mismatches"])
                        )
                except BaseException as exc:
                    record_fatal(
                        f"wire evidence validation failed: {type(exc).__name__}: {exc}"
                    )
            if samples_path is not None and samples_path.is_file():
                try:
                    mount_guard.check()
                    samples_hash = sha256_file(samples_path)
                except BaseException as exc:
                    record_fatal(
                        f"sample hash failed: {type(exc).__name__}: {exc}"
                    )

            if run_dir is not None and report is not None:
                report.update(
                    {
                        "status": (
                            "failed"
                            if fatal_errors
                            else "interrupted"
                            if interrupted
                            else "complete"
                        ),
                        "completed_utc": utc_now(),
                        "sample_cycles": sample_cycles,
                        "request_records": request_records,
                        "request_attempts": request_attempts,
                        "responses_received": responses_received,
                        "category_counts": dict(sorted(categories.items())),
                        "per_did_category_counts": {
                            f"{did:04X}": dict(
                                sorted(per_did_categories[did].items())
                            )
                            for did in CLUSTER_DIDS
                        },
                        "consecutive_failures_at_stop": {
                            f"{did:04X}": consecutive_failures[did]
                            for did in CLUSTER_DIDS
                        },
                        "startup_profile_validated": startup_profile_validated,
                        "stop_reason": stop_reason,
                        "duration_complete": stop_reason == "duration_limit",
                        "interrupted": interrupted,
                        "inflight_did_at_stop": (
                            f"{current_did:04X}" if current_did is not None else None
                        ),
                        "fatal_errors": fatal_errors,
                        "raw_capture": raw_result,
                        "wire_cross_validation": wire_validation,
                        "telemetry_publication": telemetry_report,
                        "samples_sha256": samples_hash,
                        "final_active_interface": (
                            dataclasses.asdict(final_interface)
                            if final_interface is not None
                            else None
                        ),
                        "restored_passive": restored_passive,
                        "lock_released": lock_released,
                    }
                )
                try:
                    guarded_atomic_write_json(
                        mount_guard,
                        run_dir / "summary.json",
                        report,
                    )
                except BaseException as exc:
                    record_fatal(
                        f"final summary publication failed: {type(exc).__name__}: {exc}"
                    )

    if run_dir is not None:
        print(f"campaign: {run_dir}")
        print(f"summary: {run_dir / 'summary.json'}")
    if raw_run_dir is not None:
        print(f"raw campaign: {raw_run_dir}")
    print(f"adapter restored passive: {'yes' if restored_passive else 'NO - CHECK IT NOW'}")
    if fatal_errors:
        for error in fatal_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 130 if interrupted else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        select_drive_profile(args.profile)
        soft, hard = validate_args(args)
        planned = plan(args, soft, hard)
        if not args.execute:
            print(json.dumps(planned, indent=2, sort_keys=True))
            print(
                "DRY RUN: no mount, service, subprocess, output, lock, CAN socket, "
                "interface, or transmission access occurred."
            )
            return 0
        return execute(args, soft, hard)
    except (DriveLogError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
