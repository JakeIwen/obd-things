# Early-warning rationale

Verbatim comment blocks from `projects/vehicle_data/early_warning.py`.

## running-observation-gap

```python
# 2.5 x the p90 8 s observation spacing.  Fresh RPM observations are 10-12 s
# apart 1-46 times per trip (trips 50-57); a 10 s limit restarted the 10 s
# startup grace on each, held the coolant 300 s gate on 0-11 % of ticks in
# trips 50-55 and made the oil-critical rule flap.  A real stop (257 s gap)
# still breaks the running interval.
```

## persistence-observation-spacing

```python
# The live historian stores a fresh observation about every 6 s, not 5 s
# (trips 43-57: p50 6.0 s, p90 8.0 s between distinct observations).  A
# run of N consecutive readings must fit its window, so tier-0 and tier-1
# windows allow N x 8 s; with N x 5 s the transmission-hot and
# charging-failure rules (12 obs / 60 s) could never confirm, coolant-hot
# (6 obs / 30 s) rarely, and no tier-1 run (12/60, 24/120, 60/300) or the
# pair-asymmetry run (60/300) was reachable at all.
```

## coolant-slow-motion-floor

```python
        # At stationary/urban idle the fan cycles the coolant between 203 and
        # 221 F.  The deviation bar (median 190.4-192.2 + 13.5-24) sits inside
        # that band and moves with whichever hot idles are in the baseline:
        # with the 09-17 bar of 203.9 trip 52 would have held a 133-reading /
        # 873 s "deviation" through a normal fan cycle.  No rollup bucket in
        # all 57 trips reaches 222 F (observed max 221; spec provenance:
        # thermostat fully open near 220), so slow-motion readings count only
        # from 222 F.  Road and highway keep the plain deviation.
```

## tire-warm-recovery

```python
        # Tires gain 3.2-4.4 psi (5.9-6.4 %) during a drive (trips 43-57), so
        # a front that warned at 49 psi cold reads 52-53 psi warm.  Clearing
        # against the cold limit sent a false "normal" push about 10 minutes
        # into every drive, and that recovery push ended the group cooldown,
        # so the next cold morning pushed again.  Warm readings clear only at
        # 56.2 / 75.6 psi; a cold reading still clears at 52 / 70.
```

## tire-pair-recovery

```python
        # Episode 642 (trip 59) stayed open 17 min while both rear tires read
        # the same: 60 clear readings, then the default 600 s recovery.  Only
        # fresh, moving readings count now (PAIR_* below), so a shorter hold
        # is safe: two rolling TPMS transmission periods (~64 s each).
        # Re-opening still needs the full 60-reading run, so it cannot flicker.
```

## tire-pair-transmission-gates

```python
# Pair asymmetry counts only readings that both sensors sent recently.  The RF
# hub repeats each wheel's last received value.  A TPMS sensor sends about once
# every 64 s while rolling: across trips 43-62, the gaps between value changes
# peak at 64 s and its multiples.  While the van stands, a sensor sends rarely.
# In trip 59 (episode 642, 2026-09-24) the van idled for about 23 min.  RL
# reported its cooling tire (79.6 -> 76.0 psi) while RR repeated 79.6.  That
# made a false 4.4 psi gap, and the warning opened as the van pulled away.
#   * Stationary gate: a reading counts only when a fresh vehicle.speed of at
#     least PAIR_MOVING_SPEED_MPH is at most PAIR_SPEED_MAX_AGE_SECONDS older
#     than it.  Across trips 55-62 that speed-to-tire lag is p99 10 s and at
#     most 17 s.
#   * Fresh-transmission gate: after the van starts moving, a wheel counts
#     once its value has changed since then, or once the van has moved without
#     a stop for PAIR_ROLLING_TRUST_SECONDS.  By then each sensor has sent at
#     least twice.  A steady warm tire can hold one value for up to 20 min on
#     the highway (trips 43-62), so a strict "changed" test would blind the
#     rule for most of a drive.
#   * Clearing needs PAIR_CLEAR_OBSERVATIONS fresh, moving readings (8 x ~8 s
#     is about one rolling transmission period), not 60.
```

## tire-pair-cached-readings

```python
        # The RF hub reports each wheel's last reading from before the trip
        # until that sensor transmits again; a cached wheel compared with a
        # live one fabricates an asymmetry (trips 34/36).
```

## tire-pair-latest-reading

```python
            # Never open on a reading pair that is itself inside the limit.
            # In trip 59 the run's newest reading paired RL with RR's previous,
            # stale value (4.4 psi), while the two latest readings were 0.4
            # psi apart.
```
