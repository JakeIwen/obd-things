"""Receive-only capture primitives shared by recorder entry points.

These helpers do not acquire routes, configure interfaces, or start children.
Callers retain their distinct ownership, buffering, and lifecycle policies.
"""

from __future__ import annotations

from typing import Sequence

# Keep these objects identical while recorder entry points migrate one at a
# time. The implementation is moved here in the following extraction step.
from tools.passive_drive_capture import (
    CaptureError,
    DEFAULT_DURATION_SECONDS,
    DEFAULT_HARD_FREE_BYTES,
    DEFAULT_ROTATION_SECONDS,
    DEFAULT_SOFT_FREE_BYTES,
    DiskPolicy,
    InterfaceState,
    RECEIVE_BUFFER,
    Recorder,
    atomic_write_json,
    available_bytes,
    campaign_file_lock,
    parse_interface_state,
    read_rmem_max,
    require_writable_mount,
)


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
