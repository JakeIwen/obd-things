#!/usr/bin/env python3
"""Receive-only raw recorder coordinated with the broker's active-drive owner.

This daemon never configures SocketCAN, acquires a diagnostic lock, or sends a
CAN frame.  It waits until the telemetry broker proves that its reviewed
active-drive helper owns the serial-resolved C-CAN channel, then attaches an independent receive socket
through ``candump`` and writes loss-accounted zstd chunks to the required
external mount.

The normal observer lock cannot be acquired during this interval: the broker
correctly holds the exclusive channel lock for its fixed PCM/RF-Hub requests.
Instead, this companion requires the broker's machine-readable ownership state
before accepting an armed interface.  It may continue receiving after the
broker restores listen-only mode so the same raw session reaches ignition loss.
Any armed state without the broker owner fails the capture.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api import TelemetryClient  # noqa: E402
from projects.vehicle_data.broker import DEFAULT_SOCKET  # noqa: E402
from lib.can_role_resolver import SysfsCanRoleResolver  # noqa: E402
from tools import passive_drive_capture as capture  # noqa: E402


BITRATE = 500_000
IGNITION_ID = 0x2EF
PAIR = "6/14"
DEFAULT_OUT_ROOT = (
    Path("/mnt/EXFAT512")
    / "obd-things"
    / "tmp"
    / "captures"
    / "ccan"
    / "broker-drive"
)
DEFAULT_REQUIRED_MOUNT = Path("/mnt/EXFAT512")
DEFAULT_STATE_PATH = REPO / "tmp" / "vehicle_data" / "drive-recorder-state.json"
DEFAULT_CONDITIONS = (
    "ordinary driving; broker-owned fixed PCM 01A1/06DA and RF Hub polling; "
    "serial-resolved receive-only C-CAN companion; no external diagnostic client"
)
WAIT_SECONDS = 1.0
STATUS_TIMEOUT_SECONDS = 2.0


class DriveRecorderError(RuntimeError):
    """A broker-ownership, interface, storage, or recorder gate failed."""


class BrokerOwnershipLost(DriveRecorderError):
    """The active-drive epoch ended during a bounded recorder start race."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def campaign_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"broker-drive-{stamp}"


def read_broker_status(
    client: TelemetryClient,
) -> dict[str, object]:
    status_code, payload = client.request("GET", "/v1/status")
    if status_code != 200:
        raise DriveRecorderError(
            f"broker status returned HTTP {status_code}"
        )
    if payload.get("service") != "van-telemetry":
        raise DriveRecorderError("broker status did not identify van-telemetry")
    return payload


def broker_armed_ready(
    status: object, *, expected_channel: str | None = None
) -> bool:
    """Whether one status object proves the reviewed broker owns active C-CAN."""
    if not isinstance(status, dict):
        return False
    active = status.get("active_drive")
    owner = status.get("current_owner")
    interface = status.get("interface")
    vehicle = status.get("vehicle_state")
    if not all(
        isinstance(item, dict)
        for item in (active, owner, interface, vehicle)
    ):
        return False
    topology = interface.get("topology")
    inhibits = interface.get("active_inhibits")
    channel = interface.get("channel")
    role_snapshot = interface.get("role_interfaces")
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    ccan = roles.get("c-can") if isinstance(roles, dict) else None
    expected = ccan.get("expected") if isinstance(ccan, dict) else None
    usb_serial = expected.get("usb_serial") if isinstance(expected, dict) else None
    dev_id = expected.get("dev_id") if isinstance(expected, dict) else None
    return bool(
        active.get("enabled") is True
        and active.get("state") == "armed_diagnostic"
        and active.get("reason") == "running_gate_satisfied"
        and active.get("interface_mode") == "armed_diagnostic"
        and active.get("restoration_failed") is False
        and isinstance(active.get("helper_pid"), int)
        and not isinstance(active.get("helper_pid"), bool)
        and owner.get("kind") == "broker_active_drive"
        and isinstance(channel, str)
        and re.fullmatch(r"can[0-9]+", channel)
        and (expected_channel is None or channel == expected_channel)
        and isinstance(ccan, dict)
        and ccan.get("channel") == channel
        and isinstance(usb_serial, str)
        and bool(usb_serial)
        and isinstance(dev_id, int)
        and not isinstance(dev_id, bool)
        and dev_id >= 0
        and interface.get("adapter_present") is True
        and interface.get("up") is True
        and interface.get("bitrate") == BITRATE
        and interface.get("listen_only") is False
        and interface.get("controller_state") == "ERROR-ACTIVE"
        and isinstance(topology, dict)
        and topology.get("usable") is True
        and topology.get("bus") == "c-can"
        and topology.get("pair") == PAIR
        and not inhibits
        and vehicle.get("running") is True
        and vehicle.get("basis") == "qualified_ccan_0x0fc_engine_speed"
    )


def broker_c_can_route(
    status: dict[str, object],
) -> tuple[str, str, int]:
    """Return the broker-proven channel and immutable USB identity."""
    if not broker_armed_ready(status):
        raise BrokerOwnershipLost("broker does not prove an armed C-CAN route")
    interface = status["interface"]
    assert isinstance(interface, dict)
    channel = interface["channel"]
    assert isinstance(channel, str)
    role_snapshot = interface.get("role_interfaces")
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    ccan = roles.get("c-can") if isinstance(roles, dict) else None
    expected = ccan.get("expected") if isinstance(ccan, dict) else None
    serial = expected.get("usb_serial") if isinstance(expected, dict) else None
    dev_id = expected.get("dev_id") if isinstance(expected, dict) else None
    if (
        not isinstance(serial, str)
        or not serial
        or not isinstance(dev_id, int)
        or isinstance(dev_id, bool)
        or dev_id < 0
    ):
        raise BrokerOwnershipLost("broker C-CAN USB identity is invalid")
    return channel, serial, dev_id


def query_interface(
    *,
    channel: str,
    expected_usb_serial: str | None = None,
    expected_dev_id: int | None = None,
    role_resolver: SysfsCanRoleResolver | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> capture.InterfaceState:
    if not isinstance(channel, str) or not re.fullmatch(r"can[0-9]+", channel):
        raise DriveRecorderError("C-CAN route is not a resolved kernel canN")
    if (expected_usb_serial is None) != (expected_dev_id is None):
        raise DriveRecorderError("C-CAN USB identity is incomplete")
    if expected_usb_serial is not None:
        resolver = role_resolver or SysfsCanRoleResolver()
        inventory, _issues = resolver.inventory(drivers=("gs_usb",))
        matches = [
            item
            for item in inventory
            if item.usb_vid == "1d50"
            and item.usb_pid == "606f"
            and item.usb_serial == expected_usb_serial
            and item.dev_id == expected_dev_id
        ]
        if not (
            len(matches) == 1
            and matches[0].channel == channel
        ):
            raise DriveRecorderError(
                f"{channel} no longer matches the broker-proven C-CAN USB identity"
            )
    try:
        result = runner(
            ["ip", "-details", "-statistics", "link", "show", channel],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveRecorderError(
            f"cannot inspect {channel}: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise DriveRecorderError(f"{channel} is missing or unreadable")
    state = capture.parse_interface_state(result.stdout, channel=channel)
    errors = []
    if not state.up:
        errors.append("interface is not UP")
    if state.bitrate != BITRATE:
        errors.append(f"bitrate is {state.bitrate}, expected {BITRATE}")
    if state.controller_state != "ERROR-ACTIVE":
        errors.append(
            f"controller is {state.controller_state}, expected ERROR-ACTIVE"
        )
    if state.rx_dropped is None or state.rx_missed is None:
        errors.append("RX dropped/missed counters are unavailable")
    if errors:
        raise DriveRecorderError(
            f"{channel} receive gate failed: " + "; ".join(errors)
        )
    return state


class CoordinatedSafetyCheck:
    """Accept passive mode, or armed mode only under the broker's exact owner."""

    def __init__(
        self,
        client: TelemetryClient,
        *,
        channel: str,
        expected_usb_serial: str | None = None,
        expected_dev_id: int | None = None,
        interface_reader: Callable[[], capture.InterfaceState] | None = None,
    ) -> None:
        self.client = client
        self.channel = channel
        self.expected_usb_serial = expected_usb_serial
        self.expected_dev_id = expected_dev_id
        self.interface_reader = interface_reader or (
            lambda: query_interface(
                channel=self.channel,
                expected_usb_serial=self.expected_usb_serial,
                expected_dev_id=self.expected_dev_id,
            )
        )

    def __call__(self) -> capture.InterfaceState:
        interface = self.interface_reader()
        if interface.listen_only:
            return interface
        try:
            status = read_broker_status(self.client)
        except Exception as exc:
            raise DriveRecorderError(
                "armed interface cannot be attributed to the broker: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not broker_armed_ready(status, expected_channel=self.channel):
            raise DriveRecorderError(
                "armed interface is not owned by the reviewed broker active-drive interval"
            )
        return interface


class InitialArmedSafetyCheck(CoordinatedSafetyCheck):
    """Require broker-owned armed mode once, then allow verified restoration."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.initial_armed_gate_passed = False

    def __call__(self) -> capture.InterfaceState:
        if self.initial_armed_gate_passed:
            return super().__call__()
        interface = self.interface_reader()
        try:
            status = read_broker_status(self.client)
        except Exception as exc:
            raise BrokerOwnershipLost(
                "broker status disappeared during the initial armed gate: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if interface.listen_only or not broker_armed_ready(
            status, expected_channel=self.channel
        ):
            raise BrokerOwnershipLost(
                "broker-owned armed mode disappeared during recorder startup"
            )
        self.initial_armed_gate_passed = True
        return interface


def priority_ids() -> frozenset[int]:
    parser = capture.build_parser()
    args = parser.parse_args(["--out-root", "/tmp/plan"])
    return capture.resolved_priority_ids(args)


def validate_dependencies(
    out_root: Path,
    policy: capture.DiskPolicy,
    safety_check: Callable[[], capture.InterfaceState],
    *,
    which: Callable[[str], str | None] = shutil.which,
    disk_free: Callable[[Path], int] = capture.available_bytes,
    rmem_max: Callable[[], int] = capture.read_rmem_max,
) -> tuple[capture.InterfaceState, int]:
    errors = []
    for executable in ("candump", "zstd"):
        if which(executable) is None:
            errors.append(f"required executable is missing: {executable}")
    try:
        maximum = rmem_max()
        if maximum < capture.RECEIVE_BUFFER:
            errors.append(
                "net.core.rmem_max is too small: "
                f"{maximum} < {capture.RECEIVE_BUFFER}"
            )
    except Exception as exc:
        errors.append(str(exc))
    try:
        interface = safety_check()
    except Exception as exc:
        errors.append(str(exc))
        interface = capture.InterfaceState(False, None, False, None)
    free = disk_free(out_root)
    if policy.action(free) == "stop":
        errors.append(
            f"only {free} bytes free, at/below hard floor "
            f"{policy.hard_free_bytes}"
        )
    if errors:
        raise DriveRecorderError(
            "armed-recorder preflight failed:\n- " + "\n- ".join(errors)
        )
    return interface, free


def write_state(path: Path, **payload: object) -> None:
    capture.atomic_write_json(
        path,
        {
            "updated_utc": utc_now(),
            **payload,
        },
    )


def record_one_interval(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    initial_status: dict[str, object],
    client: TelemetryClient,
) -> Path:
    """Start one raw session without changing or locking the CAN interface."""
    if not broker_armed_ready(initial_status):
        raise BrokerOwnershipLost(
            "broker ownership disappeared before raw capture setup"
        )
    channel, usb_serial, dev_id = broker_c_can_route(initial_status)
    mount_device = capture.require_writable_mount(
        args.out_root,
        args.require_mount,
    )
    initial_safety_check = InitialArmedSafetyCheck(
        client,
        channel=channel,
        expected_usb_serial=usb_serial,
        expected_dev_id=dev_id,
    )
    interface, free = validate_dependencies(
        args.out_root,
        policy,
        initial_safety_check,
    )
    capture.require_writable_mount(
        args.out_root,
        args.require_mount,
        expected_device=mount_device,
    )
    if not broker_armed_ready(
        read_broker_status(client), expected_channel=channel
    ):
        raise BrokerOwnershipLost(
            "broker ownership disappeared during raw capture preflight"
        )

    args.out_root.mkdir(parents=True, exist_ok=True)
    run_id = campaign_id()
    run_dir = args.out_root / run_id
    if run_dir.exists():
        raise DriveRecorderError(f"campaign directory already exists: {run_dir}")
    run_dir.mkdir()
    selected_ids = priority_ids()
    metadata = {
        "type": "run_metadata",
        "created_utc": utc_now(),
        "campaign": run_id,
        "conditions": args.conditions.strip(),
        "interaction": "receive_only_broker_armed_companion",
        "channel": channel,
        "usb_serial": usb_serial,
        "dev_id": dev_id,
        "bitrate": BITRATE,
        "initial_interface": dataclasses.asdict(interface),
        "initial_broker_status": initial_status,
        "capture_started_mid_running_epoch": True,
        "required_mount": str(args.require_mount.resolve()),
        "free_bytes_at_preflight": free,
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "stop_after_id": f"0x{IGNITION_ID:X}",
        "stop_after_id_absence_seconds": args.ignition_absence_seconds,
        "priority_profile": "ccan-correlation",
        "priority_ids": [f"0x{value:X}" for value in sorted(selected_ids)],
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "net_core_rmem_max": capture.read_rmem_max(),
        "does_not": [
            "configure or restore CAN",
            "acquire or release a CAN channel lock",
            "transmit CAN",
            "control the telemetry broker",
        ],
    }
    capture.atomic_write_json(run_dir / "run.json", metadata)
    write_state(
        args.state_path,
        status="recording",
        campaign=run_id,
        capture_dir=str(run_dir),
        interface_mode=(
            "listen_only" if interface.listen_only else "armed_diagnostic"
        ),
    )
    recorder_safety_check = InitialArmedSafetyCheck(
        client,
        channel=channel,
        expected_usb_serial=usb_serial,
        expected_dev_id=dev_id,
    )
    recorder = capture.Recorder(
        run_dir,
        selected_ids,
        args.rotation_seconds,
        args.duration_seconds,
        policy,
        stop_after_id=IGNITION_ID,
        stop_after_id_absence_seconds=args.ignition_absence_seconds,
        required_start_id=IGNITION_ID,
        required_start_id_timeout_seconds=5.0,
        safety_check=recorder_safety_check,
        mount_check=lambda: capture.require_writable_mount(
            args.out_root,
            args.require_mount,
            expected_device=mount_device,
        ),
        zstd=shutil.which("zstd") or "zstd",
        candump=shutil.which("candump") or "candump",
        candump_extra_args=("-D",),
        channel=channel,
    )
    try:
        with capture.campaign_file_lock(run_dir):
            recorder.run()
    except capture.CaptureError as exc:
        if "required start CAN ID" in str(exc):
            raise BrokerOwnershipLost(str(exc)) from exc
        raise
    return run_dir


def run_daemon(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    *,
    client: TelemetryClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    client = client or TelemetryClient(
        args.socket,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    last_wait_detail = None
    while True:
        try:
            status = read_broker_status(client)
            ready = broker_armed_ready(status)
            detail = (
                "waiting for reviewed broker active-drive ownership"
                if not ready
                else None
            )
        except Exception as exc:
            status = None
            ready = False
            detail = (
                "broker status unavailable; waiting without opening CAN: "
                f"{type(exc).__name__}: {exc}"
            )
        if not ready:
            if detail != last_wait_detail:
                write_state(
                    args.state_path,
                    status="waiting",
                    detail=detail,
                    output_root=str(args.out_root),
                )
                print(f"{utc_now()} {detail}", flush=True)
                last_wait_detail = detail
            sleep(WAIT_SECONDS)
            continue
        last_wait_detail = None
        assert isinstance(status, dict)
        try:
            run_dir = record_one_interval(
                args,
                policy,
                status,
                client,
            )
        except BrokerOwnershipLost as exc:
            write_state(
                args.state_path,
                status="waiting",
                detail=str(exc),
                output_root=str(args.out_root),
            )
            print(f"{utc_now()} {exc}; waiting", flush=True)
            sleep(WAIT_SECONDS)
            continue
        except Exception as exc:
            write_state(
                args.state_path,
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise DriveRecorderError(str(exc)) from exc
        write_state(
            args.state_path,
            status="waiting",
            detail="previous broker-owned drive capture finalized successfully",
            last_capture_dir=str(run_dir),
            output_root=str(args.out_root),
        )
        print(
            f"{utc_now()} finalized {run_dir.name}; waiting for next active interval",
            flush=True,
        )
        sleep(WAIT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--confirm-broker-owned-receive-only",
        action="store_true",
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument(
        "--require-mount",
        type=Path,
        default=DEFAULT_REQUIRED_MOUNT,
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--conditions", default=DEFAULT_CONDITIONS)
    parser.add_argument(
        "--rotation-seconds",
        type=int,
        default=capture.DEFAULT_ROTATION_SECONDS,
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=capture.DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--ignition-absence-seconds",
        type=float,
        default=capture.DEFAULT_STOP_ID_ABSENCE_SECONDS,
    )
    parser.add_argument(
        "--soft-free-gib",
        type=float,
        default=capture.DEFAULT_SOFT_FREE_BYTES / 1024**3,
    )
    parser.add_argument(
        "--hard-free-gib",
        type=float,
        default=capture.DEFAULT_HARD_FREE_BYTES / 1024**3,
    )
    return parser


def validate_args(args: argparse.Namespace) -> capture.DiskPolicy:
    if args.execute and not args.confirm_broker_owned_receive_only:
        raise DriveRecorderError(
            "--execute requires --confirm-broker-owned-receive-only"
        )
    for label, path in (
        ("--out-root", args.out_root),
        ("--require-mount", args.require_mount),
        ("--state-path", args.state_path),
    ):
        if not path.is_absolute():
            raise DriveRecorderError(f"{label} must be absolute")
    if not args.socket.startswith("/"):
        raise DriveRecorderError("--socket must be an absolute Unix path")
    if not args.conditions.strip():
        raise DriveRecorderError("--conditions cannot be empty")
    if args.rotation_seconds < 10:
        raise DriveRecorderError("--rotation-seconds must be at least 10")
    if not 1 <= args.duration_seconds <= 48 * 60 * 60:
        raise DriveRecorderError(
            "--duration-seconds must be between 1 and 172800"
        )
    if (
        not math.isfinite(args.ignition_absence_seconds)
        or not 5 <= args.ignition_absence_seconds <= 300
    ):
        raise DriveRecorderError(
            "--ignition-absence-seconds must be between 5 and 300"
        )
    return capture.DiskPolicy(
        soft_free_bytes=int(args.soft_free_gib * 1024**3),
        hard_free_bytes=int(args.hard_free_gib * 1024**3),
    )


def plan(args: argparse.Namespace, policy: capture.DiskPolicy) -> dict[str, object]:
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "receive_only_broker_armed_companion",
        "trigger": "broker active_drive state=armed_diagnostic",
        "channel": "broker-resolved c-can",
        "bitrate": BITRATE,
        "output_root": str(args.out_root),
        "required_mount": str(args.require_mount),
        "state_path": str(args.state_path),
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "stop_after_id": f"0x{IGNITION_ID:X}",
        "stop_after_id_absence_seconds": args.ignition_absence_seconds,
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "does_not": [
            "configure or restore CAN",
            "acquire or release a CAN channel lock",
            "transmit CAN",
            "control the telemetry broker",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = validate_args(args)
        if not args.execute:
            print(json.dumps(plan(args, policy), indent=2, sort_keys=True))
            return 0
        return run_daemon(args, policy)
    except (DriveRecorderError, capture.CaptureError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
