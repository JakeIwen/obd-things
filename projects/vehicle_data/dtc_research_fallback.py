"""Relaunching a research turn on the subscription after its credit leg failed.

A credit launch that the API refused (exhausted, bad key, model unavailable) ends with the worker's
outcome ``failed`` / ``credit_fallback``. The advisor then launches the same turn again as a fresh,
counted, forced-subscription attempt: a report starts over with its original masked first prompt, a
follow-up becomes a replay in a fresh session. The owner sees one turn throughout; a turn has at
most one fallback because the fallback itself cannot fail on credits.

Durable state in the turn: ``cancel_requested`` (a cancel is never undone by a relaunch, a restart
or a late outcome) and ``pending_fallback`` ``{predecessor, deadline}``, saved before the first
send and kept across refusals, so a restart continues the fallback until its deadline. While a
request is in flight it also holds ``sending`` ``{attempt_id, mode_before}``: a restart that finds
that attempt unknown to the worker (fenced, so it can never arrive) withdraws it and continues.
The slot is free once the credit attempt finishes but another job can take it first; a definitive
``busy``, ``warning_chat`` or ``stale_instance`` refusal is retried by a thread of its own every
``fallback.retrySeconds`` until the deadline. The manager lock covers only the reservation and the
conclusion; the worker request in between is sent outside it, so a slow worker never blocks a
cancel or a progress read. A lost or uncertain reply is never resent: the attempt is followed like
any other (the send-once rule).
"""

import threading
import time

from projects.vehicle_data.api_credits_contract import CREDIT_CODES, FALLBACK, PROTOCOL
from projects.vehicle_data.dtc_research_contract import ResearchError, TURN_REASONS
from projects.vehicle_data.dtc_research_launch import UNSENT
from projects.vehicle_data.dtc_research_log import log
from projects.vehicle_data.dtc_research_prompt import first_prompt
from projects.vehicle_data import dtc_research_store as records
from projects.vehicle_data.research_worker_client import WorkerUnreachable

FALLBACK_REASON = PROTOCOL["fallbackReason"]
CAPABILITY = PROTOCOL["capabilityField"]
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
        try:
            threading.Thread(target=self._fallback_loop, args=(run_id,), daemon=True).start()
        except BaseException:
            with self.lock:
                self.fallbacks.discard(run_id)  # reconciliation can then take the pending fallback over
            raise

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
                reserved = self._reserve_fallback(record, turn)
            except ResearchError as exc:
                if exc.reason == "storage_error":
                    return retry  # nothing was sent; saving again also makes any earlier withdrawal durable
                return None if self._settle(record, _failure(exc.reason, str(exc)[:300])) else retry
        code, reply = self._transmit(reserved)  # outside the lock
        with self.lock:
            wait, after = self._conclude_fallback(run_id, reserved, code, reply)
        if after:
            after()
        return wait

    def _reserve_fallback(self, record, turn):
        report = turn["kind"] == "report"
        kind = "initial" if report else "replay"
        prompt = first_prompt(record["context"]) if report else self._replay_prompt(record)

        def mark(saved_turn, reserved):
            saved_turn["pending_fallback"]["sending"] = {"attempt_id": reserved.attempt_id,
                                                         "mode_before": reserved.mode_before}

        return self._reserve(record, kind, prompt, forced=True, ledger_kind="fallback", mark=mark)

    def _conclude_fallback(self, run_id, reserved, code, reply):
        """Decide what the worker's answer to the fallback request means, under the lock: ``(wait,
        after)`` like a step, ``after`` being worker work to do once the lock is released."""
        retry = FALLBACK["retrySeconds"]
        record = self.store.records.get(run_id)
        turn = record["turns"][-1] if record and record["turns"] else None
        pending = turn.get("pending_fallback") if turn else None
        if not pending or turn["attempt_ids"][-1] != reserved.attempt_id or turn["state"] not in records.BUSY_STATES:
            return None, None  # the turn moved on while the request was out
        if code in (202, None):
            return None, self._fallback_accepted(record, turn, reserved.attempt_id)
        self._withdraw(turn, reserved)
        pending.pop("sending", None)
        if turn.get("cancel_requested"):
            return (None if self._settle(record, {"status": "cancelled"}) else retry), None
        if code == UNSENT:
            return (None if self._settle(record, _failure("worker_unavailable", "The research service on the Pi is not running"))
                    else retry), None
        if code != 409:
            return (None if self._settle(record, _failure("worker_unavailable", "The research service on the Pi did not accept the request"))
                    else retry), None
        reason = reply.get("reason")
        refusal = self._refusal_error(reply)
        if reason not in RETRYABLE_REFUSALS and reason != "stale_instance":
            return (None if self._settle(record, _failure(refusal.reason, str(refusal)[:300])) else retry), None
        pending.update(refusal=reason, detail=str(refusal)[:300] if reason != "stale_instance" else pending.get("detail"))
        self._save_quietly(record, "fallback_refused")  # a failed save is made good by the next reservation
        return retry, (self._refresh_instance if reason == "stale_instance" else None)

    def _refresh_instance(self):
        """After a definitive ``stale_instance``: learn the restarted worker's instance and capability
        so the next try names it. Nothing was admitted, so the pending fallback simply continues."""
        try:
            status = self.worker.status()
        except WorkerUnreachable:
            return
        with self.lock:
            self.instance_id = status.get("instance_id") or self.instance_id
            self.billing_capable = status.get(CAPABILITY) is True

    def _fallback_accepted(self, record, turn, attempt_id):
        """The fallback attempt was sent (or may have been): the pending decision is over. Returns the
        acknowledgment of the credit outcome, to run outside the lock."""
        predecessor = turn["pending_fallback"]["predecessor"]
        turn.update(pending_fallback=None, fallback_of={"successor": attempt_id, "predecessor": predecessor})
        self._save_quietly(record, "fallback_accepted")
        self._follow(record["run_id"], attempt_id)
        turn["phase"] = "starting"

        def acknowledge():
            try:
                self.worker.acknowledge(predecessor)
            except Exception:  # the next ``drained`` check or restart acknowledges it
                pass

        return acknowledge

    def unsent_fallback(self, run_id, attempt_id):
        """After a restart (or a follow thread's fence): ``True`` when ``attempt_id`` was a fallback
        request the worker has fenced as never admitted; it is withdrawn, saved, and the pending
        fallback continues. ``False`` when it is not that, ``None`` when the withdrawal could not be
        saved yet (ask again). Never resends the withdrawn request: it is a new attempt id."""
        with self.lock:
            record = self.store.records.get(run_id)
            turn = record["turns"][-1] if record and record["turns"] else None
            pending = turn.get("pending_fallback") if turn else None
            sending = pending.get("sending") if pending else None
            if not sending or sending["attempt_id"] != attempt_id or turn["attempt_ids"][-1] != attempt_id \
                    or turn["state"] not in records.BUSY_STATES:
                return False

            def withdraw(saved):
                last = saved["turns"][-1]
                last["attempt_ids"].pop()
                last["mode"] = sending["mode_before"]
                last["pending_fallback"].pop("sending", None)
                last["state"] = "running"

            if not self._persist(record, withdraw, "fallback_withdraw"):
                return None
            self.ledger.refused(attempt_id)
        self._fallback_follow(run_id)
        return True

    def _save_quietly(self, record, during):
        try:
            self.store.save(record)
        except OSError:
            log("dtc_research_save_failed", run_id=record["run_id"], during=during)
