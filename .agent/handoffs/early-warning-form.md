# Early-warning form (Add Early Warning redesign)

tier: T3 (a new advisor write route that installs monitoring rules without a model proposal, a
change to the web relay allowlist, and a broker-side change to how custom rules load and confirm)
last reviewed: plan not yet reviewed
plan written against: branch `worktree-agent-adfb8d9372b35cdbc`, commit `ce71087` (master
`0393eee` plus four early-warning commits of 2026-11-17)

## Objective

Make "Add early warning" on the dashboard's Health view a plain form that saves a custom
threshold rule with no internet and no model turn, and move the model to optional help inside
that form. "Ask Codex" on a warning card (explaining a recorded event) is unchanged.

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
  built-in warnings, and the access-code gate (a separate worker is designing a pairing PIN;
  see "Where this meets the access gate").

## Current facts the design rests on

Read these before building; each was checked against `ce71087`.

- A rule is exactly eight fields (`custom_warnings.FIELDS`): `title, metric, operator,
  threshold, persistence_observations, window_seconds, max_age_seconds, engine_running`.
  `validate_rule` limits: title 1–120 chars; metric in `catalog()`; operator `above`, `below`,
  `absolute_above`; threshold ±1,000,000; persistence integer 2–60; window 5–600 s; max age
  1..min(60, the metric's `stale_after_seconds`) and ≤ window; `engine_running` bool;
  absolute threshold ≥ 0.
- `catalog()` = numeric metrics with a `verified` or `observed_alfa_scale` source: 17 metrics
  today (battery voltage, coolant, crankshaft power and torque, target torque, oil pressure, rpm,
  VVT oil temperature, alternator field duty, four tire pressures, transmission oil temperature,
  output and turbine speed, vehicle speed). Each public dict carries `unit`, `minimum`,
  `maximum`, `stale_after_seconds` (4–30 s).
- Rules live in `/var/lib/van-telemetry-advisor/early-warnings.json` (`{"version":1,"rules":[…]}`,
  each row `{id (64 hex), rule, approved_at, chat_id, owner}`), written by the advisor with
  `engine_off_voltage._atomic_json` (temp file, fsync, rename), at most 32 rules, 128 KB.
- The broker (`warning_engine/evaluator.py`, the custom block near line 498) reloads the file on
  every evaluation with `load_rules`, which validates every row. **One row that fails
  validation makes the whole file fail**: every custom rule stops evaluating and
  `custom_rules.error` is set (built-ins carry on). The broker validates with its own code and
  `METRICS`, so a rule the advisor accepts but an older broker does not (version skew after a
  catalog change, as `43d5d37` warns for field duty) silences every custom rule.
- **Persistence finding (from code reading; confirm with the Phase 1 test before building on
  it).** The broker records a history snapshot every `--history-interval 5` seconds
  (`systemd/van-telemetry.service`); the loop does its work and then waits 5 s, so consecutive
  recorded observations of a broadcast metric are a little more than 5 s apart. While the van is
  driving, tire pressures are polled round-robin about every 4 s and VVT oil temperature every 5 s
  (`active_drive.py`), so the 5 s snapshot is the effective reading interval for every catalog
  metric. `evaluate_rule` counts a run of matching readings and breaks the run when the gap
  between two readings exceeds `max_age_seconds`. Because `max_age_seconds` is capped at the
  metric's stale limit (4 s for power, torque and field duty; 5 s for coolant, oil pressure, rpm,
  speed, transmission temperatures and speeds), any rule on those 11 metrics with persistence ≥ 2
  can never confirm: it stays "waiting to confirm" forever. Battery voltage, tire pressures
  (30 s) and VVT oil temperature (15 s) are not affected. The test fixture
  (`tests/test_custom_warnings.py`, readings 3 s apart) never exercised the real spacing. The
  stale cap is redundant for its apparent purpose: `_qualified` already requires each sample's
  recorded `freshness == "fresh"`, which applies the metric's stale limit at capture time.
- Recorded ranges already in the dashboard store: `summary.history.metric_trends[metric]` for
  the 11 metrics in `insights.DEFAULT_HISTORY_METRICS` (battery, oil pressure, coolant, VVT oil,
  crankshaft power, transmission oil, field duty, four tires), each with `days_7` and `days_30`
  (`minimum`, `mean`, `maximum`, `sample_count`; parked and running together, from minute
  rollups) and `prior_trips` (`median_of_trip_means`, `mad_of_trip_means`, `trip_count`). Missing:
  any range for rpm, speed, torque, target torque, output and turbine speed; percentiles
  ("typical high"); a running-only split. The advisor can read the same product with a cached
  `GET /v1/history` (it already does in `event_context`).
- The broker serves its own metric catalog: `GET /v1/metrics` (`broker.list_metrics`, the public
  dict of every definition) and `catalog` inside `/v1/snapshot`.
- Relay: `web.py::_assistant_request` allows a fixed list of `/v1/assistant/` paths (plus
  `dtc_research_contract.relay_allowed`), requires an exact trusted origin, same-origin fetch
  metadata, JSON POSTs, and well-formed `X-Van-Assistant-Key` / `X-Van-Chat-Client` headers.
  The advisor (`ChatHandler.handle_request`) checks the access code in `manager.authorize` before
  any route and derives `owner = sha256(client id)`.
- The Add early warning button (`views/Health.jsx`) shows only when the web listener advertises
  `warning_chat_enabled`, and opens `WarningChat.jsx` in `add-warning` mode, which immediately
  starts a model turn ("Help me set up a new early warning.").

## History findings (owner rule: redesigns start from the commit history)

| Piece | Added in | Stated reason | Still holds? | Plan |
| --- | --- | --- | --- | --- |
| Setup chat, proposal ID approval, `apply-warning`/`remove-warning`, rules file, 32 cap | `84d95f3` (2026-09-20), ported to Preact in `2fe75a4` (2026-09-26) | Docs: only the latest completed, validated proposal can be installed, with its exact ID and explicit approval, so the model can never install a rule or a stale proposal be approved | Yes for chat-made rules; the form keeps the property in a stronger form: the owner composes the exact fields, reviews them, and the model can install nothing | Form route replaces install; chat install path deleted in Phase 3 |
| Rules listed and removable only by the creating browser (`row.get("owner") == owner`) | `84d95f3` | None for rules. Docs give a privacy reason for transcripts ("keyed to the browser's random secret") | No: the broker evaluates every rule for the vehicle and every browser's Health view shows its warnings, yet the owner's Mac and tablet (and the LAN and Tailscale URLs of one device, which pair separately) each see a different list, so a rule made on the Mac can fire on the tablet and cannot be removed there | Rules become vehicle-wide; transcripts stay per browser |
| `max_age_seconds` ≤ the metric's stale limit | `84d95f3` | None stated; apparently "never count stale data" | That purpose is already met by the `freshness == "fresh"` check; the cap makes persistence unreachable on 11 of 17 metrics (see finding) | Relax to ≤ min(60, window) in Phase 1 |
| Catalog limited to verified / `observed_alfa_scale` numeric metrics | `84d95f3` | Owner rules only on trusted decodes | Yes | Keep |
| Field duty allowed in the catalog | `43d5d37` (today) | A rule the owner defines himself is an owner reference, not alternator evidence | Yes | Keep; the form shows the field-duty note |
| "What you can monitor" disclosure, `catalog_summary`, `RULE_READINGS` | `78d510b`, `ce71087` (today) | The owner could not see what he may monitor | Superseded: the form's metric picker is that list | Delete in Phase 3 |
| Setup waiting wording | `7f2c5ce` (today) | The setup chat has no event | Yes while the help chat exists | Keep for the help panel |
| Hold an open custom warning through a short run (`_open_warning`) | `2fe75a4` | A confirmed warning should not close and reopen on a regime change or gap | Yes | Unchanged |
| Access code helpers shared by both dialogs (`assistantAccess.js`, `AssistantPairing.jsx`) | `5478077` (2026-11-16) | One copy for warning chat and DTC research | Yes | The form reuses them |

## Design

### 1. The form

A new dialog, **Custom early warnings**, opened by Health's "Add early warning" button. Layout
for the portrait 800×1280 tablet, top to bottom, with the Save area pinned at the dialog
bottom so the on-screen keyboard never hides it and the dialog scrolls little:

1. Pairing (only when the browser has no access code): the existing `AssistantPairing`.
2. "Your custom warnings (N of 32)": a collapsed disclosure; see section 5.
3. The form fields (labels in plain words; the rule field each one sets in brackets):
   - **What to watch** [`metric`]: a native `<select>` (the Android picker scrolls better than a
     custom list) of the advisor's catalog, labelled with `metricLabel` and unit, sorted by
     label. Below it, one muted line: the live value ("Now 198 °F", from the store, only when
     fresh) and the recorded range (section 2). For field duty, one more line: "This is the
     engine computer's commanded alternator field duty, not current or temperature."
   - **Warn when it goes** [`operator`]: two buttons, Above / Below. Default Above.
   - **Limit** [`threshold`]: `<input type="number" inputmode="decimal">` with the unit as a
     suffix. No default: the owner chooses the number; the range line is beside it.
   - **For at least** [`persistence_observations`, `window_seconds`]: a `<select>` of 15 s,
     30 s, 1 min, 2 min, 5 min. Default 30 s: seven readings in a row, long enough to ignore a
     single odd reading and short enough to catch a developing overheat within a minute.
   - **Only while the engine is running** [`engine_running`]: a checkbox. Default on, except
     for metrics whose name starts with a prefix in `form_defaults.engine_running_off_prefixes`
     (`battery.`, `tire.`): battery voltage matters parked, and tire readings only arrive during
     drives anyway. The hint says it means "fresh RPM above 400 at every reading".
   - **Name on the warning card** [`title`]: prefilled "Coolant temperature above 230 °F" and
     kept in step with the fields until the owner edits it.
   - **Exact settings** (collapsed disclosure, for proposals and the curious): Readings in a row
     [`persistence_observations`, 2–60], All within (seconds) [`window_seconds`, 5–600],
     Longest gap between readings (seconds) [`max_age_seconds`, 1–60, ≤ window], and "Compare the
     size of the reading, ignoring its sign" (turns Above into `absolute_above`; disabled with
     Below). Editing any of these switches For at least to "Custom". One hint: "Readings arrive
     about every 5 seconds while the van is awake."
4. **Preview** (always visible, updates as fields change): "Warn when Coolant temperature stays
   above 230 °F for about 30 seconds (7 readings in a row), only while the engine is running."
   Variants: "stays below"; absolute: "stays further than 40 lb-ft from zero, in either
   direction"; custom settings: "for 9 readings in a row within 90 seconds"; engine off: no
   clause. Under it: "Owner monitoring reference, not an OEM limit. Uses recorded telemetry
   only."
5. **Review** (replaces proposal approval): "Review" validates and turns the form read-only,
   showing the preview, the exact values ("7 readings within 60 s, gap at most 15 s"), and two
   buttons, **Add warning** and **Edit**. Only Add warning sends the install request, with
   `confirmed: true`. After success the list opens with the new row, the form resets, and the
   Health card is told to refresh (`onWarningsChanged`, as today).
6. **Help me choose** (Phase 3; section 4), a secondary button under the form.

Derived values for a For at least choice of H seconds, with the reading interval R = 5 s
(`reading_interval_seconds` in the contract) and the default gap G = 15 s:

- readings N = clamp(round(H / R) + 1, 2, 60): 15 s → 4, 30 s → 7, 1 min → 13, 2 min → 25,
  5 min → 60.
- window W = clamp(2 × H, 60, 600): 60, 60, 120, 240, 600. Twice the hold leaves room for the
  real spacing (a little over 5 s) and one missed snapshot; the 60 s floor keeps W ≥ G.
- gap G = 15 s (`form_defaults.gap_seconds`): tolerates one missed snapshot (two intervals,
  about 10.4 s) plus jitter. `max_age_seconds` is therefore never shown in the main form.

Validation (client messages; the server is authoritative and its `detail` is shown when it
refuses):

- No metric: "Choose what to watch."
- Limit empty or not a number: "Enter a number in °F." (unit of the chosen metric)
- Limit outside ±1,000,000: "Use a number between −1,000,000 and 1,000,000."
- Ignoring the sign with a negative limit: "Use zero or more when ignoring the sign."
- Name empty or over 120 characters: "Give the warning a name of up to 120 characters."
- Exact settings out of range: "Readings in a row must be a whole number from 2 to 60.",
  "All within must be 5 to 600 seconds.", "Longest gap must be 1 to 60 seconds and no more than
  All within."
- Unreachable (blocks Review; the install route refuses the same): "Longest gap must be more
  than 5 seconds: readings arrive about every 5 seconds." when G ≤ R, and "N readings take about
  X seconds, longer than All within (W s), so this could never trigger." when (N − 1) × R > W.
- Soft warning only (Review stays enabled): a limit outside the metric's catalog
  `minimum`..`maximum` ("Coolant temperature readings range from −40 to 419 °F, so this could
  never trigger."), and an Above limit below the 30-day low or a Below limit above the 30-day
  high ("This would trigger on almost every reading.").

### 2. Recorded range in the form

Shown under What to watch from data the dashboard already holds, with no new route:

- From `summary.history.metric_trends[metric]`: "Last 30 days: low 61, average 189, high 221 °F
  (parked and running together)" (fall back to 7 days when 30 is absent) and, when present,
  "Usual drive average 196 ± 3 °F (12 drives)". Format with the dashboard's `fmtValue` and
  `fmtUnit`, as `history.helpers.js::trendModel` does.
- Metrics with no trend: "No recorded range on the dashboard for this metric."
- The live value from the snapshot store when fresh.

What is missing and deliberately not added now: ranges for rpm, speed, torque, target torque,
output and turbine speed (adding them to `DEFAULT_HISTORY_METRICS` adds a 24 h series query per
metric to every broker supplemental refresh; see `docs/broker-cpu.md`), percentiles and a
running-only split (new SQL over rollups). The min/average/max plus the drive average answer
most "what number?" questions; the help panel covers the rest. Listed as a follow-up, not a
blocker.

The dialog never reads the store directly: `CustomWarnings.jsx` passes `readTrend(metric)` and
`readLatest(metric)` functions to the controller, which keeps the helpers pure.

### 3. Backend

Routes, served by the advisor and relayed by both web listeners (same headers, same gate):

- `GET /v1/assistant/warnings` returns
  `{catalog: [{metric, unit, minimum, maximum}], warnings: [{id, rule, loaded, rejected,
  source, created_at, created_here}], count, max_rules, warning_config_error,
  warning_evaluator_enabled}`. `catalog` is `catalog()` reduced to those fields. `loaded` and
  `rejected` come from the broker's cached `/v1/health` `custom_rules` (`rule_ids`, and
  `rejected_ids` from Phase 1). `created_here` is `row["owner"] == owner`, provenance only.
- `POST /v1/assistant/warnings` body exactly `{request_id, rule, confirmed}`:
  1. `confirmed is True`, `request_id` matches `ID_RE` (32 hex), else 400.
  2. `validate_rule(rule)` (the same function the chat path and the broker use), then
     `unreachable(rule)` (new, section 6), else 400 with the message.
  3. Broker check: `GET /v1/metrics` on the broker socket; the rule's metric must be in
     `catalog_from(served definitions)` with the same unit. Broker unreachable → 503 "The
     telemetry service is unavailable, so the warning can't be checked; try again." Metric
     missing → 409 "The telemetry service doesn't offer this metric yet; it needs a restart
     after the update." This keeps the advisor from saving a rule the running broker would
     reject.
  4. Under `manager.lock`: read the file with the tolerant loader (Phase 1), compute
     `id = sha256("form:" + owner + ":" + request_id)` hex. If a row with that id exists and its
     rule equals the validated rule → reply 200 with the list and `replayed: true` (double tap,
     retry after a timeout). Same id, different rule → 409 "This request was already used." A
     row with the same trigger (all fields but `title`) → 409 "An identical warning already
     exists: <title>." Count ≥ 32 → 409 "Custom warning limit reached (32); remove one first."
  5. Append `{id, rule, approved_at: now(), owner, source: "form", request_id}`, write with
     `_atomic_json`, reply 200 with the list plus `installed_id`.
- `POST /v1/assistant/warnings/<64 hex>/remove` `{confirmed: true}` (exists): any paired browser
  may remove any rule (was owner-scoped). Idempotent. It also removes rows the broker or the
  advisor reject, by id, and preserves every other row verbatim.

Rows keep the key `owner`, so an advisor rolled back to the per-browser filter still lists form
rows on the browser that made them. Older code ignores the new keys (`source`, `request_id`).
Every write preserves rows the current validator rejects (never silently drop an owner rule);
only removal by id deletes them.

Rule ownership decision: **rules are vehicle-wide.** Any browser paired with the access code
sees every rule and can remove any; the list marks "Added on this browser" from `created_here`.
Chat transcripts stay per browser. Undo: restore the owner filter in the store's list and remove
functions.

`/v1/assistant/status` keeps `warnings` and `warning_config_error` (now vehicle-wide too) until
Phase 3, for dashboards still running the old bundle; Phase 3 drops them, which also removes the
broker `/v1/health` call made on every status request.

Where this meets the access gate (keep the PIN work separable): the new routes add one
allowlist predicate (`custom_warning_contract.relay_allowed`) called from `_assistant_request`
beside `dtc_research_relay_allowed`, and two dispatch branches in `ChatHandler.handle_request`
after `manager.authorize`. Neither changes the origin, header or token checks, `authorize`,
`assistantAccess.js` or `AssistantPairing.jsx`; whatever the PIN design does to those applies to
these routes unchanged. Because the list no longer filters by the client-id hash, a pairing
change that re-keys browsers cannot orphan saved rules.

### 4. The agent's new place (Phase 3)

- **Help me choose** opens a compact panel inside the form dialog: two suggestion buttons
  ("Suggest a limit from my history", "Explain this metric"), a two-line text box ("Ask about
  this metric or describe what you want to catch"), the transcript, Stop and Retry. Model and
  effort pickers sit in a collapsed "Model" disclosure (defaults as today). Nothing is sent until
  the owner taps a suggestion or Send: **the automatic first model turn is removed.**
- Context the advisor builds (cached broker GETs only, as `event_context` does): `catalog()`,
  `focus_metric` (the form's chosen metric, required, must be in the catalog), its
  `recorded_range` from `/v1/history` `metric_trends[metric]` without `sparkline` and `series`,
  its `latest` reading from `/v1/snapshot`, `reading_interval_seconds` and `form_defaults` from
  the contract. The browser sends only `metric` and the question; it never sends a draft rule or
  prose as evidence.
- `POST /v1/assistant/chats` for the setup event accepts `{event, request_id, settings,
  metric, message, new_chat?}`; `message` becomes the first turn. Setup chats are matched by
  owner, event and focus metric, so changing the metric opens a new chat.
- `SETUP_INSTRUCTIONS` changes: the proposal fills the owner's form and installs nothing; base a
  suggested limit on `recorded_range` and say that low and high include parked readings;
  readings arrive about every 5 s; use the form defaults unless asked otherwise (gap 15 s); say
  what field duty is when it is the metric; explain in plain words, not field names.
- The reply format stays `{explanation, proposal}`; the proposal is still checked with
  `validate_rule` and `unreachable`. The panel shows the proposal's preview sentence and **Use
  these settings**, which fills all eight form fields (Exact settings opens when they differ
  from a For at least choice). The owner then saves through Review and Add warning.
- Removed: `apply-warning` and `remove-warning` chat routes and their relay pattern, the
  Proposal disclosure and Approve button, the chat's Saved Custom Warnings, the "What you can
  monitor" disclosure, `add-warning` mode in `WarningChat.jsx` (explain mode only remains).

### 5. Saved custom warnings

They live in the form dialog: the "Your custom warnings (N of 32)" disclosure at the top,
collapsed by default, open after a save. Each row: the preview sentence, a status, "Added on
this browser" when `created_here`, **Copy to form** (fills the form from the rule; saving
creates a new rule and the owner removes the old one; rules are never edited in place, as
today), and **Remove** (`window.confirm`, as the other dialogs do). Statuses: "Watching"
(`loaded`); "Saved; starts on the next check" (neither loaded nor rejected); "The telemetry
service could not load this rule" (`rejected`); plus a hint "May never trigger: <reason>" when
`unreachable(rule)` reports a problem, which marks rules saved before Phase 1 whose gap is at or
below 5 s. A file-level error shows as today.

The Health card keeps its error line and, from Phase 1, adds "N saved warnings could not be
loaded" when `custom_rules.rejected_ids` is non-empty.

### 6. Files, shared contract, map

New shared contract `projects/vehicle_data/custom_warnings.json`, read by Python
(`custom_warning_contract.py`) and imported by the dashboard
(`import contract from "../../custom_warnings.json" with { type: "json" }`, as
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
  "limits": {"title_chars": [1, 120], "threshold": [-1000000, 1000000],
             "persistence_observations": [2, 60], "window_seconds": [5, 600],
             "max_age_seconds": [1, 60]},
  "reading_interval_seconds": 5,
  "engine_running_rpm_above": 400,
  "form_defaults": {"hold_seconds_choices": [15, 30, 60, 120, 300], "hold_seconds": 30,
                    "gap_seconds": 15, "window_floor_seconds": 60,
                    "engine_running_off_prefixes": ["battery.", "tire."]},
  "requests": {"install": ["request_id", "rule", "confirmed"], "remove": ["confirmed"]}
}
```

Phase 1 (broker and shared code):

- add `custom_warnings.json`, `custom_warning_contract.py` (loads the JSON; exports the limits,
  `FIELDS`, `MAX_RULES`, `READING_INTERVAL_SECONDS`, `relay_allowed(method, path)`; no
  `METRICS` import, so `web.py` stays light).
- `custom_warnings.py`: take limits from the contract; `max_age_seconds` cap becomes
  min(60, window) (drop the stale-limit cap); add `catalog_from(definitions)` (the catalog
  filter over served public dicts; `catalog()` calls it on `METRICS`); add
  `load_rule_rows(path) -> (rows, rejected)` (file-level checks as today; each row validated
  alone; rejected rows returned whole) and keep `load_rules` as the strict wrapper; add
  `unreachable(rule) -> str | None` (gap ≤ interval, or readings span beyond the window).
- `warning_engine/evaluator.py`: use `load_rule_rows`; `custom_status` gains `rejected_ids`;
  `error` only for file-level failures.
- `warning_chat.py`: `SETUP_INSTRUCTIONS` gap wording (max age 1–60 and within the window,
  default 15; readings every ~5 s).
- dashboard `views/health.helpers.js`: the rejected-rows line.

Phase 2 (form, routes, relay):

- add `projects/vehicle_data/custom_warning_store.py` (advisor side: list, install, remove,
  idempotency, cap, duplicate check, broker catalog check, tolerant read, atomic write). The
  existing `warning_status` and `remove_saved_warning` move here from `warning_chat.py`
  (already 901 lines) and lose the owner filter.
- `warning_chat.py`: two dispatch branches calling the store; status uses the store's list.
- `web.py`: `custom_warning_relay_allowed(method, path)` in the allowlist expression.
- `api_schema.py`: `AssistantWarningsResponse` (list reply) beside `AssistantWarningResponse`.
- dashboard: add `src/customWarningForm.helpers.js` (pure: defaults, derived fields, preview
  sentence, validation, range lines, statuses, fill-from-rule), `src/dialogs/CustomWarnings.jsx`
  and `CustomWarnings.helpers.js` (controller over `createRequester` from
  `WarningChat.helpers.js`, `useController.js`, `useModalDialog`), `src/styles/custom-warnings.css`
  (palette tokens only); `src/app/dialogs.js` registers `customWarnings`; `views/Health.jsx`
  button opens it. In this phase the dialog has a secondary link "Describe it to Codex instead"
  that opens the existing chat in `add-warning` mode unchanged, so the agent path keeps working.
- `WarningChat.helpers.js`: `RULE_READINGS` reads the contract (removed in Phase 3).
- tools: `demo_assistant.mjs` gains a `custom-warnings` scene (GET `/v1/assistant/warnings` with
  a catalog slice and two saved rules, one Watching and one "Saved; starts on the next check");
  `readme_shots.playwright.js` entry for `13-…` opens the form, fills coolant / Above / 230 /
  30 s and taps Review, never Add warning (requests other than GET are aborted anyway). The
  scene's coolant trend in `demo_data.mjs` must make the range line agree with 230 °F being a
  sensible limit (30-day high around 221 °F).
- docs: new `projects/vehicle_data/docs/custom-warnings.md` (the contract: fields in plain
  words, routes, storage, ownership, limits, reading interval, deploy and rollback);
  `docs/warning-chat.md` "Add Early Warning" shrinks to the help role and links it; vehicle-data
  README custom-warning paragraph; `dashboard/docs/caveats.md` field-duty line; the dashboard's
  "How warnings work" page if it describes the chat.
- `AGENTS.md`: one map paragraph: "Custom early warnings (owner request 2026-11-17): the form is
  the way to add a rule. `custom_warnings.json` with `custom_warning_contract.py` (fields,
  operators, limits, cap, reading interval, form defaults, routes; shared with the dashboard),
  `custom_warnings.py` (catalog, validation, tolerant load, evaluation), `custom_warning_store.py`
  (advisor list/install/remove), contract `projects/vehicle_data/docs/custom-warnings.md`;
  dashboard `src/customWarningForm.helpers.js`, `dialogs/CustomWarnings{.jsx,.helpers.js}`,
  `styles/custom-warnings.css`. The setup chat is optional help that fills the form."

Phase 3 (help panel, deletions): `warning_chat.py` (setup context and open rules, instructions,
deletions below), `web.py` (drop the chat apply pattern), `WarningChat.jsx` / `.helpers.js`
(`warning-help` mode: open loads status only; the first send posts `/v1/assistant/chats` with
`message` and `metric`; snapshot exposes the proposal rule for the form; deletions below), new
`src/dialogs/WarningHelp.jsx` (the panel, reusing the chat controller and its transcript rows),
`CustomWarnings.jsx` (hosts the panel; Use these settings), `app.css` (drop chat-monitor,
chat-saved and chat-proposal rules), docs.

Code shape: `warning_chat.py` is over 800 lines, so every new advisor function goes in
`custom_warning_store.py`; `WarningChat.helpers.js` is 776 lines and must shrink in Phase 3, not
grow; strings live in each helpers file's `TEXT` table; no inline styles (CSP).

### 7. Tests

Phase 1, `tests/test_custom_warnings.py` (through `evaluate_rule`, `validate_rule`, the public
evaluator):

- `test_coolant_rule_confirms_with_readings_just_over_five_seconds_apart` (readings 5.2 s apart,
  gap 15 → warning).
- `test_gap_at_the_old_stale_cap_never_confirms_at_the_recorded_spacing` (gap 5 → candidate with
  one observation; documents the finding).
- `test_max_age_may_exceed_the_stale_limit_but_not_the_window`.
- `test_one_rejected_row_leaves_the_other_custom_rules_evaluating` (`EarlyWarningEvaluator` over a
  file with a valid row and a row on an unknown metric: `rule_ids` has one, `rejected_ids` the
  other, `error` is None).
- `test_file_level_corruption_still_reports_an_error_and_built_ins_continue`.
- `test_contract_limits_are_the_validator_limits` (each bound in the JSON accepted, one past it
  refused).
- `test_reading_interval_matches_the_broker_unit_history_interval` (parse
  `systemd/van-telemetry.service`).
- `test_catalog_from_served_definitions_equals_catalog` (`catalog_from` over
  `public_dict()`s).
- `test_unreachable_reports_short_gap_and_window_too_small`.
- dashboard `test/health.helpers.test.mjs`: rejected-rows line.

Phase 2:

- `tests/test_custom_warning_store.py`: `test_confirmed_form_rule_installs_atomically_and_loads`,
  `test_unconfirmed_or_extra_fields_are_refused_without_a_write`,
  `test_same_request_id_replays_without_a_second_row`,
  `test_reused_request_id_with_another_rule_is_refused`,
  `test_identical_trigger_is_refused`, `test_cap_of_32_is_refused_without_a_write`,
  `test_metric_missing_from_broker_catalog_is_refused`,
  `test_broker_unreachable_refuses_install`, `test_unreachable_rule_is_refused`,
  `test_list_is_vehicle_wide_and_marks_created_here`,
  `test_any_paired_browser_removes_any_rule`,
  `test_rejected_rows_survive_writes_and_are_removable_by_id`,
  `test_form_and_chat_paths_accept_and_refuse_the_same_rules` (one table of rules through the
  chat proposal check and the install route).
- `tests/test_warning_chat.py`: `test_custom_warning_routes_are_relayed_only_as_get_list_and_post_install`
  (other methods and paths under `/v1/assistant/warnings` → 404; origin, fetch-metadata, header
  and JSON gates apply as for chats); update the setup approval test for vehicle-wide removal.
- dashboard `test/customWarningForm.helpers.test.mjs`: derived fields for every hold choice pass
  the contract limits and `unreachable`; preview sentence for above, below, absolute, custom and
  engine-off; validation messages; range lines from a trend, the 7-day fallback, and no trend;
  outside-bounds and every-reading soft warnings; default engine checkbox by prefix; auto title
  follows fields until edited; fill from a saved rule and from a proposal; statuses.
- dashboard `test/CustomWarnings.helpers.test.mjs`: 401 shows pairing; load shows catalog and
  list; Review then Add warning posts `confirmed: true` once; a double tap and a retry after a
  timeout reuse one `request_id`; a changed draft gets a new one; 409 and 503 details shown;
  remove; `onWarningsChanged` after install and remove.
- `tests/test_run_tests.py` passes (new dashboard tests are under the gate automatically; any new
  Python test that names the dashboard folder must be listed in `DASHBOARD_PYTEST_TARGETS`).
- Screenshot: retake `13-create-custom-early-warning.png` only (`node tools/readme_shots.mjs
  --only 13` on vanpi, the host of the committed set; `--promote` leaves the set's
  `captured_on` and `build` alone for a partial retake), update
  its manifest caption ("Add Early Warning form with an illustrative coolant rule, its recorded
  range and preview, at the review step; nothing was saved.") and the root README alt text.

Phase 3: `test_setup_chat_starts_only_with_the_owners_question`,
`test_setup_context_carries_focus_metric_range_and_latest_from_cached_gets`,
`test_setup_chats_are_matched_per_focus_metric`,
`test_chat_apply_and_remove_warning_routes_are_gone` (advisor and relay 404),
`test_setup_proposal_is_validated_but_never_installed`; dashboard controller tests for the
`warning-help` mode (no request on open, first send posts `/chats` with message and metric,
proposal exposed for the form); delete the tests of removed code.

Every change set runs the whole suite (`run_tests.py`, by the vanpi route in `AGENTS.md`).

### 8. Deploy and rollback

Before Phase 1 the deployer keeps a copy of the rules file:
`cp -p /var/lib/van-telemetry-advisor/early-warnings.json /var/lib/van-telemetry-advisor/early-warnings.json.before-form`
(skip if the file is absent). Never paste the file or the access code into an agent
conversation.

Phase 1, in order:

1. Broker: `s restart_broker` (parked; the sanctioned restart). `PartOf=` restarts both web
   listeners with it.
2. Advisor: `sudo systemctl restart van-telemetry-advisor.service` (prompt wording and the shared
   validator).
3. Dashboard build (frontend-only, allowed any time).
4. Live check: `curl -s --unix-socket /run/van-telemetry/api.sock http://broker/v1/health | jq .custom_rules`
   shows `enabled: true`, `error: null`, `rejected_ids: []` and as many `rule_ids` as saved
   rules; journals of the three units clean.
5. Rollback: remove (from the list) every rule whose gap exceeds its metric's stale limit, or
   restore `early-warnings.json.before-form`, before reverting; the old broker's strict loader
   would otherwise stop every custom rule. Then revert and repeat steps 1–3.

Phase 2, in order (each intermediate state is safe: an old bundle never calls the new routes; a
new allowlist before the new advisor gets a 404 from the advisor):

1. `sudo systemctl restart van-telemetry-advisor.service`.
2. `sudo systemctl restart van-telemetry-web.service van-telemetry-web-tailscale.service`.
3. Dashboard build.
4. Live checks: through each listener, an unpaired request to `/v1/assistant/warnings` answers
   401 (allowlisted and gated), not 404, e.g.
   `curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: http://192.168.6.103:8765' http://192.168.6.103:8765/v1/assistant/warnings`.
   The owner then opens the form on the tablet, sees the list, adds one test rule (it shows
   Watching within about 10 s while the broker runs) and removes it. Agents do not install live
   rules.
5. Rollback: revert, restart advisor and both web listeners, rebuild. Form-made rules stay in the
   file; the old advisor lists them on the browser that made them.

Phase 3: same order and checks as Phase 2 (advisor, web listeners, build), plus one help
question with internet, and the form saving while the Pi has no internet (the model
unavailable). Rollback: revert, restart advisor and web listeners, rebuild; rules are untouched.

Chats and rules saved before the change: every saved rule keeps working and appears in the
vehicle-wide list; a rule saved with a gap at or below 5 s shows "May never trigger" and the
owner copies it to the form and replaces it. Old setup chats stay on the Pi (they count toward
the 200-chat limit) but are no longer reachable from the dashboard after Phase 3, because help
chats are matched per focus metric. Explanation chats are unchanged.

### 9. What to delete (Phase 3 unless noted)

- `warning_chat.py`: `apply_warning` and the `/apply-warning|/remove-warning` dispatch
  (`84d95f3`); the automatic "Help me set up a new early warning." first question (`84d95f3`);
  `public()`'s `catalog` and the import of `catalog_summary` (`78d510b`, `ce71087`); the loop in
  removal that flips `applied` on chat turns (`84d95f3`); the status `warnings` fields
  (`84d95f3`).
- `custom_warnings.py`: `catalog_summary` (`78d510b`, `ce71087`); the stale-limit cap (Phase 1,
  `84d95f3`).
- `web.py`: `apply-warning|remove-warning` in the chat path pattern (`84d95f3`).
- `WarningChat.jsx`: `Proposal`, `SavedWarnings`, the monitor disclosure, `add-warning` mode
  (`84d95f3` via `static/warning-chat.js`, ported in `2fe75a4`; monitor `78d510b`).
- `WarningChat.helpers.js`: `SETUP_EVENT` uses in `eventSelector` and `dialogTitle`,
  `setupTitle`, `setupEvidence`, `monitorView`, `TEXT.monitor*`, `RULE_READINGS`, `proposalText`,
  `appliedText`, `applyBody`, `savedWarningsView`, `removeSavedBody`, `warningRemovePath`,
  `confirmApply`, `confirmRemove`, `confirmRemoveSaved`, proposal and saved state in the
  controller, `applyWarning`, `removeSaved` (`2fe75a4`; monitor `78d510b`, `ce71087`).
  `setupWaiting` (`7f2c5ce`) stays for the help panel.
- `app.css`: `.chat-monitor*`, `.chat-saved*`, `.disc.chat-proposal` (`78d510b`, `2fe75a4`).
- `demo_assistant.mjs`: the `setup` scene, `SETUP_CATALOG`, `SETUP_PROPOSAL` (`78d510b` and
  earlier), replaced in Phase 2 by the `custom-warnings` scene.
- Tests of each of the above.

### 10. Phasing

1. **Phase 1, evaluator and shared contract** (broker restart): relaxed gap cap, per-row
   tolerance with `rejected_ids`, `catalog_from`, `unreachable`, the JSON contract, prompt
   wording, Health rejected line. The chat keeps working and existing rules start confirming
   once replaced with a workable gap. Review before install.
2. **Phase 2, the form** (advisor, web listeners, build): routes, store, relay, dialog, list,
   screenshot 13, docs, map. The old chat stays reachable from the form. Review before install.
3. **Phase 3, help inside the form and deletions** (advisor, web listeners, build). Review
   before install.

Each phase is one change set, built in a worktree, with a cross-family review before it is
installed.

## Decided (each undoable)

- Rules are vehicle-wide; transcripts stay per browser. Undo: restore the owner filter in
  `custom_warning_store.py`.
- The stale-limit cap on `max_age_seconds` is dropped (≤ min(60, window)), because the
  freshness check already excludes stale data and the cap made persistence unreachable. Undo:
  restore the cap in `validate_rule`, after removing rules that exceed it.
- One bad row disables only itself in the broker. Undo: evaluator back to `load_rules`.
- Form defaults: For at least 30 s (7 readings, window 60 s), gap 15 s, engine-running on except
  battery and tire metrics, no default limit. Undo: edit `form_defaults` in the contract.
- Persistence is chosen as a duration and shown with its reading count; the exact fields sit in
  a collapsed disclosure.
- Review step inside the dialog replaces proposal approval; removal keeps `window.confirm`.
- Unreachable rules (gap ≤ 5 s, or readings beyond the window) are refused by the form and the
  install route but not by `validate_rule`, so existing rules are not switched off; the list
  flags them.
- Identical triggers are refused; same request id replays.
- The advisor refuses an install when it cannot read the broker's catalog.
- Recorded ranges come only from data the dashboard already has; new range data is a follow-up.
- The agent never installs: its proposal fills the form (Phase 3); the automatic first turn goes.
- Only screenshot 13 changes; the set stays at 19 images.

## Open questions for the owner

None blocking. Follow-ups he may want later: recorded ranges for rpm, speed, torque and the
transmission speeds, and a running-only "typical high" (broker work, measured against
`docs/broker-cpu.md`).

## For the reviewer

- Confirm or refute the persistence finding by reading `evaluate_rule` against the 5 s history
  loop in `broker.py` (around line 2746) and `history/queries.py::recent_numeric_samples`. If a
  replay is wanted, `tools/warning_replay.py` against the read-only export on vanpi works without
  live access.
- Check that relaxing the cap cannot admit stale data: `_qualified` and the query both require
  `freshness = 'fresh'`.

## Status

Plan only. No code changed. Next step: plan review, then a fresh builder starts Phase 1 from this
note.
