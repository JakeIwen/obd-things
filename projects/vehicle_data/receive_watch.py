"""Detect a vehicle-bus adapter that has gone deaf while another bus is busy.

On 2026-09-22 Board A (C-CAN + B-CAN) stopped delivering received frames while
its SocketCAN links stayed up, listen-only and ERROR-ACTIVE.  Every status
check passed, so the broker read the silence as a sleeping van and the
dashboard showed "asleep" through a 30-minute drive.  Board B's CAN-CH kept
receiving about 1,200 frames/s the whole time.

A sleeping van silences every branch together, so a C-CAN or B-CAN receive
counter that stays flat for minutes while CAN-CH is continuously busy is
adapter evidence, not vehicle state.  This module only reads the kernel's
``rx_packets`` counters that the interface manager already reports; it never
opens a socket, changes a link, or resets hardware.
"""

from __future__ import annotations

from collections.abc import Mapping

from lib.vehicle_can_roles import B_CAN_ROLE, CAN_CH_ROLE, C_CAN_ROLE

WATCHED_ROLES = (C_CAN_ROLE, B_CAN_ROLE)
REFERENCE_ROLE = CAN_CH_ROLE
RECEIVE_SILENT_REASON = "receive_silent"


class ReceiveSilenceWatch:
    """Track per-role receive counters across interface status probes.

    A watched role is flagged once its counter has not moved for
    ``silent_seconds`` while the reference role's counter moved on every probe
    across that same span (probe gaps up to ``max_gap_seconds`` are tolerated).
    The flag clears on the first probe where the watched counter moves.
    """

    def __init__(
        self,
        *,
        silent_seconds: float = 120.0,
        max_gap_seconds: float = 15.0,
    ) -> None:
        self.silent_seconds = silent_seconds
        self.max_gap_seconds = max_gap_seconds
        # role -> (channel, last counter, monotonic time the counter last moved)
        self._last: dict[str, tuple[str, int, float]] = {}
        # monotonic start of the reference role's unbroken activity streak
        self._reference_active_since: float | None = None
        self._reference_last_seen: float | None = None

    @staticmethod
    def _counter(payload: object) -> tuple[str, int] | None:
        if not isinstance(payload, Mapping) or payload.get("resolution") != "resolved":
            return None
        channel = payload.get("channel")
        actual = payload.get("actual")
        count = actual.get("rx_packets") if isinstance(actual, Mapping) else None
        if (
            not isinstance(channel, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            return None
        return channel, count

    def _advance(self, role: str, sample: tuple[str, int] | None, now: float) -> bool:
        """Record one probe; return whether the counter moved since the last one."""
        if sample is None:
            self._last.pop(role, None)
            return False
        channel, count = sample
        previous = self._last.get(role)
        if previous is None or previous[0] != channel or count < previous[1]:
            # New channel, re-enumeration or counter reset: restart the baseline
            # and treat this probe as fresh activity so nothing is flagged early.
            self._last[role] = (channel, count, now)
            return False
        moved = count > previous[1]
        self._last[role] = (channel, count, now if moved else previous[2])
        return moved

    def update(self, roles: Mapping[str, object], now: float) -> dict[str, dict[str, object]]:
        reference_moved = self._advance(REFERENCE_ROLE, self._counter(roles.get(REFERENCE_ROLE)), now)
        if reference_moved:
            if (
                self._reference_active_since is None
                or self._reference_last_seen is None
                or now - self._reference_last_seen > self.max_gap_seconds
            ):
                self._reference_active_since = now
            self._reference_last_seen = now
        elif (
            self._reference_last_seen is None
            or now - self._reference_last_seen > self.max_gap_seconds
        ):
            self._reference_active_since = None
        reference_active_for = (
            now - self._reference_active_since
            if self._reference_active_since is not None
            else 0.0
        )
        findings: dict[str, dict[str, object]] = {}
        for role in WATCHED_ROLES:
            self._advance(role, self._counter(roles.get(role)), now)
            last = self._last.get(role)
            if last is None:
                continue
            silent_for = now - last[2]
            flagged = (
                silent_for >= self.silent_seconds
                and reference_active_for >= self.silent_seconds
            )
            findings[role] = {
                "receive_silent": flagged,
                "silent_seconds": round(silent_for, 1),
                "reference_role": REFERENCE_ROLE,
                "reference_active_seconds": round(reference_active_for, 1),
            }
        return findings

    def silent_roles(self, findings: Mapping[str, Mapping[str, object]]) -> tuple[str, ...]:
        return tuple(
            role for role in WATCHED_ROLES
            if isinstance(findings.get(role), Mapping)
            and findings[role].get("receive_silent") is True
        )
