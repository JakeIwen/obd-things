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
           inflight=(), latched=(), collector=1.0, disabled=()):
    return {
        "vehicle_state": {"state": state, "running": running},
        "active_drive": {"helper_pid": pids[0], "restoration_failed": "active_drive" in latched,
                         "state": disabled_state("active_drive", disabled)},
        "auxiliary_drive": {"helper_pid": pids[1], "restoration_failed": "auxiliary_drive" in latched,
                            "state": disabled_state("auxiliary_drive", disabled)},
        "interface": {"active_inhibits": list(inhibits)},
        "current_owner": owner,
        "inflight": list(inflight),
        "collector": {"interval_seconds": collector},
    }


def disabled_state(key, disabled):
    return "disabled" if key in disabled else "idle"


def hold_body(active=S.IDLE, aux=S.IDLE, held=True, remaining=170.0, reason="restart_broker"):
    return {"held": held, "reason": reason, "remaining_seconds": remaining, "helpers": {"active_drive": {"state": active.value, "pid": None},
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
        self.sock_status = sock_status or status(state="running", running=True, pids=(11, 12))
        self.unit_pids = ["100", "100"]
        self.restart_rc = 0
        self.engaged = False
        self.pre_body = None  # a hold some other client already holds
        self.lose_first_hold_on = False
        self.hold_get_fails = False
        self.job_out = ""  # a queued job on the broker unit
        self.job_seq = []  # per-read job answers, when a test scripts them
        self.job_raises = False
        self.restart_duration = 0.0
        self.expiry = None  # a genuinely time-dependent hold, when a test sets real_hold
        self.real_hold = False
        self.pid_delay = 0.0
        self.lock_busy = False
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
            if payload.get("hold") is False:
                self.engaged = False
                self.expiry = None
            else:
                self.engaged = True
                self.expiry = self.now + 180.0
                if self.lose_first_hold_on:
                    self.lose_first_hold_on = False
                    raise OSError("response lost")
            queue = self.hold_posts
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if self.hold_get_fails and self.engaged:
            raise OSError("socket gone")
        if not self.engaged:
            return 200, self.pre_body or hold_body(held=False)
        if self.real_hold:
            if self.expiry is None or self.now >= self.expiry:
                return 200, hold_body(held=False)
            return 200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_VERIFIED, remaining=self.expiry - self.now)
        queue = self.hold_gets
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def run(self, argv):
        self.calls.append(("run", tuple(argv)))
        if argv[:2] == ["systemctl", "show"]:
            if argv[-1] == restart_broker.UNIT and "Job" in argv:
                if self.job_seq:
                    return 0, (self.job_seq.pop(0) if len(self.job_seq) > 1 else self.job_seq[0]) + "\n"
                if self.job_raises:
                    raise OSError("systemd unreachable")
                return 0, self.job_out + "\n"
            if argv[-1] == restart_broker.UNIT:
                self.now += self.pid_delay
                self.pid_delay = 0.0
                return 0, (self.unit_pids.pop(0) if len(self.unit_pids) > 1 else self.unit_pids[0]) + "\n"
            return 0, self.recorder.pop(0) + "\n"
        if argv[:2] == ["systemctl", "is-active"]:
            return (0, "active\n") if self.units_ok else (3, "inactive\n")
        if argv == RESTART:
            return self.restart_rc, ""
        return 0, ""

    def watched(self, argv, tick):
        """The restart client takes `restart_duration` fake seconds, ticking every 5."""
        if argv == RESTART:
            elapsed = 0.0
            while elapsed < self.restart_duration:
                self.now += 5.0
                elapsed += 5.0
                tick()
        return self.run(argv)

    def sleep(self, seconds):
        self.now += seconds

    def env(self):
        return restart_broker.Environment(
            web=self.web, socket=self.socket, run=self.run,
            tx_counters=lambda: self.tx.pop(0) if len(self.tx) > 1 else self.tx[0],
            file_inhibits=lambda: tuple(self.file_inhibits), lock=self.lock, run_watched=self.watched,
            adapters=lambda: self.adapter_map, sleep=self.sleep,
            clock=lambda: self.now, out=self.lines.append)

    def lock(self):
        import contextlib

        @contextlib.contextmanager
        def ctx():
            if self.lock_busy:
                raise RuntimeError("another restart_broker command is running")
            yield
        return ctx()

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


def test_parked_refuses_a_helper_owner_with_no_pid():
    fake = Fake(web_status=status(owner={"kind": "broker_active_drive", "roles": ["c-can"]}))
    assert fake.go() == 1
    assert "another owner" in fake.text and not fake.restarts


def test_parked_check_fails_when_the_event_index_does_not_answer():
    fake = Fake()
    fake.web = lambda m, path, payload: (200, status()) if path == "/v1/status" else (500, {})
    assert fake.go("--check") == 1
    assert "did not answer" in fake.text


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
    assert "may mean the broker latched" in fake.text and "may stay off" in fake.text
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
    original = fake.socket

    def socket(method, path, payload):
        if path == "/v1/status":
            if fake.restarts:
                return 200, status(state="running", running=True, pids=(1, 2),
                                   inhibits=["vehicle-data-restoration-failed"])
        return original(method, path, payload)

    env = fake.env()
    env.socket = socket
    assert restart_broker.main(["--while-running"], env) == 1
    assert "POSTCHECK FAILED" in fake.text
    assert len(fake.restarts) == 1


def test_expected_helper_that_never_relaunches_fails_the_postcheck():
    fake = Fake(sock_status=status(state="running", running=True, pids=(None, None)))
    assert fake.go("--while-running") == 1
    assert "active_drive did not relaunch" in fake.text
    assert "auxiliary_drive did not relaunch" in fake.text


def test_disabled_helper_is_not_expected_back():
    fake = Fake(sock_status=status(state="running", running=True, pids=(11, None),
                                   disabled=("auxiliary_drive",)))
    assert fake.go("--while-running") == 0


def test_running_helper_may_arm_its_adapter_but_not_leave_it_unhealthy():
    armed = adapter("can0", listen_only=False)
    sick = adapter("can0", listen_only=False, controller="BUS-OFF")
    fake = Fake()
    env = fake.env()
    assert restart_broker.adapter_problems(env, ("c-can",)) == []
    env.adapters = lambda: {"c-can": armed, "b-can": adapter("can1"), "can-ch": adapter("can2")}
    assert restart_broker.adapter_problems(env, ("c-can",)) == []
    assert restart_broker.adapter_problems(env, ()) != []
    env.adapters = lambda: {"c-can": sick, "b-can": adapter("can1"), "can-ch": adapter("can2")}
    assert "BUS-OFF" in restart_broker.adapter_problems(env, ("c-can",))[0]


def test_expired_hold_is_not_accepted_as_a_verified_stop():
    fake = Fake(hold_gets=[(200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_VERIFIED, held=False))])
    assert fake.go("--while-running") == 1
    assert "no longer active" in fake.text and not fake.restarts


@pytest.mark.parametrize("body, needle", [
    (hold_body(reason="someone else"), "replaced"),
    (hold_body(remaining=5.0), "too little time"),
])
def test_replaced_or_nearly_expired_hold_refuses(body, needle):
    fake = Fake(hold_gets=[(200, body)])
    assert fake.go("--while-running") == 1
    assert needle in fake.text and not fake.restarts
    if "replaced" in needle:
        assert {"hold": False} not in fake.posts()


def test_a_restart_never_follows_a_reacquired_hold_with_helpers_still_stopping():
    # Stops verify, the command stalls past the renewal point, and the renewal
    # lands on a fresh hold whose helpers are still stopping.
    fake = Fake(hold_gets=[(200, hold_body(S.STOPPED_VERIFIED, S.STOPPED_VERIFIED))])
    reads = {"adapters": 0}

    def slow_adapters():
        reads["adapters"] += 1
        fake.now += 130.0
        if reads["adapters"] == 1:
            fake.hold_gets[:] = [(200, hold_body(S.STOPPING, S.STOPPING))]
        return {r: adapter(r) for r in ("c-can", "b-can", "can-ch")}

    env = fake.env()
    env.adapters = slow_adapters
    assert restart_broker.main(["--while-running"], env) == 1
    assert not fake.restarts and fake.posts()[-1] == {"hold": False}


def test_inhibit_that_appears_after_the_stops_blocks_the_restart():
    fake = Fake()
    env = fake.env()
    base = env.adapters

    def adapters_then_inhibit():
        fake.file_inhibits = [{"name": "late-inhibit"}]
        return base()

    env.adapters = adapters_then_inhibit
    assert restart_broker.main(["--while-running"], env) == 1
    assert "late-inhibit" in fake.text and not fake.restarts
    assert fake.posts()[-1] == {"hold": False}


def test_failed_restart_command_releases_the_hold():
    fake = Fake()
    fake.restart_rc = 1
    assert fake.go("--while-running") == 1
    assert fake.posts()[-1] == {"hold": False}


def test_ctrl_c_during_the_restart_client_leaves_the_hold_to_expire():
    fake = Fake()
    fake.unit_pids = ["100", "100"]
    env = fake.env()
    env.run_watched = lambda argv, tick: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        restart_broker.main(["--while-running"], env)
    assert {"hold": False} not in fake.posts()
    assert "NOT released" in fake.text and "systemctl list-jobs" in fake.text
    assert "will lapse" in fake.text


def test_a_second_restart_command_refuses_while_one_runs():
    fake = Fake()
    fake.lock_busy = True
    assert fake.go() == 1 and fake.go("--while-running") == 1
    assert "another restart_broker" in fake.text
    assert not fake.restarts and not fake.posts()


def test_a_preexisting_hold_is_refused_and_never_released():
    fake = Fake()
    fake.pre_body = hold_body(reason="another maintenance operation")
    assert fake.go("--while-running") == 1
    assert "not taking over" in fake.text
    assert fake.posts() == [] and not fake.restarts


def test_a_hold_pointed_at_someone_else_mid_run_is_not_released():
    fake = Fake(hold_gets=[(200, hold_body(reason="another maintenance operation"))])
    assert fake.go("--while-running") == 1
    assert {"hold": False} not in fake.posts()
    assert "belongs to another request" in fake.text


def test_delayed_broker_pid_read_cannot_stretch_the_gap_before_the_restart():
    fake = Fake()
    fake.pid_delay = 181.0  # the one MainPID read that happens before the hold exists
    assert fake.go("--while-running") == 0
    assert len(fake.restarts) == 1
    names = [c for c in fake.calls if c[0] == "run" or c[:3] == ("socket", "GET", HOLD)]
    restart_at = names.index(("run", tuple(RESTART)))
    assert names[restart_at - 1][:3] == ("socket", "GET", HOLD)  # the last hold check, then the restart


def test_interrupt_during_stop_verification_releases_our_hold():
    fake = Fake()
    env = fake.env()
    env.adapters = lambda: (_ for _ in ()).throw(KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        restart_broker.main(["--while-running"], env)
    assert fake.posts()[-1] == {"hold": False} and not fake.restarts


def test_lost_hold_on_response_is_confirmed_before_release():
    fake = Fake()
    fake.lose_first_hold_on = True
    assert fake.go("--while-running") == 1
    assert fake.posts()[-1] == {"hold": False}
    assert not fake.restarts


def test_lost_hold_on_with_unreadable_state_says_it_will_expire():
    fake = Fake()
    fake.lose_first_hold_on = True
    fake.hold_get_fails = True
    assert fake.go("--while-running") == 1
    assert "expires on its own" in fake.text
    assert {"hold": False} not in fake.posts()


def test_missing_helper_message_says_the_restart_completed():
    fake = Fake(sock_status=status(state="running", running=True, pids=(None, 12)))
    assert fake.go("--while-running") == 1
    assert "the restart completed" in fake.text and "recovery was not verified" in fake.text


def test_client_timeout_with_the_restart_still_pending_does_not_release():
    fake = Fake()
    fake.restart_rc = 124  # the systemctl client timed out
    fake.job_out = "4711"  # its job is still queued
    assert fake.go("--while-running") == 1
    assert {"hold": False} not in fake.posts()
    assert "STILL PENDING" in fake.text and "systemctl list-jobs" in fake.text
    assert "race the hold's expiry" in fake.text
    renewals = [p for p in fake.posts() if p.get("hold")]
    assert len(renewals) >= 4  # kept renewing through the whole bound
    assert fake.now >= restart_broker.RESOLVE_BOUND_SECONDS


def test_client_timeout_after_the_broker_was_replaced_carries_on_to_postchecks():
    fake = Fake()
    fake.restart_rc = 124
    fake.unit_pids = ["100", "200"]
    assert fake.go("--while-running") == 0
    assert {"hold": False} not in fake.posts()
    assert "restart did complete" in fake.text


def test_unreadable_unit_state_after_a_failed_client_counts_as_pending():
    fake = Fake()
    fake.restart_rc = 1
    original = fake.run
    fake.run = lambda argv: (1, "") if argv[:2] == ["systemctl", "show"] and "Job" in argv else original(argv)
    env = fake.env()
    env.run = fake.run
    assert restart_broker.main(["--while-running"], env) == 1
    assert {"hold": False} not in fake.posts()


def test_interrupt_after_the_hold_on_was_accepted_still_releases():
    fake = Fake()
    original = fake.socket

    def socket(method, path, payload):
        reply = original(method, path, payload)
        if method == "POST" and payload.get("hold"):
            raise KeyboardInterrupt  # the broker accepted it; SIGINT beat the return
        return reply

    env = fake.env()
    env.socket = socket
    with pytest.raises(KeyboardInterrupt):
        restart_broker.main(["--while-running"], env)
    assert fake.posts()[-1] == {"hold": False}


def test_definite_refusal_clears_the_uncertainty_so_nothing_is_released():
    fake = Fake(hold_posts=[(409, {"error": "alignment_active"})])
    assert fake.go("--while-running") == 1
    assert {"hold": False} not in fake.posts()


def test_expiry_note_does_not_claim_zero_seconds():
    fake = Fake()
    hold = restart_broker.Hold(fake.env())
    hold.sent_at = hold.renewed_at = -500.0
    assert "already have run out" in hold.expiry_note("x")
    hold.sent_at = hold.renewed_at = fake.now
    assert "about 180 s" in hold.expiry_note("x")


def test_pending_restart_that_never_ran_releases_our_hold():
    fake = Fake()
    fake.restart_rc = 124
    fake.job_seq = ["4711", "4711", ""]  # the job leaves the queue; the broker never changed
    assert fake.go("--while-running") == 1
    assert "never ran" in fake.text
    assert fake.posts()[-1] == {"hold": False}


def test_pending_restart_that_lands_later_goes_on_to_postchecks():
    fake = Fake()
    fake.restart_rc = 124
    fake.job_seq = ["4711"]
    fake.unit_pids = ["100", "100", "100", "200"]
    assert fake.go("--while-running") == 0
    assert "restart did complete" in fake.text
    assert {"hold": False} not in fake.posts()


def test_ctrl_c_while_the_restart_is_pending_does_not_release():
    fake = Fake()
    fake.restart_rc = 124
    fake.job_seq = ["4711"]
    env = fake.env()
    calls = {"n": 0}
    original = env.sleep

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] > 3 and fake.restarts:
            raise KeyboardInterrupt
        original(seconds)

    env.sleep = sleep
    with pytest.raises(KeyboardInterrupt):
        restart_broker.main(["--while-running"], env)
    assert {"hold": False} not in fake.posts()


def test_timeout_with_an_unreadable_job_query_is_pending_and_posts_no_hold_off():
    fake = Fake()
    fake.restart_rc = 124
    fake.job_raises = True
    assert fake.go("--while-running") == 1
    assert {"hold": False} not in fake.posts()
    assert "STILL PENDING" in fake.text


def test_hold_is_renewed_while_a_slow_restart_client_runs_and_stays_ours():
    # The review's scenario: stops verified with ~70 s left, then a restart
    # client that times out at 120 s. Without renewal the hold expires mid-call.
    fake = Fake()
    fake.real_hold = True
    fake.restart_rc = 124
    fake.restart_duration = 120.0
    fake.unit_pids = ["100", "100", "100", "200"]
    fake.job_seq = ["4711"]
    env = fake.env()
    base = env.adapters

    def slow_adapters():
        fake.now += 110.0  # verification finishes with 70 s of hold left
        return base()

    env.adapters = slow_adapters
    assert restart_broker.main(["--while-running"], env) == 0
    assert len([p for p in fake.posts() if p.get("hold")]) >= 2  # renewed during the client
    assert "no longer ours" not in fake.text
    assert "WARNING: the hold that verified" not in fake.text


def test_a_lost_hold_makes_a_later_successful_restart_exit_nonzero():
    fake = Fake()
    fake.real_hold = True
    fake.restart_duration = 120.0
    original = fake.socket

    def socket(method, path, payload):
        if method == "POST" and payload.get("hold") and fake.now > 40:
            return 500, {}  # renewals fail, so the 180 s hold lapses during the client
        return original(method, path, payload)

    env = fake.env()
    env.socket = socket
    fake.restart_duration = 200.0
    assert restart_broker.main(["--while-running"], env) == 1
    assert "hold that verified the helper stops was lost" in fake.text
    assert len(fake.restarts) == 1
