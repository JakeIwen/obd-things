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
- The Pi PCAN and AlfaOBD adapter can share a Y-splitter downstream of the bypass on internal C-CAN.
  This permits listen-only PCAN capture of AlfaOBD module-addressed UDS traffic without recabling.
  Legislated OBD-II Mode 01 traffic does not appear on this internal tap; do not retry it as a generic
  signal source. AlfaOBD UDS is visible on C-CAN; B-CAN generally exposes only resulting body-bus effects.
- PCAN `listen-only` is sticky: a later ordinary interface bring-up does not clear it. Any intentional
  TX setup must explicitly use `listen-only off` and then restore the documented passive state. The
  adapter/driver does not support `berr-reporting on`; use received-frame and RX-error counters.
- Parked C-CAN diagnostic TX can wake the BCM, briefly power switched accessories, and boot the dashcam.
  The user approved low-frequency parked TX without a separate prompt, but it is still an observer and
  battery effect. Avoid gratuitous traffic. See `docs/bus-map.md` for verified wake behavior.

## Environment and secrets

- Historical source of environment configuration: `~/secrets/.bash_variables`, with exported
  `OBD_VIN` and `NTFY_VOLTAGE_URL`. Interactive shells sourced it from `.bashrc`; cron used it through
  `BASH_ENV`. Confirm this is still true rather than rewriting shell configuration.
- systemd does not inherit `.bashrc` or cron's `BASH_ENV`; add an appropriate `EnvironmentFile=` only
  when a unit actually needs these variables and the user authorizes the service change.
- Never print or commit secret values or the full current VIN. The secret file was historically mode
  0644; recommend mode 0600 if it is still too broad, but do not change it outside the requested scope.

## AlfaOBD data provenance

- `~/claude/shared-files/old.AlfaOBD_Debug.bin` is a large aggregate from the owner's previous 2015
  diesel ProMaster, not the current van. Treat its modules/DIDs only as same-family candidates to verify.
- `~/claude/shared-files/AlfaOBD logs and data July 8 2026/AlfaOBD_Debug.bin` and the RFH, adaptive-cruise,
  and engine info logs are from the current 2022 van. The `BCDELPHI_Info.log` inside that set is stale
  2015-van data. Verify source VIN while decoding and keep unique VIN digits out of tracked output.
- AlfaOBD debug bins use ASCII hex representing bytes XORed with `0xFF`. The maintained pipeline is
  `tools/alfaobd_decode.py` → `projects/ecu_mapping/vin_scan.py` → `extract_did_map.py` →
  `reassemble_commands.py`; read `projects/ecu_mapping/README.md` first.
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
