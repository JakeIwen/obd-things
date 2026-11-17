"""The research model picker's catalog: which providers can be used now, and their models.

Availability comes from the research worker's status (installed and verified CLI copy, sign-in,
public DNS, memory activation); a provider is never shown as available on a fallback list alone.
Claude's models come from its own model check, run by the worker in its slot (no model turn);
until that answers, the tracked fallback list is shown. One serialized refresh thread keeps the
catalog fresh for 15 minutes after a success and retries after 60 seconds after a failure; a
request never waits for it.
"""

import json
from pathlib import Path
import threading
import time
import uuid

from projects.vehicle_data.assistant_common import now
from projects.vehicle_data.dtc_research_contract import EFFORT_ORDER, MODEL_ID_RE, PROVIDERS, ResearchError
from projects.vehicle_data.research_worker_client import WorkerUnreachable

FALLBACK = json.loads(Path(__file__).with_name("assistant_models.json").read_text())
FRESH_SECONDS = 15 * 60
RETRY_SECONDS = 60
DISCOVERY_WAIT = 40
WORKER_DOWN = "The research service on the Pi is not running"


def _group(provider, available, reason, source, checked_at, models):
    return {"provider": provider, "label": PROVIDERS[provider]["label"], "available": available,
            "reason": reason, "source": source, "checked_at": checked_at, "models": models if available else []}


class Catalog:
    def __init__(self, worker, clock=time.monotonic, poll=1.0):
        self.worker, self.clock, self.poll = worker, clock, poll
        self.lock = threading.Lock()
        self.groups = {name: _group(name, False, "Not checked yet", "fallback", None, []) for name in PROVIDERS}
        self.fresh_until = 0.0
        self.refreshing = False
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.wake.set()

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def reply(self):
        with self.lock:
            if self.clock() >= self.fresh_until and not self.refreshing:
                self.refreshing = True
                self.wake.set()
            groups = [dict(group) for group in self.groups.values()]
            refreshing = self.refreshing
        return {"available": True, "refreshing": refreshing, "groups": groups, "default": default_choice(groups)}

    def validate(self, provider, model, effort):
        with self.lock:
            group = self.groups.get(provider)
            if not group or not group["available"]:
                raise ResearchError("provider_unavailable", (group or {}).get("reason") or "That provider is not available")
            entry = next((m for m in group["models"] if m["id"] == model), None)
        if entry is None or not MODEL_ID_RE.fullmatch(model):
            raise ResearchError("invalid_request", "Choose a model the picker offers")
        if (effort is None) != (not entry["efforts"]) or (effort is not None and effort not in entry["efforts"]):
            raise ResearchError("invalid_request", "Choose an effort the model offers")

    def _loop(self):
        while not self.stopped.is_set():
            self.wake.wait(RETRY_SECONDS)
            self.wake.clear()
            if self.stopped.is_set():
                return
            with self.lock:
                due = self.clock() >= self.fresh_until
                self.refreshing = due
            if due:
                ok = self.refresh()
                with self.lock:
                    self.fresh_until = self.clock() + (FRESH_SECONDS if ok else RETRY_SECONDS)
                    self.refreshing = False

    def refresh(self):
        """One serialized refresh; ``True`` when every available provider answered."""
        stamp = now()
        try:
            status = self.worker.status()
        except WorkerUnreachable:
            with self.lock:
                for name in PROVIDERS:
                    self.groups[name] = _group(name, False, WORKER_DOWN, "fallback", stamp, [])
            return False
        ok = True
        for name in PROVIDERS:
            available, reason = provider_available(status, name)
            disabled = ((status.get("providers") or {}).get(name) or {}).get("identity") == "disabled"
            ok = ok and (available or disabled)  # retry soon while a usable provider is unavailable
            models, source = list(FALLBACK["providers"][name]), "fallback"
            if available and name == "claude":
                discovered = self._discover(status["instance_id"])
                if discovered is None:
                    ok = False
                else:
                    models, source = discovered, "discovered"
            with self.lock:
                self.groups[name] = _group(name, available, reason, source, stamp, models)
        return ok

    def _discover(self, instance_id):
        attempt_id = uuid.uuid4().hex
        try:
            code, _reply = self.worker.launch("/discover", {"attempt_id": attempt_id,
                                                            "expected_instance_id": instance_id, "provider": "claude"})
            if code != 202:
                return None
            deadline = self.clock() + DISCOVERY_WAIT
            while self.clock() < deadline and not self.stopped.is_set():
                state = self.worker.attempt(attempt_id)
                if state.get("known") and state.get("outcome_state") == "held":
                    self.worker.acknowledge(attempt_id)
                    outcome = state["outcome"]
                    return _clean_models(outcome.get("models")) if outcome.get("status") == "complete" else None
                if not state.get("known"):
                    return None
                time.sleep(self.poll)
            self.worker.cancel(attempt_id)
        except WorkerUnreachable:
            return None
        return None


def provider_available(status, provider):
    if not status.get("dns_ok"):
        return False, "Public DNS is unreachable from the Pi's research service; research is unavailable on this network"
    state = (status.get("providers") or {}).get(provider) or {}
    if state.get("identity") != "ok":
        return False, state.get("reason") or f"{PROVIDERS[provider]['label']} is not available for research"
    if not state.get("signed_in"):
        return False, state.get("reason") or f"{PROVIDERS[provider]['label']} is not signed in for research yet"
    memory = status.get("memory_policy") or {}
    if not memory.get("activated"):
        return False, memory.get("reason") or "Research is not activated on this Pi yet"
    return True, None


def _clean_models(models):
    cleaned = []
    for entry in models or []:
        if not isinstance(entry, dict) or not MODEL_ID_RE.fullmatch(str(entry.get("id", ""))):
            continue
        efforts = [e for e in entry.get("efforts") or [] if e in EFFORT_ORDER]
        default = entry.get("default_effort") if entry.get("default_effort") in efforts else None
        cleaned.append({"id": entry["id"], "label": str(entry.get("label") or entry["id"])[:120],
                        "efforts": efforts, "default_effort": default})
    return cleaned or None


def default_choice(groups):
    wanted = FALLBACK["default"]
    for group in groups:
        if group["provider"] == wanted["provider"] and group["available"]:
            entry = next((m for m in group["models"] if m["id"] == wanted["model"]), None)
            if entry and wanted["effort"] in entry["efforts"]:
                return dict(wanted)
    for group in groups:
        if group["available"] and group["models"]:
            first = group["models"][0]
            return {"provider": group["provider"], "model": first["id"], "effort": first["default_effort"]}
    return None
