"""The broker's drive-helper hold: its lock, record and supervisor stop condition.

`helper_hold.py` holds the shared contract and the pure bookkeeping. This
module owns the hold's in-memory state for one broker process and answers the
local `/v1/helpers/hold` requests. Everything the hold does to the broker (its
helpers' blocked reasons and status, the supervisor stops, the radar admission
check) is passed in or stays in `broker.py`, beside the ordered CAN lifecycle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from projects.vehicle_data.helper_hold import HoldRecord, HoldRefusal, parse_request


class StopWhenAny:
    """A supervisor stop condition set while any of its events is set.

    It joins the broker's shutdown or epoch-end event with the helper hold, so
    a hold set at any moment is seen by every check the supervisor makes:
    before the launch, before the startup grant and in the run loop.
    """

    def __init__(self, *events: threading.Event):
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = 0.05 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._events[0].wait(min(remaining, 0.05))
        return True


class HelperHoldControl:
    """One broker's hold. ``monotonic`` is the broker's clock.

    The event mirrors the record's ``held`` so supervisors can poll it; both
    change under this control's lock.
    """

    def __init__(self, monotonic: Callable[[], float]):
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._record = HoldRecord()
        self._event = threading.Event()

    @property
    def active(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._record.reason

    def stop_condition(self, event: threading.Event) -> StopWhenAny:
        """``event`` or the hold: what a helper supervisor stops on."""
        return StopWhenAny(event, self._event)

    def reply(self, pids: dict[str, int | None]) -> dict[str, object]:
        with self._lock:
            return self._record.reply(self._monotonic(), pids)

    def request(
        self,
        payload: object,
        *,
        admit: Callable[[Callable[[], None]], bool],
        stop_helpers: Callable[[], None],
        pids: Callable[[], dict[str, int | None]],
    ) -> tuple[int, dict[str, object]]:
        """Answer `POST /v1/helpers/hold`: hold on (202) or off (200).

        ``admit(begin)`` calls ``begin`` unless an alignment run forbids the
        hold, inside the lock that admits runs, and says whether it did.
        ``stop_helpers`` runs once per new hold, after it is set.
        """
        parsed = parse_request(payload)
        if parsed is None:
            return 400, {"error": HoldRefusal.BAD_REQUEST.value}
        hold, reason = parsed
        if not hold:
            with self._lock:
                self._record.end()
                self._event.clear()
            return 200, self.reply(pids())
        started: list[bool] = []

        def begin() -> None:
            with self._lock:
                started.append(self._record.begin(reason, self._monotonic()))
                self._event.set()

        if not admit(begin):
            return 409, {"error": HoldRefusal.ALIGNMENT_ACTIVE.value}
        if started[0]:
            stop_helpers()
        return 202, self.reply(pids())

    def settle(self) -> bool:
        """End a lapsed hold; whether a hold is still active."""
        with self._lock:
            if self._record.expire(self._monotonic()):
                self._event.clear()
            return self._record.held

    def claim(self, helper: str) -> bool:
        """Claim a helper launch; False while held. Excludes a concurrent hold-on."""
        with self._lock:
            return self._record.claim(helper)

    def finish(
        self, helper: str, outcome: dict[str, object] | None
    ) -> tuple[bool, bool]:
        """Release a claim: (a hold had asked it to stop, a hold is active now)."""
        with self._lock:
            return self._record.finish(helper, outcome), self._record.held
