"""Vehicle-state availability from existing interface evidence, without probing.

The broker owns synchronization and supplies probe timestamps. The shared JSON
vocabulary is also consumed by the dashboard; unavailable never means stopped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path

from lib.vehicle_can_roles import B_CAN_ROLE, CAN_BUS_ROLES, CAN_CH_ROLE, C_CAN_ROLE
from projects.vehicle_data.api_schema import VehicleState


_CONTRACT = json.loads(Path(__file__).with_suffix(".json").read_text())
CAN_ADAPTER_MISSING = _CONTRACT["missing_basis"]
CAN_ADAPTER_RECOVERING = _CONTRACT["recovering_basis"]
CAN_ADAPTER_INITIALIZING = _CONTRACT["initializing_basis"]
CAN_UNAVAILABLE_ENGINE = "engine_unavailable"
CAN_UNAVAILABLE_BASES = (
    CAN_ADAPTER_MISSING, CAN_ADAPTER_RECOVERING, CAN_ADAPTER_INITIALIZING,
)
_ROLE_LABELS = {C_CAN_ROLE: "C-CAN", B_CAN_ROLE: "B-CAN", CAN_CH_ROLE: "CAN-CH"}


def vehicle_hardware_unavailable(vehicle: object) -> bool:
    basis = vehicle.get("basis") if isinstance(vehicle, Mapping) else None
    return isinstance(basis, str) and basis in CAN_UNAVAILABLE_BASES


@dataclass(frozen=True)
class PresenceEvidence:
    missing_roles: tuple[str, ...] = ()
    complete: bool = False
    discovery_failed: bool = False


def interface_presence(interface: Mapping) -> PresenceEvidence:
    """Read exact-role absence, not readiness, armed mode or old USB incidents."""
    role_snapshot = interface.get("role_interfaces")
    if not isinstance(role_snapshot, Mapping):
        return PresenceEvidence(
            missing_roles=CAN_BUS_ROLES if interface.get("adapter_present") is False else (),
            complete=interface.get("adapter_present") is True,
        )
    roles = role_snapshot.get("roles")
    if not isinstance(roles, Mapping):
        return PresenceEvidence()
    issues = role_snapshot.get("issues") or []
    missing, present = [], []
    unresolved_missing = False
    for name in CAN_BUS_ROLES:
        role = roles.get(name)
        if not isinstance(role, Mapping):
            continue
        actual = role.get("actual") or {}
        actual = actual if isinstance(actual, Mapping) else {}
        if actual.get("present") is False:
            missing.append(name)
        elif role.get("resolution") == "missing":
            # Failed sysfs/identity discovery also produces missing resolutions.
            # Those failures prove unavailable coverage, not physical absence.
            unresolved_missing = True
            if not issues:
                missing.append(name)
        elif role.get("resolution") == "resolved" and actual.get("present") is True:
            present.append(name)
    return PresenceEvidence(
        tuple(missing), len(present) == len(CAN_BUS_ROLES),
        bool(issues and unresolved_missing),
    )


def _unavailable_state(basis: str, detail: str, observed_at: str) -> VehicleState:
    return {
        "state": _CONTRACT["state"], "running": None,
        "confidence": _CONTRACT["confidence"], "basis": basis,
        "detail": detail, "observed_at": observed_at, "age_ms": 0,
    }


def _missing_label(roles: tuple[str, ...]) -> str:
    if roles == CAN_BUS_ROLES:
        return "CAN adapters missing"
    label = " / ".join(_ROLE_LABELS[role] for role in roles)
    return label + (" adapter missing" if len(roles) == 1 else " adapters missing")


class CanAvailability:
    """Latch an evidence gap until exact roles and subsequent evidence return.

    Callers hold the broker's existing lock. This latch never grants hardware
    ownership and never changes acquisition, configuration or transmission.
    """

    def __init__(self, started_monotonic: float = 0.0) -> None:
        self.started_monotonic = started_monotonic
        self._vehicle: VehicleState | None = None
        self._observed_monotonic = 0.0
        self._recovery_after: float | None = None
        self._recovery_confirmed = False
        self._requires_roles = False

    def observe(
        self, interface: Mapping, *, probe_ok: bool, now: float, observed_at: str,
    ) -> None:
        evidence = interface_presence(interface)
        self._requires_roles = self._requires_roles or "role_interfaces" in interface
        complete = evidence.complete and (
            not self._requires_roles or isinstance(interface.get("role_interfaces"), Mapping)
        )
        if evidence.missing_roles and probe_ok:
            self._vehicle = _unavailable_state(
                CAN_ADAPTER_MISSING,
                _missing_label(evidence.missing_roles)
                + "; vehicle sleep and running state cannot be determined",
                observed_at,
            )
        elif evidence.discovery_failed or (self._vehicle is not None and (
            not probe_ok or not complete
        )):
            self._vehicle = _unavailable_state(
                CAN_ADAPTER_RECOVERING,
                "CAN adapter status unavailable; exact vehicle roles have not been re-established",
                observed_at,
            )
        elif self._vehicle is not None and complete and probe_ok:
            if self._recovery_after is None:
                self._vehicle = _unavailable_state(
                    CAN_ADAPTER_RECOVERING,
                    "CAN adapters returning; awaiting fresh passive vehicle evidence",
                    observed_at,
                )
                self._observed_monotonic = now
                self._recovery_after = now
            else:
                self._recovery_confirmed = True
            return
        else:
            return
        self._observed_monotonic = now
        self._recovery_after = None
        self._recovery_confirmed = False

    def admit_observation(self, observed_monotonic: float | None) -> bool:
        if self._vehicle is None:
            return True
        if self._recovery_after is None:
            return False
        if observed_monotonic is None:
            # Undated silence cannot prove recovery in the same acquisition
            # whose finally-block first noticed the returned adapter.
            if not self._recovery_confirmed:
                return False
        elif observed_monotonic <= self._recovery_after:
            return False
        self._vehicle = None
        self._recovery_after = None
        self._recovery_confirmed = False
        return True

    def effective_vehicle_state(self, vehicle: dict, probe: dict) -> dict:
        override = self.vehicle_state(
            now=probe["elapsed_seconds"] + self.started_monotonic,
            elapsed_seconds=probe["elapsed_seconds"],
            startup_grace_seconds=probe["startup_grace_seconds"],
        )
        return override or vehicle

    def vehicle_state(
        self, *, now: float, elapsed_seconds: float, startup_grace_seconds: float = 30,
    ) -> VehicleState | None:
        if self._vehicle is None:
            return None
        vehicle = dict(self._vehicle)
        vehicle["age_ms"] = round(max(0, now - self._observed_monotonic) * 1000)
        if elapsed_seconds < startup_grace_seconds:
            vehicle.update(
                basis=CAN_ADAPTER_INITIALIZING,
                detail="Checking CAN adapters; initial interface discovery is still in progress",
            )
        return vehicle
