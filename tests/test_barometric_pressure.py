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
from projects.vehicle_data import radar_alignment, transmit_permit
from tests.test_active_drive_oil_life_step import scripted_backend
from tests.test_pcm_electrical import FakeSocket, TEST_CHANNEL, authorization, response_frame


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
TIMEOUT = TimeoutError()
MALFORMED = "malformed_response"


@pytest.mark.parametrize(("prior_reply", "prior_reason", "queued", "age", "expected"), [
    (TIMEOUT, "response_timeout", (PRESSURE_REPLY,), 0.2, None),
    (TIMEOUT, "response_timeout", ("05 62 01 09 00 FA",), 0.2, None),
    ("03 7F 22 78", "response_rejected", (PRESSURE_REPLY,), 0.2, None),
    ("03 7F 22 22", "response_rejected", (PRESSURE_REPLY,), 0.2, MALFORMED),
    ("05 62 01 09 00 FA", "response_rejected", (PRESSURE_REPLY,), 0.2, MALFORMED),
    (TIMEOUT, "response_timeout", (PRESSURE_REPLY,) * 2, 0.2, MALFORMED),
    (TIMEOUT, "response_timeout", ("05 62 01 09 01",), 0.2, MALFORMED),
    (TIMEOUT, "response_timeout", ("05 62 01 08 01 97",), 0.2, MALFORMED),
    (TIMEOUT, "response_timeout", ("02 50 92",), 0.2, MALFORMED),
    (None, None, (PRESSURE_REPLY,), 0.2, MALFORMED),
    (TIMEOUT, "response_timeout", (PRESSURE_REPLY,), 4.79, None),
    (TIMEOUT, "response_timeout", (PRESSURE_REPLY,), 4.8, MALFORMED),
    (TIMEOUT, "response_timeout", (PRESSURE_REPLY,), 600.0, MALFORMED),
])
def test_generator_skips_only_a_recent_outstanding_pcm_reply(
    prior_reply, prior_reason, queued, age, expected,
):
    socket = FakeSocket(None)
    frames = [response_frame(bytes.fromhex(reply)) for reply in queued]
    frames.append(response_frame(bytes.fromhex("05 62 01 A1 40 00")))
    clock = [0.0]

    def receive(_size):
        clock[0] += 0.2
        frame = frames.pop(0)
        if isinstance(frame, TimeoutError):
            raise frame
        return frame

    socket.recv = mock.Mock(side_effect=receive)
    socket.settimeout = mock.Mock()
    poller = pcm_electrical.PcmElectricalPoller(
        TEST_CHANNEL, socket_factory=lambda *_: socket, monotonic=lambda: clock[0],
    )
    if prior_reply is not None:
        frames.insert(0, response_frame(bytes.fromhex(prior_reply))
                      if isinstance(prior_reply, str) else prior_reply)
        pressure = poller.poll_barometric_pressure(authorization(purpose=PURPOSE))
        assert pressure.reason == prior_reason
        socket.settimeout.reset_mock()
    clock[0] = age
    generator = poller.poll(authorization())
    assert generator.reason == expected
    assert socket.sent == (
        ([REQUEST] if prior_reply is not None else [])
        + [pcm_electrical.GENERATOR_FIELD_DUTY_PROFILE.request_frame]
    )
    if expected is None:
        assert generator.value == 50.0
    else:
        assert len(frames) == 1
    expected_timeouts = [0.5, 0.3] if len(queued) == 2 or expected is None else [0.5]
    assert [call.args[0] for call in socket.settimeout.call_args_list] == pytest.approx(expected_timeouts)


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
