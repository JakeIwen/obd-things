"""A research worker on temp directories with the scripted fake Claude CLI (no network, no model)."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from projects.vehicle_data.dtc_research_worker import Worker
from projects.vehicle_data.model_cli_lock import CliLock
from projects.vehicle_data.research_cli_policy import POLICY, IdentityChecker, digest_of, file_list
from projects.vehicle_data.research_volume import Volume
from projects.vehicle_data.research_worker_facts import DnsProbe
from tests import fake_research_cli

GIB = 1024 * 1024 * 1024
FAKE_KEY = "sk-ant-api03-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
INSIDE_GRANTS = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # before ``until`` whatever today's date is


class WorkerRig:
    """Temp CLI root with a verified fake Claude copy, a volume with a token, a lock file and a
    ``/proc/meminfo`` stand-in; ``worker`` is a real ``Worker`` over them."""

    def __init__(self, scenario=None, activated=True, available_bytes=4 * GIB, dns=None, settle=True,
                 with_key=False, now=None, credits_enabled=True, prepare=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cli_root = root / "opt"
        self.release = self.cli_root / "claude" / "9.9.9"
        self.state = root / "fake-state"
        fake_research_cli.install(self.release, scenario or {"mode": "ok"}, state=self.state)
        rows = file_list(self.release)
        self.digest = digest_of(rows)
        (self.cli_root / "identity.json").write_text(json.dumps({"schema_version": 1, "providers": {"claude": {
            "version": "9.9.9", "root": str(self.release), "executable": "claude", "files": rows,
            "digest": self.digest, "verification": {"result": "passed", "digest": self.digest}}}}))
        self.volume = Volume(root / "volume")
        self.volume.root.mkdir()
        self.volume.startup_cleanup()
        self.volume.token.write_text("sk-test-token\n")
        os.chmod(self.volume.token, 0o600)
        self.lock_path = root / "model-cli.lock"
        self.lock_path.write_text("")
        self.meminfo = root / "meminfo"
        self.set_available(available_bytes)
        self.policy = copy.deepcopy(POLICY)
        if activated:
            self.policy["memory"].update(measured=True, cli_digest=self.digest, measured_values={
                "launch_floor_mib": 64, "tree_cap_mib": 4096, "critical_mib": 16})
        self.policy["claude"]["api_credits"]["enabled"] = credits_enabled
        self.now = now or INSIDE_GRANTS
        if with_key:
            self.install_key()
        if prepare:
            prepare(self)
        self.resolved = []
        given_dns = dns is not None
        dns = dns or DnsProbe(resolve=lambda *args: self.resolved.append(args) or [()])
        self.worker = Worker(self.volume, IdentityChecker(self.cli_root, self.policy), CliLock(self.lock_path),
                             meminfo=self.meminfo, policy=self.policy, dns=dns, now=lambda: self.now)
        if not given_dns:
            self.worker.dns.start(force=True).join(5)  # one successful lookup: public DNS is usable
        if settle:
            self.worker.start()
            self.settle()

    def install_key(self, key=FAKE_KEY, mode=0o600):
        self.volume.api_key.write_text(key + "\n")
        os.chmod(self.volume.api_key, mode)

    def ledger(self):
        return json.loads(self.volume.credit_ledger.read_text())

    def credit_launches(self):
        return [call for call in self.calls() if "ANTHROPIC_API_KEY" in call["env"]]

    def settle(self, timeout=10):
        """Wait for a background check that began now: the copy is hashed, as on a worker past its start."""
        _value, _at, current = self.worker.facts.current(timeout)
        assert current, "the worker's background check did not finish"

    def set_available(self, available_bytes):
        self.meminfo.write_text(f"MemTotal: 3706880 kB\nMemAvailable: {available_bytes // 1024} kB\n")

    def scenario(self, scenario):
        fake_research_cli.set_scenario(self.state, scenario)

    def calls(self):
        return fake_research_cli.calls(self.state)

    def prompts(self):
        return fake_research_cli.prompts(self.state)

    def turn_body(self, run_id=None, kind="initial", session_id=None, turn_kind="report", attempt_id=None,
                  billing_capable=None, billing=None):
        body = {"attempt_id": attempt_id or uuid.uuid4().hex, "expected_instance_id": self.worker.table.instance_id,
                "run_id": run_id or uuid.uuid4().hex, "provider": "claude", "model": "sonnet", "effort": "medium",
                "kind": kind, "turn_kind": turn_kind, "prompt": "CONTEXT {}"}
        if session_id:
            body["session_id"] = session_id
        if billing_capable is not None:
            body["billing_capable"] = billing_capable
        if billing is not None:
            body["billing"] = billing
        return body

    def wait(self, attempt_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.worker.table.get(attempt_id)
            if state.get("outcome_state") in ("held", "acknowledged", "dropped"):
                return state
            time.sleep(.05)
        raise AssertionError(f"attempt {attempt_id} did not finish")

    def close(self):
        self.worker.close()
        for attempt in list(self.worker.table.tombstones.values()):
            attempt.cancelled.set()
        deadline = time.monotonic() + 10
        while self.worker.table.current is not None and time.monotonic() < deadline:
            time.sleep(.05)
        self.tmp.cleanup()


def proc_entry(root, pid, children=(), rss_kib=0, hwm_kib=0):
    """One process in a fake ``/proc`` tree (status plus its main task's children), for tests that must
    run where there is no ``/proc`` (the Mac compute worker)."""
    entry = Path(root) / str(pid)
    (entry / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (entry / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))
    (entry / "status").write_text(f"Name:\tx\nVmHWM:\t{hwm_kib} kB\nVmRSS:\t{rss_kib} kB\n")
