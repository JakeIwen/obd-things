#!/usr/bin/env python3
"""Guarded engine-running B-CAN owner for one fixed ICS odometer candidate.

The telemetry broker starts and supervises this helper alongside its existing
C-CAN active-drive owner.  The live path has no caller-selectable DID, payload,
module, cadence, session, or TesterPresent option.  It sends only physical ICS
ReadDataByIdentifier ``22 20 01`` while the parent broker keeps the qualified
engine-running epoch alive.

The returned value is deliberately candidate-quality ``vehicle.odometer``.
The first independent cluster comparison differed by 11.14 miles, so the
metric remains visibly starred in the dashboard until the ICS counter's
offset/update relationship is resolved.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
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
from projects.vehicle_data import active_drive, auxiliary_polling


BITRATE = 125000
PAIR = "3/11"
POLL_INTERVAL_SECONDS = 5.0
INITIAL_POLL_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 0.75
STARTUP_SIGNATURE_ATTEMPTS = 6
STARTUP_SIGNATURE_RETRY_SECONDS = 0.5
RESTORATION_INHIBIT = "vehicle-data-bcan-restoration-failed"
METRIC = "vehicle.odometer"
SOURCE = "ics.did.2001"
QUALITY = "candidate"
UNIT = "mi"

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

ICS = MODULES["ics_bcan"]
REQUEST_PAYLOAD = b"\x03\x22\x20\x01"
POSITIVE_PREFIX = b"\x06\x62\x20\x01"


@dataclass
class SessionOutcome:
    reason: str
    detail: str
    restored: bool | None = None


@dataclass(frozen=True)
class OdometerResult:
    available: bool
    value: float | None = None
    reason: str | None = None
    detail: str = ""


class JsonEventSink:
    def __init__(self, stream=sys.stdout):
        self.stream = stream

    def emit(self, event_type: str, **payload: object) -> None:
        event = {"type": event_type, **payload}
        self.stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        self.stream.flush()


class IcsOdometerPoller:
    """Raw-CAN fixed single-frame request/response transport.

    Avoiding a kernel ISO-TP socket prevents a malformed multi-frame response
    from generating FlowControl traffic.  The only possible send is the fixed
    four-byte single-frame request above.
    """

    def __init__(
        self,
        channel: str,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self.channel = channel
        self.timeout_seconds = timeout_seconds
        self.socket_factory = socket_factory
        self.socket = None

    def open(self) -> None:
        if self.socket is not None:
            raise RuntimeError("ICS odometer poller is already open")
        sock = self.socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
        response_id = ICS.rxid | CAN_EFF_FLAG
        sock.setsockopt(
            SOL_CAN_RAW,
            CAN_RAW_FILTER,
            struct.pack("=II", response_id, EFF_FILTER_MASK),
        )
        sock.settimeout(self.timeout_seconds)
        sock.bind((self.channel,))
        self.socket = sock

    def close(self) -> None:
        sock, self.socket = self.socket, None
        if sock is not None:
            sock.close()

    def _drain(self) -> None:
        assert self.socket is not None
        self.socket.setblocking(False)
        try:
            while True:
                try:
                    self.socket.recv(CAN_FRAME_SIZE)
                except BlockingIOError:
                    break
        finally:
            self.socket.settimeout(self.timeout_seconds)

    @staticmethod
    def _decode(frame: bytes) -> OdometerResult | None:
        if len(frame) != CAN_FRAME_SIZE:
            return OdometerResult(
                False,
                reason="malformed_response",
                detail="ICS response was not one complete classical CAN frame",
            )
        can_id, dlc, data = struct.unpack(CAN_FRAME_FORMAT, frame)
        if (
            can_id & FRAME_TYPE_FLAGS != CAN_EFF_FLAG
            or can_id & CAN_EFF_MASK != ICS.rxid
        ):
            return None
        payload = data[:dlc]
        if dlc == 4 and payload[:3] == b"\x03\x7f\x22":
            nrc = payload[3]
            return OdometerResult(
                False,
                reason=("session_required" if nrc in (0x7E, 0x7F) else "response_rejected"),
                detail=f"ICS rejected DID 2001 with NRC {nrc:02X}",
            )
        if dlc not in (7, 8):
            return OdometerResult(
                False,
                reason="malformed_response",
                detail=f"ICS response DLC {dlc} is not the exact 7-byte response or zero-padded DLC 8",
            )
        if not payload.startswith(POSITIVE_PREFIX):
            return OdometerResult(
                False,
                reason="malformed_response",
                detail="ICS response did not contain exact single-frame 62 20 01 echo",
            )
        if dlc == 8 and payload[7] != 0:
            return OdometerResult(
                False,
                reason="malformed_response",
                detail="ICS padded response contained a non-zero trailing byte",
            )
        uds = bytes(payload[1:7])
        try:
            miles = auxiliary_polling.decode_ics_odometer_miles(uds)
        except auxiliary_polling.AuxiliaryPollingPolicyError as exc:
            return OdometerResult(False, reason="malformed_response", detail=str(exc))
        if not math.isfinite(miles) or not 0.0 <= miles <= 2_000_000.0:
            return OdometerResult(
                False,
                reason="response_rejected",
                detail=f"decoded ICS odometer candidate {miles!r} mi is implausible",
            )
        return OdometerResult(
            True,
            value=round(miles, 3),
            detail=(
                "ICS DID 2001 u24be x 0.1 km converted to miles; candidate "
                "is known to differ from the cluster display"
            ),
        )

    def poll(self) -> OdometerResult:
        if self.socket is None:
            raise RuntimeError("ICS odometer poller is not open")
        self._drain()
        request = struct.pack(
            CAN_FRAME_FORMAT,
            ICS.txid | CAN_EFF_FLAG,
            len(REQUEST_PAYLOAD),
            REQUEST_PAYLOAD.ljust(8, b"\x00"),
        )
        sent = self.socket.send(request)
        if sent != len(request):
            return OdometerResult(
                False,
                reason="helper_failed",
                detail="ICS fixed request was not written as one complete CAN frame",
            )
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OdometerResult(
                    False,
                    reason="response_timeout",
                    detail="ICS DID 2001 response timed out",
                )
            self.socket.settimeout(remaining)
            try:
                frame = self.socket.recv(CAN_FRAME_SIZE)
            except TimeoutError:
                return OdometerResult(
                    False,
                    reason="response_timeout",
                    detail="ICS DID 2001 response timed out",
                )
            decoded = self._decode(frame)
            if decoded is not None:
                return decoded


class SystemBackend:
    def __init__(
        self,
        channel: str,
        *,
        expected_usb_serial: str,
        expected_dev_id: int,
        role_resolver: SysfsCanRoleResolver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.channel = channel
        self.expected_usb_serial = expected_usb_serial
        self.expected_dev_id = expected_dev_id
        self.role_resolver = role_resolver or SysfsCanRoleResolver()
        self.monotonic = monotonic
        self.sleep = sleep

    def interface_state(self):
        return canbus.interface_state(self.channel)

    def identity_matches(self) -> bool:
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
        return len(matches) == 1 and matches[0].channel == self.channel

    def topology(self):
        return can_operation_state.load_topology(self.channel)

    def inhibits(self):
        return can_operation_state.active_inhibits(self.channel)

    def identify_bus(self):
        return canbus.identify_bus(self.channel, probe=0.25)

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
        return _safe_active_state(self.interface_state(), initial)

    def restore(self, initial: canbus.InterfaceState) -> bool:
        if not self.identity_matches():
            return False
        return bool(
            canbus.restore_interface_state(initial, noninteractive=True)
            and initial.same_configuration(self.interface_state())
        )

    def open_poller(self):
        poller = IcsOdometerPoller(self.channel)
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
        and state.restart_ms == initial.restart_ms
    )


def _topology_failure(topology: object) -> SessionOutcome | None:
    if not (
        topology is not None
        and getattr(topology, "usable", False)
        and getattr(topology, "bus", None) == "b-can"
        and getattr(topology, "pair", None) == PAIR
    ):
        return SessionOutcome(
            "wrong_bus",
            "B-CAN topology is not a usable serial-resolved pins 3/11 record",
        )
    return None


def _gate(
    backend: SystemBackend,
    initial: canbus.InterfaceState,
    *,
    active: bool,
) -> SessionOutcome | None:
    if not backend.identity_matches():
        return SessionOutcome("adapter_unhealthy", "resolved B-CAN USB identity changed or disappeared")
    state = backend.interface_state()
    if not (_safe_active_state(state, initial) if active else _safe_passive_state(state)):
        return SessionOutcome("adapter_unhealthy", "B-CAN SocketCAN state failed its exact health gate")
    topology_failure = _topology_failure(backend.topology())
    if topology_failure is not None:
        return topology_failure
    inhibits = backend.inhibits()
    if inhibits:
        names = ",".join(str(item.get("name", "invalid")) for item in inhibits)
        return SessionOutcome("inhibited", f"external-operation inhibit appeared: {names}")
    if backend.identify_bus() != "b-can":
        return SessionOutcome("wrong_bus", "awake traffic did not match the verified B-CAN signature")
    return None


def _startup_gate(
    backend: SystemBackend,
    initial: canbus.InterfaceState,
    *,
    active: bool,
) -> SessionOutcome | None:
    """Retry only transient B-CAN signature absence during helper startup.

    USB identity, topology, controller state, and operation-inhibit failures
    remain immediate.  A running helper still uses the single strict
    :func:`_gate`; this grace cannot conceal a later bus/identity change.
    """

    signature_detail = (
        "awake traffic did not match the verified B-CAN signature"
    )
    blocked = None
    for attempt in range(STARTUP_SIGNATURE_ATTEMPTS):
        blocked = _gate(backend, initial, active=active)
        if blocked is None:
            return None
        if blocked.reason != "wrong_bus" or blocked.detail != signature_detail:
            return blocked
        if attempt + 1 < STARTUP_SIGNATURE_ATTEMPTS:
            backend.sleep(STARTUP_SIGNATURE_RETRY_SECONDS)
    return blocked


def run_active_session(
    backend: SystemBackend,
    sink: JsonEventSink,
    *,
    termination_guard=None,
) -> SessionOutcome:
    lock_handle = None
    initial = None
    mutation_attempted = False
    poller = None
    outcome = SessionOutcome("helper_failed", "B-CAN auxiliary session did not start")
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
        initial = backend.interface_state()
        blocked = _startup_gate(backend, initial, active=False)
        if blocked is not None:
            outcome = blocked
            return outcome
        mutation_attempted = True
        if not backend.arm(initial):
            outcome = SessionOutcome("adapter_unhealthy", "could not arm and verify the B-CAN interface")
            return outcome
        blocked = _startup_gate(backend, initial, active=True)
        if blocked is not None:
            outcome = blocked
            return outcome
        poller = backend.open_poller()
        sink.emit(
            "status",
            state="armed_diagnostic",
            reason="running_gate_satisfied",
            detail="exclusive B-CAN owner is armed for fixed ICS DID 2001 candidate polling",
            interface_mode="armed_diagnostic",
            pid=os.getpid(),
        )
        next_poll = backend.monotonic() + INITIAL_POLL_DELAY_SECONDS
        while True:
            remaining = next_poll - backend.monotonic()
            if remaining > 0:
                backend.sleep(remaining)
            blocked = _gate(backend, initial, active=True)
            if blocked is not None:
                outcome = blocked
                break
            result = poller.poll()
            if not result.available:
                outcome = SessionOutcome(result.reason or "helper_failed", result.detail)
                break
            sink.emit(
                "observation",
                metric=METRIC,
                value=result.value,
                unit=UNIT,
                source=SOURCE,
                bus="b-can",
                quality=QUALITY,
                detail=result.detail,
                interface_mode="armed_diagnostic",
            )
            next_poll = max(next_poll + POLL_INTERVAL_SECONDS, backend.monotonic())
        return outcome
    except KeyboardInterrupt:
        outcome = SessionOutcome("engine_not_running", "broker ended the qualified running interval; cleanup started")
        return outcome
    except BaseException as exc:
        outcome = SessionOutcome("helper_failed", f"{type(exc).__name__}: {exc}")
        return outcome
    finally:
        if termination_guard is not None:
            termination_guard.begin_cleanup()
        if poller is not None:
            try:
                poller.close()
            except BaseException as exc:
                print(f"B-CAN auxiliary socket close failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if mutation_attempted and initial is not None:
            try:
                restored = bool(backend.restore(initial))
            except BaseException as exc:
                restored = False
                print(f"B-CAN auxiliary restoration raised: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if not restored:
                outcome.reason = "restoration_failed"
                outcome.detail = "exact safe listen-only B-CAN restoration could not be verified"
                outcome.restored = False
                try:
                    can_operation_state.begin_inhibit(RESTORATION_INHIBIT, channel="*", reason=outcome.detail)
                except BaseException as exc:
                    print(f"could not persist B-CAN restoration inhibit: {exc}", file=sys.stderr, flush=True)
            else:
                outcome.restored = True
        diagnostic_safety.release_channel_lock(lock_handle)
        sink.emit(
            "final",
            state="restoration_failed" if outcome.reason == "restoration_failed" else "idle",
            reason=outcome.reason,
            detail=outcome.detail,
            interface_mode="listen_only" if restored is True else "armed_diagnostic" if restored is False else "unknown",
            restored=restored,
            pid=os.getpid(),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded engine-running B-CAN ICS telemetry owner.")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--expected-usb-serial", required=True)
    parser.add_argument("--expected-dev-id", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"can[0-9]+", args.channel):
        raise SystemExit("B-CAN auxiliary helper requires a resolved kernel canN channel")
    if not args.expected_usb_serial or args.expected_dev_id < 0:
        raise SystemExit("B-CAN auxiliary helper requires a valid USB serial and dev_id")
    try:
        active_drive._set_parent_death_signal(args.expected_parent_pid)
    except Exception as exc:
        raise SystemExit(f"B-CAN auxiliary helper refused parent handshake: {exc}") from None
    sink = JsonEventSink()
    backend = SystemBackend(
        args.channel,
        expected_usb_serial=args.expected_usb_serial,
        expected_dev_id=args.expected_dev_id,
    )
    with diagnostic_safety.interrupt_on_termination() as termination:
        outcome = run_active_session(backend, sink, termination_guard=termination)
    return 0 if outcome.restored is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
