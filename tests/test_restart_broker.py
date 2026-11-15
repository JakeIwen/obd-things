"""restart_broker through its public entry point, with every effect faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from projects.vehicle_data import helper_hold, restart_broker
from projects.vehicle_data.helper_hold import HelperHoldState as S

HOLD = helper_hold.API_PATH
ALIGN = "/v1/radar/service-alignment"
RESTART = restart_broker.RESTART_COMMAND


def status(*, state="asleep", running=False, pids=(None, None), inhibits=(), owner=None,
           inflight=(), latched=(), collector=1.0):
    return {
        "vehicle_state": {"state": state, "running": running},
        "active_drive": {"helper_pid": pids[0], "restoration_failed": "active_drive" in latched},
        "auxiliary_drive": {"helper_pid": pids[1], "restoration_failed": "auxiliary_drive" in latched},
        "interface": {"active_inhibits": list(inhibits)},
        "current_owner": owner,
        "inflight": list(inflight),
        "collector": {"interval_seconds": collector},
    }


def hold_body(active=S.IDLE, aux=S.IDLE):
    return {"held": True, "helpers": {"active_drive": {"state": active.value, "pid": None},
                                      "auxiliary_drive": {"state": aux.value, "pid": None}}}


def adapter(channel, listen_only=True, controller="ERROR-ACTIVE", present=True):
    return SimpleNamespace(channel=channel, listen_only=listen_only,
                           controller_state=controller, present=present)


class Fake:
    """Scripted world; records every call and advances a fake clock on sleep."""

    def __init__(self, *, web_status=None, sock_status=None, hold_posts=None, hold_gets=None,
                 adapters=None, units_ok=True, align=None, file_inhibits=(), recorder=("1", "1"),
                 tx=("can0=1", "can0=1")):
        self.now = 0.0
        self.calls = []
        self.lines = []
        self.web_status = web_status or status()
        self.sock_status = sock_status or status(state="running", running=True)
        self.hold_posts = list(hold_posts or [(202, hold_body())])
        self.hold_gets = list(hold_gets or [(200, hold_body())])
        self.adapter_map = adapters if adapters is not None else {
            r: adapter(r) for r in ("c-can", "b-can", "can-ch")}
        self.units_ok = units_ok
        self.align = align or (200, {"run": None})
        self.file_inhibits = file_inhibits
        self.recorder = list(recorder)
        self.tx = list(tx)

    def web(self, method, path, payload):
        self.calls.append(("web", method, path))
        if path == "/v1/status":
            return 200, self.web_status
        return 200, {"events": []}

    def socket(self, method, path, payload):
        self.calls.append(("socket", method, path, payload))
        if path == "/v1/status":
            return 200, self.sock_status
        if path == ALIGN:
            return self.align
        if method == "POST":
            queue = self.hold_posts
            return queue.pop(0) if len(queue) > 1 else queue[0]
        queue = self.hold_gets
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def run(self, argv):
        self.calls.append(("run", tuple(argv)))
        if argv[:2] == ["systemctl", "show"]:
            return 0, self.recorder.pop(0) + "\n"
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active\n") if self.units_ok else (3, "inactive\n")
        return 0, ""

    def sleep(self, seconds):
        self.now += seconds

    def env(self):
        return restart_broker.Environment(
            web=self.web, socket=self.socket, run=self.run,
            tx_counters=lambda: self.tx.pop(0) if len(self.tx) > 1 else self.tx[0],
            file_inhibits=lambda: tuple(self.file_inhibits),
            adapters=lambda: self.adapter_map, sleep=self.sleep,
            clock=lambda: self.now, out=self.lines.append)

    def go(self, *argv):
        return restart_broker.main(list(argv), self.env())

    @property
    def restarts(self):
        return [c for c in self.calls if c == ("run", tuple(RESTART))]

    def posts(self):
        return [c[3] for c in self.calls if c[:3] == ("socket", "POST", HOLD)]

    @property
    def text(self):
        return "\n".join(self.lines)


# -- parked ---------------------------------------------------------------


def test_parked_success_restarts_once_and_reports():
    fake = Fake(tx=("can0=1", "can0=2"), recorder=("7", "7"))
    assert fake.go() == 0
    assert len(fake.restarts) == 1
    assert "WARNING: a CAN transmit counter changed" in fake.text
    assert "kept its process" in fake.text
    assert fake.now >= restart_broker.SETTLE_SECONDS


@pytest.mark.parametrize("web_status, needle", [
    (status(state="running", running=True), "not asleep"),
    (status(pids=(None, 55)), "drive helper is running"),
    (status(inhibits=["vehicle-data-restoration-failed"]), "inhibits are latched"),
    (status(owner={"kind": "external_inhibit"}), "another owner"),
    (status(inflight=[{"mode": "armed_diagnostic"}]), "active requests"),
])
def test_parked_refuses_each_blocker(web_status, needle):
    fake = Fake(web_status=web_status)
    assert fake.go() == 1
    assert needle in fake.text
    assert not fake.restarts
    assert fake.now == restart_broker.PREFLIGHT_RETRY_SECONDS * (restart_broker.PREFLIGHT_ATTEMPTS - 1)


def test_parked_refuses_on_a_file_inhibit_the_cached_status_lacks():
    fake = Fake(file_inhibits=[{"name": "disk-only"}])
    assert fake.go() == 1
    assert "disk-only" in fake.text and not fake.restarts


def test_passive_inflight_does_not_block_parked():
    fake = Fake(web_status=status(inflight=[{"mode": "passive"}], owner={"kind": "broker"}))
    assert fake.go() == 0


def test_parked_inactive_unit_fails_the_exit_status():
    assert Fake(units_ok=False).go() == 1


def test_parked_check_never_restarts_or_holds():
    fake = Fake()
    assert fake.go("--check") == 0
    assert not fake.restarts and not fake.posts()


# -- while running --------------------------------------------------------


def test_running_success_holds_then_restarts_once():
    fake = Fake(hold_gets=[(200, hold_body(S.STOPPING, S.STOPPING)),
                           (200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_NO_MUTATION))])
    assert fake.go("--while-running") == 0
    assert fake.posts() == [{"hold": True, "reason": "restart_broker"}]
    assert len(fake.restarts) == 1
    assert "helper pids after restart" in fake.text


@pytest.mark.parametrize("sock_status, needle", [
    (status(state="running", running=True, inhibits=["x"]), "inhibits are latched"),
    (status(state="running", running=True, owner={"kind": "participating_or_external_can_user"}),
     "another owner"),
    (status(state="running", running=True, inflight=[{"mode": "armed_diagnostic"}]), "active requests"),
    (status(state="running", running=True, latched=("auxiliary_drive",)), "restoration latched"),
])
def test_running_refuses_each_blocker(sock_status, needle):
    fake = Fake(sock_status=sock_status)
    assert fake.go("--while-running") == 1
    assert needle in fake.text
    assert not fake.restarts and not fake.posts()


def test_running_refuses_on_file_inhibit_and_alignment_run():
    fake = Fake(file_inhibits=[{"name": "disk-only"}])
    assert fake.go("--while-running") == 1 and not fake.posts()
    fake = Fake(align=(200, {"run": {"state": "running"}}))
    assert fake.go("--while-running") == 1
    assert "alignment run" in fake.text and not fake.posts() and not fake.restarts


def test_running_alignment_state_unreadable_fails_closed():
    fake = Fake(align=(404, {}))
    assert fake.go("--while-running") == 1 and not fake.posts()


def test_running_allows_the_brokers_own_helpers_as_owner():
    fake = Fake(sock_status=status(state="running", running=True, pids=(5, 6),
                                   owner={"kind": "broker_active_drive", "roles": ["c-can"]}))
    assert fake.go("--while-running", "--check") == 0


def test_hold_route_404_refuses_without_restart():
    fake = Fake(hold_posts=[(404, {})])
    assert fake.go("--while-running") == 1
    assert "predates the hold control" in fake.text
    assert not fake.restarts


def test_alignment_conflict_from_the_broker_refuses():
    fake = Fake(hold_posts=[(409, {"error": "alignment_active"})])
    assert fake.go("--while-running") == 1
    assert "alignment_active" in fake.text and not fake.restarts


def test_hold_timeout_releases_and_does_not_restart():
    fake = Fake(hold_gets=[(200, hold_body(S.STOPPING, S.IDLE))])
    assert fake.go("--while-running") == 1
    assert "still not stopped" in fake.text
    assert fake.posts()[-1] == {"hold": False}
    assert not fake.restarts


def test_stopped_unverified_explains_the_latched_inhibit():
    fake = Fake(hold_gets=[(200, hold_body(S.STOPPED_UNVERIFIED, S.STOPPED_VERIFIED))])
    assert fake.go("--while-running") == 1
    assert "latched its inhibit" in fake.text and "active telemetry stays off" in fake.text
    assert fake.posts()[-1] == {"hold": False}
    assert not fake.restarts


@pytest.mark.parametrize("bad", [
    adapter("can1", listen_only=False),
    adapter("can1", controller="ERROR-PASSIVE"),
    adapter("can1", present=False),
])
def test_adapter_not_listen_only_and_error_active_refuses(bad):
    fake = Fake(adapters={"c-can": adapter("can0"), "b-can": bad, "can-ch": adapter("can2")},
                hold_gets=[(200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_VERIFIED))])
    assert fake.go("--while-running") == 1
    assert "b-can" in fake.text
    assert fake.posts()[-1] == {"hold": False}
    assert not fake.restarts


def test_unreadable_adapters_fail_closed():
    fake = Fake()
    env = fake.env()
    env.adapters = lambda: (_ for _ in ()).throw(RuntimeError("no topology"))
    assert restart_broker.main(["--while-running"], env) == 1
    assert "no topology" in fake.text and not fake.restarts


def test_hold_is_renewed_after_120_seconds():
    slow = [(200, hold_body(S.STOPPING, S.STOPPING))] * 130 + [
        (200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_VERIFIED))]
    fake = Fake(hold_gets=slow)
    # The bound is 30 s, so stretch it: renewal is on the command's own clock.
    restart_broker.HOLD_STOP_BOUND_SECONDS, saved = 500.0, restart_broker.HOLD_STOP_BOUND_SECONDS
    try:
        assert fake.go("--while-running") == 0
    finally:
        restart_broker.HOLD_STOP_BOUND_SECONDS = saved
    assert len([p for p in fake.posts() if p.get("hold")]) == 2


def test_running_check_never_holds_or_restarts():
    fake = Fake()
    assert fake.go("--while-running", "--check") == 0
    assert not fake.posts() and not fake.restarts


def test_postcheck_finds_new_inhibit_after_restart():
    fake = Fake()
    calls = {"n": 0}
    original = fake.socket

    def socket(method, path, payload):
        if path == "/v1/status":
            calls["n"] += 1
            if calls["n"] > 1:
                return 200, status(state="running", running=True, pids=(1, 2),
                                   inhibits=["vehicle-data-restoration-failed"])
        return original(method, path, payload)

    env = fake.env()
    env.socket = socket
    assert restart_broker.main(["--while-running"], env) == 1
    assert "POSTCHECK FAILED" in fake.text
    assert len(fake.restarts) == 1
