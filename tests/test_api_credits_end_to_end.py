"""The advisor's real manager over the real worker (fake Claude CLI): the credit protocol end to end."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from projects.vehicle_data.api_credits_contract import FALLBACK, GRANTS
from projects.vehicle_data.api_credits_snapshot import GrantBalance, Snapshot
from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_reconcile import Reconciler
from projects.vehicle_data.dtc_research_store import Store
from tests.research_support import FAKE_KEY, WorkerRig
from tests.test_dtc_research_fallback import FAST, wait_for
from tests.test_dtc_research_manager import Broker, CODE, REPORT, RigClient
from tests.test_research_credits import OAUTH, assistant, result

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
                                       self.client, Broker(), poll=.05)
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

    def test_cancel_during_the_credit_leg_settles_cancelled_and_never_launches_again(self):
        self.rig.scenario({"mode": "hang"})
        run_id = self.start()["run_id"]
        self.assertTrue(wait_for(lambda: self.turn_calls()))
        self.post(action="cancel", run_id=run_id)
        run = self.settle(run_id)
        self.assertEqual(run["turns"][0]["state"], "cancelled")
        time.sleep(.5)
        self.assertEqual(len(self.turn_calls()), 1)


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
