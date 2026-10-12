"""Scripted radar ECU and authority fakes for the service-alignment tests.

``FakeRadar`` is a SocketCAN stand-in that answers the fixed requests the way
the MRR1evo14F answered them in June 2026: extended session, routine 0251 with
its four status bytes, TesterPresent, and the segmented 0845 read.
"""

from __future__ import annotations

import struct

from projects.vehicle_data import radar_alignment as radar
from projects.vehicle_data.radar_drive_steps import SessionEnding
from projects.vehicle_data.radar_service_alignment import EVENT_TYPE
from projects.vehicle_data.radar_service_control import validate_event


def radar_frame(data: bytes) -> bytes:
    return struct.pack(radar.FRAME, radar.EFF | radar.MODULE.rxid, 8, data.ljust(8, b"\0"))


class FakeRadar:
    """One socket: requests are recorded and answered from the routine's state."""

    def __init__(self, *, progress=(0, 40, 80, 100), start_nrc=None, session_nrc=None,
                 long_start_reply=False):
        self.progress = list(progress)
        self.start_nrc = start_nrc
        self.session_nrc = session_nrc
        self.long_start_reply = long_start_reply
        self.sent: list[bytes] = []
        self.queue: list[bytes] = []
        self.pending: list[bytes] = []
        self.timeout = None
        self.closed = False
        self.silent = False
        self.extended = False
        self.routine = "idle"
        self.reads = 0

    # -- socket ----------------------------------------------------------

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, _count):
        if not self.queue:
            raise BlockingIOError() if self.timeout == 0 else TimeoutError("no radar reply")
        return self.queue.pop(0)

    def send(self, frame):
        can_id, dlc, data = struct.unpack(radar.FRAME, frame)
        assert can_id == radar.EFF | radar.MODULE.txid and dlc == 8
        self.sent.append(data)
        if not self.silent:
            self._answer(data)
        return len(frame)

    def close(self):
        self.closed = True

    # -- ECU -------------------------------------------------------------

    def requests(self) -> list[str]:
        """Sent UDS requests as hex, FlowControl shown as ``FC``."""
        return ["FC" if data[0] == 0x30 else data[1:1 + data[0]].hex(" ").upper()
                for data in self.sent]

    def _reply(self, payload: bytes) -> None:
        if len(payload) <= 7:
            self.queue.append(radar_frame(bytes((len(payload),)) + payload))
            return
        self.queue.append(radar_frame(bytes((0x10, len(payload))) + payload[:6]))
        self.pending = [radar_frame(b"\x21" + payload[6:])]

    def _answer(self, data: bytes) -> None:
        if data[0] == 0x30:
            self.queue.extend(self.pending)
            self.pending = []
            return
        request = data[1:1 + data[0]]
        if request == b"\x22\x08\x45":
            return self._reply(b"\x62\x08\x45" + struct.pack(">ii", 278000, -200000))
        if request == b"\x10\x03":
            if self.session_nrc is not None:
                return self._reply(bytes((0x7F, 0x10, self.session_nrc)))
            self.extended, self.routine, self.reads = True, "idle", 0
            return self._reply(b"\x50\x03\x00\x32\x01\xf4")
        if request == b"\x3e\x00":
            return self._reply(b"\x7e\x00")
        if request[:1] == b"\x31" and not self.extended:
            return self._reply(b"\x7f\x31\x7f")
        if request == b"\x31\x01\x02\x51":
            if self.start_nrc is not None:
                return self._reply(bytes((0x7F, 0x31, self.start_nrc)))
            self.routine = "running"
            extra = b"\x01\x01\x00\x02" if self.long_start_reply else b""
            return self._reply(b"\x71\x01\x02\x51" + extra)
        if request == b"\x31\x02\x02\x51":
            self.routine = "idle"
            return self._reply(b"\x71\x02\x02\x51")
        if request == b"\x31\x03\x02\x51":
            return self._reply(b"\x71\x03\x02\x51" + self._status())
        raise AssertionError(f"unexpected radar request {request.hex()}")

    def _status(self) -> bytes:
        if self.routine == "idle":
            return b"\x00\x04\x00\x02"
        value = self.progress[min(self.reads, len(self.progress) - 1)]
        self.reads += 1
        if value >= 100:
            self.routine = "completed"
        return bytes((0x00, 0x03 if value >= 100 else 0x01, value, 0x00))

    def drop_session(self) -> None:
        self.extended, self.routine = False, "idle"


class Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeAuthority:
    """Grants opaque permits; ``script`` entries override one acquire each.

    ``SKIP`` stands for stale evidence (nothing may be sent this cycle); any
    other entry is the outcome that ends the helper session.
    """

    SKIP = object()

    def __init__(self):
        self.script: list[object] = []
        self.requested: list[tuple[str, ...]] = []

    def acquire(self, purposes, *, absent_detail):
        self.requested.append(tuple(purposes))
        if self.script:
            scripted = self.script.pop(0)
            if scripted is self.SKIP:
                return None
            raise SessionEnding(scripted)
        return tuple(object() for _ in purposes)


class Commands:
    """Stands in for the stdin reader: queued commands are returned once."""

    def __init__(self):
        self.queued: list[dict[str, str]] = []

    def send(self, command: str, request_id: str = "a" * 32) -> None:
        self.queued.append({"command": command, "request_id": request_id})

    def poll(self):
        queued, self.queued = self.queued, []
        return queued


class Sink:
    """Records helper events; service events must pass the broker's validator."""

    def __init__(self):
        self.events: list[dict[str, object]] = []

    def emit(self, event_type, **payload):
        event = {"type": event_type, **payload}
        if event_type == EVENT_TYPE:
            validate_event(event)
        self.events.append(event)

    def service(self) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == EVENT_TYPE]
