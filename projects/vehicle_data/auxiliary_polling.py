#!/usr/bin/env python3
"""Offline policy and decoders for prospective auxiliary B-CAN polling.

This module deliberately has no live CAN imports or execution mode.  It fixes
the only two current development candidates, their minimum cadence, response
echoes, units, provenance, and deployment status.  The ICS candidate passed
parked ignition-on no-session support validation without an owner-visible side
effect; its unresolved cluster relationship stays explicit below.  The
low-value Uconnect temperature candidate is deliberately omitted.

CAN-CH is passive-only for ordinary telemetry.  ABS, EPS, HALF, and ORC are
therefore outside this policy even if a caller constructs a similar object.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sys
from typing import Callable, Sequence


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.modules import MODULES


MINIMUM_TARGET_INTERVAL_SECONDS = 5.0
KM_TO_MILES = 0.621371192237334
PASSIVE_ONLY_BUSES = frozenset(("can-ch",))
PROHIBITED_MOVING_MODULES = frozenset(
    ("abs_canch", "eps_canch", "half_canch", "orc_canch")
)
SUPPORTED_TARGETS = {
    ("ics_bcan", 0x2001),
}
READINESS_BY_TARGET = {
    ("ics_bcan", 0x2001): "odometer_relationship_validation_required",
}
VERIFIED_SUPPORT = "ignition_on_no_session_verified"
OWNER_VISIBLE_EFFECTS = "none_observed"


class AuxiliaryPollingPolicyError(ValueError):
    """A candidate violates the fixed auxiliary-polling policy."""


def _positive_data(response: bytes, did: int, *, length: int) -> bytes:
    if not isinstance(response, bytes):
        raise AuxiliaryPollingPolicyError("response must be immutable bytes")
    echo = bytes((0x62, did >> 8, did & 0xFF))
    if len(response) != len(echo) + length or not response.startswith(echo):
        raise AuxiliaryPollingPolicyError(
            f"response must be exact positive echo 62 {did >> 8:02X} "
            f"{did & 0xFF:02X} with {length} data byte(s)"
        )
    return response[len(echo) :]


def decode_ics_odometer_miles(response: bytes) -> float:
    """Decode current-family ICS DID 2001 as u24be x 0.1 km, then miles."""
    data = _positive_data(response, 0x2001, length=3)
    tenths_km = int.from_bytes(data, "big")
    return tenths_km * 0.1 * KM_TO_MILES


@dataclass(frozen=True)
class AuxiliaryPollTarget:
    name: str
    module_key: str
    did: int
    metric: str
    unit: str
    minimum_interval_seconds: float
    quality: str
    readiness: str
    support: str
    owner_visible_effects: str
    enabled: bool
    decoder: Callable[[bytes], float]
    provenance: str

    def __post_init__(self) -> None:
        validate_target(self)

    @property
    def request(self) -> bytes:
        return bytes((0x22, self.did >> 8, self.did & 0xFF))

    def public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("decoder")
        payload["did"] = f"{self.did:04X}"
        payload["request"] = " ".join(f"{value:02X}" for value in self.request)
        return payload


def validate_target(target: AuxiliaryPollTarget) -> None:
    module = MODULES.get(target.module_key)
    if module is None:
        raise AuxiliaryPollingPolicyError("candidate module is not registered")
    if target.module_key in PROHIBITED_MOVING_MODULES or module.bus in PASSIVE_ONLY_BUSES:
        raise AuxiliaryPollingPolicyError(
            "CAN-CH safety modules are passive-only for ordinary telemetry"
        )
    if module.bus != "b-can":
        raise AuxiliaryPollingPolicyError("auxiliary active candidates must use B-CAN")
    if (target.module_key, target.did) not in SUPPORTED_TARGETS:
        raise AuxiliaryPollingPolicyError(
            "candidate is outside the fixed ICS DID allowlist"
        )
    if (
        isinstance(target.minimum_interval_seconds, bool)
        or not isinstance(target.minimum_interval_seconds, (int, float))
        or not math.isfinite(float(target.minimum_interval_seconds))
        or target.minimum_interval_seconds < MINIMUM_TARGET_INTERVAL_SECONDS
    ):
        raise AuxiliaryPollingPolicyError(
            f"candidate interval must be at least {MINIMUM_TARGET_INTERVAL_SECONDS:g}s"
        )
    expected_readiness = READINESS_BY_TARGET[(target.module_key, target.did)]
    if target.quality != "candidate" or target.readiness != expected_readiness:
        raise AuxiliaryPollingPolicyError(
            "candidate readiness must match its fixed post-parked evidence gate"
        )
    if (
        target.support != VERIFIED_SUPPORT
        or target.owner_visible_effects != OWNER_VISIBLE_EFFECTS
    ):
        raise AuxiliaryPollingPolicyError(
            "promoted candidates require the fixed parked support and owner observation"
        )
    if type(target.enabled) is not bool:
        raise AuxiliaryPollingPolicyError("candidate enabled state must be boolean")
    if not callable(target.decoder):
        raise AuxiliaryPollingPolicyError("candidate decoder must be callable")


TARGETS = (
    AuxiliaryPollTarget(
        name="ics_odometer",
        module_key="ics_bcan",
        did=0x2001,
        metric="vehicle.odometer",
        unit="mi",
        minimum_interval_seconds=5.0,
        quality="candidate",
        readiness="odometer_relationship_validation_required",
        support=VERIFIED_SUPPORT,
        owner_visible_effects=OWNER_VISIBLE_EFFECTS,
        enabled=True,
        decoder=decode_ics_odometer_miles,
        provenance=(
            "projects/ecu_mapping/findings/promaster_2022/"
            "2026-07-21_alfaobd_live_status_correlation.md; current-family "
            "u24be x 0.1 km association; asleep/wake support timed out; "
            "ignition-on no-session support returned 0D0FE8 on 2026-08-28; "
            "owner observed no visible side effect; simultaneous cluster "
            "display was 53203 mi versus decoded 53191.860 mi, so the ICS "
            "counter's offset/update relationship remains unresolved"
        ),
    ),
)


def plan() -> dict[str, object]:
    return {
        "mode": "offline_plan_only",
        "live_can": False,
        "active_bus": "b-can",
        "can_ch_policy": "passive_only",
        "session_control": False,
        "tester_present": False,
        "arbitrary_payload": False,
        "response_before_next_request": True,
        "failure_domain": "independent_b_can_owner",
        "targets": [target.public_dict() for target in TARGETS],
        "enabled_target_count": sum(target.enabled for target in TARGETS),
    }


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(json.dumps(plan(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
