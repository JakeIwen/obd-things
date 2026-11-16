"""Scripted ECU/watch boundaries; no SocketCAN or live service is used."""
from __future__ import annotations

import socket
import struct

from projects.vehicle_data import ccan_powertrain, drive_dtc_contract as contract
from projects.vehicle_data import radar_alignment, transmit_permit
from projects.vehicle_data.drive_dtc_transport import DriveDtcTransport
from tests.radar_service_support import Clock, FakeAuthority, Sink

CHANNEL = 'can7'
SESSION = 'a' * 32
FRAME = radar_alignment.FRAME
EFF = radar_alignment.EFF


def can_frame(module, data, *, can_id=None, dlc=None):
    endpoint = contract.ENDPOINTS[module]
    return struct.pack(FRAME, EFF | endpoint.rxid if can_id is None else can_id,
                       len(data) if dlc is None else dlc, data.ljust(8, b'\0'))


def single(payload):
    return bytes((len(payload),)) + payload + b'\0' * (7 - len(payload))


EMPTY = single(bytes.fromhex('59 02 FF'))
ONE = single(bytes.fromhex('59 02 FF 54 18 78 08'))
FIRST = bytes.fromhex('10 1B 59 02 FF 96 36 11')
PENDING = single(bytes.fromhex('7F 19 78'))


class ScriptedModule:
    """Replies scheduled at absolute offsets from the only request's send."""

    def __init__(self, module, clock, script=()):
        self.module, self.clock, self.script = module, clock, list(script)
        self.options, self.queue, self.sent, self.deadlines = [], [], [], []
        self.timeout, self.bound, self.closed = None, None, False
        self.send_error = None
        self.send_size = 16
        self.on_send = lambda: None

    def setsockopt(self, *option):
        self.options.append(option)

    def bind(self, address):
        self.bound = address

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True

    def send(self, frame):
        self.on_send()
        can_id, dlc, data = struct.unpack(FRAME, frame)
        assert (can_id, dlc, data) == (
            EFF | contract.ENDPOINTS[self.module].txid, 8, contract.REQUEST_DATA)
        self.sent.append(frame)
        if self.send_error is not None:
            raise self.send_error
        self.queue.extend((self.clock() + delay, item) for delay, item in self.script)
        return self.send_size

    def recv(self, _size):
        if self.timeout == 0:
            if not self.queue or self.queue[0][0] > self.clock():
                raise BlockingIOError()
        else:
            self.deadlines.append(self.clock() + self.timeout)
        if not self.queue or self.queue[0][0] > self.clock() + self.timeout:
            self.clock.value += self.timeout
            raise TimeoutError('script silence')
        when, item = self.queue.pop(0)
        self.clock.value = max(self.clock(), when)
        if isinstance(item, BaseException):
            raise item
        return item


class WatchFeed:
    def __init__(self, entries=()):
        self.entries, self.options = list(entries), []
        self.closed, self.timeout, self.bound = False, None, None
        self.reads = 0
        self.fail_open = False

    def setsockopt(self, *option):
        self.options.append(option)
        if self.fail_open:
            raise OSError('script open failed')

    def bind(self, address):
        self.bound = address

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True

    def recvmsg(self, size, ancillary_size, flags):
        assert size == 16 and flags == socket.MSG_DONTWAIT and self.timeout == 0
        assert ancillary_size >= socket.CMSG_SPACE(4)
        self.reads += 1
        if not self.entries:
            raise BlockingIOError()
        entry = self.entries.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def watch_entry(can_id, data, *, flags=socket.MSG_DONTROUTE, ancillary=(), dlc=8):
    return struct.pack(FRAME, can_id, dlc, data.ljust(8, b'\0')), ancillary, flags, (CHANNEL,)


class DtcSink(Sink):
    def emit(self, event_type, **payload):
        if event_type in contract.EVENT_TYPES:
            contract.validate_event({'type': event_type, **payload})
        super().emit(event_type, **payload)


def read_event(result, module='radar_acc'):
    return dict(type=contract.READ_EVENT, session=SESSION, **{'pass': 0}, module=module,
                request=contract.REQUEST, **result.event_fields(),
                started_at='2026-11-15T20:00:00Z', completed_at='2026-11-15T20:00:02Z',
                rpm=1650.0, speed_mph=61.2, interface_mode=contract.INTERFACE_MODE)


def permit_for(clock, module):
    evidence = ccan_powertrain.BroadcastSnapshot(
        observations=(), rpm_samples=(800., 801., 802.), frame_count=3,
        completed_monotonic=clock())
    return transmit_permit.issue(object(), evidence, purpose=contract.PURPOSES[module],
                                 channel=CHANNEL, monotonic=clock)


def rig(monkeypatch, module, replies=()):
    """Use real permit issue/consume and actual CAN packet parsing, fake only I/O."""
    # Radar open() evaluates these Linux constants even with a fake factory.
    monkeypatch.setattr(socket, 'AF_CAN', ccan_powertrain.AF_CAN, raising=False)
    monkeypatch.setattr(socket, 'CAN_RAW', ccan_powertrain.CAN_RAW, raising=False)
    monkeypatch.setattr(transmit_permit.diagnostic_safety, 'validate_channel_lock',
                        lambda *args: None)
    clock = Clock()
    script = [(at, can_frame(module, data) if isinstance(data, bytes) else data)
              for at, data in replies]
    sock = ScriptedModule(module, clock, script)
    factory = lambda *args: sock
    radar = None
    if module == 'radar_acc':
        radar = radar_alignment.RadarAlignmentPoller(CHANNEL, socket_factory=factory,
                                                    monotonic=clock).open()
    transport = DriveDtcTransport(CHANNEL, radar_link=radar, socket_factory=factory,
                                 monotonic=clock)
    return clock, sock, transport, permit_for(clock, module)
