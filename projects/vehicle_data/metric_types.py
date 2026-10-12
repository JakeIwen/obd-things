"""Canonical telemetry metric and source definitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    bus: str
    bitrate: int
    acquisition_class: str
    quality: str
    provenance: str
    side_effects: str
    publisher_allowed: bool = False
    publisher_values: tuple[object, ...] | None = None


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    unit: str
    value_type: str
    stale_after_seconds: float
    passive_min_interval_seconds: float
    sources: tuple[SourceDefinition, ...]
    allowed_acquisition_modes: tuple[str, ...] = ()
    wake_min_interval_seconds: float = 900.0
    minimum: float | None = None
    maximum: float | None = None

    def public_dict(self):
        public_sources = []
        for source in self.sources:
            source_payload = asdict(source)
            # This is an ingest-validation constraint, not a claim about the
            # complete physical value domain of the underlying signal.
            source_payload.pop("publisher_values", None)
            public_sources.append(source_payload)
        payload = {
            "name": self.name,
            "unit": self.unit,
            "value_type": self.value_type,
            "stale_after_seconds": self.stale_after_seconds,
            "sources": public_sources,
            "allowed_acquisition_modes": list(self.allowed_acquisition_modes),
        }
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        return payload
