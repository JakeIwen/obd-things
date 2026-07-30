# PCM generator-duty direct-read recipe and passive-field follow-up

Date: 2026-07-30  
Vehicle scope: installed 2022 ProMaster 3.6 L PCM only  
Classification: exact physical diagnostic-read recipe verified; passive carrier unresolved

## Result

PCM DID `01A1` is directly readable without sending DiagnosticSessionControl when the
request uses fixed-DLC-8 zero padding:

```text
normal-fixed 29-bit physical addressing
tester -> PCM: 18DA10F1
PCM -> tester: 18DAF110
request:        22 01 A1, ISO-TP TX padding 00
response:       62 01 A1 80 00
decode:         raw_u16be * 100 / 32768 = 100.000000 %
```

Two independent one-request probes, each starting from and restoring a verified
500 kbit/s listen-only C-CAN interface, returned the exact response above. Neither
probe sent `10`, `3E`, a functional request, or any service other than physical
`22`. A preceding unpadded `22 01A1` attempt timed out and also restored verified
listen-only mode.

This establishes the minimum recipe observed in the current parked, engine-running
state as **padded `22 01A1`, with no explicit session change**. It does not positively
identify the inherited ECU session. No external PCM request or session-maintenance
traffic appeared during two subsequent five-second passive watches, but a future
post-ignition-cycle test is still the cleanest way to label the behavior
`default_session` rather than `inherited/unknown`.

Passive `0x0FC` samples immediately before the probes decoded to approximately
847–848 rpm. The interface had a usable same-boot C-CAN pins-6/14 topology record,
was `ERROR-ACTIVE`, and had zero TX/RX bus-error counters. `tpms-logger` and
`tpms-drivesniff` were inactive. The ordinary telemetry collector's observer lock
caused several pre-transmission deferrals; the probe waited for the exclusive lock
instead of racing it.

Gitignored evidence:

| artifact | SHA-256 |
|---|---|
| unpadded attempt summary | `1d5224e3d9ab29b9f7779a27826156ad85df3a38bad5687a8337875ffec7027f` |
| unpadded result | `9d40854bdab01139195cd05c79b9527560a3c498bc308ccb8ea502293be4256a` |
| first padded positive report | `c8c9ccaac8130d91d30e3868e4d2e7871cb4c5017e2dd53141cc6e17a0752e81` |
| second padded positive report | `60439989485a7004917d361cea8fb62f269115716249df3995fba248b84d8fce` |

## Targeted passive-field search

The earlier coarse searches found no defensible passive carrier: the idle dataset
ranked `0x100` first at R² 0.7862291, while the loaded drive ranked `0x412` first
at R² 0.6079955. Because different identifiers led in different operating regimes,
the 2026-07-30 follow-up searched arbitrary unsigned DBC/cantools geometry only in
the exact streams `sff:100:8` and `sff:412:5`, using 8-, 10-, 12-, and 16-bit
lengths in both byte orders.

The targeted search did not rescue either candidate:

| dataset | best targeted result | R² | interpretation |
|---|---|---:|---|
| parked idle | `0x100`, `u16le@22` | 0.7899693 | only a negligible improvement over the coarse warm-up covariance |
| loaded drive | `0x412`, `u12le@28` | 0.6080081 | only a negligible improvement over the coarse result |

The leading identifier and field still fail to transfer across the independent
operating regimes. Both reports remain mechanically `candidate_only`, with scale
and physical identity unverified and telemetry promotion forbidden.

Freezing each best-fit formula and applying it without refitting to the other
independent dataset supplied the decisive counterexamples:

| frozen development formula | independent evaluation | coverage | RMSE | p95 absolute error |
|---|---|---:|---:|---:|
| loaded-drive `0x412 u12le@28` | parked idle | 100% / 732 samples | 8.004 percentage points | 14.806 percentage points |
| parked-idle `0x100 u16le@22` | loaded drive | 99.63% / 1,351 samples | 50.793 percentage points | 76.664 percentage points |

The conversions to percentage points use the established DID full-scale
relationship `percent = raw * 100 / 32768`. These are frozen-formula errors, not
refitted correlations. They reject both fields as transferable passive
Generator Duty Cycle representations.

Targeted-search reports:

| dataset | compute job | report SHA-256 |
|---|---|---|
| parked idle | `20260730T065837Z-a57b7270` | `fc47fff5e57f4b1f085ec43f2c3f2a9635595889e4bc512d5ba2006154d9eaff` |
| loaded drive | `20260730T065838Z-85c9f319` | `c3cd75f1569a83118af26f86f5ec3b07202035b9fbfebfa0a3e87b8f17adda13` |
| loaded `0x412` formula on idle | `20260730T070409Z-8b3b0b8e` | `01922f0df6d1a14add52297f87a5b929915ee9fd6eb958764279df878cedfc51` |
| idle `0x100` formula on loaded | `20260730T070409Z-197e1946` | `021f9e920836ef71a445f703224d06239b3cda7ef38d3ae7ca3ad6d065ad80a6` |

The practical telemetry path is therefore the guarded physical DID reader, not a
promoted passive frame. Generator duty remains a command/field-effort quantity,
not generator current or alternator temperature; thermal or output-current alerts
require separate evidence.
