"""The research worker: the only process that launches a research CLI.

Runs in ``van-telemetry-research.service`` (its own sandbox: no owner home, no broker or advisor
socket, no LAN, a fixed-size volume). It listens on a Unix socket for the advisor and the root-run
installer, admits one attempt at a time (``research_attempts``), takes the shared CLI lock so a
warning-chat Codex never runs beside it, and runs the attempt (``research_runner``). It pings the
systemd watchdog from its serving loop, so a stalled worker is killed with its whole cgroup.

``GET /status`` answers within ``STATUS_WAIT_SECONDS`` plus a few reads of ``/proc`` and the
volume: the slow facts (CLI identity, sign-in, public DNS) come from ``research_worker_facts``, and
no request hashes the CLI copy or resolves a name, least of all while holding the attempt table's
lock.
"""

import argparse
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import sys
import threading
import time

from projects.vehicle_data.api_credits import REPLAY_REQUIRED, SESSION_MISSING, redact
from projects.vehicle_data.api_credits_contract import API_CREDITS, PROTOCOL, SUBSCRIPTION
from projects.vehicle_data.assistant_http import QuietDisconnects, send_json
from projects.vehicle_data.dtc_research_contract import (
    MODEL_ID_RE, RUN_ID_RE, TIMEOUTS, WORKER, EFFORT_ORDER,
)
from projects.vehicle_data.model_cli_lock import ACQUIRED, CliLock, HELD
from projects.vehicle_data import research_maintenance, research_memory
from projects.vehicle_data.research_attempts import AttemptTable, Refused
from projects.vehicle_data.research_cli import read_token
from projects.vehicle_data.research_credits import Credits
from projects.vehicle_data.research_cli_policy import IdentityChecker, POLICY
from projects.vehicle_data.research_runner import Runner, TurnRequest
from projects.vehicle_data.research_volume import Volume
from projects.vehicle_data.research_worker_facts import DnsProbe, Facts, refusal_detail

MAX_BODY = 512 * 1024
ATTEMPT_RE = re.compile(r"[0-9a-f]{32}\Z")
WATCHDOG_EVERY = 10
PROVIDER_NAMES = ("claude", "codex")
# The advisor's worker client gives up after 5 s and an install check's curl after 10 s: a status
# reply waits at most this long for a fresh check, then answers from the last one.
STATUS_WAIT_SECONDS = 1.0
# A caller that stops sending or reading mid-request frees its handler thread after this long.
REQUEST_IO_SECONDS = 15
CHECKING = "the research service is still checking its CLI copies; try again in a minute"


def prepare_socket(path):
    """Remove a stale socket left by a crash. The runtime directory is the unit's own, so no other
    server can be listening there; anything that is not a socket is refused. (The broker's
    ``api_server._prepare_socket_path`` is not imported: it would load broker transports here.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"refusing to replace non-socket path {path}")
        path.unlink()


def systemd_notify(message):
    """Send one sd_notify datagram when systemd asked for them (``NOTIFY_SOCKET``)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(address)
            sock.sendall(message.encode())
        except OSError:
            pass


class Worker:
    def __init__(self, volume, identity, cli_lock, meminfo=Path("/proc/meminfo"), policy=POLICY, dns=None,
                 now=lambda: datetime.now(timezone.utc)):
        self.volume, self.identity, self.cli_lock, self.meminfo, self.policy = volume, identity, cli_lock, meminfo, policy
        self.table = AttemptTable()
        self.credits = Credits(volume.api_key, volume.credit_ledger, policy, now=now)
        self.runner = Runner(volume, identity, self.thresholds, meminfo=meminfo, credits=self.credits)
        self.dns = dns or DnsProbe()
        self.facts = Facts(self._slow_facts, before=self.dns.start)

    def start(self):
        """Recover the credit ledger (and, with credits disabled, drop API-created sessions), then begin
        the background checks (the CLI copy's hash, the first DNS lookup) before anyone asks."""
        self.credits.start(self.volume)
        self.facts.start()
        self.facts.request()

    def close(self):
        self.facts.stop()

    # --- policy state ------------------------------------------------------------------------
    # Request paths pass ``rehash=False``: a copy that changed since its last hash reads as
    # ``checking`` (refused, retryable) until the background check has hashed it, so no request
    # waits on a hash and none holds the attempt table's lock through one.
    def activation(self):
        identity = self.identity.check("claude", rehash=False)
        if identity.state == "checking":
            return False, identity.reason
        return research_memory.activation(self.policy["memory"], identity.digest)

    def thresholds(self):
        activated, _ = self.activation()
        return research_memory.thresholds(self.policy["memory"], measured=activated)

    def dns_ok(self):
        return self.dns.usable()

    def provider_status(self, provider, rehash=False):
        identity = self.identity.check(provider, rehash=rehash)
        signed_in, reason = False, identity.reason
        if provider == "claude" and identity.state == "ok":
            token, problem = read_token(self.volume.token)
            signed_in, reason = token is not None, problem
        return {"identity": identity.state, "signed_in": signed_in, "reason": reason}

    def _slow_facts(self):
        """Run by ``self.facts`` on its own thread: the one caller that may hash a changed copy."""
        providers = {name: self.provider_status(name, rehash=True) for name in PROVIDER_NAMES}
        activated, reason = self.activation()
        return {"providers": providers, "memory_policy": {"activated": activated, "reason": reason}}

    def status(self):
        facts, checked_at, current = self.facts.current(STATUS_WAIT_SECONDS)
        if facts is None:  # the first background check has not finished (a fresh start hashes the copy)
            facts = {"providers": {name: {"identity": "checking", "signed_in": False, "reason": CHECKING}
                                   for name in PROVIDER_NAMES},
                     "memory_policy": {"activated": False, "reason": CHECKING}}
        dns = self.dns.view()
        try:
            available = research_memory.mem_available_bytes(self.meminfo)
        except (OSError, ValueError):
            available = None
        try:
            free = self.volume.free_bytes()
        except OSError:
            free = None
        return {"instance_id": self.table.instance_id, "pid": os.getpid(), "job": self.table.job(),
                PROTOCOL["capabilityField"]: True,
                "providers": facts["providers"], "memory_policy": facts["memory_policy"],
                "checked_at": checked_at, "checks_current": current, "dns_ok": dns["ok"], "dns": dns,
                "volume": {"free_bytes": free}, "mem_available_bytes": available}

    # --- admission ---------------------------------------------------------------------------
    def _take_cli_lock(self):
        state = self.cli_lock.try_acquire()
        if state == HELD:
            raise Refused("warning_chat", "a warning chat is answering a question")
        if state != ACQUIRED:
            raise Refused("provider_unavailable", "the shared CLI lock file is missing on this Pi")

    def _check_launch(self, provider, *, turn):
        status = self.provider_status(provider)
        if status["identity"] != "ok" or not status["signed_in"]:
            raise Refused("provider_unavailable", status["reason"] or f"{provider} is not available for research")
        dns = self.dns.view()
        if not dns["ok"]:
            self.dns.start()  # never waits; a retry a minute later sees its answer
            raise Refused("provider_unavailable", refusal_detail(dns))
        if turn:
            activated, reason = self.activation()
            if not activated:
                raise Refused("not_activated", reason)
        refusal = research_memory.launch_refusal(self.thresholds(), self.meminfo)
        if refusal:
            raise Refused("low_memory", refusal)

    def admit_turn(self, body):
        request = _turn_request(body)

        plans = []

        def check():
            self._check_launch(request.provider, turn=True)
            if request.kind == "resume" and not self._session_exists(request):
                raise Refused("session_missing", "the CLI session for this run is gone")
            plan = self._plan_billing(request, body["attempt_id"])
            try:
                self._take_cli_lock()
            except BaseException:
                self.credits.withdraw(plan)  # nothing spawns: its reserved ledger entry goes away
                raise
            plans.append(plan)

        attempt = self.table.admit(body["attempt_id"], "turn", body["expected_instance_id"], request.run_id, check)
        self._start(attempt, lambda: self.runner.run_turn(attempt, request, plans[0]),
                    lambda: {"billing": plans[0].billing.choice, "credit_code": None})
        return attempt

    def _plan_billing(self, request, attempt_id):
        """The turn's billing, or a pre-spawn refusal: a resume of an API-created session that cannot
        stay on credits is ``replay_required`` (the advisor replays it on the subscription)."""
        record = None
        if request.kind == "resume":
            origin = self.volume.session_origin(request.run_id)
            if origin is None:  # missing or unreadable provenance: never resumed on either credential
                raise Refused("session_missing", "the CLI session for this run has no readable origin")
            record = {"billing": origin}
        plan = self.credits.plan(request, record, credits_allowed=True, attempt_id=attempt_id)
        if plan.billing.choice == REPLAY_REQUIRED:
            raise Refused(REPLAY_REQUIRED, "this session was billed to API credits and cannot resume on the subscription")
        if plan.billing.choice == SESSION_MISSING:
            raise Refused("session_missing", "the CLI session for this run is gone")
        return plan

    def _session_exists(self, request):
        from projects.vehicle_data.research_cli import claude_session_files
        return bool(claude_session_files(self.volume.run_dir(request.run_id) / "home", request.session_id))

    def admit_discovery(self, body):
        if body.get("provider") != "claude":
            raise Refused("invalid", "only Claude has a model check")

        def check():
            identity = self.identity.check("claude", rehash=False)
            if identity.state != "ok":
                raise Refused("provider_unavailable", identity.reason)
            refusal = research_memory.launch_refusal(self.thresholds(), self.meminfo)
            if refusal:
                raise Refused("low_memory", refusal)
            self._take_cli_lock()

        attempt = self.table.admit(body["attempt_id"], "discovery", body["expected_instance_id"], check=check)

        def work():
            models, signed_in = self.runner.discover_claude(attempt)
            return {"status": "complete", "models": models, "signed_in": signed_in}

        self._start(attempt, work)
        return attempt

    def _start(self, attempt, work, failure_fields=dict):
        """Run ``work`` on its own thread. ``failure_fields`` gives the outcome fields (a turn's
        ``billing`` and ``credit_code``) that an unexpected exception's outcome must still carry."""
        def body():
            try:
                outcome = work()
            except Exception as exc:  # the outcome must always be recorded and the slot freed
                outcome = {"status": "failed", "reason": "provider_error", "detail": redact(exc)[:300],
                           **failure_fields()}
            finally:
                self.cli_lock.release()
            self.table.finish(attempt, outcome)
            print(json.dumps({"event": "dtc_research_timing", "kind": attempt.kind,
                              "status": outcome.get("status"), "reason": outcome.get("reason"),
                              "billing": outcome.get("billing"), "credit_code": outcome.get("credit_code"),
                              **(outcome.get("diagnostics") or {})}), file=sys.stderr, flush=True)
        threading.Thread(target=body, daemon=True).start()

    # --- maintenance ---------------------------------------------------------------------------
    def admit_maintenance(self, body):
        if body.get("purpose") not in ("install", "measure", "budget_check"):
            raise Refused("invalid", "unknown maintenance purpose")
        attempt = self.table.admit(body["attempt_id"], "maintenance", body["expected_instance_id"])
        attempt.detail.update(purpose=body["purpose"], deadline=time.monotonic() + TIMEOUTS["maintenanceSeconds"],
                              steps={}, step_running=None)
        return attempt

    def maintenance_step(self, attempt_id, body):
        with self.table.lock:
            attempt = self.table.current
            if attempt is None or attempt.attempt_id != attempt_id or attempt.kind != "maintenance":
                raise Refused("not_found", "no such maintenance reservation is open")
            if attempt.detail["step_running"]:
                raise Refused("busy", "a maintenance step is already running")
            step = body.get("step")
            if step not in ("verify", "measure", "budget_check"):
                raise Refused("invalid", "unknown maintenance step")
            executable = self._staged_executable(body.get("executable")) if step == "verify" else None
            refusal = research_memory.launch_refusal(self.thresholds(), self.meminfo)
            if refusal:
                raise Refused("low_memory", refusal)
            self._take_cli_lock()
            attempt.detail["step_running"] = step
            attempt.state = "running"
        functions = {"verify": lambda: research_maintenance.verify_claude(self.runner, attempt, executable),
                     "measure": lambda: research_maintenance.measure(self.runner, attempt),
                     "budget_check": lambda: research_maintenance.budget_check(self.runner, attempt)}

        def run():
            try:
                result = functions[step]()
            except Exception as exc:
                result = {"result": "failed", "reason": redact(exc)[:300]}
            finally:
                self.cli_lock.release()
            with self.table.lock:
                attempt.detail["steps"][step] = result
                attempt.detail["step_running"] = None
        threading.Thread(target=run, daemon=True).start()

    def _staged_executable(self, value):
        staging = self.identity.cli_root / "staging"
        path = Path(str(value or ""))
        if not path.is_absolute() or staging not in path.parents or not path.is_file():
            raise Refused("invalid", "the staged executable must be a file under the staging directory")
        return path

    def end_maintenance(self, attempt_id):
        with self.table.lock:
            attempt = self.table.current
            if attempt is None or attempt.attempt_id != attempt_id or attempt.kind != "maintenance":
                return False
            if attempt.detail["step_running"]:
                attempt.cancelled.set()
                return False
            self.table.finish(attempt, {"status": "complete", "steps": attempt.detail["steps"]})
            return True

    def maintenance_view(self, attempt_id):
        with self.table.lock:
            attempt = self.table.tombstones.get(attempt_id)
            if attempt is None or attempt.kind != "maintenance":
                return {"instance_id": self.table.instance_id, "known": False}
            return {"instance_id": self.table.instance_id, "known": True, "open": attempt.state != "finished",
                    "purpose": attempt.detail.get("purpose"), "step_running": attempt.detail.get("step_running"),
                    "steps": attempt.detail.get("steps", {})}

    def expire_maintenance(self):
        with self.table.lock:
            attempt = self.table.current
            if attempt and attempt.kind == "maintenance" and not attempt.detail["step_running"] \
                    and time.monotonic() > attempt.detail["deadline"]:
                self.table.finish(attempt, {"status": "failed", "reason": "expired",
                                            "steps": attempt.detail["steps"]})
                print(json.dumps({"event": "dtc_research_maintenance_expired"}), file=sys.stderr, flush=True)


def _turn_request(body):
    if not isinstance(body.get("run_id"), str) or not RUN_ID_RE.fullmatch(body["run_id"]):
        raise Refused("invalid", "invalid run id")
    if body.get("provider") != "claude":
        raise Refused("provider_unavailable", POLICY["codex"]["reason"])
    if not isinstance(body.get("model"), str) or not MODEL_ID_RE.fullmatch(body["model"]) \
            or body["model"] not in POLICY["claude"]["models"]:
        raise Refused("invalid", "model not offered")
    if body.get("effort") is not None and body.get("effort") not in EFFORT_ORDER:
        raise Refused("invalid", "effort not offered")
    if body.get("kind") not in ("initial", "resume") or body.get("turn_kind") not in ("report", "follow_up"):
        raise Refused("invalid", "invalid turn kind")
    if body["kind"] == "resume" and not isinstance(body.get("session_id"), str):
        raise Refused("invalid", "a resume needs its session id")
    if not isinstance(body.get("prompt"), str) or not body["prompt"]:
        raise Refused("invalid", "empty prompt")
    limit = TIMEOUTS["reportSeconds"] if body["turn_kind"] == "report" else TIMEOUTS["followUpSeconds"]
    budget = POLICY["claude"]["budget_usd"]["report" if body["turn_kind"] == "report" else "follow_up"]
    capable = body.get(PROTOCOL["capabilityField"], False)
    forced = body.get(PROTOCOL["forcedBillingField"])
    if not isinstance(capable, bool) or forced not in (None, SUBSCRIPTION):
        raise Refused("invalid", "invalid billing fields")
    return TurnRequest(body["run_id"], "claude", body["model"], body.get("effort"), body["kind"], body["turn_kind"],
                       body["prompt"], limit, session_id=body.get("session_id"), budget_usd=budget,
                       billing_capable=capable, forced_billing=forced)


class WorkerHandler(http.server.BaseHTTPRequestHandler):
    timeout = REQUEST_IO_SECONDS

    def log_message(self, *_args):
        pass

    def reply(self, status, payload):
        send_json(self, status, payload)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise Refused("invalid", "invalid request body")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise Refused("invalid", "invalid request body")
        return payload

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        worker = self.server.worker
        try:
            status, payload = dispatch(worker, method, self.path, self.body if method == "POST" else None)
        except Refused as refused:
            status, payload = 409, {"reason": refused.reason, "detail": refused.detail}
        except (ValueError, KeyError, TypeError):
            status, payload = 400, {"reason": "invalid", "detail": "invalid request"}
        self.reply(status, payload)


def _launch_body(read):
    body = read()
    for key in ("attempt_id", "expected_instance_id"):
        if not isinstance(body.get(key), str) or not ATTEMPT_RE.fullmatch(body[key]):
            raise Refused("invalid", f"invalid {key}")
    return body


def dispatch(worker, method, path, read):
    """``(status, payload)`` for one worker route."""
    if method == "GET" and path == "/status":
        return 200, worker.status()
    if method == "GET" and path == PROTOCOL["workerCreditsRoute"]:
        return 200, {**worker.credits.view(), "api_sessions": worker.volume.count_sessions_billed(API_CREDITS)}
    if method == "POST" and path in ("/turns", "/discover", "/maintenance"):
        body = _launch_body(read)
        admit = {"/turns": worker.admit_turn, "/discover": worker.admit_discovery,
                 "/maintenance": worker.admit_maintenance}[path]
        attempt = admit(body)
        return 202, attempt.public(worker.table.instance_id)
    if method == "POST" and path == "/sweep":
        keep = read().get("keep")
        if not isinstance(keep, list) or not all(isinstance(k, str) and RUN_ID_RE.fullmatch(k) for k in keep):
            raise Refused("invalid", "invalid keep list")
        job = worker.table.job()
        busy = {job["run_id"]} if job and job.get("run_id") else set()
        return 200, {"removed": worker.volume.sweep(set(keep) | busy)}
    match = re.fullmatch(r"/attempts/([0-9a-f]{32})(/ack|/cancel|/fence)?", path)
    if match:
        if method == "GET" and not match[2]:
            return 200, worker.table.get(match[1])
        if method == "POST" and match[2] == "/ack":
            return 200, {"known": worker.table.acknowledge(match[1])}
        if method == "POST" and match[2] == "/cancel":
            return 200, {"known": worker.table.cancel(match[1])}
        if method == "POST" and match[2] == "/fence":
            return 200, worker.table.fence(match[1])
    match = re.fullmatch(r"/maintenance/([0-9a-f]{32})(/step|/end)?", path)
    if match:
        if method == "GET" and not match[2]:
            return 200, worker.maintenance_view(match[1])
        if method == "POST" and match[2] == "/step":
            worker.maintenance_step(match[1], read())
            return 202, worker.maintenance_view(match[1])
        if method == "POST" and match[2] == "/end":
            return 200, {"ended": worker.end_maintenance(match[1])}
    match = re.fullmatch(r"/runs/([0-9a-f]{32})/delete", path)
    if match and method == "POST":
        job = worker.table.job()
        if job and job.get("run_id") == match[1]:
            raise Refused("busy", "that run is in use")
        worker.volume.delete_run(match[1])
        return 200, {"deleted": True}
    return 404, {"reason": "not_found", "detail": "unknown worker route"}


class WorkerServer(QuietDisconnects, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    log_name = "research-worker"
    _last_ping = 0.0

    def service_actions(self):
        # Pinged from the serving loop itself: a stalled loop stops the pings and systemd kills the unit.
        now = time.monotonic()
        if now - self._last_ping >= WATCHDOG_EVERY:
            systemd_notify("WATCHDOG=1")
            self._last_ping = now
        self.worker.expire_maintenance()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=WORKER["socket"])
    parser.add_argument("--volume", default=WORKER["volume"])
    parser.add_argument("--cli-root", default=WORKER["cliRoot"])
    args = parser.parse_args()
    volume = Volume(args.volume)
    volume.startup_cleanup()
    worker = Worker(volume, IdentityChecker(args.cli_root), CliLock())
    path = Path(args.socket)
    prepare_socket(path)
    server = WorkerServer(str(path), WorkerHandler)
    server.worker = worker
    worker.start()
    os.chmod(path, 0o600)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    systemd_notify("READY=1")
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        worker.close()
        for attempt in list(worker.table.tombstones.values()):
            attempt.cancelled.set()
        server.server_close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
