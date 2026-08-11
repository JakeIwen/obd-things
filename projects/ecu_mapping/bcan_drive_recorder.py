#!/usr/bin/env python3
"""Persistent, passive B-CAN awake-interval recorder.

This daemon never configures SocketCAN and never transmits.  Its systemd unit
establishes the passive 125 kbit/s baseline before starting it.  The daemon
takes a shared CAN observer lock for each identity probe, retains that same
lock without a gap while recording a verified awake interval, and releases it
while the bus is asleep so a guarded voltage monitor may briefly own an active
wake.  It automatically re-arms after 0x46C has been absent long enough for
the awake interval to be considered complete.

An awake interval is deliberately broader than a drive: a fob wake or other
body-network activity may also be retained.  That bias avoids missing the
beginning of a drive and preserves useful wake/sleep evidence.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state, canbus, diagnostic_safety  # noqa: E402
from lib.modules import MODULES  # noqa: E402
from tools import passive_drive_capture as capture  # noqa: E402


CHANNEL = "can0"
BITRATE = canbus.BITRATE_BCAN
PAIR = "3/11"
TRACKED_ID = 0x46C
MIN_SIGNATURE_HITS = 3
DEFAULT_PROBE_SECONDS = 3.0
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_OUT_ROOT = (
    Path("/mnt/EXFAT512")
    / "obd-things"
    / "tmp"
    / "captures"
    / "bcan"
    / "auto-drive"
)
DEFAULT_REQUIRED_MOUNT = Path("/mnt/EXFAT512")
DEFAULT_STATE_PATH = REPO / "tmp" / "ecu_mapping" / "bcan-drive-recorder-state.json"
DEFAULT_CONDITIONS = (
    "PCAN on labeled B-CAN DLC pins 3/11; passive 125 kbit/s; ordinary vehicle use; "
    "every verified B-CAN awake interval retained"
)
TOPOLOGY_SOURCE = "bcan_auto_recorder_passive_signature"


class BcanRecorderError(RuntimeError):
    """A B-CAN identity, interface, storage, or recorder gate failed."""


class BcanStartRace(BcanRecorderError):
    """The verified B-CAN awake interval ended while the recorder was starting."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def campaign_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"bcan-auto-{stamp}"


def priority_ids() -> frozenset[int]:
    selected = set(canbus.BCAN_SIG)
    for module in MODULES.values():
        if module.bus == "b-can":
            selected.update((module.txid, module.rxid))
    return frozenset(selected)


def signature_hits(ids: set[int] | frozenset[int]) -> frozenset[int]:
    return frozenset(ids & canbus.BCAN_SIG)


def bcan_signature_ready(ids: set[int] | frozenset[int], rx_error_delta: int) -> bool:
    return (
        0 <= rx_error_delta < canbus.RX_ERR_ABORT
        and len(signature_hits(ids)) >= MIN_SIGNATURE_HITS
    )


def query_interface(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> capture.InterfaceState:
    try:
        result = runner(
            ["ip", "-details", "-statistics", "link", "show", CHANNEL],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BcanRecorderError(
            f"cannot inspect {CHANNEL}: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise BcanRecorderError(f"{CHANNEL} is missing or unreadable")
    state = capture.parse_interface_state(result.stdout)
    errors = []
    if not state.up:
        errors.append("interface is not UP")
    if state.bitrate != BITRATE:
        errors.append(f"bitrate is {state.bitrate}, expected {BITRATE}")
    if not state.listen_only:
        errors.append("interface is not LISTEN-ONLY")
    if state.controller_state != "ERROR-ACTIVE":
        errors.append(
            f"controller is {state.controller_state}, expected ERROR-ACTIVE"
        )
    if state.rx_dropped is None or state.rx_missed is None:
        errors.append("RX dropped/missed counters are unavailable")
    if errors:
        raise BcanRecorderError(
            f"{CHANNEL} passive B-CAN gate failed: " + "; ".join(errors)
        )
    return state


def validate_dependencies(
    out_root: Path,
    policy: capture.DiskPolicy,
    *,
    which: Callable[[str], str | None] = shutil.which,
    disk_free: Callable[[Path], int] = capture.available_bytes,
    rmem_max: Callable[[], int] = capture.read_rmem_max,
) -> int:
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
    free = disk_free(out_root)
    if policy.action(free) == "stop":
        errors.append(
            f"only {free} bytes free, at/below hard floor "
            f"{policy.hard_free_bytes}"
        )
    if errors:
        raise BcanRecorderError(
            "B-CAN recorder dependency gate failed:\n- " + "\n- ".join(errors)
        )
    return free


def write_state(path: Path, **payload: object) -> None:
    capture.atomic_write_json(path, {"updated_utc": utc_now(), **payload})


def probe_bcan(
    probe_seconds: float,
    *,
    probe: Callable[..., tuple[set[int], int]] = canbus.probe_ids,
) -> tuple[frozenset[int], int]:
    query_interface()
    try:
        ids, rx_error_delta = probe(CHANNEL, probe_seconds)
    except OSError as exc:
        raise BcanRecorderError(f"passive B-CAN probe failed: {exc}") from exc
    query_interface()
    return signature_hits(ids), rx_error_delta


def record_bcan_topology(
    confirmed_signatures: frozenset[int],
    *,
    setter: Callable[..., object] = can_operation_state.set_topology,
) -> object:
    """Persist same-boot wake authority only after strong passive identity."""
    hits = signature_hits(confirmed_signatures)
    if len(hits) < MIN_SIGNATURE_HITS:
        raise BcanRecorderError(
            "cannot record B-CAN topology without the required signature witness"
        )
    return setter(
        CHANNEL,
        "b-can",
        pair=PAIR,
        source=TOPOLOGY_SOURCE,
        note=(
            "passively verified by persistent B-CAN recorder using "
            f"{len(hits)} known signature IDs"
        ),
    )


def record_one_interval(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    confirmed_signatures: frozenset[int],
    observer_handle,
) -> Path:
    if len(confirmed_signatures) < MIN_SIGNATURE_HITS:
        raise BcanStartRace("B-CAN identity witness disappeared before recording")
    if (
        getattr(observer_handle, "closed", True)
        or not getattr(observer_handle, "_diagnostic_lock_held", False)
        or getattr(observer_handle, "_diagnostic_lock_channel", None) != CHANNEL
        or getattr(observer_handle, "_diagnostic_lock_mode", None) != "observer"
    ):
        raise BcanRecorderError("shared CAN observer lock is not held")
    mount_device = capture.require_writable_mount(
        args.out_root,
        args.require_mount,
    )
    interface = query_interface()
    free = validate_dependencies(args.out_root, policy)
    capture.require_writable_mount(
        args.out_root,
        args.require_mount,
        expected_device=mount_device,
    )

    args.out_root.mkdir(parents=True, exist_ok=True)
    run_id = campaign_id()
    run_dir = args.out_root / run_id
    if run_dir.exists():
        raise BcanRecorderError(f"campaign directory already exists: {run_dir}")
    run_dir.mkdir()
    selected_ids = priority_ids()
    metadata = {
        "type": "run_metadata",
        "created_utc": utc_now(),
        "campaign": run_id,
        "conditions": args.conditions.strip(),
        "interaction": "passive_receive_only",
        "bus": "b-can",
        "channel": CHANNEL,
        "bitrate": BITRATE,
        "pair": PAIR,
        "interface": dataclasses.asdict(interface),
        "confirmed_signature_ids": [
            f"0x{value:X}" for value in sorted(confirmed_signatures)
        ],
        "identity_minimum_hits": MIN_SIGNATURE_HITS,
        "required_mount": str(args.require_mount.resolve()),
        "free_bytes_at_preflight": free,
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "stop_after_id": f"0x{TRACKED_ID:X}",
        "stop_after_id_absence_seconds": args.silence_seconds,
        "priority_ids": [f"0x{value:X}" for value in sorted(selected_ids)],
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "net_core_rmem_max": capture.read_rmem_max(),
        "scope_note": "awake interval; may be a drive, fob wake, or other body-network wake",
        "does_not": [
            "configure or restore CAN",
            "transmit CAN",
            "wake the bus",
            "perform diagnostic requests",
        ],
    }
    capture.atomic_write_json(run_dir / "run.json", metadata)
    write_state(
        args.state_path,
        status="recording",
        campaign=run_id,
        capture_dir=str(run_dir),
        confirmed_signature_ids=metadata["confirmed_signature_ids"],
    )
    recorder = capture.Recorder(
        run_dir,
        selected_ids,
        args.rotation_seconds,
        args.duration_seconds,
        policy,
        stop_after_id=TRACKED_ID,
        stop_after_id_absence_seconds=args.silence_seconds,
        required_start_id=TRACKED_ID,
        required_start_id_timeout_seconds=args.start_timeout_seconds,
        safety_check=query_interface,
        mount_check=lambda: capture.require_writable_mount(
            args.out_root,
            args.require_mount,
            expected_device=mount_device,
        ),
        zstd=shutil.which("zstd") or "zstd",
        candump=shutil.which("candump") or "candump",
        candump_extra_args=("-D",),
    )
    try:
        with capture.campaign_file_lock(run_dir):
            recorder.run()
    except capture.CaptureError as exc:
        if "required start CAN ID" in str(exc):
            raise BcanStartRace(str(exc)) from exc
        raise
    return run_dir


def run_daemon(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    acquire_observer: Callable[[str], object] = (
        diagnostic_safety.acquire_channel_observer_lock
    ),
    release_lock: Callable[[object], None] = diagnostic_safety.release_channel_lock,
    probe: Callable[[float], tuple[frozenset[int], int]] = probe_bcan,
    topology_recorder: Callable[[frozenset[int]], object] = record_bcan_topology,
    interval_recorder: Callable[..., Path] = record_one_interval,
) -> int:
    capture.require_writable_mount(args.out_root, args.require_mount)
    validate_dependencies(args.out_root, policy)
    last_detail: tuple[object, ...] | None = None
    last_capture: str | None = None
    while True:
        observer_handle = None
        ready = False
        hits = frozenset()
        rx_error_delta: int | None = None
        detail = "waiting for verified awake B-CAN"
        state_key: tuple[object, ...] = (detail,)
        start_race: BcanStartRace | None = None
        run_dir: Path | None = None
        topology_error: str | None = None
        try:
            try:
                observer_handle = acquire_observer(CHANNEL)
            except diagnostic_safety.ChannelLockError as exc:
                detail = str(exc)
                state_key = (detail,)
            else:
                try:
                    hits, rx_error_delta = probe(args.probe_seconds)
                except BcanRecorderError as exc:
                    detail = str(exc)
                    state_key = (detail,)
                else:
                    ready = bcan_signature_ready(hits, rx_error_delta)
                    detail = (
                        "verified B-CAN awake; starting recorder"
                        if ready
                        else "waiting for verified awake B-CAN"
                    )
                    state_key = (detail, tuple(sorted(hits)), rx_error_delta)

                if ready:
                    try:
                        topology_recorder(hits)
                    except (OSError, RuntimeError, ValueError) as exc:
                        topology_error = f"{type(exc).__name__}: {exc}"
                        print(
                            f"{utc_now()} warning: could not record same-boot "
                            f"B-CAN topology: {topology_error}",
                            flush=True,
                        )
                    try:
                        run_dir = interval_recorder(
                            args,
                            policy,
                            hits,
                            observer_handle,
                        )
                    except BcanStartRace as exc:
                        start_race = exc
        finally:
            if observer_handle is not None:
                release_lock(observer_handle)

        if run_dir is not None:
            last_detail = None
            last_capture = str(run_dir)
            write_state(
                args.state_path,
                status="waiting",
                detail="previous B-CAN awake interval finalized successfully",
                last_capture_dir=last_capture,
                output_root=str(args.out_root),
            )
            print(f"{utc_now()} finalized {run_dir.name}; re-armed", flush=True)
            sleep(args.retry_seconds)
            continue

        if start_race is not None:
            last_detail = None
            write_state(
                args.state_path,
                status="waiting",
                detail=f"awake interval ended during recorder startup: {start_race}",
                output_root=str(args.out_root),
            )
            print(f"{utc_now()} {start_race}; re-arming", flush=True)
            sleep(args.retry_seconds)
            continue

        if state_key != last_detail:
            payload = {
                "status": "waiting",
                "detail": detail,
                "output_root": str(args.out_root),
                "required_signature_hits": MIN_SIGNATURE_HITS,
                "signature_ids_seen": [
                    f"0x{value:X}" for value in sorted(hits)
                ],
                "rx_error_delta": rx_error_delta,
                "idle_observer_lock_held": False,
            }
            if topology_error is not None:
                payload["topology_record_error"] = topology_error
            if last_capture is not None:
                payload["last_capture_dir"] = last_capture
            write_state(args.state_path, **payload)
            print(f"{utc_now()} {detail}", flush=True)
            last_detail = state_key
        sleep(args.retry_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-passive-bcan", action="store_true")
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
    parser.add_argument("--silence-seconds", type=float, default=30.0)
    parser.add_argument("--start-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--probe-seconds", type=float, default=DEFAULT_PROBE_SECONDS)
    parser.add_argument("--retry-seconds", type=float, default=DEFAULT_RETRY_SECONDS)
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
    if args.execute and not args.confirm_passive_bcan:
        raise BcanRecorderError("--execute requires --confirm-passive-bcan")
    for label, path in (
        ("--out-root", args.out_root),
        ("--require-mount", args.require_mount),
        ("--state-path", args.state_path),
    ):
        if not path.is_absolute():
            raise BcanRecorderError(f"{label} must be absolute")
    if not args.conditions.strip():
        raise BcanRecorderError("--conditions cannot be empty")
    if args.rotation_seconds < 10:
        raise BcanRecorderError("--rotation-seconds must be at least 10")
    if not 1 <= args.duration_seconds <= 48 * 60 * 60:
        raise BcanRecorderError(
            "--duration-seconds must be between 1 and 172800"
        )
    for label, value, lower, upper in (
        ("--silence-seconds", args.silence_seconds, 10, 600),
        ("--start-timeout-seconds", args.start_timeout_seconds, 1, 30),
        ("--probe-seconds", args.probe_seconds, 1, 30),
        ("--retry-seconds", args.retry_seconds, 1, 300),
    ):
        if not math.isfinite(value) or not lower <= value <= upper:
            raise BcanRecorderError(
                f"{label} must be between {lower} and {upper} seconds"
            )
    if args.start_timeout_seconds >= args.duration_seconds:
        raise BcanRecorderError(
            "--start-timeout-seconds must be shorter than --duration-seconds"
        )
    return capture.DiskPolicy(
        soft_free_bytes=int(args.soft_free_gib * 1024**3),
        hard_free_bytes=int(args.hard_free_gib * 1024**3),
    )


def plan(args: argparse.Namespace, policy: capture.DiskPolicy) -> dict[str, object]:
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "passive_receive_only",
        "trigger": {
            "minimum_bcan_signature_ids": MIN_SIGNATURE_HITS,
            "signature_set": [f"0x{value:X}" for value in sorted(canbus.BCAN_SIG)],
            "probe_seconds": args.probe_seconds,
            "maximum_rx_error_delta": canbus.RX_ERR_ABORT - 1,
        },
        "interface_requirement": {
            "channel": CHANNEL,
            "bitrate": BITRATE,
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
            "physical_pair": PAIR,
        },
        "output_root": str(args.out_root),
        "required_mount": str(args.require_mount),
        "state_path": str(args.state_path),
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "tracked_id": f"0x{TRACKED_ID:X}",
        "tracked_id_absence_seconds": args.silence_seconds,
        "required_start_id_timeout_seconds": args.start_timeout_seconds,
        "priority_ids": [f"0x{value:X}" for value in sorted(priority_ids())],
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "does_not": [
            "configure or restore CAN",
            "transmit CAN",
            "wake the bus",
            "perform diagnostic requests",
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
    except (
        BcanRecorderError,
        capture.CaptureError,
        diagnostic_safety.ChannelLockError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
