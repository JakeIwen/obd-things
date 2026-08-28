"""Runtime adapters for the serial-resolved three-bus telemetry installation.

The identity resolver in :mod:`projects.vehicle_data.can_interfaces` is
deliberately read-only.  This module adds the narrow runtime pieces used by the
broker:

* a guarded reconciler which may configure only the four exact gs_usb roles;
* a role-aware C-CAN voltage source; and
* a role-aware passive C-CAN powertrain reader.

Reconciliation is link configuration, not CAN traffic.  Every connected bus
is forced to classical CAN with listen-only enabled, and an already-armed
interface is never changed.  The unused fourth channel is kept down.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import subprocess
import time
from typing import Callable

from lib import can_handoff, can_operation_state, can_wake, canbus, diagnostic_safety
from lib.can_role_resolver import CanRoleResolutionError
from projects.vehicle_data import ccan_powertrain
from projects.vehicle_data.can_interfaces import (
    ALL_CAN_ROLES,
    B_CAN_ROLE,
    CAN_BUS_ROLES,
    C_CAN_ROLE,
    SPARE_ROLE,
    PassiveInterfaceManager,
    PassiveInterfaceUnavailable,
)
from projects.vehicle_data.models import AcquisitionResult, failure, success
from projects.vehicle_data.sources import VoltageAcquirer


_SAFE_CHANNEL = re.compile(r"^[A-Za-z0-9_.-]+$")


def _role_lock(role: str) -> str:
    return f"can-role-{role}"


def configure_classical_listen_only(
    channel: str,
    bitrate: int,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Configure one already-resolved gs_usb netdev and verify readback.

    The fixed argv form deliberately has no shell expansion.  ``fd off`` is
    explicit because the vehicle buses are classical CAN even though the
    adapters also support CAN FD.
    """

    if not isinstance(channel, str) or not _SAFE_CHANNEL.fullmatch(channel):
        raise ValueError(f"unsafe SocketCAN channel {channel!r}")
    if bitrate not in (125000, 500000):
        raise ValueError(f"unsupported ProMaster bitrate {bitrate!r}")
    commands = (
        ["sudo", "-n", "ip", "link", "set", "dev", channel, "down"],
        [
            "sudo",
            "-n",
            "ip",
            "link",
            "set",
            "dev",
            channel,
            "type",
            "can",
            "bitrate",
            str(bitrate),
            "fd",
            "off",
            "listen-only",
            "on",
            "one-shot",
            "off",
            "restart-ms",
            "0",
        ],
        ["sudo", "-n", "ip", "link", "set", "dev", channel, "up"],
    )
    for command in commands:
        completed = run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            return False
    state = canbus.interface_state(channel)
    return bool(
        state.present
        and state.up
        and state.bitrate == bitrate
        and state.fd_enabled is False
        and state.one_shot is False
        and state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == 0
    )


def keep_interface_down(
    channel: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    if not isinstance(channel, str) or not _SAFE_CHANNEL.fullmatch(channel):
        raise ValueError(f"unsafe SocketCAN channel {channel!r}")
    completed = run(
        ["sudo", "-n", "ip", "link", "set", "dev", channel, "down"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False
    state = canbus.interface_state(channel)
    return bool(state.present and not state.up)


@dataclass(frozen=True)
class ReconcileOutcome:
    role: str
    state: str
    channel: str | None
    detail: str
    changed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "state": self.state,
            "channel": self.channel,
            "detail": self.detail,
            "changed": self.changed,
        }


class PassiveRoleReconciler:
    """Sole broker-side configurator for the exact dual-adapter role set."""

    def __init__(
        self,
        manager: PassiveInterfaceManager,
        *,
        configure=configure_classical_listen_only,
        keep_down=keep_interface_down,
        interface_state_reader=canbus.interface_state,
        inhibit_reader=can_operation_state.active_inhibits,
        topology_writer=can_operation_state.set_topology,
        wall_clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.manager = manager
        self.configure = configure
        self.keep_down = keep_down
        self.interface_state_reader = interface_state_reader
        self.inhibit_reader = inhibit_reader
        self.topology_writer = topology_writer
        self.wall_clock = wall_clock
        self._initialized = False
        self._last_generation: str | None = None
        self._last_result: dict[str, object] | None = None

    @staticmethod
    def _role_ready(role: str, payload: object) -> bool:
        """Return readiness for one role without consulting aggregate state."""

        if not isinstance(payload, dict):
            return False
        if role == SPARE_ROLE:
            return payload.get("safe") is True
        return payload.get("passive_ready") is True

    @classmethod
    def _roles_needing_reconciliation(
        cls, status: dict[str, object]
    ) -> tuple[str, ...]:
        roles = status.get("roles")
        roles = roles if isinstance(roles, dict) else {}
        return tuple(
            role
            for role in ALL_CAN_ROLES
            if not cls._role_ready(role, roles.get(role))
        )

    def _one(self, role: str) -> ReconcileOutcome:
        try:
            before = self.manager.topology()
            resolution = before.resolution(role)
            channel = resolution.require_channel()
        except (CanRoleResolutionError, KeyError, OSError, ValueError) as exc:
            return ReconcileOutcome(
                role,
                "unavailable",
                None,
                str(exc),
            )

        try:
            with ExitStack() as stack:
                stack.enter_context(
                    diagnostic_safety.channel_lock(_role_lock(role))
                )
                stack.enter_context(diagnostic_safety.channel_lock(channel))
                after = self.manager.topology()
                refreshed = after.resolution(role)
                if (
                    refreshed.state != "resolved"
                    or refreshed.device != resolution.device
                ):
                    return ReconcileOutcome(
                        role,
                        "topology_changed",
                        channel,
                        "USB/netdev identity changed while taking configuration ownership",
                    )
                state = self.interface_state_reader(channel)
                if not state.present:
                    return ReconcileOutcome(
                        role,
                        "unavailable",
                        channel,
                        "resolved interface disappeared before configuration",
                    )
                inhibits = self.inhibit_reader(channel)
                if inhibits:
                    names = ",".join(
                        str(item.get("name", "invalid")) for item in inhibits
                    )
                    return ReconcileOutcome(
                        role,
                        "inhibited",
                        channel,
                        f"interface operation is inhibited by {names}",
                    )
                if role == SPARE_ROLE:
                    if not state.up:
                        return ReconcileOutcome(
                            role,
                            "ready",
                            channel,
                            "unused channel is already down",
                        )
                    if not state.listen_only:
                        return ReconcileOutcome(
                            role,
                            "armed",
                            channel,
                            "unused channel is up and not listen-only; refusing to change it",
                        )
                    changed = bool(self.keep_down(channel))
                    return ReconcileOutcome(
                        role,
                        "reconciled" if changed else "failed",
                        channel,
                        (
                            "unused channel was placed down"
                            if changed
                            else "could not place unused channel down"
                        ),
                        changed=changed,
                    )

                spec = refreshed.spec
                already_ready = bool(
                    state.up
                    and state.bitrate == spec.bitrate
                    and state.fd_enabled is False
                    and state.one_shot is False
                    and state.listen_only
                    and state.controller_state == "ERROR-ACTIVE"
                    and state.restart_ms == 0
                )
                if already_ready:
                    try:
                        self.topology_writer(
                            channel,
                            role,
                            pair=str(spec.pair),
                            source="usb_serial_and_dev_id",
                            note=(
                                f"{spec.board} {spec.connector}; serial-resolved "
                                "dual USBCANFD role"
                            ),
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        return ReconcileOutcome(
                            role,
                            "failed",
                            channel,
                            f"passive link is ready but topology recording failed: {exc}",
                        )
                    return ReconcileOutcome(
                        role,
                        "ready",
                        channel,
                        "interface already matches the passive classical-CAN policy",
                    )
                if state.up and not state.listen_only:
                    return ReconcileOutcome(
                        role,
                        "armed",
                        channel,
                        "interface is armed; passive reconciler left it unchanged",
                    )
                changed = bool(self.configure(channel, int(spec.bitrate)))
                if changed:
                    try:
                        self.topology_writer(
                            channel,
                            role,
                            pair=str(spec.pair),
                            source="usb_serial_and_dev_id",
                            note=(
                                f"{spec.board} {spec.connector}; serial-resolved "
                                "dual USBCANFD role"
                            ),
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        return ReconcileOutcome(
                            role,
                            "failed",
                            channel,
                            f"link reconciled but topology recording failed: {exc}",
                            changed=True,
                        )
                return ReconcileOutcome(
                    role,
                    "reconciled" if changed else "failed",
                    channel,
                    (
                        "configured classical CAN, FD off, listen-only on, one-shot off"
                        if changed
                        else "passive configuration or verification failed"
                    ),
                    changed=changed,
                )
        except diagnostic_safety.ChannelLockError as exc:
            return ReconcileOutcome(role, "busy", channel, str(exc))
        except (OSError, RuntimeError, ValueError) as exc:
            return ReconcileOutcome(
                role,
                "failed",
                channel,
                f"{type(exc).__name__}: {exc}",
            )

    def reconcile(self) -> dict[str, object]:
        outcomes = [self._one(role) for role in ALL_CAN_ROLES]
        result = {
            "generated_at": self.wall_clock().isoformat(),
            "interaction": "SocketCAN link configuration only; no CAN frames sent",
            "roles": {item.role: item.as_dict() for item in outcomes},
            "ready": all(
                item.state in ("ready", "reconciled") for item in outcomes
            ),
        }
        self._initialized = True
        self._last_result = result
        return result

    def _reconcile_roles(self, roles: tuple[str, ...]) -> dict[str, object]:
        """Retry only unhealthy roles while retaining the full status shape."""

        outcomes = [self._one(role) for role in roles]
        previous_roles: dict[str, object] = {}
        if isinstance(self._last_result, dict):
            cached = self._last_result.get("roles")
            if isinstance(cached, dict):
                previous_roles.update(cached)
        previous_roles.update(
            {item.role: item.as_dict() for item in outcomes}
        )
        result = {
            "generated_at": self.wall_clock().isoformat(),
            "interaction": (
                "targeted SocketCAN link configuration only; no CAN frames sent"
            ),
            "roles": {
                role: previous_roles[role]
                for role in ALL_CAN_ROLES
                if role in previous_roles
            },
            "ready": all(
                isinstance(previous_roles.get(role), dict)
                and previous_roles[role].get("state")
                in ("ready", "reconciled")
                for role in ALL_CAN_ROLES
            ),
            "changed": any(item.changed for item in outcomes),
        }
        self._last_result = result
        return result

    def reconcile_if_needed(self) -> dict[str, object]:
        """Reconcile unhealthy roles within one stable topology generation."""
        try:
            status = self.manager.status_snapshot()
        except (OSError, RuntimeError, ValueError) as exc:
            status = {
                "ready": False,
                "issues": [{"reason": "status_failed", "detail": str(exc)}],
            }
        generation = status.get("generation")
        generation = generation if isinstance(generation, str) else None
        if (
            self._initialized
            and generation is not None
            and generation == self._last_generation
        ):
            pending = self._roles_needing_reconciliation(status)
            if not pending:
                return {
                    "generated_at": self.wall_clock().isoformat(),
                    "interaction": "read-only readiness check; no link change",
                    "ready": bool(status.get("ready")),
                    "roles": status.get("roles", {}),
                    "changed": False,
                }
            result = self._reconcile_roles(pending)
        else:
            # Startup, unreadable generations, and any role-to-netdev mapping
            # change require a full pass before per-role retries are allowed.
            result = self.reconcile()
        try:
            final_status = self.manager.status_snapshot()
        except (OSError, RuntimeError, ValueError):
            final_status = {}
        final_generation = final_status.get("generation")
        final_generation = (
            final_generation if isinstance(final_generation, str) else None
        )
        # A reset during reconciliation gets another full pass next cycle. A
        # stable generation permits later retries to touch only unhealthy roles;
        # aggregate readiness is deliberately not part of this decision.
        if (
            generation is not None
            and final_generation == generation
        ):
            self._last_generation = final_generation
        else:
            self._last_generation = None
        result["generation"] = final_generation
        return result


class RoleAwareVoltageAcquirer:
    """Read fixed voltage roles and optionally use the fixed B-CAN wake."""

    def __init__(
        self,
        manager: PassiveInterfaceManager,
        *,
        probe_seconds: float = 0.75,
        read_timeout: float = 2.0,
        delegate_factory=VoltageAcquirer,
        inhibit_reader=can_operation_state.active_inhibits,
        wake_once=can_wake.wake_once,
        wake_prearm_check=None,
    ) -> None:
        self.manager = manager
        self.probe_seconds = probe_seconds
        self.read_timeout = read_timeout
        self.delegate_factory = delegate_factory
        self.inhibit_reader = inhibit_reader
        self.wake_once = wake_once
        self.wake_prearm_check = (
            wake_prearm_check
            if wake_prearm_check is not None
            else lambda: ("broker wake prearm gate is unavailable",)
        )
        self._last_channel: str | None = None

    @property
    def channel(self) -> str:
        try:
            channel = self.manager.channel_for_bus(C_CAN_ROLE)
        except (CanRoleResolutionError, OSError, RuntimeError, ValueError):
            return self._last_channel or "c-can-unresolved"
        self._last_channel = channel
        return channel

    def _delegate(self, channel: str, role: str):
        return self.delegate_factory(
            channel=channel,
            expected_bus=role,
            probe_seconds=self.probe_seconds,
            read_timeout=self.read_timeout,
        )

    def _passive(self, role: str) -> AcquisitionResult:
        try:
            with can_handoff.passive_turn(role):
                with self.manager.observe(role) as lease:
                    if role == C_CAN_ROLE:
                        self._last_channel = lease.channel
                    return self._delegate(lease.channel, role).acquire("passive")
        except diagnostic_safety.ChannelLockError:
            return failure(
                metric="battery.voltage",
                unit="V",
                reason="can_busy",
                detail=f"{role} passive sample yielded to a reserved active handoff",
                bus=role,
                acquisition="passive",
            )
        except PassiveInterfaceUnavailable as exc:
            return failure(
                metric="battery.voltage",
                unit="V",
                reason=(
                    "can_busy" if exc.reason == "can_busy" else "source_unavailable"
                ),
                detail=exc.detail,
                bus=role,
                acquisition="passive",
            )

    def _wake_b_can(self) -> AcquisitionResult:
        try:
            wake = self.wake_once(
                B_CAN_ROLE,
                prearm_check=self.wake_prearm_check,
                manager=self.manager,
            )
        except can_wake.CanWakeError as exc:
            if exc.reason == "bus_not_silent":
                raced = self._passive(B_CAN_ROLE)
                if raced.available:
                    return raced
            reason = "can_busy" if exc.reason == "handoff_busy" else exc.reason
            return failure(
                metric="battery.voltage",
                unit="V",
                reason=reason,
                detail=exc.detail,
                bus=B_CAN_ROLE,
                acquisition="wake_assisted",
            )
        except canbus.PassiveRestoreError as exc:
            return failure(
                metric="battery.voltage",
                unit="V",
                reason="restoration_failed",
                detail=str(exc),
                bus=B_CAN_ROLE,
                acquisition="wake_assisted",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return failure(
                metric="battery.voltage",
                unit="V",
                reason="source_unavailable",
                detail=f"fixed B-CAN wake failed closed: {exc}",
                bus=B_CAN_ROLE,
                acquisition="wake_assisted",
            )
        if wake.voltage is None:
            return failure(
                metric="battery.voltage",
                unit="V",
                reason="invalid_source_result",
                detail="fixed B-CAN wake returned no verified voltage",
                bus=B_CAN_ROLE,
                acquisition="wake_assisted",
            )
        return success(
            metric="battery.voltage",
            unit="V",
            value=wake.voltage,
            source="bcan.broadcast.0x46c",
            bus=B_CAN_ROLE,
            acquisition="wake_assisted",
            quality="verified",
            observed_monotonic=time.monotonic(),
            observed_at=datetime.now(timezone.utc),
            detail=(
                f"{wake.detail}; exact passive restoration verified before publication"
            ),
        )

    def acquire(self, mode: str) -> AcquisitionResult:
        if mode == "passive":
            return self._passive(C_CAN_ROLE)
        if mode != "wake_if_asleep":
            return failure(
                metric="battery.voltage",
                unit="V",
                reason="unsupported_mode",
                detail=(
                    "multi-bus role mode supports only passive or "
                    "wake_if_asleep acquisition"
                ),
                bus=C_CAN_ROLE,
                acquisition=mode,
            )

        # Prefer a free already-awake C-CAN sample.  If that exact role is
        # asleep or independently unavailable, check the dedicated B-CAN role
        # passively before considering its fixed wake profile.  No result from
        # one role is ever used to route or reconfigure the other.
        ccan = self._passive(C_CAN_ROLE)
        if ccan.available:
            return ccan
        bcan = self._passive(B_CAN_ROLE)
        if bcan.available or bcan.reason != "bus_asleep":
            return bcan
        return self._wake_b_can()

    def status_snapshot(self) -> dict[str, object]:
        roles = self.manager.status_snapshot()
        ccan = roles.get("roles", {}).get(C_CAN_ROLE, {})
        actual = ccan.get("actual", {}) if isinstance(ccan, dict) else {}
        channel = ccan.get("channel") if isinstance(ccan, dict) else None
        if isinstance(channel, str):
            self._last_channel = channel
        if isinstance(channel, str):
            try:
                inhibits = [
                    str(item.get("name", "invalid"))
                    for item in self.inhibit_reader(channel)
                ]
            except (OSError, RuntimeError, ValueError):
                inhibits = ["inhibit-state-unavailable"]
        else:
            inhibits = ["c-can-role-unresolved"]
        return {
            "channel": channel or self._last_channel or "c-can-unresolved",
            "adapter_present": ccan.get("resolution") == "resolved",
            "up": actual.get("up"),
            "bitrate": actual.get("bitrate"),
            "fd_enabled": actual.get("fd_enabled"),
            "one_shot": actual.get("one_shot"),
            "listen_only": actual.get("listen_only"),
            "controller_state": actual.get("controller_state"),
            "restart_ms": actual.get("restart_ms"),
            "topology": {
                "bus": C_CAN_ROLE,
                "pair": "6/14",
                "source": "usb_serial_and_dev_id",
                "usable": bool(ccan.get("passive_ready")),
                "reason": ccan.get("detail"),
            },
            "active_inhibits": inhibits,
            "role_interfaces": roles,
        }


class RoleAwareCcanPowertrainReader:
    """Read the fixed passive powertrain allowlist through a role lease."""

    def __init__(
        self,
        manager: PassiveInterfaceManager,
        *,
        probe_seconds: float = 0.25,
        read_timeout: float = 0.5,
    ) -> None:
        self.manager = manager
        self.probe_seconds = probe_seconds
        self.read_timeout = read_timeout
        # This object intentionally outlives each short SocketCAN snapshot so
        # the raw 0x1F7 plausibility comparison cannot reset at a collector
        # cycle boundary.
        self.temperature_gate = (
            ccan_powertrain.TransmissionTemperaturePlausibilityGate()
        )
        self._quality_events: list[ccan_powertrain.DataQualityEvent] = []

    def read(self) -> tuple[ccan_powertrain.PassiveObservation, ...]:
        try:
            with can_handoff.passive_turn(C_CAN_ROLE):
                with self.manager.observe(C_CAN_ROLE) as lease:
                    if (
                        canbus.identify_bus(
                            lease.channel,
                            probe=self.probe_seconds,
                        )
                        != C_CAN_ROLE
                    ):
                        return ()
                    snapshot = ccan_powertrain.read_broadcast_snapshot(
                        lease.channel,
                        timeout=self.read_timeout,
                        temperature_gate=self.temperature_gate,
                    )
                    self._quality_events.extend(snapshot.quality_events)
                    return snapshot.observations
        except (
            diagnostic_safety.ChannelLockError,
            PassiveInterfaceUnavailable,
            OSError,
            RuntimeError,
            ValueError,
        ):
            return ()

    def drain_quality_events(
        self,
    ) -> tuple[ccan_powertrain.DataQualityEvent, ...]:
        """Return each raw rejection exactly once to the broker."""

        events = tuple(self._quality_events)
        self._quality_events.clear()
        return events


class RoleAwareActiveDriveSupervisor:
    """Hold logical C-CAN ownership around one dynamically routed helper."""

    def __init__(
        self,
        manager: PassiveInterfaceManager,
        *,
        event_handler,
        supervisor_factory,
    ) -> None:
        self.manager = manager
        self.event_handler = event_handler
        self.supervisor_factory = supervisor_factory
        self._delegate = None

    @property
    def channel(self) -> str:
        try:
            return self.manager.channel_for_bus(C_CAN_ROLE)
        except (CanRoleResolutionError, OSError, RuntimeError, ValueError):
            return "c-can-unresolved"

    @staticmethod
    def _failure(reason: str, detail: str) -> dict[str, object]:
        return {
            "type": "final",
            "state": "idle",
            "reason": reason,
            "detail": detail,
            "interface_mode": "listen_only",
            "restored": None,
        }

    def stop(self) -> None:
        delegate = self._delegate
        if delegate is not None:
            delegate.stop()

    def run(self, stop_event) -> dict[str, object]:
        try:
            with diagnostic_safety.channel_lock(_role_lock(C_CAN_ROLE)):
                topology = self.manager.topology()
                resolution = topology.resolution(C_CAN_ROLE)
                channel = resolution.require_channel()
                spec = resolution.spec
                delegate = self.supervisor_factory(
                    channel=channel,
                    event_handler=self.event_handler,
                    expected_usb_serial=spec.usb_serial,
                    expected_dev_id=spec.dev_id,
                )
                self._delegate = delegate
                try:
                    return delegate.run(stop_event)
                finally:
                    self._delegate = None
        except diagnostic_safety.ChannelLockError as exc:
            return self._failure("can_busy", str(exc))
        except (CanRoleResolutionError, KeyError, OSError, RuntimeError, ValueError) as exc:
            return self._failure(
                "adapter_unhealthy",
                f"could not resolve stable C-CAN active ownership: {exc}",
            )


__all__ = (
    "PassiveRoleReconciler",
    "ReconcileOutcome",
    "RoleAwareCcanPowertrainReader",
    "RoleAwareActiveDriveSupervisor",
    "RoleAwareVoltageAcquirer",
    "configure_classical_listen_only",
    "keep_interface_down",
)
