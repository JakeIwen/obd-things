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
                                  previous(session_end_monotonic=8000.0)])
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
    record = previous(last_pass_bound_monotonic=None)
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
        assert result.first_due == 3600 and result.last_pass_bound_monotonic is None
    finally:
        store.close(2)


def test_harvest_file_is_reconsulted_only_at_launch(tmp_path):
    (tmp_path / budget.BUDGET_FILENAME).write_text(json.dumps(asdict(previous(last_pass_bound_monotonic=None))))
    store = writer(tmp_path, monotonic=lambda: 5000.0)
    try:
        first = store.reserve()
        assert first.first_due == 300
        (tmp_path / budget.HARVEST_FILENAME).write_text(json.dumps({
            'last_scan': {'completed_at': (WALL - timedelta(seconds=50)).isoformat()}}))
        assert first.first_due == 300
        assert store.end(first.reservation_id, 5100.0)
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
        store = writer(tmp_path, monotonic=lambda: 1000)
        try:
            assert store.reserve() is not None
        finally:
            store.close(2)
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
