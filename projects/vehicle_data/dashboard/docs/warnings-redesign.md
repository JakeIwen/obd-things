# Early-warning redesign — backend specification

Status: specification 2026-09-22, revised 2026-09-24 to describe the implemented behaviour
(iteration 2, uncommitted worktree, **not deployed**). Companion to `design.md` section 7. Derived from the
September 22 audit of `early_warning.py`, `custom_warnings.py`, `monitoring_coverage.py`,
`notifications.py`, `event_history.py` and the 640 advisory episodes recorded since August 22.
Owner decisions (binding, 2026-09-24): two tiers of vehicle warnings (absolute limits with
hysteresis; sustained same-regime deviation); ntfy only for actionable vehicle items; CAN-adapter,
bus and data-quality items **never notify** (dashboard System notes only); a single low reading
during engine cranking must never alert. Replay evidence: `tmp/vehicle_data/warning_replay/replay-iteration-2.md`
(gitignored machine output).

## 1. What the history says

| rule family | episodes | reached warning | pushes | mechanism of the noise |
|---|---|---|---|---|
| battery voltage relative low | 215 | 3 | 2 | alternator regulation collapses the MAD, so the 0.2 V minimum effect is the bar; PCM charge reduction and load steps look anomalous; 5 parked openings at 11.2–11.8 V were the only real cases and closed unconfirmed |
| CAN interface roles | 305 | 91 | 52 | startup absence and the broker's own armed channel counted as faults (fixed in source 09-21, one restart verified clean 09-22) |
| USB topology / transient disconnect | 49 | 49 | 47 | host hub re-enumerations, all self-resolved, 10-minute reminders |
| coolant relative high | 39 | 6 | 2 | bar at 202–214 °F while 13 of 57 trips reach 215–220 °F (thermostat fully open near 220) |
| tires relative low | 21 | 15 | 22 | first highway minutes compared to a heat-soaked highway baseline; four wheels push separately; 65 mph regime flips block recovery |
| transmission relative high | 8 | 3 | 2 | 5 °F minimum effect equals the top of the normal highway plateau; thermal regime taken from coolant |
| oil pressure (relative + absolute) | 0 | 0 | 0 | never fired |

Cross-cutting: a single anomalous 5-second sample opens a visible episode (`watch`); no
escalation hysteresis; per-rule notification dedupe; flat severity; open episodes cannot close
while parked; the 30-day baseline follows slow drifts (the rear-right tire lost about 1.4 psi a
month and nothing said so).

## 2. Tiers and delivery (implemented)

Persistence is always **N consecutive fresh readings inside N × 8 s**
(`OBSERVATION_SPACING_ALLOWANCE_SECONDS`). The historian stores a fresh observation about every 6 s
(p90 8 s), so N readings take roughly N × 6–8 s. The durations below use that rate.

| tier | source | opens an episode | dashboard | ntfy |
|---|---|---|---|---|
| 0 critical | OEM or owner limits with context gates | at the first confirmed hit | red strip and number | immediately, priority `urgent`, repeated every 5 min while active; never deferred or capped; a `default` recovery push after a delivered tier-0 push |
| 0 warning | same | at the first confirmed hit | amber strip and number | once per episode, priority `high`, 6 h cooldown per group; never deferred and never counted against the trip cap |
| 1 warning (sustained relative) | same-regime deviation held for minutes | only at confirmation; unconfirmed candidates stay in memory for two windows and never open an episode | amber strip and number | once per episode, priority `high`, 6 h cooldown per group, per-trip cap of 3 non-critical tiered pushes, quiet hours deferred only while parked (a warning raised during an open trip goes out at once) |
| 2 notice (drift) | daily job over `metric_rollups` | one per finding; closes on reversal (hysteresis) | Health list | once per episode, priority `default`, 7-day cooldown per group, quiet hours deferred |
| 3 system | interface, USB, plausibility, coverage | separate `system` category; a CAN-role event opens only at `warning` (second observation) | System notes and System view only | **never**: every `InfrastructureHealthEvaluator` and coverage assessment has `notification_eligible=False` (owner decision 2026-09-24; supersedes the earlier "hard fault > 5 min, once" design). The historian's tier-3 policy row is unreachable. |

Quiet hours are `[22:00, 07:30)` in `VAN_TELEMETRY_QUIET_ZONE` (default `US/Mountain`). A deferred
push becomes eligible at 07:30 local time. Push dedupe is per `group`, not per rule.

Grading fields on every assessment: `tier`, `group` (`oil`, `cooling`, `transmission`, `charging`,
`battery`, `tires`, `system`, `custom`), `action` (one imperative sentence) and `confidence`.
Iteration-2 additive fields are:

- `floor`: coolant slow-motion floor;
- `stale` and `motion`: pair-asymmetry axles;
- `pending`: first-observation CAN roles;
- `rate_of_rise.window_min`;
- `held_keys`: carried while a tire event is open;
- tier-2 `drift` keys: `method`, `mate`, `relative_*`, `own_*`, `held_open`, resting-voltage `level_v` / `slope_v_per_day`.

## 3. Tier-0 rules (as implemented in `early_warning.py`)

**Running** means fresh RPM ≥ 400 continuously for at least 10 s. Gaps between fresh RPM readings
of up to 20 s (`RUNNING_MAX_GAP_SECONDS`) do not break it; a longer gap restarts the 10 s grace.

**Hysteresis.** A warning clears only once the value is past the limit by the clear margin.
`tire_pressure_low_absolute` readings in the `warming`/`warm` phase clear only at
`(limit + margin) × 1.08` (`warm_clear_fraction`, 56.2 / 75.6 psi); they still escalate against
the cold limit. The historian never enqueues a tier-3 or System-category item, whatever its
`notification_eligible`. An
open critical stays `critical` through inconclusive ticks (`unavailable`, `not_applicable`,
`recovering`, `insufficient_history`, `rejected`).

| key | condition | persistence (≈ duration) | severity | clear margin |
|---|---|---|---|---|
| `engine_oil_pressure_absolute_critical` | running, < 12 psi | 2 obs / 10 s | critical | 2 psi |
| `engine_oil_pressure_below_band` | running, coolant ≥ 160 °F, and < 15 psi at 550–850 rpm, < 22 at 1,000–3,000, or < 55 above 3,500 (no judgement between bands) | 3 obs / 24 s (≈ 12–16 s) | warning | 2 psi |
| `engine_coolant_temperature_hot` | running, ≥ 230 °F; ≥ 240 °F critical | 6 obs / 48 s (≈ 30–45 s) | warning / critical | 5 °F |
| `transmission_oil_temperature_hot` | running, ≥ 230 °F | 12 obs / 96 s (≈ 66–90 s) | warning | 5 °F |
| `battery_voltage_charging_failure` | running with RPM ≥ 1,000 on every sample, < 12.0 V, generator duty ≥ 90 % on every sample | 12 obs / 96 s (≈ 66–90 s) | warning | 0.5 V |
| `battery_voltage_low_parked` | no running RPM, ≥ 30 s after the last trip activity, engine off or unknown, < 11.8 V; < 11.5 V critical | 3 obs / 60 s | warning / critical | 0.2 V |
| `tire_pressure_low_absolute` | FL/FR < 50 psi, RL/RR < 68 psi; < 45 / < 60 critical; checked on every reading because a warm tire only reads higher | 3 obs / 60 s (≈ 12–16 s) | warning / critical | 2 psi |
| `tire_pressure_pair_asymmetry` | same axle (L − R) minus that axle's 30-day median offset > 3 psi, on the latest pair and on every counted reading; only moving, freshly sent readings count (see below) | 60 obs / 480 s (≈ 6–8 min); clears after 8 fresh obs, `recovery_seconds` 120 | warning | 1 psi |

**Cranking and key-on dips.** A single low reading while cranking never alerts.

- The parked rule needs 3 settled readings, taken at least 30 s after the engine stopped.
- The charging rule needs the 10 s running grace, RPM ≥ 1,000 and 12 readings.
- The five archived 11.2–11.8 V parked watches were single crank or key-on samples. They stay at 0.
- Test: `test_single_crank_or_key_on_dip_never_warns`.

**Cached tire readings (D9).** The RF hub repeats a wheel's last pre-trip value until that sensor
transmits again. A wheel counts as cached when either condition holds:

- every reading in the trip is one value (within 0.05 psi) and equals its last reading before the
  trip;
- the pre-trip reading is beyond raw retention, and one value has held for ≥ 300 s and ≥ 10
  readings.

The tier-1 tire rule and the absolute tire limit are not gated. A cached value is a real, earlier
reading. Against a cold-tire floor it can only delay a warning.

**Moving, freshly sent readings (pair rule, 2026-09-25).** Episode 642 (trip 59) was a false
`Rear tires uneven`. The van idled for about 23 min. RL reported its cooling tire (79.6 → 76.0 psi)
while the hub kept repeating RR's 79.6, so the difference was 4.4 psi. 60 stationary readings
counted, and the warning opened as the van pulled away and RR re-sent 74.8. At that moment the
latest pair was 0.4 psi apart: the run's newest reading had paired RL with RR's previous value.
The warning then took 60 clear readings plus 600 s to close (1,028 s). Across trips 43–62 the
gaps between value changes peak at 64 s and its multiples, which puts the rolling transmission
period near 64 s. Changes in `early_warning.py` (`PAIR_*` constants):

- **Stationary gate.** A reading is gate-unmet (`fail`: it neither escalates nor clears, and it
  does not contradict recovery) unless a fresh `vehicle.speed` of at least 5 mph is at most 20 s
  older than it. The speed-to-tire lag is p99 10 s and at most 17 s. When the latest reading is
  stationary, the axle reports `not_applicable`, and an open event stays open.
- **Fresh-transmission gate.** A moving segment ends at any slower or stale speed. Within the
  current segment, a wheel counts only after its value changes, or after 150 s of unbroken
  movement (at least two rolling transmissions). A strict "value changed" test would blind the
  rule: a warm tire holds one value for up to 20 min on the highway, and in 4 moving segments of
  2–7 min (trips 55, 59, 60) RL never changed. When either wheel on the axle has not re-sent,
  the axle reports `not_applicable`.
- **Latest pair.** When the latest two readings are inside 3 psi, the escalation count is zero.
- **Clearing.** A held axle clears after 8 fresh moving readings, about one transmission period at
  about 8 s per reading. `recovery_seconds` is 120 (two periods). Re-opening needs the full
  60-reading run, so the shorter clear cannot flicker.
- **Evidence.** `axle_assessments.<axle>.motion` records `moving`, `speed_mph`,
  `segment_started_at` and the per-wheel `fresh` flags.

Regression tests are in `PairAsymmetryIncidentTests`: the real trip-59 readings, ungated
reproduction, latest-pair opening, stop-hold and fast clear, and a slow leak on a moving van.

Measured at a 6/6/8 s cadence, the time from the first faulty reading to the warning is:

| fault | time to warning |
|---|---|
| transmission 240 °F | 86 s |
| 11.5 V at 1,500 rpm with duty 100 % | 86 s |
| coolant 235 °F | 46 s |
| oil 13 psi at warm idle | 26 s |
| mid-drive RR at 55 psi | 12 s (critical) |

The test `test_tier0_limits_confirm_at_the_live_observation_cadence` enforces these times.

## 4. Tier-1 relative rules (as implemented)

**Threshold.** `threshold = max(minimum_effect, 4.5 × max(robust_sigma, sigma_floor))`. The
baseline is the last 30 days, excludes the current trip, and needs ≥ 30 buckets from ≥ 3 trips.

- **Escalation:** N consecutive readings with `effect ≥ threshold` inside N × 8 s.
- **De-escalation:** only at `effect ≤ 0.7 × threshold`.
- **Recovery:** in any regime, after 600 s of comparable normal readings. An episode also closes
  after 24 h with no open trip, with resolution `not_re_observed`.

| rule | floor / minimum effect | persistence (≈ duration) | regime and extras |
|---|---|---|---|
| `engine_oil_pressure_relative_low` | 1 psi / 5 psi | 12 obs / 96 s (≈ 1–1.5 min) | engine, motion, rpm, thermal |
| `engine_coolant_temperature_relative_high` | 3 °F / 12 °F | 24 obs / 192 s (≈ 2.4–3 min) | see below |
| `transmission_oil_temperature_relative_high` | 3 °F / 12 °F | 24 obs / 192 s | thermal band from the transmission value itself (cold < 120, warm 120–175, hot ≥ 175 °F) |
| `battery_voltage_relative_low` | 0.15 V / 0.8 V | 24 obs / 192 s | baseline and reading need generator duty ≥ 85 % (corroborator required) |
| `tire_pressure_relative_low` (one combined rule) | 0.8 psi / 3 psi | 60 obs / 480 s (≈ 6–8 min) | see below |

**Coolant.** The regime is engine, motion and rpm. The rule is gated until the engine has been
running for 5 minutes.

- **Slow-motion floor:** while stationary or urban, only readings ≥ **222 °F** count toward
  escalation. The fan cycles 203–221 °F at hot idle, and no rollup bucket in 57 trips reaches
  222 °F.
- **Rate-of-rise trigger:** every reading in 180 s is ≥ 215 °F and the slope is ≥ 0.1 °F/s, while
  stationary or urban. This implies a newest reading of at least 232 °F, so the 230 °F tier-0
  limit usually fires first. The trigger is kept for its fan-check action.

**Tires.** The rule reports the worst wheel and lists every wheel past threshold. The regime is
the phase, never the speed:

- cold: the first 3 minutes moving after ≥ 4 h parked;
- warming: 3–20 minutes moving;
- warm: more than 20 minutes moving.

While the event is open, `held_keys` names the opening wheel. A held wheel that has cleared but
has fewer than N readings reports `recovering`.

## 5. Tier-2 drift job (`drift_warnings.py`, from `metric_rollups`, at most daily from the insights maintenance pass)

| finding | method | notice | clears |
|---|---|---|---|
| tire slow leak (per wheel) | cold-start medians (window: trip start to 180 s after first moving, after ≥ 4 h parked); drops buckets equal to the wheel's last pre-trip value ± 0.05 psi (cached); compares with the axle mate (own − mate − usual offset) | relative slope ≤ −0.5 psi/week over 14 days (≥ 4 points) or relative delta ≤ −3 psi (median of last 3 minus 30-day median), **and** the wheel's own series shows at least half of that (≤ −0.25 psi/week or ≤ −1.5 psi) | relative slope > −0.25 and relative delta > −1.5 psi |
| coolant idle creep | per-trip median of `engine_running:stationary:rpm_idle:warm` buckets after minute 10; last 2 trips vs the prior 10 (≥ 5) | +5 °F | < +2.5 °F |
| oil pressure decline | per-trip median of `rpm_idle` / `rpm_low` warm buckets; last 2 vs prior 10 | −3 psi | > −1.5 psi |
| charge acceptance | share of running 1-minute buckets < 13.0 V per trip (≥ 10 buckets) | > 40 % for 3 consecutive trips | < 25 % |
| resting voltage (`battery_voltage_resting_low`, group `battery`) | per parked stop, median of settled engine-off buckets (≥ 60 s after the last activity and ending ≥ 120 s before the next start, so key-on and crank dips are excluded); majority source identity in the window | 7-day median of stops < 12.2 V (≥ 3 stops) or OLS slope < −0.1 V/day (≥ 5 stops) | ≥ 12.3 V and slope > −0.05 V/day |

Insufficient data never resolves an open notice. When the previous state was `warning`, the
notice stays open with `drift.held_open = true`. It then carries `baseline` / `deviation` with
`None` values, and `current.observed_at` / `source` are `None`. A held notice is never
notification-eligible (`notification_eligible = false`): if its event was closed meanwhile (a
rule-revision change), the held assessment opens a new event without a push.

## 6. Lifecycle and delivery details

- Tiered episodes open only at a confirmed warning. Legacy (untiered) rules keep the older
  watch-first lifecycle.
- Recovery happens in any regime after `recovery_seconds` (600 s) of comparable normal readings,
  meaning the same source, quality, provenance, unit and rule revision. Parked closure after 24 h
  records `not_re_observed`.
- Group dedupe, the per-trip cap (3 non-critical tiered pushes, excluding tier 0 and critical) and
  quiet hours live in `historian._notification_due_locked` and the tiered enqueue path.
- System items keep the September 22 probe and armed-owner logic. A missing, failing or
  unknown-health CAN role reports `normal` with `pending` on its first observation and `warning`
  on the second. Controller errors and ambiguous identities warn immediately. USB topology and
  transient items and coverage gaps have info or System-notes severity only.

## 7. Messages

The format is `<Metric> <value> <unit>, <comparison> for <duration>. <Action>.`: one line on the
card, two in ntfy, and no evaluator vocabulary. Examples and the per-state table are in
`warnings.md`. The `action` field carries the imperative sentence, so the dashboard and ntfy never
diverge.

## 8. Validation (offline) and deployment

1. `tools/warning_replay.py` replays the last 7 days of raw trips tick by tick against a scratch
   copy, and trips 1–42 as a coarse upper bound from rollups. `scripts/drift_daily.py` replays tier
   2 daily.
2. Acceptance as met by iteration 2 (see `replay-iteration-2.md`):
   - tier 0: 0 fires on trips 43–57, and crank dips stay 0;
   - tier 1: ≤ 1 warning per trip and family, with 0 on the hot-idle trips 50–54;
   - pair asymmetry: 0 warnings;
   - synthetic faults: within the bounds in §3;
   - tier 3: 0 pushes;
   - tire notices: ≤ 2 days per wheel over 09-01..09-24;
   - resting voltage: `normal`.
   The original "five parked cases must warn" and "RR slow leak appears as a notice" criteria were
   retired: the first is crank and key-on dips, and the second is a common-mode pressure drop
   across all four wheels.
3. Unit tests cover every rule and the grouping, cooldown and quiet-hours path. The full suite runs
   via `pi_compute run repo-tests`.
4. Deployment is an owner action (a broker restart while asleep), recorded in the vehicle_data
   README with the replay numbers. Nothing is deployed yet.
