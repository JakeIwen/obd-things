"""Credit-billed research turns on the worker, driven by the scripted fake Claude CLI."""

import contextlib
import copy
import io
import json
import os
import time
import unittest
from unittest import mock

from projects.vehicle_data import research_maintenance
from projects.vehicle_data.api_credits_contract import UNTIL
from projects.vehicle_data.cli_process import RunResult
from projects.vehicle_data.dtc_research_worker import Worker, dispatch
from projects.vehicle_data.model_cli_lock import CliLock
from projects.vehicle_data.research_attempts import Attempt, Refused
from projects.vehicle_data.research_cli_policy import IdentityChecker
from tests.research_support import FAKE_KEY, WorkerRig

OAUTH = "sk-test-token"


def delta(cost=0.25, model="sonnet"):
    return {model: {"costUSD": cost}}


def result(index=0, *, error=None, text="SUMMARY: Fine.\n## What it means\nok", cost=0.25, subtype=None, **extra):
    event = {"type": "result", "subtype": subtype or ("success" if error is None else "error_during_execution"),
             "is_error": error is not None, "result": error if error is not None else text,
             "total_cost_usd": 99.0, "num_turns": 2, "result_index": index,
             "model_usage_since_last_result": delta(cost)}
    event.update(extra)
    return event


def assistant(text="...", error=None):
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if error:
        event["error"] = error
    return event


class CreditTestCase(unittest.TestCase):
    rig_kwargs = {}

    def setUp(self):
        self.rig = WorkerRig(with_key=True, **self.rig_kwargs)
        self.worker = self.rig.worker

    def tearDown(self):
        self.rig.close()

    def go(self, billing_capable=True, **kwargs):
        body = self.rig.turn_body(billing_capable=billing_capable, **kwargs)
        attempt = self.worker.admit_turn(body)
        return body, self.rig.wait(attempt.attempt_id)["outcome"]

    def entries(self):
        return self.worker.credits.view()["entries"]

    def latches(self):
        return self.worker.credits.view()["latches"]


class CreditEnvironmentTests(CreditTestCase):
    def test_a_credit_turn_bills_the_key_with_no_oauth_token_and_settles_its_delta(self):
        body, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"], outcome["credit_code"]), ("complete", "api_credits", None))
        self.assertEqual(outcome["diagnostics"]["billing"], "api_credits")
        call = self.rig.calls()[-1]
        self.assertEqual(call["env"]["ANTHROPIC_API_KEY"], FAKE_KEY)
        self.assertEqual(call["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", call["env"])
        self.assertNotIn(FAKE_KEY, " ".join(call["argv"]))
        self.assertNotIn(FAKE_KEY, self.rig.prompts()[-1])
        (entry,) = self.entries()
        self.assertEqual((entry["state"], entry["known_usd"], entry["unknown"], entry["model"]),
                         ("settled", 0.25, False, "sonnet"))
        self.assertEqual(self.rig.ledger()["entries"][0]["attempt_id"], entry["attempt_id"])
        record = self.rig.volume.session_record(body["run_id"])
        self.assertEqual(record["billing"], "api_credits")

    def test_a_locally_set_base_url_never_reaches_a_credit_child(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_API_KEY": "other"}):
            self.go()
        env = self.rig.calls()[-1]["env"]
        self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_API_KEY"]),
                         ("https://api.anthropic.com", FAKE_KEY))

    def test_subscription_turns_never_carry_the_key_whatever_the_reason(self):
        for name, kwargs in (("legacy", dict(billing_capable=None)), ("forced", dict(billing="subscription"))):
            with self.subTest(name):
                _, outcome = self.go(**kwargs)
                self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))
                env = self.rig.calls()[-1]["env"]
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("ANTHROPIC_BASE_URL", env)
                self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "worker-env-leak"}):
            self.go(billing_capable=None)
        self.assertNotIn("ANTHROPIC_API_KEY", self.rig.calls()[-1]["env"])
        self.assertEqual(self.entries(), [])  # no subscription launch opens a ledger entry

    def test_discovery_help_and_maintenance_children_never_get_the_key(self):
        attempt = Attempt("c" * 32, "discovery")
        self.worker.runner.discover_claude(attempt)
        self.worker.runner.help_text(Attempt("d" * 32, "maintenance"), self.rig.release / "claude", 20)
        research_maintenance.budget_check(self.worker.runner, Attempt("e" * 32, "maintenance"))
        calls = self.rig.calls()
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
            self.assertNotIn(FAKE_KEY, json.dumps(call))
        self.assertEqual(self.entries(), [])

    def test_a_subscription_launch_that_reports_an_api_key_source_is_a_policy_violation(self):
        self.rig.scenario({"api_key_source": "ANTHROPIC_API_KEY"})
        _, outcome = self.go(billing_capable=None)
        self.assertEqual((outcome["status"], outcome["reason"]), ("failed", "policy_violation"))


class CreditFailureTests(CreditTestCase):
    def failing(self, text, error=None, extra_events=(), **result_kwargs):
        events = [assistant("sorry", error=error), *extra_events, result(0, error=text, cost=0, **result_kwargs)]
        self.rig.scenario({"events": events, "exit_code": 1})

    def test_exhausted_credits_return_credit_fallback_and_latch_the_account(self):
        self.failing("Credit balance is too low", error="billing_error")
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["reason"], outcome["credit_code"], outcome["billing"]),
                         ("failed", "credit_fallback", "exhausted", "api_credits"))
        self.assertEqual(len(self.rig.calls()), 1)  # the worker never runs a second process
        self.assertEqual(self.latches(), {"account": "exhausted", "models": {}})
        self.assertEqual(self.worker.credits.view()["last_fallback"]["code"], "exhausted")
        self.rig.scenario({"mode": "ok"})
        _, later = self.go()  # the latch holds until a restart
        self.assertEqual((later["billing"], later["status"]), ("subscription", "complete"))
        self.assertNotIn("ANTHROPIC_API_KEY", self.rig.calls()[-1]["env"])
        self.assertEqual(len(self.entries()), 1)

    def test_a_failed_credit_turn_leaves_no_session_to_resume(self):
        self.failing("Credit balance is too low", error="billing_error")
        body, outcome = self.go()
        self.assertFalse(outcome["session_present"])
        self.assertIsNone(self.rig.volume.session_record(body["run_id"]))

    def test_a_revoked_key_latches_account_wide(self):
        self.failing("Invalid API key", error="authentication_failed")
        _, outcome = self.go()
        self.assertEqual(outcome["credit_code"], "invalid_key")
        self.assertEqual(self.latches()["account"], "invalid_key")
        self.rig.scenario({"mode": "ok"})
        for model in ("sonnet", "opus"):
            body = self.rig.turn_body(billing_capable=True)
            body["model"] = model
            attempt = self.worker.admit_turn(body)
            self.assertEqual(self.rig.wait(attempt.attempt_id)["outcome"]["billing"], "subscription")

    def test_a_model_latch_is_per_model(self):
        self.failing("model not found", error="model_not_found")
        _, outcome = self.go()
        self.assertEqual(outcome["credit_code"], "model_unavailable")
        self.assertEqual(self.latches(), {"account": None, "models": {"sonnet": "model_unavailable"}})
        self.rig.scenario({"mode": "ok"})
        _, same = self.go()
        self.assertEqual(same["billing"], "subscription")
        body = self.rig.turn_body(billing_capable=True)
        body["model"] = "opus"
        attempt = self.worker.admit_turn(body)
        self.assertEqual(self.rig.wait(attempt.attempt_id)["outcome"]["billing"], "api_credits")

    def test_overload_is_not_a_credit_failure_and_leaves_no_latch_or_fallback(self):
        self.failing("API Error: overloaded_error", error="billing_error")  # terminal overload outranks the earlier error
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["reason"], outcome["credit_code"]),
                         ("failed", "provider_error", None))
        self.assertEqual(self.latches(), {"account": None, "models": {}})
        self.assertIsNone(self.worker.credits.view()["last_fallback"])

    def test_the_pinned_clis_key_refusal_codes_fall_back_and_latch_invalid_key(self):
        for code in ("api_key_auth_disabled", "org_disabled_credential"):
            with self.subTest(code):
                self.rig.worker.credits.ledger.start()  # latches live until a restart
                self.failing("Your organization has disabled API key authentication", error="invalid_request",
                             api_error_code=code, api_error_status=403)
                _, outcome = self.go()
                self.assertEqual((outcome["reason"], outcome["credit_code"]), ("credit_fallback", "invalid_key"))
                self.assertEqual(self.latches()["account"], "invalid_key")

    def test_an_unrelated_403_is_not_a_credit_failure(self):
        self.failing("forbidden by a policy", error="invalid_request", api_error_status=403)
        _, outcome = self.go()
        self.assertEqual((outcome["reason"], outcome["credit_code"]), ("provider_error", None))

    def test_a_structural_rate_limit_outranks_a_remembered_billing_error(self):
        self.failing("an opaque message", error="billing_error", api_error_type="rate_limit_error",
                     api_error_status=429)
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["credit_code"]), ("failed", None))
        self.assertEqual(self.latches(), {"account": None, "models": {}})

    def test_a_malformed_or_silent_pre_init_end_never_latches_a_healthy_key(self):
        for name, scenario in (("bare result", {"pre_init": [{"type": "result"}], "exit_code": 1}),
                               ("killed with nothing", {"pre_init": [], "kill_self": True}),
                               ("exit 1 with nothing", {"pre_init": [], "exit_code": 1})):
            with self.subTest(name):
                self.rig.scenario(scenario)
                _, outcome = self.go()
                self.assertEqual((outcome["status"], outcome["reason"], outcome["credit_code"]),
                                 ("failed", "protocol_error", None))
                self.assertEqual(self.latches(), {"account": None, "models": {}})
                self.assertIsNone(self.worker.credits.view()["last_fallback"])

    def test_the_errors_array_is_classified(self):
        self.rig.scenario({"events": [dict(result(0, error="", cost=0), errors=["Your credit balance is too low"])],
                           "exit_code": 1})
        _, outcome = self.go()
        self.assertEqual(outcome["credit_code"], "exhausted")

    def test_an_assistant_error_that_recovered_is_not_a_failure(self):
        self.rig.scenario({"events": [assistant("retrying", error="billing_error"), assistant("fine"), result(0)]})
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "api_credits"))
        self.assertEqual(self.latches(), {"account": None, "models": {}})

    def test_tool_output_mentioning_403_or_billing_is_never_read(self):
        tool = {"type": "user", "message": {"content": [{"type": "tool_result",
                                                         "content": "403 forbidden: billing page, credit balance is too low"}]}}
        self.failing("the tool crashed", extra_events=[tool])
        _, outcome = self.go()
        self.assertEqual((outcome["reason"], outcome["credit_code"]), ("provider_error", None))

    def test_budget_exceeded_is_not_a_credit_failure(self):
        self.failing("stopped", error="billing_error", subtype="error_max_budget_usd")
        _, outcome = self.go()
        self.assertEqual((outcome["reason"], outcome["credit_code"]), ("budget_exceeded", None))

    def test_a_pre_init_failure_with_empty_stderr_falls_back(self):
        self.rig.scenario({"pre_init": [result(0, error="", cost=0)], "exit_code": 1})
        _, outcome = self.go()
        self.assertEqual((outcome["reason"], outcome["credit_code"]), ("credit_fallback", "invalid_key"))
        self.assertEqual(self.latches()["account"], "invalid_key")
        (entry,) = self.entries()
        self.assertEqual(entry["state"], "settled")

    def test_a_pre_init_failure_that_says_something_unrelated_is_a_plain_failure(self):
        self.rig.scenario({"pre_init": [], "stderr": "getaddrinfo ENOTFOUND api.anthropic.com", "exit_code": 1})
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["credit_code"]), ("failed", None))
        self.assertNotEqual(outcome["reason"], "credit_fallback")

    def test_a_pre_init_success_result_is_rejected_not_accepted(self):
        self.rig.scenario({"pre_init": [result(0)], "exit_code": 0})
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["reason"], outcome["credit_code"]), ("failed", "protocol_error", None))

    def test_a_pre_init_error_result_is_not_allowed_on_a_subscription_launch(self):
        self.rig.scenario({"pre_init": [result(0, error="", cost=0)], "exit_code": 1})
        _, outcome = self.go(billing_capable=None)
        self.assertEqual(outcome["reason"], "protocol_error")

    def test_a_credit_launch_that_ignores_the_key_is_stopped_and_latches_the_account(self):
        self.rig.scenario({"api_key_source": "none"})
        _, outcome = self.go()
        self.assertEqual((outcome["reason"], outcome["credit_code"]), ("credit_fallback", "key_ignored"))
        self.assertEqual(self.latches()["account"], "key_ignored")
        (entry,) = self.entries()
        self.assertEqual((entry["state"], entry["unknown"]), ("settled", True))

    def test_cancelled_and_timed_out_credit_turns_are_never_fallbacks(self):
        self.rig.scenario({"events": [assistant("..")], "mode": "hang_after_result"})
        attempt = self.worker.admit_turn(self.rig.turn_body(billing_capable=True))
        deadline = time.monotonic() + 15
        while self.worker.table.get(attempt.attempt_id)["state"] != "running" and time.monotonic() < deadline:
            time.sleep(.05)
        self.worker.table.cancel(attempt.attempt_id)
        outcome = self.rig.wait(attempt.attempt_id)["outcome"]
        self.assertEqual((outcome["status"], outcome["credit_code"]), ("cancelled", None))
        self.assertEqual(self.latches(), {"account": None, "models": {}})


class CreditSettlementTests(CreditTestCase):
    def test_resumed_cumulative_cost_is_never_charged(self):
        body, first = self.go()
        self.rig.scenario({"events": [assistant("more"), result(0, cost=0.1, total_cost_usd=7.77)]})
        _, second = self.go(run_id=body["run_id"], kind="resume", session_id=first["session_id"], turn_kind="follow_up")
        self.assertEqual((second["status"], second["billing"]), ("complete", "api_credits"))
        self.assertEqual([e["known_usd"] for e in self.entries()], [0.25, 0.1])

    def test_a_killed_credit_process_settles_unknown_keeping_earlier_known_spend(self):
        self.rig.scenario({"events": [assistant("a"), result(0, cost=0.4)], "mode": "hang_after_result"})
        attempt = self.worker.admit_turn(self.rig.turn_body(billing_capable=True))
        deadline = time.monotonic() + 15
        while self.worker.table.get(attempt.attempt_id)["phase"] != "finishing" and time.monotonic() < deadline:
            time.sleep(.05)
        self.worker.table.cancel(attempt.attempt_id)
        self.rig.wait(attempt.attempt_id)
        (entry,) = self.entries()
        self.assertEqual((entry["state"], entry["known_usd"], entry["unknown"]), ("settled", 0.4, True))

    def test_a_result_without_a_valid_delta_settles_unknown(self):
        self.rig.scenario({"delta": None})
        _, outcome = self.go()
        self.assertEqual(outcome["status"], "complete")
        (entry,) = self.entries()
        self.assertEqual((entry["known_usd"], entry["unknown"]), (0.0, True))

    def test_a_result_number_gap_settles_unknown_and_a_duplicate_adds_nothing(self):
        self.rig.scenario({"events": [assistant("a"), result(1, cost=0.2)]})
        self.go()
        self.rig.scenario({"events": [assistant("a"), result(0, cost=0.3), result(0, cost=0.3)]})
        self.go()
        first, second = self.entries()
        self.assertEqual((first["known_usd"], first["unknown"]), (0.2, True))
        self.assertEqual((second["known_usd"], second["unknown"]), (0.3, False))

    def test_a_pre_spawn_ledger_write_failure_runs_the_turn_on_the_subscription(self):
        def refuse(*_):
            raise OSError("disk full")
        self.worker.credits.ledger._write = refuse
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))
        self.assertNotIn("ANTHROPIC_API_KEY", self.rig.calls()[-1]["env"])

    def test_a_settlement_write_failure_keeps_the_answer_and_the_entry_settles_unknown_at_next_start(self):
        ledger = self.worker.credits.ledger
        real, writes = ledger._write, []

        def fail_after_the_reservation(path, record):
            writes.append(1)
            if len(writes) > 1:
                raise OSError("disk full")
            real(path, record)

        ledger._write = fail_after_the_reservation
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "api_credits"))
        self.assertIn("dtc_research_credit_settlement_failed", stderr.getvalue())
        self.assertEqual(self.rig.ledger()["entries"][0]["state"], "open")
        ledger._write = real
        ledger.start()
        (entry,) = ledger.snapshot()["entries"]
        self.assertEqual((entry["state"], entry["unknown"]), ("settled", True))

    def test_a_launch_that_never_spawns_withdraws_its_entry(self):
        with mock.patch("projects.vehicle_data.research_runner.cli_process.run",
                        return_value=RunResult(returncode=None, stop_reason="cancelled")):
            _, outcome = self.go()
        self.assertEqual(outcome["status"], "cancelled")
        self.assertEqual(self.entries(), [])

    def test_an_admission_refusal_after_the_reservation_withdraws_it(self):
        other = CliLock(self.rig.lock_path)
        self.assertEqual(other.try_acquire(), "acquired")
        try:
            with self.assertRaises(Refused) as refused:
                self.worker.admit_turn(self.rig.turn_body(billing_capable=True))
        finally:
            other.release()
        self.assertEqual(refused.exception.reason, "warning_chat")
        self.assertEqual(self.entries(), [])


class CreditLedgerBoundTests(unittest.TestCase):
    def test_a_full_ledger_sends_new_launches_to_the_subscription_and_discards_nothing(self):
        def prepare(rig):
            rig.volume.credit_ledger.write_text(json.dumps({
                "schema_version": 1, "latches": {"account": None, "models": {}}, "last_fallback": None,
                "entries": [{"attempt_id": f"{n:032x}", "model": "m", "started_at": "2026-11-20T00:00:00Z",
                             "settled_at": "2026-11-20T00:00:01Z", "known_usd": 0.1, "unknown": False,
                             "state": "settled"} for n in range(2000)]}))
        rig = WorkerRig(with_key=True, prepare=prepare)
        try:
            attempt = rig.worker.admit_turn(rig.turn_body(billing_capable=True))
            outcome = rig.wait(attempt.attempt_id)["outcome"]
            self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))
            self.assertEqual(len(rig.worker.credits.view()["entries"]), 2000)
            self.assertTrue(rig.worker.credits.view()["ledger_full"])
        finally:
            rig.close()


class ResumeBillingTests(CreditTestCase):
    def api_session(self):
        body, outcome = self.go()
        return body, outcome["session_id"]

    def resume(self, body, session, **kwargs):
        return self.rig.turn_body(run_id=body["run_id"], kind="resume", session_id=session, turn_kind="follow_up",
                                  **{"billing_capable": True, **kwargs})

    def test_an_api_session_resumes_on_credits_and_a_subscription_session_stays_on_the_subscription(self):
        body, session = self.api_session()
        attempt = self.worker.admit_turn(self.resume(body, session))
        self.assertEqual(self.rig.wait(attempt.attempt_id)["outcome"]["billing"], "api_credits")
        sub_body, sub = self.go(billing="subscription")
        attempt = self.worker.admit_turn(self.resume(sub_body, sub["session_id"]))
        self.assertEqual(self.rig.wait(attempt.attempt_id)["outcome"]["billing"], "subscription")

    def test_an_api_session_that_cannot_stay_on_credits_is_refused_before_any_spawn(self):
        body, session = self.api_session()
        spawned, entries = len(self.rig.calls()), len(self.entries())
        changes = {"disabled": lambda: self.rig.policy["claude"]["api_credits"].update(enabled=False),
                   "no key": lambda: self.rig.volume.api_key.unlink(),
                   "expired": lambda: setattr(self.rig, "now", UNTIL),
                   "latched": lambda: self.worker.credits.latch("exhausted", "sonnet")}
        for name, change in changes.items():
            with self.subTest(name):
                change()
                with self.assertRaises(Refused) as refused:
                    self.worker.admit_turn(self.resume(body, session))
                self.assertEqual(refused.exception.reason, "replay_required")
                self.assertEqual(len(self.rig.calls()), spawned)
                self.assertEqual(len(self.entries()), entries)
                self.assertIsNone(self.worker.table.current)
        self.assertTrue(self.rig.volume.session_record(body["run_id"]))  # nothing was dropped

    def test_a_forced_subscription_resume_of_an_api_session_is_refused_too(self):
        body, session = self.api_session()
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.resume(body, session, billing="subscription"))
        self.assertEqual(refused.exception.reason, "replay_required")

    def test_the_old_protocol_never_gets_replay_required_or_a_credit_fallback(self):
        body, session = self.api_session()
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.resume(body, session, billing_capable=None))
        self.assertEqual(refused.exception.reason, "session_missing")
        self.rig.scenario({"events": [assistant("x", error="billing_error"),
                                      result(0, error="Credit balance is too low", cost=0)], "exit_code": 1})
        before = len(self.rig.credit_launches())
        _, outcome = self.go(billing_capable=None)
        self.assertNotEqual(outcome["reason"], "credit_fallback")
        self.assertEqual(len(self.rig.credit_launches()), before)  # legacy requests never launch on credits

    def test_api_sessions_counts_transcripts_not_markers(self):
        body, _ = self.api_session()
        count = lambda: dispatch(self.worker, "GET", "/credits", None)[1]["api_sessions"]
        self.assertEqual(count(), 1)
        for transcript in (self.rig.volume.run_dir(body["run_id"]) / "home").glob("**/*.jsonl"):
            transcript.unlink()
        self.assertEqual(count(), 0)  # the session.json marker alone does not count

    def test_a_worker_that_starts_with_credits_disabled_drops_api_sessions_only(self):
        api_body, _ = self.api_session()
        sub_body, _ = self.go(billing="subscription")
        self.assertEqual(dispatch(self.worker, "GET", "/credits", None)[1]["api_sessions"], 1)  # the subscription one is not counted
        policy = copy.deepcopy(self.rig.policy)
        policy["claude"]["api_credits"]["enabled"] = False
        restarted = Worker(self.rig.volume, IdentityChecker(self.rig.cli_root, policy), CliLock(self.rig.lock_path),
                           meminfo=self.rig.meminfo, policy=policy, dns=self.worker.dns, now=lambda: self.rig.now)
        restarted.start()
        try:
            self.assertEqual(dispatch(restarted, "GET", "/credits", None)[1]["api_sessions"], 0)
            self.assertIsNone(self.rig.volume.session_record(api_body["run_id"]))
            self.assertEqual(list((self.rig.volume.run_dir(api_body["run_id"]) / "home").glob("**/*.jsonl")), [])
            self.assertTrue(self.rig.volume.session_record(sub_body["run_id"]))
            self.assertTrue(list((self.rig.volume.run_dir(sub_body["run_id"]) / "home").glob("**/*.jsonl")))
        finally:
            restarted.close()


class SessionProvenanceTests(CreditTestCase):
    def api_session(self):
        body, outcome = self.go()
        return body, outcome["session_id"]

    def resume(self, body, session, **kwargs):
        return self.rig.turn_body(run_id=body["run_id"], kind="resume", session_id=session, turn_kind="follow_up",
                                  **{"billing_capable": True, **kwargs})

    def record_path(self, body):
        return self.rig.volume.run_dir(body["run_id"]) / "session.json"

    def count(self):
        return dispatch(self.worker, "GET", "/credits", None)[1]["api_sessions"]

    def test_the_billing_origin_is_durable_before_the_process_spawns(self):
        self.rig.scenario({"mode": "hang"})
        body = self.rig.turn_body(billing_capable=True)
        attempt = self.worker.admit_turn(body)
        deadline = time.monotonic() + 15
        while self.worker.table.get(attempt.attempt_id)["state"] != "running" and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(json.loads(self.record_path(body).read_text())["billing"], "api_credits")
        self.worker.table.cancel(attempt.attempt_id)
        self.rig.wait(attempt.attempt_id)

    def test_a_record_that_cannot_be_saved_stops_the_launch_before_any_spawn(self):
        with mock.patch("projects.vehicle_data.research_volume.atomic_json", side_effect=OSError("disk full")):
            _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["reason"]), ("failed", "provider_error"))
        self.assertEqual(self.rig.calls(), [])
        self.assertEqual(self.entries(), [])  # the reserved entry was withdrawn: nothing spawned

    def test_a_resume_with_missing_or_unreadable_provenance_is_refused_on_either_credential(self):
        for name, damage in (("missing", lambda path: path.unlink()),
                             ("unreadable", lambda path: path.write_text("{not json")),
                             ("not an object", lambda path: path.write_text("[]")),
                             ("odd billing", lambda path: path.write_text(json.dumps(
                                 {"provider": "claude", "session_id": "x", "billing": "gift"})))):
            with self.subTest(name):
                body, session = self.api_session()
                damage(self.record_path(body))
                spawned = len(self.rig.calls())
                for capable in (True, None):
                    with self.assertRaises(Refused) as refused:
                        self.worker.admit_turn(self.resume(body, session, billing_capable=capable))
                    self.assertEqual(refused.exception.reason, "session_missing")
                self.assertEqual(len(self.rig.calls()), spawned)

    def test_an_intact_record_from_before_billing_was_recorded_resumes_on_the_subscription(self):
        body, session = self.go(billing="subscription")[0], None
        record = json.loads(self.record_path(body).read_text())
        session = record["session_id"]
        del record["billing"]
        self.record_path(body).write_text(json.dumps(record))
        attempt = self.worker.admit_turn(self.resume(body, session))
        outcome = self.rig.wait(attempt.attempt_id)["outcome"]
        self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))

    def test_the_rollback_count_includes_a_transcript_whose_provenance_is_missing(self):
        body, _ = self.api_session()
        self.record_path(body).unlink()
        self.assertEqual(self.count(), 1)
        self.record_path(body).write_text("{not json")
        self.assertEqual(self.count(), 1)

    def test_an_intact_subscription_or_legacy_record_is_not_an_api_session(self):
        body, _ = self.go(billing="subscription")
        self.assertEqual(self.count(), 0)
        record = json.loads(self.record_path(body).read_text())
        del record["billing"]
        self.record_path(body).write_text(json.dumps(record))
        self.assertEqual(self.count(), 0)

    def test_a_deletion_that_leaves_a_transcript_keeps_the_marker_and_the_count(self):
        body, _ = self.api_session()
        with mock.patch("projects.vehicle_data.research_volume.shutil.rmtree"):  # a deletion that did nothing
            self.assertEqual(self.rig.volume.drop_sessions_billed("api_credits"), 1)
        self.assertEqual(json.loads(self.record_path(body).read_text())["billing"], "api_credits")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.rig.volume.drop_sessions_billed("api_credits"), 0)  # a real deletion verifies
        self.assertFalse(self.record_path(body).exists())
        self.assertEqual(self.count(), 0)

    def test_a_disabled_worker_start_drops_a_transcript_with_no_provenance(self):
        body, _ = self.api_session()
        self.record_path(body).unlink()
        policy = copy.deepcopy(self.rig.policy)
        policy["claude"]["api_credits"]["enabled"] = False
        restarted = Worker(self.rig.volume, IdentityChecker(self.rig.cli_root, policy), CliLock(self.rig.lock_path),
                           meminfo=self.rig.meminfo, policy=policy, dns=self.worker.dns, now=lambda: self.rig.now)
        restarted.start()
        try:
            self.assertEqual(dispatch(restarted, "GET", "/credits", None)[1]["api_sessions"], 0)
        finally:
            restarted.close()

    def test_a_fresh_launch_that_cannot_clear_the_runs_old_session_does_not_spawn(self):
        body, _ = self.api_session()
        spawned = len(self.rig.calls())
        with mock.patch("projects.vehicle_data.research_volume.shutil.rmtree"):
            _, outcome = self.go(run_id=body["run_id"])
        self.assertEqual((outcome["status"], outcome["reason"]), ("failed", "provider_error"))
        self.assertEqual(len(self.rig.calls()), spawned)

    def test_an_unenumerable_transcript_directory_is_not_proof_of_absence(self):
        if os.geteuid() == 0:
            self.skipTest("a root process can list any directory")
        body, _ = self.api_session()
        projects = self.rig.volume.run_dir(body["run_id"]) / "home" / ".claude" / "projects"
        projects.chmod(0)
        try:
            self.assertEqual(self.count(), 1)  # a scan error counts as a possible transcript
            self.assertEqual(self.rig.volume.drop_sessions_billed("api_credits"), 1)
            self.assertTrue(self.record_path(body).exists())  # the marker is kept
            self.record_path(body).unlink()
            self.assertEqual(self.count(), 1)
        finally:
            projects.chmod(0o700)

    def test_a_marker_for_another_session_does_not_authorize_the_transcript(self):
        body, session = self.api_session()
        record = json.loads(self.record_path(body).read_text())
        record.update(session_id="another-session", billing="subscription")
        self.record_path(body).write_text(json.dumps(record))
        for capable in (True, None):
            with self.assertRaises(Refused) as refused:
                self.worker.admit_turn(self.resume(body, session, billing_capable=capable))
            self.assertEqual(refused.exception.reason, "session_missing")

    def test_a_damaged_non_object_record_does_not_stop_a_disabled_worker_from_starting(self):
        body, _ = self.api_session()
        self.record_path(body).write_text('["x"]')
        policy = copy.deepcopy(self.rig.policy)
        policy["claude"]["api_credits"]["enabled"] = False
        restarted = Worker(self.rig.volume, IdentityChecker(self.rig.cli_root, policy), CliLock(self.rig.lock_path),
                           meminfo=self.rig.meminfo, policy=policy, dns=self.worker.dns, now=lambda: self.rig.now)
        restarted.start()
        try:
            self.assertEqual(dispatch(restarted, "GET", "/credits", None)[1]["api_sessions"], 0)
        finally:
            restarted.close()

    def test_a_transcript_that_holds_the_key_is_retired_and_cannot_be_resumed(self):
        self.rig.scenario({"echo_key": True})
        body, outcome = self.go()
        self.assertEqual(outcome["status"], "complete")
        self.assertFalse(outcome["session_present"])
        self.assertIsNone(outcome["session_id"])
        self.assertEqual(list((self.rig.volume.run_dir(body["run_id"]) / "home").glob("**/*.jsonl")), [])
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.resume(body, "x" * 36))
        self.assertEqual(refused.exception.reason, "session_missing")

    def test_a_tainted_session_that_cannot_be_deleted_is_still_unresumable_and_counted(self):
        self.rig.scenario({"echo_key": True})
        with mock.patch("projects.vehicle_data.research_volume.shutil.rmtree"):
            body, outcome = self.go()
        self.assertIsNone(outcome["session_id"])
        record = json.loads(self.record_path(body).read_text())
        self.assertIs(record["poisoned"], True)
        with self.assertRaises(Refused) as refused:
            self.worker.admit_turn(self.resume(body, record["session_id"]))
        self.assertEqual(refused.exception.reason, "session_missing")
        self.assertEqual(self.count(), 1)  # the transcript with the key blocks rollback until it is gone

    def test_a_clean_transcript_stays_resumable(self):
        body, session = self.api_session()
        attempt = self.worker.admit_turn(self.resume(body, session))
        self.assertEqual(self.rig.wait(attempt.attempt_id)["outcome"]["status"], "complete")


class CreditSpendAndSecrecyTests(CreditTestCase):
    def test_activity_after_the_last_result_is_unknown_even_with_a_normal_exit(self):
        self.rig.scenario({"events": [assistant("a"), result(0, cost=0.25), assistant("more work")], "exit_code": 1})
        self.go()
        (entry,) = self.entries()
        self.assertEqual((entry["known_usd"], entry["unknown"]), (0.25, True))

    def test_a_signal_death_keeps_the_known_subtotal_and_marks_unknown(self):
        self.rig.scenario({"events": [assistant("a"), result(0, cost=0.25)], "kill_self": True})
        _, outcome = self.go()
        self.assertEqual(outcome["status"], "failed")
        (entry,) = self.entries()
        self.assertEqual((entry["state"], entry["known_usd"], entry["unknown"]), ("settled", 0.25, True))

    def test_an_exit_without_a_terminal_result_is_unknown_and_a_reported_api_error_is_not(self):
        self.rig.scenario({"events": [assistant("a")], "exit_code": 1})
        self.go()
        self.rig.scenario({"events": [assistant("a"), result(0, error="API Error: overloaded_error", cost=0.1)],
                           "exit_code": 1})
        self.go()
        first, second = self.entries()
        self.assertTrue(first["unknown"])
        self.assertEqual((second["known_usd"], second["unknown"]), (0.1, False))

    def test_the_installed_key_never_leaves_in_the_answer_or_the_diagnostics(self):
        text = f"SUMMARY: x\n## What it means\nthe key is {FAKE_KEY} and sk-ant-api03-OTHER_value-1 end"
        self.rig.scenario({"answer": text})
        _, outcome = self.go()
        self.assertEqual(outcome["status"], "complete")
        self.assertNotIn(FAKE_KEY, json.dumps(outcome))
        self.assertNotIn("sk-ant-", json.dumps(outcome))
        self.assertIn("[key]", outcome["answer"])

    def test_a_key_split_by_the_answer_cap_is_redacted_before_truncation(self):
        from projects.vehicle_data.dtc_research_contract import SIZES
        cap = SIZES["reportEncodedBytes"] + 4096
        text = "a" * (cap - 10) + FAKE_KEY
        self.rig.scenario({"answer": text})
        _, outcome = self.go()
        self.assertNotIn("sk-ant-", outcome["answer"])
        self.assertNotIn(FAKE_KEY[:20], outcome["answer"])

    def test_only_real_numbers_reach_the_diagnostics(self):
        for bad in (FAKE_KEY, True, float("nan"), [1]):
            with self.subTest(bad=bad):
                self.rig.scenario({"events": [assistant("a"), result(0, num_turns=bad, total_cost_usd=bad)]})
                _, outcome = self.go()
                self.assertIsNone(outcome["diagnostics"]["num_turns"])
                self.assertIsNone(outcome["diagnostics"]["total_cost_usd"])
                self.assertNotIn(FAKE_KEY, json.dumps(outcome))

    def test_an_unexpected_runner_exception_still_carries_the_billing_fields(self):
        with mock.patch.object(type(self.worker.runner), "_run_turn", side_effect=RuntimeError(f"boom {FAKE_KEY}")):
            _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"], outcome["credit_code"]),
                         ("failed", "api_credits", None))
        self.assertNotIn(FAKE_KEY, json.dumps(outcome))
        self.assertEqual(self.entries(), [])  # nothing had spawned, so the reservation was withdrawn


class CreditLedgerReportingTests(unittest.TestCase):
    def test_malformed_ledgers_never_stop_the_worker_and_subscription_research_still_works(self):
        entry = {"attempt_id": "a" * 32, "model": "sonnet", "started_at": "2026-11-20T00:00:00Z",
                 "settled_at": "2026-11-20T00:00:01Z", "known_usd": 0.1, "unknown": False, "state": "settled"}
        base = {"schema_version": 1, "entries": [entry], "latches": {"account": None, "models": {}},
                "last_fallback": None}
        cases = {
            "list-valued model latch": {**base, "latches": {"account": None, "models": {"m": ["x"]}}},
            "list-valued account latch": {**base, "latches": {"account": ["exhausted"], "models": {}}},
            "list-valued last_fallback code": {**base, "last_fallback": {"at": "2026-11-20T00:00:00Z",
                                                                         "code": ["exhausted"], "model": "m"}},
            "dict-valued last_fallback code": {**base, "last_fallback": {"at": "2026-11-20T00:00:00Z",
                                                                         "code": {}, "model": "m"}},
        }
        for name, record in cases.items():
            with self.subTest(name):
                self._check(json.dumps(record))
        with self.subTest("oversized file"):
            self._check(json.dumps({**base, "padding": "x" * (5 * 1024 * 1024)}))
        with self.subTest("deeply nested"):
            self._check("[" * 100000)

    def _check(self, text):
        rig = WorkerRig(with_key=True, prepare=lambda rig: rig.volume.credit_ledger.write_text(text))
        try:
            self.assertIs(dispatch(rig.worker, "GET", "/credits", None)[1]["ledger_available"], False)
            attempt = rig.worker.admit_turn(rig.turn_body(billing_capable=True))
            outcome = rig.wait(attempt.attempt_id)["outcome"]
            self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))
            self.assertEqual(rig.volume.credit_ledger.read_text(), text)  # left untouched
        finally:
            rig.close()

    def test_a_corrupt_ledger_is_reported_unavailable_and_the_route_still_answers(self):
        def prepare(rig):
            rig.volume.credit_ledger.write_text(json.dumps({"schema_version": 1, "entries": [{"state": "open"}],
                                                            "latches": {"account": None, "models": {}}}))
        rig = WorkerRig(with_key=True, prepare=prepare)
        try:
            status, view = dispatch(rig.worker, "GET", "/credits", None)
            self.assertEqual(status, 200)
            self.assertIs(view["ledger_available"], False)
            attempt = rig.worker.admit_turn(rig.turn_body(billing_capable=True))
            self.assertEqual(rig.wait(attempt.attempt_id)["outcome"]["billing"], "subscription")
            self.assertIn("entries", json.loads(rig.volume.credit_ledger.read_text()))  # left untouched
            self.assertIs(dispatch(rig.worker, "GET", "/credits", None)[1]["ledger_available"], False)
        finally:
            rig.close()

    def test_a_healthy_ledger_is_reported_available(self):
        rig = WorkerRig(with_key=True)
        try:
            self.assertIs(dispatch(rig.worker, "GET", "/credits", None)[1]["ledger_available"], True)
        finally:
            rig.close()


class CreditRoutesAndSecrecyTests(CreditTestCase):
    def test_status_advertises_billing_capable(self):
        status, payload = dispatch(self.worker, "GET", "/status", None)
        self.assertEqual(status, 200)
        self.assertIs(payload["billing_capable"], True)

    def test_credits_route_reports_the_ledger_never_the_key_or_a_path(self):
        self.go()
        status, view = dispatch(self.worker, "GET", "/credits", None)
        self.assertEqual(status, 200)
        self.assertEqual((view["key_installed"], view["enabled"], view["until"]), (True, True, "2026-12-08T00:00:00Z"))
        self.assertEqual(set(view), {"key_installed", "enabled", "until", "entries", "latches", "last_fallback",
                                    "ledger_full", "ledger_available", "api_sessions"})
        self.assertEqual(set(view["entries"][0]), {"attempt_id", "model", "started_at", "settled_at", "known_usd",
                                                   "unknown", "state"})
        self.assertNotIn(FAKE_KEY, json.dumps(view))
        self.assertNotIn("anthropic-api-key", json.dumps(view))
        self.rig.volume.api_key.unlink()
        self.assertFalse(dispatch(self.worker, "GET", "/credits", None)[1]["key_installed"])
        self.rig.install_key(mode=0o644)  # too open: not installed
        self.assertFalse(dispatch(self.worker, "GET", "/credits", None)[1]["key_installed"])

    def test_a_disabled_policy_is_reported_and_launches_stay_on_the_subscription(self):
        self.rig.policy["claude"]["api_credits"]["enabled"] = False
        self.assertFalse(dispatch(self.worker, "GET", "/credits", None)[1]["enabled"])
        _, outcome = self.go()
        self.assertEqual(outcome["billing"], "subscription")

    def test_after_until_every_launch_is_subscription(self):
        self.rig.now = UNTIL
        _, outcome = self.go()
        self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))

    def test_removing_the_key_needs_no_restart(self):
        self.rig.volume.api_key.unlink()
        _, outcome = self.go()
        self.assertEqual(outcome["billing"], "subscription")

    def test_the_key_never_appears_in_outcomes_records_logs_or_exceptions(self):
        stderr, outcomes = io.StringIO(), []
        with contextlib.redirect_stderr(stderr):
            self.rig.scenario({"events": [assistant(FAKE_KEY, error="billing_error"),
                                          result(0, error=f"Credit balance is too low ({FAKE_KEY})", cost=0)],
                               "stderr": FAKE_KEY, "exit_code": 1})
            outcomes.append(self.go()[1])
            self.worker.credits.ledger.start()  # a restart clears the latch so credits are eligible again
            with mock.patch.object(self.worker.runner, "guarded_run", side_effect=RuntimeError(f"boom {FAKE_KEY}")):
                outcomes.append(self.go()[1])
        self.assertEqual(outcomes[0]["credit_code"], "exhausted")
        self.assertEqual(outcomes[1]["reason"], "provider_error")
        self.assertIn("[key]", outcomes[1]["detail"])
        for blob in (json.dumps(outcomes), stderr.getvalue(), self.rig.volume.credit_ledger.read_text(),
                     json.dumps(self.worker.credits.view()), json.dumps(self.worker.status())):
            self.assertNotIn(FAKE_KEY, blob)
        self.assertEqual(len(self.entries()), 1)  # the launch that never spawned left no entry

    def test_the_timing_journal_line_carries_billing_and_credit_code(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.go()
            self.rig.scenario({"events": [assistant("x", error="billing_error"),
                                          result(0, error="Credit balance is too low", cost=0)], "exit_code": 1})
            self.go()
        lines = [json.loads(line) for line in stderr.getvalue().splitlines() if '"dtc_research_timing"' in line]
        self.assertEqual([(l["billing"], l["credit_code"]) for l in lines],
                         [("api_credits", None), ("api_credits", "exhausted")])
        self.assertEqual(lines[1]["reason"], "credit_fallback")

    def test_a_bad_billing_field_is_invalid_not_ignored(self):
        for bad in ({"billing_capable": "yes"}, {"billing": "api_credits"}):
            with self.subTest(bad):
                body = self.rig.turn_body()
                body.update(bad)
                with self.assertRaises(Refused) as refused:
                    self.worker.admit_turn(body)
                self.assertEqual(refused.exception.reason, "invalid")


if __name__ == "__main__":
    unittest.main()
