"""Pairing PINs: a browser that holds the assistant access code shows a one-time six-digit PIN, and a
new device types it once to receive that code. The owner's driving tablet cannot receive the
43-character code (no shared clipboard, an on-screen keyboard), and guests on the van Wi-Fi must
stay out, so each PIN is single use, short lived, and guessing is capped per PIN and per window.

``assistant_pairing.json`` is the one copy of the routes, states, reasons and limits; the dashboard
imports it too. ``PairingPins`` keeps everything in memory behind its own lock, separate from the
warning chat's, so a flood of redeems never waits on or blocks a chat; an advisor restart drops the
PIN and the failure window. The contract is ``docs/warning-chat.md`` section "Pairing another
device".

Secrets: ``create``'s reply to a code holder is the only place a PIN leaves this module, and a
successful redeem's reply the only place the access code does. Log lines carry an action and a
count, never a PIN, a PIN id or the code; every reply detail is a fixed sentence.
"""

import hmac
import json
import math
from collections import deque
from pathlib import Path
import re
import secrets
import sys
import threading
import time

CONTRACT = json.loads(Path(__file__).with_name("assistant_pairing.json").read_text())

ROUTES = CONTRACT["routes"]
OWNER_PATH = ROUTES["owner"]
REDEEM_PATH = ROUTES["redeem"]
ACTIONS = CONTRACT["actions"]
STATES = CONTRACT["states"]
REASONS = CONTRACT["reasons"]
PIN_DIGITS = CONTRACT["pinDigits"]
PIN_LIFETIME = CONTRACT["pinLifetimeSeconds"]
MAX_ATTEMPTS = CONTRACT["maxAttemptsPerPin"]
WINDOW_LIMIT = CONTRACT["windowFailureLimit"]
WINDOW_SECONDS = CONTRACT["windowSeconds"]
REDEEM_BODY_MAX = CONTRACT["redeemBodyMaxBytes"]

# [0-9], not \d: Python's \d also accepts non-ASCII digits.
PIN_RE = re.compile(rf"[0-9]{{{PIN_DIGITS}}}")
PIN_ID_RE = re.compile(CONTRACT["pinIdPattern"])

DETAIL = {
    "rejected": "That PIN was not accepted",
    "locked": "Pairing is paused after too many wrong PINs",
    "invalid": "Invalid pairing request",
}
REJECTED = {"available": False, "reason": REASONS["rejected"], "detail": DETAIL["rejected"]}
INVALID = {"available": False, "reason": REASONS["invalid"], "detail": DETAIL["invalid"]}


def relay_allowed(method, path):
    """Whether the web relay may forward this owner-route request (it still needs the access code)."""
    return method in ("GET", "POST") and path == OWNER_PATH


def well_formed_pin(payload):
    """The PIN of an exact ``{"pin": "<six ASCII digits>"}`` body, else ``None``."""
    if not isinstance(payload, dict) or set(payload) != {"pin"}:
        return None
    pin = payload["pin"]
    return pin if isinstance(pin, str) and PIN_RE.fullmatch(pin) else None


def _log_stderr(fields):
    print(json.dumps(fields, separators=(",", ":")), file=sys.stderr, flush=True)


class PairingPins:
    """One active PIN at a time and one failure window, for every listener that relays here."""

    def __init__(self, access_code, *, clock=time.monotonic, randbelow=secrets.randbelow,
                 token_hex=secrets.token_hex, log=_log_stderr):
        self._code = access_code
        self._clock, self._randbelow, self._token_hex, self._log = clock, randbelow, token_hex, log
        self._lock = threading.Lock()
        self._pin = None        # the active PIN's digits; None when no PIN is active
        self._pin_id = None     # the latest PIN's id, kept after it ends for the owner's status
        self._state = STATES["none"]
        self._expires = 0.0
        self._attempts = 0
        self._failures = deque(maxlen=WINDOW_LIMIT)  # monotonic times of the latest failures
        self._locked = False    # whether the log last reported the window as locked
        # Compared when no PIN is active, so a redeem does the same comparison either way.
        self._dummy = "0" * PIN_DIGITS

    # ---------- the owner route (the caller already checked the access code) ----------

    def handle(self, method, payload):
        """``GET`` is the status; ``POST`` takes exactly ``{action: create}`` or ``{action: cancel, pin_id}``."""
        if method == "GET":
            return self.status()
        action = payload.get("action") if isinstance(payload, dict) else None
        if action == ACTIONS["create"] and set(payload) == {"action"}:
            return self.create()
        if (action == ACTIONS["cancel"] and set(payload) == {"action", "pin_id"}
                and isinstance(payload["pin_id"], str) and PIN_ID_RE.fullmatch(payload["pin_id"])):
            return self.cancel(payload["pin_id"])
        return 400, dict(INVALID)

    def status(self):
        with self._lock:
            now = self._clock()
            return 200, self._status(now, self._tick(now))

    def create(self):
        """A new PIN, replacing any active one; refused while the failure window is locked."""
        with self._lock:
            now = self._clock()
            locked_for = self._tick(now)
            if locked_for:
                return 429, self._locked_reply(locked_for)
            if self._pin is not None:
                self._end(STATES["cancelled"], "cancelled", now)
            self._pin = f"{self._randbelow(10 ** PIN_DIGITS):0{PIN_DIGITS}d}"
            self._pin_id = self._token_hex(8)
            self._state = STATES["active"]
            self._expires = now + PIN_LIFETIME
            self._attempts = 0
            self._emit("created", now)
            return 200, {"available": True, "pin": self._pin, "pin_id": self._pin_id,
                         "expires_in_s": PIN_LIFETIME}

    def cancel(self, pin_id):
        """Cancel the active PIN when ``pin_id`` names it; idempotent, and always the status."""
        with self._lock:
            now = self._clock()
            locked_for = self._tick(now)
            if self._pin is not None and hmac.compare_digest(pin_id, self._pin_id):
                self._end(STATES["cancelled"], "cancelled", now)
            return 200, self._status(now, locked_for)

    # ---------- the one route answered without the access code ----------

    def redeem(self, payload):
        """``(status, reply)`` for one redeem body.

        A body that is not exactly ``{"pin": "<six ASCII digits>"}`` is 400 and is not counted: it
        never reaches a comparison. A well-formed PIN that is wrong, expired or offered while none
        is active gets the one identical 403 and is counted; the matching PIN gets the code once.
        """
        pin = well_formed_pin(payload)
        if pin is None:
            return 400, dict(INVALID)
        with self._lock:
            now = self._clock()
            locked_for = self._tick(now)
            if locked_for:
                return 429, self._locked_reply(locked_for)
            active = self._pin
            expected = active if active is not None else self._dummy
            matched = hmac.compare_digest(pin.encode("ascii"), expected.encode("ascii"))
            if matched and active is not None:
                self._end(STATES["redeemed"], "redeemed", now)
                return 200, {"available": True, "access_code": self._code}
            self._fail(now)
            return 403, dict(REJECTED)

    # ---------- state, all under self._lock ----------

    def _fail(self, now):
        self._failures.append(now)
        burned = False
        if self._pin is not None:
            self._attempts += 1
            burned = self._attempts >= MAX_ATTEMPTS
        self._emit("rejected", now)
        if burned:
            self._end(STATES["wrongTries"], "burned", now)
        if self._locked_for(now):
            if self._pin is not None:
                self._end(STATES["wrongTries"], "burned", now)
            self._locked = True
            self._emit("locked", now)

    def _tick(self, now):
        """Expire the active PIN and note a lifted lock; the seconds the window stays locked."""
        if self._pin is not None and now >= self._expires:
            self._end(STATES["expired"], "expired", now)
        locked_for = self._locked_for(now)
        if self._locked and not locked_for:
            self._locked = False
            self._emit("unlocked", now)
        return locked_for

    def _locked_for(self, now):
        if len(self._failures) < WINDOW_LIMIT:
            return 0
        return max(0, math.ceil(self._failures[0] + WINDOW_SECONDS - now))

    def _end(self, state, action, now):
        self._pin = None
        self._state = state
        self._emit(action, now)

    def _status(self, now, locked_for):
        return {"available": True, "pin_id": self._pin_id, "state": self._state,
                "expires_in_s": max(0, math.ceil(self._expires - now)) if self._pin is not None else 0,
                "locked_for_s": locked_for}

    @staticmethod
    def _locked_reply(locked_for):
        return {"available": False, "reason": REASONS["locked"], "detail": DETAIL["locked"],
                "retry_after_s": locked_for}

    def _emit(self, action, now):
        recent = sum(1 for at in self._failures if now - at < WINDOW_SECONDS)
        self._log({"event": "assistant_pairing", "action": action, "failures_in_window": recent})
