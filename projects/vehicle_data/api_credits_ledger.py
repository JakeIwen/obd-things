"""The research worker's credit ledger: one durable file, one writer, display-only spend.

``/var/lib/vanresearch/api-credits.json`` holds one entry per credit launch (opened before the
process spawns, settled from its own result deltas), the latches that stop later credit launches
until the worker restarts, and the last fallback. The in-memory record is only replaced after a
write succeeded, so a failed write leaves memory and the last durable file agreeing. Entries are
never compacted: at the bound new credit launches are refused. The balance itself is the advisor's
view; nothing here decides billing from an estimate.
"""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import threading

from lib.reports import atomic_json
from projects.vehicle_data.api_credits import latch_scope
from projects.vehicle_data.api_credits_contract import ACCOUNT_CODES, CREDIT_CODES, LEDGER, MODEL_CODES, UNTIL

OPEN, SETTLED = "open", "settled"
FILE_MAX_BYTES = 4 * 1024 * 1024  # 2000 entries are well under 1 MiB
TEXT_LIMIT = 200  # bound on an attempt id, model name or latch table size
ENTRY_FIELDS = ("attempt_id", "model", "started_at", "settled_at", "known_usd", "unknown", "state")


class LedgerError(Exception):
    """A reservation or settlement that could not be made; ``reason`` is a contract billing reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty():
    return {"schema_version": LEDGER["schemaVersion"], "entries": [],
            "latches": {"account": None, "models": {}}, "last_fallback": None}


def _stamp_ok(value):
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return False
    return True


def _code(value, allowed):
    """``value`` is a string in ``allowed`` (the type is checked first: a list is not hashable)."""
    return isinstance(value, str) and value in allowed


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def _entry_valid(entry):
    if not isinstance(entry, dict) or entry.keys() != set(ENTRY_FIELDS) or entry["state"] not in (OPEN, SETTLED):
        return False
    known = entry.get("known_usd")
    if isinstance(known, bool) or not isinstance(known, (int, float)) or not 0 <= known <= 10000:
        return False  # NaN and infinity fail the range test
    settled_at = entry.get("settled_at")
    return _text(entry.get("attempt_id"), TEXT_LIMIT) and _text(entry.get("model"), TEXT_LIMIT) \
        and _stamp_ok(entry.get("started_at")) and isinstance(entry.get("unknown"), bool) \
        and (_stamp_ok(settled_at) if entry["state"] == SETTLED else settled_at is None)


def _fallback_valid(fallback):
    return fallback is None or (isinstance(fallback, dict) and fallback.keys() == {"at", "code", "model"}
                                and _stamp_ok(fallback["at"]) and _code(fallback["code"], CREDIT_CODES)
                                and _text(fallback["model"], TEXT_LIMIT))


def _project(record):
    """Validate all fields before projecting into the worker's bounded, shallow schema."""
    if not isinstance(record, dict) or record.keys() != _empty().keys() \
            or type(record["schema_version"]) is not int or record["schema_version"] != LEDGER["schemaVersion"]:
        raise ValueError("invalid ledger record")
    latches, entries, fallback = record["latches"], record["entries"], record["last_fallback"]
    if not isinstance(latches, dict) or latches.keys() != {"account", "models"} \
            or not isinstance(entries, list) or len(entries) > LEDGER["maxEntries"]:
        raise ValueError("invalid ledger entries or latches")
    account, models = latches["account"], latches["models"]
    if not (_fallback_valid(fallback) and (account is None or _code(account, ACCOUNT_CODES))
            and isinstance(models, dict) and len(models) <= TEXT_LIMIT
            and all(_text(m, TEXT_LIMIT) and _code(code, MODEL_CODES) for m, code in models.items())
            and all(_entry_valid(entry) for entry in entries)):
        raise ValueError("invalid ledger fields")
    return {"schema_version": LEDGER["schemaVersion"],
            "entries": [{key: entry[key] for key in ENTRY_FIELDS} for entry in entries],
            "latches": {"account": account, "models": dict(models)},
            "last_fallback": {key: fallback[key] for key in ("at", "code", "model")} if fallback else None}


def _unique_object(items):
    obj = dict(items)
    if len(obj) != len(items):
        raise ValueError("duplicate ledger field")
    return obj


def read_bounded(path):
    """Read a bounded regular ledger; a missing file is distinct from an untrusted one."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular ledger file")
        data = handle.read(FILE_MAX_BYTES + 1)
    if len(data) > FILE_MAX_BYTES:
        raise ValueError("the ledger file is larger than its bound")
    return json.loads(data, object_pairs_hook=_unique_object)


def durable_write(path, record):
    """File bytes and the directory entry both fsynced (``lib.reports.atomic_json`` with its directory option)."""
    atomic_json(path, record, prefix=".api-credits-", indent=None, sort_keys=True, fsync=True, fsync_dir=True)


class CreditLedger:
    def __init__(self, path, *, now=lambda: datetime.now(timezone.utc), write=durable_write):
        self.path, self.now, self._write = path, now, write
        self._lock = threading.Lock()
        self._record = _empty()
        self.available = True  # False when the file exists but cannot be trusted

    # --- start-up --------------------------------------------------------------------------------
    def start(self):
        """Load the file; settle every entry a crash left open as ``unknown``; clear every latch
        (they live until a restart); delete entries after ``until`` + the retention days."""
        with self._lock:
            try:
                try:
                    loaded = read_bounded(self.path)
                except FileNotFoundError:
                    loaded = _empty()
                record = _project(loaded)
                now = self.now()
                for entry in record["entries"]:
                    if entry["state"] == OPEN:
                        entry.update(state=SETTLED, unknown=True, settled_at=_stamp(now))
                record["latches"] = {"account": None, "models": {}}
                if now >= UNTIL + timedelta(days=LEDGER["retainDaysAfterUntil"]):
                    record["entries"] = []
                self.available = True
                self._commit(record, loaded)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                self.available = False  # untrusted input never prevents subscription research

    def _commit(self, record, previous):
        if record == previous and self.path.exists():
            self._record = record
            return
        try:
            self._write(self.path, record)
        except OSError:
            self.available = False
            return
        self._record = record

    # --- reservation and settlement --------------------------------------------------------------
    @property
    def full(self):
        with self._lock:
            return len(self._record["entries"]) >= LEDGER["maxEntries"]

    def reserve(self, attempt_id, model):
        """Durably open an entry before a credit process spawns, or raise ``LedgerError``."""
        with self._lock:
            if not self.available:
                raise LedgerError("ledger_unavailable")
            if len(self._record["entries"]) >= LEDGER["maxEntries"]:
                raise LedgerError("ledger_full")
            record = copy.deepcopy(self._record)
            record["entries"].append({"attempt_id": attempt_id, "model": model, "started_at": _stamp(self.now()),
                                      "settled_at": None, "known_usd": 0.0, "unknown": False, "state": OPEN})
            self._persist(record)

    def withdraw(self, attempt_id):
        """Remove an entry whose process never spawned. A failed write only leaves a harmless open
        entry, which the next start settles as unknown."""
        with self._lock:
            record = copy.deepcopy(self._record)
            record["entries"] = [e for e in record["entries"] if e.get("attempt_id") != attempt_id]
            try:
                self._persist(record)
            except LedgerError:
                pass

    def settle(self, attempt_id, known_usd, unknown):
        """Close an entry. On a write failure the entry stays open here and in the file."""
        with self._lock:
            record = copy.deepcopy(self._record)
            for entry in record["entries"]:
                if entry.get("attempt_id") == attempt_id and entry.get("state") == OPEN:
                    entry.update(state=SETTLED, settled_at=_stamp(self.now()), known_usd=known_usd, unknown=unknown)
                    break
            else:
                return
            self._persist(record)

    def _persist(self, record):
        try:
            self._write(self.path, record)
        except OSError:
            raise LedgerError("ledger_unavailable") from None
        self._record = record

    # --- latches ---------------------------------------------------------------------------------
    def latch(self, code, model):
        """Record a credit failure and the fallback. The latch applies at once even if the write fails."""
        with self._lock:
            record = copy.deepcopy(self._record)
            if latch_scope(code) == "account":
                record["latches"]["account"] = code
            else:
                record["latches"]["models"][model] = code
            record["last_fallback"] = {"at": _stamp(self.now()), "code": code, "model": model}
            try:
                self._write(self.path, record)
            except OSError:
                pass
            self._record = record

    def latches(self):
        with self._lock:
            latches = self._record["latches"]
            return {"account": latches["account"], "models": dict(latches["models"])}

    def snapshot(self):
        """The ledger part of ``GET /credits``: entries, latches, last fallback, whether it is full."""
        with self._lock:
            record = self._record
            return {"entries": [{key: entry[key] for key in ENTRY_FIELDS} for entry in record["entries"]],
                    "latches": {"account": record["latches"]["account"], "models": dict(record["latches"]["models"])},
                    "last_fallback": copy.deepcopy(record["last_fallback"]),
                    "ledger_full": len(record["entries"]) >= LEDGER["maxEntries"]}

