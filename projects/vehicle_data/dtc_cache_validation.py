"""Pieces shared by the saved parked (``dtc-cache.json``) and van (``van-scan-cache.json``) validators.

Both caches are read-only dashboard inputs written by different owners
(``lib/dtc.py`` and ``van_scan_harvest.py``); their validators live with their
readers (``insights.py`` and ``van_scan_cache.py``) and raise the same error.
"""

from datetime import datetime
import re

HEX_BYTE = re.compile(r"[0-9A-F]{2}\Z")


class DtcCacheValidationError(ValueError):
    """A saved DTC cache does not match its compact schema."""


def timestamp_or_none(value: object) -> bool:
    """True for None or a timezone-aware ISO timestamp (``Z`` accepted)."""
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None
