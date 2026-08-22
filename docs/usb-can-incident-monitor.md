# USB CAN transient incident monitor

The telemetry broker's ordinary serial-role snapshot is intentionally periodic.
A USB hub can disappear, remove both dual-CAN boards, and re-enumerate them in
about one second without any five-second historian snapshot observing an absent
role.  `projects/vehicle_data/usb_can_monitor.py` closes that evidence gap with
a receive-only kernel kobject-uevent listener.

## Scope and safety boundary

The monitor:

- opens an `AF_NETLINK` / `NETLINK_KOBJECT_UEVENT` multicast socket for receive
  only;
- accepts only the installed CAN adapter VID:PID (`1d50:606f`), netdev edges
  beneath an already identified adapter, and USB-device edges from dynamically
  learned ancestor hubs;
- learns the relevant branch from read-only sysfs and the same exact installed
  board serials used by the role resolver;
- keeps a bounded pending queue and bounded recent views; and
- exposes `receive_only=true` and `hardware_actions=false` in every retained
  event and in broker status.

It has no send, sysfs-write, reset, unbind/rebind, power-cycle, service-control,
SocketCAN, or interface-configuration path.  A monitor failure degrades only
incident observation; it cannot interfere with normal passive reconciliation.

## Identity, lifecycle, and persistence

Kernel event identity is a SHA-256 digest over the boot ID, kernel uevent
sequence number, action, device path, event kind, and stable device identity.
SQLite treats `event_id` as a primary key and rejects an identity collision with
different evidence.  Replayed queue entries therefore cannot create another
incident or advisory observation.

A parent-hub removal opens one branch incident and absorbs the consequential
adapter and CAN-netdev removal edges.  An isolated board or netdev removal opens
a board incident.  USB add events record reappearance but do not declare
recovery.  The incident resolves only after a fresh broker role snapshot proves
that every affected board again resolves to its exact serial roles and those
roles are healthy.  The resolved incident remains in SQLite.

Each monitor process also has a unique producer identity.  If the broker
restarts after persisting a removal but before observing recovery, an empty new
in-memory map is not treated as recovery.  SQLite retains the prior producer's
active incident until the new receive-only producer is running and a fresh
snapshot proves every affected serial's complete exact-role set healthy.  The
historian then appends a deterministic synthetic recovery edge and resolves,
rather than deletes, the prior incident.

The broker attaches the monitor's pending batch only to its internal historian
snapshot. Removal edges remain unconsumed until advisory episode persistence
also commits. Unconsumed edges replay from SQLite across later snapshots and
process restarts; this is independent of the event's immutable first-snapshot
provenance. The broker acknowledges non-removal queue entries after the full
checkpoint and removal IDs only when the result explicitly proves their
consumption. A failed advisory pass or an edge whose wall timestamp falls just
after the snapshot cutoff therefore remains queued/replayable. Notification
delivery happens after consumption and cannot undo it because the durable
outbox owns delivery retries. The
historian stores:

- immutable events in `usb_can_events`;
- lifecycle revisions in `usb_can_incidents`; and
- bounded-queue/drop counters per historian snapshot in
  `usb_can_monitor_samples`.

These compact incident/event tables are not raw CAN data and are not pruned
with seven-day metric samples.  Snapshot foreign keys become null when an old
snapshot is pruned, preserving the event evidence itself.

## Advisory behavior

`InfrastructureHealthEvaluator` emits
`usb_can_transient_disconnect` immediately when the current historian commit
contains a new kernel removal edge or an incident remains unresolved.  It is a
single rate-limited infrastructure warning even when one hub edge causes four
CAN netdev removals.  A subsequent healthy exact-role reconciliation returns
the assessment to normal and resolves the persisted advisory episode.
Topology-generation and per-role episodes remain visible for the same loss but
are notification-suppressed while this higher-fidelity kernel incident covers
them. Missing incident-history evidence is `unavailable`, not recovery.

The assessment can enter the existing notification outbox when advisory
delivery is explicitly enabled.  It reports observed infrastructure evidence,
not a diagnosis, and never invokes a recovery action.  Broker status exposes
the live bounded view under `status.usb_can_monitor`; the health response adds
durable history under `usb_can_incidents`.

## Validation and deployment state

Focused offline tests inject synthetic uevent datagrams and fake sysfs trees.
They cover exact filtering, one parent incident for both boards, kernel-event
deduplication, bounded queue overflow accounting, delayed healthy resolution,
SQLite atomicity, advisory open/resolve behavior, and acknowledgement only
after successful historian ingest.

Deployment completed on 2026-08-21 through the ordinary reviewed telemetry
restart. The live monitor opened its receive-only netlink socket, learned both
installed board serials and five relevant ancestor hubs, and reported
`state=running`, zero pending/dropped/active events, and no error. SQLite
`quick_check` remained `ok`; no USB reset, link mutation, CAN transmission, or
test notification was used to validate it.
