"""Receive-only capture primitives shared by recorder entry points.

These helpers do not acquire routes, configure interfaces, or start children.
Callers retain their distinct ownership, buffering, and lifecycle policies.
"""

from __future__ import annotations

from typing import Sequence


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
