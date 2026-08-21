#!/usr/bin/env python3
"""Observe AlfaOBD UI state or perform one allowlisted read-only UI action.

The controller runs locally on the Pi so each wait uses one persistent process
and the already-running ADB server.  It intentionally exposes no generic tap
command and no write/clear/routine/configuration/PROXI action.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.alfaobd_adb import (  # noqa: E402
    DEFAULT_FAILURE_ROOT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    AlfaUiError,
    GuardedController,
    SAFE_ACTIONS,
    SubprocessAdb,
    UiPoller,
    UiState,
    WaitOutcome,
)
from lib import can_operation_state  # noqa: E402


ALFA_INHIBIT_NAME = "alfaobd"


def _event_logger(path: Path | None):
    def emit(event: str, fields: dict[str, object]) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, sort_keys=True)
        print(line, file=sys.stderr, flush=True)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    return emit


def _snapshot_payload(snapshot) -> dict[str, object]:
    return {
        "primary": snapshot.primary.value,
        "states": sorted(state.value for state in snapshot.states),
    }


def _result_payload(result) -> dict[str, object]:
    return {
        "outcome": result.outcome.value,
        "attempts": result.attempts,
        "elapsed_seconds": round(result.elapsed_seconds, 6),
        "evidence_prefix": result.evidence_prefix,
        **_snapshot_payload(result.snapshot),
    }


def _parse_states(values: list[str]) -> frozenset[UiState]:
    names: list[str] = []
    for value in values:
        names.extend(part.strip() for part in value.split(",") if part.strip())
    if not names:
        raise AlfaUiError("at least one --expect state is required")
    parsed: set[UiState] = set()
    for name in names:
        try:
            parsed.add(UiState(name))
        except ValueError as exc:
            known = ", ".join(state.value for state in UiState)
            raise AlfaUiError(f"unknown UI state {name!r}; known: {known}") from exc
    if UiState.TIMEOUT in parsed:
        raise AlfaUiError("timeout is an outcome and cannot be an expected state")
    return frozenset(parsed)


def _add_live_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adb-serial")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="delay between completed UI dumps (0.5-1.0 seconds)",
    )
    parser.add_argument(
        "--failure-root",
        type=Path,
        default=REPO / DEFAULT_FAILURE_ROOT,
    )
    parser.add_argument("--event-log", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    observe = subparsers.add_parser("observe", help="read and classify one UI dump")
    _add_live_options(observe)

    wait = subparsers.add_parser("wait", help="poll until an expected UI state")
    _add_live_options(wait)
    wait.add_argument("--expect", action="append", required=True)
    wait.add_argument("--timeout", type=float, required=True)

    action = subparsers.add_parser(
        "action",
        help="perform one state-guarded, allowlisted read-only UI action",
    )
    _add_live_options(action)
    action.add_argument("name", choices=sorted(SAFE_ACTIONS))
    action.add_argument(
        "--execute",
        action="store_true",
        help="required before any tap is sent",
    )
    action.add_argument(
        "--confirm-read-only-diagnostics",
        action="store_true",
        help="required for actions that can cause diagnostic reads/connection traffic",
    )

    subparsers.add_parser(
        "campaign-begin",
        help="inhibit Pi-side active CAN operations during an external AlfaOBD campaign",
    )
    subparsers.add_parser(
        "campaign-end",
        help="explicitly release the AlfaOBD external-campaign inhibit",
    )
    subparsers.add_parser(
        "campaign-status",
        help="show the global AlfaOBD external-campaign inhibit state",
    )
    return parser


def _live_objects(args):
    adb = SubprocessAdb(args.adb_serial)
    serial = adb.resolve_serial()
    adb.foreground_package()
    poller = UiPoller(
        adb,
        interval_seconds=args.poll_interval,
        failure_root=args.failure_root,
        logger=_event_logger(args.event_log),
    )
    return serial, adb, poller


def _inhibit_for_adapter_prompt(snapshot) -> None:
    if UiState.ADAPTER_PROMPT not in snapshot.states:
        return
    can_operation_state.begin_inhibit(
        ALFA_INHIBIT_NAME,
        channel="*",
        reason="AlfaOBD adapter prompt requires external campaign review",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "campaign-begin":
            result = can_operation_state.begin_inhibit(
                ALFA_INHIBIT_NAME,
                channel="*",
                reason="explicit AlfaOBD campaign begin",
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "campaign-end":
            print(
                json.dumps(
                    {
                        "name": ALFA_INHIBIT_NAME,
                        "removed": can_operation_state.end_inhibit(
                            ALFA_INHIBIT_NAME
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "campaign-status":
            print(
                json.dumps(
                    {
                        "name": ALFA_INHIBIT_NAME,
                        "active_inhibits": [
                            item
                            for item in can_operation_state.all_active_inhibits()
                            if item.get("name") == ALFA_INHIBIT_NAME
                        ],
                        "scope": "global; no ephemeral SocketCAN topology is recorded",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "action" and not args.execute:
            spec = SAFE_ACTIONS[args.name]
            print(
                json.dumps(
                    {
                        "mode": "plan_only",
                        "action": spec.name,
                        "button_text": spec.button_text,
                        "required_states": sorted(
                            state.value for state in spec.required
                        ),
                        "forbidden_states": sorted(
                            state.value for state in spec.forbidden_if_present
                        ),
                        "expected_states": sorted(
                            state.value for state in spec.expected
                        ),
                        "timeout_seconds": spec.timeout_seconds,
                        "diagnostic_confirmation_required": (
                            spec.diagnostic_confirmation
                        ),
                        "tap_sent": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "action":
            can_operation_state.begin_inhibit(
                ALFA_INHIBIT_NAME,
                channel="*",
                reason=f"AlfaOBD controller action: {args.name}",
            )

        serial, adb, poller = _live_objects(args)
        if args.command == "observe":
            snapshot = poller.observe()
            _inhibit_for_adapter_prompt(snapshot)
            print(
                json.dumps(
                    {"serial": serial, **_snapshot_payload(snapshot)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "wait":
            result = poller.wait_for_states(
                operation="cli_wait",
                expected=_parse_states(args.expect),
                timeout_seconds=args.timeout,
                fail_on=(
                    UiState.FAILURE,
                    UiState.ADAPTER_PROMPT,
                    UiState.ISO_WARNING,
                ),
            )
        else:
            result = GuardedController(adb, poller).perform(
                args.name,
                confirmed_read_only_diagnostics=(
                    args.confirm_read_only_diagnostics
                ),
            )
        _inhibit_for_adapter_prompt(result.snapshot)
        print(
            json.dumps(
                {"serial": serial, **_result_payload(result)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.outcome is WaitOutcome.MATCHED else 2
    except (AlfaUiError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
