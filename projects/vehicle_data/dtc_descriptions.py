"""Reviewed, module-scoped descriptions for the cache-only DTC dashboard.

The DTC's reporting ECU is part of the key because FCA DTC namespaces are not
globally interchangeable.  Keep these as short factual meanings, not repair
advice or diagnoses.  The source labels deliberately describe the evidence
tier without exposing the private local service-manual path in the API.
"""

from __future__ import annotations

import re


OEM_EXACT_VEHICLE = "oem_exact_vehicle"
OEM_BASE_STANDARD_SUBTYPE = "oem_base_plus_standard_subtype"
REPOSITORY_VERIFIED_VEHICLE = "repository_verified_vehicle"
STANDARD_SUBTYPE_ONLY = "standard_failure_subtype_only"

_DTC_PATTERN = re.compile(r"^[PBCU][0-9A-F]{4}-([0-9A-F]{2})$")

# ISO 15031/SAE J2012-style failure-type byte meanings used by FCA's display
# suffix.  This is intentionally bounded to subtypes observed in this project.
_FAILURE_SUBTYPES = {
    "00": "No subtype information",
    "08": "Bus signal/message failure",
    "11": "Circuit short to ground",
    "12": "Circuit short to battery",
    "13": "Circuit open",
    "15": "Circuit short to battery or open",
    "23": "Signal stuck low",
    "24": "Signal stuck high",
    "25": "Signal shape/waveform failure",
    "2B": "Wires shorted together",
    "31": "No signal",
    "45": "Program memory failure",
    "49": "Internal electronic failure",
    "55": "Not configured",
    "64": "Signal plausibility failure",
    "66": "Signal has too many transitions/events",
    "68": "Event information",
    "86": "Signal invalid",
    "87": "Missing message",
}


def _entries(
    source: str,
    values: dict[tuple[str, str], str],
) -> dict[tuple[str, str], tuple[str, str]]:
    return {key: (description, source) for key, description in values.items()}


_DESCRIPTIONS: dict[tuple[str, str], tuple[str, str]] = {
    **_entries(
        OEM_EXACT_VEHICLE,
        {
            ("bcm_ccan", "B104E-15"): (
                "Right daytime running light (DRL) circuit — Short to battery or open"
            ),
            ("bcm_ccan", "B10AA-00"): (
                "ECU configuration mismatch (PROXI) — No subtype information"
            ),
            ("bcm_ccan", "B162A-15"): (
                "Left low-beam circuit — Short to battery or open"
            ),
            ("bcm_ccan", "B162E-15"): (
                "Right low-beam circuit — Short to battery or open"
            ),
            ("bcm_ccan", "B1632-15"): (
                "Left high-beam circuit — Short to battery or open"
            ),
            ("bcm_ccan", "B2204-55"): "ECU error (PROXI) — Not configured",
            ("bcm_ccan", "B2204-87"): "ECU error (PROXI) — Missing message",
            ("bcm_ccan", "U0011-00"): "CAN BH bus-off performance",
            ("bcm_ccan", "U1767-87"): (
                "Entertainment Multimedia Control Module (EMCM) — Missing message"
            ),
            ("cluster", "U0010-00"): "CAN BH bus mute",
            ("cluster", "U0011-00"): "CAN BH bus-off performance",
            ("cluster", "U0080-08"): "Ethernet bus — Bus signal/message failure",
            ("cluster", "U1700-86"): (
                "Implausible data received from Body Control Module (BCM)"
            ),
            ("cluster", "U1733-86"): (
                "Left Blind Spot Sensor (LBSS) — Signal invalid"
            ),
            ("cluster", "U1733-87"): (
                "Left Blind Spot Sensor (LBSS) — Missing message"
            ),
            ("cluster", "U1734-86"): (
                "Right Blind Spot Sensor (RBSS) — Signal invalid"
            ),
            ("cluster", "U1734-87"): (
                "Right Blind Spot Sensor (RBSS) — Missing message"
            ),
            ("cluster", "U173F-86"): "Trailer Tow Module (TTM) — Signal invalid",
            ("cluster", "U173F-87"): "Trailer Tow Module (TTM) — Missing message",
            ("emcm2_bcan", "U1930-00"): (
                "EMCM operational-mode-status internal signal unavailable — No subtype information"
            ),
            ("radar_acc", "C1420-25"): (
                "Radar sensor blinded (signal disturbed) — Waveform failure"
            ),
            ("radar_acc", "C1422-49"): (
                "Internal hardware plausibility check — Internal electronic failure"
            ),
            ("radar_acc", "C1422-66"): (
                "Internal hardware plausibility check — Too many transitions/events"
            ),
            ("radar_acc", "C1429-66"): (
                "Radar sensor high temperature — Too many transitions/events"
            ),
            ("tcm", "P1500-00"): "ECU configuration mismatch",
            ("telematics", "B1401-11"): (
                "Emergency-call speaker circuit — Short to ground"
            ),
            ("telematics", "B1401-12"): (
                "Emergency-call speaker circuit — Short to battery"
            ),
            ("telematics", "B1401-13"): "Emergency-call speaker circuit — Open",
            ("telematics", "B1401-2B"): (
                "Emergency-call speaker wires — Shorted together"
            ),
            ("telematics", "B143A-11"): "Microphone 1 circuit — Short to ground",
            ("telematics", "B143A-12"): "Microphone 1 circuit — Short to battery",
            ("telematics", "B143A-13"): "Microphone 1 circuit — Open",
            ("telematics", "B143A-2B"): "Microphone 1 wires — Shorted together",
            ("telematics", "B273C-23"): "Digital crash input — Signal stuck low",
            ("telematics", "U0011-00"): "CAN BH bus-off performance",
            ("telematics", "U0100-00"): (
                "Engine Control Module / Powertrain Control Module — Missing message"
            ),
            ("telematics", "U0129-00"): (
                "Lost communication with Brake System Control Module"
            ),
            ("telematics", "U0140-00"): (
                "Lost communication with Body Control Module"
            ),
            ("telematics", "U0151-00"): (
                "Lost communication with instrument cluster (CCN) — No subtype information"
            ),
            ("telematics", "U0155-00"): (
                "Lost communication with instrument cluster (CCN)"
            ),
            ("telematics", "U0184-00"): "Lost communication with radio",
            ("telematics", "U1932-86"): (
                "Ignition working-condition signal from BCM — Signal invalid"
            ),
            ("telematics", "U1933-87"): (
                "BCM command message — Missing message"
            ),
            ("telematics", "U1934-87"): (
                "BCM command/body-2 message — Missing message"
            ),
        },
    ),
    **_entries(
        OEM_BASE_STANDARD_SUBTYPE,
        {
            ("bcm_ccan", "B104D-15"): (
                "Left daytime running light (DRL) circuit — Short to battery or open"
            ),
            ("cluster", "U1741-87"): (
                "Radio Frequency Hub Module (RFHM) — Missing message"
            ),
            ("radar_acc", "C1420-66"): (
                "Radar sensor blinded (signal disturbed) — Too many transitions/events"
            ),
            ("radar_acc", "C1429-68"): (
                "Radar sensor high temperature — Event information"
            ),
            ("telematics", "B273C-24"): "Digital crash input — Signal stuck high",
            ("telematics", "B273C-87"): "Digital crash input — Missing message",
            ("telematics", "U1930-86"): (
                "Internal ignition working-condition signal — Signal invalid"
            ),
        },
    ),
    **_entries(
        REPOSITORY_VERIFIED_VEHICLE,
        {
            ("rf_hub", "B1040-64"): (
                "Operational Mode Status info 1 — Signal plausibility failure"
            ),
            ("rf_hub", "C1503-31"): (
                "Tire pressure sensor (rear left) — No signal"
            ),
        },
    ),
}


def describe_dtc(module_key: object, fca_display: object) -> dict[str, object]:
    """Return a reviewed meaning or an explicit standardized-only fallback."""

    module = str(module_key or "").strip()
    display = str(fca_display or "").strip().upper()
    reviewed = _DESCRIPTIONS.get((module, display))
    if reviewed is not None:
        description, source = reviewed
        return {
            "description": description,
            "description_source": source,
            "description_reviewed": True,
        }

    match = _DTC_PATTERN.fullmatch(display)
    subtype = _FAILURE_SUBTYPES.get(match.group(1)) if match else None
    if subtype:
        return {
            "description": (
                "No reviewed module-specific FCA component meaning; "
                f"failure subtype: {subtype}"
            ),
            "description_source": STANDARD_SUBTYPE_ONLY,
            "description_reviewed": False,
        }
    return {
        "description": "No reviewed module-specific FCA meaning available",
        "description_source": STANDARD_SUBTYPE_ONLY,
        "description_reviewed": False,
    }


__all__ = (
    "OEM_BASE_STANDARD_SUBTYPE",
    "OEM_EXACT_VEHICLE",
    "REPOSITORY_VERIFIED_VEHICLE",
    "STANDARD_SUBTYPE_ONLY",
    "describe_dtc",
)
