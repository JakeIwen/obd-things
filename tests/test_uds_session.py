from array import array
import socket
import unittest
from unittest import mock

from lib import uds_session
from tools import did_sweep, ecu_discover, routine_scan


class SessionResponseTests(unittest.TestCase):
    def test_tool_reexports(self):
        for name in (
            "classify_session_response", "classify_tester_present_response",
            "request_once", "tester_present",
        ):
            for tool in (did_sweep, routine_scan):
                with self.subTest(tool=tool.__name__, name=name):
                    self.assertIs(getattr(tool, name), getattr(uds_session, name))

    def test_session_classifier(self):
        for response, expected in (
            (None, "timeout"), (b"", "timeout"), ([], "timeout"),
            (b"\x50", "unexpected"), (b"\x50\x03", "positive_echo"),
            (b"\x50\x03\x00\x32", "positive_echo"),
            (b"\x50\x02", "unexpected"), (b"\x7f\x10", "unexpected"),
            (b"\x7f\x10\x22", "negative"), (b"\x7f\x22\x22", "unexpected"),
            ([0x50, 3], "positive_echo"), (bytearray(b"\x50\x03"), "positive_echo"),
            (memoryview(b"\x50\x03"), "positive_echo"),
            (memoryview(array("H", [0x50, 3])), "unexpected"),
        ):
            with self.subTest(response=response):
                self.assertEqual(uds_session.classify_session_response(3, response), expected)
        with self.assertRaises(TypeError):
            uds_session.classify_session_response(3, "50 03")

    def test_discovery_classifier_remains_distinct(self):
        self.assertIsNot(ecu_discover.classify_session_response,
                         uds_session.classify_session_response)
        self.assertEqual(ecu_discover.classify_session_response(3, [0x50, 3]), "unexpected")
        self.assertEqual(ecu_discover.classify_session_response(3, "50 03"), "unexpected")
        self.assertEqual(ecu_discover.classify_session_response(
            3, memoryview(array("H", [0x50, 3]))), "positive_echo")

    def test_tester_present_classifier(self):
        for response, expected in (
            (None, "timeout"), (b"", "timeout"), ([], "timeout"),
            (b"\x7e", "unexpected"), (b"\x7e\x00", "positive_echo"),
            (b"\x7e\x00\xaa", "unexpected"), (b"\x7e\x01", "unexpected"),
            (b"\x7f\x3e", "unexpected"), (b"\x7f\x3e\x22", "negative"),
            (b"\x7f\x10\x22", "unexpected"), ([0x7e, 0], "positive_echo"),
            (bytearray(b"\x7e\x00"), "positive_echo"),
            (memoryview(b"\x7e\x00"), "positive_echo"),
            (memoryview(array("H", [0x7e, 0])), "unexpected"),
        ):
            with self.subTest(response=response):
                self.assertEqual(uds_session.classify_tester_present_response(response), expected)
        with self.assertRaises(TypeError):
            uds_session.classify_tester_present_response("7E 00")


class SessionRequestTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = mock.patch.object(socket, "socket", side_effect=AssertionError("no sockets"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.sock = object()

    def test_drain_attempt_response_order_and_retries(self):
        for retries in (None, 0, 2):
            with self.subTest(retries=retries):
                attempts = {"read": 4}
                responses = {"read": 3}
                events = []

                def drain(sock):
                    self.assertIs(sock, self.sock)
                    events.append(("drain", attempts.copy(), responses.copy()))

                def request(sock, payload, **kwargs):
                    self.assertIs(sock, self.sock)
                    self.assertEqual(payload, b"\x22\xf1\x87")
                    self.assertEqual(kwargs, {"timeout": 0.75, "retries": retries or 0})
                    events.append(("request", attempts.copy(), responses.copy()))
                    return b"\x62\xf1\x87", "OK"

                kwargs = {} if retries is None else {"retries": retries}
                with mock.patch.object(uds_session.uds, "drain", side_effect=drain), \
                     mock.patch.object(uds_session.uds, "request", side_effect=request):
                    result = uds_session.request_once(
                        self.sock, b"\x22\xf1\x87", 0.75, attempts, responses, "read", **kwargs)
                self.assertEqual(result, (b"\x62\xf1\x87", "OK"))
                self.assertEqual(events, [
                    ("drain", {"read": 4}, {"read": 3}),
                    ("request", {"read": 5}, {"read": 3}),
                ])
                self.assertEqual(responses, {"read": 4})

    def test_empty_response_and_optional_counters(self):
        for response in (None, b""):
            with self.subTest(response=response):
                attempts, responses = {}, {}
                with mock.patch.object(uds_session.uds, "drain"), \
                     mock.patch.object(uds_session.uds, "request", return_value=(response, "TIMEOUT")):
                    self.assertEqual(uds_session.request_once(
                        self.sock, b"\x3e\x00", 0.5, attempts, responses), (response, "TIMEOUT"))
                    self.assertEqual(uds_session.request_once(
                        self.sock, b"\x3e\x00", 0.5), (response, "TIMEOUT"))
                self.assertEqual(attempts, {None: 1})
                self.assertEqual(responses, {})

    def test_transport_failure_accounting(self):
        for boundary in ("drain", "request"):
            with self.subTest(boundary=boundary):
                attempts, responses = {}, {}
                error = OSError("transport")
                with mock.patch.object(uds_session.uds, "drain") as drain, \
                     mock.patch.object(uds_session.uds, "request") as request:
                    (drain if boundary == "drain" else request).side_effect = error
                    with self.assertRaises(OSError) as caught:
                        uds_session.request_once(self.sock, b"\x3e\x00", 0.5,
                                                 attempts, responses, "keepalive")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(request.call_count, 0 if boundary == "drain" else 1)
                self.assertEqual(attempts, {} if boundary == "drain" else {"keepalive": 1})
                self.assertEqual(responses, {})

    def test_keepalive_exact_event_and_rejection(self):
        for response, category in (
            (b"\x7e\x00", "positive_echo"), (None, "timeout"),
            (b"\x7e\x00\xaa", "unexpected"), (b"\x7f\x3e\x22", "negative"),
        ):
            with self.subTest(response=response):
                attempts, responses, events = {}, {}, []
                with mock.patch.object(uds_session.uds, "drain"), \
                     mock.patch.object(uds_session.uds, "request", return_value=(response, "status")) as request, \
                     mock.patch.object(uds_session.time, "monotonic", side_effect=[10.0, 10.1234]):
                    if category == "positive_echo":
                        result = uds_session.tester_present(self.sock, 0.75, attempts, responses, events)
                        self.assertIs(result, events[0])
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            uds_session.tester_present(self.sock, 0.75, attempts, responses, events)
                        self.assertEqual(str(caught.exception),
                                         f"TesterPresent was not acknowledged with exact 7E 00 echo ({category}); "
                                         "explicit session is uncertain")
                request.assert_called_once_with(self.sock, b"\x3e\x00", timeout=0.75, retries=0)
                self.assertEqual(attempts, {"tester_present": 1})
                self.assertEqual(responses, {"tester_present": 1} if response else {})
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0], {
                    "request_hex": "3E 00", "response_hex": uds_session.uds.hx(response) if response else None,
                    "category": category, "validated_echo": category == "positive_echo", "status": "status",
                    "negative_response": uds_session.uds.negative_response_details(response), "elapsed_s": 0.123,
                })

    def test_keepalive_transport_exception_event(self):
        error = OSError("link lost")
        attempts, responses, events = {}, {}, []
        with mock.patch.object(uds_session.uds, "drain"), \
             mock.patch.object(uds_session.uds, "request", side_effect=error), \
             mock.patch.object(uds_session.time, "monotonic", side_effect=[1.0, 1.25]):
            with self.assertRaises(RuntimeError) as caught:
                uds_session.tester_present(self.sock, 0.75, attempts, responses, events)
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(str(caught.exception),
                         "TesterPresent transport failure; explicit session is uncertain")
        self.assertEqual(attempts, {"tester_present": 1})
        self.assertEqual(responses, {})
        self.assertEqual(events, [{
            "request_hex": "3E 00", "response_hex": None, "category": "transport_error",
            "validated_echo": False, "status": "OSError: link lost", "negative_response": None,
            "elapsed_s": 0.25,
        }])

    def test_keepalive_does_not_catch_keyboard_interrupt(self):
        events = []
        with mock.patch.object(uds_session.uds, "drain"), \
             mock.patch.object(uds_session.uds, "request", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                uds_session.tester_present(self.sock, 0.75, {}, {}, events)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
