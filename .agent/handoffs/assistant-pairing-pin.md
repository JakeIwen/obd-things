# Assistant pairing PIN

tier: T3 (adds the first unauthenticated route through the assistant relay on both web listeners and
the advisor; changes an authentication gate)
last reviewed: plan reviewed by gpt-6-astra 2026-11-17 (ship); implementation not yet reviewed
written against: `ce71087` (master `0393eee` plus four early-warning commits); built on
`9209c9e` in branch `worktree-agent-a08cb912082166e3f`

## Objective

The warning chat and DTC research sit behind the advisor's 43-character access code
(`secrets.token_urlsafe(32)`), which the owner pastes into a dialog once per browser origin. His
driving tablet (Android, portrait 800x1280, on-screen keyboard, no shared clipboard) cannot receive
it. Owner request, confirmed 2026-11-17: a browser that is already paired shows a one-time 6-digit
PIN valid for 5 minutes; the new device types it once and is paired; Wi-Fi guests on the van LAN
stay out (attempts limited per PIN and per time window, each PIN single-use).

## Current facts the design rests on

- `web.py` `TelemetryWebHandler._assistant_request` (about lines 251-287) is the relay. In order:
  503 if the listener has no advisor socket; 404 unless the method and path are on its allowlist
  (warning-chat routes inline, research routes via `dtc_research_contract.relay_allowed`); 403
  unless `http://<Host>` is in `server.warning_chat_origins`, `Sec-Fetch-Site` is absent or
  `same-origin`, and `Origin` is absent or equal (required on POST); 401 unless
  `X-Van-Assistant-Key` and `X-Van-Chat-Client` both match `[A-Za-z0-9_-]{32,128}`; 415 unless
  POST is `application/json`; 400 on a bad body (limit `warning_chat.MAX_BODY`, 32 KiB); then
  relays method, path (query already stripped), body and exactly those two headers to the advisor
  socket. Every reply carries `Cache-Control: no-store` (`_common_headers`). The listener's
  `log_message` prints client IP and the request line only (no headers, no body). No `do_OPTIONS`
  exists, so a CORS preflight is never granted.
- The listeners: LAN `192.168.6.103:8765` (machine-local drop-in, plain HTTP, reachable by every
  van Wi-Fi client including guests); Tailscale `100.82.91.76:8765`, which also trusts
  `http://vanpi:8765` through its drop-in. Both relay to the same advisor.
- `warning_chat.py` (901 lines, over the 800-line limit): `WarningChatManager.__init__` creates or
  loads `<state>/access-code` (0600, `StateDirectory` 0700) into `manager.token`;
  `authorize(token, browser)` uses `hmac.compare_digest` and returns `sha256(browser)` as the chat
  owner. `ChatHandler.handle_request` calls `authorize` first for every route, then reads the
  body, then dispatches research paths (outside `manager.lock`) or chat paths (inside it).
  `ChatHandler.log_message` is silent. `ChatServer` is `ThreadingMixIn`, so requests run
  concurrently. The unit runs with `MemoryMax=768M`, `CPUQuota=30%`, `Restart=on-failure`.
- Dashboard: `src/assistantAccess.js` (pure: storage under `van-warning-chat.`, the `access` and
  `client` keys, `ACCESS_TEXT`, `requestHeaders`, `buildRequest`); `components/AssistantPairing.jsx`
  (the paste form both dialogs render while unpaired); `WarningChat.helpers.js` and
  `DtcResearch.helpers.js` each have `pair(code)`, which validates with `isValidAccessCode`, stores
  `access` and connects. A 401 anywhere clears `access` and shows the form again. Both dialogs share
  the stored code, so pairing one pairs both.
- Chats and saved custom warnings are owned per browser client id (`X-Van-Chat-Client`); research
  runs are shared by every code holder. A newly paired tablet therefore starts with its own chat
  history and its own saved-warning list; that is unchanged by this plan.

## Design

### Credential: the redeem hands over the existing access code

Decided: a successful redeem returns the advisor's access code, and the tablet stores it exactly as
if pasted. Reasons:

- The single-code model stays the only credential `authorize` knows. A per-device credential
  would need a persistent device registry, a second comparison path inside the one auth gate, and
  a revocation UI or CLI: more new attack surface than the feature it protects.
- It adds no new exposure class. Every assistant request already carries the code in clear HTTP on
  the LAN; one more transit in the redeem reply is the same exposure.
- Revocation stays what it is: rotate the code (runbook below), then re-pair each device, now with
  one paste on the Mac and PINs everywhere else. Per-device revocation is not offered; if the owner
  later wants it, a device-credential layer can be added behind the same routes.

### Routes (one contract file, `projects/vehicle_data/assistant_pairing.json`)

| Route | Auth | Body | Reply |
| --- | --- | --- | --- |
| `GET /v1/assistant/pairing` | code + client (unchanged gate) | none | `{available: true, pin_id, state, expires_in_s, locked_for_s}` |
| `POST /v1/assistant/pairing` | code + client | exactly `{action: "create"}` | `{available: true, pin, pin_id, expires_in_s}`; 429 while locked |
| `POST /v1/assistant/pairing` | code + client | exactly `{action: "cancel", pin_id}` | the status object; cancels only when `pin_id` is the active PIN; idempotent |
| `POST /v1/assistant/pairing/redeem` | **none** (the new path) | exactly `{pin: "<6 ASCII digits>"}` | 200 `{available: true, access_code}`; 403 `pin_rejected`; 429 `pairing_locked` + `retry_after_s`; 400 `invalid_request` |

- `state` is one of `none` (nothing created since the advisor started), `active`, `redeemed`,
  `expired`, `wrong_tries` (burned by the per-PIN limit or a lockout), `cancelled`. `pin_id` is
  `secrets.token_hex(8)`, given only to code holders, never the PIN; a panel whose `pin_id` differs
  from the status's knows its PIN was replaced.
- The PIN travels only in a request or reply body, never a path or query string, so neither the
  web listener's request-line log nor `assistant_http.log_client_gone` (which logs the path) can
  carry it.
- Contract content (the one definition of paths and numbers; Python loads it, the dashboard imports
  it with `with { type: "json" }` like `dtc_research.json`):
  `routes {owner, redeem}`, `actions {create, cancel}`, `states {...}`,
  `reasons {rejected: "pin_rejected", locked: "pairing_locked", invalid: "invalid_request"}`,
  `pinDigits 6`, `pinLifetimeSeconds 300`, `maxAttemptsPerPin 5`, `windowFailureLimit 10`,
  `windowSeconds 900`, `redeemBodyMaxBytes 64`, `statusPollSeconds 2`, `pinIdPattern`.

### Relay change in `web.py` (exact shape)

`_assistant_request` gains one computed flag and one branch; nothing else moves. The existing
allowlist expression and dispatch are not rewritten: two lines are added after the expression, so a
parallel change (the early-warning catalog and rule-save routes) can add its routes to that
expression in either merge order and they stay authenticated:

```text
allowed = <existing allowlist, unchanged>
redeem  = method == "POST" and path == PAIRING_REDEEM_PATH          # exact string equality (added)
allowed = allowed or redeem or pairing_relay_allowed(method, path)  # owner route: GET, POST (added)
404 unless allowed                                   (unchanged)
403 origin / Sec-Fetch-Site / Origin checks          (unchanged; apply to redeem too, so POST needs Origin)
if redeem:   headers = {}                            # no assistant header is forwarded, whatever the caller sent
else:        the existing header-shape check -> 401  (unchanged)
415 unless application/json                          (unchanged)
body limit = pairing.REDEEM_BODY_MAX if redeem else MAX_WARNING_CHAT_BYTES
relay                                                (unchanged)
```

`pairing.relay_allowed` matches only `GET` and `POST` on the exact owner path. Prefix or suffix
variants (`/v1/assistant/pairing/redeem/x`, `.../redeemx`, `GET .../redeem`) are 404. Every route
that answered 401 without the key before still does, at the web layer and again at the advisor.

### Advisor side

- New module `projects/vehicle_data/assistant_pairing.py` (imports only the standard library and
  `assistant_common`; nothing from the broker, the runner or `warning_chat`): loads the contract;
  exposes `OWNER_PATH`, `REDEEM_PATH`, `REDEEM_BODY_MAX`, `relay_allowed(method, path)`,
  `well_formed_pin(payload)`; and class `PairingPins(access_code, *, clock=time.monotonic,
  randbelow=secrets.randbelow, token_hex=secrets.token_hex, log=<stderr JSON line>)` with
  `create()`, `cancel(pin_id)`, `status()`, `redeem(payload)` and `handle(method, payload)` for the
  owner route, each returning `(http_status, reply)`. All state sits behind one `threading.Lock`
  (`PairingPins._lock`), separate from `manager.lock`, so a redeem flood never waits on or blocks a
  chat.
- `ChatHandler.handle_request` (built; `warning_chat.py` shrank from 901 to 899 lines): first, if
  `method == "POST" and self.path == REDEEM_PATH` (exact; nothing else is handled before
  `authorize`), read the body with the redeem limit and return
  `self.reply(*server.pairing.redeem(payload))`; this branch reads no header. A body that fails
  the size or JSON check raises `ChatError(400)` from `assistant_http.read_json_body` and never
  reaches `redeem`. All other routes keep `authorize` first. After `authorize`, the owner path
  dispatches to `server.pairing.handle(method, payload)` before `manager.lock`, like research.
  `main()` builds `PairingPins(manager.token)` and sets `server.pairing`. The body parse moved
  into `assistant_http.read_json_body` in its own tidying commit.
- Advisor restart drops the active PIN and the failure window (memory only). Accepted: a guest
  cannot restart the advisor, and the owner simply makes a new PIN.

### Abuse limits and arithmetic

- Generation: `f"{secrets.randbelow(10**6):06d}"` (leading zeros allowed; 10^6 values).
- One active PIN at a time. `create` replaces any active PIN (the old one becomes `cancelled`).
- Lifetime 300 s on `time.monotonic()` (wall-clock jumps from NTP or the van's clock cannot extend
  it). Expiry is checked inside the lock on every call.
- Single use: on a match the PIN is cleared (`state = redeemed`) before the lock is released, so of
  two simultaneous correct redeems exactly one gets the code.
- Comparison: `hmac.compare_digest(pin.encode(), active.encode())`, both 6 ASCII bytes after a
  `^[0-9]{6}$` format check.
- Per PIN: 5 failed redeems while it is active burn it (`wrong_tries`).
- Malformed requests (plan-review amendment): a request that fails transport or JSON parsing, or
  is not exactly `{pin: "<6 ASCII digits>"}`, is refused with its own status (403 origin and 415
  content type at the relay; 400 for a bad size, bad JSON or the wrong shape at either hop) and is
  **not counted**: it never reaches a comparison, so it gives no extra guesses and is no PIN-state
  oracle. The shape check runs before the lock check, so a malformed body is 400 even while locked.
- Global window: every well-formed redeem that does not return the code counts as one failure
  (wrong PIN, no PIN active, expired, burned), from any client id, origin or listener, because the
  advisor sees none of them and keeps one counter. At 10 failures in a rolling 900 s the advisor is
  locked: redeem answers 429 without comparing anything, `create` answers 429, and the active PIN is
  burned. Refusals while locked are not counted (the record is a `deque(maxlen=10)` of monotonic
  times, bounded memory), so the lock lifts when the oldest failure is 900 s old.
- No oracle, stated narrowly (plan-review amendment): wrong PIN, no PIN, expired and burned all
  return the identical 403 status and body `{available: false, reason: "pin_rejected", detail:
  <one fixed sentence>}`, with no attempts-left count and no prefix leakage (`hmac.compare_digest`
  on 6 bytes). With no PIN active the advisor compares against a dummy, so both cases do the same
  comparison work. Whole-request timing is not claimed constant: lock waits, per-PIN bookkeeping
  and log writes differ by microseconds, far below LAN jitter. The test proves the comparison is
  made exactly once per well-formed redeem in every case, not that timing is equal. The 429
  reveals only that 10 failures happened, which is true whether or not a PIN exists.
- Arithmetic. A guest who knows a PIN is active gets at most 5 guesses on it (fewer if the window
  already holds failures): P = 5 / 10^6 = 1 in 200,000. Creating new PINs gives no extra
  guesses, because the window caps all guessing at 10 per 15 minutes: at most 10 / 10^6 per
  15 minutes even if PINs were active continuously, 960 / 10^6 (about 0.1 %) per day in that
  unrealistic case. Realistically a PIN is active for a minute or two a few times a year, so the
  lifetime risk is on the order of 10^-5.
- Denial of service (accepted, documented): a guest who spends 10 guesses every 15 minutes keeps
  pairing locked. The owner still has the paste path, and the lock itself is the visible sign
  (the panel says pairing is paused after wrong tries, and the journal shows `locked`).

### Logging

`PairingPins` logs one JSON line per event to stderr: `{"event": "assistant_pairing", "action":
"created|redeemed|rejected|burned|locked|unlocked|cancelled|expired", "failures_in_window": n}`.
Never the PIN, the `pin_id` or the code. Refusals while locked and malformed requests are not logged
one by one (only the `locked` transition and the later `unlocked`), so a flood cannot fill the
journal. Detail strings are fixed constants. Qualification (plan-review amendment): pairing events
log action and counts only, but `web.py` still logs one request line per HTTP request (client
address, method, path, status), as it does for every route; that line is pre-existing and holds no
header or body, so no PIN or code.

### Exposure

- Both listeners offer create and redeem (the paired Mac may be on Tailscale while the tablet is on
  the LAN; one advisor, so one PIN and one failure window serve both). `--no-warning-chat` on a
  listener removes every assistant route there, pairing included.
- The same-origin gate applies to all four requests; redeem being POST must carry a matching
  `Origin`. `application/json` is required, which forces a preflight the listener never grants, so
  no cross-origin page can send it.
- Replies are `no-store` at both hops (web `_common_headers`, advisor `reply`).
- The PIN and the code never appear in a log line, an error detail, the journal or a test fixture
  (tests use synthetic values such as `"a" * 43`).

### UI

Strings go in `ACCESS_TEXT` (`assistantAccess.js`). Pure helpers go in `assistantAccess.js` (it stays
under 150 lines); the effectful panel controller goes in a new `src/assistantPairing.js`.

- Unpaired browser: `AssistantPairing.jsx` shows the PIN section first (the tablet's path), then the
  existing paste field under "Or paste the access code". PIN input: `type="text"`,
  `inputmode="numeric"`, `pattern="[0-9]*"`, `autocomplete="one-time-code"`, `maxLength={6}`,
  `enterkeyhint="go"`, a "Pair" button. On success the component calls a new prop `onCode(code)`;
  each dialog passes `(code) => ctrl.pair(code)`, so storage and reconnect stay in the controllers'
  existing `pair`. Rejected: "That PIN was not accepted. Make a new one on a connected browser." Locked:
  "Too many wrong PINs recently. Try again in N min, or paste the access code." The dialogs focus
  the PIN field (a new `pinRef`) instead of the code field.
- Paired browser: a new `components/PairAnotherDevice.jsx`, a collapsed `<details class="disc">`
  "Pair another device" near each dialog's footnote, rendered when the snapshot's new `paired` flag
  is true (`WarningChat`: key present and not pairing; `DtcResearch`: phase not `pairing`/`loading`).
  Opening it creates a PIN and shows it in large tabular digits grouped `042 917`, with a countdown
  (`4:59`) measured from `performance.now()` at reply receipt, the hint "Type this on the new device
  within 5 minutes. It works once.", and a "New PIN" button. While open and active it polls
  `GET /v1/assistant/pairing` every 2 s: `redeemed` shows "Device paired" and stops; `expired`,
  `wrong_tries` (with "A wrong PIN was tried; if that wasn't you, consider rotating the access code"),
  a different `pin_id` ("Replaced by a newer PIN") or `locked_for_s > 0` stop polling. Closing the
  disclosure or the dialog sends `cancel` best-effort. A 401 shows the existing pairing prompt text.
- Styles: new `src/styles/assistant-pairing.css` (`app.css` is at 790 lines) imported in `main.jsx`,
  using existing tokens only; PIN digits about 48 px, `font-variant-numeric: tabular-nums`.
- Screenshots: the collapsed disclosure appears in the paired chat and research dialogs. The demo
  harness needs no new route (collapsed sends nothing), but affected README shots must be retaken by
  the AGENTS.md procedure (look at each; full retake last).

## Files

Add:
- `projects/vehicle_data/assistant_pairing.json`, `projects/vehicle_data/assistant_pairing.py`
- `projects/vehicle_data/dashboard/src/assistantPairing.js` (requests: `createPin`, `cancelPin`,
  `pinStatus`, `redeemPin` with injected `fetch`; `createPairingPanel(deps)` controller with
  injected fetch, clock and timers)
- `projects/vehicle_data/dashboard/src/components/PairAnotherDevice.jsx`,
  `projects/vehicle_data/dashboard/src/styles/assistant-pairing.css`
- tests listed below

Change:
- `web.py` `_assistant_request` (relay shape above; import from `assistant_pairing`)
- `warning_chat.py` `ChatHandler.handle_request` and `main()`; `assistant_http.py` gains
  `read_json_body`
- `dashboard/src/assistantAccess.js` (strings; pure helpers `normalizePin`, `isValidPin`,
  `pinDisplay`, `countdownText`, `redeemRequest` (no assistant headers), `redeemOutcome`,
  `pairingStatusView`; `storedIdentity(storage)` returning `{key, client}` for the panel)
- `components/AssistantPairing.jsx`, `dialogs/WarningChat.jsx` / `.helpers.js` (`paired` flag only),
  `dialogs/DtcResearch.jsx` / `.helpers.js` (same), `main.jsx` (CSS import)

Docs:
- `projects/vehicle_data/docs/warning-chat.md`: a "Pairing another device" section as the contract
  (routes, limits, arithmetic, logging, the DoS note, threat model); the activation section gains the
  rotation runbook; the User experience list mentions PIN pairing.
- `projects/vehicle_data/docs/dtc-research.md`: the Routes heading notes that the one pairing redeem
  route is the only assistant route without the code (link to warning-chat.md).
- `projects/vehicle_data/README.md`: one line under the assistant paragraph.
- `AGENTS.md` map paragraph, after the DTC research paragraph:
  "Assistant pairing PIN (owner request 2026-11-17): a paired browser shows a one-time 6-digit PIN
  (5 min, single use) that a new device types to receive the access code. `assistant_pairing.py`
  with `assistant_pairing.json` (routes, limits, states shared with the dashboard) holds the PINs,
  limits and the relay allowlist; `POST /v1/assistant/pairing/redeem` is the only assistant route
  relayed without the code. Dashboard: `src/assistantAccess.js` (strings, pure helpers),
  `src/assistantPairing.js` (requests, panel controller), `components/AssistantPairing.jsx` (PIN
  field) and `components/PairAnotherDevice.jsx`. Contract: `docs/warning-chat.md` section
  'Pairing another device'."

## Tests

Python (`tests/test_assistant_pairing.py`, pure, fake clock and random):
- `test_pin_is_six_digits_from_secrets_and_one_active_at_a_time`
- `test_expiry_uses_the_monotonic_clock` (299.9 s accepted, 300 s rejected; `time.time` patched far
  ahead changes nothing)
- `test_pin_is_single_use`
- `test_concurrent_redeems_yield_exactly_one_code` (16 threads behind a `threading.Barrier`)
- `test_five_wrong_tries_burn_the_pin`
- `test_window_limit_spans_pins_clients_and_empty_attempts` (10 failures across two PINs and with
  no PIN active lock; correct PIN refused while locked; unlocks when the oldest is 900 s old;
  refusals while locked do not extend it)
- `test_lockout_refuses_create_and_burns_the_active_pin`
- `test_rejections_are_identical_for_wrong_none_expired_and_burned` (same status and body bytes)
- `test_comparison_is_constant_time_and_done_whether_or_not_a_pin_is_active` (wraps
  `hmac.compare_digest`: exactly one 6-byte comparison per well-formed redeem, none for a malformed
  one; it does not measure whole-request timing)
- `test_malformed_bodies_are_400_and_not_counted`, `test_owner_route_bodies_are_exact`,
  `test_default_log_is_one_json_line_on_stderr`
- `test_logs_never_carry_pin_pin_id_or_code` (capture stderr across a full scenario; no PIN, no
  `pin_id`, no code, no six-digit run)
- `test_contract_matches_module_constants`

Advisor (`tests/test_assistant_pairing_relay.py`, real `ChatServer` on a temp socket):
- `test_redeem_is_the_only_route_answered_without_the_key` (a table of every assistant route,
  research included, without the key: 401; redeem: 403 `pin_rejected`)
- `test_pairing_owner_route_requires_the_key_and_exact_bodies`

Web relay (`WarningChatWebTests`, mocked advisor client):
- `test_redeem_relay_forwards_no_assistant_headers` (caller-sent key and client are dropped)
- `test_redeem_keeps_the_origin_json_and_size_gates` (cross origin, cross-site, missing Origin,
  `text/plain`, 65-byte body: refused, client never called)
- `test_redeem_path_is_exact` (`GET`, `/redeem/x`, `/redeemx`: 404)
- `test_every_other_assistant_route_still_401_without_key`
- `test_relay_logs_no_pin_or_code` (capture the listener's stdout)

End to end (`tests/test_assistant_pairing_relay.py`, one real advisor behind two real web relays,
as the LAN and Tailscale listeners are): create on one relay with the key, redeem on the other
without it, receive the code once; the second redeem is 403; captured output of all three servers
holds no secret. Plan-review additions: `test_locked_pairing_leaves_authenticated_requests_working`
(while locked, chat status and research list answer 200 for the existing client and for a browser
that pastes the code with a new client id) and `test_two_relays_share_one_failure_window`.

Dashboard (`test/assistantPairing.test.mjs`): `normalizePin`/`isValidPin`, `pinDisplay`,
`countdownText`, `redeemRequest` exact body and no `X-Van-Assistant-Key`, `redeemOutcome` for 200
(valid and malformed code), 403, 429 (minutes rounded up), 503 and network failure,
`pairingStatusView` for each state and a replaced `pin_id`; the panel controller with fake timers
(create, countdown, poll stops on `redeemed`, cancel on close, no poll after expiry).
`WarningChat.helpers.test.mjs` and `DtcResearch.helpers.test.mjs`: the `paired` flag, and that a
code delivered through `pair` is stored and connects.

Run the whole suite (`run_tests.py`; on vanpi by the AGENTS.md route). The change set spans Python
and the dashboard, so no scoped gate covers it.

## Deploy and rollback

Units: `van-telemetry-advisor`, `van-telemetry-web`, `van-telemetry-web-tailscale`, then the
dashboard build. **No broker restart**: the broker imports none of the changed modules. Confirm
before deploying with
`python3 -X importtime -c "import projects.vehicle_data.broker" 2>&1 | grep -c -e assistant_pairing -e warning_chat -e assistant_http`
(expect 0). The web units are `PartOf=van-telemetry.service`, which propagates broker restarts to
them, not the reverse. The research worker (`dtc_research_worker.py`) imports `assistant_http`
but only gains an unused function there, so it does not restart; it picks the file up at its next
ordinary restart.

Order (after the review's `ship`, the merge to master and a green whole suite):
1. Check no assistant turn is running (no `codex` or `claude` child in
   `systemctl status van-telemetry-advisor`, and the research worker's `/status` `job` is null);
   a restart marks a running chat turn interrupted.
2. `sudo systemctl restart van-telemetry-advisor` (old web units do not relay the new routes yet:
   harmless 404).
3. `sudo systemctl restart van-telemetry-web van-telemetry-web-tailscale` (parked preferred; web
   restarts send nothing on CAN; an open tablet reconnects its stream).
4. Dashboard build (`npm ci && npm run build` in `dashboard/`).

Live checks that print neither the code nor a PIN:
- `systemctl is-active` for the three units; their journals since the restart hold no traceback.
- Before any probe, make sure no PIN is active (`000000` is a valid possible PIN): no "Pair another
  device" panel is open anywhere, and the advisor was just restarted (memory only, so no PIN).
- Repeat every check below through both listeners: LAN `http://192.168.6.103:8765` and Tailscale
  under its trusted name `http://vanpi:8765` (run there from a tailnet device, with
  `-H 'Origin: http://vanpi:8765'`), plus one cross-origin probe per listener
  (`-H 'Origin: http://evil.example'` on the redeem: 403).
- Without the key, each answers 401: `curl -sS -m 5 -o /dev/null -w '%{http_code}\n' -H 'Origin: http://192.168.6.103:8765' http://192.168.6.103:8765/v1/assistant/status`, and the same `POST /v1/assistant/pairing` with `-H 'Content-Type: application/json' --data '{"action":"create"}'`.
- One redeem with a wrong PIN answers 403 (it spends 1 of the 10 window failures):
  `curl -sS -m 5 -o /dev/null -w '%{http_code}\n' -H 'Origin: http://192.168.6.103:8765' -H 'Content-Type: application/json' --data '{"pin":"000000"}' http://192.168.6.103:8765/v1/assistant/pairing/redeem`
  (`-o /dev/null`, so even an improbable match prints nothing).
- `journalctl -u van-telemetry-advisor --since '<restart time>' -o cat | grep -c -E '"pin"|access_code'` is 0, and the `assistant_pairing` lines show only `event`, `action`, `failures_in_window`.
- Owner step: on the Mac open a dialog, Pair another device, type the PIN on the tablet; the tablet
  connects and the Mac panel shows "Device paired". Until that passes, report the feature as
  deployed, not operational.

Rollback: revert the commits on master, restart the advisor and both web units, rebuild the
dashboard. Stored codes stay valid (the code itself never changed), so no device must re-pair.

Code rotation (also the response to a suspected leak, now documented in warning-chat.md):
`sudo systemctl stop van-telemetry-advisor && sudo rm -f /var/lib/van-telemetry-advisor/access-code && sudo systemctl start van-telemetry-advisor`;
every browser then gets 401 and re-pairs (paste on one, PINs on the rest).

## Threat model

- **LAN guest (on the van Wi-Fi, curl or a browser).** Before: cannot use the assistant without the
  code; an active attacker on the LAN (ARP spoofing, or a WPA2 client that captured another's
  handshake) can already read the code from any assistant request, since HTTP is cleartext. After:
  can call redeem, can forge Host and Origin from curl, so only the PIN and the limits stand in the
  way: at most 5 guesses on a PIN he knows is active (1 in 200,000), 10 guesses per 15 minutes
  overall. Can lock pairing (DoS) and can see in the 429 that someone failed 10 times. Cannot reach
  any other assistant route without the key, cannot learn whether a PIN is active from a rejection.
  A passive sniffer learns nothing new (the code already crosses on every request). Someone who
  reads the PIN off the owner's screen and types it first gets the code; the tablet then sees a
  rejection while the Mac panel says "paired", which is the cue to rotate.
- **Tailscale peer.** Same as a LAN guest on the Tailscale listener, over an encrypted path; the
  tailnet is the owner's devices. Shares the one failure window, so it cannot add guesses.
- **Hostile web page (cross-origin).** Before and after: cannot send the JSON POST (preflight never
  granted; `Origin` mismatch is 403 anyway) and could not read a reply (no CORS headers). **DNS
  rebinding**: the rebound request carries the attacker's Host, which is not a trusted origin: 403,
  unchanged.
- **Journal reader (`pi`, root).** Before: no secret in the journal. After: pairing timing and
  counts only. Note that whoever can read the journal on the Pi can already read the code file as
  `pi`; the logging rule keeps secrets out of backups and pasted logs.
- **XSS in the dashboard** (none known) could already read the code from `localStorage`; pairing
  adds nothing.

## Decided

- Redeem returns the shared access code, not a per-device credential (reasons above). Undo: add a
  device-credential layer behind the same routes.
- Both listeners offer create and redeem; one PIN and one failure window shared across them.
- Numbers: 6 digits, 300 s, 5 tries per PIN, 10 failures per rolling 900 s, one active PIN,
  everything in memory. Change them in `assistant_pairing.json` only.
- Every well-formed non-success redeem counts toward the window, including when no PIN is active
  (no oracle, bounded memory); malformed requests and refusals while locked do not.
- Built, beyond the plan: a `burned` log action for a PIN ended by wrong tries or a lockout;
  `handle(method, payload)` without a path argument (the caller dispatches the exact path); the
  panel shows "This PIN is no longer valid" when the status has no `pin_id` (advisor restarted) and
  "This PIN was cancelled" for `cancelled`, and this PIN's own ending wins over a later lock;
  `assistantAccess.js` is 162 lines, not under 150 (the strings and pure helpers stayed together as
  planned). Undo any of them in `assistant_pairing.py` or `assistantAccess.js`.
- Lockout refuses `create` too, so the owner sees the lock on the device he is using.
- The tablet keeps its own chat history and saved-warning list (the client id is not transferred).
  Undo: also return and adopt the creator's client id, which would let the tablet read and cancel
  the Mac's chats.
- PIN field shown first in the unpaired form and focused; paste stays below it.
- Web restarts preferred parked; no broker restart.

## Open questions for the owner

None blocking. Two to confirm at the live check: whether 10 failures per 15 minutes is too strict for
his own typing (no change expected), and whether he wants per-device revocation later.

## Status

Built in worktree branch `worktree-agent-a08cb912082166e3f` (on `9209c9e`): contract and advisor,
web relay, dashboard, docs, each in its own commit, plus a tidying commit for
`assistant_http.read_json_body`. Verified: targeted pytest and the whole dashboard `node --test`
suite on vanpi, a scratch dashboard build, and the whole suite on the Mac worker (job id and tested
commit in the builder's report). Not verified: the UI was not rendered (no screenshot or browser
pass); the README screenshots of the paired dialogs were not retaken. The research worker now also
imports `assistant_common` through `assistant_http` (checked with `-X importtime`); it still needs
no restart for this change.

Deploy: not done. Next: T3 review of the diff (Astra), merge, whole suite on master, then the deploy
above (advisor, then both web units, then the dashboard build), then the live checks and the owner
step.
