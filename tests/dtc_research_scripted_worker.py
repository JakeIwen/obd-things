"""A scripted research worker for the advisor's billing tests: the ``WorkerClient`` interface over an
in-memory table, speaking the credit protocol (``credit_fallback`` outcomes, ``replay_required``
refusals, busy refusals) that tests choose attempt by attempt. The real worker's own behaviour is
tested beside the worker; this only plays the other end of the advisor's conversation.
"""

import threading
import uuid

from projects.vehicle_data.research_worker_client import WorkerUnreachable

FAKE_KEY = "sk-ant-api03-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
REPORT = "SUMMARY: The radar saw a misalignment and wants a service alignment.\n## What it means\nx\n" \
         "## Likely causes on this van\nx\n## What to check\nx\n## Sources\n- [a](https://example.com)"
ANSWER = "Check the bracket first."


def complete(body, billing="subscription"):
    """A normal finished turn for ``body``."""
    answer = REPORT if body["turn_kind"] == "report" else ANSWER
    return {"status": "complete", "answer": answer, "session_present": True, "session_id": uuid.uuid4().hex,
            "billing": billing, "credit_code": None}


def credit_failure(code="exhausted"):
    return {"status": "failed", "reason": "credit_fallback", "credit_code": code, "billing": "api_credits",
            "detail": "The credit launch failed"}


def credit_leg_then_subscription(code="exhausted"):
    """Credits fail on a capable, unforced launch; a forced launch runs on the subscription."""
    def behave(body):
        if body.get("billing_capable") and body.get("billing") != "subscription":
            return credit_failure(code)
        return complete(body)
    return behave


class StubCatalog:
    """The picker's validation without a worker behind it."""

    def validate(self, provider, model, effort):
        pass

    def stop(self):
        pass

    def start(self):
        pass

    def refresh(self):
        return True


class ScriptedWorker:
    def __init__(self, capable=True, behavior=None):
        self.capable = capable
        self.behavior = behavior or (lambda body: complete(body))  # body -> outcome dict, or None to keep it running
        self.instance_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.attempts, self.launches, self.cancelled, self.acked = {}, [], [], []
        self.refusals = []  # (status, reply) answers for the next launches, before any is accepted
        self.refuse_resume = None  # a refusal reason for every resume launch
        self.withhold = False  # finished outcomes are not visible yet; ``release`` shows them
        self.redeliver = set()  # attempt ids whose outcome is shown as held again after an acknowledgment
        self.down = False
        self.ack_fails = False
        self.lose_reply_for = set()  # launch ordinals (1-based) whose reply is lost after arrival
        self.extra_attempts = {}  # attempt id -> state reply, for jobs this advisor never launched
        self.credits_reply = {"key_installed": True, "enabled": True, "until": "2026-12-08T00:00:00Z",
                              "entries": [], "latches": {"account": None, "models": {}}, "last_fallback": None,
                              "ledger_full": False, "api_sessions": 0}
        self.job = None

    def _check(self):
        if self.down:
            raise WorkerUnreachable("connection refused", sent=False)

    def status(self):
        self._check()
        reply = {"instance_id": self.instance_id, "job": self.job, "providers": {}}
        if self.capable:
            reply["billing_capable"] = True
        return reply

    def credits(self):
        self._check()
        return self.credits_reply

    def launch(self, route, body):
        self._check()
        with self.lock:
            self.launches.append(body)
            ordinal = len(self.launches)
            if self.refusals:
                return self.refusals.pop(0)
            if body["kind"] == "resume" and self.refuse_resume:
                return 409, {"reason": self.refuse_resume, "detail": "refused"}
            self.attempts[body["attempt_id"]] = {"body": body, "outcome": self.behavior(body), "acknowledged": False,
                                                 "session_id": uuid.uuid4().hex}
        if ordinal in self.lose_reply_for:
            raise WorkerUnreachable("lost after arrival")
        return 202, {"attempt_id": body["attempt_id"]}

    def _view(self, attempt_id, attempt):
        outcome = attempt["outcome"]
        if outcome is None or self.withhold:
            return {"known": True, "instance_id": self.instance_id, "state": "running", "outcome_state": None,
                    "phase": "searching", "activity": {"searches": 1, "pages": 0}, "session_id": attempt["session_id"]}
        held = not attempt["acknowledged"] or attempt_id in self.redeliver
        return {"known": True, "instance_id": self.instance_id, "state": "finished",
                "outcome_state": "held" if held else "acknowledged", "outcome": outcome}

    def attempt(self, attempt_id):
        self._check()
        with self.lock:
            if attempt_id in self.extra_attempts:
                return self.extra_attempts[attempt_id]
            attempt = self.attempts.get(attempt_id)
            if attempt is None:
                return {"known": False, "instance_id": self.instance_id}
            return self._view(attempt_id, attempt)

    def fence(self, attempt_id):
        return self.attempt(attempt_id)

    def acknowledge(self, attempt_id):
        self._check()
        if self.ack_fails:
            raise WorkerUnreachable("the acknowledgment was lost")
        with self.lock:
            self.acked.append(attempt_id)
            if attempt_id in self.attempts:
                self.attempts[attempt_id]["acknowledged"] = True

    def cancel(self, attempt_id):
        self._check()
        with self.lock:
            self.cancelled.append(attempt_id)
            attempt = self.attempts.get(attempt_id)
            if attempt is not None and attempt["outcome"] is None:
                attempt["outcome"] = {"status": "cancelled", "billing": "api_credits"}

    def finish(self, attempt_id, outcome):
        """A held-open attempt (behavior returned ``None``) ends with ``outcome``."""
        with self.lock:
            self.attempts[attempt_id]["outcome"] = outcome

    def release(self):
        self.withhold = False

    def sweep(self, keep):
        return 200, {"removed": []}

    def delete_run(self, run_id):
        return 200, {"deleted": True}

    def bodies(self, **match):
        return [b for b in self.launches if all(b.get(k) == v for k, v in match.items())]
