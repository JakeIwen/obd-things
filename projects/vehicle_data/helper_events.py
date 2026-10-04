"""Pure validation of trusted helper-pipe status/failure/final events.

Observation profiles and their side effects remain in the broker. In particular,
B-CAN is not an alias for the C-CAN protocol: it requires an explicit state,
checks failure reasons earlier, and accepts some unverified finals that C-CAN
rejects. Those differences are intentional compatibility constraints here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HelperStatus:
    state: str
    reason: str
    detail: str
    interface_mode: str


def validate_helper_status(
    event: dict[str, object], *, bus: str,
    failure_reasons: frozenset[str],
) -> HelperStatus:
    """Validate without recording a failure, claiming ownership or inhibiting CAN."""
    if bus not in ("c-can", "b-can"):
        raise ValueError("unsupported helper bus")
    active = bus == "c-can"
    label = "active-drive" if active else "B-CAN auxiliary"
    event_type = event.get("type")
    if event_type not in ("status", "failure", "final"):
        raise ValueError(f"unsupported {label} event type")
    reason = event.get("reason")
    detail = event.get("detail")
    if not isinstance(reason, str) or not isinstance(detail, str):
        raise ValueError(
            "active-drive status reason/detail must be strings"
            if active else "B-CAN auxiliary reason/detail is invalid"
        )
    interface_mode = event.get("interface_mode", "listen_only")
    if interface_mode not in ("listen_only", "armed_diagnostic", "unknown"):
        raise ValueError(f"invalid {label} interface mode")
    state = event.get("state")
    if active and state is None:
        state = (
            "restoration_failed"
            if reason == "restoration_failed"
            else ("idle" if event_type == "final" else event_type)
        )
    if not isinstance(state, str):
        raise ValueError(f"{label} state must be a string")
    if event_type == "status":
        if (
            state != "armed_diagnostic"
            or reason != "running_gate_satisfied"
            or interface_mode != "armed_diagnostic"
        ):
            raise ValueError(f"{label} armed status is inconsistent")
    elif event_type == "failure" or not active:
        # B-CAN checks the final reason before restored; C-CAN checks it after.
        if reason not in failure_reasons:
            raise ValueError(f"{label} failure reason is not allowlisted")
    if event_type == "final":
        restored = event.get("restored")
        if restored is not None and type(restored) is not bool:
            raise ValueError(
                "active-drive final restored must be boolean or null"
                if active else "B-CAN auxiliary restored must be boolean or null"
            )
        if active and reason not in failure_reasons:
            raise ValueError("active-drive final reason is not allowlisted")
        if state not in ("idle", "restoration_failed"):
            raise ValueError(f"{label} final state is invalid")
        if restored is True and interface_mode != "listen_only":
            raise ValueError(
                "restored final must report listen_only mode"
                if active else "restored B-CAN final must report listen_only"
            )
        if restored is False and (
            reason != "restoration_failed"
            or state != "restoration_failed"
            or interface_mode != "armed_diagnostic"
        ):
            raise ValueError(
                "failed restoration final is inconsistent"
                if active else "failed B-CAN restoration final is inconsistent"
            )
        if active:
            if reason == "restoration_failed" and restored is not False:
                raise ValueError("restoration failure must carry restored=false")
            if restored is None and interface_mode == "armed_diagnostic":
                raise ValueError("unverified final cannot claim armed ownership")
    return HelperStatus(state, reason, detail, interface_mode)
