"""DTC research vocabulary shared by the advisor, the research worker and the dashboard.

``dtc_research.json`` is the one copy of the routes, states, reasons, limits and sizes; this
module loads it and adds the pure checks both Python sides need: request validation, the web
relay's route allowlist, and size accounting as the advisor encodes replies (``json.dumps``
defaults, so ``ensure_ascii``: a non-BMP character costs 12 bytes).
"""

import json
from pathlib import Path
import re

from projects.vehicle_data.assistant_common import ChatError, require_keys

CONTRACT = json.loads((Path(__file__).with_name("dtc_research.json")).read_text())

ROUTES = CONTRACT["routes"]
STATES = CONTRACT["states"]
TURN_STATES = CONTRACT["turnStates"]
MODES = CONTRACT["modes"]
SUMMARY_NOTES = CONTRACT["summaryNotes"]
CONTINUE_MODES = CONTRACT["continueModes"]
PHASES = CONTRACT["phases"]
SIZES = CONTRACT["sizes"]
LIMITS = CONTRACT["limits"]
TIMEOUTS = CONTRACT["timeouts"]
WORKER = CONTRACT["worker"]
PROVIDERS = CONTRACT["providers"]
EFFORT_ORDER = CONTRACT["effortOrder"]
ERROR_STATUS = CONTRACT["errorReasons"]
TURN_REASONS = frozenset(CONTRACT["turnReasons"])
QUESTION_MAX = CONTRACT["questionMax"]
SUMMARY_MAX = CONTRACT["summaryMax"]
MAX_TURNS = CONTRACT["maxTurns"]
MAX_RUNS = CONTRACT["maxRuns"]

MODULE_KEY_RE = re.compile(CONTRACT["patterns"]["moduleKey"])
CODE_RE = re.compile(CONTRACT["patterns"]["code"])
RUN_ID_RE = re.compile(CONTRACT["patterns"]["runId"])
REQUEST_ID_RE = re.compile(CONTRACT["patterns"]["requestId"])
MODEL_ID_RE = re.compile(CONTRACT["patterns"]["modelId"])

_CODE_PATH = re.compile(re.escape(ROUTES["codePrefix"]) + r"([^/]+)/([^/]+)")
_RUN_PATH = re.compile(re.escape(ROUTES["runPrefix"]) + r"([^/]+)(" + re.escape(ROUTES["progressSuffix"]) + r")?")
TRUNCATION_MARK = "\n\n[truncated on the Pi]"


class ResearchError(ChatError):
    """A refusal with one of the contract's error reasons; its HTTP status comes from the contract."""

    def __init__(self, reason, message):
        if reason not in ERROR_STATUS:
            raise ValueError(f"unknown research error reason {reason!r}")
        self.reason = reason
        super().__init__(ERROR_STATUS[reason], message)


def is_research_path(path):
    return path == ROUTES["models"] or path == ROUTES["list"] or path.startswith(ROUTES["list"] + "/")


def parse_path(method, path):
    """``(route, args)`` for a research route, or ``None``. Arguments are validated here."""
    if method == "GET" and path == ROUTES["models"]:
        return "models", ()
    if path == ROUTES["list"]:
        return ("list", ()) if method == "GET" else ("post", ()) if method == "POST" else None
    if method != "GET":
        return None
    if match := _CODE_PATH.fullmatch(path):
        module_key, code = match.groups()
        if not MODULE_KEY_RE.fullmatch(module_key) or not CODE_RE.fullmatch(code):
            raise ResearchError("invalid_request", "That is not a code the research service accepts")
        return "code", (module_key, code)
    if match := _RUN_PATH.fullmatch(path):
        if not RUN_ID_RE.fullmatch(match[1]):
            raise ResearchError("invalid_request", "Invalid research run id")
        return ("progress" if match[2] else "run"), (match[1],)
    return None


def relay_allowed(method, path):
    """Whether the web relay may forward this research request to the advisor."""
    try:
        return parse_path(method, path) is not None
    except ChatError:
        return True  # well-formed route, bad argument: the advisor answers the 400


def _hex_id(payload, key, pattern):
    value = payload[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ResearchError("invalid_request", f"Invalid {key.replace('_', ' ')}")
    return value


def validate_post(payload):
    """The exact POST body for one action, as ``(action, fields)``."""
    if not isinstance(payload, dict) or payload.get("action") not in CONTRACT["actions"].values():
        raise ResearchError("invalid_request", "Unknown research action")
    action = payload["action"]
    keys = {"start": {"action", "module_key", "fca_display", "provider", "model", "effort", "request_id"},
            "ask": {"action", "run_id", "question", "request_id"},
            "cancel": {"action", "run_id", "request_id"}}[action]
    try:
        require_keys(payload, keys)
    except ChatError:
        raise ResearchError("invalid_request", "Unexpected or missing request fields") from None
    fields = {"request_id": _hex_id(payload, "request_id", REQUEST_ID_RE)}
    if action == "start":
        module_key, code = payload["module_key"], payload["fca_display"]
        if not isinstance(module_key, str) or not MODULE_KEY_RE.fullmatch(module_key) \
                or not isinstance(code, str) or not CODE_RE.fullmatch(code):
            raise ResearchError("invalid_request", "That is not a code the research service accepts")
        provider, model, effort = payload["provider"], payload["model"], payload["effort"]
        if provider not in PROVIDERS or not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ResearchError("invalid_request", "Choose an offered provider and model")
        if effort is not None and effort not in EFFORT_ORDER:
            raise ResearchError("invalid_request", "Choose an offered effort")
        fields.update(module_key=module_key, fca_display=code, provider=provider, model=model, effort=effort)
    else:
        fields["run_id"] = _hex_id(payload, "run_id", RUN_ID_RE)
    if action == "ask":
        question = payload["question"]
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= QUESTION_MAX \
                or encoded_len(question.strip()) > SIZES["questionEncodedBytes"]:
            raise ResearchError("invalid_request", f"Use a question between 1 and {QUESTION_MAX:,} characters")
        fields["question"] = clean_text(question.strip())
    return action, fields


def encoded_len(value):
    """Bytes ``value`` takes in a reply as the advisor and the relay encode it."""
    return len(json.dumps(value, separators=(",", ":")))


def clean_text(text):
    """Drop control characters other than newline and tab."""
    return "".join(ch for ch in text if ch in "\n\t" or not (ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0))


def truncate_encoded(text, limit):
    """``(text, truncated)`` with ``text`` at most ``limit`` encoded bytes, marked when cut."""
    if encoded_len(text) <= limit:
        return text, False
    budget = limit - encoded_len(TRUNCATION_MARK) + 2  # the mark shares the quotes
    low, high = 0, len(text)
    while low < high:  # longest prefix that fits: encoded length grows with the prefix
        middle = (low + high + 1) // 2
        if encoded_len(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low] + TRUNCATION_MARK, True
