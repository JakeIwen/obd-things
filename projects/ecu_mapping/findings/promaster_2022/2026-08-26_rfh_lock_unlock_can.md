# 2026-08-26 fob lock/unlock application-frame mapping

## Result

A controlled, simultaneous three-bus campaign isolated one C-CAN application
frame whose action payload changes exactly with the owner's fob buttons:

| action | C-CAN ID | bytes 0-5 | observed counter / CRC examples |
|---|---:|---|---|
| lock all | `0x1EF` | `42 04 00 00 10 10` | `01 DA` |
| front unlock | `0x1EF` | `42 04 00 00 11 E0` | `01 B7`, independently repeated as `0E 0C` |
| rear/cargo unlock | `0x1EF` | `42 04 00 00 11 F0` | `0B D1` |

The ordinary awake-network frame is `42 00 00 00 00 00 CC XX`, where
`CC & 0x0F` rolls `0..15`. An action replaces bytes 1 and 4-5 in exactly one
50 Hz frame while retaining the current counter:

- byte 0: constant `0x42` in every observation;
- byte 1: `0x04` only in the controlled action frame, otherwise `0x00`;
- bytes 2-3: `00 00` in these observations;
- byte 4: `0x10` for lock, `0x11` for unlock;
- byte 5: `0x10` lock-all, `0xE0` front-unlock, `0xF0` rear/cargo-unlock;
- byte 6 low nibble: rolling `0..15` counter; high nibble was zero; and
- byte 7: CRC-8/SAE-J1850 over bytes 0-6 (poly `0x1D`, init `0xFF`, xorout
  `0xFF`, non-reflected).

The exact 2022 OEM power-lock description says a valid RF-Hub transmitter
input causes electronic request messages to the BCM over CAN. That architecture
and the controlled timing make this a strong RF-Hub-result/application-command
association. Electrical source attribution has not been independently proven,
so do not claim transmitter identity from CAN ID alone.

## Provenance

All live evidence was receive-only. `tools/three_bus_capture.py` held shared
serial/`dev_id` role leases for C-CAN, B-CAN, and CAN-CH; every role remained
classical CAN, listen-only, `restart-ms 0`, and ERROR-ACTIVE. The Pi sent no
frame and changed no interface or service during these captures.

### Standalone front unlock from asleep

- session:
  `tmp/captures/internal_can_unlock/session_20260826T230125.171044Z_1104155/`
- C-CAN SHA-256:
  `5b13b7277a94b81eb5e95aa5b47ba2fc86a9433aae57018ae430e33a55c493a2`
- event onset: epoch `1787785324.489826`
- action frame: epoch `1787785324.550258`,
  `1EF#4204000011E001B7`
- offline summary job: `20260826T230503Z-5ec28c30`
- exact-window job: `20260826T232828Z-8a623261`

### Corrected lock / front unlock / rear unlock sequence

- session:
  `tmp/captures/internal_can_unlock/session_20260826T232413.160899Z_1129091/`
- C-CAN SHA-256:
  `ea61c01169a32712c7cfff836b2f102787bd263c6d42eeabb15e476a18360fa7`
- B-CAN SHA-256:
  `59ffe03581d46cbcc94b4c23f9c5aba8f598b91d12fedfd6130d19183e43758c`
- all doors were owner-confirmed closed; starting state was unlocked;
- lock action frame: epoch `1787786663.667679`,
  `1EF#42040000101001DA`;
- front-unlock action frame: epoch `1787786670.007758`,
  `1EF#4204000011E00E0C`;
- rear/cargo-unlock action frame: epoch `1787786676.347903`,
  `1EF#4204000011F00BD1`; and
- exact C-CAN windows: lock `20260826T232602Z-c9723f3b`, front
  `20260826T232602Z-4e066713`, cargo `20260826T232602Z-c315e1f2`.

The vehicle later performed its normal automatic re-lock because every door
remained closed after fob unlock. That occurred after the recorder stopped.
A live audit showed C-CAN and CAN-CH TX counters still zero, B-CAN's historical
TX counter unchanged, the COP wake count still zero, and no active capture or
diagnostic process. It is a vehicle behavior, not replay evidence.

An earlier sequence with a door open is retained as a failed-lock control at
`tmp/captures/internal_can_unlock/session_20260826T231503.552473Z_1118835/`;
do not use its B-CAN state as a successful lock transition.

## Counter and checksum validation

`tools/can_event_window.py --audit-crc8-j1850` checked all 12,986 saved
`0x1EF` frames across the standalone-unlock, failed-lock-control, and corrected
sessions (compute job `20260826T233107Z-2181e3f5`). It found:

- all four controlled command frames passed;
- 12,983/12,986 total frames passed; and
- the only three mismatches were the first one or two ordinary frames at a
  network-wake boundary.

The wake-boundary exceptions mean a future sender must not derive its next
counter/checksum from the very first observed `0x1EF`. Require a short streak
of consecutive ordinary frames with valid CRC and sequential low-nibble
counters before constructing an action frame.

The CRC formula also reproduces each controlled action exactly:

```text
CRC8_SAE_J1850(42 04 00 00 10 10 01) = DA
CRC8_SAE_J1850(42 04 00 00 11 E0 01) = B7
CRC8_SAE_J1850(42 04 00 00 11 E0 0E) = 0C
CRC8_SAE_J1850(42 04 00 00 11 F0 0B) = D1
```

CRC and a four-bit alive counter provide error/freshness detection, not
cryptographic authentication.

## Ruled-out lead

The 29-bit `1E340041` stream emits a brief `0x88/0x08` burst for lock, front
unlock, and cargo unlock alike. It is generic event/network-management traffic,
not the action selector. Likewise, C-CAN `1E340000` byte 3 `0x20` occurred for
lock and front unlock but not cargo; it identifies neither direction nor the
complete command.

## Live one-frame front-unlock proof

The owner separately authorized exactly one dynamically counter-correct front
unlock after reviewing the template. Conditions were parked, ignition off,
engine off, doors locked, with the owner inside holding the fob as recovery.
No fob or door control was operated during the proof.

`projects/ecu_mapping/rke_front_unlock.py` retained the fixed C-CAN wake's
exclusive handoff/role/channel ownership through application synchronization.
It observed three consecutive ordinary CRC-valid counters `2 -> 3 -> 4`, then
sent exactly one frame using counter `5`:

```text
1EF#4204000011E005C3
```

`C3` is the exact CRC-8/SAE-J1850 of `42 04 00 00 11 E0 05`. The owner
immediately confirmed that the front doors unlocked and that the fob was not
pressed. The machine report is
`tmp/ecu_mapping/rke_front_unlock/proof_20260826T234015Z.json`.

The complete C-CAN transaction added 12 TX packets: ten fixed one-shot RF-Hub
wake attempts, one normal acknowledged `22 FEFF` validation read, and one
application unlock frame. Cleanup restored C-CAN to classical listen-only,
ONE-SHOT off, `restart-ms 0`, and ERROR-ACTIVE with zero error counters and no
inhibit. B-CAN and CAN-CH remained listen-only with unchanged TX counts.

A separate ten-second receive-only post-proof capture is at
`tmp/captures/internal_can_unlock/post_replay/session_20260826T234037.861753Z_1148186/`.
Its B-CAN log SHA-256 is
`3df25d3100cb209186e0347b0d7dc7f43e2b33314ad4929244ae550bf5d3d990`.
Offline job `20260826T234109Z-46311423` found `0x46C =
00 20 6F B2 52 C0 00 00` and `0x5B2 = 00 00 13 10 01 00 00 00`,
corroborating the front-unlocked/cargo-still-locked result.

The guarded tool is dry-run by default, fixes every action/routing detail,
requires explicit parked/ignition/engine/front-only/recovery confirmations,
rejects ignition or running witnesses during synchronization, contains one
`send()` call, and restores passively in protected cleanup. Portable suite job
`20260826T233756Z-3be2f69f` passed 1,055 tests, 4 skipped, and 692 subtests.

## Safety boundary and next step

The one-frame front-unlock proof succeeded. Any production integration must
retain the proof tool's following constraints:

1. use the exact serial-resolved C-CAN role and 6/14 pair;
2. require parked, ignition-off, engine-off state, explicit owner confirmation,
   an accessible recovery key, and protection against lockout;
3. wake C-CAN only through the existing fixed RF-Hub profile while holding the
   same exclusive ownership—never through a remembered `canN`;
4. after wake, require consecutive CRC-valid `0x1EF` frames and sequential
   counters, then create exactly one `42 04 00 00 11 E0 CC CRC` frame for the
   next counter;
5. transmit once at a bounded offset after an observed ordinary frame, never
   flood or replay an old counter/checksum verbatim;
6. verify the physical result and B-CAN lock feedback; and
7. restore and verify the exact passive baseline before releasing ownership,
   latching the normal inhibit if restoration is unprovable.

The proof intentionally uses a narrowly scoped project composite around the
fixed wake internals. Do not generalize it into a public arbitrary post-wake
send callback or expose caller-selectable payloads.

## Vonstar service/dashboard design

The user-facing three-action system is named **Vonstar**. The design is:

```text
van-dashboard -> /run/vonstar/api.sock -> vonstar.service -> fixed CAN action
```

- `projects/ecu_mapping/vonstar_service.py` is a serialized Unix HTTP server.
- `GET /v1/status` returns availability, the fixed action catalog, cooldown,
  busy state, and last result.
- `POST /v1/actions/{lock_all|unlock_front|unlock_cargo}` accepts exactly one
  `request_id`; no bus, interface, identifier, payload, counter, CRC, timing,
  or retry option is accepted.
- `POST /v1/access-state` accepts the same exact `request_id` body and performs
  at most one fixed C-CAN wake. Under that same held session it makes exactly
  one no-retry BCM `22 0130` door-input read, then observes B-CAN
  `0x46C/0x5B2/0x5E2` and C-CAN `0x419/0x4B1` passively. It returns the complete
  bounded access-state document in one response; it never performs a per-field
  wake or a second wake when one network remains silent.
- Duplicate request IDs are idempotent, a global three-second cooldown applies,
  each request calls the guarded one-frame composite once, and no automatic
  retry occurs.
- Audit records append below `/var/lib/vonstar/events.jsonl`.
- The tracked `projects/ecu_mapping/vonstar.service` creates private runtime
  and state directories and explicitly enables the three fixed actions.
- The port-8788 dashboard sources use
  `/home/pi/scripts/python-automation/van_dashboard_vonstar.py` as an
  intent-only Unix client. The dashboard never opens CAN, derives counters,
  computes CRCs, or sees a netdev.
- The dashboard exposes the aggregate read as
  `POST /api/vonstar/access-state` with no request body; its private Unix client
  creates the request ID.

The dashboard tile offers **Lock All**, **Unlock Front**, and **Unlock Cargo**
with an ordinary confirmation dialog. Existing same-origin mutation protection
and the user's LAN/Tailscale trust boundary are the selected authentication
policy; no extra PIN is required.

The design was staged on 2026-08-26 and deployed on 2026-08-27. The tracked
unit is installed, enabled, and active; the dashboard unit now wants/starts
after Vonstar and checks its private client before launch. The dashboard was
restarted and both private Unix and loopback proxy status endpoints returned
execute-mode availability plus the exact three-action catalog. Installation
and startup added zero CAN TX on every role. Both services remained active with
zero restarts; all CAN roles remained exact passive/error-free with no inhibit.
No action or state-read endpoint was invoked during deployment smoke. Front
unlock is live-verified. Lock-all and cargo-unlock are mapped from controlled
fob captures but remain explicitly labeled `mapped_capture` pending separate
live replay validation.

## Verified front/cargo state feedback

Offline exact-window comparison of the corrected closed-door B-CAN capture
found stable `0x5E2` byte-1 values across the three action states:

| controlled state | `0x46C` bytes4-5 | `0x5E2` byte1 |
|---|---|---:|
| all locked | `53 00` | `02` |
| front unlocked, cargo locked | `52 C0` | `06` |
| front and cargo unlocked | `52 C0` | `00` |

Compute windows were `20260827T001258Z-951143e9` (locked),
`20260827T001258Z-8c9dc0f2` (front only), and
`20260827T001258Z-7f28fcb8` (all unlocked). `0x46C` discriminated the
locked-to-front-unlocked transition but not the later cargo unlock; `0x5E2`
discriminated both transitions.

This initially supported the Vonstar result enum (`locked`,
`front_unlocked_cargo_locked`, `front_and_cargo_unlocked`) as a
single-sequence candidate.

An independent receive-only session on 2026-08-27 local time (2026-08-28 UTC)
promoted `0x5E2` byte1 to verified exact-vehicle front/cargo-domain feedback.
The session is
`tmp/captures/internal_can_unlock/lock_state_repeat/session_20260828T012754.971373Z_2868003/`.
All three roles used their serial/dev-id-resolved shared passive leases; each
recorded one chunk with zero drops, zero retries, and no interface loss. The
B-CAN and C-CAN source hashes are respectively
`bd475e869917e2372d3502b428db9520a0d8b53d328d4c0edcba0fa3c3f92398`
and `191aff25a2dc2ffbf7712299ab1c7822b7254c6e4dca62484851cb9bb9c2c5be`.

The owner physically verified three complete lock → front-only → front+cargo
cycles. Exact `0x5E2` edges were:

| cycle | locked `02` | front-only `06` | front+cargo `00` |
|---|---:|---:|---:|
| 1 | `1787880579.301138` | `1787880622.560659` | `1787880671.104535` |
| 2 | `1787880717.059605` | `1787880770.819126` | `1787880798.843205` |
| 3 | `1787880827.338542` | `1787880848.718343` | `1787880892.449384` |

A driver-door-open lock attempt then failed physically. No `0x1EF` lock-all
application frame appeared and `0x5E2` remained `00`. After the driver door was
fully latched, successful lock-all `1EF#4204000010100C5B` appeared at
`1787880994.179084` and `0x5E2` returned to `02` at `1787880994.187168`.
This negative control distinguishes accepted/resulting lock-domain state from
a raw button request.

Bounded compute jobs `20260828T013715Z-59a86cfc` and
`20260828T013716Z-16ef1714` extracted 1,068 relevant B-CAN frames and 13,151
`0x1EF` frames. `0x5E2` used only the three declared values: 43 locked, 120
front-only, and 110 front+cargo samples. The `0x1EF` audit found seven CRC
mismatches, all ordinary wake-boundary frames immediately before reviewed
actions; every action frame passed.

The same repeat disproved the earlier `0x46C`/`0x5B2` lock interpretations.
`0x46C` variants changed with the embedded voltage/load/status context and did
not uniquely distinguish cargo state. `0x5B2` byte3 stayed `0x10` through all
three lock domains and later changed to `0x0C` around the driver-door
close/final-lock interval. Both remain useful raw observations but are not
lock-state sources.

`0x5E2` still does not establish individual driver, passenger, sliding-door,
or rear-door lock booleans. Door-ajar inputs are separate signals and must not
be substituted for lock state. The sliding-door ajar circuit on this vehicle
has been modified to permanently report closed, so its physical ajar state is
not observable through the factory input and must never be presented as a
confirmed closed-door state.

The aggregate access-state response publishes that enum with
quality `verified` and always includes `driver`, `passenger`,
`sliding`, and `rear` objects. Individual `locked` values stay JSON `null`.
Passenger/rear `ajar` remain unmapped; sliding `ajar` is `null` with
`hardware_bypass_forced_closed`, `reported_closed=true`, and
`physical_state_observable=false`. Driver `ajar` uses the BCM `0130` inverted
`0x04` mask with quality `candidate_one_controlled_trial`. The raw DID response, fixed-ID
summaries, sample errors, wake/request counts, and limitations are returned in
the same document so future decoder improvements do not need a new wake
endpoint. The implementation is live-validated under the conditions in the
next section and is now available through the deployed Unix service/dashboard
proxy.

### Live aggregate access-state validation

One separately authorized parked validation completed on 2026-08-27 local
time (2026-08-28 UTC) from owner-confirmed all-locked state after passive
three-second probes proved both C-CAN and B-CAN silent. Before the live call,
the B-CAN sample window was increased to 2.5 seconds and the decoder was
hardened to require at least two identical `0x5E2` samples; an insufficient or
mixed sample now returns unknown and never triggers another wake.

The one call returned:

- three identical B-CAN `5E2#00020000` frames and verified state `locked`;
- five identical `0x46C` and two identical raw `0x5B2` observations;
- exact positive BCM response `62 01 30 AC`, with the controlled driver-door
  mask reporting closed;
- stable raw C-CAN `0x419` and `0x4B1` closed-state candidates; and
- the sliding-door field as physically unobservable with the hardware-bypass
  quality marker.

C-CAN TX increased by exactly 12 packets: ten fixed ONE-SHOT RF-Hub wake
attempts, one acknowledged `22 FEFF` validation, and one no-retry BCM `22 0130`
read. B-CAN and CAN-CH TX deltas were zero. No `0x1EF` action was sent. Every
role restored to its exact listen-only, ONE-SHOT-off, `restart-ms 0`,
ERROR-ACTIVE baseline with zero current errors and no inhibit. The machine
report is
`tmp/ecu_mapping/rke_front_unlock/access_state_validation_20260828T014808Z.json`
(SHA-256
`a0330c5c55ee387753b3a65e08cff5258ca4630d316bacac7f0ffbbbac33551a`).

A second separately authorized run followed another passively proven C/B sleep
cycle. It reproduced every bounded result: three identical
`5E2#00020000` samples, exact BCM `62 01 30 AC`, the same stable `0x419` and
`0x4B1` raw candidates, verified `locked`, C-CAN TX delta 12, and zero B-CAN
or CAN-CH TX. Post-audit again found exact passive/error-free roles and no
inhibit. Its report is
`tmp/ecu_mapping/rke_front_unlock/access_state_validation_20260828T015200Z.json`
(SHA-256
`dfb006ee450c589e84058b6bbd062bc17ea630aa29e8f31d997e80b217b6e1f7`).

These two independent silent-start proofs resolve end-to-end repeatability for
the tested all-locked state. They do not yet prove acquisition in the other two
lock domains or quantify long-term failure probability.
