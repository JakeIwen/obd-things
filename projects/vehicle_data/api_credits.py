"""Pure rules for promotional API-credit billing of research turns (worker side, no effects).

Holds what decides and measures a credit launch: the strict key reader, the per-launch billing
choice, the classifier that says whether a failed credit turn failed because of the credits
themselves, and the spend tally read from a credit process's own result events. Files, the ledger
and process handling stay in ``api_credits_ledger``, ``research_credits`` and ``research_runner``.
The vocabulary (reasons, codes, ``until``) is ``api_credits.json`` via ``api_credits_contract``.
Nothing here returns or raises text that contains a key.
"""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import stat

from projects.vehicle_data.api_credits_contract import (
    ACCOUNT_CODES, API_CREDITS, KEY_FILE_MAX_BYTES, KEY_RE, MODEL_CODES, SUBSCRIPTION, UNTIL,
)

REPLAY_REQUIRED = "replay_required"
SESSION_MISSING = "session_missing"
KEY_ASSIGNMENT = re.compile(r"(?:export\s+)?ANTHROPIC_PROMO_API_KEY=(.*)\Z")
KEY_ANYWHERE = re.compile(r"sk-ant-[\w-]+")
API_KEY_SOURCE = "ANTHROPIC_API_KEY"
NO_KEY_SOURCE = "none"


def redact(text, key=None):
    """``text`` with the installed key (exact value) and anything shaped like an Anthropic key removed."""
    text = str(text)
    if key:
        text = text.replace(key, "[key]")
    return KEY_ANYWHERE.sub("[key]", text)


def scrub(value, key=None):
    """``value`` with every string inside it redacted (the boundary for everything a runner returns)."""
    if isinstance(value, str):
        return redact(value, key)
    if isinstance(value, dict):
        return {scrub(k, key) if isinstance(k, str) else k: scrub(v, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item, key) for item in value]
    return value


def number_or_none(value):
    """``value`` when it is a real int or float (not a bool, not NaN or infinite), else ``None``."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def is_failed_terminal_result(event):
    """A well-formed terminal ``result`` that reports failure: ``is_error`` true or an explicit
    non-success subtype string. A bare ``{"type": "result"}`` is malformed, not a failure."""
    if not isinstance(event, dict) or event.get("type") != "result":
        return False
    subtype = event.get("subtype")
    return event.get("is_error") is True or (isinstance(subtype, str) and bool(subtype) and subtype != "success")


# --- the key ----------------------------------------------------------------------------------------

def parse_key_text(text):
    """``(key, problem)`` from a file's text: one line, the bare key or ``[export ]ANTHROPIC_PROMO_API_KEY=<key>``.
    The text is never sourced or echoed; ``problem`` is a fixed sentence."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None, "the key file must hold exactly one line"
    value = lines[0]
    assignment = KEY_ASSIGNMENT.match(value)
    if assignment:
        value = assignment[1].strip()
    if value[:1] == value[-1:] and value[:1] in "'\"" and len(value) > 1:
        value = value[1:-1]
    return check_key(value)


def check_key(value):
    """``(key, problem)``: only a model key is accepted."""
    if KEY_RE.fullmatch(value):
        return value, None
    if value.startswith("sk-ant-admin"):
        return None, "that is an admin key, not a model key"
    if value.startswith("sk-ant-oat"):
        return None, "that is a Claude OAuth token, not an API key"
    return None, "that is not an Anthropic model API key"


def read_key_file(path, *, require_private=True):
    """``(key, problem)`` for a key file: a regular, non-symlink file of bounded size (and, for the
    volume's own copy, with no group or other permission bits) holding one model key."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "no API key is installed"
    except OSError:
        return None, "the API key file cannot be opened (a link or not readable)"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, "the API key file is not a regular file"
        if require_private and info.st_mode & 0o077:
            return None, "the API key file is readable by others"
        if info.st_size > KEY_FILE_MAX_BYTES:
            return None, "the API key file is too large"
        data = os.read(descriptor, KEY_FILE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > KEY_FILE_MAX_BYTES:
        return None, "the API key file is too large"
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None, "the API key file is not text"
    return parse_key_text(text)


# --- the billing choice -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Billing:
    """``choice`` is ``api_credits``, ``subscription`` or a pre-spawn refusal (``replay_required``,
    ``session_missing``); ``reason`` is a contract billing reason (None for credits)."""
    choice: str
    reason: str | None = None


def choose_billing(request, session_record, now, *, credits_allowed=False, enabled=False, key_installed=False,
                   latches=None, ledger_reason=None):
    """The billing of one launch, first match winning (design note section 3). ``request`` carries
    ``billing_capable``, ``forced_billing``, ``kind`` and ``model``; ``session_record`` is the run's
    ``session.json`` (resumes only); ``ledger_reason`` is ``ledger_unavailable`` or ``ledger_full``
    when the ledger cannot take a reservation. A resume stays with its session's origin."""
    outcome = _by_launch(request, now, credits_allowed, enabled, key_installed, latches or {}, ledger_reason)
    if request.kind != "resume":
        return outcome
    origin = (session_record or {}).get("billing") or SUBSCRIPTION
    if origin == API_CREDITS:
        if outcome.choice == API_CREDITS:
            return outcome
        return Billing(SESSION_MISSING if outcome.reason == "legacy" else REPLAY_REQUIRED, outcome.reason)
    # A subscription-created session never moves onto credits, even when they are eligible.
    return outcome if outcome.choice == SUBSCRIPTION else Billing(SUBSCRIPTION, "ineligible")


def _by_launch(request, now, credits_allowed, enabled, key_installed, latches, ledger_reason):
    if not request.billing_capable:
        return Billing(SUBSCRIPTION, "legacy")
    if request.forced_billing == SUBSCRIPTION:
        return Billing(SUBSCRIPTION, "forced")
    if not credits_allowed:
        return Billing(SUBSCRIPTION, "ineligible")
    if not enabled:
        return Billing(SUBSCRIPTION, "disabled")
    if not key_installed:
        return Billing(SUBSCRIPTION, "no_key")
    if now >= UNTIL:
        return Billing(SUBSCRIPTION, "expired")
    if latches.get("account") or request.model in (latches.get("models") or {}):
        return Billing(SUBSCRIPTION, "unavailable")
    if ledger_reason:
        return Billing(SUBSCRIPTION, ledger_reason)
    return Billing(API_CREDITS)


def latch_scope(code):
    """``"account"`` or ``"model"`` for a contract credit code."""
    if code in ACCOUNT_CODES:
        return "account"
    if code in MODEL_CODES:
        return "model"
    raise ValueError("unknown credit code")


# --- classifying a failed credit turn ---------------------------------------------------------------

WORKER_STOPS = frozenset({"cancelled", "timeout", "memory_exhausted", "space_exhausted", "policy_violation",
                          "resume_unverified", "protocol_error"})
PRE_INIT_RESULT = "credit_pre_init"
TYPED_ERRORS = {"billing_error": "exhausted", "authentication_failed": "invalid_key",
                "model_not_found": "model_unavailable"}
# Bare status numbers and ``permission_error`` are left out on purpose: they match unrelated failures.
_TRANSIENT = re.compile(r"overloaded|rate[_ ]limit|\b429\b|\b529\b")
_TEXT_RULES = (
    ("exhausted", re.compile(r"credit balance is too low|insufficient[_ ]credit|out of credits|purchase more "
                             r"credits|exceeded your (?:credit|spend)|billing[_ ]error|plans? (?:&|and) billing")),
    ("invalid_key", re.compile(r"invalid api key|invalid x-api-key|authentication_error|"
                               r"api key.{0,40}(?:revoked|disabled|expired)|fix external api key|"
                               r"organization has disabled api key authentication|"
                               r"anthropic_api_key belongs to a disabled organization")),
    ("model_unavailable", re.compile(r"not_found_error|model.{0,60}(?:not found|not available|does not exist|"
                                     r"not supported)|(?:not found|no access|not available).{0,40}model")),
)
TEXT_MAX = 4000
# The CLI's terminal result carries these identifiers itself (``api_error_code`` is the server's
# error.details.error_code, ``api_error_type`` its error.type, ``api_error_status`` the HTTP status).
STRUCTURED_CODES = {"api_key_auth_disabled": "invalid_key", "org_disabled_credential": "invalid_key"}
CODE_FIELDS = ("api_error_code", "api_error", "error_code")
TRANSIENT_TYPES = frozenset({"rate_limit_error", "overloaded_error"})
TRANSIENT_STATUSES = frozenset({429, 529})


@dataclass
class CreditEnd:
    """What a finished credit launch left behind; never tool results or assistant text."""
    stop_reason: str | None
    returncode: int | None
    complete: bool  # an answer was accepted
    result: dict | None
    typed_error: str | None
    init_seen: bool
    stderr_tail: str = ""


def _terminal_texts(result):
    texts = []
    if result.get("is_error") and isinstance(result.get("result"), str):
        texts.append(result["result"])
    errors = result.get("errors")
    if isinstance(errors, list):
        texts.extend(item for item in errors if isinstance(item, str))
    return [text[:TEXT_MAX].lower() for text in texts]


def _failed_end(end, result):
    ended_badly = is_failed_terminal_result(result)
    return ended_badly or end.returncode not in (0, None)


def _structured_identifiers(result):
    """Identifier strings the terminal result names itself, top level and inside an ``error`` object."""
    holders = [result] + ([result["error"]] if isinstance(result.get("error"), dict) else [])
    return [holder[field] for holder in holders for field in CODE_FIELDS if isinstance(holder.get(field), str)]


def _structurally_transient(result):
    status = result.get("api_error_status")
    return result.get("api_error_type") in TRANSIENT_TYPES or (
        isinstance(status, int) and not isinstance(status, bool) and status in TRANSIENT_STATUSES)


def classify_credit_failure(end):
    """The fixed credit code when the credits themselves failed this launch, else ``None``.
    Precedence: the worker's own stops, then a terminal result's own type, then a typed assistant
    error that the process really ended on, then bounded text of the terminal result."""
    if end.stop_reason == "key_ignored":
        return "key_ignored"
    if end.stop_reason in WORKER_STOPS or end.complete:
        return None
    result = end.result or {}
    if result.get("subtype") == "error_max_budget_usd":
        return None
    if _structurally_transient(result):
        return None
    texts = _terminal_texts(result)
    if any(_TRANSIENT.search(text) for text in texts):
        return None
    failed = _failed_end(end, result)
    if failed:
        for identifier in _structured_identifiers(result):
            if identifier in STRUCTURED_CODES:
                return STRUCTURED_CODES[identifier]
    if end.typed_error in TYPED_ERRORS and failed:
        return TYPED_ERRORS[end.typed_error]
    scanned = texts + ([end.stderr_tail[-TEXT_MAX:].lower()] if not end.init_seen else [])
    for code, pattern in _TEXT_RULES:
        if any(pattern.search(text) for text in scanned):
            return code
    if not end.init_seen and is_failed_terminal_result(end.result) and not end.stderr_tail.strip():
        return "invalid_key"  # a failed result before init and nothing on stderr: the key cannot be used
    return None


# --- spend ------------------------------------------------------------------------------------------

def _usd(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok and 0 <= value <= 10000 else None


def delta_cost(delta):
    """Total ``costUSD`` of a ``model_usage_since_last_result`` object, or ``None`` when it is not valid."""
    if not isinstance(delta, dict):
        return None
    total = 0.0
    for model, usage in delta.items():
        cost = _usd(usage.get("costUSD")) if isinstance(model, str) and isinstance(usage, dict) else None
        if cost is None:
            return None
        total += cost
    return total


@dataclass
class UsageTally:
    """Spend of one credit process, from its result events' own per-process deltas only. The
    cumulative ``total_cost_usd`` (restored from the transcript on resume) is never read."""
    known_usd: float = 0.0
    unknown: bool = False
    results: int = 0
    _seen: set = field(default_factory=set)
    _next: int = 0

    def add(self, event):
        index = event.get("result_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            self.unknown = True
            return
        if index in self._seen:
            return  # a repeated result never adds spend
        self._seen.add(index)
        self.results += 1
        if index != self._next:
            self.unknown = True
        self._next = max(self._next, index + 1)
        cost = delta_cost(event.get("model_usage_since_last_result"))
        if cost is None:
            self.unknown = True
        else:
            self.known_usd += cost

    def settled(self, *, clean_exit):
        """``(known_usd, unknown)``: no result at all or an interrupted process leaves the rest unknown."""
        unknown = self.unknown or not self.results or not clean_exit
        return round(self.known_usd, 6), unknown
