"""Validation of the passively harvested in-vehicle scan cache."""

import re

from lib.timeutil import nonnegative_int as _nonnegative_int
from projects.vehicle_data.dtc_cache_validation import (
    HEX_BYTE, DtcCacheValidationError, timestamp_or_none,
)

MAX_VAN_SCAN_CACHE_BYTES = 256 * 1024
VAN_SCAN_CACHE_SCHEMA_VERSION = 1
VAN_SCAN_CACHE_NAME = "van-scan-cache.json"
_HEX_TEXT = re.compile(r"(?:[0-9A-F]{2})+\Z")


def validate_van_scan_cache(payload: object) -> dict[str, object]:
    """Validate ``van_scan_harvest.py``'s cache of the van's own (source F1) health checks."""

    if not isinstance(payload, dict):
        raise DtcCacheValidationError("in-vehicle scan cache root is not a JSON object")
    if payload.get("schema_version") != VAN_SCAN_CACHE_SCHEMA_VERSION:
        raise DtcCacheValidationError("in-vehicle scan cache is not schema version 1")
    if payload.get("type") != "in_vehicle_scan_cache":
        raise DtcCacheValidationError("in-vehicle scan cache has the wrong type")
    if not isinstance(payload.get("provenance"), str):
        raise DtcCacheValidationError("in-vehicle scan cache has no provenance")
    if not _nonnegative_int(payload.get("scan_count")):
        raise DtcCacheValidationError("in-vehicle scan cache scan_count is invalid")
    last_scan = payload.get("last_scan")
    if last_scan is not None:
        if not isinstance(last_scan, dict):
            raise DtcCacheValidationError("in-vehicle scan cache last_scan must be an object")
        for field in ("started_at", "completed_at"):
            if not isinstance(last_scan.get(field), str) or not timestamp_or_none(last_scan[field]):
                raise DtcCacheValidationError(f"last_scan.{field} is not a timestamp")
        if not isinstance(last_scan.get("pi_quiet"), bool):
            raise DtcCacheValidationError("last_scan.pi_quiet must be a boolean")
    modules = payload.get("modules")
    if not isinstance(modules, list):
        raise DtcCacheValidationError("in-vehicle scan cache modules must be an array")
    seen: set[str] = set()
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            raise DtcCacheValidationError(f"modules[{index}] must be an object")
        key = module.get("module_key")
        if not isinstance(key, str) or not key or key in seen:
            raise DtcCacheValidationError(f"modules[{index}].module_key is invalid or repeated")
        seen.add(key)
        observed = module.get("observed_at")
        if not isinstance(observed, str) or not timestamp_or_none(observed):
            raise DtcCacheValidationError(f"modules[{index}].observed_at is not a timestamp")
        dtcs = module.get("dtcs")
        if not isinstance(dtcs, list):
            raise DtcCacheValidationError(f"modules[{index}].dtcs must be an array")
        for position, record in enumerate(dtcs):
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("raw_dtc"), str)
                or not _HEX_TEXT.fullmatch(record["raw_dtc"])
                or not isinstance(record.get("fca_display"), str)
                or not isinstance(record.get("status"), str)
                or HEX_BYTE.fullmatch(record["status"]) is None
                or not isinstance(record.get("display_group"), str)
            ):
                raise DtcCacheValidationError(
                    f"modules[{index}].dtcs[{position}] is not a valid code record"
                )
    return payload


