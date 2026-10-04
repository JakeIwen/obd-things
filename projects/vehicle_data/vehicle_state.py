"""Pure conclusions from already-acquired passive vehicle evidence.

No clock, lock, broker or hardware is accessed here. Timestamp selection and
fresh-authority protection retain their original ordering in the broker.
"""

from __future__ import annotations

from projects.vehicle_data.models import AcquisitionResult


def accepts_state_observation(result: AcquisitionResult) -> bool:
    if result.metric not in ("battery.voltage", "engine.rpm", "vehicle.ignition_on"):
        return False
    # Solicited cluster voltage proves neither passive traffic nor ignition.
    return not (
        result.metric == "battery.voltage"
        and result.acquisition == "physical_read_data_by_identifier"
    )


def needs_current_authority(result: AcquisitionResult) -> bool:
    return result.metric == "battery.voltage" or (
        result.available
        and result.metric == "vehicle.ignition_on"
        and result.value is True
    )


def preservation_authority(
    result: AcquisitionResult, current_basis: object,
) -> tuple[str, bool]:
    """Return the original definition lookup and whether its evidence can win.

    Voltage cannot downgrade fresh RPM/ignition evidence. Ignition-on cannot
    downgrade fresh RPM, which already distinguishes running from stopped.
    The ignition path still looks up engine.rpm even for an unrelated basis.
    """
    if result.metric == "battery.voltage":
        metric = {
            "qualified_ccan_0x0fc_engine_speed": "engine.rpm",
            "ccan_0x2ef_ignition_gate": "vehicle.ignition_on",
        }.get(str(current_basis))
        return metric or "", metric is not None
    return "engine.rpm", current_basis == "qualified_ccan_0x0fc_engine_speed"


def qualified_engine_state(result: AcquisitionResult) -> dict[str, object] | None:
    """Only the qualified RPM source can distinguish running from ignition-on."""
    if (
        result.metric == "engine.rpm"
        and result.available
        and result.source == "ccan.broadcast.0x0fc"
        and isinstance(result.value, (int, float))
        and not isinstance(result.value, bool)
    ):
        running = float(result.value) >= 400.0
        state = {
            "state": "running" if running else "ignition_on",
            "running": running,
            "confidence": "verified",
            "basis": "qualified_ccan_0x0fc_engine_speed",
            "detail": (
                f"qualified passive 0x0FC engine speed is "
                f"{float(result.value):.0f} rpm"
            ),
        }
        return state
    return None


def passive_vehicle_state(
    result: AcquisitionResult, *, ccan_silent: bool,
) -> dict[str, object] | None:
    """Describe passive activity without inferring running from voltage."""
    state = None
    if result.available and result.metric == "vehicle.ignition_on":
        ignition_on = result.value is True
        state = {
            "state": "ignition_on" if ignition_on else "parked",
            "running": None if ignition_on else False,
            "confidence": "verified",
            "basis": "ccan_0x2ef_ignition_gate",
            "detail": (
                "verified C-CAN ignition-on gate is present"
                if ignition_on
                else "verified C-CAN ignition-on gate is absent"
            ),
        }
    elif result.available:
        state = {
            "state": "awake",
            "running": None,
            "confidence": "observed",
            "basis": "passive_bus_activity",
            "detail": (
                f"{result.bus or 'vehicle bus'} traffic is present; "
                "running versus ignition-on versus a temporary wake is "
                "not yet distinguished"
            ),
        }
    elif result.reason == "bus_asleep" and ccan_silent:
        # The C-CAN leg is silent but CAN-CH has been busy for minutes: the
        # van is awake and the C-CAN adapter is deaf (2026-09-22 incident).
        state = {
            "state": "awake",
            "running": None,
            "confidence": "inferred",
            "basis": "passive_can_ch_activity_c_can_silent",
            "detail": (
                "CAN-CH traffic is present but the C-CAN adapter has "
                "received nothing for minutes; replug its USB cable or "
                "reboot the Pi"
            ),
        }
    elif result.reason == "bus_asleep":
        state = {
            "state": "asleep",
            "running": False,
            "confidence": "inferred",
            "basis": "passive_bus_silence",
            "detail": (
                "no frames arrived at the approved bitrate; this is "
                "consistent with a sleeping vehicle, but an unplugged "
                "physical leg is not distinguishable from silence"
            ),
        }
    elif result.bus == "can-ch":
        state = {
            "state": "awake",
            "running": None,
            "confidence": "observed",
            "basis": "passive_can_ch_activity",
            "detail": (
                "CAN-CH traffic is present; no verified running-state "
                "metric is available on this branch"
            ),
        }
    elif result.bus == "wrong-rate":
        state = {
            "state": "awake",
            "running": None,
            "confidence": "inferred",
            "basis": "wrong_rate_rx_activity",
            "detail": (
                "RX errors show traffic at another bitrate; vehicle "
                "running state cannot be determined"
            ),
        }
    return state
