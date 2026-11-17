"""A scripted stand-in for the Claude CLI, for the research worker tests (no network, no model).

``install(directory, scenario)`` writes an executable ``claude`` that follows ``scenario`` (a dict,
saved beside it as ``scenario.json``, since the worker passes the child only an allowlisted
environment). The fake records its argv, cwd and environment to ``calls.jsonl`` beside itself.
"""

import json
from pathlib import Path
import stat
import sys

SCRIPT = r'''#!{python}
import json, os, signal, subprocess, sys, time
from pathlib import Path
HERE = Path("{state}")
S = json.loads((HERE / "scenario.json").read_text())
argv = sys.argv[1:]
with open(HERE / "calls.jsonl", "a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ), "pid": os.getpid(),
                          "at": time.time()}) + "\n")
def emit(event):
    print(json.dumps(event), flush=True)
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
if "--help" in argv:
    print(S.get("help", "--print --input-format --output-format --verbose --session-id --resume --model --effort "
          "--system-prompt --tools --allowed-tools --permission-mode --permission-prompts --setting-sources "
          "--strict-mcp-config --restricted --disable-slash-commands --max-budget-usd"))
    sys.exit(0)
if opt("--input-format") == "stream-json":
    sys.stdin.readline()
    time.sleep(S.get("discovery_delay", 0))
    emit({"type": "control_response", "response": {"subtype": "success", "response": {
        "models": S.get("models", [{"value": "opus", "displayName": "Opus", "supportsEffort": True,
                                     "supportedEffortLevels": ["low", "medium", "high"]},
                                    {"value": "default", "displayName": "Default (recommended)"}]),
        "account": {"tokenSource": "CLAUDE_CODE_OAUTH_TOKEN" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "none"}}}})
    sys.stdin.read()
    sys.exit(0)
prompt = sys.stdin.read()
session = opt("--session-id") or opt("--resume")
slug = os.getcwd().replace("/", "-")
store = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / slug
path = store / (session + ".jsonl")
if "--resume" in argv and not path.exists():
    print("No conversation found with session ID", file=sys.stderr)
    sys.exit(1)
store.mkdir(parents=True, exist_ok=True)
mode = S.get("mode", "ok")
if mode != "no_growth" or not path.exists():
    with open(path, "a") as f:
        f.write(json.dumps({"prompt_chars": len(prompt)}) + "\n")
if mode == "non_json":
    print("this is not json", flush=True)
    time.sleep(5)
if mode == "huge_line":
    sys.stdout.write("x" * (5 * 1024 * 1024))
    sys.stdout.flush()
    time.sleep(5)
tools = S.get("tools", ["WebSearch", "WebFetch"])
emit({"type": "system", "subtype": "init", "session_id": S.get("init_session", session), "tools": tools,
       "mcp_servers": []})
if mode == "hang":
    time.sleep(60)
if mode == "grandchild":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (HERE / "grandchild.pid").write_text(str(child.pid))
    time.sleep(60)
if mode == "grow_ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    hog = bytearray(S.get("grow_bytes", 64 * 1024 * 1024))
    for i in range(0, len(hog), 4096):
        hog[i] = 1
    time.sleep(60)
if mode == "forbidden_tool":
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}})
    time.sleep(5)
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {}}]}})
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebFetch", "input": {}}]}})
time.sleep(S.get("delay", 0))
if mode == "budget":
    emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 0.02})
    sys.exit(1)
if mode == "exit_error":
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True})
    sys.exit(1)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": S.get("answer", "SUMMARY: Fine.\n## What it means\nok"),
       "total_cost_usd": S.get("cost", 0.5), "num_turns": 3})
'''


def install(directory, scenario, state=None):
    """Write the fake into ``directory``; its scenario and call log live in ``state`` (default:
    the same directory), so a release directory can stay unchanged for its identity digest."""
    directory = Path(directory)
    state = Path(state or directory)
    directory.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    path.write_text(SCRIPT.replace("{python}", sys.executable).replace("{state}", str(state)))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    set_scenario(state, scenario)
    return path


def set_scenario(directory, scenario):
    (Path(directory) / "scenario.json").write_text(json.dumps(scenario))


def calls(directory):
    path = Path(directory) / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
