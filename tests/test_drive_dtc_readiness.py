"""Inert observations supporting the opt-in DTC helper handshake."""
import os
from types import SimpleNamespace

import pytest

from projects.vehicle_data.helper_startup_grant import ChildStartupGate, ready_event_problem
from projects.vehicle_data.radar_service_alignment import CommandReader, ServiceAlignment
from tests.radar_service_support import Clock, Commands, FakeAuthority, Sink


def test_pending_is_nonconsuming_and_eof_is_pending():
    read_fd, write_fd = os.pipe()
    try:
        commands = CommandReader(read_fd)
        assert not commands.pending()
        os.write(write_fd, b'{"command":"start","request_id":"' + b'a' * 32)
        assert commands.pending() and commands.pending()
        assert commands.poll() == [] and commands.pending()
        os.write(write_fd, b'"}\n')
        assert commands.pending()
        assert commands.poll() == [{"command": "start", "request_id": "a" * 32}]
        assert not commands.pending()
        os.close(write_fd)
        write_fd = None
        assert commands.pending()
        assert commands.poll() == [] and commands.pending()
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_refused_service_run_stays_attempted_for_epoch():
    commands = Commands()
    run = ServiceAlignment(None, Sink(), FakeAuthority(), commands, monotonic=Clock())
    assert not run.run_attempted
    commands.send("start")
    run.run_cycle(SimpleNamespace(observations=()))
    assert run.run_attempted and run.phase is None
    run.run_cycle(SimpleNamespace(observations=()))
    assert run.run_attempted


@pytest.mark.parametrize("capabilities", [(), ("drive_dtc_v1",)])
def test_child_emits_only_requested_capabilities(capabilities):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'G')
        sink = Sink()
        gate = ChildStartupGate(read_fd, os.getppid(), capabilities=capabilities)
        assert gate(sink) is None
        read_fd = None
        ready = sink.events[0]
        assert ready_event_problem(ready, os.getpid()) is None
        if capabilities:
            assert ready["capabilities"] == list(capabilities)
        else:
            assert set(ready) == {"type", "protocol", "pid"}
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("value", ["drive_dtc_v1", [None], [True], [""],
                                    ["drive_dtc_v1"] * 2, ["x" * 65], ["å"]])
def test_invalid_ready_capabilities_are_rejected(value):
    ready = dict(type="ready", protocol=1, pid=77, capabilities=value)
    assert ready_event_problem(ready, 77) is not None
