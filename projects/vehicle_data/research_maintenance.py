"""The installer's steps, run by the research worker inside a maintenance reservation.

The installer (root, ``research_cli_install``) never launches a CLI itself: it reserves the
worker's slot, asks for one of these steps, and only then promotes files or restarts the worker.
``verify_claude`` spends nothing; ``measure`` and ``budget_check`` run real Claude turns and are
started only by the parent's runbook.
"""

import subprocess
import uuid

from projects.vehicle_data.dtc_research_contract import TIMEOUTS
from projects.vehicle_data.dtc_research_prompt import first_prompt, follow_up_prompt
from projects.vehicle_data.research_cli_policy import POLICY
from projects.vehicle_data.research_runner import TurnRequest

MEASURE_CODE = {"module_key": "radar_acc", "fca_display": "C1418-78"}
MEASURE_MODEL, MEASURE_EFFORT = "sonnet", "medium"
BUDGET_MODEL, BUDGET_CAP_USD = "opus", 0.01
HELP_TIMEOUT = 60


def verify_claude(runner, attempt, executable):
    """Flags the runner relies on are documented, and the model check answers (no model turn)."""
    try:
        shown = subprocess.run([str(executable), "--help"], capture_output=True, text=True, timeout=HELP_TIMEOUT,
                               env={"HOME": str(runner.volume.tmp), "PATH": "/usr/bin:/bin"}, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"result": "failed", "reason": f"--help could not run ({exc.__class__.__name__})"}
    missing = [flag for flag in POLICY["claude"]["required_flags"] if flag not in shown]
    if missing:
        return {"result": "failed", "reason": "the CLI lacks flags research relies on: " + ", ".join(missing)}
    try:
        models, signed_in = runner.discover_claude(attempt, executable=executable)
    except RuntimeError as exc:
        return {"result": "failed", "reason": f"the model check failed: {exc}"}
    if not models:
        return {"result": "failed", "reason": "the model check listed none of the policy's Claude models"}
    return {"result": "passed", "evidence": {"flags": POLICY["claude"]["required_flags"],
                                             "models": [m["id"] for m in models], "signed_in": signed_in}}


def _measure_context():
    return {"code": MEASURE_CODE, "module": {"key": MEASURE_CODE["module_key"]},
            "meaning": {"description": "measurement run", "description_reviewed": False},
            "vehicle": {"model": "2022 Ram ProMaster 2500"}, "record": None,
            "record_note": "Memory measurement run; no record is attached."}


def measure(runner, attempt):
    """One real report turn and one resumed follow-up on provisional thresholds; the peaks of each."""
    run_id = uuid.uuid4().hex
    figures = {}
    try:
        report = runner.run_turn(attempt, TurnRequest(
            run_id, "claude", MEASURE_MODEL, MEASURE_EFFORT, "initial", "report", first_prompt(_measure_context()),
            TIMEOUTS["reportSeconds"], budget_usd=POLICY["claude"]["budget_usd"]["report"]))
        figures["report"] = {"status": report["status"], "reason": report.get("reason"), **_memory(report)}
        if report["status"] == "complete" and report.get("session_id"):
            follow = runner.run_turn(attempt, TurnRequest(
                run_id, "claude", MEASURE_MODEL, MEASURE_EFFORT, "resume", "follow_up",
                follow_up_prompt("Which single check should the owner do first, and why?"),
                TIMEOUTS["followUpSeconds"], session_id=report["session_id"],
                budget_usd=POLICY["claude"]["budget_usd"]["follow_up"]))
            figures["follow_up"] = {"status": follow["status"], "reason": follow.get("reason"), **_memory(follow)}
    finally:
        runner.volume.delete_run(run_id)
    identity = runner.identity.check("claude")
    complete = all(part.get("status") == "complete" for part in figures.values()) and len(figures) == 2
    return {"result": "measured" if complete else "incomplete", "cli_digest": identity.digest,
            "cli_version": identity.version, "figures": figures}


def _memory(outcome):
    diagnostics = outcome.get("diagnostics") or {}
    return {key: diagnostics.get(key) for key in ("peak_tree_rss_mib", "peak_tree_hwm_mib", "lowest_available_mib")}


BUDGET_PADDING = " ".join(
    f"Background note {n}: the owner wants a careful explanation of how FCA body and chassis codes are "
    f"named, how failure-type bytes are read, and how module-specific meanings differ between model years."
    for n in range(1, 121))


def budget_check(runner, attempt):
    """A turn whose expected cost is far above the cap: enforced, not enforced, or inconclusive."""
    run_id = uuid.uuid4().hex
    prompt = (first_prompt(_measure_context()) + "\n\n" + BUDGET_PADDING +
              "\n\nWrite a 400-word answer explaining the code naming scheme described above.")
    try:
        outcome = runner.run_turn(attempt, TurnRequest(run_id, "claude", BUDGET_MODEL, "high", "initial", "report",
                                                        prompt, TIMEOUTS["reportSeconds"], budget_usd=BUDGET_CAP_USD))
    finally:
        runner.volume.delete_run(run_id)
    cost = (outcome.get("diagnostics") or {}).get("total_cost_usd")
    if outcome.get("reason") == "budget_exceeded":
        verdict = "enforced"
    elif outcome["status"] == "complete" and isinstance(cost, (int, float)) and cost > BUDGET_CAP_USD:
        verdict = "not_enforced"
    else:
        verdict = "inconclusive"
    return {"result": verdict, "status": outcome["status"], "reason": outcome.get("reason"),
            "total_cost_usd": cost, "cap_usd": BUDGET_CAP_USD, "prompt_words": len(prompt.split())}
