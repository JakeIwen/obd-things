# Uconnect access and intermittent CarPlay symptoms — 2026-10-08

Research and saved-evidence review only. No new CAN traffic, device access,
reset, setting change, or service change was performed.

## Owner report and identification boundary

The owner reports a **10.1-inch display**, navigation speech through CarPlay
becoming progressively quieter over roughly 5–10 drives, and occasional
switches away from the phone/GPS screen. Selecting the phone icon usually
restores the GPS view. The destination screen, navigation app, wired/wireless
connection, and radio replacement history were not established in this review.

The historical diagnostic evidence conflicts with assuming a 10.1-inch radio
variant: July 21 Uconnect `F194` was `145.00.15`, and `280B` contained software
level `145`, part level `3`, and sales code `UBC`. The saved current-vehicle
AlfaOBD `UCONNECT_Info.log` repeats those values. FCA bulletin 08-173-23
associates UBC/UEC with the **7-inch R1L** family. Preserve both observations;
do not reinterpret the owner's display size, infer incorrect vehicle coding,
or choose firmware from this mismatch. Reconcile current on-screen Version
Information with fresh bounded identity reads before any software decision.

The [July B-CAN discovery](2026-07-21_bcan_live_ecu_discovery.md) and
[status correlation](2026-07-21_alfaobd_live_status_correlation.md) remain the
canonical raw identity and per-ECU DID evidence. `lib/modules.py` already
registers `uconnect_bcan`: B-CAN, DLC 3/11, 125 kbit/s,
`18DA87F1 -> 18DAF187`. This is a verified diagnostic connection, not an
established shell, ADB, or application-log connection.

## What is already observable

| Surface | Existing evidence | Limitation for these symptoms |
|---|---|---|
| Fault records | Physical `19 02 FF` works | History is not a timestamped incident log or proof of present failure |
| `180C` | Six-byte volume/EQ group; volume 17 matched the saved UI | Not established as CarPlay navigation/announcement volume |
| `1004` | Voltage with observed `raw * 0.1 V` rendering | A CAN voltage value does not establish a brief drop at the radio's power pins |
| `1008`, `1009`, `2009`, `200A`, `200B`, `200C` | Lifetime, key-on, and first-DTC counter associations in July status reads | Not proven application uptime, reboot reason, or per-fault freeze frames |
| `1823` | ECU-temperature association; two observed values support `raw - 40 C` | No independent temperature ground truth; intentionally omitted from production telemetry |
| `1820`, `1821` | Candidate panel/steering-wheel input groups | Individual bits still need controlled mapping |
| EMCM2 `2A00`, `2A01` | Controlled rotary, Mute, and Screen-button observations | Different ECU namespace; these are input reads, not commands to the radio |
| `F191`–`F195`, `280B` | Hardware/software identifiers and software/sales-code group | Current variant must be reconciled as above |

Many July status associations were observed through AlfaOBD. Do not infer
session-unchanged support for every DID from that observation. The later
bounded `1823` support check is separately recorded in the
[August readiness finding](2026-08-27_three_bus_telemetry_readiness.md).
No CarPlay connection-state, foreground-app, prompt-volume, restart-reason,
or crash-log DID has been established. No radio volume/source-control payload
has been verified for replay.

## Saved October 8 fault snapshot

`tmp/inventories/uconnect_bcan/dtcs_batch_dtc-owner-20261008T090715Z_uconnect_bcan.json`
completed at **2026-10-08 09:12:52 UTC**, parked, ignition on, engine off.
It contains 37 records: six confirmed/history-only (`08`), 31 incomplete-only
(`40`), and **zero current failures**. The six stored codes are:

`B143A-11`, `B1570-19`, `B1577-13`, `B210A-16`, `B280B-02`, `U0155-86`.

The exact-vehicle local OEM corpus has radio procedures for:

- `B1570-19`: USB communication overcurrent. The procedure also lists
  unsupported devices/cables, so the title alone does not prove a short.
- `B1577-13`: Universal Consumer Interface circuit open; the procedure
  investigates the radio/media-hub connection, not just phone attachment.
- `U0155-86`: invalid cluster communication signal at the radio.

The local `B210A-16` procedure is labeled DSM low voltage, and the `B280B`
camera/video procedure covers subtype `00`, not the observed `02`. The local
`B143A-11` procedure is labeled TBM. Retain those module/subtype boundaries;
do not apply their thresholds or full repair trees to this radio as exact
matches. USB-path and supply issues are leads, not explanations for the
reported gradual volume change or screen switching. Do not clear history to
make comparisons easier.

OEM source root verified locally: `/home/pi/dev/ram_2022_GAS/vehicle/`;
procedures are under `all_diagnostic_trouble_codes_(_dtc_)/testing_and_inspection/`.

## Relevant manufacturer documentation

Sources accessed October 8 MDT (October 9 UTC), 2026:

- [Apple, Developing CarPlay Systems, Part 2](https://developer.apple.com/videos/play/wwdc2016/723/):
  volume controls select the active audio category. During a navigation
  announcement they adjust navigation volume; after it finishes they adjust
  media volume. This supports testing inadvertent prompt-level changes, but
  does not establish the cause here. Native controls/voice interactions and
  resource management can also take the screen from CarPlay without proving
  that the phone disconnected.
- [2022 ProMaster owner's manual](https://vehicleinfo.mopar.com/assets/publications/en-us/Ram/2022/ProMaster/104359_22_VF_OM_EN_USC_DIGITAL_E3_V1.pdf),
  printed pages 111–113: Audio > Volume Adjustment exposes separate media,
  phone, and navigation levels; Notifications includes navigation pop-ups;
  System Information includes Version Information. Equipment-dependent menus
  require observation on the installed unit. These settings lack a verified
  repository CLI control.
- [FCA 08-006-24 Rev. A](https://static.nhtsa.gov/odi/tsbs/2024/MC-10251848-9999.pdf),
  January 17, 2024: includes ProMaster and 10.1-inch Uconnect 5 NAV variants
  UBN/UEN/UFN/UPN. It addresses native navigation resets and unsolicited
  navigation screen changes following failed map updates, using a USB map
  update. It is a close symptom lead only if the destination is native NAV
  and current hardware/applicability agrees; it is not a generic CarPlay fix.
- [FCA 08-173-23](https://static.oemdtc.com/NHTSA-PDFs/MC-10242802-9999.pdf),
  July 13, 2023: original FCA document mirrored by OEMDTC, for 2022–2023
  ProMaster 7-inch UBC/UEC. Its target `145.00.15` equals the historical
  diagnostic value. It does not establish the latest firmware today or
  compatibility with the owner-reported 10.1-inch system.
- [FCA S2108000029 Rev. A](https://static.nhtsa.gov/odi/tsbs/2021/MC-10193980-9999.pdf),
  April 2, 2021: describes STAR-assisted radio-log capture for repeatable
  Uconnect 5 faults. This is evidence that an OEM log workflow exists on that
  platform, not a verified DIY export procedure for this ProMaster variant.

A bounded OEM-corpus search for radio-log capture/export and engineering/developer
mode phrases found no matches (compute job `20261009T042449Z-78559c26`, grep
exit 1, empty stderr). This does not prove the radio lacks those facilities.
No public exact-variant SSH/ADB/log-export recipe was established.

## Recommended investigation

1. Reconcile radio identity and record the numeric Navigation volume before
   and after normal drives. A falling setting and quieter sound at an
   unchanged setting are different problems. Check whether a knob adjustment
   during a spoken instruction selects the navigation indicator. Also record
   phone/iOS/app version and selected Uconnect user profile.
2. For each screen switch, record UTC time, exact destination screen, whether
   CarPlay audio continues, whether a reconnect banner appears, and any nearby
   knob/button, notification, call, or native voice-recognition event.
3. Preserve the existing three-bus drive evidence around those timestamps.
   The broker and drive recorder were active at inspection; the recorder was
   waiting for qualified drive ownership. This is not continuous parked
   recording and is not currently a Uconnect-specific decoded logger.
4. A future focused logger can combine passive event windows with bounded
   awake-only identity/voltage/counter/input reads and non-clearing DTC
   snapshots. First test whether `180C` responds to navigation-only changes;
   if it does not, recover an exact-variant request/decoder before inventing
   another volume metric. Preserve negative replies and observer effects.
5. Distinguish CarPlay display arbitration from disconnection, then compare
   native-NAV behavior against the map-update bulletin if applicable. Control
   replay, resets, firmware/maps installation, and parked wake logging remain
   separate work requiring their existing gates.

No new logger or production metric was implemented by this research pass.
