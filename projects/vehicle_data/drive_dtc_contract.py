"""Pure stage-1a DTC vocabulary, schedule, frame parser and event validation.

The JSON sidecar owns the shared constants. Parsing never requests missing
bytes: a FirstFrame reports only a count and its first code, without status.
No event needs an earlier event to be valid; scheduling is a separate check.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from types import MappingProxyType

from lib.dtc import DtcRecord, fca_dtc_name
from lib.modules import MODULES as MODULE_REGISTRY
from lib.timeutil import finite_number, nonnegative_int

VOCABULARY = json.loads(Path(__file__).with_name('drive_dtc.json').read_text())
SCHEMA = VOCABULARY['schema']
RESULTS_FILENAME = VOCABULARY['results_filename']
CAPABILITY = VOCABULARY['capability']
REQUEST = VOCABULARY['request']
REQUEST_DATA = bytes.fromhex(VOCABULARY['request_frame'])
REQUEST_MASK = bytes.fromhex(REQUEST)[-1]
COVERAGE = VOCABULARY['coverage']
PURPOSES = MappingProxyType(VOCABULARY['modules'])
MODULES = tuple(PURPOSES)
ENDPOINTS = MappingProxyType({key: MODULE_REGISTRY[key] for key in MODULES})
TIMING = MappingProxyType(VOCABULARY['timing'])
LIMITS = MappingProxyType(VOCABULARY['limits'])
MINIMUM_SESSION_SECONDS = TIMING['minimum_session_seconds']
INTERVAL_SECONDS = TIMING['interval_seconds']
WINDOW_SECONDS = TIMING['window_seconds']
MODULE_SPACING_SECONDS = TIMING['module_spacing_seconds']
RADAR_MARGIN_SECONDS = TIMING['radar_margin_seconds']
FIRST_REPLY_SECONDS = TIMING['first_reply_seconds']
PENDING_REPLY_SECONDS = TIMING['pending_reply_seconds']
FOREIGN_QUIET_SECONDS = TIMING['foreign_quiet_seconds']
MAX_DETAIL = LIMITS['detail_characters']
INTERFACE_MODE = VOCABULARY['interface_mode']
STEP_EVENT, PASS_EVENT, READ_EVENT = 'dtc_step', 'dtc_pass', 'dtc_read'
EVENT_TYPES = frozenset((*VOCABULARY['events'], READ_EVENT))
COMPLETE, TRUNCATED, NEGATIVE, NO_RESPONSE, MALFORMED, NOT_SENT = VOCABULARY['outcomes']
OUTCOMES = frozenset(VOCABULARY['outcomes'])
SESSION_ID = re.compile(r'[0-9a-f]{32}')
HEX_BYTE = re.compile(r'[0-9A-F]{2}')
FRAME_HEX = re.compile(r'[0-9A-F]{2}(?: [0-9A-F]{2}){0,7}')


def session_id_valid(value: object) -> bool:
    return isinstance(value, str) and SESSION_ID.fullmatch(value) is not None


def window_start(origin: float, first_due: float, pass_index: int) -> float:
    """Return the start of reservation window k; never accept an unsafe delay."""
    if (not finite_number(origin) or origin < 0 or not finite_number(first_due)
            or not MINIMUM_SESSION_SECONDS <= first_due <= INTERVAL_SECONDS
            or not nonnegative_int(pass_index)):
        raise ValueError('invalid drive-DTC reservation schedule')
    start = origin + first_due + pass_index * INTERVAL_SECONDS
    if not finite_number(start):
        raise ValueError('drive-DTC window is not finite')
    return start


def pass_start_allowed(*, origin: float, first_due: float, pass_index: int,
                       now: float, previous_started: float | None = None) -> bool:
    """A pass starts only inside its window AND an actual-pass monotonic hour.

    ``pass_index`` is the window index, not a per-session attempt counter.
    Missed windows are skipped. The broker separately ties the reservation id
    to its launch; neither wall time nor another session can shorten this rule.
    """
    start = window_start(origin, first_due, pass_index)
    if not finite_number(now) or not start <= now <= start + WINDOW_SECONDS:
        return False
    return previous_started is None or (
        finite_number(previous_started) and previous_started >= origin
        and now - previous_started >= INTERVAL_SECONDS)


def negative_reason(nrc: int) -> str:
    if nrc in VOCABULARY['session_required_nrcs']:
        return 'session_required'
    return VOCABULARY['negative_reason_format'].format(nrc=f'{nrc:02X}')


def _frame_bytes(frame: bytes | str) -> bytes:
    if isinstance(frame, str):
        if not FRAME_HEX.fullmatch(frame):
            raise ValueError('DTC frame must be one to eight uppercase hex bytes')
        return bytes.fromhex(frame)
    if not isinstance(frame, bytes) or not 1 <= len(frame) <= 8:
        raise ValueError('DTC frame must contain one to eight bytes')
    return frame


def parse_frame(frame: bytes | str) -> dict:
    """Parse CAN data (not the CAN header) for transport or broker storage.

    Returns ``outcome``, ``count``, ``nrc``, ``status_availability_mask`` and
    ``dtcs``; truncated results also have ``first_code`` (no invented status).
    ``pending`` is an intermediate parse result, never a helper event outcome.
    Raises ValueError for malformed input; the broker stores that as unparsed,
    not a protocol error. Availability is a hex byte, as in lib.dtc's caches.
    """
    data = _frame_bytes(frame)
    if data[0] >> 4 == 0:
        size = data[0]
        if size not in (3, 7) or len(data) < size + 1:
            raise ValueError('invalid DTC SingleFrame length')
        payload = data[1:1 + size]
        if payload[:2] == b'\x7f\x19' and size == 3:
            nrc = payload[2]
            return dict(outcome='pending' if nrc == 0x78 else NEGATIVE,
                        count=None, nrc=f'{nrc:02X}', dtcs=[], status_availability_mask=None)
        if payload[:2] != b'\x59\x02':
            raise ValueError('DTC SingleFrame does not match 19 02')
        records = [] if size == 3 else [DtcRecord(payload[3:6].hex(), payload[6]).as_dict()]
        return dict(outcome=COMPLETE, count=len(records), nrc=None, dtcs=records,
                    status_availability_mask=f'{payload[2]:02X}')
    if data[0] >> 4 != 1 or len(data) != 8:
        raise ValueError('expected DTC SingleFrame or full FirstFrame; no flow control')
    size = ((data[0] & 0x0F) << 8) | data[1]
    if size <= 7 or (size - 3) % 4 or data[2:4] != b'\x59\x02':
        raise ValueError('invalid DTC FirstFrame length or service')
    code = data[5:8]
    return dict(outcome=TRUNCATED, count=(size - 3) // 4, nrc=None, dtcs=[],
                status_availability_mask=f'{data[4]:02X}',
                first_code={'raw_dtc': code.hex().upper(), 'fca_display': fca_dtc_name(code)})


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('DTC timestamp must be bounded UTC ISO text')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError('DTC timestamp must carry UTC timezone')
    return parsed


def _read_problem(event: dict) -> None:
    if not isinstance(event['module'], str) or event['module'] not in MODULES or event['request'] != REQUEST:
        raise ValueError('DTC module or request not allowlisted')
    outcome, reason = event['outcome'], event['reason']
    if not isinstance(outcome, str) or outcome not in OUTCOMES or not isinstance(reason, str):
        raise ValueError('DTC outcome/reason invalid')
    nrc = event['nrc']
    if outcome == NEGATIVE:
        if (not isinstance(nrc, str) or not HEX_BYTE.fullmatch(nrc) or nrc == '78'
                or reason != negative_reason(int(nrc, 16))):
            raise ValueError('DTC negative response metadata invalid')
    elif nrc is not None or reason not in VOCABULARY['outcomes'][outcome]:
        raise ValueError('DTC outcome reason invalid')
    pending = event['pending_count']
    if not nonnegative_int(pending):
        raise ValueError('DTC pending count invalid')
    count = event['count']
    if outcome in (COMPLETE, TRUNCATED):
        low, high = (0, 1) if outcome == COMPLETE else (2, (4095 - 3) // 4)
        if not nonnegative_int(count) or not low <= count <= high:
            raise ValueError('DTC count invalid')
    elif count is not None:
        raise ValueError('DTC unsuccessful read must not claim a count')
    frame = event['frame_hex']
    if frame is not None and (not isinstance(frame, str) or not FRAME_HEX.fullmatch(frame)):
        raise ValueError('DTC frame hex invalid')
    if outcome in (COMPLETE, TRUNCATED, NEGATIVE) and frame is None:
        raise ValueError('DTC answered outcome requires its raw frame')
    if outcome == NOT_SENT and (frame is not None or pending):
        raise ValueError('DTC unsent request cannot have received a reply')
    # Frame contents are deliberately NOT parsed here: raw malformed evidence
    # must remain storable without turning an expected outcome into a failure.
    if _timestamp(event['completed_at']) < _timestamp(event['started_at']):
        raise ValueError('DTC completion precedes start')
    for key in ('rpm', 'speed_mph'):
        value = event[key]
        if value is not None and (not finite_number(value) or value < 0):
            raise ValueError(f'DTC {key} invalid')


def validate_event(event: object) -> None:
    """Validate one standalone helper event; raises ValueError, never mutates it."""
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        raise ValueError('DTC event must be an object with a type')
    kind = event['type']
    common = {'type', 'session', 'interface_mode'}
    if kind == READ_EVENT:
        keys = common | {'pass', 'module', 'request', 'outcome', 'reason', 'nrc',
                         'pending_count', 'frame_hex', 'count', 'started_at',
                         'completed_at', 'rpm', 'speed_mph'}
    elif kind in (STEP_EVENT, PASS_EVENT):
        keys = common | {'state', 'reason', 'detail'} | ({'pass'} if kind == PASS_EVENT else set())
    else:
        raise ValueError('unknown DTC event type')
    if kind == PASS_EVENT and event.get('state') == 'started':
        keys.add('started_monotonic')
    if set(event) != keys or not session_id_valid(event.get('session')):
        raise ValueError('DTC event fields or reservation identity invalid')
    if event['interface_mode'] != INTERFACE_MODE:
        raise ValueError('DTC event interface mode invalid')
    if kind != STEP_EVENT and not nonnegative_int(event['pass']):
        raise ValueError('DTC pass must be a nonnegative window index')
    if kind == PASS_EVENT and event.get('state') == 'started':
        stamp = event['started_monotonic']
        if not finite_number(stamp) or stamp < 0:
            raise ValueError('DTC pass requires a finite monotonic start')
    if kind == READ_EVENT:
        _read_problem(event)
    else:
        states = VOCABULARY['events'][kind]
        state, reason, detail = event['state'], event['reason'], event['detail']
        if (not isinstance(state, str) or state not in states
                or reason not in states[state] or not isinstance(detail, str)
                or len(detail) > MAX_DETAIL):
            raise ValueError('DTC state, reason or detail invalid')
