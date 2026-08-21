# Migrated agent context

This document preserves cross-project facts and user constraints that previously existed only in
Claude's external project-memory store. Detailed technical state remains in the canonical root and
project handoffs linked from `AGENTS.md`; prefer those when they are newer. Verify all live machine,
service, cron, network, and vehicle state before acting.

## Diagnostic approach and user constraints

- At each major fork, first check existing repo findings and locally available OEM material, then search
  for current OEM procedures/TSBs and relevant tool behavior, and ask what tools or access the user has.
  Low-level CAN/UDS reverse-engineering is the fallback after the ecosystem layer is exhausted.
- A local AllData/factory-document scrape has historically lived at `~/dev/ram_2022_GAS`; check whether
  it still exists when factory wiring or DTC procedures matter.
- The 2022 ProMaster is the user's full-time home, office, and dog's space. Prefer driveway/campsite DIY,
  tools operated on-site, and experiments piggybacking on normal driving. Dealer/shop drop-off is not a
  practical recommendation unless the user explicitly reopens it.
- Do not edit/disable crontab or the cron daemon without explicit permission, even to prevent CAN
  contention. Inspect first and let the user control unrelated persistent automation.

## Physical diagnostic topology

- The vehicle's Security Gateway is intentionally bypassed. Do not attribute failed UDS writes,
  routines, or actuations to stock SGW authentication; investigate session, addressing, ECU security,
  power mode, and bus state instead.
- The permanent Pi installation uses two dual-channel USBCANFD adapters and three simultaneous taps:
  C-CAN pins 6/14, B-CAN pins 3/11, and CAN CH pins 12/13; the fourth channel is unconnected and stays
  down. All vehicle roles are classical CAN with FD off and are identified by USB serial plus `dev_id`,
  never by Linux enumeration order. The exact executable mapping and rates live
  in `lib/vehicle_can_roles.py`; `docs/bus-map.md` is the canonical human map.
- The OBDLink MX+ can remain on the parallel diagnostic branch while the Pi observes the permanent
  taps. Historical 2026-07 PCAN captures remain valid evidence for the physical pair they record, but
  their `can0` name and single-adapter cable-switching procedure are not current routing instructions.
  A 2026-07-21 simultaneous observation captured AlfaOBD UDS to `0x85/87/98/D9` on pins-3/11 B-CAN
  while the standard BCM `0x40` profile routed over C-CAN. Legislated OBD-II Mode 01 traffic does not
  appear on the internal C-CAN tap; do not retry it as a generic signal source.
- SocketCAN `listen-only` state is sticky across an ordinary link bring-up. Every configuration must
  explicitly select classical CAN with FD off, the role's fixed bitrate, listen-only on or off as
  intended, and `restart-ms 0`, then verify one fresh atomic readback. The legacy PEAK driver also
  lacks `berr-reporting on`; that is retained as a historical adapter-specific limitation, not a
  reason to infer anything about a current `gs_usb` role.
- Safety-hardened inventory CLIs default to an offline dry run. On installed dual-USBCANFD hardware,
  their live mode derives the physical channel from `Module.bus` and the exact serial/`dev_id`, takes
  the logical-role lock followed by the currently resolved-channel lock, rechecks identity under
  ownership, verifies classical-CAN/interface/inhibit/vehicle gates, and restores passive mode before
  unlocking. A saved/default `can0` is not a bus identity. Explicit channel
  names may appear in offline capture metadata, fixtures, or deliberately
  arbitrary-hardware library use, but are not a current ProMaster bus-selection
  workflow. Cooperative locks supplement rather
  than replace service/drive-capture preflight and manual coordination with external tools.
- Parked C-CAN diagnostic TX can wake the BCM, briefly power switched accessories, and boot the dashcam.
  The user approved low-frequency parked TX without a separate prompt, but it is still an observer and
  battery effect. Avoid gratuitous traffic. See `docs/bus-map.md` for verified wake behavior.
- On 2026-08-20 the separate machine-local `van-dashboard.service` was cleaned of its fixed-`can0`
  raw monitor and COP ALERT RF-Hub wake path. COP ALERT remains a non-CAN exterior-light and
  notification feature; the existing ignition-monitor marker pauses its lights while driving. It no
  longer keeps the dashcam or vehicle network awake. The dashboard may display cache-only telemetry
  over HTTP, but it does not open a CAN socket, inspect a CAN interface, or request vehicle wake.
- Standing owner authorization: Codex may run already-reviewed, physically addressed, non-mutating
  data reads without requesting separate conversational approval each time. This covers UDS `19`,
  `1A`, `22`, result-only `31 03`, and controller-defined read-only AlfaOBD observations, provided no
  session-changing or session-maintenance preamble is required. A metric registry or controller
  action list is a mechanical schema/scope guard, not a request for per-reading human approval. This
  standing authorization does not authorize functional
  broadcast, `10`, `3E`, reset, SecurityAccess, writes/coding/PROXI, DTC clear/control,
  CommunicationControl, IOControl, or routine start/stop (`31 01`/`31 02`). Current vehicle-state,
  physical-pair, interface, scope, rate, locking, and cleanup gates remain mandatory; this standing
  authorization is not evidence that the van is parked, the engine is off, or the intended bus is
  connected.

## Private environment context

Machine-specific environment/secret locations, local monitoring paths, and private compute-worker
instructions live in the ignored root `AGENTS.secret.md`. Read that companion when it exists. Never
print or commit secret values or a full VIN. Systemd does not inherit interactive-shell or cron
environments; configure an `EnvironmentFile=` only when a unit needs it and the user authorizes the
service change.

## AlfaOBD data provenance

- Read the canonical [`AlfaOBD evidence history`](alfaobd-evidence-history.md) before treating a
  selected profile, rendered value, catalog label, timeout, or output file as ground truth.
- `~/claude/shared-files/old.AlfaOBD_Debug.bin` is a large aggregate from the owner's previous 2015
  diesel ProMaster, not the current van. Treat its modules/DIDs only as same-family candidates to verify.
- `~/claude/shared-files/AlfaOBD logs and data July 8 2026/AlfaOBD_Debug.bin` and the RFH, adaptive-cruise,
  and engine info logs are from the current 2022 van. `BCDELPHI_Info.log` is cumulative and mixed:
  early snapshots include the 2015 van, while its 2026-06-22 tail time-aligns with current-van BCM
  commands. Use only timestamp-correlated records, never the file wholesale. Verify source VIN while
  decoding and keep unique VIN digits out of tracked output.
- AlfaOBD debug bins use ASCII hex representing bytes XORed with `0xFF`. The maintained pipeline is
  `tools/alfaobd_decode.py` → `projects/ecu_mapping/vin_scan.py` → `extract_did_map.py` →
  `reassemble_commands.py`; read `projects/ecu_mapping/README.md` first.
- The owner-authorized Android-tablet pull includes AlfaOBD 2.4.4.0's installed APK under the
  gitignored `tmp/ecu_mapping/android_tablet/` tree. Its 51 split database assets reconstruct into a
  valid SQLite catalog. `tools/alfaobd_apk_db.py` and `tools/alfaobd_catalog.py` reproduce the
  read-only extraction/export; the canonical interpretation and hashes are in
  `projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md`. Raw numeric string
  placeholders are authoritative because direct indexing into the English resource is not yet a
  valid label decode. Do not redistribute the APK/database.
- Earlier interpretation of BCM `27xx`/`2Axx` as SecurityAccess was wrong: they were ISO-TP consecutive
  frames within a long `2E 2023` PROXI write. The current-van capture contains successful `2F` IO-control
  operations but no verified `27` exchange. Verify all candidates live before replaying.

## Other migrated finding: sliding-door ajar input

The right sliding-door ajar plunger historically failed to depress on soft close, preventing fob lock
and causing wake cycles. Research identified circuit G76 (BK/VT), BCM C6 pin 20, in the contact board.
The FCA input uses an internal pull-up: closed is open-circuit, so an inline disconnect can simulate
closed after polarity is verified on-vehicle. Do not unplug the whole contact-board connector because it
also carries lock power. This bypass removes the unlatched-door warning/alarm/dome behavior; treat it as
a safety-affecting hardware modification, not a casual software fix. No ProMaster-specific OBD setting
was verified. Recheck OEM wiring before any physical work.

## Superseded external memories

Claude's old battery, radar, TPMS, B-CAN, and repo-layout memories were not copied verbatim because their
newest verified state is already tracked in `docs/bus-map.md`, project READMEs, project handoffs, findings,
and executable libraries. Those tracked sources win over older conclusions in the external memory files.
