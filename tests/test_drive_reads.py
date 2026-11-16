"""Bounded file projection and additive dashboard payload; no live transports."""
import copy
import json
from unittest import mock

import pytest

from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data.drive_reads import drive_reads_response, validate_results
from projects.vehicle_data.insights import TelemetryInsights
from projects.vehicle_data.web_v2 import dtc_lite
from tests.test_vehicle_insights import FakeHistorian, compact_dtc_cache


def event(frame='07 59 02 FF 54 18 78 08', outcome='complete', count=1):
    return dict(type='dtc_read', session='a' * 32, **{'pass': 0}, module='radar_acc',
                request=contract.REQUEST, outcome=outcome,
                reason={'complete': 'single_frame', 'truncated': 'first_frame',
                        'no_response': 'timeout'}[outcome], nrc=None, pending_count=0,
                frame_hex=frame, count=count, started_at='2026-11-15T17:00:00Z',
                completed_at='2026-11-15T17:00:01Z', rpm=1600.0, speed_mph=60.0,
                interface_mode=contract.INTERFACE_MODE)


def results(read=None):
    read = read or event()
    return dict(schema=1, coverage=contract.COVERAGE,
                passes=[dict(session=read['session'], **{'pass': read['pass']},
                             reads=[{**read, 'parsed': {'outcome': 'unparsed'}}])])


def test_complete_projection_reparses_evidence_and_adds_descriptions(tmp_path):
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(json.dumps(results()))
    payload = drive_reads_response(path)
    assert payload['available'] and payload['coverage'] == contract.COVERAGE
    module, = payload['modules']
    assert module['module_key'] == 'radar_acc'
    read, = module['reads']
    assert read['outcome'] == 'complete' and read['count'] == 1
    record, = read['dtcs']
    assert record['fca_display'] == 'C1418-78' and record['status'] == '08'
    assert 'description_reviewed' in record
    assert 'rpm' not in read and 'speed_mph' not in read and 'frame_hex' not in read


def test_truncated_projection_never_invents_status(tmp_path):
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(json.dumps(results(event('10 1B 59 02 FF 96 36 11', 'truncated', 6))))
    read = drive_reads_response(path)['modules'][0]['reads'][0]
    assert read['count'] == 6 and read['dtcs'] == []
    assert read['first_code'] == {'raw_dtc': '963611', 'fca_display': 'B1636-11'}
    assert read['status_availability_mask'] == 'FF'


def test_failures_and_inconsistent_metadata_cannot_change_codes(tmp_path):
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(json.dumps(results(event(None, 'no_response', None))))
    read = drive_reads_response(path)['modules'][0]['reads'][0]
    assert read['outcome'] == 'no_response' and not read['dtcs']
    for frame in ['00', '03 59 02 FF 00 00 00 00']:
        path.write_text(json.dumps(results(event(frame))))
        read = drive_reads_response(path)['modules'][0]['reads'][0]
        assert read['outcome'] == 'unparsed' and not read['dtcs']


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(schema=True),
    lambda p: p.update(coverage='all codes'),
    lambda p: p.update(passes=p['passes'] * 201),
    lambda p: p['passes'].append(copy.deepcopy(p['passes'][0])),
    lambda p: p['passes'][0].update(session='bad'),
    lambda p: p['passes'][0]['reads'].append(copy.deepcopy(p['passes'][0]['reads'][0])),
    lambda p: p['passes'][0]['reads'][0].update(module='pcm'),
    lambda p: p['passes'][0]['reads'][0].update(session='b' * 32),
    lambda p: p['passes'][0]['reads'][0].update(completed_at='not a date'),
    lambda p: p['passes'][0]['reads'][0].pop('parsed'),
    lambda p: p['passes'][0].update(reads=[None]),
])
def test_invalid_file_is_unavailable_not_clean(tmp_path, mutation):
    payload = results()
    mutation(payload)
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(json.dumps(payload))
    assert drive_reads_response(path)['available'] is False


def test_missing_malformed_and_oversized_files_are_bounded(tmp_path):
    path = tmp_path / 'drive-dtc-results.json'
    assert drive_reads_response(path)['reason'] == 'not_read'
    for raw in [b'not JSON', b'\xff', b' ' * (contract.LIMITS['results_bytes'] + 1)]:
        path.write_bytes(raw)
        assert drive_reads_response(path)['available'] is False


def test_insights_missing_file_byte_identical_and_block_passes_through_summary(tmp_path):
    cache = tmp_path / 'dtc-cache.json'
    cache.write_text(json.dumps(compact_dtc_cache()))
    insights = TelemetryInsights(FakeHistorian(), warning_evaluator=mock.Mock(), dtc_cache_path=cache,
                                history_metrics=('battery.voltage',))
    # The existing /v1 parked and van fields are unchanged when drive results are absent.
    before = insights.dtc_response()
    assert 'drive_reads' not in before
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(json.dumps(results()))
    response = insights.dtc_response()
    drive = response.pop('drive_reads')
    assert json.dumps(response, sort_keys=True) == json.dumps(before, sort_keys=True)
    assert dtc_lite({**response, 'drive_reads': drive})['drive_reads'] == drive
    assert 'drive_reads' not in dtc_lite(before)
    path.write_text('broken')
    assert insights.dtc_response()['drive_reads']['available'] is False


def test_file_retention_bound_accepts_two_hundred_unique_passes():
    payload = results()
    row = payload['passes'][0]
    payload['passes'] = []
    for index in range(contract.LIMITS['passes']):
        entry = copy.deepcopy(row)
        entry['pass'] = index
        entry['reads'][0]['pass'] = index
        payload['passes'].append(entry)
    assert len(validate_results(payload)) == 200


def huge_rpm_file():
    """The Astra review's ~1.1 KB input: a valid read whose RPM is a 1,001-digit integer."""
    raw = json.dumps(results()).replace('"rpm": 1600.0', '"rpm": 1' + '0' * 1000)
    assert len(raw) < contract.LIMITS['results_bytes']
    return raw


def deep_file():
    """The Astra review's ~4 KB input: JSON nested about two thousand levels deep."""
    return '[' * 2000 + ']' * 2000


def parked_and_van_insights(tmp_path):
    from tests.test_vehicle_insights import van_scan_cache
    cache = tmp_path / 'dtc-cache.json'
    cache.write_text(json.dumps(compact_dtc_cache()))
    (tmp_path / 'van-scan-cache.json').write_text(json.dumps(van_scan_cache()))
    return TelemetryInsights(FakeHistorian(), warning_evaluator=mock.Mock(), dtc_cache_path=cache,
                             history_metrics=('battery.voltage',))


@pytest.mark.parametrize('raw', [huge_rpm_file(), deep_file()], ids=['huge-number', 'deep-nesting'])
def test_any_drive_file_failure_stays_inside_the_drive_block(tmp_path, raw):
    insights = parked_and_van_insights(tmp_path)
    before = insights.dtc_response()
    path = tmp_path / 'drive-dtc-results.json'
    path.write_text(raw)
    assert drive_reads_response(path)['reason'] == 'drive_reads_unavailable'
    response = insights.dtc_response()
    drive = response.pop('drive_reads')
    assert drive['available'] is False and drive['reason'] == 'drive_reads_unavailable'
    assert response['available'] is True and response['in_vehicle_scan']['available'] is True
    assert json.dumps(response, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_an_oversized_response_drops_the_drive_block_before_the_van_block(tmp_path):
    from projects.vehicle_data import insights as insights_module
    insights = parked_and_van_insights(tmp_path)
    without_drive = len(json.dumps(insights.dtc_response(), sort_keys=True, separators=(',', ':')))
    (tmp_path / 'drive-dtc-results.json').write_text(json.dumps(results()))
    with mock.patch.object(insights_module, 'MAX_RESPONSE_BYTES', without_drive + 200):
        response = insights.dtc_response()
    assert response['drive_reads'] == {'available': False, 'reason': 'drive_reads_too_large'}
    assert response['in_vehicle_scan']['available'] is True
