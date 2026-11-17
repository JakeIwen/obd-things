"""DTC research routes in the advisor: records, launches, and following each attempt to its end.

The advisor never launches a research CLI itself. It validates a request, gathers the code's
context from the broker, admits the launch in the durable ledger, and sends the research worker
one launch request per ``attempt_id``, ever (the send-once rule). After that it only asks about the
attempt: an adopted outcome is saved and acknowledged; an attempt whose outcome was dropped, or
that the worker does not know and has fenced (so the launch can no longer arrive late), makes the
turn ``interrupted``. It never infers that research is idle
from a timeout, and it acknowledges an outcome only once the record holding it is on disk: a failed
write keeps the follow thread asking (and, if a follow thread itself fails, the turn goes back to
reconciliation). See docs/dtc-research.md.
"""

import copy
from datetime import datetime
import json
import sys
import threading
import time
import uuid

from projects.vehicle_data.assistant_common import ChatError, now
from projects.vehicle_data.assistant_slot import StartWindow
from projects.vehicle_data import dtc_research_context, dtc_research_store as records
from projects.vehicle_data.dtc_research_contract import (
    LIMITS, MAX_TURNS, TURN_REASONS, ResearchError, parse_path, validate_post,
)
from projects.vehicle_data.dtc_research_prompt import (
    first_prompt, follow_up_prompt, parse_answer, parse_report, replay_prompt,
)
from projects.vehicle_data.research_worker_client import WorkerUnreachable

POLL_SECONDS = 1.0
LOST_POLL_SECONDS = 2.0
# Worker refusals, as the frontend sees them when a launch is refused synchronously.
REFUSAL_TO_ERROR = {"busy": "busy", "warning_chat": "busy", "low_memory": "busy", "stale_instance": "busy",
                    "duplicate_attempt": "busy", "not_activated": "provider_unavailable",
                    "provider_unavailable": "provider_unavailable"}
REFUSAL_DETAIL = {"warning_chat": "A warning chat is answering a question; try again when it finishes",
                  "stale_instance": "The research service restarted; try again in a few seconds"}


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


class ResearchManager:
    def __init__(self, store, ledger, catalog, worker, broker, *, window=None, poll=POLL_SECONDS):
        self.store, self.ledger, self.catalog, self.worker, self.broker = store, ledger, catalog, worker, broker
        self.window = window or StartWindow(LIMITS["startsPerWindow"], LIMITS["windowSeconds"])
        self.poll = poll
        self.lock = threading.RLock()
        self.state = "unknown"  # unknown -> idle (reconciliation); see dtc_research_reconcile
        self.instance_id = None
        self.following = set()  # attempt ids a follow thread is watching
        self.starting = {}  # request_id -> (module_key, fca_display) of starts still gathering their context
        self.closed = threading.Event()

    # --- routes ----------------------------------------------------------------------------------
    def handle(self, method, path, payload):
        route = parse_path(method, path)
        if route is None:
            raise ResearchError("not_found", "Unknown research request")
        name, args = route
        if name == "models":
            return self.catalog.reply()
        with self.lock:
            if name == "list":
                return {"available": True, "items": self.store.items()}
            if name == "code":
                runs = self.store.for_code(*args)
                return {"available": True, "module_key": args[0], "fca_display": args[1],
                        "runs": [records.summary(r) for r in runs], "selected": records.full(runs[0]) if runs else None}
            if name in ("run", "progress"):
                record = self._record(args[0])
                return ({"available": True, "run": records.full(record)} if name == "run"
                        else {"available": True, "progress": records.progress(record)})
        action, fields = validate_post(payload)
        return {"start": self.start, "ask": self.ask, "cancel": self.cancel}[action](fields)

    def _record(self, run_id):
        record = self.store.records.get(run_id)
        if record is None:
            raise ResearchError("not_found", "That research is no longer on the Pi")
        return record

    def _require_idle(self):
        if self.state != "idle":
            try:
                self.worker.status()
            except WorkerUnreachable:
                raise ResearchError("worker_unavailable", "The research service on the Pi is not running") from None
            raise ResearchError("busy", "The assistant is checking on a research job after a restart; try again shortly")

    def _admission(self):
        self._require_idle()
        # Starts still gathering their context count too, so concurrent starts cannot pass the window together.
        if self.window.full() or len(self.window.recent) + len(self.starting) >= self.window.limit:
            raise ResearchError("rate_limited", "Too many research starts in the last minute")

    def start(self, fields):
        request_id, code = fields["request_id"], (fields["module_key"], fields["fca_display"])
        with self.lock:
            duplicate = next((r for r in self.store.records.values() if request_id in r["request_ids"]), None)
            if duplicate:
                return {"available": True, "run": records.full(duplicate)}
            if request_id in self.starting or code in self.starting.values() \
                    or any(records.busy(r) for r in self.store.for_code(*code)):
                raise ResearchError("run_busy", "Research is already running for this code")
            self._admission()
            self.catalog.validate(fields["provider"], fields["model"], fields["effort"])
            # Reserved until this start ends: no second start for this request or this code meanwhile.
            self.starting[request_id] = code
        try:
            context = dtc_research_context.gather(self.broker, *code)  # outside the lock: broker reads take seconds
            with self.lock:
                self._require_idle()  # a worker restart may have been noticed while gathering
                return self._create_and_launch(fields, context)
        finally:
            with self.lock:
                self.starting.pop(request_id, None)

    def _create_and_launch(self, fields, context):
        run_id = uuid.uuid4().hex
        record = records.new_record(run_id, fields, context, fields["request_id"])
        record["turns"].append(records.new_turn("report"))
        self.store.records[run_id] = record
        try:
            self._save(record)
            self._launch(record, "initial", first_prompt(context))
        except Exception:
            self.store.delete(run_id)
            raise
        self.window.record()
        # Only an admitted launch makes room: a refused start never costs an existing report.
        for old in self.store.evictable():
            self._evict(old)
        return {"available": True, "run": records.full(record)}

    def ask(self, fields):
        with self.lock:
            record = self._record(fields["run_id"])
            if fields["request_id"] in record["request_ids"]:
                return {"available": True, "run": records.full(record)}
            report = record["turns"][0] if record["turns"] else None
            if records.busy(record):
                raise ResearchError("run_busy", "A turn is already running for this research")
            if not report or report["state"] != "complete":
                raise ResearchError("cannot_continue", "The report did not finish, so there is nothing to follow up on")
            if len(record["turns"]) >= MAX_TURNS:
                raise ResearchError("turn_limit", f"This research has reached its {MAX_TURNS}-turn limit")
            if not records.has_room(record):
                raise ResearchError("record_full", "This research is full; start new research to continue")
            self._admission()
            if len(record["request_ids"]) >= LIMITS["requestIdsPerRun"]:
                raise ResearchError("turn_limit", "This research has reached its request limit")
            turn = records.new_turn("follow_up", fields["question"])
            record["turns"].append(turn)
            record["request_ids"].append(fields["request_id"])
            try:
                self._save(record)
                if record["session"]["state"] == "present":
                    refusal = self._launch(record, "resume", follow_up_prompt(fields["question"]), quiet={"session_missing"})
                    if refusal == "session_missing":
                        record["session"] = {"id": None, "state": "absent"}
                        self._launch(record, "replay", self._replay_prompt(record))
                else:
                    self._launch(record, "replay", self._replay_prompt(record))
            except Exception:
                record["turns"].pop()
                record["request_ids"].pop()
                try:
                    self.store.save(record)
                except OSError:  # the file keeps the unsent turn; it is reconciled as never launched
                    log("dtc_research_save_failed", run_id=record["run_id"], during="ask_rollback")
                raise
            self.window.record()
            return {"available": True, "run": records.full(record)}

    def cancel(self, fields):
        with self.lock:
            record = self._record(fields["run_id"])
            if records.busy(record):
                turn = record["turns"][-1]
                for attempt_id in turn["attempt_ids"][-1:]:
                    try:
                        self.worker.cancel(attempt_id)
                    except WorkerUnreachable:
                        pass
            return {"available": True, "run": records.full(record)}

    # --- launching and following ---------------------------------------------------------------
    def _replay_prompt(self, record):
        report = record["turns"][0]["answer"] or ""
        exchanges = [(t["question"], t["answer"]) for t in record["turns"][1:-1] if t["state"] == "complete"]
        return replay_prompt(record["context"], report, exchanges, record["turns"][-1]["question"])

    def _launch(self, record, kind, prompt, quiet=()):
        """Admit in the ledger, send the launch once, and follow it. Raises ``ResearchError`` for a
        refusal the caller reports; returns a refusal reason listed in ``quiet`` instead."""
        turn = record["turns"][-1]
        attempt_id = uuid.uuid4().hex
        self.ledger.admit(record["run_id"], attempt_id, kind)
        turn["attempt_ids"].append(attempt_id)
        turn["mode"] = {"initial": "initial", "resume": "resumed", "replay": "replayed"}[kind]
        try:
            self._save(record)  # the attempt id is on disk before the request can arrive
        except ResearchError:
            self.ledger.refused(attempt_id)
            turn["attempt_ids"].pop()
            raise
        body = {"attempt_id": attempt_id, "expected_instance_id": self.instance_id, "run_id": record["run_id"],
                "provider": record["provider"], "model": record["model"], "effort": record["effort"],
                "kind": "resume" if kind == "resume" else "initial",
                "turn_kind": "report" if turn["kind"] == "report" else "follow_up", "prompt": prompt}
        if kind == "resume":
            body["session_id"] = record["session"]["id"]
        try:
            code, reply = self.worker.launch("/turns", body)
        except WorkerUnreachable as exc:
            if not exc.sent:  # certainly never arrived: nothing was launched
                self.ledger.refused(attempt_id)
                turn["attempt_ids"].pop()
                raise ResearchError("worker_unavailable", "The research service on the Pi is not running") from None
            code, reply = None, None  # it may or may not have arrived: from now on only ask about it
        if code == 409:
            self.ledger.refused(attempt_id)
            turn["attempt_ids"].pop()
            reason = reply.get("reason")
            if reason == "stale_instance":
                self.state = "unknown"  # the reconciler learns the new instance
                self.wake_reconciler()
            if reason in quiet:
                return reason
            error = REFUSAL_TO_ERROR.get(reason, "busy")
            raise ResearchError(error, REFUSAL_DETAIL.get(reason) or reply.get("detail") or "Research is busy")
        if code not in (202, None):
            raise ResearchError("worker_unavailable", "The research service on the Pi did not accept the request")
        turn["phase"] = "starting"
        self._follow(record["run_id"], attempt_id)
        return None

    def _follow(self, run_id, attempt_id):
        self.following.add(attempt_id)
        threading.Thread(target=self._follow_loop, args=(run_id, attempt_id), daemon=True).start()

    def _follow_loop(self, run_id, attempt_id):
        try:
            self._watch(run_id, attempt_id)
            return
        except Exception as exc:  # never leave a running turn unwatched
            log("dtc_research_follow_failed", run_id=run_id, attempt_id=attempt_id, error=repr(exc)[:300])
        finally:
            self.following.discard(attempt_id)
        self._return_to_reconciliation(run_id, attempt_id)

    def _return_to_reconciliation(self, run_id, attempt_id):
        """Hand a turn whose follow thread failed back to the reconciler, which adopts it or asks about it."""
        with self.lock:
            record = self.store.records.get(run_id)
            turn = record["turns"][-1] if record and record["turns"] else None
            if turn and attempt_id in turn["attempt_ids"] and turn["state"] in records.BUSY_STATES:
                turn["state"] = "reconciling"
                self.state = "unknown"
        self.wake_reconciler()

    def _watch(self, run_id, attempt_id):
        expected = self.instance_id
        while not self.closed.is_set():
            try:
                state = self.fence_unknown(attempt_id, self.worker.attempt(attempt_id), expected)
            except WorkerUnreachable:
                time.sleep(LOST_POLL_SECONDS)
                continue
            if not state.get("known") or state.get("outcome_state") == "dropped":
                why = "it was never accepted" if state.get("instance_id") == expected else "the research service restarted"
                if self._finish(run_id, attempt_id, {"status": "failed", "reason": "interrupted",
                                                     "detail": f"The answer was lost ({why})"}):
                    return
            elif state.get("outcome_state") == "held":
                if self._finish(run_id, attempt_id, state["outcome"]):
                    try:
                        self.worker.acknowledge(attempt_id)
                    except WorkerUnreachable:
                        pass
                    return
            else:
                self._progress(run_id, attempt_id, state)
            time.sleep(self.poll)

    def fence_unknown(self, attempt_id, state, expected):
        """The worker's final word on an attempt. ``known: false`` from the instance the launch was
        addressed to may only mean the request is still in transit, so the attempt is fenced and the
        fence's reply is the answer: still unknown means it can never launch, known means the request
        arrived after all. From another instance the answer stands: a late request names the old
        instance and is refused as ``stale_instance``."""
        if state.get("known") or state.get("instance_id") != expected:
            return state
        return self.worker.fence(attempt_id)

    def _progress(self, run_id, attempt_id, state):
        with self.lock:
            record = self.store.records.get(run_id)
            turn = record["turns"][-1] if record else None
            if not turn or attempt_id not in turn["attempt_ids"]:
                return
            turn["phase"] = state.get("phase") or turn["phase"]
            turn["activity"] = state.get("activity") or turn["activity"]
            if turn["state"] == "reconciling":
                turn["state"] = "running"
            if state.get("session_id") and record["session"]["id"] != state["session_id"]:
                record["session"] = {"id": state["session_id"], "state": "present"}
                try:
                    self.store.save(record)
                except OSError:  # kept in memory; the outcome's save writes it with the rest
                    log("dtc_research_save_failed", run_id=run_id, during="progress")

    def _finish(self, run_id, attempt_id, outcome):
        """Settle an attempt's outcome in its record. ``True`` once that is saved, or when the record
        no longer waits on this attempt; ``False`` when the save failed, so the caller keeps the
        outcome unacknowledged on the worker and tries again."""
        with self.lock:
            record = self.store.records.get(run_id)
            if record is None:
                return True
            turn = record["turns"][-1]
            if attempt_id not in turn["attempt_ids"] or turn["state"] not in records.BUSY_STATES:
                return True
            if outcome.get("reason") == "resume_unverified" and turn["mode"] == "resumed":
                record["session"] = {"id": None, "state": "absent"}
                try:
                    self._launch(record, "replay", self._replay_prompt(record))
                    return True
                except ChatError as exc:
                    outcome = {"status": "failed", "reason": "provider_error", "detail": f"Could not continue: {exc}"}
            return self._settle(record, outcome)

    def _settle(self, record, outcome):
        """Apply ``outcome`` to the record's last turn and save it; the live record changes only once
        the result is on disk. ``False`` when the save failed (the turn stays as it was)."""
        settled = copy.deepcopy(record)
        self._apply(settled, settled["turns"][-1], outcome)
        try:
            self.store.save(settled)
        except OSError:
            log("dtc_research_save_failed", run_id=record["run_id"], during="outcome")
            return False
        record.clear()
        record.update(settled)
        return True

    def _apply(self, record, turn, outcome):
        turn["completed_at"] = now()
        turn["phase"] = None
        started = turn["started_at"]
        turn["duration_s"] = round(time.time() - _epoch(started))
        if "session_present" in outcome:
            record["session"] = ({"id": outcome.get("session_id"), "state": "present"} if outcome["session_present"]
                                 else {"id": None, "state": "absent"})
        status = outcome.get("status")
        if status == "complete" and isinstance(outcome.get("answer"), str):
            if turn["kind"] == "report":
                parsed = parse_report(outcome["answer"])
                turn.update(answer=parsed["body"], summary=parsed["summary"], summary_note=parsed["summary_note"],
                            format_warnings=parsed["format_warnings"])
            else:
                turn["answer"], _ = parse_answer(outcome["answer"])
            turn.update(state="complete", error=None, reason=None)
            return
        reason = outcome.get("reason")
        if status == "cancelled":
            turn.update(state="cancelled", reason="cancelled", error="Stopped")
            return
        turn.update(state="failed", reason=reason if reason in TURN_REASONS else "provider_error",
                    error=str(outcome.get("detail") or "The research turn failed")[:300])

    def _save(self, record):
        try:
            self.store.save(record)
        except OSError:
            raise ResearchError("storage_error", "The Pi could not save the research record; nothing was started") from None

    def _evict(self, run_id):
        self.store.delete(run_id)
        try:
            self.worker.delete_run(run_id)
        except WorkerUnreachable:
            pass  # the next sweep removes the files

    def wake_reconciler(self):
        """Replaced by ``dtc_research_reconcile.Reconciler`` when it is attached."""

    def close(self):
        self.closed.set()
        self.catalog.stop()


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()
