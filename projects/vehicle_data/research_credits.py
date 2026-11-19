"""The research worker's credit state: policy flag, key file, ledger and the per-launch plan.

One ``Credits`` object belongs to the worker and is shared with its runner. ``plan`` makes the
billing choice for an admitted launch and, for a credit launch, durably reserves its ledger entry
before anything spawns; the runner then settles or withdraws that entry. Pure rules are in
``api_credits``; the durable file is ``api_credits_ledger``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from projects.vehicle_data.api_credits import Billing, choose_billing, read_key_file
from projects.vehicle_data.api_credits_contract import API_CREDITS, CONTRACT, SUBSCRIPTION
from projects.vehicle_data.api_credits_ledger import CreditLedger, LedgerError


@dataclass
class Plan:
    """One launch's billing. ``key`` exists only for a credit launch and never appears in a repr."""
    billing: Billing
    attempt_id: str = ""
    key: str | None = field(default=None, repr=False)

    @property
    def credit(self):
        return self.billing.choice == API_CREDITS


SUBSCRIPTION_PLAN = Plan(Billing(SUBSCRIPTION, "ineligible"))


class Credits:
    def __init__(self, key_path, ledger_path, policy, *, now=lambda: datetime.now(timezone.utc), ledger=None):
        self.key_path, self.policy, self.now = key_path, policy, now
        self.ledger = ledger or CreditLedger(ledger_path, now=now)

    def enabled(self):
        return (self.policy["claude"].get("api_credits") or {}).get("enabled") is True

    def start(self, volume):
        """At worker start: recover the ledger, and drop every API-created session when credits are
        disabled (a rolled-back worker must never find a transcript it cannot resume on its own billing)."""
        self.ledger.start()
        if not self.enabled():
            volume.drop_sessions_billed(API_CREDITS)

    def plan(self, request, session_record, *, credits_allowed, attempt_id):
        """The launch's ``Plan``. A credit plan has its ledger entry open; a refusal choice
        (``replay_required``, ``session_missing``) opens nothing."""
        key, _ = read_key_file(self.key_path)

        def decide(ledger_reason):
            return choose_billing(request, session_record, self.now(), credits_allowed=credits_allowed,
                                  enabled=self.enabled(), key_installed=key is not None,
                                  latches=self.ledger.latches(), ledger_reason=ledger_reason)

        billing = decide("ledger_full" if self.ledger.full else None if self.ledger.available else "ledger_unavailable")
        if billing.choice == API_CREDITS:
            try:
                self.ledger.reserve(attempt_id, request.model)
            except LedgerError as failed:
                billing = decide(failed.reason)
        return Plan(billing, attempt_id, key if billing.choice == API_CREDITS else None)

    def withdraw(self, plan):
        """The launch never spawned: its entry goes away."""
        if plan.credit:
            self.ledger.withdraw(plan.attempt_id)

    def settle(self, plan, tally, *, clean_exit):
        """Close the entry from the process's own result deltas. Raises ``LedgerError`` when the
        write failed (the entry then settles as unknown at the next start)."""
        known, unknown = tally.settled(clean_exit=clean_exit)
        self.ledger.settle(plan.attempt_id, known, unknown)

    def latch(self, code, model):
        self.ledger.latch(code, model)

    def view(self):
        key, _ = read_key_file(self.key_path)
        return {"key_installed": key is not None, "enabled": self.enabled(), "until": CONTRACT["until"],
                "ledger_available": self.ledger.available, **self.ledger.snapshot()}
