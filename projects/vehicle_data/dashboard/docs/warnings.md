# How early warnings work

The dashboard's warnings are evidence to look at, not a diagnosis or a health score. They are graded
in tiers, so the top bar only ever shows something you can act on.

## What you see on the dashboard

- **Top bar**: when a warning is confirmed, or an item is critical, the top bar turns amber or red.
  It shows the most serious item in short form, such as `Coolant 232 °F`, with `+1` when there are
  more. Tap it to open that item's details. The bar never grows or moves the gauges.
- **Numbers**: a gauge's number turns amber or red only while a warning for it is confirmed, or while
  a live value is past a red limit. An item that is only being watched does not colour anything.
- **Health tab**: the badge counts open items, including those still being watched. It is red when
  any item is critical.
- **Health → Early warning**: one line per open item (see below), with `Details` and `Ask Codex`.
  `Event history` browses every saved event, and `Add early warning` asks Codex to draft a rule of
  your own.
- **Health → System notes**: adapter, bus and data-quality items. They are listed for completeness,
  never counted as warnings and never notify.

## Tiers

| tier | what it means | where it shows | phone notification |
|---|---|---|---|
| **Critical** | an absolute limit is crossed far enough to act now: oil pressure below about 12 psi with the engine running, coolant at 240 °F, parked battery under 11.5 V, or a tire under 45 psi (front) / 60 psi (rear) | red top bar on every view, red number, Health list | immediately, repeated every 5 min while active; never delayed |
| **Warning** | an absolute limit (see below), or a sustained deviation from this van's own history in the same driving conditions, held for minutes | amber top bar, amber number, Health list | once when confirmed, grouped by component (at most one per component every 6 h). Absolute-limit warnings go out at once. History-relative warnings raised between 22:00 and 07:30 while parked wait until 07:30; raised during a drive they go out at once. At most 3 of them go out per drive. |
| **Watch** | older rule set only (still deployed): an item that has started but not yet lasted long enough to count. The redesigned rules and owner-added rules show nothing until a warning is confirmed. | Health list only | never |
| **Notice** | a slow drift across trips (a tire losing pressure week over week, idle coolant creeping up, oil pressure falling, the battery accepting less charge, resting voltage low or falling) | Health list | once per notice, at most one per component a week, held until 07:30 during quiet hours |
| **System** | adapter, bus or data-quality problems (an adapter missing, a sample rejected by a plausibility gate) | System notes on the Health view, System view | never; adapter, bus and data-quality items are listed in System notes only |

## What a warning line says

Every warning is written the same way: the number, the comparison, how long, and what to do.

> RR tire 74 psi, 4 psi under its usual 78 for 5 min. Check pressure at the next stop.

- The comparison is against this van's usual value in the same conditions when there is one. For an
  absolute rule it is the limit, and for a rule you added it is your threshold.
- `over` and `under` follow the two numbers on the line, so the line always adds up. When both round
  to the same number it reads `at its usual 78`.
- Durations carry their units: `5 min`, `1 h 05 min`, and days from two days on (`2 d 8 h`).
- A slow-drift notice is worded differently, because it compares cold starts or whole drives rather
  than the newest reading with a usual value. It says what was found, and the card's `since` time
  says when:

  > RR cold pressure 2.9 psi a week below RL's over 4 cold starts. Check the RR tire for a slow leak.
- Until the condition has lasted long enough to count, the line says `just now` instead of a
  duration.
- `Unconfirmed · engine off since 4:25 pm` means the item was open when the engine stopped and
  cannot be re-checked until the engine runs again.
- The evidence behind a line is one tap away in the event dialog and is kept in the saved event
  history. It covers the baseline, the spread, how many samples, and the driving conditions used for
  the comparison.

## History-relative rules

For temperatures, oil pressure, voltage and tires, the Pi learns what is normal for this van over the
last 30 days, excluding the current trip. It compares like with like: engine running or not;
stationary, urban, road or highway; rpm band; and warm or cold.

- A deviation counts only when it is larger than both a minimum effect (0.8 V, 12 °F, 5 psi of oil
  pressure or 3 psi of tire pressure) and 4.5 robust standard deviations. It must also hold for
  consecutive readings: twelve for oil pressure (about 1–1.5 min), 24 for coolant, transmission and
  charging voltage (about 2.5–3 min), and 60 for tires (about 6–8 min). Nothing shows while it is
  still counting.
- The charging comparison only uses readings taken while the alternator is working hard
  (command at 85 % or more), so a normal charge reduction by the engine computer is not a fault.
- Tires compare cold to cold, warming to warming and warm to warm (the first 3 minutes of driving
  after 4 hours parked, 3–20 minutes, then longer), never by speed.
- Coolant deviations while stopped or in slow traffic only count from 222 °F, above the point where
  the fan normally cycles (203–221 °F). On the road, the plain comparison applies.
- A warning de-escalates when the value returns to within 70 % of its threshold.
- It closes after a recovery window, or after 24 hours parked without being seen again.

## Absolute rules

- Oil pressure below about 12 psi with the engine running (OEM P06DD context): critical.
- Oil pressure below the OEM warm band for the current rpm for three readings, with coolant at
  least 160 °F: warning. The floors are 15 psi at 550–850 rpm, 22 psi at 1,000–3,000 rpm and 55 psi
  above 3,500 rpm; in between, nothing is judged.
- Coolant at or above 230 °F for six readings in a row (about 30–45 s at the live sampling rate):
  warning; at 240 °F: critical. These are owner limits: the
  thermostat is fully open near 220 °F, and the highest value seen in 57 trips is 221 °F.
- Transmission oil at or above 230 °F for twelve readings in a row (about 1–1.5 min at the live
  sampling rate): warning.
- Running voltage under 12.0 V for twelve readings in a row (about 1–1.5 min at the live sampling
  rate) with the alternator command at or above 90 %: warning ("charging may be failing").
- Parked settled voltage under 11.8 V for three readings: warning; under 11.5 V: critical.
  "Settled" means taken at least 30 s after the engine stopped. Key-on and just-stopped readings
  are never judged, and a single low reading while cranking never alerts. The five archived
  11.2–11.8 V parked readings were all cranking or key-on dips.
- A tire below 50 psi (front) or 68 psi (rear) for three readings: warning; below 45 / 60 psi:
  critical. These are cold-tire limits, checked on every reading because a warm tire only reads
  higher. A puncture to 55 psi on a rear tire is flagged critical in about 12 s. For the same
  reason a warm reading does not clear it: once the tires have warmed (a few minutes into a
  drive) it clears only at 56.2 / 75.6 psi; a cold reading clears at 52 / 70 psi. Tires gain
  3–4.5 psi on a drive, so without this a low tire sent a false "normal" message on every drive.
  These limits are also checked while the van is stopped. A value the tire hub repeats is still a
  real, earlier reading, and a tire under its cold limit at any temperature is low. A repeated
  value can therefore delay a low-tire warning, but it cannot cause a false one.
- One wheel more than 3 psi from its axle mate after removing the usual left/right offset, for 60
  readings in a row (about 6–8 min): warning. The top bar shows the axle, such as
  `Rear tires uneven`. Uneven tires are judged only while the van is moving at 5 mph or more.
  - The tire hub shows each wheel's last reading until that sensor sends again. While the van
    stands, the sensors send rarely, so one tire can show a newer value than its mate.
  - After each stop, a tire counts again only once its sensor has sent a new value, or once the
    van has kept moving for 2½ minutes (the sensors send about once a minute while rolling).
  - At the start of a drive, a tire still showing the last value from the previous drive is not
    judged.
  - The two latest readings must themselves be more than 3 psi apart for the warning to start.
  - On 24 September a 23-minute idle raised a false `Rear tires uneven` as the van pulled away.
    These checks now prevent that.
  - A stop does not close an open warning. It clears after eight even readings taken on the move
    (about a minute), and the event closes two minutes later.
- An open critical item stays critical while it briefly cannot be checked (for example a gap in
  the engine-speed readings).

Each absolute warning clears only once the value is back past the limit by a margin (for example
5 °F for coolant, 0.5 V for charging, 2 psi for tires), so a value hovering at the limit does not
flicker.

## Slow-drift notices

These are checked once a day from the one-minute history, never from a single drive:

- **Tire slow leak**: each wheel's cold-start pressure is compared with the other tire on the same
  axle, so weather, altitude or a top-up of all four does not count. A notice needs the wheel to
  fall against its mate by 0.5 psi a week (over two weeks) or 3 psi (last three cold starts against
  the month), and the wheel's own pressure to fall by at least half that. Readings the tire hub
  repeated from the previous drive are skipped.
  - The weekly trend is the middle one of the slopes between every pair of cold starts, so a single
    stray reading does not tilt it.
  - The trend only counts while the newest cold starts agree with it. Against the wheel's usual gap
    to its mate, the latest cold start must be at least 0.5 psi lower and the middle of the last
    three at least 0.25 psi lower. A tire that has come back up is not a leak.
  - An open notice clears when the trend has levelled off, or when the latest cold start and the
    middle of the last three are both back within 0.25 psi of the usual gap.
  - Cold readings on one axle can differ by a few psi from sun or sensor timing, so a notice is a
    prompt to check with a gauge on a cold tire, not proof of a leak.
- **Resting voltage**: the settled engine-off voltage of each parked stop (from 1 minute after
  stopping until 2 minutes before the next start). A notice appears when the week's median is under
  12.2 V, or it falls 0.1 V a day; it clears at 12.3 V and a flat trend.
- **Idle coolant creep** (+5 °F against the previous ten drives), **oil pressure decline** (−3 psi)
  and **charge acceptance** (more than 40 % of driving minutes under 13.0 V for three drives).
- A notice is not closed just because there is too little new data to re-check it; it stays open
  until it can be re-checked, and it is never sent again while it waits.

Owner-added rules (through "Add early warning") are bounded numeric thresholds with persistence and
an optional engine-running gate; they sit in the Warning tier.

## Phone notifications

Notifications are sent through the Pi's ntfy queue. A line under Early warning appears only when a
notification is waiting to be sent or has failed (`Phone alerts: 1 pending`). Only vehicle items you
can act on notify. Adapter, bus and data-quality items, such as a missing CAN adapter or a sample
filter throwing away an implausible reading, never notify; they are listed in System notes.

## Ask Codex

`Ask Codex` explains a warning or drafts a new rule. It runs on the Pi, and appears only on screens
where the advisor is enabled. Conversations are saved on the Pi for the browser that started them,
and closing the dialog does not stop an answer. The access code is read on the Pi (not an OpenAI API
key). A code entered once in a browser is remembered by that browser.

## Current status

The dashboard renders today's evaluator with the vocabulary above:

- history-relative rules sit in the Warning tier (as Watch until confirmed);
- the oil-pressure absolute rule is Critical;
- infrastructure and data-quality items go to System.

The absolute rules, sigma floors, tire phase conditioning, drift notices, grouping and quiet hours
listed here are built and replayed offline, but **not deployed**. Until the owner restarts the
broker with them, the older rule set still decides what is open. Over the last recorded week
(15 drives, 17–24 September), the new rules would have raised no vehicle warnings and sent no phone
notifications. The old rules opened 70 items and sent 17 notifications over the same week.

The dashboard's own limits (the red band values on the gauges) already use the numbers above. That
is why a gauge can turn red before the Pi has confirmed a rule. Watches that never confirmed are
archived as "Unconfirmed · monitoring ended" and are not shown as warnings.
