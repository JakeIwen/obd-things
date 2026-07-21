# Parked C-CAN candidate DID inventory — 2026-07-21

## Conditions and safety boundary

The owner ran `tools/ccan_inventory_campaign.sh --candidate-dids` while parked with ignition ON,
engine OFF, and the PCAN on the pigtail's C-CAN DB9 (DLC 6/14). The campaign made 1,341 physical
UDS `22` ReadDataByIdentifier requests in the inherited/default session:

- the complete `F100-F1FF` page on TCM, shifter, BCM, cluster, and telematics; and
- 61 BCM-only DIDs whose positive responses were already present in the current-van AlfaOBD trace.

It sent no functional request, session change, TesterPresent, write, routine, IO-control, DTC-clear,
or SecurityAccess request. All six child reports completed without interruption or fatal error and
each verified its own passive restore. The wrapper then restored the initially active
`tpms-logger` service. After ignition went OFF, that service observed `0x2EF` disappear and returned
to its zero-TX idle state.

## `F100-F1FF` results by ECU

All five ECUs answered every request either positively or with `7F 22 31`; there were no timeouts.
The positive counts and DIDs are:

| module | positive | `7F 22 31` | positive DIDs |
|---|---:|---:|---|
| TCM | 28 | 228 | `F100 F10B F10D F112 F132 F158 F180 F181 F182 F183 F184 F185 F186 F187 F188 F18C F190 F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0` |
| shifter | 28 | 228 | `F112 F122 F132 F158 F180 F181 F182 F183 F184 F185 F186 F187 F188 F18A F18B F18C F190 F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0` |
| BCM | 23 | 233 | `F180 F181 F182 F183 F184 F185 F186 F187 F188 F18C F190 F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0 F1F0` |
| cluster | 24 | 232 | `F180 F181 F182 F183 F184 F185 F186 F187 F188 F18A F18B F18C F190 F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0` |
| telematics | 32 | 224 | `F100 F10B F10D F112 F122 F132 F158 F180 F181 F182 F183 F184 F185 F186 F187 F188 F18A F18B F18C F190 F191 F192 F193 F194 F195 F196 F1A0 F1A1 F1A4 F1A5 F1B0 F1F6` |

The sweep reconfirmed the identity strings already used in the module registry/bus map and added
useful per-ECU evidence:

| module | selected identity/configuration responses |
|---|---|
| TCM | `F187=46342086`; `F192=ES11-1065 D`; `F194=68532161AF`; `F18C=T001U629208429` |
| shifter | `F187=P7FK46LXHAD`; `F188/F194=AGSM637FCA.`; `F18C=TA6060702003880`; `F192=073250002B0` |
| BCM | `F187=68524831AF`; `F188/F194=04446561007`; `F18C=TD3ZM3421101880`; `F192=BC637M.0001`; `F1F0=13GJ6D0YN7CB` |
| cluster | `F187=68517084AD`; `F18C=TC141126211EJD0`; `F192=50019990002`; `F194=04009460924` |
| telematics | `F188=52225318`; `F18C=TF0711552047510`; `F191=52182163`; `F192=TBM200A11P`; `F194=214284` |

The telematics ECU also returned a 255-byte value at `F1F6`. It begins with byte `01`, ten ASCII
`A` characters, and ten ASCII `0` characters, followed by structured binary data and zero fill.
Its label and encoding are unresolved; retain the raw value as an opaque, module-specific
configuration/identity lead rather than guessing from printable substrings. `F190` VIN responses
were automatically redacted in the reports.

Raw reports:
`tmp/inventories/{tcm,shifter,bcm_ccan,cluster,telematics}/dids_20260721_*.{results.jsonl,summary.json}`.

## Current-van AlfaOBD BCM candidates

The direct BCM pass independently reverified 59 positive DIDs:

`0133 0136 2013 0103 2001 2002 2003 1008 2008 1009 2009 200A 200B 200C 2010 1921 013B 013C 1000 1002 1004 1204 2949 2050 0130 0131 0132 0135 0137 0138 0151 0144 0150 0152 0153 0154 2920 2921 2922 2923 2946 2944 2962 2A50 3000 3001 3500 3DDD 3DDE 3FFD 3FFE 3FFF 40A1 40A2 40AA 292C 102A 292D 292E`.

This proves support and response length under the recorded condition, not the labels or scaling.
Notable structure worth later controlled correlation includes:

- `1008` and `2008`: nearby 32-bit values (`0x00038940` and `0x0003893D`);
- `2050`: six bytes `20 22 04 29 16 03`, possibly date/time-like but not yet labeled;
- `2962`: 70 bytes of `FF`; `2A50`: 35 zero bytes;
- `3500`: three related six-byte records;
- `40A1`: 64 bytes, and `40A2`/`40AA`: 80-byte structured records; and
- `292C`: ASCII `0000000`.

`40A3` and `40A6` returned `7F 22 31` here even though the current-van AlfaOBD trace previously
captured positive `62` responses. The AlfaOBD sequence had successfully entered extended session
with `10 03 -> 50 03 00 32 01 F4`; a successful `3E 00 -> 7E 00` at `21:54:17.454` and continuous
reads kept that session active through the `40A3`/`40A6` reads at `21:54:24-25`. The controlled
comparison documented below proves that these two DIDs are session-gated on this BCM.

Raw report:
`tmp/inventories/bcm_ccan/identity_20260721_013810_984496-0600.json`.

## Complete default-session BCM pages

The follow-on `--bcm-pages` campaign ran parked with ignition ON and engine OFF, then the owner
turned ignition OFF. All 1,024 physical reads received responses; all four reports completed and
verified passive restoration, with no interruption, timeout, or fatal error.

| page | positive | `7F 22 22` | `7F 22 31` | positive DIDs |
|---|---:|---:|---:|---|
| `0100-01FF` | 17 | 2 | 237 | `0103 0130 0131 0132 0133 0135 0136 0137 0138 013B 013C 0144 0150 0151 0152 0153 0154` |
| `2000-20FF` | 12 | 0 | 244 | `2001 2002 2003 2008 2009 200A 200B 200C 2010 2013 2023 2050` |
| `2900-29FF` | 11 | 2 | 243 | `2920 2921 2922 2923 292C 292D 292E 2944 2946 2949 2962` |
| `4000-40FF` | 3 | 0 | 253 | `40A1 40A2 40AA` |

The four condition-gated reads were `0155`, `0157`, `2940`, and `2947`. These returned
`conditionsNotCorrect`, rather than `requestOutOfRange`, in the default session and recorded
condition. The full pages found one positive DID absent from the sparse candidate list: `2023`.

BCM `2023` returned a 250-byte record. It matches the most recent AlfaOBD `2E 2023` write's
complete 250-byte payload at every byte still observable after automatic VIN-pattern redaction.
The older AlfaOBD write differs in several unredacted fields.
This establishes `2023` as the readback partner for the large BCM configuration/PROXI dataset and
shows that the currently readable record corresponds to the later captured AlfaOBD payload. It
does not by itself decode individual fields. Offline parser correction also recovered the response
sequence for both historical writes: each received `7F 2E 78` (`responsePending`) followed by the
positive WriteDataByIdentifier acknowledgement `6E 20 23`. The earlier apparent lack of a response
was a parser defect caused by an interleaved `STPTO` adapter command, a final consecutive frame
without AlfaOBD's usual trailing hint digit, and a separately logged bare prompt.

The corrected dates also enable one bounded label correlation. The first positive write occurred at
2026-06-22 00:21:57; the mixed/cumulative `BCDELPHI_Info.log` records `Headlamp LED Management:
ABSENT -> PRESENT` immediately before the same session's fault read at 00:22:18, and the command
trace records its DTC clear at 00:22:28. The two 250-byte write payloads differ at four characters in
their leading ASCII metadata and at payload offset `0x8F`, where the June-22 value `0x42` became
`0x02` in the June-25 write. Bit `0x40` at offset `0x8F` is therefore a strong candidate for the
Headlamp LED Management configuration flag. It is not yet a verified field definition: the later
write may have reverted the option or may reflect a different configuration operation, and the text
log does not provide a second matching label. Confirm with controlled before/after `22 2023` reads
around one labeled AlfaOBD change before using this offset.

The repeated reads also isolate four values that changed between the 01:38 sparse pass and the
02:19 page pass, separated by exactly one observed ignition-off/on transition:

- `2008`: `0003893D -> 00038942`;
- `2009`: `0078 -> 0033`;
- `200A`: `051A -> 051B`; and
- `2050`: `20 22 04 29 16 03 -> 20 22 04 29 58 03`.

The exact `+1` at `200A` makes an ignition/startup counter the leading interpretation, consistent
with the same FCA environmental-record convention seen at the RF Hub, but it remains a BCM-specific
candidate pending another controlled key cycle. The other changes prove dynamic or event-linked
content only; their units and labels remain unresolved. Every overlapping DID outside those four
returned byte-for-byte the same value as the sparse pass.

Raw reports:
`tmp/inventories/bcm_ccan/dids_20260721_021721_*.{results.jsonl,summary.json}` through
`tmp/inventories/bcm_ccan/dids_20260721_022347_*.{results.jsonl,summary.json}`.

## Controlled BCM session comparison

At 03:16 the owner ran the four-DID comparison parked, ignition ON, engine OFF. In the inherited
diagnostic state, `40A3`, `40A4`, `40A5`, and `40A6` all returned `7F 22 31`. The second pass then
received the exact validated session response `10 03 -> 50 03 00 32 01 F4`. Under that session:

| DID | inherited/default state | session `03` |
|---|---|---|
| `40A3` | `7F 22 31` | positive, 14 bytes: `01 00 00 00 00 00 00 00 00 00 D1 01 0B 18` |
| `40A4` | `7F 22 31` | `7F 22 31` |
| `40A5` | `7F 22 31` | `7F 22 31` |
| `40A6` | `7F 22 31` | positive, 16 zero bytes |

This isolates diagnostic session as the cause of the support difference for `40A3` and `40A6`;
it is no longer merely a cross-date or vehicle-state correlation. `40A6` exactly matches the prior
AlfaOBD value. `40A3` has the same length and structure as its AlfaOBD value, with two late bytes
changed (`... D1 00 0F 18` then versus `... D1 01 0B 18` now), establishing dynamic/event-linked
content without yet establishing a label or scale. Both child reports completed and restored
passive mode.

Raw reports:
`tmp/inventories/bcm_ccan/dids_20260721_031634_459940-0600.*` and
`tmp/inventories/bcm_ccan/dids_20260721_031637_471601-0600.*`.

## Complete BCM session-03 page

At 13:13 the owner completed the justified `4000-40FF` page parked, ignition ON, engine OFF.
The BCM accepted `10 03` with exact `50 03 00 32 01 F4`; all 61 periodic `3E 00` requests received
exact `7E 00`; and all 256 DID reads received a response. The report is complete, has no fatal or
partial state, and records a successful passive restore.

The page contained exactly five positives:

| DID | session-03 result | comparison with default page |
|---|---|---|
| `40A1` | positive, 64 bytes | positive and byte-identical in default state |
| `40A2` | positive, 80 bytes | positive and byte-identical in default state |
| `40A3` | positive, 14 bytes: `01 00 00 00 00 00 00 00 00 00 D1 01 0B 18` | session-gated; `7F 22 31` by default |
| `40A6` | positive, 16 zero bytes | session-gated; `7F 22 31` by default |
| `40AA` | positive, 80 bytes | positive and byte-identical in default state |

Every other DID in the page—including `40A4` and `40A5`—returned `7F 22 31`. The broad pass
therefore bounds the observed effect of session `03` in this page to the already isolated `40A3`
and `40A6`; it discovered no additional session-only DID. Repeating adjacent BCM scans is not
justified without a new label, state-dependent lead, or compatible diagnostic database.

Raw report:
`tmp/inventories/bcm_ccan/dids_20260721_131311_438149-0600.{results.jsonl,summary.json}`.

## Source and PCM follow-up

A same-day exact-string search of the local 1.7 GB OEM corpus and publicly indexed sources found no
ODX/PDX, diagnostic database, or DID labels for `13GJ6D0YN7CB`, `04446561007`, `TBM200A11P`,
`TF0711552047510`, `AGSM637FCA`, or `P7FK46LXHAD`. The official Mopar catalog does independently
identify [`68524831AF` as a Body Controller](https://store.mopar.com/oem-parts/mopar-body-controller-module-68524831af),
but it supplies no diagnostic schema. This is a dated negative search, not proof that a database is
unavailable outside public indexes. The broader source audit remains in
[`2026-07-19_odx_pdx_source_research.md`](2026-07-19_odx_pdx_source_research.md).

The default-session identity and evidence-selected BCM pages are now sufficiently inventoried.
Broad adjacent scanning had sharply diminishing returns: the four full pages added only `2023`
beyond the AlfaOBD-derived positive list. The controlled comparison then proved that session `03`
exposes `40A3` and `40A6`; the subsequent complete session-03 `4000-40FF` pass found no other
session-only positive. Another broad BCM page is not justified without new evidence.

The then-unresolved PCM `0x10` path remained separate. The parked ignition-ON/engine-OFF probe at 03:16
sent an unpadded `10 92` ISO-TP request to `18DA10F1` but received no response, so the tool correctly
skipped `1A 87`. Offline comparison then found one wire-format difference: immediately before every
successful AlfaOBD exchange, the app programs ELM `PP 2C=01` and `PP 2D=01`. The
[official ELM327 definition](https://elmelectronics.com/wp-content/uploads/2020/05/ELM327DSL.pdf)
decodes those values as 29-bit, fixed eight-byte CAN frames, ISO-15765 formatting, and
500 kbit/s. Our SocketCAN ISO-TP default uses the minimum DLC for a single frame. A later fixed-DLC-8
retry while parked with the engine idling received `50 92` and a positive `5A 87` response containing
`68532157AI`, independently verifying the endpoint. The successful run changed both framing and engine
power state, so it does not isolate which difference resolved the timeout. Exact evidence and report
provenance are recorded in
[`2026-07-19_live_ecu_discovery.md`](2026-07-19_live_ecu_discovery.md).
