import errno
import socket
import struct
import unittest
from itertools import count
from types import SimpleNamespace
from unittest import mock

from lib import canbus
from projects.tpms import tpms_logger


class TpmsResponseIntegrityTests(unittest.TestCase):
    def test_read_did_rejects_wrong_did_echo_before_accepting_data(self):
        events = []
        responses = [
            (bytes.fromhex("62 31 D2 12 34"), "POSITIVE"),
            (bytes.fromhex("62 31 D3 FF FF"), "POSITIVE"),
        ]

        with (
            mock.patch.object(
                tpms_logger.uds, "drain", side_effect=lambda _sock: events.append("drain")
            ) as drain,
            mock.patch.object(
                tpms_logger.uds,
                "request",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("request") or responses.pop(0)
                ),
            ) as request,
        ):
            result = tpms_logger.read_did(object(), 0x31D3)

        self.assertEqual(result, bytes.fromhex("FF FF"))
        self.assertEqual(events, ["drain", "request", "drain", "request"])
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.args[1], bytes.fromhex("22 31 D3"))
            self.assertEqual(
                call.kwargs,
                {
                    "timeout": 0.6,
                    "retries": 0,
                    "response_pending_timeout": 0.6,
                    "max_pending_responses": 16,
                },
            )

    def test_dtc_read_rejects_wrong_subfunction_and_preserves_raw_code(self):
        responses = [
            (bytes.fromhex("59 06 00 55 03 31 8F"), "POSITIVE"),
            (
                bytes.fromhex("59 02 0D 55 03 31 8F 12 34 56 01"),
                "POSITIVE",
            ),
        ]

        with (
            mock.patch.object(tpms_logger.uds, "drain") as drain,
            mock.patch.object(tpms_logger.uds, "request", side_effect=responses) as request,
        ):
            result = tpms_logger.read_dtcs(object())

        self.assertEqual(
            result,
            {"550331(C1503-31)": 0x8F, "123456": 0x01},
        )
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.args[1], bytes.fromhex("19 02 0D"))
            self.assertEqual(
                call.kwargs,
                {
                    "timeout": 0.8,
                    "retries": 0,
                    "response_pending_timeout": 0.8,
                    "max_pending_responses": 16,
                },
            )

    def test_timeout_late_reply_is_drained_before_retry(self):
        queued_late = []
        events = []
        request_count = 0

        def fake_drain(_sock):
            events.append(("drain", list(queued_late)))
            queued_late.clear()

        def fake_request(_sock, payload, **kwargs):
            nonlocal request_count
            events.append(("request", payload, kwargs))
            request_count += 1
            if request_count == 1:
                # Model a reply arriving after the first request timed out. It must be discarded
                # before the retry is sent, not consumed as the retry's response.
                queued_late.append(bytes.fromhex("62 31 D2 00 01"))
                return None, "NO_RESPONSE"
            self.assertEqual(queued_late, [])
            return bytes.fromhex("62 31 D3 01 02"), "POSITIVE"

        with (
            mock.patch.object(tpms_logger.uds, "drain", side_effect=fake_drain),
            mock.patch.object(tpms_logger.uds, "request", side_effect=fake_request),
        ):
            result = tpms_logger.read_did(object(), 0x31D3)

        self.assertEqual(result, bytes.fromhex("01 02"))
        self.assertEqual(events[0], ("drain", []))
        self.assertEqual(events[2], ("drain", [bytes.fromhex("62 31 D2 00 01")]))
        self.assertEqual(request_count, 2)

    def test_matching_negative_response_is_retried_but_not_attributed_to_did(self):
        with (
            mock.patch.object(tpms_logger.uds, "drain") as drain,
            mock.patch.object(
                tpms_logger.uds,
                "request",
                return_value=(bytes.fromhex("7F 22 31"), "NEGATIVE"),
            ) as request,
        ):
            result = tpms_logger.read_did(object(), 0x31D3)

        self.assertIsNone(result)
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(request.call_count, 2)

    def test_late_negative_is_drained_and_retry_can_accept_echoed_positive(self):
        responses = [
            (bytes.fromhex("7F 22 31"), "NEGATIVE"),
            (bytes.fromhex("62 31 D3 01 02"), "POSITIVE"),
        ]
        with (
            mock.patch.object(tpms_logger.uds, "drain") as drain,
            mock.patch.object(tpms_logger.uds, "request", side_effect=responses) as request,
        ):
            result = tpms_logger.read_did_evidence(object(), 0x31D3, expected_length=2)

        self.assertTrue(result.ok)
        self.assertEqual(result.value, bytes.fromhex("01 02"))
        self.assertEqual(drain.call_count, 2)
        self.assertEqual(request.call_count, 2)

    def test_pressure_read_requires_exactly_two_data_bytes(self):
        with (
            mock.patch.object(tpms_logger.uds, "drain"),
            mock.patch.object(
                tpms_logger.uds,
                "request",
                return_value=(bytes.fromhex("62 31 D3 01 02 03"), "POSITIVE"),
            ),
        ):
            result = tpms_logger.read_did_evidence(object(), 0x31D3, expected_length=2)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, tpms_logger.READ_MALFORMED_DATA)
        self.assertEqual(result.detail, "LEN3_EXPECTED2")
        self.assertIsNone(tpms_logger.psi(bytes.fromhex("01 02 03")))

    def test_pressure_decoder_rejects_ffff_no_data_sentinel(self):
        self.assertIsNone(tpms_logger.psi(bytes.fromhex("FF FF")))
        self.assertEqual(tpms_logger.psi(bytes.fromhex("0F A0")), 58.0)

    def test_telemetry_publishes_verified_slot_order_and_skips_ffff(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def publish(self, metric, **payload):
                self.calls.append((metric, payload))
                return 202, {"accepted": True}

        client = FakeClient()
        pressure_results = [
            tpms_logger.ReadEvidence(bytes.fromhex(raw), tpms_logger.READ_OK)
            for raw in ("0F A0", "0F B0", "10 00", "FF FF")
        ]

        errors = tpms_logger.publish_pressure_telemetry(
            client, pressure_results
        )

        self.assertEqual(errors, ())
        self.assertEqual(
            [metric for metric, _payload in client.calls],
            [
                "tire.pressure.fl",
                "tire.pressure.fr",
                "tire.pressure.rr",
            ],
        )
        self.assertEqual(
            [payload["source"] for _metric, payload in client.calls],
            [
                "rf_hub.did.31d0",
                "rf_hub.did.31d1",
                "rf_hub.did.31d2",
            ],
        )
        self.assertTrue(
            all(
                payload["quality"] == "verified"
                for _metric, payload in client.calls
            )
        )

    def test_valid_zero_dtc_response_is_distinct_from_timeout(self):
        with (
            mock.patch.object(tpms_logger.uds, "drain"),
            mock.patch.object(
                tpms_logger.uds,
                "request",
                return_value=(bytes.fromhex("59 02 0D"), "POSITIVE"),
            ),
        ):
            valid_zero = tpms_logger.read_dtcs_evidence(object())

        with (
            mock.patch.object(tpms_logger.uds, "drain"),
            mock.patch.object(
                tpms_logger.uds,
                "request",
                return_value=(None, "NO_RESPONSE"),
            ),
        ):
            failed = tpms_logger.read_dtcs_evidence(object())

        self.assertTrue(valid_zero.ok)
        self.assertEqual(valid_zero.value, {})
        self.assertFalse(failed.ok)
        self.assertEqual(failed.status, tpms_logger.READ_NO_RESPONSE)

    def test_dtc_response_requires_availability_byte_and_complete_records(self):
        for raw, detail in (
            ("59 02", "LEN2_MIN3"),
            ("59 02 0D 55 03", "RECORD_BYTES2_MOD4"),
        ):
            with self.subTest(raw=raw):
                with (
                    mock.patch.object(tpms_logger.uds, "drain"),
                    mock.patch.object(
                        tpms_logger.uds,
                        "request",
                        return_value=(bytes.fromhex(raw), "POSITIVE"),
                    ),
                ):
                    result = tpms_logger.read_dtcs_evidence(object())

                self.assertFalse(result.ok)
                self.assertEqual(result.status, tpms_logger.READ_MALFORMED_DATA)
                self.assertEqual(result.detail, detail)

    def test_csv_quality_markers_preserve_existing_schema_and_zero_dtc_meaning(self):
        ok_pressure = [tpms_logger.ReadEvidence(b"\x01\x02", tpms_logger.READ_OK)] * 4
        ok_lastrx = [tpms_logger.ReadEvidence(b"\x04", tpms_logger.READ_OK)] * 4
        valid_zero = tpms_logger.ReadEvidence({}, tpms_logger.READ_OK)
        failed = tpms_logger.ReadEvidence(None, tpms_logger.READ_NO_RESPONSE)

        self.assertEqual(tpms_logger._dtc_csv_cell(ok_pressure, ok_lastrx, valid_zero), "")
        self.assertEqual(
            tpms_logger._dtc_csv_cell(ok_pressure, ok_lastrx, failed),
            "!READ_DTCS=NO_RESPONSE",
        )

        bad_pressure = list(ok_pressure)
        bad_pressure[3] = tpms_logger.ReadEvidence(
            None, tpms_logger.READ_AMBIGUOUS_NEGATIVE, "7F2231"
        )
        self.assertEqual(
            tpms_logger._dtc_csv_cell(bad_pressure, ok_lastrx, valid_zero),
            "!READ_PRESS_RL=AMBIGUOUS_NEGATIVE(7F2231)",
        )


class TpmsInterfaceCoordinationTests(unittest.TestCase):
    @mock.patch.multiple(
        tpms_logger.socket,
        AF_CAN=29,
        SOCK_RAW=3,
        CAN_RAW=1,
        SOL_CAN_RAW=101,
        CAN_RAW_FILTER=1,
        create=True,
    )
    def test_idle_watch_closes_socket_when_setup_fails(self):
        sock = mock.Mock()
        sock.bind.side_effect = OSError("interface disappeared")

        with mock.patch.object(tpms_logger.socket, "socket", return_value=sock):
            result = tpms_logger.ignition_on("can0", 0.1)

        self.assertFalse(result)
        sock.close.assert_called_once_with()

    def test_broker_absence_requires_narrow_unix_socket_error(self):
        for status, payload in (
            (200, {"active_drive": {"enabled": True}}),
            (200, {"active_drive": {"enabled": False}}),
            (200, {}),
            (404, {"reason": "legacy_broker"}),
            (503, {"active_drive": {"enabled": True}}),
        ):
            with self.subTest(status=status, payload=payload):
                client = mock.Mock()
                client.request.return_value = status, payload
                self.assertFalse(tpms_logger.broker_absence_proven(client))
                client.request.assert_called_once_with("GET", "/v1/status")

        for name, error in (
            ("timeout", TimeoutError("broker slow")),
            ("malformed_protocol", RuntimeError("invalid JSON")),
            (
                "permission_denied",
                PermissionError(errno.EACCES, "broker socket denied"),
            ),
            ("generic_io", OSError(errno.EIO, "broker I/O failed")),
        ):
            with self.subTest(name=name):
                client = mock.Mock()
                client.request.side_effect = error
                self.assertFalse(tpms_logger.broker_absence_proven(client))

        for name, error in (
            (
                "missing_socket",
                FileNotFoundError(errno.ENOENT, "broker socket absent"),
            ),
            (
                "refused_socket",
                ConnectionRefusedError(errno.ECONNREFUSED, "broker not listening"),
            ),
        ):
            with self.subTest(name=name):
                client = mock.Mock()
                client.request.side_effect = error
                self.assertTrue(tpms_logger.broker_absence_proven(client))

    def test_auto_mode_yields_before_any_can_operation_when_broker_owns_drive(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        events = []
        with (
            mock.patch.object(tpms_logger, "get", return_value=module),
            mock.patch.object(
                tpms_logger,
                "broker_absence_proven",
                side_effect=lambda _client: events.append("broker_status") or False,
            ),
            mock.patch.object(tpms_logger, "_passive_running_ready") as ready,
            mock.patch.object(tpms_logger, "log_session") as log_session,
            mock.patch.object(
                tpms_logger.diagnostic_safety, "acquire_channel_lock"
            ) as acquire,
            mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
            mock.patch.object(
                tpms_logger.time,
                "sleep",
                side_effect=lambda _seconds: (
                    events.append("sleep")
                    or (_ for _ in ()).throw(KeyboardInterrupt)
                ),
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                tpms_logger.auto_loop()

        self.assertEqual(events, ["broker_status", "sleep"])
        ready.assert_not_called()
        log_session.assert_not_called()
        acquire.assert_not_called()
        ip_up.assert_not_called()

    def test_auto_fallback_checks_broker_before_passive_running_gate(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        events = []
        with (
            mock.patch.object(tpms_logger, "get", return_value=module),
            mock.patch.object(
                tpms_logger,
                "broker_absence_proven",
                side_effect=lambda _client: events.append("broker_status") or True,
            ),
            mock.patch.object(
                tpms_logger,
                "_restoration_is_latched",
                side_effect=lambda _channel: events.append("latch") or False,
            ),
            mock.patch.object(
                tpms_logger,
                "_passive_running_ready",
                side_effect=lambda _module: events.append("running_gate") or False,
            ),
            mock.patch.object(tpms_logger, "log_session") as log_session,
            mock.patch.object(
                tpms_logger.time,
                "sleep",
                side_effect=lambda _seconds: (
                    events.append("sleep")
                    or (_ for _ in ()).throw(KeyboardInterrupt)
                ),
            ),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                tpms_logger.auto_loop()

        self.assertEqual(
            events,
            ["broker_status", "latch", "running_gate", "sleep"],
        )
        log_session.assert_not_called()

    def test_passive_recovery_never_arms_interface(self):
        armed = canbus.InterfaceState(
            "can9", True, True, 500000, False, "ERROR-ACTIVE", 0
        )
        passive = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        with (
            mock.patch.object(
                tpms_logger.canbus,
                "interface_state",
                side_effect=(armed, armed, passive),
            ),
            mock.patch.object(
                tpms_logger,
                "_restoration_is_latched",
                return_value=False,
            ),
            mock.patch.object(tpms_logger, "_topology_is_ccan", return_value=True),
            mock.patch.object(tpms_logger, "_active_inhibits", return_value=()),
            mock.patch.object(
                tpms_logger.canbus,
                "bring_up_passive",
                return_value=True,
            ) as bring_up_passive,
            mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
        ):
            self.assertTrue(
                tpms_logger._ensure_passive_coordinated("can9", 500000)
            )

        bring_up_passive.assert_called_once_with(
            "can9",
            500000,
            restart_ms=0,
            noninteractive=True,
        )
        ip_up.assert_not_called()

    def test_safe_passive_state_requires_readable_restart_timing(self):
        incomplete = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", None
        )
        self.assertFalse(
            tpms_logger._safe_passive_state(incomplete, "can9", 500000)
        )

    def test_restoration_failure_latches_process_and_same_boot_inhibit(self):
        original = tpms_logger._restoration_failed
        try:
            tpms_logger._restoration_failed = False
            with (
                mock.patch.object(
                    tpms_logger.can_operation_state, "begin_inhibit"
                ) as begin_inhibit,
                mock.patch("builtins.print"),
            ):
                tpms_logger._latch_restoration_failure("can9")

            self.assertTrue(tpms_logger._restoration_failed)
            begin_inhibit.assert_called_once()
            self.assertEqual(
                begin_inhibit.call_args.args,
                (tpms_logger.RESTORATION_INHIBIT_NAME,),
            )
            self.assertEqual(begin_inhibit.call_args.kwargs["channel"], "can9")
            self.assertIn(
                "could not verify exact listen-only restoration",
                begin_inhibit.call_args.kwargs["reason"],
            )
        finally:
            tpms_logger._restoration_failed = original

    def test_lock_contention_skips_passive_reconfiguration(self):
        unsafe = canbus.InterfaceState(
            "can9", True, True, 500000, False, "ERROR-ACTIVE", 0
        )
        with (
            mock.patch.object(
                tpms_logger.canbus, "interface_state", return_value=unsafe
            ),
            mock.patch.object(
                tpms_logger,
                "_restoration_is_latched",
                return_value=False,
            ),
            mock.patch.object(tpms_logger, "_topology_is_ccan", return_value=True),
            mock.patch.object(tpms_logger, "_active_inhibits", return_value=()),
            mock.patch.object(
                tpms_logger.diagnostic_safety,
                "channel_lock",
                side_effect=tpms_logger.diagnostic_safety.ChannelLockError("can0 busy"),
            ) as channel_lock,
            mock.patch.object(
                tpms_logger.canbus, "bring_up_passive"
            ) as bring_up_passive,
        ):
            result = tpms_logger._ensure_passive_coordinated("can9", 500000)

        self.assertFalse(result)
        channel_lock.assert_called_once_with("can9")
        bring_up_passive.assert_not_called()

    def test_engine_running_requires_multiple_consecutive_fresh_rpm_samples(self):
        def rpm_frame(rpm, *, can_id=tpms_logger.ENGINE_SPEED_ID):
            raw = int(rpm * 4)
            data = raw.to_bytes(2, "big") + b"\0" * 6
            return struct.pack("=IB3x8s", can_id, 8, data)

        class FakeSocket:
            def __init__(self, frames):
                self.frames = list(frames)
                self.closed = False
                self.filter = None

            def setsockopt(self, _level, _option, value):
                self.filter = value

            def bind(self, address):
                self.address = address

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                if not self.frames:
                    raise socket.timeout
                return self.frames.pop(0)

            def close(self):
                self.closed = True

        passing = FakeSocket(
            [rpm_frame(650), rpm_frame(651), rpm_frame(652)]
        )
        ticks = count(start=0.0, step=0.01)
        self.assertTrue(
            tpms_logger.engine_running(
                "can9",
                window=1.0,
                socket_factory=lambda *_args: passing,
                monotonic=lambda: next(ticks),
            )
        )
        self.assertTrue(passing.closed)
        self.assertEqual(passing.address, ("can9",))
        self.assertEqual(
            struct.unpack("=II", passing.filter),
            (tpms_logger.ENGINE_SPEED_ID, tpms_logger.CAN_FILTER_MASK),
        )

        boundary = FakeSocket(
            [rpm_frame(400), rpm_frame(400), rpm_frame(400)]
        )
        ticks = count(start=0.0, step=0.01)
        self.assertTrue(
            tpms_logger.engine_running(
                "can9",
                window=1.0,
                socket_factory=lambda *_args: boundary,
                monotonic=lambda: next(ticks),
            )
        )
        self.assertTrue(boundary.closed)

        reset = FakeSocket(
            [
                rpm_frame(650),
                rpm_frame(651),
                rpm_frame(0),
                rpm_frame(652),
                rpm_frame(653),
            ]
        )
        ticks = count(start=0.0, step=0.01)
        self.assertFalse(
            tpms_logger.engine_running(
                "can9",
                window=1.0,
                socket_factory=lambda *_args: reset,
                monotonic=lambda: next(ticks),
            )
        )
        self.assertTrue(reset.closed)

    def test_passive_running_preflight_rejects_wrong_bus_inhibit_and_zero_rpm(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        passive = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        cases = (
            ("wrong_bus", (), "b-can", True),
            ("inhibited", ({"name": "alfaobd"},), "c-can", True),
            ("engine_off", (), "c-can", False),
        )
        for name, inhibits, bus, running in cases:
            with (
                self.subTest(name=name),
                mock.patch.object(
                    tpms_logger, "_restoration_is_latched", return_value=False
                ),
                mock.patch.object(
                    tpms_logger, "_ensure_passive_coordinated", return_value=True
                ),
                mock.patch.object(
                    tpms_logger, "_topology_is_ccan", return_value=True
                ),
                mock.patch.object(
                    tpms_logger, "_active_inhibits", return_value=inhibits
                ),
                mock.patch.object(
                    tpms_logger.diagnostic_safety,
                    "channel_observer_lock",
                    return_value=mock.MagicMock(),
                ) as observer_lock,
                mock.patch.object(
                    tpms_logger.canbus, "interface_state", return_value=passive
                ),
                mock.patch.object(
                    tpms_logger.canbus, "identify_bus", return_value=bus
                ) as identify,
                mock.patch.object(
                    tpms_logger, "engine_running", return_value=running
                ) as engine_running,
                mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
                mock.patch.object(tpms_logger.uds, "open_socket") as open_socket,
            ):
                self.assertFalse(tpms_logger._passive_running_ready(module))
                observer_lock.assert_called_once_with("can9")
                ip_up.assert_not_called()
                open_socket.assert_not_called()
                if inhibits:
                    identify.assert_not_called()
                    engine_running.assert_not_called()

    def test_passive_running_preflight_defers_on_observer_contention(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        with (
            mock.patch.object(
                tpms_logger, "_restoration_is_latched", return_value=False
            ),
            mock.patch.object(
                tpms_logger, "_ensure_passive_coordinated", return_value=True
            ),
            mock.patch.object(
                tpms_logger.diagnostic_safety,
                "channel_observer_lock",
                side_effect=tpms_logger.diagnostic_safety.ChannelLockError("busy"),
            ),
            mock.patch.object(tpms_logger.canbus, "interface_state") as state,
            mock.patch.object(tpms_logger.canbus, "identify_bus") as identify,
            mock.patch.object(tpms_logger, "engine_running") as engine_running,
        ):
            self.assertFalse(tpms_logger._passive_running_ready(module))

        state.assert_not_called()
        identify.assert_not_called()
        engine_running.assert_not_called()

    def test_topology_gate_requires_same_boot_ccan_on_pins_6_14(self):
        cases = (
            (
                "qualified",
                SimpleNamespace(usable=True, bus="c-can", pair="6/14"),
                True,
            ),
            (
                "wrong_pair",
                SimpleNamespace(usable=True, bus="c-can", pair="12/13"),
                False,
            ),
            (
                "wrong_bus",
                SimpleNamespace(usable=True, bus="can-ch", pair="6/14"),
                False,
            ),
            (
                "stale_or_missing",
                SimpleNamespace(usable=False, bus="unknown", pair=""),
                False,
            ),
        )
        for name, topology, expected in cases:
            with (
                self.subTest(name=name),
                mock.patch.object(
                    tpms_logger.can_operation_state,
                    "load_topology",
                    return_value=topology,
                ),
            ):
                self.assertEqual(tpms_logger._topology_is_ccan("can9"), expected)

    def test_under_lock_recheck_refuses_to_arm_after_gate_loss(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        passive = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        telemetry = mock.Mock()
        for name, broker_absent, topology, inhibits, bus, running in (
            ("broker_live_or_uncertain", False, True, (), "c-can", True),
            ("wrong_topology", True, False, (), "c-can", True),
            ("inhibited", True, True, ({"name": "alfaobd"},), "c-can", True),
            ("wrong_bus", True, True, (), "can-ch", True),
            ("engine_off", True, True, (), "c-can", False),
        ):
            with (
                self.subTest(name=name),
                mock.patch.object(
                    tpms_logger,
                    "broker_absence_proven",
                    return_value=broker_absent,
                ),
                mock.patch.object(
                    tpms_logger, "_restoration_is_latched", return_value=False
                ),
                mock.patch.object(
                    tpms_logger, "_topology_is_ccan", return_value=topology
                ),
                mock.patch.object(
                    tpms_logger, "_active_inhibits", return_value=inhibits
                ),
                mock.patch.object(
                    tpms_logger.canbus, "interface_state", return_value=passive
                ),
                mock.patch.object(
                    tpms_logger.canbus, "identify_bus", return_value=bus
                ),
                mock.patch.object(
                    tpms_logger, "engine_running", return_value=running
                ),
                mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
                mock.patch.object(tpms_logger.uds, "open_socket") as open_socket,
            ):
                self.assertIsNone(
                    tpms_logger._locked_start_state(
                        module,
                        auto=True,
                        telemetry=telemetry,
                    )
                )
                ip_up.assert_not_called()
                open_socket.assert_not_called()

    def test_under_lock_recheck_yields_if_broker_appears_during_rpm_gate(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        passive = canbus.InterfaceState(
            "can9", True, True, 500000, True, "ERROR-ACTIVE", 0
        )
        with (
            mock.patch.object(
                tpms_logger,
                "broker_absence_proven",
                side_effect=(True, False),
            ) as broker_status,
            mock.patch.object(
                tpms_logger, "_restoration_is_latched", return_value=False
            ),
            mock.patch.object(tpms_logger, "_topology_is_ccan", return_value=True),
            mock.patch.object(tpms_logger, "_active_inhibits", return_value=()),
            mock.patch.object(
                tpms_logger.canbus, "interface_state", return_value=passive
            ),
            mock.patch.object(
                tpms_logger.canbus, "identify_bus", return_value="c-can"
            ),
            mock.patch.object(tpms_logger, "engine_running", return_value=True),
            mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
            mock.patch.object(tpms_logger.uds, "open_socket") as open_socket,
        ):
            self.assertIsNone(
                tpms_logger._locked_start_state(
                    module,
                    auto=True,
                    telemetry=mock.Mock(),
                )
            )

        self.assertEqual(broker_status.call_count, 2)
        ip_up.assert_not_called()
        open_socket.assert_not_called()

    def test_manual_locked_start_also_requires_proven_broker_absence(self):
        module = SimpleNamespace(channel="can9", bitrate=500000)
        with (
            mock.patch.object(
                tpms_logger, "broker_absence_proven", return_value=False
            ) as broker_absence,
            mock.patch.object(tpms_logger, "_topology_is_ccan") as topology,
            mock.patch.object(tpms_logger.canbus, "ip_up") as ip_up,
            mock.patch.object(tpms_logger.uds, "open_socket") as open_socket,
        ):
            self.assertIsNone(
                tpms_logger._locked_start_state(
                    module,
                    auto=False,
                    telemetry=mock.Mock(),
                )
            )

        broker_absence.assert_called_once()
        topology.assert_not_called()
        ip_up.assert_not_called()
        open_socket.assert_not_called()


if __name__ == "__main__":
    unittest.main()
