"""Rollback controls: pause, drained and the admin CLI (docs/dtc-research.md "Promotional API credits")."""

import io
import threading
import time
import unittest
from unittest import mock
import uuid

from projects.vehicle_data.api_credits_contract import ADMIN_ROUTES
from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data import dtc_research_admin
from projects.vehicle_data.dtc_research_store import Store
from tests.dtc_research_scripted_worker import complete, credit_leg_then_subscription
from tests.test_dtc_research_fallback import CODE, FallbackBase, wait_for
from tests.test_dtc_research_manager import Broker


class BlockedBroker(Broker):
    """DTC reads wait until ``release`` is set (a start stuck gathering its context)."""

    def __init__(self):
        self.release = threading.Event()

    def request(self, method, path):
        if path == "/v1/diagnostics/dtcs":
            self.release.wait(20)
        return super().request(method, path)


class PauseAndDrainTests(FallbackBase):
    def pause(self):
        return self.manager.handle_admin("POST", ADMIN_ROUTES["pause"])

    def drained(self):
        return self.manager.handle_admin("GET", ADMIN_ROUTES["drained"])

    def test_a_held_credit_fallback_before_pause_is_answered_and_drained_waits_for_it(self):
        self.worker.withhold = True  # the credit failure is on the worker but not yet taken
        run_id = self.start()["run_id"]
        self.pause()
        first = self.drained()
        self.assertFalse(first["drained"])
        self.assertIn("turn_running", first["reasons"])
        self.worker.release()  # owed successors continue while paused
        run = self.settle(run_id)
        self.assertEqual((run["state"], run["turns"][0]["state"]), ("done", "complete"))
        self.assertEqual(self.worker.launches[-1]["billing"], "subscription")
        self.assertTrue(wait_for(lambda: self.drained()["drained"]), self.drained())
        self.assertIn(self.worker.launches[0]["attempt_id"], self.worker.acked)

    def test_a_held_outcome_nobody_has_taken_blocks_drained(self):
        self.worker.withhold = True
        self.start()
        self.pause()
        self.manager.closed.set()  # no follow thread will take the outcome
        self.assertTrue(wait_for(lambda: not self.manager.following))
        self.worker.release()
        self.assertTrue(wait_for(lambda: "held_outcome_unsettled" in self.drained()["reasons"]))
        self.assertFalse(self.drained()["drained"])

    def test_a_start_blocked_in_context_gathering_is_refused_and_drained_waits_for_it(self):
        broker = BlockedBroker()
        self.manager.broker = broker
        result = []

        def start():
            try:
                result.append(self.start())
            except ChatError as exc:
                result.append(exc)

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: self.manager.starting))
        self.pause()
        reply = self.drained()
        self.assertFalse(reply["drained"])
        self.assertIn("start_in_progress", reply["reasons"])
        broker.release.set()
        thread.join(10)
        [refusal] = result
        self.assertIsInstance(refusal, ChatError)
        self.assertEqual(refusal.reason, "provider_unavailable")
        self.assertEqual(self.worker.launches, [])  # nothing was launched
        self.assertTrue(wait_for(lambda: self.drained()["drained"]))

    def test_a_held_maintenance_outcome_does_not_block(self):
        self.worker.extra_attempts["f" * 32] = {"known": True, "instance_id": self.worker.instance_id,
                                                "state": "finished", "outcome_state": "held",
                                                "outcome": {"status": "complete"}}
        self.pause()
        self.assertTrue(self.drained()["drained"])

    def test_a_settled_turn_with_a_lost_acknowledgment_is_acknowledged_by_drained(self):
        self.worker.behavior = lambda body: complete(body)
        self.worker.ack_fails = True
        run = self.settle(self.start()["run_id"])
        attempt = run and self.worker.launches[0]["attempt_id"]
        self.assertNotIn(attempt, self.worker.acked)
        self.pause()
        self.assertFalse(self.drained()["drained"])  # the lost acknowledgment cannot be retried yet
        self.worker.ack_fails = False
        self.assertTrue(self.drained()["drained"])
        self.assertIn(attempt, self.worker.acked)

    def test_drained_is_false_unless_paused_and_when_the_worker_restarts_meanwhile(self):
        self.assertIn("not_paused", self.drained()["reasons"])
        self.pause()
        statuses = iter([self.worker.status()])

        def status():
            return next(statuses, {"instance_id": "new" * 8, "job": None})

        self.worker.status = status
        reply = self.drained()
        self.assertIn("worker_restarted", reply["reasons"])
        self.assertEqual(self.manager.state, "unknown")  # reconcile before asking again

    def ready(self):
        return self.manager.handle_admin("GET", ADMIN_ROUTES["rollbackReady"])

    def test_api_sessions_do_not_block_drained_but_block_rollback_ready_until_none_remain(self):
        self.pause()
        self.worker.credits_reply = {**self.worker.credits_reply, "enabled": False, "api_sessions": 2}
        self.assertTrue(self.drained()["drained"])  # work is drained: the sessions go at the worker restart
        self.assertIn("api_sessions_remain", self.ready()["reasons"])
        self.worker.credits_reply = {**self.worker.credits_reply, "api_sessions": 0}
        self.assertEqual(self.ready(), {"rollback_ready": True, "paused": True, "reasons": []})

    def test_rollback_ready_also_needs_drained_credits_off_and_the_ledger_state(self):
        self.worker.credits_reply = {**self.worker.credits_reply, "enabled": False}
        self.assertIn("not_paused", self.ready()["reasons"])
        self.pause()
        self.worker.credits_reply = {**self.worker.credits_reply, "enabled": True}
        self.assertEqual(self.ready()["reasons"], ["credits_enabled"])
        self.assertTrue(self.drained()["drained"])
        self.worker.credits_reply = {k: v for k, v in self.worker.credits_reply.items()
                                     if k not in ("api_sessions", "ledger_available")} | {"enabled": False}
        self.assertEqual(self.ready()["reasons"], ["api_sessions_unknown", "ledger_state_unknown"])
        self.worker.job = {"attempt_id": "a" * 32, "kind": "maintenance"}
        self.assertIn("worker_job", self.ready()["reasons"])

    def test_an_old_worker_is_drained_but_never_rollback_ready_because_it_cannot_attest(self):
        self.pause()
        self.worker.capable = False
        self.assertTrue(self.drained()["drained"])  # drained stays interoperable
        reply = self.ready()
        self.assertFalse(reply["rollback_ready"])
        self.assertEqual(reply["reasons"], ["worker_not_billing_capable"])

    def test_an_unreadable_ledger_does_not_block_rollback_but_a_missing_attestation_does(self):
        self.pause()
        self.worker.credits_reply = {**self.worker.credits_reply, "enabled": False, "ledger_available": False}
        self.assertTrue(self.ready()["rollback_ready"])
        self.worker.credits_reply = {key: value for key, value in self.worker.credits_reply.items() if key != "ledger_available"}
        self.assertEqual(self.ready()["reasons"], ["ledger_state_unknown"])

    def test_a_held_outcome_of_the_oldest_attempt_is_found_however_many_runs_follow(self):
        self.manager.window.limit = 10 ** 6
        self.manager = self.build(ledger_limit=500)
        self.manager.window.limit = 10 ** 6
        self.worker.behavior = lambda body: complete(body)
        self.worker.ack_fails = True
        first = self.settle(self.start()["run_id"])["run_id"]
        self.worker.ack_fails = False
        for _ in range(35):
            self.settle(self.start()["run_id"])
        self.worker.ack_fails = True  # the oldest turn's lost acknowledgment cannot be retried
        self.pause()
        reply = self.drained()
        self.assertFalse(reply["drained"], reply)
        self.assertIn("acknowledgment_failed", reply["reasons"])
        self.assertIn(self.manager.store.records[first]["turns"][-1]["attempt_ids"][-1], self.worker.attempts)

    def test_a_worker_job_that_appears_during_the_check_blocks(self):
        self.pause()
        statuses = [self.worker.status(), {**self.worker.status(), "job": {"attempt_id": "b" * 32, "kind": "turn"}}]
        self.worker.status = lambda: statuses.pop(0) if len(statuses) > 1 else statuses[0]
        self.assertIn("worker_job", self.drained()["reasons"])

    def test_pause_and_resume_sync_the_directory_so_the_pause_survives_a_power_loss(self):
        import os
        import stat
        synced = []
        real = os.fsync

        def recording(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)

        with mock.patch("os.fsync", recording):
            self.pause()
            self.assertIn(True, synced)  # the parent directory, not only the file
            synced.clear()
            self.manager.handle_admin("POST", ADMIN_ROUTES["resume"])
            self.assertEqual(synced, [True])

    def test_a_running_worker_job_blocks(self):
        self.worker.job = {"attempt_id": "a" * 32, "kind": "maintenance"}
        self.pause()
        self.assertIn("worker_job", self.drained()["reasons"])

    def test_pause_refuses_starts_and_questions_but_not_cancels(self):
        run = self.settle(self.start()["run_id"])
        self.pause()
        for action in (lambda: self.start(), lambda: self.ask(run["run_id"])):
            with self.assertRaises(ChatError) as paused:
                action()
            self.assertEqual(paused.exception.reason, "provider_unavailable")
            self.assertIn("paused", str(paused.exception))
        self.assertFalse(self.cancel(run["run_id"])["busy"])
        self.manager.handle_admin("POST", ADMIN_ROUTES["resume"])
        self.assertEqual(self.settle(self.start()["run_id"])["state"], "done")

    def test_the_pause_survives_an_advisor_restart_until_it_is_removed(self):
        self.pause()
        self.manager.close()
        self.manager = self.build()
        with self.assertRaises(ChatError):
            self.start()
        self.assertEqual(self.manager.handle_admin("GET", ADMIN_ROUTES["drained"])["paused"], True)
        self.manager.handle_admin("POST", ADMIN_ROUTES["resume"])
        self.manager.close()
        self.manager = self.build()
        self.assertEqual(self.settle(self.start()["run_id"])["state"], "done")

    def test_unknown_admin_requests_are_404(self):
        for method, path in (("GET", ADMIN_ROUTES["pause"]), ("POST", ADMIN_ROUTES["drained"]),
                             ("POST", ADMIN_ROUTES["rollbackReady"]),
                             ("POST", "/admin/dtc-research/other")):
            with self.assertRaises(ChatError) as unknown:
                self.manager.handle_admin(method, path)
            self.assertEqual(unknown.exception.status, 404)


class AdminCliTests(unittest.TestCase):
    class Client:
        def __init__(self, reply, status=200):
            self.reply, self.status, self.calls = reply, status, []

        def __call__(self, socket_path, timeout):
            self.socket_path = socket_path
            return self

        def request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            return self.status, self.reply

    def run_verb(self, verb, client):
        out = io.StringIO()
        return dtc_research_admin.run(verb, "/sock", client_factory=client, out=out), out.getvalue()

    def test_verbs_use_their_routes_and_drained_sets_the_exit_code(self):
        client = self.Client({"paused": True})
        self.assertEqual(self.run_verb("pause", client)[0], 0)
        self.assertEqual(client.calls, [("POST", ADMIN_ROUTES["pause"], {})])
        self.assertEqual(self.run_verb("resume", self.Client({"paused": False}))[0], 0)
        drained = self.Client({"drained": True, "reasons": []})
        self.assertEqual(self.run_verb("drained", drained)[0], 0)
        self.assertEqual(drained.calls, [("GET", ADMIN_ROUTES["drained"], None)])
        self.assertEqual(self.run_verb("drained", self.Client({"drained": False, "reasons": ["turn_running"]}))[0], 1)
        self.assertEqual(self.run_verb("pause", self.Client({"detail": "no"}, status=404))[0], 2)

    def test_rollback_ready_uses_its_route_and_exits_zero_only_when_ready(self):
        ready = self.Client({"rollback_ready": True, "reasons": []})
        self.assertEqual(self.run_verb("rollback-ready", ready)[0], 0)
        self.assertEqual(ready.calls, [("GET", ADMIN_ROUTES["rollbackReady"], None)])
        blocked = self.Client({"rollback_ready": False, "drained": True, "reasons": ["api_sessions_remain"]})
        self.assertEqual(self.run_verb("rollback-ready", blocked)[0], 1)
        self.assertEqual(self.run_verb("rollback-ready", self.Client({"drained": True}))[0], 1)  # drained alone is not enough

    def test_an_unreachable_advisor_exits_2(self):
        def refusing(socket_path, timeout):
            raise OSError("no socket")

        self.assertEqual(self.run_verb("drained", refusing)[0], 2)

    def test_the_default_socket_is_the_advisors(self):
        from projects.vehicle_data.warning_chat import DEFAULT_SOCKET
        self.assertEqual(dtc_research_admin.DEFAULT_SOCKET, DEFAULT_SOCKET)


if __name__ == "__main__":
    unittest.main()
