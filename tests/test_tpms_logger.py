import unittest
from unittest import mock

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
    def test_idle_watch_closes_socket_when_setup_fails(self):
        sock = mock.Mock()
        sock.bind.side_effect = OSError("interface disappeared")

        with mock.patch.object(tpms_logger.socket, "socket", return_value=sock):
            result = tpms_logger.ignition_on("can0", 0.1)

        self.assertFalse(result)
        sock.close.assert_called_once_with()

    def test_iface_inspection_runs_only_read_only_ip_command(self):
        output = (
            "3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UNKNOWN\n"
            "    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0\n"
            "          bitrate 500000 sample-point 0.875\n"
        )
        completed = mock.Mock(returncode=0, stdout=output)

        with mock.patch.object(tpms_logger.subprocess, "run", return_value=completed) as run:
            result = tpms_logger.iface_is_armed("can0", 500000)

        self.assertTrue(result)
        run.assert_called_once_with(
            ["ip", "-details", "link", "show", "can0"],
            capture_output=True,
            text=True,
        )

    def test_iface_inspection_fails_closed_without_error_active_state(self):
        template = (
            "3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UNKNOWN\n"
            "    {can_line}\n"
            "          bitrate 500000 sample-point 0.875\n"
        )
        for can_line in (
            "can state ERROR-PASSIVE (berr-counter tx 1 rx 0) restart-ms 0",
            "can state BUS-OFF (berr-counter tx 255 rx 0) restart-ms 0",
            "restart-ms 0",
        ):
            with self.subTest(can_line=can_line), mock.patch.object(
                tpms_logger.subprocess,
                "run",
                return_value=mock.Mock(
                    returncode=0,
                    stdout=template.format(can_line=can_line),
                ),
            ):
                self.assertFalse(tpms_logger.iface_is_armed("can0", 500000))

    def test_lock_contention_skips_reconfiguration_without_mutation(self):
        with (
            mock.patch.object(tpms_logger, "iface_is_armed", return_value=False) as inspect,
            mock.patch.object(
                tpms_logger.diagnostic_safety,
                "channel_lock",
                side_effect=tpms_logger.diagnostic_safety.ChannelLockError("can0 busy"),
            ) as channel_lock,
            mock.patch.object(tpms_logger, "_reconfigure_iface") as reconfigure,
            mock.patch("builtins.print"),
        ):
            result = tpms_logger.ensure_iface_coordinated("can0", 500000)

        self.assertFalse(result)
        inspect.assert_called_once_with("can0", 500000)
        channel_lock.assert_called_once_with("can0")
        reconfigure.assert_not_called()

    def test_reconfiguration_is_locked_and_rechecked_before_mutation(self):
        events = []
        states = iter((False, False, True))

        def inspect(_channel, _bitrate):
            events.append("inspect")
            return next(states)

        class FakeLock:
            def __enter__(self):
                events.append("lock")

            def __exit__(self, *_args):
                events.append("unlock")

        with (
            mock.patch.object(tpms_logger, "iface_is_armed", side_effect=inspect),
            mock.patch.object(
                tpms_logger.diagnostic_safety, "channel_lock", return_value=FakeLock()
            ) as channel_lock,
            mock.patch.object(
                tpms_logger,
                "_reconfigure_iface",
                side_effect=lambda *_args: events.append("reconfigure"),
            ) as reconfigure,
        ):
            result = tpms_logger.ensure_iface_coordinated("can0", 500000)

        self.assertTrue(result)
        channel_lock.assert_called_once_with("can0")
        reconfigure.assert_called_once_with("can0", 500000)
        self.assertEqual(
            events,
            ["inspect", "lock", "inspect", "reconfigure", "inspect", "unlock"],
        )

    def test_locked_recheck_avoids_unnecessary_reconfiguration(self):
        states = iter((False, True))

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with (
            mock.patch.object(
                tpms_logger, "iface_is_armed", side_effect=lambda *_args: next(states)
            ),
            mock.patch.object(
                tpms_logger.diagnostic_safety, "channel_lock", return_value=FakeLock()
            ),
            mock.patch.object(tpms_logger, "_reconfigure_iface") as reconfigure,
        ):
            result = tpms_logger.ensure_iface_coordinated("can0", 500000)

        self.assertTrue(result)
        reconfigure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
