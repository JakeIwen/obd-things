"""Exercise USB recovery's commit deadlines, durable publication and crash boundaries."""

import fcntl
import os
from dataclasses import replace
from pathlib import Path

import pytest

from lib import can_operation_state as operation
from projects.vehicle_data import usb_recovery_record as records
from projects.vehicle_data.usb_recovery_contract import (
    ENGINE_EVIDENCE_MAX_AGE_SECONDS, IncidentState as State, Refusal,
)
from tests.usb_recovery_support import (
    ACTIVE, AUX, BOOT, B_INHIBIT, C, C_INHIBIT, WALL,
    assert_committed, assert_failed, assert_paused_and_resumed, crash, lab, ready, target_indexes,
)


def test_compare_and_end_detects_replacement_after_commit_check(ready):
    before = ready.owned_files()
    ci, bi = target_indexes(ready)

    def racing_writer(expected, **kwargs):
        operation.begin_inhibit(B_INHIBIT, channel='*', reason='arrived at compare-and-end')
        second = f'inhibit-{B_INHIBIT}.json'
        before[second] = ready.owned_files()[second]
        return ready.end(expected, **kwargs)

    ready.effects.end = racing_writer
    ready.run()
    assert_failed(ready, Refusal.COMMIT_REFUSED, before)
    assert ready.commands()[-2:] == [('down', bi), ('down', ci)]
    assert not ready.up_indexes


def fail_unlink(monkeypatch, name, error):
    unlink = os.unlink

    def selective_unlink(path, *args, **kwargs):
        if Path(path).name == f'inhibit-{name}.json':
            raise error
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(operation.os, 'unlink', selective_unlink)


def assert_partial(lab, before):
    assert lab.persisted().state == State.PARTIAL_COMMIT
    assert lab.persisted().transaction.ended_inhibits == [C_INHIBIT]
    assert lab.owned_files() == {f'inhibit-{B_INHIBIT}.json': before[f'inhibit-{B_INHIBIT}.json']}
    assert not lab.events('clear')
    assert lab.latched == {ACTIVE, AUX}
    assert lab.reserved()
    assert_paused_and_resumed(lab)
    assert lab.up_indexes == set(target_indexes(lab))
    assert all(command == 'up-listen-only' for command, _ in lab.commands())


def test_unlink_error_after_first_unlink_never_rolls_back(ready, monkeypatch):
    before = ready.owned_files()
    fail_unlink(monkeypatch, B_INHIBIT, OSError('simulated unlink failure'))
    ready.run()
    assert_partial(ready, before)


def test_commit_deadline_expires_twice_while_the_writer_lock_is_held(ready, monkeypatch):
    before = ready.owned_files()
    flock = fcntl.flock
    attempts = []

    def advancing_flock(fd, mode):
        try:
            return flock(fd, mode)
        except BlockingIOError:
            attempts.append(ready.now)
            ready.now += ENGINE_EVIDENCE_MAX_AGE_SECONDS + 1
            raise

    # Another open description owns the actual operation-state writer lock.
    with (ready.state_dir / '.inhibit-writers.lock').open('rb') as holder:
        flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(fcntl, 'flock', advancing_flock)
        ready.run()

    assert_failed(ready, Refusal.ENGINE_EVIDENCE_STALE, before)
    assert not ready.up_indexes
    samples = [e[2] for e in ready.events('rpm')]
    assert len(samples) == 3 and samples[2] > samples[1]
    assert attempts == samples[1:]
    assert [e[1] for e in ready.events('end')] == [t + ENGINE_EVIDENCE_MAX_AGE_SECONDS for t in attempts]


@pytest.mark.parametrize('helpers,slow', [
    ((ACTIVE,), C_INHIBIT), ((ACTIVE, AUX), C_INHIBIT), ((ACTIVE, AUX), B_INHIBIT),
])
def test_late_unlink_is_reported_never_undone(lab, monkeypatch, helpers, slow):
    lab.incident(*helpers)
    before = lab.owned_files()
    lab.replace_board()
    original = os.unlink
    results = []

    def slow_unlink(path):
        original(path)
        if Path(path).name == f'inhibit-{slow}.json':
            lab.now += ENGINE_EVIDENCE_MAX_AGE_SECONDS + 1

    def end(expected, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(operation.os, 'unlink', slow_unlink)
            result = lab.end(expected, **kwargs)
            results.append(result)
            return result

    lab.effects.end = end
    lab.run()
    assert len(results) == 1 and results[0].late
    result = results[0]
    assert lab.latched == set(helpers) and not lab.events('clear')
    incident = lab.persisted()
    assert 'late' in incident.detail
    assert not any(command == 'down' for command, _ in lab.commands())
    if result.outcome == 'removed':
        assert incident.state == State.COMMITTED_LATE
        assert any(line.startswith('ERROR ') and 'state=committed_late' in line for line in lab.logs)
        assert result.removed == ((C_INHIBIT,) if slow == C_INHIBIT and helpers == (ACTIVE,) else (C_INHIBIT, B_INHIBIT))
    else:
        assert result.outcome == 'partial' and result.removed == (C_INHIBIT,)
        assert_partial(lab, before)
    assert incident.transaction.ended_inhibits == list(result.removed)
    # Whatever was removed stays removed; whatever remains is byte-identical and still honoured.
    assert lab.owned_files() == {k: v for k, v in before.items()
                                 if k not in {f'inhibit-{n}.json' for n in result.removed}}


def test_commit_deadline_starts_before_a_slow_read(ready):
    before = ready.owned_files()
    original = ready.snapshot

    def late_sample(*args, **kwargs):
        reading = original(*args, **kwargs)
        if len(ready.up_indexes) == 2:
            ready.now += ENGINE_EVIDENCE_MAX_AGE_SECONDS + 1
        return reading

    ready.snapshot = late_sample
    ready.run()
    txn = assert_failed(ready, Refusal.ENGINE_EVIDENCE_STALE, before)
    samples = [e[2] for e in ready.events('rpm')]
    assert len(samples) == 3
    assert [e[1] for e in ready.events('end')] == [t + ENGINE_EVIDENCE_MAX_AGE_SECONDS for t in samples[1:]]
    assert txn.engine_seen_monotonic == samples[-1]


def test_commit_deadline_expiring_in_first_directory_fsync_is_partial(ready, monkeypatch):
    before = ready.owned_files()
    fsync = os.fsync
    calls = []

    def slow_fsync(fd):
        assert ready.owned_files().keys() == {f'inhibit-{B_INHIBIT}.json'}
        calls.append(fd)
        ready.now += ENGINE_EVIDENCE_MAX_AGE_SECONDS + 1
        fsync(fd)

    def end(expected, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(operation.os, 'fsync', slow_fsync)
            return ready.end(expected, **kwargs)

    ready.effects.end = end
    ready.run()
    assert_partial(ready, before)
    assert len(calls) == len(ready.events('end')) == 1
    assert len(ready.events('rpm')) == 2


def test_failed_committed_record_write_does_not_rollback_or_restore_latches(ready):
    def write(incident, **kwargs):
        if incident.state == State.COMMITTED:
            raise OSError('committed record could not be published')
        records.write_incident(incident, **kwargs)

    ready.store.write_incident = write
    ready.run()
    assert ready.persisted().state == State.COMMITTING
    assert ready.recovery.incident.state == State.COMMITTED
    assert any('post-commit record failed: OSError' in line for line in ready.logs)
    assert len(ready.events('clear')) == 1 and not ready.latched
    assert_committed_effects(ready)


def assert_committed_effects(lab):
    assert not lab.owned_files()
    assert lab.reserved()
    assert_paused_and_resumed(lab)
    assert lab.up_indexes == set(target_indexes(lab))
    assert lab.commands() == [('up-listen-only', index) for index in target_indexes(lab)]


def test_clear_latches_failure_is_post_commit(ready):
    attempts = []

    def clear(helpers, incident_id):
        attempts.append((helpers, incident_id))
        assert not ready.owned_files()
        raise OSError('clear callback refused')

    ready.recovery.bind(clear_latches=clear, display_receiver=ready)
    ready.run()
    incident = ready.persisted()
    assert incident.state == State.COMMITTED_LATCHES_KEPT
    assert attempts == [(frozenset({ACTIVE, AUX}), incident.incident_id)]
    assert ready.latched == {ACTIVE, AUX}
    assert_committed_effects(ready)


@pytest.mark.parametrize('one_gone', [False, True])
def test_unexpected_commit_error_rolls_back_only_if_both_inhibits_are_intact(ready, one_gone):
    before = ready.owned_files()

    def end(expected, **kwargs):
        if one_gone:
            operation.end_inhibit(C_INHIBIT)
        raise OSError('commit callback failed')

    ready.effects.end = end
    ready.run()
    if one_gone:
        assert_partial(ready, before)
    else:
        assert_failed(ready, Refusal.UNEXPECTED_ERROR, before)
        ci, bi = target_indexes(ready)
        assert ready.commands()[-2:] == [('down', bi), ('down', ci)]
        assert not ready.up_indexes


def test_boot_budget_is_kept_even_if_other_boot_files_look_newer(ready):
    ready.run()
    first = assert_committed(ready)
    reservation, = ready.directory.glob(f'reservation-{BOOT}.json')
    original = reservation.read_bytes()
    for boot in range(8):
        incident_id = f'{boot:032x}'
        boot_id = f'future-{boot}'
        seed = ready.directory.parent / boot_id
        records.reserve(boot_id, incident_id, broker_pid=1, reserved_at=WALL, directory=seed)
        records.write_incident(replace(first, boot_id=boot_id, incident_id=incident_id), directory=seed)
        future = reservation.stat().st_mtime + 86400
        for path in seed.glob('*.json'):
            os.utime(path, (future, future))
            path.rename(ready.directory / path.name)
    ready.trace.clear()
    ready.incident()
    second = ready.persisted()
    assert second.incident_id != first.incident_id
    assert (second.state, second.reason, second.transaction) == (State.INELIGIBLE, Refusal.BOOT_BUDGET_SPENT, None)
    ready.run()
    assert not ready.commands() and not ready.events('lock')
    assert reservation.read_bytes() == original
    assert ready.persisted(first.incident_id).state == State.COMMITTED


def test_incident_publication_syncs_file_and_directory_before_replace_returns(ready, monkeypatch):
    real = records.atomic_json
    calls = []

    def observed(path, obj, **kwargs):
        result = real(path, obj, **kwargs)
        calls.append((obj['state'], kwargs.get('fsync'), kwargs.get('fsync_dir')))
        return result

    monkeypatch.setattr(records, 'atomic_json', observed)
    ready.run()
    assert_committed(ready)
    assert calls == [(state, True, True) for state in (
        State.RESERVED, State.CONFIGURING, State.VERIFYING, State.COMMITTING, State.COMMITTED)]


@pytest.mark.parametrize('boundary,disk_state,remaining,up_count', [
    ('latch', State.OPEN, 2, 0),
    ('reserved', State.RESERVED, 2, 0),
    ('pre_submit', State.CONFIGURING, 2, 0),
    ('only_c', State.CONFIGURING, 2, 1),
    ('both_up', State.VERIFYING, 2, 2),
    ('first_unlink', State.COMMITTING, 1, 2),
    ('both_unlinked', State.COMMITTING, 0, 2),
    ('cleared', State.COMMITTING, 0, 2),
])
def test_crash_table(ready, monkeypatch, boundary, disk_state, remaining, up_count):
    before = ready.owned_files()
    incident_id = ready.persisted().incident_id
    if boundary == 'latch':
        ready.store.reserve = crash
    elif boundary == 'pre_submit':
        ready.submit = crash
    elif boundary == 'only_c':
        ready.snapshot = crash
    elif boundary == 'both_up':
        ready.effects.write_topology = crash
    elif boundary == 'first_unlink':
        fail_unlink(monkeypatch, B_INHIBIT, SystemExit('simulated crash'))
    elif boundary == 'both_unlinked':
        ready.recovery.bind(clear_latches=crash, display_receiver=ready)
    else:
        def write(incident, **kwargs):
            if boundary == 'cleared' and incident.state == State.COMMITTED:
                crash()
            records.write_incident(incident, **kwargs)
            if boundary == 'reserved' and incident.state == State.RESERVED:
                crash()

        ready.store.write_incident = write

    with pytest.raises(SystemExit, match='simulated crash'):
        ready.run()
    persisted = ready.persisted(incident_id)
    assert persisted.state == disk_state
    names = () if remaining == 0 else ((B_INHIBIT,) if remaining == 1 else (C_INHIBIT, B_INHIBIT))
    assert ready.owned_files() == {f'inhibit-{n}.json': before[f'inhibit-{n}.json'] for n in names}
    assert len(ready.up_indexes) == up_count
    assert not any(command == 'down' for command, _ in ready.commands())
    assert ready.reserved() == (boundary != 'latch')
    assert_paused_and_resumed(ready)
    if boundary == 'pre_submit':
        assert [(m.role, m.ifindex, m.outcome) for m in persisted.transaction.mutations] == [
            (C, target_indexes(ready)[0], None)]
    assert not ready.latched if boundary == 'cleared' else ready.latched == {ACTIVE, AUX}

    ready.trace.clear()
    ready.new_runtime()
    ready.run()
    assert ready.recovery.incident is None
    assert not ready.commands() and not ready.events('pause')
    assert ready.persisted(incident_id).state == disk_state
    if boundary != 'latch':
        ready.store.write_incident = records.write_incident
        ready.latch(ACTIVE, 'new process, new latch')
        assert ready.persisted().reason == Refusal.BOOT_BUDGET_SPENT
        ready.run()
        assert not ready.commands()


@pytest.mark.parametrize('took_effect', [False, True])
def test_death_at_final_unlink_makes_no_second_attempt(ready, monkeypatch, took_effect):
    before = ready.owned_files()
    incident_id = ready.persisted().incident_id
    original = os.unlink

    def dying(path, *args, **kwargs):
        if Path(path).name == f'inhibit-{B_INHIBIT}.json':
            if took_effect:
                original(path, *args, **kwargs)
            raise SystemExit('simulated crash')
        original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(operation.os, 'unlink', dying)
        with pytest.raises(SystemExit, match='simulated crash'):
            ready.run()
    assert ready.persisted(incident_id).state == State.COMMITTING
    expected_left = {} if took_effect else {f'inhibit-{B_INHIBIT}.json': before[f'inhibit-{B_INHIBIT}.json']}
    assert ready.owned_files() == expected_left
    assert [r['name'] for r in operation.active_inhibits('can0')] == ([] if took_effect else [B_INHIBIT])
    ready.trace.clear()
    ready.new_runtime()
    ready.run()
    assert ready.recovery.incident is None and not ready.commands()
    assert ready.persisted(incident_id).state == State.COMMITTING
    assert ready.owned_files() == expected_left
    ready.latch(ACTIVE, 'new process observed another restoration fault')
    ready.run()
    assert ready.persisted().reason == Refusal.BOOT_BUDGET_SPENT
    assert not ready.commands()
