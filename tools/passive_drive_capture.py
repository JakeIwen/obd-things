#!/usr/bin/env python3
"""Bounded, passive SocketCAN drive recorder.

The default invocation is a plan: it performs no subprocess calls and writes no
files.  Live recording requires ``--execute --confirm-passive --conditions``.

This tool never configures CAN, controls a service, or transmits. It resolves
the requested logical role and accepts only its already-UP, exact-bitrate,
LISTEN-ONLY, ERROR-ACTIVE interface, then runs one persistent ``candump`` process.
Its text stream is compressed into
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

from lib import can_runtime_route
from lib.modules import MODULES
from lib.timeutil import utc_now
from lib.capture_pipeline import (
    RECEIVE_BUFFER,
    RMEM_MAX_PATH,
    DEFAULT_ROTATION_SECONDS,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_STOP_ID_ABSENCE_SECONDS,
    MAX_PENDING_FINALIZATION_SECONDS,
    DEFAULT_SOFT_FREE_BYTES,
    DEFAULT_HARD_FREE_BYTES,
    CANDUMP_RE,
    DROP_RE,
    CaptureError,
    RecoverableCaptureError,
    InterfaceState,
    DiskPolicy,
    parse_interface_state,
    parse_candump_line,
    parse_drop_line,
    is_priority_line,
    fsync_directory,
    atomic_write_json,
    append_manifest,
    campaign_file_lock,
    sha256_file,
    available_bytes,
    require_writable_mount,
    read_rmem_max,
    strip_partial_suffix,
    verify_zstd_file,
    recover_partials,
    runtime_safety_check,
    preflight,
    ZstdStream,
    Chunk,
    Recorder as _Recorder,
)


DEFAULT_BUS = "c-can"
CAMPAIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
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


class Recorder(_Recorder):
    """Compatibility entry point retaining this module's Chunk patch boundary."""

    @staticmethod
    def _new_chunk(*args, **kwargs) -> Chunk:
        return Chunk(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bus",
        choices=("c-can", "b-can", "can-ch"),
        default=DEFAULT_BUS,
        help="logical installed bus resolved by USB serial/dev_id (default: c-can)",
    )
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
        help="confirm that the resolved interface must remain listen-only and never transmit",
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
        if args.priority_profile == "ccan-correlation" and args.bus != "c-can":
            raise CaptureError(
                "--priority-profile ccan-correlation requires --bus c-can; "
                "select --priority-profile none for another bus"
            )
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
            "logical_role": args.bus,
            "channel": "resolved at execution by USB serial/dev_id",
            "up": True,
            "bitrate": "from canonical role configuration",
            "listen_only": True,
            "controller_state": "ERROR-ACTIVE",
        },
        "candump_command": [
            "candump",
            "-L",
            "-d",
            "-r",
            str(RECEIVE_BUFFER),
            "<resolved-canN>",
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
    try:
        ownership = can_runtime_route.acquire_passive_bus_route(args.bus)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CaptureError(f"stable passive route failed: {exc}") from exc
    route = ownership.route
    try:
        mount_device = require_writable_mount(capture_root, args.require_mount)
        state, free = preflight(
            capture_root,
            policy,
            channel=route.channel,
            bitrate=route.bitrate,
        )
        # Close the mount/interface race after reserving the participating channel.
        require_writable_mount(
            capture_root,
            args.require_mount,
            expected_device=mount_device,
        )
        ownership.revalidate()
        state, free = preflight(
            capture_root,
            policy,
            channel=route.channel,
            bitrate=route.bitrate,
        )
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
            "logical_bus": route.role,
            "channel": route.channel,
            "physical_pair": route.pair,
            "bitrate": route.bitrate,
            "topology_fingerprint": route.topology_fingerprint,
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
            channel=route.channel,
            bitrate=route.bitrate,
            safety_check=lambda: (
                ownership.revalidate()
                or runtime_safety_check(
                    channel=route.channel,
                    bitrate=route.bitrate,
                )
            ),
        )
        with campaign_file_lock(run_dir):
            return recorder.run()
    finally:
        ownership.release()


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
