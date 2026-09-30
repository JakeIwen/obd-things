# Next-drive ACC deceleration callouts

Use the next normal drive. A passenger can note these, or dictate a short note
when safe; there is no need to operate the Pi or manufacture a traffic event.
The proposed indicator is **not on the dashboard**. `0x4AF` byte 3 bit 2
remains Tier 1: previously it appeared only while slowing behind a vehicle,
without an independent braking reference.

Please give the date and **Central time to the minute**. Include seconds only
if convenient. The Pi logs Mountain time. For the September drive, 7:11 PM CDT
was 6:11 PM MDT and 00:11 UTC the next day. Keep the zone with each note; we
will convert using `America/Chicago` and `America/Denver` for the actual date.

| When it naturally happens | What to note | Example note (illustrative) |
|---|---|---|
| ACC slows behind a vehicle, your foot off both pedals | Start/end minute, vehicle icon, road speed before/after, set speed, gap bars, flat/uphill/downhill | “7:11–7:12 Central: ACC set 61, gap 3, vehicle icon on; slowed 60 to 52; no pedals; flat.” |
| Following at steady speed | Minute and roughly how long, icon on, no pedals | “7:15: followed steadily about a minute, icon on, no slowing.” |
| ACC accelerates again while the icon is still on | Minute, speed before/after, no pedals | “7:17: lead car sped up, van went 52 to 60, no pedals.” |
| You brake and cruise cancels | Minute, explicitly say brake pedal, icon before the cancel | “7:20: I pressed the brake; ACC went to standby.” |
| You use CANC without touching a pedal | Minute, explicitly say CANC, whether the van then coasts/slows | “7:23: CANC button, no brake; coasted down.” |
| Ordinary slowing with no vehicle icon, or on a hill | Minute, ACC on/off, pedal use and grade | “7:28: downhill, ACC engaged, no icon; speed reduced.” |

A note that a situation **did not happen** is useful. Two or three clearly
remembered episodes are more useful than trying to capture every event. Do not
change following distance or provoke a hard-braking/FCW event for this task.

## Promotion decision

Freeze the proposed bit before matching new notes. Use synchronized full C-CAN
recordings of a complete independent drive, with intact timestamps and explicit
coverage, to compare positive episodes with steady following, acceleration,
driver braking, CANC/coasting, and grade-related slowing. Require repeated
onset/offset agreement within each note's time uncertainty and investigate
every counterexample. Minute-only notes cannot prove sub-second response time.

This can establish an **ACC-associated deceleration indication** if the
independent callouts discriminate the alternatives. To label it **ACC braking**
as a Tier 3 physical brake request/application, also require an independently
trusted labeled brake-command/pressure reference or an exact compatible OEM
definition. Slowing alone is insufficient: Bosch describes ACC speed reduction
through either releasing acceleration or brake control, so those mechanisms
must be distinguished. [Bosch ACC operation](https://www.bosch-mobility.com/en/solutions/assistance-systems/adaptive-cruise-control/).

Only after that review should the canonical bus map, decoder quality and
display be changed together. No warning or safety logic is proposed.
