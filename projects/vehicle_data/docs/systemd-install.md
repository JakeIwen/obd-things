# Telemetry systemd installation

Use this for unit-only updates on vanpi. Review the effective units with
`systemctl cat` first; repository edits do not update `/etc/systemd/system`.
Do not start, stop, or restart any CAN owner as an installation shortcut.

## Ordering and lifecycle

The enabled `van-telemetry.service` softly wants both web listeners and
`van-dtc-batch.path`. All three retain `PartOf=van-telemetry.service` so an
explicit broker stop/restart propagates. The Tailscale listener additionally
requires the path watcher; its guarded DTC worker still starts after the broker.

The path watcher must **not** have `After=van-telemetry.service`: its normal
`Before=paths.target` ordering precedes `basic.target`, whereas the broker
starts after `basic.target`. Watching a not-yet-created runtime directory is
safe; the triggered worker, not the watcher, needs the broker ready.
`Requires=` and `PartOf=` do not themselves add start ordering. There is no
need to disable the path unit's default startup/shutdown dependencies.

`WantedBy=multi-user.target` remains a valid optional enablement target for the
path, but is redundant when the broker is enabled. Leave the deployed path
**disabled** (as verified 2026-10-07); the broker's `Wants=` starts it anyway.
Do not enable either web listener independently. No enable/disable operation
is needed for this update.

## LAN address readiness

The base LAN/loopback web unit wants and starts after `network-online.target`.
On vanpi, read-only inspection on 2026-10-07 found:

- `NetworkManager-wait-online.service` enabled, successfully completed, and
  wanted by `network-online.target`; networkd's waiter is disabled.
- `eth0` uses NetworkManager's autoconnecting `Wired connection 1` profile with
  `ipv4.method=auto` and no configured static IPv4 address. DHCP from
  `192.168.6.1` supplied `192.168.6.103/24`. The client lease does not prove a
  server-side DHCP reservation; confirm that separately before changing DHCP.
- Both IP families have `may-fail=yes`. `nm-online -s` waits for startup to
  settle, not for this particular address, and its waiter can time out. In the
  inspected boot the lease arrived at 16:08:52 and wait-online completed at
  16:08:54, after the un-ordered web process had failed at 16:08:48.

Install the optional `systemd/van-telemetry-web.service.d/20-wait-lan.conf`
**only alongside vanpi's existing `10-lan.conf`**. It waits for the exact
`192.168.6.103` IPv4 address on `eth0` before invoking Python. Keep its address
in sync with `10-lan.conf`'s `--bind` argument. It neither binds a wildcard nor
changes NetworkManager, DHCP, routes, interface state, or application code.
The 90-second start timeout bounds an unavailable-network attempt; the existing
five-second `Restart=on-failure` retries it. `AF_NETLINK` is added to the
existing address-family allowlist for the read-only `ip address show` command;
`AF_CAN` remains excluded, and all other sandbox settings remain unchanged.
This is a startup gate, not a guarantee against later address removal.

## Install the reviewed files

After the reviewed revision has been delivered to the Pi, run these commands
from an owner-authorized shell. They reload definitions only, with **no restart**:

```sh
cd /home/pi/dev/obd-things/projects/vehicle_data/systemd
units=/etc/systemd/system
sudo install -m 0644 van-dtc-batch.path "$units/"
sudo install -m 0644 van-telemetry-web.service "$units/"
sudo install -d -m 0755 "$units/van-telemetry-web.service.d"
cd van-telemetry-web.service.d
sudo install -m 0644 20-wait-lan.conf "$units/van-telemetry-web.service.d/"
sudo systemctl daemon-reload
```

Preserve the machine-local `10-lan.conf` and Tailscale environment file.
No drop-in removal is required. The installed broker `20-web-stack.conf`
contains only `Wants=van-telemetry-web-tailscale.service`, and Tailscale's
`20-lifecycle.conf` contains only `PartOf=van-telemetry.service`: both duplicate
tracked directives and may be left in place. Review their current contents
before any optional removal; never remove the containing drop-in directories.

A definition reload does not launch a missed unit or apply `ExecStartPre` to
an already-running process. The next normal boot uses the new ordering.
If immediate activation is separately authorized, wait until parked and asleep
and use **only** the existing guarded restart helper (do not bypass its checks):

```sh
cd /home/pi/dev/obd-things
bash tmp/vehicle_data/restart-broker-parked.sh
```

## Verify without activating units

`systemd-analyze verify --man=no multi-user.target default.target` checks the
boot transactions without running service commands. Check its output, **not
just its exit code**: Bookworm systemd 252 can log a deleted ordering-cycle job
and still exit zero. Verifying individual telemetry units alone can miss this
boot cycle.

For a pre-install comparison, copy the installed unit fragments and their
`.d` directories into two temporary directories. Change only the candidate
copies, retaining all installed drop-ins. Prepend each directory to the
complete output of `systemd-analyze unit-paths` in `SYSTEMD_UNIT_PATH` and
verify `multi-user.target default.target` with `--generators=no --man=no`.
Keep the remaining host search paths, including generated mount dependencies
and the installed target `.wants` links. The current copies must reproduce
the cycle; the candidate copies must report no cycle or deleted job. This is
a graph/syntax check, not a simulated DHCP boot or a live restart test.

On the next normal boot:

```sh
journalctl -b | grep -i "ordering cycle"
systemctl is-active van-telemetry.service van-dtc-batch.path
systemctl is-active van-telemetry-web.service van-telemetry-web-tailscale.service
systemctl show van-telemetry-web.service -p NRestarts -p Result
journalctl -b -u van-telemetry-web.service --no-pager
```

Expect no ordering-cycle messages (grep exits 1 for no match), all four units
active (the path waiting), no bind exception, and LAN `NRestarts=0` following
ordinary successful address assignment. The DTC worker remains inactive until
an authorized request; never trigger a diagnostic job merely to test a watcher.
If activation fails, preserve logs and the effective units before changes;
do not restore the known cyclic path definition or bypass the parked gate.
