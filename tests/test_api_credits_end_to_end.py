"""The advisor's real manager over the real worker (fake Claude CLI): the credit protocol end to end."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from projects.vehicle_data import cli_process
from projects.vehicle_data.api_credits_contract import ADMIN_ROUTES, FALLBACK, GRANTS, UNTIL
from projects.vehicle_data.api_credits_snapshot import GrantBalance, Snapshot
from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_reconcile import Reconciler
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.dtc_research_worker import Worker
from projects.vehicle_data.model_cli_lock import CliLock
from projects.vehicle_data.research_cli_policy import IdentityChecker
from tests.research_support import FAKE_KEY, WorkerRig
from tests.test_dtc_research_fallback import FAST, wait_for
from tests.test_dtc_research_manager import Broker, CODE, REPORT, RigClient
from tests.test_research_credits import OAUTH, assistant, result

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # the advisor's frozen clock: before ``until`` whatever today is
CREDITS_PATH = "/v1/assistant/research/credits"
BILLING_FAILURE = {"events": [assistant("sorry", error="billing_error"),
                              result(0, error="Credit balance is too low", cost=0)], "exit_code": 1}


class CreditClient(RigClient):
    """The in-process worker client with the scenario changes a test needs at a launch boundary."""

    def __init__(self, rig):
        super().__init__(rig)
        self.on_subscription = None  # a scenario to install just before a forced-subscription launch
        self.billing_capable = True
        self.bodies = []

    def status(self):
        reply = super().status()
        if not self.billing_capable:
            reply.pop("billing_capable", None)
        return reply

    def launch(self, route, body):
        self.bodies.append(body)
        if self.on_subscription and body.get("billing") == "subscription":
            self.rig.scenario(self.on_subscription)
        return super().launch(route, body)


class FixedSnapshot:
    """The Mac's snapshot store with one valid record fetched before the worker's clock (a $5 grant)."""

    def __init__(self):
        label, expires = GRANTS[-1]
        grant = GrantBalance(label, expires, Decimal("5.00"))
        self.snapshot = Snapshot(datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc), (grant,), {})

    def current(self):
        return self.snapshot


class EndToEndBase(unittest.TestCase):
    scenario = {"mode": "ok", "answer": REPORT}
    capable = True

    def setUp(self):
        patch = mock.patch.dict(FALLBACK, FAST)
        patch.start()
        self.addCleanup(patch.stop)
        self.rig = WorkerRig(self.scenario, with_key=True)
        self.state = tempfile.TemporaryDirectory()
        self.client = CreditClient(self.rig)
        self.client.billing_capable = self.capable
        base = Path(self.state.name)
        store = Store(base / "runs")
        store.load(log=lambda line: None)
        catalog = Catalog(self.client, poll=.05)
        self.manager = ResearchManager(store, Ledger(base / "admissions.json", limit=60), catalog,
                                       self.client, Broker(), poll=.05, clock=lambda: NOW)
        self.assertTrue(Reconciler(self.manager, retry=.05).reconcile_once())
        self.assertTrue(catalog.refresh())

    def tearDown(self):
        self.manager.close()
        wait_for(lambda: not (self.manager.following or self.manager.fallbacks), 10)
        self.rig.close()
        self.state.cleanup()

    def post(self, **body):
        return self.manager.handle("POST", "/v1/assistant/dtc-research",
                                   {"request_id": uuid.uuid4().hex, **body})["run"]

    def start(self):
        return self.post(action="start", **CODE, provider="claude", model="opus", effort="high")

    def ask(self, run_id, question="And then?"):
        return self.post(action="ask", run_id=run_id, question=question)

    def settle(self, run_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.handle("GET", f"/v1/assistant/dtc-research/run/{run_id}", None)["run"]
            if not run["busy"]:
                return run
            time.sleep(.05)
        raise AssertionError("run did not settle")

    def view(self):
        return self.manager.handle("GET", CREDITS_PATH, None)

    def turn_calls(self):
        """The model turns the fake CLI ran (discovery runs have no ``--model``)."""
        return [call for call in self.rig.calls() if "--model" in call["argv"]]

    def keyed_launches(self):
        return [call for call in self.rig.calls() if "ANTHROPIC_API_KEY" in call["env"]]


    def restart_worker(self, *, credits_enabled):
        rig = self.rig
        rig.policy["claude"]["api_credits"]["enabled"] = credits_enabled
        rig.worker.close()
        rig.worker = Worker(rig.volume, IdentityChecker(rig.cli_root, rig.policy), CliLock(rig.lock_path),
                            meminfo=rig.meminfo, policy=rig.policy, dns=rig.worker.dns, now=lambda: rig.now)
        rig.worker.start()
        rig.settle()


class CreditLegFailureTests(EndToEndBase):
    scenario = BILLING_FAILURE

    def test_a_billing_error_on_the_credit_leg_is_answered_on_the_subscription_and_latches(self):
        self.client.on_subscription = {"mode": "ok", "answer": REPORT}
        run = self.settle(self.start()["run_id"])
        self.assertEqual((run["state"], len(run["turns"]), run["turns"][0]["state"]), ("done", 1, "complete"))
        self.assertEqual(len(self.keyed_launches()), 1)
        self.assertEqual(len(self.turn_calls()), 2)
        self.assertEqual(self.turn_calls()[-1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)
        self.assertNotIn("ANTHROPIC_API_KEY", self.turn_calls()[-1]["env"])
        (entry,) = self.rig.ledger()["entries"]
        self.assertIsNotNone(entry["settled_at"])
        self.assertEqual(self.rig.worker.credits.view()["latches"]["account"], "exhausted")
        new = self.view()["new_research"]
        self.assertEqual((new["billing"], new["reason"]), ("subscription", "unavailable"))
        self.rig.scenario({"mode": "ok", "answer": REPORT})
        self.settle(self.start()["run_id"])
        self.assertEqual(len(self.keyed_launches()), 1)  # the next start never tried credits


class CreditSuccessTests(EndToEndBase):
    def test_a_credit_report_bills_the_key_and_the_view_shows_the_spend_without_the_key(self):
        run = self.settle(self.start()["run_id"])
        self.assertEqual(run["turns"][0]["state"], "complete")
        (call,) = self.turn_calls()
        self.assertEqual(call["env"]["ANTHROPIC_API_KEY"], FAKE_KEY)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", call["env"])
        (entry,) = self.rig.ledger()["entries"]
        self.assertEqual((entry["known_usd"], entry["unknown"]), (0.25, False))
        self.manager.snapshots = FixedSnapshot()
        view = self.view()
        self.assertEqual(view["spent_since_snapshot_usd"], 0.25)
        self.assertEqual(view["balance"]["total_usd"], 4.75)
        self.assertEqual(view["new_research"]["billing"], "api_credits")
        self.assertNotIn(FAKE_KEY, json.dumps(view))
        self.assertNotIn(FAKE_KEY, json.dumps(run))

    def test_a_follow_up_on_an_api_session_after_credits_are_off_is_replayed_on_the_subscription(self):
        run_id = self.start()["run_id"]
        self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        self.rig.policy["claude"]["api_credits"]["enabled"] = False
        self.rig.volume.api_key.unlink()
        self.rig.scenario({"mode": "ok", "answer": "Check the bracket first."})
        self.ask(run_id)
        run = self.settle(run_id)
        self.assertEqual((run["turns"][-1]["state"], run["turns"][-1]["text"]), ("complete", "Check the bracket first."))
        self.assertEqual(len(self.keyed_launches()), 1)
        turns = [b for b in self.client.bodies if "turn_kind" in b]
        self.assertEqual([b["kind"] for b in turns], ["initial", "resume", "initial"])  # the replay is a fresh session
        self.assertEqual(turns[-1]["billing"], "subscription")
        self.assertNotIn("--resume", " ".join(self.turn_calls()[-1]["argv"]))

    def test_an_interrupted_credit_resume_is_not_resumed_again_after_restart(self):
        run_id = self.start()["run_id"]
        self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        self.rig.scenario({"echo_key": True, "answer": REPORT})
        real_run = cli_process.run

        def interrupt_after_reaping(argv, **kwargs):
            result = real_run(argv, **kwargs)
            if "--resume" in argv:
                raise RuntimeError("interrupted before the transcript scan")
            return result

        with mock.patch.object(cli_process, "run", side_effect=interrupt_after_reaping):
            self.ask(run_id)
            self.assertEqual(self.settle(run_id)["turns"][-1]["state"], "failed")
        run_dir = self.rig.volume.run_dir(run_id)
        self.assertIs(json.loads((run_dir / "session.json").read_text())["pending_scan"], True)
        self.assertTrue(any(FAKE_KEY in path.read_text() for path in (run_dir / "home").glob("**/*.jsonl")))
        self.restart_worker(credits_enabled=True)
        self.assertTrue(Reconciler(self.manager, retry=.05).reconcile_once())
        self.assertEqual(self.client.credits()["api_sessions"], 1)
        self.rig.scenario({"mode": "ok", "answer": "Start with the bracket."})
        self.ask(run_id, "What should I check?")
        self.assertEqual(self.settle(run_id)["turns"][-1]["text"], "Start with the bracket.")
        turns = [body["kind"] for body in self.client.bodies if "turn_kind" in body]
        self.assertEqual(turns, ["initial", "resume", "resume", "initial"])
        self.assertEqual(["--resume" in call["argv"] for call in self.turn_calls()], [False, True, False])
        self.assertNotIn(FAKE_KEY, self.rig.prompts()[-1])

    def test_a_clean_marker_write_failure_keeps_the_answer_but_requires_replay(self):
        save = self.rig.volume.save_session_record

        def refuse_clean_marker(run_id, record):
            if not record.get("pending_scan"):
                raise OSError("cannot clear pending marker")
            save(run_id, record)

        with mock.patch.object(self.rig.volume, "save_session_record", side_effect=refuse_clean_marker):
            run_id = self.start()["run_id"]
            self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        self.assertIs(self.rig.volume.session_record(run_id)["pending_scan"], True)
        self.assertEqual(self.client.credits()["api_sessions"], 1)
        self.rig.scenario({"mode": "ok", "answer": "Check the bracket."})
        self.ask(run_id)
        self.assertEqual(self.settle(run_id)["turns"][-1]["state"], "complete")
        self.assertNotIn("--resume", self.turn_calls()[-1]["argv"])

    def test_cancel_during_the_credit_leg_settles_cancelled_and_never_launches_again(self):
        self.rig.scenario({"mode": "hang"})
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: self.turn_calls()))
        self.post(action="cancel", run_id=run_id)
        run = self.settle(run_id)
        self.assertEqual(run["turns"][0]["state"], "cancelled")
        time.sleep(.5)
        self.assertEqual(len(self.turn_calls()), 1)


class UnsafeTranscriptTests(EndToEndBase):
    def test_a_transcript_fifo_does_not_hang_the_scan_and_is_not_verified(self):
        self.rig.scenario({**self.scenario, "fifo_transcript": True})
        with mock.patch("projects.vehicle_data.research_volume.shutil.rmtree"):
            run_id = self.start()["run_id"]
            self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        home = self.rig.volume.run_dir(run_id) / "home"
        self.assertTrue(any(path.is_fifo() for path in home.glob("**/*.jsonl")))
        self.assertEqual(self.client.credits()["api_sessions"], 1)
        self.rig.scenario({"mode": "ok", "answer": "Check the bracket."})
        self.ask(run_id)
        self.assertEqual(self.settle(run_id)["turns"][-1]["state"], "complete")
        self.assertNotIn("--resume", self.turn_calls()[-1]["argv"])

    def test_an_unreadable_sibling_of_a_clean_transcript_is_not_verified(self):
        if os.geteuid() == 0:
            self.skipTest("a root process can list any directory")
        self.rig.scenario({**self.scenario, "unreadable_sibling": True})
        run_id = self.start()["run_id"]
        self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        home = self.rig.volume.run_dir(run_id) / "home"
        try:
            self.assertEqual(self.client.credits()["api_sessions"], 1)
            marker = json.loads((home.parent / "session.json").read_text())
            self.assertIs(marker["poisoned"], True)
        finally:
            for sibling in home.glob(".claude/projects/*/hidden"):
                sibling.chmod(0o700)
        self.rig.scenario({"mode": "ok", "answer": "Check the bracket."})
        self.ask(run_id)
        self.assertEqual(self.settle(run_id)["turns"][-1]["state"], "complete")
        self.assertNotIn("--resume", self.turn_calls()[-1]["argv"])


class MalformedLedgerTests(unittest.TestCase):
    def test_a_parseable_ledger_with_a_deep_unknown_field_allows_subscription_research(self):
        entry = {"attempt_id": "a" * 32, "model": "sonnet", "started_at": "2026-11-20T00:00:00Z",
                 "settled_at": None, "known_usd": 0.1, "unknown": False, "state": "open", "extra": "DEEP"}
        ledger = {"schema_version": 1, "entries": [entry], "latches": {"account": None, "models": {}},
                  "last_fallback": None}
        text = json.dumps(ledger).replace('"DEEP"', "[" * 700 + "0" + "]" * 700)
        json.loads(text)  # parseable, but the unknown nested value must not be copied or saved
        rig = WorkerRig(with_key=True, prepare=lambda rig: rig.volume.credit_ledger.write_text(text))
        try:
            self.assertIs(CreditClient(rig).credits()["ledger_available"], False)
            attempt = rig.worker.admit_turn(rig.turn_body(billing_capable=True))
            outcome = rig.wait(attempt.attempt_id)["outcome"]
            self.assertEqual((outcome["status"], outcome["billing"]), ("complete", "subscription"))
            self.assertEqual(rig.volume.credit_ledger.read_text(), text)
        finally:
            rig.close()


class RollbackSequenceTests(EndToEndBase):
    def admin(self, name):
        return self.manager.handle_admin("GET" if name != "pause" else "POST", ADMIN_ROUTES[name])

    def test_the_documented_sequence_passes_from_a_completed_api_report_with_no_manual_edits(self):
        self.assertEqual(self.settle(self.start()["run_id"])["turns"][0]["state"], "complete")
        self.assertEqual(self.client.credits()["api_sessions"], 1)  # the report's transcript is retained
        self.admin("pause")  # step 1
        drained = self.admin("drained")  # step 2: work is drained although the API session remains
        self.assertEqual((drained["drained"], drained["reasons"]), (True, []))
        blocked = self.admin("rollbackReady")
        self.assertFalse(blocked["rollback_ready"])
        self.assertEqual(set(blocked["reasons"]), {"credits_enabled", "api_sessions_remain"})
        self.restart_worker(credits_enabled=False)  # step 3: disable credits, restart the worker
        self.assertTrue(self.admin("drained")["drained"])
        ready = self.admin("rollbackReady")
        self.assertEqual((ready["rollback_ready"], ready["reasons"]), (True, []))
        self.assertEqual(self.client.credits()["api_sessions"], 0)

    def test_a_stale_subscription_marker_cannot_hide_an_api_transcript_from_rollback(self):
        run_id = self.start()["run_id"]
        self.assertEqual(self.settle(run_id)["turns"][0]["state"], "complete")
        run_dir = self.rig.volume.run_dir(run_id)
        marker = run_dir / "session.json"
        record = json.loads(marker.read_text())
        marker.write_text(json.dumps({**record, "billing": "subscription", "session_id": str(uuid.uuid4())}))
        self.admin("pause")
        self.assertTrue(self.admin("drained")["drained"])
        self.assertEqual(self.client.credits()["api_sessions"], 1)
        with mock.patch("projects.vehicle_data.research_volume.shutil.rmtree"):
            self.restart_worker(credits_enabled=False)
        self.assertEqual(self.admin("rollbackReady")["reasons"], ["api_sessions_remain"])
        self.assertTrue(list((run_dir / "home").glob("**/*.jsonl")))
        self.restart_worker(credits_enabled=False)
        self.assertEqual(self.client.credits()["api_sessions"], 0)
        self.assertTrue(self.admin("rollbackReady")["rollback_ready"])
        self.assertFalse(marker.exists())
        self.assertFalse(list((run_dir / "home").glob("**/*.jsonl")))

    def test_a_report_still_running_blocks_drained_and_then_the_sequence_completes(self):
        self.rig.scenario({"mode": "hang"})
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: self.turn_calls()))
        self.admin("pause")
        self.assertIn("turn_running", self.admin("drained")["reasons"])
        self.post(action="cancel", run_id=run_id)
        self.settle(run_id)
        self.assertTrue(wait_for(lambda: self.admin("drained")["drained"]))


class AdvisorClockTests(EndToEndBase):
    def test_the_view_bills_by_the_advisors_frozen_clock_on_both_sides_of_until(self):
        self.settle(self.start()["run_id"])
        self.manager.snapshots = FixedSnapshot()
        self.assertEqual(self.view()["new_research"]["billing"], "api_credits")
        self.assertEqual(self.view()["balance"]["total_usd"], 4.75)
        self.manager.clock = lambda: UNTIL - timedelta(seconds=1)
        self.assertFalse(self.view()["expired"])
        self.manager.clock = lambda: UNTIL
        expired = self.view()
        self.assertEqual((expired["expired"], expired["new_research"]["reason"]), (True, "expired"))
        self.assertEqual(expired["balance"]["grants"], [])


class OldWorkerTests(EndToEndBase):
    capable = False

    def test_a_worker_without_billing_capable_never_uses_credits(self):
        run = self.settle(self.start()["run_id"])
        self.assertEqual(run["turns"][0]["state"], "complete")
        self.assertNotIn("billing_capable", self.client.bodies[0])
        self.assertEqual(self.keyed_launches(), [])
        self.assertEqual(self.turn_calls()[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], OAUTH)


if __name__ == "__main__":
    unittest.main()
