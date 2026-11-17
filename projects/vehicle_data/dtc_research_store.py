"""Research runs on the Pi: one bounded JSON record per run, and the shapes the dashboard reads.

Records live in ``<advisor state>/dtc-research/runs/<run_id>.json``, written atomically and only by
the advisor, so their bounds hold in code: at most ``MAX_RUNS`` runs (the oldest finished run is
evicted first, a busy one never; only once a new run's launch has been admitted, so a refused start
never costs a report and one extra record exists only during a launch request), and every reply
about a run stays under the sizes in
``dtc_research.json`` (a follow-up is refused when its worst case would not fit).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from projects.vehicle_data.assistant_common import now
from projects.vehicle_data.dtc_research_contract import (
    CONTINUE_MODES, MAX_RUNS, MAX_TURNS, PHASES, RUN_ID_RE, SIZES, STATES, encoded_len,
)
from projects.vehicle_data.engine_off_voltage import _atomic_json

SCHEMA_VERSION = 1
BUSY_STATES = ("running", "reconciling")
_RUN_STATE = {"running": STATES["running"], "reconciling": STATES["running"], "complete": STATES["done"],
              "failed": STATES["failed"], "cancelled": STATES["cancelled"]}
SUMMARY_FIELDS = ("run_id", "module_key", "fca_display", "created_at", "updated_at", "provider", "model",
                  "effort", "state", "busy", "summary", "summary_note", "duration_s", "error", "reason",
                  "turn_count", "can_continue", "continue_mode")


def new_record(run_id, fields, context, request_id):
    stamp = now()
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "module_key": fields["module_key"],
            "fca_display": fields["fca_display"], "provider": fields["provider"], "model": fields["model"],
            "effort": fields["effort"], "created_at": stamp, "updated_at": stamp, "request_ids": [request_id],
            "context": context, "session": {"id": None, "state": "absent"}, "turns": []}


def new_turn(kind, question=None):
    return {"kind": kind, "question": question, "answer": None, "summary": None, "summary_note": None,
            "format_warnings": [], "state": "running", "error": None, "reason": None, "mode": "initial",
            "attempt_ids": [], "started_at": now(), "completed_at": None, "duration_s": None,
            "phase": "preparing", "activity": {"searches": 0, "pages": 0}}


def busy(record):
    return bool(record["turns"]) and record["turns"][-1]["state"] in BUSY_STATES


def _elapsed(turn):
    if turn["state"] not in BUSY_STATES:
        return None
    started = datetime.fromisoformat(turn["started_at"])
    return max(0, round((datetime.now(timezone.utc) - started).total_seconds()))


def public_turns(record):
    rows = []
    for turn in record["turns"]:
        if turn["kind"] == "follow_up":
            rows.append({"role": "user", "kind": "question", "text": turn["question"], "at": turn["started_at"]})
        rows.append({"role": "assistant", "kind": "report" if turn["kind"] == "report" else "answer",
                     "text": turn["answer"] if turn["state"] == "complete" else None,
                     "at": turn["completed_at"] or turn["started_at"],
                     "state": "running" if turn["state"] in BUSY_STATES else turn["state"],
                     "error": turn["error"], "reason": turn["reason"], "mode": turn["mode"],
                     "duration_s": turn["duration_s"]})
    return rows


def progress_text(turn):
    if turn["state"] not in BUSY_STATES:
        return None
    activity = turn["activity"]
    counts = []
    if activity["searches"]:
        counts.append(f"{activity['searches']} search" + ("es" if activity["searches"] != 1 else ""))
    if activity["pages"]:
        counts.append(f"{activity['pages']} page" + ("s" if activity["pages"] != 1 else ""))
    text = PHASES.get(turn["phase"] or "preparing", PHASES["preparing"])
    return text + (" · " + ", ".join(counts) if counts else "")


def summary(record):
    turns = record["turns"]
    report = turns[0] if turns else None
    last = turns[-1] if turns else None
    failed = last if last and last["state"] in ("failed", "cancelled") else None
    is_busy = busy(record)
    can = (bool(report) and report["state"] == "complete" and not is_busy and len(turns) < MAX_TURNS
           and has_room(record))
    return {"run_id": record["run_id"], "module_key": record["module_key"], "fca_display": record["fca_display"],
            "created_at": record["created_at"], "updated_at": record["updated_at"], "provider": record["provider"],
            "model": record["model"], "effort": record["effort"],
            "state": _RUN_STATE[report["state"]] if report else STATES["running"], "busy": is_busy,
            "summary": report["summary"] if report else None, "summary_note": report["summary_note"] if report else None,
            "duration_s": report["duration_s"] if report else None,
            "error": failed["error"] if failed else None, "reason": failed["reason"] if failed else None,
            "turn_count": len(public_turns(record)), "can_continue": can,
            "continue_mode": (CONTINUE_MODES["resume"] if record["session"]["state"] == "present"
                              else CONTINUE_MODES["replay"]) if can else CONTINUE_MODES["none"]}


def progress(record):
    last = record["turns"][-1] if record["turns"] else None
    is_busy = busy(record)
    return {"run_id": record["run_id"], "state": summary(record)["state"], "busy": is_busy,
            "phase": last["phase"] if last and is_busy else None,
            "progress": progress_text(last) if last else None,
            "activity": dict(last["activity"]) if last else {"searches": 0, "pages": 0},
            "elapsed_s": _elapsed(last) if last else None, "turn_count": len(public_turns(record)),
            "updated_at": record["updated_at"]}


def full(record):
    report = record["turns"][0] if record["turns"] else None
    live = progress(record)
    return {**summary(record), "phase": live["phase"], "progress": live["progress"], "activity": live["activity"],
            "elapsed_s": live["elapsed_s"], "format_warnings": report["format_warnings"] if report else [],
            "turns": public_turns(record)}


def has_room(record):
    """Whether one more question and answer (worst case) keeps the run's reply within its size."""
    return encoded_len(_bare_full(record)) + SIZES["followUpReserveBytes"] <= SIZES["runEncodedBytes"]


def _bare_full(record):
    # ``full`` without the room check, to avoid recursion through ``summary``.
    return {"turns": public_turns(record), "head": {key: record.get(key) for key in SUMMARY_FIELDS}}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.records = {}

    def load(self, log=print):
        """Read every record; a turn that was running becomes ``reconciling`` (decided with the worker)."""
        for path in sorted(self.directory.glob("*.json")):
            if path.is_symlink() or path.stat().st_size > SIZES["recordBytes"] or not RUN_ID_RE.fullmatch(path.stem):
                log(json.dumps({"event": "dtc_research_record_skipped", "file": path.name}))
                continue
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                log(json.dumps({"event": "dtc_research_record_unreadable", "file": path.name}))
                continue
            for turn in record.get("turns", []):
                if turn["state"] == "running":
                    turn["state"] = "reconciling"
            self.records[record["run_id"]] = record
        return self.records

    def save(self, record):
        record["updated_at"] = now()
        _atomic_json(self.directory / f"{record['run_id']}.json", record)

    def delete(self, run_id):
        (self.directory / f"{run_id}.json").unlink(missing_ok=True)
        self.records.pop(run_id, None)

    def evictable(self):
        """Run ids to evict so the runs fit ``MAX_RUNS`` (oldest finished first, never a busy one)."""
        finished = sorted((r for r in self.records.values() if not busy(r)), key=lambda r: r["created_at"])
        excess = len(self.records) - MAX_RUNS
        return [r["run_id"] for r in finished[:max(0, excess)]]

    def for_code(self, module_key, fca_display):
        return sorted((r for r in self.records.values()
                       if r["module_key"] == module_key and r["fca_display"] == fca_display),
                      key=lambda r: r["created_at"], reverse=True)

    def items(self):
        groups = {}
        for record in self.records.values():
            groups.setdefault((record["module_key"], record["fca_display"]), []).append(record)
        rows = []
        for records in groups.values():
            head = summary(max(records, key=lambda r: r["created_at"]))
            rows.append({key: head[key] for key in ("module_key", "fca_display", "run_id", "state", "busy",
                                                    "summary", "summary_note", "provider", "model", "effort",
                                                    "updated_at")} | {"runs": len(records)})
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)
