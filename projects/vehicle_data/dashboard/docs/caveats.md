# What each number on the dashboard really is

This page holds the provenance notes and caveats that used to be printed on the cards. A card
shows one of three things: a value, a dimmed last value with its time, or a dash. Everything else
you might want to know about that value is on this page.

## How to read a tile

- **White number**: a live reading.
- **Dimmed number with a time under it** (for example `4:25 pm`): the last reading taken, kept on
  screen because the value cannot be read right now, usually because the engine is off or the van
  is asleep. Only a few metrics keep a last reading (see "Last readings" below).
- **A dash (`—`)**: the value cannot be read in the current state, and showing an old one could be
  mistaken for a current one.
- **The top bar** states the van's condition once (`Running · 32 min`, `Asleep · last drive
  4:25 pm`, `Ignition on · engine off`). That is why the cards no longer repeat "engine not
  running", "bus asleep" or "not live" under every value. A card says "stale" at most once.
- **The dot at the top left** shows the link to the Pi. Green means live, amber means reconnecting
  and red means the broker is unavailable, in which case the screen shows the last data it had. Tap
  the dot to reload the data from the Pi. This reads only the Pi's cache; it never asks the van for
  anything.
- **Dim** (top bar) darkens the screen for night driving. Red and amber stay as they are.
- **Auto** (top bar) lets the dashboard switch between Drive and Parked by itself. When you tap a tab
  yourself, Auto turns off until the engine next starts or stops.

## What the colours mean

- **Band bar** (the thin bar under a gauge): the lighter grey-green segment is the normal range for
  this van. The marker is where the value sits. It is white inside the normal range, amber in a
  range worth a look, and red past a warning limit. The numbers behind each band are in the
  [warnings page](/docs/warnings.html) and in the sections below.
- **An amber or red number** means one of two things. Either a warning for that metric is confirmed
  (it has lasted long enough to count), or a live value is past a red limit. A value that is merely
  unusual stays white; the bar shows where it is.
- **Top bar turns amber or red**: the same two triggers. It shows the most serious item, such as
  `Coolant 232 °F`, plus `+1` if there are more. Tap it for details. Items still being watched
  appear only in the Health view.
- **Green** is used only for things that are on or live (the connection dot, the active tab, a
  toggle that is on), never for a value.
- **No colour at all** on voltage is deliberate at certain moments. Running limits apply only after
  the engine has run for 10 s. Parked limits apply only to readings taken at least 30 s after the
  engine stopped. Key-on readings (about 11.9 V is normal before cranking), cranking dips and the
  seconds just after a stop are shown without colour so they never look like a fault.

## Words you will no longer see on cards

The old dashboard printed these words on tiles and badges. They are gone from the cards; this is
what they meant and where that information lives now.

| Old word | What it meant | Where it is now |
|---|---|---|
| `REGISTERED`, `registered` | The metric is defined in the Pi's catalog. It said nothing about whether the value is trustworthy or current. | System view → Metric catalog |
| `MAPPED`, `UNMAPPED`, `mapping pending`, `n/m MAPPED` | Whether a decoding for that value exists yet on this van. `UNMAPPED` just meant "not available". | A dash on the card; the catalog lists what exists |
| `n/m LIVE`, `NOT LIVE`, `n/m LAST READINGS`, `LAST RECORDED · NOT LIVE` | How many values in a card were fresh, and that a shown value was a last reading. | Dimmed number with its time, or a dash |
| `STALE · 2.6 hr old`, `1.0 s old` under every tile | The age of each value, updated every second. | Live values show nothing; last readings show their time |
| `ALFA SCALE`, `OBSERVED ALFA SCALE` | The quality word "observed scale" (below). Every engine and temperature value on this van has it, so it told you nothing on a tile. | Catalog quality column |
| `CANDIDATE`, `ODOMETER*`, "validation required" | The quality word "candidate" (below). | A small `*` on the odometer; everything else candidate-quality stays in the catalog |
| `MIXED QUALITY`, `UNKNOWN` | The card combined values of different quality words. | Catalog |
| `diagnostics only` | The value is shown only for diagnosis, never as a driving number. | System → Metric catalog → Diagnostic-only |
| `SUSPECT SAMPLE REJECTED · showing last good`, `SAMPLE FILTER` | One raw reading failed a plausibility check (for example, transmission temperature jumping more than 10 °C in one second) and was thrown away. The previous good value stayed on screen. It is a data glitch, not a vehicle event, and never notifies. | Health → System notes ("sample filter", "implausible reading") |
| `BUS_ASLEEP`, `ENGINE_NOT_RUNNING`, `WRONG_BUS` | The Pi's reason a value could not be read. | The top bar's state; a refused voltage read explains itself in a sentence |
| `Listen-only`, `Armed diagnostic`, `Adapter absent` | The adapter's mode. Listen-only receives only; "armed" means the Pi's own drive helper is polling the engine computer, which is normal while driving. | System → Buses |
| `passive broadcast`, `physical read data by identifier`, source names like `ccan.broadcast.0x41a` or `pcm.did.01a1` | How a value is obtained. Broadcast values are only listened to; identifier reads are requests the Pi sends while the engine runs. | Catalog, and the sections below |
| `inferred` / `verified` confidence, "passive bus silence", "client freshness expired" | How sure the Pi is about the van's state, and why. | System → Vehicle state disclosure |
| "Cached vehicle state was invalidated across a browser page-lifecycle boundary" | When you switch back to the dashboard, it throws away what it had and fetches fresh data. For a moment values show as not live. | Nothing to show; it happens silently |
| `Loading metric catalog…` | Placeholder before the first data arrived. | A dash |
| `TRAINING`, `NO PERSISTENT CHANGES`, `TO REVIEW`, `Unresolved advisory`, `unavailable` under a warning | Early-warning states. | Health → Early warning; see the [warnings page](/docs/warnings.html) |
| `DELIVERY PENDING`, `DELIVERY ERROR`, "Persisted outbox" | Phone notifications waiting to be sent or failing. | One line under Early warning, only when something is pending or failed |
| `NEVER SCANNED`, `COVERAGE GAPS`, `NO DTCs IN AUTHORITATIVE RESULTS`, `256 SAVED STATUS RECORDS` | The state of the saved trouble-code results. | Health → Diagnostic codes (count of current codes; module list) |
| `ERROR-ACTIVE` | The normal state of a CAN controller, not an error. | Shown as "controller normal" |
| Regime, MAD, deviation, persistence `4/5`, episode numbers | The early-warning evaluator's working. | Event dialog → evidence sections |

## Quality words in the catalog

- **verified**: decoded from a physical test on this van (for example the tire deflate and reinflate
  test) or from an independently established bus decode.
- **observed scale** (`observed_alfa_scale`): the byte position and scale were matched against the
  AlfaOBD gauges on this van across hundreds to thousands of samples. It is not an independent
  physical calibration, but it has tracked the engine computer's own value closely in every drive so
  far. Only verified and observed-scale values drive a big number on Drive.
- **candidate**: a strong lead whose offset, scale or identity is not yet settled. A candidate never
  drives an alarm. Candidates shown outside the catalog are the odometer (dimmed with its time,
  with a small `*` on the Service card), the gear estimate (always written with `~`) and the radar
  angles (System card and Drive page 2), and each is explained in its own section below.

## Drive and engine

- **Speed**: C-CAN frame `0x101`, 12-bit field, raw ÷ 16 km/h, shown in mph. Observed scale.
- **RPM**: C-CAN frame `0x0FC`, bytes 0–1, low two bits masked, raw ÷ 4. Observed scale.
- **Gear** (`~7` under RPM and on Drive page 2): an estimate. It divides turbine speed by output
  shaft speed (both from frame `0x1F7`) and matches the result against the ZF 9-speed nominal
  ratios. The `~` means estimate, and it is not the gear the transmission computer reports. It
  appears only while the van is moving. Page 2 also shows the ratio itself, for example
  `ratio 0.70` (7th is about 0.70 and 5th is 1.00). During shifts or torque-converter slip the ratio
  sits between gears, and the estimate can lag or disappear briefly.
- **Speed limit** (Drive page 2, and `limit 55 mph` under Speed on page 1): the number the
  instrument cluster's traffic-sign display shows, copied from C-CAN frame `0x0E0` byte 0 in mph.
  It is whatever the van's camera last recognised, not a map value: it can lag a new sign, miss
  one, or keep the previous road's limit. It was checked against the cluster on a September 27
  drive (every noted change matched). The cluster sends 0 when it shows no limit, and the tile
  then reads `—`.
- **ACC** (under Speed on Drive page 1, with the full tile on page 2): the adaptive-cruise set speed from the cluster frame `0x5A0` (byte 3 in
  mph, cross-checked against byte 2 in km/h), checked against the cluster display on the same
  drive. The words under it come from the same frame and were checked against the cluster on a
  September 28 drive, where every noted event matched:
  - the state (`engaged`, `standby`, `accelerator override`, `ready`) and the `Off` value;
  - `vehicle ahead`, shown while the cluster shows its vehicle-ahead icon;
  - `gap 3 of 4`, the following-distance setting in bars;
  - `fixed cruise`, when regular cruise is in use instead of adaptive cruise. This was seen in
    one short episode only.

  Page 1 keeps the speed limit and adds the cruise set speed, state and `vehicle ahead`.
  `Cruise` means fixed-speed cruise. A missing or expired state hides every
  cruise detail, even if a set speed remains cached. Off and ready hide set speed;
  standby labels it as remembered. On page 2 the standby number is also dimmed.
  The frame
  arrives about once a second, so the tile can take a few seconds to follow a button press or a
  vehicle moving in. `vehicle ahead` is the cluster's icon, not a distance: the van does not put
  the range or closing speed of that vehicle on the buses the Pi listens to. All of it is display
  information only and never drives a warning.
- **Coolant**: C-CAN `0x2ED` byte 0, raw − 40 °C, shown in °F. The thermostat is fully open near
  220 °F. The highest value seen in 57 recorded trips is 221 °F. Hot-day idle at 213–220 °F is
  inside the normal envelope for this engine (13 of 57 trips reached it). Below 160 °F the engine is
  warming up and the bar has no colour.
- **Oil pressure**: C-CAN `0x41D` byte 2, raw × 4 kPa, shown in psi.
  - The engine has a two-stage oil pump, so about 80 psi at higher rpm and load is normal, not high.
  - OEM warm-engine references apply only at 192–212 °F coolant: 15–34 psi near 650 rpm, 28–35 psi
    at 1,000–3,000 rpm and 65–80 psi above 3,500 rpm.
  - The dashboard's floors are a little lower, so normal variation is not coloured: 15 psi below
    1,000 rpm, 22 psi up to 3,500 rpm and 55 psi above that.
  - The bar is judged only when the engine runs (live rpm ≥ 400) and coolant is at least 160 °F;
    otherwise it has no colour.
  - The OEM low-pressure fault threshold is about 12 psi while running (code P06DD context); below
    that the value is red.
  - The old dashboard's "550–850 rpm" idle window was a display convenience, not an OEM test band.
- **Transmission oil**: C-CAN `0x1F7` byte 3, raw × 0.375 + 57 °C, shown in °F. A plausibility
  gate drops single samples that jump more than 10 °C within one second (a known glitch); the last
  good value is kept. A highway plateau at about 178 °F is normal. Below 80 °F it is cold and has no
  colour.
- **Oil temp (VVT)**: PCM diagnostic identifier `069F`, which AlfaOBD labels "VVT Oil
  Temperature"; raw − 64 °C, shown in °F. It is read every 5 s while the engine runs. **Its
  relationship to the oil sump temperature has not been established.** Every attempt to find a
  separate sump-temperature identifier on this PCM has failed, so this is the only engine-oil
  temperature available.
- **Power**: computed on the Pi from crankshaft torque and RPM (`hp = lb-ft × rpm ÷ 5252`) when both
  are within 1.5 s of each other. It is the engine computer's estimate at the crankshaft, not wheel
  horsepower or a dynamometer figure. Negative values on overrun are real.
- **Torque** (the line under Power): PCM identifier `06DA`, signed 16-bit × 0.04 Nm, shown in lb-ft,
  read about once a second while the engine runs. It is the engine computer's crankshaft estimate.
- **Trip** (Drive): time since the trip started; distance added up from live speed readings (marked
  `≈`, gaps longer than 30 s are skipped, and it survives a page reload); the trip's highest coolant
  and average power from the Pi's trip history, refreshed once a minute.
- **Target torque** (Drive page 2): the transmission's requested crankshaft torque from frame
  `0x100`. It is a command, not a measurement. Compare it with **Torque** beside it to see how
  closely the engine follows the request. Negative values on overrun are normal.
- **Torque** (Drive page 2, and the line under Power on page 1): the same PCM `06DA` estimate as
  above. It is read only while the Pi's engine-running reads are active, so it can show `—` while
  target torque is live.
- **Turbine and output shaft speeds** (Drive page 2): frame `0x1F7`, observed scale. Turbine speed
  is the transmission input after the torque converter, and output speed is the shaft to the rear
  axle, both in rpm. They feed the gear estimate.
- **Odometer** (Drive page 2): the same ICS estimate as under Service below, always shown dimmed
  with the time it was read. It reads about 11 miles below the cluster.

## Electrical

- **Voltage**: C-CAN frame `0x41A` byte 0 × 0.05 + 4.0 V while the powertrain bus is awake, or the
  B-CAN frame `0x46C` (13-bit ÷ 400) when only the body bus is awake. Both are verified. Drive shows
  one decimal; Parked shows two.
- **Voltage limits.** Running: 13.6–14.6 V is normal. Below 12.8 V the card says "reduced charge",
  which is amber, not red, because the engine computer lowers the charge voltage routinely. It turns
  red only when the charging-failure warning is open. Parked (settled): 12.2 V or more is normal,
  11.8–12.2 V amber, under 11.8 V red, and under 11.5 V critical. Cranking dips to 11.1 V have been
  recorded and are normal.
- **Alternator** (`generator.field_duty`, the line under Voltage): PCM identifier `01A1`, the
  generator field command duty in percent.
  - It is a command, not alternator current or temperature. Sustained high duty means the engine
    computer is asking for a lot of charging effort; the dashboard does not infer any thermal
    danger from it.
  - The house-battery DC-DC charger is a normal, substantial load, so 100 % occurs routinely and is
    not by itself a fault.
  - The dashboard uses it only to back up a low running voltage.
  - Drive page 2 gives it its own tile, with a 15-minute trend and the live battery voltage
    underneath, so you can see charging effort and the voltage it produces together. It is read only
    while the Pi's engine-running reads are active; otherwise it shows `—`.
- **Parked battery reading.** The line under the number says when the reading was taken and how long
  after the engine stopped, for example `read 4:25 pm · 2 h after engine off`. After a stop, the Pi
  takes a settled reading once the battery has rested; the card says when that is due.
- **`Read voltage now`** asks the Pi to listen for one voltage frame. It is receive-only: it never
  sends anything on the van's buses, so it cannot wake the van, the dash USB or the dashcam. If the
  van is asleep there is nothing to hear, and the button explains that. On a screen where reading
  on request is turned off (a cache-only listener) the button is disabled with a note. The Pi's
  separate parked "wake and read" path does transmit, and it is not reachable from the dashboard.

## Tires

- **Tire pressures**: RF Hub identifiers `31D0`–`31D3`, read by the Pi during engine-running
  intervals only. Verified by a deflate/reinflate test on 2026-07-07. Slot order is FL, FR, RR, RL
  (the last two are not swapped). Raw `FFFF` means no sensor data and is never shown. Readings expire
  after 30 s; while parked, the last reading is kept with one time for the set.
- **Colours today**: a wheel turns red only below its absolute floor (front under 50 psi, rear under
  68 psi) or when a tire warning is confirmed. Pressures normally rise 2–4 psi when warm and 5–8 psi
  on the highway.
- **Colours later**: comparing each wheel with its own cold baseline needs a per-wheel figure the Pi
  does not publish yet. The 30-day and prior-trip averages it does publish mix warm and highway
  readings, and would be off by 2–3 psi, enough to colour normal cold mornings amber. That
  comparison will arrive with the Pi's cold-baseline figure.

## Service

- **Odometer**: body-bus module ICS identifier `2001`, read every 5 s during engine-running
  intervals. Its progression matches the speed integral within 0.5 %, but it reads about 11.1–11.6
  miles **below** the instrument cluster, and the reason is unresolved. No offset is applied. It is
  a candidate, marked with a small `*` (estimated mileage), shown dimmed with its time when not
  live, and never used for anything safety-related.
- **Miles since service** is calculated only when the oil change was recorded from the same mileage
  source as the odometer reading (for example, both from the ICS estimate). Mixing the cluster
  reading with the ICS estimate would be off by about 11 miles, so otherwise the line is left out.
  "Check the entry" means the recorded service mileage is higher than the odometer.
- **Oil life**: no source exists on this van yet, so the row is hidden (the old dashboard showed
  `UNMAPPED`). A historical AlfaOBD reading of 17 % is not a live value and is not shown.
- **Oil-change records** are stored on the Pi in `maintenance.json` and are shared by all your
  devices. Recording one never resets the vehicle's own oil-change indicator, and earlier records
  are kept.

## Vehicle state

- **Asleep** is inferred from silence on the powertrain bus at the approved bitrate. An unplugged
  adapter leg looks the same as a sleeping vehicle; the System view shows the adapter identities so
  you can tell them apart.
- **Ignition on** comes from the presence of frame `0x2EF`. Silence never means "off", it means
  unknown. That is why there is no ignition tile and the catalog shows `on` or `—`, never `off`.
- **Running** requires the verified RPM frame to report at least 400 rpm within the last 5 s.
- **Waiting for data** means the dashboard has no recent, trustworthy state (for example just after
  you switch back to it) and is fetching fresh data.
- Any transmission on a sleeping powertrain bus wakes the body computer, which can power the dash
  USB and the dashcam for a minute or two. Nothing the dashboard does transmits on a sleeping bus;
  the guarded code scan is a parked read you start deliberately (see below).

## Last readings

Some metrics keep their last recorded value on screen, dimmed and with its time, when they cannot be
read: voltage, odometer, coolant, VVT oil temperature, transmission oil temperature, the four tire
pressures and the radar angles. Everything else (speed, RPM, oil pressure, torque, power, alternator
duty, shaft speeds) is live-only and shows a dash when unavailable, because a stale value there
could be mistaken for a current one.

## Trends and trips

Completed-trip cruise summaries come from the saved full drive recording.
`Cruise engaged` counts the cluster's engaged state; accelerator override is
separate. `Vehicle ahead` counts the cluster icon while engaged or overriding,
not a measured following distance. Button cancels require a valid CANC press
and a transition to standby. `Possible brake` means that transition coincided
with the still-unverified service-brake bit; it is not a proven brake-cancel
count. Unknown and simultaneous button/brake events are kept separate.

Set speed is compared with the displayed speed limit only while engaged and
both values are recorded. The average difference is weighted by time; it is
not actual road-speed speeding time. A no-limit display contributes no speed
limit. Coverage and missing time remain explicit: display observations are held
at most two seconds, speed limits at most half a second, and missing capture
tails are not extrapolated. `Not recorded` does not mean cruise was unused.
These summaries never create a warning.

- The History view's trends come from the Pi's historian: 7-day and 30-day low, average and high,
  the typical range of earlier trips, and 24-hour lines built from 15-minute buckets. Missing
  telemetry stays a visible gap in the line; nothing is filled in.
- The Drive view's small lines cover the last 15 minutes, built in the browser from live readings.
  They start empty after a page reload.
- While parked, most metrics have "gaps" because nothing can be read; that is expected and is not
  flagged.
- Trip statistics beyond duration and end time (distance, maxima) will appear when the Pi publishes
  per-trip summaries.

## Early warnings and system notes

Warnings are evidence to look at, not a diagnosis or an opaque health score. How they are graded
and when your phone is notified is on the [warnings page](/docs/warnings.html). On the Health view:

- **Early warning** lists vehicle items only. `Unconfirmed · engine off since 4:25 pm` means the
  warning was open when the engine stopped and cannot be re-checked until it runs again.
- **System notes** hold adapter, bus and data-quality items, such as a sample filter that threw away
  implausible readings. They are never counted as warnings and never notify.
- **Phone alerts: n pending / failed** appears only when a notification is waiting or failed.

## Radar alignment

Two angles come from the ACC radar identifier `0845`, at candidate quality, read during
engine-running intervals. The ±1° reference is a monitoring convention for this project, not an OEM
alignment result. The System view shows the latest angle per axis, and on tap the 1-minute and
5-minute averages (with sample counts), the 5-minute peak, and the margin to ±1.00°. Drive page 2's
**Radar aim** tile shows the same angles at a glance: the latest value per axis and the 1-minute and
5-minute averages. The values are what the ACC radar module reports about its own aim. They are not
a measurement of the van, and no alignment result or calibration is implied. Its footer reads
`within ±1°`, `near the ±1° limit` (0.8° or more) or `outside ±1°` while readings arrive. When they
stop, it reads `Not reading · last 3:10 pm` and the last values are dimmed; the averages then cover
the last minutes of that reading run, not the current minute. "Awaiting
readings" means no angle has been read during the current drive yet. If the radar reads are ever
switched off on the Pi, the System view says so in one sentence.

## Diagnostic codes

- The codes list is the saved result of the last read from each module, with its date. "Current"
  and "pending" describe that module's status bits when it was last read, not the vehicle right now.
- Titles come from the reviewed 2022 ProMaster service material for the reporting module. Where no
  reviewed title exists, only the standardized failure subtype is shown. A title explains what the
  code means; it is not a diagnosis.
- Status `0x40` (test not completed) is listed separately and is not a fault.
- The Pi keeps at most 25 records per group, so a long group (for example "Test not completed")
  says how many of how many are shown. The counts are always complete.
- The module list shows which modules answered, which were unavailable and which have never been
  read.
- **Scanning.** The LAN screen cannot scan or clear. The Tailscale screen (port 8765 on the Pi's
  Tailscale address) can queue one guarded, parked read of 15 modules after you confirm the van is
  parked in P with the ignition on and the engine off. It can never clear a code. Every browser
  open on the Tailscale screen shares the same job record, so each shows a scan started from
  another; start one scan at a time.
  "Restoration unverified" means the Pi could not prove the adapters returned to listen-only
  afterwards; inspect before scanning again.

### The van's own health check

- The van has its own built-in diagnostic client, probably the telematics box (TBM2). Every so often
  during a drive it reads the modules' identity and their trouble codes. It uses the same tester
  address as the Pi (`F1`) and only runs while the Pi's drive helper is not polling. It then yields
  within one or two seconds when the Pi resumes. It has been seen on 2026-08-31 and 2026-09-24.
- The Pi sends nothing for this. `projects/vehicle_data/van_scan_harvest.py` reads the finished
  files of the drive recorder afterwards. It rebuilds the van's requests and replies, and it skips
  the Pi's own fixed requests. It saves each check under `tmp/vehicle_data/van_scans/`, in
  separate tables of the DTC history, and in `van-scan-cache.json`. The broker adds that file to
  its saved-codes answer as `in_vehicle_scan`.
- **Health → Diagnostic codes** uses the newer read for each module, the Pi's or the van's. Codes
  and modules taken from the van's check say "from the van's own health check · 3:12 pm". That time
  is when the check started. The line under "Last read" counts how many modules came from it.
- The van asks only for codes whose status has *test failed*, *pending* or *confirmed* set (UDS
  `19 02 0D`, status mask `0x0D`). The Pi's parked scan asks for every status (`19 02 FF`). So when
  the van's read is newer, the Pi's older codes *inside* that mask are replaced. The Pi's codes
  *outside* it stay, for example "test not completed" only, because the van's check cannot see
  them. The van's results never mark a Pi code as resolved in the Pi's own history. Group totals
  are corrected only by the records the Pi returned, so a truncated group can be off by the hidden
  ones.
- The engine computer (PCM) does not support that request. The van falls back to an older KWP
  read (`18 00 FF 00`), and that answer is shown the same way.
- Modules that the van tried but that never answered are not listed. They are optional equipment
  this van does not have. Identity reads are saved in the scan files but not shown on cards. Any
  VIN in them is masked.
- **System → Broker → Van health check** shows when the last check started. "(while the Pi was not
  polling)" means none of the Pi's own requests overlapped it. "None recorded yet" means no check
  has been harvested yet.
- The harvester runs from a timer (`van-scan-harvest.timer`) against completed recorder chunks
  only, so a check appears some minutes after it ends. It never appears while the chunk holding it
  is still being written.

## Buses and adapters

- There are three permanent taps: C-CAN (powertrain, 500 kbit/s), B-CAN (body, 125 kbit/s) and
  CAN-CH (a second high-speed bus, 500 kbit/s, passive only).
- They are identified by adapter serial, not by Linux interface name, because interface names can
  change after a reconnection.
- "Controller normal" is what the old dashboard printed as `ERROR-ACTIVE`, the normal CAN controller
  state.
- While the Pi's drive helper polls the engine computer, the C-CAN role reports "armed"; that is
  normal during a drive.
- **Safety inhibits** lists any lock the Pi has placed on transmitting after something it could not
  verify. "None" is the normal state.
- An unconnected spare adapter that needs attention appears in System notes, not as a warning.

## This device

Dashboard settings (the view, Auto, Dim, hidden cards and card order) are stored in this browser
only and never reach the Pi or the van. The dashboard replaced the old one at the same address
(port 8765), so the view last chosen on the old dashboard is carried over once, and a Codex chat
access code already entered in this browser still works.
