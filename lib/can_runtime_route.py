"""Bind logical vehicle buses to current SocketCAN names for live operations.

``lib.modules`` stores physical bus roles, never Linux enumeration names.  The
installed dual-USBCANFD adapters are identified by USB serial plus controller
``dev_id`` in :mod:`lib.vehicle_can_roles`.  This module is the
small bridge used by non-telemetry tools: resolve once, acquire both the stable
role lock and current-channel lock, then re-resolve before returning ownership.

Active ownership can temporarily arm exactly one resolved classical-CAN
interface, but only after identity, contention, inhibit, physical-pair, and
passive-baseline gates all succeed.  The same ownership capability holds both
locks through the operation and exactly restores the passive baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib import can_operation_state, canbus, diagnostic_safety
from lib.modules import Module, bind_channel


class RuntimeRouteError(RuntimeError):
    """A module could not be bound to one stable installed CAN role."""


@dataclass(frozen=True)
class RuntimeModuleRoute:
    """One immutable stable-identity resolution for a registry module."""

    module: Module
    role: str
    channel: str
    pair: str
    topology_fingerprint: str
    device_identity: tuple[str, str, str, str, int]


@dataclass(frozen=True)
class RuntimeBusRoute:
    """One resolved installed bus without diagnostic-module addressing."""

    role: str
    channel: str
    pair: str
    bitrate: int
    topology_fingerprint: str
    device_identity: tuple[str, str, str, str, int]


@dataclass
class PassiveBusOwnership:
    """Shared logical-role plus channel observer ownership."""

    route: RuntimeBusRoute
    role_lock: object
    channel_lock: object
    manager: object
    _closed: bool = False

    def revalidate(self) -> None:
        revalidate_bus_route(self.route, manager=self.manager)
        state = canbus.interface_state(self.route.channel)
        if not _is_exact_passive_state(
            state,
            self.route.channel,
            self.route.bitrate,
        ):
            raise RuntimeRouteError(
                f"{self.route.role}/{self.route.channel} is no longer exact passive classical CAN"
            )

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        diagnostic_safety.release_channel_lock(self.channel_lock)
        diagnostic_safety.release_channel_lock(self.role_lock)

    def __enter__(self) -> "PassiveBusOwnership":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


@dataclass
class ActiveBusOwnership:
    """Exclusive logical-role plus resolved-channel ownership capability.

    This is the bus-scoped counterpart to :class:`ActiveModuleOwnership` for
    reviewed operations, such as network-management wake traffic, which are a
    property of one physical bus rather than an arbitrary diagnostic module.
    The capability never guesses a module or a Linux ``canN`` identity.
    """

    route: RuntimeBusRoute
    role_lock: object
    channel_lock: object
    manager: object
    initial_state: canbus.InterfaceState | None = None
    armed: bool = False
    _closed: bool = False

    def restore(self) -> bool:
        """Exactly restore the verified passive starting state under both locks."""

        if not self.armed:
            return True
        restored = False
        try:
            revalidate_bus_route(self.route, manager=self.manager)
            if self.initial_state is None:
                raise RuntimeRouteError("active ownership has no captured starting state")
            restored = bool(
                canbus.restore_interface_state(
                    self.initial_state,
                    noninteractive=True,
                )
            )
            if restored:
                revalidate_bus_route(self.route, manager=self.manager)
                restored = _is_exact_passive_state(
                    canbus.interface_state(self.route.channel),
                    self.route.channel,
                    self.route.bitrate,
                )
        except BaseException:
            restored = False
        if restored:
            self.armed = False
            return True
        _latch_restoration_failure(self.route)
        return False

    def release(self) -> bool:
        if self._closed:
            return not self.armed
        restored = self.restore()
        self._closed = True
        diagnostic_safety.release_channel_lock(self.channel_lock)
        diagnostic_safety.release_channel_lock(self.role_lock)
        return restored

    def __enter__(self) -> "ActiveBusOwnership":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if not self.release():
            raise canbus.PassiveRestoreError(
                f"could not verify {self.route.role}/{self.route.channel} passive restoration"
            )


@dataclass
class ActiveModuleOwnership:
    """Exclusive logical-role plus resolved-channel ownership capability."""

    route: RuntimeModuleRoute
    role_lock: object
    channel_lock: object
    manager: object
    initial_state: canbus.InterfaceState | None = None
    armed: bool = False
    _closed: bool = False

    def restore(self) -> bool:
        """Exactly restore the verified passive starting state under both locks."""

        if not self.armed:
            return True
        restored = False
        try:
            revalidate_module_route(self.route, manager=self.manager)
            if self.initial_state is None:
                raise RuntimeRouteError("active ownership has no captured starting state")
            restored = bool(
                canbus.restore_interface_state(
                    self.initial_state,
                    noninteractive=True,
                )
            )
            if restored:
                revalidate_module_route(self.route, manager=self.manager)
                restored = _is_exact_passive_state(
                    canbus.interface_state(self.route.channel),
                    self.route.channel,
                    self.route.module.bitrate,
                )
        except Exception:
            restored = False
        if restored:
            self.armed = False
            return True
        _latch_restoration_failure(self.route)
        return False

    def release(self) -> bool:
        if self._closed:
            return not self.armed
        restored = self.restore()
        self._closed = True
        diagnostic_safety.release_channel_lock(self.channel_lock)
        diagnostic_safety.release_channel_lock(self.role_lock)
        return restored

    def __enter__(self) -> "ActiveModuleOwnership":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if not self.release():
            raise canbus.PassiveRestoreError(
                f"could not verify {self.route.role}/{self.route.channel} passive restoration"
            )


def _manager_or_default(manager=None):
    if manager is not None:
        return manager
    from lib.vehicle_can_roles import InstalledCanRoleResolver

    return InstalledCanRoleResolver()


def resolve_module_route(module: Module, *, manager=None) -> RuntimeModuleRoute:
    """Resolve ``module.bus`` using one read-only serial/dev_id snapshot."""

    if not isinstance(module, Module):
        raise TypeError("module must be a Module")
    manager = _manager_or_default(manager)
    try:
        topology = manager.topology()
        resolution = topology.resolution(module.bus)
        channel = resolution.require_channel()
        device = resolution.device
        spec = resolution.spec
    except Exception as exc:
        raise RuntimeRouteError(
            f"cannot resolve logical bus {module.bus!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if device is None:
        raise RuntimeRouteError(f"logical bus {module.bus!r} has no resolved USB identity")
    if spec.bitrate != module.bitrate:
        raise RuntimeRouteError(
            f"{module.key} expects {module.bitrate} bit/s but role {module.bus} "
            f"is configured for {spec.bitrate}"
        )
    if not isinstance(spec.pair, str) or not spec.pair:
        raise RuntimeRouteError(f"logical bus {module.bus!r} has no physical pair")
    return RuntimeModuleRoute(
        module=bind_channel(module, channel),
        role=module.bus,
        channel=channel,
        pair=spec.pair,
        topology_fingerprint=topology.fingerprint,
        device_identity=device.identity,
    )


def resolve_bus_route(bus: str, *, manager=None) -> RuntimeBusRoute:
    """Resolve one installed logical vehicle bus without opening or changing it."""

    manager = _manager_or_default(manager)
    try:
        topology = manager.topology()
        resolution = topology.resolution(bus)
        channel = resolution.require_channel()
        device = resolution.device
        spec = resolution.spec
    except Exception as exc:
        raise RuntimeRouteError(
            f"cannot resolve logical bus {bus!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if device is None or spec.bitrate is None or not spec.pair:
        raise RuntimeRouteError(f"logical bus {bus!r} is not a connected vehicle tap")
    return RuntimeBusRoute(
        role=spec.role,
        channel=channel,
        pair=spec.pair,
        bitrate=spec.bitrate,
        topology_fingerprint=topology.fingerprint,
        device_identity=device.identity,
    )


def revalidate_bus_route(route: RuntimeBusRoute, *, manager=None) -> None:
    if not isinstance(route, RuntimeBusRoute):
        raise TypeError("route must be a RuntimeBusRoute")
    manager = _manager_or_default(manager)
    try:
        resolution = manager.topology().resolution(route.role)
        device = resolution.device
    except Exception as exc:
        raise RuntimeRouteError(
            f"cannot revalidate logical bus {route.role!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        resolution.state != "resolved"
        or resolution.channel != route.channel
        or device is None
        or device.identity != route.device_identity
        or resolution.spec.bitrate != route.bitrate
        or resolution.spec.pair != route.pair
    ):
        raise RuntimeRouteError(
            f"logical bus {route.role!r} changed during passive ownership"
        )


def acquire_passive_bus_route(
    bus: str,
    *,
    asserted_pair: str | None = None,
    manager=None,
) -> PassiveBusOwnership:
    """Resolve and hold shared observer locks for one exact passive bus."""

    manager = _manager_or_default(manager)
    route = resolve_bus_route(bus, manager=manager)
    role_lock = None
    channel_lock = None
    try:
        if asserted_pair is not None and asserted_pair.strip() != route.pair:
            raise RuntimeRouteError(
                f"resolved {route.role} requires pair {route.pair}; "
                f"asserted pair was {asserted_pair!r}"
            )
        role_lock = diagnostic_safety.acquire_channel_observer_lock(
            f"can-role-{route.role}"
        )
        channel_lock = diagnostic_safety.acquire_channel_observer_lock(route.channel)
        ownership = PassiveBusOwnership(
            route,
            role_lock,
            channel_lock,
            manager,
        )
        ownership.revalidate()
        return ownership
    except BaseException:
        if channel_lock is not None:
            diagnostic_safety.release_channel_lock(channel_lock)
        if role_lock is not None:
            diagnostic_safety.release_channel_lock(role_lock)
        raise


def revalidate_module_route(route: RuntimeModuleRoute, *, manager=None) -> None:
    """Fail if USB identity, channel, rate, or pair changed since resolution."""

    if not isinstance(route, RuntimeModuleRoute):
        raise TypeError("route must be a RuntimeModuleRoute")
    manager = _manager_or_default(manager)
    try:
        topology = manager.topology()
        resolution = topology.resolution(route.role)
        device = resolution.device
    except Exception as exc:
        raise RuntimeRouteError(
            f"cannot revalidate logical bus {route.role!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        resolution.state != "resolved"
        or resolution.channel != route.channel
        or device is None
        or device.identity != route.device_identity
        or resolution.spec.bitrate != route.module.bitrate
        or resolution.spec.pair != route.pair
    ):
        raise RuntimeRouteError(
            f"logical bus {route.role!r} changed while acquiring CAN ownership"
        )


def acquire_active_module_route(
    module: Module,
    *,
    manager=None,
) -> ActiveModuleOwnership:
    """Resolve and exclusively own a module's logical role and current netdev."""

    manager = _manager_or_default(manager)
    route = resolve_module_route(module, manager=manager)
    role_lock = None
    channel_lock = None
    try:
        role_lock = diagnostic_safety.acquire_channel_lock(f"can-role-{route.role}")
        channel_lock = diagnostic_safety.acquire_channel_lock(route.channel)
        revalidate_module_route(route, manager=manager)
        return ActiveModuleOwnership(
            route,
            role_lock,
            channel_lock,
            manager,
        )
    except Exception:
        if channel_lock is not None:
            diagnostic_safety.release_channel_lock(channel_lock)
        if role_lock is not None:
            diagnostic_safety.release_channel_lock(role_lock)
        raise


def acquire_active_bus_route(
    bus: str,
    *,
    manager=None,
) -> ActiveBusOwnership:
    """Resolve and exclusively own one logical bus and its current netdev."""

    manager = _manager_or_default(manager)
    route = resolve_bus_route(bus, manager=manager)
    role_lock = None
    channel_lock = None
    try:
        role_lock = diagnostic_safety.acquire_channel_lock(f"can-role-{route.role}")
        channel_lock = diagnostic_safety.acquire_channel_lock(route.channel)
        revalidate_bus_route(route, manager=manager)
        return ActiveBusOwnership(
            route,
            role_lock,
            channel_lock,
            manager,
        )
    except BaseException:
        if channel_lock is not None:
            diagnostic_safety.release_channel_lock(channel_lock)
        if role_lock is not None:
            diagnostic_safety.release_channel_lock(role_lock)
        raise


def _is_exact_passive_state(
    state: canbus.InterfaceState,
    channel: str,
    bitrate: int,
) -> bool:
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.channel == channel
        and state.present
        and state.up
        and state.bitrate == bitrate
        and state.fd_enabled is False
        and state.one_shot is False
        and state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == 0
    )


def _is_exact_armed_state(
    state: canbus.InterfaceState,
    channel: str,
    bitrate: int,
    restart_ms: int = 0,
    one_shot: bool = False,
) -> bool:
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.channel == channel
        and state.present
        and state.up
        and state.bitrate == bitrate
        and state.fd_enabled is False
        and state.one_shot is one_shot
        and not state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == restart_ms
    )


def _latch_restoration_failure(
    route: RuntimeModuleRoute | RuntimeBusRoute,
) -> None:
    try:
        can_operation_state.begin_inhibit(
            "runtime-route-restoration-failed",
            channel="*",
            reason=(
                f"failed to restore {route.role} identity {route.device_identity} "
                f"after active use on {route.channel}; inspect every CAN role before "
                "manually clearing this same-boot inhibit"
            ),
        )
    except Exception:
        # Restoration has already failed.  The caller still receives a loud
        # failure even if durable inhibit publication is unavailable.
        pass


def acquire_armed_bus_route(
    bus: str,
    *,
    asserted_pair: str,
    prearm_check,
    manager=None,
    one_shot: bool = False,
    wake_probe_policy: str | None = None,
    passive_prearm_check=None,
) -> ActiveBusOwnership:
    """Own, verify, and arm one resolved bus from an exact passive baseline.

    This low-level capability deliberately requires both the physical-pair
    assertion and a caller-specific contention/state callback.  Higher-level
    reviewed profiles hide every physical detail from their consumers. The
    installed ``gs_usb`` controllers do not support automatic bus-off restart,
    so this capability always arms with explicit ``restart-ms 0``. ``one_shot``
    is an internal reviewed-profile setting and must be a real boolean.
    """

    if type(one_shot) is not bool:
        raise TypeError("one_shot must be boolean")
    if wake_probe_policy not in (None, "silent", "role_or_silent"):
        raise ValueError("invalid wake probe policy")
    if passive_prearm_check is not None and not callable(passive_prearm_check):
        raise TypeError("passive_prearm_check must be callable or None")
    ownership = acquire_active_bus_route(bus, manager=manager)
    route = ownership.route
    mutation_attempted = False
    try:
        if not isinstance(asserted_pair, str) or asserted_pair.strip() != route.pair:
            raise RuntimeRouteError(
                f"resolved {route.role} requires physical pair {route.pair}; "
                f"asserted pair was {asserted_pair!r}"
            )
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            raise RuntimeRouteError(f"active CAN operation is inhibited by {names}")
        if not callable(prearm_check):
            raise TypeError("prearm_check must be callable")
        conflicts = tuple(prearm_check())
        if conflicts:
            raise RuntimeRouteError("; ".join(str(item) for item in conflicts))
        topology = can_operation_state.load_topology(route.channel)
        if (
            not topology.usable
            or topology.bus != route.role
            or topology.pair != route.pair
        ):
            raise RuntimeRouteError(
                f"same-boot topology for {route.channel} does not prove "
                f"{route.role} on pair {route.pair}: {topology.reason or topology.bus}"
            )
        initial = canbus.interface_state(route.channel)
        if not _is_exact_passive_state(initial, route.channel, route.bitrate):
            raise RuntimeRouteError(
                f"{route.role}/{route.channel} must start UP, classical FD-off, "
                "listen-only, ERROR-ACTIVE, restart-ms 0 at the role bitrate"
            )
        ownership.initial_state = initial
        if wake_probe_policy is not None:
            observed_bus = canbus.identify_bus(route.channel, probe=0.5)
            revalidate_bus_route(route, manager=ownership.manager)
            checked = canbus.interface_state(route.channel)
            if not initial.same_configuration(checked):
                raise RuntimeRouteError(
                    f"{route.role}/{route.channel} changed during the final passive wake probe"
                )
            if wake_probe_policy == "silent" and observed_bus == route.role:
                raise RuntimeRouteError(
                    f"{route.role}/{route.channel} became awake before wake arming"
                )
            allowed = (
                ("silent",)
                if wake_probe_policy == "silent"
                else ("silent", route.role)
            )
            if observed_bus not in allowed:
                raise RuntimeRouteError(
                    f"{route.role}/{route.channel} wake probe returned {observed_bus}"
                )
        if passive_prearm_check is not None:
            conflicts = tuple(passive_prearm_check(route))
            if conflicts:
                raise RuntimeRouteError("; ".join(str(item) for item in conflicts))
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            raise RuntimeRouteError(f"active CAN operation is inhibited by {names}")
        conflicts = tuple(prearm_check())
        if conflicts:
            raise RuntimeRouteError("; ".join(str(item) for item in conflicts))
        revalidate_bus_route(route, manager=ownership.manager)
        checked = canbus.interface_state(route.channel)
        if not initial.same_configuration(checked):
            raise RuntimeRouteError(
                f"{route.role}/{route.channel} changed immediately before wake arming"
            )
        mutation_attempted = True
        if not canbus.ip_up(
            route.channel,
            route.bitrate,
            listen_only=False,
            restart_ms=0,
            one_shot=one_shot,
            noninteractive=True,
        ):
            raise RuntimeRouteError(f"failed to arm {route.role}/{route.channel}")
        ownership.armed = True
        revalidate_bus_route(route, manager=ownership.manager)
        if not _is_exact_armed_state(
            canbus.interface_state(route.channel),
            route.channel,
            route.bitrate,
            0,
            one_shot,
        ):
            raise RuntimeRouteError(
                f"could not prove {route.role}/{route.channel} exact classical armed state"
            )
        topology = can_operation_state.load_topology(route.channel)
        if (
            not topology.usable
            or topology.bus != route.role
            or topology.pair != route.pair
        ):
            raise RuntimeRouteError(
                f"same-boot topology changed while arming {route.role}/{route.channel}"
            )
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            raise RuntimeRouteError(f"active CAN operation is inhibited by {names}")
        conflicts = tuple(prearm_check())
        if conflicts:
            raise RuntimeRouteError("; ".join(str(item) for item in conflicts))
        return ownership
    except BaseException:
        if mutation_attempted:
            ownership.armed = True
        restored = ownership.release()
        if not restored:
            raise canbus.PassiveRestoreError(
                f"failed to restore {route.role}/{route.channel} after arm failure"
            )
        raise


def acquire_armed_module_route(
    module: Module,
    *,
    asserted_pair: str,
    prearm_check,
    manager=None,
) -> ActiveModuleOwnership:
    """Own, verify, and arm one resolved role from an exact passive baseline.

    ``prearm_check`` is mandatory so a new caller cannot omit its service and
    external-owner contention gate.  It is evaluated while both locks are held
    and before any interface mutation.  The returned capability holds both
    locks for the entire live operation.
    ``release`` exactly restores the captured passive state.  Any unverified
    cleanup latches a same-boot global inhibit before the locks are released.
    """

    ownership = acquire_active_module_route(module, manager=manager)
    route = ownership.route
    try:
        if not isinstance(asserted_pair, str) or asserted_pair.strip() != route.pair:
            raise RuntimeRouteError(
                f"resolved {route.role} requires physical pair {route.pair}; "
                f"asserted pair was {asserted_pair!r}"
            )
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            raise RuntimeRouteError(f"active CAN operation is inhibited by {names}")
        if not callable(prearm_check):
            raise TypeError("prearm_check must be callable")
        conflicts = tuple(prearm_check())
        if conflicts:
            raise RuntimeRouteError("; ".join(str(item) for item in conflicts))
        initial = canbus.interface_state(route.channel)
        if not _is_exact_passive_state(initial, route.channel, route.module.bitrate):
            raise RuntimeRouteError(
                f"{route.role}/{route.channel} must start UP, classical FD-off, "
                "listen-only, ERROR-ACTIVE, restart-ms 0 at the role bitrate"
            )
        ownership.initial_state = initial
        if not canbus.ip_up(
            route.channel,
            route.module.bitrate,
            listen_only=False,
            restart_ms=0,
            noninteractive=True,
        ):
            raise RuntimeRouteError(f"failed to arm {route.role}/{route.channel}")
        ownership.armed = True
        revalidate_module_route(route, manager=ownership.manager)
        if not _is_exact_armed_state(
            canbus.interface_state(route.channel),
            route.channel,
            route.module.bitrate,
        ):
            raise RuntimeRouteError(
                f"could not prove {route.role}/{route.channel} exact classical armed state"
            )
        # Recheck immediately before handing an armed capability to the
        # caller.  An inhibit can appear while link commands/readback are in
        # progress; that must trigger exact restoration under both locks.
        inhibits = can_operation_state.active_inhibits(route.channel)
        if inhibits:
            names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
            raise RuntimeRouteError(f"active CAN operation is inhibited by {names}")
        return ownership
    except Exception:
        # ip_up can fail after taking the link down.  Once an initial state was
        # captured, always attempt exact restoration before releasing ownership.
        if ownership.initial_state is not None:
            ownership.armed = True
        restored = ownership.release()
        if not restored:
            raise canbus.PassiveRestoreError(
                f"failed to restore {route.role}/{route.channel} after arm failure"
            )
        raise
