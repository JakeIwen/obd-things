"""Durable advisory evidence and bounded asynchronous event browsing. No CAN I/O.

Writer helpers run inside the historian's episode/event/outbox transaction.
The reader uses its own query-only connection, never the live API thread.
"""
from collections import OrderedDict
from contextlib import closing
import zlib
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

VERSION = 3
WINDOW_BEFORE = 120
WINDOW_AFTER = 180
WINDOW_LIMIT = 256
WINDOW_BUDGET = 128 * 1024 * 1024
ACTIVE = {"watch", "warning"}
EVALUABLE = ACTIVE | {"normal", "recovering"}
GUIDE = {
    "version": VERSION,
    "architecture": "CAN observations -> five-second historian -> deterministic assessments -> atomic episode/events/outbox -> separate event reader -> dashboard and explanation chat. Viewing events never acquires vehicle data.",
    "episode": "An episode can open as watch before persistence qualifies warning. first_assessment is opening evidence; first_warning is separate and null means no recorded warning. A confirmed warning remains unresolved when latest_assessment is unavailable. An interrupted vehicle-health watch that never warned is archived unconfirmed after a full persistence window without usable evidence (at least 60 seconds). Archival is not recovery.",
    "overview": "The Early Warning health endpoint is a compact overview, with baseline input arrays and qualifying sample arrays omitted and explicitly marked. These omissions are not deletion or missing acquisition: full saved evidence remains in per-event details and exports. A failed overview fetch establishes neither that warnings cleared nor that notification delivery is disabled.",
    "storage": "Opening and transition assessments are retained; latest assessment is replaced. The event ledger is not every evaluation. Since evidence version 2, last evaluable/peak assessments and bounded sample windows are also retained. Legacy missing fields are never reconstructed as facts.",
    "baseline": "Relative rules compare a value to the median of comparable prior-trip minute medians, with threshold max(minimum_effect, MAD multiplier * 1.4826 * MAD). The full observation regime is not the set of baseline matching dimensions. A relative band is not an OEM limit or a diagnosis.",
    "interface_health": "Interface health is host telemetry infrastructure, not engine health. Before the first broker status probe, absent status is initialization and opens no role-failure episode. A probe still pending after 30 seconds or a failed probe is one status-discovery advisory, not three claims of broken adapters. Explicit missing/ambiguous identities, down links, controller errors and restoration inhibits remain actionable even at startup. Broker-owned B-CAN diagnostic mode is healthy only with a matching recorded supervisor route and exact healthy channel configuration; passive_ready=false alone does not mean a fault. Legacy generic unhealthy titles may be clarified for display; original_title and saved assessments preserve the original record. Do not assume every historical interface event was benign.",
    "monitoring": "Routine freshness expiry is a quiet coverage note within an episode, not a new vehicle-health event or alert. Never infer shutdown from silence. Confirmed running with a sustained loss of an expected metric can produce a separate telemetry-quality advisory, never a mechanical warning. Unconfirmed archived watches are excluded from TO REVIEW. Historical notes retain exact measurement age and any saved freshness limit.",
    "freshness": "observed_at is the measurement timestamp; captured_at is historian capture; evaluated_at is the decision time. capture_freshness describes capture only. effective_age_seconds and evaluation_state govern decision validity. Stale is not deleted. Unavailable persistence is not evaluated, not failed persistence.",
    "lifecycle": "Acknowledged/dismissed are owner dispositions, not mechanical recovery. Rule retirement and unconfirmed-watch archival are administrative. Confirmed warnings retain unresolved history during missing or inapplicable evidence. Recovering assessments retain unresolved episodes. Relative recovery requires comparable regime, unchanged rule revision, three distinct normal observations with bounded gaps and 20% threshold hysteresis. Historical pre-v2 recovery retains its original semantics.",
    "duration": "Elapsed open time includes parked/unobserved time. observed_abnormal_seconds sums only bounded adjacent fresh abnormal observations under the same regime; it does not prove continuous abnormality between samples. Unobserved time and evaluator gaps are separate; legacy durations may be unknown.",
    "retention": "Compact episode/event evidence has no automatic age deletion. Content-addressed baseline input archives are retained with decisions. Raw metric/interface samples default to seven days with rollup-before-prune. Per-event windows cover two minutes before/three after opening (256 primary samples max), globally budgeted to 128 MiB; pruned windows leave explicit tombstones. Export pages and event JSON for independent backups.",
    "analysis": "Cite evidence_ref/event_id/sample_id for facts. Separate recorded facts, calculations, hypotheses, and unknowns. Correlation with DTC/maintenance/data quality is temporal, not causal. Counterfactual replay never changes original decisions; partial windows cannot establish complete historical outcomes. Chat keeps the dated evidence revision used for its answers; refresh opens a new conversation.",
    "coolant": "Coolant baseline matching excludes thermal bands because conditioning on the measured temperature can hide an anomaly. Coolant v2 requires five minutes of continuous fresh RPM evidence and excludes baseline buckets in the first five minutes of each prior trip; these are monitoring choices, not OEM limits. Engine-off and early running applicability are explicit; candidate changes must be replayed with coverage shown. A later cold regime cannot demonstrate recovery in the earlier regime.",
}


UNCONFIRMED = "unconfirmed_monitoring_ended"
COVERAGE_EVENTS = frozenset(("evidence_inconclusive", "evidence_restored", "watch_unconfirmed"))


def episode_outcome(row):
    if row["resolution_reason"] == UNCONFIRMED:
        return "unconfirmed"
    return "unresolved" if row["status"] == "open" else "closed"


def archive_interrupted_watch(conn, episode, assessment, us):
    """Administrative closure after a full quiet window; never claims recovery.

    Runs in the existing ingest transaction. Existing warning evidence, including
    a warning that later deescalated, disqualifies automatic archival.
    """
    if episode is None or episode["category"] != "vehicle_health":
        return False
    first = json.loads(episode["first_assessment_json"])
    prior = json.loads(episode["latest_assessment_json"])
    if first.get("state") != "watch" or episode["current_state"] == "warning":
        return False
    inactive = {"unavailable", "not_applicable"}
    if assessment.get("state") not in inactive and prior.get("state") not in inactive:
        return False
    if conn.execute("SELECT 1 FROM advisory_episode_events WHERE episode_id=? AND new_state='warning' LIMIT 1", (episode["id"],)).fetchone():
        return False
    saved = conn.execute("SELECT data_json FROM advisory_evidence WHERE episode_id=?", (episode["id"],)).fetchone()
    evidence = json.loads(saved[0]) if saved else {}
    if evidence.get("first_warning"):
        return False
    window = (first.get("persistence") or {}).get("window_seconds")
    if not number(window):
        config = first.get("rule_snapshot") or {}
        window = config.get("persistence_window_seconds", config.get("window_seconds", 60))
    grace = max(60, min(600, window)) if number(window) else 60
    last_us = episode["last_observed_us"]
    last_usable = (evidence.get("last_evaluable") or {}).get("at")
    if last_usable:
        try:
            last_us = max(last_us, int(datetime.fromisoformat(last_usable).timestamp()*1e6))
        except (ValueError, TypeError):
            pass
    if us - last_us < grace * 1e6:
        return False
    # New fresh evidence belongs to a new episode; it cannot rewrite the old one.
    closing = stamp(assessment if assessment.get("state") in inactive else prior, iso(us))
    closing["episode_outcome"] = "unconfirmed"
    closing["monitoring_ended"] = {"quiet_window_seconds":grace,"last_usable_evaluation_at":iso(last_us),
                                    "archived_at":iso(us),"cause":"not_established"}
    checkpoint(conn, episode, closing, us)
    conn.execute("""UPDATE advisory_episodes SET status='resolved',current_state='suppressed',
        evidence_state=?,last_evaluated_us=?,last_evaluated_at=?,resolved_us=?,resolved_at=?,
        resolution_reason=?,latest_assessment_json=?,update_count=update_count+1,
        transition_count=transition_count+1 WHERE id=? AND status='open'""",
        (closing["state"],us,iso(us),us,iso(us),UNCONFIRMED,encode(closing),episode["id"]))
    conn.execute("""INSERT INTO advisory_episode_events(episode_id,event_us,event_at,event_type,
        previous_state,new_state,context_fingerprint,assessment_json) VALUES(?,?,?,?,?,?,?,?)""",
        (episode["id"],us,iso(us),"watch_unconfirmed",episode["current_state"],"suppressed",digest(closing),encode(closing)))
    conn.execute("UPDATE advisory_notification_outbox SET status='cancelled',last_error='unconfirmed watch archived' WHERE episode_id=? AND status='pending'", (episode["id"],))
    return True


def coverage_note(assessment):
    """Human explanation of monitoring validity; no inference about shutdown."""
    a = assessment or {}
    current = a.get("current") or {}
    policy = a.get("rule_snapshot") or {}
    age = current.get("effective_age_seconds")
    limit = policy.get("max_age_seconds")
    if a.get("state") in EVALUABLE:
        return {"title":"Monitoring resumed", "detail":"Usable observations became available again."}
    if a.get("state") == "unavailable":
        if number(age):
            text = f"The last reading was {current.get('value')} {current.get('unit','')}, recorded {age:.1f} seconds before this evaluation."
            if number(limit):
                text += f" This rule requires a reading no older than {limit:g} seconds."
            else:
                text += " The saved evaluator marked it too old; the original freshness limit was not saved."
        else:
            text = "No usable fresh reading was available for this evaluation."
        text += " Earlier evidence is retained. This does not establish recovery or explain why readings stopped."
        return {"title":"Monitoring paused", "detail":text}
    return {"title":"Monitoring coverage changed", "detail":a.get("reason") or "The rule could not continue its comparison under the current conditions."}

SCHEMA = """
CREATE TABLE IF NOT EXISTS advisory_baselines (digest TEXT PRIMARY KEY, inputs_zlib BLOB NOT NULL, bucket_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS advisory_evidence (
 episode_id INTEGER PRIMARY KEY REFERENCES advisory_episodes(id) ON DELETE CASCADE,
 data_json TEXT NOT NULL, window_json TEXT NOT NULL DEFAULT '[]',
 window_bytes INTEGER NOT NULL DEFAULT 2, window_state TEXT NOT NULL DEFAULT 'collecting',
 last_us INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE IF NOT EXISTS advisory_annotations (
 id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES advisory_episodes(id),
 request_id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, kind TEXT NOT NULL,
 note TEXT NOT NULL, links_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS advisory_annotations_episode ON advisory_annotations(episode_id,id);
"""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def iso(us):
    return datetime.fromtimestamp(us / 1e6, timezone.utc).isoformat()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def seconds(a, b):
    try:
        return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()
    except (TypeError, ValueError):
        return None


def stamp(assessment, at):
    """Freeze complete rule metadata at evaluation, without claiming a git revision."""
    a = json.loads(encode(assessment))
    a["evaluated_at"] = at
    a["evidence_version"] = VERSION
    current = a.get("current") or {}
    if "freshness" in current:
        current["capture_freshness"] = current.pop("freshness")
    if current:
        current["evaluation_state"] = a.get("state")
        current["decoder_revision"] = digest({k: current.get(k) for k in ("source", "quality", "provenance", "unit")})
    if a.get("state") not in EVALUABLE:
        a["persistence"] = {**a.get("persistence", {}), "evaluated": False}
    else:
        a["persistence"] = {**a.get("persistence", {}), "evaluated": True}
    return a


def recovery_gate(conn, episode, a, us):
    """Apply only the versioned relative-rule recovery contract; no legacy rewrite."""
    if episode is None or not a.get("rule_snapshot", {}).get("recovery_observations"):
        return a
    first = json.loads(episode["first_assessment_json"])
    if a.get("state") != "normal":
        return a
    a = dict(a)
    old = first.get("rule_revision")
    changed = old is not None and old != a.get("rule_revision")
    comparable = first.get("baseline_regime") == a.get("baseline_regime")
    comparable = comparable and all((first.get("current") or {}).get(k) == (a.get("current") or {}).get(k) for k in ("source","quality","provenance","unit"))
    if changed or not comparable:
        a.update(state="not_applicable", notification_eligible=False,
                 reason="rule revision changed; original condition not re-evaluated" if changed else "current operating regime does not establish recovery in the opening regime")
        return a
    saved = conn.execute("SELECT data_json FROM advisory_evidence WHERE episode_id=?", (episode["id"],)).fetchone()
    data = json.loads(saved[0]) if saved else {}
    previous = data.get("recovery", {})
    obs = (a.get("current") or {}).get("observed_at")
    deviation = a.get("deviation") or {}
    effect, threshold = deviation.get("effect_in_rule_direction"), deviation.get("threshold")
    safe = number(effect) and number(threshold) and effect <= threshold * .8
    gap = seconds(obs, previous.get("observed_at"))
    max_gap = a["rule_snapshot"].get("max_age_seconds", 10) * 2
    count = previous.get("count", 0) if gap is not None and 0 <= gap <= max_gap else 0
    if safe and obs and gap != 0:
        count += 1
    if not safe:
        count = 0
    a["recovery"] = {"count": count, "required": a["rule_snapshot"]["recovery_observations"],
                     "observed_at": obs, "hysteresis_fraction": .2, "comparable": True}
    if count < a["recovery"]["required"]:
        a.update(state="recovering", notification_eligible=False,
                 reason="comparable normal evidence awaits persistent recovery with hysteresis")
    return a


def checkpoint(conn, episode, a, us):
    """Update durable aggregates and copy a bounded primary-sample window."""
    ident = episode["id"]
    row = conn.execute("SELECT * FROM advisory_evidence WHERE episode_id=?", (ident,)).fetchone()
    data = json.loads(row["data_json"]) if row else {
        "started_at": iso(us), "legacy_prefix": us != episode["opened_us"],
        "observed_abnormal_seconds": 0.0, "unobserved_seconds": 0.0,
        "evaluator_gap_seconds": 0.0, "distinct_abnormal_observations": 0,
    }
    if row and us <= row["last_us"]:
        return  # replay cannot multiply counts or replace newer checkpoints
    prev = data.get("previous", {})
    current = a.get("current") or {}
    obs = current.get("observed_at")
    gap = seconds(obs, prev.get("observed_at"))
    fresh_limit = a.get("rule_snapshot", {}).get("max_age_seconds", 10)
    limit = 2 * fresh_limit
    if row:
        elapsed = (us - row["last_us"]) / 1e6
        if elapsed > 15:
            data["evaluator_gap_seconds"] += elapsed
        if a.get("state") not in EVALUABLE or prev.get("state") not in EVALUABLE or elapsed > limit:
            data["unobserved_seconds"] += elapsed
    if a.get("state") in ACTIVE and obs != prev.get("observed_at"):
        data["distinct_abnormal_observations"] += 1
        if (prev.get("state") in ACTIVE and gap is not None and 0 < gap <= limit
                and prev.get("regime") == a.get("regime")):
            data["observed_abnormal_seconds"] += gap
    ref = {"at": iso(us), "assessment": a, "evidence_ref": f"episode:{ident}:assessment:{us}"}
    if a.get("state") in EVALUABLE:
        data["last_evaluable"] = ref
        if a.get("state") == "warning" and not data.get("first_warning"):
            data["first_warning"] = ref
        effect = (a.get("deviation") or {}).get("effect_in_rule_direction")
        old_effect = ((data.get("peak_deviation") or {}).get("assessment", {}).get("deviation") or {}).get("effect_in_rule_direction")
        if number(effect) and (not number(old_effect) or effect > old_effect):
            data["peak_deviation"] = ref
        value = current.get("value")
        old_value = ((data.get("peak_value") or {}).get("assessment", {}).get("current") or {}).get("value")
        if number(value) and (not number(old_value) or value > old_value):
            data["peak_value"] = ref
    data["previous"] = {"observed_at": obs, "state": a.get("state"), "regime": a.get("regime")}
    data["recovery"] = a.get("recovery", {})
    data["last_evaluated_at"] = iso(us)
    window = json.loads(row["window_json"]) if row else []
    window_state = row["window_state"] if row else "collecting"
    if window_state == "collecting":
        metric = a.get("metric")
        start = episode["opened_us"] - WINDOW_BEFORE * 1_000_000
        end = episode["opened_us"] + WINDOW_AFTER * 1_000_000
        if metric:
            samples = conn.execute("""SELECT id,captured_us,observed_at,value_num,unit,source,
                quality,provenance,regime,trip_id,freshness FROM metric_samples
                WHERE metric=? AND captured_us>=? AND captured_us<=? AND value_kind='number'
                ORDER BY captured_us LIMIT ?""", (metric, start, min(us, end), WINDOW_LIMIT + 1)).fetchall()
            # Retain copied evidence even if ordinary raw rows subsequently expire.
            merged = {s["sample_id"]: s for s in window}
            for s in samples:
                item = dict(s)
                item["sample_id"] = item.pop("id")
                item["captured_at"] = iso(item.pop("captured_us"))
                item["value"] = item.pop("value_num")
                item["metric"] = metric
                item["evidence_ref"] = f"sample:{item['sample_id']}"
                merged[item["sample_id"]] = item
            window = sorted(merged.values(), key=lambda s: (s["captured_at"], s["sample_id"]))
            if len(window) > WINDOW_LIMIT:
                window, window_state = window[:WINDOW_LIMIT], "truncated_at_sample_limit"
        if us >= end and window_state == "collecting":
            window_state = "retained_partial_coverage"  # silence cannot prove complete coverage
        data["window_requested"] = {"start_at": iso(start), "end_at": iso(end)}
    raw = encode(window)
    conn.execute("""INSERT INTO advisory_evidence(episode_id,data_json,window_json,window_bytes,window_state,last_us)
        VALUES(?,?,?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET data_json=excluded.data_json,
        window_json=excluded.window_json,window_bytes=excluded.window_bytes,
        window_state=excluded.window_state,last_us=excluded.last_us""",
        (ident, encode(data), raw, len(raw.encode()), window_state, us))
    # Budget maintenance is bounded and only needed while adding window evidence.
    if window_state == "collecting" or not row:
        used = conn.execute("SELECT coalesce(sum(window_bytes),0) FROM advisory_evidence").fetchone()[0]
        if used > WINDOW_BUDGET:
            for old in conn.execute("SELECT episode_id,window_bytes FROM advisory_evidence WHERE window_bytes>2 ORDER BY episode_id LIMIT 32").fetchall():
                if used <= WINDOW_BUDGET:
                    break
                conn.execute("UPDATE advisory_evidence SET window_json='[]',window_bytes=2,window_state='pruned_budget' WHERE episode_id=?", (old[0],))
                used -= old[1] - 2


def summary(row):
    result = {**{k: row[k] for k in ("id", "rule_key", "title", "status", "current_state", "evidence_state", "opened_at", "resolved_at", "resolution_reason")}, "outcome":episode_outcome(row)}
    # Explain legacy generic titles without rewriting the saved assessments.
    if row["rule_key"].startswith("can_interface_role_") and row["title"].endswith("interface role is unhealthy"):
        a = json.loads(row["first_assessment_json"])
        c = a.get("current") or {}
        role = c.get("role", row["rule_key"].removeprefix("can_interface_role_").replace("_", "-"))
        result["original_title"] = row["title"]
        if c.get("reason") == "interface_role_absent":
            result["title"] = f"{role} interface status was unavailable"
        elif c.get("reason") == "topology_unusable" and str(c.get("role_reason", "")).startswith("broker auxiliary-drive owner"):
            result["title"] = f"{role} interface status flagged during polling"
        else:
            result["title"] = f"{role} interface check: {c.get('role_reason') or c.get('reason') or 'incomplete evidence'}"
    return result


def detail(conn, ident, before=None):
    row = conn.execute("SELECT * FROM advisory_episodes WHERE id=?", (ident,)).fetchone()
    if row is None:
        return 404, {"available": False, "reason": "not_found", "detail": "Event is not retained in this database"}
    result = summary(row)
    result["rule"] = result.pop("rule_key")
    result["first_assessment"] = json.loads(row["first_assessment_json"])
    result["latest_assessment"] = json.loads(row["latest_assessment_json"])
    result["last_evaluated_at"] = row["last_evaluated_at"]
    result["monitoring_note"] = coverage_note(result["latest_assessment"]) if result["latest_assessment"].get("state") in ("unavailable", "not_applicable") else None
    result["acknowledged"] = row["acknowledged_us"] is not None
    saved = conn.execute("SELECT * FROM advisory_evidence WHERE episode_id=?", (ident,)).fetchone()
    data = json.loads(saved["data_json"]) if saved else {}
    result["evidence"] = data
    # First warning lookup is independent of the bounded recent timeline page.
    first_warning = conn.execute("SELECT * FROM advisory_episode_events WHERE episode_id=? AND new_state='warning' ORDER BY event_us,id LIMIT 1", (ident,)).fetchone()
    result["first_warning"] = ({"at": first_warning["event_at"], "assessment": json.loads(first_warning["assessment_json"]),
                                "evidence_ref": f"event:{first_warning['id']}"} if first_warning else data.get("first_warning"))
    event_rows = conn.execute("SELECT * FROM advisory_episode_events WHERE episode_id=? AND id<? ORDER BY id DESC LIMIT 11", (ident, before or 2**63-1)).fetchall()
    result["timeline"] = [{"id": e["id"], "at": e["event_at"], "type": e["event_type"],
        "previous_state": e["previous_state"], "new_state": e["new_state"], "assessment": json.loads(e["assessment_json"]),
        "evidence_ref": f"event:{e['id']}",
        "presentation": "monitoring_note" if e["event_type"] in COVERAGE_EVENTS else "event",
        "monitoring_note": coverage_note(json.loads(e["assessment_json"])) if e["event_type"] in COVERAGE_EVENTS else None} for e in event_rows[:10]]
    result["next_event_before"] = event_rows[9]["id"] if len(event_rows) > 10 else None
    result["sample_window"] = json.loads(saved["window_json"]) if saved else []
    result["completeness"] = {
        "opening": "retained", "first_warning": "retained" if result["first_warning"] else "never_recorded",
        "checkpoints": "legacy_partial" if data.get("legacy_prefix") else "retained" if saved else "never_recorded_legacy",
        "sample_window": saved["window_state"] if saved else "never_recorded_legacy",
        "timeline": "paginated" if result["next_event_before"] else "page_complete",
        "rule_revision": "retained" if result["first_assessment"].get("rule_revision") else "never_recorded_legacy",
    }
    if saved and saved["window_state"] == "collecting":
        deadline = row["opened_us"] + WINDOW_AFTER * 1_000_000
        if int(time.time()*1e6) > deadline:
            result["completeness"]["sample_window"] = "collection_incomplete_pending_recorder"
    # Recovered checkpoints are explicitly event-only, not a fabricated full evaluation history.
    if not data.get("last_evaluable"):
        e = conn.execute("SELECT * FROM advisory_episode_events WHERE episode_id=? AND new_state IN ('watch','warning','normal') ORDER BY event_us DESC,id DESC LIMIT 1", (ident,)).fetchone()
        if e:
            data["last_evaluable"] = {"at": e["event_at"], "assessment": json.loads(e["assessment_json"]), "evidence_ref": f"event:{e['id']}", "coverage": "saved_transition_only"}
    now_us = int(time.time() * 1e6)
    result["duration"] = {"elapsed_open_seconds": max(0, ((row["resolved_us"] or now_us) - row["opened_us"]) / 1e6),
        **{k: data.get(k) for k in ("observed_abnormal_seconds", "unobserved_seconds", "evaluator_gap_seconds")},
        "coverage_since": data.get("started_at"), "legacy_prefix_unknown": not saved or data.get("legacy_prefix", False)}
    if row["status"] == "open" and saved:
        tail = max(0, (now_us - saved["last_us"]) / 1e6)
        result["duration"]["since_last_evaluation_seconds"] = tail
    result["notifications"] = [dict(n) for n in conn.execute("SELECT id,event_id,status,created_at,delivered_at,attempt_count,last_error FROM advisory_notification_outbox WHERE episode_id=? ORDER BY id DESC LIMIT 50", (ident,))]
    result["annotations"] = [{**dict(n), "links": json.loads(n["links_json"])} for n in conn.execute("SELECT id,at,kind,note,links_json FROM advisory_annotations WHERE episode_id=? ORDER BY id DESC LIMIT 50", (ident,))]
    trip = (result["first_assessment"].get("current") or {}).get("trip_id")
    result["related"] = {"trip": dict(t) if trip and (t := conn.execute("SELECT * FROM trips WHERE id=?", (trip,)).fetchone()) else None,
        "data_quality": [dict(r) for r in conn.execute("SELECT incident_id,metric,status,first_seen_at,resolved_at FROM data_quality_events WHERE first_seen_us BETWEEN ? AND ? ORDER BY first_seen_us LIMIT 25", (row["opened_us"]-300_000_000,row["opened_us"]+300_000_000))],
        "meaning": "Temporal association only; not evidence of causation. DTC and maintenance links can be recorded in owner notes."}
    recurrences = conn.execute("SELECT id,opened_at,status FROM advisory_episodes WHERE rule_key=? ORDER BY opened_us DESC LIMIT 51", (row["rule_key"],)).fetchall()
    result["recurrences"] = {"events": [dict(r) for r in recurrences[:50]], "truncated": len(recurrences)>50,
        "interpretation": "Event openings, not continuous fault duration. Different operating regimes and rule revisions may occur."}
    period_start = now_us - 30 * 86400 * 1_000_000
    trips = conn.execute("SELECT count(*),coalesce(sum(max(0,min(last_active_us,?)-max(started_us,?))),0) FROM trips WHERE last_active_us>=? AND started_us<=?", (now_us,period_start,period_start,now_us)).fetchone()
    count = conn.execute("SELECT count(*) FROM advisory_episodes WHERE rule_key=? AND opened_us>=? AND opened_us<=?", (row["rule_key"],period_start,now_us)).fetchone()[0]
    hours = trips[1] / 3_600_000_000
    result["recurrences"]["last_30_days"] = {"start_at":iso(period_start),"end_at":iso(now_us),"episode_openings":count,"recorded_trips":trips[0],"recorded_trip_hours":hours,"openings_per_recorded_trip":count/trips[0] if trips[0] else None,"openings_per_recorded_trip_hour":count/hours if hours else None,"denominator_caveat":"Recorded trip spans, not independently measured driving hours; includes watches and all revisions of this rule."}
    result["baseline_archives"] = []
    for a in [result["first_assessment"], (result["first_warning"] or {}).get("assessment",{})]:
        b = a.get("baseline") or {}
        key = b.get("input_digest")
        if key and not any(x["digest"]==key for x in result["baseline_archives"]):
            found = conn.execute("SELECT bucket_count FROM advisory_baselines WHERE digest=?",(key,)).fetchone()
            result["baseline_archives"].append({"digest":key,"status":"retained" if found else "not_retained","bucket_count":found[0] if found else None,"access":"Included in browser event export and offline event backup; bounded preview is in each assessment"})
    result["evidence_ref"] = f"episode:{ident}"
    result["revision"] = digest({k: v for k, v in result.items() if k not in ("duration", "recurrences")})
    return 200, {"available": True, "schema_version": VERSION, "event": result, "system_guide": GUIDE, "generated_at": iso(now_us)}


def replay(event, settings):
    """Bounded counterfactual threshold replay; no writes and no OEM inference."""
    allowed = {"threshold", "operator", "persistence", "max_gap_seconds", "running_only"}
    if set(settings) != allowed or settings["operator"] not in ("above", "below"):
        raise ValueError("Expected threshold, operator, persistence, max_gap_seconds, running_only")
    if (not number(settings["threshold"]) or type(settings["persistence"]) is not int
            or not 1 <= settings["persistence"] <= 60 or not number(settings["max_gap_seconds"])
            or not 1 <= settings["max_gap_seconds"] <= 60 or type(settings["running_only"]) is not bool):
        raise ValueError("Invalid replay settings")
    count, prev, points, seen = 0, None, [], set()
    reference = event.get("first_assessment", {}).get("current") or {}
    for s in event["sample_window"]:
        if any(reference.get(k) is not None and s.get(k) != reference[k] for k in ("source","quality","provenance","unit")):
            count, prev = 0, None
            continue
        key = (s.get("source"), s.get("observed_at"))
        if key in seen:
            continue
        seen.add(key)
        if not s.get("observed_at") or s.get("freshness") != "fresh":
            count, prev = 0, None
            continue
        if settings["running_only"] and not s.get("regime", "").startswith("engine_running:"):
            count, prev = 0, None
            continue
        gap = seconds(s["observed_at"], prev)
        if gap is None or gap <= 0 or gap > settings["max_gap_seconds"]:
            count = 0
        abnormal = s["value"] > settings["threshold"] if settings["operator"] == "above" else s["value"] < settings["threshold"]
        count = count + 1 if abnormal else 0
        points.append({"evidence_ref": s["evidence_ref"], "at": s["observed_at"], "value": s["value"], "count": count, "would_warn": count >= settings["persistence"]})
        prev = s["observed_at"]
    return {"available": True, "counterfactual": True, "event_revision": event["revision"], "settings": settings,
            "points": points, "coverage": event["completeness"]["sample_window"],
            "detail": "Threshold experiment on retained samples only; no retraining, original decision change, or complete-trip inference."}


def parse_route(path):
    if len(path) > 1000:
        raise ValueError("Event query is too long")
    parts = urlsplit(path)
    match = re.fullmatch(r"/v1/events(?:/([1-9][0-9]{0,17})(?:/(annotations|replay|baselines))?)?", parts.path)
    if not match:
        raise ValueError("Unknown event route")
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    if set(query) - {"before", "rule", "status", "q", "digest", "offset"} or any(len(v) != 1 for v in query.values()):
        raise ValueError("Invalid event filters")
    params = {k:v[0] for k,v in query.items()}
    if "before" in params and (not params["before"].isdigit() or not 0 < int(params["before"]) < 2**63):
        raise ValueError("Invalid cursor")
    if params.get("status", "all") not in ("all", "open", "resolved"):
        raise ValueError("Invalid event status")
    if len(params.get("q", "")) > 100 or len(params.get("rule", "")) > 200:
        raise ValueError("Filter is too long")
    if "digest" in params and not re.fullmatch(r"[0-9a-f]{64}", params["digest"]):
        raise ValueError("Invalid baseline digest")
    if "offset" in params and (not params["offset"].isdigit() or int(params["offset"]) > 100000):
        raise ValueError("Invalid baseline offset")
    return int(match[1]) if match[1] else None, match[2], params


class EventReader:
    """32-entry, one-worker cache. API calls enqueue and return immediately."""
    def __init__(self, database, related_context=None):
        self.database = database
        self.related_context = related_context
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.jobs = queue.Queue(maxsize=8)
        self.pending = set()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="telemetry-events", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=3)

    def request(self, method, path, payload=None):
        try:
            ident, action, params = parse_route(path)
            if method == "POST" and (not ident or action not in ("annotations", "replay")):
                raise ValueError("Unsupported event action")
            if method == "GET" and action not in (None, "baselines"):
                raise ValueError("Unsupported event route")
            if payload is not None and len(encode(payload)) > 4096:
                raise ValueError("Event action is too large")
            key = encode([method, path, payload])
        except (ValueError, TypeError) as exc:
            return 400, {"available": False, "detail": str(exc)}
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < (5 if method == "GET" else 300):
                return cached[1], json.loads(cached[2])
            if key not in self.pending:
                if self.jobs.full():
                    return 429, {"available": False, "detail": "Event reader is busy; retry shortly"}
                self.pending.add(key)
                self.jobs.put_nowait((key, method, ident, action, params, payload))
        return 202, {"available": False, "pending": True, "detail": "Loading saved event evidence"}

    def _run(self):
        while not self.closed.is_set():
            try:
                job = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            key, method, ident, action, params, payload = job
            try:
                mode = "rw" if action == "annotations" else "ro"
                with closing(sqlite3.connect(Path(self.database).resolve().as_uri()+f"?mode={mode}", uri=True, timeout=1)) as conn:
                    conn.row_factory = sqlite3.Row
                    if mode == "ro":
                        conn.execute("PRAGMA query_only=ON")
                    deadline = time.monotonic() + 2
                    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                    conn.execute("BEGIN" if mode == "ro" else "BEGIN IMMEDIATE")
                    status, result = self.query(conn, ident, action, params, payload)
                    conn.commit()
                if status == 200 and result.get("event") and self.related_context:
                    context = self.related_context()
                    event = result["event"]
                    at = event["opened_at"]
                    dtcs = context.get("dtcs", {})
                    related_dtcs = []
                    for group in (dtcs.get("groups") or {}).values():
                        for r in group:
                            age = seconds(r.get("last_success_at"), at)
                            if age is not None and abs(age) <= 300:
                                related_dtcs.append({k:r.get(k) for k in ("module_key","raw_dtc","fca_display","status","last_success_at","description","first_seen_at","last_seen_at")})
                    service = context.get("maintenance", {})
                    event["related"]["dtc_observations_within_five_minutes"] = related_dtcs[:25]
                    event["related"]["dtc_coverage"] = "bounded current saved-DTC cache, not a complete scan archive; no match does not mean no codes"
                    event["related"]["maintenance_records"] = [{**r,"evidence_ref": "maintenance:"+r["request_id"]} for r in service.get("oil_changes",[])[:20]]
                    event["related"]["maintenance_coverage"] = "latest 20 owner records, dates must be compared to the event; not necessarily contemporaneous"
                    result["operational_health"] = context.get("health",{})
                    event["revision"] = digest({k:v for k,v in event.items() if k not in ("revision","duration","recurrences")})
            except (ValueError, TypeError, KeyError) as exc:
                status, result = 400, {"available": False, "detail": str(exc)}
            except (sqlite3.Error, OSError):
                status, result = 503, {"available": False, "reason": "event_storage_unavailable", "detail": "Event storage unavailable or query deadline exceeded; no evidence was deleted"}
            with self.lock:
                if action == "annotations" and status == 200:
                    self.cache.clear()
                self.cache[key] = (time.monotonic(), status, encode(result))
                self.cache.move_to_end(key)
                while len(self.cache) > 32:
                    self.cache.popitem(last=False)
                self.pending.discard(key)

    @staticmethod
    def query(conn, ident, action, params, payload):
        if ident:
            if action == "baselines":
                # Digest must belong to this episode, not an arbitrary storage key.
                code, packet = detail(conn,ident)
                if code != 200:
                    return code,packet
                key = params.get("digest")
                if not any(b["digest"] == key and b["status"] == "retained" for b in packet["event"]["baseline_archives"]):
                    raise ValueError("Baseline archive is not retained for this event's opening/warning")
                row = conn.execute("SELECT inputs_zlib FROM advisory_baselines WHERE digest=?",(key,)).fetchone()
                values = json.loads(zlib.decompress(row[0]))
                offset = int(params.get("offset",0))
                return 200,{"available":True,"digest":key,"inputs":values[offset:offset+128],"total":len(values),"next_offset":offset+128 if offset+128<len(values) else None}
            if action == "annotations":
                annotate(conn, ident, payload)
            status, result = detail(conn, ident, int(params["before"]) if "before" in params else None)
            if action == "replay" and status == 200:
                return 200, replay(result["event"], payload)
            return status, result
        clauses, args = ["id<?"], [int(params.get("before", 2**63-1))]
        if params.get("status", "all") != "all":
            clauses.append("status=?")
            args.append(params["status"])
        if params.get("rule"):
            clauses.append("rule_key=?")
            args.append(params["rule"])
        if params.get("q"):
            clauses.append("(instr(lower(title),lower(?))>0 OR CAST(id AS TEXT)=?)")
            args.extend([params["q"], params["q"]])
        rows = conn.execute(f"SELECT * FROM advisory_episodes WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 26", args).fetchall()
        return 200, {"available": True, "schema_version": VERSION, "events": [summary(r) for r in rows[:25]],
            "next_before": rows[24]["id"] if len(rows)>25 else None, "system_guide": GUIDE}


def annotate(conn, ident, payload):
    if not isinstance(payload, dict) or set(payload) != {"kind", "note", "links", "request_id"}:
        raise ValueError("Expected kind, note, links, request_id")
    if payload["kind"] not in ("note", "acknowledged", "dismissed", "reopened_for_review"):
        raise ValueError("Invalid disposition; owner notes cannot declare mechanical recovery")
    if not isinstance(payload["note"], str) or not 1 <= len(payload["note"].strip()) <= 1000:
        raise ValueError("A note of 1-1000 characters is required")
    if not isinstance(payload["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", payload["request_id"]):
        raise ValueError("Invalid request ID")
    links = payload["links"]
    if not isinstance(links, list) or len(links)>10 or any(not isinstance(s,str) or not re.fullmatch(r"(?:trip|dtc|maintenance|quality|episode):[A-Za-z0-9_.:-]{1,120}",s) for s in links):
        raise ValueError("Use up to ten typed record references")
    if not conn.execute("SELECT 1 FROM advisory_episodes WHERE id=?",(ident,)).fetchone():
        raise ValueError("Event does not exist")
    prior = conn.execute("SELECT * FROM advisory_annotations WHERE request_id=?", (payload["request_id"],)).fetchone()
    if prior:
        if (prior["episode_id"],prior["kind"],prior["note"],prior["links_json"]) != (ident,payload["kind"],payload["note"],encode(links)):
            raise ValueError("Request ID already used for a different annotation")
        return
    conn.execute("INSERT INTO advisory_annotations(episode_id,request_id,at,kind,note,links_json) VALUES(?,?,?,?,?,?)",
        (ident,payload["request_id"],iso(int(time.time()*1e6)),payload["kind"],payload["note"],encode(links)))


def archive_baselines(conn, assessment, inputs):
    baselines = [assessment.get("baseline") or {}] + [c.get("baseline") or {} for c in assessment.get("corroborators", [])]
    for baseline in baselines:
        key = baseline.get("input_digest")
        if key in inputs and not conn.execute("SELECT 1 FROM advisory_baselines WHERE digest=?",(key,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO advisory_baselines(digest,inputs_zlib,bucket_count) VALUES(?,?,?)",
                         (key,zlib.compress(encode(inputs[key]).encode()),len(inputs[key])))


def finish_windows(conn, us):
    # Only pending resolved windows; open episodes are checkpointed above.
    rows = conn.execute("""SELECT e.* FROM advisory_episodes e JOIN advisory_evidence v ON v.episode_id=e.id
        WHERE e.status='resolved' AND v.window_state='collecting' ORDER BY e.id LIMIT 32""").fetchall()
    for episode in rows:
        saved = conn.execute("SELECT * FROM advisory_evidence WHERE episode_id=?", (episode["id"],)).fetchone()
        # checkpoint captures samples; restore all analytical fields afterward so
        # post-resolution collection cannot invent later evaluator observations.
        checkpoint(conn, episode, json.loads(episode["latest_assessment_json"]), us)
        conn.execute("UPDATE advisory_evidence SET data_json=?,last_us=? WHERE episode_id=?",
                     (saved["data_json"],saved["last_us"],episode["id"]))
