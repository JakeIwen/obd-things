"""Closed, single-frame PCM engine-running telemetry reads.

This module deliberately is not a general UDS transport.  Its public poller can
send only the reviewed, physical, 29-bit SocketCAN frames for generator field
duty and current crankshaft torque.  Both expected responses are single-frame,
so a raw CAN socket is used instead of an ISO-TP socket: malformed multi-frame
traffic is rejected without any possibility of transmitting an ISO-TP
FlowControl frame.

Interface arming, topology checks, operation inhibits, and listen-only
restoration belong to the coordinated active-drive owner.  Every poll also
requires an opaque, short-lived, one-use permit that independently proves the
owner still holds the exclusive ``can0`` lock and issued the capability from a
qualified running-RPM snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
import socket
import struct
import time
from typing import Callable

from lib.modules import MODULES, NORMAL_29BITS
from projects.vehicle_data import transmit_permit


# Numeric SocketCAN constants keep the offline tests portable to Python builds
# that do not expose AF_CAN/CAN_RAW.
AF_CAN = getattr(socket, "AF_CAN", 29)
CAN_RAW = getattr(socket, "CAN_RAW", 1)
SOL_CAN_RAW = getattr(socket, "SOL_CAN_RAW", 101)
CAN_RAW_FILTER = getattr(socket, "CAN_RAW_FILTER", 1)

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)

GENERATOR_DUTY_REQUEST_DATA = bytes.fromhex(
    "03 22 01 A1 00 00 00 00"
)
GENERATOR_DUTY_POSITIVE_ECHO = bytes.fromhex("62 01 A1")
CRANKSHAFT_TORQUE_REQUEST_DATA = bytes.fromhex(
    "03 22 06 DA 00 00 00 00"
)
CRANKSHAFT_TORQUE_POSITIVE_ECHO = bytes.fromhex("62 06 DA")
NM_TO_LB_FT = 0.7375621492772656
SESSION_REQUIRED_NRCS = frozenset((0x7E, 0x7F))


@dataclass(frozen=True)
class PcmElectricalProfile:
    metric: str
    did: int
    unit: str
    source: str
    bus: str
    quality: str
    acquisition_class: str
    channel: str
    bitrate: int
    addressing_mode: str
    request_id: int
    response_id: int
    request_data: bytes
    positive_echo: bytes
    response_data_length: int
    minimum: float
    maximum: float

    @property
    def request_frame(self) -> bytes:
        return struct.pack(
            CAN_FRAME_FORMAT,
            CAN_EFF_FLAG | self.request_id,
            8,
            self.request_data,
        )

    @property
    def response_filter(self) -> bytes:
        # CAN_ERR_FLAG has special receive-filter semantics in SocketCAN and is
        # therefore rejected in userspace rather than included in this mask.
        return struct.pack(
            "=II",
            CAN_EFF_FLAG | self.response_id,
            CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_EFF_MASK,
        )


@dataclass(frozen=True)
class PcmElectricalResult:
    """One decoded observation or one fail-closed bounded failure."""

    metric: str
    available: bool
    unit: str
    value: float | None = None
    raw_value: int | None = None
    source: str | None = None
    bus: str | None = None
    quality: str | None = None
    acquisition: str | None = None
    reason: str | None = None
    detail: str = ""


_PCM = MODULES["pcm"]
_GENERATOR_FIELD_DUTY_PROFILE = PcmElectricalProfile(
    metric="generator.field_duty",
    did=0x01A1,
    unit="%",
    source="pcm.did.01a1",
    bus="c-can",
    quality="observed_alfa_scale",
    acquisition_class="physical_read_data_by_identifier",
    channel=_PCM.channel,
    bitrate=_PCM.bitrate,
    addressing_mode=_PCM.addressing_mode,
    request_id=_PCM.txid,
    response_id=_PCM.rxid,
    request_data=GENERATOR_DUTY_REQUEST_DATA,
    positive_echo=GENERATOR_DUTY_POSITIVE_ECHO,
    response_data_length=2,
    minimum=0.0,
    maximum=101.0,
)
GENERATOR_FIELD_DUTY_PROFILE = _GENERATOR_FIELD_DUTY_PROFILE
_CRANKSHAFT_TORQUE_PROFILE = PcmElectricalProfile(
    metric="engine.crankshaft_torque",
    did=0x06DA,
    unit="lb-ft",
    source="pcm.did.06da",
    bus="c-can",
    quality="observed_alfa_scale",
    acquisition_class="physical_read_data_by_identifier",
    channel=_PCM.channel,
    bitrate=_PCM.bitrate,
    addressing_mode=_PCM.addressing_mode,
    request_id=_PCM.txid,
    response_id=_PCM.rxid,
    request_data=CRANKSHAFT_TORQUE_REQUEST_DATA,
    positive_echo=CRANKSHAFT_TORQUE_POSITIVE_ECHO,
    response_data_length=2,
    minimum=-1000.0,
    maximum=1000.0,
)
CRANKSHAFT_TORQUE_PROFILE = _CRANKSHAFT_TORQUE_PROFILE

# MappingProxyType plus a frozen value makes the reviewed registry immutable at
# runtime.  No function below accepts a profile name, DID, CAN ID, or payload.
PCM_ELECTRICAL_PROFILES = MappingProxyType(
    {
        _GENERATOR_FIELD_DUTY_PROFILE.metric: _GENERATOR_FIELD_DUTY_PROFILE,
        _CRANKSHAFT_TORQUE_PROFILE.metric: _CRANKSHAFT_TORQUE_PROFILE,
    }
)


def _validate_closed_registry() -> None:
    if tuple(PCM_ELECTRICAL_PROFILES) != (
        "generator.field_duty",
        "engine.crankshaft_torque",
    ):
        raise RuntimeError(
            "PCM engine-running allowlist must contain exactly the two "
            "reviewed metrics"
        )
    expected_module = (
        "pcm",
        "c-can",
        500000,
        NORMAL_29BITS,
        0x18DA10F1,
        0x18DAF110,
    )
    observed_module = (
        _PCM.key,
        _PCM.bus,
        _PCM.bitrate,
        _PCM.addressing_mode,
        _PCM.txid,
        _PCM.rxid,
    )
    if observed_module != expected_module:
        raise RuntimeError(
            "registered PCM endpoint no longer matches the reviewed electrical "
            f"profile: observed={observed_module!r}"
        )
    if (
        _GENERATOR_FIELD_DUTY_PROFILE.did != 0x01A1
        or _GENERATOR_FIELD_DUTY_PROFILE.request_data
        != bytes.fromhex("03 22 01 A1 00 00 00 00")
        or _GENERATOR_FIELD_DUTY_PROFILE.positive_echo
        != bytes.fromhex("62 01 A1")
        or _GENERATOR_FIELD_DUTY_PROFILE.response_data_length != 2
    ):
        raise RuntimeError("Generator field duty wire profile changed")
    if (
        _CRANKSHAFT_TORQUE_PROFILE.did != 0x06DA
        or _CRANKSHAFT_TORQUE_PROFILE.request_data
        != bytes.fromhex("03 22 06 DA 00 00 00 00")
        or _CRANKSHAFT_TORQUE_PROFILE.positive_echo
        != bytes.fromhex("62 06 DA")
        or _CRANKSHAFT_TORQUE_PROFILE.response_data_length != 2
    ):
        raise RuntimeError("Crankshaft torque wire profile changed")


_validate_closed_registry()


def _failure(
    profile: PcmElectricalProfile,
    reason: str,
    detail: str,
) -> PcmElectricalResult:
    return PcmElectricalResult(
        metric=profile.metric,
        available=False,
        unit=profile.unit,
        source=profile.source,
        bus=profile.bus,
        quality=profile.quality,
        acquisition=profile.acquisition_class,
        reason=reason,
        detail=detail,
    )


def _decode_response(
    profile: PcmElectricalProfile,
    frame: bytes,
) -> PcmElectricalResult:
    if len(frame) != CAN_FRAME_SIZE:
        return _failure(
            profile,
            "malformed_response",
            f"raw SocketCAN response length {len(frame)} is not {CAN_FRAME_SIZE}",
        )

    raw_can_id, dlc, padded_data = struct.unpack(CAN_FRAME_FORMAT, frame)
    if dlc > 8:
        return _failure(
            profile,
            "malformed_response",
            f"response DLC {dlc} exceeds classic CAN capacity",
        )
    if raw_can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG):
        return _failure(
            profile,
            "malformed_response",
            "response carried an RTR or CAN error flag",
        )
    if not raw_can_id & CAN_EFF_FLAG:
        return _failure(
            profile,
            "malformed_response",
            "response did not use the reviewed 29-bit extended identifier",
        )
    response_id = raw_can_id & CAN_EFF_MASK
    if response_id != profile.response_id:
        return _failure(
            profile,
            "malformed_response",
            f"response identifier 0x{response_id:08X} did not match PCM",
        )
    if dlc < 1:
        return _failure(
            profile,
            "malformed_response",
            "response omitted ISO-TP PCI",
        )

    data = padded_data[:dlc]
    pci = data[0]
    if pci >> 4 != 0:
        return _failure(
            profile,
            "malformed_response",
            "response was not an ISO-TP SingleFrame; no FlowControl was sent",
        )
    payload_length = pci & 0x0F
    if payload_length != 3 + profile.response_data_length:
        return _failure(
            profile,
            "malformed_response",
            "response SingleFrame length "
            f"{payload_length} is not {3 + profile.response_data_length}",
        )
    if dlc < payload_length + 1:
        return _failure(
            profile,
            "malformed_response",
            "response was truncated before its declared SingleFrame payload",
        )

    payload = data[1 : payload_length + 1]
    if payload[0] == 0x7F:
        # A valid UDS negative response is three bytes, so it cannot also have
        # the five-byte length required by the positive Generator Duty profile.
        return _failure(
            profile,
            "malformed_response",
            "negative response used the positive response payload length",
        )
    if payload[:3] != profile.positive_echo:
        return _failure(
            profile,
            "malformed_response",
            "response did not exactly echo service 62 and the reviewed DID",
        )

    if profile is _GENERATOR_FIELD_DUTY_PROFILE:
        raw_value = int.from_bytes(payload[3:5], "big")
        value = raw_value * 100.0 / 32768.0
        decode_detail = "u16be x 100 / 32768"
    elif profile is _CRANKSHAFT_TORQUE_PROFILE:
        raw_value = int.from_bytes(payload[3:5], "big", signed=True)
        torque_nm = raw_value * 0.04
        value = torque_nm * NM_TO_LB_FT
        decode_detail = "i16be x 0.04 Nm, converted to lb-ft"
    else:
        raise RuntimeError("unreviewed PCM profile reached the decoder")
    if (
        not math.isfinite(value)
        or value < profile.minimum
        or value > profile.maximum
    ):
        return _failure(
            profile,
            "response_rejected",
            f"decoded {profile.metric} value {value:.6f} {profile.unit} "
            "is implausible",
        )
    return PcmElectricalResult(
        metric=profile.metric,
        available=True,
        unit=profile.unit,
        value=value,
        raw_value=raw_value,
        source=profile.source,
        bus=profile.bus,
        quality=profile.quality,
        acquisition=profile.acquisition_class,
        detail=(
            f"physical PCM 22 {profile.did:04X}; exact "
            f"62 {profile.did:04X} echo; {decode_detail}"
        ),
    )


def _decode_wire_response(
    profile: PcmElectricalProfile,
    frame: bytes,
) -> PcmElectricalResult:
    """Decode a positive or negative fixed-ID ISO-TP SingleFrame."""

    if len(frame) != CAN_FRAME_SIZE:
        return _decode_response(profile, frame)
    raw_can_id, dlc, padded_data = struct.unpack(CAN_FRAME_FORMAT, frame)
    if (
        dlc <= 8
        and raw_can_id & CAN_EFF_FLAG
        and not raw_can_id & (CAN_RTR_FLAG | CAN_ERR_FLAG)
        and raw_can_id & CAN_EFF_MASK == profile.response_id
        and dlc >= 1
    ):
        data = padded_data[:dlc]
        pci = data[0]
        payload_length = pci & 0x0F
        if pci >> 4 == 0 and payload_length == 3:
            if dlc < 4:
                return _failure(
                    profile,
                    "malformed_response",
                    "negative response was truncated",
                )
            payload = data[1:4]
            if payload[:2] != b"\x7F\x22":
                return _failure(
                    profile,
                    "malformed_response",
                    "negative response did not echo request service 22",
                )
            nrc = payload[2]
            if nrc in SESSION_REQUIRED_NRCS:
                return _failure(
                    profile,
                    "session_required",
                    f"PCM rejected 22 {profile.did:04X} in the active "
                    f"session with NRC {nrc:02X}",
                )
            return _failure(
                profile,
                "response_rejected",
                f"PCM rejected 22 {profile.did:04X} with NRC {nrc:02X}",
            )
    return _decode_response(profile, frame)


def _positive_finite_timeout(value: object) -> float:
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise ValueError("timeout_seconds must be a positive finite number")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "timeout_seconds must be a positive finite number"
        ) from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return normalized


class PcmElectricalPoller:
    """Reusable owner-scoped transport for two reviewed PCM metrics.

    ``channel`` exists so a coordinated active-drive owner can pass its already
    verified interface explicitly.  It must still equal the PCM registry
    channel; selecting another bus fails before a socket is opened.
    """

    def __init__(
        self,
        channel: str = _GENERATOR_FIELD_DUTY_PROFILE.channel,
        *,
        timeout_seconds: float = 0.5,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if channel != _GENERATOR_FIELD_DUTY_PROFILE.channel:
            raise ValueError(
                "PCM electrical polling is restricted to "
                f"{_GENERATOR_FIELD_DUTY_PROFILE.channel!r}"
            )
        self.channel = channel
        self.timeout_seconds = _positive_finite_timeout(timeout_seconds)
        self._socket_factory = socket_factory
        self._monotonic = monotonic
        self._socket: socket.socket | None = None

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    def open(self) -> PcmElectricalPoller:
        """Open and filter the raw socket without transmitting."""

        if self._socket is not None:
            return self
        sock = None
        try:
            sock = self._socket_factory(AF_CAN, socket.SOCK_RAW, CAN_RAW)
            sock.setsockopt(
                SOL_CAN_RAW,
                CAN_RAW_FILTER,
                _GENERATOR_FIELD_DUTY_PROFILE.response_filter,
            )
            sock.bind((self.channel,))
        except BaseException:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise
        self._socket = sock
        return self

    def close(self) -> None:
        """Close the raw socket; interface restoration remains owner-managed."""

        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def __enter__(self) -> PcmElectricalPoller:
        return self.open()

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.close()
        return False

    def poll(self, permit: object) -> PcmElectricalResult:
        """Poll generator field duty; retained as the narrow legacy entrypoint."""

        return self._poll(
            _GENERATOR_FIELD_DUTY_PROFILE,
            transmit_permit.PCM_GENERATOR_DUTY,
            permit,
        )

    def poll_crankshaft_torque(
        self,
        permit: object,
    ) -> PcmElectricalResult:
        """Consume one torque permit and send only physical PCM ``22 06DA``."""

        return self._poll(
            _CRANKSHAFT_TORQUE_PROFILE,
            transmit_permit.PCM_CRANKSHAFT_TORQUE,
            permit,
        )

    def _poll(
        self,
        profile: PcmElectricalProfile,
        purpose: str,
        permit: object,
    ) -> PcmElectricalResult:
        """Consume one owner permit, send one fixed request, and validate it."""

        try:
            self.open()
        except (OSError, RuntimeError, ValueError) as exc:
            return _failure(
                profile,
                "response_rejected",
                f"could not open the fixed PCM raw transport: {exc}",
            )

        sock = self._socket
        assert sock is not None
        try:
            transmit_permit.consume(
                permit,
                purpose=purpose,
                channel=self.channel,
            )
        except transmit_permit.TransmitPermitError as exc:
            return _failure(
                profile,
                "response_rejected",
                f"fixed PCM request lacked a valid transmit permit: {exc}",
            )
        deadline = self._monotonic() + self.timeout_seconds
        try:
            sent = sock.send(profile.request_frame)
        except OSError as exc:
            self.close()
            return _failure(
                profile,
                "response_rejected",
                f"fixed PCM request could not be sent: {exc}",
            )
        if sent != CAN_FRAME_SIZE:
            self.close()
            return _failure(
                profile,
                "response_rejected",
                f"fixed PCM request send length {sent} is not {CAN_FRAME_SIZE}",
            )

        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return _failure(
                profile,
                "response_timeout",
                "PCM response deadline expired after the fixed request",
            )
        try:
            sock.settimeout(remaining)
            frame = sock.recv(CAN_FRAME_SIZE)
        except (socket.timeout, TimeoutError):
            return _failure(
                profile,
                "response_timeout",
                f"PCM did not answer physical 22 {profile.did:04X} "
                "before the deadline",
            )
        except OSError as exc:
            self.close()
            return _failure(
                profile,
                "response_rejected",
                f"PCM response receive failed: {exc}",
            )
        try:
            frame_bytes = bytes(frame)
        except (TypeError, ValueError):
            return _failure(
                profile,
                "malformed_response",
                "raw SocketCAN transport returned a non-byte response",
            )
        return _decode_wire_response(profile, frame_bytes)
