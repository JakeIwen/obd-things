# Promaster ACC Radar Calibration — PCAN / SocketCAN Handoff

> You are Claude running on a Raspberry Pi with a **PEAK PCAN-USB** interface wired to a
> 2022 Ram Promaster's OBD-II port. Your job: stand up reliable raw CAN + ISO-TP UDS comms
> to the radar module, verify prior findings, and (eventually, with consent + a mirror)
> run the radar alignment routine. This file is self-contained — you do NOT need the prior
> ELM327/Bluetooth conversation. Earlier ELM scripts in adjacent files are obsolete here.

---

## 1. Goal
The van has an active **DTC C1418-78 = vertical misalignment of the ACC radar** (Bosch DASM,
the "Adaptive Cruise Control Unit"). The forward-collision/ACC warning is on. We need the
radar **alignment routine** to run successfully so the fault clears. A prior investigation
(over ELM327) strongly indicates the diagnostic tool (AlfaOBD) calls the **wrong routine ID**
for this model year. Your platform (PCAN + SocketCAN) is the clean way to verify and act.

Owner constraints: an **SGW bypass is installed**, so diagnostic writes reach the modules.
Vehicle VIN `3C6LRVDG4NE134328` (MY2022 confirmed).

---

## 2. Hardware / physical connection
- **PEAK PCAN-USB** adapter → **OBD-II-to-DB9 cable** → van OBD-II port.
- The DB9 should follow **CiA-303 / PEAK pinout: pin 7 = CAN-H, pin 2 = CAN-L, pin 3 = GND**.
  The OBD-to-DB9 cable must map **OBD pin 6 → CAN-H (DB9 pin7)** and **OBD pin 14 → CAN-L
  (DB9 pin2)**. If you see no traffic, a wrong-pinout cable is the #1 suspect.
- Bus = the OBD diagnostic **HS-CAN, 11-bit-physical + 29-bit-diagnostic, 500 kbit/s**.
- **Ignition must be ON (engine running ideal, ~14 V)** or the bus is asleep and you'll see
  nothing / bus-off.

---

## 3. Bring up SocketCAN (PEAK uses the in-kernel `peak_usb` driver)
```bash
# PEAK PCAN-USB is supported by the mainline kernel; usually no extra install needed.
dmesg | grep -i peak              # confirm peak_usb bound, "can0" created
ip link show can0                 # interface should exist (DOWN at first)

sudo ip link set can0 up type can bitrate 500000
ip -details -statistics link show can0     # confirm state ERROR-ACTIVE, bitrate 500000

# sanity: with the van running you should see a flood of frames
candump can0
```
If `can0` doesn't appear: `sudo modprobe peak_usb`; check `lsusb` for the PEAK device.
If it goes **bus-off** / error counters climb: wrong bitrate, wrong cable pinout, or ignition off.

For UDS you need the ISO-TP kernel module:
```bash
sudo modprobe can-isotp
lsmod | grep isotp                # confirm loaded
```

---

## 4. DASM addressing (verified earlier by direct probing)
- **29-bit, ISO-TP, 500k.** Module physical address **0x2A**; tester **0xF1**.
- **TX (tester→DASM) CAN ID = `0x18DA2AF1`**
- **RX (DASM→tester) CAN ID = `0x18DAF12A`**
- This is ISO-TP **Normal fixed addressing, 29-bit**.

Quick manual ISO-TP smoke test from the shell (`can-utils`):
```bash
# terminal A — listen as the tester
isotprecv -s 18DA2AF1 -d 18DAF12A -x can0
# terminal B — send "ReadDataByIdentifier F1A5" (extended diag session first if needed)
echo "22 F1 A5" | isotpsend -s 18DA2AF1 -d 18DAF12A can0
# expected reply on A: 62 F1 A5 00 39 50 16 20
```
(Flags: `-s` = source/TX id, `-d` = destination/RX id. Some builds want them swapped on
recv — if no reply, try swapping -s/-d on `isotprecv`.)

---

## 5. Known-good baseline (must reproduce before trusting anything)
Confirmed working over ELM327 earlier; these are the values to expect on `can0` too:
- DiagnosticSessionControl extended: TX `10 03` → RX `50 03 00 32 01 F4`
- ReadDataByID: TX `22 F1A5` → RX `62 F1A5 00 39 50 16 20`
- Module IDs: `22 F195`(SW)→`62F195 0400`; `22 F193`(HW)→`62F193 01`;
  `22 F18C`(serial)→ ASCII `TD5730292062400`
- DTCs: **only ReadDTCInformation sub-function 0x02 is supported** (`19 01` and `19 0A`
  return `7F 19 12 subFunctionNotSupported`). TX `19 02 FF` returns 8 DTCs:
  - **C1418-78  status 0x8F** (testFailed+confirmed+pending+warningLamp) → the ONE active fault
  - C1425-45, C1422-66, C1422-49, C1429-68, C1429-66, C1420-25, C1420-66 → status 0x40 (dormant)
  - (FCA 3-byte DTC encoding: e.g. C1418-78 = bytes `54 18 78`, status byte follows.)

---

## 6. ★ THE KEY FINDING — routine 0x0250 is WRONG, 0x0251 is the suspect ★
**AlfaOBD's "Active alignment: radar calibration" fails** with
`The ECU has detected that the request contains parameter(s) with value(s) outside allowable
range` = UDS **NRC 0x31 (requestOutOfRange)**.

Experiments and results:
1. AlfaOBD sends RoutineControl **startRoutine** `31 01 0250 01` (routine 0x0250, option 0x01).
   ECU replies **`7F 31 31`**. Rejected in ~50 ms, *before* any measurement — so it is NOT a
   mirror/precondition/physical-alignment failure; the request itself is refused.
2. We sent **requestRoutineResults** `31 03 0250` (read-only, no params besides the routine ID).
   ECU also replied **`7F 31 31`**. Because 31 03 carries no parameter that could be
   "out of range," `requestOutOfRange` here means **routine 0x0250 is not implemented on this
   ECU variant.**
3. **Read-only routine-ID scan** with `31 03 <rid>` over 0x0200–0x031F and 0xFF00–0xFF03 found
   **exactly one recognized routine: `0x0251` → `7F 31 24` (requestSequenceError)**.
   `0x24` = "routine exists but was not started yet" → **routine 0x0251 EXISTS and is startable.**

**Conclusion:** AlfaOBD is **off-by-one** for the MY2022 250-series Promaster DASM. The real
radar-alignment routine is almost certainly **`0x0251`**, not `0x0250`.

**Rigor caveat:** the scan silently skipped any empty responses (link flapped on the old
Bluetooth setup). A drop can only cause **false negatives** (a real routine read as absent),
never a false positive — so `0x0251` is trustworthy, but the scan was NOT certified complete.
**First task on this hardware: re-run a hardened scan** (flag + retry empty/timeout responses,
print a clean/dirty verdict) and reconfirm `0x0251`. Raw SocketCAN+isotp makes this reliable.

---

## 7. Mirror requirement (for the routine to COMPLETE, not to start)
AlfaOBD's on-screen instructions for the alignment: *"Position the mirror at a distance of
**120 cm (±5 cm)** from the sensor. Put mirror in **position 1** (forward position) and start
measurement."* This Bosch unit does a **static alignment by measuring its own reflection off a
flat mirror.** So even once `0x0251` starts, it needs a flat glass mirror squared to the
vehicle centerline at 120 cm to actually converge. The owner does NOT yet have the mirror.

---

## 8. Suggested work order on the Pi
1. Bring up `can0` @ 500k; `candump` to confirm live bus (van running).
2. Manual `isotpsend/recv` smoke test of `22 F1A5` → expect `62F1A5 0039501620`.
3. Build a Python tool with **`python-can` + `isotp`** (or `udsoncan`) using:
   `isotp.Address(isotp.AddressingMode.Normal_29bits, txid=0x18DA2AF1, rxid=0x18DAF12A)`.
4. Reproduce the baseline (§5): session `10 03`, read `F1A5`, read DTCs `19 02 FF`.
5. **Hardened routine scan** (§6 caveat): `31 03` across at least 0x0200–0x03FF (extend
   further if time allows), retrying empties, logging a clean/dirty verdict. Reconfirm 0x0251.
6. Verify 0x0251 is the alignment routine WITHOUT committing: e.g. `31 03 0251` should give
   `7F 31 24`; optionally inspect related DIDs (vertical/horizontal deviation angles) the tool
   exposes. **Do NOT startRoutine 0x0251 until step 8.**
7. Draft an AlfaOBD bug report: "MY2022 Promaster Bosch DASM (addr 0x2A): radar-cal routine
   `0x0250`→7F3131 (unsupported); `0x0251`→7F3124. Routine ID should be 0x0251 for this MY."
8. **(ACTUATION — explicit owner consent required, and ideally the 120 cm mirror staged):**
   attempt `31 01 0251 <param>` (try option `01` = "position 1" first, mirroring AlfaOBD's
   0250 call). Starting it confirms the ID (expect a conditions/measurement response, NOT
   `0x31`) but can invalidate the current alignment state — so do it deliberately, once,
   with the mirror, not as a probe.

---

## 9. Safety
This is a **forward-collision radar**. A mis-calibrated unit causes phantom braking and missed
detection. Only run actuation/calibration routines with the owner's explicit OK; for a real
calibration use the correct mirror geometry on a level surface, normal tire pressures, normal
load, engine running. Reads and `31 03` (requestRoutineResults) are safe; `31 01` (startRoutine)
is actuation.

---

## 10. UDS quick reference (for decoding replies)
- Positive response SID = request SID + 0x40 (e.g. 0x31→0x71, 0x22→0x62, 0x19→0x59, 0x10→0x50).
- Negative response = `7F <SID> <NRC>`. NRCs seen / relevant:
  `0x31` requestOutOfRange · `0x24` requestSequenceError · `0x22` conditionsNotCorrect ·
  `0x33` securityAccessDenied · `0x12` subFunctionNotSupported · `0x13` len/format ·
  `0x78` responsePending (wait for follow-up) · `0x7E` subFuncNotSupportedInActiveSession.
- No SecurityAccess (`27`) was needed to reach the routine layer in prior tests; if `0x0251`
  start returns `0x33`, a seed/key unlock (proprietary FCA algo) would be required — flag it,
  don't brute force.
```

