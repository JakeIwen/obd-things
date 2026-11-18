"""Rollback controls: pause, drained and the admin CLI (docs/dtc-research.md "Promotional API credits")."""

import io
import threading
import time
import unittest
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

    def test_an_api_created_session_blocks_drained_until_none_remain(self):
        self.pause()
        self.worker.credits_reply = {**self.worker.credits_reply, "api_sessions": 2}
        self.assertIn("api_sessions_remain", self.drained()["reasons"])
        self.worker.credits_reply = {**self.worker.credits_reply, "api_sessions": 0}
        self.assertTrue(self.drained()["drained"])

    def test_a_credits_reply_without_the_count_is_not_drained_but_an_old_worker_is(self):
        self.pause()
        self.worker.credits_reply = {k: v for k, v in self.worker.credits_reply.items() if k != "api_sessions"}
        self.assertIn("api_sessions_unknown", self.drained()["reasons"])
        self.worker.capable = False  # no billing, so no API sessions by construction
        self.assertTrue(self.drained()["drained"])

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

    def test_an_unreachable_advisor_exits_2(self):
        def refusing(socket_path, timeout):
            raise OSError("no socket")

        self.assertEqual(self.run_verb("drained", refusing)[0], 2)

    def test_the_default_socket_is_the_advisors(self):
        from projects.vehicle_data.warning_chat import DEFAULT_SOCKET
        self.assertEqual(dtc_research_admin.DEFAULT_SOCKET, DEFAULT_SOCKET)


if __name__ == "__main__":
    unittest.main()
