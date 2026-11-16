"""Read-only, bounded dashboard projection of the broker's drive-DTC results."""
from __future__ import annotations

import json
from pathlib import Path

from lib.timeutil import nonnegative_int
from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data.dtc_descriptions import describe_dtc

RESULTS_FILENAME = contract.RESULTS_FILENAME


def validate_results(payload: object) -> list[dict]:
    """Validate stored events and reparse raw evidence, never trusting saved semantics."""
    if (not isinstance(payload, dict) or set(payload) != {'schema', 'coverage', 'passes'}
            or type(payload['schema']) is not int or payload['schema'] != contract.SCHEMA
            or payload['coverage'] != contract.COVERAGE
            or not isinstance(payload['passes'], list)
            or len(payload['passes']) > contract.LIMITS['passes']):
        raise ValueError('invalid drive-DTC results header')
    seen = set()
    reads = []
    for row in payload['passes']:
        if (not isinstance(row, dict) or set(row) != {'session', 'pass', 'reads'}
                or not contract.session_id_valid(row['session'])
                or not nonnegative_int(row['pass']) or not isinstance(row['reads'], list)
                or not 1 <= len(row['reads']) <= len(contract.MODULES)):
            raise ValueError('invalid stored drive-DTC pass')
        identity = (row['session'], row['pass'])
        if identity in seen:
            raise ValueError('duplicate stored drive-DTC pass')
        seen.add(identity)
        modules = set()
        for read in row['reads']:
            if not isinstance(read, dict) or 'parsed' not in read:
                raise ValueError('invalid stored drive-DTC read')
            event = {key: value for key, value in read.items() if key != 'parsed'}
            contract.validate_event(event)
            if (event['type'] != contract.READ_EVENT or event['session'] != row['session']
                    or event['pass'] != row['pass'] or event['module'] in modules):
                raise ValueError('drive-DTC read does not match its pass')
            modules.add(event['module'])
            reads.append(_project_read(event))
    return reads


def _project_read(event: dict) -> dict:
    try:
        parsed = contract.parse_frame(event['frame_hex'])
    except (TypeError, ValueError):
        parsed = {'outcome': 'unparsed'}
    # A valid raw frame with inconsistent event metadata cannot change codes.
    outcome = event['outcome']
    if (parsed['outcome'] != outcome or parsed.get('count') != event['count']
            or parsed.get('nrc') != event['nrc']):
        parsed = {'outcome': 'unparsed'}
    records = [{**record, **describe_dtc(event['module'], record['fca_display'])}
               for record in parsed.get('dtcs', [])]
    return dict(module_key=event['module'], session=event['session'],
                pass_index=event['pass'], outcome=parsed['outcome'] if outcome in
                (contract.COMPLETE, contract.TRUNCATED) else outcome,
                reason=event['reason'], observed_at=event['completed_at'],
                count=parsed.get('count'), status_availability_mask=parsed.get('status_availability_mask'),
                dtcs=records, first_code=parsed.get('first_code'))


def drive_reads_response(path: Path) -> dict:
    """Read at most 256 KiB; missing/corrupt evidence is unavailable, never clean."""
    try:
        with path.open('rb') as stream:
            raw = stream.read(contract.LIMITS['results_bytes'] + 1)
        if len(raw) > contract.LIMITS['results_bytes']:
            raise ValueError('drive-DTC results exceed 256 KiB')
        reads = validate_results(json.loads(raw))
    except FileNotFoundError:
        return {'available': False, 'reason': 'not_read', 'detail': 'No saved drive reads yet.'}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'available': False, 'reason': 'drive_reads_unavailable',
                'detail': f'Saved drive reads are invalid or unreadable: {exc}'}
    modules = []
    for key in contract.MODULES:
        rows = [read for read in reads if read['module_key'] == key]
        if rows:
            module = contract.ENDPOINTS[key]
            modules.append(dict(module_key=key, module_name=module.name,
                                logical_bus=module.bus, reads=rows))
    return {'available': True, 'coverage': contract.COVERAGE, 'modules': modules}
