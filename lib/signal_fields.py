"""Pure helpers for extracting and inserting DBC-style CAN signal fields.

The repository previously had several local byte/word decoders plus two
Stellantis-specific packed fields.  This module provides one deterministic,
dependency-free representation for arbitrary 1..32 bit fields:

* ``little`` uses the DBC/cantools Intel convention: ``start_bit`` is the
  signal's least-significant bit and successive raw bits occupy increasing
  payload bit numbers.
* ``big`` uses the DBC/cantools Motorola sawtooth convention: ``start_bit`` is
  the signal's most-significant bit.  Traversal moves toward bit zero within a
  byte, then continues at bit seven of the next payload byte.

Payload bit numbers follow the usual DBC convention where bit 0 is the least
significant bit of payload byte 0 and bit 7 is its most significant bit.

These helpers are strictly offline data transformations.  They perform no CAN
I/O and deliberately know nothing about ECU/DID namespaces or physical units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


MAX_SIGNAL_BITS = 32
MAX_PAYLOAD_BYTES = 64
BYTE_ORDERS = frozenset(("little", "big"))


class SignalFieldError(ValueError):
    """Raised when a field geometry or raw value is invalid."""


@dataclass(frozen=True, order=True)
class SignalField:
    """One raw CAN signal using DBC/cantools start-bit conventions."""

    dbc_start_bit: int
    length_bits: int
    byte_order: str
    signed: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.dbc_start_bit, int)
            or isinstance(self.dbc_start_bit, bool)
            or self.dbc_start_bit < 0
        ):
            raise SignalFieldError(
                "dbc_start_bit must be a non-negative integer"
            )
        if self.dbc_start_bit >= MAX_PAYLOAD_BYTES * 8:
            raise SignalFieldError(
                f"dbc_start_bit must be below {MAX_PAYLOAD_BYTES * 8}"
            )
        if (
            not isinstance(self.length_bits, int)
            or isinstance(self.length_bits, bool)
            or not 1 <= self.length_bits <= MAX_SIGNAL_BITS
        ):
            raise SignalFieldError(
                f"length_bits must be between 1 and {MAX_SIGNAL_BITS}"
            )
        if self.byte_order not in BYTE_ORDERS:
            raise SignalFieldError("byte_order must be 'little' or 'big'")
        if type(self.signed) is not bool:
            raise SignalFieldError("signed must be a bool")
        if max(self.occupied_bits()) >= MAX_PAYLOAD_BYTES * 8:
            raise SignalFieldError(
                f"field geometry exceeds the {MAX_PAYLOAD_BYTES}-byte "
                "payload limit"
            )

    @property
    def raw_minimum(self) -> int:
        if self.signed:
            return -(1 << (self.length_bits - 1))
        return 0

    @property
    def raw_maximum(self) -> int:
        if self.signed:
            return (1 << (self.length_bits - 1)) - 1
        return (1 << self.length_bits) - 1

    @property
    def raw_mask(self) -> int:
        return (1 << self.length_bits) - 1

    def occupied_bits(self) -> tuple[int, ...]:
        """Return payload bit positions in raw significance order.

        For little-endian fields the tuple runs raw LSB -> MSB.  For
        big-endian fields it runs raw MSB -> LSB, mirroring DBC traversal.
        """

        if self.byte_order == "little":
            return tuple(
                range(
                    self.dbc_start_bit,
                    self.dbc_start_bit + self.length_bits,
                )
            )

        positions: list[int] = []
        position = self.dbc_start_bit
        for _ in range(self.length_bits):
            positions.append(position)
            if position % 8 == 0:
                position += 15
            else:
                position -= 1
        return tuple(positions)

    @property
    def required_payload_bytes(self) -> int:
        return max(self.occupied_bits()) // 8 + 1

    @property
    def first_payload_byte(self) -> int:
        return min(self.occupied_bits()) // 8

    @property
    def last_payload_byte(self) -> int:
        return max(self.occupied_bits()) // 8

    @property
    def span_bytes(self) -> int:
        return self.last_payload_byte - self.first_payload_byte + 1

    @property
    def label(self) -> str:
        prefix = "i" if self.signed else "u"
        order = "le" if self.byte_order == "little" else "be"
        return f"{prefix}{self.length_bits}{order}@{self.dbc_start_bit}"

    def _require_payload(self, payload: bytes | bytearray) -> None:
        if not isinstance(payload, (bytes, bytearray)):
            raise SignalFieldError("payload must be bytes or bytearray")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise SignalFieldError(
                f"payload exceeds {MAX_PAYLOAD_BYTES} bytes"
            )
        if self.required_payload_bytes > len(payload):
            raise SignalFieldError(
                f"field {self.label} exceeds a {len(payload)}-byte payload"
            )

    def extract_unsigned(self, payload: bytes | bytearray) -> int:
        """Extract the field's unsigned raw bit pattern."""

        self._require_payload(payload)
        if self.byte_order == "little":
            packed = int.from_bytes(payload, "little")
            return (packed >> self.dbc_start_bit) & self.raw_mask

        raw = 0
        for position in self.occupied_bits():
            byte_index, bit_index = divmod(position, 8)
            raw = (raw << 1) | ((payload[byte_index] >> bit_index) & 1)
        return raw

    def extract(self, payload: bytes | bytearray) -> int:
        """Extract the field and apply two's-complement signedness."""

        raw = self.extract_unsigned(payload)
        if self.signed and raw & (1 << (self.length_bits - 1)):
            raw -= 1 << self.length_bits
        return raw

    def insert(self, payload: bytes | bytearray, value: int) -> bytes:
        """Return a copy of ``payload`` with this raw field set to ``value``.

        Bits outside the field are preserved.  This is primarily useful for
        synthetic fixtures and round-trip validation.
        """

        self._require_payload(payload)
        if not isinstance(value, int) or isinstance(value, bool):
            raise SignalFieldError("value must be an integer")
        if not self.raw_minimum <= value <= self.raw_maximum:
            raise SignalFieldError(
                f"value {value} is outside {self.label}'s raw range "
                f"{self.raw_minimum}..{self.raw_maximum}"
            )
        raw = value & self.raw_mask
        output = bytearray(payload)

        if self.byte_order == "little":
            positions = self.occupied_bits()
            raw_bit_indexes = range(self.length_bits)
        else:
            positions = self.occupied_bits()
            raw_bit_indexes = range(
                self.length_bits - 1, -1, -1
            )

        for position, raw_bit_index in zip(positions, raw_bit_indexes):
            byte_index, bit_index = divmod(position, 8)
            mask = 1 << bit_index
            if raw & (1 << raw_bit_index):
                output[byte_index] |= mask
            else:
                output[byte_index] &= ~mask
        return bytes(output)

    def value_signature(self) -> tuple[tuple[int, int], ...]:
        """Return a byte-order-independent bit-to-raw-weight mapping.

        Equivalent one-byte Intel/Motorola definitions therefore share a
        signature and can be de-duplicated during exhaustive enumeration.
        """

        positions = self.occupied_bits()
        if self.byte_order == "little":
            raw_indexes: Iterable[int] = range(self.length_bits)
        else:
            raw_indexes = range(self.length_bits - 1, -1, -1)
        return tuple(sorted(zip(positions, raw_indexes)))

    def as_dict(self) -> dict[str, object]:
        return {
            "dbc_start_bit": self.dbc_start_bit,
            "length_bits": self.length_bits,
            "byte_order": self.byte_order,
            "signed": self.signed,
            "label": self.label,
            "first_payload_byte": self.first_payload_byte,
            "last_payload_byte": self.last_payload_byte,
            "span_bytes": self.span_bytes,
        }


def _validated_lengths(
    lengths: Iterable[int] | None,
    *,
    minimum_bits: int,
    maximum_bits: int,
) -> tuple[int, ...]:
    if (
        not isinstance(minimum_bits, int)
        or isinstance(minimum_bits, bool)
        or not isinstance(maximum_bits, int)
        or isinstance(maximum_bits, bool)
        or not 1 <= minimum_bits <= maximum_bits <= MAX_SIGNAL_BITS
    ):
        raise SignalFieldError(
            f"bit-length bounds must satisfy 1 <= minimum <= maximum <= "
            f"{MAX_SIGNAL_BITS}"
        )
    if lengths is None:
        return tuple(range(minimum_bits, maximum_bits + 1))
    selected: set[int] = set()
    for length in lengths:
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or not minimum_bits <= length <= maximum_bits
        ):
            raise SignalFieldError(
                "selected lengths must be integers within the configured bounds"
            )
        selected.add(length)
    if not selected:
        raise SignalFieldError("at least one signal length is required")
    return tuple(sorted(selected))


def iter_signal_fields(
    payload_length: int,
    *,
    minimum_bits: int = 1,
    maximum_bits: int = MAX_SIGNAL_BITS,
    lengths: Iterable[int] | None = None,
    byte_orders: Iterable[str] = ("little", "big"),
    signedness: Iterable[bool] = (False, True),
) -> Iterator[SignalField]:
    """Enumerate unique valid fields for one fixed payload length.

    Equivalent geometries that map every payload bit to the same raw bit
    weight are emitted once per signedness.  Ordering is deterministic.
    """

    if (
        not isinstance(payload_length, int)
        or isinstance(payload_length, bool)
        or not 1 <= payload_length <= MAX_PAYLOAD_BYTES
    ):
        raise SignalFieldError(
            f"payload_length must be between 1 and {MAX_PAYLOAD_BYTES}"
        )
    selected_lengths = _validated_lengths(
        lengths,
        minimum_bits=minimum_bits,
        maximum_bits=maximum_bits,
    )
    selected_orders = tuple(dict.fromkeys(byte_orders))
    if not selected_orders or any(
        order not in BYTE_ORDERS for order in selected_orders
    ):
        raise SignalFieldError(
            "byte_orders must contain 'little' and/or 'big'"
        )
    selected_signedness = tuple(dict.fromkeys(signedness))
    if not selected_signedness or any(
        type(value) is not bool for value in selected_signedness
    ):
        raise SignalFieldError(
            "signedness must contain bool values"
        )

    payload_bits = payload_length * 8
    seen: set[tuple[tuple[tuple[int, int], ...], bool]] = set()
    for byte_order in selected_orders:
        for start_bit in range(payload_bits):
            for length_bits in selected_lengths:
                try:
                    field = SignalField(
                        dbc_start_bit=start_bit,
                        length_bits=length_bits,
                        byte_order=byte_order,
                        signed=False,
                    )
                except SignalFieldError:
                    continue
                if field.required_payload_bytes > payload_length:
                    continue
                for signed in selected_signedness:
                    candidate = SignalField(
                        dbc_start_bit=start_bit,
                        length_bits=length_bits,
                        byte_order=byte_order,
                        signed=signed,
                    )
                    signature = (candidate.value_signature(), signed)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    yield candidate
