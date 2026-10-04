"""Snapshot value parsing and shared deterministic historian helpers."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Mapping, Sequence
from lib.timeutil import finite_number as _finite_number

from .models import (
    MICROSECONDS,
    REGIME_DIMENSIONS,
    SnapshotValidationError,
    _MetricDefinition,
    _MetricSample,
    _SourceDefinition,
)


def _is_placeholder_interface_role(value: object) -> bool:
    """Return whether a stored role is really an ephemeral/placeholder key."""

    if not isinstance(value, str):
        return False
    return value in ("default", "unknown") or (
        value.startswith("can") and value[3:].isdigit()
    )


def _utc_datetime(value: datetime | str, field: str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SnapshotValidationError(f"{field} is not an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SnapshotValidationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _to_us(value: datetime) -> int:
    return int(round(value.timestamp() * MICROSECONDS))


def _iso_from_us(value: int) -> str:
    return datetime.fromtimestamp(value / MICROSECONDS, timezone.utc).isoformat()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not _finite_number(value) or float(value) < 0:
        raise SnapshotValidationError(f"{field} must be finite and nonnegative")
    return int(round(float(value)))


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotValidationError(f"{field} must be a nonempty string")
    return value


def _bool_db(value: object) -> int | None:
    return int(value) if type(value) is bool else None


def _median_mad(values: Sequence[float]) -> tuple[float, float]:
    center = float(statistics.median(values))
    mad = float(statistics.median(abs(value - center) for value in values))
    return center, mad


def project_regime(
    regime: str,
    dimensions: Sequence[str] = REGIME_DIMENSIONS,
) -> str:
    """Return an explicit, rule-specific projection of one stored regime."""

    parts = regime.split(":")
    if len(parts) != len(REGIME_DIMENSIONS):
        raise ValueError(f"invalid stored regime {regime!r}")
    selected = tuple(dimensions)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("regime dimensions must be nonempty and unique")
    unknown = set(selected) - set(REGIME_DIMENSIONS)
    if unknown:
        raise ValueError(f"unknown regime dimensions: {', '.join(sorted(unknown))}")
    by_name = dict(zip(REGIME_DIMENSIONS, parts))
    return "|".join(f"{name}={by_name[name]}" for name in selected)


class ValidationMixin:
    """Snapshot value parsing and shared deterministic historian helpers."""

    @staticmethod
    def _parse_catalog(snapshot: Mapping[str, object]) -> dict[str, _MetricDefinition]:
        payload = snapshot.get("catalog")
        if not isinstance(payload, list):
            raise SnapshotValidationError("snapshot.catalog must be a list")
        definitions: dict[str, _MetricDefinition] = {}
        for index, item in enumerate(payload):
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"catalog[{index}] must be an object")
            name = _required_text(item.get("name"), f"catalog[{index}].name")
            if name in definitions:
                raise SnapshotValidationError(f"catalog repeats metric {name!r}")
            unit = _required_text(item.get("unit"), f"catalog[{index}].unit")
            value_type = _required_text(
                item.get("value_type"), f"catalog[{index}].value_type"
            )
            if value_type not in ("number", "integer", "boolean", "string"):
                raise SnapshotValidationError(
                    f"catalog metric {name!r} has unsupported value_type {value_type!r}"
                )
            stale_seconds = item.get("stale_after_seconds")
            if not _finite_number(stale_seconds) or float(stale_seconds) < 0:
                raise SnapshotValidationError(
                    f"catalog metric {name!r} has invalid stale_after_seconds"
                )
            source_items = item.get("sources")
            if not isinstance(source_items, list) or not source_items:
                raise SnapshotValidationError(f"catalog metric {name!r} has no sources")
            sources: dict[str, _SourceDefinition] = {}
            for source_index, source_item in enumerate(source_items):
                if not isinstance(source_item, Mapping):
                    raise SnapshotValidationError(
                        f"catalog source {name}[{source_index}] must be an object"
                    )
                source_name = _required_text(
                    source_item.get("name"), f"catalog source {name}.name"
                )
                if source_name in sources:
                    raise SnapshotValidationError(
                        f"catalog metric {name!r} repeats source {source_name!r}"
                    )
                sources[source_name] = _SourceDefinition(
                    name=source_name,
                    bus=_required_text(
                        source_item.get("bus"), f"catalog source {name}.bus"
                    ),
                    quality=_required_text(
                        source_item.get("quality"), f"catalog source {name}.quality"
                    ),
                    provenance=_required_text(
                        source_item.get("provenance"),
                        f"catalog source {name}.provenance",
                    ),
                )
            definitions[name] = _MetricDefinition(
                name=name,
                unit=unit,
                value_type=value_type,
                stale_after_ms=int(round(float(stale_seconds) * 1000)),
                sources=sources,
            )
        return definitions

    @staticmethod
    def _parse_metric(
        name: str,
        payload: Mapping[str, object],
        definition: _MetricDefinition,
    ) -> _MetricSample | tuple[str, str, str]:
        available = payload.get("available")
        if type(available) is not bool:
            raise SnapshotValidationError(f"metric {name!r} available must be boolean")
        if not available:
            reason = payload.get("reason", "source_unavailable")
            detail = payload.get("detail", "")
            if not isinstance(reason, str) or not reason:
                raise SnapshotValidationError(f"metric {name!r} reason must be text")
            if not isinstance(detail, str):
                raise SnapshotValidationError(f"metric {name!r} detail must be text")
            return ("missing", reason, detail)

        unit = _required_text(payload.get("unit"), f"metric {name}.unit")
        if unit != definition.unit:
            raise SnapshotValidationError(
                f"metric {name!r} unit {unit!r} does not match catalog {definition.unit!r}"
            )
        source_name = _required_text(payload.get("source"), f"metric {name}.source")
        source = definition.sources.get(source_name)
        if source is None:
            raise SnapshotValidationError(
                f"metric {name!r} source {source_name!r} is absent from its catalog"
            )
        bus = _required_text(payload.get("bus"), f"metric {name}.bus")
        quality = _required_text(payload.get("quality"), f"metric {name}.quality")
        if bus != source.bus or quality != source.quality:
            raise SnapshotValidationError(
                f"metric {name!r} bus/quality does not match catalog source {source_name!r}"
            )

        value = payload.get("value")
        if definition.value_type == "boolean":
            if type(value) is not bool:
                raise SnapshotValidationError(f"metric {name!r} value must be boolean")
            value_kind, value_num, value_text, value_bool = (
                "boolean",
                None,
                None,
                int(value),
            )
        elif definition.value_type in ("number", "integer"):
            valid = _finite_number(value)
            if definition.value_type == "integer":
                valid = isinstance(value, int) and not isinstance(value, bool)
            if not valid:
                raise SnapshotValidationError(
                    f"metric {name!r} value must be a finite {definition.value_type}"
                )
            value_kind, value_num, value_text, value_bool = (
                "number",
                float(value),
                None,
                None,
            )
        else:
            if not isinstance(value, str):
                raise SnapshotValidationError(f"metric {name!r} value must be text")
            value_kind, value_num, value_text, value_bool = (
                "string",
                None,
                value,
                None,
            )

        observed = payload.get("observed_at")
        if observed is None:
            observed_at = None
            observed_us = None
        else:
            observed_dt = _utc_datetime(observed, f"metric {name}.observed_at")
            observed_at = _iso(observed_dt)
            observed_us = _to_us(observed_dt)
        age_ms = _optional_nonnegative_int(payload.get("age_ms"), f"metric {name}.age_ms")
        reported_stale = payload.get("stale")
        if reported_stale is not None and type(reported_stale) is not bool:
            raise SnapshotValidationError(f"metric {name!r} stale must be boolean or null")
        if observed_us is None or age_ms is None:
            freshness = "undated"
        elif reported_stale is True or age_ms > definition.stale_after_ms:
            freshness = "stale"
        else:
            freshness = "fresh"
        acquisition = payload.get("acquisition")
        interface_mode = payload.get("interface_mode")
        if acquisition is not None and not isinstance(acquisition, str):
            raise SnapshotValidationError(f"metric {name!r} acquisition must be text or null")
        if interface_mode is not None and not isinstance(interface_mode, str):
            raise SnapshotValidationError(
                f"metric {name!r} interface_mode must be text or null"
            )
        return _MetricSample(
            metric=name,
            value_kind=value_kind,
            value_num=value_num,
            value_text=value_text,
            value_bool=value_bool,
            unit=unit,
            source=source_name,
            bus=bus,
            acquisition=acquisition,
            interface_mode=interface_mode,
            quality=quality,
            provenance=source.provenance,
            observed_us=observed_us,
            observed_at=observed_at,
            source_age_ms=age_ms,
            reported_stale=(int(reported_stale) if type(reported_stale) is bool else None),
            freshness=freshness,
        )

    @staticmethod
    def _vehicle_fields(snapshot: Mapping[str, object]) -> dict[str, object]:
        status = snapshot.get("status")
        vehicle = status.get("vehicle_state") if isinstance(status, Mapping) else None
        if not isinstance(vehicle, Mapping):
            return {
                "state": None,
                "running": None,
                "confidence": None,
                "basis": None,
                "observed_at": None,
                "age_ms": None,
            }
        running = vehicle.get("running")
        observed_at = vehicle.get("observed_at")
        if observed_at is not None:
            observed_at = _iso(
                _utc_datetime(observed_at, "status.vehicle_state.observed_at")
            )
        return {
            "state": vehicle.get("state") if isinstance(vehicle.get("state"), str) else None,
            "running": _bool_db(running),
            "confidence": (
                vehicle.get("confidence")
                if isinstance(vehicle.get("confidence"), str)
                else None
            ),
            "basis": vehicle.get("basis") if isinstance(vehicle.get("basis"), str) else None,
            "observed_at": observed_at,
            "age_ms": _optional_nonnegative_int(
                vehicle.get("age_ms"), "status.vehicle_state.age_ms"
            ),
        }

    @staticmethod
    def _delivery(snapshot: Mapping[str, object]) -> tuple[str | None, int | None, int | None]:
        delivery = snapshot.get("web_delivery")
        if not isinstance(delivery, Mapping):
            return None, None, None
        instance = delivery.get("instance_id")
        sequence = delivery.get("sequence")
        generated_ms = delivery.get("generated_at_ms")
        if not isinstance(instance, str) or not instance:
            instance = None
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            sequence = None
        if not isinstance(generated_ms, int) or isinstance(generated_ms, bool) or generated_ms < 0:
            generated_ms = None
        return instance, sequence, generated_ms

    @staticmethod
    def _interface_payloads(
        snapshot: Mapping[str, object],
    ) -> dict[str, Mapping[str, object]]:
        status = snapshot.get("status")
        if not isinstance(status, Mapping):
            return {}
        multiple = status.get("interfaces")
        if isinstance(multiple, Mapping):
            normalized_multiple: dict[str, Mapping[str, object]] = {}
            for key, payload in multiple.items():
                if not isinstance(payload, Mapping):
                    continue
                role = key
                if _is_placeholder_interface_role(role):
                    topology = payload.get("topology")
                    topology = topology if isinstance(topology, Mapping) else {}
                    role = topology.get("bus") or payload.get("role")
                if (
                    not isinstance(role, str)
                    or not role
                    or _is_placeholder_interface_role(role)
                ):
                    continue
                normalized_multiple[role] = payload
            if normalized_multiple:
                return normalized_multiple
        single = status.get("interface")
        if not isinstance(single, Mapping):
            return {}
        role_snapshot = single.get("role_interfaces")
        if "role_interfaces" in single:
            if not isinstance(role_snapshot, Mapping):
                return {}
            role_payloads = role_snapshot.get("roles")
            if not isinstance(role_payloads, Mapping):
                return {}
            normalized: dict[str, Mapping[str, object]] = {}
            for role, payload in role_payloads.items():
                if (
                    not isinstance(role, str)
                    or not role
                    or _is_placeholder_interface_role(role)
                    or not isinstance(payload, Mapping)
                ):
                    continue
                expected = payload.get("expected")
                expected = expected if isinstance(expected, Mapping) else {}
                if expected.get("passive_required") is not True:
                    continue
                actual = payload.get("actual")
                actual = actual if isinstance(actual, Mapping) else {}
                operating_mode = payload.get("operating_mode")
                topology_usable = payload.get("topology_usable")
                if type(topology_usable) is not bool:
                    topology_usable = payload.get("passive_ready")
                normalized[role] = {
                    "resolution": payload.get("resolution"),
                    "role_reason": payload.get("reason"),
                    "detail": payload.get("detail"),
                    "channel": payload.get("channel"),
                    "usb_serial": expected.get("usb_serial"),
                    "usb_dev_id": expected.get("dev_id"),
                    "topology_generation": role_snapshot.get("generation"),
                    "adapter_present": payload.get("resolution") == "resolved",
                    "up": actual.get("up"),
                    "bitrate": actual.get("bitrate"),
                    "listen_only": actual.get("listen_only"),
                    "controller_state": actual.get("controller_state"),
                    "receive_silent": (
                        payload.get("receive_watch", {}).get("receive_silent") is True
                        if isinstance(payload.get("receive_watch"), Mapping)
                        else False
                    ),
                    "mode": operating_mode,
                    # Set by the broker only for its own verified armed owner
                    # ("broker_active_drive" / "broker_auxiliary_drive").
                    "armed_owner": payload.get("armed_owner"),
                    "topology": {
                        "bus": role,
                        "usable": topology_usable,
                    },
                }
            # Presence of the role-aware shape is authoritative even before
            # reconciliation has populated a usable vehicle role.  Falling
            # through here used to promote its transient top-level ``canN``
            # channel to a durable historian role.
            return normalized
        topology = single.get("topology")
        bus = topology.get("bus") if isinstance(topology, Mapping) else None
        role = (
            bus
            if (
                isinstance(bus, str)
                and bool(bus)
                and not _is_placeholder_interface_role(bus)
            )
            else single.get("role")
        )
        if (
            not isinstance(role, str)
            or not role
            or _is_placeholder_interface_role(role)
        ):
            return {}
        return {role: single}

    @staticmethod
    def _interface_health(payload: Mapping[str, object]) -> tuple[str, str]:
        resolution = payload.get("resolution")
        if isinstance(resolution, str) and resolution != "resolved":
            state = (
                resolution
                if resolution in ("missing", "ambiguous")
                else "unresolved"
            )
            return "unhealthy", f"role_{state}"
        topology = payload.get("topology")
        topology_usable = topology.get("usable") if isinstance(topology, Mapping) else None
        checks = {
            "adapter_missing": payload.get("adapter_present") is False,
            "interface_down": payload.get("up") is False,
            "controller_unhealthy": (
                isinstance(payload.get("controller_state"), str)
                and payload.get("controller_state") != "ERROR-ACTIVE"
            ),
            "topology_unusable": topology_usable is False,
            # Board A went deaf on 2026-09-22 with every link check passing;
            # see receive_watch.py.
            "receive_silent": payload.get("receive_silent") is True,
        }
        failures = [reason for reason, failed in checks.items() if failed]
        if failures:
            return "unhealthy", ",".join(failures)
        known = (
            type(payload.get("adapter_present")) is bool
            and type(payload.get("up")) is bool
            and isinstance(payload.get("controller_state"), str)
            and type(topology_usable) is bool
        )
        if not known:
            return "unknown", "interface_health_incomplete"
        if payload.get("mode") == "armed_diagnostic":
            return "healthy", "armed_diagnostic"
        return "healthy", "healthy"

    @staticmethod
    def _safe_json_list(value: object) -> list[object]:
        """Keep only JSON-compatible list data from broker health status."""

        if not isinstance(value, list):
            return []
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            decoded = json.loads(encoded)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []

    @staticmethod
    def _usb_can_serials(value: object, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 8:
            raise SnapshotValidationError(f"{field} must contain at most 8 serials")
        serials: list[str] = []
        for index, serial in enumerate(value):
            serial = _required_text(serial, f"{field}[{index}]")
            if len(serial) > 256:
                raise SnapshotValidationError(f"{field}[{index}] is oversized")
            serials.append(serial)
        if len(set(serials)) != len(serials):
            raise SnapshotValidationError(f"{field} contains duplicate serials")
        return sorted(serials)

    @staticmethod
    def _usb_can_time(value: object, field: str) -> tuple[int, str]:
        moment = _utc_datetime(value, field)
        return _to_us(moment), _iso(moment)

    @staticmethod
    def _usb_can_serial_health_proven(
        snapshot: Mapping[str, object], serial: str
    ) -> bool:
        """Require every exact role for one serial to be resolved and safe."""

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        interface = status.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = role_snapshot if isinstance(role_snapshot, Mapping) else {}
        roles = role_snapshot.get("roles")
        roles = roles if isinstance(roles, Mapping) else {}
        matched: list[Mapping[str, object]] = []
        for payload in roles.values():
            if not isinstance(payload, Mapping):
                continue
            expected = payload.get("expected")
            expected = expected if isinstance(expected, Mapping) else {}
            if expected.get("usb_serial") != serial:
                continue
            matched.append(payload)
            if payload.get("resolution") != "resolved":
                return False
            actual = payload.get("actual")
            actual = actual if isinstance(actual, Mapping) else {}
            if actual.get("present") is False:
                return False
            if payload.get("safe") is True:
                continue
            if expected.get("passive_required") is True:
                if payload.get("passive_ready") is not True:
                    return False
            elif actual.get("up") is not False:
                return False
        return bool(matched)
