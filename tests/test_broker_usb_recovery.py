"""The opt-in broker hooks; no service, hardware, or production state effects."""

import time
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.can_role_resolver import SysfsCanRoleResolver
from lib.vehicle_can_roles import BOARD_A_SERIAL, BOARD_B_SERIAL
from projects.vehicle_data import active_drive, bcan_auxiliary, broker as telemetry
from projects.vehicle_data.usb_recovery_contract import ACTIVE_DRIVE, AUXILIARY_DRIVE, FLAG, OWNED_INHIBITS
from tests import test_active_drive as drive_tests
from tests.test_broker_helper_hold import FAILED, ScriptedSupervisor


ARMED = dict(type="status", state="armed_diagnostic", reason="running_gate_satisfied",
             detail="scripted helper is armed", interface_mode="armed_diagnostic", pid=1234)
CLI = ["serve", "--can-interface-mode", "dual-usbcanfd"]


class RecordingRecovery:
    def __init__(self):
        self.begun = []
        self.armed = []
        self.latched = []
        self.tick = lambda read_gates: None

    def bind(self, *, clear_latches, display_receiver):
        self.clear = clear_latches
        self.display = display_receiver

    def begin_launch(self, helper):
        self.begun.append(helper)

    def note_armed(self, helper, pid):
        self.armed.append((helper, pid))

    def note_latch(self, helper, event, payload):
        self.latched.append((helper, event, payload))

    def run_if_due(self, read_gates):
        self.tick(read_gates)


def make_broker(recovery=None, **kwargs):
    return telemetry.TelemetryBroker(acquirer=drive_tests.BrokerActiveDriveTests.Acquirer(),
                                     usb_drop_recovery=recovery, **kwargs)


def test_cli_defaults_off_and_the_four_inhibit_names_match():
    assert telemetry.build_parser().parse_args(CLI).enable_usb_drop_recovery is False
    assert telemetry.build_parser().parse_args([*CLI, FLAG]).enable_usb_drop_recovery is True
    assert telemetry.ACTIVE_DRIVE_RESTORATION_INHIBIT == active_drive.RESTORATION_INHIBIT == OWNED_INHIBITS[ACTIVE_DRIVE]
    assert telemetry.AUXILIARY_DRIVE_RESTORATION_INHIBIT == bcan_auxiliary.RESTORATION_INHIBIT == OWNED_INHIBITS[AUXILIARY_DRIVE]


@pytest.mark.parametrize("flag,helper_ok,want_recovery", [(False, False, False), (True, False, False), (True, True, True)])
def test_startup_requires_both_flag_and_verified_helper(monkeypatch, tmp_path, capsys, flag, helper_ok, want_recovery):
    from projects.vehicle_data import api, can_interfaces, display_receivers, usb_can_monitor, usb_recovery

    sys_class_net = tmp_path / "class-net"
    sys_class_net.mkdir()
    manager = can_interfaces.PassiveInterfaceManager(resolver=SysfsCanRoleResolver(sys_class_net))
    monitor = usb_can_monitor.UsbCanIncidentMonitor(sysfs_root=tmp_path, boot_id="synthetic-boot")
    monkeypatch.setattr(can_interfaces, "PassiveInterfaceManager", lambda: manager)
    monkeypatch.setattr(usb_can_monitor, "UsbCanIncidentMonitor", lambda: monitor)
    check = mock.Mock(return_value=helper_ok)
    instance = mock.Mock(return_value=None)
    monkeypatch.setattr(usb_recovery, "RecoveryEffects", lambda: SimpleNamespace(
        helper_ok=check, instance=instance, monotonic=time.monotonic,
    ))
    monkeypatch.setattr(telemetry.TelemetryBroker, "start_usb_monitor", lambda _: None)
    receiver = mock.Mock()
    monkeypatch.setattr(display_receivers, "DisplayReceivers", lambda *_: receiver)
    seen = []
    monkeypatch.setattr(api, "serve_unix", lambda broker, *_, **__: seen.append(broker))
    args = [*CLI, "--no-collector", "--no-history", "--history-db", str(tmp_path / "history.sqlite3")]
    assert telemetry.main([*args, FLAG] if flag else args) == 0
    broker, = seen
    recovery = broker.usb_drop_recovery
    assert (recovery is not None) == want_recovery
    if recovery is not None:
        assert recovery.display is receiver
        assert recovery.radar_marker() == (0, False)
        assert recovery.monitor_ids() == ()
        monkeypatch.setattr(broker.usb_can_monitor, "status_snapshot", lambda: {
            "active": [{"incident_id": "other", "affected_serials": [BOARD_B_SERIAL]},
                       {"incident_id": "current", "affected_serials": [BOARD_A_SERIAL, BOARD_B_SERIAL]}],
            "recent_incidents": [{"incident_id": "prior", "affected_serials": [BOARD_A_SERIAL]}],
        })
        assert recovery.monitor_ids() == ("current",)
    for wrapper, cls in ((broker.active_drive_supervisor, telemetry.ActiveDriveSupervisor),
                         (broker.auxiliary_drive_supervisor, telemetry.AuxiliaryDriveSupervisor)):
        delegate = wrapper.supervisor_factory(channel="can-test", event_handler=lambda _: None,
                                              expected_usb_serial="synthetic", expected_dev_id=0)
        assert isinstance(delegate, cls)
    assert instance.call_args_list == ([mock.call("can-test")] * 2 if want_recovery else [])
    assert check.call_count == int(flag)
    output = capsys.readouterr().out
    assert ("usb-drop-recovery enabled" in output) == want_recovery
    assert ("usb-drop-recovery disabled" in output) == (flag and not helper_ok)


@pytest.mark.parametrize("helper,handler_name", [
    (ACTIVE_DRIVE, "handle_active_drive_event"),
    (AUXILIARY_DRIVE, "handle_auxiliary_drive_event"),
])
@pytest.mark.parametrize("inhibit_written", [True, False])
def test_valid_armed_once_per_claim_and_exact_latch_payload(helper, handler_name, inhibit_written):
    recovery = RecordingRecovery()
    broker = make_broker(recovery)
    handle = getattr(broker, handler_name)
    payload = {"name": OWNED_INHIBITS[helper], "channel": "*", "reason": "owned"}
    assert broker._claim_helper_launch(helper)
    with pytest.raises(ValueError):
        handle({**ARMED, "reason": "invalid"})
    handle({**ARMED, "pid": True})
    handle(ARMED)
    handle(ARMED)
    assert recovery.armed == [(helper, ARMED["pid"])]
    with mock.patch.object(telemetry.can_operation_state, "begin_inhibit", return_value=payload,
                           side_effect=None if inhibit_written else OSError("unwritable")) as begin:
        handle(FAILED)
    begin.assert_called_once()
    assert begin.call_args.args == (OWNED_INHIBITS[helper],)
    expected = payload if inhibit_written else None
    assert recovery.latched == [(helper, FAILED, expected)]
    assert recovery.latched[0][2] is expected
    broker._end_helper_run(helper, FAILED)
    assert broker._claim_helper_launch(helper)
    handle({**ARMED, "pid": 2345})
    assert recovery.begun == [helper, helper]
    assert recovery.armed[-1] == (helper, 2345)


def collector_cycles(broker, cycles):
    """End the real loop at its idle wait, after exactly N complete cycles."""
    count = 0

    def idle(_interval):
        nonlocal count
        count += 1
        if count == cycles:
            broker._collector_stop.set()

    with mock.patch.object(broker._collector_stop, "wait", side_effect=idle):
        broker._collector_loop()
    assert broker.status_response()["collector"]["state"] == "stopped"
    assert count == cycles


def test_disabled_recovery_preserves_restoration_latch_and_admission(monkeypatch):
    supervisor = ScriptedSupervisor(FAILED)
    supervisor.stopped.set()
    broker = make_broker(active_drive_supervisor=supervisor, active_drive_enabled=True)
    monkeypatch.setattr(broker, "acquire", lambda *_: None)
    monkeypatch.setattr(broker, "_collect_passive_powertrain", lambda _: setattr(broker, "_passive_engine_evidence", "running"))
    monkeypatch.setattr(broker, "_update_engine_off_voltage_capture", lambda _: None)
    with mock.patch.object(telemetry.can_operation_state, "begin_inhibit") as begin:
        collector_cycles(broker, 2)
    begin.assert_called_once()
    assert supervisor.calls == 1
    assert broker.status_response()["active_drive"]["restoration_failed"] is True
    assert broker.usb_drop_recovery is None


def test_collector_runs_after_startup_assessment_keeps_latch_visible_and_retries(monkeypatch, caplog):
    order = []
    recovery = RecordingRecovery()
    supervisor = ScriptedSupervisor()
    supervisor.stopped.set()
    reconcile = {"ready": False}
    receiver = mock.Mock()
    broker = make_broker(recovery, display_receiver=receiver, active_drive_supervisor=supervisor,
                         active_drive_enabled=True,
                         interface_reconciler=SimpleNamespace(reconcile_if_needed=lambda: reconcile))
    assert recovery.display is receiver
    monkeypatch.setattr(broker, "_inhibit_armed_at_startup", lambda seen: order.append(("startup", seen)))
    monkeypatch.setattr(broker, "acquire", lambda *_: None)
    monkeypatch.setattr(broker, "_collect_passive_powertrain", lambda _: setattr(broker, "_passive_engine_evidence", "running"))
    monkeypatch.setattr(broker, "_update_engine_off_voltage_capture", lambda _: None)
    with mock.patch.object(telemetry.can_operation_state, "begin_inhibit"):
        broker.handle_active_drive_event(FAILED)

    def tick(read_gates):
        order.append("recover")
        assert read_gates().latched == {ACTIVE_DRIVE}
        assert broker.status_response()["active_drive"]["restoration_failed"] is True
        assert any("restoration" in conflict for conflict in broker._begin_wake_authorization())
        assert broker.wake_prearm_conflicts()
        assert supervisor.calls == 0
        if order.count("recover") < 3:
            raise ValueError("retry next cycle")
        recovery.clear({ACTIVE_DRIVE}, "0123456789abcdef")
        assert read_gates().latched == set()
        status = broker.status_response()["active_drive"]
        assert status["restoration_failed"] is False
        assert status["reason"] == "engine_not_running"
        assert "01234567" in status["detail"]

    recovery.tick = tick
    collector_cycles(broker, 3)
    assert order == [("startup", reconcile), "recover"] * 3
    assert supervisor.calls == 1
    assert recovery.begun == [ACTIVE_DRIVE]
    assert caplog.text.count("usb-drop-recovery unexpected error: ValueError: retry next cycle") == 1
