"""Helper-side Service Drive Alignment: fixed frames, state machine, helper loop."""

import math
import os
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import diagnostic_safety
from projects.vehicle_data import (
    active_drive, radar_alignment as radar, radar_drive_steps,
    radar_service_alignment as service, radar_service_transport as transport,
    transmit_permit as permits,
)
from tests.radar_service_support import (
    Clock, Commands, FakeAuthority, FakeRadar, Sink, radar_frame,
)
from tests.test_active_drive import FakeBackend, rpm_observation, snapshot, speed_observation


def link_for(fake: FakeRadar, clock=None) -> radar.RadarAlignmentPoller:
    link = radar.RadarAlignmentPoller("can7", monotonic=clock or Clock())
    link.sock = fake
    return link


def stopped(speed=0.0):
    return snapshot((750, 751, 752), rpm_observation(), speed_observation(speed))


class TransportTests(unittest.TestCase):
    def exchange(self, fake, spec, fail_consume=None):
        radar_transport = transport.RadarServiceTransport(link_for(fake))
        with mock.patch.object(permits, "consume", side_effect=fail_consume) as consume:
            reply = radar_transport.exchange(spec, *(object() for _ in spec.purposes))
        return reply, consume

    def test_only_the_five_fixed_requests_exist_and_each_has_its_own_purpose(self):
        specs = transport.EXCHANGES
        self.assertEqual([spec.request.hex() for spec in specs], [
            "0210030000000000", "0431010251000000", "0431020251000000",
            "0431030251000000", "023e000000000000",
        ])
        purposes = {spec.purpose for spec in specs} | {permits.RADAR_SERVICE_FLOW}
        self.assertEqual(len(purposes), 6)
        self.assertLessEqual(purposes, permits.ALLOWED_PURPOSES)
        self.assertFalse(purposes & {permits.RADAR_ALIGNMENT, permits.RADAR_ALIGNMENT_FLOW})
        self.assertEqual(transport.KEEP_ALIVE.purposes, (permits.RADAR_SERVICE_KEEPALIVE,))
        self.assertEqual(transport.READ_STATUS.purposes,
                         (permits.RADAR_SERVICE_STATUS, permits.RADAR_SERVICE_FLOW))

    def test_segmented_status_reply_gets_exactly_one_flow_control(self):
        fake = FakeRadar(progress=(91,))
        fake.extended, fake.routine = True, "running"
        reply, consume = self.exchange(fake, transport.READ_STATUS)
        self.assertEqual(reply.kind, transport.POSITIVE)
        self.assertEqual(transport.RoutineStatus.from_reply(reply).text, "00 01 5B 00")
        self.assertEqual(fake.requests(), ["31 03 02 51", "FC"])
        self.assertEqual([call.kwargs["purpose"] for call in consume.call_args_list],
                         [permits.RADAR_SERVICE_STATUS, permits.RADAR_SERVICE_FLOW])

    def test_single_frame_replies_send_no_flow_control(self):
        for spec, expected in ((transport.ENTER_SESSION, b"\x50\x03\x00\x32\x01\xf4"),
                               (transport.KEEP_ALIVE, b"\x7e\x00")):
            fake = FakeRadar()
            reply, _ = self.exchange(fake, spec)
            self.assertEqual((reply.kind, reply.payload), (transport.POSITIVE, expected))
            self.assertEqual(len(fake.sent), 1)

    def test_start_reply_is_accepted_in_either_recorded_possible_shape(self):
        for long_reply, frames in ((False, 1), (True, 2)):
            fake = FakeRadar(long_start_reply=long_reply)
            fake.extended = True
            reply, _ = self.exchange(fake, transport.START_ROUTINE)
            self.assertEqual(reply.kind, transport.POSITIVE)
            self.assertEqual(len(fake.sent), frames)

    def test_negative_reply_reports_its_code_and_pending_waits_without_resending(self):
        fake = FakeRadar(start_nrc=0x22)
        fake.extended = True
        reply, _ = self.exchange(fake, transport.START_ROUTINE)
        self.assertEqual((reply.kind, reply.nrc), (transport.NEGATIVE, 0x22))
        fake = FakeRadar()
        fake.extended = True
        original = fake._answer
        def pending_first(data):
            fake.queue.append(radar_frame(b"\x03\x7f\x31\x78"))
            original(data)
        fake._answer = pending_first
        reply, _ = self.exchange(fake, transport.START_ROUTINE)
        self.assertEqual(reply.kind, transport.POSITIVE)
        self.assertEqual(len(fake.sent), 1)

    def test_unexpected_first_frame_never_gets_flow_control(self):
        for first in (b"\x10\x08\x71\x03\x02\x50\0\0",   # another routine
                      b"\x10\x20\x71\x03\x02\x51\0\0",   # longer than one consecutive frame
                      b"\x21\x00\x00\x00\x00\x00\x00\0"):  # stray consecutive frame
            fake = FakeRadar()
            fake._answer = lambda data, first=first: fake.queue.append(radar_frame(first))
            reply, _ = self.exchange(fake, transport.READ_STATUS)
            self.assertEqual(reply.kind, transport.MALFORMED)
            self.assertEqual(len(fake.sent), 1)
        fake = FakeRadar()
        fake._answer = lambda data: fake.queue.append(radar_frame(b"\x10\x08\x7e\x00\0\0\0\0"))
        reply, _ = self.exchange(fake, transport.KEEP_ALIVE)
        self.assertEqual((reply.kind, len(fake.sent)), (transport.MALFORMED, 1))

    def test_expired_permit_sends_nothing_and_other_permit_failures_propagate(self):
        fake = FakeRadar()
        reply, _ = self.exchange(fake, transport.ENTER_SESSION, permits.ExpiredTransmitPermitError("expired"))
        self.assertEqual((reply.kind, fake.sent), (transport.SKIPPED, []))
        with self.assertRaises(permits.TransmitPermitError):
            self.exchange(FakeRadar(), transport.ENTER_SESSION, permits.TransmitPermitError("wrong lock"))
        fake = FakeRadar()
        fake.silent = True
        reply, _ = self.exchange(fake, transport.ENTER_SESSION)
        self.assertEqual(reply.kind, transport.NO_REPLY)

    def test_a_real_permit_for_another_purpose_cannot_send_a_service_frame(self):
        from tests.test_active_drive import transmit_authorization
        fake = FakeRadar()
        radar_transport = transport.RadarServiceTransport(link_for(fake, lambda: 10.0))
        with mock.patch.object(diagnostic_safety, "validate_channel_lock", side_effect=lambda h, _c: h):
            wrong = transmit_authorization(permits.RADAR_ALIGNMENT)
            with self.assertRaises(permits.TransmitPermitError):
                radar_transport.exchange(transport.START_ROUTINE, wrong)
            right = transmit_authorization(permits.RADAR_SERVICE_SESSION)
            self.assertEqual(radar_transport.exchange(transport.ENTER_SESSION, right).kind,
                             transport.POSITIVE)
        self.assertEqual(fake.requests(), ["10 03"])


class Harness:
    """The service run as the helper drives it, with the 0845 read switched off."""

    def __init__(self, fake=None):
        self.fake = fake or FakeRadar()
        self.clock = Clock()
        self.sink = Sink()
        self.authority = FakeAuthority()
        self.commands = Commands()
        self.steps = radar_drive_steps.RadarDriveSteps(
            SimpleNamespace(monotonic=self.clock), self.sink, link_for(self.fake, self.clock),
            self.authority, next_due=math.inf, service_commands=self.commands,
        )
        self._consume = mock.patch.object(permits, "consume")

    def __enter__(self):
        self._consume.start()
        return self

    def __exit__(self, *_exc):
        self._consume.stop()

    def cycle(self, speed=0.0, seconds=1.0):
        self.clock.value += seconds
        return self.steps.run(stopped(speed))

    def start(self):
        self.commands.send("start")
        self.cycle()       # accepted, 10 03
        self.cycle()       # 31 01
        return self

    def states(self):
        return [event["state"] for event in self.sink.service()]


class StateMachineTests(unittest.TestCase):
    def test_without_a_command_nothing_is_ever_sent(self):
        with Harness() as h:
            for _ in range(5):
                self.assertIsNone(h.cycle())
            self.assertEqual(h.fake.sent, [])
            self.assertEqual(h.authority.requested, [])
            self.assertEqual(h.states(), [service.IDLE])
            self.assertIs(h.sink.service()[0]["supported"], True)

    def test_june_sequence_runs_to_completion_and_then_goes_quiet(self):
        with Harness(FakeRadar(progress=(0, 40, 80, 100))) as h:
            h.start()
            for _ in range(4):
                h.cycle()
            self.assertEqual(h.fake.requests(), [
                "10 03", "31 01 02 51",
                "3E 00", "31 03 02 51", "FC", "3E 00", "31 03 02 51", "FC",
                "3E 00", "31 03 02 51", "FC", "3E 00", "31 03 02 51", "FC",
            ])
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["progress"], final["routine_status"]),
                             (service.COMPLETED, 100, "00 03 64 00"))
            self.assertEqual(final["request_id"], "a" * 32)
            self.assertEqual(h.states(), [
                service.IDLE, service.STARTING, service.RUNNING, service.RUNNING,
                service.RUNNING, service.RUNNING, service.COMPLETED,
            ])
            sent = len(h.fake.sent)
            for _ in range(10):
                h.cycle()
            # No stop, no keepalive and never a second 10 03: the session lapses.
            self.assertEqual(len(h.fake.sent), sent)
            self.assertEqual(h.fake.requests().count("10 03"), 1)

    def test_start_is_refused_while_moving_or_without_a_speed(self):
        for speed in (3.0, None):
            with Harness() as h:
                h.commands.send("start")
                h.clock.value += 1
                h.steps.run(stopped(speed) if speed is not None
                            else snapshot((750, 751, 752), rpm_observation()))
                event = h.sink.service()[-1]
                self.assertEqual((event["state"], event["reason"]),
                                 (service.FAILED, "start_refused_moving"))
                self.assertEqual(h.fake.sent, [])

    def test_once_running_the_van_may_move_and_duplicate_starts_are_ignored(self):
        with Harness(FakeRadar(progress=(5, 6, 7, 8, 9))) as h:
            h.start()
            h.commands.send("start", "b" * 32)
            h.cycle(speed=42.0)
            h.cycle(speed=44.0)
            last = h.sink.service()[-1]
            self.assertEqual((last["state"], last["request_id"], last["speed_mph"]),
                             (service.RUNNING, "a" * 32, 44.0))
            self.assertEqual(h.fake.requests().count("31 01 02 51"), 1)

    def test_rejected_session_or_start_fails_with_the_code(self):
        with Harness(FakeRadar(session_nrc=0x22)) as h:
            h.commands.send("start")
            h.cycle()
            event = h.sink.service()[-1]
            self.assertEqual((event["state"], event["reason"]), (service.FAILED, "session_rejected"))
            self.assertEqual(h.fake.requests(), ["10 03"])
        with Harness(FakeRadar(start_nrc=0x33)) as h:
            h.start()
            event = h.sink.service()[-1]
            self.assertEqual((event["state"], event["reason"]), (service.FAILED, "start_rejected"))
            self.assertIn("NRC 33", event["detail"])
            self.assertIn("security", event["detail"])
            h.cycle()
            self.assertEqual(h.fake.requests(), ["10 03", "31 01 02 51"])

    def test_owner_stop_sends_the_stop_request_once(self):
        with Harness(FakeRadar(progress=(10, 20, 30))) as h:
            h.start()
            h.cycle()
            h.commands.send("stop")
            h.cycle()
            self.assertEqual(h.states()[-2:], [service.STOPPING, service.STOPPED])
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"]), (service.STOPPED, "owner_stopped"))
            self.assertEqual(h.fake.requests()[-1], "31 02 02 51")
            h.cycle()
            self.assertEqual(h.fake.requests().count("31 02 02 51"), 1)

    def test_stop_for_another_request_is_ignored(self):
        with Harness(FakeRadar(progress=(10, 20, 30))) as h:
            h.start()
            h.commands.send("stop", "b" * 32)
            h.cycle()
            self.assertEqual(h.sink.service()[-1]["state"], service.RUNNING)
            self.assertNotIn("31 02 02 51", h.fake.requests())

    def test_lost_session_is_reported_after_three_reads(self):
        with Harness(FakeRadar(progress=(10, 20, 30, 40, 50))) as h:
            h.start()
            h.cycle()
            h.fake.drop_session()
            h.cycle()
            h.cycle()
            self.assertEqual(h.sink.service()[-1]["state"], service.RUNNING)
            h.cycle()
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"]), (service.FAILED, "routine_reset"))
            self.assertIn("NRC 7F", final["detail"])

    def test_stall_and_time_limit_stop_the_routine(self):
        with Harness(FakeRadar(progress=(55,))) as h:
            h.start()
            h.cycle()
            h.cycle(seconds=service.STALL_SECONDS)
            self.assertEqual(h.sink.service()[-1]["state"], service.STOPPING)
            h.cycle()
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"]), (service.FAILED, "stalled"))
            self.assertIn("55%", final["detail"])
            self.assertEqual(h.fake.requests()[-1], "31 02 02 51")
        with Harness(FakeRadar(progress=tuple(range(0, 100)))) as h:
            h.start()
            h.cycle(seconds=service.MAX_RUN_SECONDS)
            h.cycle()
            self.assertEqual(h.sink.service()[-1]["reason"], "time_limit")

    def test_silent_radar_ends_the_run_after_the_session_must_have_lapsed(self):
        with Harness(FakeRadar(progress=(10, 20))) as h:
            h.start()
            h.cycle()
            h.fake.silent = True
            for _ in range(7):
                h.cycle()
            self.assertEqual(h.sink.service()[-1]["state"], service.RUNNING)
            h.cycle()
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"]), (service.FAILED, "no_response"))

    def test_stale_evidence_skips_a_step_and_only_fails_when_it_persists(self):
        with Harness() as h:
            h.authority.script = [FakeAuthority.SKIP] * 2
            h.commands.send("start")
            h.cycle()
            h.cycle()
            self.assertEqual(h.fake.sent, [])
            h.cycle()
            self.assertEqual(h.fake.requests(), ["10 03"])
        with Harness() as h:
            h.authority.script = [FakeAuthority.SKIP] * service.SKIPPED_STEP_LIMIT
            h.commands.send("start")
            for _ in range(service.SKIPPED_STEP_LIMIT):
                h.cycle()
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"]), (service.FAILED, "no_response"))
            self.assertEqual(h.fake.sent, [])

    def test_session_ending_outcome_is_returned_and_recorded(self):
        with Harness(FakeRadar(progress=(10, 20))) as h:
            h.start()
            ending = SimpleNamespace(reason="engine_not_running", detail="engine stopped")
            h.authority.script = [ending]
            self.assertIs(h.cycle(), ending)
            final = h.sink.service()[-1]
            self.assertEqual((final["state"], final["reason"], final["detail"]),
                             (service.FAILED, "helper_ended", "engine stopped"))

    def test_without_a_radar_link_a_start_reports_not_supported(self):
        sink, commands = Sink(), Commands()
        run = service.ServiceAlignment(None, sink, FakeAuthority(), commands, monotonic=Clock())
        commands.send("start")
        run.run_cycle(stopped())
        self.assertIs(sink.service()[0]["supported"], False)
        self.assertEqual(sink.service()[-1]["reason"], "not_supported")

    def test_every_event_stays_inside_the_limits_the_broker_enforces(self):
        # Sink runs the broker's validator on each event, so the other tests cover
        # the ordinary paths; these are the two values the helper has to bound itself.
        def failing_send(_data):
            raise OSError("x" * 2000)

        with Harness() as h:
            h.fake._answer = failing_send
            h.commands.send("start")
            h.cycle()
            final = h.sink.service()[-1]
            self.assertEqual(final["reason"], "no_response")
            self.assertEqual(len(final["detail"]), service.MAX_DETAIL_CHARS)
        with Harness() as h:
            h.cycle(speed=service.MAX_SPEED_MPH + 1)
            self.assertIsNone(h.sink.service()[-1]["speed_mph"])


class CommandReaderTests(unittest.TestCase):
    def test_the_broker_encoding_is_what_the_helper_parses(self):
        for name in (service.START_COMMAND, service.STOP_COMMAND):
            line = service.encode_command(name, "d" * 32)
            self.assertTrue(line.endswith("\n"))
            self.assertEqual(service.parse_command(line.encode()),
                             {"command": name, "request_id": "d" * 32})
        self.assertIsNone(service.parse_command(service.encode_command("10 03", "d" * 32).encode()))

    def test_reads_whole_valid_lines_and_ignores_everything_else(self):
        read_end, write_end = os.pipe()
        try:
            reader = service.CommandReader(read_end)
            self.assertEqual(reader.poll(), [])
            good = b'{"command":"start","request_id":"' + b"c" * 32 + b'"}\n'
            os.write(write_end, good[:20])
            self.assertEqual(reader.poll(), [])
            with mock.patch.object(service.sys, "stderr"):
                os.write(write_end, good[20:] + b'{"command":"10 03","request_id":"x"}\nnot json\n'
                         + b'{"command":"stop","request_id":"' + b"c" * 32 + b'","extra":1}\n')
                self.assertEqual(reader.poll(), [{"command": "start", "request_id": "c" * 32}])
            os.close(write_end)
            write_end = None
            self.assertEqual(reader.poll(), [])
            self.assertTrue(reader.closed)
        finally:
            os.close(read_end)
            if write_end is not None:
                os.close(write_end)


class HelperLoopTests(unittest.TestCase):
    """The routine inside ``run_active_session`` with real permits and gates."""

    def run_session(self, fake, commands, running):
        # ``running`` snapshots show a running engine, then one shows it stopped.
        # Preflight and arming take one each; a cycle takes its own snapshot
        # plus one for every radar step that transmits.
        backend = FakeBackend(snapshots=[stopped() for _ in range(running)]
                              + [snapshot((0, 0, 0), rpm_observation(0))])
        backend.open_radar = lambda: link_for(fake, backend.monotonic)
        sink = Sink()
        lock = object()
        with (
            mock.patch.object(diagnostic_safety, "acquire_channel_lock", return_value=lock),
            mock.patch.object(diagnostic_safety, "release_channel_lock"),
            mock.patch.object(diagnostic_safety, "validate_channel_lock",
                              side_effect=lambda handle, _channel: handle),
            mock.patch.object(active_drive.can_operation_state, "begin_inhibit"),
        ):
            outcome = active_drive.run_active_session(backend, sink, service_commands=commands)
        return outcome, sink, backend

    def test_without_the_handshake_the_helper_cannot_build_a_service_request(self):
        fake = FakeRadar()
        outcome, sink, _ = self.run_session(fake, None, running=2 + 2)
        self.assertTrue(outcome.restored)
        self.assertEqual(sink.service(), [])
        self.assertEqual(fake.requests(), ["22 08 45", "FC"])

    def test_owner_start_runs_beside_the_alignment_read_and_restores(self):
        fake = FakeRadar(progress=(0, 50, 100))
        commands = Commands()
        commands.send("start")
        # Cycle 1: 0845 and 10 03; cycle 2: 31 01; cycles 3-5: hold to 100%.
        outcome, sink, backend = self.run_session(fake, commands, running=2 + 3 + 2 + 3 * 2)
        self.assertEqual(outcome.reason, "engine_not_running")
        self.assertTrue(outcome.restored)
        self.assertTrue(fake.closed)
        self.assertEqual(fake.requests(), [
            "22 08 45", "FC", "10 03", "31 01 02 51",
            "3E 00", "31 03 02 51", "FC", "3E 00", "31 03 02 51", "FC",
            "3E 00", "31 03 02 51", "FC",
        ])
        self.assertEqual(sink.service()[-1]["state"], service.COMPLETED)
        observed = [e for e in sink.events if e["type"] == "observation"
                    and e["metric"] in radar.METRICS]
        self.assertEqual([e["value"] for e in observed], [.278, -.2])
        self.assertEqual(backend.events[-1], "restore")

    def test_engine_stop_mid_routine_ends_the_session_and_reports_it(self):
        fake = FakeRadar(progress=(1, 2, 3, 4, 5, 6))
        commands = Commands()
        commands.send("start")
        # Cycle 1: 0845 and 10 03; cycle 2: 31 01; cycle 3: hold; cycle 4: its own
        # snapshot is fine, then the routine's fresh evidence shows a stopped engine.
        outcome, sink, _ = self.run_session(fake, commands, running=2 + 3 + 2 + 2 + 1)
        self.assertEqual(outcome.reason, "engine_not_running")
        self.assertTrue(outcome.restored)
        final = sink.service()[-1]
        self.assertEqual((final["state"], final["reason"]), (service.FAILED, "helper_ended"))
        self.assertNotIn("31 02 02 51", fake.requests())


class FrameShapeTests(unittest.TestCase):
    def test_radar_frames_are_physical_29_bit_dlc_8(self):
        fake = FakeRadar()
        link = link_for(fake)
        with mock.patch.object(permits, "consume"):
            transport.RadarServiceTransport(link).exchange(transport.KEEP_ALIVE, object())
        self.assertEqual(struct.pack(radar.FRAME, radar.EFF | 0x18DA2AF1, 8, transport.KEEP_ALIVE.request),
                         struct.pack(radar.FRAME, radar.EFF | radar.MODULE.txid, 8, fake.sent[0]))


if __name__ == "__main__":
    unittest.main()
