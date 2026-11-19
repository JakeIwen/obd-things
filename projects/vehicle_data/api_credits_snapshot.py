"""The Mac's promotional-credit snapshot, as the advisor reads it.

The Mac pushes ``claude-promo-credits.json`` to the Pi (``api_credits.json`` ``paths.snapshotFile``).
``validate_snapshot`` is a disclosed port of ``validate()`` in ``~/config/shared/promo-api/credits.py``
(the producer; its tests are ``test_credits.py`` there). Differences, each on purpose: the record's
age is not a rejection (the view flags a stale snapshot instead of hiding it), the "expires today"
test runs at ``fetched_at`` (a snapshot is not wrong because its grant has since expired), up to
``FUTURE_SKEW_SECONDS`` of Mac/Pi clock skew is tolerated, and each grant label must be one of the
contract's verified grants (a label that is not in the table rejects the whole snapshot, since a
yearless label cannot be dated otherwise).

A version-1 error record means the lookup failed on the Mac (a zero balance looks the same), so the
balance is "unknown" while it is the file's content. A missing, oversized or invalid file keeps the
last valid snapshot, which is also written to ``snapshotCache`` so an advisor restart does not
forget it; a cache write failure only loses the cache.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import stat
import threading

from projects.vehicle_data.api_credits_contract import GRANTS, SNAPSHOT
from projects.vehicle_data.dtc_research_log import log
from projects.vehicle_data.engine_off_voltage import _atomic_json

VERSION = 2
ERROR_VERSION = 1
FIELDS = {"version", "fetched_at", "promo_balance_usd", "grants", "partial", "display"}
GRANT_FIELDS = {"balance_usd", "expiration"}
PARTIAL_NOTE = " · purchased credit present; other grants not counted"
FUTURE_SKEW_SECONDS = 300
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_EXPIRES = dict(GRANTS)  # label -> expiry instant, the verified table


@dataclass(frozen=True)
class GrantBalance:
    label: str
    expires: datetime
    balance: Decimal


@dataclass(frozen=True)
class Snapshot:
    fetched_at: datetime
    grants: tuple  # GrantBalance, earliest expiry first
    raw: dict  # the validated record, for the cache file


UNKNOWN = "unknown"  # ``SnapshotStore.current`` answer for "the Mac's lookup failed"


def _display(total, grants, partial):
    if len(grants) == 1:
        text = f"${total:,.2f} left · Expires {grants[0]['expiration']} (UTC)"
    else:
        text = f"${total:,.2f} left in {len(grants)} grants: " + ", ".join(
            f"${Decimal(g['balance_usd']):,.2f} expires {g['expiration']} (UTC)" for g in grants)
    return text + (PARTIAL_NOTE if partial else "")


def _expiry_usable(label, at):
    """False once the grant has expired or expires on ``at``'s UTC day (the producer's rule)."""
    day = datetime.strptime("2000 " + label, "%Y %b %d")
    return (day.month, day.day) != (at.month, at.day)


def validate_snapshot(record, now):
    """The ``Snapshot`` for a sanitized v2 record, or ``ValueError`` with a fixed message."""
    if not isinstance(record, dict) or set(record) != FIELDS or type(record["version"]) is not int \
            or record["version"] != VERSION:
        raise ValueError("missing or unavailable snapshot")
    grants = record["grants"]
    try:
        fetched = datetime.fromisoformat(record["fetched_at"].replace("Z", "+00:00"))
        total = Decimal(record["promo_balance_usd"])
        well_formed = type(record["partial"]) is bool and isinstance(grants, list) and bool(grants) \
            and len(grants) <= SNAPSHOT["maxGrants"] \
            and all(isinstance(g, dict) and set(g) == GRANT_FIELDS for g in grants)
        if not well_formed:
            raise ValueError
        balances = [Decimal(g["balance_usd"]) for g in grants]
        labels = [g["expiration"] for g in grants]
        if any(label not in _EXPIRES for label in labels) or len(set(labels)) != len(labels):
            raise ValueError
        if not fetched.tzinfo:
            raise ValueError
        usable = [_expiry_usable(label, fetched) for label in labels]
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        raise ValueError("invalid snapshot") from None
    if (fetched - now).total_seconds() > FUTURE_SKEW_SECONDS:
        raise ValueError("snapshot has an invalid timestamp")
    if any(not b.is_finite() or b <= 0 for b in balances) or not total.is_finite() or total != sum(balances):
        raise ValueError("no positive promotional credit balance")
    if not all(usable):
        raise ValueError("a promotional grant had expired when the snapshot was taken")
    if record["display"] != _display(total, grants, record["partial"]):
        raise ValueError("invalid snapshot display")
    ordered = sorted((GrantBalance(label, _EXPIRES[label], balance) for label, balance in zip(labels, balances)),
                     key=lambda grant: grant.expires)
    return Snapshot(fetched.astimezone(timezone.utc), tuple(ordered), record)


def is_error_record(record):
    """The Mac's failed-lookup record: version 1, no timestamp, an error string."""
    return isinstance(record, dict) and record.get("version") == ERROR_VERSION and record.get("fetched_at") is None \
        and isinstance(record.get("error"), str)


def _read_bounded_json(path):
    """The parsed file, or ``None`` when it is absent, oversized (read one byte past the bound), a
    symlink, not a regular file (a FIFO would block a read) or not JSON."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, SNAPSHOT["maxBytes"] + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > SNAPSHOT["maxBytes"]:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class SnapshotStore:
    """The advisor's view of the snapshot file, with the last valid one kept across gaps."""

    def __init__(self, snapshot_file, cache_file, clock=lambda: datetime.now(timezone.utc)):
        self.snapshot_file, self.cache_file, self.clock = Path(snapshot_file), Path(cache_file), clock
        self.last = None
        self.cache_loaded = False
        self.lock = threading.Lock()

    def current(self):
        """A ``Snapshot``, ``UNKNOWN`` (the Mac's lookup failed) or ``None`` (nothing usable yet)."""
        with self.lock:
            now = self.clock()
            record = _read_bounded_json(self.snapshot_file)
            if is_error_record(record):
                return UNKNOWN
            try:
                fresh = validate_snapshot(record, now)
            except ValueError:
                fresh = None
            if fresh is not None:
                if self.last is None or fresh.fetched_at != self.last.fetched_at:
                    self.last = fresh
                    self._write_cache(fresh)
                return fresh
            if self.last is None and not self.cache_loaded:
                self.cache_loaded = True
                self.last = self._read_cache(now)
            return self.last

    def _write_cache(self, snapshot):
        try:
            _atomic_json(self.cache_file, snapshot.raw)
        except OSError:
            log("api_credits_cache_write_failed")

    def _read_cache(self, now):
        try:
            return validate_snapshot(_read_bounded_json(self.cache_file), now)
        except ValueError:
            return None
