"""Deterministic old/new historian oracle; no frozen implementation or live I/O.

Run this file with --repo-root pointing at each checkout and compare --output
bytes. --coverage records executed lines and conditional-jump outcomes using
Python's opcode tracer (coverage.py is not required). Coverage is deliberately
separate from the behavior artifact because extraction moves source locations.
The fixture imports come from the selected checkout, not the script's checkout.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
import dis
import hashlib
import itertools
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def tables(conn):
    """Every table, including evidence blobs, sequence counters and trip context."""
    result = {}
    names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    for (name,) in names:
        quoted = '"' + name.replace('"', '""') + '"'
        rows = conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
        result[name] = {
            "columns": [column[0] for column in rows.description],
            "rows": [
                [value if not isinstance(value, bytes) else {"hex": value.hex()}
                 for value in row]
                for row in rows
            ],
        }
    return result


class BranchTrace:
    def __init__(self):
        self.lines = defaultdict(set)
        self.branches = defaultdict(set)
        self.instructions = {}
        self.previous = {}

    def __call__(self, frame, event, arg):
        code = frame.f_code
        if not code.co_filename.endswith("/history/advisories.py"):
            return None
        key = code.co_name
        if event == "call":
            frame.f_trace_opcodes = True
            self.instructions.setdefault(code, {i.offset: i for i in dis.get_instructions(code)})
        elif event == "line":
            self.lines[key].add(frame.f_lineno)
        elif event == "opcode":
            previous = self.previous.get(id(frame))
            if previous is not None:
                instruction = self.instructions[code].get(previous)
                if instruction and ("IF" in instruction.opname or instruction.opname == "FOR_ITER"):
                    self.branches[(key, instruction.offset, instruction.starts_line,
                                   instruction.opname)].add(frame.f_lasti == instruction.argval)
            self.previous[id(frame)] = frame.f_lasti
        elif event == "return":
            self.previous.pop(id(frame), None)
        return self

    def report(self):
        return {
            "lines": {key: sorted(value) for key, value in sorted(self.lines.items())},
            "conditional_jumps": [
                {"function": key[0], "offset": key[1], "line": key[2],
                 "opcode": key[3], "outcomes": sorted(outcomes)}
                for key, outcomes in sorted(self.branches.items())
            ],
        }


def run(args):
    sys.path.insert(0, str(args.repo_root.resolve()))
    from projects.vehicle_data import event_history, notifications
    from projects.vehicle_data.historian import TelemetryHistorian
    from projects.vehicle_data.history.models import SYSTEM_NOTE_CATEGORIES
    from tests.test_event_history import coolant
    from tests.test_vehicle_advisory_episodes import assessment
    from tests.test_vehicle_historian import available, definition, snapshot
    from tests.test_warning_delivery import DAY, NIGHT, tiered, us

    trace = BranchTrace()
    totals = Counter()
    events = Counter()
    states = Counter()
    exceptions = Counter()
    scenarios = 0
    batches = 0
    output = args.output.open("w")

    class Scenario:
        def __init__(self, name, start=DAY):
            nonlocal scenarios
            scenarios += 1
            self.name, self.start = name, start
            self.tmp = tempfile.TemporaryDirectory()
            self.h = TelemetryHistorian(Path(self.tmp.name) / "history.sqlite3")
            self.h._baseline_inputs["oracle-baseline"] = [{"value": 190.4, "trip_id": 1}]
            self.index = 0

        def record(self, items, offset, **kwargs):
            nonlocal batches
            at = self.start + timedelta(seconds=offset)
            sql = []
            self.h._conn.set_trace_callback(sql.append)
            try:
                result = self.h.record_advisory_assessments(items, evaluated_at=at, **kwargs)
                result = vars(result)
                totals.update({k: v for k, v in result.items() if isinstance(v, int)})
            except Exception as exc:
                result = {"exception": type(exc).__name__, "message": str(exc)}
                exceptions[type(exc).__name__ + ": " + str(exc)] += 1
            finally:
                self.h._conn.set_trace_callback(None)
            dump = tables(self.h._conn)
            output.write(encode({"scenario": self.name, "batch": self.index,
                                 "result": result, "sql_digest": digest(sql),
                                 "tables": {k: digest(v) for k, v in dump.items()}}) + "\n")
            self.index += 1
            batches += 1

        def trip(self, offset, close=False):
            at = self.start + timedelta(seconds=offset)
            with self.h._conn:
                if close:
                    self.h._conn.execute(
                        "UPDATE trips SET ended_us=?,ended_at=?,end_reason='test' WHERE ended_us IS NULL",
                        (us(at), at.isoformat()),
                    )
                else:
                    self.h._conn.execute(
                        "INSERT INTO trips(started_us,started_at,last_active_us,last_active_at,"
                        "start_basis,snapshot_count) VALUES(?,?,?,?,'test',0)",
                        (us(at), at.isoformat(), us(at), at.isoformat()),
                    )

        def deliver(self, offset, mode):
            at = self.start + timedelta(seconds=offset)
            for row in self.h.pending_advisory_notifications(at=at, limit=100):
                if mode == "delivered":
                    self.h.mark_advisory_notification_delivered(row["id"], delivered_at=at)
                elif mode == "failed":
                    self.h.mark_advisory_notification_failed(
                        row["id"], attempted_at=at, error="synthetic failure", max_attempts=1,
                    )

        def finish(self):
            dump = tables(self.h._conn)
            for row in self.h._conn.execute("SELECT event_type FROM advisory_episode_events"):
                events[row[0]] += 1
            for row in self.h._conn.execute("SELECT evidence_state FROM advisory_episodes"):
                states[row[0]] += 1
            output.write(encode({"scenario": self.name, "final_tables": dump}) + "\n")
            self.h.close()
            self.tmp.cleanup()

    with patch.object(notifications, "QUIET_HOURS_ZONE", "US/Mountain"), patch.object(
        event_history.time, "time", return_value=DAY.timestamp()
    ):
        sys.settrace(trace)
        # 360 independent databases; exercise each tier, group representation,
        # urgency, delivery outcome, day/night eligibility and trip context.
        matrix = itertools.product(
            (None, 0, 1, 2, 3), ("warning", "critical"),
            ("pending", "delivered", "failed"), (DAY, NIGHT),
            (None, "shared", 7), (False, True),
        )
        for index, (tier, severity, delivery, start, group, driving) in enumerate(matrix):
            s = Scenario(f"matrix-{index:03d}", start)
            if driving:
                s.trip(-1)

            def item(state, offset, rule="main", **changes):
                at = start + timedelta(seconds=offset)
                a = tiered(state, at=at, rule=rule, tier=tier, group=group,
                           severity=severity, rule_snapshot={})
                if tier is None:
                    a.pop("tier")
                a.update(changes)
                return a

            for offset, state in enumerate(("unavailable", "normal", "watch", "warning")):
                s.record([item(state, offset)], offset)
            s.deliver(4, delivery)
            s.record([item("warning", 3)], 3)  # duplicate time
            s.record([item("warning", 2)], 2)  # late time
            for offset, state in enumerate(("warning", "watch", "warning", "unavailable",
                                           "unavailable", "insufficient_history", "rejected",
                                           "not_applicable", "recovering", "warning"), 5):
                s.record([item(state, offset)], offset)
            s.record([item("warning", 15, reason="changed context")], 15)
            s.record([item("warning", 16, rule="peer")], 16)
            s.record([item("warning", 17, rule=f"cap-{i}", group=f"group-{i}")
                      for i in range(5)], 17)
            s.record([item("warning", 18, severity="critical", tier=0)], 18)
            s.deliver(19, delivery)
            s.record([item("warning", 319, severity="critical", tier=0)], 319)
            s.deliver(320, delivery)
            if driving:
                s.trip(321, close=True)
                s.trip(322)
            s.record([item("warning", 323)], 323)
            s.record([item("normal", 324)], 324)
            s.deliver(325, "delivered")
            s.record([item("warning", 326)], 326)
            s.record([item("suppressed", 327, reason="")], 327)
            s.record([item("warning", 700000)], 700000)
            s.record([], 700001, authoritative_rule_keys=[])
            s.finish()

        # Time-based and count-based recovery, baseline retention, annotations,
        # interrupted watches, parked closure and episodes spanning trips.
        for timed, opening, driving in itertools.product((False, True), ("watch", "warning"), (False, True)):
            s = Scenario(f"evidence-{timed}-{opening}-{driving}", event_history.datetime.fromisoformat("2026-09-18T00:33:13+00:00"))
            metric = definition("engine.coolant_temperature", "°F")
            s.h.ingest_snapshot(snapshot(s.start, [metric], {metric["name"]: available(metric, 220, s.start)}, running=False), captured_at=s.start)
            def evidence(state, offset):
                a = coolant(state, value=195 if state == "normal" else 220, offset=offset)
                a["baseline"]["input_digest"] = "oracle-baseline"
                if timed:
                    a.update(tier=0, group="cooling")
                    a["rule_snapshot"].update(recovery_seconds=30)
                return a
            if driving:
                s.trip(-1)
            s.record([evidence(opening, 0)], 0)
            with s.h._conn:
                event_history.annotate(s.h._conn, 1, {"kind": "note", "note": "oracle",
                    "links": ["trip:1"], "request_id": "oracle-note-0001"})
            s.deliver(1, "delivered")
            for offset in range(5, 46, 5):
                a = evidence("normal", offset)
                if offset == 15:
                    a["state"] = "unavailable"
                s.record([a], offset)
            s.record([evidence("watch", 50)], 50)
            s.record([evidence("unavailable", 55)], 55)
            s.record([evidence("unavailable", 700)], 700)
            s.record([evidence("warning", 705)], 705)
            s.record([evidence("unavailable", 710)], 710)
            s.trip(720, close=True)
            s.record([evidence("unavailable", 90000)], 90000)
            s.record([evidence("warning", 90005)], 90005)
            a = evidence("warning", 90010)
            a["rule_revision"] = "replacement"
            s.record([a], 90010)
            s.h.acknowledge_advisory_episode(
                s.h.list_advisory_episodes(now=s.start)[0]["id"], acknowledged_at=s.start + timedelta(seconds=90011),
            )
            s.record([a], 90012)
            s.record([], 90013, authoritative_rule_keys=[])
            s.finish()

        for category, tier in itertools.product(sorted(SYSTEM_NOTE_CATEGORIES), (None, 0, 1, 2, 3)):
            s = Scenario(f"system-{category}-{tier}")
            for offset in range(4):
                a = tiered("warning", at=DAY, tier=tier, category=category)
                s.record([a], offset)
            s.finish()

        invalid = [None, 1, {}, {"rule": 1}, {"rule": ""}]
        for key, values in {
            "title": (None, ""), "state": (None, "invalid", []),
            "category": (None, ""), "advisory": (False, 1),
            "notification_rate_limit_seconds": (False, "60", 59, 691201, float("inf")),
            "current": ("invalid",), "persistence": ("invalid",),
            "extra": ({1, 2},),
        }.items():
            for value in values:
                a = assessment("warning", eligible=True)
                a[key] = value
                invalid.append(a)
        for index, bad in enumerate(invalid):
            s = Scenario(f"invalid-{index}")
            s.record([assessment("watch", rule="rollback"), bad], 0)
            s.record([assessment("warning", eligible=True)], 1)
            s.record([assessment("warning", eligible=True), assessment("watch")], 2)
            s.finish()
        for index, keys in enumerate(("bad", b"bad", [None], [""], ["x" * 501], ["x", "x"], [], ["other"], ["test_rule"])):
            s = Scenario(f"authoritative-{index}")
            s.record([assessment("warning", eligible=True)], 0)
            s.record([assessment("watch")], 1, authoritative_rule_keys=keys)
            s.finish()
        # Corrupt persisted JSON is deliberately recorded, not silently repaired
        # by this oracle, covering the retirement parser's compatibility paths.
        for index, stored in enumerate(("invalid json", "[]")):
            s = Scenario(f"stored-{index}")
            s.record([assessment("warning", eligible=True)], 0)
            with s.h._conn:
                s.h._conn.execute("UPDATE advisory_episodes SET latest_assessment_json=?", (stored,))
            s.record([], 1, authoritative_rule_keys=[])
            s.finish()
        for tier in (None, 0, 1):
            s = Scenario(f"repeat-and-dedupe-{tier}")
            a = assessment("warning", eligible=False)
            if tier is not None:
                a.update(tier=tier, group="repeat", severity="critical" if tier == 0 else "warning")
            s.record([a], 0)
            a["notification_eligible"] = True
            s.record([a], 1)
            # A cancelled owner remains a dedupe-key collision in this bucket.
            with s.h._conn:
                s.h._conn.execute("UPDATE advisory_notification_outbox SET status='cancelled'")
            s.record([a], 2)
            s.record([a], 604800)
            s.deliver(604801, "delivered")
            s.record([a], 1209600)
            s.record([a], 1209601, authoritative_rule_keys=[a["rule"]])
            s.finish()
        sys.settrace(None)
    summary = {"scenarios": scenarios, "batches": batches, "totals": dict(totals),
               "event_types": dict(events), "final_evidence_states": dict(states),
               "exceptions": dict(exceptions)}
    output.write(encode({"summary": summary}) + "\n")
    output.close()
    args.coverage.write_text(encode(trace.report()) + "\n")
    print(json.dumps(summary, sort_keys=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    run(parser.parse_args())
