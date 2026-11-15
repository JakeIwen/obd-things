"""Which vehicle adapters a starting broker found armed with no owner (pure).

A drive helper restores its adapter to listen-only when it is stopped and
records an inhibit itself if that fails. A helper killed inside that
restoration (a stop that overran TimeoutStopSec, or an OOM kill) records
nothing, and the passive reconciler deliberately leaves an UP, armed
interface as it finds it. So the broker checks its first full reconciliation
pass: a vehicle role reported ``armed`` (up, not listen-only) that nothing
accounts for gets a wildcard inhibit, and every helper refuses to start
until a person inspects the adapter and ends it.

Why "armed" means unowned: the reconciler reports it only while it holds
that role's exclusive logical-role and channel locks and finds no inhibit
for the channel. Every participating owner (this broker's helpers, a
reviewed wake, a foreign CAN tool, a shared observer) holds one of those
locks while it works, so it would have produced ``busy``, and an existing
inhibit ``inhibited``. The checks here repeat what the broker itself knows
at the moment it acts: its claimed helpers, any non-passive acquisition in
flight, and every same-boot inhibit.

Only the first full pass counts. That is where the gap is: an earlier
process's helper that died mid-restoration. While this broker runs, its own
helpers' unverified finals already latch the restoration inhibit, and a
foreign tool's crash is that tool's restoration to own; widening the check
would add a way to lose active telemetry mid-drive on evidence nobody has
audited.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

INHIBIT_PREFIX = "vehicle-data-armed-at-startup"


@dataclass(frozen=True)
class ArmedRole:
    """One vehicle role found armed with no owner."""

    role: str
    channel: str

    @property
    def inhibit_name(self) -> str:
        return f"{INHIBIT_PREFIX}-{self.role}"

    @property
    def inhibit_reason(self) -> str:
        return (
            f"broker startup found {self.role} ({self.channel}) up and not listen-only "
            "with no owner; inspect the adapter, restore listen-only, then end this inhibit"
        )


def unowned_armed_roles(
    reconcile: object,
    *,
    vehicle_roles: Iterable[str],
    owned_roles: frozenset[str],
    inhibits: Iterable[dict[str, object]],
    nonpassive_inflight: bool,
) -> tuple[ArmedRole, ...] | None:
    """Roles of one reconciliation result that need an armed-at-startup inhibit.

    ``reconcile`` is the reconciler's result (``roles`` maps each role to its
    outcome). ``owned_roles`` are roles this broker's helpers hold or are
    about to; ``inhibits`` every same-boot inhibit. Any inhibit on the
    channel or the wildcard already stops every helper, so none is added
    then. None means no conclusion yet: the result has no role outcomes (the
    pass did not complete), or a non-passive acquisition in flight may own an
    interface.
    """

    roles = reconcile.get("roles") if isinstance(reconcile, dict) else None
    if not isinstance(roles, dict) or nonpassive_inflight:
        return None
    inhibited = {item.get("channel") for item in inhibits if isinstance(item, dict)}
    found = []
    for role in vehicle_roles:
        outcome = roles.get(role)
        if not isinstance(outcome, dict) or outcome.get("state") != "armed":
            continue
        channel = outcome.get("channel")
        if role in owned_roles or not isinstance(channel, str) or not channel:
            continue
        if "*" in inhibited or channel in inhibited:
            continue
        found.append(ArmedRole(role, channel))
    return tuple(found)
