# Lead-vehicle range and relative speed: proposed research, no execution

No request in this plan has been sent. No helper request list, transmit permit,
parked gate, session gate or dashboard metric is changed. The owner's task
classifies moving-vehicle diagnostics as actuation and requires separate
authorization. Approval of offline cruise-summary compute is unrelated.

## What is known

The owner-referenced `0x5A0` display gives state, set speed, gap bars and a
vehicle icon. The saved-bus search has not found lead range or relative speed;
that is an evidence limit, not proof that no possible bus encoding exists.
Diagnostic radar reads are the remaining candidate route, **not a verified
range API**. Neither a gap bar nor a vehicle icon yields meters or seconds.

The exact ECU-scoped evidence is in [did_map.md](did_map.md) and the
[September 22 assessment](2026-09-22_acc_and_dashboard_mapping_outlook.md).
The matched Alfa profile lacks a lead-range/relative-speed label and has known
mis-mappings. Generic Bosch sensor capabilities do not establish DIDs for this
MRR1evo14F software. The current OEM manual link was unavailable during the
September 29 web check; no new OEM diagnostic definition was recovered.

## Fixed candidates and proposed requests

All radar requests use module key `radar_acc`: verified normal-fixed 29-bit
TX `18DA2AF1`, RX `18DAF12A`, C-CAN pins 6/14 resolved from its USB role.
These are application payloads; the reviewed transport owns ISO-TP framing.

| Priority | Exact requests | Evidence and question |
|---|---|---|
| First | `22 10 2A`, `22 19 21` | Nine data bytes each in the historical parked sweep; `102A` was all zero and `1921` began `08 20`. Test whether either changes with independently referenced targets. No range scale or field layout is known. |
| Context | `22 01 03`, `22 08 51`, `22 08 58`, `22 08 63`, `22 08 72`, `22 20 13`, `22 29 2E`, `22 08 57` | Status/enable/sentinel candidates. Determine exact reply shapes and whether an apparent target measurement is invalid or unavailable. |
| Lower priority | `22 08 61`, `22 08 62` | Four-byte auxiliary angle/spare candidates, not established range. Do not fit distance merely because a field varies. |
| Deferred | PCM `22 08 91` (TX `18DA10F1`, RX `18DAF110`) | Vendor label is target cruise speed, not range. Passive set speed is already known, so omit this from the proposed moving experiment. |

## Parked first

1. Review an exact bounded request plan and current role/helper/recorder/inhibit
   status. Park, ignition on, engine off, vehicle stationary, no active routine
   or competing diagnostic client. Use the maintained scoped C-CAN owner; do
   not run a legacy PCAN recipe or manually arm a remembered `canN`.
2. One fixed pass over the 12 radar DIDs above: at most **one request per second
   in aggregate**, one outstanding request, 0.75 s response deadline, no retries.
   Total acquisition at most 30 s. Use no session change or TesterPresent.
   A negative/session-required response is a result, not permission to add a
   preamble. Prove default/no-session-change support again after a clean ignition
   cycle; the historical sweep's support may depend on inherited session state.
3. Validate exact ECU ID, `62` service and DID echo, lengths, sequence and
   completion. The nine-byte candidates need segmented replies: propose a
   narrowly permitted single `30 01 00 00 00 00 00 00` FlowControl only after the
   matching bounded first frame, as in the existing radar-alignment transport.
   Refuse a reply requiring additional blocks or an unexpected length. Even
   FlowControl is transmission and belongs in the authorized payload review.
4. If these reads work, repeat **only 102A/1921**, alternating at one aggregate
   request/second, for at most 60 s per parked condition. Where safely possible,
   use a stationary vehicle at measured bumper-to-bumper distances and a clear
   scene as counterexample. Nobody stands in a travel path. A stationary radar
   may suppress objects; zero data would be inconclusive, not “zero distance.”
5. Save raw request/reply timestamps and conditions under `tmp/radar/`, compare
   controller/error/drop counters before and after, close sockets, restore and
   verify exact listen-only state. Failed restoration latches the existing
   inhibit; do not clear it automatically.

Current commands below **only print plans**; neither includes `--execute`:

```bash
cd /home/pi/dev/obd-things
python3 tools/uds_send.py radar_acc 22 10 2A
python3 tools/uds_send.py radar_acc 22 19 21
```

## Moving experiment, only after a separate approval and parked results

Prepare a reviewed, temporary fixed profile inside the existing qualified
owner, with explicit experiment enablement and a **10-minute maximum**. Do not
modify that owner now. Start with **102A and 1921 only**, alternating, at most
**one additional request/second total** (each DID at most 0.5 Hz), at most one
outstanding transaction and one bounded FlowControl per permitted segmented
reply. Include existing polling and flow frames in the aggregate load review;
do not weaken current per-send RPM, identity, inhibit or ownership gates.

Replay malformed, late, duplicate, mismatched, oversized and out-of-order
responses offline. Prove timeout/stop/parent-death cleanup, per-send permit
consumption, no concurrent radar reply attribution with `0845`, no helper
heartbeat masking, and preserved PCM/recorder timing before deployment.

Begin with a quiet normal-driving segment and ordinary traffic encounters,
with the passenger handling notes/stop controls. Record icon on/off, target
changes, stable following and opening/closing gaps. Do not tailgate, provoke
FCW or create a sudden-stop scenario. Stop the optional experiment on any new
ACC/FCW fault or behavior change, diagnostic collision, failed freshness gate,
timeout/malformed reply, route loss, rising error/drop counters, or host stall.
Driver braking/cancellation always takes priority; a computer logger is not a
safety control. Restoration failure uses the existing fail-closed inhibit path.

Risks include the radar changing its operating behavior during diagnostics,
session assumptions, reply confusion with the van's own F1 client, additional
ECU load, late responses consuming the polling budget, adapter/host stalls, and
mistaking an object-list slot or validity sentinel for the selected lead car.
These risks are why the parked check and narrow abortable profile precede a
moving run; successful reads alone do not prove harmlessness or semantic scale.

## Evidence needed before displaying a distance

Freeze any candidate formula and test a separate complete drive/condition set.
Use an independent distance reference (for example measured stationary spacing
if the radar reports it, or synchronized independently calibrated ranging) and
independent relative-speed reference. Visual estimates/time headway alone are
not an absolute metric calibration. Test target absent/present, target switching,
multiple distances, opening and closing velocity, sign, units and sentinel
handling. A promising unknown word remains Tier 1; no distance/closing-speed
dashboard value or warning is authorized by this plan.

## Authorization requested

First authorize only the parked 12-DID support pass and its bounded conditional
FlowControl, with the specified conditions and limits. After reviewing those
actual results, decide whether to authorize implementation and commissioning of
the temporary 102A/1921 moving profile above. Moving reads, extra context DIDs,
session control, TesterPresent, SecurityAccess, routines, calibration and writes
are not implicitly authorized. Work stops at this proposal until the owner
answers; there is no executable moving command to run today.
