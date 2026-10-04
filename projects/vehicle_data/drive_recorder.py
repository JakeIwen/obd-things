#!/usr/bin/env python3
"""Receive-only raw recorder coordinated with the broker's active-drive owner.

This daemon never configures SocketCAN or sends a CAN frame.  It waits until
the telemetry broker proves that its reviewed active-drive helper owns the
serial-resolved C-CAN channel, attaches an independent receive socket to that
broker-owned route, and records the separately resolved B-CAN (shared passive
lease, or the broker's exact auxiliary owner) and CAN-CH (shared passive
lease) routes.  Each writes distinct loss-accounted zstd streams to the
required external mount.

The normal observer lock cannot be acquired during this interval: the broker
correctly holds the exclusive channel lock for its fixed PCM/RF-Hub requests.
Instead, this companion requires the broker's machine-readable ownership state
before accepting the armed C-CAN interface.  It may continue receiving after
the broker restores C-CAN listen-only mode so the same raw session reaches
ignition loss.  Any armed C-CAN state without the broker owner ends the
interval.

C-CAN is the primary, required role.  B-CAN and CAN-CH are best-effort
segments (2026-09-27): a secondary records only while its exact route is
proven; a route loss (broker proof, identity/listen-only revalidation, or a
missing bus-identity start frame) cleanly ends only that role's segment, and
the role is re-admitted after a fresh broker proof plus a fresh lease or
armed-owner check.  Every gap is recorded in ``route-events.jsonl`` and
``capture-set.json`` (``complete`` stays true only for three uninterrupted
roles).  Storage, compression, socket-drop, and cleanup failures on any role
still fail the whole campaign.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import dataclasses
import datetime as dt
import errno
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data.api import TelemetryClient  # noqa: E402
from projects.vehicle_data.broker import DEFAULT_SOCKET  # noqa: E402
from lib import capture_pipeline as capture  # noqa: E402
from lib.broadcast_signals import B_CAN_VOLTAGE, IGNITION_ON  # noqa: E402
from lib.can_role_resolver import SysfsCanRoleResolver  # noqa: E402
from lib.can_runtime_route import NetdevIdentityExpectation  # noqa: E402
from lib.modules import MODULES  # noqa: E402
from lib.timeutil import utc_now  # noqa: E402
from projects.vehicle_data.can_interfaces import (  # noqa: E402
    PassiveInterfaceLease,
    PassiveInterfaceManager,
    PassiveInterfaceUnavailable,
)
from tools.passive_drive_capture import CCAN_CORRELATION_BROADCAST_IDS  # noqa: E402


BITRATE = 500_000
IGNITION_ID = IGNITION_ON.can_id
PAIR = "6/14"
SECONDARY_ROLES = ("b-can", "can-ch")
SECONDARY_START_IDS = {"b-can": B_CAN_VOLTAGE.can_id, "can-ch": 0x0DA}
SECONDARY_START_TIMEOUT_SECONDS = 5.0
SECONDARY_START_WAIT_SECONDS = 8.0
SECONDARY_JOIN_TIMEOUT_SECONDS = 150.0
# Re-admission of a lost/unproven secondary: poll the broker this often, and
# never start a new segment sooner than the backoff after the previous loss.
SECONDARY_READMIT_INTERVAL_SECONDS = 2.0
SECONDARY_READMIT_BACKOFF_SECONDS = 10.0
ROUTE_EVENTS_NAME = "route-events.jsonl"
DEFAULT_OUT_ROOT = (
    Path("/mnt/EXFAT512")
    / "obd-things"
    / "tmp"
    / "captures"
    / "three_bus_drive"
    / "broker-drive"
)
DEFAULT_REQUIRED_MOUNT = Path("/mnt/EXFAT512")
DEFAULT_STATE_PATH = REPO / "tmp" / "vehicle_data" / "drive-recorder-state.json"
DEFAULT_CONDITIONS = (
    "ordinary driving; broker-owned fixed PCM 01A1/06DA/069F and RF Hub polling; "
    "serial-resolved synchronized C-CAN, B-CAN, and CAN-CH receive-only companions; "
    "the van's own diagnostic client (also tester source F1) may run a read-only sweep while "
    "the Pi is not polling; such non-Pi F1 traffic is noted by van_scan_harvest.py"
)
WAIT_SECONDS = 1.0
STATUS_TIMEOUT_SECONDS = 2.0
BROKER_STATUS_RETRY_ATTEMPTS = 5
BROKER_STATUS_RETRY_DEADLINE_SECONDS = 5.0
BROKER_STATUS_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.25, 0.5)


class DriveRecorderError(RuntimeError):
    """A broker-ownership, interface, storage, or recorder gate failed."""


class BrokerOwnershipLost(DriveRecorderError, capture.RecoverableCaptureError):
    """Broker attribution was lost; recover only after successful capture cleanup."""


class SecondaryRouteLost(BrokerOwnershipLost):
    """One B-CAN/CAN-CH route could not be re-proven.

    It ends only that role's current segment (after the generic recorder has
    finalized it); the primary C-CAN capture and any other proven role go on.
    """


@dataclasses.dataclass(frozen=True)
class CaptureRoute:
    role: str
    channel: str
    usb_serial: str
    dev_id: int
    bitrate: int
    pair: str
    ownership: str

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def campaign_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"broker-drive-{stamp}"


def read_broker_status(
    client: TelemetryClient,
) -> dict[str, object]:
    status_code, payload = client.request("GET", "/v1/status")
    if status_code != 200:
        raise DriveRecorderError(
            f"broker status returned HTTP {status_code}"
        )
    if payload.get("service") != "van-telemetry":
        raise DriveRecorderError("broker status did not identify van-telemetry")
    return payload


def _transient_broker_status_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return bool(
        isinstance(exc, BlockingIOError)
        and exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    )


def read_broker_status_bounded(
    client: TelemetryClient,
    *,
    initial_interface: capture.InterfaceState,
    interface_reader: Callable[[], capture.InterfaceState],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, object] | None, capture.InterfaceState]:
    """Retry transient status I/O while continuously failing closed.

    A verified passive readback ends the retry successfully because broker
    attribution is no longer required.  An authoritative non-200/malformed
    status, an interface-read failure, or bounded retry exhaustion remains an
    ownership loss.  This helper never opens or configures CAN.
    """

    started = monotonic()
    interface = initial_interface
    last_error: Exception | None = None
    attempts_used = 0
    for attempt in range(1, BROKER_STATUS_RETRY_ATTEMPTS + 1):
        attempts_used = attempt
        try:
            return read_broker_status(client), interface
        except Exception as exc:
            if not _transient_broker_status_error(exc):
                raise BrokerOwnershipLost(
                    "armed broker status failed with a non-transient error: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            last_error = exc
        try:
            interface = interface_reader()
        except Exception as exc:
            raise BrokerOwnershipLost(
                "broker status was transiently unavailable and exact interface "
                f"revalidation failed: {type(exc).__name__}: {exc}"
            ) from exc
        if interface.listen_only:
            return None, interface

        elapsed = max(0.0, monotonic() - started)
        remaining = BROKER_STATUS_RETRY_DEADLINE_SECONDS - elapsed
        if attempt >= BROKER_STATUS_RETRY_ATTEMPTS or remaining <= 0:
            break
        delay = BROKER_STATUS_RETRY_DELAYS_SECONDS[
            min(attempt - 1, len(BROKER_STATUS_RETRY_DELAYS_SECONDS) - 1)
        ]
        sleep(min(delay, remaining))

    elapsed = max(0.0, monotonic() - started)
    assert last_error is not None
    raise BrokerOwnershipLost(
        "bounded broker-status attribution exhausted while the interface "
        f"remained armed: attempts={attempts_used}/"
        f"{BROKER_STATUS_RETRY_ATTEMPTS}, "
        f"elapsed={elapsed:.3f}s, last={type(last_error).__name__}: {last_error}"
    ) from last_error


def broker_armed_ready(
    status: object, *, expected_channel: str | None = None
) -> bool:
    """Whether one status object proves the reviewed broker owns active C-CAN."""
    if not isinstance(status, dict):
        return False
    active = status.get("active_drive")
    owner = status.get("current_owner")
    interface = status.get("interface")
    vehicle = status.get("vehicle_state")
    if not all(
        isinstance(item, dict)
        for item in (active, owner, interface, vehicle)
    ):
        return False
    topology = interface.get("topology")
    inhibits = interface.get("active_inhibits")
    channel = interface.get("channel")
    role_snapshot = interface.get("role_interfaces")
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    ccan = roles.get("c-can") if isinstance(roles, dict) else None
    expected = ccan.get("expected") if isinstance(ccan, dict) else None
    usb_serial = expected.get("usb_serial") if isinstance(expected, dict) else None
    dev_id = expected.get("dev_id") if isinstance(expected, dict) else None
    auxiliary = status.get("auxiliary_drive")
    auxiliary_ready = bool(
        not isinstance(auxiliary, dict)
        or auxiliary.get("enabled") is not True
        or auxiliary.get("state") == "armed_diagnostic"
        or (
            auxiliary.get("state")
            in ("idle", "blocked_until_engine_stop", "restoration_failed")
            and auxiliary.get("interface_mode") == "listen_only"
        )
    )
    return bool(
        active.get("enabled") is True
        and active.get("state") == "armed_diagnostic"
        and active.get("reason") == "running_gate_satisfied"
        and active.get("interface_mode") == "armed_diagnostic"
        and active.get("restoration_failed") is False
        and isinstance(active.get("helper_pid"), int)
        and not isinstance(active.get("helper_pid"), bool)
        and owner.get("kind") == "broker_active_drive"
        and isinstance(channel, str)
        and re.fullmatch(r"can[0-9]+", channel)
        and (expected_channel is None or channel == expected_channel)
        and isinstance(ccan, dict)
        and ccan.get("channel") == channel
        and isinstance(usb_serial, str)
        and bool(usb_serial)
        and isinstance(dev_id, int)
        and not isinstance(dev_id, bool)
        and dev_id >= 0
        and interface.get("adapter_present") is True
        and interface.get("up") is True
        and interface.get("bitrate") == BITRATE
        and interface.get("listen_only") is False
        and interface.get("controller_state") == "ERROR-ACTIVE"
        and isinstance(topology, dict)
        and topology.get("usable") is True
        and topology.get("bus") == "c-can"
        and topology.get("pair") == PAIR
        and not inhibits
        and vehicle.get("running") is True
        and vehicle.get("basis") == "qualified_ccan_0x0fc_engine_speed"
        and auxiliary_ready
    )


def broker_c_can_route(
    status: dict[str, object],
) -> tuple[str, str, int]:
    """Return the broker-proven channel and immutable USB identity."""
    if not broker_armed_ready(status):
        raise BrokerOwnershipLost("broker does not prove an armed C-CAN route")
    interface = status["interface"]
    assert isinstance(interface, dict)
    channel = interface["channel"]
    assert isinstance(channel, str)
    role_snapshot = interface.get("role_interfaces")
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    ccan = roles.get("c-can") if isinstance(roles, dict) else None
    expected = ccan.get("expected") if isinstance(ccan, dict) else None
    serial = expected.get("usb_serial") if isinstance(expected, dict) else None
    dev_id = expected.get("dev_id") if isinstance(expected, dict) else None
    if (
        not isinstance(serial, str)
        or not serial
        or not isinstance(dev_id, int)
        or isinstance(dev_id, bool)
        or dev_id < 0
    ):
        raise BrokerOwnershipLost("broker C-CAN USB identity is invalid")
    return channel, serial, dev_id


def _broker_role_payload(
    status: dict[str, object], role: str
) -> dict[str, object]:
    interface = status.get("interface")
    role_snapshot = (
        interface.get("role_interfaces") if isinstance(interface, dict) else None
    )
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    payload = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(payload, dict):
        raise BrokerOwnershipLost(f"broker status omits the {role} route")
    return payload


def broker_secondary_route(
    status: dict[str, object], role: str
) -> CaptureRoute:
    """Return one broker-proven exact passive secondary role."""
    if role not in SECONDARY_ROLES:
        raise ValueError(f"unsupported secondary recorder role {role!r}")
    payload = _broker_role_payload(status, role)
    expected = payload.get("expected")
    actual = payload.get("actual")
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise BrokerOwnershipLost(f"broker {role} route evidence is incomplete")
    channel = payload.get("channel")
    serial = expected.get("usb_serial")
    dev_id = expected.get("dev_id")
    bitrate = expected.get("bitrate")
    pair = expected.get("pair")
    # ``safe`` belongs to the passive-observer contract (can_interfaces.py sets
    # it only for a verified listen-only role), so it is required below for
    # the passive path only.  An armed B-CAN is proven instead by the broker's
    # verified-owner overlay (``armed_owner``/``topology_usable``), which
    # re-checks the owner route identity, restoration latch, inhibits, link,
    # bitrate, FD, one-shot, restart-ms, and controller state.  Requiring
    # ``safe`` there stopped the 2026-09-27 drive: the pre-arm snapshot still
    # said safe=true, the 22:00 voltage_mon refresh re-probed B-CAN as
    # ``interface_armed``/safe=false, and every B-CAN route check failed.
    common_valid = bool(
        payload.get("resolution") == "resolved"
        and isinstance(channel, str)
        and re.fullmatch(r"can[0-9]+", channel)
        and isinstance(serial, str)
        and serial
        and isinstance(dev_id, int)
        and not isinstance(dev_id, bool)
        and dev_id >= 0
        and isinstance(bitrate, int)
        and not isinstance(bitrate, bool)
        and bitrate > 0
        and isinstance(pair, str)
        and pair
        and actual.get("present") is True
        and actual.get("up") is True
        and actual.get("bitrate") == bitrate
        and actual.get("fd_enabled") is False
        and actual.get("one_shot") is False
        and actual.get("controller_state") == "ERROR-ACTIVE"
        and actual.get("restart_ms") == 0
    )
    auxiliary = status.get("auxiliary_drive")
    armed_bcan = bool(
        role == "b-can"
        and isinstance(auxiliary, dict)
        and auxiliary.get("enabled") is True
        and auxiliary.get("state") == "armed_diagnostic"
        and auxiliary.get("reason") == "running_gate_satisfied"
        and auxiliary.get("interface_mode") == "armed_diagnostic"
        and auxiliary.get("restoration_failed") is False
        and isinstance(auxiliary.get("helper_pid"), int)
        and not isinstance(auxiliary.get("helper_pid"), bool)
        and payload.get("operating_mode") == "armed_diagnostic"
        and payload.get("armed_owner") == "broker_auxiliary_drive"
        and payload.get("topology_usable") is True
        and payload.get("passive_ready") is False
        and actual.get("listen_only") is False
    )
    passive = bool(
        payload.get("safe") is True
        and payload.get("passive_ready") is True
        and actual.get("listen_only") is True
    )
    if not common_valid or not (armed_bcan or passive):
        raise BrokerOwnershipLost(
            f"broker does not prove an exact passive or auxiliary-owned {role} route"
        )
    return CaptureRoute(
        role=role,
        channel=channel,
        usb_serial=serial,
        dev_id=dev_id,
        bitrate=bitrate,
        pair=pair,
        ownership=(
            "broker_auxiliary_drive_companion"
            if armed_bcan
            else "shared_passive_observer"
        ),
    )


def route_from_lease(lease: PassiveInterfaceLease) -> CaptureRoute:
    return CaptureRoute(
        role=lease.role,
        channel=lease.channel,
        usb_serial=lease.usb_serial,
        dev_id=lease.dev_id,
        bitrate=lease.bitrate,
        pair=lease.pair,
        ownership="shared_passive_observer",
    )


def query_interface(
    *,
    channel: str,
    bitrate: int = BITRATE,
    require_listen_only: bool | None = None,
    role: str = "c-can",
    expected_usb_serial: str | None = None,
    expected_dev_id: int | None = None,
    role_resolver: SysfsCanRoleResolver | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> capture.InterfaceState:
    if not isinstance(channel, str) or not re.fullmatch(r"can[0-9]+", channel):
        raise DriveRecorderError(f"{role} route is not a resolved kernel canN")
    if (expected_usb_serial is None) != (expected_dev_id is None):
        raise DriveRecorderError(f"{role} USB identity is incomplete")
    if expected_usb_serial is not None:
        resolver = role_resolver or SysfsCanRoleResolver()
        inventory, _issues = resolver.inventory(drivers=("gs_usb",))
        if not NetdevIdentityExpectation(
            channel,
            expected_usb_serial,
            expected_dev_id,
            driver=None,  # Preserve reliance on inventory(drivers=("gs_usb",)).
        ).matches_inventory(inventory):
            raise DriveRecorderError(
                f"{channel} no longer matches the broker-proven {role} USB identity"
            )
    try:
        result = runner(
            ["ip", "-details", "-statistics", "link", "show", channel],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriveRecorderError(
            f"cannot inspect {channel}: {type(exc).__name__}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise DriveRecorderError(f"{channel} is missing or unreadable")
    state = capture.parse_interface_state(result.stdout, channel=channel)
    errors = []
    if not state.up:
        errors.append("interface is not UP")
    if state.bitrate != bitrate:
        errors.append(f"bitrate is {state.bitrate}, expected {bitrate}")
    if require_listen_only is True and not state.listen_only:
        errors.append("interface is not LISTEN-ONLY")
    if require_listen_only is False and state.listen_only:
        errors.append("interface is unexpectedly LISTEN-ONLY")
    if state.controller_state != "ERROR-ACTIVE":
        errors.append(
            f"controller is {state.controller_state}, expected ERROR-ACTIVE"
        )
    if state.rx_dropped is None or state.rx_missed is None:
        errors.append("RX dropped/missed counters are unavailable")
    if errors:
        raise DriveRecorderError(
            f"{channel} receive gate failed: " + "; ".join(errors)
        )
    return state


class PassiveLeaseSafetyCheck:
    """Revalidate one held B-CAN/CAN-CH observer lease and loss counters."""

    def __init__(
        self,
        manager: PassiveInterfaceManager,
        lease: PassiveInterfaceLease,
    ) -> None:
        self.manager = manager
        self.lease = lease

    def __call__(self) -> capture.InterfaceState:
        try:
            with self.manager.observe(self.lease.role) as checked:
                if route_from_lease(checked) != route_from_lease(self.lease):
                    raise DriveRecorderError(
                        f"{self.lease.role} changed while validating capture ownership"
                    )
            return query_interface(
                channel=self.lease.channel,
                bitrate=self.lease.bitrate,
                require_listen_only=True,
                role=self.lease.role,
                expected_usb_serial=self.lease.usb_serial,
                expected_dev_id=self.lease.dev_id,
            )
        except BrokerOwnershipLost:
            raise
        except (DriveRecorderError, PassiveInterfaceUnavailable) as exc:
            # Identity, listen-only, bitrate, controller, or counter proof is
            # gone: stop receiving this role, never guess it is still valid.
            raise SecondaryRouteLost(
                f"{self.lease.role} passive route could not be revalidated: {exc}"
            ) from exc


class CoordinatedSafetyCheck:
    """Accept passive mode, or armed mode only under the broker's exact owner."""

    def __init__(
        self,
        client: TelemetryClient,
        *,
        channel: str,
        expected_usb_serial: str | None = None,
        expected_dev_id: int | None = None,
        interface_reader: Callable[[], capture.InterfaceState] | None = None,
        status_sleep: Callable[[float], None] = time.sleep,
        status_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.channel = channel
        self.expected_usb_serial = expected_usb_serial
        self.expected_dev_id = expected_dev_id
        self.status_sleep = status_sleep
        self.status_monotonic = status_monotonic
        self.interface_reader = interface_reader or (
            lambda: query_interface(
                channel=self.channel,
                expected_usb_serial=self.expected_usb_serial,
                expected_dev_id=self.expected_dev_id,
            )
        )

    def __call__(self) -> capture.InterfaceState:
        interface = self.interface_reader()
        if interface.listen_only:
            return interface
        status, current_interface = read_broker_status_bounded(
            self.client,
            initial_interface=interface,
            interface_reader=self.interface_reader,
            sleep=self.status_sleep,
            monotonic=self.status_monotonic,
        )
        if status is None:
            return current_interface
        if not broker_armed_ready(status, expected_channel=self.channel):
            current_interface = self.interface_reader()
            if current_interface.listen_only:
                return current_interface
            raise BrokerOwnershipLost(
                "armed interface is not owned by the reviewed broker active-drive interval"
            )
        return interface


class InitialArmedSafetyCheck(CoordinatedSafetyCheck):
    """Require broker-owned armed mode once, then allow verified restoration."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.initial_armed_gate_passed = False

    def __call__(self) -> capture.InterfaceState:
        if self.initial_armed_gate_passed:
            return super().__call__()
        interface = self.interface_reader()
        status, current_interface = read_broker_status_bounded(
            self.client,
            initial_interface=interface,
            interface_reader=self.interface_reader,
            sleep=self.status_sleep,
            monotonic=self.status_monotonic,
        )
        if (
            status is None
            or current_interface.listen_only
            or not broker_armed_ready(
            status, expected_channel=self.channel
            )
        ):
            raise BrokerOwnershipLost(
                "broker-owned armed mode disappeared during recorder startup"
            )
        self.initial_armed_gate_passed = True
        return interface


class AuxiliaryBcanSafetyCheck:
    """Accept passive B-CAN or the broker's exact armed auxiliary owner."""

    def __init__(
        self,
        client: TelemetryClient,
        route: CaptureRoute,
        *,
        require_initial_armed: bool = False,
        interface_reader: Callable[[], capture.InterfaceState] | None = None,
        status_sleep: Callable[[float], None] = time.sleep,
        status_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if route.role != "b-can":
            raise ValueError("auxiliary safety check requires a B-CAN route")
        self.client = client
        self.route = route
        self.require_initial_armed = require_initial_armed
        self.initial_gate_passed = False
        self.status_sleep = status_sleep
        self.status_monotonic = status_monotonic
        self.interface_reader = interface_reader or (
            lambda: query_interface(
                channel=route.channel,
                bitrate=route.bitrate,
                expected_usb_serial=route.usb_serial,
                expected_dev_id=route.dev_id,
                role="b-can",
            )
        )

    def __call__(self) -> capture.InterfaceState:
        try:
            return self._check()
        except BrokerOwnershipLost:
            raise
        except DriveRecorderError as exc:
            raise SecondaryRouteLost(
                f"b-can route could not be revalidated: {exc}"
            ) from exc

    def _check(self) -> capture.InterfaceState:
        interface = self.interface_reader()
        if interface.listen_only:
            if self.require_initial_armed and not self.initial_gate_passed:
                raise BrokerOwnershipLost(
                    "B-CAN auxiliary ownership disappeared during recorder startup"
                )
            return interface
        status, current_interface = read_broker_status_bounded(
            self.client,
            initial_interface=interface,
            interface_reader=self.interface_reader,
            sleep=self.status_sleep,
            monotonic=self.status_monotonic,
        )
        if status is None:
            if self.require_initial_armed and not self.initial_gate_passed:
                raise BrokerOwnershipLost(
                    "B-CAN auxiliary ownership disappeared during recorder startup"
                )
            return current_interface
        current = broker_secondary_route(status, "b-can")
        if current != self.route or current.ownership != "broker_auxiliary_drive_companion":
            raise BrokerOwnershipLost(
                "armed B-CAN interface is not owned by the reviewed broker auxiliary helper"
            )
        self.initial_gate_passed = True
        return interface


AdmittedRoute = tuple[CaptureRoute, Callable[[], capture.InterfaceState], ExitStack]


def admit_secondary_route(
    role: str,
    status: dict[str, object],
    client: TelemetryClient,
    interface_manager: PassiveInterfaceManager,
    *,
    expected_route: CaptureRoute | None = None,
    auxiliary_check_factory: Callable[..., Callable[[], capture.InterfaceState]]
    | None = None,
    passive_check_factory: Callable[..., Callable[[], capture.InterfaceState]]
    | None = None,
) -> tuple[AdmittedRoute, capture.InterfaceState]:
    """Prove one secondary route from a broker status plus a fresh local check.

    The broker must prove the exact route; a passive role then takes a fresh
    shared role/channel lease that must match it, and an auxiliary-owned
    B-CAN must pass the exact armed-owner check.  Any failure raises
    ``SecondaryRouteLost`` with nothing left held.
    """
    auxiliary_check_factory = auxiliary_check_factory or AuxiliaryBcanSafetyCheck
    passive_check_factory = passive_check_factory or PassiveLeaseSafetyCheck
    route = broker_secondary_route(status, role)
    if expected_route is not None and route != expected_route:
        raise SecondaryRouteLost(f"{role} route changed after broker admission")
    stack = ExitStack()
    try:
        if route.ownership == "broker_auxiliary_drive_companion":
            check = auxiliary_check_factory(
                client, route, require_initial_armed=True
            )
        else:
            try:
                lease = stack.enter_context(interface_manager.observe(role))
            except Exception as exc:
                raise SecondaryRouteLost(
                    f"cannot acquire required passive {role} recorder route: {exc}"
                ) from exc
            if route_from_lease(lease) != route:
                raise SecondaryRouteLost(f"{role} route changed after broker admission")
            check = passive_check_factory(interface_manager, lease)
        interface = check()
    except BaseException:
        stack.close()
        raise
    return (route, check, stack), interface


class SecondaryRoleSupervisor:
    """Record one B-CAN/CAN-CH role as consecutive broker-proven segments.

    A route loss (broker proof, lease/identity revalidation, or a missing
    bus-identity start frame) ends only this role's current segment, after the
    generic recorder has finalized it cleanly; the primary C-CAN capture and
    any still-proven role continue.  The role is re-admitted only from a fresh
    broker status that still proves the primary interval and the exact route,
    followed by a fresh lease or armed-owner check.  Storage, compression,
    socket-drop accounting, and every other capture failure stays fatal for
    the whole campaign.  Nothing here configures CAN or transmits.
    """

    def __init__(
        self,
        *,
        role: str,
        role_dir: Path,
        client: TelemetryClient,
        interface_manager: PassiveInterfaceManager,
        recorder_factory: Callable[
            [CaptureRoute, Callable[[], capture.InterfaceState], int], object
        ],
        stop_event: threading.Event,
        settled_event: threading.Event,
        events_path: Path,
        admitted: AdmittedRoute | None,
        admission_detail: str | None = None,
        on_change: Callable[[], None] | None = None,
        status_reader: Callable[[TelemetryClient], dict[str, object]] = (
            lambda client: read_broker_status(client)
        ),
        admitter: Callable[..., tuple[AdmittedRoute, capture.InterfaceState]]
        | None = None,
        readmit_interval_seconds: float | None = None,
        readmit_backoff_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if role not in SECONDARY_ROLES:
            raise ValueError(f"unsupported secondary recorder role {role!r}")
        self.role = role
        self.role_dir = role_dir
        self.client = client
        self.interface_manager = interface_manager
        self.recorder_factory = recorder_factory
        self.stop_event = stop_event
        self.settled_event = settled_event
        self.events_path = events_path
        self._admitted = admitted
        self.admitted_at_start = admitted is not None
        self.admission_detail = admission_detail
        self.on_change = on_change
        self.status_reader = status_reader
        self.admitter = admitter or admit_secondary_route
        self.readmit_interval_seconds = (
            SECONDARY_READMIT_INTERVAL_SECONDS
            if readmit_interval_seconds is None
            else readmit_interval_seconds
        )
        self.readmit_backoff_seconds = (
            SECONDARY_READMIT_BACKOFF_SECONDS
            if readmit_backoff_seconds is None
            else readmit_backoff_seconds
        )
        self.monotonic = monotonic
        self.segments: list[dict[str, object]] = []
        self.state = "starting" if admitted is not None else "awaiting_route"
        if admitted is None:
            # Nothing to wait for at startup; the role joins when proven.
            settled_event.set()

    def _event(self, kind: str, **payload: object) -> None:
        capture.append_manifest(
            self.events_path,
            {"type": kind, "role": self.role, "time_utc": utc_now(), **payload},
        )

    def _set_state(self, state: str) -> None:
        self.state = state
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                # The small operational state file is advisory only.
                pass

    def next_sequence(self) -> int:
        """First chunk number no earlier segment (complete or partial) used."""
        highest = -1
        if self.role_dir.is_dir():
            for path in self.role_dir.iterdir():
                match = re.match(r"chunk_([0-9]+)_", path.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest + 1

    def continuous(self) -> bool:
        return bool(
            self.admitted_at_start
            and len(self.segments) == 1
            and self.segments[0].get("end") == "completed"
        )

    def _try_admit(self) -> AdmittedRoute | None:
        try:
            status = self.status_reader(self.client)
        except Exception as exc:
            self.admission_detail = (
                f"broker status unavailable: {type(exc).__name__}: {exc}"
            )
            return None
        if not broker_armed_ready(status):
            self.admission_detail = "broker no longer proves the primary C-CAN interval"
            return None
        try:
            admitted, _interface = self.admitter(
                self.role, status, self.client, self.interface_manager
            )
        except BrokerOwnershipLost as exc:
            self.admission_detail = str(exc)
            return None
        return admitted

    def run(self) -> None:
        admitted = self._admitted
        self._admitted = None
        lost_at: float | None = None
        announced_wait = False
        while not self.stop_event.is_set():
            if admitted is None:
                if not announced_wait:
                    self._event(
                        "secondary_awaiting_route", detail=self.admission_detail
                    )
                    self._set_state("awaiting_route")
                    announced_wait = True
                if self.stop_event.wait(self.readmit_interval_seconds):
                    break
                if (
                    lost_at is not None
                    and self.monotonic() - lost_at < self.readmit_backoff_seconds
                ):
                    continue
                admitted = self._try_admit()
                if admitted is None:
                    continue
            route, check, stack = admitted
            admitted = None
            announced_wait = False
            sequence_start = self.next_sequence()
            segment: dict[str, object] = {
                "segment": len(self.segments),
                "route": route.as_dict(),
                "sequence_start": sequence_start,
                "started_utc": utc_now(),
                "ended_utc": None,
                "end": None,
                "detail": None,
            }
            self.segments.append(segment)
            self._event(
                "secondary_segment_start",
                segment=segment["segment"],
                sequence_start=sequence_start,
                route=route.as_dict(),
            )
            self._set_state("recording")
            end = "completed"
            detail: str | None = None
            fatal: BaseException | None = None
            try:
                with stack:
                    recorder = self.recorder_factory(route, check, sequence_start)
                    recorder.run()
            except BrokerOwnershipLost as exc:
                end, detail = "route_lost", f"{type(exc).__name__}: {exc}"
            except capture.CaptureError as exc:
                if "required start CAN ID" in str(exc):
                    end, detail = "start_signature_missing", str(exc)
                else:
                    end, detail, fatal = "error", f"{type(exc).__name__}: {exc}", exc
            except BaseException as exc:
                end, detail, fatal = "error", f"{type(exc).__name__}: {exc}", exc
            segment.update(ended_utc=utc_now(), end=end, detail=detail)
            try:
                self._event(
                    "secondary_segment_end",
                    segment=segment["segment"],
                    end=end,
                    detail=detail,
                )
            except Exception:
                if fatal is None:
                    raise
            if fatal is not None:
                self._set_state("failed")
                raise fatal
            if end == "completed":
                break
            lost_at = self.monotonic()
            self.admission_detail = detail
            # A loss before the first start frame still settles startup.
            self.settled_event.set()
        self._set_state("stopped")


def priority_ids() -> frozenset[int]:
    # The recorder always uses the CLI's default ccan-correlation profile.
    # Keep its broadcast table authoritative without depending on its parser.
    selected = set(CCAN_CORRELATION_BROADCAST_IDS)
    for module in MODULES.values():
        if module.bus == "c-can":
            selected.update((module.txid, module.rxid))
    return frozenset(selected)


def validate_dependencies(
    out_root: Path,
    policy: capture.DiskPolicy,
    safety_check: Callable[[], capture.InterfaceState],
    *,
    which: Callable[[str], str | None] = shutil.which,
    disk_free: Callable[[Path], int] = capture.available_bytes,
    rmem_max: Callable[[], int] = capture.read_rmem_max,
) -> tuple[capture.InterfaceState, int]:
    errors = []
    for executable in ("candump", "zstd"):
        if which(executable) is None:
            errors.append(f"required executable is missing: {executable}")
    try:
        maximum = rmem_max()
        if maximum < capture.RECEIVE_BUFFER:
            errors.append(
                "net.core.rmem_max is too small: "
                f"{maximum} < {capture.RECEIVE_BUFFER}"
            )
    except Exception as exc:
        errors.append(str(exc))
    try:
        interface = safety_check()
    except Exception as exc:
        errors.append(str(exc))
        interface = capture.InterfaceState(False, None, False, None)
    free = disk_free(out_root)
    if policy.action(free) == "stop":
        errors.append(
            f"only {free} bytes free, at/below hard floor "
            f"{policy.hard_free_bytes}"
        )
    if errors:
        raise DriveRecorderError(
            "armed-recorder preflight failed:\n- " + "\n- ".join(errors)
        )
    return interface, free


def write_state(path: Path, **payload: object) -> None:
    capture.atomic_write_json(
        path,
        {
            "updated_utc": utc_now(),
            **payload,
        },
    )


def _admit_interval_secondaries(
    initial_status: dict[str, object],
    client: TelemetryClient,
    interface_manager: PassiveInterfaceManager,
    lease_stack: ExitStack,
) -> tuple[
    dict[str, AdmittedRoute], dict[str, str], dict[str, capture.InterfaceState]
]:
    admitted: dict[str, AdmittedRoute] = {}
    admission_detail: dict[str, str] = {}
    secondary_interfaces: dict[str, capture.InterfaceState] = {}
    for role in SECONDARY_ROLES:
        try:
            item, secondary_interfaces[role] = admit_secondary_route(
                role, initial_status, client, interface_manager
            )
        except BrokerOwnershipLost as exc:
            # The primary C-CAN evidence does not wait for a secondary.
            admission_detail[role] = str(exc)
            continue
        lease_stack.callback(item[2].close)
        admitted[role] = item
    return admitted, admission_detail, secondary_interfaces


def _refresh_interval_routes(
    client: TelemetryClient,
    channel: str,
    admitted: dict[str, AdmittedRoute],
    secondary_interfaces: dict[str, capture.InterfaceState],
    admission_detail: dict[str, str],
) -> None:
    refreshed_status = read_broker_status(client)
    if not broker_armed_ready(refreshed_status, expected_channel=channel):
        raise BrokerOwnershipLost(
            "broker ownership disappeared during raw capture preflight"
        )
    for role in list(admitted):
        try:
            current = broker_secondary_route(refreshed_status, role)
        except BrokerOwnershipLost as exc:
            current, detail = None, str(exc)
        else:
            detail = f"broker {role} route changed during raw capture preflight"
        if current != admitted[role][0]:
            admitted.pop(role)[2].close()
            secondary_interfaces.pop(role, None)
            admission_detail[role] = detail


def _create_interval_directories(
    args: argparse.Namespace,
) -> tuple[str, Path, dict[str, Path], Path]:
    args.out_root.mkdir(parents=True, exist_ok=True)
    run_id = campaign_id()
    run_dir = args.out_root / run_id
    if run_dir.exists():
        raise DriveRecorderError(f"campaign directory already exists: {run_dir}")
    run_dir.mkdir()
    role_dirs = {role: run_dir / role for role in ("c-can", *SECONDARY_ROLES)}
    # Secondary directories appear with their first admitted segment, so a
    # never-proven role leaves no empty, never-ending bus directory.
    role_dirs["c-can"].mkdir()
    events_path = run_dir / ROUTE_EVENTS_NAME
    return run_id, run_dir, role_dirs, events_path


def _interval_routes(
    channel: str,
    usb_serial: str,
    dev_id: int,
    admitted: dict[str, AdmittedRoute],
) -> dict[str, CaptureRoute]:
    routes = {
        "c-can": CaptureRoute(
            role="c-can",
            channel=channel,
            usb_serial=usb_serial,
            dev_id=dev_id,
            bitrate=BITRATE,
            pair=PAIR,
            ownership="broker_active_drive_companion",
        ),
        **{role: item[0] for role, item in admitted.items()},
    }
    return routes


def _write_interval_metadata(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    initial_status: dict[str, object],
    interface: capture.InterfaceState,
    free: int,
    run_id: str,
    run_dir: Path,
    selected_ids: frozenset[int],
    routes: dict[str, CaptureRoute],
    admitted: dict[str, AdmittedRoute],
    admission_detail: dict[str, str],
    secondary_interfaces: dict[str, capture.InterfaceState],
) -> None:
    metadata = {
        "type": "run_metadata",
        "created_utc": utc_now(),
        "campaign": run_id,
        "conditions": args.conditions.strip(),
        "interaction": "synchronized_three_bus_receive_only_companion",
        "roles_required": ["c-can"],
        "secondary_roles": list(SECONDARY_ROLES),
        "secondary_policy": (
            "best-effort segments: a secondary records only while its exact "
            "route is proven; a loss ends that segment only and the role is "
            f"re-admitted after re-proof; see {ROUTE_EVENTS_NAME}"
        ),
        "secondary_admission": {
            role: (
                "admitted"
                if role in admitted
                else f"awaiting_route: {admission_detail.get(role)}"
            )
            for role in SECONDARY_ROLES
        },
        "routes": {role: route.as_dict() for role, route in routes.items()},
        "initial_interfaces": {
            "c-can": dataclasses.asdict(interface),
            **{
                role: dataclasses.asdict(state)
                for role, state in secondary_interfaces.items()
            },
        },
        "initial_broker_status": initial_status,
        "capture_started_mid_running_epoch": True,
        "required_mount": str(args.require_mount.resolve()),
        "free_bytes_at_preflight": free,
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "c_can_stop_after_id": f"0x{IGNITION_ID:X}",
        "c_can_stop_after_id_absence_seconds": args.ignition_absence_seconds,
        "secondary_required_start_ids": {
            role: f"0x{SECONDARY_START_IDS[role]:X}"
            for role in SECONDARY_ROLES
        },
        "secondary_required_start_timeout_seconds": (
            SECONDARY_START_TIMEOUT_SECONDS
        ),
        "c_can_priority_profile": "ccan-correlation",
        "c_can_priority_ids": [
            f"0x{value:X}" for value in sorted(selected_ids)
        ],
        "secondary_priority_streams": False,
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "net_core_rmem_max": capture.read_rmem_max(),
        "does_not": [
            "configure or restore CAN",
            "acquire exclusive B-CAN or CAN-CH ownership",
            "transmit CAN",
            "control the telemetry broker",
        ],
    }
    capture.atomic_write_json(run_dir / "run.json", metadata)


def _interval_state_publisher(
    args: argparse.Namespace,
    run_id: str,
    run_dir: Path,
    interface: capture.InterfaceState,
    supervisors: dict[str, SecondaryRoleSupervisor],
    state_lock,
) -> Callable[[], None]:
    def publish_state() -> None:
        with state_lock:
            recording = [
                role
                for role, supervisor in supervisors.items()
                if supervisor.state == "recording"
            ]
            awaiting = [
                role
                for role, supervisor in supervisors.items()
                if supervisor.state == "awaiting_route"
            ]
            write_state(
                args.state_path,
                status="recording",
                campaign=run_id,
                capture_dir=str(run_dir),
                roles_recording=["c-can", *recording],
                roles_awaiting_route=awaiting,
                c_can_interface_mode=(
                    "listen_only" if interface.listen_only else "armed_diagnostic"
                ),
            )
    return publish_state


def _secondary_recorder_factory(
    role: str,
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    role_dirs: dict[str, Path],
    mount_check: Callable[[], object],
    zstd: str,
    candump: str,
    stop_secondaries: threading.Event,
    secondary_settled: dict[str, threading.Event],
) -> Callable[[CaptureRoute, Callable[[], capture.InterfaceState], int], capture.Recorder]:
    def build(
        route: CaptureRoute,
        check: Callable[[], capture.InterfaceState],
        sequence_start: int,
    ) -> capture.Recorder:
        role_dirs[role].mkdir(exist_ok=True)
        return capture.Recorder(
            role_dirs[role],
            frozenset(),
            args.rotation_seconds,
            args.duration_seconds,
            policy,
            required_start_id=SECONDARY_START_IDS[role],
            required_start_id_timeout_seconds=SECONDARY_START_TIMEOUT_SECONDS,
            safety_check=check,
            mount_check=mount_check,
            zstd=zstd,
            candump=candump,
            candump_extra_args=("-D",),
            external_stop_requested=stop_secondaries.is_set,
            started_callback=secondary_settled[role].set,
            install_signal_handlers=False,
            channel=route.channel,
            bitrate=route.bitrate,
            sequence_start=sequence_start,
        )

    return build


def _secondary_thread_target(
    supervisors: dict[str, SecondaryRoleSupervisor],
    secondary_errors: dict[str, BaseException],
    secondary_error_lock,
) -> Callable[[str], None]:
    def run_secondary(role: str) -> None:
        try:
            supervisors[role].run()
        except BaseException as exc:
            with secondary_error_lock:
                secondary_errors[role] = exc
    return run_secondary


def _secondary_health_checker(
    secondary_errors: dict[str, BaseException],
    secondary_error_lock,
    secondary_threads: dict[str, threading.Thread],
    stop_secondaries: threading.Event,
) -> Callable[[], None]:
    def secondary_health_check() -> None:
        # Route loss is handled inside each supervisor; only a failure that
        # would compromise the whole evidence set (storage, compression,
        # drop accounting, cleanup) reaches this point.
        with secondary_error_lock:
            failures = dict(secondary_errors)
        if failures:
            detail = "; ".join(
                f"{role}: {type(exc).__name__}: {exc}"
                for role, exc in sorted(failures.items())
            )
            if all(isinstance(exc, BrokerOwnershipLost) for exc in failures.values()):
                raise BrokerOwnershipLost("secondary ownership lost: " + detail)
            raise DriveRecorderError(
                "required secondary recorder failed: " + detail
            )
        stopped = [
            role
            for role, thread in secondary_threads.items()
            if not thread.is_alive() and not stop_secondaries.is_set()
        ]
        if stopped:
            raise DriveRecorderError(
                "required secondary recorder stopped unexpectedly: "
                + ", ".join(sorted(stopped))
            )
    return secondary_health_check


def _primary_recorder(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    role_dirs: dict[str, Path],
    selected_ids: frozenset[int],
    client: TelemetryClient,
    channel: str,
    usb_serial: str,
    dev_id: int,
    mount_check: Callable[[], object],
    zstd: str,
    candump: str,
    secondary_health_check: Callable[[], None],
) -> capture.Recorder:
    c_can_recorder = capture.Recorder(
        role_dirs["c-can"],
        selected_ids,
        args.rotation_seconds,
        args.duration_seconds,
        policy,
        stop_after_id=IGNITION_ID,
        stop_after_id_absence_seconds=args.ignition_absence_seconds,
        required_start_id=IGNITION_ID,
        required_start_id_timeout_seconds=5.0,
        safety_check=InitialArmedSafetyCheck(
            client,
            channel=channel,
            expected_usb_serial=usb_serial,
            expected_dev_id=dev_id,
        ),
        mount_check=mount_check,
        zstd=zstd,
        candump=candump,
        candump_extra_args=("-D",),
        health_check=secondary_health_check,
        channel=channel,
        bitrate=BITRATE,
    )
    return c_can_recorder


@dataclasses.dataclass(frozen=True)
class _IntervalRecorders:
    """Keep the prepared workers and their shared callbacks together through cleanup."""

    primary: capture.Recorder
    supervisors: dict[str, SecondaryRoleSupervisor]
    threads: dict[str, threading.Thread]
    settled: dict[str, threading.Event]
    stop: threading.Event
    health_check: Callable[[], None]


def _prepare_interval_recorders(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    client: TelemetryClient,
    interface_manager: PassiveInterfaceManager,
    mount_device: int,
    interface: capture.InterfaceState,
    run_id: str,
    run_dir: Path,
    role_dirs: dict[str, Path],
    events_path: Path,
    admitted: dict[str, AdmittedRoute],
    admission_detail: dict[str, str],
    channel: str,
    usb_serial: str,
    dev_id: int,
    selected_ids: frozenset[int],
) -> _IntervalRecorders:
    supervisors: dict[str, SecondaryRoleSupervisor] = {}
    state_lock = threading.Lock()

    publish_state = _interval_state_publisher(
        args, run_id, run_dir, interface, supervisors, state_lock
    )

    mount_check = lambda: capture.require_writable_mount(
        args.out_root,
        args.require_mount,
        expected_device=mount_device,
    )
    zstd = shutil.which("zstd") or "zstd"
    candump = shutil.which("candump") or "candump"
    stop_secondaries = threading.Event()
    secondary_settled = {
        role: threading.Event() for role in SECONDARY_ROLES
    }
    secondary_errors: dict[str, BaseException] = {}
    secondary_error_lock = threading.Lock()
    secondary_threads: dict[str, threading.Thread] = {}

    for role in SECONDARY_ROLES:
        supervisors[role] = SecondaryRoleSupervisor(
            role=role,
            role_dir=role_dirs[role],
            client=client,
            interface_manager=interface_manager,
            recorder_factory=_secondary_recorder_factory(
                role, args, policy, role_dirs, mount_check, zstd, candump,
                stop_secondaries, secondary_settled,
            ),
            stop_event=stop_secondaries,
            settled_event=secondary_settled[role],
            events_path=events_path,
            admitted=admitted.get(role),
            admission_detail=admission_detail.get(role),
            on_change=publish_state,
        )
    publish_state()

    run_secondary = _secondary_thread_target(
        supervisors, secondary_errors, secondary_error_lock
    )

    for role in SECONDARY_ROLES:
        secondary_threads[role] = threading.Thread(
            name=f"broker-drive-recorder-{role}",
            target=run_secondary,
            args=(role,),
        )

    secondary_health_check = _secondary_health_checker(
        secondary_errors, secondary_error_lock, secondary_threads,
        stop_secondaries,
    )

    c_can_recorder = _primary_recorder(
        args, policy, role_dirs, selected_ids, client, channel, usb_serial,
        dev_id, mount_check, zstd, candump, secondary_health_check,
    )
    return _IntervalRecorders(
        c_can_recorder, supervisors, secondary_threads, secondary_settled,
        stop_secondaries, secondary_health_check,
    )


def _run_interval_recorders(run_dir: Path, recorders: _IntervalRecorders) -> None:
    c_can_recorder = recorders.primary
    secondary_threads = recorders.threads
    secondary_settled = recorders.settled
    stop_secondaries = recorders.stop
    secondary_health_check = recorders.health_check
    main_error: BaseException | None = None
    try:
        with capture.campaign_file_lock(run_dir):
            for thread in secondary_threads.values():
                thread.start()
            deadline = time.monotonic() + SECONDARY_START_WAIT_SECONDS
            for role in SECONDARY_ROLES:
                remaining = max(0.0, deadline - time.monotonic())
                if not secondary_settled[role].wait(remaining):
                    secondary_health_check()
                    raise DriveRecorderError(
                        f"required {role} recorder did not start within "
                        f"{SECONDARY_START_WAIT_SECONDS:.1f} seconds"
                    )
            secondary_health_check()
            c_can_recorder.run()
    except BaseException as exc:
        main_error = exc
    finally:
        stop_secondaries.set()
        for thread in secondary_threads.values():
            if thread.ident is not None:
                thread.join(SECONDARY_JOIN_TIMEOUT_SECONDS)

    alive = [
        role for role, thread in secondary_threads.items() if thread.is_alive()
    ]
    if alive:
        raise DriveRecorderError(
            "secondary recorder cleanup timed out: " + ", ".join(alive)
        )
    if main_error is not None:
        if isinstance(main_error, BrokerOwnershipLost):
            # A concurrent secondary storage/cleanup failure remains fatal.
            secondary_health_check()
        if isinstance(main_error, capture.CaptureError) and (
            "required start CAN ID" in str(main_error)
        ):
            raise BrokerOwnershipLost(str(main_error)) from main_error
        raise main_error
    secondary_health_check()


def _write_capture_set(
    run_id: str,
    run_dir: Path,
    role_dirs: dict[str, Path],
    events_path: Path,
    routes: dict[str, CaptureRoute],
    supervisors: dict[str, SecondaryRoleSupervisor],
) -> None:
    continuous = {
        role: supervisor.continuous()
        for role, supervisor in supervisors.items()
    }
    capture.atomic_write_json(
        run_dir / "capture-set.json",
        {
            "type": "synchronized_three_bus_capture_set",
            "completed_utc": utc_now(),
            "campaign": run_id,
            # True only when every role recorded one uninterrupted segment
            # for the whole interval, as before the segmented policy.
            "complete": all(continuous.values()),
            "primary_complete": True,
            "route_events": str(events_path),
            "roles": {
                "c-can": {
                    "route": routes["c-can"].as_dict(),
                    "capture_dir": str(role_dirs["c-can"]),
                    "checkpoint": str(role_dirs["c-can"] / "checkpoint.json"),
                    "manifest": str(role_dirs["c-can"] / "manifest.jsonl"),
                    "continuous": True,
                },
                **{
                    role: {
                        "route": (
                            routes[role].as_dict() if role in routes else None
                        ),
                        "capture_dir": str(role_dirs[role]),
                        "checkpoint": str(role_dirs[role] / "checkpoint.json"),
                        "manifest": str(role_dirs[role] / "manifest.jsonl"),
                        "continuous": continuous[role],
                        "segments": supervisor.segments,
                    }
                    for role, supervisor in supervisors.items()
                },
            },
        },
    )


def record_one_interval(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    initial_status: dict[str, object],
    client: TelemetryClient,
    *,
    interface_manager: PassiveInterfaceManager | None = None,
) -> Path:
    """Start one C-CAN-primary session with best-effort B-CAN/CAN-CH segments.

    Nothing here configures CAN.  C-CAN is required for the whole interval;
    each secondary role records only while its exact route is proven and is
    re-admitted after a loss (see ``SecondaryRoleSupervisor``).
    """
    if not broker_armed_ready(initial_status):
        raise BrokerOwnershipLost(
            "broker ownership disappeared before raw capture setup"
        )
    channel, usb_serial, dev_id = broker_c_can_route(initial_status)
    interface_manager = interface_manager or PassiveInterfaceManager()
    mount_device = capture.require_writable_mount(
        args.out_root,
        args.require_mount,
    )
    initial_safety_check = InitialArmedSafetyCheck(
        client,
        channel=channel,
        expected_usb_serial=usb_serial,
        expected_dev_id=dev_id,
    )
    interface, free = validate_dependencies(
        args.out_root,
        policy,
        initial_safety_check,
    )
    if policy.action(free) != "full":
        raise DriveRecorderError(
            "three-bus capture requires full-stream storage above the soft floor"
        )
    capture.require_writable_mount(
        args.out_root,
        args.require_mount,
        expected_device=mount_device,
    )
    with ExitStack() as lease_stack:
        admitted, admission_detail, secondary_interfaces = _admit_interval_secondaries(
            initial_status, client, interface_manager, lease_stack
        )
        _refresh_interval_routes(
            client, channel, admitted, secondary_interfaces, admission_detail
        )
        run_id, run_dir, role_dirs, events_path = _create_interval_directories(args)
        selected_ids = priority_ids()
        routes = _interval_routes(channel, usb_serial, dev_id, admitted)
        _write_interval_metadata(
            args, policy, initial_status, interface, free, run_id, run_dir,
            selected_ids, routes, admitted, admission_detail, secondary_interfaces,
        )
        recorders = _prepare_interval_recorders(
            args, policy, client, interface_manager, mount_device, interface,
            run_id, run_dir, role_dirs, events_path, admitted, admission_detail,
            channel, usb_serial, dev_id, selected_ids,
        )
        _run_interval_recorders(run_dir, recorders)
        _write_capture_set(
            run_id, run_dir, role_dirs, events_path, routes, recorders.supervisors
        )
        return run_dir


def run_daemon(
    args: argparse.Namespace,
    policy: capture.DiskPolicy,
    *,
    client: TelemetryClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    client = client or TelemetryClient(
        args.socket,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    last_wait_detail = None
    while True:
        try:
            status = read_broker_status(client)
            ready = broker_armed_ready(status)
            detail = (
                "waiting for reviewed broker active-drive ownership"
                if not ready
                else None
            )
        except Exception as exc:
            status = None
            ready = False
            detail = (
                "broker status unavailable; waiting without opening CAN: "
                f"{type(exc).__name__}: {exc}"
            )
        if not ready:
            if detail != last_wait_detail:
                write_state(
                    args.state_path,
                    status="waiting",
                    detail=detail,
                    output_root=str(args.out_root),
                )
                print(f"{utc_now()} {detail}", flush=True)
                last_wait_detail = detail
            sleep(WAIT_SECONDS)
            continue
        last_wait_detail = None
        assert isinstance(status, dict)
        try:
            run_dir = record_one_interval(
                args,
                policy,
                status,
                client,
            )
        except BrokerOwnershipLost as exc:
            write_state(
                args.state_path,
                status="waiting",
                detail=str(exc),
                output_root=str(args.out_root),
            )
            print(f"{utc_now()} {exc}; waiting", flush=True)
            sleep(WAIT_SECONDS)
            continue
        except Exception as exc:
            write_state(
                args.state_path,
                status="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise DriveRecorderError(str(exc)) from exc
        write_state(
            args.state_path,
            status="waiting",
            detail="previous broker-owned drive capture finalized successfully",
            last_capture_dir=str(run_dir),
            output_root=str(args.out_root),
        )
        print(
            f"{utc_now()} finalized {run_dir.name}; waiting for next active interval",
            flush=True,
        )
        sleep(WAIT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--confirm-broker-owned-receive-only",
        action="store_true",
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument(
        "--require-mount",
        type=Path,
        default=DEFAULT_REQUIRED_MOUNT,
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--conditions", default=DEFAULT_CONDITIONS)
    parser.add_argument(
        "--rotation-seconds",
        type=int,
        default=capture.DEFAULT_ROTATION_SECONDS,
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=capture.DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--ignition-absence-seconds",
        type=float,
        default=capture.DEFAULT_STOP_ID_ABSENCE_SECONDS,
    )
    parser.add_argument(
        "--soft-free-gib",
        type=float,
        default=capture.DEFAULT_SOFT_FREE_BYTES / 1024**3,
    )
    parser.add_argument(
        "--hard-free-gib",
        type=float,
        default=capture.DEFAULT_HARD_FREE_BYTES / 1024**3,
    )
    return parser


def validate_args(args: argparse.Namespace) -> capture.DiskPolicy:
    if args.execute and not args.confirm_broker_owned_receive_only:
        raise DriveRecorderError(
            "--execute requires --confirm-broker-owned-receive-only"
        )
    for label, path in (
        ("--out-root", args.out_root),
        ("--require-mount", args.require_mount),
        ("--state-path", args.state_path),
    ):
        if not path.is_absolute():
            raise DriveRecorderError(f"{label} must be absolute")
    if not args.socket.startswith("/"):
        raise DriveRecorderError("--socket must be an absolute Unix path")
    if not args.conditions.strip():
        raise DriveRecorderError("--conditions cannot be empty")
    if args.rotation_seconds < 10:
        raise DriveRecorderError("--rotation-seconds must be at least 10")
    if not 1 <= args.duration_seconds <= 48 * 60 * 60:
        raise DriveRecorderError(
            "--duration-seconds must be between 1 and 172800"
        )
    if (
        not math.isfinite(args.ignition_absence_seconds)
        or not 5 <= args.ignition_absence_seconds <= 300
    ):
        raise DriveRecorderError(
            "--ignition-absence-seconds must be between 5 and 300"
        )
    return capture.DiskPolicy(
        soft_free_bytes=int(args.soft_free_gib * 1024**3),
        hard_free_bytes=int(args.hard_free_gib * 1024**3),
    )


def plan(args: argparse.Namespace, policy: capture.DiskPolicy) -> dict[str, object]:
    return {
        "mode": "execute" if args.execute else "plan_only",
        "interaction": "synchronized_three_bus_receive_only_companion",
        "trigger": "broker active_drive state=armed_diagnostic",
        "roles": ["c-can", *SECONDARY_ROLES],
        "routing": {
            "c-can": "broker-owned serial-resolved active-drive channel",
            "b-can": (
                "shared serial-resolved passive observer lease, or the broker's "
                "exact auxiliary-drive owner"
            ),
            "can-ch": "shared serial-resolved passive observer lease",
        },
        "roles_required": ["c-can"],
        "secondary_policy": (
            "best-effort segments with re-admission after a fresh route proof"
        ),
        "bitrates": {"c-can": BITRATE, "b-can": 125000, "can-ch": 500000},
        "output_root": str(args.out_root),
        "required_mount": str(args.require_mount),
        "state_path": str(args.state_path),
        "rotation_seconds": args.rotation_seconds,
        "duration_seconds": args.duration_seconds,
        "stop_after_id": f"0x{IGNITION_ID:X}",
        "stop_after_id_absence_seconds": args.ignition_absence_seconds,
        "secondary_required_start_ids": {
            role: f"0x{SECONDARY_START_IDS[role]:X}" for role in SECONDARY_ROLES
        },
        "soft_free_bytes": policy.soft_free_bytes,
        "hard_free_bytes": policy.hard_free_bytes,
        "does_not": [
            "configure or restore CAN",
            "acquire exclusive B-CAN or CAN-CH ownership",
            "transmit CAN",
            "control the telemetry broker",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = validate_args(args)
        if not args.execute:
            print(json.dumps(plan(args, policy), indent=2, sort_keys=True))
            return 0
        return run_daemon(args, policy)
    except (DriveRecorderError, capture.CaptureError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
