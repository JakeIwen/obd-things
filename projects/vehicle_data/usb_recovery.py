"""Collector-owned recovery of this process's restoration latches. Never resumes an old incident."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lib import can_operation_state as operation
from lib import canbus, diagnostic_safety, netdev_instance
from lib.can_runtime_route import LinkExpectation
from lib.timeutil import utc_now
from lib.vehicle_can_roles import ALL_CAN_ROLES
from projects.vehicle_data import active_session_gates, bound_link, ccan_powertrain, service_cgroup
from projects.vehicle_data import usb_recovery_contract as contract
from projects.vehicle_data import usb_recovery_model as model
from projects.vehicle_data import usb_recovery_record
from projects.vehicle_data.usb_recovery_contract import IncidentState as State, MutationOutcome, Refusal


def _disk_record(name):
    # Read the canonical *pathname*, not a record that merely claims this name.
    if name not in contract.OWNED_INHIBITS.values():
        raise ValueError('not an owned inhibit')
    try:
        value = json.loads((operation.STATE_DIR / f'inhibit-{name}.json').read_bytes())
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError, OSError):
        return {'invalid': True}
    return value if isinstance(value, dict) else {'invalid': True}


def ignition_flag_present():
    # Match the producer's test -f. Its last positive can outlive the producer.
    return os.path.isfile(contract.IGNITION_FLAG_PATH)


@dataclass
class RecoveryEffects:
    monotonic: Callable = time.monotonic
    utc_now: Callable = utc_now
    sleep: Callable = time.sleep
    boot_id: Callable = operation.current_boot_id
    instance: Callable = netdev_instance.read_instance
    indexes: Callable = netdev_instance.present_ifindexes
    read_link: Callable = bound_link.read_bound_state
    helper_ok: Callable = bound_link.link_helper_ok
    ignition_flag: Callable = ignition_flag_present
    processes: Callable = service_cgroup.inspect
    inhibits: Callable = operation.all_active_inhibits
    disk_record: Callable = _disk_record
    end: Callable = operation.end_inhibits_if_exactly
    write_topology: Callable = operation.set_topology
    exclusive: Callable = diagnostic_safety.channel_lock
    shared: Callable = diagnostic_safety.channel_observer_lock
    up: Callable = bound_link.bring_up_listen_only
    down: Callable = bound_link.bring_down
    identify: Callable = canbus.identify_bus
    snapshot: Callable = ccan_powertrain.read_broadcast_snapshot


class Rejected(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(detail)
        self.reason = reason


class UsbDropRecovery:
    def __init__(self, interface_manager, *, radar_marker=lambda: (0, False), monitor_ids=lambda: (),
                 effects=None, store=usb_recovery_record, record_dir=contract.RECORD_DIR, log=None):
        self.manager = interface_manager
        self.radar_marker = radar_marker
        self.monitor_ids = monitor_ids
        self.fx = effects or RecoveryEffects()
        self.store = store
        self.directory = Path(record_dir)
        self._log_sink = log
        self.broker_instance = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._slots = {}
        self._delegates = {}
        self._revision = 0
        self.incident = None
        self._disk_commit_started = False
        self.display = None
        self.clear_latches = None

    def log(self, message, *, error=False):
        # The unit's stdout is a journal socket. Its priority prefix is parsed by systemd.
        try:
            if self._log_sink is None:
                print(('<3>ERROR ' if error else '') + message, flush=True)
            else:
                self._log_sink(('ERROR ' if error else '') + message)
            return True
        except Exception:
            return False  # Diagnostics cannot interrupt restoration or its durable record.

    @classmethod
    def if_enabled(cls, enabled, interface_manager, **kwargs):
        if not enabled:
            return None
        recovery = cls(interface_manager, **kwargs)
        if not recovery.fx.helper_ok():
            recovery.log('usb-drop-recovery disabled: link helper not installed or differs')
            return None
        recovery.log(f'usb-drop-recovery enabled (once per boot, window {contract.RECOVERY_WINDOW_SECONDS} s)')
        return recovery

    def bind(self, *, clear_latches, display_receiver):
        self.clear_latches = clear_latches
        self.display = display_receiver

    def _marker(self):
        try:
            return self.radar_marker()
        except Exception:
            return None

    def _invalidate(self, helper):
        # No injected effects (including clocks, files or logging) on this fallback path.
        slot = model.HelperRecord(contract.HELPER_ROLES[helper], ambiguous=True)
        self._slots[helper] = slot
        self._delegates.pop(helper, None)
        if self.incident is not None and helper in self.incident.helpers and self.incident.state not in contract.TERMINAL_STATES:
            self.incident.helpers[helper] = slot
        self._revision += 1

    def capture(self, hook, helper, *args):
        """Broker boundary: the helper's claim, real factory and finalizer do not depend on capture."""
        try:
            return hook(helper, *args)
        except Exception as exc:
            with self._lock:
                self._invalidate(helper)
            self.log(f'usb-drop-recovery capture unavailable helper={helper} ({type(exc).__name__})', error=True)
            return None

    def begin_launch(self, helper):
        marker = self.radar_marker() if helper == contract.ACTIVE_DRIVE else None
        with self._lock:
            self._revision += 1
            slot = model.HelperRecord(contract.HELPER_ROLES[helper], launch_seq=self._revision,
                                      claimed_monotonic=self.fx.monotonic())
            if helper == contract.ACTIVE_DRIVE:
                slot.radar_at_claim = marker if marker is not None else contract.RadarCondition.UNCERTAIN
            self._slots[helper] = slot
            self._delegates.pop(helper, None)

    def launch_recorder(self, helper, factory):
        def construct(**kwargs):
            delegate = factory(**kwargs)  # Real factory failures still belong to the supervisor.
            self.capture(self._note_factory, helper, kwargs, delegate)
            return delegate
        return construct

    def _note_factory(self, helper, kwargs, delegate):
        channel = kwargs['channel']
        launch = model.LaunchRecord(channel, kwargs['expected_usb_serial'], kwargs['expected_dev_id'],
                                    self.fx.instance(channel), self.fx.monotonic())
        with self._lock:
            slot = self._slots.get(helper)
            if slot is not None and not slot.ambiguous:
                if slot.launch is not None:
                    slot.ambiguous = True
                else:
                    slot.launch = launch
                    self._delegates[helper] = delegate
                self._revision += 1

    def note_armed(self, helper, pid):
        with self._lock:
            slot = self._slots.get(helper)
            if slot is not None and not slot.ambiguous and slot.armed is None:
                instance = self.fx.instance(slot.launch.channel) if slot.launch else None
                slot.armed = model.ArmedRecord(instance, self.fx.monotonic())
                slot.helper_pid = pid
                self._revision += 1

    def note_latch(self, helper, event, payload):
        with self._lock:
            self._revision += 1
            slot = self._slots.setdefault(helper, model.HelperRecord(contract.HELPER_ROLES[helper]))
            grant = getattr(self._delegates.get(helper), 'last_grant', None)
            if grant is not None:
                slot.grant = model.GrantRecord(grant.grant_attempted, grant.granted_at, grant.granted_monotonic)
            now, wall = self.fx.monotonic(), self.fx.utc_now()
            slot.note_latch(model.LatchRecord(wall, now, event.get('reason'), event.get('restored'),
                                              str(event.get('detail', ''))), payload if isinstance(payload, dict) else None)
            i = self.incident
            opening = i is None or i.state in contract.TERMINAL_STATES
            if opening:
                boot = self.fx.boot_id()
                i = model.Incident(uuid.uuid4().hex, boot, os.getpid(), self.broker_instance, wall, now, wall)
                self.incident = i
                if not boot:
                    i.state, i.reason = State.INELIGIBLE, Refusal.BOOT_ID_UNAVAILABLE
                else:
                    try:
                        spent = self.store.reservation_exists(boot, directory=self.directory)
                    except (OSError, ValueError):
                        i.state, i.reason = State.INELIGIBLE, Refusal.INCIDENT_RECORD_UNWRITTEN
                    else:
                        if spent:
                            i.state, i.reason = State.INELIGIBLE, Refusal.BOOT_BUDGET_SPENT
            i.helpers[helper] = slot
            if not self._save_before_reservation(i, announce=opening):
                self._invalidate(helper)

    def _save(self, i, *, announce=True):
        i.updated_at = self.fx.utc_now()
        logged = True
        if announce:
            logged = self.log(f'{i.updated_at} usb-drop-recovery incident={i.incident_id[:8]} state={i.state} '
                     f'reason={i.reason or "-"} detail={i.detail}', error=i.state == State.COMMITTED_LATE)
        self.store.write_incident(i, directory=self.directory)
        return logged

    def _save_before_reservation(self, i, *, announce=True):
        try:
            return self._save(i, announce=announce)
        except Exception as exc:
            i.state = State.INELIGIBLE
            i.reason = Refusal.INCIDENT_RECORD_UNWRITTEN if i.boot_id else Refusal.BOOT_ID_UNAVAILABLE
            self.log(f'usb-drop-recovery incident={i.incident_id[:8]} state={i.state} reason={i.reason} ({type(exc).__name__})')

    def _before(self, i, decision, revision):
        with self._lock:
            # An auxiliary thread can finish late. Never close its newer record on an older observation.
            if self.incident is not i or self._revision != revision:
                return False
            if isinstance(decision, model.Proceed):
                return True
            state = State.WAITING if isinstance(decision, model.Wait) else State.INELIGIBLE
            if (i.state, i.reason) != (state, decision.reason):
                i.state, i.reason = state, decision.reason
                self._save_before_reservation(i)
        return False

    def _resolve(self):
        topology = self.manager.topology()
        found = {}
        for role in contract.BOARD_A_ROLES:
            res = topology.resolution(role)
            instance = self.fx.instance(res.channel) if res.state == 'resolved' else None
            if instance and (instance.usb_serial != res.spec.usb_serial or instance.dev_id != res.spec.dev_id
                             or instance.netdev_sysfs_path != res.device.sysfs_path or instance.channel != res.channel):
                instance = None
            found[role] = (res, instance)
        return found

    def _observation(self, i, gates, resolved, *, stable=True, commit=False):
        board = {}
        for role, (res, instance) in resolved.items():
            link = None
            if instance:
                read = self.fx.read_link(instance.channel)
                passive = LinkExpectation(res.spec.bitrate, True, channel=instance.channel).matches(read.state)
                link = model.LinkSeen(read.ifindex, read.state.present, read.up, passive)
            board[role] = model.RoleNow(res.state, instance, link)
        proc = self.fx.processes()
        for _ in range(contract.COMMIT_PROCESS_RETRIES if commit else 0):
            if not proc.unstable or proc.unverifiable or proc.offenders:
                break
            self.fx.sleep(0.1)
            proc = self.fx.processes()
        marker = self._marker()
        boot = self.fx.boot_id()
        disk = {name: self.fx.disk_record(name) for name in contract.OWNED_INHIBITS.values()}
        return model.Observation(boot, bool(boot and self.store.reservation_exists(boot, directory=self.directory)),
                                 self._gates(gates), marker, proc, self.fx.inhibits(), disk, self.fx.indexes(), board,
                                 self.fx.helper_ok(), ignition_flag=self.fx.ignition_flag(), topology_stable=stable)

    def _assess(self, i, gates, resolved, revision, *, stable=True):
        obs = self._observation(i, gates, resolved, stable=stable)
        with self._lock:
            if self._revision != revision:
                return False
            i.radar_condition = model.radar_condition(i, obs.radar_marker)
            decision = model.assess(i, obs, self.fx.monotonic())
        return self._before(i, decision, revision)

    def _resume_display(self):
        if self.display is not None:
            try:
                self.display.resume_passive()
            finally:
                self.display.resume_bcan_passive()

    def _gates(self, read):
        try:
            return read()
        except Exception as exc:
            with self._lock:
                for helper in contract.HELPERS:
                    self._invalidate(helper)
            self.log(f'usb-drop-recovery broker gates unavailable ({type(exc).__name__})', error=True)
            return model.BrokerGates(stopping=True)

    def run_if_due(self, read_gates):
        with self._lock:
            i, revision = self.incident, self._revision
            if i is None or i.state in contract.TERMINAL_STATES:
                return
        if self._gates(read_gates).stopping or not self._assess(i, read_gates, self._resolve(), revision):
            return
        admitted = [model.StepRecord('precheck', self.fx.utc_now(), True)]
        try:
            if self.display is not None:
                c = self.display.pause_passive()
                b = self.display.pause_bcan_passive()
                if not (c and b):
                    self._before(i, model.Wait(Refusal.DISPLAY_BUSY), revision)
                    return
            admitted.append(model.StepRecord('display_paused', self.fx.utc_now(), True))
            with ExitStack() as locks:
                for role in ALL_CAN_ROLES:
                    lock = self.fx.exclusive if role in contract.BOARD_A_ROLES else self.fx.shared
                    locks.enter_context(lock(f'can-role-{role}'))
                before = self._resolve()
                for _, instance in before.values():
                    if instance is None:
                        self._before(i, model.Wait(Refusal.ROLE_UNRESOLVED), revision)
                        return
                    locks.enter_context(self.fx.exclusive(instance.channel))
                after = self._resolve()
                if self._assess(i, read_gates, after, revision, stable=before == after):
                    admitted.append(model.StepRecord('locked_precheck', self.fx.utc_now(), True))
                    self._reserve_and_run(i, read_gates, after, revision, admitted)
        except diagnostic_safety.ChannelLockError:
            self._before(i, model.Wait(Refusal.LOCKS_BUSY), revision)
        finally:
            self._resume_display()

    def _reserve_and_run(self, i, gates, resolved, revision, admitted):
        wall = self.fx.utc_now()
        try:
            self.store.reserve(i.boot_id, i.incident_id, broker_pid=i.broker_pid, reserved_at=wall, directory=self.directory)
        except FileExistsError:
            self._before(i, model.GiveUp(Refusal.BOOT_BUDGET_SPENT), revision)
            return
        except Exception:
            self._before(i, model.GiveUp(Refusal.RESERVATION_FAILED), revision)
            return
        self._disk_commit_started = False
        i.transaction = model.TransactionRecord(wall, {r: inst for r, (_, inst) in resolved.items()}, steps=admitted)
        try:
            self._step(i, 'reserve')
            try:
                i.transaction.usb_monitor_incident_ids = list(self.monitor_ids())[:8]
            except Exception:
                pass  # Correlation only, never evidence for recovery.
            self._phase(i, State.RESERVED)
            c, b = contract.BOARD_A_ROLES
            self._configure(i, c, resolved[c], State.CONFIGURING)
            self._engine(i, identify=True)
            self._configure(i, b, resolved[b], State.VERIFYING)
            self._topology(i, resolved)
            obs = self._observation(i, gates, self._resolve(), commit=True)
            i.radar_condition = model.radar_condition(i, obs.radar_marker)
            refusal = model.commit_check(i, i.transaction.board_a, obs)
            if refusal:
                raise Rejected(refusal)
            self._step(i, 'commit_check')
            self._phase(i, State.COMMITTING)
            self._commit(i)
        except Exception as exc:
            if self._disk_commit_started:
                if i.state not in contract.TERMINAL_STATES:
                    self._finish(i, State.COMMIT_OUTCOME_UNKNOWN, (), None)
            else:
                self._rollback(i, exc)

    def _step(self, i, name, ok=True, detail=''):
        i.transaction.steps.append(model.StepRecord(name, self.fx.utc_now(), ok, detail))

    def _phase(self, i, state):
        i.state, i.reason, i.detail = state, None, ''
        try:
            self._save(i)
        except Exception as exc:
            raise Rejected(Refusal.INCIDENT_RECORD_UNWRITTEN) from exc

    def _configure(self, i, role, target, state):
        res, instance = target
        mutation = model.MutationRecord(role, instance.ifindex, self.fx.utc_now())
        i.transaction.mutations.append(mutation)
        try:
            self._phase(i, state)
            if not self.fx.helper_ok():
                raise Rejected(Refusal.LINK_HELPER_UNVERIFIED)
        except Exception:
            mutation.outcome = MutationOutcome.NOT_SUBMITTED
            raise
        result = self.fx.up(instance, res.spec.bitrate)
        mutation.outcome = result.outcome
        self._step(i, f'configure_{role}', result.refusal is None, str(result.outcome))
        if result.refusal is not None:
            raise Rejected(result.refusal)

    def _engine(self, i, *, identify=False):
        instance = i.transaction.board_a[contract.BOARD_A_ROLES[0]]
        t0 = self.fx.monotonic()
        if self.fx.read_link(instance.channel).ifindex != instance.ifindex:
            raise Rejected(Refusal.INSTANCE_CHANGED)
        if identify:
            bus = self.fx.identify(instance.channel, probe=0.25, subprocess_timeout=contract.IP_TIMEOUT_SECONDS,
                                   monotonic=self.fx.monotonic)
            self._step(i, 'identify_bus', bus == contract.BOARD_A_ROLES[0], str(bus))
            if bus != contract.BOARD_A_ROLES[0]:
                raise Rejected(Refusal.WRONG_BUS)
        try:
            for _ in range(contract.RUNNING_CHECK_ATTEMPTS if identify else 1):
                snap = self.fx.snapshot(instance.channel, timeout=0.5, required_rpm_samples=active_session_gates.REQUIRED_RPM_SAMPLES)
                if active_session_gates.running_snapshot(snap):
                    break
            else:
                raise Rejected(Refusal.ENGINE_NOT_RUNNING)
        except OSError as exc:
            raise Rejected(Refusal.ENGINE_NOT_RUNNING) from exc
        if self.fx.read_link(instance.channel).ifindex != instance.ifindex:
            raise Rejected(Refusal.INSTANCE_CHANGED)
        i.transaction.engine_seen_monotonic = t0
        self._step(i, 'engine_running')
        return t0 + contract.ENGINE_EVIDENCE_MAX_AGE_SECONDS

    def _topology(self, i, resolved):
        for role, (res, instance) in resolved.items():
            spec = res.spec
            try:
                self.fx.write_topology(instance.channel, role, pair=str(spec.pair), source='usb_serial_and_dev_id',
                                       note=f'{spec.board} {spec.connector}; serial-resolved dual USBCANFD role')
            except Exception as exc:
                raise Rejected(Refusal.TOPOLOGY_RECORD_FAILED) from exc
            self._step(i, f'topology_{role}')

    def _commit(self, i):
        expected = model.owned_inhibits(i)
        for _ in range(2):
            deadline = self._engine(i)
            self._disk_commit_started = True
            try:
                result = self.fx.end(expected, deadline_monotonic=deadline)
            except Exception:
                # No rollback on an uncertain commit unless every canonical file is positively intact.
                try:
                    actual = {n: self.fx.disk_record(n) for n in expected}
                except Exception:
                    actual = {}
                if actual == expected:
                    self._disk_commit_started = False
                    raise Rejected(Refusal.UNEXPECTED_ERROR)
                missing = [n for n in expected if n in actual and actual[n] is None]
                partial = missing and len(missing) < len(expected) and all(
                    actual[n] == v for n, v in expected.items() if n not in missing)
                self._finish(i, State.PARTIAL_COMMIT if partial else State.COMMIT_OUTCOME_UNKNOWN, missing, None)
                return
            if result.removed:
                state = State.COMMITTED if tuple(result.removed) == tuple(expected) else State.PARTIAL_COMMIT
                if result.late:
                    state = State.COMMITTED_LATE
                    i.detail = result.detail
                self._finish(i, state, result.removed, result.durable)
                return
            self._disk_commit_started = False
            self._step(i, 'end_inhibits', False, str(result.outcome))
            if result.outcome != 'expired':
                raise Rejected(Refusal.COMMIT_REFUSED, result.detail)
        raise Rejected(Refusal.ENGINE_EVIDENCE_STALE)

    def _finish(self, i, state, removed, durable):
        # Post-unlink code must never unwind into rollback, even if publication or the callback fails.
        i.state, i.reason = state, None
        try:
            i.transaction.ended_inhibits = list(removed)
            i.transaction.durable = durable
            if state == State.COMMITTED:
                try:
                    self.clear_latches(model.latched_helpers(i), i.incident_id)
                except Exception:
                    i.state = State.COMMITTED_LATCHES_KEPT
            self._step(i, 'end_inhibits', i.state == State.COMMITTED)
            self._save(i)
        except Exception as exc:
            self.log(f'usb-drop-recovery incident={i.incident_id[:8]} state={i.state} '
                     f'post-commit record failed: {type(exc).__name__}', error=True)

    def _quiet(self):
        deadline = self.fx.monotonic() + contract.SUBMITTER_WAIT_SECONDS
        while True:
            p = self.fx.processes()
            if p.quiet and not (p.unverifiable or p.unstable or p.offenders):
                return True
            if self.fx.monotonic() >= deadline:
                return False
            self.fx.sleep(0.2)

    def _rollback_quiet(self):
        try:
            return self._quiet()
        except Exception:
            return False

    def _rollback_step(self, i, name, ok, detail):
        try:
            self._step(i, name, ok, detail)
        except Exception:
            self.log(f'usb-drop-recovery could not record {name}', error=True)

    def _rollback(self, i, exc):
        i.state = State.FAILED
        i.reason = exc.reason if isinstance(exc, Rejected) else Refusal.UNEXPECTED_ERROR
        i.detail = str(exc)[:240] if isinstance(exc, Rejected) else type(exc).__name__
        self._rollback_step(i, 'abort', False, str(i.reason))
        mutations = [m for m in reversed(i.transaction.mutations)
                     if m.outcome not in (MutationOutcome.NOT_SUBMITTED, MutationOutcome.KERNEL_REFUSED)]
        quiet = bool(mutations) and self._rollback_quiet()
        for n, m in enumerate(mutations):
            inst = i.transaction.board_a[m.role]
            outcome = MutationOutcome.ROLLBACK_UNVERIFIED
            if quiet:
                try:
                    read = self.fx.read_link(inst.channel)
                    if read.ifindex == inst.ifindex and read.state.present and read.up is False:
                        outcome = MutationOutcome.DOWN_VERIFIED
                    elif (indexes := self.fx.indexes()) is not None and inst.ifindex not in indexes:
                        outcome = MutationOutcome.INSTANCE_GONE
                    elif self.fx.helper_ok():
                        outcome = self.fx.down(inst, wait_quiet=self._quiet)
                except Exception:
                    pass  # An incomplete read never verifies a rollback.
            if outcome == MutationOutcome.ROLLBACK_UNVERIFIED:
                self.log(f'usb-drop-recovery incident={i.incident_id[:8]} rollback_unverified role={m.role}', error=True)
                # Re-establish quiescence before the next instance: this down might have a survivor.
                if quiet and n + 1 < len(mutations):
                    quiet = self._rollback_quiet()
            m.outcome = outcome
            self._rollback_step(i, f'rollback_{m.role}', outcome != MutationOutcome.ROLLBACK_UNVERIFIED, str(outcome))
        try:
            self._save(i)
        except Exception as failure:
            self.log(f'usb-drop-recovery incident={i.incident_id[:8]} failure record unwritten: {type(failure).__name__}')
