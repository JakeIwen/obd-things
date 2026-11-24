"""Offline coverage of the PCM barometric request and its guarded callers."""

from contextlib import nullcontext
from dataclasses import replace
from io import StringIO
import json
import os
import struct
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from projects.vehicle_data import active_drive, barometric_pressure, broker, pcm_electrical
from projects.vehicle_data import pcm_temperature_support
from projects.vehicle_data import radar_alignment, radar_service_alignment as service, transmit_permit
from tests.radar_service_support import FakeRadar, Sink
from tests.test_active_drive import speed_observation
from tests.test_active_drive_oil_life_step import scripted_backend
from tests.test_pcm_electrical import FakeSocket, TEST_CHANNEL, authorization, response_frame
from tests.test_radar_service_alignment import link_for


METRIC = "engine.barometric_pressure"
ENV = barometric_pressure.ENABLE_ENV
PURPOSE = transmit_permit.PCM_BAROMETRIC_PRESSURE
VALUE = 81.307611
REQUEST = struct.pack("=IB3x8s", 0x98DA10F1, 8, bytes.fromhex("03 22 01 09 00 00 00 00"))


@pytest.mark.parametrize(("reply", "reason"), [
    ("05 62 01 09 01 97", None),
    ("05 62 01 09 00 FA", "response_rejected"),  # Below 50 kPa
    ("05 62 01 09 02 27", "response_rejected"),  # Above 110 kPa
    ("05 62 01 08 01 97", "malformed_response"),
    ("05 62 01 09 01", "malformed_response"),
    ("06 62 01 09 01 97 00", "malformed_response"),
    ("10 08 62 01 09 01 97", "malformed_response"),
    ("03 7F 22 7E", "session_required"),
    (None, "response_timeout"),
])
def test_fixed_barometric_read(reply, reason):
    wire = response_frame(bytes.fromhex(reply)) if reply else TimeoutError()
    sock = FakeSocket(wire)
    poller = pcm_electrical.PcmElectricalPoller(TEST_CHANNEL, socket_factory=lambda *_: sock)
    wrong_permit = authorization(purpose=transmit_permit.PCM_OIL_LIFE)
    assert not poller.poll_barometric_pressure(wrong_permit).available
    assert sock.sent == []
    permit = authorization(purpose=PURPOSE)
    result = poller.poll_barometric_pressure(permit)
    assert result.reason == reason
    assert not poller.poll_barometric_pressure(permit).available
    assert sock.sent == [REQUEST]
    if reason is None:
        assert result.available
        assert result.value == pytest.approx(VALUE)
        assert result.raw_value == 407
        assert (result.metric, result.unit, result.source, result.bus, result.quality) == (
            METRIC, "kPa", "pcm.did.0109", "c-can", "observed_alfa_scale",
        )
    else:
        assert not result.available


def test_parked_support_profile_is_one_fixed_read_without_io(capsys):
    with mock.patch.object(pcm_temperature_support.socket, "socket") as socket_factory:
        assert pcm_temperature_support.main(["--profile", "barometric-pressure"]) == 0
    socket_factory.assert_not_called()
    plan = json.loads(capsys.readouterr().out)
    assert plan["requests"] == ["03 22 01 09 00 00 00 00"]
    assert plan["maximum_requests"] == 1
    assert plan["retries"] == 0 and not plan["session_change"] and not plan["flow_control"]
    reply = response_frame(bytes.fromhex("05 62 01 09 01 97"))
    assert pcm_temperature_support.decode_reply(0x0109, reply)["value_kpa"] == pytest.approx(VALUE)


@pytest.mark.parametrize(("enabled", "failure"), [
    (False, None), (True, None), (True, "response_timeout"),
    (True, "session_required"), (True, "malformed_response"),
])
def test_public_session_runs_barometric_schedule_and_stops_retrying(enabled, failure):
    actions, poll_times = [], []
    backend, clock = scripted_backend(actions)

    def poll(permit):
        transmit_permit.consume(permit, purpose=PURPOSE, channel=backend.channel)
        poll_times.append(clock[0])
        failed = bool(failure and len(poll_times) == 2)
        return pcm_electrical.PcmElectricalResult(
            metric=METRIC, available=not failed, value=None if failed else VALUE,
            unit="kPa", source="pcm.did.0109", bus="c-can", quality="observed_alfa_scale",
            reason=failure if failed else None, detail="scripted reply",
        )

    backend.pcm.poll_barometric_pressure = poll
    stream = StringIO()
    safety = active_drive.diagnostic_safety
    with (
        mock.patch.object(safety, "acquire_channel_lock", return_value=object()),
        mock.patch.object(safety, "release_channel_lock"),
        mock.patch.object(safety, "validate_channel_lock", side_effect=lambda handle, channel: handle),
        mock.patch.object(active_drive.can_operation_state, "begin_inhibit") as inhibit,
    ):
        outcome = active_drive.run_active_session(
            backend, active_drive.JsonEventSink(stream), enable_oil_life=True,
            enable_barometric_pressure=enabled,
            termination_guard=SimpleNamespace(begin_cleanup=lambda: None),
        )
    assert outcome.restored and outcome.reason == "engine_not_running"
    inhibit.assert_not_called()
    expected = [] if not enabled else [100, 110] if failure else list(range(100, 225, 10))
    assert poll_times == expected
    events = [event for event in map(json.loads, stream.getvalue().splitlines())
              if event.get("metric") == METRIC]
    assert [event["type"] for event in events] == (
        ["observation", "metric_failure"] if failure else ["observation"] * len(expected)
    )
    assert backend.pcm.oil_life_poll_count == 3


def test_only_opted_in_ccan_supervisor_passes_its_pid_in_the_environment():
    launches = []
    args = ["serve", "--can-interface-mode", "dual-usbcanfd"]
    parser = broker.build_parser()
    flag = barometric_pressure.ENABLE_FLAG
    off = parser.parse_args(args)
    on = parser.parse_args([*args, flag])
    assert not off.enable_barometric_pressure and on.enable_barometric_pressure

    def popen(command, **kwargs):
        launches.append((command, kwargs["env"]))
        raise OSError("capture launch without spawning")

    for supervisor_cls, options in [
        (broker.ActiveDriveSupervisor, off), (broker.ActiveDriveSupervisor, on),
        (broker.AuxiliaryDriveSupervisor, on),
    ]:
        with mock.patch.dict(os.environ, {ENV: str(os.getpid())}, clear=True):
            supervisor = supervisor_cls(
                channel="can7", expected_usb_serial="S", expected_dev_id=0,
                event_handler=lambda _event: None, popen_factory=popen,
                enable_barometric_pressure=options.enable_barometric_pressure,
            )
            supervisor.run(threading.Event())
    assert all(flag not in command for command, _env in launches)
    assert [env.get(ENV) for _command, env in launches] == [None, str(os.getpid()), None]
    for command, _env in launches[:2]:
        assert active_drive.build_parser().parse_args(command[3:]).expected_parent_pid == os.getpid()


@pytest.mark.parametrize(("env_pid", "enabled"), [
    (None, False), ("garbage", False), ("101", False), ("102", True),
])
def test_helper_accepts_pressure_opt_in_only_for_expected_parent(env_pid, enabled):
    argv = [
        "--channel", TEST_CHANNEL, "--expected-parent-pid", "102",
        "--expected-usb-serial", "S", "--expected-dev-id", "0",
    ]
    with (
        mock.patch.dict(os.environ, {} if env_pid is None else {ENV: env_pid}, clear=True),
        mock.patch.object(active_drive, "_set_parent_death_signal") as check_parent,
        mock.patch.object(active_drive, "SystemBackend"),
        mock.patch.object(active_drive.diagnostic_safety, "interrupt_on_termination",
                          return_value=nullcontext()),
        mock.patch.object(active_drive, "run_active_session") as run,
    ):
        assert active_drive.main(argv) == 0
    check_parent.assert_called_once_with(102)
    assert run.call_args.kwargs["enable_barometric_pressure"] is enabled


PRESSURE_REPLY = "05 62 01 09 01 97"
PENDING = "03 7F 22 78"
GENERATOR_REPLY = "05 62 01 A1 40 00"


class TimedSocket(FakeSocket):
    """Deliver a scripted frame only while the socket is waiting for it."""

    def __init__(self, clock, events):
        super().__init__(None)
        self.clock = clock
        self.events = list(events)
        self.send_timeouts = []
        self.receive_timeouts = []

    def send(self, frame):
        self.send_timeouts.append(self.timeout)
        return super().send(frame)

    def recv(self, _size):
        self.receive_timeouts.append(self.timeout)
        deadline = self.clock[0] + self.timeout
        if not self.events or self.events[0][0] >= deadline:
            self.clock[0] = deadline
            raise TimeoutError()
        arrival, reply = self.events.pop(0)
        self.clock[0] = max(self.clock[0], arrival)
        return response_frame(bytes.fromhex(reply))


@pytest.mark.parametrize(("events", "elapsed", "reason"), [
    ([(0.2, PENDING), (1.0, PENDING), (2.0, PENDING), (2.5, PENDING),
      (3.0, PENDING), (4.9, PRESSURE_REPLY)], 4.9, None),
    ([(0.2, PENDING), (1.2, "03 7F 22 22")], 1.2, "response_rejected"),
    ([(0.2, PENDING), (4.9, PENDING)], 5.0, "response_timeout"),
    ([], 5.0, "response_timeout"),
])
def test_pressure_owns_its_replies_within_one_deadline(events, elapsed, reason):
    clock = [0.0]
    sock = TimedSocket(clock, events)
    poller = pcm_electrical.PcmElectricalPoller(
        TEST_CHANNEL, socket_factory=lambda *_: sock, monotonic=lambda: clock[0],
        timeout_seconds=0.45,
    )

    pressure = poller.poll_barometric_pressure(authorization(purpose=PURPOSE))
    assert clock[0] == pytest.approx(elapsed)
    assert pressure.reason == reason
    if reason is None:
        assert pressure.value == pytest.approx(VALUE)
    elif reason == "response_rejected":
        assert "NRC 22" in pressure.detail
    assert not sock.events
    assert sock.sent == [REQUEST]
    reply_times = [0.0, *(arrival for arrival, reply in events if reply == PENDING)]
    assert sock.receive_timeouts == pytest.approx([5.0 - time for time in reply_times])

    sock.events.append((elapsed + 0.2, GENERATOR_REPLY))
    assert poller.poll(authorization()).value == 50.0
    assert sock.sent == [REQUEST, pcm_electrical.GENERATOR_FIELD_DUTY_PROFILE.request_frame]
    assert sock.send_timeouts == [None, pytest.approx(0.45)]


def test_pressure_reply_after_deadline_is_foreign_to_next_read():
    clock = [0.0]
    sock = TimedSocket(clock, [(5.05, PRESSURE_REPLY), (5.1, GENERATOR_REPLY)])
    poller = pcm_electrical.PcmElectricalPoller(
        TEST_CHANNEL, socket_factory=lambda *_: sock, monotonic=lambda: clock[0],
    )
    assert poller.poll_barometric_pressure(authorization(purpose=PURPOSE)).reason == "response_timeout"
    generator = poller.poll(authorization())
    assert generator.reason == "malformed_response"
    assert sock.events == [(5.1, GENERATOR_REPLY)]


@pytest.fixture
def run_pressure_session(monkeypatch):
    safety = active_drive.diagnostic_safety
    monkeypatch.setattr(safety, "acquire_channel_lock", lambda _: object())
    monkeypatch.setattr(safety, "release_channel_lock", lambda _: None)
    monkeypatch.setattr(safety, "validate_channel_lock", lambda handle, _: handle)
    inhibit = mock.Mock()
    monkeypatch.setattr(active_drive.can_operation_state, "begin_inhibit", inhibit)

    def run(backend, sink, *, enabled=True, commands=None):
        outcome = active_drive.run_active_session(
            backend, sink, enable_barometric_pressure=enabled, service_commands=commands,
            termination_guard=SimpleNamespace(begin_cleanup=lambda: None),
        )
        assert outcome.restored and outcome.reason == "engine_not_running", outcome
        inhibit.assert_not_called()

    return run


def install_pressure_socket(backend, sock):
    poller = pcm_electrical.PcmElectricalPoller(
        TEST_CHANNEL, socket_factory=lambda *_: sock, monotonic=backend.monotonic,
    )
    backend.pcm.poll_barometric_pressure = poller.poll_barometric_pressure


def longest_gap(times):
    return max(later - earlier for earlier, later in zip(times, times[1:]))


def test_pressure_runs_last_and_silence_stays_below_watchdog(run_pressure_session, monkeypatch):
    backend, clock = scripted_backend([], running_snapshots=6)
    actions, event_times = [], []
    sock = TimedSocket(clock, [])
    sink = Sink()

    def emit(event_type, **payload):
        event_times.append(clock[0])
        Sink.emit(sink, event_type, **payload)

    def radar_step(snapshot):
        actions.append(("radar", snapshot.completed_monotonic))

    def dtc_step(snapshot):
        actions.append(("dtc", snapshot.completed_monotonic))
        clock[0] += 0.030

    def send(frame):
        actions.append(("pressure", clock[0]))
        return TimedSocket.send(sock, frame)

    sink.emit = emit
    sock.send = send
    install_pressure_socket(backend, sock)
    radar = SimpleNamespace(run=radar_step, service_work_pending=lambda: False)
    dtc = SimpleNamespace(begin_cycle=lambda: None, run=dtc_step, close=lambda: None)
    monkeypatch.setattr(active_drive.radar_drive_steps, "RadarDriveSteps", lambda *a, **kw: radar)
    monkeypatch.setattr(active_drive.drive_dtc_launch, "steps", lambda *a, **kw: dtc)
    run_pressure_session(backend, sink)

    assert [name for name, _ in actions[:5]] == ["radar", "dtc", "pressure", "radar", "dtc"]
    assert [time for _, time in actions[:5]] == pytest.approx([100, 100, 100.03, 105.03, 105.03])
    failures = [event for event in sink.events if event.get("metric") == METRIC]
    assert [(event["type"], event["reason"]) for event in failures] == [
        ("metric_failure", "response_timeout"),
    ]
    assert sock.sent == [REQUEST]
    assert longest_gap(event_times) == pytest.approx(5.03)


@pytest.fixture
def send_service_command():
    read_fd, write_fd = os.pipe()
    try:
        reader = service.CommandReader(read_fd)

        def send(command):
            os.write(write_fd, service.encode_command(command, "a" * 32).encode("ascii"))

        yield reader, send
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("enabled", [False, True])
def test_start_queued_during_pressure_wait_uses_next_cycles_speed(
    enabled, send_service_command, run_pressure_session,
):
    reader, send = send_service_command
    backend, clock = scripted_backend([], running_snapshots=8)
    fake = FakeRadar()
    backend.open_radar = lambda: link_for(fake, backend.monotonic)
    sample_speed = backend.broadcast_snapshot
    sock = TimedSocket(clock, [(100.45, PRESSURE_REPLY)])
    sink = Sink()

    def broadcast_snapshot(seconds):
        sample = sample_speed(seconds)
        speed = speed_observation(0.0 if clock[0] < 100.45 else 1.0)
        return replace(sample, observations=(*sample.observations, speed))

    def recv(size):
        assert clock[0] == 100.0
        clock[0] += 0.2
        send(service.START_COMMAND)
        assert reader.pending()
        return TimedSocket.recv(sock, size)

    def sleep(seconds):
        if clock[0] == 100:
            send(service.START_COMMAND)
        clock[0] += seconds

    backend.broadcast_snapshot = broadcast_snapshot
    backend.sleep = sleep
    sock.recv = recv
    install_pressure_socket(backend, sock)
    run_pressure_session(backend, sink, enabled=enabled, commands=reader)

    assert sock.sent == ([REQUEST] if enabled else [])
    refused = [event for event in sink.service() if event["reason"] == "start_refused_moving"]
    assert [event["speed_mph"] for event in refused] == [1.0]
    assert fake.requests() == ["22 08 45", "FC"]


def test_silent_pressure_leaves_active_alignment_cadence_unchanged(
    send_service_command, run_pressure_session,
):
    reader, send_command = send_service_command
    radar_gaps = []
    for enabled in (False, True):
        send_command(service.START_COMMAND)
        backend, clock = scripted_backend([], running_snapshots=13)
        fake = FakeRadar(progress=(0, 10, 20, 30, 40, 50))
        sock = TimedSocket(clock, [])
        sink = Sink()
        radar_times = []
        sample_speed = backend.broadcast_snapshot

        def broadcast_snapshot(seconds):
            sample = sample_speed(seconds)
            return replace(sample, observations=(*sample.observations, speed_observation(0.0)))

        def send(frame):
            radar_times.append(clock[0])
            return FakeRadar.send(fake, frame)

        backend.broadcast_snapshot = broadcast_snapshot
        backend.open_radar = lambda: link_for(fake, backend.monotonic)
        fake.send = send
        install_pressure_socket(backend, sock)
        run_pressure_session(backend, sink, enabled=enabled, commands=reader)

        assert "3E 00" in fake.requests()
        assert any(event["state"] == service.RUNNING for event in sink.service())
        assert not sock.sent
        assert not any(event.get("metric") == METRIC for event in sink.events)
        radar_gaps.append(longest_gap(radar_times))

    assert radar_gaps == pytest.approx([1.0, 1.0])


@pytest.mark.parametrize("queue_after_permit", [False, True])
def test_pending_service_command_defers_pressure_until_it_can_run(
    queue_after_permit, send_service_command, run_pressure_session, monkeypatch,
):
    reader, send = send_service_command
    backend, clock = scripted_backend([], running_snapshots=6)
    sock = TimedSocket(clock, [(101.1, PRESSURE_REPLY)])
    sink = Sink()
    dtc_steps = active_drive.drive_dtc_launch.steps(backend, sink, None, reservation=None)
    issue = transmit_permit.issue
    permit_times = []

    def dtc_step(snapshot):
        if not queue_after_permit and clock[0] == 100.0:
            send(service.START_COMMAND)

    def issue_permit(*args, **kwargs):
        permit = issue(*args, **kwargs)
        if kwargs["purpose"] == PURPOSE:
            permit_times.append(clock[0])
            if queue_after_permit and clock[0] == 100.0:
                send(service.START_COMMAND)
        return permit

    dtc_steps.run = dtc_step
    monkeypatch.setattr(active_drive.drive_dtc_launch, "steps", lambda *a, **kw: dtc_steps)
    monkeypatch.setattr(transmit_permit, "issue", issue_permit)
    install_pressure_socket(backend, sock)
    run_pressure_session(backend, sink, commands=reader)

    assert permit_times == ([100.0, 101.0] if queue_after_permit else [101.0])
    assert sock.sent == [REQUEST]
    assert sock.receive_timeouts == pytest.approx([5.0])
    assert [event["type"] for event in sink.events if event.get("metric") == METRIC] == ["observation"]


@pytest.mark.parametrize(("pressure_enabled", "purpose", "poll_name"), [
    (True, PURPOSE, "poll_barometric_pressure"),
    (False, transmit_permit.PCM_OIL_LIFE, "poll_oil_life"),
])
def test_expired_optional_pcm_read_does_not_starve_radar(pressure_enabled, purpose, poll_name):
    # Two startup snapshots, then fresh cycle and radar snapshots for six cycles.
    backend, clock = scripted_backend([], running_snapshots=14)
    rf_hub_delays = iter([0, 0.260, 0.260, 0.260, 0.260, 0])
    rf_hub_poll = backend.tpms.poll_next
    optional_attempts, radar_times = [], []
    optional_reply = backend.pcm.oil_life_result
    if pressure_enabled:
        optional_reply = replace(
            optional_reply, metric=METRIC, value=VALUE, unit="kPa",
            source="pcm.did.0109", quality="observed_alfa_scale",
        )

    def poll_rf_hub(permit):
        result = rf_hub_poll(permit)
        clock[0] += next(rf_hub_delays)
        return result

    def poll_optional(permit):
        optional_attempts.append(clock[0])
        if len(optional_attempts) == 1:
            # The first cycle warms the VVT cadence. Simulate a permit that
            # expires at consume time, leaving the optional read due.
            return replace(optional_reply, available=False, reason="transmit_permit_expired")
        transmit_permit.consume(permit, purpose=purpose, channel=backend.channel)
        return optional_reply

    def poll_radar(request, flow):
        transmit_permit.consume(request, purpose=transmit_permit.RADAR_ALIGNMENT,
                                channel=backend.channel)
        transmit_permit.consume(flow, purpose=transmit_permit.RADAR_ALIGNMENT_FLOW,
                                channel=backend.channel)
        radar_times.append(clock[0])
        return radar_alignment.RadarResult(available=True, angles=(0.0, 0.0))

    backend.tpms.poll_next = poll_rf_hub
    setattr(backend.pcm, poll_name, poll_optional)
    backend.open_radar = lambda: SimpleNamespace(poll=poll_radar, close=lambda: None)
    safety = active_drive.diagnostic_safety
    with (
        mock.patch.object(safety, "acquire_channel_lock", return_value=object()),
        mock.patch.object(safety, "release_channel_lock"),
        mock.patch.object(safety, "validate_channel_lock", side_effect=lambda handle, channel: handle),
        mock.patch.object(radar_alignment, "POLL_INTERVAL_SECONDS", 0),
    ):
        outcome = active_drive.run_active_session(
            backend, active_drive.JsonEventSink(StringIO()), enable_oil_life=not pressure_enabled,
            enable_barometric_pressure=pressure_enabled,
            termination_guard=SimpleNamespace(begin_cleanup=lambda: None),
        )
    assert outcome.restored and outcome.reason == "engine_not_running"
    assert optional_attempts == [100.0, 105.0]
    assert radar_times == pytest.approx([100, 101.26, 102.26, 103.26, 104.26, 105])


def test_broker_expires_barometric_readings_and_retains_the_last_one():
    clock = [100.0]
    telemetry = broker.TelemetryBroker(acquirer=mock.Mock(), monotonic=lambda: clock[0])
    observation = {
        "type": "observation", "metric": METRIC, "value": VALUE, "unit": "kPa",
        "source": "pcm.did.0109", "bus": "c-can", "quality": "observed_alfa_scale",
        "interface_mode": "armed_diagnostic",
    }
    telemetry.handle_active_drive_event(observation)
    assert telemetry.metric_response(METRIC)["value"] == VALUE
    clock[0] = 129.0
    assert not telemetry.metric_response(METRIC)["stale"]
    clock[0] = 131.0
    assert telemetry.metric_response(METRIC)["stale"]
    telemetry.handle_active_drive_event({
        **observation, "type": "metric_failure", "reason": "response_timeout", "detail": "timeout",
    })
    assert telemetry.metric_response(METRIC)["last_recorded"]["value"] == VALUE
    telemetry.handle_active_drive_event(observation)
    telemetry.handle_active_drive_event({
        "type": "final", "state": "idle", "reason": "engine_not_running", "detail": "stopped",
        "restored": True, "interface_mode": "listen_only",
    })
    result = telemetry.metric_response(METRIC)
    assert not result["available"] and result["last_recorded"]["value"] == VALUE
