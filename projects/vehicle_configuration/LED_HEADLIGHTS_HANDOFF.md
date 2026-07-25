# LED low beams / IPC lamp-out handoff

Last reviewed: 2026-07-25

## Decision state

The van is presently on a healthy, internally consistent configuration:

- `PROXI Status: OK`
- configuration-check-fail counter `0`
- PROXI write counter `15`
- `Headlamp LED Management: Absent`
- two independent current 250-byte reads are byte-identical
- AlfaOBD's native backup decodes to the same 250 bytes

The next supported experiment is one labeled AlfaOBD change:
`Headlamp LED Management: Absent -> Present`. It has not been performed in the
fresh controlled campaign. Do not edit a raw byte and do not run PROXI
alignment as a preliminary test.

Procedure 1 is complete. Procedure 2 stopped before any write because:

1. AlfaOBD could not retrieve an authoritative factory/VIN configuration; and
2. BCM voltage was 12.81 V rather than the documented 13.2–13.5 V programming
   range.

The owner may explicitly revise the factory/VIN prerequisite and use the
verified current-state backup as the operational rollback reference. Stable
regulated voltage remains a hard prerequisite.

## Problem being investigated

LED low-beam bulbs were installed in place of the OEM bulbs. Their lower and
different electrical load causes the BCM's exterior-light monitoring to report
lamp/circuit faults and the IPC to show a headlight-out warning. The exact
warning path is BCM-originated; there is no evidence that the IPC itself needs
an independent LED coding change.

The current DTC baseline includes active BCM high-beam, low-beam, and DRL
open/short-to-battery families. Because several lighting outputs are involved,
changing `Headlamp LED Management` may alter more than the two low-beam
monitoring thresholds. The experiment must verify all exterior lighting, not
only disappearance of the IPC icon.

## Prior incomplete experiment

The current-vehicle historical AlfaOBD trace contains one useful but incomplete
configuration sequence:

- the cumulative BCM info log records
  `Headlamp LED Management: ABSENT -> PRESENT`;
- a time-aligned 250-byte DID `0x2023` write received response-pending and then
  a positive acknowledgement;
- a DTC clear followed in that session; and
- a later positively acknowledged 250-byte write matches the present
  configuration rather than the earlier payload.

The two historical payloads differ at several leading metadata characters and
at configuration offset `0x8F`: the earlier value has bit `0x40` set and the
later/current value does not. That makes offset `0x8F`, bit `0x40`, a strong
candidate for the labeled option, but not a verified definition. The
cumulative text log supplies only one matching label transition, so other
differences or actions cannot be excluded.

Use the labeled AlfaOBD operation and compare fresh before/after reads. Never
write the candidate bit directly. Canonical analysis:
[`2026-07-21 candidate DID inventory`](../ecu_mapping/findings/promaster_2022/2026-07-21_candidate_did_inventory.md#complete-default-session-bcm-pages).

## Owner-reported no-shift event

An earlier LED experiment was followed by a vehicle state in which the
electronic parking-brake switch was reported unreadable or stuck and the van
would not shift into gear. Returning to the OEM bulbs and disconnecting the
main battery restored operation.

That sequence is important risk evidence but does not establish causation:

- the bulb reversal and battery reset were bundled;
- the exact DTC inventory and module configuration state were not preserved;
- the fault could have involved LED electrical behavior, supply voltage,
  stale/module communication state, an incomplete configuration operation, or
  coincidence; and
- the fresh July baseline currently reports PROXI healthy.

The local OEM corpus confirms that the 948TE vehicle has several interdependent
shift-lock paths. The RF Hub uses hardwired brake input plus CAN inputs for
Brake Transmission Shift Interlock control; dedicated-powertrain CAN or ESM
communication faults may leave the shifter locked in Park **or Neutral**.
Therefore a failure cannot be assumed to leave Neutral selectable.

Before the write, preserve a recovery path that does not depend on electronic
Neutral: trailer winch plus wheel skates/dollies or another reviewed method
appropriate to a locked driveline. The local service corpus did not yield a
confirmed owner-accessible manual Park release for this exact 2022
configuration. Do not invent or improvise one during a fault.

## Verified baseline and backups

The sanitized durable baseline is
[`findings/2026-07-25_proxi_baseline.md`](findings/2026-07-25_proxi_baseline.md).
The raw files remain local and ignored:

- Procedure 1:
  `tmp/proxi_safety/20260724_2033_baseline/`
- Procedure 2 pre-write:
  `tmp/proxi_safety/20260725_procedure2/`

Required rollback inputs:

- native AlfaOBD backup:
  `ProxyBackup_2026_07_24_19_48_12.txt`
- exact current bytes: `proxi_250.bin`
- independent fresh comparison: `current_proxi_250.bin`
- full positive DID responses for both reads
- both reviewed manifests, which contain local hashes

The current record is known-good in the practical sense that the van returned
to normal operation and AlfaOBD reports PROXI OK with a zero fail counter. It
is not proven factory-as-built.

## Factory/VIN configuration limitation

In the exact current BCM profile, AlfaOBD exposes:

- present-PROXI backup and restore;
- custom-PROXI tools;
- a one-option vehicle-configuration change; and
- PROXI alignment.

It does not expose the OEM `Restore Vehicle Configuration` operation that
retrieves authoritative as-built data for the VIN. That is a separate wiTECH
Guided Diagnostics function. No factory/VIN artifact exists in the retained
campaign.

Consequences:

- an every-difference comparison against factory data cannot be completed with
  the available tool;
- the existing 250-byte record is still the correct pre-change comparison and
  operational rollback candidate;
- an AlfaOBD restore capability shown in the menu is not proof that rollback
  has been tested; and
- anyone requiring factory-as-built certainty must obtain wiTECH/RVC access
  before the change.

This limitation is substantive, not a ceremonial precaution. It is also
independent of whether a no-op alignment might succeed.

## AlfaOBD limitations relevant to this run

The installed application is AlfaOBD 2.4.4.0. The exact 2022+ ProMaster BCM
catalog/profile has been verified, but the repository has documented multiple
AlfaOBD definition and rendering defects on other functions and profiles.
Raw responses and exact ECU identity outrank a plausible rendered value.

Specific operational boundaries:

- a visible `DISCONNECT` button does not prove ECU verification has finished;
  wait for the terminal connected-status message;
- a `Could not verify ISO code` warning is a profile-compatibility warning,
  not an automatic success;
- the reusable ADB controller is intentionally read-only and has no generic
  configuration, PROXI, DTC-clear, reset, or routine tap;
- configuration writes and alignment require supervised human interaction;
- taps must never be blindly retried after ambiguous ADB delivery; and
- adapter prompts are terminal state changes and must be handled deliberately.

See [`docs/alfaobd-adb-controller.md`](../../docs/alfaobd-adb-controller.md)
and
[`docs/alfaobd-evidence-history.md`](../../docs/alfaobd-evidence-history.md).

## Adapter and bus routing

Canonical physical routing is maintained in
[`docs/bus-map.md`](../../docs/bus-map.md). The campaign-specific summary is:

| Branch | Vehicle pins | Current access |
|---|---:|---|
| C-CAN | 6/14, 500 kbit/s | standard SGW-bypass path; BCM, PCM, TCM, shifter, IPC, RFH, and other C-CAN participants |
| B-CAN / CAN-IHS | 3/11, 125 kbit/s | OBDLink MX+ internal routing for the successful Alfa reads; PCAN uses the dedicated B-CAN pigtail leg |
| CAN-CH | 12/13, 500 kbit/s | grey adapter remaps vehicle 12/13 to interface 6/14; ABS, EPS, HALF, and ORC verified |

The known physical stack during CAN-CH work was:

`SGW bypass -> grey adapter -> Y-splitter -> PEAK PCAN + OBDLink MX+`

The extra historical `B10 -> A11` wire was removed before the verified grey
campaign. The verified grey mapping must not be repurposed as a blue adapter.

No external blue adapter is presently available. B-CAN identification/DTC
reads succeeded through the OBDLink's internal routing without one, but that
does not prove a 2022+ full alignment will never request adapter 6/blue. If
AlfaOBD requests an unowned or unclear adapter during alignment, stop at the
prompt. Do not guess a routing or continue on the wrong branch.

The exact 2022+ alignment prompt sequence is still unobserved. It may request
grey and then standard routing as participating modules are visited. Every
physical adapter change must:

1. occur only when AlfaOBD explicitly pauses for it;
2. invalidate the Pi's topology record before movement;
3. be confirmed against the exact pin pair after movement; and
4. leave the PCAN listen-only if it remains attached as an observer.

## Alignment and rollback requirements

Changing the BCM option and aligning the vehicle are one supervised campaign.
Do not leave the new BCM configuration installed merely because the headlight
warning changed.

Required successful participants include at least the modules AlfaOBD marks as
configured and alignment-capable, with special attention to BCM, PCM, TCM,
shifter/ESM/AGSM, IPC, ABS, EPS, ORC, HALF, RFH, DASM, climate, and
entertainment/telematics. AlfaOBD may use profile-specific
`Don't care or Not OK` semantics for climate or entertainment; record the exact
result rather than converting that string into success or failure by
assumption.

The campaign is successful only if:

- the BCM accepts the one labeled change;
- alignment completes without a critical participant rejection;
- the final BCM record is saved and its difference from baseline is understood;
- PROXI status returns OK and the fail counter remains zero;
- the intended module-presence/configuration snapshot remains intact;
- the vehicle passes a complete functional/key-cycle check; and
- fresh DTCs do not reveal a configuration or communications failure.

If the BCM write succeeds but a critical alignment participant rejects it:

- do not operate or road-test the vehicle;
- preserve screenshots, UI XML, debug logs, current BCM readback, and a
  non-clearing DTC inventory;
- restore the saved current BCM configuration only through AlfaOBD's supported
  restore path;
- align again only if the original failure cause has been corrected; and
- require the same final status and module/function checks before declaring
  recovery.

Restoring only the BCM and disconnecting is not a complete rollback if other
modules may have accepted part of the new alignment.

## Safe next-run procedure

This is a supervised runbook, not authorization for unattended execution.

1. **Confirm recovery logistics.** Park on level ground where the van may
   remain overnight. Chock the wheels. Keep OEM bulbs, battery-disconnect
   access, trailer/winch support, and a locked-driveline loading plan
   available. Do not rely on Neutral.
2. **Establish power support.** Connect a regulated supply that can hold
   13.2–13.5 V continuously without a timeout. Verify independently that it
   cannot climb above 13.5 V. Engine remains off.
3. **Freeze unrelated automation.** Inspect rather than assume service and
   interface state. Keep the AlfaOBD campaign inhibit active. Do not modify
   cron or unrelated services. If a logger owns the selected CAN interface,
   follow its documented stop/restart procedure.
4. **Verify physical routing.** Start on standard C-CAN pins 6/14 for the BCM.
   Grey is removed. If the PCAN is attached, it is 500 kbit/s,
   listen-only, error-active, and has clean error counters. Record
   same-boot topology as C-CAN only after physical confirmation.
5. **Collect the pre-write checkpoint.** Save current BCM System ID, PROXI
   status, fail/write counters, `Headlamp LED Management`, battery voltage,
   a new DID `0x2023` response, and targeted non-clearing DTC inventories for
   BCM, IPC, TCM, shifter/ESM, RFH, and the CAN-CH safety modules.
6. **Verify exact equality.** Extract the new 250-byte configuration and
   compare it byte-for-byte with both retained known-good binaries. Stop on
   any difference until it is explained. Save all new raw output under
   `tmp/vehicle_configuration/<timestamp>/`.
7. **Confirm the application/profile.** Require AlfaOBD 2.4.4.0 or document
   and revalidate any version change. Select `RAM PRO MASTER (VF) 2022+`,
   the exact Delphi/Marelli/Aptiv BCM, and wait for terminal connection
   confirmation. Do not continue past an unexpected ISO/variant mismatch.
8. **OWNER GO/NO-GO — first write.** Present the fresh equality result,
   voltage, DTC snapshot, recovery readiness, and unresolved factory/VIN
   limitation. No configuration tap occurs before explicit approval.
9. **Make one labeled change.** Use only AlfaOBD's supported
   `Headlamp LED Management: Absent -> Present` operation. Do not change a
   second option, edit raw bytes, clear DTCs, reset a module, or run another
   diagnostic action.
10. **Capture the write result immediately.** Save the exact Alfa status,
    debug artifacts, new DID `0x2023` response, new 250-byte record, counters,
    and byte-level difference report. If the write is rejected or the
    difference is not consistent with one labeled option, stop and plan
    restoration; do not align an unexplained record.
11. **OWNER GO/NO-GO — alignment.** Review the accepted write and difference
    evidence. Alignment begins only after a second explicit approval. A
    no-op/test alignment is never part of preflight.
12. **Run alignment under continuous support.** Follow only AlfaOBD's exact
    prompts. Record each participant/result. For an adapter prompt, stop,
    invalidate topology, make the verified physical change, then resume. Stop
    on an unclear prompt, power excursion, disconnect, app failure, or
    critical module rejection.
13. **Verify before any movement.** Read final PROXI status, fail/write
    counters, full configured/present snapshot, and non-clearing DTCs. Perform
    the required key cycle without removing voltage support until AlfaOBD says
    the write sequence is complete.
14. **Check vehicle functions while stationary.** Confirm service brake,
    brake lamps, EPB apply/release and switch readability, shifter state,
    cluster warnings, steering, exterior lights in every mode, turn signals,
    hazards, DRLs, high beams, low beams, HVAC, wipers, radio, and camera/ADAS
    warnings. Stop immediately on a shift-interlock or safety-module anomaly.
15. **Evaluate the LEDs.** Install or enable the intended LED low beams only
    under the agreed experimental order. Verify both lamp operation and IPC
    warning state with lights off/on and after a key cycle. Record fresh BCM
    and IPC DTCs before any clear.
16. **Handle failure conservatively.** If the van will not shift, preserve
    state and DTCs before battery disconnection when safe. Do not force the
    shifter or assume Neutral. If configuration consistency is suspect,
    perform the supported restore-and-realign recovery while voltage and
    communications are stable; otherwise stop for diagnosis.
17. **Close the campaign only after recovery/success.** Save the final raw
    evidence under `tmp/vehicle_configuration/`, return physical routing and
    PCAN/service state deliberately, update the topology record, and only then
    end the AlfaOBD campaign inhibit.

## Unresolved questions

1. Will the labeled option eliminate only low-beam load monitoring, or also
   change DRL/high-beam diagnostics or output behavior?
2. Is offset `0x8F`, bit `0x40`, the sole fresh before/after difference once
   metadata is excluded?
3. Does AlfaOBD 2.4.4.0 automatically begin alignment after the labeled
   one-option write, or return to a separate confirmation screen?
4. Which exact adapter prompts and participating modules appear during a full
   2022+ ProMaster alignment?
5. Can all required B-CAN participants be reached by the OBDLink MX+ internal
   routing, or will Alfa request an external blue route?
6. Does a supported restore of the saved current record require immediate
   alignment before a key cycle?
7. What exact fault caused the prior no-shift episode, and was it a BTSI, ESM,
   TCM, ABS/EPB, voltage, or configuration-consistency event?
8. Is an owner-accessible manual Park release available for this exact
   electronic shifter/948TE installation? None has yet been verified in the
   local service corpus.
9. If factory-as-built certainty remains a requirement, what wiTECH/RVC access
   will supply the authoritative VIN configuration?

Do not answer these by adding extra writes to the next campaign. The one
supported LED change, its exact before/after record, the required alignment,
and the post-operation inventory should answer only the questions naturally
exposed by that procedure.
