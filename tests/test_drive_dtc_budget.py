"""Real temporary-file persistence and bounded, scripted writer failures."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import threading
import time

import pytest

from projects.vehicle_data import drive_dtc_budget as budget

BOOT = '00000000-0000-0000-0000-000000000001'
OTHER_BOOT = '00000000-0000-0000-0000-000000000002'
SESSION = '1' * 32
WALL = datetime(2026, 11, 15, tzinfo=timezone.utc)


def previous(**changes):
    payload = asdict(budget.Reservation(SESSION, BOOT, 100.0, 300.0,
                                       session_end_monotonic=1000.0,
                                       last_pass_bound_monotonic=1000.0))
    return budget.Reservation(**(payload | changes))


def writer(tmp_path, **kwargs):
    return budget.DriveDtcBudget(tmp_path, boot_id=lambda: BOOT,
                                wall_now=lambda: WALL, **kwargs)


@pytest.mark.parametrize('record', [None, previous(boot_id=OTHER_BOOT),
                                  previous(session_end_monotonic=None),
                                  previous(last_pass_bound_monotonic=None)])
def test_uncertain_history_waits_full_hour(record):
    assert budget.first_due(record, boot_id=BOOT, now=2000.0,
                            harvest_completed=None, wall_now=WALL) == 3600


@pytest.mark.parametrize('now,expected', [(1000, 3600), (1500, 3100),
                                          (4400, 300), (10000, 300)])
def test_same_boot_previous_bound_controls_launch(now, expected):
    assert budget.first_due(previous(), boot_id=BOOT, now=now,
                            harvest_completed=None, wall_now=WALL) == expected


@pytest.mark.parametrize('age,expected', [(0, 3600), (60, 3540), (-60, 3600),
                                         (3600, 300), (7200, 300)])
def test_launch_only_harvest_cooldown(age, expected):
    record = previous(last_pass_bound_monotonic=0.0)
    assert budget.first_due(record, boot_id=BOOT, now=9000,
                            harvest_completed=WALL - timedelta(seconds=age),
                            wall_now=WALL) == expected


@pytest.mark.parametrize('end,bound', [(399, 50), (400, 400), (700, 700),
                                     (1500, 1000), (3999, 1000),
                                     (4000, 4000), (5000, 4600)])
def test_session_end_bounds_windows_without_using_events(end, bound):
    record = previous(session_end_monotonic=None, last_pass_bound_monotonic=50)
    ended = budget.ended_record(record, end)
    assert ended.session_end_monotonic == end
    assert ended.last_pass_bound_monotonic == bound


@pytest.mark.parametrize('raw', [None, b'{broken', b'{}', b'x' * 1025])
def test_missing_corrupt_or_oversize_budget_is_uncertain(tmp_path, raw):
    path = tmp_path / budget.BUDGET_FILENAME
    if raw is not None:
        path.write_bytes(raw)
    store = writer(tmp_path, monotonic=lambda: 1000.0)
    try:
        result = store.reserve()
        assert result.first_due == 3600
        assert len(path.read_bytes()) <= 1024
        assert json.loads(path.read_text())['session_end_monotonic'] is None
    finally:
        store.close(2)


def test_end_and_new_reservation_are_serialized_stale_end_cannot_overwrite(tmp_path):
    now = [100.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        first = store.reserve()
        assert first.first_due == 3600
        assert store.end(first.reservation_id, 4200.0)
        now[0] = 4500.0
        second = store.reserve()
        assert second.first_due == 3300.0
        assert second.last_pass_bound_monotonic == 4200.0
        assert store.end(first.reservation_id, 9999.0)
        store.close(2)
        saved = budget.parse_record(json.loads(store.path.read_text()))
        assert saved.reservation_id == second.reservation_id
        assert saved.session_end_monotonic is None
    finally:
        store.close(2)


def test_reboot_and_null_end_override_old_wall_clock_harvest(tmp_path):
    (tmp_path / budget.BUDGET_FILENAME).write_text(json.dumps(asdict(previous(boot_id=OTHER_BOOT))))
    (tmp_path / budget.HARVEST_FILENAME).write_text(json.dumps({
        'last_scan': {'completed_at': (WALL - timedelta(days=5)).isoformat()}}))
    store = writer(tmp_path, monotonic=lambda: 10.0)
    try:
        result = store.reserve()
        assert result.first_due == 3600 and result.last_pass_bound_monotonic == 10.0
    finally:
        store.close(2)


def test_harvest_file_is_reconsulted_only_at_launch(tmp_path):
    (tmp_path / budget.BUDGET_FILENAME).write_text(json.dumps(asdict(previous(last_pass_bound_monotonic=0.0))))
    now = [5000.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        first = store.reserve()
        assert first.first_due == 300
        (tmp_path / budget.HARVEST_FILENAME).write_text(json.dumps({
            'last_scan': {'completed_at': (WALL - timedelta(seconds=50)).isoformat()}}))
        assert first.first_due == 300
        now[0] = 5100.0
        assert store.end(first.reservation_id, now[0])
        # The new process-clock value must follow the prior session end.
        store.close(2)
        second_store = writer(tmp_path, monotonic=lambda: 5200.0)
        try:
            assert second_store.reserve().first_due == 3550
        finally:
            second_store.close(2)
    finally:
        store.close(2)


def test_durable_write_orders_file_fsync_rename_directory_fsync(tmp_path, monkeypatch):
    calls = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        fsync(fd)

    def rename(source, destination):
        calls.append('rename')
        replace(source, destination)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', rename)
    budget.durable_json(tmp_path / budget.BUDGET_FILENAME, asdict(previous()))
    assert calls == ['file', 'rename', 'directory']
    assert not list(tmp_path.glob('*.tmp'))


def test_blocked_write_times_out_entire_reservation_and_keeps_null_end(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocked(path, payload):
        entered.set()
        assert release.wait(8)
        budget.durable_json(path, payload)

    store = writer(tmp_path, monotonic=lambda: 100.0, write=blocked)
    started = time.monotonic()
    try:
        assert store.reserve() is None
        elapsed = time.monotonic() - started
        assert entered.is_set() and 1.9 <= elapsed < 2.8
        # Closing never joins indefinitely when an operation is inside the OS.
        before = time.monotonic()
        store.close()
        assert time.monotonic() - before < 0.1
    finally:
        release.set()
        store.close(2)
    assert json.loads(store.path.read_text())['session_end_monotonic'] is None


def test_queue_full_rejects_immediately_and_drops_end(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocked(path, payload):
        entered.set()
        assert release.wait(8)
        budget.durable_json(path, payload)

    store = writer(tmp_path, monotonic=lambda: 100.0, write=blocked)
    thread = threading.Thread(target=store.reserve)
    thread.start()
    try:
        assert entered.wait(2)
        assert store.end('a' * 32, 1000)
        assert store.end('b' * 32, 1000)
        before = time.monotonic()
        assert store.reserve() is None
        assert time.monotonic() - before < 0.1
        assert not store.end('c' * 32, 1000)
    finally:
        release.set()
        thread.join(3)
        store.close(2)


def test_abandoned_queued_reservation_never_writes(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writes = []

    def blocked(path, payload):
        writes.append(payload)
        entered.set()
        assert release.wait(8)
        budget.durable_json(path, payload)

    store = writer(tmp_path, monotonic=lambda: 100.0, write=blocked)
    first = threading.Thread(target=store.reserve)
    first.start()
    try:
        assert entered.wait(2)
        assert store.reserve() is None
    finally:
        release.set()
        first.join(3)
        store.close(2)
    assert len(writes) == 1


def test_failed_durable_write_never_grants_and_next_attempt_waits(tmp_path):
    calls = []

    def fail_once(path, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise OSError('disk unavailable')
        budget.durable_json(path, payload)

    store = writer(tmp_path, monotonic=lambda: 10000.0, write=fail_once)
    try:
        assert store.reserve() is None
        assert store.reserve().first_due == 3600
    finally:
        store.close(2)


def test_startup_read_is_inside_total_two_second_reservation_bound(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_read = budget.read_bounded_json
    def blocked_read(path, limit):
        if path.name == budget.BUDGET_FILENAME:
            entered.set()
            assert release.wait(6)
        return real_read(path, limit)
    monkeypatch.setattr(budget, 'read_bounded_json', blocked_read)
    store = writer(tmp_path, monotonic=lambda: 100)
    try:
        start = time.monotonic()
        assert store.reserve() is None
        assert entered.is_set() and 1.9 <= time.monotonic() - start < 2.8
    finally:
        release.set()
        store.close(2)
    assert not store.path.exists()  # Abandoned before the writer started the operation.


def test_repeated_crash_orphans_are_removed_before_next_budget_copy(tmp_path, monkeypatch):
    foreign = tmp_path / '.unrelated.keep.tmp'
    foreign.write_text('keep')
    path = tmp_path / budget.BUDGET_FILENAME
    real_atomic = budget.atomic_json
    calls = []
    def checked_atomic(destination, payload):
        assert not list(tmp_path.glob(f'.{path.name}.*.tmp'))
        calls.append(payload)
        real_atomic(destination, payload)
    monkeypatch.setattr(budget, 'atomic_json', checked_atomic)
    for index in range(3):
        orphan = tmp_path / f'.{path.name}.crashed{index}.tmp'
        orphan.write_bytes(b'x' * 1024)
        # Test orphan recovery through the real durable adapter, independently
        # of the launch deadline: an SD-card fsync may legitimately take >2 s.
        budget.durable_json(path, asdict(previous(reservation_id=f'{index + 1:032x}')))
        assert not orphan.exists()
        assert path.stat().st_size <= 1024
    assert len(calls) == 3 and foreign.read_text() == 'keep'


def test_failed_orphan_cleanup_refuses_budget_reservation(tmp_path, monkeypatch):
    from pathlib import Path
    orphan = tmp_path / f'.{budget.BUDGET_FILENAME}.crashed.tmp'
    orphan.write_text('orphan')
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == orphan:
            raise PermissionError('cannot remove prior writer temporary')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    store = writer(tmp_path, monotonic=lambda: 1000)
    try:
        assert store.reserve() is None
        assert orphan.exists() and not store.path.exists()
    finally:
        store.close(2)


@pytest.mark.parametrize('case', ['missing', 'corrupt', 'unreadable', 'reboot', 'null_end', 'null_bound'])
def test_uncertainty_hour_survives_several_short_sessions(tmp_path, monkeypatch, case):
    path = tmp_path / budget.BUDGET_FILENAME
    if case == 'corrupt':
        path.write_text('{broken')
    elif case in ('reboot', 'null_end', 'null_bound'):
        record = previous(origin_monotonic=10, session_end_monotonic=50,
                          last_pass_bound_monotonic=20)
        if case == 'reboot':
            record = budget.replace(record, boot_id=OTHER_BOOT)
        elif case == 'null_end':
            record = budget.replace(record, session_end_monotonic=None)
        else:
            record = budget.replace(record, last_pass_bound_monotonic=None)
        path.write_text(json.dumps(asdict(record)))
    elif case == 'unreadable':
        real_read = budget.read_bounded_json
        def unreadable(source, limit):
            if source == path:
                raise PermissionError('budget cannot be read at startup')
            return real_read(source, limit)
        monkeypatch.setattr(budget, 'read_bounded_json', unreadable)
    now = [100.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        first = store.reserve()
        assert first.origin_monotonic + first.first_due == 3700
        assert first.last_pass_bound_monotonic == 100
        for start, end in [(100, 200), (250, 300), (400, 500), (600, 700)]:
            now[0] = start
            current = first if start == 100 else store.reserve()
            assert current.origin_monotonic + current.first_due == 3700
            assert current.last_pass_bound_monotonic == 100
            now[0] = end
            assert store.end(current.reservation_id, end)
        store.close(2)
        saved = budget.parse_record(json.loads(path.read_text()))
        assert saved.last_pass_bound_monotonic == 100 and saved.session_end_monotonic == 700
    finally:
        store.close(2)


def test_lost_end_establishes_a_new_synthetic_bound_that_survives_early_end(tmp_path):
    now = [100.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        first = store.reserve()
        now[0] = 250
        second = store.reserve()  # No end for the first session.
        assert second.last_pass_bound_monotonic == 250
        assert second.origin_monotonic + second.first_due == 3850
        now[0] = 300
        store.end(second.reservation_id, now[0])
        assert store.end(first.reservation_id, 99999)  # Delayed stale end changes nothing.
        store.close(2)
        now[0] = 400
        store = writer(tmp_path, monotonic=lambda: now[0])
        third = store.reserve()
        assert third.last_pass_bound_monotonic == 250
        assert third.origin_monotonic + third.first_due == 3850
    finally:
        store.close(2)


@pytest.mark.parametrize('new_bound', [None, 99, 0])
@pytest.mark.parametrize('is_end', [False, True])
def test_transition_validator_rejects_lowering_or_clearing_bound(new_bound, is_end):
    old = previous(origin_monotonic=100, session_end_monotonic=None,
                   last_pass_bound_monotonic=100)
    candidate = budget.replace(old, last_pass_bound_monotonic=new_bound,
        session_end_monotonic=200 if is_end else None,
        reservation_id=SESSION if is_end else 'a' * 32)
    with pytest.raises(budget.ClockEvidenceError):
        budget.validate_transition(old, candidate, now=250)


@pytest.mark.parametrize('field,value', [('origin_monotonic', 300),
                                         ('session_end_monotonic', 300),
                                         ('last_pass_bound_monotonic', 300),
                                         ('origin_monotonic', float('nan')),
                                         ('last_pass_bound_monotonic', -1),
                                         ('session_end_monotonic', 'bad-clock')])
def test_future_or_corrupt_same_boot_clock_evidence_refuses_without_rewrite(tmp_path, field, value):
    raw = asdict(previous(origin_monotonic=100, session_end_monotonic=200,
                          last_pass_bound_monotonic=100)) | {field: value}
    path = tmp_path / budget.BUDGET_FILENAME
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    store = writer(tmp_path, monotonic=lambda: 250)
    try:
        assert store.reserve() is None
        assert store.reserve() is None
        assert path.read_bytes() == original
    finally:
        store.close(2)


def test_invalid_clock_from_previous_boot_can_recover_with_fresh_synthetic_bound(tmp_path):
    raw = asdict(previous(boot_id=OTHER_BOOT, last_pass_bound_monotonic=float('nan')))
    (tmp_path / budget.BUDGET_FILENAME).write_text(json.dumps(raw))
    store = writer(tmp_path, monotonic=lambda: 100)
    try:
        result = store.reserve()
        assert result.last_pass_bound_monotonic == 100 and result.first_due == 3600
    finally:
        store.close(2)


def test_future_session_end_and_backwards_clock_cannot_erase_active_bound(tmp_path):
    now = [100.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        first = store.reserve()
        original = store.path.read_bytes()
        assert store.end(first.reservation_id, 10000)
        now[0] = 99
        assert store.reserve() is None
        assert store.path.read_bytes() == original
        now[0] = 250
        second = store.reserve()
        assert second.first_due == 3600 and second.last_pass_bound_monotonic == 250
    finally:
        store.close(2)


@pytest.mark.parametrize('seed', [19, 317, 9011])
def test_random_launch_end_loss_and_reboot_sequences_preserve_cooldown(tmp_path, monkeypatch, seed):
    import random
    import uuid
    randomizer = random.Random(seed)
    clock, boot, disk = [100.0], [BOOT], [None]
    last_start, recovery_floor = None, 0.0
    writes = []
    def read(path, limit):
        if path.name != budget.BUDGET_FILENAME or disk[0] is None:
            raise FileNotFoundError(path)
        return dict(disk[0])
    def write(path, payload):
        prior = disk[0]
        if prior is not None and prior['boot_id'] == payload['boot_id']:
            assert payload['last_pass_bound_monotonic'] >= prior['last_pass_bound_monotonic']
        disk[0] = dict(payload)
        writes.append(dict(payload))
    monkeypatch.setattr(budget, 'read_bounded_json', read)
    def new_writer():
        return budget.DriveDtcBudget(tmp_path, monotonic=lambda: clock[0],
                                    boot_id=lambda: boot[0], wall_now=lambda: WALL, write=write)
    store = new_writer()
    previous_id = None
    try:
        for index in range(200):
            if randomizer.random() < 0.06:
                store.close(2)
                boot[0] = str(uuid.UUID(int=index + 10))
                clock[0], last_start, recovery_floor = 10.0, None, 0.0
                store = new_writer()
            prior = disk[0]
            uncertain = prior is None or prior['boot_id'] != boot[0] or prior['session_end_monotonic'] is None
            if uncertain:
                recovery_floor = clock[0] + 3600
            record = store.reserve()
            assert record is not None
            earliest = record.origin_monotonic + record.first_due
            assert earliest >= recovery_floor
            if last_start is not None:
                assert earliest - last_start >= 3600
            duration = randomizer.randint(0, 250) if randomizer.random() < 0.7 else randomizer.randint(1000, 15000)
            end = clock[0] + duration
            for window in range(5):
                low = max(earliest + window * 3600, (last_start + 3600) if last_start is not None else 0)
                high = min(end, earliest + window * 3600 + 600)
                if low <= high and randomizer.random() < 0.8:
                    actual = randomizer.uniform(low, high)
                    assert actual >= recovery_floor
                    assert last_start is None or actual - last_start >= 3600
                    last_start = actual
            clock[0] = end
            if randomizer.random() < 0.8:
                assert store.end(record.reservation_id, end)
            if previous_id is not None and randomizer.random() < 0.1:
                store.end(previous_id, end + 100000)
            # Closing drains updates and reloads them on a process restart; a
            # lost end remains null in durable state, independent of events.
            store.close(2)
            clock[0] += randomizer.randint(1, 3000)
            previous_id = record.reservation_id
            store = new_writer()
        assert len(writes) >= 200
    finally:
        store.close(2)


@pytest.mark.parametrize('bound', [None, 99])
def test_writer_rejects_corrupt_end_candidate_before_any_publication(tmp_path, monkeypatch, bound):
    now = [100.0]
    store = writer(tmp_path, monotonic=lambda: now[0])
    try:
        record = store.reserve()
        original = store.path.read_bytes()
        real_end = budget.ended_record
        monkeypatch.setattr(budget, 'ended_record', lambda current, ended:
                            budget.replace(real_end(current, ended), last_pass_bound_monotonic=bound))
        now[0] = 200
        assert store.end(record.reservation_id, now[0])
        store.close(2)
        assert store.path.read_bytes() == original
    finally:
        store.close(2)


def test_blocked_fsync_returns_timeout_not_writer_exception(tmp_path, monkeypatch):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real_fsync = os.fsync
    def blocked_fsync(descriptor):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        real_fsync(descriptor)
    def observed_write(path, payload):
        try:
            budget.durable_json(path, payload)
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            completed.set()
    monkeypatch.setattr(os, 'fsync', blocked_fsync)
    store = writer(tmp_path, monotonic=lambda: 1000, write=observed_write)
    try:
        started = time.monotonic()
        assert store.reserve() is None
        assert 1.9 <= time.monotonic() - started < 2.8
        # The only in-flight write is parked inside fsync, not an exception
        # branch or a refused queue. The caller abandoned a valid operation.
        assert entered.is_set() and not completed.is_set() and not errors
    finally:
        release.set()
        assert completed.wait(10)
        store.close(2)
    assert not errors
    saved = budget.parse_record(json.loads(store.path.read_text()))
    assert saved.session_end_monotonic is None
    assert saved.last_pass_bound_monotonic == 1000
