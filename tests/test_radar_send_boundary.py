"""Optional in-memory marker preserves existing radar callers' wire behavior."""
import pytest

from projects.vehicle_data import radar_alignment as radar, transmit_permit
from projects.vehicle_data.radar_service_transport import ENTER_SESSION, RadarServiceTransport
from tests.radar_service_support import Clock, FakeRadar


def link(monkeypatch):
    consumed = []
    monkeypatch.setattr(transmit_permit, 'consume', lambda *args, **kw: consumed.append(kw))
    sock = FakeRadar()
    poller = radar.RadarAlignmentPoller('can7', monotonic=Clock())
    poller.sock = sock
    return poller, sock, consumed


def test_old_0845_and_service_calls_are_output_identical(monkeypatch):
    poller, sock, consumed = link(monkeypatch)
    result = poller.poll(object(), object())
    assert result.available and result.angles == (0.278, -0.2)
    assert sock.requests() == ['22 08 45', 'FC']
    assert [item['purpose'] for item in consumed] == [transmit_permit.RADAR_ALIGNMENT,
                                                    transmit_permit.RADAR_ALIGNMENT_FLOW]
    result = RadarServiceTransport(poller).exchange(ENTER_SESSION, object())
    assert result.kind == 'positive' and result.payload == b'\x50\x03\x00\x32\x01\xf4'
    assert sock.requests() == ['22 08 45', 'FC', '10 03']


def test_marker_is_after_consume_and_before_send(monkeypatch):
    poller, sock, consumed = link(monkeypatch)
    marks = []
    def mark():
        assert len(consumed) == 1 and not sock.sent
        marks.append(True)
    poller.send_fixed(radar.REQUEST_DATA, object(), transmit_permit.RADAR_ALIGNMENT,
                      before_send=mark)
    assert marks == [True] and sock.requests() == ['22 08 45']


def test_marker_exception_surfaces_and_sends_nothing(monkeypatch):
    poller, sock, consumed = link(monkeypatch)
    def mark():
        raise ValueError('marker failed')
    with pytest.raises(ValueError, match='marker failed'):
        poller.send_fixed(radar.REQUEST_DATA, object(), transmit_permit.RADAR_ALIGNMENT,
                          before_send=mark)
    assert len(consumed) == 1 and not sock.sent
