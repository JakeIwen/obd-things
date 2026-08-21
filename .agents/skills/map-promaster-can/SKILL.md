---
name: map-promaster-can
description: Safely research, survey, implement, and document CAN/UDS diagnostics for Jacob's 2022 Ram ProMaster on vanpi. Use for passive DLC-pair surveys, SocketCAN/ISO-TP tooling, ECU address discovery, 11-bit or 29-bit diagnostic addressing, DID/DTC/routine inventories, ODX/PDX research, AlfaOBD or wiTECH trace analysis, and controlled signal-scaling experiments in the obd-things repository.
---

# Map ProMaster CAN

Work passive-first, preserve provenance, and keep every conclusion scoped to the exact ECU variant that produced it. Treat the repository's tracked maps as durable memory and correct them instead of creating competing summaries.

## Establish context

1. Read `git status`, preserve existing work, and read the current root `AGENTS.md` instructions.
2. Read `README.md`, `docs/bus-map.md`, `lib/modules.py`, and `docs/agent-context.md`.
3. Read the target `projects/<name>/README.md` and any handoff it names before modifying that project.
4. Inspect live interface, service, device, and mount state before relying on historical facts.
5. Check whether the OEM resources on `m4mac` or any local mirror are actually reachable before planning around them.

## Classify bus interaction before acting

- **Passive:** Receive frames only with SocketCAN listen-only explicitly enabled. Passive capture does not transmit or ACK.
- **Diagnostic read:** Services such as `22`, `19`, and `31 03` transmit, may change diagnostic-session state, can wake modules, and can power accessory rails. Call them read-only diagnostically, never passive.
- **Actuation or mutation:** Treat DTC clearing, ECU reset, SecurityAccess attempts, communication/DTC control, writes, IO control, coding/programming, and routine start/stop as actuation. This includes services such as `11`, `14`, `27`, `28`, `2E`, `2F`, `31 01`, `31 02`, and `85`.

Do not infer actuation permission from permission to survey or read. Require explicit owner authorization, an exact payload review, safe vehicle conditions, and a verification/recovery plan before actuation.

## Coordinate live CAN ownership safely

Before manual CAN work:

1. Read `projects/vehicle_data/README.md` and `projects/tpms/README.md`, then inspect the broker, recorder, TPMS logger, every `can*` interface, USB identity, bitrate, classical-CAN/FD mode, listen-only state, restart policy, and error counters. Read-only inspection does not authorize a service or link change.
2. Determine which permanent role is in scope. The installed dual-USBCANFD topology has three vehicle roles and one unused channel; use the exact USB serial plus `dev_id` map in `lib/vehicle_can_roles.py` through a role-aware resolver/owner. Linux `canN` names are ephemeral and must never select a physical bus. Historical PEAK captures remain evidence for their recorded physical pair, but the retired single-adapter workflow is not a current operating path.
3. Coordinate ownership of the one logical role in scope before manual work. Cooperative passive observers may coexist under shared role/channel locks; stop only an active non-cooperating owner or an observer that blocks the required exclusive lease, following its deployment handoff. Do not stop an owner merely because it uses another physical bus, and do not alter service enablement, cron, or unrelated services without explicit authorization.
4. Use role-aware tools whose live path derives the channel from `Module.bus`, holds the logical-role lock and the resolved-channel lock, and rechecks the USB identity after locking. Do not recreate the removed single-adapter bring-up workflow, save a current `canN`, or manually substitute one role's netdev for another.
5. For passive surveys, require the exact resolved role to be classical CAN with FD off, at its fixed rate, listen-only, ERROR-ACTIVE, and `restart-ms 0`. Use shared role/channel observer ownership and write raw output below `tmp/captures/` with logical bus, physical pair, resolved identity, ignition state, wake condition, and timestamp in the metadata or filename.
6. Active diagnostics must use an already reviewed role-aware arming/restoration path. Dry-run first; require the tool's vehicle-state, physical-pair, inhibit, identity, rate, and cleanup gates. Never clear listen-only on a guessed or merely current `canN`.
7. Restore and verify the exact passive role state before releasing ownership. Restore only a service that was deliberately stopped **and** whose current deployment handoff authorizes restarting that effective installed unit; never start a disabled/staged migration as generic cleanup. A failed or unprovable CAN restoration is a blocking fault, not permission to retry on another channel.

Never transmit during a passive DLC-pair survey. A silent capture is inconclusive until bus wake state, wiring, polarity, bitrate candidates, and RX error behavior are accounted for.

## Research before broad probing

1. Search tracked findings and local OEM material first.
2. Use native live web search and DDGS for independent discovery; use Playwright only for dynamic pages and downloads, and PDF tools for rendering/extraction.
3. Search exact ECU family, supplier, hardware, software, and part identifiers rather than vehicle model alone.
4. Treat a PDX as an archive and inspect nested files by type. Work on copies under `tmp/`, record source URL, access date, SHA-256, license, and extraction method.
5. Parse ODX with the repository research environment when available, but require an exact or explicitly compatible ECU variant match before accepting names, requests, scaling, sessions, or routines.
6. Never merge DID namespaces globally. The same DID can mean different things on different ECUs.
7. Do not bypass authentication, paywalls, licensing controls, or access restrictions.

## Extend transport and discover ECUs conservatively

Use `lib/modules.py` as the executable addressing source of truth. When adding 11-bit support, represent addressing mode explicitly and preserve current 29-bit normal-fixed behavior by default. Route every generic tool through the shared transport abstraction and test both modes offline before live use.

An ECU discovery tool must:

- identify itself as active diagnostic traffic;
- default to a bounded target set and conservative request rate;
- use non-mutating identification or presence requests;
- record positive responses, negative responses, timeouts, addressing mode, channel, bitrate, and conditions;
- avoid functional broadcast unless its exact effect is understood and explicitly selected;
- refuse to run while the interface is listen-only rather than silently claiming no ECUs;
- write results under `tmp/`; and
- restore the interface to passive mode afterward.

Probe likely identity DIDs before a full `0000-FFFF` sweep. Keep standardized, supplier, ODX-derived, same-platform, and observed-AlfaOBD candidates labeled separately until verified on this van.

## Inventory without mutation

For each independently verified module:

1. Record bus, bitrate, addressing mode, request/response IDs, power state, session, and ECU identity.
2. Inventory DIDs with `22`, preserving NRCs and unresolved timeouts.
3. Inventory DTCs with supported `19` subfunctions without clearing them.
4. Discover routines only with `31 03` requestRoutineResults. Never infer that result-only enumeration permits `31 01` or `31 02`.
5. Rate-limit requests, checkpoint output, and distinguish unsupported, locked, conditions-not-correct, unresolved, and transport failure.
6. Keep each ECU's outputs and canonical DID map in its relevant project.

## Establish names and scaling experimentally

Change one physical variable at a time and capture a baseline, the controlled change, and a repeat. Use an external ground-truth instrument or known labeled source for absolute units; correlation alone proves association or relative scaling, not identity or absolute scale. Record timing, state, units, uncertainty, byte order, signedness, offset, multiplier, and counterexamples.

Use AlfaOBD or wiTECH observation only for unresolved labels or explicitly authorized operations, and separate observed tool behavior from independently reproduced behavior.

For DID-to-passive mapping, use this staged offline workflow:

1. Establish the ECU-scoped label and physical scale, then extract exact kernel-timestamped raw diagnostic request/response observations (including historical PCAN captures where that was the observer). Do not sample-hold buffered CSV values as the primary timebase.
2. Run `tools/can_timeseries_correlate.py` with its backward-compatible coarse profile. Keep channel, SFF/EFF namespace, CAN ID, DLC, source hashes, and maximum match staleness in the evidence.
3. Shortlist at most two identifiers, then use `--bit-search-id` with selected lengths and byte orders for arbitrary DBC/cantools Intel or Motorola geometry. Do not exhaustively expand the whole bus.
4. Keep the first-pass result `exploratory_candidate`; one representative capture is sufficient to shortlist and learn from it. Evaluate a frozen field on a complete independent drive leg only when pursuing an operational proxy or verified decode. Never randomly split adjacent samples from one drive into train and holdout sets.
5. When semantically different torque stages remain highly correlated, use the correlator's opt-in `--regime-analysis` on no more than four exact shortlisted streams. Make the speed/RPM/throttle fields and raw-unit classifier thresholds explicit, derive them on the development leg, and freeze them before holdout use.
6. Follow `docs/can-evidence-tiers.md`. A frozen formula may qualify as an `operational_proxy` only for a declared non-critical use and explicit whole-leg error tolerances. It remains physically unverified and cannot enter the ordinary verified telemetry allowlist. Require counterexamples, physical plausibility, and independent scaling evidence only for `verified_decode` promotion.

Use `tools/signal_correlate.py` for offline DID-to-DID relationships. Keep `tools/can_field_finder.py` as a bounded exploratory aid, not as the provenance or promotion authority. Submit full saved-log searches through an existing named `pi_compute` task; do not run them on vanpi. Obtain owner approval before adding or changing a `.van-compute.json` task.

This workflow accelerates field layout, scaling, and DID-to-broadcast correlation. It does not discover which DID addresses exist; use ECU-scoped catalogs, wire mining, and bounded DID scanners for that separate task.

## Promote verified knowledge

- Broadcast frame, decode, or wake/sleep behavior -> `docs/bus-map.md`.
- ECU addressing, bus, or addressing mode -> `lib/modules.py` and the bus-map summary.
- DID, DTC, service, routine, or scaling behavior -> the relevant project findings/map.
- Campaign state and next step -> the relevant project README.

Include provenance and confidence. Mask the unique VIN in tracked output. Keep raw and machine-written material under `tmp/`, promoting only selected evidence deliberately.
