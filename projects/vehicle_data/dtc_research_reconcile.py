"""Settling what the research worker holds after an advisor restart or a worker restart.

Research admission stays closed (``state == "unknown"``) until the worker has answered: a turn it
still runs is adopted by its ``attempt_id``; any other job is cancelled (an installer's maintenance
reservation is waited for); every turn left ``reconciling`` is asked about once, and an attempt the
worker does not know (and has fenced against a late launch), or whose outcome it dropped, becomes
``interrupted``. Nothing is ever
relaunched here, and nothing is inferred from a timeout: an unreachable worker keeps the state
``unknown`` and research answers ``worker_unavailable``. An outcome is acknowledged only after its
record was saved; a failed write keeps the state ``unknown`` and the pass is retried.
"""

import json
import sys
import threading

from projects.vehicle_data.research_worker_client import WorkerUnreachable

IDLE_CHECK_SECONDS = 30
RETRY_SECONDS = 1.0


class Reconciler:
    def __init__(self, manager, retry=RETRY_SECONDS):
        self.manager, self.retry = manager, retry
        self.wake = threading.Event()
        manager.wake_reconciler = self.wake.set

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        manager = self.manager
        while not manager.closed.is_set():
            try:
                if manager.state == "idle":
                    self.wake.wait(IDLE_CHECK_SECONDS)
                    self.wake.clear()
                    self._notice_restart()
                    continue
                settled = self.reconcile_once()
            except Exception as exc:  # the only thread that can reopen admission must not die
                print(json.dumps({"event": "dtc_research_reconcile_failed", "error": repr(exc)[:300]}),
                      file=sys.stderr, flush=True)
                settled = False
            if not settled:
                manager.closed.wait(self.retry)

    def _notice_restart(self):
        manager = self.manager
        try:
            status = manager.worker.status()
        except WorkerUnreachable:
            return
        with manager.lock:
            if status["instance_id"] != manager.instance_id:
                manager.state = "unknown"
            manager.store.retry_deletes()

    def reconcile_once(self):
        """One pass; ``True`` once admission can reopen."""
        manager = self.manager
        try:
            status = manager.worker.status()
        except WorkerUnreachable:
            return False
        with manager.lock:
            manager.instance_id = status["instance_id"]
            pending = {turn["attempt_ids"][-1]: record["run_id"]
                       for record in manager.store.records.values() if record["turns"]
                       for turn in record["turns"][-1:] if turn["state"] == "reconciling" and turn["attempt_ids"]}
            for record in manager.store.records.values():  # reconciling without any attempt: never launched
                last = record["turns"][-1] if record["turns"] else None
                if last and last["state"] == "reconciling" and not last["attempt_ids"] \
                        and not manager._settle(record, {"status": "failed", "reason": "interrupted",
                                                         "detail": "Interrupted by a restart before it started"}):
                    return False
            job = status.get("job")
            if job and job.get("attempt_id") in pending and job.get("kind") == "turn":
                run_id = pending.pop(job["attempt_id"])
                manager.store.records[run_id]["turns"][-1]["state"] = "running"
                manager._follow(run_id, job["attempt_id"])
            elif job and job.get("attempt_id") not in manager.following:
                if job.get("kind") != "maintenance":
                    try:
                        manager.worker.cancel(job["attempt_id"])
                    except WorkerUnreachable:
                        pass
                return False
        for attempt_id, run_id in pending.items():
            try:
                state = manager.fence_unknown(attempt_id, manager.worker.attempt(attempt_id), manager.instance_id)
            except WorkerUnreachable:
                return False
            if state.get("known") and state.get("outcome_state") == "held":
                if not manager._finish(run_id, attempt_id, state["outcome"]):
                    return False  # not saved: keep it on the worker and try again
                try:
                    manager.worker.acknowledge(attempt_id)
                except WorkerUnreachable:
                    pass
            elif state.get("known") and state.get("state") != "finished":
                return False  # the worker took it on after all; the next pass adopts it
            elif not manager._finish(run_id, attempt_id, {"status": "failed", "reason": "interrupted",
                                                          "detail": "Interrupted by a restart of the research service"}):
                return False
        with manager.lock:
            manager.trim()  # an eviction that failed earlier, or more records than the cap after a restart
            try:
                manager.worker.sweep(set(manager.store.records))
            except WorkerUnreachable:
                return False
            manager.state = "idle"
        return True

