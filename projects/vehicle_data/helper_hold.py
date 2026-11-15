"""Drive-helper hold: the contract shared by the broker and `restart_broker`.

A hold lets a broker restart happen while the van is running. While a hold is
active the broker launches no drive helper, stops any running one through its
normal supervisor path, and reports how each helper ended. The restart command
restarts the unit only after every helper reports a verified stop and its own
read of each adapter shows listen-only. The hold lives in the broker's memory
and expires on its own, so a crashed restart command cannot leave active
telemetry off for the rest of a drive; a new broker always starts unheld.

The route exists only on the broker's local Unix socket. `web.py` does not
relay it, so no dashboard or network client can hold the helpers.

This module is pure: request validation, the state vocabulary, the timing
constants and the hold bookkeeping (`HoldRecord`, fed the broker's clock
readings). Locks, clocks and supervisor calls stay in `broker.py`.
"""

from __future__ import annotations

import dataclasses
import enum

API_PATH = "/v1/helpers/hold"

# Seconds a hold lasts after its latest hold-on request. The restart command
# renews it (re-POSTs hold on) once RENEW_AFTER_SECONDS have passed on its own
# clock, so the expiry need not cover a slow stop plus a restart.
HOLD_SECONDS = 180.0
RENEW_AFTER_SECONDS = 120.0
REASON_MAX_LENGTH = 80

# The blocked reason a helper outcome gets when it ended while a hold was
# active. Releasing or expiring the hold clears exactly this reason, so a hold
# that ends without a restart lets the helpers relaunch in the same drive.
HELD_BLOCKED_REASON = "held"

HELPERS = ("active_drive", "auxiliary_drive")


class HelperHoldState(str, enum.Enum):
    """How one drive helper stands under a hold (`GET` reply, per helper)."""

    # No helper process existed when the hold started and none has run since.
    IDLE = "idle"
    # The helper was running and has been asked to stop; no final event yet.
    STOPPING = "stopping"
    # Final event reported a verified listen-only restoration.
    STOPPED_VERIFIED = "stopped_verified"
    # Cancelled before its startup grant: it never touched CAN (`restored` None).
    STOPPED_NO_MUTATION = "stopped_no_mutation"
    # Missing or failed restoration: the broker latched its usual inhibit.
    STOPPED_UNVERIFIED = "stopped_unverified"


# States in which the restart command may proceed for that helper.
SAFE_STATES = frozenset({
    HelperHoldState.IDLE,
    HelperHoldState.STOPPED_VERIFIED,
    HelperHoldState.STOPPED_NO_MUTATION,
})


class HoldRefusal(str, enum.Enum):
    """`error` values of a refused hold request (HTTP 400 or 409)."""

    BAD_REQUEST = "bad_request"
    # A Radar Service Drive Alignment run is active; stopping its helper would abort it.
    ALIGNMENT_ACTIVE = "alignment_active"
    # `reason` of a refused non-passive acquisition (HTTP 409): a wake is a CAN
    # transmission, which a hold for a restart must not start.
    HELPERS_HELD = "helpers_held"


def parse_request(payload: object) -> tuple[bool, str] | None:
    """Return ``(hold, reason)`` for a valid body, or None.

    Valid bodies are exactly ``{"hold": true, "reason": "<text>"}`` (1 to
    REASON_MAX_LENGTH printable characters) or exactly ``{"hold": false}``.
    """

    if not isinstance(payload, dict) or type(payload.get("hold")) is not bool:
        return None
    if payload["hold"] is False:
        return (False, "") if set(payload) == {"hold"} else None
    reason = payload.get("reason")
    if set(payload) != {"hold", "reason"} or not isinstance(reason, str):
        return None
    if not 1 <= len(reason) <= REASON_MAX_LENGTH or not reason.isprintable():
        return None
    return True, reason


def state_from_final(final: dict[str, object]) -> HelperHoldState:
    """Classify a supervisor-validated final outcome for the hold reply."""

    restored = final.get("restored")
    if restored is True:
        return HelperHoldState.STOPPED_VERIFIED
    # Same no-mutation shape helper_startup_grant accepts from a pre-grant child.
    if (
        restored is None
        and final.get("interface_mode", "unknown") == "unknown"
        and final.get("reason") != "restoration_failed"
    ):
        return HelperHoldState.STOPPED_NO_MUTATION
    return HelperHoldState.STOPPED_UNVERIFIED


def blocked_reason_after_stop(
    blocked_reason: str | None,
    *,
    stopped_by_hold: bool,
    still_held: bool,
    keep_reasons: frozenset[str],
) -> str | None:
    """The relaunch-blocking reason a helper outcome leaves, given the hold.

    A helper stopped by a hold would otherwise keep the reason its own final
    gives (a terminated C-CAN helper reports ``engine_not_running``) and stay
    off until the engine stops. It gets HELD_BLOCKED_REASON instead, which the
    hold's end clears; if the hold already ended, nothing blocks it. A reason
    in ``keep_reasons`` (a helper failure that blocks for the rest of the
    running epoch anyway) is kept, so a hold never shortens that block.
    """

    if not stopped_by_hold or blocked_reason in keep_reasons:
        return blocked_reason
    return HELD_BLOCKED_REASON if still_held else None


@dataclasses.dataclass
class HoldRecord:
    """The broker's hold bookkeeping; the broker serializes every call with its lock.

    ``running`` names the helpers whose supervisor run the broker has claimed
    (launched or about to launch). A claim and a hold-on exclude each other, so
    a hold either sees a claimed helper (STOPPING) or prevents its launch.
    """

    held: bool = False
    reason: str | None = None
    expires_at: float | None = None
    states: dict[str, HelperHoldState] = dataclasses.field(
        default_factory=lambda: dict.fromkeys(HELPERS, HelperHoldState.IDLE)
    )
    running: set[str] = dataclasses.field(default_factory=set)

    def begin(self, reason: str, now: float) -> bool:
        """Hold on at ``now``; True for a new hold, False for a renewal.

        A renewal only moves the expiry: the reason and per-helper states stay
        those of the hold being renewed.
        """

        self.expires_at = now + HOLD_SECONDS
        if self.held:
            return False
        self.held = True
        self.reason = reason
        self.states = {
            helper: (
                HelperHoldState.STOPPING
                if helper in self.running
                else HelperHoldState.IDLE
            )
            for helper in HELPERS
        }
        return True

    def end(self) -> None:
        """Release the hold; the states keep describing it."""

        self.held = False
        self.reason = None
        self.expires_at = None

    def expire(self, now: float) -> bool:
        """End a hold whose expiry has passed; True when it did."""

        if self.held and self.expires_at is not None and now >= self.expires_at:
            self.end()
            return True
        return False

    def claim(self, helper: str) -> bool:
        """Claim a launch of ``helper``; refused (False) while held."""

        if self.held:
            return False
        self.running.add(helper)
        return True

    def finish(self, helper: str, final: dict[str, object] | None) -> bool:
        """Record the end of a claimed run; True when a hold had asked it to stop.

        ``final`` is the supervisor-validated outcome, or None when supervision
        itself raised, which counts as unverified.
        """

        self.running.discard(helper)
        if self.states[helper] is not HelperHoldState.STOPPING:
            return False
        self.states[helper] = (
            HelperHoldState.STOPPED_UNVERIFIED if final is None else state_from_final(final)
        )
        return True

    def reply(self, now: float, pids: dict[str, int | None]) -> dict[str, object]:
        """The `GET` body. ``pids`` maps each helper to its last reported PID."""

        remaining = (
            max(0.0, self.expires_at - now)
            if self.held and self.expires_at is not None
            else None
        )
        return {
            "held": self.held,
            "reason": self.reason if self.held else None,
            "remaining_seconds": remaining,
            "helpers": {
                helper: {
                    "state": state.value,
                    "pid": (
                        pids.get(helper)
                        if state is HelperHoldState.STOPPING
                        else None
                    ),
                }
                for helper, state in self.states.items()
            },
        }
