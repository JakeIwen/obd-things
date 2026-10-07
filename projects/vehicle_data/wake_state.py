"""Pure parked-wake admission over the broker's already-captured evidence."""

from __future__ import annotations


def wake_state_conflicts(
    *, active_drive: dict, auxiliary_drive: dict, vehicle_state: dict,
    restoration_latched: bool, auxiliary_restoration_latched: bool,
    collector_state: str, vehicle_observed: float | None,
    now: float, collector_interval_seconds: float, require_fresh: bool,
) -> tuple[str, ...]:
    conflicts: list[str] = []
    if collector_state != "running":
        conflicts.append("passive collector is not running")
    freshness_limit = max(3.0, collector_interval_seconds * 3.0)
    if require_fresh and (
        vehicle_observed is None or now - vehicle_observed > freshness_limit
    ):
        conflicts.append("passive vehicle-state evidence is missing or stale")
    if restoration_latched or active_drive.get("restoration_failed"):
        conflicts.append("active-drive passive restoration is latched failed")
    if auxiliary_restoration_latched or auxiliary_drive.get("restoration_failed"):
        conflicts.append("B-CAN auxiliary passive restoration is latched failed")
    active_state = active_drive.get("state")
    active_mode = active_drive.get("interface_mode")
    if active_state not in ("idle", "disabled"):
        conflicts.append("broker active-drive state is not idle or disabled")
    if active_mode != "listen_only":
        conflicts.append("broker active-drive interface mode is not listen-only")
    if active_drive.get("helper_pid") is not None:
        conflicts.append("broker active-drive helper is still present")
    if auxiliary_drive.get("state") not in ("idle", "disabled"):
        conflicts.append("broker B-CAN auxiliary state is not idle or disabled")
    if auxiliary_drive.get("interface_mode") != "listen_only":
        conflicts.append("broker B-CAN auxiliary mode is not listen-only")
    if auxiliary_drive.get("helper_pid") is not None:
        conflicts.append("broker B-CAN auxiliary helper is still present")
    if (
        vehicle_state.get("running") is True
        or vehicle_state.get("state") in ("running", "ignition_on")
    ):
        conflicts.append(
            "broker has verified running or ignition-on vehicle evidence"
        )
    elif vehicle_state.get("state") not in ("asleep", "awake", "parked"):
        conflicts.append("broker vehicle state is not safe for a parked wake")
    return tuple(conflicts)
