"""Drive a real broker collector and recovery against a synthetic kernel."""

import os
from functools import partial
from subprocess import CompletedProcess
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from lib import can_operation_state as operation, canbus, diagnostic_safety, netdev_instance
from lib.can_role_resolver import SysfsCanRoleResolver
from lib.vehicle_can_roles import BOARD_A_SERIAL, BOARD_B_SERIAL
from projects.vehicle_data import bound_link, broker as telemetry, ccan_powertrain, service_cgroup, usb_recovery_probe
from projects.vehicle_data.can_interfaces import PassiveInterfaceManager
from projects.vehicle_data.can_runtime import (
    PassiveRoleReconciler, RoleAwareActiveDriveSupervisor, RoleAwareAuxiliaryDriveSupervisor,
)
from projects.vehicle_data.models import success
from projects.vehicle_data.usb_recovery import RecoveryEffects, UsbDropRecovery
from projects.vehicle_data.usb_recovery_contract import ACTIVE_DRIVE, AUXILIARY_DRIVE, IncidentState, Refusal
from tests.helper_startup_support import HelperLauncher, ScriptedHelper, WAIT_SECONDS
from tests.test_broker_helper_hold import FAILED, RESTORED
from tests.test_broker_usb_recovery import ARMED
from tests.test_can_role_resolver import FakeSysfs
from tests.test_ccan_powertrain import FakeSocket, frame


_BOOT = "19a832ac-dc5f-47aa-9d89-d1dc35850e6d"
_CCAN, _BCAN = "can0", "can1"
_RPM = frame(0x0FC, b"\x0b\xb8")  # 750 RPM


class ReplayKernel:
    def __init__(self, root, monkeypatch):
        self.sysfs = FakeSysfs(root)
        self.channels = {}
        self.commands = []
        self.clock_offset = 0.0
        self.ccan = _CCAN
        self.board_a = self.sysfs.add_usb_board("board-a", BOARD_A_SERIAL)
        board_b = self.sysfs.add_usb_board("board-b", BOARD_B_SERIAL)
        self._configure_board(self.board_a, 20)
        self._configure_board(board_b, 21)
        self.add_channel(self.board_a, _CCAN, 0, 10, 500000, up=True)
        self.add_channel(self.board_a, _BCAN, 1, 11, 125000, up=True)
        self.add_channel(board_b, "can4", 0, 12, 500000, up=True)
        self.add_channel(board_b, "can5", 1, 13, 500000, up=False)
        self.proc = root / "proc"
        self.cgroup = root / "cgroup"
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "self/cgroup").write_text("0::/system.slice/van-telemetry.service\n")
        group = self.cgroup / "system.slice/van-telemetry.service"
        group.mkdir(parents=True)
        (group / "cgroup.procs").write_text(f"{os.getpid()}\n")
        monkeypatch.setattr(operation, "STATE_DIR", root / "operation-state")
        monkeypatch.setattr(operation, "current_boot_id", lambda: _BOOT)
        monkeypatch.setattr(diagnostic_safety, "LOCK_DIR", str(root / "locks"))
        monkeypatch.setattr(canbus, "rx_errors", lambda _, **kwargs: 0)
        monkeypatch.setattr(canbus, "socket", SimpleNamespace(
            AF_CAN=29, SOCK_RAW=3, CAN_RAW=1, timeout=TimeoutError,
            socket=lambda *_: FakeSocket([frame(0x100, b"\0\0")]),
        ))

    @staticmethod
    def _configure_board(board, devnum):
        (board / "busnum").write_text("1\n")
        (board / "devnum").write_text(f"{devnum}\n")

    def add_channel(self, board, name, dev_id, index, bitrate, *, up):
        self.sysfs.add_channel(board, name, dev_id)
        entry = self.sysfs.class_net / name
        physical = board / f"interface-{name}" / "net" / name
        physical.parent.mkdir()
        entry.rename(physical)
        entry.symlink_to(physical)
        (physical / "ifindex").write_text(f"{index}\n")
        self.channels[name] = (index, up, bitrate)

    def unplug(self):
        for name in (_CCAN, _BCAN):
            (self.sysfs.class_net / name).unlink()
            del self.channels[name]

    def replug(self):
        self._configure_board(self.board_a, 22)
        self.ccan = "can3"
        self.add_channel(self.board_a, self.ccan, 0, 30, 500000, up=False)
        self.add_channel(self.board_a, "can2", 1, 31, 125000, up=False)

    def run(self, argv, **_kwargs):
        assert argv[:4] == ["ip", "-details", "link", "show"]
        name = argv[4]
        if name not in self.channels:
            return CompletedProcess(argv, 1, "", "missing")
        index, up, bitrate = self.channels[name]
        flags = "NOARP,UP,LOWER_UP" if up else "NOARP"
        return CompletedProcess(argv, 0, f"{index}: {name}: <{flags}> mtu 16\n"
                               f"    can <LISTEN-ONLY> state ERROR-ACTIVE restart-ms 0\n"
                               f"    bitrate {bitrate}\n", "")

    def popen(self, argv, **_kwargs):
        assert argv[:3] == ["sudo", "-n", "/usr/bin/python3"]
        assert argv[6] == "up-listen-only", argv
        index, bitrate = map(int, argv[7:9])
        name, = (key for key, cfg in self.channels.items() if cfg[0] == index)
        assert self.channels[name] == (index, False, bitrate)
        assert argv[9] == "--not-after"
        self.commands.append((index, bitrate))
        self.channels[name] = (index, True, bitrate)
        return SimpleNamespace(communicate=lambda **_: ("", None), returncode=0, stdout=None)

    def read_link(self, channel, **kwargs):
        return bound_link.read_bound_state(channel, run=self.run, **kwargs)

    def snapshot(self, channel, **kwargs):
        assert self.channels[channel][1]
        return ccan_powertrain.read_broadcast_snapshot(
            channel, socket_factory=lambda *_: FakeSocket([_RPM] * 3), **kwargs,
        )

    def read_passive(self):
        if not self.read_link(self.ccan).up:
            return ()
        return self.snapshot(self.ccan).observations

    def probe(self, kind, channel, *, deadline=None):
        return usb_recovery_probe.observe(kind, channel, snapshot=self.snapshot)

    def effects(self):
        class_net = self.sysfs.class_net
        return RecoveryEffects(
            boot_id=lambda: _BOOT, helper_ok=lambda: True, ignition_flag=lambda: True,
            instance=partial(netdev_instance.read_instance, sys_class_net_root=class_net),
            indexes=partial(netdev_instance.present_ifindexes, sys_class_net_root=class_net),
            read_link=self.read_link, probe=self.probe,
            processes=partial(service_cgroup.inspect, proc_root=self.proc, cgroup_root=self.cgroup),
            up=partial(bound_link.bring_up_listen_only, read_bound_state=self.read_link, popen=self.popen),
            down=partial(bound_link.bring_down, read_bound_state=self.read_link, popen=self.popen),
        )

    def acquisition(self, mode):
        assert mode == "passive"
        return success(metric="battery.voltage", unit="V", value=13.8, source="ccan.broadcast.0x41a",
                       bus="c-can", acquisition="passive", quality="verified",
                       observed_monotonic=self.monotonic(), interface_mode="listen_only")

    def monotonic(self):
        return time.monotonic() + self.clock_offset


class ReplayDisplay:
    def __init__(self, late_auxiliary):
        self.late_auxiliary = late_auxiliary
        self.release = threading.Event()
        self.in_resume = threading.Event()
        self.calls = []

    def start(self):
        pass

    def status(self):
        return {}

    def pause_passive(self):
        self.calls.append("pause C-CAN")
        return True

    def pause_bcan_passive(self):
        self.calls.append("pause B-CAN")
        return True

    def resume_passive(self):
        self.calls.append("resume C-CAN")

    def resume_bcan_passive(self):
        self.calls.append("resume B-CAN")
        if self.late_auxiliary and threading.current_thread().name == "van-telemetry-bcan-auxiliary":
            self.in_resume.set()
            assert self.release.wait(WAIT_SECONDS)


def wire_broker(kernel, path, display):
    broker = None
    manager = PassiveInterfaceManager(
        resolver=SysfsCanRoleResolver(kernel.sysfs.class_net),
        interface_state_reader=lambda channel: kernel.read_link(channel).state,
    )
    recovery = UsbDropRecovery.if_enabled(
        True, manager, effects=kernel.effects(), record_dir=path / "incidents",
        radar_marker=lambda: broker.radar_service.request_marker(),
    )
    queues = {}
    armed = {}
    children = {}
    for n, helper in enumerate((ACTIVE_DRIVE, AUXILIARY_DRIVE)):
        launchers = [HelperLauncher(ScriptedHelper(8000 + n * 2 + i)) for i in range(2)]
        queues[helper] = iter(launchers)
        children[helper] = launchers
        armed[helper] = threading.Event()

    def route(helper, event):
        handle = (broker.handle_active_drive_event if helper == ACTIVE_DRIVE
                  else broker.handle_auxiliary_drive_event)
        handle(event)
        if event["type"] == "status":
            armed[helper].set()

    def make_supervisor(helper, **kwargs):
        cls = telemetry.ActiveDriveSupervisor if helper == ACTIVE_DRIVE else telemetry.AuxiliaryDriveSupervisor
        return cls(popen_factory=next(queues[helper]), **kwargs)

    def wrap(cls, helper):
        return cls(manager, event_handler=partial(route, helper),
                   supervisor_factory=recovery.launch_recorder(helper, partial(make_supervisor, helper)))

    def no_legacy_configure(*_args):
        pytest.fail("ordinary reconcile attempted a link change during the replay")

    reconcile = PassiveRoleReconciler(
        manager, interface_state_reader=lambda channel: kernel.read_link(channel).state,
        configure=no_legacy_configure, keep_down=no_legacy_configure,
    )
    maintenance, radar_history, last_readings = telemetry._reading_stores(path / "history.sqlite3")
    broker = telemetry.TelemetryBroker(
        acquirer=SimpleNamespace(acquire=kernel.acquisition, status_snapshot=lambda: {}),
        monotonic=kernel.monotonic, passive_powertrain_reader=SimpleNamespace(read=kernel.read_passive),
        interface_reconciler=reconcile, active_drive_enabled=True, auxiliary_drive_enabled=True,
        active_drive_supervisor=wrap(RoleAwareActiveDriveSupervisor, ACTIVE_DRIVE),
        auxiliary_drive_supervisor=wrap(RoleAwareAuxiliaryDriveSupervisor, AUXILIARY_DRIVE),
        usb_drop_recovery=recovery, display_receiver=display, maintenance_store=maintenance,
        radar_history=radar_history, last_readings=last_readings,
    )
    return broker, recovery, children, armed


def emit_final(launcher, final):
    launcher.process.emit({**final, "pid": launcher.process.pid})
    launcher.process.die()


def check_second_cycle(broker, kernel, recovery, display, *, late):
    assert not kernel.commands
    latched = broker._recovery_gates()
    assert not latched.claimed
    assert latched.latched == {ACTIVE_DRIVE, AUXILIARY_DRIVE}
    assert len(operation.all_active_inhibits()) == 2
    assert latched.auxiliary_thread_alive == late
    assert recovery.incident.state == IncidentState.WAITING
    assert recovery.incident.reason == (Refusal.HELPER_THREAD_ALIVE if late else Refusal.ROLE_UNRESOLVED)
    if display is not None:
        assert display.calls.count("pause C-CAN") == display.calls.count("pause B-CAN") == 1


def check_committed(broker, kernel, recovery, display):
    assert kernel.commands == [(30, 500000), (31, 125000)]
    assert not operation.all_active_inhibits()
    assert not list(operation.STATE_DIR.glob("inhibit-*.json"))
    assert not broker._recovery_gates().latched
    assert recovery.incident.state == IncidentState.COMMITTED
    if display is not None:
        assert display.calls[4:8] == ["pause C-CAN", "pause B-CAN", "resume C-CAN", "resume B-CAN"]


@pytest.mark.parametrize("late,with_display", [(False, True), (True, True), (False, False)])
def test_collector_replays_reenumeration_and_relaunches(monkeypatch, tmp_path, late, with_display):
    kernel = ReplayKernel(tmp_path, monkeypatch)
    display = ReplayDisplay(late) if with_display else None
    broker, recovery, children, armed = wire_broker(kernel, tmp_path, display)
    idle = [threading.Event() for _ in range(3)]
    advance = [threading.Event() for _ in idle]
    count = 0

    def wait(_interval):
        nonlocal count
        index, count = count, count + 1
        idle[index].set()
        assert advance[index].wait(WAIT_SECONDS)
        kernel.clock_offset += 120
        if count == len(idle):
            broker._collector_stop.set()

    real_join = threading.Thread.join

    def join(thread, timeout=None):
        if late and thread.name == "van-telemetry-bcan-auxiliary" and timeout == 15.0:
            assert display.in_resume.wait(WAIT_SECONDS)
            return real_join(thread, 0)
        return real_join(thread, timeout)

    with mock.patch.object(broker._collector_stop, "wait", side_effect=wait), \
         mock.patch.object(threading.Thread, "join", join):
        collector = threading.Thread(target=broker._collector_loop, daemon=True)
        collector.start()
        try:
            for helper, launchers in children.items():
                first = launchers[0]
                assert first.spawned.wait(WAIT_SECONDS), broker.status_response()["collector"]
                first.process.ready()
                assert first.read_grant() == b"G"
                first.process.emit({**ARMED, "pid": first.process.pid})
                assert armed[helper].wait(WAIT_SECONDS)
            kernel.unplug()
            emit_final(children[AUXILIARY_DRIVE][0], FAILED)
            if display is not None and late:
                assert display.in_resume.wait(WAIT_SECONDS)
            emit_final(children[ACTIVE_DRIVE][0], FAILED)
            assert idle[0].wait(WAIT_SECONDS)
            advance[0].set()
            assert idle[1].wait(WAIT_SECONDS)
            check_second_cycle(broker, kernel, recovery, display, late=late)
            if late:
                display.release.set()
                real_join(broker._auxiliary_thread, WAIT_SECONDS)
                assert not broker._recovery_gates().auxiliary_thread_alive
            kernel.replug()
            advance[1].set()
            assert children[ACTIVE_DRIVE][1].spawned.wait(WAIT_SECONDS), recovery.incident
            check_committed(broker, kernel, recovery, display)
            for launchers in children.values():
                second = launchers[1]
                assert second.spawned.wait(WAIT_SECONDS)
                second.process.ready()
                assert second.read_grant() == b"G"
            emit_final(children[AUXILIARY_DRIVE][1], RESTORED)
            emit_final(children[ACTIVE_DRIVE][1], RESTORED)
            assert idle[2].wait(WAIT_SECONDS)
        finally:
            broker._collector_stop.set()
            if display is not None:
                display.release.set()
            for signal in advance:
                signal.set()
            real_join(collector, WAIT_SECONDS)
            if broker._auxiliary_thread is not None:
                real_join(broker._auxiliary_thread, WAIT_SECONDS)
            for launchers in children.values():
                for launcher in launchers:
                    launcher.close()
        assert not collector.is_alive()
        assert broker.status_response()["collector"]["state"] == "stopped"
