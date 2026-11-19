"""The advisor's rollback controls: pause new research, and say when everything in flight has settled.

``python3 -m projects.vehicle_data.dtc_research_admin pause|resume|drained`` reaches these through
admin routes on the advisor's own Unix socket (never relayed by a web listener). The procedure is
docs/dtc-research.md "Promotional API credits"; the pause is a file so a restarted advisor stays
paused until a person removes it.

``drained`` runs under the manager lock and answers true only when nothing can still produce a
credit outcome that an old advisor would not understand: no start gathering context, no busy or
reconciling turn, no pending fallback, no running worker attempt, and no held turn outcome whose
turn is not already settled. It does not look at API-created sessions: a completed API report
keeps its transcript until the worker restarts with credits off, which comes after ``drained``.
``rollback_ready`` is ``drained`` plus the worker-side state the old code needs: credits switched
off, no API-created session left, and the worker's ledger state reported. Held outcomes of settled turns (a lost acknowledgment) are
acknowledged here. A held ``credit_fallback`` keeps the answer "not drained" until the turn is
settled; it is never discarded to pass. Maintenance and discovery outcomes have no turn, so they
are never asked about and never block.
"""

import os
from pathlib import Path

from projects.vehicle_data.api_credits_contract import ADMIN_ROUTES
from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data.dtc_research_contract import ResearchError
from projects.vehicle_data import dtc_research_store as records
from projects.vehicle_data.research_worker_client import WorkerUnreachable

PAUSED_MESSAGE = "Research is paused for an upgrade or rollback; try again shortly"


class PauseFile:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.paused = bool(self.path and self.path.exists())

    def set(self):
        """Pause now; ``OSError`` when the file could not be written (still paused until restart)."""
        self.paused = True
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._sync_directory()  # the new name must survive a power loss, or a restart would resume

    def clear(self):
        """Resume; the pause stays when the file cannot be removed."""
        if self.path:
            self.path.unlink(missing_ok=True)
            self._sync_directory()
        self.paused = False

    def _sync_directory(self):
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class PauseMixin:
    """Admin routes and the pause check for ``ResearchManager``."""

    def _require_unpaused(self):
        if self.pause.paused:
            raise ResearchError("provider_unavailable", PAUSED_MESSAGE)

    def handle_admin(self, method, path):
        if method == "POST" and path == ADMIN_ROUTES["pause"]:
            return self.set_paused(True)
        if method == "POST" and path == ADMIN_ROUTES["resume"]:
            return self.set_paused(False)
        if method == "GET" and path == ADMIN_ROUTES["drained"]:
            return self.drained()
        if method == "GET" and path == ADMIN_ROUTES["rollbackReady"]:
            return self.rollback_ready()
        raise ChatError(404, "Unknown research admin request")

    def set_paused(self, paused):
        with self.lock:
            try:
                self.pause.set() if paused else self.pause.clear()
            except OSError as exc:
                raise ChatError(503, f"The pause file could not be updated: {exc.strerror}") from None
            return {"paused": self.pause.paused}

    def drained(self):
        """``{"drained": bool, "paused": bool, "reasons": [...]}``; see the module doc."""
        with self.lock:
            reasons = self._drain_blockers()
            return {"drained": not reasons, "paused": self.pause.paused, "reasons": reasons}

    def rollback_ready(self):
        """``drained`` plus credits off and no API-created session on the worker."""
        with self.lock:
            reasons = self._drain_blockers(credit_state=True)
            return {"rollback_ready": not reasons, "paused": self.pause.paused, "reasons": reasons}

    def _drain_blockers(self, credit_state=False):
        reasons = []
        if not self.pause.paused:
            reasons.append("not_paused")
        if self.state != "idle":
            reasons.append("reconciling")
        if self.starting:
            reasons.append("start_in_progress")
        if any(records.busy(record) for record in self.store.records.values()):
            reasons.append("turn_running")
        if any((turn.get("pending_fallback")) for record in self.store.records.values() for turn in record["turns"]):
            reasons.append("pending_fallback")
        try:
            instance = self.worker.status()
            if instance.get("job"):
                reasons.append("worker_job")
            reasons += self._settle_held_outcomes()
            if credit_state:
                reasons += self._credit_state_blockers(instance)
            final = self.worker.status()
            if final.get("instance_id") != instance.get("instance_id"):
                reasons.append("worker_restarted")  # a new instance: reconcile, then ask again
                self.state = "unknown"
                self.wake_reconciler()
            elif final.get("job") and "worker_job" not in reasons:
                reasons.append("worker_job")  # something started while the outcomes were being settled
        except WorkerUnreachable:
            reasons.append("worker_unreachable")
        return reasons

    def _credit_state_blockers(self, status):
        """What an old worker must not meet: credits still on, an API-created session (it looks for the
        transcript, not the billing marker), or a worker that does not report its ledger state. A
        worker that does not advertise billing has none of these by construction."""
        if not status.get("billing_capable"):
            return []
        credits = self.worker.credits()
        reasons = []
        if credits.get("enabled") is not False:
            reasons.append("credits_enabled")
        count = credits.get("api_sessions")
        if not isinstance(count, int) or isinstance(count, bool):
            reasons.append("api_sessions_unknown")
        elif count:
            reasons.append("api_sessions_remain")
        if "ledger_available" not in credits:
            reasons.append("ledger_state_unknown")
        return reasons

    def _settle_held_outcomes(self):
        """Acknowledge the held outcome of every attempt whose turn already has its answer,
        cancellation or successor; name the ones that block."""
        candidates = [(attempt_id, turn, index == len(turn["attempt_ids"]) - 1)
                      for record in self.store.records.values() for turn in record["turns"]
                      for index, attempt_id in enumerate(turn["attempt_ids"])]
        reasons = []
        for attempt_id, turn, is_last in candidates:
            if self.worker.attempt(attempt_id).get("outcome_state") != "held":
                continue
            if turn["state"] in records.BUSY_STATES and is_last:
                reasons.append("held_outcome_unsettled")
                continue
            try:
                self.worker.acknowledge(attempt_id)  # the turn is settled or has a successor
            except WorkerUnreachable:
                reasons.append("acknowledgment_failed")
        return reasons
