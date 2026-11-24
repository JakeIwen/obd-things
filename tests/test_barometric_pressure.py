"""Offline coverage of the PCM barometric request and its guarded callers."""

from contextlib import nullcontext
from dataclasses import replace
from io import StringIO
import json
import struct
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from projects.vehicle_data import active_drive, broker, pcm_electrical, pcm_temperature_support
from projects.vehicle_data import radar_alignment, transmit_permit
from tests.test_active_drive_oil_life_step import scripted_backend
from tests.test_pcm_electrical import FakeSocket, TEST_CHANNEL, authorization, response_frame


METRIC = "engine.barometric_pressure"
ENV = "VAN_TELEMETRY_BAROMETRIC_PRESSURE"
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


def test_only_opted_in_ccan_supervisor_passes_the_barometric_flag():
    launches = []
    args = ["serve", "--can-interface-mode", "dual-usbcanfd"]
    parser = broker.build_parser()
    flag = "--enable-barometric-pressure"
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
        with mock.patch.dict(active_drive.os.environ, {ENV: "1"}, clear=True):
            supervisor = supervisor_cls(
                channel="can7", expected_usb_serial="S", expected_dev_id=0,
                event_handler=lambda _event: None, popen_factory=popen,
                enable_barometric_pressure=options.enable_barometric_pressure,
            )
            supervisor.run(threading.Event())
    assert [command.count(flag) for command, _env in launches] == [0, 1, 0]
    for (command, environ), enabled in zip(launches[:2], (False, True), strict=True):
        assert environ[ENV] == "1"
        with (
            mock.patch.dict(active_drive.os.environ, environ, clear=True),
            mock.patch.object(active_drive, "_set_parent_death_signal"),
            mock.patch.object(active_drive, "SystemBackend"),
            mock.patch.object(active_drive.diagnostic_safety, "interrupt_on_termination",
                              return_value=nullcontext()),
            mock.patch.object(active_drive, "run_active_session") as run,
        ):
            assert active_drive.main(command[3:]) == 0
        assert run.call_args.kwargs["enable_barometric_pressure"] is enabled


@pytest.mark.parametrize(("prior_timeout", "queued", "expected"), [
    (True, ("05 62 01 09 01 97",), None),
    (True, ("05 62 01 09 01 97",) * 2, "malformed_response"),
    (True, ("05 62 01 09 01",), "malformed_response"),
    (True, ("05 62 01 08 01 97",), "malformed_response"),
    (True, ("02 50 92",), "malformed_response"),
    (False, ("05 62 01 09 01 97",), "malformed_response"),
])
def test_generator_skips_only_an_outstanding_pcm_reply(prior_timeout, queued, expected):
    socket = FakeSocket(None)
    frames = [response_frame(bytes.fromhex(reply)) for reply in queued]
    frames.append(response_frame(bytes.fromhex("05 62 01 A1 40 00")))
    clock = [0.0]

    def receive(_size):
        clock[0] += 0.2
        if prior_timeout and len(socket.sent) == 1:
            raise TimeoutError
        return frames.pop(0)

    socket.recv = mock.Mock(side_effect=receive)
    socket.settimeout = mock.Mock()
    poller = pcm_electrical.PcmElectricalPoller(
        TEST_CHANNEL, socket_factory=lambda *_: socket, monotonic=lambda: clock[0],
    )
    if prior_timeout:
        pressure = poller.poll_barometric_pressure(authorization(purpose=PURPOSE))
        assert pressure.reason == "response_timeout"
        socket.settimeout.reset_mock()
    generator = poller.poll(authorization())
    assert generator.reason == expected
    assert socket.sent == (
        ([REQUEST] if prior_timeout else []) + [pcm_electrical.GENERATOR_FIELD_DUTY_PROFILE.request_frame]
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
