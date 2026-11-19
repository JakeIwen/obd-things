"""Command lines for the research CLIs and the readers of what they print. No process handling.

Claude's pre-execution gates are its flags: ``--tools`` defines what is offered, ``dontAsk`` with
``--allowed-tools`` refuses anything else before it runs, ``--restricted`` drops code-running
tools, and no settings, MCP servers or slash commands load. The stream readers are a supplement:
an init event offering any other tool, or a call to one, stops the run. Codex is disabled by
policy this round; its builders and readers exist so a verified version can be enabled.
"""

import json
import os
from pathlib import Path

from projects.vehicle_data.api_credits import (
    API_KEY_SOURCE, NO_KEY_SOURCE, PRE_INIT_RESULT, TYPED_ERRORS, UsageTally, is_failed_terminal_result,
    number_or_none, redact,
)
from projects.vehicle_data.cli_process import StopRun
from projects.vehicle_data.dtc_research_prompt import SYSTEM
from projects.vehicle_data.research_cli_policy import POLICY

ENV_PASSED = ("LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
PATH_VALUE = "/usr/bin:/bin"
TOKEN_MAX = 4096


def _base_env(home, tmpdir):
    env = {name: os.environ[name] for name in ENV_PASSED if name in os.environ}
    env.update(HOME=str(home), PATH=PATH_VALUE, TMPDIR=str(tmpdir))
    return env


def read_token(path):
    """The Claude token, or a sentence why it cannot be used. Never logged or returned to callers."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, "Claude is not signed in for research yet"
    if not path.is_file() or path.is_symlink() or info.st_mode & 0o077 or info.st_size > TOKEN_MAX:
        return None, "the Claude sign-in file has the wrong type, mode or size"
    token = path.read_text().strip()
    if not token or any(not (ch.isalnum() or ch in "._-") for ch in token):
        return None, "the Claude sign-in file is not a token"
    return token, None


def claude_env(home, tmpdir, token):
    env = _base_env(home, tmpdir)
    env.update(CLAUDE_CONFIG_DIR=str(Path(home) / ".claude"), CLAUDE_CODE_OAUTH_TOKEN=token,
               DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


def claude_credit_env(home, tmpdir, key):
    """A credit launch's environment: the subscription one without the OAuth token, plus the API key
    and the public API endpoint (never a locally set base URL). Only this one child ever gets the key."""
    env = claude_env(home, tmpdir, "")
    del env["CLAUDE_CODE_OAUTH_TOKEN"]
    env.update(ANTHROPIC_API_KEY=key, ANTHROPIC_BASE_URL="https://api.anthropic.com")
    return env


def claude_argv(executable, *, model, effort, session_id, resume, budget_usd):
    tools = ",".join(POLICY["claude"]["allowed_tools"])
    argv = [str(executable), "--print", "--input-format", "text", "--output-format", "stream-json", "--verbose",
            "--resume" if resume else "--session-id", session_id, "--model", model]
    if effort:
        argv += ["--effort", effort]
    return argv + ["--system-prompt", SYSTEM, "--tools", tools, "--allowed-tools", tools,
                   "--permission-mode", "dontAsk", "--permission-prompts", "none", "--setting-sources", "",
                   "--strict-mcp-config", "--restricted", "--disable-slash-commands",
                   "--max-budget-usd", f"{budget_usd:g}"]


def claude_discovery_argv(executable):
    tools = ",".join(POLICY["claude"]["allowed_tools"])
    return [str(executable), "--print", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--setting-sources", "", "--strict-mcp-config", "--restricted", "--disable-slash-commands",
            "--tools", tools, "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence"]


INITIALIZE_REQUEST = json.dumps({"type": "control_request", "request_id": "discover",
                                 "request": {"subtype": "initialize"}}) + "\n"


def parse_discovery(event):
    """``(models, signed_in)`` from the initialize ``control_response``, or ``None`` for other events."""
    if event.get("type") != "control_response":
        return None
    inner = (event.get("response") or {}).get("response") or {}
    allowed = set(POLICY["claude"]["models"])
    models = []
    for entry in inner.get("models") or []:
        if not isinstance(entry, dict) or entry.get("value") not in allowed:
            continue
        efforts = [e for e in entry.get("supportedEffortLevels") or [] if isinstance(e, str)] \
            if entry.get("supportsEffort") else []
        label = str(entry.get("displayName") or entry["value"]).replace("(recommended)", "").strip()[:120]
        default = "high" if "high" in efforts else (efforts[len(efforts) // 2] if efforts else None)
        models.append({"id": entry["value"], "label": label, "efforts": efforts, "default_effort": default})
    account = inner.get("account") or {}
    return models, account.get("tokenSource") not in (None, "none")


class ClaudeStream:
    """Reads ``--output-format stream-json`` events; raises ``StopRun`` on anything outside policy.

    ``credit`` marks a launch billed to the API key: init must then report that key as its source
    (else the worker stops it as ``key_ignored``), a subscription launch must report none, and a
    credit launch's result events feed ``tally``."""

    def __init__(self, expected_session, *, credit=False, key=None):
        self.expected_session = expected_session
        self.credit = credit
        self.key = key  # redacted from the answer before it is stored (and so before any truncation)
        self.tally = UsageTally()
        self.typed_error = None
        self.api_key_source = None
        self.allowed = set(POLICY["claude"]["allowed_tools"])
        self.session_id = None
        self.searches = self.pages = 0
        self.phase = "starting"
        self.result = None
        self.answer = None

    def __call__(self, event):
        kind = event.get("type")
        if self.session_id is None:
            if self.credit and kind == "result" and self._is_failed_result(event):
                # Diagnostic evidence only: it authorizes nothing and ends the run.
                self.result = event
                self.tally.add(event)
                raise StopRun(PRE_INIT_RESULT, "Claude ended before it started", immediate=True)
            if kind != "system" or event.get("subtype") != "init":
                raise StopRun("protocol_error", "Claude did not start with its init event")
            tools = event.get("tools")
            if not isinstance(tools, list) or not set(tools) <= self.allowed or event.get("mcp_servers"):
                raise StopRun("policy_violation", "Claude offered tools outside research mode", immediate=True)
            self.session_id = event.get("session_id")
            if self.session_id != self.expected_session:
                raise StopRun("resume_unverified", "Claude started a different session", immediate=True)
            self._check_key_source(event)
            return
        if kind == "assistant":
            if isinstance(event.get("error"), str):
                self.typed_error = event["error"] if event["error"] in TYPED_ERRORS else None
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._tool(block.get("name"))
                elif isinstance(block, dict) and block.get("type") == "text":
                    self.phase = "writing"
        elif kind == "result":
            self.result = event
            if self.credit:
                self.tally.add(event)
            if event.get("subtype") == "success" and not event.get("is_error") and isinstance(event.get("result"), str):
                self.answer = redact(event["result"], self.key)
            self.phase = "finishing"

    @staticmethod
    def _is_failed_result(event):
        return is_failed_terminal_result(event)

    def _check_key_source(self, event):
        self.api_key_source = event.get("apiKeySource")
        if self.credit and self.api_key_source != API_KEY_SOURCE:
            raise StopRun("key_ignored", "Claude did not use the API key for this credit launch", immediate=True)
        if not self.credit and self.api_key_source != NO_KEY_SOURCE:
            raise StopRun("policy_violation", "Claude reported an API key on a subscription launch", immediate=True)

    def _tool(self, name):
        if name not in self.allowed:
            raise StopRun("policy_violation", "Claude called a tool outside research mode", immediate=True)
        if name == "WebSearch":
            self.searches += 1
            self.phase = "searching"
        else:
            self.pages += 1
            self.phase = "reading"

    def failure(self):
        """The turn reason when no answer was accepted."""
        result = self.result or {}
        if result.get("subtype") == "error_max_budget_usd":
            return "budget_exceeded"
        text = json.dumps(result).lower()
        if "401" in text or "unauthorized" in text or "oauth" in text:
            return "auth"
        if "rate limit" in text or "429" in text:
            return "upstream_rate_limit"
        return "provider_error"

    def diagnostics(self):
        result = self.result or {}
        return {"num_turns": number_or_none(result.get("num_turns")),
                "total_cost_usd": number_or_none(result.get("total_cost_usd"))}


def claude_session_files(home, session_id):
    return sorted((Path(home) / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))


def session_size(paths):
    return sum(path.stat().st_size for path in paths if path.exists())


# --- Codex (disabled by policy this round) --------------------------------------------------------

def codex_argv(executable, *, workdir, model, effort, log_dir, thread_id=None):
    config = {"approval_policy": "never", "default_permissions": "dtc-research",
              "permissions.dtc-research.filesystem": {":minimal": "read", ":workspace_roots": "read"},
              "permissions.dtc-research.network.enabled": False, "web_search": "live",
              "project_doc_max_bytes": 0, "history.persistence": "none", "developer_instructions": SYSTEM,
              "log_dir": str(log_dir), "model": model}
    if effort:
        config["model_reasoning_effort"] = effort
    for feature in POLICY["codex"]["features_disabled"]:
        config[f"features.{feature}"] = False
    argv = [str(executable), "exec"] + (["resume"] if thread_id else []) + [
        "--ignore-user-config", "--ignore-rules", "--strict-config", "--skip-git-repo-check", "--json"]
    if not thread_id:
        argv += ["--color", "never", "-C", str(workdir)]
    for key, value in config.items():
        literal = ("{" + ", ".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in value.items()) + "}"
                   if isinstance(value, dict) else json.dumps(value))
        argv += ["-c", f"{key}={literal}"]
    return argv + ([thread_id] if thread_id else []) + ["-"]


def codex_env(home, tmpdir, codex_home):
    env = _base_env(home, tmpdir)
    env["CODEX_HOME"] = str(codex_home)
    return env


def offered_local_tools(rollout_lines):
    """Tool names in a rollout's ``additional_tools`` record; raises ``ValueError`` when the record is
    missing, malformed or incomplete (never read as an empty list)."""
    record = None
    for line in rollout_lines:
        item = json.loads(line)
        payload = item.get("payload") if isinstance(item, dict) else None
        if item.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "additional_tools":
            record = payload
    if record is None or not isinstance(record.get("tools"), list):
        raise ValueError("no complete additional_tools record")
    names = []

    def walk(entry, prefix):
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not isinstance(entry.get("name"), str):
            raise ValueError("a tool entry without a type and name")
        name = f"{prefix}{entry['name']}"
        if entry["type"] == "namespace":
            if not isinstance(entry.get("tools"), list):
                raise ValueError("a namespace without tools")
            for child in entry["tools"]:
                walk(child, name + ".")
        else:
            names.append(name)

    for entry in record["tools"]:
        walk(entry, "")
    return names


class CodexStream:
    """Reads ``codex exec --json`` events; raises ``StopRun`` on any item outside policy."""

    def __init__(self, expected_thread=None):
        self.expected_thread = expected_thread
        self.allowed = set(POLICY["codex"]["allowed_items"])
        self.thread_id = None
        self.searches = self.pages = 0
        self.phase = "starting"
        self.answer = None
        self.completed = False
        self.errors = []

    def __call__(self, event):
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_id = event.get("thread_id")
            if self.expected_thread and self.thread_id != self.expected_thread:
                raise StopRun("resume_unverified", "Codex started a different thread", immediate=True)
            return
        if kind == "turn.completed":
            self.completed = True
            self.phase = "finishing"
            return
        if kind in ("error", "turn.failed"):
            self.errors.append(json.dumps(event)[-400:])
            return
        if kind not in ("item.started", "item.updated", "item.completed"):
            return
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in self.allowed:
            raise StopRun("policy_violation", "Codex used an item outside research mode", immediate=True)
        if item["type"] == "web_search" and kind == "item.started":
            self.searches += 1
            self.phase = "searching"
        if item["type"] == "agent_message" and kind == "item.completed":
            self.answer = item.get("text")
            self.phase = "writing"
