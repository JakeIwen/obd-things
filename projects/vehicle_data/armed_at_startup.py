"""Which vehicle adapters a starting broker found armed with no owner (pure).

A drive helper restores its adapter to listen-only when it is stopped and
records an inhibit itself if that fails. A helper killed inside that
restoration (a stop that overran TimeoutStopSec, or an OOM kill) records
nothing, and the passive reconciler deliberately leaves an UP, armed
interface as it finds it. So the broker assesses each vehicle role once at
startup: a role reconciliation reports ``armed`` (up, not listen-only) that
nothing accounts for gets a wildcard inhibit, and every helper refuses to
start until a person inspects the adapter and ends it.

What "armed" shows: the reconciler reports it only while it holds that
role's exclusive logical-role and channel locks and finds no inhibit for the
channel. Every participating owner traced (this broker's helpers, generic
active routes, a reviewed wake, the DTC batch, Vonstar) holds those locks
from arming through restoration, and a shared observer holds the channel
lock too, so at the moment of observation no participating lock holder
exists; an owner would have made the outcome ``busy``, an inhibit
``inhibited``. It does not prove the absence of manual or out-of-tree
activity, and the locks are released before the inhibit is written.

Each role counts once, at its first conclusive outcome: listen-only
(``ready``/``reconciled``, or a verified passive-ready entry of the
reconciler's read-only readiness snapshot), ``inhibited`` or ``armed``. Anything else
(``busy``, ``failed``, ``unavailable``, ``topology_changed``, missing, a
role this broker's helper has claimed, or any pass taken while a
non-passive acquisition is in flight) leaves it pending, so a transient
read failure never exempts it. Once a role is assessed it is not looked at
again: the gap is an earlier process's helper that died mid-restoration.
While this broker runs, its own helpers' unverified finals latch the
restoration inhibit, and a foreign tool's crash is that tool's restoration
to own; reassessing would add a mid-drive way to lose active telemetry on
evidence nobody has audited.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

INHIBIT_PREFIX = "vehicle-data-armed-at-startup"
# Outcomes that settle a role's startup assessment.
CONCLUSIVE_STATES = frozenset({"ready", "reconciled", "inhibited", "armed"})


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


@dataclass(frozen=True)
class StartupAssessment:
    """What one reconciliation result settles for the still-pending roles."""

    assessed: frozenset[str]
    armed: tuple[ArmedRole, ...]


NO_ASSESSMENT = StartupAssessment(frozenset(), ())


def _outcome(entry: object) -> tuple[object, object]:
    """``(state, channel)`` from either role-entry shape the reconciler returns.

    A reconciliation pass reports ``state``. The read-only readiness check
    (taken once no role needs reconciliation) reports the manager's role
    status instead: ``passive_ready``/``reason``, ``resolution`` and the
    ``actual`` link. It counts as listen-only only when all of those agree.
    """
    if not isinstance(entry, dict):
        return None, None
    channel = entry.get("channel")
    if "state" in entry:
        return entry.get("state"), channel
    actual = entry.get("actual")
    if (
        entry.get("passive_ready") is True
        and entry.get("reason") == "ready"
        and entry.get("resolution") == "resolved"
        and isinstance(actual, dict)
        and actual.get("listen_only") is True
        and isinstance(channel, str)
        and channel
    ):
        return "ready", channel
    return None, channel


def assess_startup_roles(
    reconcile: object,
    *,
    pending_roles: Iterable[str],
    owned_roles: frozenset[str],
    inhibits: Iterable[dict[str, object]],
    nonpassive_inflight: bool,
) -> StartupAssessment:
    """Settle each pending vehicle role this result observed conclusively.

    ``reconcile`` is the reconciler's result (``roles`` maps each role to its
    outcome). ``owned_roles`` are roles this broker's helpers hold or are
    about to; ``inhibits`` every same-boot inhibit. An armed role already
    covered by an inhibit on its channel or the wildcard is assessed but
    needs no new inhibit.
    """

    roles = reconcile.get("roles") if isinstance(reconcile, dict) else None
    if not isinstance(roles, dict) or nonpassive_inflight:
        return NO_ASSESSMENT
    inhibited = {item.get("channel") for item in inhibits if isinstance(item, dict)}
    assessed = []
    armed = []
    for role in pending_roles:
        if role in owned_roles:
            continue
        state, channel = _outcome(roles.get(role))
        if state not in CONCLUSIVE_STATES:
            continue
        if state == "armed":
            if not isinstance(channel, str) or not channel:
                continue  # unreadable: stays pending
            if "*" not in inhibited and channel not in inhibited:
                armed.append(ArmedRole(role, channel))
        assessed.append(role)
    return StartupAssessment(frozenset(assessed), tuple(armed))
