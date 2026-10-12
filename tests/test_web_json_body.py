"""Each web POST route keeps its own answer for a missing, oversized or malformed body.

The routes share one body reader (``TelemetryWebHandler._read_json``); these are the
answers each gave before they did.
"""

import json
import socket
import threading
import unittest

from projects.vehicle_data import web


class Controller:
    def start(self):
        return {"state": "queued"}

    def cancel(self):
        return {"state": "cancel_requested"}


INVALID = {"available": False, "reason": "invalid_request"}
SIZE = {**INVALID, "detail": "request body size is invalid"}


class WebJsonBodyTests(unittest.TestCase):
    def setUp(self):
        self.server = web.TelemetryWebServer(
            ("127.0.0.1", 0), socket_path="/nonexistent/api.sock", allow_acquisitions=True,
            stream_interval_seconds=1.0, stream_max_seconds=1.0, dtc_controller=Controller(),
            warning_chat_socket="/nonexistent/chat.sock", radar_service_origins=[])
        self.server.RequestHandlerClass = type(
            "QuietHandler", (web.TelemetryWebHandler,), {"log_message": lambda *args: None})
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.server.dtc_trusted_origin = self.origin
        self.server.telemetry_client = type("Client", (), {"request": staticmethod(
            lambda method, path, payload=None: (200, {"echo": payload}))})()
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body=b"", length=None, **headers):
        head = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
                "Origin": self.origin, "Connection": "close", **headers}
        if length is not None or body:
            head["Content-Length"] = length if length is not None else str(len(body))
        request = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in head.items())
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(request.encode() + b"\r\n" + body)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        head, _, payload = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    def test_size_errors_are_answered_before_any_body_is_read(self):
        for length in ("0", "abc", "-5", "999999"):
            with self.subTest(length=length):
                for path in ("/v1/acquisitions/battery.voltage", "/v1/diagnostics/dtc-jobs"):
                    self.assertEqual(self.post(path, length=length), (400, SIZE))
                self.assertEqual(self.post("/v1/assistant/chats", length=length,
                                           **{"X-Van-Assistant-Key": "k" * 40, "X-Van-Chat-Client": "k" * 40}),
                                 (400, {"available": False, "detail": "Invalid assistant request body"}))
        self.assertEqual(self.post("/v1/maintenance/oil-changes", length="999999"),
                         (400, {"available": False, "detail": "Invalid service record size"}))
        self.assertEqual(self.post("/v1/radar/service-alignment", length="0"), (400, SIZE))

    def test_malformed_json_gets_each_route_s_own_answer(self):
        self.assertEqual(self.post("/v1/acquisitions/battery.voltage", b"{oops"), (400, {
            **INVALID, "detail": "only the approved acquisition modes are accepted"}))
        code, reply = self.post("/v1/diagnostics/dtc-jobs", b"{oops")
        self.assertEqual((code, reply["reason"]), (409, "dtc_job_rejected"))
        code, reply = self.post("/v1/maintenance/oil-changes", b"{oops")
        self.assertEqual(code, 400)
        self.assertIn("Expecting property name", reply["detail"])
        code, reply = self.post("/v1/radar/service-alignment", b"{oops")
        self.assertEqual((code, reply["reason"]), (400, "invalid_request"))
        self.assertEqual(self.post("/v1/radar/service-alignment", b"[1]"),
                         (400, {**INVALID, "detail": "body must be one JSON object"}))

    def test_well_formed_bodies_reach_the_broker_or_controller(self):
        self.assertEqual(self.post("/v1/acquisitions/battery.voltage", b'{"mode":"passive"}'),
                         (200, {"echo": {"mode": "passive"}}))
        self.assertEqual(self.post("/v1/maintenance/oil-changes", b'{"date":"2026-10-01"}'),
                         (200, {"echo": {"date": "2026-10-01"}}))
        self.assertEqual(self.post("/v1/diagnostics/dtc-jobs/current/cancel", b'{"action":"cancel"}'),
                         (202, {"state": "cancel_requested"}))
        self.assertEqual(self.post("/v1/radar/service-alignment", b'{"action":"stop"}'),
                         (200, {"echo": {"action": "stop"}}))


if __name__ == "__main__":
    unittest.main()
