# Telemetry advisory episodes and delivery boundary

The vehicle-data historian persists explainable advisory episodes separately
from raw telemetry. This layer reads stored broker snapshots only. It cannot
open a CAN socket, transmit a diagnostic request, reconfigure SocketCAN, reset
a USB device, or power-cycle a hub.

## Lifecycle

Each rule has at most one open episode. A `watch` opens an episode without
notification eligibility; a persistent `warning` opens or escalates it. An
affirmative `normal` or `suppressed` evaluation resolves it. An unavailable,
insufficient-history, or plausibility-rejected evaluation is inconclusive: it
updates the episode's evidence state but deliberately does not claim recovery.

Episode rows retain the first and latest complete assessment, while transition
events retain open, escalation/de-escalation, material context change,
evidence-loss/restoration, acknowledgement, and resolution history. Exact
metric source, bus, quality, provenance, operating regime, baseline regime,
and thresholds remain in those assessment payloads. Acknowledging an episode
cancels its pending delivery and suppresses further delivery for that episode;
a later resolved-and-recurrent condition creates a new episode.
After both evaluators complete, Insights supplies their exact authoritative
rule catalog. An open episode whose rule was deliberately removed is resolved
as `suppressed`, receives a `rule_retired` event, and has any pending delivery
cancelled. Partial/manual persistence calls omit this catalog and therefore
cannot retire a rule by accident.

## Notification outbox

Only an unacknowledged `warning` explicitly marked notification-eligible may
enter the SQLite outbox. `watch` never does. The outbox deduplicates payloads
and rate-limits across recurrent episodes of the same rule. Failed attempts
use bounded retries and terminal failure state. One pending or terminal-failed
row owns an episode until it resolves, so periodic evaluation cannot create an
unbounded chain of retries. Dispatch is serialized through fetch, delivery, and
final marking; persisted error text is bounded to the SQLite contract. The
dashboard shows pending/failed counts and the latest dispatcher error rather
than equating enabled delivery with successful delivery.

The default library sink remains disabled. The tracked telemetry service
explicitly opts into `NtfyAdvisoryNotificationSink`, which invokes the host's
existing `/usr/local/bin/ntfy-send` helper with a fixed argument vector. That
helper delivers to the configured local ntfy server or atomically queues the
message for its existing retry timer when the server is unavailable. No token
or server secret enters this repository. Removing the explicit broker flags
returns delivery to disabled mode without changing episode persistence.

## Mechanical/electrical boundaries

- Generator field duty is prohibited as a primary warning signal. It may only
  corroborate an independently persistent low-voltage deviation. The owner's
  routine house-battery DC-DC charger can legitimately command high generator
  duty, so that load transition alone cannot open an episode.
- The approximately 12 psi oil-pressure advisory is fixed to fresh qualified
  `engine.oil_pressure` from passive `0x41D` plus independent `engine.rpm` from
  passive `0x0FC`, with their exact observed quality and psi/rpm units. It
  requires RPM
  at or above 400, a continuous ten-second startup/cranking grace interval,
  and two independent post-grace low-pressure observations within ten seconds.
  It remains an OEM-context advisory rather than a component diagnosis or a
  replacement for the factory warning lamp.
- A transmission-oil-temperature change greater than 10 °C within less than
  one second is rejected on individual raw `0x1F7` frames before aggregation,
  cache admission, and metric history. Stateful readers quarantine the bad
  level while retaining the last good temperature and both sibling shaft-speed
  fields. The historian stores a deduplicated quality incident containing the
  rejected delta/window evidence, not a fabricated replacement temperature.
  This incident is outside the advisory lifecycle and is never notification
  eligible. A broker restart does not strand an active quality incident: the
  first authoritative admitted sample from the replacement process retires the
  prior-process incident, while an empty startup snapshot cannot claim recovery.

## USB/CAN infrastructure advisories

The same lifecycle covers the three connected logical roles without relying
on ephemeral `canN` names. It records missing or ambiguous serial/`dev_id`
resolution, controller health, topology-generation changes, and restoration
inhibits. A missing/unresolved role must persist for two historian samples
before notification eligibility; ambiguity, a controller outside
`ERROR-ACTIVE`, a topology-generation change, or a restoration inhibit is
immediate. These assessments are observational only and never trigger an
automatic reset or service/link mutation.

A receive-only kernel removal incident is the sole notification owner while it
covers the same topology/role loss. The topology and role episodes remain
visible but suppress their own notification eligibility, avoiding multiple
alerts for one physical reset. Missing USB incident history is inconclusive,
never affirmative recovery.

Removal edges also carry an event-level advisory-consumption checkpoint. An
edge stays replayable across later snapshots and process restarts until the
episode transaction commits; only then is it marked consumed. Broker queue
acknowledgement accepts non-removal events plus removal IDs explicitly proven
consumed. This preserves an edge that arrives just after a snapshot timestamp,
and a notification-delivery failure does not undo the already durable episode
or consumption checkpoint.
