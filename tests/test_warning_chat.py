import json
import io
import http.client
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
from unittest import mock

from projects.vehicle_data.warning_chat import (
    ChatError, CodexRunner, WarningChatManager, event_context, resolve_settings, RunTiming,
)
from projects.vehicle_data.web import TelemetryWebServer


class CachedEvidence:
    def __init__(self):
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if path.startswith("/v1/events/"):
            return 404, {"available": False}
        if path == "/v1/health":
            return 200, {"available": True, "episodes": {"active": [{
                "id": 579, "title": "Coolant above history", "status": "active",
                "evidence_state": "unavailable", "latest_assessment": {
                    "metric": "engine.coolant_temperature", "state": "unavailable",
                    "persistence": {"observed": 0, "required": 10},
                }}]}, "assessments": [{"rule": "watch", "state": "watch", "metric": "battery.voltage"}],
                "data_quality": {"recent": [{"incident_id": "old:1", "status": "resolved",
                                              "metric": "transmission.oil_temperature"}]}}
        if path == "/v1/snapshot":
            return 200, {"status": {"vehicle_state": {"state": "asleep"}},
                         "metrics": {}, "catalog": []}
        if path == "/v1/history":
            return 200, {"metric_trends": {}}
        raise AssertionError(path)


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, context, turns, cancelled, diagnostics=None, progress=None):
        self.calls.append((context, turns))
        self.started.set()
        while not self.release.wait(.01):
            if cancelled.is_set():
                raise ChatError(409, "Stopped")
        return "Historical advisory; current evidence is unavailable."


class WarningChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = FakeRunner()
        self.evidence = CachedEvidence()
        self.manager = WarningChatManager(self.tmp.name, self.evidence, self.runner)
        self.owner = self.manager.authorize(self.manager.token, "b" * 64)

    def tearDown(self):
        self.runner.release.set()
        self.manager.close()
        self.tmp.cleanup()

    def open(self, request_id="a" * 32):
        with self.manager.lock:
            return self.manager.open_chat(self.owner, {
                "event": {"kind": "episode", "id": "579"}, "request_id": request_id})

    def finish(self):
        self.runner.release.set()
        self.manager.worker.join(timeout=2)
        self.assertFalse(self.manager.worker.is_alive())

    def test_server_resolves_evidence_and_never_acquires(self):
        for event in ({"kind": "episode", "id": "579"}, {"kind": "quality", "id": "old:1"},
                      {"kind": "assessment", "id": "watch"}):
            context = event_context(self.evidence, event)
            self.assertEqual(context["event_kind"], event["kind"])
        self.assertTrue(all(method == "GET" for method, _ in self.evidence.calls))
        with self.assertRaises(ChatError):
            event_context(self.evidence, {"kind": "episode", "id": "579", "command": "unsafe"})
        with self.assertRaises(ChatError):
            event_context(self.evidence, {"kind": "episode", "id": "999"})

    def test_auth_ownership_and_private_access_code(self):
        with self.assertRaises(ChatError) as error:
            self.manager.authorize("x" * 43, "b" * 64)
        self.assertEqual(error.exception.status, 401)
        self.assertEqual((Path(self.tmp.name) / "access-code").stat().st_mode & 0o777, 0o600)
        chat = self.open()
        with self.assertRaises(ChatError) as error:
            self.manager.get(chat["id"], "different-owner")
        self.assertEqual(error.exception.status, 404)
        self.assertNotIn("owner", chat)
        self.assertNotIn("context", chat)

    def test_double_click_and_followup_idempotency(self):
        chat = self.open()
        self.assertEqual(self.open("c" * 32)["id"], chat["id"])
        self.assertTrue(self.runner.started.wait(1))
        self.assertEqual(len(self.runner.calls), 1)
        self.finish()
        with self.manager.lock:
            record = self.manager.get(chat["id"], self.owner)
            followup = self.manager.start_turn(record, "Why is it still open?", "d" * 32)
            duplicate = self.manager.start_turn(record, "Why is it still open?", "d" * 32)
            self.assertEqual(len(duplicate["turns"]), 2)
        self.finish()
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.runner.calls[-1][1][0]["state"], "complete")
        self.assertNotIn(self.manager.token, json.dumps(followup))

    def test_global_single_job_cancel_and_restart_recovery(self):
        chat = self.open()
        with self.manager.lock, self.assertRaises(ChatError) as error:
            self.manager.open_chat(self.owner, {"event": {"kind": "quality", "id": "old:1"}, "request_id": "c" * 32})
        self.assertEqual(error.exception.status, 429)
        self.manager.cancelled.set()
        self.manager.worker.join(timeout=2)
        record = self.manager.get(chat["id"], self.owner)
        self.assertEqual(record["turns"][-1]["state"], "cancelled")
        record["turns"][-1]["state"] = "running"
        self.manager.save(record)
        recovered = WarningChatManager(self.tmp.name, self.evidence, FakeRunner())
        self.assertEqual(recovered.get(chat["id"], self.owner)["turns"][-1]["state"], "error")
        recovered.close()

    def test_new_chat_preserves_old_transcript_and_reopens_latest(self):
        first = self.open()
        self.finish()
        payload = {"event": first["event"], "request_id": "f" * 32, "new_chat": True}
        with self.manager.lock:
            second = self.manager.open_chat(self.owner, payload)
            duplicate = self.manager.open_chat(self.owner, payload)
        self.finish()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["id"], duplicate["id"])
        self.assertEqual(self.open()["id"], first["id"])  # original request ID is idempotent
        self.assertEqual(self.open("e" * 32)["id"], second["id"])
        self.assertEqual(len(self.manager.chats), 2)

    def test_runner_arguments_are_fixed_restricted_and_no_personal_tools(self):
        runner = CodexRunner("/fixed/codex", Path(self.tmp.name) / "runtime")
        with mock.patch("pathlib.Path.read_text", return_value=""):
            command = runner.command(Path(self.tmp.name) / "isolated")
        self.assertEqual(command[:2], ["/fixed/codex", "exec"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ephemeral", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('default_permissions="warning-chat"', command)
        self.assertIn("permissions.warning-chat.network.enabled=false", command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("features.plugins=false", command)
        self.assertIn("features.apps=false", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_jsonl_runner_returns_only_answer_and_preserves_conversation(self):
        runner = CodexRunner("/unused", Path(self.tmp.name) / "runtime", timeout=3)
        fixture = '''import json,sys
p=json.load(sys.stdin)
assert p["conversation"][0]["text"]=="first question"
assert p["conversation"][-1]["text"]=="follow-up"
print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"not user-facing"}}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"The actual answer"}}))
print(json.dumps({"type":"turn.completed"}))
'''
        turns = [{"state": "complete", "user": "first question", "assistant": "first answer"},
                 {"state": "running", "user": "follow-up"}]
        with mock.patch.object(runner, "command", return_value=[sys.executable, "-c", fixture]):
            self.assertEqual(runner.run({}, turns, threading.Event()), "The actual answer")

    def test_runner_stops_on_tool_request_or_timeout(self):
        runner = CodexRunner("/unused", Path(self.tmp.name) / "runtime", timeout=.3)
        turns = [{"state": "running", "user": "explain"}]
        tool = 'import json;print(json.dumps({"type":"item.started","item":{"type":"command_execution"}}),flush=True)'
        with mock.patch.object(runner, "command", return_value=[sys.executable, "-c", tool]):
            with self.assertRaises(ChatError) as error:
                runner.run({}, turns, threading.Event())
            self.assertEqual(error.exception.status, 502)
        with mock.patch.object(runner, "command", return_value=[sys.executable, "-c", "import time;time.sleep(10)"]):
            with self.assertRaises(ChatError) as error:
                runner.run({}, turns, threading.Event())
            self.assertEqual(error.exception.status, 504)

    def test_oversized_questions_and_arbitrary_request_fields_rejected(self):
        with self.manager.lock, self.assertRaises(ChatError):
            self.manager.open_chat(self.owner, {"event": {"kind": "episode", "id": "579"},
                "request_id": "a" * 32, "command": "unsafe"})
        chat = self.open()
        self.finish()
        with self.manager.lock, self.assertRaises(ChatError):
            self.manager.start_turn(self.manager.get(chat["id"], self.owner), "x" * 2001, "e" * 32)

    def test_five_minute_default_and_explicit_model_effort(self):
        runner = CodexRunner("/fixed/codex", Path(self.tmp.name) / "runtime")
        self.assertEqual(runner.timeout, 300)
        with mock.patch("pathlib.Path.read_text", return_value='model = "gpt-6-astra"\nmodel_reasoning_effort = "low"'):
            command = runner.command(Path(self.tmp.name))
            self.assertIn('model="gpt-6-astra"', command)
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertEqual(resolve_settings()["model"], "default")
            with self.assertRaises(ChatError):
                resolve_settings({"model": "arbitrary-command", "effort": "high"})
            with self.assertRaises(ChatError):
                resolve_settings({"model": "default", "effort": "invalid"})

    def test_runner_reuses_normal_codex_index_without_new_home_or_permissions(self):
        runner = CodexRunner("/fixed/codex", Path(self.tmp.name) / "runtime")
        with mock.patch.dict("os.environ", {"CODEX_HOME": "/existing/codex"}), mock.patch("pathlib.Path.read_text", return_value=""):
            command = runner.command(Path(self.tmp.name))
        self.assertIn('sqlite_home="/existing/codex"', command)
        self.assertNotIn(f'sqlite_home="{runner.runtime_dir}"', command)
        self.assertIn('history.persistence="none"', command)
        self.assertIn("--ephemeral", command)
        self.assertIn('permissions.warning-chat.filesystem={":minimal"="read", ":workspace_roots"="read"}', command)
        with mock.patch("pathlib.Path.read_text", return_value='sqlite_home = "/existing/custom-index"'):
            self.assertIn('sqlite_home="/existing/custom-index"', runner.command(Path(self.tmp.name)))
        with mock.patch("pathlib.Path.read_text", return_value='sqlite_home = "relative-index"'), self.assertRaises(ChatError):
            runner.command(Path(self.tmp.name))

    def test_timing_milestones_and_logs_do_not_leak_content(self):
        runner = CodexRunner("/unused", Path(self.tmp.name) / "runtime", timeout=3)
        fixture = '''import json,sys,time
json.load(sys.stdin)
print("401 unauthorized PRIVATE_CREDENTIAL_SENTINEL",file=sys.stderr,flush=True)
for event in [
 {"type":"thread.started","thread_id":"PRIVATE_THREAD_SENTINEL"},
 {"type":"turn.started"},
 {"type":"item.completed","item":{"type":"reasoning","text":"PRIVATE_REASONING_SENTINEL"}},
 {"type":"item.completed","item":{"type":"agent_message","text":"PRIVATE_ANSWER_SENTINEL"}},
 {"type":"turn.completed"}]:
 print(json.dumps(event),flush=True)
 time.sleep(.015)
'''
        diagnostics, updates, output = {}, [], io.StringIO()
        with mock.patch.object(runner, "command", return_value=[sys.executable, "-c", fixture]), mock.patch("sys.stderr", output):
            answer = runner.run({"secret": "PRIVATE_CONTEXT_SENTINEL"},
                [{"state": "running", "user": "PRIVATE_QUESTION_SENTINEL"}], threading.Event(), diagnostics, updates.append)
        self.assertEqual(answer, "PRIVATE_ANSWER_SENTINEL")
        self.assertEqual(diagnostics["outcome"], "complete")
        milestones = diagnostics["milestones_ms"]
        names = ["process_started_ms", "thread_started_ms", "turn_started_ms", "first_model_item_ms", "turn_completed_ms", "process_exit_ms"]
        self.assertEqual([milestones[n] for n in names], sorted(milestones[n] for n in names))
        self.assertGreater(diagnostics["stdin_bytes"], 0)
        self.assertGreater(diagnostics["stdout_bytes"], 0)
        self.assertIn("authentication", diagnostics["signals"])
        self.assertNotIn("PRIVATE_", json.dumps([diagnostics, updates]) + output.getvalue())
        self.assertEqual(diagnostics["exit_code"], 0)
        self.assertGreaterEqual(diagnostics["child_cpu_ms"], 0)
        self.assertGreater(len(updates), 3)
        self.assertTrue(all(json.loads(line)["event"] == "warning_codex_timing" for line in output.getvalue().splitlines()))

    def test_timeout_diagnostics_distinguish_startup_response_and_shutdown(self):
        cases = [([], "starting", "local startup"),
                 ([{"type": "thread.started"}, {"type": "turn.started"}], "waiting_for_model", "before a model response"),
                 ([{"type": "turn.completed"}], "finishing", "waiting for Codex to exit")]
        for events, phase, message in cases:
            with self.subTest(phase=phase):
                runner = CodexRunner("/unused", Path(self.tmp.name) / "runtime", timeout=.4)
                script = "import sys,json,time;json.load(sys.stdin);" + "".join(
                    f"print({json.dumps(event)!r},flush=True);" for event in events) + "sys.stdout.close();sys.stderr.close();time.sleep(5)"
                diagnostics = {}
                with mock.patch.object(runner, "command", return_value=[sys.executable, "-c", script]), mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(ChatError) as error:
                        runner.run({}, [{"state": "running", "user": "explain"}], threading.Event(), diagnostics)
                self.assertEqual(error.exception.status, 504)
                self.assertIn(message, str(error.exception))
                self.assertEqual(diagnostics["phase"], phase)
                self.assertEqual(diagnostics["outcome"], "timeout")
                self.assertTrue(diagnostics["terminated_by_advisor"])
                self.assertIn("process_exit_ms", diagnostics["milestones_ms"])

    def test_timing_persists_with_failed_turn_and_reopen(self):
        def failure(_context, _turns, _cancelled, diagnostics, progress):
            diagnostics.update({"phase": "starting", "outcome": "running", "milestones_ms": {"process_started_ms": 1}})
            progress(dict(diagnostics))
            diagnostics.update(outcome="timeout", elapsed_ms=300000)
            raise ChatError(504, "Timed out during startup")
        self.runner.run = failure
        chat = self.open()
        self.finish()
        reopened = self.manager.public(self.manager.get(chat["id"], self.owner))
        self.assertEqual(reopened["turns"][-1]["diagnostics"]["outcome"], "timeout")
        recovered = WarningChatManager(self.tmp.name, self.evidence, FakeRunner())
        self.assertEqual(recovered.get(chat["id"], self.owner)["turns"][-1]["diagnostics"]["elapsed_ms"], 300000)
        recovered.close()

    def test_timing_cgroup_deltas_and_heartbeat_are_bounded(self):
        diagnostics, updates = {}, []
        with mock.patch("projects.vehicle_data.warning_chat._cgroup_cpu", side_effect=[
                {"usage_usec": 1000, "throttled_usec": 2000, "nr_throttled": 3},
                {"usage_usec": 5000, "throttled_usec": 12000, "nr_throttled": 5}]), mock.patch("sys.stderr", io.StringIO()):
            timing = RunTiming(300, diagnostics, updates.append)
            timing.mark("thread_started_ms", "thread_ready")
            timing.mark("thread_started_ms", "thread_ready")
            for _ in range(100):
                timing.heartbeat()
            self.assertEqual(len(updates), 2)
            timing.finish("complete")
        self.assertEqual(diagnostics["cgroup_cpu_ms"], 4)
        self.assertEqual(diagnostics["cgroup_throttled_ms"], 10)
        self.assertEqual(diagnostics["cgroup_throttled_periods"], 2)

    def test_setup_proposal_requires_exact_explicit_approval_and_can_be_removed(self):
        from test_custom_warnings import RULE
        self.runner.run = lambda *_: json.dumps({"explanation": "Proposed only, in volts", "proposal": RULE})
        with self.manager.lock:
            opened = self.manager.open_chat(self.owner, {"event": {"kind": "setup", "id": "new-warning"}, "request_id": "c" * 32})
        self.finish()
        record = self.manager.get(opened["id"], self.owner)
        turn = record["turns"][-1]
        path = Path(self.tmp.name) / "early-warnings.json"
        self.assertFalse(path.exists())
        self.assertEqual(turn["proposal_unit"], "V")
        for payload in ({"confirmed": False, "proposal_id": turn["proposal_id"]},
                        {"confirmed": True, "proposal_id": "wrong"}):
            with self.assertRaises(ChatError):
                self.manager.apply_warning(record, payload)
        payload = {"confirmed": True, "proposal_id": turn["proposal_id"]}
        self.manager.apply_warning(record, payload)
        self.manager.apply_warning(record, payload)
        self.assertEqual(len(json.loads(path.read_text())["rules"]), 1)
        self.assertEqual(len(self.manager.warning_status(self.owner)["warnings"]), 1)
        self.manager.remove_saved_warning("not-owner", turn["proposal_id"], {"confirmed": True})
        self.assertEqual(len(json.loads(path.read_text())["rules"]), 1)
        self.manager.remove_saved_warning(self.owner, turn["proposal_id"], {"confirmed": True})
        self.assertEqual(json.loads(path.read_text())["rules"], [])
        self.assertFalse(turn["applied"])

    def test_invalid_setup_output_never_creates_a_rule(self):
        self.runner.run = lambda *_: '{"explanation":"done", "proposal":{"command":"unsafe"}}'
        with self.manager.lock:
            opened = self.manager.open_chat(self.owner, {"event": {"kind": "setup", "id": "new-warning"}, "request_id": "c" * 32})
        self.finish()
        record = self.manager.get(opened["id"], self.owner)
        self.assertEqual(record["turns"][-1]["state"], "error")
        self.assertFalse((Path(self.tmp.name) / "early-warnings.json").exists())


class WarningChatWebTests(unittest.TestCase):
    def setUp(self):
        self.web = TelemetryWebServer(("127.0.0.1", 0), socket_path="/unused-broker.sock",
            allow_acquisitions=False, stream_interval_seconds=.1, stream_max_seconds=.1,
            warning_chat_socket="/fixed-advisor.sock")
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.web.server_port}"

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        self.thread.join(timeout=2)

    def request(self, method="POST", path="/v1/assistant/chats", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=2)
        base = {"Origin": self.origin, "Content-Type": "application/json",
                "X-Van-Assistant-Key": "a" * 43, "X-Van-Chat-Client": "b" * 64}
        base.update(headers or {})
        connection.request(method, path, body=json.dumps(body or {}), headers=base)
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload

    def test_only_exact_same_origin_authenticated_proxy_routes(self):
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            client.return_value.request.return_value = (200, {"available": True})
            for headers, expected in [
                ({"Origin": "http://evil.example"}, 403),
                ({"Host": "evil.example", "Origin": "http://evil.example"}, 403),
                ({"Sec-Fetch-Site": "cross-site"}, 403),
                ({"X-Van-Assistant-Key": ""}, 401),
                ({"Content-Type": "text/plain"}, 415),
            ]:
                with self.subTest(headers=headers):
                    self.assertEqual(self.request(headers=headers)[0], expected)
            self.assertFalse(client.return_value.request.called)
            self.assertEqual(self.request(path="/v1/assistant/exec")[0], 404)
            self.assertEqual(self.request(body={"event": {"kind": "episode", "id": "579"}, "request_id": "c" * 32})[0], 200)
            client.assert_called_with("/fixed-advisor.sock", timeout=12)
            call = client.return_value.request.call_args
            self.assertEqual(call.args[:2], ("POST", "/v1/assistant/chats"))
            self.assertEqual(call.kwargs["headers"]["X-Van-Chat-Client"], "b" * 64)

    def test_disabled_worker_and_transport_failure_are_explicit(self):
        self.web.warning_chat_socket = None
        self.assertEqual(self.request()[0], 503)
        self.web.warning_chat_socket = "/fixed-advisor.sock"
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            client.return_value.request.side_effect = OSError("connection refused")
            status, response = self.request()
            self.assertEqual(status, 503)
            self.assertIn("not running", response["detail"])
