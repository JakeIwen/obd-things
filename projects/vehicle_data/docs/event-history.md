# Event history and evidence contract

Implemented September 21, 2026 UTC (September 20 local). Source implementation and activation are complete as of September 22, including
the monitoring, interface-health and overview-size repairs. Earlier dated
activation notes below describe historical checkpoints; the September 22 live
repair section records the current deployment. Historical motivation and selected episode 579 evidence
remain in [event-evidence-review.md](event-evidence-review.md).

## User workflow

### Presentation refinement (September 21)

Event cards now have one Event Details entry point; the card body also opens
details. Ask Codex remains inside the event dialog, not duplicated beside the
card button. Non-episode/sample-quality cards retain their existing chat path.
The dialog has a compact sticky header, subtle borderless 44px close control,
native Escape handling and title-first focus. Search/status/rule filters appear
only in the saved-event index. The detail toolbar groups All Events, Ask Codex,
and More (refresh/export), using the dashboard's muted outlined buttons.

The initial overview shows opening reading, baseline median and persistence,
clearly separates a watch from a recorded warning escalation, and labels the
latest saved evaluation independently. The sample plot and recent transitions
follow. Notes & Review, More Evidence & Tools, and How Early Warning Works are
expandable; forms have visible field labels and feedback. Review/dismissal
remains an annotation, not recovery or notification control. Supporting records
render as labeled facts, not raw JSON. How Early Warning Works has five short
topics with the complete backend-supplied guide in a nested technical reference.
Raw evidence remains available through the existing JSON export; large generic
record lists show 30 entries in the UI with explicit export guidance.

This is a static presentation/navigation refactor, not a rule, lifecycle,
storage, notification or API change. Existing annotation and replay payloads,
pagination and export bounds are preserved. Stale navigation responses cannot
replace another event, and pending buttons prevent duplicate action clicks.
Reload to pick up the UI; this refinement itself needs no service restart.
UI regression validation: 1,237 tests passed, 4 skipped, 836 subtests. Synthetic
browser checks at 800/390 pixels covered entry points, overview/guide, note save,
both empty and positive replay, full event export, list navigation, X/Escape,
and handoff to the fake advisor. No production event or real model was used.

Early Warning's **Event history** opens a searchable, paginated saved-event
index. **Event details** opens an event directly. Resolved events remain
addressable by ID. Each detail separates opening, first warning if any, last
usable assessment, latest evaluation, peaks, and lifecycle transitions. A watch
is not automatically a qualified warning. Inconclusive persistence reads **Not
evaluated**. Full observation regime and actual baseline matching criteria have
separate fields.

The sample plot preserves gaps over 15 seconds, labels the opening boundary,
and shows the retained window's coverage. Counts distinguish elapsed open time,
bounded observed abnormal intervals, unobserved intervals, and evaluator gaps.
They cover only the stated recording interval; subsequent time and legacy
prefixes are explicitly not covered. None proves continuous mechanical trouble.

**Export event JSON** includes all retained transitions (up to 200 pages with
an explicit truncation marker), retained windows, checkpoints, and complete
archived opening/first-warning baseline inputs. A separate index-page export
includes its pagination cursor. The offline command below exports every retained
event table in one consistent read transaction, including all archived baselines:

```bash
python3 tools/telemetry_event_export.py --database /var/lib/van-telemetry/history.sqlite3
```

Default output is a uniquely named JSONL file in
`tmp/vehicle_data/event-backups/`. Existing files are never overwritten. The
manifest records scope and a final completion record includes row counts and a
SHA-256 checksum of preceding lines. A partial file without that completion
record is not a complete backup. This does not install a backup schedule or
create an off-device backup. Large database work remains subject to the local
compute handoff; this command was verified on an offline test database, not run
against production during implementation.

Owner notes accept bounded typed links (`trip:`, `dtc:`, `maintenance:`,
`quality:`, `episode:`). Note, acknowledged, dismissed, and reopened-for-review
are distinct owner annotations. **These annotations neither resolve the episode
nor alter notification delivery.** The existing historian notification
acknowledgment API retains its separate behavior; the event dialog explains
this boundary. No owner annotation is treated as mechanical recovery.

Recurrence detail links same-rule events and reports 30-day opening counts per
recorded trip and recorded trip-hour. Those denominators are recorded trip
spans, not independently measured driving hours; watches and rule revisions
remain included and labeled. Related information includes the trip, nearby
sample-quality incidents, cached DTC observations with a success timestamp
within five minutes of opening, and up to 20 dated maintenance records. These
are temporal associations, not diagnoses. No matching DTC in the bounded cache
is not proof that no code existed. Owner references preserve links beyond the
bounded automatic context.

**Try a threshold against saved samples** is a counterfactual experiment with
explicit threshold, direction, distinct-observation persistence, bounded gaps,
and optional running-regime filtering. It uses the original source, unit,
quality, and provenance. It cannot retrain a baseline, change a rule, rewrite an
episode, or establish an outcome outside the retained window.

## Monitoring pauses and interrupted watches (September 21 follow-up)

Freshness expiry is routine monitoring metadata. It does not open a new
vehicle-health episode or generate a notification. The **What Happened** timeline
keeps loss/restoration of evidence and unconfirmed-watch archival in a collapsed
**Monitoring Notes** disclosure. Notes explain the actual saved age, reading,
and freshness limit when that limit was recorded. Legacy unsaved limits remain
unknown. Silence alone never establishes engine shutdown, collection failure,
recovery, or continuing abnormal temperature.

A vehicle-health episode which opened as **watch** and **never recorded a
warning** is archived **Unconfirmed · Monitoring ended** after a full original
persistence window without usable evaluation, with a minimum grace of 60 seconds
and maximum of 600 seconds. Coolant uses 60 seconds. Brief gaps remain monitoring
pauses; usable readings reset the grace. This is a monitoring policy, not a
30-second event expiration or mechanical recovery. Archived watches leave the
active/TO REVIEW list but remain searchable/exportable with opening, baseline,
transitions, and samples intact. Subsequent abnormal readings start a separate
episode. If new readings arrive after a recorded gap already exceeded its grace,
the old watch is archived before the new assessment is recorded.

Any prior recorded warning protects the episode from this automatic archive,
including a warning subsequently deescalated to watch. Infrastructure/telemetry
incidents are also excluded. Legacy open watches are handled on the next normal
historian evaluation, with the actual archival timestamp; original assessments
are not rewritten or backdated. The existing SQLite `status='resolved'` means
closed for storage compatibility; `outcome='unconfirmed'` and the durable
`unconfirmed_monitoring_ended` reason distinguish this from recovery.

`monitoring_coverage.py` independently detects an unexpected data gap for
previously observed coolant, oil-pressure, and transmission-temperature metrics.
It requires at least 60 seconds without a reading **and** 60 seconds of independent,
fresh running RPM observations (at least two distinct observations, gaps no more
than 10 seconds, latest RPM age at most 5 seconds). Its category is
`telemetry_quality`; it never enters the notification outbox or asserts a
mechanical fault. Silence without positive running evidence opens nothing.
Fresh target readings close that telemetry incident; fresh zero RPM ends its
running-only applicability. Missing RPM alone cannot claim recovery.

Both the agent's canonical guide and its instructions describe these distinctions.
The agent should explain an archived coolant watch as a brief recorded deviation
that never qualified as a warning before monitoring ended, not unresolved
mechanical trouble. New chats obtain the current outcome; saved conversations
retain their original dated evidence until explicitly refreshed.

## Evidence and retention

`event_history.py:GUIDE` is the versioned application guide delivered to both
the UI and in-app agent. Historian writes remain atomic with episode events and
the notification outbox:

- `advisory_episodes`: immutable first assessment, evolving latest assessment.
- `advisory_episode_events`: immutable lifecycle/context snapshots; not every
  evaluation. Opening and warning escalation have separate snapshots.
- `advisory_evidence`: last usable and peak assessments, distinct sample counts,
  interval coverage, recovery progress, and copied sample window.
- `advisory_baselines`: complete contributing minute-bucket records, compressed
  and deduplicated by content hash. Assessments include a bounded 128-bucket
  preview with completeness flag and archive digest; the full inputs survive.
- `advisory_annotations`: append-only idempotent owner notes and typed references.

Relative and owner-defined rules retain their exact configuration/hash and
explicit evaluator revision. Absolute oil rules also freeze their configuration.
Each current metric snapshot retains its decoder/source provenance hash,
measurement and capture timestamps, evaluation time, capture freshness, and
separate evaluation validity. Qualifying relative-rule persistence observations
are saved with their values/times/IDs. New first-warning and peak snapshots
remain independent of later missing data or changed baselines.

The copied primary-metric window requests 120 seconds before and 180 seconds
after opening, capped at 256 samples. Collection continues after an episode
resolves; missing data remains missing. A 128 MiB global window budget prunes
oldest copied windows in bounded batches and leaves `pruned_budget` tombstones.
Partial, truncated, interrupted, legacy-never-recorded, and retained evidence
have explicit states. Compact event evidence and baseline archives have no
automatic age deletion. Ordinary raw metric/interface retention remains seven
days by default with the existing rollup-before-prune gate. Storage exceptions
remain visible and are never reinterpreted as absence of events.

Legacy opening/event assessments are exposed as saved. When newer checkpoints
were never recorded, last evaluable evidence is labeled **saved transition
only**, peaks/duration are unknown, and rule revision is legacy-missing. No
backfill invents a former peak, persistence run, baseline, or warning escalation.

## Relative-rule applicability and recovery

Coolant v2 requires five minutes of continuous fresh running RPM evidence and
excludes the first five minutes of each prior trip from baseline training.
Engine-off and early-running comparisons are not applicable. These durations
are explicit monitoring choices, not OEM limits. Thermal state remains outside
coolant baseline matching to avoid conditioning an anomaly test on its own
measured temperature. Oil/transmission and other rule dimensions remain explicit.

A normal relative assessment can resolve an existing episode only with matching
baseline regime and metric source/quality/provenance/unit, and three distinct
normal observations inside a 20% hysteresis margin of the relative threshold.
Gaps reset recovery progress. A changed regime produces `not_applicable`; valid
normal evidence awaiting persistence produces `recovering`. Confirmed warnings
remain unresolved in these states. A never-warning watch may instead archive
unconfirmed under the monitoring policy above. Changed versioned rules close the old episode administratively with a
`rule_replaced` event and cancel its pending deliveries; an active result under
the new revision opens a separate episode. Rule removal remains `rule_retired`.
Neither administrative closure establishes vehicle recovery. Original legacy
closures are never rewritten. Absolute critical-oil recovery semantics are
unchanged.

## API, latency, and agent context

`GET /v1/events` accepts bounded `q`, `rule`, `status`, and `before` filters.
`GET /v1/events/<id>` returns detail with ten timeline transitions per page;
`before` continues older transitions. `GET /v1/events/<id>/baselines` accepts an
associated `digest` and `offset` for 128 input buckets per page.
`POST /v1/events/<id>/annotations` records human notes; the separate `/replay`
POST computes a read-only counterfactual. Web POSTs require same-origin JSON,
strict request bounds, and server validation. GETs and replay never acquire CAN.

The serialized Unix API only queues a job or copies a cached result, returning
202 while loading. One bounded worker uses an independent query-only SQLite
connection for reads/replay, with a two-second query deadline, eight queued
jobs, and 32 cached responses. Annotation writes use a separate bounded
transaction. Connections are closed after each job. Live snapshots never wait
for an event SQL query. The web and advisor retry pending requests briefly.
Related DTC/maintenance and operational health come from existing memory caches.

The advisor loads the same versioned event detail, not merely the active-card
summary. It receives the system guide, opening/first-warning/checkpoint evidence,
completeness, and stable evidence references. It distinguishes facts, calculations,
hypotheses, and counterfactuals. Baseline input arrays are omitted from prompts
with explicit archive references; oversized windows/timeline pages are marked
as omitted, not lost. Each new turn records the event revision and evidence date.
**Refresh Evidence & New Chat** preserves the prior transcript and original
packet while opening a new dated explanation. No shell, filesystem, CAN,
service-control, or additional model tools were enabled.

Prompt/context separation follows the official
[OpenAI prompting guidance](https://developers.openai.com/api/docs/guides/prompt-engineering).
The application-specific guide and tests define these telemetry semantics.

## Validation and activation

Portable full regression job `20260921T021136Z-a1905ac8`: **1,233 passed,
4 skipped, 836 subtests**. Tests cover the episode-579 opening facts, stale data,
restart, first-warning separation, duplicates, recovery regime/gaps/hysteresis,
raw deletion/window pruning, transactional rollback, baseline input archives and
early-trip exclusion, pagination, annotations, bounded worker behavior, replay,
backup, rule replacement, and the agent's evidence contract. Browser verification
uses `tests/fixtures/event_history_preview.py` (synthetic data and fake advisor).
No real model reply or live event mutation was used as a test.
Browser checks passed at 900px and 390px: event lookup, saved opening/current
separation, owner note persistence, JSON export (including opening baseline and
timeline), bounded replay, and event-to-fake-advisor conversation. The final
phone screenshot is `tmp/playwright/event-evidence-final-390.png`; exported
fixture evidence is `tmp/playwright/event-evidence/telemetry-event-1.json`.
The fixture advisor confirms the UI/context integration, not production model
answer quality. A real reply remains part of post-activation verification.

Initial implementation inspection at 2026-09-21 02:05 UTC showed the
broker/web/advisor active, TPMS fallback inactive, vehicle inferred asleep, and
both active helpers idle. The session cannot use sudo because its no-new-
privileges restriction blocks elevation. No production database migration or
service restart was performed.

From an owner host shell, after freshly confirming the vehicle is parked and
both helpers idle, restart the broker first, then its web listeners and advisor:

```bash
sudo systemctl restart van-telemetry.service
sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service van-telemetry-advisor.service
```

Then reload the dashboard, open Event 579, verify its watch opening at 215.6°F
against 190.4°F with 1/10 persistence, and start a fresh explanation. Check
service health, advancing history, and passive role/helper state. These commands
activate the code in the existing units; no unit installation or cron change is
needed. Old evidence remains intact; the schema additions are additive.


Follow-up verification: `20260921T073825Z-5d5ef1b4` passed **1,246 tests,
4 skips, and 840 subtests**. Added cases cover quiet-note rendering and age/limit
wording, interrupted-watch archival, legacy evidence retention, restored-reading
grace, new episodes after interrupted watches, protection of deescalated confirmed
warnings, and positive-RPM-only telemetry gap classification. The UI styling
work in `.agent/handoffs/event-history-ui.md` is preserved.

To activate only this follow-up, use a host shell while parked and with active
helpers idle, then reload the dashboard:

```bash
sudo systemctl restart van-telemetry.service van-telemetry-advisor.service
```

The event viewer is served from current static files; this follow-up adds no web
routes or service units. Use a new/refreshed explanation to pick up the new guide.


Final follow-up checks: `20260921T074143Z-9f22f8c5` passed 32 focused tests
and 5 subtests, including rejection of future-dated running evidence. Browser
checks at 900px/390px verified that the archived watch is absent from TO REVIEW,
the confirmed missing-data warning remains, and coverage changes are collapsed
by default. Expanded notes show the saved 11.8-second reading age and 10-second
limit. Screenshots: `tmp/playwright/monitoring-unconfirmed-{900,390}.png` and
`tmp/playwright/monitoring-notes-390.png`. Fixtures and browser were stopped.

## Interface-health event audit (September 21, 09:52 UTC)

Owner asked why event history contains many “interface role is unhealthy” rows.
A bounded read-only inspection of the newest 60 episodes, selected event timelines,
current cached broker status, and the broker's systemd start records found:

- IDs 582–593 are four groups of three startup watches, one for each vehicle
  role. Every group begins roughly three seconds after a recorded broker start
  (September 20 20:22:53/23:34:05 UTC; September 21 06:30:27/09:22:07 UTC).
  Their opening reason is `logical role is absent from current interface status`,
  not a recorded controller error. All close with healthy role evidence and none
  has a recorded warning escalation. Three groups close after approximately five
  seconds; IDs 588–590 close after approximately 260 seconds. That elapsed span
  is not proof of a physical outage.
- Older B-CAN examples 536, 542, 544, 552, 564, and 570 open as `topology_unusable`
  while the saved controller is ERROR-ACTIVE and the role explanation explicitly
  identifies the broker's auxiliary-drive owner arming B-CAN for fixed ICS polling.
  ID 570 escalates on its second observation and remains recorded until passive
  mode returns. Source inspection identifies a mismatch: broker status sets B-CAN
  `passive_ready=false` and `operating_mode=armed_diagnostic` without an explicit
  `topology_usable` override; historian normalization falls back to passive_ready,
  then its health classifier rejects topology before handling armed mode.
- ID 581 is a different C-CAN case, with `one_shot_not_disabled`; it must not be
  blanket-dismissed as the startup/B-CAN issue without separate verification.
- At inspection all three vehicle roles are resolved, UP, exact listen-only,
  ERROR-ACTIVE and reported safe, with no active inhibit. Current status alone
  cannot rule out unrelated historical hardware failures.

Provenance: `InfrastructureHealthEvaluator.evaluate`, broker active/auxiliary
status overlays, `TelemetryHistorian._interface_payloads/_interface_health`,
query-only production advisory rows, and bounded service-start journal reads.
This audit changed documentation only. Follow-up should distinguish initialization
from a proven missing role, correctly represent verified active B-CAN ownership,
and use cause-specific titles. Do not suppress genuine USB removal, controller,
identity, or restoration errors or blindly mark every armed channel healthy.

## Interface-health and toolbar fixes (September 21 implementation)

The preceding interface audit is now remediated in source:

- Broker status includes a process-scoped `interface_probe` record. The historian
  persists it alongside each snapshot. Before the first probe, absent/incomplete
  role status is unavailable initialization evidence and cannot open three role
  failure episodes. If a first probe remains pending for 30 seconds, or a probe
  fails, one cause-specific discovery/status advisory is emitted. This does not
  suppress an explicitly observed missing/ambiguous identity, controller failure,
  down link, USB removal, or restoration inhibit. Initialization after restart
  cannot resolve a previously recorded real fault.
- The validated B-CAN helper's armed status captures the already-created
  supervisor delegate's channel/serial/dev_id route, without doing discovery from
  an HTTP handler. An active-mode health override requires enabled ownership,
  the running-gate status, a positive helper PID, exact matching route and role
  identity, UP/present 125 kbit/s classical CAN, ONE-SHOT off, restart-ms zero,
  ERROR-ACTIVE, and no inhibit/restoration failure. Only `ready` or
  `interface_armed` role causes qualify. Verified ownership sets
  `topology_usable=true` while retaining `passive_ready=false`; missing proof
  remains unhealthy. This affects reporting only, not CAN authority or controls.
- Titles describe missing status, missing/ambiguous adapters, configuration
  mismatch, down links, or controller errors. Controller errors remain immediately
  actionable even when accompanied by another health-check failure. Legacy
  generic titles are clarified for display using saved evidence; `original_title`,
  opening assessments and historical transitions preserve the original record.
  A legacy status flagged during polling is not silently deleted or relabeled a
  proven healthy interval.
- The in-app agent's system guide explains initialization, valid active ownership,
  genuine faults, and legacy display titles. Existing conversations still require
  an explicit evidence refresh to use a new packet.

The event-index status paragraph now has an ID-qualified margin rule, fixing the
more-specific general dialog-paragraph selector that had removed its side inset.
The upper More disclosure is removed. All Events and Ask Codex remain on the left;
Refresh Evidence and Export Event JSON are visible on the same row, right-aligned.
At phone widths their labels wrap within the buttons while all four controls
remain in one row with no horizontal overflow.

Validation: full job `20260921T100651Z-b854e27b` passed **1,258 tests, 4 skips,
847 subtests**. New tests cover initialization, discovery timeout/probe failure,
true missing/controller faults, prior-fault preservation across initialization,
exact active-owner identity/health checks, and unchanged legacy evidence.
Browser checks at 900/390 pixels verified the status inset, four controls sharing
a row, right alignment at desktop width, and no overflow. Screenshots:
`tmp/playwright/interface-history-list-900.png` and
`tmp/playwright/interface-history-actions-{900,390}.png`.

Formatting is served on reload. Broker/advisor activation requires the same
parked, helpers-idle host-shell restart documented above; this managed session
cannot elevate with sudo. No service was restarted and no live CAN path was
exercised for this change.


## Overview size regression and live repair (September 22)

The Early Warning card's `/v1/health` response grew to **1,334,011 bytes**, beyond
its unchanged 1 MiB client limit. The 25 recent episodes repeated complete
baseline bucket previews and persistence sample arrays in opening/latest
assessments (episodes accounted for about 1.26 MB). The web proxy returned 503;
the UI repeated the error and incorrectly treated missing notification metadata
as disabled delivery, although live delivery was enabled.

`health_summary.py` now projects the background health overview without those
bulk arrays, marking omissions explicitly. Values, median/MAD, deviations,
thresholds, source/time provenance, persistence counts and delivery/outbox state
remain. Historian rows, per-event detail and export are unchanged. A 512 KiB
serialized overview budget leaves room beneath the 1 MiB transport bound.
Secondary recent history or duplicate USB detail can be explicitly omitted if
necessary; unexpectedly oversized essential data fails as unavailable with a
small actionable response, never an empty/all-clear warning list. GET remains
memory-only and the transport limit was not increased.

On failure the card now shows the error once and says status could not be
loaded. It reports delivery disabled only when `enabled` is explicitly false.
The in-app guide explains the compact projection and distinction between
omission and deletion.

Validation: focused job `20260922T151353Z-3e0325e9` passed 35 tests and 12 subtests;
full job `20260922T151518Z-bf3a4c20` passed **1,262 tests, 4 skips, 851 subtests**.
Regression cases exceed the former wire limit, retain decision facts and original
stored evidence, exercise explicit budget omissions/failure, and prevent the
misleading delivery-disabled/error-duplication messages.

Deployed September 22 from this now-privileged session after fresh asleep,
helpers-idle, exact-passive-role and inactive-TPMS-fallback checks. Broker restart
also restarted the two web listeners through their existing PartOf dependencies;
the advisor was restarted after confirming no queued/running saved chat turn.
All four services are active. Live LAN health now returns **HTTP 200 / 301,123
bytes**, available=true and notification_delivery.enabled=true. The ordinary
bounded Unix client also succeeds. Event 638's full detail still carries 128
baseline input buckets. Historian is advancing without error; all vehicle roles
remain passive/ERROR-ACTIVE, no inhibits, and every channel's TX counter was
unchanged. Evidence logs are private under `tmp/vehicle_data/response-audit/`.
This deployment also activates the preceding interface-health/monitoring changes;
older activation-pending notes above are historical context.


The final live browser check passed: **2 TO REVIEW**, delivery enabled, zero
pending/failed deliveries, and neither duplicated size-limit error nor the
incorrect disabled-delivery statement. Screenshot:
`tmp/playwright/health-size-live-fixed.png`. The read-only browser was closed
at completion.
