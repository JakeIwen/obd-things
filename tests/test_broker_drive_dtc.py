"""Real supervisor startup pipes; scripted children never touch CAN or a socket."""
import threading
import time
from unittest import mock

import pytest

from projects.vehicle_data import broker as broker_module, drive_dtc_contract as contract
from projects.vehicle_data.broker import ActiveDriveSupervisor, TelemetryBroker, build_parser
from projects.vehicle_data.drive_dtc_budget import DriveDtcBudget, durable_json
from projects.vehicle_data.drive_dtc_control import DriveDtcControl, DriveDtcResults
from tests.helper_startup_support import HelperLauncher, ScriptedHelper
import tests.test_active_drive as active_drive_tests
from tests.test_drive_dtc_budget import BOOT
from tests.test_drive_dtc_control import Budget, FINAL, Supervisor, pass_event, read_event


def broker(control):
    return TelemetryBroker(acquirer=active_drive_tests.BrokerActiveDriveTests.Acquirer(), drive_dtc=control)


def supervisor(control, launcher, handler):
    return ActiveDriveSupervisor(channel='can8', expected_usb_serial='scripted', expected_dev_id=0,
                                 event_handler=handler, popen_factory=launcher, drive_dtc=control,
                                 queue_poll_seconds=0.002)


def test_broker_cli_opt_in_defaults_off():
    parser = build_parser()
    args = ['serve', '--can-interface-mode', 'dual-usbcanfd']
    assert not parser.parse_args(args).enable_drive_dtc_reads
    assert parser.parse_args(args + ['--enable-drive-dtc-reads']).enable_drive_dtc_reads


def test_missing_capability_cancels_before_grant_reaps_and_never_inhibits(tmp_path):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget, monotonic=lambda: 1000)
    owner = broker(control)
    launcher = HelperLauncher(on_spawn=lambda spawn: spawn.process.ready())
    active = supervisor(control, launcher, owner.handle_active_drive_event)
    try:
        with mock.patch.object(broker_module.can_operation_state, 'begin_inhibit') as inhibit:
            final = control.run(active, threading.Event())
            owner.handle_active_drive_event(final)
            assert final['restored'] is None
            assert final['reason'] == 'engine_not_running'
            assert launcher.process.poll() is not None
            assert launcher.read_grant() == b''
            assert '--enable-drive-dtc' in launcher.commands[0]
            assert not control.available
            assert budget.ends
            inhibit.assert_not_called()
    finally:
        launcher.close()
        control.close(2)


@pytest.mark.parametrize('events,receipt', [
    ([pass_event(399)], 1000),
    ([pass_event(700)], 699),
    ([pass_event(1000), pass_event(4000, 1)], 5000),
])
def test_bad_schedule_ordinary_supervisor_stop_retains_verified_final(tmp_path, events, receipt):
    budget = Budget()
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget, monotonic=lambda: receipt)
    owner = broker(control)
    child = ScriptedHelper(die_on_terminate=False)
    launcher = HelperLauncher(child, on_spawn=lambda spawn: spawn.process.ready(capabilities=[contract.CAPABILITY]))
    active = supervisor(control, launcher, owner.handle_active_drive_event)
    outputs = []
    thread = threading.Thread(target=lambda: outputs.append(control.run(active, threading.Event())), daemon=True)
    try:
        with mock.patch.object(broker_module.can_operation_state, 'begin_inhibit') as inhibit:
            thread.start()
            assert launcher.spawned.wait(2)
            assert launcher.read_grant() == b'G'
            for event in events:
                child.emit(event)
            assert child.terminated.wait(2)
            child.emit(FINAL)
            child.die()
            thread.join(3)
            assert not thread.is_alive() and outputs == [FINAL]
            owner.handle_active_drive_event(outputs[0])
            assert not control.available
            assert child.kill_calls == 0
            inhibit.assert_not_called()
    finally:
        child.die()
        thread.join(3)
        launcher.close()
        control.close(2)


def test_valid_window_edge_delivered_late_does_not_stop_helper(tmp_path):
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=Budget(), monotonic=lambda: 2000)
    owner = broker(control)
    child = ScriptedHelper(die_on_terminate=False)
    processed = threading.Event()
    def handle(event):
        owner.handle_active_drive_event(event)
        processed.set()
    launcher = HelperLauncher(child, on_spawn=lambda spawn: spawn.process.ready(capabilities=[contract.CAPABILITY]))
    active = supervisor(control, launcher, handle)
    outputs = []
    thread = threading.Thread(target=lambda: outputs.append(control.run(active, threading.Event())), daemon=True)
    try:
        thread.start()
        assert launcher.spawned.wait(2) and launcher.read_grant() == b'G'
        child.emit(pass_event(1000))
        assert processed.wait(2)
        assert child.terminate_calls == 0 and control.available
        child.emit(FINAL)
        child.die()
        thread.join(3)
        assert outputs == [FINAL]
    finally:
        child.die()
        thread.join(3)
        launcher.close()
        control.close(2)


def test_flag_off_real_supervisor_uses_legacy_ready_without_new_arguments():
    control = DriveDtcControl()
    child = ScriptedHelper()
    launcher = HelperLauncher(child, on_spawn=lambda spawn: spawn.process.ready())
    active = supervisor(control, launcher, lambda event: None)
    outputs = []
    thread = threading.Thread(target=lambda: outputs.append(control.run(active, threading.Event())), daemon=True)
    try:
        thread.start()
        assert launcher.spawned.wait(2) and launcher.read_grant() == b'G'
        child.emit(FINAL)
        child.die()
        thread.join(3)
        assert outputs == [FINAL]
        assert not any(arg.startswith('--drive-dtc') or arg == '--enable-drive-dtc'
                       for arg in launcher.commands[0])
    finally:
        child.die()
        thread.join(3)
        launcher.close()


def test_blocked_budget_launches_plain_within_total_two_second_wait(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocked(path, payload):
        entered.set()
        assert release.wait(6)
        durable_json(path, payload)
    budget = DriveDtcBudget(tmp_path, boot_id=lambda: BOOT, monotonic=lambda: 100, write=blocked)
    control = DriveDtcControl(enabled=True, directory=tmp_path, budget=budget)
    try:
        def run(stop):
            assert entered.is_set()
            assert control.launch_arguments() == []
            return FINAL
        start = time.monotonic()
        assert control.run(Supervisor(run), threading.Event()) == FINAL
        assert 1.9 <= time.monotonic() - start < 2.8
    finally:
        release.set()
        control.close(2)


def test_blocked_results_never_hold_broker_lock_or_delay_status(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocked(path, payload):
        entered.set()
        assert release.wait(6)
    results = DriveDtcResults(tmp_path, write=blocked)
    control = DriveDtcControl(enabled=True, budget=Budget(), results=results, monotonic=lambda: 1000)
    owner = broker(control)
    try:
        def run(stop):
            owner.handle_active_drive_event(read_event())
            assert entered.wait(2)
            start = time.monotonic()
            owner.handle_active_drive_event(read_event(module='bcm_ccan'))
            status = owner.status_response()
            assert isinstance(status, dict)
            assert time.monotonic() - start < 0.2
            return FINAL
        assert control.run(Supervisor(run), threading.Event()) == FINAL
    finally:
        release.set()
        control.close(2)


def test_main_places_budget_results_and_harvest_beside_dtc_cache_not_history(tmp_path):
    import contextlib
    from pathlib import Path
    from projects.vehicle_data import can_interfaces, can_runtime, display_receivers, api
    from projects.vehicle_data import usb_can_monitor
    history = tmp_path / 'history' / 'history.sqlite3'
    cache = tmp_path / 'dtc' / 'dtc-cache.json'
    # No runtime constructor, socket, thread, or monitor escapes this CLI test.
    owner = mock.Mock()
    options = []
    class RecordingControl(DriveDtcControl):
        def __init__(self, **kwargs):
            options.append(kwargs)
    with contextlib.ExitStack() as stack:
        for module, names in [
            (can_interfaces, ['PassiveInterfaceManager']),
            (can_runtime, ['PassiveRoleReconciler', 'RoleAwareVoltageAcquirer',
                           'RoleAwareCcanPowertrainReader', 'RoleAwareActiveDriveSupervisor',
                           'RoleAwareAuxiliaryDriveSupervisor']),
            (display_receivers, ['DisplayReceivers']),
            (usb_can_monitor, ['UsbCanIncidentMonitor']),
            (broker_module, ['EngineOffVoltageCapture', 'MaintenanceStore', 'AlignmentHistory', 'LastReadings']),
        ]:
            for name in names:
                stack.enter_context(mock.patch.object(module, name))
        stack.enter_context(mock.patch.object(broker_module, 'DriveDtcControl', RecordingControl))
        stack.enter_context(mock.patch.object(broker_module, 'TelemetryBroker', return_value=owner))
        stack.enter_context(mock.patch.object(api, 'serve_unix'))
        stack.enter_context(mock.patch.object(broker_module.diagnostic_safety,
            'interrupt_on_termination', return_value=contextlib.nullcontext(mock.Mock())))
        assert broker_module.main(['serve', '--can-interface-mode', 'dual-usbcanfd',
                                   '--no-history', '--no-collector', '--history-db', str(history),
                                   '--dtc-cache', str(cache), '--enable-drive-dtc-reads']) == 0
        assert options == [{'enabled': True, 'directory': Path(cache).parent}]
    assert not list(tmp_path.iterdir())
