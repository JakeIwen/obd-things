"""Standalone event acceptance, raw evidence parsing and elapsed-time windows."""
import copy
import math

import pytest

from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data.drive_dtc_transport import ReadResult
from tests.drive_dtc_support import EMPTY, ONE, FIRST, SESSION, read_event


def test_complete_parser_reuses_status_semantics():
    parsed = contract.parse_frame(ONE)
    record, = parsed['dtcs']
    assert record['raw_dtc'] == '541878' and record['fca_display'] == 'C1418-78'
    assert record['status'] == '08' and record['confirmed'] and not record['current']
    assert parsed['count'] == 1 and parsed['status_availability_mask'] == 'FF'
    assert contract.parse_frame(EMPTY)['dtcs'] == []
    assert contract.parse_frame(ONE.hex(' ').upper()) == parsed


def test_truncated_parser_never_invents_status_or_retrieves_missing_list():
    parsed = contract.parse_frame(FIRST)
    assert parsed == dict(outcome='truncated', count=6, nrc=None, dtcs=[],
                          status_availability_mask='FF',
                          first_code={'raw_dtc': '963611', 'fca_display': 'B1636-11'})


@pytest.mark.parametrize('frame', [
    b'', b'\x00', b'\x01\x59', b'\x03\x59\x02', b'\x08' + bytes(8),
    b'\x07\x59\x03' + bytes(5), b'\x03\x7f\x22\x31',
    b'\x10\x07\x59\x02\xff\0\0\0', b'\x10\x0c\x59\x02\xff\0\0\0',
    b'\x10\x0b\x62\x02\xff\0\0\0', b'\x10\x0b\x59\x02\xff\0\0',
    b'\x20' + bytes(7), b'\x30' + bytes(7), '03 59 02 ff', '035902FF', None,
])
def test_parser_rejects_malformed_or_unbounded_frame(frame):
    with pytest.raises(ValueError):
        contract.parse_frame(frame)


@pytest.mark.parametrize('nrc', range(256))
def test_every_nrc_is_parseable_and_final_negatives_validate(nrc):
    frame = bytes((3, 0x7F, 0x19, nrc, 0, 0, 0, 0))
    parsed = contract.parse_frame(frame)
    assert parsed['outcome'] == ('pending' if nrc == 0x78 else 'negative')
    if nrc != 0x78:
        result = ReadResult('negative', contract.negative_reason(nrc), True,
                            nrc=f'{nrc:02X}', frame_hex=frame.hex(' ').upper())
        contract.validate_event(read_event(result))


@pytest.mark.parametrize('outcome', list(contract.OUTCOMES - {'negative'}))
def test_every_declared_outcome_reason_is_standalone_valid(outcome):
    for reason in contract.VOCABULARY['outcomes'][outcome]:
        frame = {'complete': ONE, 'truncated': FIRST}.get(outcome)
        result = ReadResult(outcome, reason, outcome != 'not_sent',
                            frame_hex=frame.hex(' ').upper() if frame else None,
                            count={'complete': 1, 'truncated': 6}.get(outcome))
        contract.validate_event(read_event(result))


def test_all_step_and_pass_states_are_standalone_valid():
    for event_type, states in contract.VOCABULARY['events'].items():
        for state, reasons in states.items():
            for reason in reasons:
                event = dict(type=event_type, state=state, reason=reason, detail='',
                             session=SESSION, interface_mode=contract.INTERFACE_MODE)
                if event_type == contract.PASS_EVENT:
                    event['pass'] = 99
                    if state == 'started':
                        event['started_monotonic'] = 3600.0
                contract.validate_event(event)


def valid_read():
    return read_event(ReadResult('complete', 'single_frame', True,
                                frame_hex=EMPTY.hex(' ').upper(), count=0))


@pytest.mark.parametrize('field,bad', [
    ('type', 'unknown'), ('session', 'b' * 31), ('session', []), ('pass', True),
    ('pass', -1), ('module', []), ('module', 'pcm'), ('request', '19 02 FF'),
    ('outcome', []), ('outcome', 'pending'), ('reason', 'anything'),
    ('nrc', '31'), ('pending_count', True), ('pending_count', -1),
    ('count', True), ('count', 2), ('frame_hex', None), ('frame_hex', 'ff'),
    ('frame_hex', '00 ' * 9), ('started_at', '2026-11-15T20:00:00'),
    ('started_at', '2026-11-15T20:00:00-07:00'), ('completed_at', '2026-11-14T20:00:00Z'),
    ('rpm', math.nan), ('speed_mph', False), ('interface_mode', 'listen_only'),
])
def test_validator_rejects_invalid_contract_fields(field, bad):
    event = valid_read()
    event[field] = bad
    with pytest.raises(ValueError):
        contract.validate_event(event)


def test_raw_reply_is_not_a_protocol_validation_dependency():
    event = valid_read()
    event['frame_hex'] = 'FF'
    # Broker separately catches this parser failure and stores unparsed.
    contract.validate_event(event)
    with pytest.raises(ValueError):
        contract.parse_frame(event['frame_hex'])


def test_validator_is_pure_exact_key_and_accepts_absent_evidence():
    event = valid_read()
    event['rpm'] = event['speed_mph'] = None
    before = copy.deepcopy(event)
    contract.validate_event(event)
    assert before == event
    event['extra'] = True
    with pytest.raises(ValueError):
        contract.validate_event(event)


@pytest.mark.parametrize('now,previous,allowed', [
    (1299, None, False), (1300, None, True), (1900, None, True), (1900.01, None, False),
    (4900, 1900, False), (5499, 1900, False), (5500, 1900, True),
])
def test_window_and_actual_pass_hour_are_both_required(now, previous, allowed):
    index = 0 if now < 4900 else 1
    assert contract.pass_start_allowed(origin=1000, first_due=300, pass_index=index,
                                      now=now, previous_started=previous) is allowed


def test_missed_window_does_not_slide_and_reboot_full_hour_is_representable():
    assert not contract.pass_start_allowed(origin=0, first_due=300, pass_index=0, now=901)
    assert contract.window_start(0, 3600, 0) == 3600
    assert not contract.pass_start_allowed(origin=0, first_due=3600, pass_index=0, now=3599)


@pytest.mark.parametrize('origin,due,index', [(-1, 300, 0), (0, 299, 0), (0, 3601, 0),
                                            (0, 300, True), (math.nan, 300, 0)])
def test_invalid_schedule_is_refused(origin, due, index):
    with pytest.raises(ValueError):
        contract.window_start(origin, due, index)


@pytest.mark.parametrize('stamp', [None, True, False, math.nan, math.inf, -1, '1000'])
def test_pass_start_requires_real_monotonic_stamp(stamp):
    event = dict(type=contract.PASS_EVENT, session=SESSION, state='started',
                 reason='first_attempt', detail='', interface_mode=contract.INTERFACE_MODE,
                 started_monotonic=stamp, **{'pass': 0})
    with pytest.raises(ValueError):
        contract.validate_event(event)


def test_pass_timestamp_required_on_start_forbidden_on_finish():
    event = dict(type=contract.PASS_EVENT, session=SESSION, state='started',
                 reason='first_attempt', detail='', interface_mode=contract.INTERFACE_MODE,
                 **{'pass': 0})
    with pytest.raises(ValueError):
        contract.validate_event(event)
    event['started_monotonic'] = 1300.
    contract.validate_event(event)
    event.update(state='finished', reason='all_modules_done')
    with pytest.raises(ValueError):
        contract.validate_event(event)
    del event['started_monotonic']
    contract.validate_event(event)
