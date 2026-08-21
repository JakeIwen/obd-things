#!/usr/bin/env python3
"""Arm one passive C-CAN capture that starts when ignition frame 0x2EF appears.

The default invocation is an inert JSON plan. Live mode only observes an
already-configured listen-only SocketCAN interface while waiting. Once 0x2EF is
seen it starts :mod:`passive_drive_capture` as a child. The child cleanly
finishes after 0x2EF has remained absent for the configured grace period.

This tool never configures CAN, controls a service, uses ADB, or transmits.
It is intentionally one-shot: one arm produces at most one drive capture.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Sequence


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_runtime_route  # noqa: E402
from tools import passive_drive_capture as passive  # noqa: E402


CHANNEL: str | None = None
C_CAN_BITRATE = 500_000
IGNITION_ID = 0x2EF
DEFAULT_OUT_ROOT = (
    Path("/mnt/EXFAT512")
    / "obd-things"
    / "tmp"
    / "captures"
    / "ccan"
    / "drive-correlation"
)
DEFAULT_REQUIRED_MOUNT = Path("/mnt/EXFAT512")
DEFAULT_STATE_PATH = (
    REPO / "tmp" / "ecu_mapping" / "ignition-drive-arm" / "state.json"
)
DEFAULT_DURATION_SECONDS = 22 * 60 * 60
DEFAULT_ABSENCE_SECONDS = 20.0
DEFAULT_PREFLIGHT_INTERVAL_SECONDS = 30.0
CAN_SFF_MASK = 0x7FF


class ArmError(RuntimeError):
    """The one-shot arm could not safely wait or launch."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def campaign_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"pcm-plots-drive-{stamp}"


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        passive.fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def open_ignition_socket() -> socket.socket:
    if CHANNEL is None:
        raise ArmError("C-CAN runtime role has not been resolved")
    try:
        can_socket = socket.socket(
            socket.PF_CAN,
            socket.SOCK_RAW,
            socket.CAN_RAW,
        )
        can_socket.setsockopt(
            socket.SOL_CAN_RAW,
            socket.CAN_RAW_FILTER,
            struct.pack("=II", IGNITION_ID, CAN_SFF_MASK),
        )
        can_socket.bind((CHANNEL,))
    except OSError as exc:
        try:
            can_socket.close()
        except (NameError, OSError):
            pass
        raise ArmError(f"cannot open passive {CHANNEL} ignition watcher: {exc}") from exc
    return can_socket


def wait_for_ignition(
    *,
    preflight_interval_seconds: float,
    monotonic=time.monotonic,
) -> float:
    """Wait for one fresh 0x2EF frame, periodically revalidating passive state."""
    next_preflight = 0.0
    can_socket: socket.socket | None = None
    try:
        while True:
            now = monotonic()
            if now >= next_preflight:
                if CHANNEL is None:
                    raise ArmError("C-CAN runtime role has not been resolved")
                passive.runtime_safety_check(
                    channel=CHANNEL,
                    bitrate=C_CAN_BITRATE,
                )
                next_preflight = now + preflight_interval_seconds
                if can_socket is None:
                    can_socket = open_ignition_socket()

            assert can_socket is not None
            try:
                ready, _, _ = select.select((can_socket,), (), (), 1.0)
            except (OSError, ValueError) as exc:
                can_socket.close()
                can_socket = None
                next_preflight = 0.0
                print(
                    f"{utc_now()} ignition watcher socket reset: {exc}",
                    flush=True,
                )
                time.sleep(1.0)
                continue
            if not ready:
                continue
            try:
                frame = can_socket.recv(16)
            except OSError as exc:
                can_socket.close()
                can_socket = None
                next_preflight = 0.0
                print(
                    f"{utc_now()} ignition watcher receive reset: {exc}",
                    flush=True,
                )
                continue
            if len(frame) < 4:
                continue
            can_id = struct.unpack_from("=I", frame)[0] & CAN_SFF_MASK
            if can_id == IGNITION_ID:
                return time.time()
    finally:
        if can_socket is not None:
            can_socket.close()


def child_command(args: argparse.Namespace, run_id: str) -> list[str]:
    return [
        sys.executable,
        str(REPO / "tools" / "passive_drive_capture.py"),
        "--bus",
        "c-can",
        "--out-root",
        str(args.out_root),
        "--require-mount",
        str(args.require_mount),
        "--campaign",
        run_id,
        "--duration-seconds",
        str(args.duration_seconds),
        "--stop-after-id",
        f"0x{IGNITION_ID:X}",
        "--stop-after-id-absence-seconds",
        str(args.ignition_absence_seconds),
        "--soft-free-gib",
        str(args.soft_free_gib),
        "--hard-free-gib",
        str(args.hard_free_gib),
        "--execute",
        "--confirm-passive",
        "--conditions",
        args.conditions.strip(),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-passive", action="store_true")
    parser.add_argument("--confirm-one-drive", action="store_true")
    parser.add_argument("--conditions", default="")
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument(
        "--require-mount",
        type=Path,
        default=DEFAULT_REQUIRED_MOUNT,
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--ignition-absence-seconds",
        type=float,
        default=DEFAULT_ABSENCE_SECONDS,
    )
    parser.add_argument(
        "--preflight-interval-seconds",
        type=float,
        default=DEFAULT_PREFLIGHT_INTERVAL_SECONDS,
    )
    parser.add_argument("--soft-free-gib", type=float, default=30.0)
    parser.add_argument("--hard-free-gib", type=float, default=25.0)
    return parser


def validate_args(args: argparse.Namespace) -> passive.DiskPolicy:
    if args.execute:
        if not args.confirm_passive:
            raise ArmError("--execute requires --confirm-passive")
        if not args.confirm_one_drive:
            raise ArmError("--execute requires --confirm-one-drive")
        if not args.conditions.strip():
            raise ArmError("--execute requires non-empty --conditions")
    if not args.out_root.is_absolute():
        raise ArmError("--out-root must be absolute")
    if not args.require_mount.is_absolute():
        raise ArmError("--require-mount must be absolute")
    if not args.state_path.is_absolute():
        raise ArmError("--state-path must be absolute")
    if not 1 <= args.duration_seconds <= 48 * 60 * 60:
        raise ArmError("--duration-seconds must be between 1 and 172800")
    if not 5 <= args.ignition_absence_seconds <= 300:
        raise ArmError("--ignition-absence-seconds must be between 5 and 300")
    if not 5 <= args.preflight_interval_seconds <= 300:
        raise ArmError("--preflight-interval-seconds must be between 5 and 300")
    try:
        policy = passive.DiskPolicy(
            soft_free_bytes=int(args.soft_free_gib * 1024**3),
            hard_free_bytes=int(args.hard_free_gib * 1024**3),
        )
    except (OverflowError, ValueError) as exc:
        raise ArmError(f"invalid disk policy: {exc}") from exc
    return policy


def plan(args: argparse.Namespace, policy: passive.DiskPolicy) -> dict[str, object]:
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "passive_one_drive_ignition_trigger",
        "logical_role": "c-can",
        "channel": "resolved at execution by USB serial/dev_id",
        "bitrate": C_CAN_BITRATE,
        "listen_only_required": True,
        "ignition_trigger_id": f"0x{IGNITION_ID:X}",
        "ignition_absence_seconds": args.ignition_absence_seconds,
        "maximum_duration_seconds": args.duration_seconds,
        "out_root": str(args.out_root),
        "required_mount": str(args.require_mount),
        "state_path": str(args.state_path),
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "one_shot": True,
        "does_not": [
            "configure CAN",
            "transmit CAN",
            "control AlfaOBD or ADB",
            "control system services",
            "change network or proxy settings",
        ],
    }


def execute(args: argparse.Namespace, policy: passive.DiskPolicy) -> int:
    global CHANNEL
    try:
        ownership = can_runtime_route.acquire_passive_bus_route(
            "c-can",
            asserted_pair="6/14",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArmError(f"stable passive C-CAN route failed: {exc}") from exc
    CHANNEL = ownership.route.channel
    try:
        mount_device = passive.require_writable_mount(
            args.out_root,
            args.require_mount,
        )
        passive.preflight(
            args.out_root,
            policy,
            channel=CHANNEL,
            bitrate=ownership.route.bitrate,
        )
        state = {
            "status": "waiting_for_ignition",
            "armed_utc": utc_now(),
            "channel": CHANNEL,
            "logical_bus": ownership.route.role,
            "physical_pair": ownership.route.pair,
            "bitrate": ownership.route.bitrate,
            "topology_fingerprint": ownership.route.topology_fingerprint,
            "listen_only_required": True,
            "trigger_id": f"0x{IGNITION_ID:X}",
            "out_root": str(args.out_root),
            "conditions": args.conditions.strip(),
        }
        atomic_write_json(args.state_path, state)
        print(
            f"{utc_now()} armed: waiting passively for ignition frame "
            f"0x{IGNITION_ID:X}",
            flush=True,
        )
        wait_for_ignition(
            preflight_interval_seconds=args.preflight_interval_seconds,
        )

        ownership.revalidate()
        passive.require_writable_mount(
            args.out_root,
            args.require_mount,
            expected_device=mount_device,
        )
        passive.preflight(
            args.out_root,
            policy,
            channel=CHANNEL,
            bitrate=ownership.route.bitrate,
        )
        run_id = campaign_id()
        command = child_command(args, run_id)
        state.update(
            {
                "status": "capture_starting",
                "triggered_utc": utc_now(),
                "campaign": run_id,
                "capture_dir": str(args.out_root / run_id),
            }
        )
        atomic_write_json(args.state_path, state)
        print(f"{utc_now()} ignition observed; starting {run_id}", flush=True)
        result = subprocess.run(command, check=False)
        state.update(
            {
                "status": "complete" if result.returncode == 0 else "error",
                "completed_utc": utc_now(),
                "capture_exit_status": result.returncode,
            }
        )
        atomic_write_json(args.state_path, state)
        if result.returncode != 0:
            raise ArmError(
                f"passive capture child exited with status {result.returncode}"
            )
        print(f"{utc_now()} one-drive capture finalized: {run_id}", flush=True)
        return 0
    finally:
        ownership.release()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = validate_args(args)
        if not args.execute:
            print(json.dumps(plan(args, policy), indent=2, sort_keys=True))
            return 0
        return execute(args, policy)
    except (ArmError, passive.CaptureError) as exc:
        raise SystemExit(f"refusing ignition-triggered capture: {exc}") from None


if __name__ == "__main__":
    raise SystemExit(main())
