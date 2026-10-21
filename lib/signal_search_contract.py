"""Pure records and bounds shared by saved-log signal-search tools."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from lib.signal_fields import SignalField


SCHEMA_VERSION = 1
EXPLORATORY_EVIDENCE_TIER = "exploratory_candidate"
PROXY_EVALUATION_EVIDENCE_TIER = "operational_proxy_evaluation"
MAX_BIT_SEARCH_IDENTIFIERS = 2
MAX_RADIUS_MS = 10_000


class CorrelateError(RuntimeError):
    """Raised for malformed evidence or a violated offline-analysis gate."""


@dataclass(frozen=True, order=True)
class FieldSpec:
    kind: str
    offset: int
    geometry: SignalField | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.offset, int)
            or isinstance(self.offset, bool)
            or self.offset < 0
        ):
            raise CorrelateError("field offset must be a non-negative integer")
        if self.kind == "bitfield":
            if self.geometry is None:
                raise CorrelateError("bitfield specification requires geometry")
            if self.offset != self.geometry.first_payload_byte:
                raise CorrelateError(
                    "bitfield offset must equal its first payload byte"
                )
        elif self.geometry is not None:
            raise CorrelateError(
                "legacy field specification cannot carry bit geometry"
            )

    @classmethod
    def from_signal_field(cls, geometry: SignalField) -> "FieldSpec":
        return cls("bitfield", geometry.first_payload_byte, geometry)

    @property
    def width_bytes(self) -> int:
        if self.geometry is not None:
            return self.geometry.span_bytes
        if self.kind == "byte":
            return 1
        if self.kind == "u13be-low5":
            return 2
        if self.kind == "u17be-low1":
            return 3
        if self.kind.startswith(("u16", "i16")):
            return 2
        if self.kind.startswith(("u32", "i32")):
            return 4
        raise CorrelateError(f"unsupported field kind {self.kind!r}")

    @property
    def byte_order(self) -> str | None:
        if self.geometry is not None:
            return self.geometry.byte_order
        if self.kind in ("u13be-low5", "u17be-low1"):
            return "big"
        if self.kind in ("u16be", "i16be"):
            return "big"
        if self.kind in ("u16le", "i16le"):
            return "little"
        if self.kind in ("u32be", "i32be"):
            return "big"
        if self.kind in ("u32le", "i32le"):
            return "little"
        return None

    @property
    def signed(self) -> bool:
        if self.geometry is not None:
            return self.geometry.signed
        return self.kind.startswith("i")

    @property
    def label(self) -> str:
        if self.geometry is not None:
            return self.geometry.label
        return f"{self.kind}:{self.offset}"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "offset": self.offset,
            "width_bytes": self.width_bytes,
            "byte_order": self.byte_order,
            "signed": self.signed,
        }
        if self.geometry is not None:
            result.update(
                {
                    "dbc_start_bit": self.geometry.dbc_start_bit,
                    "length_bits": self.geometry.length_bits,
                    "label": self.geometry.label,
                    "bit_numbering": "dbc_cantools_sawtooth",
                }
            )
        return result

    def sort_key(self) -> tuple[object, ...]:
        if self.geometry is None:
            return (0, self.kind, self.offset)
        return (
            1,
            self.kind,
            self.offset,
            self.geometry.dbc_start_bit,
            self.geometry.length_bits,
            self.geometry.byte_order,
            self.geometry.signed,
        )


@lru_cache(maxsize=None)
def legacy_signal_field(spec: FieldSpec) -> SignalField:
    if spec.geometry is not None:
        return spec.geometry
    if spec.kind == "byte":
        return SignalField(spec.offset * 8, 8, "little")
    if spec.kind == "u13be-low5":
        return SignalField(spec.offset * 8 + 4, 13, "big")
    if spec.kind == "u17be-low1":
        return SignalField(spec.offset * 8, 17, "big")
    match = re.fullmatch(r"([ui])(16|32)(be|le)", spec.kind)
    if match is None:
        raise CorrelateError(f"unsupported field kind {spec.kind!r}")
    byte_order = "big" if match.group(3) == "be" else "little"
    start_bit = spec.offset * 8 + (7 if byte_order == "big" else 0)
    return SignalField(
        start_bit,
        int(match.group(2), 10),
        byte_order,
        signed=match.group(1) == "i",
    )


# Backward-compatible private alias retained by tools.can_timeseries_correlate.
_legacy_signal_field = legacy_signal_field
