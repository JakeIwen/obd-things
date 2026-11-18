# Custom early warnings

The owner's own threshold warnings ("warn when coolant stays above 230 °F for 30 seconds while the
engine runs"). They are owner monitoring references, never OEM limits or diagnoses. They are added
today through the Add Early Warning chat ([warning chat](warning-chat.md)); a plain form replaces
that path in a later phase (plan: `.agent/handoffs/early-warning-form.md`).

Where things live (`projects/vehicle_data/`):

- `custom_warnings.json` with `custom_warning_contract.py`: fields, limits, the 32-rule cap, file
  size, form defaults, evaluator revision; shared with the dashboard.
- `custom_warnings.py`: catalog, `validate_rule`, the evaluation semantics below (`RuleHolds`,
  `evaluate_rule`, `evaluate_rules`) and `advice`.
- `custom_warning_file.py`: the rules file (tolerant read, one durable write per operation, removed
  ids).
- `history/custom_rule_queries.py`: `recording_readings`, the one-recording readings query.
- `warning_engine/evaluator.py`: runs the custom rules after the built-in ones and reports their
  status in `custom_rules`.

## What a rule means (evaluator revision `owner-threshold-v2`)

A warning confirms when every one of the Pi's recordings of the metric for at least T seconds was
beyond the limit, with no recording missing it and never more than G seconds between recordings,
and those recordings carried at least N separate readings from the van (N is 2 unless changed).
With "only while the engine is running", every one of those recordings also showed the engine above
400 rpm. The telemetry service keeps this time while it runs; a restart of the service starts it
again. Nothing is claimed about moments between recordings.

The rule's eight fields keep their names, because they are the file contract:

| Field | Meaning | Limits |
| --- | --- | --- |
| `title` | Name on the warning card | 1–120 characters |
| `metric` | A catalog metric (numeric, `verified` or `observed_alfa_scale` source) | 17 metrics today |
| `operator` | `above`, `below` or `absolute_above` (size of the reading, ignoring its sign) | absolute needs a limit ≥ 0 |
| `threshold` | The limit, in the metric's unit | ±1,000,000 |
| `window_seconds` | **T, the hold time**: from the run's first recording to its latest | 5–600 s |
| `max_age_seconds` | **G, the longest time between two consecutive recordings in the run** | 1–60 s and ≤ T |
| `persistence_observations` | **N, the least number of separate readings in the run** | 2–60 |
| `engine_running` | Every recording must also show RPM above 400 | true / false |

Details, as the evaluator applies them:

- **Recording clock.** T and G are measured on the broker's monotonic clock, read beside each
  snapshot's capture time in the history loop. It is never stepped by NTP or `fake-hwclock`, so a
  wall-clock correction neither shortens nor lengthens a hold. NTP slewing can make a hold short by
  at most 0.05 % (0.3 s of 10 minutes). Offline callers with no broker (tests,
  `tools/telemetry_history_profile.py`) pass no clock; capture time stands in.
- **Current reading.** The newest stored snapshot at or before the evaluation time must carry a
  qualified reading of the metric (fresh at capture, finite, catalog unit, a catalog source of a
  qualified quality) no older than the metric's own stale limit. Otherwise the rule is
  `unavailable` ("No current reading"). A snapshot in which the broker reported the metric
  unavailable has no reading; an older value never stands in for it.
- **Engine running.** RPM comes from the same snapshot only, at most 5 s old, above 400. Nothing
  captured later is used.
- **Separate readings.** A reading is identified by its source and observation time; a value the
  broker repeats from its cache across recordings counts once and does not break the run. A change
  between qualified sources (battery voltage has three) continues the run.
- **What starts the time again:** a recording that misses the metric, is unqualified, not current,
  within the limit or without engine evidence; more than G seconds on the recording clock between
  stored recordings (this also covers captures the historian refused after a backward wall-clock
  step); a stored recording whose evaluation failed; a trip change; a broker restart (the hold is
  in memory); a rule removed, rejected or edited. Driving condition (regime) is not considered.
- **States.** Not beyond the limit: `normal`. Beyond but not yet confirmed: `normal` with a
  low-confidence `candidate` carrying `observed`/`required` (readings and N) and
  `held_seconds`/`hold_seconds`; Health shows "beyond its limit for 12 of 30 s, waiting to confirm".
  Confirmed: `warning`. An already open warning stays `warning` while the reading stays beyond,
  also across a broker restart or a restarted hold, so the event does not close and reopen.
- **Evaluations that are not a recording** (`/v1/health`) report the hold as of the newest
  recording and never change it.

How often a rule can confirm depends on the recording interval (`--history-interval`, 5 s in the
unit; recordings land about every 6–8 s on the van). `advice(rule, interval)` warns when G is under
twice the reported interval; nothing is refused for that.

## The rules file

`/var/lib/van-telemetry-advisor/early-warnings.json`, written only by the advisor:
`{"version": 1, "rules": [...], "removed": ["<64 hex>", ...]}`. Each row is `{id (64 hex), rule,
approved_at, chat_id, owner, ...}`; extra row keys and extra top-level keys are kept. At most 32
rows; a file over 128 KiB is refused before writing.

- **Read** (broker and advisor): an absent file is empty. A symlink, an oversized file, a version
  other than 1, a non-list, more than 32 rows or duplicate ids are file-level errors: no custom rule
  runs and `custom_rules.error` is set; built-in warnings carry on. Each row is otherwise validated
  alone: a row that fails validation (or whose assessment fails) stops alone, is listed in
  `custom_rules.rejected_ids`, and Health shows "N saved warnings could not be loaded". Its open
  event is retired exactly as a removed rule's is.
- **Write**: one write per operation: temp file, fsync, rename, then an fsync of the directory,
  so a reply sent after it describes a durable file. Rejected rows are kept verbatim after the valid
  ones. `removed` keeps the newest 256 removed ids; an id present as a row is dropped from it (the
  row wins).
- **Install** of an id in `removed` is refused ("added and has since been removed"); a replayed
  install of an existing id is a no-op. **Remove** drops the row and records its id, also when no
  row matched, so a retried install of it is refused.

Crash states: killed before the rename leaves the old file (a leftover `.early-warnings.json.*.tmp`
is never read); power lost after the rename but before the directory fsync leaves the old or the new
file, never a partial one, and no reply was sent; the chat's `applied` flag is display only and a
retry sets it to match the file.

`/v1/health` `custom_rules`: `enabled`, `count` (rules assessed), `rule_ids`, `rejected_ids`,
`error` (file-level only) and `evaluator_revision: "owner-threshold-v2"`.

## Deploy (Phase 1) and rollback

Saved rules change meaning under `owner-threshold-v2` (the gap is measured between recordings and
the window becomes a hold time), so this evaluator activates only on a vehicle with **no saved
custom rules**; it never reinterprets one. Never paste the rules file into an agent conversation;
the commands print counts only. `F=/var/lib/van-telemetry-advisor/early-warnings.json`.

Before install: the built-in invariance proof (`tests/advisory_lifecycle_oracle.py` byte-identical
against the deployed ref; `tools/warning_replay.py replay` on the vanpi export with identical
built-in transitions), the cost gate (below) and the chat rollback check (an old advisor opening a
v2 proposal chat reads the transcript and answers 409 on apply, with no rules file created).

1. `sudo systemctl stop van-telemetry-advisor.service` (the only writer).
2. Both must print 0: `if sudo test -e $F; then sudo jq '.rules | length' $F; else echo 0; fi` and
   `curl -s --unix-socket /run/van-telemetry/api.sock http://broker/v1/health | jq '.custom_rules | if .error then "error" else .count end'`.
   Otherwise `sudo systemctl start van-telemetry-advisor.service`, stop, and tell the owner the
   count; each rule needs his decision (he removes it on the dashboard and recreates it afterwards).
   No agent edits or converts the file.
3. `s restart_broker` (parked). The health command must show `enabled: true`, `error: null`,
   `evaluator_revision: "owner-threshold-v2"`, `count: 0`, `rejected_ids: []`.
4. `sudo systemctl start van-telemetry-advisor.service`.
5. Dashboard build (frontend only).
6. Journals of the broker, advisor and both web listeners clean.

During the rollout an old dashboard tab gets no installable proposal (the advisor stores proposals
under `rule_proposal*`, which the old bundle does not read) and an apply without `semantics` is
answered 409 "The dashboard was updated; reload the page." A proposal made before the advisor
restart is refused ("ask again").

Cost gate: the `telemetry-history-profile` compute task on a finalized backup, once with a context
holding 32 valid rules (ten-minute holds, every catalog metric, some beyond their limit but not
yet confirmed) and once with none. It passes when (a) with zero rules the ingest phase is unchanged from master
within noise; (b) with 32 worst-case rules it is no slower than master with the same rules; and
(c) the absolute time 32 rules add on the Pi is reported against the 5 s recording interval and
the 72.0 ms saved-snapshot ingest figure in [broker CPU](broker-cpu.md). The target for the custom
evaluation step alone is 10 ms (measured about 5 ms on the Pi); the rest of the added time is the
unchanged lifecycle storing 32 more assessments, which master pays too.
The task's numbers are a relative check: the task runs on a compute worker (usually the Mac) against an in-memory
copy of the database, so its absolute numbers are not the Pi's. After install, take one live
sample of the historian interval on the Pi, as the broker CPU baseline did (median 5.128 s after
the October change): record `last_stored_at` for five minutes,
`for i in $(seq 300); do curl -s --unix-socket /run/van-telemetry/api.sock http://broker/v1/status | jq -r '.history_recorder.last_stored_at'; sleep 1; done | uniq > /tmp/stored.txt`,
and take the median of consecutive differences. The loop waits the interval (5 s) after its work,
so the median minus 5 s is the per-recording work on the Pi. With no rules saved it must match the
pre-install figure within its spread; once the owner saves rules, the change is their cost on the
Pi. A busy Pi (a backup, a compute job) inflates it, so compare samples taken when `uptime` is calm.

Rollback (writes quiesced; every step is a rename, nothing is overwritten):

1. `sudo systemctl stop van-telemetry-advisor.service`.
2. Set the file aside:
   `if sudo test -e $F; then N=$(sudo jq '.rules | length' $F) && SAVED=$F.v2-saved-$(date +%Y%m%d%H%M%S)-${N}rules && sudo test ! -e $SAVED && sudo mv -n $F $SAVED && sudo test ! -e $F && echo "$SAVED"; fi`
3. Revert the code, `s restart_broker`; check `custom_rules` `count: 0`, `error: null`.
4. `sudo systemctl start van-telemetry-advisor.service` and rebuild the dashboard. Tell the owner N
   and that those rules are kept in the saved file, inactive. Saved chats stay as they are: the old
   advisor never installs a `rule_proposal`, and when the old advisor replays such a chat to its
   model the v2 proposal reaches it as null (context only; accepted).

Roll-forward (replaces deploy steps 1–3):

1. `sudo systemctl stop van-telemetry-advisor.service`.
2. `sudo sh -c 'ls -1 $0.v2-saved-*' $F`. With none, deploy as above. With more than one, ask the
   owner which. Set `SAVED` to that path and `N` to the count in its name;
   `sudo jq '.rules | length' $SAVED` must print N.
3. Precondition on the live file as deploy step 2. If it holds rules, stop for the owner. If it
   exists with zero rules:
   `EMPTY=$F.v1-empty-$(date +%Y%m%d%H%M%S); sudo test ! -e $EMPTY && sudo mv -n $F $EMPTY && sudo test ! -e $F`.
4. `sudo mv -n $SAVED $F && sudo test ! -e $SAVED`.
5. Deploy the code and `s restart_broker`; health must show `count` N, `rejected_ids: []`,
   `error: null`, `evaluator_revision: "owner-threshold-v2"`. Otherwise stop with the advisor
   stopped and report.
6. Deploy steps 4–6.
