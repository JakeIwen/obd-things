"""Exercise the recovery collector transaction without touching vehicle or service state."""

from dataclasses import replace

import pytest

from lib import can_operation_state as operation, diagnostic_safety
from projects.vehicle_data.usb_recovery import UsbDropRecovery
from projects.vehicle_data.usb_recovery_contract import (
    COMMIT_PROCESS_RETRIES, RECOVERY_WINDOW_SECONDS, RUNNING_CHECK_ATTEMPTS,
    SUBMITTER_WAIT_SECONDS, MutationOutcome, Refusal,
)
from projects.vehicle_data.usb_recovery_model import ProcessCheck
from tests.test_service_cgroup import FakeCgroup
from tests.usb_recovery_support import (
    ACTIVE, AUX, B, B_INHIBIT, C, C_INHIBIT, ORPHAN, QUIET, UNSTABLE, FakeCommand,
    assert_committed, assert_deferred, assert_failed, assert_paused_and_resumed, lab, ready, target_indexes,
)


@pytest.mark.parametrize('renumber', [False, True])
def test_recovers_both_roles_under_ordered_locks(lab, renumber):
    lab.incident()
    lab.replace_board(renumber=renumber)
    c, b = (lab.mapping[role] for role in (C, B))
    ci, bi = target_indexes(lab)
    lab.run()

    incident = assert_committed(lab)
    assert [m.outcome for m in incident.transaction.mutations] == [MutationOutcome.UP_VERIFIED] * 2
    assert [step.step for step in incident.transaction.steps][:4] == [
        'precheck', 'display_paused', 'locked_precheck', 'reserve',
    ]
    assert all(step.ok for step in incident.transaction.steps)
    assert lab.commands() == [('up-listen-only', ci), ('up-listen-only', bi)]
    assert lab.events('topology') == [('topology', c, C), ('topology', b, B)]
    for channel, role in ((c, C), (b, B)):
        topology = operation.load_topology(channel)
        assert topology.usable and topology.bus == role and topology.source == 'usb_serial_and_dev_id'
    modes = [('exclusive', 'can-role-c-can'), ('exclusive', 'can-role-b-can'),
             ('shared', 'can-role-can-ch'), ('shared', 'can-role-spare'), ('exclusive', c), ('exclusive', b)]
    assert lab.events('lock') == [('lock', *mode) for mode in modes]
    assert lab.events('release') == [('release', *mode) for mode in reversed(modes)]
    assert_paused_and_resumed(lab)
    command_c = lab.trace.index(('command', 'up-listen-only', ci))
    command_b = lab.trace.index(('command', 'up-listen-only', bi))
    rpm_indexes = [i for i, event in enumerate(lab.trace) if event[0] == 'rpm']
    first_rpm, commit_rpm = lab.events('rpm')
    assert first_rpm[1::2] == (c, {'timeout': 0.5, 'required_rpm_samples': 3})
    assert commit_rpm[1::2] == first_rpm[1::2]
    first_read, commit_read = rpm_indexes
    assert (lab.trace.index(('pause', B)) < lab.trace.index(lab.events('lock')[0]) < command_c
            < lab.trace.index(('identify', c, 0.25)) < first_read < command_b < commit_read
            < lab.trace.index(lab.events('clear')[0]) < lab.trace.index(lab.events('release')[0]))


def test_replay_old_then_missing_then_replacement(lab):
    lab.incident()
    before = lab.owned_files()
    lab.run()
    assert_deferred(lab, Refusal.OLD_INSTANCE_PRESENT)
    for role in (C, B):
        del lab.netdevs[lab.mapping[role]]
    lab.run()
    assert_deferred(lab, Refusal.ROLE_UNRESOLVED)
    assert lab.owned_files() == before
    lab.replace_board()
    lab.run()
    assert_committed(lab)


def test_second_drop_between_the_two_link_commands(ready):
    ci, bi = target_indexes(ready)
    name = ready.mapping[B]
    original = ready.effects.snapshot

    def snapshot(*args, **kwargs):
        ready.netdevs[name] = replace(ready.netdevs[name], ifindex=bi + 1, usb_devnum=10)
        return original(*args, **kwargs)

    ready.effects.snapshot = snapshot
    before = ready.owned_files()
    ready.run()
    txn = assert_failed(ready, Refusal.INSTANCE_CHANGED, before)
    assert ready.commands() == [('up-listen-only', ci), ('down', ci)]
    assert not ready.up_indexes
    assert [m.outcome for m in txn.mutations] == [MutationOutcome.DOWN_VERIFIED, MutationOutcome.INSTANCE_GONE]


def test_unverified_first_rollback_does_not_abandon_the_other_instance(ready):
    ci, bi = target_indexes(ready)
    before = ready.owned_files()
    down = ready.effects.down
    ready.effects.end = lambda *args, **kwargs: operation.EndResult('mismatch')

    def down_one(instance, *, wait_quiet):
        if instance.ifindex == bi:
            return MutationOutcome.ROLLBACK_UNVERIFIED
        assert instance.ifindex == ci
        return down(instance, wait_quiet=wait_quiet)

    ready.effects.down = down_one
    ready.run()
    txn = assert_failed(ready, Refusal.COMMIT_REFUSED, before)
    assert [m.outcome for m in txn.mutations] == [MutationOutcome.DOWN_VERIFIED, MutationOutcome.ROLLBACK_UNVERIFIED]
    assert ready.up_indexes == {bi}


def test_orphan_delays_reservation_but_later_exit_allows_recovery(ready, tmp_path):
    group = FakeCgroup(tmp_path / 'group')
    group.process(417, '/usr/bin/sudo', '-n', ppid=1)
    group.list(417)
    ready.effects.processes = group.inspect
    ready.run()
    assert_deferred(ready, Refusal.PROCESSES_NOT_QUIET)
    assert not ready.events('pause')
    group.list()
    ready.run()
    assert_committed(ready)


def test_orphan_at_commit_rolls_back_after_it_exits(ready):
    seen = iter([QUIET, QUIET, ORPHAN, ORPHAN, QUIET])
    ready.effects.processes = lambda: next(seen)
    before = ready.owned_files()
    ci, bi = target_indexes(ready)
    ready.run()
    assert_failed(ready, Refusal.PROCESSES_NOT_QUIET, before)
    assert ready.commands()[-2:] == [('down', bi), ('down', ci)]
    assert not ready.up_indexes
    assert ready.events('sleep') == [('sleep', 0.2)]


def test_notification_orphan_does_not_block_commit(ready, tmp_path):
    group = FakeCgroup(tmp_path / 'group')
    group.process(419, '/usr/bin/curl', 'example notification', ppid=1)

    def processes():
        if len(ready.up_indexes) == 2:
            group.list(419)
            ready.trace.append(('notification-at-commit',))
        return group.inspect()

    ready.effects.processes = processes
    ready.run()
    assert ready.events('notification-at-commit') == [('notification-at-commit',)]
    assert_committed(ready)


@pytest.mark.parametrize('name', ['alfaobd', 'vehicle-data-armed-at-startup-c-can'])
def test_foreign_inhibit_is_untouched(ready, name):
    operation.begin_inhibit(name, channel='*', reason='foreign owner')
    before = ready.owned_files()
    ready.run()
    assert_deferred(ready, Refusal.FOREIGN_INHIBIT)
    assert not ready.events('lock')
    assert ready.owned_files() == before


def test_overwritten_owned_inhibit_is_untouched(ready):
    operation.begin_inhibit(B_INHIBIT, channel='*', reason='new helper evidence')
    before = ready.owned_files()
    ready.run()
    assert_deferred(ready, Refusal.INHIBIT_RECORD_CHANGED, terminal=True)
    assert ready.owned_files() == before


@pytest.mark.parametrize('sensor_failure', [
    'low', 'oserror', 'stops_at_commit', 'replaced_ccan', 'renamed_ccan', 'wrong_bus',
])
def test_receive_only_engine_and_identity_gates(ready, sensor_failure):
    original = ready.effects.snapshot
    ci, bi = target_indexes(ready)
    before = ready.owned_files()

    def snapshot(channel, **kwargs):
        valid = original(channel, **kwargs)
        if sensor_failure == 'oserror':
            raise OSError('receive failed')
        if sensor_failure in ('replaced_ccan', 'renamed_ccan'):
            instance = ready.netdevs[channel]
            ready.netdevs[channel] = replace(instance, ifindex=ci + bi)
            if sensor_failure == 'replaced_ccan':
                ready.up_indexes.remove(ci)
            else:
                ready.netdevs['can4'] = replace(instance, channel='can4')
        if sensor_failure == 'low' or (sensor_failure == 'stops_at_commit' and len(ready.up_indexes) == 2):
            return ready.sample(399)
        return valid

    ready.effects.snapshot = snapshot
    if sensor_failure == 'wrong_bus':
        ready.effects.identify = lambda channel, *, probe: B
    ready.run()
    reasons = {'replaced_ccan': Refusal.INSTANCE_CHANGED, 'renamed_ccan': Refusal.INSTANCE_CHANGED,
               'wrong_bus': Refusal.WRONG_BUS}
    txn = assert_failed(ready, reasons.get(sensor_failure, Refusal.ENGINE_NOT_RUNNING), before)
    if sensor_failure == 'stops_at_commit':
        assert ready.commands() == [('up-listen-only', ci), ('up-listen-only', bi), ('down', bi), ('down', ci)]
    elif sensor_failure == 'replaced_ccan':
        assert ready.commands() == [('up-listen-only', ci)]
        assert [m.outcome for m in txn.mutations] == [MutationOutcome.INSTANCE_GONE]
    else:
        assert ready.commands() == [('up-listen-only', ci), ('down', ci)]
        if sensor_failure == 'renamed_ccan':
            # The index was targeted, but readback by the original name cannot verify DOWN.
            assert [m.outcome for m in txn.mutations] == [MutationOutcome.ROLLBACK_UNVERIFIED]
    assert not ready.up_indexes
    expected_reads = {'low': RUNNING_CHECK_ATTEMPTS, 'wrong_bus': 0, 'stops_at_commit': 2}
    assert len(ready.events('rpm')) == expected_reads.get(sensor_failure, 1)
    assert not ready.events('end')


@pytest.mark.parametrize('failure', ['busy', 'exception'])
def test_display_resumes_both_after_second_pause_fails(ready, failure):
    pause = ready.pause_bcan_passive

    def second_pause():
        pause()
        if failure == 'exception':
            raise OSError('second display pause failed')
        return False

    ready.pause_bcan_passive = second_pause
    if failure == 'exception':
        with pytest.raises(OSError, match='second display pause failed'):
            ready.run()
        assert not ready.reserved()
    else:
        ready.run()
        assert_deferred(ready, Refusal.DISPLAY_BUSY)
    assert_paused_and_resumed(ready)
    assert not ready.events('lock') and not ready.commands()


@pytest.mark.parametrize('exclusive', [False, True])
def test_board_b_lock_coexists_only_with_shared_recorder(ready, exclusive):
    holder = diagnostic_safety.channel_lock if exclusive else diagnostic_safety.channel_observer_lock
    with holder('can-role-can-ch'):
        ready.run()
    assert_paused_and_resumed(ready)
    if exclusive:
        assert_deferred(ready, Refusal.LOCKS_BUSY)
        assert ready.events('release') == [('release', 'exclusive', 'can-role-b-can'),
                                           ('release', 'exclusive', 'can-role-c-can')]
    else:
        assert_committed(ready)


def test_unchanged_usb_enumeration_expires(lab):
    lab.incident()
    lab.replace_board(devnum=7)
    lab.run()
    assert_deferred(lab, Refusal.USB_INSTANCE_UNCHANGED)
    lab.now += RECOVERY_WINDOW_SECONDS + 1
    lab.run()
    assert_deferred(lab, Refusal.WINDOW_EXPIRED, terminal=True)
    assert not lab.events('pause')


def test_synthesized_aux_final_waits_for_claim_and_uses_real_final(ready):
    ready.gates['claimed'] = frozenset({AUX})
    synthesized = ready.latch(AUX, 'synthesized final while claimed')
    ready.run()
    assert_deferred(ready, Refusal.HELPER_CLAIMED)
    real = ready.latch(AUX, 'late real final after child reaped')
    assert synthesized != real
    ready.gates.clear()
    ready.run()
    assert assert_committed(ready).helpers[AUX].owned_inhibit == real


@pytest.mark.parametrize('marker,reason', [((1, False), Refusal.RADAR_RUN_ATTEMPTED),
                                          (None, Refusal.RADAR_RUN_UNCERTAIN)])
def test_radar_refusal_never_pauses_or_takes_locks(ready, marker, reason):
    ready.radar = marker
    ready.run()
    assert_deferred(ready, reason, terminal=True)
    assert not ready.events('pause') and not ready.events('lock')


@pytest.mark.parametrize('phase', ['locked', 'commit'])
@pytest.mark.parametrize('gate,reason', [('hold_active', Refusal.HELPERS_HELD),
                                        ('stopping', Refusal.BROKER_STOPPING)])
def test_broker_gates_are_fresh_after_initial_check(ready, phase, gate, reason):
    before = ready.owned_files()

    def processes():
        if ready.events('lock') and (phase == 'locked' or len(ready.up_indexes) == 2):
            ready.gates[gate] = True
        return QUIET

    ready.effects.processes = processes
    ready.run()
    if phase == 'locked':
        assert_deferred(ready, reason)
        assert_paused_and_resumed(ready)
    else:
        assert_failed(ready, reason, before)
        assert not ready.up_indexes


def test_auxiliary_thread_alive_without_a_claim_prevents_pause(ready):
    ready.gates['auxiliary_thread_alive'] = True
    ready.run()
    assert_deferred(ready, Refusal.HELPER_THREAD_ALIVE)
    assert not ready.events('pause')


def test_locked_terminal_refusal_resumes_displays(ready):
    ready.effects.helper_ok = lambda: not ready.events('lock')
    ready.run()
    assert_deferred(ready, Refusal.LINK_HELPER_UNVERIFIED, terminal=True)
    assert_paused_and_resumed(ready)


def test_missing_link_helper_disables_opt_in_and_refuses_decision(ready):
    ready.effects.helper_ok = lambda: False
    assert UsbDropRecovery.if_enabled(True, ready, effects=ready.effects, log=ready.logs.append) is None
    assert ready.logs[-1] == 'usb-drop-recovery disabled: link helper not installed or differs'
    ready.run()
    assert_deferred(ready, Refusal.LINK_HELPER_UNVERIFIED, terminal=True)
    assert not ready.events('pause')


def test_link_helper_replaced_before_commit_is_not_used_for_rollback(ready):
    before = ready.owned_files()
    ci, bi = target_indexes(ready)
    ready.effects.helper_ok = lambda: len(ready.events('topology')) < 2
    ready.run()
    txn = assert_failed(ready, Refusal.LINK_HELPER_UNVERIFIED, before)
    assert ready.commands() == [('up-listen-only', ci), ('up-listen-only', bi)]
    assert [m.outcome for m in txn.mutations] == [MutationOutcome.ROLLBACK_UNVERIFIED] * 2
    assert ready.up_indexes == {ci, bi}
    assert not ready.events('end')


@pytest.mark.parametrize('helper', [ACTIVE, AUX])
def test_single_helper_latch(lab, helper):
    lab.incident(helper)
    lab.replace_board()
    lab.run()
    if helper == AUX:
        assert_deferred(lab, Refusal.BCAN_ONLY_LATCH, terminal=True)
        assert not lab.events('lock')
    else:
        assert_committed(lab, helpers=(ACTIVE,), names=(C_INHIBIT,))


def test_new_claim_that_never_reaches_factory_discards_old_armed_slot(lab):
    lab.launch(ACTIVE)  # First launch ends clean without a latch.
    lab.recovery.begin_launch(ACTIVE)
    lab.latch(ACTIVE, 'synthesized final without a factory invocation')
    lab.replace_board()
    lab.run()
    assert_deferred(lab, Refusal.ARMED_INSTANCE_UNKNOWN, terminal=True)
    assert not lab.events('pause')


@pytest.mark.parametrize('persistent', [False, True])
def test_commit_retries_cgroup_churn_only_to_its_limit(ready, persistent):
    unstable = COMMIT_PROCESS_RETRIES + 1 if persistent else 1
    scripted = iter([QUIET, QUIET] + [UNSTABLE] * unstable + [QUIET])
    ready.effects.processes = lambda: next(scripted)
    before = ready.owned_files()
    ready.run()
    assert ready.events('sleep') == [('sleep', 0.1)] * (COMMIT_PROCESS_RETRIES if persistent else 1)
    if persistent:
        assert_failed(ready, Refusal.PROCESSES_UNSTABLE, before)
        assert not ready.up_indexes
    else:
        assert_committed(ready)


@pytest.mark.parametrize('sudo_outcome', ['timeout', 'early_exit'])
@pytest.mark.parametrize('survivor', ['exits', 'root', 'unverifiable', 'unstable'])
def test_uncertain_submission_demands_positive_quiescence_before_read_or_down(ready, sudo_outcome, survivor):
    ci, bi = target_indexes(ready)
    original_submit = ready.submit
    before = ready.owned_files()
    submitted = None
    busy = {'exits': ORPHAN, 'root': ProcessCheck(False, offenders=((417, 'root', 'python3'),)),
            'unverifiable': ProcessCheck(False, unverifiable=True), 'unstable': UNSTABLE}[survivor]

    def submit(cmd, index):
        nonlocal submitted
        if cmd == 'up-listen-only' and index == bi:
            submitted = ready.now
            return FakeCommand(ready, 1, hangs=sudo_outcome == 'timeout')
        return original_submit(cmd, index)

    def processes():
        if submitted is None:
            return QUIET
        if survivor == 'exits' and ready.now >= submitted + 0.39:
            ready.up_indexes.add(bi)  # The surviving child applied its request after its parent exited.
            ready.trace.append(('quiet-after-late-up',))
            return QUIET
        ready.trace.append(('not-quiet',))
        return busy

    ready.submit = submit
    ready.effects.processes = processes
    ready.run()
    txn = assert_failed(ready, Refusal.CONFIGURE_FAILED, before)
    suffix = ready.trace[ready.trace.index(('command', 'up-listen-only', bi)) + 1:]
    if sudo_outcome == 'timeout':
        assert suffix[:3] == [('signal', 'SIGTERM'), ('signal', 'SIGKILL'), ('reap',)]
    else:
        assert not ready.events('signal')
    if survivor == 'exits':
        before_quiet = suffix[:suffix.index(('quiet-after-late-up',))]
        assert len([e for e in before_quiet if e == ('not-quiet',)]) == 2
        assert not any(e[0] in ('read', 'command') for e in before_quiet)
        assert ready.commands()[-2:] == [('down', bi), ('down', ci)]
        assert [m.outcome for m in txn.mutations] == [MutationOutcome.DOWN_VERIFIED] * 2
        assert not ready.up_indexes
    else:
        assert not any(e[0] in ('read', 'command') for e in suffix)
        assert ready.now - submitted == pytest.approx(SUBMITTER_WAIT_SECONDS, abs=0.21)
        assert [m.outcome for m in txn.mutations] == [MutationOutcome.ROLLBACK_UNVERIFIED] * 2
        assert ready.up_indexes == {ci}


@pytest.mark.parametrize('exits', [False, True])
def test_uncertain_down_requires_a_second_positive_cgroup_check(ready, exits):
    ci, bi = target_indexes(ready)
    before = ready.owned_files()
    original_submit = ready.submit
    original_snapshot = ready.effects.snapshot
    down_at = None

    def submit(command, index):
        nonlocal down_at
        result = original_submit(command, index)
        if command == 'down' and index == bi:
            down_at = ready.now
            return FakeCommand(ready, 1)
        return result

    def snapshot(*args, **kwargs):
        running = original_snapshot(*args, **kwargs)
        return ready.sample(399) if len(ready.up_indexes) == 2 else running

    def processes():
        if down_at is None or (exits and ready.now >= down_at + 0.19):
            if down_at is not None:
                ready.trace.append(('quiet-after-down',))
            return QUIET
        return ORPHAN

    ready.submit = submit
    ready.effects.snapshot = snapshot
    ready.effects.processes = processes
    ready.run()
    txn = assert_failed(ready, Refusal.ENGINE_NOT_RUNNING, before)
    after_down = ready.trace[ready.trace.index(('command', 'down', bi)) + 1:]
    outcome = MutationOutcome.DOWN_VERIFIED if exits else MutationOutcome.ROLLBACK_UNVERIFIED
    assert [m.outcome for m in txn.mutations] == [outcome, outcome]
    if exits:
        assert after_down[:3] == [('sleep', 0.2), ('quiet-after-down',), ('read', ready.mapping[B])]
        assert ready.commands()[-2:] == [('down', bi), ('down', ci)]
        assert not ready.up_indexes
    else:
        assert not any(e[0] in ('read', 'command') for e in after_down)
        assert ready.up_indexes == {ci}


@pytest.mark.parametrize('failure', ['timeout', 'bad_readback'])
def test_uncertain_up_or_failed_readback_rolls_back_an_up_link(ready, failure):
    ci, _ = target_indexes(ready)
    before = ready.owned_files()
    original_submit = ready.submit
    original_read = ready.effects.read_link
    bad_once = failure == 'bad_readback'

    def submit(command, index):
        result = original_submit(command, index)
        if failure == 'timeout' and command == 'up-listen-only':
            return FakeCommand(ready, hangs=True)
        return result

    def read_link(name):
        nonlocal bad_once
        result = original_read(name)
        if result.up and bad_once:
            bad_once = False
            return replace(result, state=replace(result.state, controller_state='ERROR-WARNING'))
        return result

    ready.submit = submit
    ready.effects.read_link = read_link
    ready.run()
    reason = Refusal.CONFIGURE_FAILED if failure == 'timeout' else Refusal.VERIFICATION_FAILED
    txn = assert_failed(ready, reason, before)
    assert [m.outcome for m in txn.mutations] == [MutationOutcome.DOWN_VERIFIED]
    assert ready.commands() == [('up-listen-only', ci), ('down', ci)]
    assert not ready.up_indexes
