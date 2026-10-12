"""The five fixed radar Service Drive Alignment requests and their replies.

Everything this feature can put on the bus is in this file: five single-frame
requests to the ACC radar and one FlowControl, each sent through the 0845
poller's socket behind its own one-use transmit permit. The run policy that
decides when to send them is in radar_service_alignment.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects.vehicle_data import transmit_permit
from projects.vehicle_data.radar_alignment import FLOW_CONTROL_DATA


PENDING_SECONDS = 5.0
MAX_PENDING_REPLIES = 2
# Routine state byte: 01 running, 03 completed, 04 idle.
ROUTINE_COMPLETED, ROUTINE_IDLE = 0x03, 0x04

# Reply kinds. SKIPPED: nothing was sent (the permit aged out first).
# NO_REPLY: the request went out, or the send itself failed, and no reply came.
POSITIVE, NEGATIVE, SKIPPED, NO_REPLY, MALFORMED = (
    "positive", "negative", "skipped", "no_reply", "malformed",
)


def _request(*payload: int) -> bytes:
    return bytes((len(payload), *payload)).ljust(8, b"\0")


@dataclass(frozen=True)
class Exchange:
    """One fixed single-frame request and the only reply shape accepted for it."""

    name: str
    request: bytes
    purpose: str
    positive: bytes
    min_length: int
    max_length: int
    timeout: float

    @property
    def purposes(self) -> tuple[str, ...]:
        """Permits one exchange needs: the request, plus FlowControl if the reply may be segmented."""
        return (self.purpose,) + ((transmit_permit.RADAR_SERVICE_FLOW,) if self.max_length > 7 else ())


# A reply longer than seven bytes arrives as FirstFrame plus one
# ConsecutiveFrame (at most 13 bytes), which the proven 30 01 00 FlowControl
# releases. The start/stop reply lengths were never recorded, so both shapes
# are accepted for them.
ENTER_SESSION = Exchange("10 03", _request(0x10, 0x03), transmit_permit.RADAR_SERVICE_SESSION,
                         b"\x50\x03", 2, 7, 0.5)
START_ROUTINE = Exchange("31 01 0251", _request(0x31, 0x01, 0x02, 0x51),
                         transmit_permit.RADAR_SERVICE_START, b"\x71\x01\x02\x51", 4, 13, 0.5)
STOP_ROUTINE = Exchange("31 02 0251", _request(0x31, 0x02, 0x02, 0x51),
                        transmit_permit.RADAR_SERVICE_STOP, b"\x71\x02\x02\x51", 4, 13, 0.5)
READ_STATUS = Exchange("31 03 0251", _request(0x31, 0x03, 0x02, 0x51),
                       transmit_permit.RADAR_SERVICE_STATUS, b"\x71\x03\x02\x51", 8, 8, 0.2)
KEEP_ALIVE = Exchange("3E 00", _request(0x3E, 0x00), transmit_permit.RADAR_SERVICE_KEEPALIVE,
                      b"\x7e\x00", 2, 2, 0.2)
EXCHANGES = (ENTER_SESSION, START_ROUTINE, STOP_ROUTINE, READ_STATUS, KEEP_ALIVE)


@dataclass(frozen=True)
class Reply:
    kind: str
    payload: bytes = b""
    nrc: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class RoutineStatus:
    """The four status bytes of ``71 03 02 51``: flags, state, progress, tail."""

    raw: bytes

    @classmethod
    def from_reply(cls, reply: Reply) -> "RoutineStatus":
        return cls(reply.payload[4:8])

    @property
    def state(self) -> int:
        return self.raw[1]

    @property
    def progress(self) -> int | None:
        return self.raw[2] if self.raw[2] <= 100 else None

    @property
    def text(self) -> str:
        return " ".join(f"{byte:02X}" for byte in self.raw)


class RadarServiceTransport:
    """The fixed service-alignment requests on the 0845 poller's socket.

    ``link`` is the open ``RadarAlignmentPoller``. Sharing its socket keeps one
    tester conversation with the radar: a second socket would leave these
    replies queued in front of the next 0845 read.
    """

    def __init__(self, link):
        self.link = link

    def _payload(self, spec: Exchange, flow_permit, deadline: float) -> bytes:
        first = self.link.receive(deadline)
        frame_type = first[0] >> 4
        if frame_type == 0:
            length = first[0] & 0x0F
            if not 1 <= length <= 7 or len(first) <= length:
                raise ValueError(f"{spec.name}: invalid single-frame length")
            return first[1:1 + length]
        length = ((first[0] & 0x0F) << 8) | first[1] if len(first) == 8 else 0
        if (frame_type != 1 or not 8 <= length <= spec.max_length
                or first[2:2 + len(spec.positive)] != spec.positive):
            raise ValueError(f"{spec.name}: unexpected first frame; no FlowControl sent")
        self.link.send_fixed(FLOW_CONTROL_DATA, flow_permit, transmit_permit.RADAR_SERVICE_FLOW)
        consecutive = self.link.receive(deadline)
        if consecutive[0] != 0x21 or len(first) - 2 + len(consecutive) - 1 < length:
            raise ValueError(f"{spec.name}: consecutive frame sequence/length mismatch")
        return (first[2:] + consecutive[1:])[:length]

    def _reply(self, spec: Exchange, flow_permit) -> Reply:
        deadline = self.link.monotonic() + spec.timeout
        for _ in range(MAX_PENDING_REPLIES + 1):
            payload = self._payload(spec, flow_permit, deadline)
            if payload[:1] != b"\x7f":
                if (payload[:len(spec.positive)] != spec.positive
                        or not spec.min_length <= len(payload) <= spec.max_length):
                    raise ValueError(f"{spec.name}: reply does not match the fixed request")
                return Reply(POSITIVE, payload)
            if len(payload) != 3 or payload[1] != spec.request[1]:
                raise ValueError(f"{spec.name}: negative reply for another service")
            if payload[2] != 0x78:
                return Reply(NEGATIVE, nrc=payload[2], detail=f"{spec.name} answered NRC {payload[2]:02X}")
            # responsePending: the request was accepted; wait, never resend.
            deadline = self.link.monotonic() + PENDING_SECONDS
        raise ValueError(f"{spec.name}: too many response-pending replies")

    def exchange(self, spec: Exchange, request_permit, flow_permit=None) -> Reply:
        sent = False
        try:
            self.link.drain()
            self.link.send_fixed(spec.request, request_permit, spec.purpose)
            sent = True
            return self._reply(spec, flow_permit)
        except transmit_permit.ExpiredTransmitPermitError as exc:
            return Reply(NO_REPLY if sent else SKIPPED, detail=f"{spec.name}: {exc}")
        except transmit_permit.TransmitPermitError:
            # Wrong lock/purpose/channel/clock is an owner safety failure.
            raise
        except ValueError as exc:
            return Reply(MALFORMED, detail=str(exc))
        except OSError as exc:
            return Reply(NO_REPLY, detail=f"{spec.name}: {exc}")
