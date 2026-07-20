# TPMS intermittent no-signal evidence — service handoff

**Vehicle:** 2022 Ram ProMaster 2500  
**Prepared:** July 19, 2026  
**Customer/VIN:** omitted from this copy; available from the owner

## Requested service outcome

Please test the **physical rear-left TPMS sensor, ID `7004C287`**, for an intermittent transmitter
or sensor failure and replace it if the direct TPMS-tool test confirms the fault. This is the only
sensor channel that has lost data in the vehicle logs.

The logging evidence makes `7004C287` the evidence-backed replacement target. A shop TPMS
trigger/analyzer is still useful because vehicle-side diagnostics cannot, by themselves,
distinguish a sensor that stopped transmitting from the RF Hub failing to receive that one sensor.
The isolated, repeatable one-sensor behavior makes the sensor/transmitter the leading cause.

## Verified current wheel and sensor map

The physical locations were established on July 7, 2026 by lowering each tire about 5 psi, one at a
time, while reading all four RF Hub pressure channels, then re-inflating the tires in reverse order.
Each pressure channel followed only the corresponding physical tire in both directions.

| physical wheel | sensor ID | RFH pressure channel | result in 6,891 logged samples |
|---|---|---|---|
| Front left | `11825BA9` | slot 1 / DID `31D0` | no invalid readings |
| Front right | `7004E049` | slot 2 / DID `31D1` | no invalid readings |
| Rear right | `700497DF` | slot 3 / DID `31D2` | no invalid readings |
| **Rear left** | **`7004C287`** | **slot 4 / DID `31D3`** | **FAULTY DROPOUT CHANNEL: 708 invalid readings, all with active C1503-31** |

This map is valid until the wheels are moved or rotated. Please identify the service target by both
its **physical rear-left location** and **ID `7004C287`**. Historical vehicle position records once
showed the two rear positions crossed, so the ID is the safer identifier.

## Evidence of the intermittent fault

The RF Hub was sampled approximately every 10 seconds during monitored vehicle operation from
July 7 through July 19. The fault was first captured on July 16 and recurred through July 18.

- All **708** invalid pressure samples were on rear-left channel `31D3`, paired with sensor ID
  `7004C287`.
- Every one of those 708 samples occurred on the same poll as raw DTC `550331=8F`, decoded as
  **C1503-31 — tire-pressure sensor rear left, no signal, active/warning requested**.
- The front-left, front-right, and rear-right channels had **zero** invalid samples.
- There were **zero mismatches** between the rear-left invalid value and active C1503-31.
- The sensor recovered several times and later failed again. Two measured relapse intervals were
  20 minutes 26 seconds and 20 minutes 36 seconds after recovery, closely matching the OEM monitor's
  cumulative 20-minutes-above-15-mph no-message threshold.
- The final logged sample on July 19 at 00:06:53 had all four pressures valid and C1503 stored but
  non-active. This is an intermittent fault; a passing single reading does not contradict it.

Representative consecutive log rows:

| local time (MDT) | FL psi | FR psi | RR psi | RL result | RF Hub result |
|---|---:|---:|---:|---|---|
| Jul 16 22:31:37 | 65.7 | 64.5 | 86.8 | 83.2 psi | no active C1503 |
| **Jul 16 22:31:47** | 65.7 | 64.5 | 86.8 | **invalid (`FFFF`)** | **C1503-31 active (`8F`)** |
| Jul 17 22:00:19 | 63.7 | 62.5 | 83.6 | invalid (`FFFF`) | C1503-31 active (`8F`) |
| **Jul 17 22:00:29** | 63.7 | 62.5 | 84.4 | **84.4 psi, recovered** | **C1503 non-active (`0E`)** |
| Jul 18 21:18:44 | 66.9 | 65.3 | 87.2 | 88.0 psi | C1503 stored/non-active (`08`) |
| **Jul 18 21:18:54** | 66.9 | 65.3 | 87.2 | **invalid (`FFFF`)** | **C1503-31 active (`8F`)** |
| Jul 18 22:02:14 | 63.7 | 62.5 | 83.6 | invalid (`FFFF`) | C1503-31 active (`8F`) |
| **Jul 18 22:02:24** | 64.1 | 62.5 | 84.0 | **84.0 psi, recovered** | **C1503 non-active (`0E`)** |

`FFFF` is the RF Hub's explicit invalid/no-data pressure value. The logger's older CSV decoder
renders that raw value as an impossible 950.5 psi; it is not an actual tire pressure. A failed
diagnostic request would instead leave the CSV field blank.

## Requested shop checks and documentation

1. Before dismounting, trigger/analyze all four sensors and record each ID, pressure, temperature,
   battery/status result, manufacturer, part number, and protocol when the tool provides them.
2. Pay particular attention to physical rear-left ID `7004C287`. Because the fault is intermittent,
   repeat the trigger test if the first test passes.
3. Retrieve the customer's 2024 Discount Tire work order and record which two sensor brands/SKUs
   and wheel positions were installed. Please check whether `7004C287` is covered by that sale or
   any applicable sensor warranty.
4. If replacing it, photograph or record the removed sensor's manufacturer and part number. Record
   the new sensor ID and correctly relearn/write that new ID to the vehicle.
5. Clear RF Hub DTCs once after the repair, then verify all four pressures and no warning on a road
   test long enough to exercise the 20-minute no-message monitor.

Rewriting the unchanged `7004C287` ID is not a repair for an intermittent no-signal condition. FCA
service guidance treats sensor-ID programming as appropriate when a new sensor is installed or the
stored IDs are absent/mismatched, not as the remedy for a mechanical/no-signal sensor DTC.

## OEM versus aftermarket status

The physical manufacturer of `7004C287` is **not established by the vehicle logs**. The owner reports
that Discount Tire installed two missing sensors in 2024. Three current IDs begin with `7004`, while
`11825BA9` is an outlier, but programmable aftermarket sensors can clone an ID already stored in the
vehicle. The ID pattern therefore cannot prove which sensor body is OEM or aftermarket. The 2024
invoice/SKU and the markings on the removed sensor are the reliable evidence.

## Supporting material

- Detailed vehicle-log analysis: [C1503-31 / slot-4 dropout finding](2026-07-16_c1503_slot4_dropout.md)
- Complete project map and diagnostic history: [TPMS / RF Hub diagnosis](../README.md)
- [FCA warranty/service guidance on TPMS sensor replacement and sensor-ID programming](https://static.nhtsa.gov/odi/tsbs/2020/MC-10187669-9999.pdf)
- [2022 Ram ProMaster owner's manual](https://vehicleinfo.mopar.com/assets/publications/en-us/Ram/2022/ProMaster/P5580858-22_VF_OM_EN_USC_DIGITAL_V3.pdf)

The raw timestamped CSV remains available from the owner if the shop needs the complete record.
