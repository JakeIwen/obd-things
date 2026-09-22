# Early Warning event evidence review and proposal

Review date: 2026-09-21 UTC (September 20 local). This document preserves the
original review. The subsequent implementation and activation status are in
[event-history.md](event-history.md).

## Verified findings

Source inspection: `historian.py` schema and `record_advisory_assessments`,
`_advisory_episode_dict`, `list_advisory_events`, and `advisory_summary`;
`early_warning.py` relative coolant rule; `static/app.js` warning rendering;
`warning_chat.py:event_context`.

- The historian stores an immutable `first_assessment_json` at episode opening
  and complete assessments on lifecycle/context events. It overwrites only the
  episode's latest assessment on subsequent evaluations. This is not a complete
  journal of every evaluation.
- The public episode serializer omits the first assessment. The health summary
  omits event timelines. Cards render the latest assessment; chat receives the
  episode summary and current cached context. This hides existing evidence.
- Episodes can open as watch before persistence qualifies a warning. Opening
  evidence and first warning evidence are different milestones.
- No advisory episode/event deletion path was found in the project Python code.
  Historian raw numeric/interface retention defaults to seven days; it does not
  imply seven-day event retention. API list limits are not deletion policies.
- Unavailable evidence leaves an episode open and overwrites its latest
  assessment with null baseline/deviation and zero persistence. A stored sample's
  `freshness: fresh` label can coexist with a much older effective age: capture
  freshness and evaluation validity need distinct names.

A bounded, query-only read of the live historian at
`/var/lib/van-telemetry/history.sqlite3` inspected the latest 25 episodes and at
most eight chronologically ordered events per matching coolant episode. It
verified the following selected evidence; no full VIN or raw capture is promoted.

Episode **579**, rule `engine_coolant_temperature_relative_high`:

- Opened `2026-09-18T00:33:13.564204+00:00` as **watch**.
- Opening observation `00:33:12.926581+00:00`: **215.6 °F**, source
  `ccan.broadcast.0x2ed`, age 0.641 s, trip 49.
- Baseline median **190.4 °F**, MAD **3.6 °F**, robust sigma **5.33736 °F**;
  446 minute buckets, 2,601 samples, 48 prior trips.
- Deviation **+25.2 °F**; required deviation **24.01812 °F**. Thus the opening
  rule boundary was approximately **214.42 °F**, not an OEM temperature limit.
- Persistence **1/10**, within a 60 s window; no corroborator required.
- The two returned events were `opened` and `evidence_inconclusive` at
  `00:33:40.722574+00:00`; no warning escalation is recorded.
- The latter references **219.2 °F** observed at `00:33:28.926483+00:00`, already
  11.799 s old at that evaluation. The episode remained open at inspection.
- Full observation regime includes `warm`, but baseline matching uses only
  engine-running, stationary, and idle RPM. It excludes thermal state; baseline
  minimum was 80.6 °F. Calling this a warm-only baseline is inaccurate.

Adjacent episode **568** supplies a further review case: it opened with an
engine-off warm observation of 208.4 °F against a 73.4 °F engine-off median,
then resolved on the next trip with a cold, engine-running low-RPM normal
assessment. That establishes current cross-regime resolution behavior, not
recovery under the original conditions or a mechanical fault.

## Proposed implementation order

1. **Expose existing evidence.** Add opening assessment, first warning milestone
   if present, last evaluable assessment, and bounded lifecycle summaries to
   event detail. Preserve historical state and the latest evaluation separately.
   Support stable lookup of resolved events, pagination, and evidence export.
   Keep SQLite work outside the serialized live snapshot handler; use the
   existing background cache or a separate bounded read worker.
2. **Make the event readable.** Show why it opened, whether it ever qualified as
   a warning, the last evaluated deviation, and what can be assessed now.
   Display actual baseline matching dimensions separately from full observation
   regime. Label unavailable persistence as not evaluated, not 0/N. Separate
   elapsed open time, observed abnormal duration, and unobserved time.
3. **Complete the evidence record.** Freeze rule definition/revision, evaluator
   and decoder versions, actual baseline statistics and contributing bucket IDs,
   qualifying sample values/times, and freshness/quality decisions at opening,
   first warning, material escalation, and recovery. Retain last evaluable and
   peak value/deviation with context. Save a bounded before/after sample window
   independently of ordinary raw retention; distinguish missing post-event data.
   References alone are insufficient if referenced samples will be pruned.
4. **Improve lifecycle semantics.** Keep acknowledged, dismissed, rule retired,
   no fresh evidence, condition no longer applicable, and observed recovery
   distinct. Review recovery under comparable operating conditions and require
   suitable persistence/hysteresis. Link recurrences across trips without counting
   parked time as continuous abnormal operation. Review engine-off and warm-up
   baseline eligibility using replay; do not blindly condition coolant detection
   on the same temperature being tested, which could mask anomalies.
5. **Make analysis evidence-addressable.** Give chat the same versioned event
   detail as the UI, with event/sample references, retention/completeness flags,
   and explicit capture/evaluation times. Preserve evidence used by prior answers;
   offer explicit refresh for current state. Distinguish recorded facts,
   deterministic calculations, hypotheses, and unavailable evidence. Rule replay
   must be labeled counterfactual and never rewrite original decisions.
6. **Define retention and operational health.** Retain compact event evidence
   long term, with an explicit policy and backup/export; reserve bounded disk
   space for high-resolution windows. Expose storage failures, pruning coverage,
   evaluator downtime, and notification disposition per event. Preserve atomic
   episode/event/outbox writes and idempotent retry semantics. Link trips, data
   quality incidents, DTC observations, and owner maintenance notes by ID/time
   while preserving their separate meanings.

## Acceptance cases

- Episode 579 explains its 215.6 °F opening, 190.4 °F median, +25.2 °F
  deviation, 1/10 persistence, lack of escalation, and later stale evidence.
- A watch-to-warning event exposes both milestones with their own baselines.
- Restart, raw-sample pruning, or changed rule configuration cannot alter the
  saved explanation. Missing legacy evidence is explicitly marked, never invented.
- No fresh data cannot imply recovery or continuous abnormal duration. A new
  regime cannot silently establish recovery in the original regime.
- UI and chat agree on the same event revision, units, timestamps, completeness,
  and baseline dimensions. Event browsing remains cache/storage-only.

No tests were run: this review used source inspection and bounded read-only
database queries. Implementation and regression testing remain future work.
