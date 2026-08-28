#!/usr/bin/env python3
"""Fail-closed C-CAN wake supervisor for the dashboard's COP ALERT marker.

The machine-local dashboard remains CAN-free.  It publishes only
``/run/van-dashboard/cop-alert.active``.  This separately managed process
observes that marker, the ignition-monitor marker, and the telemetry broker's
cache-only status before asking :mod:`lib.can_wake` for one fixed, bounded
C-CAN RF-Hub wake transaction.

Each successful request is followed by verified passive restoration before
the fixed cadence wait.  This process never accepts a SocketCAN channel,
bitrate, CAN identifier, payload, physical pair, or alternate bus from a
caller.  CAN-CH is not a wake target.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from typing import Callable


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_wake  # noqa: E402
from projects.vehicle_data.api import TelemetryClient  # noqa: E402
from projects.vehicle_data.broker import DEFAULT_SOCKET  # noqa: E402


ROLE = "c-can"
COP_MARKER = Path("/run/van-dashboard/cop-alert.active")
IGNITION_MARKER = Path("/home/pi/hooks/ignition_is_on")
STATUS_PATH = Path("/run/van-cop-can-wake/status.json")

# These are deliberately fixed policy, not command-line or environment knobs.
SUCCESS_CADENCE_SECONDS = 15.0
FAILED_RETRY_SECONDS = 5.0
SAFETY_RETRY_SECONDS = 0.5
IDLE_POLL_SECONDS = 0.25
ACTIVATION_DEBOUNCE_SECONDS = 0.25
PREEXISTING_MARKER_DELAY_SECONDS = 3.0
BROKER_TIMEOUT_SECONDS = 0.75

ALLOWED_ACTIVE_DRIVE_STATES = frozenset(("idle", "disabled"))
# Admission never accepts state older than the registered five-second
# ignition/RPM freshness window.  A live three-role collector cycle can leave
# the vehicle-state sample about 4.1 seconds old while it services the other
# passive roles, so a shorter threshold phase-locks into false stale failures.
# Once the exact C-CAN role is exclusively owned, the collector cannot refresh
# that role; a separate nonextendable allowance covers the bounded wake,
# one-shot-to-normal re-arm, validation, and passive restoration. The wake core
# independently checks fresh 0x2EF/0x0FC witnesses at every send boundary.
MAX_BROKER_START_STATE_AGE_MS = 5_000
MAX_BROKER_TRANSACTION_STATE_AGE_MS = 12_000
STATUS_TEXT_LIMIT = 500
SOCKETCAN_NAME = re.compile(r"\bcan[0-9]+\b")


class CopCanWakeError(RuntimeError):
    """The supervisor cannot prove that one COP wake is currently safe."""


def log_status_event(payload: dict[str, object]) -> None:
    """Write one compact channel-free transition to the systemd journal."""

    print(
        json.dumps(
            {"event": "cop-can-wake-state", **payload},
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def public_status_text(value: object, *, limit: int = STATUS_TEXT_LIMIT) -> str:
    """Bound one status string and remove ephemeral SocketCAN identities."""

    text = " ".join(str(value).replace("\x00", "").split())
    text = SOCKETCAN_NAME.sub("<resolved-can>", text)
    return text[:limit]


def utc_now(wall_clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(wall_clock(), timezone.utc).isoformat()


def marker_present(path: Path, *, fail_present: bool) -> bool:
    """Inspect one marker without following it or failing open on I/O errors."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return fail_present
    return True


def read_broker_status(client: TelemetryClient) -> dict[str, object]:
    """Read one cache-only broker status object; this method performs no CAN I/O."""

    status, payload = client.request("GET", "/v1/status")
    if status != 200:
        raise CopCanWakeError(f"telemetry broker status returned HTTP {status}")
    if not isinstance(payload, dict) or payload.get("service") != "van-telemetry":
        raise CopCanWakeError("status endpoint did not identify van-telemetry")
    return payload


def broker_safety_conflicts(
    status: object,
    *,
    require_passive_role: bool = True,
    max_vehicle_state_age_ms: int = MAX_BROKER_START_STATE_AGE_MS,
) -> tuple[str, ...]:
    """Return reasons that cache-only broker state cannot authorize a COP wake.

    This is a corroborating service/vehicle gate.  The wake core separately
    owns and revalidates the exact serial/``dev_id`` C-CAN role, checks current
    operation inhibits and fresh bus evidence, and restores the exact passive
    baseline.  A bitrate or current ``canN`` is never used here as bus identity.
    """

    if not isinstance(status, dict):
        return ("telemetry broker status is not an object",)

    conflicts: list[str] = []
    collector = status.get("collector")
    active = status.get("active_drive")
    interface = status.get("interface")
    vehicle = status.get("vehicle_state")
    usb_monitor = status.get("usb_can_monitor")
    if not all(
        isinstance(item, dict)
        for item in (collector, active, interface, vehicle, usb_monitor)
    ):
        return ("telemetry broker omitted required safety state",)

    assert isinstance(collector, dict)
    assert isinstance(active, dict)
    assert isinstance(interface, dict)
    assert isinstance(vehicle, dict)
    assert isinstance(usb_monitor, dict)

    if collector.get("state") != "running":
        conflicts.append("telemetry passive collector is not running")
    if active.get("restoration_failed") is not False:
        conflicts.append("telemetry active-drive restoration is not proven safe")
    if active.get("state") not in ALLOWED_ACTIVE_DRIVE_STATES:
        conflicts.append("telemetry active-drive owner is not idle")
    if active.get("interface_mode") != "listen_only":
        conflicts.append("telemetry reports an armed diagnostic interface")
    if active.get("helper_pid") is not None:
        conflicts.append("telemetry active-drive helper is present")

    vehicle_state = vehicle.get("state")
    vehicle_age = vehicle.get("age_ms")
    try:
        numeric_vehicle_age = float(vehicle_age)
    except (TypeError, ValueError, OverflowError):
        numeric_vehicle_age = math.nan
    fresh_vehicle_state = bool(
        isinstance(vehicle_age, (int, float))
        and not isinstance(vehicle_age, bool)
        and math.isfinite(numeric_vehicle_age)
        and 0 <= numeric_vehicle_age <= max_vehicle_state_age_ms
    )
    if not fresh_vehicle_state:
        conflicts.append("telemetry vehicle-state evidence is missing or stale")
    if vehicle.get("running") is True or vehicle_state == "running":
        conflicts.append("telemetry reports the engine running")
    elif (
        vehicle_state == "ignition_on"
        and vehicle.get("basis") == "ccan_0x2ef_ignition_gate"
    ):
        conflicts.append("telemetry reports verified ignition-on presence")
    elif vehicle.get("running") is False:
        pass
    elif (
        vehicle_state == "awake"
        and vehicle.get("running") is None
        and vehicle.get("basis") == "passive_bus_activity"
    ):
        # A prior COP poke intentionally creates this temporary broker state.
        # It does not establish engine-off by itself, so only the C-CAN wake
        # profile may act on it: that profile independently rejects fresh
        # 0x2EF and >=400-rpm 0x0FC witnesses under the exclusive role/channel
        # locks immediately before transmission and after the response.
        pass
    else:
        conflicts.append(
            "telemetry has not freshly established a supported parked wake state"
        )

    inhibits = interface.get("active_inhibits")
    if not isinstance(inhibits, list) or inhibits:
        conflicts.append("telemetry reports an active or unreadable CAN-operation inhibit")

    role_snapshot = interface.get("role_interfaces")
    roles = role_snapshot.get("roles") if isinstance(role_snapshot, dict) else None
    c_can = roles.get(ROLE) if isinstance(roles, dict) else None
    expected = c_can.get("expected") if isinstance(c_can, dict) else None
    actual = c_can.get("actual") if isinstance(c_can, dict) else None
    exact_passive_role = bool(
        isinstance(c_can, dict)
        and isinstance(expected, dict)
        and isinstance(actual, dict)
        and c_can.get("resolution") == "resolved"
        and c_can.get("passive_ready") is True
        and expected.get("role") == ROLE
        and expected.get("pair") == "6/14"
        and expected.get("bitrate") == 500_000
        and expected.get("dev_id") == 0
        and isinstance(expected.get("usb_serial"), str)
        and bool(expected.get("usb_serial"))
        and actual.get("up") is True
        and actual.get("bitrate") == 500_000
        and actual.get("fd_enabled") is False
        and actual.get("one_shot") is False
        and actual.get("listen_only") is True
        and actual.get("controller_state") == "ERROR-ACTIVE"
        and actual.get("restart_ms") == 0
    )
    if require_passive_role and not exact_passive_role:
        conflicts.append("broker does not prove the exact passive C-CAN role")

    if (
        usb_monitor.get("enabled") is not True
        or usb_monitor.get("state") != "running"
        or usb_monitor.get("active_count") != 0
        or usb_monitor.get("last_error") is not None
    ):
        conflicts.append("USB/CAN incident monitor is not healthy and clear")

    return tuple(conflicts)


class StatusPublisher:
    """Atomically publish a compact, channel-free supervisor status file."""

    def __init__(
        self,
        path: Path = STATUS_PATH,
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.wall_clock = wall_clock
        self._last_content: str | None = None

    def publish(self, payload: dict[str, object]) -> None:
        public = {
            "schema_version": 1,
            "service": "van-cop-can-wake",
            "role": ROLE,
            "wake_method": "fixed one-shot physical RF Hub ReadDataByIdentifier FEFF",
            **payload,
        }
        comparable = json.dumps(public, sort_keys=True, separators=(",", ":"))
        if comparable == self._last_content:
            return
        public["generated_at"] = utc_now(self.wall_clock)
        encoded = json.dumps(public, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, mode=0o750, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fchmod(handle.fileno(), 0o640)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self._last_content = comparable


class CopCanWakeSupervisor:
    """Translate the dashboard marker into fixed, restored C-CAN wake pokes."""

    def __init__(
        self,
        *,
        marker: Path = COP_MARKER,
        ignition_marker: Path = IGNITION_MARKER,
        broker_client: TelemetryClient | None = None,
        wake_once=None,
        wake_error_type=None,
        status_publisher: StatusPublisher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        stop_event: threading.Event | None = None,
        event_logger: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.marker = marker
        self.ignition_marker = ignition_marker
        self.broker_client = broker_client or TelemetryClient(
            DEFAULT_SOCKET,
            timeout=BROKER_TIMEOUT_SECONDS,
        )
        self.wake_once = wake_once or can_wake.wake_once
        self.wake_error_type = wake_error_type or can_wake.CanWakeError
        self.status = status_publisher or StatusPublisher(wall_clock=wall_clock)
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.stop_event = stop_event or threading.Event()
        self.event_logger = event_logger or log_status_event
        self.started_monotonic = self._monotonic_value()
        self.marker_present_at_start = self._marker_active()
        self.marker_stable_since: float | None = None
        self.marker_delay_seconds = (
            PREEXISTING_MARKER_DELAY_SECONDS
            if self.marker_present_at_start
            else ACTIVATION_DEBOUNCE_SECONDS
        )
        self.next_attempt_monotonic = self.started_monotonic
        self.next_attempt_at: str | None = None
        self.next_attempt_state: str | None = None
        self.wake_count = 0
        self.last_attempt_at: str | None = None
        self.last_transaction_seconds: float | None = None
        self.last_success_at: str | None = None
        self.last_reason: str | None = None
        self.last_detail: str | None = None
        self.last_blocked_at: str | None = None
        self.last_blocked_reason: str | None = None
        self.last_blocked_detail: str | None = None
        self._last_logged_transition: tuple[object, ...] | None = None

    def _monotonic_value(self) -> float:
        value = float(self.monotonic())
        if not math.isfinite(value):
            raise CopCanWakeError("monotonic clock did not return a finite value")
        return value

    def _marker_active(self) -> bool:
        # An unreadable COP marker cannot authorize transmission.
        return marker_present(self.marker, fail_present=False)

    def _ignition_on(self) -> bool:
        # An unreadable ignition marker blocks wake just like a present marker.
        return marker_present(self.ignition_marker, fail_present=True)

    def _read_broker_status(self) -> dict[str, object]:
        return read_broker_status(self.broker_client)

    def safety_conflicts(
        self,
        *,
        require_passive_role: bool = True,
    ) -> tuple[str, ...]:
        """Recheck every external gate without opening or changing CAN."""

        conflicts: list[str] = []
        if self.stop_event.is_set():
            conflicts.append("COP wake supervisor is stopping")
        if not self._marker_active():
            conflicts.append("COP ALERT marker is absent or unreadable")
        if self._ignition_on():
            conflicts.append("ignition marker is present or unreadable")
        try:
            status = self._read_broker_status()
        except Exception as exc:
            conflicts.append(
                f"telemetry broker status unavailable: {type(exc).__name__}: {exc}"
            )
        else:
            conflicts.extend(
                broker_safety_conflicts(
                    status,
                    require_passive_role=require_passive_role,
                    max_vehicle_state_age_ms=(
                        MAX_BROKER_START_STATE_AGE_MS
                        if require_passive_role
                        else MAX_BROKER_TRANSACTION_STATE_AGE_MS
                    ),
                )
            )
        return tuple(conflicts)

    def transaction_safety_conflicts(self) -> tuple[str, ...]:
        """Recheck marker/vehicle/owner state while the wake core owns C-CAN.

        The outer gate requires the broker's cached passive-role snapshot.
        During a transaction the broker may observe the core's intentional
        short armed interval and mark that cache non-passive.  The wake core is
        then the live identity/interface authority, so this callback continues
        to require fresh stopped-state, idle active-drive, USB health, markers,
        and broker availability without racing its own link-state reflection.
        """

        return self.safety_conflicts(require_passive_role=False)

    def _reset_marker_window(self) -> None:
        self.marker_stable_since = None
        self.marker_delay_seconds = ACTIVATION_DEBOUNCE_SECONDS

    def _schedule_attempt(self, now: float, delay: float, state: str) -> None:
        delay = max(0.0, float(delay))
        self.next_attempt_monotonic = now + delay
        self.next_attempt_at = datetime.fromtimestamp(
            self.wall_clock() + delay,
            timezone.utc,
        ).isoformat()
        self.next_attempt_state = state

    def _clear_attempt_schedule(self, now: float) -> None:
        self.next_attempt_monotonic = now
        self.next_attempt_at = None
        self.next_attempt_state = None

    def _record_block(self, reason: str, detail: str) -> None:
        reason = public_status_text(reason, limit=80)
        detail = public_status_text(detail)
        self.last_reason = reason
        self.last_detail = detail
        self.last_blocked_at = utc_now(self.wall_clock)
        self.last_blocked_reason = reason
        self.last_blocked_detail = detail

    def _publish(self, state: str, *, transaction_in_progress: bool = False) -> None:
        payload = {
            "state": state,
            "marker_active": self._marker_active(),
            "ignition_on": self._ignition_on(),
            "transaction_in_progress": transaction_in_progress,
            "activation_debounce_seconds": ACTIVATION_DEBOUNCE_SECONDS,
            "preexisting_marker_delay_seconds": PREEXISTING_MARKER_DELAY_SECONDS,
            "safety_retry_seconds": SAFETY_RETRY_SECONDS,
            "fixed_success_cadence_seconds": SUCCESS_CADENCE_SECONDS,
            "success_cadence_basis": "attempt_start",
            "next_attempt_at": self.next_attempt_at,
            "wake_count": self.wake_count,
            "last_attempt_at": self.last_attempt_at,
            "last_transaction_seconds": self.last_transaction_seconds,
            "last_success_at": self.last_success_at,
            "last_reason": self.last_reason,
            "last_detail": self.last_detail,
            "last_blocked_at": self.last_blocked_at,
            "last_blocked_reason": self.last_blocked_reason,
            "last_blocked_detail": self.last_blocked_detail,
        }
        self.status.publish(payload)
        transition = (
            state,
            payload["marker_active"],
            payload["ignition_on"],
            transaction_in_progress,
            self.last_reason,
            self.last_detail,
            self.wake_count,
        )
        if transition != self._last_logged_transition:
            self._last_logged_transition = transition
            try:
                self.event_logger(
                    {
                        "occurred_at": utc_now(self.wall_clock),
                        **payload,
                    }
                )
            except Exception:
                # The atomic status file remains authoritative. A broken
                # stdout/journal sink must not change CAN gating or cleanup.
                pass

    def tick(self) -> float:
        """Run one decision/transaction and return a bounded next wait."""

        now = self._monotonic_value()
        if self.stop_event.is_set():
            self._publish("stopping")
            return 0.0

        if not self._marker_active():
            self._reset_marker_window()
            self._clear_attempt_schedule(now)
            self.last_reason = None
            self.last_detail = "waiting for the dashboard COP ALERT marker"
            self._publish("idle")
            return IDLE_POLL_SECONDS

        if self._ignition_on():
            self._reset_marker_window()
            self._clear_attempt_schedule(now)
            self.last_reason = "ignition_on"
            self.last_detail = "COP CAN wake is paused while ignition is present"
            self._publish("paused_ignition")
            return IDLE_POLL_SECONDS

        if self.marker_stable_since is None:
            self.marker_stable_since = now
            self._schedule_attempt(
                now,
                self.marker_delay_seconds,
                "arming_delay",
            )
        stable_for = now - self.marker_stable_since
        if stable_for < self.marker_delay_seconds:
            if self.marker_delay_seconds == PREEXISTING_MARKER_DELAY_SECONDS:
                self.last_reason = "preexisting_marker_delay"
                self.last_detail = (
                    "service started with COP already armed; waiting through the "
                    "fixed restart grace before the first wake"
                )
            else:
                self.last_reason = "activation_debounce"
                self.last_detail = (
                    "new COP button marker observed; waiting through the short "
                    "activation debounce"
                )
            self._publish("arming_delay")
            return min(
                IDLE_POLL_SECONDS,
                max(0.0, self.marker_delay_seconds - stable_for),
            )

        if self.next_attempt_state == "arming_delay":
            self._clear_attempt_schedule(now)

        if now < self.next_attempt_monotonic:
            self._publish(self.next_attempt_state or "active_waiting")
            return min(IDLE_POLL_SECONDS, self.next_attempt_monotonic - now)

        conflicts = self.safety_conflicts()
        if conflicts:
            self._record_block("safety_gate", "; ".join(conflicts))
            self._schedule_attempt(
                now,
                SAFETY_RETRY_SECONDS,
                "blocked",
            )
            self._publish("blocked")
            return IDLE_POLL_SECONDS

        self._clear_attempt_schedule(now)
        attempt_started = self._monotonic_value()
        self.last_attempt_at = utc_now(self.wall_clock)
        self.last_reason = None
        self.last_detail = "bounded C-CAN wake transaction is in progress"
        # Failure to publish the armed-transaction intent aborts before CAN I/O.
        self._publish("waking", transaction_in_progress=True)
        try:
            result = self.wake_once(
                ROLE,
                prearm_check=self.transaction_safety_conflicts,
            )
        except self.wake_error_type as exc:
            completed = self._monotonic_value()
            self.last_transaction_seconds = round(
                max(0.0, completed - attempt_started),
                3,
            )
            reason = str(getattr(exc, "reason", "wake_failed"))
            self._record_block(
                reason,
                str(getattr(exc, "detail", exc)),
            )
            self._schedule_attempt(
                completed,
                (
                    SAFETY_RETRY_SECONDS
                    if reason == "handoff_busy"
                    else FAILED_RETRY_SECONDS
                ),
                "blocked",
            )
            self._publish("blocked")
            return IDLE_POLL_SECONDS

        completed = self._monotonic_value()
        self.last_transaction_seconds = round(
            max(0.0, completed - attempt_started),
            3,
        )
        self.wake_count += 1
        self.last_success_at = utc_now(self.wall_clock)
        self.last_reason = None
        self.last_detail = public_status_text(
            getattr(result, "detail", "RF Hub response verified and C-CAN restored")
        )
        self._schedule_attempt(
            completed,
            max(
                0.0,
                SUCCESS_CADENCE_SECONDS - self.last_transaction_seconds,
            ),
            "active_waiting",
        )
        self._publish("active_waiting")
        return IDLE_POLL_SECONDS

    def run(self) -> int:
        self.last_detail = "supervisor started; no wake occurs without every parked gate"
        self._publish("starting")
        try:
            while not self.stop_event.is_set():
                wait_seconds = self.tick()
                self.stop_event.wait(max(0.0, min(IDLE_POLL_SECONDS, wait_seconds)))
        except KeyboardInterrupt:
            # The shared wake core may translate TERM/INT received during an
            # armed transaction into KeyboardInterrupt so its context manager
            # can restore first.  Once it reaches this boundary, exit cleanly
            # instead of letting Restart=on-failure re-arm a persistent marker.
            self.stop_event.set()
        finally:
            self.last_reason = "stopped"
            self.last_detail = "supervisor stopped; no CAN transaction remains owned"
            self._publish("stopped")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the fixed marker supervisor; absent by default so manual use is inert",
    )
    parser.add_argument(
        "--confirm-fixed-c-can-wake",
        action="store_true",
        help="confirm the reviewed COP ALERT RF-Hub wake policy",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "role": ROLE,
                    "marker": str(COP_MARKER),
                    "ignition_marker": str(IGNITION_MARKER),
                    "cadence_seconds": SUCCESS_CADENCE_SECONDS,
                    "detail": "plan only; no CAN interface was inspected or changed",
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.confirm_fixed_c_can_wake:
        raise SystemExit("--execute requires --confirm-fixed-c-can-wake")

    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    supervisor = CopCanWakeSupervisor(stop_event=stop_event)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
