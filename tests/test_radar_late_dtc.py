"""Late DTC frames never acknowledge a different radar request or extend it."""
import pytest

from projects.vehicle_data import radar_alignment as radar, transmit_permit
from projects.vehicle_data.radar_service_transport import (
    ENTER_SESSION, START_ROUTINE, RadarServiceTransport,
)
from tests.radar_service_support import Clock, FakeRadar, radar_frame


STALE = (
    bytes.fromhex("03 59 02 FF 00 00 00 00"),
    bytes.fromhex("03 7F 19 78 00 00 00 00"),
    bytes.fromhex("10 1B 59 02 FF 91 41 78"),
)


class LateRadar(FakeRadar):
    def __init__(self, clock, stale, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.stale = clock, stale
        self.deadlines = []

    def _answer(self, data):
        super()._answer(data)
        if data[0] != 0x30:
            self.queue[:0] = [radar_frame(frame) for frame in self.stale]

    def recv(self, count):
        if self.timeout != 0:
            self.deadlines.append(self.clock() + self.timeout)
            self.clock.value += 0.01
        return super().recv(count)


def link(monkeypatch, stale, **kwargs):
    # Only the permit owner is substituted; production parsers and both
    # transports exchange actual packed CAN frames with the scripted ECU.
    monkeypatch.setattr(transmit_permit, "consume", lambda *args, **kw: None)
    clock = Clock()
    sock = LateRadar(clock, stale, **kwargs)
    poller = radar.RadarAlignmentPoller("can7", monotonic=clock)
    poller.sock = sock
    return poller, sock


@pytest.mark.parametrize("stale", [STALE, (STALE[0],) * 4])
def test_0845_skips_late_dtc_with_original_deadline(monkeypatch, stale):
    poller, sock = link(monkeypatch, stale)
    result = poller.poll(object(), object())
    assert result.available and result.angles == (0.278, -0.2)
    assert sock.requests() == ["22 08 45", "FC"]
    assert sock.deadlines == pytest.approx([1000.2] * (len(stale) + 2))


@pytest.mark.parametrize("spec", [ENTER_SESSION, START_ROUTINE])
def test_service_ack_skips_late_dtc_without_extra_send(monkeypatch, spec):
    poller, sock = link(monkeypatch, STALE)
    sock.extended = True
    result = RadarServiceTransport(poller).exchange(spec, object())
    assert result.kind == "positive"
    assert len(sock.sent) == 1 and sock.sent[0] == spec.request
    assert sock.deadlines == pytest.approx([1000.5] * 4)


@pytest.mark.parametrize("service", [False, True])
def test_fifth_stale_falls_through_malformed(monkeypatch, service):
    poller, sock = link(monkeypatch, (STALE[0],) * 5)
    if service:
        result = RadarServiceTransport(poller).exchange(ENTER_SESSION, object())
        assert result.kind == "malformed"
    else:
        result = poller.poll(object(), object())
        assert result.reason == "malformed_response"
    assert len(sock.sent) == 1 and len(sock.deadlines) == 5


def test_own_service_negatives_are_not_discarded(monkeypatch):
    poller, sock = link(monkeypatch, (), start_nrc=0x31)
    sock.extended = True
    result = RadarServiceTransport(poller).exchange(START_ROUTINE, object())
    assert result.kind == "negative" and result.nrc == 0x31
    poller, sock = link(monkeypatch, (bytes.fromhex("03 7F 22 7F"),))
    result = poller.poll(object(), object())
    assert result.reason == "session_required" and len(sock.sent) == 1
