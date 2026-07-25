"""Conservative AlfaOBD-over-ADB UI state observation and guarded actions.

This module deliberately understands only read-only diagnostic UI actions.  It
does not expose DTC clearing, Active Diagnostics, resets, calibrations,
configuration changes, writes, or PROXI operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Iterable, Protocol
import xml.etree.ElementTree as ET


PACKAGE = "com.AlfaOBD.AlfaOBD"
DEFAULT_POLL_INTERVAL_SECONDS = 0.75
DEFAULT_DUMP_TIMEOUT_SECONDS = 12.0
DEFAULT_FAILURE_ROOT = Path("tmp/alfaobd_controller/failures")
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


class AlfaUiError(RuntimeError):
    """A fail-closed ADB, UI-state, selector, or transition error."""


class UiState(str, Enum):
    MAIN = "main"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ADAPTER_PROMPT = "adapter_prompt"
    ISO_WARNING = "iso_warning"
    SYSTEM_STATUS_EMPTY = "system_status_empty"
    SYSTEM_ID_POPULATED = "system_id_populated"
    FAULTS_EMPTY = "faults_empty"
    FAULTS_POPULATED = "faults_populated"
    NO_FAULTS = "no_faults"
    FAILURE = "failure"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"


class WaitOutcome(str, Enum):
    MATCHED = "matched"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


@dataclass(frozen=True)
class UiNode:
    text: str
    resource_id: str
    class_name: str
    package: str
    clickable: bool
    enabled: bool
    checked: bool
    bounds: Bounds


@dataclass(frozen=True)
class UiSnapshot:
    xml: str
    nodes: tuple[UiNode, ...]
    states: frozenset[UiState]
    primary: UiState

    def has(self, *states: UiState) -> bool:
        return bool(self.states.intersection(states))


@dataclass(frozen=True)
class WaitResult:
    outcome: WaitOutcome
    snapshot: UiSnapshot
    attempts: int
    elapsed_seconds: float
    evidence_prefix: str | None = None

    def require_match(self) -> UiSnapshot:
        if self.outcome is not WaitOutcome.MATCHED:
            raise AlfaUiError(
                f"UI wait ended as {self.outcome.value}: "
                f"primary={self.snapshot.primary.value}, "
                f"states={','.join(sorted(state.value for state in self.snapshot.states))}, "
                f"evidence={self.evidence_prefix or 'none'}"
            )
        return self.snapshot


class AdbLike(Protocol):
    def dump_ui(self) -> str: ...

    def screenshot(self) -> bytes: ...


EventLogger = Callable[[str, dict[str, object]], None]
Predicate = Callable[[UiSnapshot], bool]


def _bool(value: str | None) -> bool:
    return value == "true"


def _parse_bounds(value: str) -> Bounds:
    match = BOUNDS_RE.fullmatch(value)
    if not match:
        raise AlfaUiError(f"invalid UI bounds {value!r}")
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        raise AlfaUiError(f"empty/reversed UI bounds {value!r}")
    return Bounds(left, top, right, bottom)


def parse_ui_xml(xml_text: str) -> tuple[UiNode, ...]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AlfaUiError(f"invalid UI XML: {exc}") from exc
    if root.tag != "hierarchy":
        raise AlfaUiError(f"unexpected UI root {root.tag!r}")
    nodes: list[UiNode] = []
    for element in root.iter("node"):
        package = element.attrib.get("package", "")
        if package not in ("", PACKAGE, "android"):
            raise AlfaUiError(f"foreign package in UI hierarchy: {package!r}")
        nodes.append(
            UiNode(
                text=element.attrib.get("text", "").strip(),
                resource_id=element.attrib.get("resource-id", ""),
                class_name=element.attrib.get("class", ""),
                package=package,
                clickable=_bool(element.attrib.get("clickable")),
                enabled=_bool(element.attrib.get("enabled")),
                checked=_bool(element.attrib.get("checked")),
                bounds=_parse_bounds(element.attrib.get("bounds", "")),
            )
        )
    if not nodes:
        raise AlfaUiError("UI hierarchy contains no nodes")
    return tuple(nodes)


def _contains(joined: str, *needles: str) -> bool:
    return any(needle.casefold() in joined for needle in needles)


def classify_ui(xml_text: str) -> UiSnapshot:
    nodes = parse_ui_xml(xml_text)
    texts = tuple(node.text for node in nodes if node.text)
    exact = {text.casefold() for text in texts}
    joined = "\n".join(texts).casefold()
    states: set[UiState] = set()

    if (
        "ecu verification failed" in exact
        or _contains(joined, "failure to verify iso code")
    ):
        states.add(UiState.ISO_WARNING)
    if "connect adapter" in exact and _contains(
        joined, "connect the grey adapter", "connect any adapter"
    ):
        states.add(UiState.ADAPTER_PROMPT)
    connecting = _contains(
        joined,
        "status: connected. checking connected device model",
        "checking connected device model",
        "connection in progress",
        "connecting...",
    )
    if connecting:
        states.add(UiState.CONNECTING)
    # The DISCONNECT button appears before ECU/interface verification is
    # complete, so it is not terminal-success evidence by itself.
    if not connecting and _contains(
        joined, "connected to ", "status: connected."
    ):
        states.add(UiState.CONNECTED)
    if (
        "connect" in exact
        and _contains(
            joined,
            "status: idle",
            "connection status: not connected",
            "interface not connected",
        )
    ):
        states.add(UiState.DISCONNECTED)
    if (
        "interface not connected" in exact
        and _contains(joined, "my interface", "my car")
    ):
        states.update((UiState.MAIN, UiState.DISCONNECTED))

    system_status = "system status" in exact
    # AlfaOBD may render the complete identification report in one large text
    # node rather than exposing DEVICE INFO and each field as separate nodes.
    has_device_info = _contains(joined, "device info:") and _contains(
        joined,
        "fiat drawing number:",
        "hardware number:",
        "software number:",
        "iso code:",
    )
    if has_device_info:
        states.add(UiState.SYSTEM_ID_POPULATED)
    elif system_status and "read system id" in exact:
        states.add(UiState.SYSTEM_STATUS_EMPTY)

    faults_page = "faults" in exact
    no_faults = (
        "no faults found" in exact
        or _contains(joined, "no faults reported by the unit")
    )
    faults_found = _contains(joined, "faults found.")
    if no_faults:
        states.update((UiState.NO_FAULTS, UiState.FAULTS_POPULATED))
    elif faults_found:
        states.add(UiState.FAULTS_POPULATED)
    elif faults_page and "read all faults" in exact:
        states.add(UiState.FAULTS_EMPTY)

    if _contains(
        joined,
        "no reply from interface",
        "interface message: no data",
        "connection failed",
        "could not connect",
        "status: connection error",
        "fatal error",
    ) or "no reply" in exact or "failed!" in exact:
        states.add(UiState.FAILURE)

    if not states:
        states.add(UiState.UNKNOWN)

    priority = (
        UiState.FAILURE,
        UiState.ISO_WARNING,
        UiState.ADAPTER_PROMPT,
        UiState.SYSTEM_ID_POPULATED,
        UiState.NO_FAULTS,
        UiState.FAULTS_POPULATED,
        UiState.CONNECTING,
        UiState.CONNECTED,
        UiState.DISCONNECTED,
        UiState.SYSTEM_STATUS_EMPTY,
        UiState.FAULTS_EMPTY,
        UiState.MAIN,
        UiState.UNKNOWN,
    )
    primary = next(state for state in priority if state in states)
    return UiSnapshot(
        xml=xml_text,
        nodes=nodes,
        states=frozenset(states),
        primary=primary,
    )


class UiPoller:
    """Poll fresh UI dumps without overlapping uiautomator invocations."""

    def __init__(
        self,
        adb: AdbLike,
        *,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        failure_root: Path = DEFAULT_FAILURE_ROOT,
        logger: EventLogger | None = None,
        max_consecutive_dump_errors: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not 0.5 <= interval_seconds <= 1.0:
            raise AlfaUiError("poll interval must be between 0.5 and 1.0 seconds")
        if max_consecutive_dump_errors < 0 or max_consecutive_dump_errors > 5:
            raise AlfaUiError("max consecutive dump errors must be between 0 and 5")
        self.adb = adb
        self.interval_seconds = interval_seconds
        self.failure_root = failure_root
        self.logger = logger
        self.max_consecutive_dump_errors = max_consecutive_dump_errors
        self.monotonic = monotonic
        self.sleep = sleep

    def _log(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger(event, fields)

    def observe(self) -> UiSnapshot:
        snapshot = classify_ui(self.adb.dump_ui())
        self._log(
            "ui_observed",
            primary=snapshot.primary.value,
            states=sorted(state.value for state in snapshot.states),
        )
        return snapshot

    def _capture_evidence(
        self,
        *,
        operation: str,
        outcome: WaitOutcome,
        snapshot: UiSnapshot,
        attempts: int,
        elapsed_seconds: float,
        error: str | None = None,
    ) -> str:
        safe_operation = re.sub(r"[^A-Za-z0-9_.-]+", "_", operation).strip("._")
        safe_operation = safe_operation or "operation"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        prefix = self.failure_root / f"{stamp}_{safe_operation}_{outcome.value}"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{prefix}.xml").write_text(snapshot.xml, encoding="utf-8")
        screenshot_error = None
        try:
            Path(f"{prefix}.png").write_bytes(self.adb.screenshot())
        except Exception as exc:  # Evidence capture must not hide the primary failure.
            screenshot_error = f"{type(exc).__name__}: {exc}"
        payload = {
            "schema_version": 1,
            "operation": operation,
            "outcome": outcome.value,
            "attempts": attempts,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "primary": snapshot.primary.value,
            "states": sorted(state.value for state in snapshot.states),
            "error": error,
            "screenshot_error": screenshot_error,
        }
        Path(f"{prefix}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._log("ui_failure_evidence", prefix=str(prefix), **payload)
        return str(prefix)

    def wait_for_states(
        self,
        *,
        operation: str,
        expected: Iterable[UiState],
        timeout_seconds: float,
        fail_on: Iterable[UiState] = (UiState.FAILURE,),
    ) -> WaitResult:
        expected_set = frozenset(expected)
        failure_set = frozenset(fail_on)
        if not expected_set:
            raise AlfaUiError("expected state set must not be empty")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise AlfaUiError("timeout must be greater than zero and at most 300 seconds")
        return self.wait_for_predicate(
            operation=operation,
            predicate=lambda snapshot: bool(snapshot.states & expected_set),
            timeout_seconds=timeout_seconds,
            fail_on=failure_set,
            expected_states=expected_set,
        )

    def wait_for_predicate(
        self,
        *,
        operation: str,
        predicate: Predicate,
        timeout_seconds: float,
        fail_on: Iterable[UiState] = (
            UiState.FAILURE,
            UiState.ADAPTER_PROMPT,
            UiState.ISO_WARNING,
        ),
        expected_states: Iterable[UiState] = (),
    ) -> WaitResult:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise AlfaUiError("timeout must be greater than zero and at most 300 seconds")
        expected_set = frozenset(expected_states)
        failure_set = frozenset(fail_on) - expected_set
        started = self.monotonic()
        deadline = started + timeout_seconds
        attempts = 0
        consecutive_errors = 0
        last_snapshot = UiSnapshot(
            xml="<hierarchy rotation=\"0\"></hierarchy>",
            nodes=(),
            states=frozenset((UiState.UNKNOWN,)),
            primary=UiState.UNKNOWN,
        )
        last_primary: UiState | None = None

        while True:
            attempts += 1
            try:
                snapshot = classify_ui(self.adb.dump_ui())
                last_snapshot = snapshot
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                elapsed = self.monotonic() - started
                self._log(
                    "ui_dump_retry",
                    operation=operation,
                    attempt=attempts,
                    consecutive_errors=consecutive_errors,
                    elapsed_seconds=round(elapsed, 6),
                    error=f"{type(exc).__name__}: {exc}",
                )
                if consecutive_errors > self.max_consecutive_dump_errors:
                    prefix = self._capture_evidence(
                        operation=operation,
                        outcome=WaitOutcome.FAILURE,
                        snapshot=last_snapshot,
                        attempts=attempts,
                        elapsed_seconds=elapsed,
                        error=f"UI dump failed: {type(exc).__name__}: {exc}",
                    )
                    return WaitResult(
                        outcome=WaitOutcome.FAILURE,
                        snapshot=last_snapshot,
                        attempts=attempts,
                        elapsed_seconds=elapsed,
                        evidence_prefix=prefix,
                    )
                now = self.monotonic()
                if now >= deadline:
                    break
                self.sleep(min(self.interval_seconds, deadline - now))
                continue

            elapsed = self.monotonic() - started
            if snapshot.primary != last_primary:
                self._log(
                    "ui_state_changed",
                    operation=operation,
                    attempt=attempts,
                    elapsed_seconds=round(elapsed, 6),
                    primary=snapshot.primary.value,
                    states=sorted(state.value for state in snapshot.states),
                )
                last_primary = snapshot.primary
            else:
                self._log(
                    "ui_poll",
                    operation=operation,
                    attempt=attempts,
                    elapsed_seconds=round(elapsed, 6),
                    primary=snapshot.primary.value,
                )

            try:
                matched = predicate(snapshot)
            except Exception as exc:
                prefix = self._capture_evidence(
                    operation=operation,
                    outcome=WaitOutcome.FAILURE,
                    snapshot=snapshot,
                    attempts=attempts,
                    elapsed_seconds=elapsed,
                    error=(
                        f"state predicate failed: {type(exc).__name__}: {exc}"
                    ),
                )
                return WaitResult(
                    outcome=WaitOutcome.FAILURE,
                    snapshot=snapshot,
                    attempts=attempts,
                    elapsed_seconds=elapsed,
                    evidence_prefix=prefix,
                )
            if matched:
                self._log(
                    "ui_wait_matched",
                    operation=operation,
                    attempts=attempts,
                    elapsed_seconds=round(elapsed, 6),
                    primary=snapshot.primary.value,
                )
                return WaitResult(
                    outcome=WaitOutcome.MATCHED,
                    snapshot=snapshot,
                    attempts=attempts,
                    elapsed_seconds=elapsed,
                )
            if snapshot.states & failure_set:
                prefix = self._capture_evidence(
                    operation=operation,
                    outcome=WaitOutcome.FAILURE,
                    snapshot=snapshot,
                    attempts=attempts,
                    elapsed_seconds=elapsed,
                    error=(
                        "unexpected terminal UI state: "
                        + ",".join(
                            sorted(
                                state.value
                                for state in snapshot.states & failure_set
                            )
                        )
                    ),
                )
                return WaitResult(
                    outcome=WaitOutcome.FAILURE,
                    snapshot=snapshot,
                    attempts=attempts,
                    elapsed_seconds=elapsed,
                    evidence_prefix=prefix,
                )
            now = self.monotonic()
            if now >= deadline:
                break
            self.sleep(min(self.interval_seconds, deadline - now))

        elapsed = self.monotonic() - started
        timeout_snapshot = UiSnapshot(
            xml=last_snapshot.xml,
            nodes=last_snapshot.nodes,
            states=frozenset(set(last_snapshot.states) | {UiState.TIMEOUT}),
            primary=UiState.TIMEOUT,
        )
        prefix = self._capture_evidence(
            operation=operation,
            outcome=WaitOutcome.TIMEOUT,
            snapshot=timeout_snapshot,
            attempts=attempts,
            elapsed_seconds=elapsed,
            error=f"timeout after {timeout_seconds}s",
        )
        return WaitResult(
            outcome=WaitOutcome.TIMEOUT,
            snapshot=timeout_snapshot,
            attempts=attempts,
            elapsed_seconds=elapsed,
            evidence_prefix=prefix,
        )


class SubprocessAdb:
    """ADB boundary used by the reusable controller CLI."""

    def __init__(self, serial: str | None = None):
        self.serial = serial

    def _base(self) -> list[str]:
        command = ["adb"]
        if self.serial:
            command += ["-s", self.serial]
        return command

    def _run(
        self,
        command: list[str],
        *,
        timeout: float,
        binary: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                text=not binary,
            )
        except subprocess.TimeoutExpired as exc:
            raise AlfaUiError(f"ADB command timed out after {timeout}s") from exc
        if check and result.returncode != 0:
            stderr = (
                result.stderr.decode(errors="replace")
                if isinstance(result.stderr, bytes)
                else result.stderr
            )
            raise AlfaUiError(
                f"ADB command failed ({result.returncode}): {str(stderr).strip()}"
            )
        return result

    def resolve_serial(self) -> str:
        result = self._run(["adb", "devices"], timeout=10)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device":
                devices.append(fields[0])
        if self.serial:
            if self.serial not in devices:
                raise AlfaUiError(f"ADB device {self.serial!r} is unavailable")
            return self.serial
        if len(devices) != 1:
            raise AlfaUiError(
                f"expected exactly one authorized ADB device, found {devices}"
            )
        self.serial = devices[0]
        return self.serial

    def foreground_package(self) -> str:
        result = self._run(
            self._base() + ["shell", "dumpsys", "window", "windows"],
            timeout=15,
        )
        focus = next(
            (line for line in result.stdout.splitlines() if "mCurrentFocus" in line),
            "",
        )
        if PACKAGE not in focus:
            raise AlfaUiError(f"AlfaOBD is not foreground: {focus.strip()!r}")
        return PACKAGE

    def dump_ui(self) -> str:
        remote = f"/sdcard/window-obd-things-{os.getpid()}.xml"
        script = (
            f"uiautomator dump --compressed {remote} >/dev/null && cat {remote}"
        )
        result = self._run(
            self._base() + ["exec-out", "sh", "-c", script],
            timeout=DEFAULT_DUMP_TIMEOUT_SECONDS,
        )
        return result.stdout

    def screenshot(self) -> bytes:
        result = self._run(
            self._base() + ["exec-out", "screencap", "-p"],
            timeout=20,
            binary=True,
        )
        return result.stdout

    def tap(self, node: UiNode) -> None:
        if not node.clickable or not node.enabled:
            raise AlfaUiError(
                f"refusing tap on disabled/nonclickable node {node.resource_id!r}"
            )
        x, y = node.bounds.center
        self._run(
            self._base() + ["shell", "input", "tap", str(x), str(y)],
            timeout=10,
        )


@dataclass(frozen=True)
class SafeAction:
    name: str
    button_text: str
    required: frozenset[UiState]
    forbidden_if_present: frozenset[UiState]
    expected: frozenset[UiState]
    timeout_seconds: float
    diagnostic_confirmation: bool


SAFE_ACTIONS: dict[str, SafeAction] = {
    "connect": SafeAction(
        name="connect",
        button_text="CONNECT",
        required=frozenset((UiState.DISCONNECTED,)),
        forbidden_if_present=frozenset((UiState.CONNECTED,)),
        expected=frozenset(
            (UiState.CONNECTED, UiState.ADAPTER_PROMPT, UiState.ISO_WARNING)
        ),
        timeout_seconds=60,
        diagnostic_confirmation=True,
    ),
    "disconnect": SafeAction(
        name="disconnect",
        button_text="DISCONNECT",
        required=frozenset((UiState.CONNECTED,)),
        forbidden_if_present=frozenset((UiState.DISCONNECTED,)),
        expected=frozenset((UiState.DISCONNECTED, UiState.MAIN)),
        timeout_seconds=20,
        diagnostic_confirmation=False,
    ),
    "adapter-ok": SafeAction(
        name="adapter-ok",
        button_text="OK",
        required=frozenset((UiState.ADAPTER_PROMPT,)),
        forbidden_if_present=frozenset(),
        expected=frozenset(
            (UiState.CONNECTING, UiState.CONNECTED, UiState.ISO_WARNING)
        ),
        timeout_seconds=60,
        diagnostic_confirmation=True,
    ),
    "iso-continue": SafeAction(
        name="iso-continue",
        button_text="CONTINUE",
        required=frozenset((UiState.ISO_WARNING,)),
        forbidden_if_present=frozenset(),
        expected=frozenset((UiState.CONNECTED,)),
        timeout_seconds=60,
        diagnostic_confirmation=True,
    ),
    "read-system-id": SafeAction(
        name="read-system-id",
        button_text="READ SYSTEM ID",
        required=frozenset((UiState.CONNECTED, UiState.SYSTEM_STATUS_EMPTY)),
        forbidden_if_present=frozenset((UiState.SYSTEM_ID_POPULATED,)),
        expected=frozenset((UiState.SYSTEM_ID_POPULATED,)),
        timeout_seconds=30,
        diagnostic_confirmation=True,
    ),
    "read-faults": SafeAction(
        name="read-faults",
        button_text="READ ALL FAULTS",
        required=frozenset((UiState.CONNECTED, UiState.FAULTS_EMPTY)),
        forbidden_if_present=frozenset(
            (UiState.FAULTS_POPULATED, UiState.NO_FAULTS)
        ),
        expected=frozenset((UiState.FAULTS_POPULATED, UiState.NO_FAULTS)),
        timeout_seconds=45,
        diagnostic_confirmation=True,
    ),
}


class GuardedController:
    """State-check, single-tap, state-wait controller for allowlisted actions."""

    def __init__(self, adb: SubprocessAdb, poller: UiPoller):
        self.adb = adb
        self.poller = poller

    @staticmethod
    def _button(snapshot: UiSnapshot, text: str) -> UiNode:
        matches = [
            node
            for node in snapshot.nodes
            if node.text == text and node.clickable and node.enabled
        ]
        if len(matches) != 1:
            raise AlfaUiError(
                f"expected one enabled clickable {text!r} button, found {len(matches)}"
            )
        return matches[0]

    def perform(
        self,
        action_name: str,
        *,
        confirmed_read_only_diagnostics: bool,
    ) -> WaitResult:
        try:
            action = SAFE_ACTIONS[action_name]
        except KeyError as exc:
            raise AlfaUiError(f"unsupported/unsafe action {action_name!r}") from exc
        if action.diagnostic_confirmation and not confirmed_read_only_diagnostics:
            raise AlfaUiError(
                f"{action_name} requires explicit read-only diagnostic confirmation"
            )
        before = self.poller.observe()
        if action.forbidden_if_present & before.states:
            raise AlfaUiError(
                f"refusing duplicate/stale {action_name}: "
                f"states={','.join(sorted(state.value for state in before.states))}"
            )
        if not action.required.issubset(before.states):
            raise AlfaUiError(
                f"refusing {action_name}; required "
                f"{','.join(sorted(state.value for state in action.required))}, observed "
                f"{','.join(sorted(state.value for state in before.states))}"
            )
        button = self._button(before, action.button_text)
        self.poller._log(
            "ui_tap_intent",
            action=action_name,
            text=button.text,
            resource_id=button.resource_id,
            bounds=[
                button.bounds.left,
                button.bounds.top,
                button.bounds.right,
                button.bounds.bottom,
            ],
        )
        # A tap is never retried: command failure leaves delivery ambiguous.
        self.adb.tap(button)
        self.poller._log("ui_tap_returned", action=action_name)
        return self.poller.wait_for_states(
            operation=action_name,
            expected=action.expected,
            timeout_seconds=action.timeout_seconds,
            fail_on=(
                UiState.FAILURE,
                UiState.ADAPTER_PROMPT,
                UiState.ISO_WARNING,
            ),
        )
