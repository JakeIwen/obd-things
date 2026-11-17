"""The research worker's status stays fast and its replies survive callers that leave.

The first real install (2026-11-17) saw ``GET /status`` miss a 10 s curl deadline while the worker
hashed a freshly installed 348 MiB CLI copy inline, and two ``BrokenPipeError`` tracebacks when the
timed-out callers had gone. These tests use real HTTP exchanges over the worker's Unix socket (the
advisor's own ``WorkerClient``), with the slow parts replaced by ones that block until released.
"""

import contextlib
import io
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from projects.vehicle_data import dtc_research_worker, research_cli_policy
from projects.vehicle_data.assistant_http import QuietDisconnects
from projects.vehicle_data.assistant_models import provider_available
from projects.vehicle_data.dtc_research_worker import STATUS_WAIT_SECONDS, WorkerHandler, WorkerServer, dispatch
from projects.vehicle_data.model_cli_lock import ACQUIRED, CliLock
from projects.vehicle_data.research_attempts import Refused
from projects.vehicle_data.research_worker_client import WorkerClient
from projects.vehicle_data.research_worker_facts import DnsProbe, Facts, refusal_detail
from projects.vehicle_data.warning_chat import ChatHandler, ChatServer, WarningChatManager
from tests.research_support import WorkerRig

# The bound a caller may rely on: the worker's own wait plus generous room for a busy test host.
STATUS_BOUND = STATUS_WAIT_SECONDS + 1.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class BlockingResolver:
    """A resolver that answers only when told to: ``answer()`` succeeds, ``fail()`` raises."""

    def __init__(self):
        self.release = threading.Event()
        self.error = None
        self.calls = 0

    def __call__(self, *_args):
        self.calls += 1
        self.release.wait(30)
        if self.error:
            raise self.error
        return [()]

    def answer(self):
        self.release.set()

    def fail(self, error):
        self.error = error
        self.release.set()


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


class Served:
    """A real ``WorkerServer`` on a short Unix socket path (macOS limits them to about 104 bytes)."""

    def __init__(self, worker):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="rw")
        self.path = str(Path(self.dir.name) / "w.sock")
        self.server = WorkerServer(self.path, WorkerHandler)
        self.server.worker = worker
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def client(self, timeout=5):
        return WorkerClient(self.path, timeout=timeout)

    def raw(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(self.path)
        return sock

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()


class StatusBoundTests(unittest.TestCase):
    def serve(self, rig):
        self.addCleanup(rig.close)
        served = Served(rig.worker)
        self.addCleanup(served.close)
        return served

    def timed_status(self, served):
        began = time.monotonic()
        status = served.client().status()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, STATUS_BOUND, f"status took {elapsed:.2f} s")
        return status

    def test_status_answers_while_a_changed_copy_is_hashed_and_launches_wait_for_the_hash(self):
        rig = WorkerRig()
        served = self.serve(rig)
        release = threading.Event()
        real_file_list = research_cli_policy.file_list

        def slow_file_list(root):
            release.wait(30)
            return real_file_list(root)

        (rig.release / "extra").write_text("x")  # the copy changed: the next check must hash it
        with mock.patch.object(research_cli_policy, "file_list", slow_file_list):
            try:
                status = self.timed_status(served)
                self.assertFalse(status["checks_current"])
                self.assertEqual(status["providers"]["claude"]["identity"], "ok")  # the last check, dated
                self.assertIsNotNone(status["checked_at"])
                again = self.timed_status(served)  # a second caller does not queue behind the first
                self.assertFalse(again["checks_current"])
                with self.assertRaises(Refused) as refused:  # never launched on the stale "ok"
                    rig.worker.admit_turn(rig.turn_body())
                self.assertEqual(refused.exception.reason, "provider_unavailable")
                self.assertIn("still checking", refused.exception.detail)
            finally:
                release.set()
        self.assertTrue(wait_until(lambda: served.client().status()["providers"]["claude"]["identity"] == "changed"))
        self.assertEqual(rig.calls(), [])

    def test_status_before_the_first_check_says_checking(self):
        release = threading.Event()
        real_file_list = research_cli_policy.file_list

        def slow_file_list(root):
            release.wait(30)
            return real_file_list(root)

        with mock.patch.object(research_cli_policy, "file_list", slow_file_list):
            rig = WorkerRig(settle=False)
            served = self.serve(rig)
            try:
                status = self.timed_status(served)
            finally:
                release.set()
        self.assertEqual(status["providers"]["claude"]["identity"], "checking")
        self.assertIn("still checking", status["providers"]["claude"]["reason"])
        self.assertFalse(status["memory_policy"]["activated"])
        self.assertIsNone(status["checked_at"])
        self.assertTrue(wait_until(lambda: served.client().status()["providers"]["claude"]["identity"] == "ok"))

    def test_status_answers_while_the_dns_lookup_hangs(self):
        resolver = BlockingResolver()
        rig = WorkerRig(dns=DnsProbe(resolve=resolver, bound=.2))
        served = self.serve(rig)
        try:
            self.assertTrue(wait_until(lambda: resolver.calls == 1))
            time.sleep(.3)
            status = self.timed_status(served)
            self.assertFalse(status["dns_ok"])
            self.assertEqual(status["dns"]["result"], "pending")
            self.assertIn("no answer within 0.2 s", status["dns"]["reason"])
            self.assertIn("Public DNS", provider_available(status, "claude")[1])
            with self.assertRaises(Refused) as refused:
                rig.worker.admit_turn(rig.turn_body())
            self.assertEqual(refused.exception.reason, "provider_unavailable")
            self.assertIn("DNS", refused.exception.detail)
            self.assertIn("try again", refused.exception.detail)
            self.assertEqual(resolver.calls, 1)  # one lookup in flight at a time, however often asked
        finally:
            resolver.answer()
        self.assertTrue(wait_until(lambda: served.client().status()["dns_ok"]))
        _body, outcome = self.run_turn(rig)
        self.assertEqual(outcome["outcome"]["status"], "complete")

    def run_turn(self, rig):
        body = rig.turn_body()
        attempt = rig.worker.admit_turn(body)
        return body, rig.wait(attempt.attempt_id)

    def test_status_answers_while_maintenance_is_open_and_the_cli_lock_is_held(self):
        rig = WorkerRig()
        served = self.serve(rig)
        attempt_id = uuid.uuid4().hex
        code, _ = dispatch(rig.worker, "POST", "/maintenance", lambda: {
            "attempt_id": attempt_id, "expected_instance_id": rig.worker.table.instance_id, "purpose": "install"})
        self.assertEqual(code, 202)
        chat = CliLock(rig.lock_path)
        self.assertEqual(chat.try_acquire(), ACQUIRED)  # a warning chat holds the shared CLI lock
        try:
            status = self.timed_status(served)
        finally:
            chat.release()
        self.assertEqual(status["job"]["kind"], "maintenance")
        self.assertEqual(status["providers"]["claude"]["identity"], "ok")
        self.assertTrue(status["checks_current"])

    def test_status_answers_while_other_requests_are_in_flight(self):
        rig = WorkerRig()
        served = self.serve(rig)
        stalled = served.raw()
        stalled.sendall(b"POST /sweep HTTP/1.1\r\nHost: w\r\nContent-Length: 100\r\n\r\n{\"keep\"")
        silent = served.raw()
        try:
            status = self.timed_status(served)
        finally:
            stalled.close()
            silent.close()
        self.assertEqual(status["providers"]["claude"]["identity"], "ok")


class ClientGoneTests(unittest.TestCase):
    """A caller that leaves at any point of a reply costs one log line and no traceback."""

    def setUp(self):
        self.rig = WorkerRig()
        self.addCleanup(self.rig.close)
        self.served = Served(self.rig.worker)
        self.addCleanup(self.served.close)
        self.err = io.StringIO()
        redirect = contextlib.redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def gone_lines(self):
        return [json.loads(line) for line in self.err.getvalue().splitlines()
                if '"assistant_http_client_gone"' in line]

    def assert_quiet_and_serving(self, stage):
        self.assertTrue(wait_until(lambda: len(self.gone_lines()) == 1), self.err.getvalue())
        self.assertEqual(self.gone_lines()[0]["stage"], stage)
        self.assertEqual(self.gone_lines()[0]["server"], "research-worker")
        self.assertEqual(self.served.client().status()["providers"]["claude"]["identity"], "ok")
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_a_caller_gone_before_the_headers(self):
        asked, left = threading.Event(), threading.Event()

        def slow_dispatch(*_args):
            asked.set()
            left.wait(10)
            return 200, {"ok": True}

        with mock.patch.object(dtc_research_worker, "dispatch", slow_dispatch):
            sock = self.served.raw()
            sock.sendall(b"GET /status HTTP/1.1\r\nHost: w\r\n\r\n")
            self.assertTrue(asked.wait(5))
            sock.close()
            left.set()
            self.assertTrue(wait_until(lambda: len(self.gone_lines()) == 1), self.err.getvalue())
        self.assert_quiet_and_serving("reply")
        self.assertEqual(self.gone_lines()[0]["path"], "/status")

    def test_a_caller_gone_in_the_middle_of_the_body(self):
        with mock.patch.object(dtc_research_worker, "dispatch", lambda *_args: (200, {"pad": "x" * 4_000_000})):
            sock = self.served.raw()
            sock.sendall(b"GET /status HTTP/1.1\r\nHost: w\r\n\r\n")
            self.assertTrue(sock.recv(64).startswith(b"HTTP/1."))  # the headers arrived; the body has not
            sock.close()
            self.assertTrue(wait_until(lambda: len(self.gone_lines()) == 1), self.err.getvalue())
        self.assert_quiet_and_serving("reply")

    def test_a_caller_that_stalls_mid_request_is_dropped_quietly(self):
        with mock.patch.object(WorkerHandler, "timeout", .3):
            sock = self.served.raw()
            sock.sendall(b"POST /sweep HTTP/1.1\r\nHost: w\r\nContent-Length: 100\r\n\r\n{\"keep\"")
            self.assertEqual(sock.recv(64), b"")  # the worker hung up on it after its I/O timeout
            sock.close()
        self.assertEqual(self.served.client().status()["providers"]["claude"]["identity"], "ok")
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_a_connection_error_while_reading_is_one_line_and_other_errors_keep_their_traceback(self):
        server = QuietDisconnects()
        try:
            raise ConnectionResetError(104, "Connection reset by peer")
        except ConnectionResetError:
            server.handle_error(None, "")
        self.assertEqual(self.gone_lines()[0]["stage"], "request")
        self.assertNotIn("Traceback", self.err.getvalue())

        class Plain(QuietDisconnects, socketserver.BaseServer):
            def __init__(self):
                pass

        try:
            raise ValueError("a real handler bug")
        except ValueError:
            Plain().handle_error(None, "")
        self.assertIn("Traceback", self.err.getvalue())


class AdvisorClientGoneTests(unittest.TestCase):
    """The advisor's handler (the research routes' first hop after the web relay) has the same guard."""

    def test_a_caller_gone_before_the_advisor_replies(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = WarningChatManager(Path(tmp.name) / "advisor", object(), runner=None)
        asked, left = threading.Event(), threading.Event()

        class SlowResearch:
            def handle(self, *_args):
                asked.set()
                left.wait(10)
                return {"available": True}

        socket_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="ra")
        self.addCleanup(socket_dir.cleanup)
        path = str(Path(socket_dir.name) / "a.sock")
        server = ChatServer(path, ChatHandler)
        server.manager, server.research = chat, SlowResearch()
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
            sock.sendall((f"GET /v1/assistant/dtc-research HTTP/1.1\r\nHost: a\r\n"
                          f"X-Van-Assistant-Key: {chat.token}\r\nX-Van-Chat-Client: {'b' * 64}\r\n\r\n").encode())
            self.assertTrue(asked.wait(5))
            sock.close()
            left.set()
            self.assertTrue(wait_until(lambda: "assistant_http_client_gone" in err.getvalue()), err.getvalue())
        line = json.loads(next(l for l in err.getvalue().splitlines() if "assistant_http_client_gone" in l))
        self.assertEqual((line["server"], line["stage"]), ("advisor", "reply"))
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn(chat.token, err.getvalue())


class DnsProbeTests(unittest.TestCase):
    def probe(self, resolver, **kwargs):
        self.clock = Clock()
        return DnsProbe(resolve=resolver, clock=self.clock, wall=lambda: f"t{self.clock.now:.0f}", **kwargs)

    def test_success_failure_and_reason_with_when_it_was_measured(self):
        outcomes = [None, OSError(-3, "Temporary failure in name resolution")]

        def resolve(*_args):
            error = outcomes.pop(0)
            if error:
                raise error
            return [()]

        probe = self.probe(resolve)
        self.assertEqual(probe.view()["result"], "pending")
        self.assertFalse(probe.view()["ok"])
        self.assertIn("first public DNS check", refusal_detail(probe.view()))
        probe.start().join(5)
        view = probe.view()
        self.assertEqual((view["ok"], view["result"], view["reason"], view["checked_at"]), (True, "ok", None, "t1000"))
        self.clock.now += 100
        probe.start().join(5)
        view = probe.view()
        self.assertEqual((view["result"], view["checked_at"]), ("failed", "t1100"))
        self.assertEqual(view["reason"], "OSError: [Errno -3] Temporary failure in name resolution")

    def test_one_failure_after_a_recent_success_keeps_dns_usable_until_the_success_is_stale(self):
        outcomes = [None, OSError(-3, "Temporary failure in name resolution")]

        def resolve(*_args):
            error = outcomes.pop(0)
            if error:
                raise error
            return [()]

        probe = self.probe(resolve)
        probe.start().join(5)
        self.clock.now += 100
        probe.start().join(5)
        view = probe.view()
        self.assertTrue(view["ok"])  # last success 100 s ago: within the 300 s freshness rule
        self.assertEqual(view["result"], "failed")
        self.assertIn("Temporary failure in name resolution", view["reason"])
        self.assertEqual(view["checked_at"], "t1100")
        self.assertEqual(view["last_ok_at"], "t1000")
        self.clock.now += 201  # 301 s after the success
        self.assertFalse(probe.view()["ok"])
        detail = refusal_detail(probe.view())
        self.assertIn("public DNS has not answered", detail)
        self.assertIn("Temporary failure", detail)
        self.assertIn("try again", detail)

    def test_a_lookup_that_does_not_answer_is_reported_after_its_bound_and_counts_when_it_does(self):
        resolver = BlockingResolver()
        probe = self.probe(resolver, bound=10)
        thread = probe.start()
        self.assertIsNone(probe.start(force=True))  # never a second lookup beside one in flight
        self.clock.now += 11
        view = probe.view()
        self.assertFalse(view["ok"])
        self.assertEqual(view["result"], "pending")
        self.assertIn("no answer within 10 s", view["reason"])
        resolver.answer()
        thread.join(5)
        view = probe.view()
        self.assertTrue(view["ok"])
        self.assertEqual(view["seconds"], 11.0)

    def test_lookups_are_spaced_by_the_gap(self):
        calls = []
        probe = self.probe(lambda *args: calls.append(args) or [()], gap=30)
        probe.start().join(5)
        self.assertIsNone(probe.start())
        self.clock.now += 31
        probe.start().join(5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], socket.AF_INET)  # IPv4 only: this Pi has IPv6 disabled


class LaunchGateTests(unittest.TestCase):
    def test_launch_gate_with_a_fresh_and_a_stale_dns_success(self):
        clock = Clock()
        state = {"error": None, "calls": 0}

        def resolve(*_args):
            state["calls"] += 1
            if state["error"]:
                raise state["error"]
            return [()]

        probe = DnsProbe(resolve=resolve, clock=clock)
        rig = WorkerRig(dns=probe)  # its first background check starts the first lookup: a success
        self.addCleanup(rig.close)
        self.assertTrue(wait_until(lambda: probe.view()["result"] == "ok"))
        clock.now += 100
        state["error"] = OSError(-2, "Name or service not known")
        probe.start().join(5)  # a failure 100 s after the success
        self.assertEqual(probe.view()["result"], "failed")
        attempt = rig.worker.admit_turn(rig.turn_body())  # fresh success: admitted despite the newer failure
        self.assertEqual(rig.wait(attempt.attempt_id)["outcome"]["status"], "complete")
        clock.now += 250  # the success is now 350 s old
        calls = state["calls"]
        with self.assertRaises(Refused) as refused:
            rig.worker.admit_turn(rig.turn_body())
        self.assertEqual(refused.exception.reason, "provider_unavailable")
        self.assertIn("public DNS has not answered", refused.exception.detail)
        self.assertIn("Name or service not known", refused.exception.detail)
        self.assertTrue(wait_until(lambda: state["calls"] == calls + 1))  # the refusal asked again
        self.assertTrue(wait_until(lambda: probe.in_flight_since is None))
        state["error"] = None
        clock.now += 31
        probe.start().join(5)
        attempt = rig.worker.admit_turn(rig.turn_body())  # the retry is admitted once DNS answers
        self.assertEqual(rig.wait(attempt.attempt_id)["outcome"]["status"], "complete")
        self.assertEqual(len(rig.calls()), 2)


class FactsTests(unittest.TestCase):
    def test_current_waits_only_its_bound_and_keeps_the_last_value(self):
        release = threading.Event()
        values = iter([{"n": 1}, {"n": 2}])

        def compute():
            value = next(values)
            if value["n"] == 2:
                release.wait(10)
            return value

        facts = Facts(compute, wall=lambda: "now")
        self.addCleanup(facts.stop)
        self.assertEqual(facts.current(5), ({"n": 1}, "now", True))
        began = time.monotonic()
        value, _at, current = facts.current(.2)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual((value, current), ({"n": 1}, False))
        release.set()
        self.assertTrue(wait_until(lambda: facts.value == {"n": 2}))

    def test_a_failed_refresh_keeps_the_previous_value(self):
        results = iter([{"n": 1}, RuntimeError("boom"), {"n": 3}])

        def compute():
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value

        facts = Facts(compute, wall=lambda: "now")
        self.addCleanup(facts.stop)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(facts.current(5), ({"n": 1}, "now", True))
            self.assertEqual(facts.current(5), ({"n": 1}, "now", False))  # failed: the last value, not current
            self.assertEqual(facts.current(5), ({"n": 3}, "now", True))
        self.assertIn("dtc_research_facts_failed", err.getvalue())


class PickerReasonTests(unittest.TestCase):
    def test_the_picker_says_dns_is_still_being_checked_or_has_not_answered(self):
        base = {"providers": {"claude": {"identity": "ok", "signed_in": True}}, "memory_policy": {"activated": True}}
        pending = {**base, "dns_ok": False, "dns": {"result": "pending", "reason": None, "fresh_seconds": 300}}
        self.assertIn("still checking public DNS", provider_available(pending, "claude")[1])
        failed = {**base, "dns_ok": False, "dns": {"result": "failed", "reason": "gaierror", "fresh_seconds": 300}}
        self.assertIn("last 5 minutes", provider_available(failed, "claude")[1])
        older_worker = {**base, "dns_ok": False}
        self.assertIn("Public DNS", provider_available(older_worker, "claude")[1])
        self.assertEqual(provider_available({**base, "dns_ok": True}, "claude"), (True, None))


if __name__ == "__main__":
    unittest.main()
