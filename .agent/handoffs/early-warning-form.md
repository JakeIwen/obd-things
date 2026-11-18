# Early-warning form (Add Early Warning redesign)

tier: T3 (custom-rule evaluator semantics in the broker, a new historian query, a new advisor write
route that installs monitoring rules without a model proposal, a change to the web relay
allowlist, and a deploy precondition on the owner's rules file)
last reviewed: plan round 4 by gpt-6-astra (ship); Phase 1 implementation not yet reviewed
plan written against: commit `ce71087` (master `0393eee` plus four early-warning commits of
2026-11-17). Master has since gained the same content, a small review fix and unrelated
dashboard commits; none touches the evaluator, historian or advisor logic below (re-checked
against master `9209c9e` on 2026-11-17).

## Objective

Make "Add early warning" on the dashboard's Health view a plain form that saves a custom
threshold rule with no internet and no model turn, move the model to optional help inside that
form, and first make custom rules confirm correctly, which today they mostly do not. "Ask Codex"
on a warning card (explaining a recorded event) is unchanged.

## Owner's vehicle today

- 2026-11-17, 5:53 pm Mountain: the broker's `/v1/health` reported `custom_rules` count 0, error
  null. There are no saved custom rules on this vehicle. The owner has been told the feature does
  not fire reliably and is not relying on it.
- One owner, one vehicle, two paired browsers (a Mac and the driving tablet). There is no
  multi-tenant concern; the machinery below is sized for that.

## What works today (for the owner)

Verified on 2026-11-17 by in-memory probes against the real historian (`TelemetryHistorian`
over a temporary SQLite file, snapshots ingested through `ingest_snapshot` as the broker does,
rules evaluated with `custom_warnings.evaluate_rule` at each capture time). The broker's history
loop waits `--history-interval` (5 s in the tracked unit) after each snapshot's work, so on the
van recordings land about every 6 s (p50) and up to 8 s (p90) per
`docs/history/early-warning-rationale.md`. That spacing is a deployment setting, not a historian
invariant: the broker accepts any positive interval, the historian only requires increasing
capture times (probe: seven fresh recordings 1 s apart were all stored, and a new broker instance
recording 0.5 s after the previous one's last snapshot was accepted). Broadcast values are
already 0 to about 4 s old when recorded.

- **"Only while the engine is running" depends on timestamps.** Each reading looks up RPM at its
  own observation time, but that lookup only sees snapshots captured before that moment, so it
  gets an earlier snapshot's RPM, which must still be under 5 s old. Probes, battery rule (gap
  10 s), seven readings 5.2 s apart, RPM observed 0.05 s before capture: metric observed 0.1 s
  before capture, 0/7; observed 2 s before capture, 6/7 (the oldest reading has no earlier
  snapshot), and 7/7 when one earlier snapshot exists (round-2 reviewer's probe).
- **Rules on battery voltage, the four tire pressures and VVT oil temperature, with the engine
  option off, do confirm** (their gap may be up to 30 s or 15 s). Probe: seven battery readings
  5.2 s apart, 7/7, warning. Two defects remain: a normal reading recorded in a different
  driving condition (another RPM band, speed band or temperature band) is skipped instead of
  breaking the run, so the warning can confirm across it; and a value the source repeats from
  cache can break the run.
- **Rules on the other eleven catalog metrics** (gap at most 5 s, or 4 s for power, torque and
  field duty) **rarely confirm**: the gap is measured between observation times, and two readings
  recorded 5.2 s apart are observed less than 5 s apart only when the newer one was older at
  recording than the one before it. Probes: coolant, 7 readings 5.2 s apart, 1/7; field duty,
  gap 4 s, two recordings 5.2 s apart observed 1.5 s and 2.9 s before capture (older, newer):
  warning, 2/2.
- A count of readings promises no duration: the same probe's seven field-duty recordings 1 s apart
  satisfy a seven-reading rule within 6 s.
- A rule never warns on a reading older than its metric's stale limit today, because the gap is
  capped at that limit.
- Owner-made rules are listed and removable only on the browser that made them, and one saved
  rule that the running broker rejects stops every custom rule.

## The owner's request (2026-11-17, confirmed by him)

He tried Add Early Warning, was refused (metric not in the catalog; rule shape unsupported),
could not see what he may monitor, and asked: "If rule shapes are predefined, is there even a
purpose for using an agent?" He approved this direction:

- Add Early Warning becomes a form: pick a metric from a visible list, choose above or below
  (or absolute above), enter the number and how long it must persist, save. It works with no
  internet and no model turn.
- The agent becomes optional help inside the form ("suggest a threshold from my history",
  "explain this metric"), given his recorded ranges. The existing setup chat may be kept as
  that help path.
- Out of scope: new rule shapes (a "share of a drive above threshold" rule was deferred),
  built-in warnings, and the access-code gate (a parallel change adds a pairing PIN; see "Where
  this meets the access gate").

## Current facts the design rests on

Each was checked against `ce71087` and still holds on `9209c9e`.

- A rule is exactly eight fields (`custom_warnings.FIELDS`): `title, metric, operator,
  threshold, persistence_observations, window_seconds, max_age_seconds, engine_running`.
  `validate_rule` limits: title 1–120 chars; metric in `catalog()`; operator `above`, `below`,
  `absolute_above`; threshold ±1,000,000; persistence integer 2–60; window 5–600 s; max age
  1..min(60, the metric's `stale_after_seconds`) and ≤ window; `engine_running` bool; absolute
  threshold ≥ 0. Field names are a file contract.
- `catalog()` = numeric metrics with a `verified` or `observed_alfa_scale` source: 17 metrics.
  Stale limits: 4 s (crankshaft power, crankshaft torque, field duty), 5 s (coolant, oil
  pressure, rpm, target torque, transmission oil temperature, output and turbine speed, vehicle
  speed), 15 s (VVT oil temperature), 30 s (battery voltage, four tire pressures). Battery
  voltage has three qualified sources (B-CAN `0x46C`, C-CAN `0x41A`, cluster DID `1004`).
- Rules live in `/var/lib/van-telemetry-advisor/early-warnings.json` (`{"version":1,"rules":[…]}`,
  each row `{id (64 hex), rule, approved_at, chat_id, owner}`), written only by the advisor with
  `engine_off_voltage._atomic_json` (unique `mkstemp` temp file, fsync, `os.replace`; no
  parent-directory fsync), at most 32 rules; readers refuse a file over 128 KiB. `load_rules`
  ignores top-level keys other than `version`/`rules` and row keys other than `id`/`rule`.
- The broker (`warning_engine/evaluator.py`, custom block near line 498) reloads the file on
  every evaluation with `load_rules`, which validates every row: one row that fails validation
  makes every custom rule stop and sets `custom_rules.error` (built-ins carry on).
- Evaluation runs inside `insights.ingest_snapshot` right after each snapshot is stored, at
  `at = captured_at` (`broker.py` history loop; the loop waits `history_interval_seconds` after
  the work). It runs once per stored snapshot, in capture order, on the history thread; a
  duplicate or a refused snapshot is not evaluated, and an exception in the evaluation is caught
  in `_evaluate_and_persist_advisories`. `/v1/health` also calls `evaluate()` (at the current
  time) from request threads. The configured interval is served at runtime in `/v1/status`
  `history_recorder.interval_seconds`.
- Clocks: `captured_at` and every `observed_at` are wall-clock times; the broker also holds
  `self.monotonic` (`time.monotonic`). The historian refuses a snapshot captured at or before its
  newest one (`OutOfOrderSnapshotError`). The Pi has no battery clock: `fake-hwclock` restores
  the last saved time at boot (saved hourly by cron and at shutdown) and `systemd-timesyncd`
  (default `timesyncd.conf`) steps the clock when the network answers; the broker unit is
  ordered only after `local-fs.target`, so it records before that step. timesyncd steps offsets
  above about 0.4 s and slews smaller ones (systemd's `NTP_MAX_ADJUST`; from the source, not
  measured here), so after an offline stretch of the hotspot a step of a few seconds either way
  is a normal event. A backward step makes the historian refuse captures until the wall clock
  passes its newest one. The dashboard stream already ages data on monotonic clocks for this
  reason (vehicle-data README, "Telemetry dashboard").
- History storage: `snapshots` (one row per capture, `captured_us` unique, `trip_id`, regime),
  `metric_samples` (at most one row per snapshot and metric, `UNIQUE(snapshot_id, metric)`, with
  `observed_us`, `source_age_ms`, `freshness` decided at capture from the broker's `age_ms` and
  `stale`), and `metric_gaps`. A metric the broker reports unavailable gets no `metric_samples`
  row in that snapshot.
- Today's custom evaluation (`custom_warnings.evaluate_rule`): latest row by `latest_sample(at,
  fresh_only=False)`, accepted when fresh at capture and observed ≤ `max_age_seconds` before
  `at`; run from `recent_numeric_samples` (filtered to the latest reading's regime, source,
  quality, provenance and trip, deduplicated by `observed_us`, ordered by observation time),
  broken when the observation spacing exceeds `max_age_seconds`; engine running per reading via
  `latest_sample("engine.rpm", at=<the reading's observed_at>)`, which filters by capture time.
- Built-ins (`warning_engine/`) use effective age = recorded `age_ms` + time since capture
  (`samples._sample_age_seconds`) and RPM running intervals from
  `continuous_numeric_condition`.
- Episodes: the lifecycle closes an open episode administratively when its rule key leaves the
  assessment list (`advisories._retire_absent_advisories_locked`, "rule retired from
  authoritative evaluator catalog") or its `rule_revision` changes.
- Recorded ranges already in the dashboard store: `summary.history.metric_trends[metric]` for
  the 11 metrics in `insights.DEFAULT_HISTORY_METRICS` (battery, oil pressure, coolant, VVT oil,
  crankshaft power, transmission oil, field duty, four tires), each with `days_7` and `days_30`
  (`minimum`, `mean`, `maximum`, `sample_count`; parked and running together, from minute
  rollups) and `prior_trips` (`median_of_trip_means`, `mad_of_trip_means`, `trip_count`).
  Missing: rpm, speed, torque, target torque, output and turbine speed; percentiles; a
  running-only split.
- The broker serves its catalog: `GET /v1/metrics` and `catalog` inside `/v1/snapshot`;
  `/v1/health` carries the evaluator's `custom_rules` status.
- Relay: `web.py::_assistant_request` allows a fixed list of `/v1/assistant/` paths (plus
  `dtc_research_contract.relay_allowed`), requires an exact trusted origin, same-origin fetch
  metadata, JSON POSTs, and well-formed `X-Van-Assistant-Key` / `X-Van-Chat-Client` headers. The
  advisor (`ChatHandler.handle_request`) checks the access code in `manager.authorize` before any
  route, derives `owner = sha256(client id)`, and runs warning routes under `manager.lock`.
- Chat records: the advisor loads every `chat-*.json` at start and keeps turn keys it does not
  know; `open_chat` returns the browser's newest chat for the event. `apply_warning` (and
  `remove-warning`) act only on the latest turn's `proposal` and `proposal_id`, otherwise 409
  "Review the latest completed proposal before applying it"; the dashboard shows a proposal only
  from `turn.proposal`.
- Chat install (`WarningChatManager.apply_warning`): exact body `{proposal_id, confirmed}`
  (`assistant_common.require_keys` refuses extra keys), exact latest completed proposal,
  `confirmed: true`; strict `load_rules`; row id = `proposal_id` (a digest of chat id, turn count
  and proposal); rewrites the file as `{"version":1,"rules":rows}`, dropping any other top-level
  key. The dashboard's `proposalText` describes a proposal as "N distinct observations within W s;
  sample age/gap ≤ G s".
- The Add early warning button (`views/Health.jsx`) shows only when the web listener advertises
  `warning_chat_enabled`, and opens `WarningChat.jsx` in `add-warning` mode, which immediately
  starts a model turn.

## History findings (owner rule: redesigns start from the commit history)

| Piece | Added in | Stated reason | Still holds? | Plan |
| --- | --- | --- | --- | --- |
| Setup chat, proposal ID approval, `apply-warning`/`remove-warning`, rules file, 32 cap | `84d95f3` (2026-09-20), ported to Preact in `2fe75a4` | Only the latest completed, validated proposal can be installed, with its exact ID and explicit approval | Yes for chat-made rules; the form keeps it in a stronger form (the owner composes the exact fields; the model installs nothing) | Chat install goes through the common store in Phase 2; deleted in Phase 4 |
| Rules listed and removable only by the creating browser | `84d95f3` | None for rules (the privacy reason is for transcripts) | No: every rule fires vehicle-wide but each browser sees a different list | Rules become vehicle-wide; transcripts stay per browser |
| `max_age_seconds` ≤ the metric's stale limit | `84d95f3` | None stated | It does two jobs: keeps the latest reading current and bounds spacing. The second makes most rules unreachable | Phase 1: the latest reading is bound by the metric's stale limit (fixed); the field becomes the longest time between recordings |
| Persistence as a count of readings | `84d95f3` | "N distinct observations" | No: a count promises no duration at an unenforced cadence | Phase 1: the hold is a time (`window_seconds`); the count is a floor of separate readings, settable in the form |
| Run built from `recent_numeric_samples` (regime-filtered) | `84d95f3` | Reused the tier-1 query, which conditions on regime by design | No for owner thresholds: a filtered-out normal reading vanishes | Phase 1: per-recording run on the broker's steady clock, no regime filter |
| RPM looked up at each reading's `observed_at` | `84d95f3` | "Engine running at every reading" | Intent yes, lookup no (filters by capture time) | Phase 1: RPM from the same snapshot |
| Catalog limited to verified / `observed_alfa_scale` numeric metrics | `84d95f3` | Owner rules only on trusted decodes | Yes | Keep |
| Field duty allowed | `43d5d37` | An owner-defined rule is an owner reference, not alternator evidence | Yes | Keep; the form shows the field-duty note |
| "What you can monitor", `catalog_summary`, `RULE_READINGS` | `78d510b`, `ce71087` | The owner could not see what he may monitor | Superseded by the form's metric picker | Delete in Phase 4 |
| Setup waiting wording | `7f2c5ce` | The setup chat has no event | Yes while the help chat exists | Keep for the help panel |
| Hold an open custom warning through a short run (`_open_warning`) | `2fe75a4` | A confirmed warning should not close and reopen on a gap | Yes | Unchanged |
| Access-code helpers shared by both dialogs | `5478077` | One copy for warning chat and DTC research | Yes | The form reuses them |

## Custom rule semantics (Phase 1; the contract the form promises)

The rule is defined in time, kept on the broker's steady (monotonic) clock, so its promise holds
at any recording interval and across wall-clock corrections. Plain words, for the owner and the
docs: *a warning confirms when every one of the Pi's recordings of the metric for at least T
seconds was beyond the limit, with no recording missing it and never more than G seconds between
recordings, and those recordings carried at least N separate readings from the vehicle (N is 2
unless changed); with "only while the engine is running", every one of those recordings also
showed the engine above 400 rpm. The telemetry service keeps this time while it runs; a restart of
the service starts it again.* Nothing is claimed about moments between recordings.

Field meanings (names unchanged, they are the file contract):

- `window_seconds` (5–600) = **T, the hold time**: from the run's first recording to its latest.
- `max_age_seconds` (1–min(60, T)) = **G, the longest time between two consecutive recordings in
  the run**.
- `persistence_observations` (2–60) = **N, the least number of separate readings in the run**. A
  reading is (`source`, `observed_us`); a value the broker repeats from its cache across recordings
  counts once and does not break the run. A hold can be carried by one cached reading (battery
  from a quiet bus, up to 30 s), so N is a real requirement, not implied by T. Form default 2,
  settable under Exact settings.
- The latest reading's freshness is no longer a rule field: it is the metric's stale limit.
- Both T and G are measured on the **recording clock**: the broker's `self.monotonic()` read
  beside `captured_at` at the top of each history-loop iteration and passed with that snapshot to
  the evaluator. Observation times are used only for each reading's own freshness.

Precisely, for rule R:

1. **Recording and reading.** A recording is one stored snapshot with its recording-clock time.
   The metric's reading in a recording is its `metric_samples` row in that snapshot. A reading is
   *qualified* when it is fresh at capture, numeric and finite, has the catalog unit, and comes
   from one of the metric's catalog sources with a qualified quality. A change between qualified
   sources (battery voltage has three) is allowed.
2. **Current reading.** S0 is the newest snapshot captured at or before `at`. Its reading must be
   qualified and current: effective age = recorded `age_ms`/1000 + (`at` − S0 capture) ≤ the
   metric's `stale_after_seconds`. Otherwise the state is `unavailable` ("No current reading").
   A snapshot in which the broker reported the metric unavailable has no reading, so an older
   value never stands in for it.
3. **Engine running** (when `engine_running`): S0's `engine.rpm` reading, from the same snapshot,
   must be qualified, effective age ≤ 5 s, value > 400; otherwise `unavailable` ("Fresh
   engine-running evidence is absent"). The RPM and the metric each meet their own freshness bound
   at that recording. No RPM from another snapshot, and nothing captured after `at`, is used.
4. **Run, one recording at a time.** The evaluator keeps per rule, in memory: `since` and `last`
   (recording-clock times of the run's first and latest recordings), `last_snapshot_id`,
   `trip_id`, and the run's distinct readings (at most N kept). When `insights` evaluates a newly
   stored snapshot, S0 *continues* the run only when all of these hold: S0 passes steps 2–3 and its
   reading is beyond the limit; a run exists for the rule at its current `rule_revision`; the
   snapshot stored just before S0 is `last_snapshot_id` (no stored recording went unevaluated); S0's
   recording-clock time − `last` ≤ G; and S0 is in the run's trip (both without a trip count as
   the same). If S0 passes steps 2–3 and is beyond but does not continue, a new run starts at S0
   (`since` = S0's time). Otherwise the run is dropped. Driving condition (regime) is not
   considered; nothing is skipped.
5. **Held time and confirmation.** Held time = S0's recording-clock time − `since`. **Confirmed**
   when held time ≥ T and distinct readings ≥ N.
6. **What starts the time again:** a recording that misses the metric, is unqualified, not
   current, within the limit or without engine evidence; more than G seconds on the recording
   clock between two stored recordings, which also covers recordings the historian refused (after a
   backward wall-clock step it refuses captures until the wall clock passes its newest one) and
   loop iterations that stored nothing; a stored recording whose evaluation failed (the next
   recording's predecessor is not `last_snapshot_id`); a trip change; a broker restart (the state
   is in memory); a rule removed, rejected or with a changed `rule_revision` (its state is dropped
   at the next recording).
7. **Evaluations that are not a recording** (`/v1/health`, `insights.health_response` and any other
   `evaluate()` call without a recording) compute steps 2–3 at their `at`, report the held time as
   of S0 when S0 is the tracker's `last_snapshot_id` (0 otherwise), and never change the state. The
   tracker is written only on the history thread and read under its own lock. Offline callers that
   ingest without a broker (`tools/telemetry_history_profile.py`, tests) may pass no recording
   clock; capture time then stands in, which gives the live result whenever the wall clock did not
   step.
8. Not beyond the limit at S0: `normal`. Beyond but not confirmed: `normal` with a low-confidence
   candidate, or `warning` while an episode is already open (`_open_warning`, unchanged; it also
   keeps an open warning open across a broker restart while the reading stays beyond). The
   candidate and `persistence` keep `observed`/`required` (distinct readings and N) and add
   `held_seconds` and `hold_seconds`.
9. `rule_revision` stays `digest(rule)`; `evaluator_revision` becomes `owner-threshold-v2`.

Why the recording clock (round 3, findings 1 and 2). Measuring the hold from observation times let
a cached reading from another source extend it backward over a normal recording; measuring it from
capture times trusts the wall clock, which on this Pi steps forward and back (Current facts:
`fake-hwclock`, `systemd-timesyncd`, broker started before time sync). The monotonic clock is never
stepped; NTP slewing changes its rate by at most 500 ppm (the kernel's limit), so the one residual
is that a hold can be shorter than T by at most 0.05 %: 0.3 s of a 10-minute hold, 0.015 s of 30 s.
The considered alternative, a capture-time walk over history (replayable by
`tools/warning_replay.py`), closes finding 1 but not 2: a forward step that fits inside G looks
like a slow loop iteration (an iteration is the interval plus unbounded work), so it either leaves
a residual of up to G minus the recording spacing per step (about 54 s at G 60) or needs a count of
recordings derived from the runtime interval, which would hold a 30 s rule at the van's 8 s spacing
until 7 recordings, 48 s; a backward step hides refused recordings behind a short capture gap and
would need sequence numbers parsed from `ingest_key`; and it keeps the paged walk, whose cost grows
with T and the recording rate. The recording clock costs replay from history and a restarted hold
after a broker restart; both are conservative.

Cost: per recording, one indexed query returns S0's readings for every metric the saved rules
use, S0's `engine.rpm` and the id of the snapshot stored before S0; then constant work per rule.
It does not depend on T, G or the recording interval. Benchmark gate in section 8.

What the form may promise: "at least T seconds" of recordings on the Pi's steady clock, every one
beyond the limit, recordings ≤ G apart, at least N separate readings; restarted by a telemetry
service restart. Nothing is unreachable from the fields alone, so nothing is refused for
reachability. How often a rule can confirm depends on the actual recording interval, so the form
gives *advice* when G is under twice the broker's reported `history_recorder.interval_seconds`
("The Pi records about every 5 s right now, more slowly when busy; a longest time between readings
under 10 s may rarely be met."); with no reported interval it gives none. A changed interval
changes how many recordings a hold takes, never the hold.

Probe (2026-11-17, real `TelemetryHistorian` on a temporary file, snapshots ingested as the broker
does, battery below 12 V, T 30 s, G 15 s, N 2; plan revision 2's observation-span walk beside the
recording-clock tracker; the probe passes the real elapsed seconds as the recording clock):

| Case | Revision 2 | Recording clock |
| --- | --- | --- |
| Reviewer's source switch: 0 s 13 V C-CAN; 5 s 11 V B-CAN observed −24 s; 10 s 11 V C-CAN | `warning`, held 34 s at 10 s | `normal`, held 5 s, 2 readings |
| Forward wall step +10 s after 5 s, recordings every 5 s | `warning` at 20 s elapsed (held 30) | `warning` at 30 s elapsed (held 30) |
| Backward wall step −12 s after 5 s, G 10 s | historian refused the 10 s and 15 s captures; run continued across 15 s with no recording (held 28 at 40 s) | restarted at 20 s (15 s gap > G) |
| One cached B-CAN observation repeated 0–25 s, a new one at 30 s, T 25 s | `warning` at 30 s | held 25 s with 1 reading: `normal`; `warning` at 30 s |

Built-in warnings do not change: no existing historian query, built-in rule, gate or lifecycle
function is edited; the custom path stops calling `recent_numeric_samples` and
`latest_sample`. The recording clock is only passed through: the broker's history loop reads
`self.monotonic()` beside `captured_at`, `insights.ingest_snapshot` and
`_evaluate_and_persist_advisories` take it as an optional argument, and `evaluate()` takes an
optional `recording`; built-in rules never read it. Proof required before install:
`tests/advisory_lifecycle_oracle.py` run against
the old and new checkouts with byte-identical `--output`; `tools/warning_replay.py replay`
(`current` = the deployed ref, `new` = the worktree, no custom rules file) on the vanpi export
`tmp/vehicle_data/warning_replay/export-20261003.sqlite3` with identical built-in transitions in
its report. Built-in warning tests stay unchanged and green; custom-rule tests and fixtures
(`tests/test_custom_warnings.py`, the custom parts of `tests/test_warning_chat.py`) are updated
to the new semantics.

## Design

### 1. The form (Phase 3)

A new dialog, **Custom early warnings**, opened by Health's "Add early warning" button when the
advisor's status reports `custom_warning_form: 1` (Phase 2); otherwise the button opens the old
chat as today, so a new dashboard bundle is safe against an older advisor. Layout for the
portrait 800×1280 tablet, top to bottom, with the Review/Add area pinned at the dialog bottom:

1. Pairing (only when the browser has no access code): the existing `AssistantPairing`.
2. "Your custom warnings (N of 32)": a collapsed disclosure; see section 5.
3. The fields (plain labels; the rule field each sets in brackets):
   - **What to watch** [`metric`]: a native `<select>` of the advisor's catalog, labelled with
     `metricLabel` and unit, sorted by label. Below it, one muted line: the live value ("Now
     198 °F", from the store, only when fresh) and the recorded range (section 2). For field
     duty, one more line: "This is the engine computer's commanded alternator field duty, not
     current or temperature."
   - **Warn when it goes** [`operator`]: two buttons, Above / Below. Default Above.
   - **Limit** [`threshold`]: `<input type="number" inputmode="decimal">` with the unit as a
     suffix. No default.
   - **For at least** [`window_seconds`, `max_age_seconds`]: a
     `<select>` of 15 s, 30 s, 1 min, 2 min, 5 min, 10 min. Default 30 s.
   - **Only while the engine is running** [`engine_running`]: a checkbox. Default on, except for
     metrics whose name starts with a prefix in `form_defaults.engine_running_off_prefixes`
     (`battery.`, `tire.`). Hint: "Every reading must also show the engine above 400 rpm."
   - **Name on the warning card** [`title`]: prefilled ("Coolant temperature above 230 °F") and
     kept in step with the fields until the owner edits it.
   - **Exact settings** (collapsed): Hold for (seconds) [5–600], Longest time between readings
     (seconds) [1–60, ≤ Hold for], **Minimum separate readings** [`persistence_observations`,
     2–60, default 2; hint: "Separate measurements from the van; a value the Pi repeats from its
     cache counts once."], and "Compare the size of the reading, ignoring its sign" (turns Above
     into `absolute_above`; disabled with Below). Editing Hold for or Longest time between
     readings switches For at least to "Custom"; the readings floor is independent of it. Hint: "The Pi records a reading about every X seconds right now,
     more slowly when busy." (X from the list reply; the hint is omitted without it.)
4. **Preview** (always visible): "Warn when Coolant temperature is above 230 °F at every reading
   for at least 30 seconds, only while the engine is running above 400 rpm." Variants: "below";
   absolute: "is more than 40 lb-ft from zero, in either direction"; engine off: no clause; a
   floor above 2, from the control, a copied rule or a proposal: "…, over at least 5 separate
   readings". Under it: "Readings are the Pi's recordings; a reading that is missing or within
   the limit, more than 15 seconds without a recording, or a restart of the telemetry service
   starts the time again. Owner monitoring reference, not an OEM limit."
5. **Review** (replaces proposal approval): validates and turns the form read-only, showing the
   preview and the exact values ("held at least 30 s, recordings at most 15 s apart, at least 2
   separate readings"), with **Add
   warning** and **Edit**. Only Add warning sends the install, with `confirmed: true`. After
   success the list opens with the new row, the form resets, and the Health card refreshes
   (`onWarningsChanged`, as today).
6. **Help me choose** (Phase 4; section 4), a secondary button under the form. In Phase 3 the
   same place holds "Describe it to Codex instead", which opens the existing chat unchanged.

Derived values for a For at least choice of H seconds (`form_defaults` in the contract):
`window_seconds` = H; `max_age_seconds` = min(15, H). A preset never changes
`persistence_observations`: it starts at 2 in a new form and keeps whatever the control, a copied
rule or a proposal set, so no requirement is dropped silently; the preview and the Review values
always state a floor above 2. At the van's usual 6–8 s spacing a warning confirms about one
recording after H seconds.

Request ids: Review creates one 32-hex `request_id` for the reviewed draft. Add warning and any
retry of it (a double tap, a timeout, a network error) reuse that id while the dialog stays on
the review step and for at most `request_retry_seconds` (600) after the first send; after that,
on Edit, on close and on success the id is dropped, and adding needs Review again (a new id).

Validation (client messages; the server is authoritative and its `detail` is shown):

- No metric: "Choose what to watch."
- Limit empty or not a number: "Enter a number in °F." (unit of the chosen metric)
- Limit outside ±1,000,000: "Use a number between −1,000,000 and 1,000,000."
- Ignoring the sign with a negative limit: "Use zero or more when ignoring the sign."
- Name empty or over 120 characters: "Give the warning a name of up to 120 characters."
- Exact settings out of range: "Hold for must be 5 to 600 seconds.", "Longest time between
  readings must be 1 to 60 seconds and no more than Hold for.", "Minimum separate readings must
  be a whole number from 2 to 60."
- Advice only (Review stays enabled): G under twice the reported recording interval (above); a
  limit outside the catalog `minimum`..`maximum` ("… readings range from −40 to 419 °F, so this
  could never trigger."); an Above limit below the 30-day low or a Below limit above the 30-day
  high ("This would trigger on almost every reading.").

A saved rule copied into the form (section 5), or a proposal used to fill it, sets every value,
the floor included, and Exact settings opens when any value differs from a preset.

### 2. Recorded range in the form

Shown under What to watch from data the dashboard already holds, with no new route:

- From `summary.history.metric_trends[metric]`: "Last 30 days: low 61, average 189, high 221 °F
  (parked and running together)" (7-day fallback) and, when present, "Usual drive average 196 ±
  3 °F (12 drives)", formatted as `history.helpers.js::trendModel` does.
- Metrics with no trend: "No recorded range on the dashboard for this metric."
- The live value from the snapshot store when fresh.

Not added now: ranges for rpm, speed, torque, target torque, output and turbine speed (each adds a
24 h series query to every supplemental refresh; see `docs/broker-cpu.md`), percentiles and a
running-only split. A follow-up, not a blocker. `CustomWarnings.jsx` passes `readTrend(metric)`
and `readLatest(metric)` to the controller, which keeps the helpers pure.

### 3. Backend

#### The rules file and its writes (Phase 1 defines it; Phase 2 reuses it)

File: `{"version":1,"rules":[…],"removed":["<64 hex>", …]}`. `removed` holds the ids of removed
rules, newest last, at most `max_removed_ids` (256, about 17 KB). The broker ignores it (today's
`load_rules` already ignores unknown top-level keys). New module `custom_warning_file.py`:

- `read(path) -> RulesFile(rows, rejected, removed, other)`: file-level checks as today (absent
  file = empty; symlink, over 128 KiB, not version 1, not a list, more than 32 rows or duplicate
  ids are file-level errors); each row validated alone, rejected rows returned whole; unknown
  top-level keys kept in `other`.
- `write(path, rules_file)`: drops from `removed` any id present in `rows` (a row always wins),
  keeps the newest 256, keeps `other`, rows (rejected ones included) verbatim; serializes and
  refuses over 128 KiB before writing; then `_atomic_json` and an `fsync` of the parent
  directory, so a reply sent after `write` returns describes a durable file. One `write` per
  operation, never two files.
- The broker's evaluator uses `read` (tolerant); `custom_warnings.load_rules` stays as the strict
  wrapper for callers that want it.

Every crash a write can leave, and what a retry does (install and remove are each one `write`):

- Killed before `os.replace`: the old file is intact; a leftover `.early-warnings.json.*.tmp` is
  never read (a deployer may delete it). A retried install installs once; a retried remove
  removes.
- Power lost after `os.replace` but before the directory `fsync`: the file is either old or new,
  never partial; no reply was sent; the retry behaves as one of the two lines next to this one.
- Killed after the directory `fsync` (row written, or row removed with its id in `removed`): a
  retried install finds the row and answers 200 `replayed: true` (or 409 "This request was already
  used." when the rule differs); a retried remove finds no row and answers success.
- Chat installs and removals also update the chat file afterwards (`applied`, display only): a
  crash between the two writes leaves `applied` stale until the retry, which finds the row
  (install) or no row (remove) and sets `applied` to match.
- Row present and its id in `removed` (only from outside the store: a hand edit, or a chat
  approval by a rolled-back advisor): the row wins; the broker evaluates it, the list shows it,
  the next `write` drops that tombstone, and a later removal adds it back. Harmless.

#### Phase 1, broker, advisor wording and shared module

- `custom_warnings.py`: `evaluate_rule(historian, row, at, *, holds, recording=None)`
  implements the semantics above; `RuleHolds` (the per-rule run state of step 4: `since`, `last`,
  `last_snapshot_id`, `trip_id`, distinct readings capped at N; its own lock; `observe(...)` on a
  recording, `peek(...)` otherwise, `retain(ids)` drops removed or rejected rules);
  `validate_rule` takes limits from the contract and caps `max_age_seconds` at min(60, window)
  (no stale cap); `catalog_from(definitions)`; `advice(rule, interval_seconds) -> str | None`.
- `custom_warning_file.py` as above.
- `history/custom_rule_queries.py` (new mixin added to `TelemetryHistorian`'s bases; `queries.py`
  is already 1,609 lines): `recording_readings(metrics, *, at)` returns, for the newest snapshot
  captured at or before `at`, `{snapshot_id, captured_us, trip_id, previous_snapshot_id,
  readings: {metric: reading | None}, rpm}`; a reading is `{value, unit, source, quality,
  freshness, observed_us, observed_at, source_age_ms}`. One indexed query (`snapshots` by
  `captured_us`, `LEFT JOIN metric_samples` on `(snapshot_id, metric)`, plus the previous
  snapshot's id) under the historian lock. Read-only; no schema change.
- `warning_engine/evaluator.py`: owns one `RuleHolds`; `evaluate(at=None, *, recording=None)`,
  where `recording` is `(snapshot_id, clock_seconds)`, calls `recording_readings` once for all
  custom rules and `evaluate_rule` per row; reads with `custom_warning_file.read`; `custom_rules`
  status gains `rejected_ids` and `evaluator_revision: "owner-threshold-v2"`; `error` only for
  file-level failures. A rejected row is not assessed, so its open episode is retired by the
  existing lifecycle ("rule retired from authoritative evaluator catalog"), exactly as removing
  it does; documented, and shown on Health.
- `insights.py`: `ingest_snapshot(..., recording_clock=None)` passes `(snapshot_id,
  recording_clock if given, else the capture time in seconds)` to `evaluate` through
  `_evaluate_and_persist_advisories`; `health_response` is unchanged (no recording).
- `broker.py` history loop: `recording_clock = self.monotonic()` beside `captured_at`, passed to
  `ingest_snapshot`.
- `custom_warnings.json` and `custom_warning_contract.py` (section 6).
- `warning_chat.py`: `SETUP_INSTRUCTIONS` describes the new field meanings (`window_seconds` is
  the hold time; `max_age_seconds` the longest time between recordings, default 15; the floor
  defaults to 2; recordings are usually 5–8 s apart but not guaranteed; latest-reading freshness
  is automatic; a restart of the telemetry service restarts the time). Each proposal turn made
  by this advisor stores the proposal under `rule_proposal`, its id under `rule_proposal_id`, its
  unit under `rule_proposal_unit` and `semantics: "owner-threshold-v2"`, and never writes
  `proposal` or `proposal_id`. A rolled-back advisor therefore has nothing to install from such a
  turn: its loader keeps the unknown keys, `open_chat` returns the chat and the transcript shows,
  its `apply_warning` finds no `proposal` and answers 409 "Review the latest completed proposal
  before applying it", and its dashboard shows no Approve (Current facts, chat records). A new
  question asked there gets an old-code proposal with old-code meaning, which is right for old
  code. This advisor's `apply_warning` reads only `rule_proposal*` and requires the body
  `{proposal_id, confirmed, semantics}` with `semantics == "owner-threshold-v2"`: a body without
  it (an old bundle) gets 409 reason `dashboard_reload_required`, detail "The dashboard was
  updated; reload the page."; a latest turn with only the pre-update `proposal` key gets 409 "This
  suggestion was made before an update; ask again." Apply and remove read and write through `custom_warning_file` (tolerant read, rejected
  rows kept, other top-level keys kept, remove records the id in `removed`, install of an id in
  `removed` refused as in Phase 2).
- Dashboard (Phase 1 build): `WarningChat.helpers.js` reads `rule_proposal`,
  `rule_proposal_id` and `rule_proposal_unit` (demo and test fixtures that carry a setup proposal
  move to these keys), `proposalText` uses the new meaning ("held at least W s, recordings at most
  G s apart, at least N separate readings") and `applyBody` sends `semantics`;
  `views/health.helpers.js` adds "N saved warnings could not be loaded" from `rejected_ids` and
  shows `held_seconds` of `hold_seconds` on a custom candidate when present.

#### Phase 2, advisor store and routes

Routes, served by the advisor and relayed by both web listeners (same headers, same gate):

- `GET /v1/assistant/warnings` returns `{catalog: [{metric, unit, minimum, maximum,
  stale_after_seconds}], warnings: [{id, rule, loaded, rejected, advice, source, created_at,
  created_here}], count, max_rules, recording_interval_seconds, warning_config_error,
  warning_evaluator_enabled, evaluator_ready}`. `loaded`/`rejected` come from the broker's cached
  `/v1/health` `custom_rules`; `recording_interval_seconds` from cached `/v1/status`
  `history_recorder.interval_seconds` (null when absent); `advice` from the shared function;
  `created_here` = `row["owner"] == owner` (provenance only).
- `POST /v1/assistant/warnings` body exactly `{request_id, rule, confirmed}`. Route and chat both
  call `custom_warning_store.install(rule, *, owner, source, row_id, chat_id=None)`:
  1. `confirmed is True`, `request_id` matches `ID_RE` (32 hex), else 400 (route only).
  2. `validate_rule(rule)`, else 400 with the message.
  3. Broker readiness (before taking the lock): cached `GET /v1/health`; `custom_rules
     .evaluator_revision` must be `owner-threshold-v2`, else 409 "The telemetry service needs
     its update before warnings can be saved."; `GET /v1/metrics`, the rule's metric must be in
     `catalog_from(served)` with the same unit, else 409 "The telemetry service doesn't offer
     this metric yet; it needs a restart after the update." Broker unreachable: 503 "The
     telemetry service is unavailable, so the warning can't be checked; try again."
  4. Under `manager.lock`, one `custom_warning_file.read`, then in order: `row_id` among the rows
     with the same rule → 200 `replayed: true`, with another rule → 409 "This request was already
     used."; `row_id` in `removed` → 409 "This warning was added and has since been removed;
     review it again to add it once more."; a row with the same trigger (all fields but `title`)
     → 409 "An identical warning already exists: <title>."; 32 rows → 409 "Custom warning limit
     reached (32); remove one first."
  5. Append `{id: row_id, rule, approved_at, owner, source, chat_id}` and one
     `custom_warning_file.write`; over 128 KiB → 409 "The saved warnings file would be too large;
     remove a warning first." `row_id` = `sha256("form:" + owner + ":" + request_id)` for the
     form, the `proposal_id` for the chat, so a retried request is the same row.
- `POST /v1/assistant/warnings/<64 hex>/remove` `{confirmed: true}`: any paired browser may
  remove any rule, rejected rows included. One `write` drops the row and records its id in
  `removed` (also when the row is already gone, so a repeat is a no-op); every other row is kept
  verbatim; then clears `applied` on the chat turn that installed it.
- Retry contract (stated, not claimed absolute): a retried install whose rule was removed is
  refused while the id is among the newest 256 recorded removals. The dashboard retries one id
  for at most 600 s. Every removal request records an id, also one that matches no row, so the
  tombstone can be evicted within that window by 256 or more later removal requests from paired
  browsers (authenticated; two devices today), with no additions needed. A retry after the id has
  aged out is a new install of the reviewed rule, exactly as if the owner had tapped Add warning
  again.
- The chat's `apply-warning` keeps its exact-proposal and marker checks in `warning_chat.py`,
  then calls `install(..., source="chat", row_id=proposal_id)`; `remove-warning` calls the
  store's remove. Both thereby gain the readiness check and identical-trigger refusal.
- `/v1/assistant/status` keeps `warnings`/`warning_config_error` (now vehicle-wide) for old
  bundles and adds `custom_warning_form: 1`; Phase 4 drops the warning fields.

Rule ownership decision: **rules are vehicle-wide.** Any paired browser sees and may remove every
rule; the list marks "Added on this browser". Chat transcripts stay per browser. Rows keep the
key `owner`, so a rolled-back advisor still lists form rows on the browser that made them. Undo:
restore the owner filter in `custom_warning_store.list_rows` and `remove`.

Where this meets the access gate (keep the PIN work separable): the routes add one allowlist
predicate (`custom_warning_contract.relay_allowed`) in `_assistant_request` beside
`dtc_research_relay_allowed`, and dispatch branches in `ChatHandler.handle_request` after
`manager.authorize`. They change no origin, header or token check, `authorize`,
`assistantAccess.js` or `AssistantPairing.jsx`. The parallel pairing change adds one
unauthenticated route (`POST /v1/assistant/pairing/redeem`, handled before `authorize`); every
route here stays authenticated, and the two changes merge in either order. Because the list no
longer filters by the client-id hash, re-keying browsers cannot orphan rules.

### 4. The agent's new place (Phase 4)

- **Help me choose** opens a compact panel in the form dialog: "Suggest a limit from my history",
  "Explain this metric", a two-line text box, the transcript, Stop, Retry, **New chat**, and the
  line "Readings as of <time>". Model and effort pickers sit in a collapsed "Model" disclosure.
  Nothing is sent until the owner taps a suggestion or Send: the automatic first turn is removed.
- Context (cached broker GETs only, as `event_context` does): `catalog()`, `focus_metric`
  (required, in the catalog), its `recorded_range` from `/v1/history` `metric_trends[metric]`
  without `sparkline`/`series`, its `latest` reading, the recording interval and
  `form_defaults`. The browser sends only `metric` and the question.
- Opening and sending, defined so no question is ever dropped:
  - The list reply carries `help_chats: {metric: chat_id}` (newest setup chat per metric for this
    browser). Opening the panel only reads that chat (`GET /v1/assistant/chats/<id>`), so an
    answer that finished while the panel was closed appears; nothing is sent.
  - `POST /v1/assistant/chats` for setup takes `{event, request_id, settings, metric, message,
    new_chat?}`. A `request_id` already in a matching chat returns that chat (replay, no new
    turn). Otherwise, with no chat for (owner, setup, metric) or with `new_chat: true`, a new chat
    with fresh context is created and `message` is its first turn. Otherwise `message` is
    appended to the existing chat as a new turn (`start_turn`).
  - Refusals carry a `reason`: `busy` (an answer is running), `conversation_full` (24 turns or 96
    request ids). The panel shows the detail and, for `conversation_full`, offers New chat with
    the question kept in the text box.
  - New chat is also the evidence refresh: a chat's context is fixed at creation and the panel
    shows its time.
- `SETUP_INSTRUCTIONS`: the proposal fills the owner's form and installs nothing; base a limit on
  `recorded_range` and say low/high include parked readings; explain the hold time, the
  longest time between recordings and that a missing reading restarts it; use the form defaults
  unless asked; explain field duty when it is the metric; plain words, not field names.
- The reply stays `{explanation, proposal}`, checked with `validate_rule`. The panel shows the
  proposal's preview and **Use these settings**, which fills all eight fields; the owner saves
  through Review and Add warning.
- Compatibility window for dashboards still open on an old bundle: the Phase 4 advisor answers
  the removed `apply-warning`/`remove-warning` routes and a setup `POST /chats` without `metric`
  and `message` with 409, reason `dashboard_reload_required`, detail "The dashboard was updated;
  reload the page.", and the relay keeps forwarding those paths until a later cleanup removes
  them after the owner has reloaded both devices.

### 5. Saved custom warnings (Phase 3)

The "Your custom warnings (N of 32)" disclosure, open after a save. Each row: the preview
sentence, a status, "Added on this browser" when `created_here`, **Copy to form** (fills every
value; saving creates a new rule and the owner removes the old one; rules are never edited in
place), and **Remove** (`window.confirm`). Statuses: "Watching" (`loaded`); "Saved; starts on the
next check" (neither loaded nor rejected); "The telemetry service could not load this rule"
(`rejected`); plus "May rarely trigger: <reason>" (`advice`). A file-level error shows as today.

### 6. Files, shared contract, map

New shared contract `projects/vehicle_data/custom_warnings.json`, read by Python
(`custom_warning_contract.py`) and imported by the dashboard (`with { type: "json" }`, as
`dtcIgnore.helpers.js` does):

```json
{
  "version": 1,
  "paths": {"warnings": "/v1/assistant/warnings",
            "remove": "/v1/assistant/warnings/{id}/remove"},
  "fields": ["title", "metric", "operator", "threshold", "persistence_observations",
             "window_seconds", "max_age_seconds", "engine_running"],
  "operators": ["above", "below", "absolute_above"],
  "catalog_qualities": ["verified", "observed_alfa_scale"],
  "max_rules": 32,
  "max_file_bytes": 131072,
  "max_removed_ids": 256,
  "request_retry_seconds": 600,
  "evaluator_revision": "owner-threshold-v2",
  "limits": {"title_chars": [1, 120], "threshold": [-1000000, 1000000],
             "persistence_observations": [2, 60], "window_seconds": [5, 600],
             "max_age_seconds": [1, 60]},
  "engine_running_rpm_above": 400,
  "engine_running_rpm_max_age_seconds": 5,
  "advice_gap_interval_multiple": 2,
  "form_defaults": {"hold_seconds_choices": [15, 30, 60, 120, 300, 600], "hold_seconds": 30,
                    "gap_seconds": 15, "persistence_observations": 2,
                    "engine_running_off_prefixes": ["battery.", "tire."]},
  "requests": {"install": ["request_id", "rule", "confirmed"], "remove": ["confirmed"],
               "chat_apply": ["proposal_id", "confirmed", "semantics"]}
}
```

`custom_warning_contract.py` loads it and exports the limits, `FIELDS`, `MAX_RULES`,
`MAX_FILE_BYTES`, `MAX_REMOVED_IDS` and `relay_allowed(method, path)`; no `METRICS` import, so
`web.py` stays light.

Phase 1 files: `custom_warnings.py`, `custom_warning_file.py`, `history/custom_rule_queries.py`,
`historian.py` (mixin in the bases), `warning_engine/evaluator.py`, `insights.py` and
`broker.py` (the recording clock, passed through), `custom_warnings.json`,
`custom_warning_contract.py`, `warning_chat.py`, dashboard `WarningChat.helpers.js` and
`views/health.helpers.js`; docs: new `projects/vehicle_data/docs/custom-warnings.md` (semantics in
plain words, fields, the file, crash states, deploy precondition and rollback),
`docs/warning-chat.md`, vehicle-data README custom-warning paragraph; `AGENTS.md` map paragraph
(extended in Phase 2): "Custom early warnings (owner request 2026-11-17): the form is the way to
add a rule. `custom_warnings.json` with `custom_warning_contract.py` (fields, limits, cap, form
defaults, routes; shared with the dashboard), `custom_warnings.py` (catalog, validation,
evaluation semantics, advice), `custom_warning_file.py` (the rules file: tolerant read, one
durable write per operation, removed ids), `history/custom_rule_queries.py` (the one-recording
readings query; holds are timed in `custom_warnings.RuleHolds` on the broker's monotonic
clock), `custom_warning_store.py` (advisor list/install/remove);
contract `projects/vehicle_data/docs/custom-warnings.md`; dashboard
`src/customWarningForm.helpers.js`, `dialogs/CustomWarnings{.jsx,.helpers.js}`,
`styles/custom-warnings.css`."

Phase 2 files: `custom_warning_store.py` (list, install, remove, readiness, duplicate check;
`warning_status` and `remove_saved_warning` move here from `warning_chat.py`, which is over 800
lines); `warning_chat.py` (dispatch branches; chat install and remove call the store); `web.py`
(`custom_warning_relay_allowed` in the allowlist expression); `api_schema.py`
(`AssistantWarningsResponse`); docs and the map.

Phase 3 files (dashboard only): `src/customWarningForm.helpers.js` (pure: defaults, derived
fields, preview, validation, advice text, range lines, statuses, fill-from-rule, request-id
lifetime), `src/dialogs/CustomWarnings.jsx` and `.helpers.js` (controller over
`createRequester`, `useController.js`, `useModalDialog`), `src/styles/custom-warnings.css`
(palette tokens only), `src/app/dialogs.js`, `views/Health.jsx`; `tools/demo_assistant.mjs`
`custom-warnings` scene (catalog slice, two saved rules: one Watching, one "Saved; starts on the
next check"); `readme_shots.playwright.js` entry 13 fills coolant / Above / 230 / 30 s and taps
Review, never Add warning; `demo_data.mjs` coolant trend with a 30-day high near 221 °F;
`dashboard/docs/caveats.md` field-duty line.

Phase 4 files: `warning_chat.py` (setup context, opening rules, instructions, compatibility
replies, deletions), `WarningChat.jsx`/`.helpers.js` (`warning-help` mode), new
`src/dialogs/WarningHelp.jsx`, `CustomWarnings.jsx` (hosts the panel; Use these settings),
`app.css` (drop chat-monitor, chat-saved and chat-proposal rules), docs.

Code shape: every new advisor function goes in `custom_warning_store.py`; file I/O in
`custom_warning_file.py`; the new historian query in its own mixin; `WarningChat.helpers.js` (776
lines) changes only text and the apply body in Phase 1 and must shrink in Phase 4; strings in
each helpers file's `TEXT` table; no inline styles (CSP).

### 7. Tests

Phase 1. A shared fixture `tests/custom_rule_history.py` builds a real `TelemetryHistorian` on a
temporary file and ingests snapshots made from `METRICS[...].public_dict()` with explicit capture
time, recording-clock time (independent of capture time, so wall-clock steps can be staged),
observation lag per metric, source and unavailable entries (the probe harness of 2026-11-17), and
evaluates each stored snapshot as `insights` does. Tests in `tests/test_custom_warnings.py`,
through `EarlyWarningEvaluator.evaluate` with and without a recording:

- Current reading: `test_unavailable_snapshot_after_fresh_readings_is_unavailable_not_warning`;
  `test_latest_reading_older_than_its_stale_limit_is_unavailable`;
  `test_stale_snapshot_between_readings_breaks_the_run`.
- Engine: `test_engine_running_rule_confirms_with_capture_and_observation_times_apart`;
  `test_engine_running_uses_only_rpm_from_the_same_snapshot`;
  `test_no_evidence_captured_after_at_is_used`.
- Regime: `test_normal_reading_in_another_regime_breaks_the_run`;
  `test_regime_change_without_a_normal_reading_does_not_break_the_run`.
- Time contract: `test_fast_recordings_never_confirm_before_the_hold_time` (seven field-duty
  recordings 1 s apart, hold 30 s: `normal`, held 6 s);
  `test_hold_is_measured_in_time_at_any_recording_interval` (1 s, 5.2 s, 8 s, 5 s then 1 s:
  confirms at the first recording whose held time ≥ H, never before, for every form hold
  choice); `test_gap_within_the_rule_gap_continues_and_over_it_breaks` (12 s and 20 s, G 15 s);
  `test_broker_restart_starts_the_hold_again_and_an_open_warning_holds` (a fresh evaluator);
  `test_trip_change_breaks_the_run`;
  `test_variable_source_lag_does_not_change_the_outcome`;
  `test_repeated_cached_observation_counts_once_and_does_not_break_the_run`;
  `test_distinct_reading_floor_above_two_is_enforced`;
  `test_one_cached_reading_across_the_hold_waits_for_a_second_reading`;
  `test_source_change_with_qualified_sources_continues`.
- Source switch and clocks (round 3):
  `test_source_switch_to_an_older_cached_observation_does_not_extend_the_hold` (the reviewer's
  case: 0 s 13 V C-CAN, 5 s 11 V B-CAN observed −24 s, 10 s 11 V C-CAN, T 30, G 15: `normal`, held
  5 s); `test_forward_wall_step_within_the_gap_does_not_shorten_the_hold` (+10 s step, recordings
  5 s apart on the recording clock: confirms at 30 s of recording clock, not 20);
  `test_forward_wall_step_never_lengthens_the_hold` (+120 s, G 15; a step that makes the historian
  open a new trip restarts the hold, which is conservative);
  `test_backward_wall_step_refused_recordings_restart_the_hold` (−12 s, G 10: the historian
  refuses two captures and the hold restarts); `test_backward_wall_step_within_the_gap_continues`
  (−3 s, one refused capture, recording-clock gap 10 s ≤ G 15);
  `test_failed_evaluation_of_a_stored_recording_restarts_the_hold`;
  `test_health_evaluation_reports_the_hold_and_changes_nothing`;
  `test_removed_or_revised_rule_state_is_dropped`.
- Query and cost: `tests/test_vehicle_historian.py::test_recording_readings_returns_the_newest_snapshot_with_rpm_and_predecessor`
  (missing metric is `None`, nothing after `at`); `test_one_readings_query_per_recording_for_all_custom_rules`
  (32 rules, query count by a counting historian wrapper);
  `test_built_in_rules_ignore_the_recording_argument` (same built-in assessments with and without
  it).
- Validation and contract: `test_gap_may_exceed_the_stale_limit_but_not_the_hold`;
  `test_advice_uses_the_reported_interval_and_is_silent_without_one`;
  `test_contract_limits_are_the_validator_limits`;
  `test_catalog_from_served_definitions_equals_catalog`.
- File: `tests/test_custom_warning_file.py`: tolerant read keeps rejected rows whole and
  `other` keys; `test_one_rejected_row_leaves_the_other_custom_rules_evaluating` (`rule_ids` one,
  `rejected_ids` the other, `error` None); `test_rejected_rule_retires_its_open_episode`;
  `test_file_level_corruption_still_reports_an_error_and_built_ins_continue`;
  `test_removed_ids_are_ignored_by_the_broker_and_the_current_loader`;
  `test_write_keeps_the_newest_256_removed_ids_and_drops_ids_present_as_rows`;
  `test_write_over_128_kib_is_refused_without_a_write`;
  `test_crash_before_replace_leaves_the_old_file` and
  `test_write_fsyncs_the_directory_after_replace` (fault injection by patching `os.replace` and
  `os.fsync`).
- Chat (`tests/test_warning_chat.py`): `test_apply_without_semantics_answers_reload_required`;
  `test_proposal_made_before_the_update_is_refused`;
  `test_chat_writes_keep_rejected_rows_other_keys_and_record_removed_ids`;
  `test_chat_install_of_a_removed_id_is_refused`;
  `test_v2_proposal_turn_carries_no_key_the_pre_v2_advisor_installs` (a completed setup turn has
  `rule_proposal`, `rule_proposal_id`, `semantics` and neither `proposal` nor `proposal_id`; the
  saved chat file is the fixture for the rollback check in section 8); existing setup tests
  updated to the new keys and body.
- Built-in invariance: the oracle and replay comparison named in "Custom rule semantics".
- Dashboard: `test/health.helpers.test.mjs` (rejected line, held time on a candidate);
  `WarningChat` helper tests for the new proposal text and apply body.

Phase 2, `tests/test_custom_warning_store.py`:
`test_confirmed_form_rule_installs_and_loads`,
`test_unconfirmed_or_extra_fields_are_refused_without_a_write`,
`test_same_request_id_replays_without_a_second_row`,
`test_reused_request_id_with_another_rule_is_refused`,
`test_lost_reply_then_removal_elsewhere_then_retry_is_refused_as_removed`,
`test_crash_at_each_write_point_then_removal_and_retry` (for install and remove: fault injected
before `os.replace`, after `os.replace` before the directory fsync, and after the write; then
another browser removes and the original request retries; the final file and replies match the
crash-state list), `test_row_and_tombstone_together_the_row_wins_and_the_next_write_heals`,
`test_identical_trigger_is_refused`, `test_form_and_chat_duplicates_are_refused_both_ways`,
`test_cap_of_32_is_refused_without_a_write`,
`test_rejected_rows_survive_form_and_chat_writes_and_are_removable_by_id`,
`test_broker_without_v2_evaluator_refuses_form_and_chat_installs`,
`test_metric_missing_from_broker_catalog_is_refused_for_form_and_chat`,
`test_broker_unreachable_refuses_install`,
`test_list_is_vehicle_wide_marks_created_here_and_reports_the_interval`,
`test_any_paired_browser_removes_any_rule`,
`test_form_and_chat_paths_accept_and_refuse_the_same_rules`,
`test_chat_install_keeps_exact_proposal_approval`.
`tests/test_warning_chat.py`: `test_custom_warning_routes_are_relayed_only_as_get_list_and_post_install`
(other methods and paths → 404; origin, fetch-metadata, header and JSON gates apply);
`test_custom_warning_routes_require_authorization`; setup approval test updated for vehicle-wide
removal.

Phase 3: dashboard `test/customWarningForm.helpers.test.mjs` (derived fields for every hold
choice pass the limits; every hold preset keeps the readings floor (2, 5 and 60 survive a
preset change); the floor control's range and message; preview and Review values for above,
below, absolute, engine-off and a floor above 2, whether set by the control, a copied rule or a
proposal;
validation and advice messages, advice absent without an interval; range lines, 7-day fallback,
no trend; default engine checkbox by prefix; auto title until edited; fill from a saved rule and
from a proposal; statuses); `test/CustomWarnings.helpers.test.mjs` (401 shows pairing; status
without `custom_warning_form` opens the old chat; load shows catalog and list; Review then Add
warning posts `confirmed: true` once; a double tap and a retry after a timeout reuse one
`request_id`; after 600 s, Edit or a changed draft a new one; 409, 503 and the "removed since"
reply shown; remove; `onWarningsChanged`). `tests/test_run_tests.py` passes. Screenshot 13
retaken (`node tools/readme_shots.mjs --only 13` on vanpi; `--promote`), manifest caption "Add
Early Warning form with an illustrative coolant rule, its recorded range and preview, at the
review step; nothing was saved." and the root README alt text. On-screen keyboard check: the
Playwright pair also shoots the form at 800×640 with the Limit field focused, and a dashboard
test asserts the Review/Add area is inside the viewport there; the owner confirms once on the
tablet.

Phase 4: `test_setup_chat_starts_only_with_the_owners_question`,
`test_setup_context_carries_focus_metric_range_and_latest_from_cached_gets`,
`test_setup_chats_are_matched_per_focus_metric`,
`test_reopening_a_metric_with_a_chat_appends_the_new_question`,
`test_request_id_replay_returns_the_chat_without_a_new_turn`,
`test_full_conversation_refuses_with_reason_and_new_chat_starts_fresh_context`,
`test_answer_completed_while_closed_is_returned_on_reopen`,
`test_removed_chat_routes_answer_reload_required`,
`test_setup_proposal_is_validated_but_never_installed`; dashboard controller tests for
`warning-help`; delete the tests of removed code.

Every change set runs the whole suite (`run_tests.py`, by the vanpi route in `AGENTS.md`).

### 8. Deploy and rollback

General rules: deploy phases in order and roll back in reverse order. Never paste the rules
file or the access code into an agent conversation; commands print counts only. The rules file
is `F=/var/lib/van-telemetry-advisor/early-warnings.json`.

**Phase 1, custom rules fire correctly** (precondition, broker, advisor, build). Saved rules
would change meaning (the gap is measured between recordings, the window becomes a hold time),
so Phase 1 activates only on a vehicle with no saved custom rules; it never reinterprets one.

Before install (with the review; each result recorded in the review brief):

- Built-in invariance proof ("Custom rule semantics").
- Cost gate: `tools/telemetry_history_profile.py` (`telemetry-history-profile` compute task) on a
  finalized backup, with a context whose saved `custom_rules` holds 32 valid rules (ten-minute
  holds, every catalog metric represented, some beyond their limit but not yet confirmed, so the
  candidate path and its episode lookup are measured) and once with none. It is a relative check:
  the task runs on a compute worker (usually the Mac) against an in-memory copy of the database,
  so its absolute times are not the Pi's. The ingest phase (one stored
  snapshot: ingest, warning evaluation, persistence) with 32 rules may exceed the run without
  rules by at most 10 ms (the target), against the 72.0 ms "Saved-snapshot ingest" figure in
  `docs/broker-cpu.md`, and the custom block issues exactly one readings query and at most one
  episode read. The pass condition is the parent's gate under Decided ("Cost gate"): zero rules
  unchanged from master, 32 worst-case rules no costlier than master with the same rules, and the
  absolute added time measured on the Pi reported beside the 10 ms target. The cost does not
  depend on hold length or recording rate (no history walk), so one interval suffices. Custom
  rules reach the historian's cadence only through this ingest time (the loop waits the interval
  after the work), so a 10 ms bound keeps the 5 s interval's spacing within 0.2 %; the unit's
  `--history-interval` is unchanged. After install, one live Pi sample: the median historian
  interval over five minutes (consecutive `/v1/status` `history_recorder.last_stored_at`
  differences, as `docs/broker-cpu.md` measured it) minus the 5 s wait is the per-recording work on
  the Pi; with no rules it must match the pre-install figure within its spread (runbook in
  `docs/custom-warnings.md`).
- Rollback check of the chat keys: in a scratch worktree at the deployed ref, with a temporary
  state directory holding the v2 setup chat saved by
  `test_v2_proposal_turn_carries_no_key_the_pre_v2_advisor_installs`, construct the old
  `WarningChatManager` (stub client and runner), `open_chat` the setup event (returns that chat,
  transcript intact) and `apply_warning` with its `rule_proposal_id`: 409, and no rules file is
  created.

1. `sudo systemctl stop van-telemetry-advisor.service` (the only rules writer; chat and research
   pause), so nothing is saved between the check and the broker restart.
2. Precondition, both must print 0: `if sudo test -e $F; then sudo jq '.rules | length' $F; else
   echo 0; fi` (an unreadable file prints an error, not 0) and `curl -s --unix-socket /run/van-telemetry/api.sock http://broker/v1/health | jq
   '.custom_rules | if .error then "error" else .count end'`. If either is not 0: `sudo
   systemctl start van-telemetry-advisor.service`, stop the deploy and tell the owner the count.
   Each rule needs his explicit decision before Phase 1: he removes it on the dashboard (the
   existing Remove), noting any he wants back; after Phase 1 he recreates it with new settings
   through the chat (or, after Phase 3, the form). No agent edits or converts the file. Then
   restart from step 1.
3. `s restart_broker` (parked; the sanctioned restart; `PartOf=` restarts both web listeners).
   Check: the health command above shows `enabled: true`, `error: null`, `evaluator_revision:
   "owner-threshold-v2"`, `count: 0`, `rejected_ids: []`.
4. `sudo systemctl start van-telemetry-advisor.service` (new wording, marker, file module).
5. Dashboard build (frontend-only).
6. Journals of the four units clean. Agents do not install live rules; the owner tries one when
   he chooses.

During the rollout, a dashboard tab still open on the old bundle can still ask the chat, but
gets no installable proposal: it shows proposals only from `proposal`, which this advisor never
writes, and an apply body without `semantics` is answered 409 "The dashboard was updated; reload
the page." A proposal made before the advisor restart is refused even from the new bundle ("ask
again"). No rule with old meanings can be saved after step 1, and after a rollback no v2 proposal
can be installed by the old advisor (section 3, `warning_chat.py`).

Phase 1 rollback (writes quiesced throughout; every step is a rename, nothing is overwritten).
The saved v2 configuration and any empty live file get different, never-reused name patterns,
and the saved file's rule count is part of its name, so a later roll-forward needs no shell
variable from this one:

1. `sudo systemctl stop van-telemetry-advisor.service`.
2. Set the complete file aside: `if sudo test -e $F; then N=$(sudo jq '.rules | length' $F) &&
   SAVED=$F.v2-saved-$(date +%Y%m%d%H%M%S)-${N}rules && sudo test ! -e $SAVED && sudo mv -n $F
   $SAVED && sudo test ! -e $F && echo "$SAVED"; fi` (an unreadable file stops at `jq`). With no
   `$F` there is nothing to set aside. A crash here leaves the file at `$F` (run it again) or at
   `$SAVED`; a rerun never touches an earlier saved file.
3. Revert the code, `s restart_broker`; check `custom_rules` `count: 0`, `error: null`.
4. `sudo systemctl start van-telemetry-advisor.service` and rebuild. Tell the owner N and that
   those rules are kept in the saved file, inactive. Saved chats are left as they are: their v2
   proposals are under keys the old advisor does not install, and their transcripts stay readable.
   The old advisor's replay sends `turn.get("proposal")`, so a v2 proposal reaches its model as
   null: accepted, context only.
5. Roll-forward (any time later; replaces Phase 1 steps 1–3):
   1. `sudo systemctl stop van-telemetry-advisor.service`.
   2. `sudo sh -c 'ls -1 $0.v2-saved-*' $F`. With none, deploy Phase 1 as above. With more than
      one, stop and ask the owner which to restore. Set `SAVED` to that path and `N` to the
      count in its name; `sudo jq '.rules | length' $SAVED` must print N, else stop.
   3. Precondition on the live file, as Phase 1 step 2. If it holds rules (saved under the old
      code meanwhile), stop for the owner's per-rule decision. If it exists with zero rules, set it
      aside under the other pattern: `EMPTY=$F.v1-empty-$(date +%Y%m%d%H%M%S); sudo test ! -e
      $EMPTY && sudo mv -n $F $EMPTY && sudo test ! -e $F`.
   4. `sudo mv -n $SAVED $F && sudo test ! -e $SAVED`.
   5. Deploy the Phase 1 code and `s restart_broker`; the health command must show `count` N,
      `rejected_ids: []`, `error: null` and `evaluator_revision: "owner-threshold-v2"`. Otherwise
      stop with the advisor stopped and report.
   6. Phase 1 steps 4–6.
   Tested once before install in a scratch directory standing in for `$F`'s folder: rollback
   with 3 rules, then roll-forward with an empty live file and with none, ends with the 3-rule
   file at `$F`, the empty file under `v1-empty-`, and no file overwritten; the runbook in
   `docs/custom-warnings.md` carries these exact commands.

**Phase 2, advisor store and routes** (advisor, web listeners). Prerequisite: Phase 1 deployed;
the store also enforces it through `evaluator_revision`. Each intermediate state is safe: an old
bundle never calls the new routes; a new allowlist before the new advisor gets the advisor's 404.

1. `sudo systemctl restart van-telemetry-advisor.service`.
2. `sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service`.
3. Checks: through each listener an unpaired `GET /v1/assistant/warnings` answers 401 (relayed
   and gated), not 404, e.g. `curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin:
   http://192.168.6.103:8765' http://192.168.6.103:8765/v1/assistant/warnings`; the owner adds
   and removes one test rule through the chat (now through the store).
4. Rollback: revert, restart advisor and both web listeners. The Phase 1 advisor reads and writes
   the same file through `custom_warning_file` (extra row keys and `removed` kept, removals still
   recorded), so nothing is lost.

**Phase 3, the form** (dashboard build only; allowed at any time; safe before Phase 2 because
the button falls back to the chat without `custom_warning_form`). Checks: the owner opens the
form on the tablet, sees the list, adds one test rule (Watching within about 10 s), removes it,
and checks the pinned buttons with the keyboard open. Rollback: rebuild the previous ref.

**Phase 4, help inside the form and deletions** (advisor, web listeners, build, in that order).
Checks as Phase 2, plus one help question with internet, reopening a metric's chat, and saving
through the form with the Pi offline. Old bundles get "reload the page" (section 4). Rollback:
revert, restart advisor and web listeners, rebuild; rules untouched.

Old setup chats stay on the Pi (counting toward the 200-chat limit) but are not reachable from
the dashboard after Phase 4, because help chats are matched per focus metric.

### 9. What to delete (Phase 4 unless noted)

- `warning_chat.py`: `apply_warning` and the apply/remove dispatch bodies (replaced by the
  reload-required reply), the automatic first question (`84d95f3`), `public()`'s `catalog` and the
  `catalog_summary` import (`78d510b`, `ce71087`), the status `warnings` fields (`84d95f3`).
- `custom_warnings.py`: `catalog_summary`; the stale cap, the count-only persistence and the
  regime-filtered run (Phase 1, `84d95f3`).
- `web.py`: the apply/remove relay pattern, in the later cleanup after both devices reloaded.
- `WarningChat.jsx`: `Proposal`, `SavedWarnings`, the monitor disclosure, `add-warning` mode.
- `WarningChat.helpers.js`: `SETUP_EVENT` uses in `eventSelector`/`dialogTitle`, `setupTitle`,
  `setupEvidence`, `monitorView`, `TEXT.monitor*`, `RULE_READINGS`, `proposalText`,
  `appliedText`, `applyBody`, `savedWarningsView`, `removeSavedBody`, `warningRemovePath`,
  `confirmApply`, `confirmRemove`, `confirmRemoveSaved`, proposal and saved state,
  `applyWarning`, `removeSaved`. `setupWaiting` (`7f2c5ce`) stays.
- `app.css`: `.chat-monitor*`, `.chat-saved*`, `.disc.chat-proposal`.
- `demo_assistant.mjs`: the `setup` scene, `SETUP_CATALOG`, `SETUP_PROPOSAL` (replaced in Phase 3).
- Tests of each of the above.

### 10. Phasing

1. **Phase 1, custom rules fire correctly** (broker, advisor, build): time-defined semantics on
   the recording clock (`RuleHolds`), the one-recording query mixin, the rules-file module,
   tolerant load with `rejected_ids` and
   `evaluator_revision`, contract, advice, chat wording, `rule_proposal` keys and semantics
   marker, Health lines, invariance proof, cost gate, the zero-rules precondition. Plan
   re-check, then review before install.
2. **Phase 2, advisor store and routes** (advisor, web listeners): store, both installers through
   it, routes, relay, vehicle-wide list, docs, map. Review before install.
3. **Phase 3, the form** (build only): dialog, list, screenshot 13. Review before install.
4. **Phase 4, help inside the form and deletions** (advisor, web listeners, build). Review before
   install.

Each phase is one change set, built in a worktree, with a cross-family review before install.
Each is deployable alone with the restart order and rollback in section 8. Phase 1 no longer
carries a rollback tool, a reachability check or a cadence constant; Phase 2 no longer carries a
ledger file.

## Decided (each undoable)

- Rules are defined in time: hold T (`window_seconds`), longest time between recordings G
  (`max_age_seconds`), a floor of distinct readings (`persistence_observations`, form default
  2). Chosen over counting recordings because the van's spacing is a setting the code does not
  enforce, so only a time can make "for at least 30 seconds" true. Undo: restore the previous
  `evaluate_rule` (git), with the precondition and set-aside rollback in section 8.
- Custom-rule run: current reading bound by the metric's stale limit, RPM from the same
  snapshot, no regime filter, any non-qualifying recording breaks the run, distinct readings
  count once. Undo: as above.
- The hold is timed per recording on the broker's monotonic clock, in memory, not from wall-clock
  or observation timestamps in history: immune to clock steps and source switches, short by at
  most 0.05 % (NTP slewing); it restarts with the broker and is not replayed from history.
  Chosen over a capture-time history walk, which could not bound forward clock steps without
  delaying confirmation. Undo: restore a history walk (plan revision 2, `5296ab4`) with its
  residual.
- An intervening unavailable or stale recording restarts the hold rather than being bridged.
  Undo: let a recording that misses the metric continue the run within G.
- A change between qualified sources does not break a run. Undo: require S0's source.
- No reachability refusals; advice below twice the reported recording interval.
- Phase 1 activates only with zero saved custom rules, verified with the advisor stopped; a
  non-zero count stops the deploy for the owner's per-rule decision. Rollback sets the whole
  file aside as `v2-saved-<time>-<N>rules`; roll-forward sets an empty live file aside as
  `v1-empty-<time>` and restores only after the count matches.
- v2 chat proposals are stored under `rule_proposal*` keys, so a rolled-back advisor shows them
  but cannot install them; chats are not touched on rollback. Undo: write `proposal` again.
- Old-bundle installs during the rollout are refused with "reload the page"; proposals made
  before the update are refused.
- Removed ids are kept in the rules file (newest 256), so every operation is one durable write;
  the dashboard retries one request id for at most 600 s. Undo: edit `max_removed_ids` and
  `request_retry_seconds`.
- Rules are vehicle-wide; transcripts stay per browser.
- One bad row disables only itself; its open episode is retired as if removed.
- Form defaults: 30 s, gap min(15, H), floor 2, engine-running on except battery and tire
  metrics, no default limit; hold choices up to 10 min. Undo: edit `form_defaults`.
- The readings floor is a form control under Exact settings; hold presets keep it, and the
  preview states any floor above 2. Undo: hide the control (presets still keep the value).
- The advisor refuses installs unless the broker reports `owner-threshold-v2` and serves the
  metric with the same unit.
- The agent never installs: its proposal fills the form (Phase 4); the automatic first turn goes.
- Only screenshot 13 changes; the set stays at 19 images.
- Cost gate (parent's decision, 2026-11-17): Phase 1 passes when (a) with the owner's real
  configuration (zero saved rules) ingest time is unchanged from master within noise; (b) with 32
  worst-case rules (ten-minute holds, some beyond their limit but not confirmed) it costs no more
  than master with the same 32 rules; (c) the absolute added time with 32 rules, measured on the
  Pi without a profiler, is reported against the 5 s recording interval and the 72 ms ingest
  reference. The 10 ms figure stays as the target, with the measured number beside it. Why: the
  owner has no saved rules, 32 ten-minute rules is a worst case, and Phase 1 must not make that
  worst case worse than today's code; most of the excess over 10 ms is the unchanged lifecycle
  storing 32 more assessments, which master pays too. Undo: make the 10 ms bound a hard gate
  again (it then needs cheaper persistence of custom assessments before deploy).

## Open questions for the owner

None blocking. Follow-ups he may want: recorded ranges for rpm, speed, torque and the
transmission speeds, and a running-only "typical high" (broker work, measured against
`docs/broker-cpu.md`); a parked battery rule confirms only when fresh recordings span the hold,
and parked wakes are brief, so the existing built-in parked battery warning remains the parked
safeguard.

## Review

- Round 1, gpt-6-astra on `aa25ae9`: hold. Each finding was reproduced on 2026-11-17 with the
  in-memory historian probes and is closed as follows.
  1. High, relaxed cap admitted expired readings after an unavailable snapshot: closed by
     semantics steps 2 and 4 (current reading bounded by the metric's stale limit; an
     unavailable or stale recording ends the run); current-reading tests on a real historian.
  2. High, engine-running rules rarely confirm: closed by step 3 (RPM from the same snapshot,
     nothing captured later); engine tests with distinct capture and observation times.
  3. High, regime filtering bridged a normal reading: closed by step 4 (snapshot-ordered run, no
     regime filter); regime tests; built-ins untouched, proven by the oracle and replay.
  4. Medium, "11 metrics can never fire" overstated: reworded to "rarely" (reopened in round 2,
     item 1).
  5. Medium, chat installer bypassed the store: both installers go through
     `custom_warning_store.install` in Phase 2, the chat keeping its exact-proposal check.
  6. Medium, help-panel reopening and exhaustion undefined: section 4 defines read-on-open,
     append-on-send, request-id replay, `busy`/`conversation_full` reasons and New chat.
  7. Medium, a retried request could resurrect a removed rule (reopened in round 2, item 3).
  8. Medium, rollback could lose or race rules (reopened in round 2, item 4).
- Round 2, gpt-6-astra on `56ef026`: hold. Reproduced on 2026-11-17 unless noted.
  1. Medium, five seconds is a setting, not an invariant (reproduced: seven recordings 1 s apart
     stored and satisfied a seven-reading rule; a restart 0.5 s after the last snapshot was
     accepted): closed by defining rules in time (hold T, gap G, floor), no reachability
     refusals, advice from the reported interval, a query bounded by T + G + stale limit in
     keyset pages; time-contract and query-bound tests cover fast, slow and mixed cadence and a
     restart.
  2. High, Phase 1 would silently change saved rules (reproduced: the legacy field-duty rule
     gives `warning, 2/2` today and its 4 s gap is under the 5.2 s capture spacing): closed by
     the zero-rules precondition checked with the advisor stopped (the van had 0 on 2026-11-17),
     the owner's per-rule decision otherwise, and refusal of old-bundle and pre-update installs.
  3. Medium, a pending ledger entry could resurrect a removed rule (confirmed by tracing the
     plan's own write order): closed by removing the ledger; the row id is derived from the
     request id, removed ids live in the rules file, every operation is one durable write, every
     crash state is listed, and the 256-id / 600 s retry bound is stated instead of claimed
     absolute; crash-point tests.
  4. Medium, the archive tool could overwrite archived rules on rerun (confirmed by tracing):
     closed by deleting the tool; rollback renames the whole file to a new timestamped name with
     `mv -n` and roll-forward renames it back only over an empty slot.
  Lows: the RPM defect is timestamp-dependent (reworded, with both probes); the lag direction on
  the eleven-metric line corrected; the RPM and the metric each meet their own freshness bound
  (step 3); the query exposes `observed_us`; custom-rule test fixtures may change, built-in tests
  may not.
- Round 3, gpt-6-astra on `5296ab4`: hold. Probes on 2026-11-17 used a real historian
  (table under "Custom rule semantics").
  1. High, observation-time hold confirmed across a normal recording after a source switch
     (reproduced: held 34 s, warning, with five seconds of qualifying recordings): closed by
     timing the hold per recording on the broker's monotonic clock, observation times used only
     for freshness (semantics steps 4–6); the reviewer's case is a regression test (`normal`, held
     5 s).
  2. Medium, wall-clock steps inflate the hold (reproduced: +10 s step confirmed at 20 s
     elapsed): closed by the same clock; backward steps (historian refusals) show as a gap over G;
     residual at most 0.05 % (NTP slew); forward and backward step tests; built-ins only pass the
     argument through.
  3. Medium, rollback left v2 proposals installable by the old advisor (confirmed against the
     current `apply_warning`, `open_chat` and the chat loader): closed by storing v2 proposals
     under `rule_proposal*` keys the old advisor never installs; a structural test and a
     pre-install check with the deployed ref (reopen and approve gives 409, no rules file).
  4. Medium, roll-forward could restore the empty file (confirmed by tracing): closed by distinct
     name patterns `v2-saved-<time>-<N>rules` and `v1-empty-<time>`, a count check before and
     after the restore, and a scratch test of the branch before install.
  5. Medium, no form control for a floor above two (confirmed in the plan text): closed by
     Minimum separate readings under Exact settings (2–60, default 2); presets keep it; the preview
     and Review values state any floor above 2.
  Notes: the retry-guarantee text now says any 256 removal requests, absent ids included, can
  evict a tombstone; the cost gate before install uses `telemetry-history-profile` with 32 rules
  (no walk remains, so cost is independent of hold length and cadence).
  Changed as a consequence: the paged walk, `snapshot_readings_page` and the query-bound tests
  are replaced by a one-recording query, `RuleHolds` and clock tests; `insights.py` and
  `broker.py` join Phase 1 to pass the recording clock; a broker restart restarts a hold (an
  open warning still holds through `_open_warning`); with one cached reading able to span a hold,
  the floor of 2 is enforced rather than implied.
- Round 4, gpt-6-astra (plan revision 3): `Verdict: ship`. Three lows, folded in: the cost gate is
  a relative check because `telemetry-history-profile` runs on a compute worker against an
  in-memory database, plus one live Pi ingest-time sample after install (section 8); the 32-rule
  benchmark includes rules beyond their limit but not yet confirmed; after a rollback the old
  advisor's replay sends `turn.get("proposal")`, so a v2 proposal reaches its model as null
  (accepted, context only; section 8 rollback step 4).
- last reviewed: plan round 4 by gpt-6-astra (ship); Phase 1 implementation not yet reviewed.

## Status

Phase 1 built in worktree `.claude/worktrees/agent-a67697fc393f05642` (branch
`worktree-agent-a67697fc393f05642`), not reviewed, not merged, not deployed. Verification results
are recorded below as they are run; the deploy (section 8, Phase 1) has not started.

Builder decisions within the plan (each undoable):

- `custom_warning_contract.relay_allowed` is deferred to Phase 2 (no relay route in Phase 1).
- Replay sends only `rule_proposal` to the model, so a pre-update turn's proposal reaches it as
  null.
- `remove_saved_warning` keeps the owner filter until Phase 2 makes rules vehicle-wide.
- An exception in one rule's assessment stops that rule alone and lists its id in `rejected_ids`
  (the plan's per-rule tolerance); `error` is set only when the file or the shared readings query
  fails.
- Health has no card that shows candidates, so the Early warning card gained one status line per
  custom candidate ("… beyond its limit for 12 of 30 s, waiting to confirm.") and the rejected
  count line; a small render change in `Health.jsx`.

Verification (Phase 1):

- Related tests after the rebase onto master `36f520a` (39 files: custom warnings, rules file,
  historian, broker, drift, event history, insights, delivery, replay, rules_v3, web_v2, chat,
  pairing, CLI lock isolation): 579 passed. After the optimisation: `test_custom_warnings`,
  `test_custom_warning_file`, `test_vehicle_early_warning`, `test_warning_replay`, advisory
  episodes, insights, lifecycle regressions, warning package, drift and rules_v3: 223 passed.
  Dashboard `health`, `WarningChat.helpers` and `demoDialogs` node tests: 82 passed. Whole suite: run on the commit that adds this line (job id
  and summary in the builder's report to the parent).
- Built-in invariance, oracle: `tests/advisory_lifecycle_oracle.py --repo-root <worktree>` output
  byte-identical to the master baseline (`1ed6d7f`; no oracle-covered file changed on master since).
- Built-in invariance, replay: `tools.warning_replay replay --evaluator both --current-ref master`
  on `export-20261003.sqlite3` (master `e5344b8`'s evaluator package from git against the
  worktree's; five budgeted chunks each, resumed): all 5,851 ticks evaluated by both; counts,
  persistence totals (37,549 inconclusive, 0 opened, resolved, updated or notified), recorded
  outcomes, simulated deliveries and query verification identical; the summaries differ only in
  timing, chunk boundaries, memo statistics and evaluator identity. Built-in transitions match. The
  export opens no episode under either evaluator, so this check is weak; the oracle is the strong
  one. The two 93 MB replay copies were deleted afterwards (reports kept under the worktree's
  `tmp/vehicle_data/warning_replay/`).
- Rollback check: a v2 setup chat saved by this advisor, reopened by master `36f520a`'s
  `WarningChatManager` (stub runner, private CLI lock): `open_chat` returns it with its transcript,
  `apply_warning` with its `rule_proposal_id` answers 409 "Review the latest completed proposal
  before applying it", and no rules file is created. Pass.
- Optimisation after the first cost run (`63c5f5c`): one evaluation reads the open advisory
  episodes at most once for every custom rule (`custom_warnings.OpenWarnings`; 32 reads for 32
  waiting rules before, 1 after, pinned by
  `test_one_episode_read_per_evaluation_for_all_waiting_custom_rules`), and the rule revision
  digest is cached per distinct rule. Oracle rerun on that commit: byte-identical to the
  `1ed6d7f` baseline (no oracle-covered file changed on master through `e5344b8`).
- Cost gate, first look (Mac worker m4mac.01, `telemetry-history-profile`, under cProfile, before
  the optimisation; 32 rules with ten-minute holds, every catalog metric, 16 beyond their limit):
  ingest 183/230 ms without rules and 237/253 ms with 32; custom evaluation 9.2–9.7 ms; storing
  the 32 extra assessments in the unchanged lifecycle about +20 ms.
- Cost gate, Pi (the gate of record; parent's definition under Decided). vanpi, no profiler,
  `nice -n 10`, the in-memory export harness of `telemetry-history-profile` (its SQL statement
  recorder adds overhead to every statement, so absolute times sit well above the broker's), 20
  measured recordings 5 s apart per run, six runs per case in ABBA order, tip `63c5f5c` (measured
  as `77857b4`, the same code before the rebase onto `e5344b8`) against a `git archive` of master
  `e5344b8`. The van was on (broker, drive recorder, active-drive helper
  running): 1-minute load 3.3–5.4 during the runs. Master needs the old rules (gaps within the
  stale cap, `context-32old`) or it rejects them; the tip ran the same file. Medians of run
  medians, [range]:

  | Case | Ingest wall ms | Custom block ms |
  | --- | ---: | ---: |
  | master, no rules | 290.6 [278.6–347.1] | 0 |
  | tip, no rules | 292.9 [265.6–342.6] | 0.02 |
  | tip, 32 rules | 344.8 [284.1–395.9] | 5.1 [4.1–6.0] |
  | master, 32 rules | 495.7 [465.8–663.1] | 86.9 [81.1–121.4] |

  A second six-run batch at 40 recordings gave no rules 343.2 (master) and 314.5 (tip), and with
  32 rules 365.1 (old file) and 339.3 (ten-minute holds, 15 s gaps) on the tip, custom block
  4.8 ms; master with 32 rules exceeds that harness's 4096-statement cap at 40 recordings (its
  per-rule history queries), so it has only the 20-recording batch.
  (a) Zero rules: unchanged within noise (+2.3 ms in one batch, −28.7 ms in the other). Pass.
  (b) 32 rules: tip 344.8 ms against master 495.7 ms; custom block 5.1 against 86.9 ms. Pass.
  (c) Added by 32 worst-case rules on the Pi: about +25 to +52 ms per recording (0.5–1.0 % of the
  5 s interval), of which the custom evaluation is about 5 ms and the rest is the unchanged
  lifecycle storing 32 more assessments. The 10 ms target is met by the custom evaluation and not
  by the whole ingest; the 72 ms Mac reference is not comparable to this harness's Pi figures.
  Gate verdict under the parent's definition: pass. Raw results: builder scratchpad
  `costgate/pi-results{,2,3}.jsonl` (the first, unordered three-run batch is superseded by the
  ABBA batches: its order effects exceeded the difference measured).

Next step: the Phase 1 implementation review by gpt-6-astra (diff, this note, test output); then the
parent merges and deploys by section 8.
