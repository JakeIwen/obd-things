# Migrated agent context

This document preserves cross-project facts and user constraints that previously existed only in
Claude's external project-memory store. Detailed technical state remains in the canonical root and
project handoffs linked from `AGENTS.md`; prefer those when they are newer. Verify all live machine,
service, cron, network, and vehicle state before acting.

## Diagnostic approach and user constraints

- At each major fork, first check existing repo findings and locally available OEM material, then search
  for current OEM procedures/TSBs and relevant tool behavior, and ask what tools or access the user has.
  Low-level CAN/UDS reverse-engineering is the fallback after the ecosystem layer is exhausted.
- A local AllData/factory-document scrape has historically lived at `~/dev/ram_2022_GAS`; check whether
  it still exists when factory wiring or DTC procedures matter.
- The 2022 ProMaster is the user's full-time home, office, and dog's space. Prefer driveway/campsite DIY,
  tools operated on-site, and experiments piggybacking on normal driving. Dealer/shop drop-off is not a
  practical recommendation unless the user explicitly reopens it.
- Do not edit/disable crontab or the cron daemon without explicit permission, even to prevent CAN
  contention. Inspect first and let the user control unrelated persistent automation.

## Physical diagnostic topology

- The vehicle's Security Gateway is intentionally bypassed. Do not attribute failed UDS writes,
  routines, or actuations to stock SGW authentication; investigate session, addressing, ECU security,
  power mode, and bus state instead.
- The permanent Pi installation uses two dual-channel USBCANFD adapters and three simultaneous taps:
  C-CAN pins 6/14, B-CAN pins 3/11, and CAN CH pins 12/13; the fourth channel is unconnected and stays
  down. All vehicle roles are classical CAN with FD off and are identified by USB serial plus `dev_id`,
  never by Linux enumeration order. The exact executable mapping and rates live
  in `lib/vehicle_can_roles.py`; `docs/bus-map.md` is the canonical human map.
- The OBDLink MX+ can remain on the parallel diagnostic branch while the Pi observes the permanent
  taps. Historical 2026-07 PCAN captures remain valid evidence for the physical pair they record, but
  their `can0` name and single-adapter cable-switching procedure are not current routing instructions.
  A 2026-07-21 simultaneous observation captured AlfaOBD UDS to `0x85/87/98/D9` on pins-3/11 B-CAN
  while the standard BCM `0x40` profile routed over C-CAN. Legislated OBD-II Mode 01 traffic does not
  appear on the internal C-CAN tap; do not retry it as a generic signal source.
- SocketCAN `listen-only` state is sticky across an ordinary link bring-up. Every configuration must
  explicitly select classical CAN with FD off, the role's fixed bitrate, listen-only on or off as
  intended, and `restart-ms 0`, then verify one fresh atomic readback. The legacy PEAK driver also
  lacks `berr-reporting on`; that is retained as a historical adapter-specific limitation, not a
  reason to infer anything about a current `gs_usb` role.
- Safety-hardened inventory CLIs default to an offline dry run. On installed dual-USBCANFD hardware,
  their live mode derives the physical channel from `Module.bus` and the exact serial/`dev_id`, takes
  the logical-role lock followed by the currently resolved-channel lock, rechecks identity under
  ownership, verifies classical-CAN/interface/inhibit/vehicle gates, and restores passive mode before
  unlocking. A saved/default `can0` is not a bus identity. Explicit channel
  names may appear in offline capture metadata, fixtures, or deliberately
  arbitrary-hardware library use, but are not a current ProMaster bus-selection
  workflow. Cooperative locks supplement rather
  than replace service/drive-capture preflight and manual coordination with external tools.
- Parked C-CAN diagnostic TX can wake the BCM, briefly power switched accessories, and boot the dashcam.
  The user approved low-frequency parked TX without a separate prompt, but it is still an observer and
  battery effect. Avoid gratuitous traffic. See `docs/bus-map.md` for verified wake behavior.
- Two different dashboards exist; do not confuse them. The repository's telemetry dashboard is the
  single Preact app served on port 8765 by `projects/vehicle_data/web_v2.py`
  (`van-telemetry-web{,-tailscale}.service`; the old static frontend and the parallel "v2" units were retired on
  2026-09-27). The MacBook-managed Van Dashboard (`van-dashboard.service`, running from read-only
  `/home/pi/scripts`) consumes that listener's `/v1/snapshot` API.
- The separate machine-local `van-dashboard.service` remains CAN-free: it may display cache-only
  telemetry over HTTP, publish the existing COP ALERT marker, and observe the independent ignition
  marker, but it must never open a CAN socket, inspect an interface, or select a netdev. The role-aware
  `van-cop-can-wake.service` is the only maintained bridge from the COP marker to CAN. It uses the
  fixed one-shot C-CAN RF-Hub `22 FEFF` wake profile, requires fresh broker/vehicle/USB safety state, pauses
  for the ignition marker or running evidence, and restores the exact passive role before every
  fixed-cadence wait. A disabled, blocked, or unavailable wake service leaves the exterior-light
  and notification behavior intact but does not keep the dashcam/accessory network awake.
  Explicit marker creation uses a short 250 ms debounce; only a marker already present at process
  start receives the three-second restart grace. Pre-transmit safety contention retries after
  500 ms. Structured journal transitions and persistent `last_blocked_*` status fields preserve
  the reason for a failed activation even after the dashboard removes the marker.
  Broker C-CAN voltage/powertrain reads now pass through a reader gate before taking a shared
  per-role fairness turn; the fixed wake closes that gate, waits at most 1.25 seconds for in-flight
  readers to drain, and reserves the turn exclusively before taking the still-authoritative
  exclusive role/channel locks. This prevents new broker observers from overtaking a waiting COP
  refresh while allowing the current observer to finish. The handoff is scheduling only and never
  authorizes CAN.
  The 15-second COP cadence is measured attempt-start to attempt-start rather than after completion.
  Its outer broker-state gate accepts at most five-second-old evidence, matching the registered
  ignition/RPM freshness window; the wake core still checks live C-CAN ignition/RPM witnesses at
  every send boundary.
- `lib/can_wake.py` owns the two reviewed network-wake profiles. Scheduled parked voltage uses only
  the fixed B-CAN `0x7FF` burst followed by verified `0x46C` voltage; COP ALERT uses only the fixed
  addressed C-CAN RF-Hub read because its accessory-rail side effect is the intended behavior.
  Consumers pass `b-can` or `c-can`, never a channel, rate, pair, identifier, payload, cadence, or
  restore policy. There is no CAN-CH profile, and one role's wake method is never substituted for
  another. Both paths resolve USB serial plus `dev_id`, hold the exact role/channel exclusively,
  honor same-boot inhibits and other owners, and return success only after verified passive
  restoration. Passive signatures and bitrate are corroborating state checks, never routing
  authority.
- The installed `gs_usb` firmware supports ONE-SHOT but rejects automatic bus-off restart. A
  sleeping-bus F190 request with ordinary retransmission returned data yet left C-CAN
  ERROR-WARNING across link down/up; the restoration latch and an exact Board A USB reset were
  required. Do not reintroduce that PEAK-era retry policy. The maintained C wake uses ONE-SHOT
  `22 FEFF` only until C-CAN signatures appear, then switches to normal retry for one acknowledged
  validation read and restores ONE-SHOT off.
- Board A (CANable 2.5, ElmueSoft Candlelight firmware, serial `207C3384413250013`) can also hang
  silently with no transmit history: from about 2026-11-19 to 2026-11-22 both its roles (C-CAN
  `can0`, B-CAN `can1`) stayed up, listen-only and ERROR-ACTIVE with zero error counters and no
  kernel messages, yet received nothing, and scheduled B-CAN wakes were counted as TX `dropped`.
  The dashboard went dark through the next drive while Board B's CAN CH kept receiving. The
  broker's `receive_watch` flagged both roles `receive_silent` against the CAN CH reference but
  neither alerts nor recovers. Recovery (2026-11-22, while driving): `sudo usbreset BUS/DEV` for
  that exact serial (read `busnum`/`devnum` under `/sys/bus/usb/devices/<port>`); the broker's
  passive reconciler restored both roles to listen-only within seconds and relaunched the drive
  helpers, with no broker restart. Check `receive_watch` in `/v1/status` `interface.role_interfaces`
  when C-CAN data is missing while CAN CH is live.
- Standing owner authorization: Codex may run already-reviewed, physically addressed, non-mutating
  data reads without requesting separate conversational approval each time. This covers UDS `19`,
  `1A`, `22`, result-only `31 03`, and controller-defined read-only AlfaOBD observations, provided no
  session-changing or session-maintenance preamble is required. A metric registry or controller
  action list is a mechanical schema/scope guard, not a request for per-reading human approval. This
  standing authorization does not authorize functional
  broadcast, `10`, `3E`, reset, SecurityAccess, writes/coding/PROXI, DTC clear/control,
  CommunicationControl, IOControl, or routine start/stop (`31 01`/`31 02`). Current vehicle-state,
  physical-pair, interface, scope, rate, locking, and cleanup gates remain mandatory; this standing
  authorization is not evidence that the van is parked, the engine is off, or the intended bus is
  connected.

## USB 2.0 block for the disk hub chain

The Pi 4's onboard USB 2.0 hub (`1-1`, VIA 2109:3431) carries the CAN adapters' own hub on port 4
(`1-1.4`, adapters at `1-1.4.2` and `1-1.4.4`; moved there by the owner on 2026-11-22). Its port 2
is the USB 2.0 half of the external disk hub chain (three Realtek 0bda:5411 hubs in series), whose
disks all run on USB 3.0 (bus 2). Port 2 is kept logically off (owner decision 2026-11-22: the
disks have no reason to be on USB 2.0).

The three hubs are very likely one box, not three: each hangs off port 4 of the one before and
they offer 3 + 3 + 4 sockets (`lsusb -t`, 2026-11-23), which matches the Sabrent HB-BU10 10-port
powered hub in the owner's inventory (a Mac session's reading of the same day; nobody has looked
at the unit). If so, the "deepest hub" (`1-1.2.4.4` on USB 2.0, `2-2.4.4` on USB 3.0) is a chip
inside it that cannot be taken out of the chain; the physical choices are to retire the one
socket or replace the unit.

- Why: on 2026-11-22 port 2 of the deepest disk hub (`1-1.2.4.4`) failed low-speed enumeration
  every few seconds for a day (about 197,000 kernel lines). The failures stayed on that port
  after the disk that had been on it was moved to another. Twice that evening every USB 2.0
  device under `1-1` failed at once with `-71`, CAN adapters included: at 17:45, mid-drive, which
  latched the two restoration inhibits and ended telemetry for the drive, and at 19:50, five
  minutes after a reboot let the chain enumerate again. Port 2 of the Pi's own hub also failed
  enumeration at 19:22. The USB 3.0 side logged nothing each time. The chain is the suspected
  cause, not a proven one: hub drops are also recorded on earlier dates (`usb_can_incidents`).
- Source: `projects/vehicle_data/usb2_block/`. `install.sh install` copies two root-owned files
  and enables the watcher; `install.sh status` is the read-only check (installed copies identical
  to the checkout, watcher enabled and active, block held); `install.sh uninstall` removes them,
  verifies it and exits 1 when anything is left.
  - `vanpi-usb2-disk-hub-off` (Python, standard library) → `/usr/local/sbin/`, run as root by
    `vanpi-usb2-disk-hub-off.service` (`watch`). Every 5 s for its first minute, then every 10 s,
    it checks that `/sys/bus/usb/devices/1-1/1-1:1.0/1-1-port2/disable` reads `1` and that
    nothing is enumerated at `1-1.2`. When either fails it writes `1` (up to six attempts 2 s apart,
    each confirmed twice), which disconnects whatever enumerated behind the port and stops detection
    on it, and it sends one ntfy per boot when the port cannot be held off; the sender runs as
    `pi`, never as root.
  - It is the only writer on purpose. A single write at boot is not enough: a connection event
    already queued in the kernel can power the port back on, a failed control transfer leaves it
    on, and an in-place hub reset or controller recovery can restore power with no udev event.
    There is no udev rule: one would write without the disk check below, and initramfs-tools
    would copy it into every boot image, out of an uninstall's reach.
  - It does not pull a disk that is in use (a disk can fall back from USB 3.0 to USB 2.0 and
    be mounted before a pass sees it). Before each write it takes the kernel's exclusive claim
    on every disk behind the port, whatever capacity the disk reports, checks that each opened
    node is the device sysfs describes, and holds the claims across the write. A disk that is
    mounted, is swap, is held by another block device or cannot be inspected or claimed makes
    it refuse and alert. It cuts only storage it knows: `KNOWN_STORAGE_DISKS` in the program
    lists each model with the number of disks it registers (the Genesys 05e3:0764 reader: two,
    one per card slot), and the write waits for all of them to be listed, because the kernel
    registers disks one by one, a second or more after the USB probe. That wait is bounded
    (owner approval 2026-11-23): a reader whose probe is failing never lists its disks, so when
    three attempts in a row see the identical listing with a known reader still short of its
    disks, the third claims the disks that are listed and writes, with one warning in the
    journal that names the reader. A write, or a listing that changes, restarts the count, and
    a listing that cannot be read in full counts for nothing: it refuses. Three checks span
    about 4 s, which a slow but working reader can meet too, so such a reader is cut as well.
    Not covered: a disk of that reader that the kernel registers after the final listing has
    read the block entries, and that something mounts before the kernel carries out the write,
    is disconnected unclaimed (udisks2 and the desktop file manager can automount a card). Any
    other storage behind
    the port, such as a disk that fell back to USB 2.0, is never disconnected: it refuses and
    alerts, and a person decides. It then lists the storage behind the port again, and writes
    nothing on that attempt if a disk or storage interface appeared, left or changed while it
    was claiming. The known reader does not stop the power-off when both of its disks are
    listed and unused, cards or no cards. Two
    listings are not a lock on the USB topology: a device whose storage interface appears after
    the second listing, and whose disks are registered and mounted before the kernel carries
    out the write, is not covered. A new USB 2.0-only storage device in the chain needs a row
    in that table (or a Pi port of its own) before the block will hold with it attached.
  - A hub at `1-1` whose port control is missing counts as a failure after three passes, at
    once when something is enumerated behind the port. It writes nothing but that attribute
    and its alert marker under `/run`, and never touches another port or a hub that is not
    2109:3431 at `1-1`.
- Tested live 2026-11-22 20:17 (VL805 firmware `000138c0`, kernel 6.12.62): with `disable` 1 and
  `early_stop` cleared, nothing enumerated on port 2 for the minutes observed, bus 2 kept all
  eight devices, every disk and mount stayed attached, and `can0`/`can1`/`can2` stayed UP,
  listen-only, ERROR-ACTIVE with no kernel message. The hub reports ganged power switching, so
  one port's power-off is logical; the 5 V rail was not measured. That test wrote `1` to a port
  that was already idle; cutting off an enumerated chain happens only at a boot or a hub
  re-enumeration and had not been observed when this was written.
- Observed 2026-11-23 03:13-03:16 (US/Mountain), with no reboot: the block lapsed and the fault
  it guards against happened within two minutes.
  - 03:13:23: port 2 came back on by itself (`disable` read `0`) in the same second a Samsung
    Android device (04e8:6860) enumerated on Pi port 3 (`1-1.3`). The whole chain enumerated
    again, the failing `1-1.2.4.4` port included, and its failures restarted at 03:13:25. Cause
    not established; the coincidence says a connection on another port of the onboard hub can
    reopen port 2. The device is a Galaxy Tab E that the owner's voice-desk project drives over
    ADB from the Mac (not the dash tablet), so it will reconnect from time to time. That night
    the owner was replugging cables for that project from 02:35 to 03:13, which is how the
    tablet reached port 3.
  - 03:13:51-03:13:57: the watcher saw it at its next pass and refused three times, "storage
    interface 1-1.2.1:1.0 behind it shows 0 of its 2 disks; nothing written", then logged "still
    on after 3 attempts". The card reader was being reset by the kernel and never registered its
    disks, so the rule that waits for all of a known reader's disks kept the watcher from writing
    for as long as the chain stayed up. Whether its one-per-boot ntfy was sent was not checked.
  - 03:15:05: `clear tt ... error -71` on the CAN adapters' hub (`1-1.4`); 03:15:16-03:15:22: the
    chain, the CAN hub and both adapters disconnected and the kernel power-cycled port 2. The
    adapters re-enumerated with new interface indexes. The van was asleep and no helper was
    armed, so no restoration inhibit was written: the broker brought all three roles back up
    listen-only at 03:15:24-03:15:25 by its ordinary reconciliation, and the USB drop recovery
    below had nothing to do.
  - 03:15:43: the watcher logged "the USB 2.0 block holds again" without having logged a write
    of its own; how the port returned to off is not established.
  This is the third time the CAN adapters dropped with the chain on USB 2.0 and the first with
  the block installed. The watcher of that night could not cut a chain whose known reader never
  finished registering its disks, and it looked only every 30 s; the bounded wait and the 10 s
  interval described above are the answer to both. With them the first write to a port that
  reopens between two 10 s checks comes about 15 s after the reopening when the listing does
  not change (up to 10 s to notice, 4 s of unchanged listings), against the 100 s the collapse
  took that night. That is the time to the first write, not a limit on how long the port can
  stay on: a listing that keeps changing, or a refusal, leaves it on and alerts. The margin
  rests on one observation, and the bounded wait has run
  only against a scratch sysfs tree (`tests/test_usb2_disk_hub_off.py`), never against the real
  stuck reader. From 03:18 to 13:41 that day, with the block held, the kernel logged no USB
  line at all.
- The fault's pattern, reconciled from the kernel logs by a Mac session on 2026-11-23 (not
  re-derived here): the failing port enumerates a low-speed device that is not there, about four
  attempts a second, alternating `descriptor read/64, error -32` and `not accepting address,
  error -71`. Every USB 2.0 device under `1-1` failed at once four times: 17:45:34, 19:22:19 and
  19:50:27 on 2026-11-22 and 03:15:05 on 2026-11-23. All four came while that port was failing
  and none in about twelve hours without it, but two 80-minute runs of failures (10:58-12:18 and
  17:47-19:08 on 2026-11-22) passed with no such collapse, so the failures alone do not cause
  one. `xhci_hcd: unexpected setup address command completion code 0x24` preceded three of the
  four. On 2026-11-22 the failures ran only while the Galaxy tablet was not enumerated on that
  chip's port 1: they stopped the second it enumerated and restarted the second it left, each
  time. An unverified reading is that the port misbehaves only when the chip has no active
  USB 2.0 child. Power is not indicated: `get_throttled=0x0` since boot, no over-current line,
  and bus 2 kept every device through each collapse.
- Limits: xHCI is built into the kernel, so at boot the chain enumerates until the watcher's
  first pass, when the multi-user services start. A USB 2.0-only device anywhere in the disk
  hub chain stops working (one was present on 2026-11-22: a Genesys 05e3:0764 "USB Storage"
  reader on the first hub's port 1, holding two 62.5 GB cards); such a device needs one of the
  Pi's own sockets instead. Ports 1 and 2 of the onboard hub are the USB 2.0 halves of the two
  blue sockets and ports 3 and 4 are the two black ones, so a blue socket with a USB 3.0 device
  in it is not free. On 2026-11-23 none was free: a USB 3.0 stick in port 1's socket, the disk
  hub in port 2's, and the CAN hub and (at times) the owner's Galaxy tablet in the black ones.
  Do not plug the CAN hub into port 2.
- Check: `projects/vehicle_data/usb2_block/install.sh status`, or
  `/usr/local/sbin/vanpi-usb2-disk-hub-off status` (prints `held`). The watcher's journal
  (`journalctl -u vanpi-usb2-disk-hub-off`) is silent while the block holds and has a warning
  each time it had to power the port off again.
- Undo: `install.sh uninstall`, then `echo 0 | sudo tee
  /sys/bus/usb/devices/1-1/1-1:1.0/1-1-port2/disable`. Do that only after the faulty hub or
  cable is out of the chain; while the watcher runs it undoes a manual `0` at its next pass.
- Open as of 2026-11-23, the first boot with the block installed (installed 2026-11-22 21:14
  US/Mountain; the Pi had been up since 19:47 that evening). A boot is the first time the watcher
  has to cut an enumerated chain. Whichever session first finds `uptime -s` later than the
  install time runs the check and replaces this bullet with the dated result: `install.sh status`
  exits 0 with `block: held`; `journalctl -u vanpi-usb2-disk-hub-off -b` shows one or more
  "powering it off" warnings followed by "off again" (an error line, or an ntfy "vanpi USB 2.0
  block FAILED", is a failure); `journalctl -k -b | grep -c 'usb 1-1'` stays flat after the first
  minute. The owner reboots when he chooses (`sudo /home/pi/scripts/safe_reboot.sh`); agents do
  not reboot the Pi for this.
- Waiting on the owner (physical, not urgent while the block holds; told 2026-11-22 and
  2026-11-23, no answer recorded): port 2 of the deepest disk hub (`1-1.2.4.4`) is the faulty
  one, since the failures stayed on it after the Seagate was moved to port 3, which cleared that
  disk, its cable and enclosure. On 2026-11-23 at 13:41 the 460 GB SanDisk stick mounted at
  `/mnt/xf-500` sat in that socket (`2-2.4.4.2`, on USB 3.0). The owner's choices are to move
  that stick to another socket and leave this one empty for good, or to replace the unit;
  whether an empty socket still fails is not known, because the block has kept the USB 2.0 side
  off since. At 19:22 on 2026-11-22 the first disk hub's own USB 2.0 link to the Pi's port 2
  also failed, so the unit's upstream cable or the Pi port may be involved too. The card reader
  the block cuts off works only on a free Pi socket (see Limits); if he does not use it,
  unplugging it removes the one device that has stalled the watcher.
- Unrelated to USB 2.0, also the owner's: the WD Elements 6 TB (`/mnt/bigboi`) has a disk fault,
  not a cable fault. SMART shows 12 pending sectors and no CRC errors, and the kernel's medium
  errors repeat at the same three addresses on every mount (13 on 2026-11-22; SMART read by a
  Mac session on 2026-11-23). `/mnt/EXFAT512` was flagged "not properly unmounted" at 17:52 and
  19:46 on 2026-11-22 and has not been checked.

## CAN adapter USB drop (Board A) and automatic recovery

Board A is the dual-channel adapter that carries C-CAN and B-CAN (its serial is `BOARD_A_SERIAL` in
`lib/vehicle_can_roles.py`).

- Incident, 2026-11-22 (US/Mountain): at 17:45 every USB 2.0 device under the Pi's onboard hub
  failed at once with `-71` mid-drive (see the USB 2.0 block section above for the suspected
  cause). Both drive helpers were armed and could not verify restoration, so the broker wrote
  `vehicle-data-restoration-failed` and `vehicle-data-bcan-restoration-failed` (channel `*`) and
  latched both in memory. The re-enumerated netdevs came back DOWN and stayed down, because the
  reconciler returns `inhibited` before it looks at link state. No telemetry for the rest of the
  drive. Hub drops are also recorded on Aug 22-25, Sep 2-3, Oct 7-8 and Nov 18.
- Manual recovery (still needed when the automatic path does not act: parked, the boot's one
  attempt already spent, any refusal, `committed_late`, or an incident left at `committing` after
  a broker crash): inspect all roles and each channel
  (`python3 -m tools.can_operation_state status --channel <ch>`), run
  `python3 -m tools.can_operation_state inhibit-end NAME` for each restoration inhibit, then
  `python3 -m projects.vehicle_data.restart_broker` (parked; `--while-running` with the van on),
  because the broker holds the latch in memory. Establish why an inhibit was written before
  ending it. Run these as `pi`, never with `sudo`: a root-created
  `tmp/locks/operation-state/.inhibit-writers.lock` makes later `pi` writers fall back to
  `nolock.*` wildcards and manual ends refuse.
- Automatic path: ACTIVE since 2026-11-23. The flag `--enable-usb-drop-recovery` is in
  `projects/vehicle_data/systemd/van-telemetry.service`. Root runs only the installed link program
  `/usr/local/sbin/vanpi-can-link-by-index`, installed from `projects/vehicle_data/usb_recovery_link/`
  and pinned by digest; a change there or to the digest takes effect only through
  `install.sh install` (`status` is the check; a missing or changed copy disables recovery).
  Conditions, vocabulary, files, activation and rollback are in
  `projects/vehicle_data/docs/usb-drop-recovery.md`; the trail is the journal
  (`journalctl -u van-telemetry`) and `tmp/vehicle_data/usb-recovery/`.
- Owner decisions and accepted risks (Jacob, 2026-11-22 and 2026-11-23): the adapter-firmware
  risk after a powered USB disconnect is accepted without bench validation (a new netdev and
  host listen-only readback do not prove the controller stopped transmitting; lower while
  driving, worse parked, where a stuck transmitter can keep modules awake and drain the battery).
  Recovery runs at most once per boot and only with the engine proven running (400 rpm) on the
  new listen-only C-CAN link. The ignition flag (`/home/pi/hooks/ignition_is_on`, written by the
  MacBook-managed `ignitionmon.service`) is a pre-gate only; a stale flag can start the engine
  check while parked. That check keeps C-CAN up listen-only normally two to four seconds, with
  evidence collection capped at five; a blocked take-down leaves it up listen-only until
  inspected. The 2 s engine rule is a check made right before each inhibit removal, not a bound
  on when the removal takes effect; a late completion keeps the latches (`committed_late`), and a
  broker crash at that moment leaves the incident record at `committing` with one error line in
  the journal at the next start.
- Verified 2026-11-23: whole offline suite; flag-off parked restart (no recovery line, no record
  directory); link program installed and digest-verified; an unprivileged request refused by the
  kernel (`EPERM`); the privileged request exercised only in throwaway network namespaces (down
  on loopback ok, up-listen-only refused as not a CAN device, unknown index `ENODEV`); activation
  restart with the enabled journal line, three vehicle roles healthy and listen-only, TX counters
  unchanged. Not verified: a real mid-drive recovery, a real USB disconnect, adapter firmware
  behaviour. The optional owner test (unplug and replug Board A's USB with the engine idling,
  parked, helpers armed) has not been run.
- Limits: other long-running services (vonstar, COP wake, the drive recorder, the web listeners)
  keep the previous `lib/can_operation_state.py` in memory until they next start; there is no
  dashboard indication beyond the existing restoration fault.

## Private environment context

Machine-specific environment/secret locations, local monitoring paths, and private compute-worker
instructions live in the ignored root `AGENTS.secret.md`. Read that companion when it exists. Never
print or commit secret values or a full VIN. Systemd does not inherit interactive-shell or cron
environments; configure an `EnvironmentFile=` only when a unit needs it and the user authorizes the
service change.

## AlfaOBD data provenance

- Read the canonical [`AlfaOBD evidence history`](alfaobd-evidence-history.md) before treating a
  selected profile, rendered value, catalog label, timeout, or output file as ground truth.
- `~/claude/shared-files/old.AlfaOBD_Debug.bin` is a large aggregate from the owner's previous 2015
  diesel ProMaster, not the current van. Treat its modules/DIDs only as same-family candidates to verify.
- `~/claude/shared-files/AlfaOBD logs and data July 8 2026/AlfaOBD_Debug.bin` and the RFH, adaptive-cruise,
  and engine info logs are from the current 2022 van. `BCDELPHI_Info.log` is cumulative and mixed:
  early snapshots include the 2015 van, while its 2026-06-22 tail time-aligns with current-van BCM
  commands. Use only timestamp-correlated records, never the file wholesale. Verify source VIN while
  decoding and keep unique VIN digits out of tracked output.
- AlfaOBD debug bins use ASCII hex representing bytes XORed with `0xFF`. The maintained pipeline is
  `tools/alfaobd_decode.py` → `projects/ecu_mapping/vin_scan.py` → `extract_did_map.py` →
  `reassemble_commands.py`; read `projects/ecu_mapping/README.md` first.
- The owner-authorized Android-tablet pull includes AlfaOBD 2.4.4.0's installed APK under the
  gitignored `tmp/ecu_mapping/android_tablet/` tree. Its 51 split database assets reconstruct into a
  valid SQLite catalog. `tools/alfaobd_apk_db.py` and `tools/alfaobd_catalog.py` reproduce the
  read-only extraction/export; the canonical interpretation and hashes are in
  `projects/ecu_mapping/findings/promaster_2022/2026-07-21_alfaobd_apk_catalog.md`. Raw numeric string
  placeholders must retain their namespace: whole-file indexing into the English resource is not a
  valid label decode. The September 22
  [loader investigation](../projects/ecu_mapping/findings/promaster_2022/2026-09-22_gear_and_oil_life_offline.md)
  establishes zero-based `J1` plain labels after `######`, separate from `K1` enum pairs before it.
  A recovered label still needs an exact request/decoder/variant join. Do not redistribute the APK/database.
- Earlier interpretation of BCM `27xx`/`2Axx` as SecurityAccess was wrong: they were ISO-TP consecutive
  frames within a long `2E 2023` PROXI write. The current-van capture contains successful `2F` IO-control
  operations but no verified `27` exchange. Verify all candidates live before replaying.

## Other migrated finding: sliding-door ajar input

The right sliding-door ajar plunger historically failed to depress on soft close, preventing fob lock
and causing wake cycles. Research identified circuit G76 (BK/VT), BCM C6 pin 20, in the contact board.
The FCA input uses an internal pull-up: closed is open-circuit, so an inline disconnect can simulate
closed after polarity is verified on-vehicle. Do not unplug the whole contact-board connector because it
also carries lock power. This bypass removes the unlatched-door warning/alarm/dome behavior; treat it as
a safety-affecting hardware modification, not a casual software fix. No ProMaster-specific OBD setting
was verified. Recheck OEM wiring before any physical work.

## Superseded external memories

Claude's old battery, radar, TPMS, B-CAN, and repo-layout memories were not copied verbatim because their
newest verified state is already tracked in `docs/bus-map.md`, project READMEs, project handoffs, findings,
and executable libraries. Those tracked sources win over older conclusions in the external memory files.
