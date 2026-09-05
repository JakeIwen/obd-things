#!/usr/bin/env python3
"""One guarded C-CAN owner for engine-running diagnostic telemetry.

This helper is started and supervised by the telemetry broker. It performs no
work while imported. A live invocation owns one bounded engine-running epoch:

* prove C-CAN pins 6/14 and multiple fresh passive 0x0FC RPM samples;
* acquire the exclusive cross-process lock and repeat every gate;
* arm the adapter once, continue receiving the existing broadcast allowlist,
  and issue only the fixed reviewed PCM/RF-Hub single-frame RDBI requests;
* close every socket, restore the exact safe listen-only state, verify it, and
  only then release the channel lock.

Machine-readable events are written to stdout. Human diagnostics go to stderr.
There is deliberately no arbitrary DID, payload, session, or tester-present
argument.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import pathlib
import re
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state, canbus, diagnostic_safety
from lib.can_role_resolver import SysfsCanRoleResolver
from lib.modules import MODULES
from projects.vehicle_data import (
    ccan_powertrain,
    pcm_electrical,
    transmit_permit,
)


BITRATE = 500000
PAIR = "6/14"
RUNNING_RPM = 400.0
REQUIRED_RPM_SAMPLES = 3
PASSIVE_GATE_SECONDS = 0.5
ACTIVE_SNAPSHOT_SECONDS = 0.35
POLL_INTERVAL_SECONDS = 1.0
VVT_TEMPERATURE_INTERVAL_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 0.45
RESTORATION_INHIBIT = "vehicle-data-restoration-failed"

AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
FRAME_TYPE_FLAGS = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG
EFF_FILTER_MASK = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)

TPMS_PROFILES = (
    (0x31D0, "tire.pressure.fl"),
    (0x31D1, "tire.pressure.fr"),
    (0x31D2, "tire.pressure.rr"),
    (0x31D3, "tire.pressure.rl"),
)
TPMS_INVALID = b"\xFF\xFF"
KPA_TO_PSI = 0.14503773773020923
TPMS_MIN_PSI = 0.0
TPMS_MAX_PSI = 150.0


@dataclass
class SessionOutcome:
    reason: str
    detail: str
    restored: bool | None = None


@dataclass(frozen=True)
class PressureResult:
    available: bool
    metric: str
    value: float | None = None
    source: str | None = None
    reason: str | None = None
    detail: str = ""


class JsonEventSink:
    def __init__(self, stream=sys.stdout):
        self.stream = stream

    def emit(self, event_type: str, **payload: object) -> None:
        event = {"type": event_type, **payload}
        self.stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        self.stream.flush()


class SystemBackend:
    """Narrow injectable boundary around live SocketCAN and operation state."""

    def __init__(
        self,
        channel: str,
        *,
        expected_usb_serial: str,
        expected_dev_id: int,
        role_resolver: SysfsCanRoleResolver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not expected_usb_serial:
            raise ValueError("active-drive USB serial is required")
        if (
            not isinstance(expected_dev_id, int)
            or isinstance(expected_dev_id, bool)
            or expected_dev_id < 0
        ):
            raise ValueError(
                "active-drive dev_id must be a non-negative integer"
            )
        self.channel = channel
        self.expected_usb_serial = expected_usb_serial
        self.expected_dev_id = expected_dev_id
        self.role_resolver = role_resolver or SysfsCanRoleResolver()
        self.monotonic = monotonic
        self.sleep = sleep
        # Preserve raw 0x1F7 temperature state across every bounded broadcast
        # snapshot in this one armed ownership interval.
        self.temperature_gate = (
            ccan_powertrain.TransmissionTemperaturePlausibilityGate()
        )

    def interface_state(self):
        return canbus.interface_state(self.channel)

    def identity_matches(self) -> bool:
        if (
            not isinstance(self.expected_usb_serial, str)
            or not self.expected_usb_serial
            or not isinstance(self.expected_dev_id, int)
            or isinstance(self.expected_dev_id, bool)
            or self.expected_dev_id < 0
        ):
            return False
        inventory, _issues = self.role_resolver.inventory(drivers=("gs_usb",))
        matches = [
            item
            for item in inventory
            if item.driver == "gs_usb"
            and item.usb_vid == "1d50"
            and item.usb_pid == "606f"
            and item.usb_serial == self.expected_usb_serial
            and item.dev_id == self.expected_dev_id
        ]
        return bool(
            len(matches) == 1
            and matches[0].channel == self.channel
        )

    def topology(self):
        return can_operation_state.load_topology(self.channel)

    def inhibits(self):
        return can_operation_state.active_inhibits(self.channel)

    def identify_bus(self):
        return canbus.identify_bus(self.channel, probe=0.25)

    def broadcast_snapshot(self, timeout: float):
        return ccan_powertrain.read_broadcast_snapshot(
            self.channel,
            timeout=timeout,
            include_battery=True,
            required_rpm_samples=REQUIRED_RPM_SAMPLES,
            monotonic=self.monotonic,
            temperature_gate=self.temperature_gate,
        )

    def arm(self, initial: canbus.InterfaceState) -> bool:
        if not self.identity_matches():
            return False
        restart_ms = initial.restart_ms if initial.restart_ms is not None else 0
        if not canbus.ip_up(
            self.channel,
            BITRATE,
            listen_only=False,
            restart_ms=restart_ms,
            noninteractive=True,
        ):
            return False
        state = self.interface_state()
        return bool(
            state.present
            and state.up
            and state.bitrate == BITRATE
            and state.fd_enabled is False
            and state.one_shot is False
            and not state.listen_only
            and state.controller_state == "ERROR-ACTIVE"
            and state.restart_ms == restart_ms
        )

    def restore(self, initial: canbus.InterfaceState) -> bool:
        # A hub reset may reuse the same canN for another adapter channel.
        # Never apply the old C-CAN state to a new physical identity.
        if not self.identity_matches():
            return False
        return bool(
            canbus.restore_interface_state(initial, noninteractive=True)
            and initial.same_configuration(self.interface_state())
        )

    def open_pcm(self):
        poller = pcm_electrical.PcmElectricalPoller(
            channel=self.channel,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        poller.open()
        return poller

    def open_tpms(self):
        poller = RfHubPressurePoller(
            channel=self.channel,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        poller.open()
        return poller


def _safe_passive_state(state: object) -> bool:
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.present
        and state.up
        and state.bitrate == BITRATE
        and state.fd_enabled is False
        and state.one_shot is False
        and state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms == 0
    )


def _safe_active_state(state: object, initial: canbus.InterfaceState) -> bool:
    return bool(
        isinstance(state, canbus.InterfaceState)
        and state.present
        and state.up
        and state.bitrate == BITRATE
        and state.fd_enabled is False
        and state.one_shot is False
        and not state.listen_only
        and state.controller_state == "ERROR-ACTIVE"
        and state.restart_ms
        == (initial.restart_ms if initial.restart_ms is not None else 0)
    )


def _backend_identity_matches(backend: object) -> bool:
    """Keep legacy injected test backends compatible; live backend always checks."""
    checker = getattr(backend, "identity_matches", None)
    return bool(checker()) if callable(checker) else True


def _running_snapshot(snapshot: object) -> bool:
    samples = getattr(snapshot, "rpm_samples", ())
    return bool(
        getattr(snapshot, "frame_count", 0) > 0
        and len(samples) >= REQUIRED_RPM_SAMPLES
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= RUNNING_RPM
            for value in samples[-REQUIRED_RPM_SAMPLES:]
        )
    )


def _topology_failure(topology: object) -> SessionOutcome | None:
    if not getattr(topology, "usable", False):
        return SessionOutcome(
            "wrong_bus",
            f"same-boot topology is unusable: {getattr(topology, 'reason', 'unknown')}",
        )
    if getattr(topology, "bus", None) != "c-can":
        return SessionOutcome(
            "wrong_bus",
            f"same-boot topology reports {getattr(topology, 'bus', 'unknown')}",
        )
    if getattr(topology, "pair", None) != PAIR:
        return SessionOutcome(
            "wrong_bus",
            f"C-CAN topology must explicitly report DLC pins {PAIR}",
        )
    return None


def _preflight_under_lock(backend: SystemBackend) -> tuple[canbus.InterfaceState | None, SessionOutcome | None]:
    if not _backend_identity_matches(backend):
        return None, SessionOutcome(
            "adapter_unhealthy",
            "resolved C-CAN USB identity is absent or no longer matches this canN",
        )
    initial = backend.interface_state()
    if not _safe_passive_state(initial):
        return None, SessionOutcome(
            "adapter_unhealthy",
            f"{backend.channel} must already be UP, 500 kbit/s classical CAN (FD off), "
            "listen-only, ERROR-ACTIVE, and expose readable restart timing",
        )
    topology_failure = _topology_failure(backend.topology())
    if topology_failure is not None:
        return None, topology_failure
    inhibits = backend.inhibits()
    if inhibits:
        names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
        return None, SessionOutcome(
            "inhibited",
            f"active C-CAN collection inhibited by {names}",
        )
    identified = backend.identify_bus()
    if identified != "c-can":
        reason = "bus_asleep" if identified == "silent" else "wrong_bus"
        return None, SessionOutcome(
            reason,
            f"passive C-CAN identity recheck returned {identified}",
        )
    snapshot = backend.broadcast_snapshot(PASSIVE_GATE_SECONDS)
    if not _running_snapshot(snapshot):
        return None, SessionOutcome(
            "engine_not_running",
            "fewer than three fresh passive 0x0FC samples were at or above 400 rpm",
        )
    checked = backend.interface_state()
    if not initial.same_configuration(checked):
        return None, SessionOutcome(
            "can_busy",
            "SocketCAN configuration changed during active-drive preflight",
        )
    return initial, None


class RfHubPressurePoller:
    """Round-robin reader for exactly four reviewed RF Hub pressure DIDs."""

    def __init__(
        self,
        *,
        channel: str,
        timeout: float,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.module = MODULES["rf_hub"]
        if not isinstance(channel, str) or not re.fullmatch(r"can[0-9]+", channel):
            raise ValueError("RF Hub pressure polling requires a SocketCAN canN channel")
        if isinstance(timeout, (bool, str, bytes, bytearray)):
            raise ValueError("timeout must be a positive finite number")
        try:
            normalized_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout must be a positive finite number") from None
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.channel = channel
        self.timeout = normalized_timeout
        self.socket_factory = socket_factory
        self.monotonic = monotonic
        self._socket = None
        self._index = 0

    def open(self) -> None:
        if self._socket is not None:
            raise RuntimeError("RF Hub pressure socket is already open")
        sock = self.socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        try:
            response_id = CAN_EFF_FLAG | self.module.rxid
            sock.setsockopt(
                SOL_CAN_RAW,
                CAN_RAW_FILTER,
                struct.pack("=II", response_id, EFF_FILTER_MASK),
            )
            sock.bind((self.channel,))
            sock.settimeout(self.timeout)
        except BaseException:
            try:
                sock.close()
            except Exception:
                pass
            raise
        self._socket = sock

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _drain(self) -> None:
        sock = self._socket
        if sock is None:
            raise RuntimeError("RF Hub pressure socket is not open")
        sock.settimeout(0.0)
        try:
            while True:
                try:
                    if not sock.recv(16):
                        break
                except (BlockingIOError, TimeoutError, socket.timeout):
                    break
        finally:
            sock.settimeout(self.timeout)

    def poll_next(self, permit: object) -> PressureResult:
        sock = self._socket
        if sock is None:
            raise RuntimeError("RF Hub pressure socket is not open")
        did, metric = TPMS_PROFILES[self._index]
        self._index = (self._index + 1) % len(TPMS_PROFILES)
        request_data = bytes((0x03, 0x22, did >> 8, did & 0xFF, 0, 0, 0, 0))
        request_frame = struct.pack(
            CAN_FRAME_FORMAT,
            CAN_EFF_FLAG | self.module.txid,
            8,
            request_data,
        )
        self._drain()
        try:
            transmit_permit.consume(
                permit,
                purpose=transmit_permit.RF_HUB_PRESSURE,
                channel=self.channel,
            )
        except transmit_permit.ExpiredTransmitPermitError as exc:
            return PressureResult(
                False,
                metric,
                reason="transmit_permit_expired",
                detail=f"fixed RF Hub request was skipped before send: {exc}",
            )
        except transmit_permit.TransmitPermitError as exc:
            return PressureResult(
                False,
                metric,
                reason="response_rejected",
                detail=f"fixed RF Hub request lacked a valid transmit permit: {exc}",
            )
        sent = sock.send(request_frame)
        if sent != len(request_frame):
            return PressureResult(
                False,
                metric,
                reason="response_rejected",
                detail=(
                    f"RF Hub fixed request send length {sent} "
                    f"is not {len(request_frame)}"
                ),
            )
        deadline = self.monotonic() + self.timeout
        while self.monotonic() < deadline:
            sock.settimeout(max(0.001, deadline - self.monotonic()))
            try:
                frame = sock.recv(16)
            except (TimeoutError, socket.timeout):
                break
            if not frame:
                break
            if len(frame) != CAN_FRAME_SIZE:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail=(
                        f"raw RF Hub response length {len(frame)} "
                        f"is not {CAN_FRAME_SIZE}"
                    ),
                )
            can_id, dlc, raw = struct.unpack(CAN_FRAME_FORMAT, frame)
            if dlc > 8:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail=f"RF Hub response DLC {dlc} exceeds classic CAN capacity",
                )
            if can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG):
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail="RF Hub response carried an RTR or CAN error flag",
                )
            if not can_id & CAN_EFF_FLAG:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail="RF Hub response did not use a 29-bit extended identifier",
                )
            response_id = can_id & CAN_EFF_MASK
            if response_id != self.module.rxid:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail=(
                        f"RF Hub response identifier 0x{response_id:08X} "
                        "did not match the registered physical endpoint"
                    ),
                )
            if dlc < 1:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail="RF Hub response omitted ISO-TP PCI",
                )

            data = raw[:dlc]
            pci_type = data[0] >> 4
            payload_length = data[0] & 0x0F
            if pci_type != 0:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail=(
                        "RF Hub response was not an ISO-TP SingleFrame; "
                        "no FlowControl was sent"
                    ),
                )
            if payload_length > 7 or len(data) < payload_length + 1:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail="RF Hub response was truncated before its declared payload",
                )
            payload = bytes(data[1 : 1 + payload_length])
            if payload_length == 3 and payload[:2] == b"\x7f\x22":
                return PressureResult(
                    False,
                    metric,
                    reason="response_rejected",
                    detail=f"RF Hub returned NRC {payload[2]:02X}",
                )
            expected = bytes((0x62, did >> 8, did & 0xFF))
            if payload_length != 5 or payload[:3] != expected:
                return PressureResult(
                    False,
                    metric,
                    reason="malformed_response",
                    detail="RF Hub response did not contain the exact DID echo and u16 payload",
                )
            raw_value = payload[3:5]
            if raw_value == TPMS_INVALID:
                return PressureResult(
                    False,
                    metric,
                    reason="sensor_unavailable",
                    detail="RF Hub returned FFFF invalid/no-sensor-data sentinel",
                )
            decoded_pressure = (
                int.from_bytes(raw_value, "big") * 0.1 * KPA_TO_PSI
            )
            if (
                not math.isfinite(decoded_pressure)
                or decoded_pressure < TPMS_MIN_PSI
                or decoded_pressure > TPMS_MAX_PSI
            ):
                return PressureResult(
                    False,
                    metric,
                    reason="response_rejected",
                    detail=(
                        f"decoded RF Hub pressure {decoded_pressure:.3f} psi "
                        "is physically implausible"
                    ),
                )
            pressure = round(decoded_pressure, 1)
            return PressureResult(
                True,
                metric,
                value=pressure,
                source=f"rf_hub.did.{did:04x}",
                detail=f"RF Hub DID {did:04X} u16 x 0.1 kPa, converted to psi",
            )
        return PressureResult(
            False,
            metric,
            reason="response_timeout",
            detail=f"RF Hub DID {did:04X} response timed out",
        )


def _emit_observation(sink: JsonEventSink, observation: ccan_powertrain.PassiveObservation) -> None:
    sink.emit(
        "observation",
        metric=observation.metric,
        value=observation.value,
        unit=observation.unit,
        source=observation.source,
        bus="c-can",
        quality=observation.quality,
        detail=observation.detail,
        interface_mode="armed_diagnostic",
    )


def _emit_quality_event(
    sink: JsonEventSink,
    event: ccan_powertrain.DataQualityEvent,
) -> None:
    """Report bounded acquisition evidence without failing the CAN session."""

    sink.emit(
        "quality_event",
        metric=event.metric,
        source=event.source,
        bus="c-can",
        quality="observed_alfa_scale",
        reason=event.reason,
        detail=event.detail,
        previous_value_c=event.previous_value_c,
        rejected_value_c=event.rejected_value_c,
        delta_c=event.delta_c,
        elapsed_seconds=event.elapsed_seconds,
        rejection_count=event.rejection_count,
        interface_mode="armed_diagnostic",
    )


def _pcm_outcome(result: object) -> SessionOutcome | None:
    if getattr(result, "available", False):
        return None
    return SessionOutcome(
        str(getattr(result, "reason", "helper_failed")),
        str(getattr(result, "detail", "PCM poll failed")),
    )


def _active_gate(
    backend: SystemBackend,
    initial: canbus.InterfaceState,
) -> SessionOutcome | None:
    if not _backend_identity_matches(backend):
        return SessionOutcome(
            "adapter_unhealthy",
            "resolved C-CAN USB identity changed or disappeared",
        )
    if not _safe_active_state(backend.interface_state(), initial):
        return SessionOutcome(
            "adapter_unhealthy",
            "active SocketCAN state changed or left ERROR-ACTIVE",
        )
    topology_failure = _topology_failure(backend.topology())
    if topology_failure is not None:
        return topology_failure
    inhibits = backend.inhibits()
    if inhibits:
        names = ",".join(
            str(item.get("name", "invalid")) for item in inhibits
        )
        return SessionOutcome(
            "inhibited",
            f"external-operation inhibit appeared: {names}",
        )
    return None


def _wait_for_next_cycle(
    backend: SystemBackend,
    next_cycle: float,
    cycle_started: float,
) -> float:
    """Preserve the one-hertz cadence after a completed or safely skipped cycle."""

    scheduled = max(next_cycle + POLL_INTERVAL_SECONDS, cycle_started)
    remaining = scheduled - backend.monotonic()
    if remaining > 0:
        backend.sleep(remaining)
    return scheduled


def run_active_session(
    backend: SystemBackend,
    sink: JsonEventSink,
    *,
    termination_guard=None,
) -> SessionOutcome:
    """Run one engine-running ownership interval and always restore before unlock."""
    lock_handle = None
    initial = None
    mutation_attempted = False
    pcm_poller = None
    tpms_poller = None
    outcome = SessionOutcome("helper_failed", "active-drive session did not start")
    restored: bool | None = None
    try:
        try:
            lock_handle = diagnostic_safety.acquire_channel_lock(backend.channel)
        except diagnostic_safety.ChannelLockError:
            outcome = SessionOutcome(
                "can_busy",
                f"another participating CAN operation owns {backend.channel}",
            )
            return outcome
        initial, blocked = _preflight_under_lock(backend)
        if blocked is not None:
            outcome = blocked
            return outcome
        if initial is None:
            outcome = SessionOutcome("helper_failed", "preflight lost initial interface state")
            return outcome

        mutation_attempted = True
        if not backend.arm(initial):
            outcome = SessionOutcome(
                "adapter_unhealthy",
                "could not arm and verify the C-CAN interface",
            )
            return outcome
        if not _safe_active_state(backend.interface_state(), initial):
            outcome = SessionOutcome(
                "adapter_unhealthy",
                "armed interface readback failed its health gate",
            )
            return outcome

        first_active = backend.broadcast_snapshot(PASSIVE_GATE_SECONDS)
        for quality_event in getattr(first_active, "quality_events", ()):
            _emit_quality_event(sink, quality_event)
        if not _running_snapshot(first_active):
            outcome = SessionOutcome(
                "engine_not_running",
                "fresh 0x0FC running evidence disappeared after arming",
            )
            return outcome

        pcm_poller = backend.open_pcm()
        tpms_poller = backend.open_tpms()
        sink.emit(
            "status",
            state="armed_diagnostic",
            reason="running_gate_satisfied",
            detail=(
                "exclusive C-CAN owner is armed; broadcast telemetry, PCM "
                "generator duty/current torque/VVT oil temperature, and fixed "
                "RF Hub pressures "
                "share this interval"
            ),
            interface_mode="armed_diagnostic",
            pid=os.getpid(),
        )

        next_cycle = backend.monotonic()
        torque_enabled = True
        vvt_temperature_enabled = True
        next_vvt_temperature = next_cycle
        while True:
            cycle_started = backend.monotonic()
            gate_failure = _active_gate(backend, initial)
            if gate_failure is not None:
                outcome = gate_failure
                break

            snapshot = backend.broadcast_snapshot(ACTIVE_SNAPSHOT_SECONDS)
            if getattr(snapshot, "frame_count", 0) <= 0:
                outcome = SessionOutcome(
                    "bus_asleep",
                    "C-CAN traffic vanished during the active interval",
                )
                break
            if not _running_snapshot(snapshot):
                outcome = SessionOutcome(
                    "engine_not_running",
                    "0x0FC became zero, sub-threshold, missing, or stale",
                )
                break
            for quality_event in getattr(snapshot, "quality_events", ()):
                _emit_quality_event(sink, quality_event)
            for observation in snapshot.observations:
                _emit_observation(sink, observation)

            # Broadcast collection takes a bounded interval. Repeat every cheap
            # health/topology/inhibit gate at the transmission boundary so a
            # mid-snapshot change cannot leak one diagnostic request.
            gate_failure = _active_gate(backend, initial)
            if gate_failure is not None:
                outcome = gate_failure
                break
            try:
                pcm_permit = transmit_permit.issue(
                    lock_handle,
                    snapshot,
                    purpose=transmit_permit.PCM_GENERATOR_DUTY,
                    channel=backend.channel,
                    monotonic=backend.monotonic,
                )
            except transmit_permit.StaleTransmitEvidenceError:
                # Host latency may age the 250 ms evidence window after a
                # successful snapshot.  Send nothing, preserve ownership, and
                # acquire a wholly new snapshot on the next scheduled cycle.
                next_cycle = _wait_for_next_cycle(
                    backend, next_cycle, cycle_started
                )
                continue
            pcm_result = pcm_poller.poll(pcm_permit)
            if getattr(pcm_result, "reason", None) == "transmit_permit_expired":
                next_cycle = _wait_for_next_cycle(
                    backend, next_cycle, cycle_started
                )
                continue
            failed = _pcm_outcome(pcm_result)
            if failed is not None:
                outcome = failed
                break
            sink.emit(
                "observation",
                metric=pcm_result.metric,
                value=pcm_result.value,
                unit=pcm_result.unit,
                source=pcm_result.source,
                bus=pcm_result.bus,
                quality=pcm_result.quality,
                detail=pcm_result.detail,
                interface_mode="armed_diagnostic",
            )

            if torque_enabled:
                # Each PCM response wait is a bounded gap. Recheck before
                # issuing the second, independently permitted fixed request.
                gate_failure = _active_gate(backend, initial)
                if gate_failure is not None:
                    outcome = gate_failure
                    break
                try:
                    torque_permit = transmit_permit.issue(
                        lock_handle,
                        snapshot,
                        purpose=transmit_permit.PCM_CRANKSHAFT_TORQUE,
                        channel=backend.channel,
                        monotonic=backend.monotonic,
                    )
                except transmit_permit.StaleTransmitEvidenceError:
                    next_cycle = _wait_for_next_cycle(
                        backend, next_cycle, cycle_started
                    )
                    continue
                torque_result = pcm_poller.poll_crankshaft_torque(
                    torque_permit
                )
                if (
                    getattr(torque_result, "reason", None)
                    == "transmit_permit_expired"
                ):
                    next_cycle = _wait_for_next_cycle(
                        backend, next_cycle, cycle_started
                    )
                    continue
                failed = _pcm_outcome(torque_result)
                if failed is not None:
                    # Torque is useful but not an ownership or capture safety
                    # prerequisite. Report one exact failure and stop polling
                    # it for this epoch; keep generator, TPMS, passive
                    # telemetry, and the receive-only recorder alive.
                    torque_enabled = False
                    sink.emit(
                        "metric_failure",
                        metric=torque_result.metric,
                        unit=torque_result.unit,
                        source=torque_result.source,
                        bus=torque_result.bus,
                        quality=torque_result.quality,
                        reason=failed.reason,
                        detail=failed.detail,
                        interface_mode="armed_diagnostic",
                    )
                else:
                    sink.emit(
                        "observation",
                        metric=torque_result.metric,
                        value=torque_result.value,
                        unit=torque_result.unit,
                        source=torque_result.source,
                        bus=torque_result.bus,
                        quality=torque_result.quality,
                        detail=torque_result.detail,
                        interface_mode="armed_diagnostic",
                    )

            # The second PCM response wait is another bounded gap. Recheck
            # again before the independent RF Hub request.
            gate_failure = _active_gate(backend, initial)
            if gate_failure is not None:
                outcome = gate_failure
                break
            try:
                pressure_permit = transmit_permit.issue(
                    lock_handle,
                    snapshot,
                    purpose=transmit_permit.RF_HUB_PRESSURE,
                    channel=backend.channel,
                    monotonic=backend.monotonic,
                )
            except transmit_permit.StaleTransmitEvidenceError:
                next_cycle = _wait_for_next_cycle(
                    backend, next_cycle, cycle_started
                )
                continue
            pressure = tpms_poller.poll_next(pressure_permit)
            if pressure.reason == "transmit_permit_expired":
                next_cycle = _wait_for_next_cycle(
                    backend, next_cycle, cycle_started
                )
                continue
            if pressure.available:
                sink.emit(
                    "observation",
                    metric=pressure.metric,
                    value=pressure.value,
                    unit="psi",
                    source=pressure.source,
                    bus="c-can",
                    quality="verified",
                    detail=pressure.detail,
                    interface_mode="armed_diagnostic",
                )
            elif pressure.reason not in ("sensor_unavailable",):
                outcome = SessionOutcome(
                    pressure.reason or "helper_failed",
                    pressure.detail,
                )
                break

            # Temperature changes slowly. Its fixed five-second read follows
            # the normal PCM/TPMS cycle and waits for every prior response.
            if (
                vvt_temperature_enabled
                and backend.monotonic() >= next_vvt_temperature
            ):
                gate_failure = _active_gate(backend, initial)
                if gate_failure is not None:
                    outcome = gate_failure
                    break
                try:
                    temperature_permit = transmit_permit.issue(
                        lock_handle,
                        snapshot,
                        purpose=transmit_permit.PCM_VVT_OIL_TEMPERATURE,
                        channel=backend.channel,
                        monotonic=backend.monotonic,
                    )
                except transmit_permit.StaleTransmitEvidenceError:
                    next_cycle = _wait_for_next_cycle(backend, next_cycle, cycle_started)
                    continue
                temperature_result = pcm_poller.poll_vvt_oil_temperature(
                    temperature_permit
                )
                if temperature_result.reason == "transmit_permit_expired":
                    next_cycle = _wait_for_next_cycle(backend, next_cycle, cycle_started)
                    continue
                next_vvt_temperature = (
                    backend.monotonic() + VVT_TEMPERATURE_INTERVAL_SECONDS
                )
                failed = _pcm_outcome(temperature_result)
                if failed is not None:
                    vvt_temperature_enabled = False
                    sink.emit(
                        "metric_failure",
                        metric=temperature_result.metric,
                        unit=temperature_result.unit,
                        source=temperature_result.source,
                        bus=temperature_result.bus,
                        quality=temperature_result.quality,
                        reason=failed.reason,
                        detail=failed.detail,
                        interface_mode="armed_diagnostic",
                    )
                else:
                    sink.emit(
                        "observation",
                        metric=temperature_result.metric,
                        value=temperature_result.value,
                        unit=temperature_result.unit,
                        source=temperature_result.source,
                        bus=temperature_result.bus,
                        quality=temperature_result.quality,
                        detail=temperature_result.detail,
                        interface_mode="armed_diagnostic",
                    )

            next_cycle = _wait_for_next_cycle(
                backend, next_cycle, cycle_started
            )
        return outcome
    except KeyboardInterrupt:
        outcome = SessionOutcome(
            "engine_not_running",
            "active-drive helper was terminated; cleanup started",
        )
        return outcome
    except BaseException as exc:
        outcome = SessionOutcome(
            "helper_failed",
            f"{type(exc).__name__}: {exc}",
        )
        return outcome
    finally:
        # The signal guard's contract requires cleanup protection to begin
        # before any socket close, interface restoration, or lock release. This
        # also covers a TERM that first arrives after a normal RPM-gate exit.
        if termination_guard is not None:
            termination_guard.begin_cleanup()
        for poller in (tpms_poller, pcm_poller):
            if poller is not None:
                try:
                    poller.close()
                except BaseException as exc:
                    print(
                        f"active-drive socket close failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        if mutation_attempted and initial is not None:
            try:
                restored = bool(backend.restore(initial))
            except BaseException as exc:
                restored = False
                print(
                    f"active-drive restoration raised: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            if not restored:
                # Return expressions are evaluated before ``finally``. Mutate
                # the same object so direct callers and the broker's final
                # event both observe the authoritative cleanup result.
                outcome.reason = "restoration_failed"
                outcome.detail = (
                    "exact safe listen-only SocketCAN restoration could not be verified"
                )
                outcome.restored = False
                try:
                    can_operation_state.begin_inhibit(
                        RESTORATION_INHIBIT,
                        channel="*",
                        reason=outcome.detail,
                    )
                except BaseException as exc:
                    print(
                        f"could not persist restoration inhibit: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                outcome.restored = True
        diagnostic_safety.release_channel_lock(lock_handle)
        # A return expression is evaluated before finally. Emit the authoritative
        # post-cleanup outcome here so the broker never trusts that earlier value.
        sink.emit(
            "final",
            state=(
                "restoration_failed"
                if outcome.reason == "restoration_failed"
                else "idle"
            ),
            reason=outcome.reason,
            detail=outcome.detail,
            interface_mode=(
                "listen_only"
                if restored is True
                else "armed_diagnostic"
                if restored is False
                else "unknown"
            ),
            restored=restored,
            pid=os.getpid(),
        )


def _set_parent_death_signal(
    expected_parent_pid: int,
    *,
    parent_pid_reader: Callable[[], int] | None = None,
    libc_loader: Callable[..., object] | None = None,
) -> None:
    """Install PDEATHSIG only while still parented by the expected broker.

    Linux does not retroactively deliver ``PR_SET_PDEATHSIG`` when the original
    parent dies before ``prctl``. Checking ``getppid`` on both sides of the call
    closes that standard fork/exec race: a child that was already reparented, or
    became reparented while installing the signal, refuses to approach CAN.
    """
    if type(expected_parent_pid) is not int or expected_parent_pid <= 1:
        raise RuntimeError("expected broker parent PID must be greater than one")
    read_parent_pid = parent_pid_reader or os.getppid
    load_libc = libc_loader or ctypes.CDLL
    observed_before = read_parent_pid()
    if observed_before != expected_parent_pid:
        raise RuntimeError(
            "active-drive helper parent did not match the supervising broker "
            f"before prctl (expected {expected_parent_pid}, observed {observed_before})"
        )
    try:
        libc = load_libc(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    except (AttributeError, OSError) as exc:
        raise RuntimeError(
            f"could not install broker parent-death signal: {exc}"
        ) from exc
    observed_after = read_parent_pid()
    if observed_after != expected_parent_pid:
        raise RuntimeError(
            "supervising broker disappeared while the parent-death signal was "
            f"installed (expected {expected_parent_pid}, observed {observed_after})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded engine-running C-CAN telemetry owner."
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="broker-resolved C-CAN SocketCAN netdev",
    )
    parser.add_argument("--expected-usb-serial", required=True)
    parser.add_argument(
        "--expected-dev-id",
        type=lambda value: int(value, 0),
        required=True,
    )
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"can[0-9]+", args.channel):
        raise SystemExit(
            "active-drive helper requires a resolved kernel canN channel"
        )
    if not args.expected_usb_serial or args.expected_dev_id < 0:
        raise SystemExit(
            "active-drive helper requires a valid USB serial and dev_id"
        )
    try:
        _set_parent_death_signal(args.expected_parent_pid)
    except Exception as exc:
        raise SystemExit(
            f"active-drive helper refused parent handshake: {exc}"
        ) from None
    sink = JsonEventSink()
    backend = SystemBackend(
        args.channel,
        expected_usb_serial=args.expected_usb_serial,
        expected_dev_id=args.expected_dev_id,
    )
    with diagnostic_safety.interrupt_on_termination() as termination:
        outcome = run_active_session(
            backend,
            sink,
            termination_guard=termination,
        )
    return 0 if outcome.restored is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
