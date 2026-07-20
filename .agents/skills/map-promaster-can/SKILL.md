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

## Coordinate the live adapter safely

Before manual CAN work:

1. Read `projects/tpms/README.md`, then inspect `tpms-logger`, `can*`, PCAN USB presence, bitrate, listen-only state, and error counters.
2. Stop `tpms-logger` using the documented exception. Do not alter its unit, enablement, cron, or unrelated services.
3. Confirm the PCAN is physically connected to the intended DLC pair. One channel observes one pair at a time.
4. For surveys, bring the interface up explicitly listen-only and write raw output below `tmp/captures/` using pair, rate, ignition state, wake condition, and timestamp in metadata or filename.
5. Restore the documented passive state and restart `tpms-logger` when the manual campaign ends, including after failures.

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

Prefer existing `tools/signal_correlate.py` for DID relationships and `tools/can_field_finder.py` for passive broadcast fields. Use AlfaOBD or wiTECH observation only for unresolved labels or explicitly authorized operations, and separate observed tool behavior from independently reproduced behavior.

## Promote verified knowledge

- Broadcast frame, decode, or wake/sleep behavior -> `docs/bus-map.md`.
- ECU addressing, bus, or addressing mode -> `lib/modules.py` and the bus-map summary.
- DID, DTC, service, routine, or scaling behavior -> the relevant project findings/map.
- Campaign state and next step -> the relevant project README.

Include provenance and confidence. Mask the unique VIN in tracked output. Keep raw and machine-written material under `tmp/`, promoting only selected evidence deliberately.
