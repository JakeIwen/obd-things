"""Public control/results entry points with scripted supervisors and temp files."""
from dataclasses import asdict
import json
import threading
import time

import pytest

from projects.vehicle_data import drive_dtc_contract as contract
from projects.vehicle_data import drive_dtc_control as control_module
from projects.vehicle_data.drive_dtc_budget import Reservation, json_size
from projects.vehicle_data.drive_dtc_control import DriveDtcControl, DriveDtcResults
from tests.test_drive_dtc_budget import BOOT, SESSION

FINAL = {'type': 'final', 'state': 'idle', 'reason': 'engine_not_running',
         'detail': 'stopped', 'interface_mode': 'listen_only', 'restored': True}


def read_event(*, session=SESSION, index=0, module='radar_acc', frame='03 59 02 FF 00 00 00 00'):
    return dict(type='dtc_read', session=session, **{'pass': index}, module=module,
                request=contract.REQUEST, outcome='complete', reason='single_frame', nrc=None,
                pending_count=0, frame_hex=frame, count=0,
                started_at='2026-11-15T00:00:00+00:00', completed_at='2026-11-15T00:00:01+00:00',
                rpm=1650.0, speed_mph=61.2, interface_mode='armed_diagnostic')


def pass_event(started=400.0, index=0, session=SESSION):
    return dict(type='dtc_pass', session=session, **{'pass': index}, state='started',
                started_monotonic=started, reason='first_attempt', detail='',
                interface_mode='armed_diagnostic')


class Budget:
    def __init__(self):
        self.reservation = Reservation(SESSION, BOOT, 100.0, 300.0)
        self.calls = 0
        self.ends = []

    def reserve(self):
        self.calls += 1
        return self.reservation

    def end(self, session, ended):
        self.ends.append((session, ended))
        return True

    def close(self, timeout=0):
        pass


class Supervisor:
    def __init__(self, body):
        self.body = body

    def run(self, stop):
        return self.body(stop)


class Publications:
    """Observe actual writer callbacks without sleeping or racing its queue."""
    def __init__(self):
        self.condition = threading.Condition()
        self.count = 0
        self.last = None
        self.max_bytes = 0

    def __call__(self, path, payload):
        with self.condition:
            self.last = json.loads(json.dumps(payload))
            self.max_bytes = max(self.max_bytes, json_size(payload))
            self.count += 1
            self.condition.notify_all()

    def wait(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: self.count >= count, timeout=5)


def test_flag_off_is_inert_and_adds_no_capability(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('flag-off created a writer')
    monkeypatch.setattr(control_module, 'DriveDtcBudget', forbidden)
    monkeypatch.setattr(control_module, 'DriveDtcResults', forbidden)
    control = DriveDtcControl()
    assert not control.available and not control.drive_dtc_storage_failed
    assert control.launch_arguments() == []
    assert control.accept_ready({'type': 'ready'})
    assert control.run(Supervisor(lambda stop: FINAL), threading.Event()) == FINAL
    control.close()


def test_completed_reservation_arguments_and_final_update_without_events(tmp_path):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget, monotonic=lambda: 1000)
    try:
        def run(stop):
            assert control.launch_arguments() == budget.reservation.launch_arguments()
            assert control.accept_ready({'capabilities': [contract.CAPABILITY]})
            return FINAL
        assert control.run(Supervisor(run), threading.Event()) == FINAL
        assert budget.ends == [(SESSION, 1000)]
        assert control.launch_arguments() == []
    finally:
        control.close(2)


def test_supervisor_exception_still_accounts_session_end(tmp_path):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget, monotonic=lambda: 1000)
    try:
        def run(stop):
            raise RuntimeError('unexpected supervisor failure')
        with pytest.raises(RuntimeError):
            control.run(Supervisor(run), threading.Event())
        assert budget.ends == [(SESSION, 1000)]
    finally:
        control.close(2)


def test_failed_reservation_launches_without_capability(tmp_path):
    budget = Budget()
    budget.reservation = None
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget)
    try:
        def run(stop):
            assert control.launch_arguments() == []
            return FINAL
        assert control.run(Supervisor(run), threading.Event()) == FINAL
        assert budget.ends == []
    finally:
        control.close(2)


def test_delayed_last_instant_start_is_valid_and_reads_store_independently(tmp_path):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget, monotonic=lambda: 2000)
    try:
        def run(stop):
            assert control.handle_event(pass_event(1000))  # window closed at 1000, receipt at 2000
            assert not stop.is_set()
            assert control.handle_event(read_event())
            assert control.handle_event(read_event(module='bcm_ccan', frame='03 00 00 00'))
            return FINAL
        control.run(Supervisor(run), threading.Event())
    finally:
        control.close(2)
    stored = json.loads((tmp_path / control_module.RESULTS_FILENAME).read_text())
    assert stored['coverage'] == contract.COVERAGE
    reads = stored['passes'][0]['reads']
    assert reads[0]['parsed']['outcome'] == 'complete'
    assert reads[1]['parsed'] == {'outcome': 'unparsed'}
    assert budget.ends == [(SESSION, 2000)]


@pytest.mark.parametrize('events,receipt', [
    ([pass_event(399)], 1000),
    ([pass_event(1001)], 2000),
    ([pass_event(700)], 699),
    ([pass_event(1000), pass_event(4000, 1)], 5000),
    ([pass_event(400), pass_event(400)], 5000),
    ([pass_event(400, session='a' * 32)], 5000),
])
def test_invalid_schedule_stops_normally_drops_session_and_disables_future(tmp_path, events, receipt):
    budget, published = Budget(), Publications()
    results = DriveDtcResults(tmp_path, write=published)
    control = DriveDtcControl(enabled=True, budget=budget, results=results, monotonic=lambda: receipt)
    try:
        def run(stop):
            control.handle_event(read_event())
            published.wait(1)
            for event in events:
                assert control.handle_event(event)
            assert stop.is_set()
            assert control.handle_event(read_event(index=99))
            return FINAL
        assert control.run(Supervisor(run), threading.Event()) == FINAL
        assert not control.available
        assert budget.ends == [(SESSION, receipt)]
        control.run(Supervisor(lambda stop: FINAL), threading.Event())
        assert budget.calls == 1
    finally:
        control.close(2)
    assert published.last['passes'] == []


def test_exact_hour_separation_and_two_sessions_are_distinct(tmp_path):
    budget, published = Budget(), Publications()
    results = DriveDtcResults(tmp_path, write=published)
    control = DriveDtcControl(enabled=True, budget=budget, results=results, monotonic=lambda: 5000)
    try:
        def run(stop):
            for start, index in [(1000, 0), (4600, 1)]:
                control.handle_event(pass_event(start, index))
                control.handle_event(read_event(index=index))
            assert not stop.is_set()
            return FINAL
        control.run(Supervisor(run), threading.Event())
        published.wait(2)
        budget.reservation = Reservation('a' * 32, BOOT, 5000, 3600)
        def second(stop):
            # Reads are standalone evidence even if their pass event was lost.
            control.handle_event(read_event(session='a' * 32))
            return FINAL
        control.run(Supervisor(second), threading.Event())
    finally:
        control.close(2)
    assert [(p['session'], p['pass']) for p in published.last['passes']] == [
        (SESSION, 0), (SESSION, 1), ('a' * 32, 0)]


def test_malformed_event_never_raises_or_claims_restoration_failure(tmp_path):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget)
    try:
        def run(stop):
            assert not control.handle_event({'type': 'observation'})
            assert control.handle_event({'type': 'dtc_read', 'module': 'not-a-module'})
            assert stop.is_set()
            return FINAL
        assert control.run(Supervisor(run), threading.Event())['restored'] is True
    finally:
        control.close(2)


def test_result_writer_failure_latches_without_stopping_current_helper(tmp_path):
    entered = threading.Event()
    def broken(path, payload):
        entered.set()
        raise OSError('disk failure')
    budget = Budget()
    results = DriveDtcResults(tmp_path, write=broken)
    control = DriveDtcControl(enabled=True, budget=budget, results=results, monotonic=lambda: 1000)
    try:
        def run(stop):
            control.handle_event(read_event())
            assert entered.wait(2) and results.failed.wait(2)
            assert control.drive_dtc_storage_failed and not control.available
            assert not stop.is_set()
            control.handle_event(read_event(module='bcm_ccan'))
            assert not stop.is_set()
            return FINAL
        assert control.run(Supervisor(run), threading.Event()) == FINAL
        control.run(Supervisor(lambda stop: FINAL), threading.Event())
        assert budget.calls == 1 and budget.ends == [(SESSION, 1000)]
    finally:
        control.close(2)


def test_results_queue_full_drops_and_latches_without_blocking(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocked(path, payload):
        entered.set()
        assert release.wait(5)
    results = DriveDtcResults(tmp_path, write=blocked)
    try:
        assert results.enqueue(read_event())
        assert entered.wait(2)
        for index in range(contract.LIMITS['result_queue']):
            assert results.enqueue(read_event(index=index + 1))
        before = time.monotonic()
        assert not results.enqueue(read_event(index=999))
        assert time.monotonic() - before < 0.1
        assert results.failed.is_set()
    finally:
        release.set()
        results.close(2)


def test_storage_bound_one_read_per_module_and_newest_passes(tmp_path):
    published = Publications()
    results = DriveDtcResults(tmp_path, write=published)
    count = 0
    try:
        for index in range(205):
            for module in contract.MODULES:
                # The duplicate replaces, not appends, a module's sole read.
                for duplicate in range(2 if index == 0 else 1):
                    assert results.enqueue(read_event(index=index, module=module))
                    count += 1
                    published.wait(count)
    finally:
        results.close(2)
    payload = published.last
    assert len(payload['passes']) <= 200
    assert sum(len(p['reads']) for p in payload['passes']) <= 400
    assert payload['passes'][-1]['pass'] == 204
    assert all(len(p['reads']) == 2 for p in payload['passes'])
    assert published.max_bytes <= 256 * 1024
    # Actual publication uses the exact encoding bounded above, with one temp copy.
    from lib.dtc_batch import atomic_json
    atomic_json(tmp_path / control_module.RESULTS_FILENAME, payload)
    assert (tmp_path / control_module.RESULTS_FILENAME).stat().st_size <= 256 * 1024
    assert not list(tmp_path.glob('.*.tmp'))


def test_existing_saved_reads_are_revalidated_and_retained(tmp_path):
    first = DriveDtcResults(tmp_path)
    first.enqueue(read_event())
    first.close(2)
    second = DriveDtcResults(tmp_path)
    second.enqueue(read_event(index=1))
    second.close(2)
    rows = json.loads((tmp_path / control_module.RESULTS_FILENAME).read_text())['passes']
    assert [row['pass'] for row in rows] == [0, 1]


def test_corrupt_results_disable_new_reads_without_changing_budget(tmp_path):
    (tmp_path / control_module.RESULTS_FILENAME).write_text('{broken')
    results = DriveDtcResults(tmp_path)
    try:
        assert results.failed.wait(2)
        assert not results.enqueue(read_event())
    finally:
        results.close(2)


def test_results_remove_only_own_crash_orphans_before_new_temp(tmp_path):
    path = tmp_path / control_module.RESULTS_FILENAME
    foreign = tmp_path / '.another-writer.keep.tmp'
    foreign.write_text('keep')
    for index in range(3):
        orphan = tmp_path / f'.{path.name}.crash{index}.tmp'
        orphan.write_bytes(b'x' * (256 * 1024))
        def checked_write(destination, payload):
            from lib.dtc_batch import atomic_json
            assert not list(tmp_path.glob(f'.{path.name}.*.tmp'))
            atomic_json(destination, payload)
        results = DriveDtcResults(tmp_path, write=checked_write)
        results.enqueue(read_event(index=index))
        results.close(2)
        assert not results.failed.is_set()
        assert not orphan.exists() and path.stat().st_size <= 256 * 1024
    assert foreign.read_text() == 'keep'


def test_results_cleanup_failure_latches_without_writing(tmp_path, monkeypatch):
    from pathlib import Path
    orphan = tmp_path / f'.{control_module.RESULTS_FILENAME}.crash.tmp'
    orphan.write_text('orphan')
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == orphan:
            raise PermissionError('orphan cleanup denied')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    results = DriveDtcResults(tmp_path)
    try:
        assert results.failed.wait(2)
        assert not results.enqueue(read_event())
        assert not results.path.exists()
    finally:
        results.close(2)
