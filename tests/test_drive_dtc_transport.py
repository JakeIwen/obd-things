"""Fixed sends, bounded receives and attempted-before-send on scripted sockets."""
import struct

import pytest

from projects.vehicle_data import drive_dtc_contract as contract, transmit_permit
from projects.vehicle_data.drive_dtc_transport import AttemptMarkerError
from tests.drive_dtc_support import (
    CHANNEL, EMPTY, ONE, FIRST, PENDING, can_frame, single, rig, read_event,
)


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('response,outcome,count', [
    (EMPTY, 'complete', 0), (ONE, 'complete', 1), (FIRST, 'truncated', 6),
    (single(bytes.fromhex('7F 19 31')), 'negative', None),
    (single(bytes.fromhex('7F 19 7E')), 'negative', None),
    (single(bytes.fromhex('7F 19 7F')), 'negative', None),
    (b'\x21\x59\x02\xff', 'malformed', None),
    (b'\x30\0\0', 'malformed', None),
    (b'\x02\x59\x02', 'malformed', None),
])
def test_exact_request_and_no_flow_control(monkeypatch, module, response, outcome, count):
    clock, sock, transport, permit = rig(monkeypatch, module, [(0.1, response)])
    marks = []
    sock.on_send = lambda: marks == [1000.] or pytest.fail('send preceded attempt mark')
    result = transport.read(module, permit, before_send=lambda: marks.append(clock()))
    assert result.outcome == outcome and result.count == count and result.attempted
    assert len(sock.sent) == 1 and marks == [1000.]
    if module == 'bcm_ccan':
        assert sock.closed and sock.bound == (CHANNEL,)
    contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('pending_count', range(4))
def test_pending_deadline_is_anchored_to_send(monkeypatch, module, pending_count):
    pendings = [(0.1 + i * 0.5, PENDING) for i in range(pending_count)]
    clock, sock, transport, permit = rig(monkeypatch, module, pendings)
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.outcome == 'no_response' and result.reason == 'timeout'
    assert result.pending_count == pending_count and len(sock.sent) == 1
    assert clock() == (1002.5 if pending_count else 1001.)
    assert sock.deadlines == [1001.] + [1002.5] * pending_count
    contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_pending_count_does_not_reject_an_on_time_reply(monkeypatch, module):
    for number, outcome in ((3, 'complete'), (4, 'complete'), (100, 'complete')):
        replies = [(0.1, PENDING)] * number + [(0.2, EMPTY)]
        clock, sock, transport, permit = rig(monkeypatch, module, replies)
        result = transport.read(module, permit, before_send=lambda: None)
        assert result.outcome == outcome and result.pending_count == number
        assert len(sock.sent) == 1 and clock() <= 1000.2
        contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_late_answer_stays_timeout(monkeypatch, module):
    clock, sock, transport, permit = rig(monkeypatch, module, [(0.1, PENDING), (2.6, EMPTY)])
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.outcome == 'no_response' and clock() == 1002.5
    assert len(sock.sent) == 1


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('error', [OSError('ambiguous'), ValueError('send'), TimeoutError('send')])
def test_ambiguous_send_is_marked_and_never_resent(monkeypatch, module, error):
    clock, sock, transport, permit = rig(monkeypatch, module)
    marks = []
    sock.send_error = error
    result = transport.read(module, permit, before_send=lambda: marks.append(True))
    assert marks == [True] and result.attempted and result.reason == 'send_error'
    assert len(sock.sent) == 1
    contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_short_send_is_ambiguous_attempt(monkeypatch, module):
    _clock, sock, transport, permit = rig(monkeypatch, module)
    sock.send_size = 0
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.attempted and result.reason == 'send_error' and len(sock.sent) == 1


@pytest.mark.parametrize('module', contract.MODULES)
def test_expired_permit_is_unattempted_and_spent(monkeypatch, module):
    clock, sock, transport, permit = rig(monkeypatch, module)
    clock.value += transmit_permit.PERMIT_TTL_SECONDS
    marks = []
    result = transport.read(module, permit, before_send=lambda: marks.append(True))
    assert result.outcome == 'not_sent' and result.reason == 'permit_expired'
    assert not result.attempted and not marks and not sock.sent
    with pytest.raises(transmit_permit.TransmitPermitError, match='already consumed'):
        transport.read(module, permit, before_send=lambda: marks.append(True))
    contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_refused_permit_is_fatal_before_mark(monkeypatch, module):
    _clock, sock, transport, _permit = rig(monkeypatch, module)
    marks = []
    with pytest.raises(transmit_permit.TransmitPermitError):
        transport.read(module, object(), before_send=lambda: marks.append(True))
    assert not marks and not sock.sent


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('error', [OSError('marker'), ValueError('marker'), RuntimeError('marker')])
def test_marker_failure_surfaces_without_send(monkeypatch, module, error):
    _clock, sock, transport, permit = rig(monkeypatch, module)
    def broken_marker():
        raise error
    with pytest.raises(AttemptMarkerError) as caught:
        transport.read(module, permit, before_send=broken_marker)
    assert caught.value.__cause__ is error and not sock.sent


@pytest.mark.parametrize('module', contract.MODULES)
@pytest.mark.parametrize('error', [OSError('drain'), ValueError('drain')])
def test_drain_failure_does_not_mark_or_consume(monkeypatch, module, error):
    clock, sock, transport, permit = rig(monkeypatch, module)
    sock.queue.append((clock(), error))
    marks = []
    result = transport.read(module, permit, before_send=lambda: marks.append(True))
    assert result.reason == 'drain_failed' and not result.attempted
    assert not marks and not sock.sent
    transmit_permit.consume(permit, purpose=contract.PURPOSES[module], channel=CHANNEL)
    contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_bounded_drain_cannot_hold_owner(monkeypatch, module):
    clock, sock, transport, permit = rig(monkeypatch, module)
    sock.queue = [(clock(), can_frame(module, EMPTY))] * 33
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.reason == 'drain_failed' and len(sock.queue) == 1 and not sock.sent


@pytest.mark.parametrize('error,outcome', [(OSError('receive'), 'no_response'),
                                         (ValueError('receive'), 'malformed')])
def test_receive_errors_are_valid_outcomes(monkeypatch, error, outcome):
    _clock, sock, transport, permit = rig(monkeypatch, 'bcm_ccan', [(0.1, error)])
    result = transport.read('bcm_ccan', permit, before_send=lambda: None)
    assert result.outcome == outcome and sock.closed
    contract.validate_event(read_event(result))


def test_bcm_filter_accepts_normal_eff_reply_without_error_only_mask(monkeypatch):
    _clock, sock, transport, permit = rig(monkeypatch, 'bcm_ccan', [(0.1, EMPTY)])
    transport.read('bcm_ccan', permit, before_send=lambda: None)
    assert sock.options == [(101, 1, struct.pack('=II', 0x98DAF140, 0xDFFFFFFF))]
    wanted, mask = struct.unpack('=II', sock.options[0][2])
    assert not mask & 0x20000000  # ERR would select Linux's error-only list.
    assert 0x98DAF140 & mask == wanted
    assert 0x18DAF140 & mask != wanted
    assert 0xD8DAF140 & mask != wanted


@pytest.mark.parametrize('can_id,dlc', [
    (0x18DAF140, 8), (0xD8DAF140, 8), (0xB8DAF140, 8), (0x98DAF12A, 8),
    (0x98DAF140, 0), (0x98DAF140, 9),
])
def test_bcm_rechecks_filter_in_userspace(monkeypatch, can_id, dlc):
    _clock, sock, transport, permit = rig(monkeypatch, 'bcm_ccan')
    sock.script = [(0.1, can_frame('bcm_ccan', EMPTY, can_id=can_id, dlc=dlc))]
    result = transport.read('bcm_ccan', permit, before_send=lambda: None)
    assert result.outcome == 'malformed' and len(sock.sent) == 1 and sock.closed


@pytest.mark.parametrize('module', contract.MODULES)
def test_all_pci_bytes_can_never_trigger_flow_control(monkeypatch, module):
    # Every ISO-TP type/length nibble, including malicious First/Consecutive/FC.
    for first_byte in range(256):
        _clock, sock, transport, permit = rig(monkeypatch, module,
                                            [(0.01, bytes((first_byte,)) + b'\x59\x02\xff\0\0\0\0')])
        result = transport.read(module, permit, before_send=lambda: None)
        assert len(sock.sent) == 1
        contract.validate_event(read_event(result, module))


@pytest.mark.parametrize('module', contract.MODULES)
def test_pending_flood_is_stopped_by_absolute_deadline(monkeypatch, module):
    replies = [(i * 0.01, PENDING) for i in range(1, 1001)]
    clock, sock, transport, permit = rig(monkeypatch, module, replies)
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.outcome == 'no_response' and clock() == 1002.5
    assert result.pending_count == 250 and len(sock.sent) == 1
    assert max(sock.deadlines) == 1002.5
    contract.validate_event(read_event(result, module))


def test_bcm_open_failure_is_unsent_and_closes_partial_socket(monkeypatch):
    _clock, sock, transport, permit = rig(monkeypatch, 'bcm_ccan')
    def fail_bind(address):
        raise OSError('bind failed')
    sock.bind = fail_bind
    result = transport.read('bcm_ccan', permit, before_send=lambda: pytest.fail('marked'))
    assert result.reason == 'drain_failed' and not result.attempted and sock.closed
    assert not sock.sent
    contract.validate_event(read_event(result, 'bcm_ccan'))


def test_bcm_factory_failure_is_unsent(monkeypatch):
    _clock, _sock, transport, permit = rig(monkeypatch, 'bcm_ccan')
    def fail_socket(*args):
        raise OSError('socket unavailable')
    transport.socket_factory = fail_socket
    result = transport.read('bcm_ccan', permit, before_send=lambda: pytest.fail('marked'))
    assert result.reason == 'drain_failed' and not result.attempted


@pytest.mark.parametrize('module', contract.MODULES)
def test_short_packet_is_malformed_without_raw_data_claim(monkeypatch, module):
    _clock, sock, transport, permit = rig(monkeypatch, module)
    sock.script = [(0.1, b'\0' * 15)]
    result = transport.read(module, permit, before_send=lambda: None)
    assert result.outcome == 'malformed' and result.frame_hex is None and len(sock.sent) == 1
    contract.validate_event(read_event(result, module))
