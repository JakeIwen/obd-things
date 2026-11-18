"""Relaunching a research turn on the subscription after its credit leg failed.

A credit launch that the API refused (exhausted, bad key, model unavailable) ends with the worker's
outcome ``failed`` / ``credit_fallback``. The advisor then launches the same turn again as a fresh,
counted, forced-subscription attempt: a report starts over with its original masked first prompt, a
follow-up becomes a replay in a fresh session. The owner sees one turn throughout; a turn has at
most one fallback because the fallback itself cannot fail on credits.

Durable state in the turn: ``cancel_requested`` (a cancel is never undone by a relaunch, a restart
or a late outcome) and ``pending_fallback`` ``{predecessor, deadline}``, saved before the first
send and kept across refusals, so a restart continues the fallback until its deadline. The slot is
free once the credit attempt finishes but another job can take it first; a definitive ``busy`` or
``warning_chat`` refusal is retried by a thread of its own (never under the manager lock) every
``fallback.retrySeconds`` until the deadline. A lost or uncertain reply is never resent: the
attempt is followed like any other (the send-once rule).
"""

import threading
import time

from projects.vehicle_data.api_credits_contract import CREDIT_CODES, FALLBACK, PROTOCOL
from projects.vehicle_data.dtc_research_contract import ResearchError, TURN_REASONS
from projects.vehicle_data.dtc_research_log import log
from projects.vehicle_data.dtc_research_prompt import first_prompt
from projects.vehicle_data import dtc_research_store as records

FALLBACK_REASON = PROTOCOL["fallbackReason"]
RETRYABLE_REFUSALS = frozenset({"busy", "warning_chat"})
DEFAULT_BUSY_WORDING = "Research is busy"
ALREADY_FELL_BACK = "The subscription run also failed on credits; this should not happen"


def is_credit_failure(outcome):
    return outcome.get("status") == "failed" and outcome.get("reason") == FALLBACK_REASON


def _failure(reason, detail):
    reason = reason if reason in TURN_REASONS else "provider_error"
    return {"status": "failed", "reason": reason, "detail": detail}


class FallbackMixin:
    """The fallback half of ``ResearchManager`` (it supplies the lock, store, ``_launch`` and ``_settle``)."""

    def _credit_failure(self, record, turn, attempt_id, outcome):
        """Decide a credit outcome for the turn's current attempt, under the lock. ``True`` once the
        decision is saved (a settled turn, or a pending fallback with its thread started); ``False``
        when the save failed, so the outcome stays unacknowledged and is delivered again."""
        pending = turn.get("pending_fallback")
        if pending and pending["predecessor"] == attempt_id:
            return True  # a duplicate delivery of an outcome already being acted on
        if turn.get("cancel_requested"):
            return self._settle(record, {"status": "cancelled"})
        if turn.get("fallback_of"):
            return self._settle(record, _failure("provider_error", ALREADY_FELL_BACK))
        code = outcome.get("credit_code") if outcome.get("credit_code") in CREDIT_CODES else None
        deadline = time.time() + FALLBACK["windowSeconds"]

        def mark(saved):
            last = saved["turns"][-1]
            last.update(pending_fallback={"predecessor": attempt_id, "deadline": deadline}, credit_code=code,
                        phase="starting")

        if not self._persist(record, mark, "fallback"):
            return False
        log("dtc_research_fallback", run_id=record["run_id"], predecessor=attempt_id, credit_code=code)
        self._fallback_follow(record["run_id"])
        return True

    def _fallback_follow(self, run_id):
        """Start the thread that sends the fallback and retries it; one per run."""
        with self.lock:
            if run_id in self.fallbacks:
                return
            self.fallbacks.add(run_id)
        threading.Thread(target=self._fallback_loop, args=(run_id,), daemon=True).start()

    def _fallback_loop(self, run_id):
        try:
            while not self.closed.is_set():
                wait = self._fallback_step(run_id)
                if wait is None:
                    return
                self.closed.wait(wait)
        except Exception as exc:  # never leave a pending fallback unattended
            log("dtc_research_fallback_failed", run_id=run_id, error=repr(exc)[:300])
            with self.lock:
                self.fallbacks.discard(run_id)
                record = self.store.records.get(run_id)
                turn = record["turns"][-1] if record and record["turns"] else None
                if turn and turn["state"] in records.BUSY_STATES:
                    turn["state"] = "reconciling"
                    self.state = "unknown"
            self.wake_reconciler()
        finally:
            with self.lock:
                self.fallbacks.discard(run_id)

    def _fallback_step(self, run_id):
        """One attempt to settle or send the fallback. ``None`` when nothing is pending any more,
        else the seconds to wait before the next step."""
        retry = FALLBACK["retrySeconds"]
        with self.lock:
            record = self.store.records.get(run_id)
            turn = record["turns"][-1] if record and record["turns"] else None
            pending = turn.get("pending_fallback") if turn else None
            if not pending or turn["state"] not in records.BUSY_STATES or turn["attempt_ids"][-1] != pending["predecessor"]:
                return None
            if turn["state"] == "reconciling":
                turn["state"] = "running"
            if turn.get("cancel_requested"):
                return None if self._settle(record, {"status": "cancelled"}) else retry
            if time.time() >= pending["deadline"]:
                outcome = _failure("provider_error", pending.get("detail") or DEFAULT_BUSY_WORDING)
                return None if self._settle(record, outcome) else retry
            try:
                refusal = self._send_fallback(record, turn)
            except ResearchError as exc:
                return None if self._settle(record, _failure(exc.reason, str(exc)[:300])) else retry
            return None if refusal is None else retry

    def _send_fallback(self, record, turn):
        report = turn["kind"] == "report"
        kind = "initial" if report else "replay"
        prompt = first_prompt(record["context"]) if report else self._replay_prompt(record)
        return self._launch(record, kind, prompt, quiet=RETRYABLE_REFUSALS, forced=True, ledger_kind="fallback",
                            accepted=self._fallback_accepted)

    def _fallback_accepted(self, record, turn, attempt_id):
        """The fallback attempt was sent: the pending decision is over and the credit outcome can go."""
        predecessor = turn["pending_fallback"]["predecessor"]
        turn.update(pending_fallback=None, fallback_of={"successor": attempt_id, "predecessor": predecessor})
        self._save_quietly(record, "fallback_accepted")
        try:
            self.worker.acknowledge(predecessor)
        except Exception:  # the next ``drained`` check or restart acknowledges it
            pass

    def _fallback_refused(self, record, turn, reason, detail):
        """A definitive refusal of the fallback's launch: keep it pending, with the wording to fail
        with, and save the restored turn before waiting (a restart then continues the fallback)."""
        turn["pending_fallback"].update(refusal=reason, detail=str(detail or DEFAULT_BUSY_WORDING)[:300])
        self._save_quietly(record, "fallback_refused")

    def _save_quietly(self, record, during):
        try:
            self.store.save(record)
        except OSError:
            log("dtc_research_save_failed", run_id=record["run_id"], during=during)
