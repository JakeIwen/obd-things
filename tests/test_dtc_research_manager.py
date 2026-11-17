"""The advisor's research routes over a real research worker (fake Claude CLI), in process."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data.assistant_models import Catalog
from projects.vehicle_data import dtc_research_store as records
from projects.vehicle_data.dtc_research import ResearchManager
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_reconcile import Reconciler
from projects.vehicle_data.dtc_research_contract import MAX_RUNS
from projects.vehicle_data.dtc_research_store import Store
from projects.vehicle_data.dtc_research_worker import dispatch
from projects.vehicle_data.research_attempts import Refused
from projects.vehicle_data.research_worker_client import WorkerUnreachable
from tests.research_support import WorkerRig

CODE = {"module_key": "radar_acc", "fca_display": "C1418-78"}
REPORT = "SUMMARY: The radar saw a misalignment and wants a service alignment.\n## What it means\nx\n" \
         "## Likely causes on this van\nx\n## What to check\nx\n## Sources\n- [a](https://example.com)"


class Broker:
    def request(self, method, path):
        if path == "/v1/diagnostics/dtcs":
            return 200, {"groups": {"current": [{**CODE, "status": "08", "first_seen_at": "2026-10-08T00:00:00Z",
                                                  "module_name": "Radar", "unlisted": "dropped"}]}}
        if path == "/v1/diagnostics/dtc-ignores":
            return 200, {"available": True, "ignored": []}
        return 404, {}


class RigClient:
    """``WorkerClient`` over an in-process ``Worker``, with switches for lost replies."""

    def __init__(self, rig):
        self.rig, self.down, self.lose_reply, self.drop_request = rig, False, False, False
        self.hold_request = False  # the launch request is delayed in transit; ``deliver_held`` sends it
        self.launches, self.deleted, self.held = [], [], []
        self.garbled_replies = 0  # attempt replies that break the follow thread (an unforeseen bug)

    def _call(self, method, path, payload=None):
        if self.down:
            raise WorkerUnreachable("connection refused", sent=False)
        try:
            return dispatch(self.rig.worker, method, path, lambda: payload or {})
        except Refused as refused:
            return 409, {"reason": refused.reason, "detail": refused.detail}

    def status(self):
        return self._call("GET", "/status")[1]

    def launch(self, route, body):
        self.launches.append(body["attempt_id"])
        if self.drop_request:
            raise WorkerUnreachable("lost before arrival")
        if self.hold_request:
            self.held.append((route, body))
            raise WorkerUnreachable("no reply yet; the request is still in transit")
        reply = self._call("POST", route, body)
        if self.lose_reply:
            raise WorkerUnreachable("lost after arrival")
        return reply

    def attempt(self, attempt_id):
        if self.garbled_replies:
            self.garbled_replies -= 1
            return {"known": True, "outcome_state": "held"}  # no outcome: the follow thread raises
        return self._call("GET", f"/attempts/{attempt_id}")[1]

    def acknowledge(self, attempt_id):
        self._call("POST", f"/attempts/{attempt_id}/ack")

    def cancel(self, attempt_id):
        self._call("POST", f"/attempts/{attempt_id}/cancel")

    def fence(self, attempt_id):
        status, body = self._call("POST", f"/attempts/{attempt_id}/fence")
        if status != 200:
            raise WorkerUnreachable(f"fence answered {status}")
        return body

    def deliver_held(self):
        """The delayed launch requests finally reach the worker: ``[(status, reply), ...]``."""
        held, self.held = self.held, []
        return [self._call("POST", route, body) for route, body in held]

    def sweep(self, keep):
        return self._call("POST", "/sweep", {"keep": sorted(keep)})

    def delete_run(self, run_id):
        self.deleted.append(run_id)
        return self._call("POST", f"/runs/{run_id}/delete")


class GatedBroker(Broker):
    """Every DTC read waits at a two-party barrier (so two starts are both past their first check),
    and the second read also waits for ``release``."""

    def __init__(self):
        self.barrier, self.release, self.reads = threading.Barrier(2), threading.Event(), 0

    def request(self, method, path):
        if path == "/v1/diagnostics/dtcs":
            self.reads += 1
            second = self.reads == 2
            try:
                self.barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
            if second:
                self.release.wait(timeout=20)
        return super().request(method, path)


class FlakyStore(Store):
    """A store whose writes fail while ``failing`` is set (a full or read-only disk)."""

    failing = False

    def save(self, record):
        if self.failing:
            raise OSError(28, "No space left on device")
        super().save(record)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.rig = WorkerRig({"mode": "ok", "answer": REPORT})
        self.state = tempfile.TemporaryDirectory()
        self.client = RigClient(self.rig)
        self.manager = self.build()

    def build(self, ledger_limit=60, refresh=True, broker=None, store_class=Store):
        base = Path(self.state.name)
        store = store_class(base / "runs")
        store.load(log=lambda line: None)
        catalog = Catalog(self.client, poll=.05)
        manager = ResearchManager(store, Ledger(base / "admissions.json", limit=ledger_limit), catalog,
                                  self.client, broker or Broker(), poll=.05)
        reconciler = Reconciler(manager, retry=.05)
        self.assertTrue(reconciler.reconcile_once())
        if refresh:
            self.assertTrue(catalog.refresh())
        else:
            manager.catalog = self.manager.catalog  # the worker is busy with the turn: keep the old list
        return manager

    def tearDown(self):
        self.manager.close()
        deadline = time.monotonic() + 10
        while self.manager.following and time.monotonic() < deadline:
            time.sleep(.05)
        self.rig.close()
        self.state.cleanup()

    def start(self, **extra):
        body = {"action": "start", **CODE, "provider": "claude", "model": "opus", "effort": "high",
                "request_id": uuid.uuid4().hex, **extra}
        return self.manager.handle("POST", "/v1/assistant/dtc-research", body)["run"]

    def ask(self, run_id, question="And then?"):
        body = {"action": "ask", "run_id": run_id, "question": question, "request_id": uuid.uuid4().hex}
        return self.manager.handle("POST", "/v1/assistant/dtc-research", body)["run"]

    def seed_finished_runs(self, count):
        """``count`` finished runs on disk, oldest first, as earlier research would have left them."""
        store = Store(Path(self.state.name) / "runs")
        ids = []
        for n in range(count):
            fields = {**CODE, "fca_display": f"C{1000 + n:04X}-00", "provider": "claude", "model": "opus",
                      "effort": "high"}
            record = records.new_record(uuid.uuid4().hex, fields, {"code": {}}, uuid.uuid4().hex)
            record["created_at"] = f"2026-11-01T00:{n // 60:02d}:{n % 60:02d}+00:00"
            turn = records.new_turn("report")
            turn.update(state="complete", answer="ok", phase=None, completed_at=record["created_at"], duration_s=1)
            record["turns"].append(turn)
            store.save(record)
            ids.append(record["run_id"])
        return ids

    def settle(self, run_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.manager.handle("GET", f"/v1/assistant/dtc-research/run/{run_id}", None)["run"]
            if not run["busy"]:
                return run
            time.sleep(.05)
        raise AssertionError("run did not settle")

    def test_report_then_resumed_follow_up_and_the_read_routes(self):
        models = self.manager.handle("GET", "/v1/assistant/models", None)
        claude = next(g for g in models["groups"] if g["provider"] == "claude")
        self.assertTrue(claude["available"])
        self.assertEqual(claude["source"], "discovered")
        self.assertEqual([m["id"] for m in claude["models"]], ["opus"])  # "default" is never offered
        self.assertEqual(models["default"], {"provider": "claude", "model": "opus", "effort": "high"})
        run = self.start()
        self.assertTrue(run["busy"])
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["summary_note"], run["format_warnings"]), ("done", None, []))
        self.assertTrue(run["summary"].startswith("The radar saw"))
        self.assertNotIn("SUMMARY", run["turns"][0]["text"])
        self.assertEqual(run["continue_mode"], "resume")
        self.ask(run["run_id"])
        run = self.settle(run["run_id"])
        self.assertEqual([t["role"] for t in run["turns"]], ["assistant", "user", "assistant"])
        self.assertEqual(run["turns"][-1]["mode"], "resumed")
        self.assertEqual(run["turn_count"], 3)
        code = self.manager.handle("GET", "/v1/assistant/dtc-research/code/radar_acc/C1418-78", None)
        self.assertEqual(code["selected"]["run_id"], run["run_id"])
        items = self.manager.handle("GET", "/v1/assistant/dtc-research", None)["items"]
        self.assertEqual((items[0]["runs"], items[0]["summary"]), (1, run["summary"]))
        progress = self.manager.handle("GET", f"/v1/assistant/dtc-research/run/{run['run_id']}/progress", None)
        self.assertEqual(progress["progress"]["turn_count"], 3)
        self.assertEqual(len(self.client.launches), len(set(self.client.launches)))  # send-once

    def test_context_comes_from_the_pi_not_the_browser(self):
        run = self.settle(self.start()["run_id"])
        record = self.manager.store.records[run["run_id"]]
        self.assertEqual(record["context"]["record_source"], "pi_scan")
        self.assertNotIn("unlisted", record["context"]["record"])
        prompt = self.rig.calls()[-1]
        self.assertIn("--session-id", prompt["argv"])

    def test_replay_when_the_session_is_gone(self):
        run = self.settle(self.start()["run_id"])
        self.rig.volume.drop_session(run["run_id"])
        self.ask(run["run_id"])
        run = self.settle(run["run_id"])
        self.assertEqual(run["turns"][-1]["mode"], "replayed")
        self.assertEqual(run["turns"][-1]["state"], "complete")

    def test_refusals(self):
        run = self.settle(self.start()["run_id"])
        errors = []
        for action in (lambda: self.ask("0" * 32), lambda: self.manager.handle("GET", "/v1/assistant/x", None)):
            with self.assertRaises(ChatError) as caught:
                action()
            errors.append(caught.exception.reason)
        self.assertEqual(errors, ["not_found", "not_found"])
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": 2})
        busy = self.start(module_key="bcm_ccan", fca_display="B1632-15")
        with self.assertRaises(ChatError) as running:
            self.start(module_key="bcm_ccan", fca_display="B1632-15")
        self.assertEqual(running.exception.reason, "run_busy")
        with self.assertRaises(ChatError) as worker_busy:
            self.ask(run["run_id"])
        self.assertEqual(worker_busy.exception.reason, "busy")
        self.settle(busy["run_id"])
        self.rig.scenario({"mode": "exit_error"})
        failed = self.settle(self.start(fca_display="C1420-25")["run_id"])
        self.assertEqual((failed["state"], failed["reason"]), ("failed", "provider_error"))
        with self.assertRaises(ChatError) as cannot:
            self.ask(failed["run_id"])
        self.assertEqual(cannot.exception.reason, "cannot_continue")

    def test_daily_limit_is_durable_and_refusals_do_not_count(self):
        self.manager.close()
        self.manager = self.build(ledger_limit=2)
        self.rig.set_available(1024)  # every launch is refused for low memory
        for _ in range(5):
            with self.assertRaises(ChatError) as low:
                self.start()
            self.assertEqual(low.exception.reason, "busy")
        self.assertEqual(self.manager.ledger.count(), 0)
        self.rig.set_available(4 * 1024 ** 3)
        self.settle(self.start()["run_id"])
        self.manager.close()
        self.manager = self.build(ledger_limit=2)  # an advisor restart keeps the count
        self.settle(self.start(fca_display="C1420-25")["run_id"])
        with self.assertRaises(ChatError) as limit:
            self.start(fca_display="C1422-49")
        self.assertEqual(limit.exception.reason, "daily_limit")

    def test_lost_launch_reply_is_adopted_and_a_lost_request_is_interrupted(self):
        self.client.lose_reply = True
        run = self.start()
        self.client.lose_reply = False
        self.assertEqual(self.settle(run["run_id"])["state"], "done")
        self.client.drop_request = True
        run = self.start(fca_display="C1420-25")
        self.client.drop_request = False
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["reason"]), ("failed", "interrupted"))
        self.assertEqual(len(self.client.launches), len(set(self.client.launches)))
        turns = [c for c in self.rig.calls() if "--session-id" in c["argv"] or "--resume" in c["argv"]]
        self.assertEqual(len(turns), 1)  # the dropped request was never re-sent

    def test_advisor_restart_during_a_report_adopts_it(self):
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": 1.5})
        run = self.start()
        self.manager.closed.set()  # the old advisor stops following
        second = self.build(refresh=False)
        self.manager = second
        record = second.store.records[run["run_id"]]
        self.assertIn(record["turns"][0]["state"], ("running", "reconciling"))
        self.assertEqual(self.settle(run["run_id"])["state"], "done")
        turns = [c for c in self.rig.calls() if "--session-id" in c["argv"] or "--resume" in c["argv"]]
        self.assertEqual(len(turns), 1)  # adopted, not relaunched

    def test_advisor_restart_during_discovery_cancels_it_and_waits(self):
        self.rig.scenario({"mode": "ok", "discovery_delay": 3})
        attempt = uuid.uuid4().hex
        self.rig.worker.admit_discovery({"attempt_id": attempt, "provider": "claude",
                                         "expected_instance_id": self.rig.worker.table.instance_id})
        manager = ResearchManager(Store(Path(self.state.name) / "runs"), self.manager.ledger, self.manager.catalog,
                                  self.client, Broker(), poll=.05)
        reconciler = Reconciler(manager, retry=.05)
        self.assertFalse(reconciler.reconcile_once())
        with self.assertRaises(ChatError) as settling:
            manager.handle("POST", "/v1/assistant/dtc-research", {"action": "start", **CODE, "provider": "claude",
                                                                   "model": "opus", "effort": "high",
                                                                   "request_id": uuid.uuid4().hex})
        self.assertEqual(settling.exception.reason, "busy")
        deadline = time.monotonic() + 15
        while not reconciler.reconcile_once() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertEqual(manager.state, "idle")

    def test_unresponsive_worker_never_frees_research_and_a_new_instance_interrupts(self):
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": 1})
        run = self.start()
        self.client.down = True
        time.sleep(.5)
        record = self.manager.store.records[run["run_id"]]
        self.assertEqual(record["turns"][0]["state"], "running")
        with self.assertRaises(ChatError) as busy:
            self.start(fca_display="C1420-25")
        self.assertIn(busy.exception.reason, ("busy", "run_busy", "worker_unavailable"))
        while self.rig.worker.table.current is not None:
            time.sleep(.05)
        self.rig.worker.table.__init__()  # the worker restarted: a new instance with no memory
        self.client.down = False
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["reason"]), ("failed", "interrupted"))


    def test_follow_up_questions_never_leave_the_pi_with_a_vin(self):
        vin = "1ABCDEFGH2JKLMN34"  # synthetic, VIN-shaped
        run = self.settle(self.start()["run_id"])
        self.rig.scenario({"mode": "ok", "answer": f"Recalls for {vin} are listed by the dealer."})
        asked = f"My VIN is {vin}; which recalls apply?"
        self.ask(run["run_id"], asked)
        self.assertEqual(self.settle(run["run_id"])["turns"][-1]["mode"], "resumed")
        self.rig.volume.drop_session(run["run_id"])
        self.ask(run["run_id"], f"And is {vin} covered by the radar TSB?")
        run = self.settle(run["run_id"])
        self.assertEqual(run["turns"][-1]["mode"], "replayed")
        prompts = self.rig.prompts()
        self.assertEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertNotIn(vin, prompt)
        self.assertIn("[vehicle identifier omitted]", prompts[1])  # the resumed question
        self.assertIn("OWNER ASKED\nMy VIN is [vehicle identifier omitted]", prompts[2])  # replayed question
        self.assertIn("Recalls for [vehicle identifier omitted]", prompts[2])  # replayed answer
        # On the Pi the owner's text is kept as he typed it; only what leaves the Pi is masked.
        self.assertEqual(run["turns"][1]["text"], asked)

    def test_a_refused_start_at_the_run_cap_keeps_every_record(self):
        self.manager.close()
        oldest = self.seed_finished_runs(MAX_RUNS)[0]
        self.manager = self.build(ledger_limit=0)
        with self.assertRaises(ChatError) as limit:
            self.start()
        self.assertEqual(limit.exception.reason, "daily_limit")
        self.manager.close()
        self.manager = self.build()
        self.rig.set_available(1024)
        with self.assertRaises(ChatError) as low:
            self.start()
        self.assertEqual(low.exception.reason, "busy")
        self.client.down = True
        with self.assertRaises(ChatError) as down:
            self.start()
        self.assertEqual(down.exception.reason, "worker_unavailable")
        self.client.down = False
        self.assertEqual((len(self.manager.store.records), self.client.deleted), (MAX_RUNS, []))
        self.assertEqual(len(list(Path(self.state.name, "runs").glob("*.json"))), MAX_RUNS)
        self.rig.set_available(4 * 1024 ** 3)
        run = self.settle(self.start()["run_id"])  # admitted: only now does the oldest make room
        self.assertEqual(self.client.deleted, [oldest])
        self.assertEqual(len(self.manager.store.records), MAX_RUNS)
        self.assertIn(run["run_id"], self.manager.store.records)

    def test_concurrent_starts_with_one_request_id_launch_once_and_keep_the_cap(self):
        self.manager.close()
        self.seed_finished_runs(MAX_RUNS)
        broker = GatedBroker()
        self.manager = self.build(broker=broker)
        request_id, results = uuid.uuid4().hex, []

        def start():
            try:
                results.append(self.start(request_id=request_id)["run_id"])
            except ChatError as exc:
                results.append(exc.reason)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 20
        while not any(len(r) == 32 for r in results) and time.monotonic() < deadline:
            time.sleep(.05)
        self.settle(next(r for r in results if len(r) == 32))
        broker.release.set()  # a second start still gathering now meets a finished run for this code
        for thread in threads:
            thread.join(20)
        self.assertEqual(len(self.rig.prompts()), 1, results)  # one model turn
        mine = [r for r in self.manager.store.records.values() if request_id in r["request_ids"]]
        self.assertEqual(len(mine), 1)
        self.assertLessEqual(len(self.manager.store.records), MAX_RUNS)
        self.assertTrue(all(r == mine[0]["run_id"] or r == "run_busy" for r in results), results)

    def test_a_failed_record_write_keeps_following_and_saves_before_acknowledging(self):
        self.manager.close()
        self.manager = self.build(store_class=FlakyStore)
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": 1})
        run = self.start()
        attempt = self.manager.store.records[run["run_id"]]["turns"][0]["attempt_ids"][0]
        self.manager.store.failing = True  # the session id and the outcome cannot be written
        self.rig.wait(attempt)
        time.sleep(.5)
        self.assertEqual(self.rig.worker.table.get(attempt)["outcome_state"], "held")  # not acknowledged
        self.assertIn(attempt, self.manager.following)
        path = f"/v1/assistant/dtc-research/run/{run['run_id']}"
        self.assertTrue(self.manager.handle("GET", path, None)["run"]["busy"])
        self.manager.store.failing = False
        self.assertEqual(self.settle(run["run_id"])["state"], "done")
        deadline = time.monotonic() + 5
        while self.rig.worker.table.get(attempt)["outcome_state"] != "acknowledged" and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.rig.worker.table.get(attempt)["outcome_state"], "acknowledged")
        saved = Store(Path(self.state.name) / "runs")
        saved.load(log=lambda line: None)
        self.assertEqual(saved.records[run["run_id"]]["turns"][0]["state"], "complete")
        self.assertEqual(saved.records[run["run_id"]]["session"]["state"], "present")

    def test_a_failed_follow_thread_hands_its_turn_back_to_reconciliation(self):
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": 1})
        self.client.garbled_replies = 1
        run = self.start()
        deadline = time.monotonic() + 5
        while self.manager.state != "unknown" and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.manager.state, "unknown")  # research admission closes until it is settled
        self.assertEqual(self.manager.store.records[run["run_id"]]["turns"][0]["state"], "reconciling")
        self.assertTrue(Reconciler(self.manager, retry=.05).reconcile_once())  # adopted or settled, never relaunched
        self.assertEqual(self.manager.state, "idle")
        self.assertEqual(self.settle(run["run_id"])["state"], "done")
        self.assertEqual(len(self.rig.prompts()), 1)

    def test_reconciliation_does_not_acknowledge_an_outcome_it_could_not_save(self):
        self.rig.scenario({"mode": "ok", "answer": REPORT, "delay": .5})
        run = self.start()
        attempt = self.manager.store.records[run["run_id"]]["turns"][0]["attempt_ids"][0]
        self.manager.closed.set()  # the advisor stops before the answer arrives
        self.rig.wait(attempt)
        store = FlakyStore(Path(self.state.name) / "runs")
        store.load(log=lambda line: None)
        store.failing = True
        restarted = ResearchManager(store, self.manager.ledger, self.manager.catalog, self.client, Broker(), poll=.05)
        reconciler = Reconciler(restarted, retry=.05)
        self.assertFalse(reconciler.reconcile_once())
        self.assertEqual(self.rig.worker.table.get(attempt)["outcome_state"], "held")
        self.assertEqual(restarted.state, "unknown")
        store.failing = False
        self.assertTrue(reconciler.reconcile_once())
        self.assertEqual(self.rig.worker.table.get(attempt)["outcome_state"], "acknowledged")
        self.assertEqual(store.records[run["run_id"]]["turns"][0]["state"], "complete")
        self.manager = restarted

    def test_an_unknown_attempt_is_fenced_before_it_is_called_interrupted(self):
        self.client.hold_request = True  # the POST is still in transit when the advisor asks about it
        run = self.start()
        self.client.hold_request = False
        run = self.settle(run["run_id"])
        self.assertEqual((run["state"], run["reason"]), ("failed", "interrupted"))
        [(status, reply)] = self.client.deliver_held()  # the original request finally arrives
        self.assertEqual((status, reply["reason"]), (409, "duplicate_attempt"))
        self.assertEqual(self.rig.prompts(), [])  # nothing was launched behind the dashboard's back

if __name__ == "__main__":
    unittest.main()
