"""Read-only multi-bus SocketCAN role ownership and status.

The two dual-channel adapters have stable USB serial/channel identities but
ephemeral Linux ``canN`` names.  This module binds the exact installed hardware
to the ProMaster's three physical buses, exposes fail-closed runtime routing,
and grants shared passive-observer leases.  It never configures an interface,
opens a CAN socket, or transmits a frame.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from lib import canbus, diagnostic_safety
from lib.can_role_resolver import (
    CanRoleResolutionError,
    CanRoleSpec,
    RoleResolution,
    RoleTopology,
    SysfsCanRoleResolver,
)
from lib.vehicle_can_roles import (
    ALL_CAN_ROLES,
    B_CAN_ROLE,
    BOARD_A_SERIAL,
    BOARD_B_SERIAL,
    CAN_BUS_ROLES,
    CAN_CH_ROLE,
    CAN_ROLE_SPECS,
    C_CAN_ROLE,
    SPARE_ROLE,
    normalize_can_role,
)


class PassiveInterfaceUnavailable(RuntimeError):
    """A role cannot safely grant a passive observer lease."""

    def __init__(self, role: str, reason: str, detail: str):
        super().__init__(detail)
        self.role = role
        self.reason = reason
        self.detail = detail


class ObserverLocks(Protocol):
    def observer(self, name: str): ...


class SystemObserverLocks:
    def observer(self, name: str):
        return diagnostic_safety.channel_observer_lock(name)


@dataclass(frozen=True)
class PassiveInterfaceLease:
    """A shared, read-only capability for one freshly resolved physical bus."""

    role: str
    channel: str
    usb_serial: str
    dev_id: int
    bitrate: int
    pair: str
    topology_generation: str

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "channel": self.channel,
            "usb_serial": self.usb_serial,
            "dev_id": self.dev_id,
            "bitrate": self.bitrate,
            "pair": self.pair,
            "topology_generation": self.topology_generation,
            "ownership": "shared_passive_observer",
        }


class PassiveInterfaceManager:
    """Resolve, report, and share already-passive physical CAN interfaces.

    ``status_snapshot`` and routing methods are read-only.  ``observe`` adds
    stable logical-role and current-netdev shared locks, then re-resolves after
    taking each lock so a USB reset cannot silently hand a caller the wrong
    bus.  A consumer still handles ordinary socket loss during a held lease and
    asks for a new lease on its next acquisition cycle.
    """

    def __init__(
        self,
        *,
        resolver: SysfsCanRoleResolver | None = None,
        specs: tuple[CanRoleSpec, ...] = CAN_ROLE_SPECS,
        interface_state_reader=None,
        locks: ObserverLocks | None = None,
        wall_clock=lambda: datetime.now(timezone.utc),
    ):
        self.resolver = resolver or SysfsCanRoleResolver()
        self.specs = tuple(specs)
        self._spec_by_role = {item.role: item for item in self.specs}
        self.interface_state_reader = interface_state_reader or canbus.interface_state
        self.locks = locks or SystemObserverLocks()
        self.wall_clock = wall_clock
        roles = tuple(item.role for item in self.specs)
        if set(roles) != set(ALL_CAN_ROLES) or len(roles) != len(ALL_CAN_ROLES):
            raise ValueError(
                "interface manager requires exactly c-can, b-can, can-ch, and spare"
            )
        for role in CAN_BUS_ROLES:
            spec = self._spec_by_role[role]
            if not spec.passive_required or spec.bitrate is None or spec.pair is None:
                raise ValueError(f"{role} requires a bitrate and physical pair")
        if self._spec_by_role[SPARE_ROLE].passive_required:
            raise ValueError("spare must not be an active passive-observer role")

    def topology(self) -> RoleTopology:
        return self.resolver.resolve(self.specs)

    def channel_for_role(
        self, role: str, *, topology: RoleTopology | None = None
    ) -> str:
        normalized = normalize_can_role(role)
        return (topology or self.topology()).channel_for(normalized)

    def channel_for_bus(
        self, bus: str, *, topology: RoleTopology | None = None
    ) -> str:
        normalized = normalize_can_role(bus)
        if normalized not in CAN_BUS_ROLES:
            raise ValueError(f"{bus!r} is not a connected vehicle bus")
        return self.channel_for_role(normalized, topology=topology)

    def channel_map(
        self,
        *,
        required: tuple[str, ...] = ALL_CAN_ROLES,
        topology: RoleTopology | None = None,
    ) -> dict[str, str]:
        normalized = tuple(normalize_can_role(item) for item in required)
        return (topology or self.topology()).channel_map(normalized)

    def module_channel(
        self, module, *, topology: RoleTopology | None = None
    ) -> str:
        """Route a ``lib.modules.Module`` by physical bus, never legacy channel."""
        bus = getattr(module, "bus", None)
        if not isinstance(bus, str):
            raise ValueError("diagnostic module has no physical bus name")
        return self.channel_for_bus(bus, topology=topology)

    @staticmethod
    def _actual_payload(state: canbus.InterfaceState | None) -> dict[str, object]:
        if state is None:
            return {
                "present": None,
                "up": None,
                "bitrate": None,
                "fd_enabled": None,
                "listen_only": None,
                "controller_state": None,
                "restart_ms": None,
            }
        return {
            "present": state.present,
            "up": state.up,
            "bitrate": state.bitrate,
            "fd_enabled": state.fd_enabled,
            "listen_only": state.listen_only,
            "controller_state": state.controller_state,
            "restart_ms": state.restart_ms,
        }

    def _role_status(self, resolution: RoleResolution) -> dict[str, object]:
        spec = resolution.spec
        payload: dict[str, object] = {
            "resolution": resolution.state,
            "channel": resolution.channel,
            "expected": spec.as_dict(),
            "actual": self._actual_payload(None),
            "passive_ready": False,
            "safe": False,
            "reason": f"role_{resolution.state}",
            "detail": resolution.detail,
        }
        if resolution.state != "resolved":
            return payload

        channel = resolution.require_channel()
        try:
            state = self.interface_state_reader(channel)
        except Exception as exc:
            payload.update(
                reason="interface_state_unavailable",
                detail=f"cannot inspect {channel}: {type(exc).__name__}: {exc}",
            )
            return payload
        if not isinstance(state, canbus.InterfaceState):
            payload.update(
                reason="invalid_interface_state",
                detail="interface state reader returned an unsupported value",
            )
            return payload
        payload["actual"] = self._actual_payload(state)
        if state.channel != channel:
            payload.update(
                reason="interface_identity_changed",
                detail=(
                    f"resolved {channel} but state reader returned "
                    f"{state.channel}"
                ),
            )
            return payload
        if not state.present:
            payload.update(
                reason="interface_disappeared",
                detail=f"{channel} disappeared after USB role resolution",
            )
            return payload

        if not spec.passive_required:
            if state.up:
                payload.update(
                    reason="spare_up",
                    detail=f"unused {channel} must remain down",
                )
            else:
                payload.update(
                    safe=True,
                    reason="spare_down",
                    detail=f"unused {channel} is down",
                )
            return payload

        if not state.up:
            payload.update(
                reason="interface_down",
                detail=f"{channel} is down; manager leaves it unchanged",
            )
        elif state.bitrate != spec.bitrate:
            payload.update(
                reason="wrong_bitrate",
                detail=(
                    f"{channel} is at {state.bitrate}; {spec.role} requires "
                    f"{spec.bitrate} bit/s"
                ),
            )
        elif state.fd_enabled is not False:
            payload.update(
                reason="fd_mode_not_classical",
                detail=(
                    f"{channel} does not prove classical CAN with FD disabled"
                ),
            )
        elif not state.listen_only:
            payload.update(
                reason="interface_armed",
                detail=f"{channel} is not listen-only",
            )
        elif state.controller_state != "ERROR-ACTIVE":
            payload.update(
                reason="controller_not_error_active",
                detail=(
                    f"{channel} controller is "
                    f"{state.controller_state or 'unavailable'}"
                ),
            )
        elif state.restart_ms != 0:
            payload.update(
                reason="restart_policy_mismatch",
                detail=(
                    f"{channel} restart-ms is {state.restart_ms}; passive policy requires 0"
                ),
            )
        else:
            payload.update(
                passive_ready=True,
                safe=True,
                reason="ready",
                detail=(
                    f"{spec.role} is resolved to {channel} and verified "
                    "listen-only"
                ),
            )
        return payload

    def status_snapshot(self) -> dict[str, object]:
        topology = self.topology()
        roles = {
            item.spec.role: self._role_status(item)
            for item in topology.resolutions
        }
        active_ready = all(
            bool(roles[role]["passive_ready"]) for role in CAN_BUS_ROLES
        )
        all_safe = all(bool(roles[role]["safe"]) for role in ALL_CAN_ROLES)
        ready = active_ready and all_safe
        return {
            "mode": "read_only",
            "configures_interfaces": False,
            "generated_at": self.wall_clock().isoformat(),
            "generation": topology.fingerprint,
            "resolved": topology.all_resolved(ALL_CAN_ROLES),
            "vehicle_buses_ready": active_ready,
            "passive_ready": ready,
            "ready": ready,
            "roles": roles,
            "inventory": [item.as_dict() for item in topology.inventory],
            "issues": [item.as_dict() for item in topology.issues],
        }

    @contextmanager
    def observe(self, bus: str):
        """Yield a freshly resolved shared passive lease for one vehicle bus."""
        role = normalize_can_role(bus)
        spec = self._spec_by_role.get(role)
        if spec is None or not spec.passive_required:
            raise PassiveInterfaceUnavailable(
                role,
                "not_observable",
                f"{role} is not an active passive-observer role",
            )
        role_lock_name = f"can-role-{role}"
        try:
            with self.locks.observer(role_lock_name):
                before = self.topology()
                before_resolution = before.resolution(role)
                channel = before_resolution.require_channel()
                with self.locks.observer(channel):
                    after = self.topology()
                    after_resolution = after.resolution(role)
                    if (
                        after_resolution.state != "resolved"
                        or after_resolution.device != before_resolution.device
                    ):
                        raise PassiveInterfaceUnavailable(
                            role,
                            "topology_changed",
                            f"{role} USB/netdev mapping changed while acquiring ownership",
                        )
                    status = self._role_status(after_resolution)
                    if not status["passive_ready"]:
                        raise PassiveInterfaceUnavailable(
                            role,
                            str(status["reason"]),
                            str(status["detail"]),
                        )
                    yield PassiveInterfaceLease(
                        role=role,
                        channel=channel,
                        usb_serial=spec.usb_serial,
                        dev_id=spec.dev_id,
                        bitrate=spec.bitrate,
                        pair=spec.pair,
                        topology_generation=after.fingerprint,
                    )
        except CanRoleResolutionError as exc:
            raise PassiveInterfaceUnavailable(
                role, f"role_{exc.state}", exc.detail
            ) from None
        except diagnostic_safety.ChannelLockError as exc:
            raise PassiveInterfaceUnavailable(role, "can_busy", str(exc)) from None


__all__ = (
    "ALL_CAN_ROLES",
    "B_CAN_ROLE",
    "BOARD_A_SERIAL",
    "BOARD_B_SERIAL",
    "CAN_BUS_ROLES",
    "CAN_CH_ROLE",
    "CAN_ROLE_SPECS",
    "C_CAN_ROLE",
    "PassiveInterfaceLease",
    "PassiveInterfaceManager",
    "PassiveInterfaceUnavailable",
    "SPARE_ROLE",
    "normalize_can_role",
)
