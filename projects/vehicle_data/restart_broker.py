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
import contextlib
import fcntl
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

from lib import canbus, can_operation_state, diagnostic_safety
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

READ_TIMEOUT_SECONDS = 10.0
# How long a submitted-but-unconfirmed restart is watched (renewing our hold).
RESOLVE_BOUND_SECONDS = 600.0
RESOLVE_POLL_SECONDS = 5.0
RESTART_TIMEOUT_SECONDS = 120.0
RESTART_TICK_SECONDS = 5.0
REAP_BOUND_SECONDS = 10.0
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
# Owner kinds that are not a foreign bus user. Parked mode allows only the
# broker itself (a helper owner there means a reserved or starting helper);
# running mode also allows the broker's own helpers.
PARKED_OWN_KINDS = ("broker",)
RUNNING_OWN_KINDS = ("broker", "broker_active_drive", "broker_auxiliary_drive")
# Remaining hold time required when a restart is authorized: covers the
# restart command itself (stop budget up to 40 s) with margin to spare.
MIN_HOLD_REMAINING_SECONDS = 60.0
VERIFY_ROUNDS = 3
LOCK_NAME = "restart-broker.lock"

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
    """Every subprocess is bounded; a timeout reads as failure (124) with no output."""
    timeout = RESTART_TIMEOUT_SECONDS if argv == RESTART_COMMAND else READ_TIMEOUT_SECONDS
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    return result.returncode, result.stdout


def _run_watched(argv: list[str], tick: Callable[[], None]) -> tuple[int, str]:
    """Run the restart client as a polled subprocess, calling `tick` every few seconds.

    The tick renews the hold, so no interval without renewal is longer than one
    poll. A client that outlives its bound is killed (its queued job is not),
    which reads as 124 and is classified like any other unconfirmed restart.
    Its output is discarded (DEVNULL) so a descendant such as sudo's child that
    inherits the pipes can never keep a read blocked, and the reap after the
    kill is bounded and keeps ticking.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + RESTART_TIMEOUT_SECONDS
    if _wait_ticking(proc, deadline, tick):
        return proc.returncode, ""
    proc.kill()
    _wait_ticking(proc, time.monotonic() + REAP_BOUND_SECONDS, tick)
    return 124, ""


def _wait_ticking(proc: subprocess.Popen, deadline: float, tick: Callable[[], None]) -> bool:
    """Wait for `proc` until `deadline`, ticking each poll; True once it has exited."""
    while True:
        try:
            proc.wait(timeout=RESTART_TICK_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            tick()
            if time.monotonic() >= deadline:
                return False


def _tx_counters() -> str:
    parts = []
    for device in sorted(glob.glob("/sys/class/net/can*")):
        with open(os.path.join(device, "statistics", "tx_packets")) as handle:
            parts.append(f"{os.path.basename(device)}={handle.read().strip()}")
    return " ".join(parts)


@contextlib.contextmanager
def _exclusive_lock():
    """One restart command at a time; a second one refuses instead of waiting."""
    os.makedirs(diagnostic_safety.LOCK_DIR, exist_ok=True)
    with open(os.path.join(diagnostic_safety.LOCK_DIR, LOCK_NAME), "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("another restart_broker command is running") from error
        yield


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
    run_watched: Callable[[list[str], Callable[[], None]], tuple[int, str]] = _run_watched
    tx_counters: Callable[[], str] = _tx_counters
    file_inhibits: Callable[[], tuple] = can_operation_state.all_active_inhibits
    adapters: Callable[[], dict[str, object]] = _read_adapters
    lock: Callable[[], object] = _exclusive_lock
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


def _common_blockers(status: dict, env: Environment, own_kinds: tuple[str, ...]) -> list[str]:
    """Blockers both modes share: inhibits, foreign owner and active operations.

    The inhibit list also reads the disk files, a deliberate addition to the
    old script (which trusted only the cached status).
    """
    blockers = []
    owner = status.get("current_owner")
    foreign = owner is not None and owner.get("kind") not in own_kinds
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
    return blockers + _common_blockers(status, env, PARKED_OWN_KINDS)


def _latched(status: dict) -> list[str]:
    return [key for key in HELPER_KEYS if (status.get(key) or {}).get("restoration_failed")]


def running_blockers(status: dict, env: Environment) -> list[str]:
    blockers = _common_blockers(status, env, RUNNING_OWN_KINDS)
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


def _states_in(body: dict) -> dict[str, helper_hold.HelperHoldState]:
    helpers = body.get("helpers") or {}
    try:
        return {key: helper_hold.HelperHoldState(helpers[key]["state"]) for key in helper_hold.HELPERS}
    except (KeyError, TypeError, ValueError) as error:
        raise HoldFailure(f"hold status has an unknown helper state: {error!r}") from error


def _unsafe_states(states: dict[str, helper_hold.HelperHoldState]) -> dict:
    """The helpers not yet safe; a stopped_unverified one aborts at once."""
    for key, state in states.items():
        if state is helper_hold.HelperHoldState.STOPPED_UNVERIFIED:
            raise HoldFailure(
                f"{key} stopped without a verified restoration, which may mean the broker "
                "latched its restoration inhibit: active telemetry may stay off until the "
                "adapters are inspected and the inhibit is ended (check /v1/status "
                "interface.active_inhibits)"
            )
    return {k: v for k, v in states.items() if v not in helper_hold.SAFE_STATES}


class Hold:
    """The broker's helper hold, with renewal on this command's own clock."""

    def __init__(self, env: Environment):
        self.env = env
        self.renewed_at = 0.0
        self.renewals = 0
        self.sent_at = 0.0  # when the latest hold-on left, accepted or not
        self.lost = False  # the verified hold expired or was replaced before the restart settled
        self.keep = False  # set once a restart may be pending: never release then
        self.engaged = False  # a hold-on we confirmed
        self.unsure = False  # a hold-on whose answer was lost

    def _post(self, payload: dict) -> Reply:
        return self.env.socket("POST", helper_hold.API_PATH, payload)

    def engage(self) -> None:
        """Take the hold; the first call refuses when any hold is already active.

        A hold-on while one is active renews it under its original reason, so
        without this check a foreign hold would look like ours.
        """
        if not self.engaged and not self.unsure:
            self._refuse_existing_hold()
        # Mark the request uncertain and stamp it before it leaves: an interrupt or a
        # lost answer after the broker accepted it must still find it at cleanup.
        was_unsure, self.unsure = self.unsure, True
        self.sent_at = self.env.clock()
        code, body = self._post({"hold": True, "reason": HOLD_REASON})
        if code == 404:
            self.unsure = was_unsure
            raise HoldFailure("the running broker predates the hold control; "
                              "restart it parked once to install it")
        if code != 202:
            self.unsure = was_unsure  # a definite refusal: nothing of ours exists
            raise HoldFailure(f"hold refused with {code} {body.get('error', '')}".strip())
        self.engaged, self.unsure = True, False
        self.renewed_at = self.env.clock()
        self.renewals += 1

    def _refuse_existing_hold(self) -> None:
        code, body = self.env.socket("GET", helper_hold.API_PATH, None)
        if code == 404:
            raise HoldFailure("the running broker predates the hold control; "
                              "restart it parked once to install it")
        if code != 200 or body.get("held") is not False:
            raise HoldFailure("a hold is already active or its state is unknown "
                              f"({code}, reason {body.get('reason')!r}); not taking over another "
                              "operation's hold")

    def renew_if_due(self) -> None:
        if self.env.clock() - self.renewed_at >= helper_hold.RENEW_AFTER_SECONDS:
            self.engage()

    def release_if_ours(self, same_broker: bool) -> None:
        """Lift the hold only when it is ours and the broker that took it still runs."""
        if not (self.engaged or self.unsure):
            return
        out = self.env.out
        if not same_broker:
            out("Not releasing the hold: the broker's identity changed or could not be "
                "read, and a replaced broker's hold ended with it.")
            return
        try:
            code, body = self.env.socket("GET", helper_hold.API_PATH, None)
        except (OSError, RuntimeError, ValueError):
            code, body = 0, {}
        if code != 200:
            out(self.expiry_note("Could not confirm the hold is ours."))
        elif body.get("held") is not True:
            out("The hold had already ended.")
        elif body.get("reason") != HOLD_REASON:
            out(f"Not releasing: the hold now belongs to another request ({body.get('reason')!r}).")
        else:
            out(self._release())

    def expiry_note(self, lead: str) -> str:
        """What to tell the operator when the hold is left to lapse (an upper bound)."""
        left = helper_hold.HOLD_SECONDS - (self.env.clock() - max(self.sent_at, self.renewed_at))
        if left < 1.0:
            return (f"{lead} If it is ours, its time should already have run out; "
                    f"confirm with GET {helper_hold.API_PATH}.")
        return f"{lead} If it is ours, it expires on its own within about {left:.0f} s."

    def _release(self) -> str:
        try:
            code, _ = self._post({"hold": False})
        except (OSError, RuntimeError, ValueError) as error:
            return (f"hold release failed ({error}); it expires on its own within "
                    f"{helper_hold.HOLD_SECONDS:.0f} s")
        return "hold released" if code == 200 else f"hold release answered {code}"

    def snapshot(self) -> dict[str, helper_hold.HelperHoldState]:
        """Helper states, but only from our own live hold with time left."""
        code, body = self.env.socket("GET", helper_hold.API_PATH, None)
        if code != 200:
            raise HoldFailure(f"hold status answered {code}")
        if body.get("held") is not True:
            raise HoldFailure("the hold is no longer active (it expired or was released)")
        if body.get("reason") != HOLD_REASON:
            raise HoldFailure(f"the hold was replaced by another request ({body.get('reason')!r})")
        remaining = body.get("remaining_seconds")
        if not isinstance(remaining, (int, float)) or remaining < MIN_HOLD_REMAINING_SECONDS:
            raise HoldFailure(f"the hold has too little time left ({remaining!r} s)")
        return _states_in(body)

    def wait_for_safe(self) -> None:
        deadline = self.env.clock() + HOLD_STOP_BOUND_SECONDS
        while True:
            self.renew_if_due()
            unsafe = _unsafe_states(self.snapshot())
            self.env.out(f"helpers not yet stopped: { {k: v.value for k, v in unsafe.items()} }")
            if not unsafe:
                return
            if self.env.clock() >= deadline:
                raise HoldFailure(f"helpers still not stopped after {HOLD_STOP_BOUND_SECONDS:.0f} s: "
                                  f"{ {k: v.value for k, v in unsafe.items()} }")
            self.env.sleep(HOLD_POLL_SECONDS)


# -- adapters -------------------------------------------------------------


def adapter_problems(env: Environment, armed_roles: tuple[str, ...] = ()) -> list[str]:
    """Each role must be ERROR-ACTIVE; listen-only too unless a helper legitimately armed it."""
    try:
        states = env.adapters()
    except Exception as error:  # topology or ip failures must fail closed
        return [f"could not read the adapters: {error}"]
    problems = []
    for role, state in states.items():
        if not state.present:
            problems.append(f"{role} ({state.channel}) is not present")
        elif not state.listen_only and role not in armed_roles:
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


def _settle(env: Environment) -> bool:
    env.out(f"Restarted. Waiting {SETTLE_SECONDS:.0f} s for the first evaluations.")
    env.sleep(SETTLE_SECONDS)
    return True


def _restart(env: Environment) -> bool:
    """Parked mode: the script's blocking restart; any nonzero status is a failure."""
    code, _ = env.run(RESTART_COMMAND)
    if code != 0:
        env.out(f"systemctl restart failed with exit status {code}")
        return False
    return _settle(env)


def _restart_state(env: Environment, pid_before: str) -> str:
    """After a restart client that did not report success: what really happened.

    "done": a new broker process runs. "not_started": the same broker runs and
    no job is queued, so nothing is pending. "pending": anything else, a queued
    job or unreadable state (a query that raises included), because a systemctl
    client that timed out or was interrupted leaves its job to run. Blocking
    `systemctl restart` is kept over `--no-block` because the sudoers entry
    matches the literal argv and a new flag would change what must be allowed.
    """
    try:
        code, pid_out = env.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT])
        jcode, job_out = env.run(["systemctl", "show", "-p", "Job", "--value", UNIT])
    except Exception:
        return "pending"
    pid = pid_out.strip()
    if code != 0 or jcode != 0 or not pid_before:
        return "pending"
    if pid not in ("", "0", pid_before):
        return "done"
    return "not_started" if pid == pid_before and not job_out.strip() else "pending"


def _submit_restart(env: Environment, hold: Hold, pid_before: str) -> bool:
    """Running mode: restart, treating the restart as indeterminate from the start.

    `hold.keep` is set before systemctl runs and cleared only once a terminal
    result is established ("not_started", so cleanup may release). A restart
    that cannot be confirmed keeps the hold renewed until it resolves or the
    bound passes.
    """
    hold.keep = True
    try:
        code, _ = env.run_watched(RESTART_COMMAND, lambda: _renew_while_pending(env, hold))
    except BaseException:
        # Ctrl-C ends monitoring and leaves the hold to expire: the restart job
        # may still be queued, so nothing here releases it.
        env.out("Interrupted: monitoring stopped and the hold is NOT released. Check "
                "`systemctl list-jobs` and `systemctl show -p MainPID,Job van-telemetry.service`. "
                + hold.expiry_note("The hold will lapse."))
        raise
    if code == 0:
        hold.keep = False
        return _settle(env)
    env.out(f"systemctl restart client ended with exit status {code}")
    state = _resolve_pending(env, hold, pid_before)
    if state == "not_started":
        hold.keep = False
        env.out("The restart never ran; releasing our hold.")
    elif state == "done":
        hold.keep = False
        env.out("The restart did complete (a new broker process runs); the old hold ended with it.")
        return _settle(env)
    return False


def _resolve_pending(env: Environment, hold: Hold, pid_before: str) -> str:
    """Keep our hold renewed until the restart is positively resolved, within a bound.

    Ctrl-C here propagates with the hold left alone: it is never released
    while the restart is pending.
    """
    deadline = env.clock() + RESOLVE_BOUND_SECONDS
    while True:
        state = _restart_state(env, pid_before)
        if state != "pending":
            return state
        if env.clock() >= deadline:
            env.out(
                f"STILL PENDING after {RESOLVE_BOUND_SECONDS:.0f} s: a restart job may still be "
                "queued for van-telemetry.service. It can still run and race the hold's expiry, "
                "with helpers armed. Check `systemctl list-jobs` and "
                "`systemctl show -p MainPID,Job van-telemetry.service`. "
                + hold.expiry_note("Hold no longer renewed.")
            )
            return "pending"
        _renew_while_pending(env, hold)
        env.sleep(RESOLVE_POLL_SECONDS)


def _renew_while_pending(env: Environment, hold: Hold) -> None:
    """Renew our hold while the restart client or its job runs; note a lost hold."""
    try:
        code, body = env.socket("GET", helper_hold.API_PATH, None)
        if code == 200 and body.get("held") is True and body.get("reason") == HOLD_REASON:
            hold.renew_if_due()
        elif code == 200:
            hold.lost = True
            env.out("WARNING: the verified hold is no longer ours or has ended; helpers may "
                    "relaunch before the restart runs.")
    except (OSError, RuntimeError, ValueError, HoldFailure) as error:
        env.out(f"hold renewal failed while the restart is pending: {error}")


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
        status = _open_events(env)
        env.out("Check only: nothing was restarted.")
        return status
    if not _restart(env):
        return 1
    if not all([_unit_active(env, unit) for unit in UNITS]):
        return 1
    _tx_line(env, before)
    _recorder_line(env, recorder)
    return _open_events(env)


# -- while running --------------------------------------------------------


def _engine_running(status: dict) -> bool:
    return bool(status["vehicle_state"].get("running"))


def _expected_helpers(status: dict) -> list[str]:
    return [key for key in HELPER_KEYS if (status.get(key) or {}).get("state") != "disabled"]


def _wait_for_relaunch(env: Environment) -> dict:
    """Poll status until every configured helper is back, or the bound passes."""
    status = _status_once(env, True)
    cycle = float((status.get("collector") or {}).get("interval_seconds") or 1.0)
    for _ in range(RELAUNCH_WAIT_CYCLES):
        pids = {k: (status.get(k) or {}).get("helper_pid") for k in _expected_helpers(status)}
        if not _engine_running(status) or all(pids.values()):
            break
        env.sleep(cycle)
        status = _status_once(env, True)
    return status


def _not_relaunched(key: str, helper: dict) -> str:
    return (f"{key} did not relaunch (state {helper.get('state')!r}, reason {helper.get('reason')!r}): "
            "the restart completed, but active-telemetry recovery was not verified; "
            "no retry was made and no block was cleared")


def _running_postchecks(env: Environment) -> list[str]:
    """Judge the final state, after the relaunch wait, never before it."""
    status = _wait_for_relaunch(env)
    problems = []
    if _engine_running(status):
        problems += [_not_relaunched(key, status.get(key) or {}) for key in _expected_helpers(status)
                     if not (status.get(key) or {}).get("helper_pid")]
    problems += _common_blockers(status, env, RUNNING_OWN_KINDS)
    problems += [f"restoration latched: {key}" for key in _latched(status)]
    armed = tuple(role for role, key in (("c-can", "active_drive"), ("b-can", "auxiliary_drive"))
                  if (status.get(key) or {}).get("helper_pid"))
    problems += adapter_problems(env, armed)
    env.out(f"helper pids after restart: {_helper_pids(status)}")
    return problems


def _verified_quiet(env: Environment, hold: Hold) -> str | None:
    """Return why a restart is unsafe, or None once every check holds together.

    Every round ends with a renewal check and a fresh state read, and a round
    that had to renew starts over, so a hold that lapsed and was re-acquired
    never authorizes a restart on stale evidence.
    """
    try:
        for _ in range(VERIFY_ROUNDS):
            hold.wait_for_safe()
            problems = adapter_problems(env)
            problems += running_blockers(_status_once(env, True), env)
            if problems:
                return "; ".join(problems)
            renewals = hold.renewals
            hold.renew_if_due()
            if _unsafe_states(hold.snapshot()):
                continue
            if hold.renewals == renewals:
                return None
        return "the hold had to be renewed repeatedly, so the stops were not confirmed in time"
    except HoldFailure as error:
        return str(error)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return f"the broker could not be read: {error}"


def _hold_then_restart(env: Environment, hold: Hold, pid_before: str) -> bool:
    """Engage, verify, restart. The restart follows the last hold check directly:
    nothing unbounded or slow sits between them, so the 60 s of hold time that
    the last check demanded still stands when the restart is submitted."""
    try:
        hold.engage()
    except HoldFailure as error:
        env.out(f"NOT RESTARTING: {error}")
        return False
    except (OSError, RuntimeError, ValueError) as error:
        env.out(f"NOT RESTARTING: the hold control could not be reached: {error}")
        return False
    reason = _verified_quiet(env, hold)
    if reason is not None:
        env.out(f"NOT RESTARTING: {reason}")
        return False
    return _submit_restart(env, hold, pid_before)


def run_while_running(env: Environment, check: bool) -> int:
    if not preflight(env, True):
        env.out("Not restarting: inhibits, a foreign owner, active requests, a latched "
                "restoration or a radar alignment run block it.")
        return 1
    before, recorder = env.tx_counters(), _main_pid(env, RECORDER_UNIT)
    env.out(f"TX before: {before}")
    if check:
        status = _open_events(env)
        env.out("Check only: nothing was held or restarted.")
        return status
    pid_before = _main_pid(env, UNIT)  # read before the hold exists, so it can't delay the restart
    hold = Hold(env)
    restarted = False
    try:
        restarted = _hold_then_restart(env, hold, pid_before)
    finally:
        # One cleanup scope for engagement, verification and restart; it
        # releases only a hold that is ours on the broker that took it, and an
        # exception in flight (Ctrl-C included) propagates unchanged.
        if not restarted:
            _cleanup(env, hold, pid_before)
    if not restarted:
        return 1
    status = _after_restart(env, before, recorder)
    if hold.lost:
        env.out("WARNING: the hold that verified the helper stops was lost before the restart was "
                "confirmed, so helpers may have been armed when it ran. Exiting nonzero; inspect the "
                "adapters and inhibits.")
        return 1
    return status


def _cleanup(env: Environment, hold: Hold, pid_before: str) -> None:
    if hold.keep:
        return
    try:
        same = bool(pid_before) and _main_pid(env, UNIT) == pid_before
        hold.release_if_ours(same)
    except Exception as error:  # cleanup must never mask the original outcome
        env.out(f"Hold cleanup failed ({error}); any hold of ours expires on its own.")


def _after_restart(env: Environment, before: str, recorder: str) -> int:
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
    try:
        with env.lock():
            return runner(env, args.check)
    except RuntimeError as error:
        if "another restart_broker" not in str(error):
            raise
        env.out(f"Not restarting: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
