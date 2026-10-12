"""Broker relay, local API and web gating for the radar Service Drive Alignment."""

import http.client
import io
import json
import pathlib
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from datetime import datetime, timezone

from projects.vehicle_data import api_server, radar_reference, radar_service_alignment as service, web
from projects.vehicle_data import radar_service_control as control
from projects.vehicle_data.broker import ActiveDriveSupervisor, TelemetryBroker
from projects.vehicle_data.radar_alignment import AlignmentHistory
from tests.radar_service_support import Clock, record_reading
from tests.vehicle_data_api_concurrency_support import read_http, unix_socket


REPO = pathlib.Path(__file__).resolve().parents[1]
START = {"action": "start", "confirm_stationary_engine_running": True,
         "confirm_drive_without_acc": True}
STOP = {"action": "stop"}


def event(state, **fields):
    base = dict(type=service.EVENT_TYPE, state=state, reason=None, detail="test", supported=True,
                request_id=None, progress=None, routine_status=None, elapsed_seconds=None,
                speed_mph=None, interface_mode="armed_diagnostic")
    return {**base, **fields}


class FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def commands(self):
        return [json.loads(line) for line in self.stdin.getvalue().splitlines()]


class Helper:
    """A control wired to a fake helper process through the real factory wrapper."""

    def __init__(self, log_dir=None, history=None):
        self.clock = Clock()
        self.process = FakeProcess()
        self.popen_calls = []
        self.forwarded = []
        self.control = control.RadarServiceControl(
            history=history, log_dir=log_dir, popen_factory=self._popen, monotonic=self.clock)
        self.supervisor_kwargs = self.control.supervisor(
            lambda **kwargs: kwargs, event_handler=self.forwarded.append, channel="can7")

    def _popen(self, command, **kwargs):
        self.popen_calls.append((command, kwargs))
        return self.process

    def launch(self, announce=True):
        self.supervisor_kwargs["popen_factory"](["python3", "active_drive.py"], text=True)
        if announce:
            self.emit(event(service.IDLE))
        return self

    def emit(self, payload):
        self.supervisor_kwargs["event_handler"](payload)

    def run(self):
        return self.control.status_response()[1]["run"]


class ControlTests(unittest.TestCase):
    def test_helper_is_started_with_the_flag_and_a_stdin_pipe_and_other_events_pass_through(self):
        helper = Helper().launch()
        command, kwargs = helper.popen_calls[0]
        self.assertEqual(command, ["python3", "active_drive.py", service.ENABLE_FLAG])
        self.assertEqual(kwargs, {"stdin": subprocess.PIPE, "text": True})
        self.assertEqual(helper.supervisor_kwargs["channel"], "can7")
        status = {"type": "status", "state": "armed_diagnostic"}
        helper.emit(status)
        self.assertEqual(helper.forwarded, [status])

    def test_start_needs_a_running_helper_that_announced_support(self):
        helper = Helper()
        code, body = helper.control.request(START)
        self.assertEqual((code, body["reason"]), (409, "helper_not_ready"))
        helper.launch(announce=False)
        self.assertEqual(helper.control.request(START)[1]["reason"], "helper_not_ready")
        helper.emit(event(service.IDLE, supported=False))
        self.assertEqual(helper.control.request(START)[1]["reason"], "helper_not_ready")
        self.assertEqual(helper.process.commands(), [])
        helper.emit(event(service.IDLE))
        code, body = helper.control.request(START)
        self.assertEqual((code, body["run"]["state"]), (202, service.REQUESTED))
        self.assertEqual(helper.process.commands(),
                         [{"command": "start", "request_id": body["run"]["request_id"]}])

    def test_only_a_completed_run_is_handed_to_the_reference_points(self):
        helper = Helper().launch()
        waiting = lambda: helper.control.references.status_response()[1]["pending_calibration"]
        for end in (service.FAILED, service.STOPPED, service.COMPLETED):
            helper.clock.value += control.START_MIN_INTERVAL_SECONDS
            request_id = helper.control.request(START)[1]["run"]["request_id"]
            helper.emit(event(service.RUNNING, request_id=request_id, progress=40))
            self.assertIsNone(waiting())
            reason = {service.FAILED: "stalled", service.STOPPED: "owner_stopped"}.get(end)
            helper.emit(event(end, request_id=request_id, reason=reason))
            if end != service.COMPLETED:
                self.assertIsNone(waiting(), end)
        self.assertEqual(waiting()["request_id"], request_id)

    def test_only_the_exact_bodies_are_accepted(self):
        helper = Helper().launch()
        for body in (None, [], {}, {"action": "start"}, {**START, "extra": 1},
                     {**START, "confirm_drive_without_acc": 1},
                     {**START, "confirm_stationary_engine_running": False},
                     {"action": "stop", "request_id": "x"}, {"action": "10 03"}):
            code, reply = helper.control.request(body)
            self.assertEqual((code, reply["reason"]), (400, "invalid_request"), body)
        self.assertEqual(helper.process.commands(), [])

    def test_run_is_tracked_logged_and_finished_by_helper_events(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Helper(directory).launch()
            request_id = helper.control.request(START)[1]["run"]["request_id"]
            helper.emit(event(service.STARTING, request_id=request_id))
            helper.emit(event(service.RUNNING, request_id=request_id, progress=37,
                              routine_status="00 01 25 00", elapsed_seconds=61.5, speed_mph=41.0))
            run = helper.run()
            self.assertEqual((run["state"], run["progress"], run["speed_mph"]),
                             (service.RUNNING, 37, 41.0))
            self.assertIsNone(run["finished_at"])
            self.assertEqual(helper.control.request(START)[1]["reason"], "already_active")
            # A heartbeat without a progress byte keeps the last one.
            helper.emit(event(service.RUNNING, request_id=request_id, elapsed_seconds=70.0))
            self.assertEqual(helper.run()["progress"], 37)
            helper.emit(event(service.COMPLETED, request_id=request_id, progress=100,
                              routine_status="00 03 64 00", elapsed_seconds=1013.4))
            run = helper.run()
            self.assertEqual((run["state"], run["progress"]), (service.COMPLETED, 100))
            self.assertIsNotNone(run["finished_at"])
            self.assertEqual(helper.forwarded, [])
            log, = pathlib.Path(directory).glob("*.jsonl")
            self.assertIn(request_id[:8], log.name)
            states = [json.loads(line)["state"] for line in log.read_text().splitlines()]
            self.assertEqual(states, [service.REQUESTED, service.STARTING, service.RUNNING,
                                      service.RUNNING, service.COMPLETED])

    def test_events_for_another_request_are_not_tracked(self):
        helper = Helper().launch()
        helper.control.request(START)
        helper.emit(event(service.RUNNING, request_id="f" * 32, progress=90))
        self.assertEqual(helper.run()["state"], service.REQUESTED)

    def test_stop_is_relayed_only_for_an_active_run_and_restart_is_rate_limited(self):
        helper = Helper().launch()
        self.assertEqual(helper.control.request(STOP)[1]["reason"], "not_active")
        request_id = helper.control.request(START)[1]["run"]["request_id"]
        helper.emit(event(service.RUNNING, request_id=request_id, progress=5))
        code, body = helper.control.request(STOP)
        self.assertEqual((code, body["run"]["stop_requested"]), (202, True))
        self.assertEqual(helper.process.commands()[-1], {"command": "stop", "request_id": request_id})
        helper.emit(event(service.STOPPED, request_id=request_id, reason="owner_stopped"))
        self.assertEqual(helper.run()["reason"], "owner_stopped")
        self.assertEqual(helper.control.request(START)[0], 429)
        helper.clock.value += control.START_MIN_INTERVAL_SECONDS
        self.assertEqual(helper.control.request(START)[0], 202)

    def test_helper_exit_ends_an_active_run(self):
        helper = Helper().launch()
        request_id = helper.control.request(START)[1]["run"]["request_id"]
        helper.emit(event(service.RUNNING, request_id=request_id, progress=60))
        helper.process.returncode = 0
        body = helper.control.status_response()[1]
        self.assertFalse(body["helper"]["running"])
        self.assertEqual((body["run"]["state"], body["run"]["reason"], body["run"]["progress"]),
                         (service.FAILED, "helper_ended", 60))
        # The next engine start is a new helper that has not announced support yet.
        helper.process = FakeProcess()
        helper.launch(announce=False)
        helper.clock.value += 60
        self.assertEqual(helper.control.request(START)[1]["reason"], "helper_not_ready")

    def test_unacknowledged_request_expires_but_a_late_report_is_still_believed(self):
        helper = Helper().launch()
        request_id = helper.control.request(START)[1]["run"]["request_id"]
        helper.clock.value += control.REQUEST_ACK_SECONDS + 1
        self.assertEqual(helper.run()["reason"], "not_delivered")
        helper.emit(event(service.RUNNING, request_id=request_id, progress=3))
        run = helper.run()
        self.assertEqual((run["state"], run["reason"], run["finished_at"]),
                         (service.RUNNING, None, None))

    def test_broken_pipe_is_reported_as_not_delivered(self):
        helper = Helper().launch()
        helper.process.stdin.close()
        code, body = helper.control.request(START)
        self.assertEqual((code, body["reason"], body["run"]["state"]),
                         (503, "not_delivered", service.FAILED))

    def test_malformed_helper_events_are_rejected(self):
        good = event(service.RUNNING, request_id="a" * 32, progress=10, routine_status="00 01 0A 00")
        control.validate_event(good)
        for change in ({"state": "armed"}, {"reason": "because"}, {"progress": 101},
                       {"progress": True}, {"routine_status": "00 01 0A"}, {"request_id": "A" * 32},
                       {"supported": 1}, {"interface_mode": "listen_only"}, {"detail": "x" * 401},
                       {"speed_mph": float("nan")}, {"elapsed_seconds": -1}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                control.validate_event({**good, **change})
        with self.assertRaises(ValueError):
            control.validate_event({k: v for k, v in good.items() if k != "speed_mph"})
        helper = Helper().launch()
        with self.assertRaises(ValueError):
            helper.emit({**good, "state": "armed"})


HELPER_SCRIPT = textwrap.dedent('''
    import json, sys, time
    sys.path.insert(0, {repo!r})
    from projects.vehicle_data import radar_service_alignment as service

    def emit(event_type, **payload):
        print(json.dumps({{"type": event_type, **payload}}), flush=True)

    def report(state, request_id=None, reason=None, progress=None):
        emit(service.EVENT_TYPE, state=state, reason=reason, detail="scripted", supported=True,
             request_id=request_id, progress=progress, routine_status=None,
             elapsed_seconds=None, speed_mph=0.0, interface_mode="armed_diagnostic")

    assert service.ENABLE_FLAG in sys.argv
    reader = service.CommandReader(sys.stdin.fileno())
    report(service.IDLE)
    deadline = time.monotonic() + 20
    done = False
    while not done and time.monotonic() < deadline:
        for command in reader.poll():
            if command["command"] == "start":
                report(service.RUNNING, command["request_id"], progress=50)
            else:
                report(service.STOPPED, command["request_id"], reason="owner_stopped")
                done = True
        time.sleep(0.02)
    emit("final", state="idle", reason="engine_not_running", detail="scripted end",
         interface_mode="listen_only", restored=True)
''')


class RealPipeTests(unittest.TestCase):
    def wait_for(self, predicate, seconds=15.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition was not reached")

    def test_commands_reach_a_supervised_child_and_its_events_come_back(self):
        with tempfile.TemporaryDirectory() as directory:
            script = pathlib.Path(directory) / "helper.py"
            script.write_text(HELPER_SCRIPT.format(repo=str(REPO)))
            service_control = control.RadarServiceControl(log_dir=directory)
            forwarded = []
            supervisor = service_control.supervisor(
                ActiveDriveSupervisor, event_handler=forwarded.append, channel="can7",
                expected_usb_serial="serial-a", expected_dev_id=0, helper_path=script)
            result = {}
            thread = threading.Thread(
                target=lambda: result.update(final=supervisor.run(threading.Event())))
            thread.start()
            try:
                self.wait_for(lambda: service_control.status_response()[1]["helper"]["ready"])
                code, body = service_control.request(START)
                self.assertEqual(code, 202)
                self.wait_for(lambda: service_control.status_response()[1]["run"]["state"]
                              == service.RUNNING)
                self.assertEqual(service_control.request(STOP)[0], 202)
                self.wait_for(lambda: service_control.status_response()[1]["run"]["state"]
                              == service.STOPPED)
            finally:
                thread.join(30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["final"]["reason"], "engine_not_running")
            self.assertEqual(forwarded, [])
            status = service_control.status_response()[1]
            self.assertFalse(status["helper"]["running"])
            self.assertEqual(status["run"]["progress"], 50)


class WebRelayTests(unittest.TestCase):
    """Dashboard listener -> broker API -> control, with the listener's gates."""

    def setUp(self):
        # A short path: AF_UNIX names are limited to about 100 bytes.
        self.tmp = tempfile.TemporaryDirectory(prefix="rs-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        socket_path = str(pathlib.Path(self.tmp.name) / "api.sock")
        self.broker = TelemetryBroker(acquirer=mock.Mock(channel="can7"))
        self.addCleanup(self.broker.close)
        self.history = AlignmentHistory()
        self.helper = Helper(self.tmp.name, history=self.history)
        self.broker.radar_service = self.helper.control
        self.socket_path = socket_path
        self.api = api_server.UnixHTTPServer(socket_path, self.broker)
        self.web = web.TelemetryWebServer(
            ("127.0.0.1", 0), socket_path=socket_path, allow_acquisitions=False,
            stream_interval_seconds=1.0, stream_max_seconds=1.0, warning_chat_socket=None)
        self.port = self.web.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        for server in (self.api, self.web):
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def enable(self):
        self.web.radar_service_origins = frozenset({self.origin})

    def http(self, method, body=None, headers=None, path=service.API_PATH):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            base = {"Origin": self.origin, "Content-Type": "application/json"} if method == "POST" else {}
            connection.request(method, path, body=data, headers={**base, **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_listener_without_the_flag_refuses_both_methods_and_reports_it(self):
        self.assertEqual(self.http("GET")[1]["reason"], "radar_service_disabled")
        code, body = self.http("POST", START)
        self.assertEqual((code, body["reason"]), (403, "radar_service_disabled"))
        self.assertEqual(self.helper.process.commands(), [])

    def test_enabled_listener_relays_only_trusted_same_origin_json(self):
        self.enable()
        self.helper.launch()
        code, body = self.http("GET")
        self.assertEqual((code, body["helper"]["ready"], body["run"]), (200, True, None))
        for headers, reason in (
                ({"Origin": "http://evil.example:8765"}, "origin_rejected"),
                ({"Origin": f"http://localhost:{self.port}"}, "origin_rejected"),
                ({"Sec-Fetch-Site": "cross-site"}, "origin_rejected"),
                ({"Content-Type": "text/plain"}, "invalid_content_type")):
            code, body = self.http("POST", START, headers)
            self.assertEqual(body["reason"], reason, headers)
        self.assertEqual(self.http("POST", b"[1]")[1]["reason"], "invalid_request")
        self.assertEqual(self.http("POST", {"action": "start"})[1]["reason"], "invalid_request")
        self.assertEqual(self.helper.process.commands(), [])
        code, body = self.http("POST", START)
        self.assertEqual((code, body["run"]["state"]), (202, service.REQUESTED))
        request_id = body["run"]["request_id"]
        self.assertEqual(self.helper.process.commands(), [{"command": "start", "request_id": request_id}])
        self.helper.emit(event(service.RUNNING, request_id=request_id, progress=12))
        self.assertEqual(self.http("GET")[1]["run"]["progress"], 12)
        code, body = self.http("POST", STOP)
        self.assertEqual((code, body["run"]["stop_requested"]), (202, True))

    def test_reference_points_are_relayed_without_the_flag_to_same_origin_json_only(self):
        path = radar_reference.API_PATH
        code, body = self.http("GET", path=path)
        self.assertEqual((code, body["points"], body["current"]), (200, [], None))
        record_reading(self.history, datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc), -3.766, -0.068)
        for headers, detail in (
                ({"Origin": "http://evil.example:8765"}, "Radar references require a same-origin request"),
                ({"Sec-Fetch-Site": "cross-site"}, "Radar references require a same-origin request"),
                ({"Content-Type": "text/plain"}, "Radar references require JSON")):
            code, body = self.http("POST", {"action": "zero"}, headers, path=path)
            self.assertEqual(body, {"available": False, "detail": detail}, headers)
        self.assertEqual(self.http("POST", b"{oops", path=path)[1]["reason"], "invalid_request")
        self.assertEqual(self.http("POST", {"action": "start"}, path=path)[1]["reason"], "invalid_request")
        code, body = self.http("POST", {"action": "zero"}, path=path)
        self.assertEqual((code, body["points"][0]["angles"]), (201, {"elevation": -3.766, "azimuth": -0.068}))
        self.assertEqual(self.http("GET", path=path)[1]["selected"], body["selected"])
        self.assertEqual(self.http("POST", {"action": "clear"}, path=path)[1]["selected"], None)
        self.assertEqual(self.helper.process.commands(), [])

    def api_post(self, head: bytes, body: bytes):
        with unix_socket(self.socket_path, 5.0) as sock:
            sock.sendall(b"POST " + service.API_PATH.encode() + b" HTTP/1.1\r\nHost: local\r\n"
                         + head + b"\r\n" + body)
            return read_http(sock, 5.0)

    def test_broker_api_rejects_malformed_posts_with_the_established_envelope(self):
        # These three refusals moved into one helper to make room for the new routes.
        for head, body, detail in (
                (b"Content-Length: nope\r\n", b"", "invalid Content-Length"),
                (b"Content-Length: 0\r\n", b"", "request body size is invalid"),
                (b"Content-Length: 99999\r\n", b"", "request body size is invalid"),
                (b"Content-Length: 5\r\n", b"{oops", "body must be one JSON object")):
            code, reply = self.api_post(head, body)
            self.assertEqual((code, reply), (400, {
                "available": False, "reason": "invalid_request", "detail": detail}), head)
        code, reply = self.api_post(b"Content-Length: 17\r\n", b'{"action":"stop"}')
        self.assertEqual((code, reply["reason"]), (409, "not_active"))

    def test_every_broker_has_its_own_control_and_reports_an_idle_helper(self):
        other = TelemetryBroker(acquirer=mock.Mock(channel="can7"))
        self.addCleanup(other.close)
        self.assertIsNot(other.radar_service, TelemetryBroker(
            acquirer=mock.Mock(channel="can7")).radar_service)
        code, body = other.radar_service.status_response()
        self.assertEqual((code, body["helper"]["running"], body["run"]), (200, False, None))

    def test_flag_and_origin_arguments(self):
        parser = web.build_parser()
        self.assertIsNone(web.radar_service_origins_from_args(parser.parse_args([])))
        self.assertEqual(web.radar_service_origins_from_args(
            parser.parse_args(["--enable-radar-service-alignment"])), [])
        args = parser.parse_args(["--bind", "192.168.6.103", "--allow-remote-bind",
                                  "--enable-radar-service-alignment",
                                  "--radar-service-origin", "http://vanpi.lan:8765/"])
        self.assertEqual(web.radar_service_origins_from_args(args), ["http://vanpi.lan:8765"])
        for argv in (["--radar-service-origin", "http://vanpi.lan:8765"],
                     ["--enable-radar-service-alignment", "--radar-service-origin", "vanpi.lan"],
                     ["--enable-radar-service-alignment", "--radar-service-origin",
                      "http://vanpi.lan:8765/path"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                web.radar_service_origins_from_args(parser.parse_args(argv))
        self.assertIs(web.web_flags(self.web)["radar_service_enabled"], False)
        self.enable()
        self.assertIs(web.web_flags(self.web)["radar_service_enabled"], True)

    def test_an_enabled_listener_trusts_its_bound_origin_and_the_listed_extras(self):
        server = web.TelemetryWebServer(
            ("127.0.0.1", 0), socket_path=self.socket_path, allow_acquisitions=False,
            stream_interval_seconds=1.0, stream_max_seconds=1.0, warning_chat_socket=None,
            radar_service_origins=["http://vanpi.lan:8765"])
        try:
            port = server.server_address[1]
            self.assertEqual(server.radar_service_origins, {
                f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://vanpi.lan:8765"})
            # The chat origins come from the same rule and keep their own extras.
            self.assertEqual(server.warning_chat_origins,
                             {f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
