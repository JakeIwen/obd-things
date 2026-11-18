# Early-warning form (Add Early Warning redesign)

tier: T3 (custom-rule evaluator semantics in the broker, a new historian query, a new advisor write
route that installs monitoring rules without a model proposal, a change to the web relay
allowlist, and a rollback tool that rewrites the owner's rules file)
last reviewed: plan revision awaiting re-check (round 1 by gpt-6-astra on `aa25ae9`: hold)
plan written against: commit `ce71087` (master `0393eee` plus four early-warning commits of
2026-11-17). Master has since gained the same content, a small review fix and two unrelated
dashboard commits; none touches the evaluator, historian or advisor logic below.

## Objective

Make "Add early warning" on the dashboard's Health view a plain form that saves a custom
threshold rule with no internet and no model turn, move the model to optional help inside that
form, and first make custom rules confirm correctly, which today they mostly do not. "Ask Codex"
on a warning card (explaining a recorded event) is unchanged.

## What works today (for the owner)

Verified on 2026-11-17 by in-memory probes against the real historian (`TelemetryHistorian`
over a temporary SQLite file, snapshots ingested through `ingest_snapshot` exactly as the broker
does, rules evaluated with `custom_warnings.evaluate_rule` at each capture time). The Pi records a
snapshot at least 5 s apart (the history loop waits 5 s after each one), in practice about every
6 s (p50) and up to 8 s (p90) per `docs/history/early-warning-rationale.md`; broadcast values are
already 0 to about 4 s old when recorded because the passive collector cycles about every 4.3 s.

- **A rule with "only while the engine is running" never confirms**, on any metric. Each
  reading looks up RPM at its own observation time but can only see snapshots captured before
  that moment, so it gets the previous snapshot's RPM, already about 5 s or more old and past
  the 5 s limit. Probe: seven readings 5.2 s apart, 0/7 counted; even at 4.9 s apart, 6/7.
- **Rules on battery voltage, the four tire pressures and VVT oil temperature, with the engine
  option off, do confirm** (their gap may be up to 30 s or 15 s). Probe: seven battery readings
  5.2 s apart, 7/7, warning. Two defects remain: a normal reading recorded in a different
  driving condition (another RPM band, speed band or temperature band) is skipped instead of
  breaking the run, so the warning can confirm across it; and a value the source repeats from
  cache can break the run.
- **Rules on the other eleven catalog metrics** (coolant, oil pressure, rpm, speed, target torque,
  transmission oil temperature, output and turbine speed with a gap of at most 5 s; power,
  torque and alternator field duty with at most 4 s) **rarely confirm**: only when the readings'
  age at recording happens to shrink from one snapshot to the next. Probe: coolant, 7 readings
  5.2 s apart, 1/7; but 2 readings recorded 5.2 s apart and observed 4.8 s apart did confirm.
- A rule never warns on a reading older than its metric's stale limit today, because the gap is
  capped at that limit. Simply raising the gap (the first version of this plan) would break that.
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

Each was checked against `ce71087`.

- A rule is exactly eight fields (`custom_warnings.FIELDS`): `title, metric, operator,
  threshold, persistence_observations, window_seconds, max_age_seconds, engine_running`.
  `validate_rule` limits: title 1–120 chars; metric in `catalog()`; operator `above`, `below`,
  `absolute_above`; threshold ±1,000,000; persistence integer 2–60; window 5–600 s; max age
  1..min(60, the metric's `stale_after_seconds`) and ≤ window; `engine_running` bool; absolute
  threshold ≥ 0. Field names are a file contract: older brokers reject any other key set.
- `catalog()` = numeric metrics with a `verified` or `observed_alfa_scale` source: 17 metrics.
  Stale limits: 4 s (crankshaft power, crankshaft torque, field duty), 5 s (coolant, oil
  pressure, rpm, target torque, transmission oil temperature, output and turbine speed, vehicle
  speed), 15 s (VVT oil temperature), 30 s (battery voltage, four tire pressures). Battery
  voltage has three qualified sources (B-CAN `0x46C`, C-CAN `0x41A`, cluster DID `1004`).
- Rules live in `/var/lib/van-telemetry-advisor/early-warnings.json` (`{"version":1,"rules":[…]}`,
  each row `{id (64 hex), rule, approved_at, chat_id, owner}`), written only by the advisor with
  `engine_off_voltage._atomic_json` (temp file, fsync, rename; no parent-directory fsync, a
  pre-existing limitation), at most 32 rules; readers refuse a file over 128 KiB.
- The broker (`warning_engine/evaluator.py`, custom block near line 498) reloads the file on
  every evaluation with `load_rules`, which validates every row: one row that fails validation
  makes every custom rule stop and sets `custom_rules.error` (built-ins carry on).
- Evaluation runs inside `insights.ingest_snapshot` right after each snapshot is stored, at
  `at = captured_at` (`broker.py` history loop, about line 2649; the loop reads `captured_at`
  at its top and waits `--history-interval 5` after the work, `projects/vehicle_data/systemd/
  van-telemetry.service`).
- History storage: `snapshots` (one row per capture, `captured_us` unique, `trip_id`, regime),
  `metric_samples` (at most one row per snapshot and metric, `UNIQUE(snapshot_id, metric)`, with
  `observed_us`, `source_age_ms`, `freshness` decided at capture from the broker's `age_ms` and
  `stale`), and `metric_gaps` (intervals in which a metric was missing, stale or undated). A
  metric the broker reports unavailable gets no `metric_samples` row in that snapshot.
- Today's custom evaluation (`custom_warnings.evaluate_rule`): latest row by `latest_sample(at,
  fresh_only=False)`, accepted when fresh at capture and observed ≤ `max_age_seconds` before
  `at`; run from `recent_numeric_samples` (filtered to the latest reading's regime, source,
  quality, provenance and trip, deduplicated by `observed_us`, ordered by observation time),
  broken when the observation spacing exceeds `max_age_seconds`; engine running per reading via
  `latest_sample("engine.rpm", at=<the reading's observed_at>)`, which filters by capture time.
- Built-ins (`warning_engine/`) use effective age = recorded `age_ms` + time since capture
  (`samples._sample_age_seconds`) and RPM running intervals from
  `continuous_numeric_condition`; they allow 8 s per observation in windows
  (`OBSERVATION_SPACING_ALLOWANCE_SECONDS`) and a 20 s running gap.
- Episodes: the lifecycle closes an open episode administratively when its rule key leaves the
  assessment list (`advisories._retire_absent_advisories_locked`, "rule retired from
  authoritative evaluator catalog") or its `rule_revision` changes; `evaluator_revision` is
  informational.
- Recorded ranges already in the dashboard store: `summary.history.metric_trends[metric]` for
  the 11 metrics in `insights.DEFAULT_HISTORY_METRICS` (battery, oil pressure, coolant, VVT oil,
  crankshaft power, transmission oil, field duty, four tires), each with `days_7` and `days_30`
  (`minimum`, `mean`, `maximum`, `sample_count`; parked and running together, from minute
  rollups) and `prior_trips` (`median_of_trip_means`, `mad_of_trip_means`, `trip_count`).
  Missing: rpm, speed, torque, target torque, output and turbine speed; percentiles; a
  running-only split. The advisor can read the same product with a cached `GET /v1/history`.
- The broker serves its catalog: `GET /v1/metrics` (`broker.list_metrics`) and `catalog` inside
  `/v1/snapshot`; `/v1/health` carries the evaluator's `custom_rules` status.
- Relay: `web.py::_assistant_request` allows a fixed list of `/v1/assistant/` paths (plus
  `dtc_research_contract.relay_allowed`), requires an exact trusted origin, same-origin fetch
  metadata, JSON POSTs, and well-formed `X-Van-Assistant-Key` / `X-Van-Chat-Client` headers. The
  advisor (`ChatHandler.handle_request`) checks the access code in `manager.authorize` before any
  route, derives `owner = sha256(client id)`, and runs warning routes under `manager.lock`.
- Chat install (`WarningChatManager.apply_warning`): exact latest completed proposal and its
  `proposal_id` with `confirmed: true`; strict `load_rules`; row id = `proposal_id`; no broker
  check, no duplicate-trigger check. Chat limits: 24 turns, 96 request ids per chat; `open_chat`
  returns an existing matching chat without processing a new message.
- The Add early warning button (`views/Health.jsx`) shows only when the web listener advertises
  `warning_chat_enabled`, and opens `WarningChat.jsx` in `add-warning` mode, which immediately
  starts a model turn ("Help me set up a new early warning.").

## History findings (owner rule: redesigns start from the commit history)

| Piece | Added in | Stated reason | Still holds? | Plan |
| --- | --- | --- | --- | --- |
| Setup chat, proposal ID approval, `apply-warning`/`remove-warning`, rules file, 32 cap | `84d95f3` (2026-09-20), ported to Preact in `2fe75a4` | Only the latest completed, validated proposal can be installed, with its exact ID and explicit approval | Yes for chat-made rules; the form keeps it in a stronger form (the owner composes the exact fields; the model installs nothing) | Chat install goes through the common store in Phase 2; deleted in Phase 4 |
| Rules listed and removable only by the creating browser | `84d95f3` | None for rules (the privacy reason is for transcripts) | No: every rule fires vehicle-wide but each browser sees a different list | Rules become vehicle-wide; transcripts stay per browser |
| `max_age_seconds` ≤ the metric's stale limit | `84d95f3` | None stated | It does two jobs: keeps the latest reading current and bounds spacing. The second makes most rules unreachable | Phase 1 splits them: latest reading bound by the metric's stale limit (fixed), gap ≤ min(60, window) |
| Run built from `recent_numeric_samples` (regime-filtered) | `84d95f3` | Reused the tier-1 query, which conditions on regime by design | No for owner thresholds: a filtered-out normal reading vanishes | Phase 1: snapshot-ordered run, no regime filter |
| RPM looked up at each reading's `observed_at` | `84d95f3` | "Engine running at every reading" | Intent yes, lookup no (filters by capture time) | Phase 1: RPM from the same snapshot |
| Catalog limited to verified / `observed_alfa_scale` numeric metrics | `84d95f3` | Owner rules only on trusted decodes | Yes | Keep |
| Field duty allowed | `43d5d37` | An owner-defined rule is an owner reference, not alternator evidence | Yes | Keep; the form shows the field-duty note |
| "What you can monitor", `catalog_summary`, `RULE_READINGS` | `78d510b`, `ce71087` | The owner could not see what he may monitor | Superseded by the form's metric picker | Delete in Phase 4 |
| Setup waiting wording | `7f2c5ce` | The setup chat has no event | Yes while the help chat exists | Keep for the help panel |
| Hold an open custom warning through a short run (`_open_warning`) | `2fe75a4` | A confirmed warning should not close and reopen on a gap | Yes | Unchanged |
| Access-code helpers shared by both dialogs | `5478077` | One copy for warning chat and DTC research | Yes | The form reuses them |

## Custom rule semantics (Phase 1; the contract the form promises)

Plain words, for the owner and the docs: *a warning confirms when the Pi's own recorded
readings of the metric were beyond the limit at every recording, with nothing missing in
between, for N readings in a row; with "only while the engine is running", every one of those
recordings also showed the engine above 400 rpm.*

Precisely, evaluating rule R at time `at`:

1. **Recording.** A recording is one stored snapshot (captured at least 5 s after the previous
   one). The metric's reading in a recording is its `metric_samples` row in that snapshot. A
   reading is *qualified* when it is fresh at capture, numeric and finite, has the catalog unit,
   and comes from one of the metric's catalog sources with a qualified quality. A change of
   source between recordings (battery voltage has three) is allowed: each reading must qualify
   on its own.
2. **Current reading.** S0 is the newest snapshot captured at or before `at`. Its reading must be
   qualified and current: effective age = recorded `age_ms`/1000 + (`at` − S0 capture) ≤ the
   metric's `stale_after_seconds`. Otherwise the state is `unavailable` ("No current reading").
   A snapshot in which the broker reported the metric unavailable has no reading, so the
   previous fresh value can never stand in for it.
3. **Engine running** (when `engine_running`): S0's `engine.rpm` reading must be qualified,
   effective age ≤ 5 s, value > 400; otherwise `unavailable` ("Fresh engine-running evidence is
   absent"). For older recordings, the RPM reading of that same snapshot must be qualified (fresh
   at that capture) and > 400. Both readings were then at most 5 s old at that capture, so they
   describe the same moment; no RPM from a later snapshot, and nothing captured after `at`, is
   ever used.
4. **Run.** Walk snapshots from S0 back in capture order. A recording continues the run only
   when: it is in the same trip as S0 (both without a trip counts as the same); the time from it
   to the next newer recording is at most the rule's gap (`max_age_seconds`); it was captured
   no earlier than `at` − `window_seconds`; its reading is qualified and beyond the limit; and,
   with the engine option, its RPM passes step 3. The first recording that fails ends the run;
   nothing is skipped. Driving condition (regime) is not considered at all.
5. **Count.** The run's readings count once per distinct observation (`source`, `observed_us`): a
   value the source repeats from cache across recordings neither counts twice nor breaks the run.
   Confirmed when the count reaches `persistence_observations`.
6. Not beyond the limit at S0: `normal`. Beyond but not confirmed: `normal` with a low-confidence
   candidate, or `warning` while an episode is already open (`_open_warning`, unchanged).
7. `rule_revision` stays `digest(rule)` so episodes open at deploy time are not retired;
   `evaluator_revision` becomes `owner-threshold-v2`.

Field meanings under these semantics (names unchanged, they are the file contract):
`persistence_observations` = readings in a row; `window_seconds` = all within; `max_age_seconds`
= longest time between two recordings. The latest reading's freshness is no longer a rule field.

What the evaluator guarantees, and so what the form may promise: N distinct readings come from
N different recordings, each at least 5 s after the one before, so a confirmed rule was beyond
its limit at every recording over at least (N − 1) × 5 s. It says nothing about moments between
recordings. A rule can never confirm when its gap is under 5 s (consecutive recordings are at
least 5 s apart) or when (N − 1) × 5 > window; these are *unreachable*. A gap under 8 s or a
window under 8 × N s is reachable but may rarely confirm (recordings are usually 5–8 s apart);
these get *advice*, never a refusal.

Built-in warnings do not change: no existing historian query, built-in rule, gate or lifecycle
function is edited; the custom path stops calling `recent_numeric_samples` and
`latest_sample`. Proof required before install: `tests/advisory_lifecycle_oracle.py` run against
the old and new checkouts with byte-identical `--output`; `tools/warning_replay.py replay`
(`current` = the deployed ref, `new` = the worktree, no custom rules file) on the vanpi export
`tmp/vehicle_data/warning_replay/export-20261003.sqlite3` with identical built-in transitions in
its report; every existing `tests/test_*warning*` and historian test unchanged and green.

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
   - **For at least** [`persistence_observations`, `window_seconds`, `max_age_seconds`]: a
     `<select>` of 15 s, 30 s, 1 min, 2 min, 4 min. Default 30 s.
   - **Only while the engine is running** [`engine_running`]: a checkbox. Default on, except for
     metrics whose name starts with a prefix in `form_defaults.engine_running_off_prefixes`
     (`battery.`, `tire.`). Hint: "Every reading must also show the engine above 400 rpm."
   - **Name on the warning card** [`title`]: prefilled ("Coolant temperature above 230 °F") and
     kept in step with the fields until the owner edits it.
   - **Exact settings** (collapsed): Readings in a row [2–60], All within (seconds) [5–600],
     Longest time between readings (seconds) [1–60, ≤ All within], and "Compare the size of the
     reading, ignoring its sign" (turns Above into `absolute_above`; disabled with Below).
     Editing any of these switches For at least to "Custom". Hint: "The Pi records a reading at
     least 5 seconds apart, usually every 5 to 8 seconds."
4. **Preview** (always visible): "Warn when Coolant temperature is above 230 °F at every reading
   for at least 30 seconds (7 readings in a row), only while the engine is running above
   400 rpm." Variants: "below"; absolute: "is more than 40 lb-ft from zero, in either
   direction"; custom: "for 9 readings in a row (at least 40 seconds) within 90 seconds"; engine
   off: no clause. Under it: "Readings are the Pi's recordings, about every 5 to 8 seconds; a
   missing reading starts the count again. Owner monitoring reference, not an OEM limit."
5. **Review** (replaces proposal approval): validates and turns the form read-only, showing the
   preview and the exact values ("7 readings within 60 s, at most 15 s apart"), with **Add
   warning** and **Edit**. Only Add warning sends the install, with `confirmed: true`. After
   success the list opens with the new row, the form resets, and the Health card refreshes
   (`onWarningsChanged`, as today).
6. **Help me choose** (Phase 4; section 4), a secondary button under the form. In Phase 3 the
   same place holds "Describe it to Codex instead", which opens the existing chat unchanged.

Derived values for a For at least choice of H seconds (constants in the contract:
`min_reading_spacing_seconds` 5, `typical_reading_spacing_seconds` 8, `gap_seconds` 15,
`window_floor_seconds` 60):

- readings N = H / 5 + 1: 15 s → 4, 30 s → 7, 1 min → 13, 2 min → 25, 4 min → 49. (5 min
  would need 61, past the 60 limit older brokers enforce, so the longest choice is 4 min.)
- window W = clamp(max(2 × H, 8 × N), 60, 600): 60, 60, 120, 240, 480. This holds N readings at
  the usual 5–8 s spacing.
- gap G = 15 s: tolerates a slow recording on a busy Pi; it is never shown in the main form.

So the promise "for at least H seconds" is exactly the guarantee above ((N − 1) × 5 = H). A
warning usually confirms after about (N − 1) × 6 s, and a missed or stale recording restarts the
count; the form's hint and the docs say so.

Validation (client messages; the server is authoritative and its `detail` is shown):

- No metric: "Choose what to watch."
- Limit empty or not a number: "Enter a number in °F." (unit of the chosen metric)
- Limit outside ±1,000,000: "Use a number between −1,000,000 and 1,000,000."
- Ignoring the sign with a negative limit: "Use zero or more when ignoring the sign."
- Name empty or over 120 characters: "Give the warning a name of up to 120 characters."
- Exact settings out of range: "Readings in a row must be a whole number from 2 to 60.", "All
  within must be 5 to 600 seconds.", "Longest time between readings must be 1 to 60 seconds and
  no more than All within."
- Unreachable (blocks Review; the install route refuses the same): "The Pi records readings at
  least 5 seconds apart, so a longest time between readings under 5 seconds can never be met."
  and "N readings take at least X seconds, longer than All within (W s), so this could never
  trigger." Each offers a one-tap fix ("Use 15 seconds", "Use W' seconds").
- Advice only (Review stays enabled): a gap under 8 s or W < 8 × N ("Readings usually arrive 5
  to 8 seconds apart, so this may rarely trigger."); a limit outside the catalog
  `minimum`..`maximum` ("… readings range from −40 to 419 °F, so this could never trigger."); an
  Above limit below the 30-day low or a Below limit above the 30-day high ("This would trigger on
  almost every reading.").

A saved rule copied into the form (section 5) keeps every value. Only the two unreachable cases
block it, and only because the evaluator provably cannot confirm them.

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

#### Phase 1, broker and shared module

- `custom_warnings.py`: `evaluate_rule` implements the semantics above over the new historian
  query; `validate_rule` takes limits from the contract and caps `max_age_seconds` at min(60,
  window) (no stale cap); `catalog_from(definitions)`; `load_rule_rows(path) -> (rows,
  rejected)` (file-level checks as today; each row validated alone; rejected rows returned
  whole); `load_rules` stays as the strict wrapper; `unreachable(rule) -> str | None` and
  `advice(rule) -> str | None`; `legacy_compatible(rule) -> bool` (passes the pre-Phase-1
  validator: today's checks plus the stale cap), used by the rollback tool.
- `history/custom_rule_queries.py` (new mixin added to `TelemetryHistorian`'s bases; `queries.py`
  is already 1,609 lines): `snapshot_readings(metric, *, at, lookback_seconds, include_rpm)`
  returns, newest first, `{captured_at, captured_us, trip_id, reading, rpm}` for each snapshot
  captured in [`at` − lookback, `at`] (`reading`/`rpm` are sample dicts or `None`). One indexed
  query (`snapshots` by `captured_us`, then `LEFT JOIN metric_samples` on `(snapshot_id, metric)`
  for the metric and for `engine.rpm`), `LIMIT 200` (600 s ÷ 5 s + slack). Read-only; no schema
  change.
- `warning_engine/evaluator.py`: use `load_rule_rows`; `custom_rules` status gains
  `rejected_ids`, `unreachable_ids` and `evaluator_revision: "owner-threshold-v2"`; `error` only
  for file-level failures. A rejected row is not assessed, so its open episode is retired by the
  existing lifecycle ("rule retired from authoritative evaluator catalog"), exactly as removing
  it does; documented, and shown on Health.
- `custom_warnings.json` and `custom_warning_contract.py` (section 6).
- `custom_warning_rollback.py` (CLI, `python3 -m projects.vehicle_data.custom_warning_rollback`):
  `archive` moves every row that fails `legacy_compatible` from the rules file into
  `early-warnings.archived.json` beside it (each archived row whole, plus `archived_at` and the
  reason), keeps compatible rows verbatim, writes both with `_atomic_json` (archive first), and
  refuses to run while the advisor socket answers (writes must be quiesced). `restore` merges
  archived rows back (skipping ids already present, refusing past 32 rows or 128 KiB) and
  empties the archive. Both print counts only, never rule contents.
- `warning_chat.py`: `SETUP_INSTRUCTIONS` describes the new field meanings (gap = longest time
  between readings, 5–60 s, default 15; readings at least 5 s apart, usually 5–8; latest reading
  freshness is automatic); `apply_warning` also refuses `unreachable(rule)` with its message.
- dashboard `views/health.helpers.js`: "N saved warnings could not be loaded" from
  `rejected_ids`, and "N saved warnings can never trigger as saved" from `unreachable_ids`.

#### Phase 2, advisor store and routes

Routes, served by the advisor and relayed by both web listeners (same headers, same gate):

- `GET /v1/assistant/warnings` returns `{catalog: [{metric, unit, minimum, maximum,
  stale_after_seconds}], warnings: [{id, rule, loaded, rejected, unreachable, advice, source,
  created_at, created_here}], count, max_rules, warning_config_error,
  warning_evaluator_enabled, evaluator_ready}`. `loaded`/`rejected` come from the broker's
  cached `/v1/health` `custom_rules`; `unreachable`/`advice` from the shared functions;
  `created_here` = `row["owner"] == owner` (provenance only).
- `POST /v1/assistant/warnings` body exactly `{request_id, rule, confirmed}`. Route and chat both
  call `custom_warning_store.install(rule, *, owner, source, request_key, chat_id=None)`:
  1. `confirmed is True`, `request_id` matches `ID_RE` (32 hex), else 400 (route only).
  2. `validate_rule(rule)`, then `unreachable(rule)`, else 400 with the message.
  3. Broker readiness (before taking the lock): cached `GET /v1/health`; `custom_rules
     .evaluator_revision` must be `owner-threshold-v2`, else 409 "The telemetry service needs
     its update before warnings can be saved." (this is how the advisor knows Phase 1 is
     deployed); `GET /v1/metrics`, the rule's metric must be in `catalog_from(served)` with the
     same unit, else 409 "The telemetry service doesn't offer this metric yet; it needs a
     restart after the update." Broker unreachable: 503 "The telemetry service is unavailable,
     so the warning can't be checked; try again." This proves metric/unit compatibility and,
     through the revision, validator compatibility.
  4. Under `manager.lock`: tolerant read (`load_rule_rows`); replay check (below); a row with the
     same trigger (all fields but `title`) → 409 "An identical warning already exists:
     <title>."; ≥ 32 rows → 409 "Custom warning limit reached (32); remove one first."
  5. Append `{id, rule, approved_at, owner, source, chat_id}` (`id` = `sha256("form:" + owner +
     ":" + request_id)` for the form, the `proposal_id` for the chat), serialize, refuse with
     409 "The saved warnings file would be too large; remove a warning first." when over 128 KiB,
     then write with `_atomic_json`. Every write keeps rejected rows verbatim.
- **Replay ledger** (advisor-only file `early-warning-requests.json` beside the rules, never read
  by the broker): entries `{request_key, rule_digest, installed_id, state, at}`, `request_key` =
  `sha256(owner + ":" + request_id)`; at most 512 entries, pruned past 7 days (a dashboard
  request id lives only while one draft is open, so a retry after 7 days does not occur; the
  client drops its id on success, on Edit and on close). Order under the lock: write the entry
  as `pending`, write the rules file, mark it `installed`. On a request whose key is known:
  different rule digest → 409 "This request was already used."; `pending` → install if the row is
  absent, else mark installed and replay; `installed` and the row present → 200 with
  `replayed: true`; `installed` and the row absent → 409 "This warning was added and has since
  been removed; review it again to add it once more." So a lost reply followed by another
  browser's removal can never reinstall the rule.
- `POST /v1/assistant/warnings/<64 hex>/remove` `{confirmed: true}`: any paired browser may
  remove any rule, rejected rows included; idempotent; preserves every other row verbatim; still
  clears `applied` on the chat turn that installed it.
- The chat's `apply-warning` keeps its exact-proposal approval check in `warning_chat.py`, then
  calls `install(..., source="chat", request_key=proposal_id)`; `remove-warning` calls the
  store's remove. Both thereby gain tolerant reads, the readiness check, identical-trigger and
  unreachable refusal, the size check and the ledger.
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
route here stays authenticated, and the two changes merge in either order (different predicates,
different dispatch positions). Because the list no longer filters by the client-id hash,
re-keying browsers cannot orphan rules.

### 4. The agent's new place (Phase 4)

- **Help me choose** opens a compact panel in the form dialog: "Suggest a limit from my history",
  "Explain this metric", a two-line text box, the transcript, Stop, Retry, **New chat**, and the
  line "Readings as of <time>". Model and effort pickers sit in a collapsed "Model" disclosure.
  Nothing is sent until the owner taps a suggestion or Send: the automatic first turn is removed.
- Context (cached broker GETs only, as `event_context` does): `catalog()`, `focus_metric`
  (required, in the catalog), its `recorded_range` from `/v1/history` `metric_trends[metric]`
  without `sparkline`/`series`, its `latest` reading, the contract's spacing constants and
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
  `recorded_range` and say low/high include parked readings; explain readings in a row, the 5–8 s
  spacing and that a missing reading restarts the count; use the form defaults unless asked;
  explain field duty when it is the metric; plain words, not field names.
- The reply stays `{explanation, proposal}`, checked with `validate_rule` and `unreachable`. The
  panel shows the proposal's preview and **Use these settings**, which fills all eight fields;
  the owner saves through Review and Add warning.
- Compatibility window for dashboards still open on an old bundle: the Phase 4 advisor answers
  the removed `apply-warning`/`remove-warning` routes and a setup `POST /chats` without `metric`
  and `message` with 409, reason `dashboard_reload_required`, detail "The dashboard was updated;
  reload the page." (the old bundle shows `detail`), and the relay keeps forwarding those paths
  until a later cleanup removes them after the owner has reloaded both devices.

### 5. Saved custom warnings (Phase 3)

The "Your custom warnings (N of 32)" disclosure, open after a save. Each row: the preview
sentence, a status, "Added on this browser" when `created_here`, **Copy to form** (fills every
value; saving creates a new rule and the owner removes the old one; rules are never edited in
place), and **Remove** (`window.confirm`). Statuses: "Watching" (`loaded`); "Saved; starts on the
next check" (neither loaded nor rejected); "The telemetry service could not load this rule"
(`rejected`); plus "Can never trigger: <reason>" (`unreachable`, typically rules saved before
Phase 1 with a gap of 4 s) or "May rarely trigger: <reason>" (`advice`). A file-level error shows
as today.

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
  "evaluator_revision": "owner-threshold-v2",
  "limits": {"title_chars": [1, 120], "threshold": [-1000000, 1000000],
             "persistence_observations": [2, 60], "window_seconds": [5, 600],
             "max_age_seconds": [1, 60]},
  "min_reading_spacing_seconds": 5,
  "typical_reading_spacing_seconds": 8,
  "engine_running_rpm_above": 400,
  "engine_running_rpm_max_age_seconds": 5,
  "form_defaults": {"hold_seconds_choices": [15, 30, 60, 120, 240], "hold_seconds": 30,
                    "gap_seconds": 15, "window_floor_seconds": 60,
                    "engine_running_off_prefixes": ["battery.", "tire."]},
  "requests": {"install": ["request_id", "rule", "confirmed"], "remove": ["confirmed"]}
}
```

`custom_warning_contract.py` loads it and exports the limits, `FIELDS`, `MAX_RULES`,
`MAX_FILE_BYTES`, the spacing constants and `relay_allowed(method, path)`; no `METRICS` import,
so `web.py` stays light.

Phase 2 files: `custom_warning_store.py` (list, install, remove, ledger, readiness, duplicate,
size, tolerant read, atomic write; `warning_status` and `remove_saved_warning` move here from
`warning_chat.py`, which is over 800 lines); `warning_chat.py` (dispatch branches; chat install
and remove call the store); `web.py` (`custom_warning_relay_allowed` in the allowlist
expression); `api_schema.py` (`AssistantWarningsResponse`); docs: new
`projects/vehicle_data/docs/custom-warnings.md` (semantics in plain words, fields, routes,
storage, ledger, ownership, limits, deploy and rollback), `docs/warning-chat.md`, vehicle-data
README custom-warning paragraph; `AGENTS.md` map paragraph: "Custom early warnings (owner request
2026-11-17): the form is the way to add a rule. `custom_warnings.json` with
`custom_warning_contract.py` (fields, limits, cap, reading spacing, form defaults, routes;
shared with the dashboard), `custom_warnings.py` (catalog, validation, tolerant load,
evaluation semantics), `history/custom_rule_queries.py` (the snapshot-ordered readings query),
`custom_warning_store.py` (advisor list/install/remove and the replay ledger),
`custom_warning_rollback.py` (archive and restore for a rollback); contract
`projects/vehicle_data/docs/custom-warnings.md`; dashboard `src/customWarningForm.helpers.js`,
`dialogs/CustomWarnings{.jsx,.helpers.js}`, `styles/custom-warnings.css`."

Phase 3 files (dashboard only): `src/customWarningForm.helpers.js` (pure: defaults, derived
fields, preview, validation, unreachable/advice text, range lines, statuses, fill-from-rule),
`src/dialogs/CustomWarnings.jsx` and `.helpers.js` (controller over `createRequester`,
`useController.js`, `useModalDialog`), `src/styles/custom-warnings.css` (palette tokens only),
`src/app/dialogs.js`, `views/Health.jsx`; `WarningChat.helpers.js` `RULE_READINGS` reads the
contract; `tools/demo_assistant.mjs` `custom-warnings` scene (catalog slice, two saved rules: one
Watching, one "Saved; starts on the next check"); `readme_shots.playwright.js` entry 13 fills
coolant / Above / 230 / 30 s and taps Review, never Add warning; `demo_data.mjs` coolant trend
with a 30-day high near 221 °F; `dashboard/docs/caveats.md` field-duty line.

Phase 4 files: `warning_chat.py` (setup context, opening rules, instructions, compatibility
replies, deletions), `WarningChat.jsx`/`.helpers.js` (`warning-help` mode), new
`src/dialogs/WarningHelp.jsx`, `CustomWarnings.jsx` (hosts the panel; Use these settings),
`app.css` (drop chat-monitor, chat-saved and chat-proposal rules), docs.

Code shape: every new advisor function goes in `custom_warning_store.py`; the new historian
query in its own mixin; `WarningChat.helpers.js` (776 lines) must shrink in Phase 4, not grow;
strings in each helpers file's `TEXT` table; no inline styles (CSP).

### 7. Tests

Phase 1. A shared fixture `tests/custom_rule_history.py` builds a real `TelemetryHistorian` on a
temporary file and ingests snapshots made from `METRICS[...].public_dict()` with explicit capture
time, observation lag per metric and unavailable entries (the probe harness of 2026-11-17). Tests
in `tests/test_custom_warnings.py`, through `evaluate_rule` and `EarlyWarningEvaluator`:

- F1 `test_unavailable_snapshot_after_fresh_readings_is_unavailable_not_warning` (seven
  field-duty readings 5.2 s apart, then a snapshot with field duty unavailable: `unavailable`);
  `test_latest_reading_older_than_its_stale_limit_is_unavailable` (evaluate 5.3 s after a 4 s
  metric's last capture); `test_stale_snapshot_between_readings_breaks_the_run`.
- F2 `test_engine_running_rule_confirms_with_capture_and_observation_times_apart` (snapshots 5.2
  and 6.1 s apart, metric observed 0.1–3.5 s before capture, RPM 0.05 s: 7/7, warning);
  `test_engine_running_uses_only_rpm_from_the_same_snapshot` (RPM ≤ 400 in one middle snapshot
  breaks the run; RPM > 400 only in a later snapshot never rescues an earlier reading);
  `test_no_evidence_captured_after_at_is_used` (rows captured after `at` exist; result equals
  the result without them).
- F3 `test_normal_reading_in_another_regime_breaks_the_run` (seven high field-duty readings with
  a 20 % reading at 3,000 rpm between them: not confirmed, count resumes after it);
  `test_regime_change_without_a_normal_reading_does_not_break_the_run`.
- F4 `test_variable_source_lag_does_not_change_the_outcome` (same values, lags varying 0–3.5 s);
  `test_repeated_cached_observation_counts_once_and_does_not_break_the_run`;
  `test_run_stops_at_the_window_boundary`; `test_gap_between_recordings_over_the_rule_gap_breaks`;
  `test_trip_change_breaks_the_run`; `test_source_change_with_qualified_sources_continues`.
- `test_every_form_hold_choice_confirms_at_5_2_and_8_second_spacing_and_not_before_h_seconds`.
- `test_gap_may_exceed_the_stale_limit_but_not_the_window`;
  `test_unreachable_and_advice_boundaries` (gap 4.9/5/7.9/8; (N − 1) × 5 vs W);
  `test_legacy_compatible_matches_the_pre_phase_1_validator`.
- `test_one_rejected_row_leaves_the_other_custom_rules_evaluating` (`rule_ids` one,
  `rejected_ids` the other, `error` None); `test_rejected_rule_retires_its_open_episode`
  (real historian lifecycle: open, then reject the row, episode closed with the retirement
  reason); `test_file_level_corruption_still_reports_an_error_and_built_ins_continue`;
  `test_rule_revision_is_unchanged_so_open_episodes_survive_the_upgrade`.
- `test_contract_limits_are_the_validator_limits`;
  `test_min_reading_spacing_matches_the_broker_unit_history_interval` (parse
  `projects/vehicle_data/systemd/van-telemetry.service`);
  `test_catalog_from_served_definitions_equals_catalog`.
- `tests/test_custom_warning_rollback.py`: archive keeps compatible rows verbatim and archives
  incompatible ones whole; refuses while the advisor socket answers; restore round-trips; restore
  refuses past 32 rows or 128 KiB; output carries counts only.
- `tests/test_vehicle_historian.py`: `test_snapshot_readings_joins_metric_and_rpm_per_snapshot`
  (missing metric is `None`, limit and lookback bounds).
- Built-in invariance: the oracle and replay comparison named in "Custom rule semantics".
- dashboard `test/health.helpers.test.mjs`: rejected and never-trigger lines.

Phase 2, `tests/test_custom_warning_store.py`:
`test_confirmed_form_rule_installs_atomically_and_loads`,
`test_unconfirmed_or_extra_fields_are_refused_without_a_write`,
`test_same_request_id_replays_without_a_second_row`,
`test_reused_request_id_with_another_rule_is_refused`,
`test_lost_reply_then_removal_elsewhere_then_retry_does_not_reinstall` (finding 7 sequence),
`test_pending_ledger_entry_after_a_failed_rules_write_installs_on_retry`,
`test_ledger_is_bounded_by_count_and_age`, `test_identical_trigger_is_refused`,
`test_form_and_chat_duplicates_are_refused_both_ways`, `test_cap_of_32_is_refused_without_a_write`,
`test_write_over_128_kib_is_refused_without_a_write` (with large rejected rows present),
`test_rejected_rows_survive_form_and_chat_writes_and_are_removable_by_id`,
`test_broker_without_v2_evaluator_refuses_form_and_chat_installs`,
`test_metric_missing_from_broker_catalog_is_refused_for_form_and_chat`,
`test_broker_unreachable_refuses_install`, `test_unreachable_rule_is_refused_for_form_and_chat`,
`test_list_is_vehicle_wide_and_marks_created_here`, `test_any_paired_browser_removes_any_rule`,
`test_form_and_chat_paths_accept_and_refuse_the_same_rules`, `test_chat_install_keeps_exact_proposal_approval`.
`tests/test_warning_chat.py`: `test_custom_warning_routes_are_relayed_only_as_get_list_and_post_install`
(other methods and paths → 404; origin, fetch-metadata, header and JSON gates apply);
`test_custom_warning_routes_require_authorization`; setup approval test updated for vehicle-wide
removal.

Phase 3: dashboard `test/customWarningForm.helpers.test.mjs` (derived fields for every hold choice
pass the limits and `unreachable`, and (N − 1) × 5 equals H; preview for above, below, absolute,
custom and engine-off; validation, unreachable and advice messages with their one-tap fixes;
range lines, 7-day fallback, no trend; default engine checkbox by prefix; auto title until
edited; fill from a saved rule, including a gap-4 legacy rule, and from a proposal; statuses);
`test/CustomWarnings.helpers.test.mjs` (401 shows pairing; status without `custom_warning_form`
opens the old chat; load shows catalog and list; Review then Add warning posts `confirmed: true`
once; a double tap and a retry after a timeout reuse one `request_id`; Edit and a changed draft
get a new one; 409, 503 and the "removed since" reply shown; remove; `onWarningsChanged`).
`tests/test_run_tests.py` passes. Screenshot 13 retaken (`node tools/readme_shots.mjs --only 13`
on vanpi; `--promote`), manifest caption "Add Early Warning form with an illustrative coolant
rule, its recorded range and preview, at the review step; nothing was saved." and the root README
alt text. On-screen keyboard check: the Playwright pair also shoots the form at 800×640 (the
tablet's height with the Android keyboard open) with the Limit field focused, and a dashboard
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
`warning-help` (no request on open, reads the metric's chat, first send posts `/chats` with
message and metric, New chat, full and busy reasons, proposal exposed for the form); delete the
tests of removed code.

Every change set runs the whole suite (`run_tests.py`, by the vanpi route in `AGENTS.md`).

### 8. Deploy and rollback

General rules: deploy phases in order and roll back in reverse order (never roll back Phase 1
while Phase 2 is deployed). Never paste the rules file, the archive, the ledger or the access
code into an agent conversation; the tools print counts. Before Phase 1 the deployer keeps a
dated copy: `sudo cp -p /var/lib/van-telemetry-advisor/early-warnings.json
/var/lib/van-telemetry-advisor/early-warnings.json.before-phase1` (skip if absent), and reports
to the owner only the number of saved rules whose gap is under 5 s
(`sudo jq '[.rules[] | select(.rule.max_age_seconds < 5)] | length' <file>`): those become
"can never trigger" until replaced.

**Phase 1, custom rules fire correctly** (broker, then advisor, then build):

1. `s restart_broker` (parked; the sanctioned restart; `PartOf=` restarts both web listeners).
2. Check: `curl -s --unix-socket /run/van-telemetry/api.sock http://broker/v1/health | jq
   .custom_rules` shows `enabled: true`, `error: null`, `evaluator_revision:
   "owner-threshold-v2"`, `rejected_ids: []`, and as many `rule_ids` + `rejected_ids` as rows.
3. `sudo systemctl restart van-telemetry-advisor.service` (prompt wording, shared validator,
   unreachable refusal in the chat).
4. Dashboard build (frontend-only).
5. Journals of the four units clean. On the next drive the owner's rules show candidates or
   warnings on Health as the semantics say; agents do not install live rules.
6. Rollback (writes quiesced throughout, nothing lost):
   1. `sudo systemctl stop van-telemetry-advisor.service` (the only rules writer; chat and
      research pause).
   2. `sudo cp -p early-warnings.json early-warnings.json.rollback-<YYYYMMDDHHMM>` (the complete
      current file, with every addition and removal since deploy).
   3. `sudo -u <advisor user> python3 -m projects.vehicle_data.custom_warning_rollback archive`
      from the Phase 1 checkout: compatible rows stay, incompatible rows move whole to
      `early-warnings.archived.json`.
   4. Revert the code, `s restart_broker`, and check `/v1/health` `custom_rules.error` is null
      and `rule_ids` counts the kept rows.
   5. Only then `sudo systemctl start van-telemetry-advisor.service` and rebuild.
   6. Roll-forward later: deploy Phase 1 again, then `custom_warning_rollback restore` with the
      advisor stopped, then start it.

**Phase 2, advisor store and routes** (advisor, web listeners). Prerequisite: Phase 1 deployed;
the store also enforces it through `evaluator_revision`. Each intermediate state is safe: an old
bundle never calls the new routes; a new allowlist before the new advisor gets the advisor's 404.

1. `sudo systemctl restart van-telemetry-advisor.service`.
2. `sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service`.
3. Checks: through each listener an unpaired `GET /v1/assistant/warnings` answers 401 (relayed
   and gated), not 404, e.g. `curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin:
   http://192.168.6.103:8765' http://192.168.6.103:8765/v1/assistant/warnings`; the owner adds
   and removes one test rule through the old chat (now through the store).
4. Rollback: revert, restart advisor and both web listeners. Rows written by the store carry only
   extra keys (`source`) that the old advisor ignores; the ledger file is ignored. Nothing is
   lost.

**Phase 3, the form** (dashboard build only; allowed at any time; safe before Phase 2 because
the button falls back to the chat without `custom_warning_form`). Checks: the owner opens the
form on the tablet, sees the list, adds one test rule (Watching within about 10 s), removes it,
and checks the pinned buttons with the keyboard open. Rollback: rebuild the previous ref.

**Phase 4, help inside the form and deletions** (advisor, web listeners, build, in that order).
Checks as Phase 2, plus one help question with internet, reopening a metric's chat, and saving
through the form with the Pi offline. Old bundles get "reload the page" (section 4). Rollback:
revert, restart advisor and web listeners, rebuild; rules untouched.

Chats and rules saved before the change: every rule keeps its row and appears in the
vehicle-wide list; gap-under-5 rules show "Can never trigger" and the owner copies and replaces
them. Old setup chats stay on the Pi (counting toward the 200-chat limit) but are not reachable
from the dashboard after Phase 4, because help chats are matched per focus metric.

### 9. What to delete (Phase 4 unless noted)

- `warning_chat.py`: `apply_warning` and the apply/remove dispatch bodies (replaced by the
  reload-required reply), the automatic first question (`84d95f3`), `public()`'s `catalog` and the
  `catalog_summary` import (`78d510b`, `ce71087`), the status `warnings` fields (`84d95f3`).
- `custom_warnings.py`: `catalog_summary`; the stale cap and the regime-filtered run (Phase 1,
  `84d95f3`).
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

1. **Phase 1, custom rules fire correctly** (broker, advisor, build): semantics, query mixin,
   relaxed gap, tolerant load with `rejected_ids`/`unreachable_ids`/`evaluator_revision`,
   contract, `unreachable`/`advice`, rollback tool, prompt wording, Health lines, invariance
   proof. Plan re-check, then review before install.
2. **Phase 2, advisor store and routes** (advisor, web listeners): store, ledger, both
   installers through it, routes, relay, vehicle-wide list, docs, map. Review before install.
3. **Phase 3, the form** (build only): dialog, list, screenshot 13. Review before install.
4. **Phase 4, help inside the form and deletions** (advisor, web listeners, build). Review before
   install.

Each phase is one change set, built in a worktree, with a cross-family review before install.

## Decided (each undoable)

- Custom-rule semantics as above: recordings, current-reading bound by the metric's stale limit,
  RPM from the same snapshot, no regime filter, any non-qualifying recording breaks the run,
  distinct observations count. Undo: restore the previous `evaluate_rule` (git), after archiving
  rules incompatible with the old cap.
- An intervening unavailable or stale recording restarts the count rather than being bridged:
  "every reading" then stays literally true. Undo: let the walk skip such recordings within the
  gap.
- A change between qualified sources does not break a run. Undo: require S0's source.
- `max_age_seconds` keeps its name and becomes the longest time between recordings (≤ min(60,
  window)). Undo: restore the cap in `validate_rule`.
- Unreachable rules (gap < 5 s, or (N − 1) × 5 > window) are refused by the form, the install
  route and the chat but accepted by `validate_rule`, so saved ones keep loading and are flagged.
- The longest For at least choice is 4 min (5 min needs 61 readings, past older brokers' limit).
- Rules are vehicle-wide; transcripts stay per browser.
- One bad row disables only itself; its open episode is retired as if removed.
- Form defaults: 30 s (7 readings, window 60 s), gap 15 s, engine-running on except battery and
  tire metrics, no default limit. Undo: edit `form_defaults`.
- Replay ledger lifetime 7 days and 512 entries.
- The advisor refuses installs unless the broker reports `owner-threshold-v2` and serves the
  metric with the same unit.
- The agent never installs: its proposal fills the form (Phase 4); the automatic first turn goes.
- Only screenshot 13 changes; the set stays at 19 images.

## Open questions for the owner

None blocking. Follow-ups he may want: recorded ranges for rpm, speed, torque and the
transmission speeds, and a running-only "typical high" (broker work, measured against
`docs/broker-cpu.md`); a parked battery rule confirms only when N fresh recordings arrive in a
row, and parked wakes are brief, so the existing built-in parked battery warning remains the
parked safeguard.

## Review

- Round 1, gpt-6-astra on `aa25ae9`: hold. Each finding was reproduced on 2026-11-17 with the
  in-memory historian probes and is closed as follows.
  1. High, relaxed cap admitted expired readings after an unavailable snapshot (probe: warning
     on a 5.3 s old field-duty reading with the relaxed cap): closed by semantics steps 2 and 4
     (current reading from the newest snapshot, bounded by the metric's stale limit; an
     unavailable or stale recording ends the run); F1 tests on a real historian.
  2. High, engine-running rules never confirm (probe: 0/7 at 5.2 s spacing, 6/7 at 4.9 s):
     closed by step 3 (RPM from the same snapshot, nothing captured later); F2 tests with
     distinct capture and observation times.
  3. High, regime filtering bridged a normal reading (probe: warning across a 20 % reading at
     3,000 rpm): closed by step 4 (snapshot-ordered run, no regime filter, failures end the run);
     F3 tests; built-ins untouched, proven by the oracle and replay comparison.
  4. Medium, "11 metrics can never fire" overstated (probe: a 2-reading coolant rule confirmed
     with observations 4.8 s apart): the "what works today" section now says "rarely"; the hard
     refusal is limited to provable impossibility under the new semantics, everything else is
     advice; F4 tests cover variable lag, deduplicated observations and window boundaries.
  5. Medium, chat installer bypassed the store: both installers go through
     `custom_warning_store.install` in Phase 2, the chat keeping its exact-proposal check; mixed
     duplicate, rejected-row and broker-skew tests.
  6. Medium, help-panel reopening and exhaustion undefined: section 4 defines read-on-open,
     append-on-send, request-id replay, `busy`/`conversation_full` reasons and New chat as the
     evidence refresh; Phase 4 tests for each.
  7. Medium, a retried request could resurrect a removed rule: the replay ledger (7 days, 512
     entries, pending/installed states, "removed since" reply); a test of exactly that sequence.
  8. Medium, rollback could lose or race rules: quiesce the advisor, keep the complete current
     file, archive incompatible rows whole with the rollback tool, restart the broker and check it
     before writes resume, restore on roll-forward; reverse-order phase rollbacks.
  Lows folded in: Phase 1 readiness via `evaluator_revision`; rejected-row episode retirement
  documented and tested; old-bundle compatibility in Phase 4; keyboard-open viewport check; unit
  path corrected; 128 KiB enforced before every write.
- last reviewed: plan revision awaiting re-check (round 2 goes to the same reviewer).

## Status

Plan only. No code changed. Next step: round 2 plan re-check by gpt-6-astra; then a fresh builder
starts Phase 1 from this note in a worktree.
