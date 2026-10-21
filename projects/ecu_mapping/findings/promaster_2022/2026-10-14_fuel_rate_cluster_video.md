# Fuel rate broadcast: `0x2ED` checked against the cluster's instant MPG

## Result

The C-CAN `0x2ED` frame carries an instantaneous fuel-rate field, alongside
the verified coolant byte. It is the fuel-consumption source the
[October 12 readiness finding](2026-10-12_fuel_economy_readiness.md) was
missing. No diagnostic request or injector model is needed.

| Item | Value |
|---|---|
| Frame | C-CAN `0x2ED`, DLC 8, 20 Hz, ignition on/running (same frame as coolant byte0) |
| Field | `raw = ((b4 & 0x07) << 13) \| (b5 << 5) \| (b6 >> 3)`: Motorola, DBC start bit 34, 16 bits, unsigned |
| Nominal decode | `raw / 512` L/h |
| Cluster relation | Cluster fuel ≈ `1.13 × raw / 512` L/h: a reset Trip B average fits `k = 1.131` (RMS 0.04 mpg) and the video ratio fit gives 1.128. A fixed +1 L/h is ruled out (see the trip-average check) |
| Status | **Strong candidate.** Identity supported by cluster-display, overrun and idle evidence; absolute accuracy not verified. Telemetry metric `engine.fuel_rate` (candidate, cluster-matched) since 2026-10-20 |

`b4` bits 7–3 and `b6` bits 2–0 were zero in every frame of the three legs
examined, so the field could be wider than 16 bits. The 1/512 L/h binary scale
matches the AlfaOBD catalog label for the PCM petrol-status record (`1942`
u16 fuel consumption at 1/512 L/h). That is supporting context only: the
readiness finding did not establish an installed-variant match for that row.

## Evidence

**Owner reference video.** On 2026-10-14, the owner recorded the cluster's
"Current Trip" page, showing instant mpg and range, for 592 s of highway
driving: 52–107 km/h, ACC engaged for most of it, no stops. The iPhone
`creation_time` is 18:17:28Z (1 s resolution). The drive recorder's
synchronized capture `broker-drive-20261014T180442821084` covers
18:04:43–18:34:12Z. The video's GPS metadata is deliberately not reproduced
here.

- 634 HEVC keyframes (0.935 s apart) were cropped to the display and read by
  template OCR trained on 32 hand-read frames. Four reads were corrected by
  manual review. A random 48-frame visual check had no errors. Three end
  frames are unreadable because the camera moved.
- The readout is promoted as
  [`2026-10-14_cluster_instant_mpg_video.csv`](2026-10-14_cluster_instant_mpg_video.csv):
  631 values from 8.4 to 99.9 mpg. 139 are at the 99.9 display cap.
- The displayed value updates about once a second and lags the CAN field by
  about 1.5 s with visible smoothing. Approach to and departure from the 99.9
  cap is gradual, so moment-to-moment comparisons need a window (3 s used).

**Display reproduction.** The comparison set was 136 steady-display samples,
where the neighbouring readings changed by less than 5% and the display was
below the cap. Using `0x0EE` speed, a 3 s window and a −1.5 s lag:

- least squares gives display-implied L/h = `0.9945 × raw/512 + 1.020`;
- `raw/512` alone predicts mpg 12.8% (first half) and 8.5% (second half)
  above the display;
- frozen holdout: an affine model fitted on the first half (`0.9994`,
  `+1.030` L/h) and applied to the second half had steady mean absolute
  error 1.02 mpg, median +1.3%, and p95 3.6 mpg (n = 31).

The steady samples span 4.7–22.9 L/h but only 1,960–2,570 rpm (p5–p95).
On the video alone, the shape of the gap is unresolved:

- Constant, rpm-proportional and mixed offsets fit equally well (RMS
  0.79–0.81 L/h).
- A pure ratio of 1.13 has the lowest steady MAE (0.84 mpg against 0.97 for
  `+0.495 L/h per 1,000 rpm` and 0.99 for constant `+1.02`).
- The ratio errs −0.6 mpg on the highest-load samples (`0x2ED` median
  22.9 L/h, n = 11, above 2,400 rpm).
- In the first-half/second-half holdout, the ratio was slightly worse than
  the affine model (steady MAE 1.18 against 1.02 mpg).

The trip-average check below settles the shape in favour of the ratio.
Summing 1 Hz instant readings over the window is not a usable total
reference, because the display smooths and caps.

**Speed basis.** `0x0EE` agrees with telemetry's `0x101` speed within
0.05 mph (median), and the owner reports that the app matches GPS while the
cluster speedometer reads about 1 mph higher. The logs are consistent with
that: steady engaged-ACC cruise settles 0.6–0.8 mph below the `0x5A0` set
speed at set speeds of 27–65 mph on three drives (video window, today's
earlier leg, Oct 10). The offset looks fixed rather than proportional. It
cannot explain the fuel gap. If the cluster computes instant mpg from its
higher displayed speed, its mpg would rise, yet it reads lower than
`raw/512` predicts, and the inferred cluster fuel would be about 1% higher.
If it uses true speed, nothing changes. The trip distance does not carry the
bias: Trip B read 10.2 mi, against 10.30 mi integrated from `0x0EE`.

**Trip-average check (owner photos, 2026-10-15 01:53–02:30Z).** The owner
reset Trip B with the engine idling and drove 10.2 mi of town driving. That
was 36.4 min of running, 15.1 min of it idling, including 7.7 min before
moving and 3 min parked at the end. Photos of the trip page were taken
within 2 s of a displayed update, except one. A 1 s clip shows the
14.2 → 14.1 step while idling in Park. Capture:
`broker-drive-20261015T015240015334` c-can chunks 0–3, compute job
`20261015T025835Z-b5c6ffed`, script `scripts/trip_check.py` in the working
directory.

- Trip B travel time counts engine-running time. The 7:20 and 12:18
  readings and the final 36:22 all put the reset at 01:53:10.5Z, and the
  last one ends exactly at the `0x0FC` engine stop (02:29:32.6Z).
- Displayed average mpg, against predictions from `0x2ED` with distance
  from `0x0EE`:

| UTC | Dash | `raw/512` | ×1.131 | +1.02 L/h always | +1.02 × rpm/2060 |
|---|---|---|---|---|---|
| 02:05:30 (not fresh) | 5.0 | 5.62 | 4.97 | 3.84 | 4.66 |
| 02:08:12 | 9.3 | 10.53 | 9.32 | 8.02 | 9.07 |
| 02:08:22 | 9.5 | 10.81 | 9.57 | 8.30 | 9.34 |
| 02:15:03 | 14.6 | 16.53 | 14.63 | 13.63 | 14.64 |
| 02:28:00 (14.2 → 14.1) | 14.15 | 15.93 | 14.10 | 12.83 | 13.99 |
| 02:29:33 (engine off, final) | 13.9 | 15.72 | 13.92 | 12.59 | 13.78 |

- Best fits to the five fresh points: ratio `k = 1.131`, RMS 0.04 mpg (no
  distance correction); rpm-proportional 0.86 L/h at 2,060 rpm, RMS 0.095;
  constant 0.48 L/h, RMS 0.17. A constant offset large enough for the
  highway video (about 1 L/h) under-reads this trip by 1.3 mpg.
- The idle-heavy start, where an offset model and a ratio diverge most,
  follows the ratio.
- Under the ratio, the 14.15 crossing falls about 30 s before the clip's
  recorded start (02:28:00Z, 1 s resolution). The cause could be the
  display's update cadence or the clip's timing; the cadence is unknown.

Two independent references now agree that the cluster's fuel is about 13%
above `raw/512`, at highway load and at idle. Either the true scale is
nearer `raw/453` than `raw/512`, or the cluster over-reads by a constant
percentage. Only measured fuel can tell which.

**Blind screen (independent of the video).** A worker screened 59,384
packed geometries on 80 standard C-CAN IDs for rate- and counter-like fuel
behaviour, without the video:

- `0x2ED` ranked first, with r = 0.984 against an rpm × current-torque proxy.
- It was exactly zero over overrun: 80% of the overrun-eligible mask
  overall, 95% after trimming 1 s from the interval edges, and all of the
  10.3 s remaining after trimming 2 s. One complete 9.4 s interval at
  1,771 rpm and 84 km/h was zero throughout.
- No credible accumulating fuel counter survived.

**Idle (separate legs, frozen decode).**

- Pre-video portion of the same drive, coolant 37–49 °C: 52 s of idle at
  about 779 rpm, 2.0–2.6 L/h, never zero.
- Drive `broker-drive-20261010T232609499013` (chunks 0–1, 180 s of idle at
  about 754 rpm): median 1.78 L/h below 60 °C, 1.44 L/h at 60–80 °C, and
  1.78 L/h above 80 °C.
- The trip totals from `raw/512` were 14.6 mpg (5.9 km cold start, today) and
  16.1 mpg (10.0 km of stop-and-go, Oct 10). Both are plausible for this van
  but are not reference-checked.

**Rejected alternative.** `0x0FC` `((b2 & 0x1F) << 3) | (b3 >> 5)` (8 bits,
DBC start 20) correlated with load and was zero on overrun, but it was also
zero at idle on both legs (99.4% of Oct 10 idle samples). It behaves like
accelerator or torque demand, not fuel flow. Treat it as an unrelated
candidate. `0x1F2` is gated by the ACC state, and `0x41B` stays nonzero
on overrun.

A supervised screen of every 8–16-bit field against the video ranked
`0x5AE` `((b5 & 3) << 6) | (b6 >> 2)` (event-driven, about 5.5 Hz) marginally
first: r 0.752, versus 0.733 for `0x2ED` in the same 1 s window. It is a
smoothed signed quantity, not fuel:

- it sits at 115–119 at idle and falls as low as 63;
- during at least 2 s of `0x2ED` fuel cut its median is 119 (p5–p95 94–143)
  instead of a floor;
- it correlates 0.87–0.93 with a 2–5 s average of the `0x2ED` rate.

Its slight edge over `0x2ED` comes from smoothing similar to the display's.
It is plausibly a load or power indicator and remains unmapped.

## Reproduce

Working files are under `tmp/vehicle_data/fuel-economy/mpg-video-20261014/`
(gitignored). `scripts/` holds the keyframe band search and OCR, the
alignment helpers, the screens, the holdout and the idle check; `astra/`
holds the blind screen; `data/` holds the per-ID arrays.

- C-CAN arrays come from the new offline `candump-id-arrays` compute task
  (`tools/candump_arrays.py`).
- Jobs: `20261014T190017Z-f23f1948` (video window, 18:15–18:29Z),
  `20261014T192530Z-f1f2b574` (today chunk 0) and
  `20261014T192533Z-478c3c4a` (Oct 10 chunks 0–1).
- Video SHA-256:
  `778a434eefb0c37a1bf421bf7525f1ad4d335fc5c4fee2c0473dd807cd4eee2d`. It
  stays in the owner's shared folder and is not committed.

## Open items

1. **Absolute accuracy.** Only a full-tank to full-tank fill-up interval
   measures real fuel, so only it can choose between `raw/512` and the
   cluster's `1.13 × raw/512`. The cluster's trip-average page uses the
   same cluster fuel model as its instant mpg, so it cannot settle this. The
   Pi keeps a raw capture of each drive, so past fill-ups could be checked
   if no drive in between went unrecorded. The owner asked for no required
   fill-up logging, so this stays opportunistic.
2. **Cluster correction shape.** Resolved by the trip-average check as a
   ratio of about 1.13. The remaining tension is the −0.6 mpg ratio error
   on the 11 highest-load highway samples.
3. **Telemetry.** Done 2026-10-20: `engine.fuel_rate` (candidate, gal/h,
   `1.131 × raw/512`) plus estimated trip and 7/30-day economy, computed as
   mean speed over mean fuel rate across the historian's engine-running
   samples (five-second cadence). Resampling the four full-rate captures at
   five seconds put trip mpg within about −5% to +5% (p5–p95) of the
   full-rate integral on 10–38 min trips. See the vehicle-data README.
4. The automatic injector-pulse-width collection designed in the readiness
   finding is no longer needed for a fuel source. Keep it only if the
   broadcast scale proves unusable.
