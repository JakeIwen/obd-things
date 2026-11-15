"""Restart the telemetry broker to load changed code, with its safety checks.

Run from the repo root (`s restart_broker` does this by name):

    python3 -m projects.vehicle_data.restart_broker [--check] [--while-running]

Default (parked): restart only while the van is asleep, with no helper, inhibit,
outside owner or active request; afterwards confirm the units are active and
report whether any CAN transmit counter moved.

`--while-running`: restart with the van on. The broker's helper hold
(`helper_hold.py`) stops both drive helpers first; the restart goes ahead only
once every helper reports a verified (or never-started) stop and this command's
own read of each vehicle adapter shows listen-only and ERROR-ACTIVE. Any failure
releases the hold and exits nonzero with no restart. Costs: the web units
restart with the broker (the tablet reconnects within seconds), the history and
drive recording have a gap, and a failed restoration latches the broker's usual
inhibit (passive-only telemetry for the drive until someone inspects the
adapters and ends it).

`--check` runs the chosen mode's preflight and exits without holding or restarting.
Every effect (HTTP, subprocess, sysfs, clock) goes through `Environment` so the
tests need no live system.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from lib import canbus, can_operation_state
from lib.vehicle_can_roles import CAN_BUS_ROLES
from projects.vehicle_data import helper_hold
from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.broker import DEFAULT_SOCKET
from projects.vehicle_data.can_interfaces import PassiveInterfaceManager

WEB_BASE = "http://192.168.6.103:8765"
UNIT = "van-telemetry.service"
UNITS = (UNIT, "van-telemetry-web.service", "van-telemetry-web-tailscale.service")
RECORDER_UNIT = "van-drive-recorder.service"
RESTART_COMMAND = ["sudo", "-n", "/usr/bin/systemctl", "restart", UNIT]

PREFLIGHT_ATTEMPTS = 6
PREFLIGHT_RETRY_SECONDS = 5.0
SETTLE_SECONDS = 90.0
EVENT_ATTEMPTS = 10
EVENT_RETRY_SECONDS = 2.0
HOLD_STOP_BOUND_SECONDS = 30.0
HOLD_POLL_SECONDS = 1.0
RELAUNCH_WAIT_CYCLES = 60
HOLD_REASON = "restart_broker"
EXPECTED_CONTROLLER_STATE = "ERROR-ACTIVE"
HELPER_KEYS = ("active_drive", "auxiliary_drive")
# Owner kinds that are the broker's own helpers, not a foreign bus user.
OWN_OWNER_KINDS = ("broker", "broker_active_drive", "broker_auxiliary_drive")

Reply = tuple[int, dict]


def _web_request(method: str, path: str, payload: dict | None) -> Reply:
    request = urllib.request.Request(WEB_BASE + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {}


def _socket_request(method: str, path: str, payload: dict | None) -> Reply:
    return TelemetryClient(DEFAULT_SOCKET, timeout=10.0).request(method, path, payload)


def _run(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, text=True)
    return result.returncode, result.stdout


def _tx_counters() -> str:
    parts = []
    for device in sorted(glob.glob("/sys/class/net/can*")):
        with open(os.path.join(device, "statistics", "tx_packets")) as handle:
            parts.append(f"{os.path.basename(device)}={handle.read().strip()}")
    return " ".join(parts)


def _read_adapters() -> dict[str, object]:
    manager = PassiveInterfaceManager()
    topology = manager.topology()
    return {
        role: canbus.interface_state(manager.channel_for_role(role, topology=topology))
        for role in CAN_BUS_ROLES
    }


@dataclass
class Environment:
    """Every effect the command has, replaceable in tests."""

    web: Callable[[str, str, dict | None], Reply] = _web_request
    socket: Callable[[str, str, dict | None], Reply] = _socket_request
    run: Callable[[list[str]], tuple[int, str]] = _run
    tx_counters: Callable[[], str] = _tx_counters
    file_inhibits: Callable[[], tuple] = can_operation_state.all_active_inhibits
    adapters: Callable[[], dict[str, object]] = _read_adapters
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    out: Callable[[str], None] = field(default=print)


# -- preflight ------------------------------------------------------------


def _helper_pids(status: dict) -> list:
    return [(status.get(key) or {}).get("helper_pid") for key in HELPER_KEYS]


def _operations(status: dict) -> list:
    owner = status.get("current_owner")
    return list(status.get("inflight") or []) + list((owner or {}).get("operations") or [])


def _inhibit_names(status: dict, env: Environment) -> list[str]:
    cached = list((status.get("interface") or {}).get("active_inhibits") or [])
    names = [str(item.get("name", item)) if isinstance(item, dict) else str(item) for item in cached]
    on_disk = [str(item.get("name")) for item in env.file_inhibits()]
    return names + [name for name in on_disk if name not in names]


def _common_blockers(status: dict, env: Environment) -> list[str]:
    """Blockers both modes share: inhibits, foreign owner and active operations."""
    blockers = []
    owner = status.get("current_owner")
    foreign = owner is not None and owner.get("kind") not in OWN_OWNER_KINDS
    active = [op for op in _operations(status) if op.get("mode") != "passive"]
    inhibits = _inhibit_names(status, env)
    if inhibits:
        blockers.append(
            "inhibits are latched: " + ", ".join(inhibits)
            + " (inspect the adapters, then end each with"
            + " tools/can_operation_state.py inhibit-end NAME)"
        )
    elif foreign:
        blockers.append(f"another owner holds the bus: {owner}")
    if active:
        blockers.append(f"active requests are in flight: {active}")
    return blockers


def parked_blockers(status: dict, env: Environment) -> list[str]:
    vehicle = status["vehicle_state"]
    blockers = []
    if vehicle.get("state") != "asleep" or vehicle.get("running"):
        blockers.append(f"vehicle is {vehicle.get('state')}, not asleep")
    if any(_helper_pids(status)):
        blockers.append(f"a drive helper is running: {_helper_pids(status)}")
    return blockers + _common_blockers(status, env)


def _latched(status: dict) -> list[str]:
    return [key for key in HELPER_KEYS if (status.get(key) or {}).get("restoration_failed")]


def running_blockers(status: dict, env: Environment) -> list[str]:
    blockers = _common_blockers(status, env)
    if _latched(status):
        blockers.append(f"restoration latched for: {', '.join(_latched(status))}")
    return blockers + _alignment_blockers(env)


def _alignment_blockers(env: Environment) -> list[str]:
    path = "/v1/radar/service-alignment"
    try:
        code, body = env.socket("GET", path, None)
    except (OSError, RuntimeError, ValueError) as error:
        return [f"could not read the radar alignment state: {error}"]
    if code != 200:
        return [f"radar alignment state answered {code}, so a run cannot be ruled out"]
    run = body.get("run") or {}
    if run.get("state") in ("requested", "starting", "running", "stopping"):
        return [f"a radar alignment run is {run['state']}; stopping its helper would abort it"]
    return []


def _summary(status: dict) -> str:
    vehicle = status["vehicle_state"]
    owner = status.get("current_owner")
    return (
        f"vehicle={vehicle.get('state')} running={vehicle.get('running')} "
        f"helpers={_helper_pids(status)} "
        f"inhibits={(status.get('interface') or {}).get('active_inhibits') or []} "
        f"owner={None if owner is None else owner.get('kind')}"
    )


def _status_once(env: Environment, mode_running: bool) -> dict:
    request = env.socket if mode_running else env.web
    code, body = request("GET", "/v1/status", None)
    if code != 200:
        raise RuntimeError(f"/v1/status answered {code}")
    return body


def preflight(env: Environment, mode_running: bool) -> bool:
    """The script's six tries, 5 s apart: a cached status can lag an ended inhibit."""
    env.out("Preflight:")
    judge = running_blockers if mode_running else parked_blockers
    for attempt in range(1, PREFLIGHT_ATTEMPTS + 1):
        try:
            status = _status_once(env, mode_running)
            env.out(_summary(status))
            blockers = judge(status, env)
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            blockers = [f"status unavailable: {error}"]
        for blocker in blockers:
            env.out(f"BLOCKED: {blocker}")
        if not blockers:
            return True
        if attempt != PREFLIGHT_ATTEMPTS:
            env.out(f"Preflight attempt {attempt} blocked; checking again in 5 s.")
            env.sleep(PREFLIGHT_RETRY_SECONDS)
    return False


# -- hold -----------------------------------------------------------------


class HoldFailure(Exception):
    """The hold could not reach a state where a restart is safe."""


class Hold:
    """The broker's helper hold, with renewal on this command's own clock."""

    def __init__(self, env: Environment):
        self.env = env
        self.renewed_at = 0.0

    def _post(self, payload: dict) -> Reply:
        return self.env.socket("POST", helper_hold.API_PATH, payload)

    def engage(self) -> None:
        code, body = self._post({"hold": True, "reason": HOLD_REASON})
        if code == 404:
            raise HoldFailure("the running broker predates the hold control; "
                              "restart it parked once to install it")
        if code != 202:
            raise HoldFailure(f"hold refused with {code} {body.get('error', '')}".strip())
        self.renewed_at = self.env.clock()

    def renew_if_due(self) -> None:
        if self.env.clock() - self.renewed_at >= helper_hold.RENEW_AFTER_SECONDS:
            self.engage()

    def release(self) -> str:
        try:
            code, _ = self._post({"hold": False})
        except (OSError, RuntimeError, ValueError) as error:
            return f"hold release failed ({error}); it expires on its own within {helper_hold.HOLD_SECONDS:.0f} s"
        return "hold released" if code == 200 else f"hold release answered {code}"

    def wait_for_safe(self) -> None:
        deadline = self.env.clock() + HOLD_STOP_BOUND_SECONDS
        while True:
            self.renew_if_due()
            states = self._helper_states()
            unsafe = {k: v for k, v in states.items() if v not in helper_hold.SAFE_STATES}
            self.env.out(f"helpers: { {k: v.value for k, v in states.items()} }")
            if not unsafe:
                return
            for key, state in unsafe.items():
                if state is helper_hold.HelperHoldState.STOPPED_UNVERIFIED:
                    raise HoldFailure(
                        f"{key} stopped without a verified restoration; the broker "
                        "latched its inhibit, so active telemetry stays off until the "
                        "adapters are inspected and the inhibit is ended"
                    )
            if self.env.clock() >= deadline:
                raise HoldFailure(f"helpers still not stopped after {HOLD_STOP_BOUND_SECONDS:.0f} s: "
                                  f"{ {k: v.value for k, v in unsafe.items()} }")
            self.env.sleep(HOLD_POLL_SECONDS)

    def _helper_states(self) -> dict[str, helper_hold.HelperHoldState]:
        code, body = self.env.socket("GET", helper_hold.API_PATH, None)
        if code != 200:
            raise HoldFailure(f"hold status answered {code}")
        helpers = body.get("helpers") or {}
        try:
            return {key: helper_hold.HelperHoldState(helpers[key]["state"]) for key in helper_hold.HELPERS}
        except (KeyError, TypeError, ValueError) as error:
            raise HoldFailure(f"hold status has an unknown helper state: {error!r}") from error


# -- adapters -------------------------------------------------------------


def adapter_problems(env: Environment, skip_roles: tuple[str, ...] = ()) -> list[str]:
    try:
        states = env.adapters()
    except Exception as error:  # topology or ip failures must fail closed
        return [f"could not read the adapters: {error}"]
    problems = []
    for role, state in states.items():
        if role in skip_roles:
            continue
        if not state.present or not state.listen_only:
            problems.append(f"{role} ({state.channel}) is not listen-only")
        elif state.controller_state != EXPECTED_CONTROLLER_STATE:
            problems.append(f"{role} ({state.channel}) controller state is {state.controller_state}")
    return problems


# -- restart and postchecks ----------------------------------------------


def _unit_active(env: Environment, unit: str) -> bool:
    code, out = env.run(["systemctl", "is-active", unit])
    env.out(f"{unit}: {out.strip()}")
    return code == 0


def _main_pid(env: Environment, unit: str) -> str:
    return env.run(["systemctl", "show", "-p", "MainPID", "--value", unit])[1].strip()


def _open_events(env: Environment) -> int:
    for _ in range(EVENT_ATTEMPTS):
        try:
            code, body = env.web("GET", "/v1/events?status=open", None)
        except (OSError, ValueError):
            code, body = 0, {}
        if code == 200:
            events = body.get("events") or []
            env.out(f"open events: {len(events)}")
            for event in events:
                env.out(f"   {event.get('id')} {event.get('title')} | opened {event.get('opened_at')}")
            return 0
        env.sleep(EVENT_RETRY_SECONDS)
    env.out("event index did not answer")
    return 1


def _restart(env: Environment) -> bool:
    code, _ = env.run(RESTART_COMMAND)
    if code != 0:
        env.out(f"systemctl restart failed with exit status {code}")
        return False
    env.out(f"Restarted. Waiting {SETTLE_SECONDS:.0f} s for the first evaluations.")
    env.sleep(SETTLE_SECONDS)
    return True


def _recorder_line(env: Environment, before: str) -> None:
    after = _main_pid(env, RECORDER_UNIT)
    verdict = "kept its process" if before == after else "WARNING: was restarted"
    env.out(f"{RECORDER_UNIT} {verdict} (pid {before} -> {after})")


def _tx_line(env: Environment, before: str) -> None:
    after = env.tx_counters()
    env.out(f"TX after:  {after}")
    if before != after:
        env.out("WARNING: a CAN transmit counter changed across the restart.")


def run_parked(env: Environment, check: bool) -> int:
    if not preflight(env, False):
        env.out("Not restarting: the van must be asleep with no helper, outside owner, "
                "active request or inhibit.")
        return 1
    before, recorder = env.tx_counters(), _main_pid(env, RECORDER_UNIT)
    env.out(f"TX before: {before}")
    if check:
        _open_events(env)
        env.out("Check only: nothing was restarted.")
        return 0
    if not _restart(env):
        return 1
    if not all([_unit_active(env, unit) for unit in UNITS]):
        return 1
    _tx_line(env, before)
    _recorder_line(env, recorder)
    return _open_events(env)


def _running_postchecks(env: Environment) -> list[str]:
    status = _status_once(env, True)
    problems = _common_blockers(status, env) + [f"restoration latched: {k}" for k in _latched(status)]
    pids = _helper_pids(status)
    skip = tuple(role for role, pid in zip(("c-can", "b-can"), pids) if pid)
    problems += adapter_problems(env, skip)
    if (status["vehicle_state"].get("running") or False) and not all(pids):
        pids = _wait_for_relaunch(env, status)
    env.out(f"helper pids after restart: {pids}")
    return problems


def _wait_for_relaunch(env: Environment, status: dict) -> list:
    cycle = float((status.get("collector") or {}).get("interval_seconds") or 1.0)
    pids = _helper_pids(status)
    for _ in range(RELAUNCH_WAIT_CYCLES):
        if all(pids) or not _status_once(env, True)["vehicle_state"].get("running"):
            break
        env.sleep(cycle)
        pids = _helper_pids(_status_once(env, True))
    return pids


def _hold_and_verify(env: Environment, hold: Hold) -> str | None:
    """Return the reason a restart is unsafe, or None when it is safe."""
    try:
        hold.engage()
        hold.wait_for_safe()
    except HoldFailure as error:
        return str(error)
    except (OSError, RuntimeError, ValueError) as error:
        return f"the hold control could not be reached: {error}"
    problems = adapter_problems(env)
    return "; ".join(problems) if problems else None


def run_while_running(env: Environment, check: bool) -> int:
    if not preflight(env, True):
        env.out("Not restarting: inhibits, a foreign owner, active requests, a latched "
                "restoration or a radar alignment run block it.")
        return 1
    before, recorder = env.tx_counters(), _main_pid(env, RECORDER_UNIT)
    env.out(f"TX before: {before}")
    if check:
        _open_events(env)
        env.out("Check only: nothing was held or restarted.")
        return 0
    hold = Hold(env)
    reason = _hold_and_verify(env, hold)
    if reason is not None:
        env.out(f"NOT RESTARTING: {reason}")
        env.out(hold.release())
        return 1
    try:
        hold.renew_if_due()
    except (HoldFailure, OSError, RuntimeError, ValueError) as error:
        env.out(f"NOT RESTARTING: hold renewal failed: {error}")
        env.out(hold.release())
        return 1
    return _restart_and_check(env, before, recorder)


def _restart_and_check(env: Environment, before: str, recorder: str) -> int:
    if not _restart(env):
        return 1
    failed = not all([_unit_active(env, unit) for unit in UNITS])
    try:
        problems = _running_postchecks(env)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        problems = [f"status unavailable after the restart: {error}"]
    for problem in problems:
        env.out(f"POSTCHECK FAILED: {problem}")
    _tx_line(env, before)
    _recorder_line(env, recorder)
    return 1 if failed or problems else _open_events(env)


def main(argv: list[str] | None = None, env: Environment | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="preflight only")
    parser.add_argument("--while-running", action="store_true",
                        help="restart with the van on, holding the drive helpers first")
    args = parser.parse_args(argv)
    env = env or Environment()
    runner = run_while_running if args.while_running else run_parked
    return runner(env, args.check)


if __name__ == "__main__":
    sys.exit(main())
