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
from projects.vehicle_data.assistant_pairing import OWNER_PATH, REDEEM_BODY_MAX, REDEEM_PATH
from projects.vehicle_data.web import TelemetryWebServer
from tests.assistant_lock_support import private_cli_lock


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
        self.manager = WarningChatManager(self.tmp.name, self.evidence, self.runner, cli_lock=private_cli_lock(self.tmp.name))
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
        # This guards against a hung worker. The worker may still have state to save.
        self.manager.worker.join(timeout=20)
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
        recovered = WarningChatManager(self.tmp.name, self.evidence, FakeRunner(), cli_lock=private_cli_lock(self.tmp.name))
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
                runner = CodexRunner("/unused", Path(self.tmp.name) / "runtime", timeout=2)
                script = "\n".join([
                    "import json, os, signal, sys",
                    "signal.alarm(30)",  # Bound the fixture's lifetime if the runner breaks.
                    "json.load(sys.stdin)",
                    *(f"print({json.dumps(event)!r}, flush=True)" for event in events),
                    "os.close(1)",
                    "os.close(2)",
                    "signal.pause()",
                ])
                diagnostics = {}
                cancelled = threading.Event()
                with (
                    mock.patch.object(runner, "command", return_value=[sys.executable, "-c", script]),
                    mock.patch("projects.vehicle_data.warning_chat.time") as clock,
                    mock.patch.object(cancelled, "wait") as wait_for_exit,
                    mock.patch("sys.stderr", io.StringIO()),
                ):
                    # Advance the deadline only after the runner has drained the event stream.
                    clock.monotonic.return_value = 0
                    wait_for_exit.side_effect = lambda _: clock.monotonic.configure_mock(return_value=runner.timeout)
                    with self.assertRaises(ChatError) as error:
                        runner.run({}, [{"state": "running", "user": "explain"}], cancelled, diagnostics)
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
        recovered = WarningChatManager(self.tmp.name, self.evidence, FakeRunner(), cli_lock=private_cli_lock(self.tmp.name))
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

    def test_setup_chat_reply_lists_the_catalog_the_model_received(self):
        from projects.vehicle_data.custom_warnings import catalog
        self.runner.run = lambda *_: json.dumps({"explanation": "What would you like to monitor?", "proposal": None})
        with self.manager.lock:
            opened = self.manager.open_chat(self.owner, {"event": {"kind": "setup", "id": "new-warning"}, "request_id": "d" * 32})
        self.finish()
        rows = opened["catalog"]
        self.assertEqual({row["metric"] for row in rows}, set(catalog()))
        self.assertEqual([row["metric"] for row in rows], sorted(row["metric"] for row in rows))
        self.assertTrue(all(set(row) == {"metric", "unit"} for row in rows))
        self.assertIn({"metric": "generator.field_duty", "unit": "%"}, rows)
        record = self.manager.get(opened["id"], self.owner)
        record["context"].pop("catalog")
        self.assertEqual(self.manager.public(record)["catalog"], [])
        record["event"] = {"kind": "episode", "id": "1"}
        self.assertNotIn("catalog", self.manager.public(record))

    def proposed(self, request_id="c" * 32):
        """A completed setup chat whose latest turn proposes tests.custom_rule_history.RULE."""
        from tests.custom_rule_history import RULE
        self.runner.run = lambda *_: json.dumps({"explanation": "Proposed only, in volts", "proposal": RULE})
        with self.manager.lock:
            opened = self.manager.open_chat(self.owner, {"event": {"kind": "setup", "id": "new-warning"}, "request_id": request_id})
        self.finish()
        record = self.manager.get(opened["id"], self.owner)
        return record, record["turns"][-1]

    @staticmethod
    def approval(turn, **changes):
        return {"confirmed": True, "proposal_id": turn["rule_proposal_id"], "semantics": "owner-threshold-v2", **changes}

    def test_setup_proposal_requires_exact_explicit_approval_and_can_be_removed(self):
        record, turn = self.proposed()
        path = Path(self.tmp.name) / "early-warnings.json"
        self.assertFalse(path.exists())
        self.assertEqual(turn["rule_proposal_unit"], "V")
        for payload in (self.approval(turn, confirmed=False), self.approval(turn, proposal_id="wrong"),
                        self.approval(turn, extra=True)):
            with self.assertRaises(ChatError):
                self.manager.apply_warning(record, payload)
        payload = self.approval(turn)
        self.manager.apply_warning(record, payload)
        self.manager.apply_warning(record, payload)
        self.assertEqual(len(json.loads(path.read_text())["rules"]), 1)
        self.assertEqual(len(self.manager.warning_status(self.owner)["warnings"]), 1)
        self.manager.remove_saved_warning("not-owner", turn["rule_proposal_id"], {"confirmed": True})
        self.assertEqual(len(json.loads(path.read_text())["rules"]), 1)
        self.manager.remove_saved_warning(self.owner, turn["rule_proposal_id"], {"confirmed": True})
        self.assertEqual(json.loads(path.read_text())["rules"], [])
        self.assertFalse(turn["applied"])

    def test_apply_over_the_file_size_cap_answers_409_not_unavailable(self):
        record, turn = self.proposed()
        with mock.patch("projects.vehicle_data.custom_warning_file.write",
                        side_effect=ValueError("The saved warnings file would be too large; remove a warning first")):
            with self.assertRaises(ChatError) as caught:
                self.manager.apply_warning(record, self.approval(turn))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("too large", str(caught.exception))
        self.assertFalse(turn.get("applied"))

    def test_apply_without_semantics_answers_reload_required(self):
        record, turn = self.proposed()
        for payload in ({"confirmed": True, "proposal_id": turn["rule_proposal_id"]},
                        self.approval(turn, semantics="owner-threshold-v1")):
            with self.subTest(payload=payload), self.assertRaises(ChatError) as caught:
                self.manager.apply_warning(record, payload)
            self.assertEqual((caught.exception.status, caught.exception.reason), (409, "dashboard_reload_required"))
            self.assertEqual(str(caught.exception), "The dashboard was updated; reload the page.")
        self.assertFalse((Path(self.tmp.name) / "early-warnings.json").exists())

    def test_proposal_made_before_the_update_is_refused(self):
        record, turn = self.proposed()
        old = {key: value for key, value in turn.items() if not key.startswith("rule_proposal") and key != "semantics"}
        old.update(proposal=turn["rule_proposal"], proposal_id=turn["rule_proposal_id"], proposal_unit="V")
        record["turns"][-1] = old
        with self.assertRaises(ChatError) as caught:
            self.manager.apply_warning(record, self.approval(turn))
        self.assertEqual((caught.exception.status, str(caught.exception)),
                         (409, "This suggestion was made before an update; ask again."))
        self.assertFalse((Path(self.tmp.name) / "early-warnings.json").exists())

    def test_chat_writes_keep_rejected_rows_other_keys_and_record_removed_ids(self):
        from tests.custom_rule_history import RULE
        path = Path(self.tmp.name) / "early-warnings.json"
        bad = {"id": "e" * 64, "rule": dict(RULE, metric="engine.oil_life_remaining"), "owner": "x"}
        path.write_text(json.dumps({"version": 1, "rules": [bad], "future": [1]}))
        record, turn = self.proposed()
        self.manager.apply_warning(record, self.approval(turn))
        saved = json.loads(path.read_text())
        self.assertEqual((saved["future"], len(saved["rules"])), ([1], 2))
        self.assertIn(bad, saved["rules"])
        self.manager.apply_warning(record, self.approval(turn), remove=True)
        saved = json.loads(path.read_text())
        self.assertEqual((saved["rules"], saved["removed"]), ([bad], [turn["rule_proposal_id"]]))

    def test_chat_install_of_a_removed_id_is_refused(self):
        record, turn = self.proposed()
        self.manager.apply_warning(record, self.approval(turn))
        self.manager.remove_saved_warning(self.owner, turn["rule_proposal_id"], {"confirmed": True})
        with self.assertRaises(ChatError) as caught:
            self.manager.apply_warning(record, self.approval(turn))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("since been removed", str(caught.exception))

    def test_v2_proposal_turn_carries_no_key_the_pre_v2_advisor_installs(self):
        from tests.custom_rule_history import RULE
        record, turn = self.proposed()
        self.assertEqual((turn["rule_proposal"], turn["semantics"]), (RULE, "owner-threshold-v2"))
        self.assertTrue(turn["rule_proposal_id"])
        saved = json.loads((Path(self.tmp.name) / f"chat-{record['id']}.json").read_text())["turns"][-1]
        for key in ("proposal", "proposal_id", "proposal_unit"):
            self.assertNotIn(key, saved)
        # A follow-up replays the v2 proposal to the model under its reply key.
        self.runner.run = lambda context, turns, *_: json.dumps({"explanation": json.dumps(turns[0].get("rule_proposal")),
                                                                 "proposal": None})
        with self.manager.lock:
            self.manager.start_turn(record, "Make it 11.5 V", "f" * 32)
        self.finish()
        self.assertEqual(json.loads(record["turns"][-1]["assistant"]), RULE)

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

    def test_redeem_relay_forwards_no_assistant_headers(self):
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            client.return_value.request.return_value = (403, {"available": False, "reason": "pin_rejected"})
            self.assertEqual(self.request(path=REDEEM_PATH, body={"pin": "123456"})[0], 403)
            call = client.return_value.request.call_args
            self.assertEqual(call.args, ("POST", REDEEM_PATH, {"pin": "123456"}))
            self.assertEqual(call.kwargs["headers"], {})
            # No key at all is relayed too: the advisor decides.
            self.assertEqual(self.request(path=REDEEM_PATH, body={"pin": "123456"},
                                          headers={"X-Van-Assistant-Key": "", "X-Van-Chat-Client": ""})[0], 403)

    def test_redeem_keeps_the_origin_json_and_size_gates(self):
        oversized = {"pin": "1" * (REDEEM_BODY_MAX - len('{"pin": ""}') + 1)}
        self.assertEqual(len(json.dumps(oversized)), REDEEM_BODY_MAX + 1)
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            for headers, body, expected in [
                ({"Origin": "http://evil.example"}, None, 403),
                ({"Host": "evil.example", "Origin": "http://evil.example"}, None, 403),
                ({"Sec-Fetch-Site": "cross-site"}, None, 403),
                ({"Origin": ""}, None, 403),
                ({"Content-Type": "text/plain"}, None, 415),
                ({}, oversized, 400),
                ({}, ["123456"], 400),
            ]:
                with self.subTest(headers=headers, body=body):
                    self.assertEqual(self.request(path=REDEEM_PATH, headers=headers, body=body or {"pin": "123456"})[0], expected)
            self.assertFalse(client.called)

    def test_redeem_path_is_exact(self):
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            for method, path in [("GET", REDEEM_PATH), ("POST", REDEEM_PATH + "/x"), ("POST", REDEEM_PATH + "x"),
                                 ("POST", REDEEM_PATH + "/")]:
                with self.subTest(method=method, path=path):
                    status = self.request(method=method, path=path, body={"pin": "123456"},
                                          headers={"X-Van-Assistant-Key": ""})[0]
                    self.assertEqual(status, 404)
            self.assertFalse(client.called)

    def test_every_other_assistant_route_still_401_without_key(self):
        from tests.test_assistant_pairing_relay import AUTHENTICATED_ROUTES
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            for method, path, body in AUTHENTICATED_ROUTES:
                if path.startswith(REDEEM_PATH):
                    continue  # not relayed at all (test_redeem_path_is_exact)
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method=method, path=path, body=body,
                                                  headers={"X-Van-Assistant-Key": ""})[0], 401)
            self.assertFalse(client.called)

    def test_relay_logs_no_pin_or_code(self):
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client, \
                mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()) as err:
            client.return_value.request.return_value = (200, {"available": True, "access_code": "q" * 43})
            self.assertEqual(self.request(path=REDEEM_PATH, body={"pin": "482913"}), (200, {"available": True, "access_code": "q" * 43}))
            client.return_value.request.return_value = (200, {"available": True, "pin": "715204", "pin_id": "0" * 16})
            self.assertEqual(self.request(path=OWNER_PATH, body={"action": "create"})[0], 200)
        text = out.getvalue() + err.getvalue()
        self.assertIn(REDEEM_PATH, text)  # the one request line per request
        for secret in ("482913", "715204", "q" * 43, "a" * 43):
            self.assertNotIn(secret, text)

    def test_disabled_worker_and_transport_failure_are_explicit(self):
        self.web.warning_chat_socket = None
        self.assertEqual(self.request()[0], 503)
        self.web.warning_chat_socket = "/fixed-advisor.sock"
        with mock.patch("projects.vehicle_data.web.TelemetryClient") as client:
            client.return_value.request.side_effect = OSError("connection refused")
            status, response = self.request()
            self.assertEqual(status, 503)
            self.assertIn("not running", response["detail"])
