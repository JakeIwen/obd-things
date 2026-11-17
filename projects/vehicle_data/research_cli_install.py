"""Install, measure or budget-check the research CLI copy (run as root by the deploy runbook).

``install_research_cli.sh claude`` stages a root-owned read-only copy of the owner's installed
Claude CLI under ``/opt/van-research-cli/staging``, opens a maintenance reservation in the research
worker (so no research turn can start until the worker restarts), has the worker verify the staged
copy inside its own sandbox, confirms the reservation is still open, promotes the copy, records its
identity and file-set digest, and restarts the worker. ``--measure`` and ``--budget-check`` run the
worker's real-turn checks inside a reservation. ``--dry-run`` prints the plan and touches nothing.
This command never launches a model CLI itself.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from projects.vehicle_data.api_client import TelemetryClient
from projects.vehicle_data.dtc_research_contract import WORKER
from projects.vehicle_data.research_cli_policy import IDENTITY_FILE, POLICY, digest_of, file_list

CLI_ROOT = Path(WORKER["cliRoot"])
OWNER_CLAUDE = Path("/home/pi/.local/bin/claude")
UNIT = "van-telemetry-research.service"
STEP_WAIT = {"verify": 300, "measure": 1500, "budget_check": 900}


class Abort(Exception):
    pass


def say(message):
    print(message, flush=True)


class Installer:
    def __init__(self, cli_root=CLI_ROOT, socket_path=WORKER["socket"], dry_run=False, client=None,
                 restart=None, poll=2.0):
        self.cli_root, self.dry_run, self.poll = Path(cli_root), dry_run, poll
        self.client = client or TelemetryClient(socket_path, timeout=10)
        self.restart = restart or (lambda: subprocess.run(["systemctl", "restart", UNIT], check=True))

    # --- worker conversation ------------------------------------------------------------------
    def _call(self, method, path, payload=None):
        try:
            return self.client.request(method, path, payload)
        except (OSError, RuntimeError, ValueError) as exc:
            raise Abort(f"the research worker is not answering ({exc}); is {UNIT} running?") from exc

    def reserve(self, purpose):
        code, status = self._call("GET", "/status")
        if code != 200:
            raise Abort("the research worker status could not be read")
        attempt_id = uuid.uuid4().hex
        code, reply = self._call("POST", "/maintenance", {"attempt_id": attempt_id,
                                                          "expected_instance_id": status["instance_id"],
                                                          "purpose": purpose})
        if code != 202:
            raise Abort(f"research is busy ({reply.get('detail')}); run this again later")
        say(f"maintenance reservation {attempt_id} open (worker {status['instance_id']})")
        return attempt_id, status["instance_id"]

    def step(self, attempt_id, step, **extra):
        code, reply = self._call("POST", f"/maintenance/{attempt_id}/step", {"step": step, **extra})
        if code != 202:
            raise Abort(f"the worker refused the {step} step ({reply.get('detail')})")
        deadline = time.monotonic() + STEP_WAIT[step]
        while time.monotonic() < deadline:
            view = self.view(attempt_id)
            if step in view.get("steps", {}):
                return view["steps"][step]
            time.sleep(self.poll)
        raise Abort(f"the {step} step did not finish in time")

    def view(self, attempt_id):
        code, view = self._call("GET", f"/maintenance/{attempt_id}")
        if code != 200 or not view.get("known"):
            raise Abort("the maintenance reservation is gone (the worker restarted)")
        return view

    def still_open(self, attempt_id, instance_id):
        view = self.view(attempt_id)
        if not view.get("open") or view.get("instance_id") != instance_id:
            raise Abort("the maintenance reservation expired or the worker restarted; nothing was promoted")

    def end(self, attempt_id):
        self._call("POST", f"/maintenance/{attempt_id}/end", {})

    # --- install --------------------------------------------------------------------------------
    def install_claude(self):
        source = OWNER_CLAUDE.resolve()
        version = source.name
        staging = self.cli_root / "staging" / f"claude-{version}"
        target = self.cli_root / "claude" / version
        say(f"source: {source} (version {version})")
        say(f"stage:  {staging}/claude (root-owned, read-only)")
        say(f"then:   worker verification in a maintenance reservation, promotion to {target},")
        say(f"        {self.cli_root / IDENTITY_FILE} update, systemctl restart {UNIT}")
        if self.dry_run:
            say("dry run: nothing was changed")
            return 0
        if not source.is_file():
            raise Abort(f"{source} is not a file")
        with self._command_lock():
            if any((self.cli_root / "staging").glob("*")):
                raise Abort(f"{self.cli_root / 'staging'} is not empty; remove it by hand after checking it")
            self._stage(source, staging)
            rows = file_list(staging)
            digest = digest_of(rows)
            try:
                attempt_id, instance_id = self.reserve("install")
            except Abort:
                _discard(staging)
                raise
            try:
                result = self.step(attempt_id, "verify", executable=str(staging / "claude"))
                if result.get("result") != "passed":
                    self._record_failure("claude", version, digest, result.get("reason"))
                    raise Abort(f"verification failed: {result.get('reason')}")
                self.still_open(attempt_id, instance_id)  # a paused installer must not promote late
                self._promote(staging, target)
                self._record("claude", {"version": version, "root": str(target), "executable": "claude",
                                        "files": rows, "digest": digest},
                             {"result": "passed", "digest": digest, "evidence": result.get("evidence", {})})
            except Abort:
                _discard(staging)
                try:
                    self.end(attempt_id)
                except Abort:
                    pass
                raise
        say(f"promoted {target}; restarting {UNIT} (this ends the reservation)")
        self.restart()
        return 0

    def _command_lock(self):
        self.cli_root.mkdir(mode=0o755, exist_ok=True)
        handle = open(self.cli_root / ".install.lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise Abort("another install_research_cli run is in progress") from None
        return handle

    def _stage(self, source, staging):
        staging.mkdir(parents=True, mode=0o755)
        shutil.copyfile(source, staging / "claude")
        os.chmod(staging / "claude", 0o555)
        os.chmod(staging, 0o555)

    def _promote(self, staging, target):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(staging, 0o755)
        if target.exists():
            shutil.rmtree(target)
        os.rename(staging, target)
        os.chmod(target, 0o555)
        for old in target.parent.iterdir():
            if old != target:
                shutil.rmtree(old) if old.is_dir() else old.unlink()

    def _load_identity(self):
        try:
            return json.loads((self.cli_root / IDENTITY_FILE).read_text())
        except (OSError, ValueError):
            return {"schema_version": 1, "providers": {}}

    def _write_identity(self, current):
        path = self.cli_root / IDENTITY_FILE
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, indent=1))
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)

    def _record(self, provider, entry, verification):
        current = self._load_identity()
        entry["verification"] = {**verification, "verified_at": datetime.now(timezone.utc).isoformat()}
        current["providers"][provider] = entry
        self._write_identity(current)

    def _record_failure(self, provider, version, digest, reason):
        """A failed upgrade leaves the installed, verified copy (if any) in service."""
        current = self._load_identity()
        entry = current["providers"].setdefault(provider, {"digest": None, "verification": {
            "result": "failed", "reason": reason, "digest": digest}})
        entry["last_failed_install"] = {"version": version, "digest": digest, "reason": reason,
                                        "at": datetime.now(timezone.utc).isoformat()}
        self._write_identity(current)

    # --- measurement and budget check -----------------------------------------------------------
    def run_check(self, purpose, warning_chat_peak_mib=None):
        say(f"would open a {purpose} reservation and run the worker's {purpose} step (real Claude turns)")
        if self.dry_run:
            say("dry run: nothing was changed")
            return 0
        attempt_id, _instance = self.reserve(purpose)
        try:
            result = self.step(attempt_id, purpose)
        finally:
            self.end(attempt_id)
        say(json.dumps(result, indent=1, sort_keys=True))
        if purpose == "measure":
            if result.get("result") != "measured":
                raise Abort("the measurement did not complete; nothing to record")
            say("memory block for research_cli_policy.json (review, commit, deploy):")
            say(json.dumps(memory_block(result, warning_chat_peak_mib), indent=1))
            (self.cli_root / "measurement.json").write_text(json.dumps(result, indent=1))
        return 0


def _discard(staging):
    """Remove a staged copy (made read-only when staged)."""
    if staging.exists():
        os.chmod(staging, 0o755)
        shutil.rmtree(staging)


def _round_up(value, step):
    return int(math.ceil(value / step) * step)


def memory_block(result, warning_chat_peak_mib=None):
    """The policy's ``memory`` section from a measurement: floor = peak + reserve, cap = 1.5 x peak."""
    figures = result["figures"]
    peak = max(part.get("peak_tree_hwm_mib") or 0 for part in figures.values())
    reserve = POLICY["memory"]["reserve_mib"]
    if warning_chat_peak_mib and warning_chat_peak_mib > peak:
        reserve += _round_up(warning_chat_peak_mib - peak, 64)
    return {"measured": True, "cli_digest": result["cli_digest"],
            "measured_at": datetime.now(timezone.utc).date().isoformat(),
            "provisional": POLICY["memory"]["provisional"],
            "measured_values": {"launch_floor_mib": _round_up(peak + reserve, 64),
                                "tree_cap_mib": _round_up(peak * 1.5, 64),
                                "critical_mib": POLICY["memory"]["provisional"]["critical_mib"]},
            "research_peak_mib": round(peak, 1), "warning_chat_peak_mib": warning_chat_peak_mib,
            "cli_version": result.get("cli_version"), "reserve_mib": POLICY["memory"]["reserve_mib"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("claude", "codex"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--measure", action="store_true", help="measure research memory peaks (real turns)")
    mode.add_argument("--budget-check", action="store_true", help="check the per-turn cost cap (a real turn)")
    parser.add_argument("--warning-chat-peak-mib", type=float, help="from measure_assistant_peak, for --measure")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen; change nothing")
    args = parser.parse_args(argv)
    if args.provider == "codex":
        say(POLICY["codex"]["reason"] + "; Codex research is disabled by policy (follow-up F3).")
        return 2
    if not args.dry_run and os.geteuid() != 0:
        say("run this with sudo (it writes /opt/van-research-cli and restarts the worker)")
        return 2
    installer = Installer(dry_run=args.dry_run)
    try:
        if args.measure:
            return installer.run_check("measure", args.warning_chat_peak_mib)
        if args.budget_check:
            return installer.run_check("budget_check")
        return installer.install_claude()
    except Abort as exc:
        say(f"stopped: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
