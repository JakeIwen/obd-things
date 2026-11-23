"""Offline kernel and broker boundaries for the USB recovery transaction tests."""

import errno
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lib import can_operation_state as operation
from lib import diagnostic_safety
from lib.canbus import InterfaceState
from lib.netdev_instance import NetdevInstance
from lib.vehicle_can_roles import CAN_ROLE_SPECS
from projects.vehicle_data import bound_link, usb_recovery_record as records
from projects.vehicle_data.usb_recovery import RecoveryEffects, UsbDropRecovery
from projects.vehicle_data.usb_recovery_contract import (
    BOARD_A_ROLES, HELPERS, OWNED_INHIBITS, IncidentState as State,
)
from projects.vehicle_data.usb_recovery_model import BrokerGates, ProcessCheck

BOOT = '00000000-0000-4000-8000-000000000001'
WALL = '2026-01-01T00:00:00+00:00'
C, B = BOARD_A_ROLES
ACTIVE, AUX = HELPERS
C_INHIBIT, B_INHIBIT = (OWNED_INHIBITS[helper] for helper in HELPERS)
QUIET = ProcessCheck(True)
ORPHAN = ProcessCheck(False, offenders=((417, 'sudo', 'sudo'),))
UNSTABLE = ProcessCheck(False, unstable=True)
SPECS = {spec.role: spec for spec in CAN_ROLE_SPECS}


def crash(*args, **kwargs):
    raise SystemExit('simulated crash')


class FakeCommand:
    stdout = None

    def __init__(self, lab, returncode=None, out='', hangs=False):
        self.lab, self.returncode, self.out, self.hangs = lab, returncode, out, hangs

    def communicate(self, *, timeout):
        if self.hangs:
            raise subprocess.TimeoutExpired('simulated setter', timeout)
        return self.out, ''

    def terminate(self):
        self.lab.trace.append(('signal', 'SIGTERM'))

    def kill(self):
        self.lab.trace.append(('signal', 'SIGKILL'))

    def wait(self):
        self.lab.trace.append(('reap',))


class RecoveryLab:
    def __init__(self, tmp_path, monkeypatch):
        self.directory = tmp_path / 'records'
        self.state_dir = tmp_path / 'operation'
        monkeypatch.setattr(operation, 'STATE_DIR', self.state_dir)
        monkeypatch.setattr(operation, 'current_boot_id', lambda: BOOT)
        monkeypatch.setattr(diagnostic_safety, 'LOCK_DIR', str(tmp_path / 'locks'))
        self.now = 100.0
        self.trace = []
        self.logs = []
        self.latched = set()
        self.gates = {}
        self.up_indexes = set()
        self.mapping = {C: 'can0', B: 'can1', 'can-ch': 'can2', 'spare': 'can3'}
        self.netdevs = {}
        self.nodes(0, 7)
        self.old = self.netdevs.copy()
        self.radar = (0, False)
        self.store = SimpleNamespace(reserve=records.reserve, reservation_exists=records.reservation_exists,
                                     write_incident=records.write_incident)
        self.effects = RecoveryEffects(
            monotonic=lambda: self.now, utc_now=lambda: WALL, sleep=self.sleep,
            boot_id=operation.current_boot_id, instance=self.netdevs.get, indexes=self.indexes,
            read_link=self.read_link, helper_ok=lambda: True, processes=lambda: self.processes(),
            ignition_flag=lambda: True,
            inhibits=operation.all_active_inhibits, end=self.end, write_topology=self.topology_write,
            exclusive=lambda name: self.lease(name, 'exclusive'), shared=lambda name: self.lease(name, 'shared'),
            up=self.up, down=self.down, identify=self.identify, snapshot=self.snapshot,
        )
        self.new_runtime()

    def new_runtime(self):
        self.latched.clear()
        self.recovery = UsbDropRecovery(self, radar_marker=lambda: self.radar, effects=self.effects,
                                        store=self.store, record_dir=self.directory, log=self.logs.append)
        self.recovery.bind(clear_latches=self.clear, display_receiver=self)

    def nodes(self, offset, devnum):
        self.netdevs.clear()
        for idx, (role, name) in enumerate(self.mapping.items(), 10):
            spec = SPECS[role]
            board_a = role in BOARD_A_ROLES
            port = '/fake/usb/board-a' if board_a else '/fake/usb/board-b'
            self.netdevs[name] = NetdevInstance(name, idx + (offset if board_a else 0),
                                               f'{port}/{spec.dev_id}/net/{name}', port, 1,
                                               devnum if board_a else 8, spec.usb_serial, spec.dev_id)
        self.up_indexes.intersection_update(self.indexes())

    def replace_board(self, *, renumber=False, devnum=9):
        if renumber:
            self.mapping = {C: 'can3', B: 'can2', 'can-ch': 'can0', 'spare': 'can1'}
        self.nodes(20, devnum)

    def indexes(self):
        return {instance.ifindex for instance in self.netdevs.values()}

    def resolution(self, role):
        name = self.mapping[role]
        device = self.netdevs.get(name)
        return SimpleNamespace(state='resolved' if device else 'missing', channel=name, spec=SPECS[role],
                               device=SimpleNamespace(sysfs_path=device.netdev_sysfs_path) if device else None)

    def topology(self):
        return self

    def sleep(self, seconds):
        self.now += seconds
        self.trace.append(('sleep', seconds))

    def read_link(self, name):
        self.trace.append(('read', name))
        inst = self.netdevs.get(name)
        up = inst is not None and inst.ifindex in self.up_indexes
        role = next(role for role, channel in self.mapping.items() if channel == name)
        state = InterfaceState(name, inst is not None, up, SPECS[role].bitrate, up,
                               'ERROR-ACTIVE' if up else 'STOPPED', 0, False, False)
        return bound_link.LinkRead(inst.ifindex if inst else None, up if inst else None, state)

    def processes(self):
        return QUIET

    def submit(self, command, index):
        if index not in self.indexes():
            return FakeCommand(self, 3, f'errno={errno.ENODEV} ENODEV\n')
        if command == 'up-listen-only':
            self.up_indexes.add(index)
        else:
            assert command == 'down'
            self.up_indexes.discard(index)
        return FakeCommand(self, 0)

    def popen(self, argv, **kwargs):
        command, index = argv[6], int(argv[7])
        self.trace.append(('command', command, index))
        return self.submit(command, index)

    def up(self, instance, bitrate):
        return bound_link.bring_up_listen_only(instance, bitrate, read_bound_state=self.effects.read_link,
                                               popen=self.popen, monotonic=self.effects.monotonic)

    def down(self, instance, *, wait_quiet):
        return bound_link.bring_down(instance, wait_quiet=wait_quiet, read_bound_state=self.effects.read_link,
                                     present_ifindexes=self.indexes, popen=self.popen,
                                     monotonic=self.effects.monotonic)

    def identify(self, channel, *, probe):
        self.trace.append(('identify', channel, probe))
        return C

    def snapshot(self, channel, **kwargs):
        self.trace.append(('rpm', channel, self.now, kwargs))
        return self.sample(800)

    @staticmethod
    def sample(rpm):
        return SimpleNamespace(frame_count=3, rpm_samples=(rpm,) * 3)

    def topology_write(self, channel, role, **kwargs):
        self.trace.append(('topology', channel, role))
        return operation.set_topology(channel, role, **kwargs)

    def end(self, expected, *, deadline_monotonic):
        self.trace.append(('end', deadline_monotonic, tuple(expected)))
        return operation.end_inhibits_if_exactly(expected, deadline_monotonic=deadline_monotonic,
                                                 monotonic=self.effects.monotonic)

    @contextmanager
    def lease(self, name, mode):
        lock = diagnostic_safety.channel_lock if mode == 'exclusive' else diagnostic_safety.channel_observer_lock
        entered = False
        try:
            with lock(name):
                entered = True
                self.trace.append(('lock', mode, name))
                yield
        finally:
            if entered:
                self.trace.append(('release', mode, name))

    def pause_passive(self):
        self.trace.append(('pause', C))
        return True

    def pause_bcan_passive(self):
        self.trace.append(('pause', B))
        return True

    def resume_passive(self):
        self.trace.append(('resume', C))

    def resume_bcan_passive(self):
        self.trace.append(('resume', B))

    def clear(self, helpers, incident_id):
        self.trace.append(('clear', helpers, incident_id))
        assert not operation.all_active_inhibits()
        self.latched.difference_update(helpers)

    def launch(self, helper):
        self.recovery.begin_launch(helper)
        name = self.mapping[BOARD_A_ROLES[HELPERS.index(helper)]]
        instance = self.netdevs[name]
        grant = SimpleNamespace(grant_attempted=True, granted_at=WALL, granted_monotonic=self.now + 1)
        factory = self.recovery.launch_recorder(helper, lambda **kwargs: SimpleNamespace(last_grant=grant))
        factory(channel=name, expected_usb_serial=instance.usb_serial, expected_dev_id=instance.dev_id)
        self.now += 2
        self.recovery.note_armed(helper, 1001 + HELPERS.index(helper))

    def latch(self, helper, reason='restoration_failed'):
        self.latched.add(helper)
        payload = operation.begin_inhibit(OWNED_INHIBITS[helper], channel='*', reason=reason)
        self.recovery.note_latch(helper, {'reason': reason, 'restored': False}, payload)
        return payload

    def incident(self, *helpers):
        for helper in helpers or HELPERS:
            self.launch(helper)
            self.latch(helper)

    def run(self):
        self.recovery.run_if_due(lambda: BrokerGates(latched=frozenset(self.latched), **self.gates))

    def persisted(self, incident_id=None):
        incident_id = incident_id or self.recovery.incident.incident_id
        return records.read_incident(self.directory / f'incident-{BOOT}-{incident_id}.json')

    def reserved(self):
        return records.reservation_exists(BOOT, directory=self.directory)

    def owned_files(self):
        return {path.name: path.read_bytes() for path in self.state_dir.glob('inhibit-*.json')}

    def events(self, kind):
        return [event for event in self.trace if event[0] == kind]

    def commands(self):
        return [event[1:] for event in self.events('command')]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return RecoveryLab(tmp_path, monkeypatch)


@pytest.fixture
def ready(lab):
    lab.incident()
    lab.replace_board()
    return lab


def assert_paused_and_resumed(lab):
    assert lab.events('pause') == [('pause', C), ('pause', B)]
    assert lab.events('resume') == [('resume', C), ('resume', B)]
    assert lab.trace[-2:] == lab.events('resume')


def assert_deferred(lab, reason, *, terminal=False):
    incident = lab.persisted()
    assert (incident.state, incident.reason) == (State.INELIGIBLE if terminal else State.WAITING, reason)
    assert incident.transaction is None
    assert not lab.commands()
    assert not lab.reserved()
    assert not lab.events('clear')


def assert_failed(lab, reason, before):
    incident = lab.persisted()
    assert (incident.state, incident.reason) == (State.FAILED, reason)
    assert lab.owned_files() == before
    assert lab.reserved()
    assert not lab.events('clear')
    assert lab.latched == {ACTIVE, AUX}
    assert_paused_and_resumed(lab)
    return incident.transaction


def target_indexes(lab):
    return tuple(lab.netdevs[lab.mapping[role]].ifindex for role in (C, B))


def assert_committed(lab, *, helpers=(ACTIVE, AUX), names=(C_INHIBIT, B_INHIBIT)):
    incident = lab.persisted()
    assert incident.state == State.COMMITTED
    assert incident.transaction.ended_inhibits == list(names)
    assert lab.events('clear') == [('clear', frozenset(helpers), incident.incident_id)]
    assert not lab.owned_files()
    assert not lab.latched
    assert lab.reserved()
    assert lab.up_indexes == set(target_indexes(lab))
    return incident
