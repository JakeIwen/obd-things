"""Keep the saved-DTC response inside the broker transport limit.

The parked result is checked on its own where it is built (``insights.py``).
The optional blocks beside it are shed here, newest first: the drive reads,
then the van's in-vehicle scan. Each is first replaced by a small unavailable
marker and omitted entirely when even that marker does not fit, so the final
encoded size is checked after every change.
"""
from __future__ import annotations

import json

OPTIONAL_BLOCKS = (
    ("drive_reads", {"available": False, "reason": "drive_reads_too_large"}),
    ("in_vehicle_scan", {
        "available": False,
        "reason": "in_vehicle_scan_too_large",
        "detail": "in-vehicle scan results exceed the broker transport limit",
    }),
)


def encoded_size(payload: dict) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def fit_response(payload: dict, limit: int) -> dict:
    """Shed optional blocks until the response encodes below ``limit``; mutates and returns it."""
    for key, marker in OPTIONAL_BLOCKS:
        if key not in payload or encoded_size(payload) < limit:
            continue
        payload[key] = dict(marker)
        if encoded_size(payload) >= limit:
            del payload[key]
    return payload
