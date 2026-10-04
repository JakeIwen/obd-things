#!/usr/bin/env python3
"""Offline ordered-effect oracle. Usage: python THIS.py REPO OUTPUT.json.

Run this SAME harness against both trees. Real session, link command builders,
parsers, sysfs resolver, permits, transport codecs and cleanup execute; only OS
boundaries are faked. An audit hook forbids subprocesses, sockets and real sysfs.
The sysfs fixture is local, and every resolver filesystem query is recorded.
No hardware, service, operation-state file or authoritative lock is touched.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

REPO = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(REPO))

from lib import can_handoff, can_operation_state, canbus, diagnostic_safety
from lib import can_role_resolver as resolver
from lib import capture_pipeline as capture
from projects.vehicle_data import active_drive as active, can_runtime as runtime
from projects.vehicle_data import ccan_powertrain, pcm_electrical, radar_alignment
from projects.vehicle_data import drive_recorder as recorder
from tests.test_active_drive import FakeDiagnosticLock, interface


def forbid_hardware(event, args):
    if event in ("subprocess.Popen", "socket.__new__"):
        raise AssertionError(f"unfaked external effect: {event}")
    if event == "open" and str(args[0]).startswith("/sys/"):
        raise AssertionError(f"real sysfs access: {args[0]}")


def value(item):
    if isinstance(item, bytes):
        return {"hex": item.hex()}
    if isinstance(item, Path):
        return str(item)
    if isinstance(item, (tuple, list)):
        return [value(part) for part in item]
    if isinstance(item, dict):
        return {key: value(part) for key, part in item.items()}
    return item


def frame(can_id, data):
    return struct.pack("=IB3x8s", can_id, len(data), data.ljust(8, b"\0"))


class World:
    def __init__(self, root, scenario):
        self.root = root
        self.scenario = scenario
        self.effects = []
        self.state = interface(listen_only=True)
        self.initial = self.state
        self.inventory_count = 0
        self.snapshot_count = 0
        self.socket_count = 0
        self.restoring = False
        self.handlers = {}
        self.lock_names = {}
        self.now = 100.0
        self.configure_count = 0
        self.make_sysfs()
        self.resolver = resolver.SysfsCanRoleResolver(root / "net")
        fields = {
            "wrong_bitrate": {"bitrate": 125000},
            "fd_enabled": {"fd_enabled": True},
            "fd_unknown": {"fd_enabled": None},
            "one_shot": {"one_shot": True},
            "one_shot_unknown": {"one_shot": None},
            "restart_nonzero": {"restart_ms": 100},
            "restart_unknown": {"restart_ms": None},
            "error_passive": {"controller_state": "ERROR-PASSIVE"},
            "bus_off": {"controller_state": "BUS-OFF"},
        }
        self.arm_fault = fields.get(scenario.removeprefix("arm_"), {}) if scenario.startswith("arm_") else {}
        self.state = replace(self.state, **fields.get(scenario, {}))

    def log(self, name, *args, **kwargs):
        self.effects.append([name, value(args), value(kwargs)])

    def make_sysfs(self):
        (self.root / "net" / "can7").mkdir(parents=True)
        usb = self.root / "usb"
        (usb / "interface").mkdir(parents=True)
        (self.root / "drivers" / "gs_usb").mkdir(parents=True)
        (usb / "interface" / "driver").symlink_to(self.root / "drivers" / "gs_usb")
        (self.root / "net" / "can7" / "device").symlink_to(usb / "interface")
        for path, text in (("net/can7/type", "280"), ("net/can7/dev_id", "0"),
                           ("usb/idVendor", "1d50"), ("usb/idProduct", "606f"),
                           ("usb/serial", "serial-a")):
            (self.root / path).write_text(text)

    def inventory(self, *, drivers=("gs_usb",)):
        self.inventory_count += 1
        mismatch_at = {"identity_initial": 1, "identity_arm": 2,
                       "identity_revalidate": 3}.get(self.scenario)
        if self.inventory_count == mismatch_at:
            (self.root / "usb" / "serial").write_text("other-adapter")
        if self.scenario == "renumber" and self.inventory_count == 2:
            (self.root / "net" / "can7").rename(self.root / "net" / "can8")
        self.log("inventory", drivers=drivers)
        devices, issues = self.resolver.inventory(drivers=drivers)
        # Record all discovered values; fixture paths have no identity significance.
        self.log("inventory.result", [asdict(item) for item in devices],
                 [asdict(item) for item in issues])
        return devices, issues

    def details(self, statistics=False):
        state = self.state
        options = [name for name, enabled in (
            ("LISTEN-ONLY", state.listen_only), ("ONE-SHOT", state.one_shot),
            ("FD", state.fd_enabled)) if enabled]
        flags = "UP,LOWER_UP" if state.up else "LOWER_UP"
        mtu = "" if state.fd_enabled is None else f" mtu {72 if state.fd_enabled else 16}"
        controller = "" if state.one_shot is None else f" state {state.controller_state}"
        restart = "" if state.restart_ms is None else f" restart-ms {state.restart_ms}"
        result = (f"7: can7: <{flags}>{mtu}\n"
                  f"    can <{','.join(options)}>{controller}{restart}\n"
                  f"    bitrate {state.bitrate}\n    berr-counter tx 0 rx 0\n")
        if statistics:
            result += "    RX: bytes packets errors dropped missed\n        0 0 0 0 0\n"
        return result

    def run(self, argv, **kwargs):
        self.log("ip", argv, **kwargs)
        if "show" in argv:
            result = subprocess.CompletedProcess(argv, 0 if self.state.present else 1,
                                                 self.details("-statistics" in argv), "")
        else:
            code = 0
            if "down" in argv:
                self.state = replace(self.state, up=False)
            if "bitrate" in argv:
                self.configure_count += 1
                passive = argv[argv.index("listen-only") + 1] == "on"
                self.restoring = passive
                if self.scenario == "restore_command_failure" and passive:
                    code = 1
                else:
                    self.state = replace(
                        self.state, up="up" in argv,
                        bitrate=int(argv[argv.index("bitrate") + 1]),
                        fd_enabled=False, one_shot=False,
                        listen_only=passive or self.scenario == "listen_only_stuck",
                        restart_ms=int(argv[argv.index("restart-ms") + 1]),
                    )
                    if not passive:
                        self.state = replace(self.state, **self.arm_fault)
                    if self.scenario == "restore_verify_failure" and passive:
                        self.state = replace(self.state, controller_state="BUS-OFF")
            elif "up" in argv:
                self.state = replace(self.state, up=True)
            result = subprocess.CompletedProcess(argv, code, "", "")
        self.log("ip.result", result.returncode, result.stdout, result.stderr)
        return result

    def clock(self):
        self.log("clock", self.now)
        return self.now

    def sleep(self, seconds):
        self.log("sleep", seconds)
        self.now += seconds

    def inhibits(self, channel):
        self.log("inhibits.read", channel)
        return [{"name": "existing"}] if self.scenario == "existing_inhibit" else []

    def topology(self, channel):
        self.log("topology.read", channel)
        return SimpleNamespace(usable=True, bus="c-can", pair="6/14", reason="fixture")

    def acquire(self, name):
        self.log("lock.acquire", name)
        if self.scenario == "lock_contention":
            raise diagnostic_safety.ChannelLockError("fixture contention")
        handle = FakeDiagnosticLock()
        self.lock_names[id(handle)] = name
        return handle

    def release(self, handle):
        self.log("lock.release", self.lock_names.get(id(handle)))
        if handle is not None:
            handle.closed = True

    @contextmanager
    def lock(self, name):
        handle = self.acquire(name)
        try:
            yield handle
        finally:
            self.release(handle)

    def validate_lock(self, handle, channel):
        self.log("lock.validate", self.lock_names.get(id(handle)), channel)
        if handle.closed or self.lock_names.get(id(handle)) != channel:
            raise diagnostic_safety.ChannelLockError("fixture invalid lock")
        return handle

    def signal(self, signum, handler):
        self.log("signal.handler", int(signum), "install" if callable(handler) else "restore")
        old = self.handlers.get(signum, signal.SIG_DFL)
        self.handlers[signum] = handler
        return old

    def socket(self, kind, *args):
        self.socket_count += 1
        return FakeSocket(self, kind, self.socket_count, args)

    @contextmanager
    def patches(self):
        with ExitStack() as stack:
            def patch(obj, name, replacement):
                stack.enter_context(mock.patch.object(obj, name, replacement))

            # Record all sysfs reads, directory enumeration and symlink/stat queries.
            for method in ("read_text", "iterdir", "resolve", "is_file"):
                original = getattr(Path, method)
                def watched(path, *args, _method=method, _original=original, **kwargs):
                    if str(path).startswith(str(self.root)):
                        self.log("sysfs." + _method, path, args, kwargs)
                    return _original(path, *args, **kwargs)
                patch(Path, method, watched)
            patch(subprocess, "run", self.run)
            for name, number in (("AF_CAN", 29), ("CAN_RAW", 1)):
                stack.enter_context(mock.patch.object(socket, name, number, create=True))
            patch(socket, "socket", lambda *args: self.socket("probe", *args))
            patch(canbus.time, "sleep", self.sleep)
            patch(canbus.time, "time", self.clock)
            patch(can_operation_state, "load_topology", self.topology)
            patch(can_operation_state, "active_inhibits", self.inhibits)
            patch(can_operation_state, "begin_inhibit", lambda *a, **k: self.log("inhibit.begin", *a, **k))
            patch(can_operation_state, "end_inhibit", lambda *a, **k: self.log("inhibit.end", *a, **k))
            patch(diagnostic_safety, "acquire_channel_lock", self.acquire)
            patch(diagnostic_safety, "release_channel_lock", self.release)
            patch(diagnostic_safety, "channel_lock", self.lock)
            patch(diagnostic_safety, "validate_channel_lock", self.validate_lock)
            patch(signal, "signal", self.signal)
            patch(active.os, "getpid", lambda: 4242)
            # A future scheduling change must appear in the trace, not touch real locks.
            for name in ("passive_turn", "active_turn"):
                @contextmanager
                def turn(*args, _name=name, **kwargs):
                    self.log("handoff.acquire", _name, args, kwargs)
                    try:
                        yield
                    finally:
                        self.log("handoff.release", _name)
                patch(can_handoff, name, turn)
            snapshot_reader = ccan_powertrain.read_broadcast_snapshot
            patch(ccan_powertrain, "read_broadcast_snapshot", lambda *a, **k: snapshot_reader(
                *a, **k, socket_factory=lambda *args: self.socket("broadcast", *args)))
            for module, name, kind in (
                (pcm_electrical, "PcmElectricalPoller", "pcm"),
                (active, "RfHubPressurePoller", "tpms"),
                (radar_alignment, "RadarAlignmentPoller", "radar"),
            ):
                constructor = getattr(module, name)
                def factory(*args, _constructor=constructor, _kind=kind, **kwargs):
                    return _constructor(*args, **kwargs, monotonic=self.clock,
                                        socket_factory=lambda *a: self.socket(_kind, *a))
                patch(module, name, factory)
            yield


class FakeSocket:
    def __init__(self, world, kind, number, args):
        self.world, self.kind, self.number = world, kind, number
        self.timeout = None
        self.frames = []
        world.log("socket.open", number, kind, args)
        if kind == "probe":
            self.frames = [frame(0x100, b"\0" * 8)]
        elif kind == "broadcast":
            world.snapshot_count += 1
            rpm = b"\x0b\xb8" if world.snapshot_count <= 3 else b"\0\0"
            self.frames = [frame(0x0FC, rpm)] * 3
        self.interrupted = False

    def setsockopt(self, *args):
        self.world.log("socket.setsockopt", self.number, *args)

    def bind(self, *args):
        self.world.log("socket.bind", self.number, *args)

    def settimeout(self, timeout):
        self.timeout = timeout
        self.world.log("socket.settimeout", self.number, timeout)

    def send(self, data):
        self.world.log("socket.send", self.number, data)
        if self.kind == "pcm" and not self.interrupted:
            self.interrupted = True
            if self.world.scenario == "keyboard_interrupt":
                self.world.log("interrupt", "KeyboardInterrupt")
                raise KeyboardInterrupt
            if self.world.scenario == "sigterm":
                self.world.log("interrupt", "SIGTERM")
                self.world.handlers[signal.SIGTERM](signal.SIGTERM, None)
        _can_id, _dlc, payload = struct.unpack("=IB3x8s", data)
        did = payload[2:4]
        if self.kind == "pcm":
            raw = b"\x05\x62" + did + b"\x00\x64"
            if did in (b"\x06\x9f", b"\x21\x85"):
                raw = b"\x04\x62" + did + b"\x50"
            self.frames.append(frame(0x98DAF110, raw))
        elif self.kind == "tpms":
            self.frames.append(frame(0x98DAF1C7, b"\x05\x62" + did + b"\x01\xb5"))
        elif self.kind == "radar":
            self.frames.append(frame(0x98DAF12A, b"\x03\x7f\x22\x31"))
        return len(data)

    def recv(self, size):
        self.world.log("socket.recv", self.number, size)
        if self.timeout == 0:
            self.world.log("socket.raise", self.number, "BlockingIOError")
            raise BlockingIOError
        if not self.frames:
            self.world.log("socket.raise", self.number, "TimeoutError")
            raise socket.timeout
        result = self.frames.pop(0)
        self.world.log("socket.received", self.number, result)
        return result

    def close(self):
        self.world.log("socket.close", self.number)


def active_case(world):
    backend = active.SystemBackend("can7", expected_usb_serial="serial-a", expected_dev_id=0,
                                   role_resolver=SimpleNamespace(inventory=world.inventory),
                                   monotonic=world.clock, sleep=world.sleep)
    if world.scenario == "already_passive_restore":
        return backend.restore(world.initial)
    sink = SimpleNamespace(emit=lambda event, **payload: world.log("event", event, **payload))
    with diagnostic_safety.interrupt_on_termination() as guard:
        original_cleanup = guard.begin_cleanup
        def cleanup():
            world.log("cleanup.begin")
            if world.scenario == "already_passive_cleanup":
                world.state = world.initial
            original_cleanup()
        guard.begin_cleanup = cleanup
        return asdict(active.run_active_session(backend, sink, termination_guard=guard,
                                               enable_oil_life=True))


def runtime_case(world):
    spec = resolver.CanRoleSpec("c-can", "serial-a", 0, 500000, "6/14", "A", "CAN1")
    def topology():
        inventory, issues = world.inventory()
        matches = tuple(item for item in inventory if spec.matches(item))
        state = "resolved" if len(matches) == 1 else "missing" if not matches else "ambiguous"
        return resolver.RoleTopology((resolver.RoleResolution(spec, state, matches, "fixture"),),
                                     inventory, issues, "fixture")
    reconciler = runtime.PassiveRoleReconciler(
        SimpleNamespace(topology=topology),
        configure=lambda channel, bitrate: runtime.configure_classical_listen_only(
            channel, bitrate, run=world.run),
        interface_state_reader=canbus.interface_state, inhibit_reader=world.inhibits,
        topology_writer=lambda *a, **k: world.log("topology.write", *a, **k),
        wall_clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    return reconciler._one("c-can").as_dict()


def passive_case(world, recorder_check):
    kwargs = dict(channel="can7", bitrate=500000)
    if recorder_check:
        return asdict(recorder.query_interface(
            **kwargs, require_listen_only=True, expected_usb_serial="serial-a", expected_dev_id=0,
            role_resolver=SimpleNamespace(inventory=world.inventory), runner=world.run))
    return asdict(capture.runtime_safety_check(**kwargs, runner=world.run))


def run_case(kind, scenario):
    with tempfile.TemporaryDirectory(prefix="route-effects-") as directory:
        world = World(Path(directory), scenario)
        with world.patches():
            try:
                if kind == "active":
                    result = active_case(world)
                elif kind == "runtime":
                    result = runtime_case(world)
                else:
                    result = passive_case(world, kind == "recorder")
                world.log("return", result)
            except BaseException as exc:
                world.log("raise", type(exc).__name__, str(exc))
        # Strip ONLY the random fixture root, not channel, serial, errors or effects.
        return json.loads(json.dumps(world.effects).replace(directory, "<sysfs>"))


def predicate_cases():
    """Direct readbacks retain values the text parser cannot produce (e.g. None).

    Record the existing sites, not the proposed abstraction. These are independent
    behavioral snapshots taken before any implementation change.
    """
    result = {}
    initial = interface(listen_only=True)
    states = [("exact", initial), ("duck", SimpleNamespace(**asdict(initial))),
              ("none", None)]
    for field, values in {
        "channel": ("can8", None), "present": (False, None, 1),
        "up": (False, None, 1), "bitrate": (None, 125000),
        "fd_enabled": (None, True, 0), "one_shot": (None, True, 0),
        "listen_only": (None, False, 1), "restart_ms": (None, 100, False),
        "controller_state": (None, "ERROR-PASSIVE", "BUS-OFF"),
    }.items():
        states.extend((f"{field}={item!r}", replace(initial, **{field: item})) for item in values)
    for name, state in states:
        effects = []
        def reader(channel):
            effects.append(["state.read", channel])
            return state
        def command(argv, **kwargs):
            effects.append(["ip", argv, kwargs])
            return subprocess.CompletedProcess(argv, 0, "", "")
        inventory = (resolver.NetdevIdentity("can7", "gs_usb", "1d50", "606f", "serial-a", 0, "/fixture"),)
        backend = active.SystemBackend(
            "can7", expected_usb_serial="serial-a", expected_dev_id=0,
            role_resolver=SimpleNamespace(inventory=lambda **kwargs: (inventory, ())),
        )
        with mock.patch.object(canbus, "interface_state", reader), \
             mock.patch.object(subprocess, "run", command), \
             mock.patch.object(canbus.time, "sleep", lambda delay: effects.append(["sleep", delay])):
            for site, call in (
                ("passive", lambda: active._safe_passive_state(state)),
                ("active", lambda: active._safe_active_state(state, initial)),
                ("arm", lambda: backend.arm(initial)),
                ("configure", lambda: runtime.configure_classical_listen_only("can7", 500000, run=command)),
            ):
                try:
                    effects.append([site, "return", call()])
                except BaseException as exc:
                    effects.append([site, "raise", type(exc).__name__, str(exc)])
        result[name] = effects
    return result


def main():
    sys.addaudithook(forbid_hardware)
    scenarios = (
        "normal", "already_passive_restore", "already_passive_cleanup", "identity_arm",
        "identity_revalidate", "renumber", "wrong_bitrate", "fd_enabled", "fd_unknown",
        "listen_only_stuck", "one_shot", "one_shot_unknown", "restart_nonzero", "restart_unknown",
        "error_passive", "bus_off", "restore_command_failure", "restore_verify_failure",
        "keyboard_interrupt", "sigterm", "lock_contention", "existing_inhibit", "identity_initial",
        "arm_wrong_bitrate", "arm_fd_enabled", "arm_one_shot", "arm_restart_nonzero",
        "arm_error_passive", "arm_bus_off",
    )
    result = {kind: {scenario: run_case(kind, scenario) for scenario in scenarios}
              for kind in ("active", "runtime", "recorder", "capture")}
    result["predicates"] = predicate_cases()
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    # Check the happy path really transmits through real pollers and restores.
    normal = result["active"]["normal"]
    assert len([row for row in normal if row[0] == "socket.send"]) >= 5
    assert normal[-1][0] == "return" and normal[-1][1][0]["restored"] is True
    for scenario in ("restore_command_failure", "restore_verify_failure"):
        rows = result["active"][scenario]
        assert any(row[0] == "inhibit.begin" for row in rows), scenario
        assert rows[-1][1][0]["restored"] is False
    for scenario in ("keyboard_interrupt", "sigterm"):
        rows = result["active"][scenario]
        assert any(row[0] == "interrupt" for row in rows), scenario
        assert rows[-1][1][0]["restored"] is True
    print(f"recorded {sum(map(len, result.values()))} ordered-effect scenarios")


if __name__ == "__main__":
    main()
