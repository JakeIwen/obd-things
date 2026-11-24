"""Credit-failure fallback, cancel intent and replay-required in the advisor, against a scripted worker."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from projects.vehicle_data.api_credits_contract import FALLBACK
from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data.dtc_research import REFUSAL_DETAIL, ResearchManager
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_reconcile import Reconciler
from projects.vehicle_data.dtc_research_store import Store
from tests.dtc_research_scripted_worker import (
    ScriptedWorker, StubCatalog, complete, credit_failure, credit_leg_then_subscription,
)
from tests.test_dtc_research_manager import Broker, CODE

INSIDE_GRANTS = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # before ``until`` whatever today's date is
FAST = {"retrySeconds": .05, "windowSeconds": 30}


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


class FallbackBase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        patch = mock.patch.dict(FALLBACK, FAST)
        patch.start()
        self.addCleanup(patch.stop)
        self.managers = []
        self.worker = ScriptedWorker(behavior=credit_leg_then_subscription())
        self.manager = self.build()

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        wait_for(lambda: not any(m.following or m.fallbacks for m in self.managers), 5)
        self.state.cleanup()

    def build(self, ledger_limit=60, worker=None, reconcile=True):
        base = Path(self.state.name)
        store = Store(base / "runs")
        store.load(log=lambda line: None)
        manager = ResearchManager(store, Ledger(base / "admissions.json", limit=ledger_limit), StubCatalog(),
                                  worker or self.worker, Broker(), poll=.02, pause_file=base / "paused",
                                  clock=lambda: INSIDE_GRANTS)
        if reconcile:
            self.assertTrue(Reconciler(manager, retry=.05).reconcile_once())
        self.managers.append(manager)
        return manager

    def post(self, **body):
        return self.manager.handle("POST", "/v1/assistant/dtc-research", {"request_id": uuid.uuid4().hex, **body})["run"]

    def start(self):
        return self.post(action="start", **CODE, provider="claude", model="opus", effort="high")

    def ask(self, run_id, question="And then?"):
        return self.post(action="ask", run_id=run_id, question=question)

    def cancel(self, run_id):
        return self.post(action="cancel", run_id=run_id)

    def settle(self, run_id, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.handle("GET", f"/v1/assistant/dtc-research/run/{run_id}", None)["run"]
            if not run["busy"]:
                return run
            time.sleep(.02)
        raise AssertionError("run did not settle")

    def ledger_kinds(self):
        launches = json.loads((Path(self.state.name) / "admissions.json").read_text())["launches"]
        return [entry["kind"] for entry in launches]

    def turn(self, run_id, index=-1):
        return self.manager.store.records[run_id]["turns"][index]


class FallbackTests(FallbackBase):
    def test_an_exhausted_credit_leg_is_relaunched_on_the_subscription_as_one_turn(self):
        run = self.settle(self.start()["run_id"])
        self.assertEqual((run["state"], len(run["turns"])), ("done", 1))
        first, second = self.worker.launches
        self.assertTrue(first["billing_capable"])
        self.assertNotIn("billing", first)
        self.assertEqual((second["billing"], second["kind"], second["turn_kind"]), ("subscription", "initial", "report"))
        self.assertEqual(second["prompt"], first["prompt"])  # the original masked first prompt
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])
        turn = self.turn(run["run_id"])
        self.assertEqual((turn["billing"], turn["credit_code"], turn["pending_fallback"]), ("subscription", "exhausted", None))
        self.assertEqual(turn["fallback_of"], {"successor": second["attempt_id"], "predecessor": first["attempt_id"]})
        self.assertEqual(turn["attempt_ids"], [first["attempt_id"], second["attempt_id"]])
        self.assertIn(first["attempt_id"], self.worker.acked)  # the credit outcome was taken

    def test_a_follow_up_fallback_is_a_replay_in_a_fresh_session(self):
        self.worker.behavior = lambda body: credit_failure("model_unavailable") \
            if body["turn_kind"] == "follow_up" and body.get("billing") != "subscription" else complete(body)
        run = self.settle(self.start()["run_id"])
        self.ask(run["run_id"], "Which bracket?")
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["turns"][-1]["mode"], run["turns"][-1]["state"]), ("done", "replayed", "complete"))
        resume, replay = self.worker.launches[1:]
        self.assertEqual((resume["kind"], replay["kind"], replay["billing"]), ("resume", "initial", "subscription"))
        self.assertIn("Which bracket?", replay["prompt"])
        self.assertIn("OWNER ASKS NOW", replay["prompt"])
        self.assertEqual(self.ledger_kinds(), ["initial", "resume", "fallback"])
        self.assertEqual(len(run["turns"]), 3)  # report, question, one answer

    def test_a_credit_failure_without_the_capability_never_happens_and_the_body_stays_old(self):
        worker = ScriptedWorker(capable=False)
        self.manager = self.build(worker=worker)
        self.assertEqual(self.settle(self.start()["run_id"])["state"], "done")
        [body] = worker.launches
        self.assertNotIn("billing_capable", body)
        self.assertNotIn("billing", body)

    def test_the_daily_cap_fails_the_fallback_as_daily_limit(self):
        self.manager = self.build(ledger_limit=1)
        run = self.settle(self.start()["run_id"])
        self.assertEqual((run["state"], run["reason"]), ("failed", "daily_limit"))
        self.assertEqual(run["error"], "The daily research limit (1 launches) has been reached")
        self.assertEqual(len(self.worker.launches), 1)

    def test_a_lost_fallback_reply_is_followed_never_resent(self):
        self.worker.lose_reply_for = {2}
        run = self.settle(self.start()["run_id"])
        self.assertEqual(run["state"], "done")
        ids = [b["attempt_id"] for b in self.worker.launches]
        self.assertEqual((len(ids), len(set(ids))), (2, 2))


class FallbackBusyTests(FallbackBase):
    def refuse_fallbacks(self, count, reason="busy", detail="busy now"):
        """Refuse the next ``count`` launches after the credit leg has been launched."""
        original = self.worker.behavior

        def behave(body):
            if body.get("billing") != "subscription" and not self.worker.refusals:
                self.worker.refusals = [(409, {"reason": reason, "detail": detail})] * count
            return original(body)

        self.worker.behavior = behave

    def test_a_busy_refusal_is_retried_and_refusals_are_not_counted(self):
        self.refuse_fallbacks(3)
        run = self.settle(self.start()["run_id"])
        self.assertEqual(run["state"], "done")
        ids = [b["attempt_id"] for b in self.worker.launches]
        self.assertEqual((len(ids), len(set(ids))), (5, 5))  # the credit leg, three refused tries, the accepted one
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_after_the_window_the_turn_fails_with_the_busy_wording(self):
        # The window must outlast one durable refused try on a loaded host; at 0.4 s a busy Pi fit
        # only two tries. The credit leg plus two refused tries is enough to show one retry.
        FALLBACK["windowSeconds"] = 1.0
        self.refuse_fallbacks(10 ** 4, reason="warning_chat", detail="ignored")
        run = self.settle(self.start()["run_id"])
        self.assertEqual((run["state"], run["reason"]), ("failed", "provider_error"))
        self.assertEqual(run["error"], REFUSAL_DETAIL["warning_chat"])
        self.assertEqual(self.ledger_kinds(), ["initial"])  # every refused try was withdrawn
        self.assertGreaterEqual(len(self.worker.launches), 3)

    def test_a_restart_after_a_refusal_continues_the_pending_fallback(self):
        self.refuse_fallbacks(10 ** 4)
        run = self.start()
        run_id = run["run_id"]
        self.assertTrue(wait_for(lambda: (self.turn(run_id).get("pending_fallback") or {}).get("refusal") == "busy"))
        self.manager.close()  # the advisor stops mid-window
        self.assertTrue(wait_for(lambda: not self.manager.fallbacks))
        saved = Store(Path(self.state.name) / "runs")
        saved.load(log=lambda line: None)
        turn = saved.records[run_id]["turns"][-1]
        self.assertEqual(turn["pending_fallback"]["predecessor"], turn["attempt_ids"][-1])  # saved after the refusal
        self.assertEqual(turn["state"], "reconciling")
        launches_before = len(self.worker.launches)
        self.worker.refusals = []
        self.worker.behavior = credit_leg_then_subscription()
        self.manager = self.build()  # reconciliation takes the pending fallback over
        run = self.settle(run_id)
        self.assertEqual(run["state"], "done")
        self.assertGreater(len(self.worker.launches), launches_before)
        self.assertEqual(self.worker.launches[-1]["billing"], "subscription")
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_a_pending_fallback_past_its_deadline_fails_after_a_restart(self):
        self.refuse_fallbacks(10 ** 4)
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: (self.turn(run_id).get("pending_fallback") or {}).get("refusal") == "busy"))
        self.manager.close()
        self.assertTrue(wait_for(lambda: not self.manager.fallbacks))
        store = Store(Path(self.state.name) / "runs")
        store.load(log=lambda line: None)
        store.records[run_id]["turns"][-1]["pending_fallback"]["deadline"] = time.time() - 1
        store.save(store.records[run_id])
        self.worker.refusals = []
        self.manager = self.build()
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["reason"]), ("failed", "provider_error"))
        self.assertEqual(run["error"], "busy now")


class CancelTests(FallbackBase):
    def test_cancel_during_the_credit_leg_is_never_relaunched(self):
        self.worker.behavior = lambda body: None  # the credit process keeps running
        run_id = self.start()["run_id"]
        run = self.cancel(run_id)
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["reason"]), ("cancelled", "cancelled"))
        self.assertEqual(len(self.worker.launches), 1)
        self.assertTrue(self.turn(run_id)["cancel_requested"])

    def test_cancel_between_the_credit_outcome_and_the_relaunch_never_relaunches(self):
        self.worker.withhold = True  # the credit failure is held on the worker, not yet seen
        run_id = self.start()["run_id"]
        self.cancel(run_id)
        self.worker.release()
        run = self.settle(run_id)
        self.assertEqual(run["state"], "cancelled")
        self.assertEqual(len(self.worker.launches), 1)

    def test_cancel_while_the_fallback_waits_for_the_slot_settles_it_at_once(self):
        self.worker.behavior = lambda body: credit_failure()
        self.worker.refusals = []
        original = self.worker.behavior

        def behave(body):
            if not self.worker.refusals:
                self.worker.refusals = [(409, {"reason": "busy", "detail": "busy now"})] * 10 ** 4
            return original(body)

        self.worker.behavior = behave
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: (self.turn(run_id).get("pending_fallback") or {}).get("refusal") == "busy"))
        # A cancel that lands while a retry is out is settled when that send returns,
        # so the reply to the cancel itself may still say running.
        self.cancel(run_id)
        run = self.settle(run_id)
        self.assertEqual(run["state"], "cancelled")
        count = len(self.worker.launches)
        time.sleep(.3)
        self.assertEqual(len(self.worker.launches), count)  # nothing was sent after the cancel
        self.assertIsNone(self.turn(run_id)["pending_fallback"])

    def test_cancel_after_the_worker_holds_a_credit_outcome_then_a_restart_does_not_relaunch(self):
        self.worker.withhold = True
        run_id = self.start()["run_id"]
        self.cancel(run_id)
        self.manager.close()
        self.assertTrue(wait_for(lambda: not self.manager.following))
        self.worker.release()  # the outcome is now held on the worker
        self.manager = self.build()  # reconciliation reads the held credit failure
        run = self.settle(run_id)
        self.assertEqual(run["state"], "cancelled")
        self.assertEqual(len(self.worker.launches), 1)

    def test_a_duplicate_predecessor_outcome_is_ignored(self):
        first = credit_leg_then_subscription()
        self.worker.behavior = lambda body: first(body) if body.get("billing") != "subscription" else None
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: len(self.worker.launches) == 2))
        predecessor = self.worker.launches[0]["attempt_id"]
        self.worker.redeliver.add(predecessor)  # its credit failure is shown as held again
        self.manager._follow(run_id, predecessor)  # a second watcher meets the duplicate
        time.sleep(.3)
        self.assertEqual(len(self.worker.launches), 2)  # no second fallback
        self.worker.finish(self.worker.launches[1]["attempt_id"], complete(self.worker.launches[1]))
        self.assertEqual(self.settle(run_id)["state"], "done")
        self.assertEqual(len(self.worker.launches), 2)


class ReplayRequiredTests(FallbackBase):
    def test_a_replay_required_refusal_becomes_a_fresh_forced_subscription_replay(self):
        self.worker.behavior = lambda body: complete(body)
        run = self.settle(self.start()["run_id"])
        self.worker.refuse_resume = "replay_required"
        self.ask(run["run_id"], "Which bracket?")
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["turns"][-1]["mode"]), ("done", "replayed"))
        refused, replay = self.worker.launches[1:]
        self.assertEqual((refused["kind"], replay["kind"], replay["billing"]), ("resume", "initial", "subscription"))
        self.assertIn("OWNER ASKS NOW", replay["prompt"])
        self.assertEqual(self.ledger_kinds(), ["initial", "replay"])  # the refusal's entry was withdrawn
        self.assertNotEqual(refused["attempt_id"], replay["attempt_id"])

    def test_an_ordinary_missing_session_replay_is_not_forced(self):
        self.worker.behavior = lambda body: complete(body)
        run = self.settle(self.start()["run_id"])
        self.worker.refuse_resume = "session_missing"
        self.ask(run["run_id"])
        self.settle(run["run_id"])
        self.assertNotIn("billing", self.worker.launches[-1])


class CancelDurabilityTests(FallbackBase):
    def test_a_cancel_that_cannot_be_saved_is_refused_and_not_forwarded(self):
        self.worker.behavior = lambda body: None
        run_id = self.start()["run_id"]
        real = self.manager.store.save

        def failing(record):
            raise OSError("disk full")

        self.manager.store.save = failing
        with self.assertRaises(ChatError) as refused:
            self.cancel(run_id)
        self.assertEqual(refused.exception.reason, "storage_error")
        self.assertEqual(self.worker.cancelled, [])  # nothing was forwarded or acknowledged
        self.assertFalse(self.turn(run_id).get("cancel_requested"))
        self.manager.store.save = real
        self.cancel(run_id)  # the owner can try again once the disk works
        self.assertEqual(self.settle(run_id)["state"], "cancelled")

    def test_a_saved_cancel_whose_forward_was_lost_is_sent_again_when_a_restart_adopts_the_attempt(self):
        self.worker.behavior = lambda body: None  # the credit process keeps running
        run_id = self.start()["run_id"]
        attempt = self.worker.launches[0]["attempt_id"]
        self.worker.down = True
        self.cancel(run_id)  # saved, but the worker never hears of it
        self.manager.close()
        self.assertTrue(wait_for(lambda: not self.manager.following))
        self.worker.down = False
        self.assertEqual(self.worker.cancelled, [])
        self.worker.job = {"attempt_id": attempt, "kind": "turn"}  # what the worker reports for a running attempt
        self.manager = self.build()  # reconciliation adopts the still-running attempt
        self.assertTrue(wait_for(lambda: attempt in self.worker.cancelled))
        self.assertEqual(self.settle(run_id)["state"], "cancelled")


class FallbackLockTests(FallbackBase):
    def test_a_blocked_fallback_launch_does_not_block_cancel_or_progress(self):
        self.worker.behavior = lambda body: credit_failure() if body.get("billing") != "subscription" else None
        entered, gate = threading.Event(), threading.Event()
        real = self.worker.launch

        def slow(route, body):
            if body.get("billing") == "subscription":
                entered.set()
                gate.wait(20)
            return real(route, body)

        self.worker.launch = slow
        self.addCleanup(gate.set)
        run_id = self.start()["run_id"]
        self.assertTrue(entered.wait(10))  # the fallback request is out and its reply is blocked
        answered = []

        def probe():
            self.manager.handle("GET", f"/v1/assistant/dtc-research/run/{run_id}/progress", None)
            answered.append(self.cancel(run_id))

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        thread.join(3)
        self.assertEqual(len(answered), 1, "progress and cancel waited for the worker's launch reply")
        gate.set()
        run = self.settle(run_id)
        self.assertEqual(run["state"], "cancelled")  # the cancel saved meanwhile reaches the accepted fallback
        self.assertEqual(len(self.worker.launches), 2)


class FallbackRefusalDurabilityTests(FallbackBase):
    def refuse_fallback_with(self, reason, times):
        original = self.worker.behavior

        def behave(body):
            if body.get("billing") != "subscription" and not self.worker.refusals:
                self.worker.refusals = [(409, {"reason": reason, "detail": "refused"})] * times
            return original(body)

        self.worker.behavior = behave

    def test_a_refusal_whose_withdrawal_cannot_be_saved_continues_the_fallback_after_a_restart(self):
        self.refuse_fallback_with("busy", 10 ** 4)
        real = self.manager.store.save
        refused_once = threading.Event()

        def failing(record):
            pending = record["turns"][-1].get("pending_fallback") or {}
            if pending.get("refusal"):
                refused_once.set()
                raise OSError("disk full")  # the restored predecessor cannot be saved
            return real(record)

        self.manager.store.save = failing
        run_id = self.start()["run_id"]
        self.assertTrue(refused_once.wait(10))
        self.manager.close()
        self.assertTrue(wait_for(lambda: not self.manager.fallbacks))
        saved = Store(Path(self.state.name) / "runs")
        saved.load(log=lambda line: None)
        turn = saved.records[run_id]["turns"][-1]
        self.assertEqual(len(turn["attempt_ids"]), 2)  # disk still names the refused successor, as "sending"
        self.assertEqual(turn["pending_fallback"]["sending"]["attempt_id"], turn["attempt_ids"][-1])
        self.worker.refusals = []
        self.worker.behavior = credit_leg_then_subscription()
        self.manager = self.build()  # the fenced, never-admitted reservation is withdrawn; the fallback continues
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))
        self.assertNotEqual(run["turns"][0].get("reason"), "interrupted")
        self.assertEqual(self.worker.launches[-1]["billing"], "subscription")
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_a_stale_instance_between_retries_refreshes_the_instance_and_continues(self):
        self.refuse_fallback_with("stale_instance", 1)
        original = self.worker.behavior
        restarted = "new" * 10 + "ab"

        def behave(body):
            result = original(body)
            if body.get("billing") != "subscription":
                self.worker.instance_id = restarted  # the worker restarts before the fallback's first try
            return result

        self.worker.behavior = behave
        run = self.settle(self.start()["run_id"])
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))
        refused, accepted = self.worker.launches[1:]
        self.assertNotEqual(refused["expected_instance_id"], restarted)
        self.assertEqual((accepted["expected_instance_id"], accepted["billing"]), (restarted, "subscription"))
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_an_ambiguous_fallback_launch_is_never_resent_after_the_restart_that_fenced_it(self):
        self.worker.lose_reply_for = {2}  # the fallback request arrives, its reply is lost
        self.worker.behavior = lambda body: credit_failure() if body.get("billing") != "subscription" else None
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: len(self.worker.launches) == 2))
        self.manager.close()
        self.assertTrue(wait_for(lambda: not self.manager.following and not self.manager.fallbacks))
        self.worker.job = {"attempt_id": self.worker.launches[1]["attempt_id"], "kind": "turn"}
        self.manager = self.build()  # the worker knows the attempt: it is adopted, not relaunched
        self.assertEqual(len(self.worker.launches), 2)
        self.worker.finish(self.worker.launches[1]["attempt_id"], complete(self.worker.launches[1]))
        self.assertEqual(self.settle(run_id)["state"], "done")
        self.assertEqual(len(self.worker.launches), 2)


class Crash(BaseException):
    """The advisor dying inside a call (never caught by the fallback thread's ``except Exception``)."""


class FallbackRecoveryTests(FallbackBase):
    def crash_after_the_worker_admitted_the_fallback(self):
        """Worker A admits the fallback request; the advisor dies before it records the acceptance."""
        hook = mock.patch.object(threading, "excepthook", lambda args: None)  # the simulated death's traceback
        hook.start()
        self.addCleanup(hook.stop)
        self.worker.behavior = lambda body: credit_failure() if body.get("billing") != "subscription" else None
        real = self.worker.launch

        def admitting_then_dying(route, body):
            real(route, body)
            if body.get("billing") == "subscription":
                raise Crash()
            return 202, {}

        self.worker.launch = admitting_then_dying
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: len(self.worker.launches) == 2 and not self.manager.fallbacks))
        self.worker.launch = real
        self.manager.closed.set()
        return run_id

    def replace_worker(self):
        """Worker B: a new instance that has never heard of the fallback request."""
        self.worker.attempts.clear()
        self.worker.instance_id = "b" * 32
        self.worker.job = None

    def test_a_replacement_worker_fence_is_not_proof_the_previous_worker_never_launched(self):
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.replace_worker()
        self.manager = self.build()
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["reason"]), ("failed", "interrupted"))  # the ambiguous-launch rule
        self.assertEqual(len(self.worker.launches), 2)  # no relaunch
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])  # the admission stays counted

    def test_a_plain_unknown_reply_from_the_destination_is_not_fallback_fence_proof(self):
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.worker.attempts.clear()
        self.worker.behavior = credit_leg_then_subscription()
        with mock.patch.object(self.worker, "fence", side_effect=self.worker.attempt):
            self.manager = self.build()
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["reason"]), ("failed", "interrupted"))
        self.assertEqual(len(self.worker.launches), 2)
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_a_fence_by_the_same_instance_is_proof_and_the_fallback_continues(self):
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.worker.attempts.clear()  # the request never arrived at this instance (e.g. it was lost)
        self.worker.behavior = credit_leg_then_subscription()
        self.manager = self.build()
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))
        self.assertEqual(self.ledger_kinds(), ["initial", "fallback"])

    def test_the_retained_fence_still_proves_non_admission_when_saving_the_withdrawal_failed(self):
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.worker.attempts.clear()
        self.worker.behavior = credit_leg_then_subscription()
        self.manager = self.build(reconcile=False)
        real = self.manager.store.save
        failures = []

        def failing_once(record):
            if not failures and "sending" not in (record["turns"][-1].get("pending_fallback") or {"sending": 1}):
                failures.append(1)
                raise OSError("disk full")
            return real(record)

        self.manager.store.save = failing_once
        self.assertFalse(Reconciler(self.manager, retry=.05).reconcile_once())  # fenced, withdrawal not saved
        self.assertTrue(failures)
        self.assertTrue(Reconciler(self.manager, retry=.05).reconcile_once())  # the tombstone answers now
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))

    def test_an_advisor_restart_between_fencing_and_saving_still_continues_the_fallback(self):
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.worker.attempts.clear()
        self.worker.fence(self.turn(run_id)["pending_fallback"]["sending"]["attempt_id"])  # fenced, never saved
        self.worker.behavior = credit_leg_then_subscription()
        self.manager = self.build()
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))

    def test_reconciliation_never_cancels_a_fallback_request_whose_reply_is_outstanding(self):
        self.worker.behavior = lambda body: credit_failure() if body.get("billing") != "subscription" else None
        entered, gate = threading.Event(), threading.Event()
        real = self.worker.launch

        def slow(route, body):
            reply = real(route, body)
            if body.get("billing") == "subscription":
                entered.set()
                gate.wait(20)
            return reply

        self.worker.launch = slow
        self.addCleanup(gate.set)
        run_id = self.start()["run_id"]
        self.assertTrue(entered.wait(10))
        sending = self.turn(run_id)["pending_fallback"]["sending"]["attempt_id"]
        self.worker.job = {"attempt_id": sending, "kind": "turn"}
        self.assertFalse(Reconciler(self.manager, retry=.05).reconcile_once())  # waits, as for any owned job
        self.assertEqual(self.worker.cancelled, [])
        self.assertEqual(len(self.worker.launches), 2)  # and nothing is sent a second time
        gate.set()
        self.worker.job = None
        self.worker.finish(sending, complete(self.worker.launches[1]))
        self.assertEqual(self.settle(run_id)["state"], "done")

    def test_a_thread_that_cannot_start_during_restart_recovery_leaves_the_turn_reconciling(self):
        from projects.vehicle_data import dtc_research_fallback
        original = self.worker.behavior
        self.worker.behavior = lambda body: credit_failure() if body.get("billing") != "subscription" else None
        run_id = self.crash_after_the_worker_admitted_the_fallback()
        self.worker.attempts.clear()
        self.worker.behavior = original
        real = dtc_research_fallback.threading.Thread
        failures = []

        class FailingOnce(real):
            def start(self):
                if self._target and self._target.__name__ == "_fallback_loop" and not failures:
                    failures.append(1)
                    raise RuntimeError("can't start new thread")
                super().start()

        self.manager = self.build(reconcile=False)
        reconciler = Reconciler(self.manager, retry=.05)
        with mock.patch.object(dtc_research_fallback.threading, "Thread", FailingOnce):
            with self.assertRaises(RuntimeError):
                reconciler.reconcile_once()
            self.assertEqual(self.turn(run_id)["state"], "reconciling")  # picked up again by the next pass
            self.assertNotEqual(self.manager.state, "idle")
            self.assertTrue(reconciler.reconcile_once())
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))


class StoreDurabilityTests(FallbackBase):
    def test_a_saved_cancel_syncs_the_records_directory(self):
        import os
        import stat
        self.worker.behavior = lambda body: None
        run_id = self.start()["run_id"]
        synced = []
        real = os.fsync

        def recording(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)

        with mock.patch("os.fsync", recording):
            self.cancel(run_id)
        self.assertIn(True, synced)  # the rename is durable, not only the file's bytes
        self.assertIn(False, synced)


class FallbackThreadTests(FallbackBase):
    def test_a_fallback_thread_that_cannot_start_leaves_the_fallback_to_reconciliation(self):
        from projects.vehicle_data import dtc_research_fallback
        real = dtc_research_fallback.threading.Thread
        failures = []

        class FailingOnce(real):
            def start(self):
                if self._target and self._target.__name__ == "_fallback_loop" and not failures:
                    failures.append(1)
                    raise RuntimeError("can't start new thread")
                super().start()

        with mock.patch.object(dtc_research_fallback.threading, "Thread", FailingOnce):
            run_id = self.start()["run_id"]
            self.assertTrue(wait_for(lambda: failures))
            self.assertTrue(wait_for(lambda: self.turn(run_id)["state"] != "running" or not self.manager.fallbacks))
            self.assertNotIn(run_id, self.manager.fallbacks)  # not left registered with no thread behind it
        self.assertTrue(wait_for(lambda: self.turn(run_id).get("pending_fallback")))
        self.manager.state = "unknown"
        self.assertTrue(Reconciler(self.manager, retry=.05).reconcile_once())  # takes the pending fallback over
        self.assertEqual(self.settle(run_id)["state"], "done")


class CreditsRouteTests(FallbackBase):
    def test_the_route_answers_the_view_and_a_missing_worker_is_503(self):
        view = self.manager.handle("GET", "/v1/assistant/research/credits", None)
        self.assertEqual((view["key_installed"], view["balance"]["state"]), (True, "unknown"))
        self.assertEqual(view["new_research"]["billing"], "api_credits")
        self.assertTrue(view["ledger_available"])
        self.worker.down = True
        with self.assertRaises(ChatError) as down:
            self.manager.handle("GET", "/v1/assistant/research/credits", None)
        self.assertEqual((down.exception.reason, down.exception.status), ("worker_unavailable", 503))


    def test_the_advisors_own_clock_decides_expiry_and_a_ledger_the_worker_cannot_load_bills_the_subscription(self):
        until = datetime(2026, 12, 8, tzinfo=timezone.utc)
        self.manager.clock = lambda: until - timedelta(seconds=1)
        self.assertEqual(self.manager.handle("GET", "/v1/assistant/research/credits", None)["new_research"]["billing"],
                         "api_credits")
        self.manager.clock = lambda: until
        expired = self.manager.handle("GET", "/v1/assistant/research/credits", None)
        self.assertEqual((expired["expired"], expired["new_research"]["reason"]), (True, "expired"))
        self.manager.clock = lambda: INSIDE_GRANTS
        self.worker.credits_reply = {**self.worker.credits_reply, "ledger_available": False}
        view = self.manager.handle("GET", "/v1/assistant/research/credits", None)
        self.assertEqual((view["ledger_available"], view["new_research"]["reason"]), (False, "ledger_unavailable"))


if __name__ == "__main__":
    unittest.main()
