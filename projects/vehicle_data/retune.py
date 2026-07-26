#!/usr/bin/env python3
"""Guarded listen-only bitrate retuning for the telemetry collector.

This helper runs as a separate process so interface cleanup remains protected by
the repository's main-thread termination guard. It never transmits CAN frames.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib import can_operation_state, canbus, diagnostic_safety


SUPPORTED_BITRATES = frozenset((canbus.BITRATE_CCAN, canbus.BITRATE_BCAN))
BITRATE_BY_BUS = {
    "c-can": canbus.BITRATE_CCAN,
    "b-can": canbus.BITRATE_BCAN,
    "can-ch": canbus.BITRATE_CANCH,
}
PAIR_BY_BUS = {"c-can": "6/14", "b-can": "3/11", "can-ch": "12/13"}
RETUNABLE_CONTROLLER_STATES = frozenset(
    ("ERROR-ACTIVE", "ERROR-WARNING", "ERROR-PASSIVE")
)
CONFLICTING_SYSTEMD_UNITS = (
    "tpms-logger.service",
    "tpms-drivesniff.service",
)


def _result(
    state: str,
    reason: str,
    detail: str,
    *,
    from_bitrate: int | None = None,
    to_bitrate: int | None = None,
    bus: str | None = None,
) -> dict[str, object]:
    return {
        "state": state,
        "reason": reason,
        "detail": detail,
        "from_bitrate": from_bitrate,
        "to_bitrate": to_bitrate,
        "bus": bus,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def _set_topology(channel: str, bus: str, *, note: str) -> None:
    can_operation_state.set_topology(
        channel,
        bus,
        pair=PAIR_BY_BUS.get(bus, ""),
        source="vehicle_telemetry_auto_retune",
        note=note,
    )


def _active_conflicting_services() -> tuple[str, ...]:
    active = []
    for unit in CONFLICTING_SYSTEMD_UNITS:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"cannot inspect conflicting service {unit}: {exc}"
            ) from exc
        if result.returncode == 0:
            active.append(unit)
        elif result.returncode not in (3, 4):
            raise RuntimeError(
                f"cannot determine whether {unit} is active "
                f"(systemctl exit {result.returncode})"
            )
    return tuple(active)


def _passive_configuration_matches(
    actual: canbus.InterfaceState,
    expected: canbus.InterfaceState,
) -> bool:
    """Compare link configuration while allowing error-counter recovery.

    A wrong bitrate can push a listen-only controller into ERROR-WARNING or
    ERROR-PASSIVE. Reapplying the original passive configuration legitimately
    resets that transient state to ERROR-ACTIVE.
    """
    expected_restart_ms = (
        expected.restart_ms if expected.restart_ms is not None else 0
    )
    return (
        actual.channel == expected.channel
        and actual.present
        and actual.up
        and actual.bitrate == expected.bitrate
        and actual.listen_only == expected.listen_only
        and actual.restart_ms == expected_restart_ms
        and actual.controller_state in RETUNABLE_CONTROLLER_STATES
    )


def _restore_starting_configuration(
    initial: canbus.InterfaceState,
) -> bool:
    current = canbus.interface_state(initial.channel)
    if initial.same_configuration(current):
        return True
    if not canbus.bring_up_passive(
        initial.channel,
        initial.bitrate,
        restart_ms=initial.restart_ms if initial.restart_ms is not None else 0,
        noninteractive=True,
    ):
        return False
    return _passive_configuration_matches(
        canbus.interface_state(initial.channel), initial
    )


def attempt_passive_retune(
    channel: str,
    expected_bitrate: int,
    *,
    probe_seconds: float = 0.75,
) -> dict[str, object]:
    """Try the other approved bitrate after independently confirming wrong-rate.

    The caller's evidence is treated only as a trigger. This process takes the
    exclusive channel lock and rechecks all gates. It requires fresh RX-error-
    based ``wrong-rate`` evidence, or the listen-only ERROR-WARNING/ERROR-PASSIVE
    state that such a sample can leave behind, before changing the interface.
    An unrecognized alternate rate is restored to the starting passive
    configuration.
    """
    if expected_bitrate not in SUPPORTED_BITRATES:
        return _result(
            "blocked",
            "unsupported_bitrate",
            f"expected bitrate {expected_bitrate} is not approved for auto-retune",
            from_bitrate=expected_bitrate,
        )
    if probe_seconds <= 0:
        return _result(
            "blocked",
            "invalid_probe",
            "probe duration must be positive",
            from_bitrate=expected_bitrate,
        )

    target_bitrate = (
        canbus.BITRATE_BCAN
        if expected_bitrate == canbus.BITRATE_CCAN
        else canbus.BITRATE_CCAN
    )
    initial = None
    lock_handle = None
    mutation_started = False
    keep_target = False
    outcome = _result(
        "failed",
        "retune_incomplete",
        "auto-retune ended without a result",
        from_bitrate=expected_bitrate,
        to_bitrate=target_bitrate,
    )

    try:
        with diagnostic_safety.interrupt_on_termination() as termination:
            try:
                lock_handle = diagnostic_safety.acquire_channel_lock(channel)
            except diagnostic_safety.ChannelLockError as exc:
                return _result(
                    "blocked",
                    "channel_busy",
                    str(exc),
                    from_bitrate=expected_bitrate,
                    to_bitrate=target_bitrate,
                )

            try:
                conflicting_services = _active_conflicting_services()
                if conflicting_services:
                    return _result(
                        "blocked",
                        "service_conflict",
                        "auto-retune blocked while active service(s) may own "
                        "or reconfigure can0: "
                        + ",".join(conflicting_services),
                        from_bitrate=expected_bitrate,
                        to_bitrate=target_bitrate,
                    )

                initial = canbus.interface_state(channel)
                if not initial.present:
                    return _result(
                        "blocked",
                        "adapter_absent",
                        f"{channel} is not present",
                        from_bitrate=expected_bitrate,
                        to_bitrate=target_bitrate,
                    )
                if not initial.up or initial.bitrate is None:
                    return _result(
                        "blocked",
                        "interface_down",
                        f"{channel} is down or has no readable bitrate",
                        from_bitrate=expected_bitrate,
                        to_bitrate=target_bitrate,
                    )
                if not initial.listen_only:
                    return _result(
                        "blocked",
                        "interface_armed",
                        f"{channel} is armed; refusing passive auto-retune",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )
                if initial.controller_state not in RETUNABLE_CONTROLLER_STATES:
                    return _result(
                        "blocked",
                        "controller_unhealthy",
                        f"{channel} controller is "
                        f"{initial.controller_state or 'unavailable'}",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )
                if initial.bitrate != expected_bitrate:
                    return _result(
                        "blocked",
                        "configuration_changed",
                        f"{channel} changed from expected {expected_bitrate} "
                        f"to {initial.bitrate} bit/s before retune",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )

                inhibits = can_operation_state.active_inhibits(channel)
                if inhibits:
                    names = ",".join(
                        str(item.get("name", "invalid")) for item in inhibits
                    )
                    return _result(
                        "blocked",
                        "external_inhibit",
                        f"auto-retune inhibited by {names}",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )

                current_bus = canbus.identify_bus(
                    channel, probe=probe_seconds
                )
                if current_bus in BITRATE_BY_BUS:
                    return _result(
                        "no_change",
                        "evidence_cleared",
                        f"fresh passive probe identifies {current_bus}; "
                        "retune no longer needed",
                        from_bitrate=initial.bitrate,
                        to_bitrate=initial.bitrate,
                        bus=current_bus,
                    )
                degraded_wrong_rate_evidence = (
                    initial.controller_state
                    in ("ERROR-WARNING", "ERROR-PASSIVE")
                    and current_bus in ("wrong-rate", "silent", "unknown")
                )
                if current_bus != "wrong-rate" and not degraded_wrong_rate_evidence:
                    return _result(
                        "blocked",
                        "insufficient_evidence",
                        f"fresh passive probe returned {current_bus}; "
                        "only wrong-rate evidence permits retuning",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )

                try:
                    _set_topology(
                        channel,
                        "unknown",
                        note="invalidated before passive telemetry auto-retune",
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    return _result(
                        "failed",
                        "topology_invalidation_failed",
                        f"could not invalidate topology before retune: {exc}",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )

                mutation_started = True
                configured = canbus.bring_up_passive(
                    channel,
                    target_bitrate,
                    restart_ms=0,
                    noninteractive=True,
                )
                if not configured:
                    outcome = _result(
                        "failed",
                        "reconfigure_failed",
                        f"could not configure and verify {channel} listen-only "
                        f"at {target_bitrate} bit/s; noninteractive privilege "
                        "or interface readback may be unavailable",
                        from_bitrate=initial.bitrate,
                        to_bitrate=target_bitrate,
                    )
                else:
                    target_state = canbus.interface_state(channel)
                    if (
                        not target_state.present
                        or not target_state.up
                        or not target_state.listen_only
                        or target_state.bitrate != target_bitrate
                        or target_state.controller_state != "ERROR-ACTIVE"
                        or target_state.restart_ms != 0
                    ):
                        outcome = _result(
                            "failed",
                            "target_state_unverified",
                            f"{channel} did not verify listen-only, ERROR-ACTIVE, "
                            f"{target_bitrate} bit/s, restart-ms 0 after retune",
                            from_bitrate=initial.bitrate,
                            to_bitrate=target_bitrate,
                        )
                    else:
                        detected = canbus.identify_bus(
                            channel, probe=probe_seconds
                        )
                        if (
                            detected not in BITRATE_BY_BUS
                            or BITRATE_BY_BUS[detected] != target_bitrate
                        ):
                            outcome = _result(
                                "failed",
                                "alternate_not_identified",
                                f"alternate passive probe returned {detected}; "
                                "restoring the previous configuration",
                                from_bitrate=initial.bitrate,
                                to_bitrate=target_bitrate,
                                bus=detected,
                            )
                        else:
                            try:
                                _set_topology(
                                    channel,
                                    detected,
                                    note=(
                                        "passively identified after guarded "
                                        "telemetry bitrate auto-retune"
                                    ),
                                )
                            except (OSError, RuntimeError, ValueError) as exc:
                                outcome = _result(
                                    "failed",
                                    "topology_record_failed",
                                    f"identified {detected} but could not record "
                                    f"the topology safely: {exc}",
                                    from_bitrate=initial.bitrate,
                                    to_bitrate=target_bitrate,
                                    bus=detected,
                                )
                            else:
                                keep_target = True
                                if detected == "can-ch":
                                    outcome = _result(
                                        "switched",
                                        "unsupported_bus_detected",
                                        "passively switched to CAN-CH; no approved "
                                        "battery-voltage source exists on that bus",
                                        from_bitrate=initial.bitrate,
                                        to_bitrate=target_bitrate,
                                        bus=detected,
                                    )
                                else:
                                    outcome = _result(
                                        "switched",
                                        "bus_identified",
                                        f"passively switched to {detected} at "
                                        f"{target_bitrate} bit/s",
                                        from_bitrate=initial.bitrate,
                                        to_bitrate=target_bitrate,
                                        bus=detected,
                                    )
            except (OSError, RuntimeError, ValueError) as exc:
                outcome = _result(
                    "failed",
                    "helper_error",
                    f"auto-retune helper failed closed: {exc}",
                    from_bitrate=(
                        initial.bitrate
                        if initial is not None
                        else expected_bitrate
                    ),
                    to_bitrate=target_bitrate,
                )
            finally:
                termination.begin_cleanup()
                try:
                    if mutation_started and not keep_target and initial is not None:
                        restored = _restore_starting_configuration(initial)
                        if not restored:
                            outcome = _result(
                                "failed",
                                "restoration_failed",
                                "could not verify exact SocketCAN restoration "
                                "after an unsuccessful auto-retune",
                                from_bitrate=initial.bitrate,
                                to_bitrate=target_bitrate,
                            )
                finally:
                    diagnostic_safety.release_channel_lock(lock_handle)
    except (OSError, RuntimeError, ValueError) as exc:
        return _result(
            "failed",
            "helper_error",
            f"auto-retune helper failed closed: {exc}",
            from_bitrate=(
                initial.bitrate if initial is not None else expected_bitrate
            ),
            to_bitrate=target_bitrate,
        )
    return outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Retune one SocketCAN interface only after fresh passive "
            "wrong-bitrate evidence."
        )
    )
    parser.add_argument("--channel", default="can0")
    parser.add_argument("--expected-bitrate", type=int, required=True)
    parser.add_argument("--probe-seconds", type=float, default=0.75)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = attempt_passive_retune(
        args.channel,
        args.expected_bitrate,
        probe_seconds=args.probe_seconds,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
