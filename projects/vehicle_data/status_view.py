"""Pure status builders over the broker's lock-protected JSON snapshot.

The broker owns synchronization, clocks and component calls. These builders do
not read a broker, perform I/O or mutate the snapshot. The JSON round trips
that populate it deliberately remain inside the broker lock.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class StatusSnapshot:
    """Captured status values; builders treat all nested payloads as read-only."""

    radar_alignment: dict[str, object]
    interface: dict[str, object]
    interface_probe: dict[str, object]
    inflight: list[dict[str, str]]
    last_errors: dict[str, object]
    cached: dict[str, object]
    collector: dict[str, object]
    history_recorder: dict[str, object]
    supplemental_cache: dict[str, object]
    active_drive: dict[str, object]
    auxiliary_drive: dict[str, object]
    data_quality: dict[str, object]
    interface_reconcile: dict[str, object]
    vehicle_state: dict[str, object]
    vehicle_observed: float | None


@dataclass(frozen=True)
class InterfaceView:
    interface: dict[str, object]
    current_owner: dict[str, object] | None
    active_permitted: bool


def build_vehicle_state(
    snapshot: StatusSnapshot, *, age_ms: int | None,
    ignition_stale: bool, rpm_stale: bool,
) -> dict[str, object]:
    vehicle_state = dict(snapshot.vehicle_state)
    vehicle_state["age_ms"] = age_ms
    if ignition_stale:
        vehicle_state.update(
            {
                "state": "unknown",
                "running": None,
                "confidence": "stale",
                "basis": "stale_ccan_0x2ef_ignition_gate",
                "detail": (
                    "the last verified ignition-on gate observation is "
                    "stale; current ignition state is unknown"
                ),
            }
        )
    if rpm_stale:
        vehicle_state.update(
            {
                "state": "unknown",
                "running": None,
                "confidence": "stale",
                "basis": "stale_ccan_0x0fc_engine_speed",
                "detail": (
                    "the last qualified passive engine-speed observation "
                    "is stale; current running state is unknown"
                ),
            }
        )
    return vehicle_state


def build_interface_view(
    snapshot: StatusSnapshot, *, auxiliary_drive_enabled: bool,
    auxiliary_restoration_latched: bool,
) -> InterfaceView:
    # Overlay only our owned copy. In particular, topology must keep its alias
    # to this interface while the active-owner overlay repairs the usable bit.
    interface = deepcopy(snapshot.interface)
    last_errors = snapshot.last_errors
    inflight = snapshot.inflight
    active_drive = snapshot.active_drive
    auxiliary_drive = snapshot.auxiliary_drive
    topology = interface.get("topology") or {}
    inhibits = interface.get("active_inhibits") or []
    busy_error = next(
        (
            error
            for error in last_errors.values()
            if error.get("reason") == "can_busy"
        ),
        None,
    )
    active_drive_owns = (
        active_drive.get("interface_mode") == "armed_diagnostic"
        and active_drive.get("state")
        not in ("idle", "disabled", "restoration_failed")
    )
    active_drive_reserved = active_drive_owns or active_drive.get(
        "state"
    ) == "starting"
    auxiliary_drive_owns = (
        auxiliary_drive.get("interface_mode") == "armed_diagnostic"
        and auxiliary_drive.get("state")
        not in ("idle", "disabled", "restoration_failed")
    )
    auxiliary_drive_reserved = auxiliary_drive_owns or auxiliary_drive.get(
        "state"
    ) == "starting"
    if active_drive_owns:
        # The last normal status probe predates the child-owned interval.
        # Overlay the helper's verified mode so status never calls an armed
        # adapter listen-only merely because the normal collector is
        # synchronously waiting for the child.
        interface["listen_only"] = False
        interface["mode"] = "armed_diagnostic"
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = (
            role_snapshot if isinstance(role_snapshot, dict) else None
        )
        role_payloads = (
            role_snapshot.get("roles")
            if isinstance(role_snapshot, dict)
            else None
        )
        ccan_role = (
            role_payloads.get("c-can")
            if isinstance(role_payloads, dict)
            else None
        )
        if (
            isinstance(ccan_role, dict)
            and ccan_role.get("channel") == interface.get("channel")
        ):
            actual = ccan_role.get("actual")
            # Physical route validity, not passive admission.  A status
            # refresh during the armed interval (the scheduled voltage_mon
            # acquisition at 18:00/20:00 local) re-probes the role as
            # ``interface_armed`` with passive_ready false, and the
            # top-level topology.usable copies that bit (see
            # 2026-09-06_broker_topology_refresh_diagnosis.md).  Copying it
            # here made the historian flag the broker's own armed channel
            # as "topology_unusable" on every such drive (episodes 643/644).
            # ``interface_armed`` is only reached after the identity, link,
            # bitrate, FD and one-shot checks pass (can_interfaces.py), so
            # it still proves the route; any other reason stays unusable.
            route_verified = (
                ccan_role.get("resolution") == "resolved"
                and ccan_role.get("reason") in ("ready", "interface_armed")
                and isinstance(actual, dict)
                and actual.get("up") is True
                and actual.get("controller_state") == "ERROR-ACTIVE"
            )
            if isinstance(actual, dict):
                actual["listen_only"] = False
                actual["mode"] = "armed_diagnostic"
            ccan_role["passive_ready"] = False
            ccan_role["operating_mode"] = "armed_diagnostic"
            ccan_role["topology_usable"] = route_verified
            if route_verified:
                # Structured owner marker for status consumers (historian).
                ccan_role["armed_owner"] = "broker_active_drive"
                # Same repair for the top-level flag the drive recorder
                # admits on: after that mid-drive refresh it copied
                # passive_ready=false and the recorder stopped for the rest
                # of the drive (2026-09-24 18:00 and 20:00). It describes the
                # verified physical route here, not passive admission;
                # listen_only stays false, so passive admission is unchanged.
                if isinstance(interface.get("topology"), dict):
                    interface["topology"]["usable"] = True
                    interface["topology"]["reason"] = (
                        "C-CAN route verified; armed by the broker active-drive owner"
                    )
            ccan_role["reason"] = (
                "broker active-drive owner has the resolved C-CAN channel "
                "armed for reviewed diagnostics"
            )
            role_snapshot["ready"] = False
    else:
        interface["mode"] = (
            "listen_only"
            if interface.get("listen_only") is True
            else "armed_or_unknown"
        )
    if auxiliary_drive_owns:
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = role_snapshot if isinstance(role_snapshot, dict) else None
        role_payloads = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
        bcan_role = role_payloads.get("b-can") if isinstance(role_payloads, dict) else None
        if isinstance(bcan_role, dict):
            actual = bcan_role.get("actual") or {}
            expected = bcan_role.get("expected") or {}
            route = auxiliary_drive.get("owner_route") or {}
            verified_owner = (
                auxiliary_drive_enabled
                and auxiliary_drive.get("state") == "armed_diagnostic"
                and auxiliary_drive.get("reason") == "running_gate_satisfied"
                and type(auxiliary_drive.get("helper_pid")) is int
                and auxiliary_drive["helper_pid"] > 0
                and auxiliary_drive.get("restoration_failed") is False
                and not auxiliary_restoration_latched
                and not inhibits
                and bcan_role.get("resolution") == "resolved"
                and bcan_role.get("reason") in ("ready", "interface_armed")
                and isinstance(route.get("channel"), str)
                and bcan_role.get("channel") == route.get("channel")
                and isinstance(route.get("usb_serial"), str)
                and expected.get("usb_serial") == route.get("usb_serial")
                and type(route.get("dev_id")) is int
                and expected.get("dev_id") == route.get("dev_id")
                and actual.get("present") is True and actual.get("up") is True
                and actual.get("bitrate") == expected.get("bitrate") == 125000
                and actual.get("fd_enabled") is False
                and actual.get("one_shot") is False
                and actual.get("restart_ms") == 0
                and actual.get("controller_state") == "ERROR-ACTIVE"
            )
            # This affects the health report only; it grants no CAN authority.
            if verified_owner:
                actual["listen_only"] = False
                actual["mode"] = "armed_diagnostic"
                bcan_role["passive_ready"] = False
                bcan_role["operating_mode"] = "armed_diagnostic"
                bcan_role["topology_usable"] = True
                bcan_role["armed_owner"] = "broker_auxiliary_drive"
                bcan_role["reason"] = "broker auxiliary-drive owner has the resolved B-CAN channel armed for fixed ICS polling"
            else:
                bcan_role["topology_usable"] = False
                if bcan_role.get("reason") in ("ready", "interface_armed"):
                    bcan_role["reason"] = "active_owner_unverified"
            if role_snapshot is not None:
                role_snapshot["ready"] = False
    active_permitted = bool(
        interface.get("adapter_present")
        and interface.get("up")
        and interface.get("fd_enabled") is False
        and interface.get("one_shot") is False
        and interface.get("listen_only")
        and interface.get("controller_state") == "ERROR-ACTIVE"
        and topology.get("usable")
        and topology.get("bus") in ("c-can", "b-can")
        and not inhibits
        and not inflight
        and busy_error is None
        and not active_drive_reserved
        and not auxiliary_drive_reserved
        and not active_drive.get("restoration_failed")
        and not auxiliary_drive.get("restoration_failed")
    )
    if active_drive_reserved:
        current_owner = {
            "kind": "broker_active_drive",
            "detail": active_drive.get("detail"),
            "roles": (
                ["c-can", "b-can"]
                if auxiliary_drive_reserved
                else ["c-can"]
            ),
        }
    elif auxiliary_drive_reserved:
        current_owner = {
            "kind": "broker_auxiliary_drive",
            "detail": auxiliary_drive.get("detail"),
            "roles": ["b-can"],
        }
    elif inhibits:
        current_owner = {
            "kind": "external_inhibit",
            "names": inhibits,
        }
    elif inflight:
        current_owner = {
            "kind": "broker",
            "operations": inflight,
        }
    elif busy_error is not None:
        current_owner = {
            "kind": "participating_or_external_can_user",
            "detail": busy_error["detail"],
        }
    else:
        current_owner = None
    return InterfaceView(interface, current_owner, active_permitted)


def build_status(
    snapshot: StatusSnapshot, interface_view: InterfaceView,
    vehicle_state: dict[str, object], *, started_at: str,
    last_readings: dict[str, object], display_receiver: dict[str, object],
    usb_can_monitor: dict[str, object], engine_off_voltage: dict[str, object],
    radar_polling_commissioned: bool, radar_storage_error: str | None,
) -> dict[str, object]:
    """Assemble the public keys in their established wire order."""
    return {
        "service": "van-telemetry",
        "started_at": started_at,
        "interface_probe": snapshot.interface_probe,
        "interface": interface_view.interface,
        "current_owner": interface_view.current_owner,
        "last_readings": last_readings,
        "active_acquisition_permitted": interface_view.active_permitted,
        "collector": snapshot.collector,
        "display_receiver": display_receiver,
        "history_recorder": snapshot.history_recorder,
        "supplemental_cache": snapshot.supplemental_cache,
        "usb_can_monitor": usb_can_monitor,
        "data_quality": snapshot.data_quality,
        "active_drive": snapshot.active_drive,
        "auxiliary_drive": snapshot.auxiliary_drive,
        "engine_off_voltage": engine_off_voltage,
        "interface_reconcile": snapshot.interface_reconcile,
        "vehicle_state": vehicle_state,
        "radar_alignment": snapshot.radar_alignment,
        "radar_alignment_polling": {
            "commissioned": radar_polling_commissioned,
            "retained_storage_error": radar_storage_error,
            "detail": (
                "Fixed radar angle reads during qualified engine-running intervals"
                if radar_polling_commissioned else
                "Live monitoring awaits a parked no-session radar angle support check"
            ),
        },
        "inflight": snapshot.inflight,
        "last_acquisition_errors": snapshot.last_errors,
        "cached_metrics": snapshot.cached,
    }
