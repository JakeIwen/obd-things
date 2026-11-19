"""The promotional-credit view the dashboard card reads: the worker's ledger against the Mac's snapshot.

Pure: ``build_view(credits, snapshot, now)`` takes the worker's ``/credits`` reply (data, never
trusted for shape), a ``Snapshot`` (or ``UNKNOWN`` / ``None``) from ``api_credits_snapshot`` and the
current instant, and returns the reply of ``GET /v1/assistant/research/credits``. Only whitelisted,
re-typed fields are copied, so nothing else the worker sends (it never sends the key) can reach a
browser. The balance is display only; the API's own refusal is the billing guard.

Spend model, per ``docs/dtc-research.md`` "Promotional API credits":
- an entry settled at or before the snapshot's ``fetched_at`` is already reflected in it;
- an entry that started after ``fetched_at`` draws on the earliest-expiring grant live at its
  ``started_at`` (spilling to the next live grant when one runs out);
- an entry that straddles the fetch (``started_at <= fetched_at`` and not settled before it) or is
  still open, even one that started after the fetch, is subtracted in full and counted as uncertain;
- a worker whose reply lacks ``ledger_available: true`` has no spend accounting: the balance is unknown
  and new research bills the subscription (``ledger_unavailable``);
- a grant's remainder is lost when it expires, so only live grants are listed and totalled.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from projects.vehicle_data.api_credits_contract import (
    API_CREDITS, BALANCE_STATES, CREDIT_CODES, SNAPSHOT, SUBSCRIPTION, UNTIL,
)
from projects.vehicle_data.api_credits_snapshot import UNKNOWN

CENT = Decimal("0.0001")


def _instant(value):
    """A timezone-aware instant from an ISO string, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _amount(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _ledger_available(credits):
    return credits.get("ledger_available") is True


def new_research(credits, expired):
    """What a new research run bills by default, and which models are latched to the subscription."""
    latches = credits.get("latches") if isinstance(credits.get("latches"), dict) else {}
    models = latches.get("models") if isinstance(latches.get("models"), dict) else {}
    latched = sorted(str(model)[:120] for model, code in models.items()
                     if code in CREDIT_CODES and "sk-ant-" not in str(model))  # never echo anything key-shaped
    account = latches.get("account") in CREDIT_CODES
    # The worker's own order (api_credits.choose_billing): disabled, no_key, expired, latch, ledger.
    reason = next((name for name, hit in (
        ("disabled", credits.get("enabled") is not True), ("no_key", credits.get("key_installed") is not True),
        ("expired", expired), ("unavailable", account), ("ledger_unavailable", not _ledger_available(credits)),
        ("ledger_full", credits.get("ledger_full") is True)) if hit), None)
    return {"billing": SUBSCRIPTION if reason else API_CREDITS, "reason": reason, "latched_models": latched}


def _entries(credits):
    """``(started, settled|None, known_usd, unknown)`` for each well-formed ledger entry, and how many
    entries could not be read (their spend is unknown)."""
    rows, unreadable = [], 0
    for entry in credits.get("entries") if isinstance(credits.get("entries"), list) else []:
        started = _instant(entry.get("started_at")) if isinstance(entry, dict) else None
        settled = _instant(entry.get("settled_at")) if isinstance(entry, dict) else None
        known = _amount(entry.get("known_usd")) if isinstance(entry, dict) else None
        if started is None or known is None:
            unreadable += 1
            continue
        rows.append((started, settled, known, entry.get("unknown") is True or entry.get("state") == "open"))
    return rows, unreadable


def _draw(remaining, grants, amount, at):
    """Subtract ``amount`` from the earliest-expiring grants live at ``at`` (spilling on, floor 0)."""
    for grant in grants:
        if amount <= 0:
            return
        if grant.expires <= at:
            continue
        taken = min(remaining[grant.label], amount)
        remaining[grant.label] -= taken
        amount -= taken


def _balance(credits, snapshot, now):
    """``(balance, spent_since, uncertain_count, unknown_count)``."""
    if snapshot is None or snapshot == UNKNOWN or not _ledger_available(credits):
        return ({"state": BALANCE_STATES["unknown"], "total_usd": None, "as_of": None, "stale": False,
                 "grants": []}, 0, 0, 0)
    fetched = snapshot.fetched_at
    remaining = {grant.label: grant.balance for grant in snapshot.grants}
    spent, uncertain, unknown = Decimal(0), 0, 0
    rows, unreadable = _entries(credits)
    for started, settled, known, spend_unknown in sorted(rows, key=lambda row: row[0]):
        if settled is not None and settled <= fetched:
            continue  # reflected in the snapshot
        if started <= fetched or settled is None:
            uncertain += 1  # straddles the fetch, or is still open (whenever it started)
        spent += known
        unknown += 1 if spend_unknown else 0
        _draw(remaining, snapshot.grants, known, max(started, fetched))
    live = [grant for grant in snapshot.grants if grant.expires > now]
    grants = [{"label": grant.label, "expires": _iso(grant.expires),
               "remaining_usd": float(remaining[grant.label].quantize(CENT))} for grant in live]
    total = sum((remaining[grant.label] for grant in live), Decimal(0))
    balance = {"state": BALANCE_STATES["known"], "total_usd": float(total.quantize(CENT)), "as_of": _iso(fetched),
               "stale": (now - fetched).total_seconds() > SNAPSHOT["freshSeconds"], "grants": grants}
    return balance, float(spent.quantize(CENT)), uncertain, unknown + unreadable


def _last_fallback(value):
    if not isinstance(value, dict) or value.get("code") not in CREDIT_CODES:
        return None
    at, model = _instant(value.get("at")), value.get("model")
    if at is None or not isinstance(model, str) or "sk-ant-" in model:  # never echo anything key-shaped
        return None
    return {"at": _iso(at), "code": value["code"], "model": model[:120]}


def build_view(credits, snapshot, now):
    """The credits route's reply for the worker's ``/credits`` reply and the Mac's snapshot."""
    until = _instant(credits.get("until")) or UNTIL
    expired = now >= until
    balance, spent, uncertain, unknown = _balance(credits, snapshot, now)
    return {"key_installed": credits.get("key_installed") is True, "enabled": credits.get("enabled") is True,
            "ledger_available": _ledger_available(credits), "until": _iso(until), "expired": expired, "new_research": new_research(credits, expired),
            "balance": balance, "spent_since_snapshot_usd": spent, "uncertain_count": uncertain,
            "unknown_spend_count": unknown, "last_fallback": _last_fallback(credits.get("last_fallback"))}
