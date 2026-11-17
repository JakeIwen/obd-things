"""The research worker's admission slot and its memory of attempts. In-process, so atomic.

One attempt at a time. An attempt is admitted under one lock: the caller's expected worker instance
must match, its ``attempt_id`` must be new to this instance, and the slot must be free; then the
attempt holds the slot until its process group is reaped. Every admitted attempt leaves a
tombstone for this instance's lifetime (bounded by count), so a repeated id is always recognised
and never launched twice. Finished outcomes are kept until the advisor acknowledges them.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time
import uuid

from projects.vehicle_data.dtc_research_contract import WORKER

TOMBSTONES_KEPT = WORKER["tombstonesKept"]
OUTCOMES_KEPT = WORKER["outcomesKept"]
KINDS = ("turn", "discovery", "maintenance")


class Refused(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


@dataclass
class Attempt:
    attempt_id: str
    kind: str
    run_id: str | None = None
    state: str = "accepted"  # accepted -> running -> finished
    phase: str | None = "starting"
    searches: int = 0
    pages: int = 0
    session_id: str | None = None
    accepted_at: float = field(default_factory=time.time)
    outcome: dict | None = None
    outcome_state: str = "pending"  # pending | held | acknowledged | dropped
    cancelled: threading.Event = field(default_factory=threading.Event)
    detail: dict = field(default_factory=dict)

    def public(self, instance_id):
        body = {"instance_id": instance_id, "known": True, "attempt_id": self.attempt_id, "kind": self.kind,
                "run_id": self.run_id, "state": self.state, "phase": self.phase,
                "activity": {"searches": self.searches, "pages": self.pages}, "session_id": self.session_id,
                "outcome_state": self.outcome_state}
        if self.outcome_state == "held":
            body["outcome"] = self.outcome
        return body


class AttemptTable:
    def __init__(self, instance_id=None):
        self.instance_id = instance_id or uuid.uuid4().hex
        self.lock = threading.RLock()
        self.current = None
        self.tombstones = OrderedDict()
        self.maintenance_deadline = None

    def admit(self, attempt_id, kind, expected_instance, run_id=None, check=None):
        """Take the slot for a new attempt, or raise ``Refused``. ``check()`` runs inside the lock
        (identity, memory, CLI lock) and may raise ``Refused`` itself."""
        with self.lock:
            if expected_instance != self.instance_id:
                raise Refused("stale_instance", "the research worker restarted; ask about the attempt instead")
            if attempt_id in self.tombstones:
                raise Refused("duplicate_attempt", "this attempt was already sent once")
            if self.current is not None:
                kind_held = self.current.kind
                raise Refused("busy", "the model check is running" if kind_held == "discovery" else
                              "research maintenance is running" if kind_held == "maintenance" else
                              "another research job is running")
            if len(self.tombstones) >= TOMBSTONES_KEPT and not self._evict_one():
                raise Refused("busy", "the research worker's attempt table is full of unacknowledged results")
            if check:
                check()
            attempt = Attempt(attempt_id, kind, run_id)
            self.tombstones[attempt_id] = attempt
            self.current = attempt
            return attempt

    def _evict_one(self):
        for key, attempt in self.tombstones.items():
            if attempt.outcome_state in ("acknowledged", "dropped"):
                del self.tombstones[key]
                return True
        return False

    def finish(self, attempt, outcome):
        """Record the outcome (the process group is already reaped) and free the slot."""
        with self.lock:
            attempt.state, attempt.phase = "finished", None
            attempt.outcome, attempt.outcome_state = outcome, "held"
            if self.current is attempt:
                self.current = None
            held = [a for a in self.tombstones.values() if a.outcome_state == "held"]
            for old in held[:-OUTCOMES_KEPT] if len(held) > OUTCOMES_KEPT else []:
                old.outcome, old.outcome_state = None, "dropped"

    def get(self, attempt_id):
        with self.lock:
            attempt = self.tombstones.get(attempt_id)
            return attempt.public(self.instance_id) if attempt else {"instance_id": self.instance_id, "known": False}

    def acknowledge(self, attempt_id):
        with self.lock:
            attempt = self.tombstones.get(attempt_id)
            if attempt and attempt.outcome_state == "held":
                attempt.outcome, attempt.outcome_state = None, "acknowledged"
            return attempt is not None

    def cancel(self, attempt_id):
        with self.lock:
            attempt = self.tombstones.get(attempt_id)
            if attempt and attempt.state != "finished":
                attempt.cancelled.set()
            return attempt is not None

    def job(self):
        with self.lock:
            attempt = self.current
            if attempt is None:
                return None
            return {"attempt_id": attempt.attempt_id, "run_id": attempt.run_id, "kind": attempt.kind,
                    "state": attempt.state, "started_at": attempt.accepted_at}
