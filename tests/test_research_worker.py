"""The research worker with a scripted fake Claude CLI: admission, turns, policy, memory, sessions."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid

from projects.vehicle_data import cli_process
from projects.vehicle_data.dtc_research_worker import dispatch
from projects.vehicle_data.model_cli_lock import ACQUIRED, CliLock
from projects.vehicle_data.research_attempts import OUTCOMES_KEPT, Attempt, Refused
from projects.vehicle_data.research_memory import Thresholds, Watchdog
from projects.vehicle_data.research_runner import TurnRequest
from tests.research_support import GIB, WorkerRig, proc_entry


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.rig = WorkerRig()
        self.worker = self.rig.worker

    def tearDown(self):
        self.rig.close()

    def run_turn(self, **kwargs):
        body = self.rig.turn_body(**kwargs)
        attempt = self.worker.admit_turn(body)
        return body, self.rig.wait(attempt.attempt_id)

    def test_report_turn_completes_with_only_web_tools_and_a_private_environment(self):
        os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:1"
        os.environ["OPENAI_API_KEY"] = "leak"
        try:
            body, state = self.run_turn()
        finally:
            del os.environ["ANTHROPIC_BASE_URL"], os.environ["OPENAI_API_KEY"]
        outcome = state["outcome"]
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(outcome["mode"], "initial")
        self.assertTrue(outcome["session_present"])
        self.assertEqual((outcome["diagnostics"]["searches"], outcome["diagnostics"]["pages"]), (1, 1))
        call = self.rig.calls()[-1]
        argv, env = call["argv"], call["env"]
        self.assertEqual(argv[argv.index("--tools") + 1], "WebSearch,WebFetch")
        for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("sk-test-token", " ".join(argv))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-test-token")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertTrue(call["cwd"].endswith(f"/runs/{body['run_id']}/work"))
        self.assertIsNone(self.worker.table.current)  # slot released after reaping

    def test_resume_is_verified_and_a_non_growing_session_is_not_called_resumed(self):
        body, state = self.run_turn()
        session = state["outcome"]["session_id"]
        _, resumed = self.run_turn(run_id=body["run_id"], kind="resume", session_id=session, turn_kind="follow_up")
        self.assertEqual(resumed["outcome"]["mode"], "resumed")
        self.rig.scenario({"mode": "no_growth"})
        _, stale = self.run_turn(run_id=body["run_id"], kind="resume", session_id=session, turn_kind="follow_up")
        self.assertEqual(stale["outcome"]["reason"], "resume_unverified")
        self.assertFalse(stale["outcome"]["session_present"])  # dropped: the next follow-up replays

    def test_resume_with_another_session_id_is_killed_and_resume_of_a_missing_session_is_refused(self):
        body, state = self.run_turn()
        self.rig.scenario({"init_session": "someone-else"})
        _, other = self.run_turn(run_id=body["run_id"], kind="resume", session_id=state["outcome"]["session_id"],
                                 turn_kind="follow_up")
        self.assertEqual(other["outcome"]["reason"], "resume_unverified")
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.rig.turn_body(run_id=body["run_id"], kind="resume", session_id="gone"))
        self.assertEqual(refused.exception.reason, "session_missing")

    def test_policy_violations_and_protocol_errors_fail_closed(self):
        for scenario, reason in (({"tools": ["WebSearch", "WebFetch", "Bash"]}, "policy_violation"),
                                 ({"mode": "forbidden_tool"}, "policy_violation"),
                                 ({"mode": "non_json"}, "protocol_error"),
                                 ({"mode": "huge_line"}, "protocol_error"),
                                 ({"mode": "budget"}, "budget_exceeded"),
                                 ({"mode": "exit_error"}, "provider_error")):
            with self.subTest(scenario=scenario):
                self.rig.scenario(scenario)
                _, state = self.run_turn()
                self.assertEqual(state["outcome"]["status"], "failed")
                self.assertEqual(state["outcome"]["reason"], reason)

    def test_cancel_reaps_the_whole_process_group_including_a_grandchild(self):
        self.rig.scenario({"mode": "grandchild"})
        attempt = self.worker.admit_turn(self.rig.turn_body())
        pid_file = self.rig.state / "grandchild.pid"
        deadline = time.monotonic() + 15
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        grandchild = int(pid_file.read_text())
        self.worker.table.cancel(attempt.attempt_id)
        state = self.rig.wait(attempt.attempt_id)
        self.assertEqual(state["outcome"]["status"], "cancelled")
        time.sleep(.2)
        self.assertFalse(Path(f"/proc/{grandchild}").exists() and "Z" not in
                         Path(f"/proc/{grandchild}/stat").read_text().split()[2])

    @unittest.skipUnless(Path("/proc/self/status").exists(), "the memory watchdog reads /proc, which this host lacks")
    def test_memory_watchdog_kills_at_once_without_grace(self):
        self.rig.policy["memory"]["measured_values"]["tree_cap_mib"] = 40
        self.rig.scenario({"mode": "grow_ignore_term", "grow_bytes": 96 * 1024 * 1024})
        started = time.monotonic()
        _, state = self.run_turn()
        self.assertEqual(state["outcome"]["reason"], "memory_exhausted")
        self.assertLess(time.monotonic() - started, 15)  # a SIGTERM-ignoring child was not given 3 s of grace

    def test_watchdog_stops_a_tree_over_its_cap_or_a_pi_at_its_critical_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, meminfo = Path(tmp) / "proc", Path(tmp) / "meminfo"
            proc_entry(proc, 7, children=[8], rss_kib=30 * 1024)
            proc_entry(proc, 8, rss_kib=20 * 1024, hwm_kib=25 * 1024)
            meminfo.write_text("MemAvailable: 2097152 kB\n")
            watchdog = Watchdog(Thresholds(64, 40, 16), pid=7, meminfo=meminfo, proc_root=proc)
            self.assertEqual(watchdog.check(), "the research process grew past its memory cap")  # 50 MiB > 40
            self.assertEqual(watchdog.figures()["peak_tree_rss_mib"], 50.0)
            watchdog = Watchdog(Thresholds(64, 400, 16), pid=7, meminfo=meminfo, proc_root=proc)
            self.assertIsNone(watchdog.check())
            meminfo.write_text("MemAvailable: 8192 kB\n")
            self.assertIn("critical", watchdog.check())

    def test_admission_refusals(self):
        self.rig.set_available(32 * 1024 * 1024)
        with self.assertRaises(Refused) as low:
            self.worker.admit_turn(self.rig.turn_body())
        self.assertEqual(low.exception.reason, "low_memory")
        self.rig.set_available(4 * GIB)
        stale = self.rig.turn_body()
        stale["expected_instance_id"] = uuid.uuid4().hex
        with self.assertRaises(Refused) as instance:
            self.worker.admit_turn(stale)
        self.assertEqual(instance.exception.reason, "stale_instance")
        other = CliLock(self.rig.lock_path)
        self.assertEqual(other.try_acquire(), ACQUIRED)  # a warning chat holds the shared lock
        try:
            with self.assertRaises(Refused) as chat:
                self.worker.admit_turn(self.rig.turn_body())
            self.assertEqual(chat.exception.reason, "warning_chat")
        finally:
            other.release()
        self.rig.lock_path.unlink()
        with self.assertRaises(Refused) as missing:
            self.worker.admit_turn(self.rig.turn_body())
        self.assertEqual(missing.exception.reason, "provider_unavailable")

    def test_not_activated_refuses_turns_until_measured_for_this_copy(self):
        self.rig.policy["memory"]["cli_digest"] = "0" * 64
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.rig.turn_body())
        self.assertEqual(refused.exception.reason, "not_activated")
        self.assertFalse(self.worker.status()["memory_policy"]["activated"])

    def test_changed_copy_makes_claude_unavailable(self):
        (self.rig.release / "extra").write_text("x")
        self.assertEqual(self.worker.status()["providers"]["claude"]["identity"], "changed")
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.rig.turn_body())
        self.assertEqual(refused.exception.reason, "provider_unavailable")
        self.assertEqual(self.worker.status()["providers"]["codex"]["identity"], "disabled")

    def test_attempt_ids_are_never_launched_twice_and_dropped_outcomes_say_so(self):
        body, _ = self.run_turn()
        with self.assertRaises(Refused) as duplicate:
            self.worker.admit_turn(body)
        self.assertEqual(duplicate.exception.reason, "duplicate_attempt")
        ids = [self.run_turn()[0]["attempt_id"] for _ in range(OUTCOMES_KEPT)]
        first = self.worker.table.get(body["attempt_id"])
        self.assertEqual((first["known"], first["outcome_state"]), (True, "dropped"))
        self.assertEqual(self.worker.table.get(ids[-1])["outcome_state"], "held")
        self.assertEqual(len(self.rig.calls()), OUTCOMES_KEPT + 1)
        unknown = self.worker.table.get(uuid.uuid4().hex)
        self.assertEqual(unknown, {"instance_id": self.worker.table.instance_id, "known": False})

    def test_simultaneous_research_start_and_maintenance_admit_exactly_one(self):
        self.rig.scenario({"mode": "ok", "delay": 1})
        barrier, results = threading.Barrier(2), []

        def start():
            barrier.wait()
            try:
                results.append(("turn", self.worker.admit_turn(self.rig.turn_body()).attempt_id))
            except Refused as exc:
                results.append(("turn", exc.reason))

        def maintain():
            barrier.wait()
            body = {"attempt_id": uuid.uuid4().hex, "expected_instance_id": self.worker.table.instance_id,
                    "purpose": "install"}
            try:
                results.append(("maintenance", self.worker.admit_maintenance(body).attempt_id))
            except Refused as exc:
                results.append(("maintenance", exc.reason))

        threads = [threading.Thread(target=start), threading.Thread(target=maintain)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        refusals = [value for _, value in results if value == "busy"]
        self.assertEqual(len(refusals), 1, results)
        self.assertLessEqual(len(self.rig.calls()), 1)

    def test_maintenance_holds_the_slot_until_it_ends_or_expires(self):
        body = {"attempt_id": uuid.uuid4().hex, "expected_instance_id": self.worker.table.instance_id,
                "purpose": "install"}
        self.worker.admit_maintenance(body)
        with self.assertRaises(Refused) as busy:
            self.worker.admit_turn(self.rig.turn_body())
        self.assertEqual(busy.exception.reason, "busy")
        self.assertTrue(self.worker.maintenance_view(body["attempt_id"])["open"])
        self.worker.table.current.detail["deadline"] = 0
        self.worker.expire_maintenance()
        self.assertFalse(self.worker.maintenance_view(body["attempt_id"])["open"])
        self.run_turn()

    def test_an_accepted_attempt_cancelled_before_its_spawn_never_spawns(self):
        spawned, cancelled = [], threading.Event()
        cancelled.set()
        result = cli_process.run([sys.executable, "-c", "pass"], cwd=self.rig.tmp.name, env={}, stdin_bytes=b"",
                                 deadline=time.monotonic() + 30, cancelled=cancelled, on_line=lambda event: None,
                                 on_spawn=spawned.append)
        self.assertEqual((result.stop_reason, result.returncode, spawned), ("cancelled", None, []))
        attempt = Attempt(uuid.uuid4().hex, "turn", uuid.uuid4().hex)  # the same through a turn
        attempt.cancelled.set()
        outcome = self.worker.runner.run_turn(attempt, TurnRequest(
            attempt.run_id, "claude", "sonnet", "medium", "initial", "report", "CONTEXT {}", 30, budget_usd=1))
        self.assertEqual((outcome["status"], outcome["reason"], attempt.state), ("cancelled", "cancelled", "accepted"))

    def test_routes_and_sweep(self):
        status, payload = dispatch(self.worker, "GET", "/status", None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["providers"]["claude"]["identity"], "ok")
        body, _ = self.run_turn()
        status, payload = dispatch(self.worker, "POST", "/sweep", lambda: {"keep": []})
        self.assertEqual(payload["removed"], 1)
        self.assertFalse(self.rig.volume.run_dir(body["run_id"]).exists())
        with self.assertRaises(Refused):  # the handler answers 409 with the reason
            dispatch(self.worker, "POST", "/turns", lambda: {"attempt_id": "bad"})
        self.assertEqual(dispatch(self.worker, "GET", "/nope", None)[0], 404)


if __name__ == "__main__":
    unittest.main()
