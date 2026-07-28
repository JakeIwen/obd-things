import random
import unittest

from lib.signal_fields import (
    SignalField,
    SignalFieldError,
    iter_signal_fields,
)

try:
    import cantools
    from cantools.database.can import Message, Signal
except ImportError:  # The repository's base runtime intentionally has no dependency.
    cantools = None
    Message = None
    Signal = None


class SignalFieldGeometryTests(unittest.TestCase):
    def test_aligned_standard_byte_order_vectors(self):
        payload = bytes.fromhex("12 34 56 78")

        self.assertEqual(SignalField(7, 16, "big").extract(payload), 0x1234)
        self.assertEqual(
            SignalField(0, 16, "little").extract(payload), 0x3412
        )
        self.assertEqual(
            SignalField(15, 16, "big").extract(payload), 0x3456
        )
        self.assertEqual(
            SignalField(8, 16, "little").extract(payload), 0x5634
        )

    def test_fixed_cantools_generated_boundary_vectors(self):
        # Generated once with cantools 42.0.3. These remain dependency-free
        # regression fixtures in the base environment.
        fixtures = (
            (SignalField(3, 29, "little"), 0x1234567, "38 2B 1A 09 00 00 00 00"),
            (SignalField(6, 21, "big"), 0x155555, "55 55 54 00 00 00 00 00"),
            (
                SignalField(55, 9, "little", signed=True),
                -173,
                "00 00 00 00 00 00 80 A9",
            ),
            (
                SignalField(55, 16, "big", signed=True),
                -12345,
                "00 00 00 00 00 00 CF C7",
            ),
            (SignalField(48, 9, "big"), 0x1A5, "00 00 00 00 00 00 01 A5"),
        )

        for field, value, payload_hex in fixtures:
            with self.subTest(field=field):
                expected = bytes.fromhex(payload_hex)
                self.assertEqual(field.extract(expected), value)
                self.assertEqual(field.insert(bytes(8), value), expected)

    def test_little_endian_extract_insert_and_signedness(self):
        unsigned = SignalField(4, 12, "little")
        payload = bytes.fromhex("A5 BC 7E")

        self.assertEqual(unsigned.extract(payload), 0xBCA)
        inserted = unsigned.insert(bytes.fromhex("05 00 7E"), 0xBCA)
        self.assertEqual(inserted, payload)
        self.assertEqual(inserted[2], 0x7E)

        signed = SignalField(0, 12, "little", signed=True)
        negative = signed.insert(b"\x00\xA0", -5)
        self.assertEqual(negative, bytes.fromhex("FB AF"))
        self.assertEqual(signed.extract(negative), -5)

    def test_motorola_sawtooth_geometry_and_stellantis_fields(self):
        torque = SignalField(4, 13, "big")
        output_speed = SignalField(0, 17, "big")

        self.assertEqual(
            torque.occupied_bits(),
            (4, 3, 2, 1, 0, 15, 14, 13, 12, 11, 10, 9, 8),
        )
        self.assertEqual(torque.extract(bytes.fromhex("E1 23")), 0x123)
        self.assertEqual(torque.required_payload_bytes, 2)
        self.assertEqual(torque.label, "u13be@4")

        self.assertEqual(
            output_speed.occupied_bits(),
            (0, 15, 14, 13, 12, 11, 10, 9, 8, 23, 22, 21, 20, 19, 18, 17, 16),
        )
        self.assertEqual(
            output_speed.extract(bytes.fromhex("FD 6F A1")), 0x16FA1
        )
        self.assertEqual(output_speed.required_payload_bytes, 3)

    def test_motorola_signed_field_and_unrelated_bits_are_preserved(self):
        field = SignalField(15, 12, "big", signed=True)
        background = bytes.fromhex("A5 00 0F 77")

        encoded = field.insert(background, -5)

        self.assertEqual(encoded, bytes.fromhex("A5 FF BF 77"))
        self.assertEqual(field.extract(encoded), -5)
        self.assertEqual(encoded[0], background[0])
        self.assertEqual(encoded[2] & 0x0F, background[2] & 0x0F)
        self.assertEqual(encoded[3], background[3])

    def test_value_range_payload_bounds_and_type_validation(self):
        with self.assertRaisesRegex(SignalFieldError, "dbc_start_bit"):
            SignalField(True, 8, "little")
        with self.assertRaisesRegex(SignalFieldError, "length_bits"):
            SignalField(0, 33, "little")
        with self.assertRaisesRegex(SignalFieldError, "byte_order"):
            SignalField(0, 8, "network")
        with self.assertRaisesRegex(SignalFieldError, "payload limit"):
            SignalField(511, 2, "little")
        with self.assertRaisesRegex(SignalFieldError, "payload limit"):
            SignalField(504, 2, "big")
        with self.assertRaisesRegex(SignalFieldError, "exceeds"):
            SignalField(4, 13, "big").extract(b"\x00")
        with self.assertRaisesRegex(SignalFieldError, "raw range"):
            SignalField(0, 8, "little", signed=True).insert(b"\x00", 128)
        with self.assertRaisesRegex(SignalFieldError, "integer"):
            SignalField(0, 8, "little").insert(b"\x00", True)

    def test_enumerator_deduplicates_equivalent_one_byte_geometries(self):
        fields = list(
            iter_signal_fields(
                1,
                minimum_bits=1,
                maximum_bits=8,
                byte_orders=("little", "big"),
                signedness=(False,),
            )
        )
        signatures = [field.value_signature() for field in fields]

        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertIn(SignalField(0, 8, "little"), fields)
        self.assertNotIn(SignalField(7, 8, "big"), fields)
        self.assertEqual(len(fields), 36)

    def test_enumerator_respects_lengths_orders_and_signedness(self):
        fields = list(
            iter_signal_fields(
                3,
                minimum_bits=13,
                maximum_bits=17,
                lengths=(13, 17),
                byte_orders=("big",),
                signedness=(False, True),
            )
        )

        self.assertIn(SignalField(4, 13, "big"), fields)
        self.assertIn(SignalField(4, 13, "big", signed=True), fields)
        self.assertIn(SignalField(0, 17, "big"), fields)
        self.assertTrue(
            all(field.length_bits in (13, 17) for field in fields)
        )
        self.assertTrue(
            all(field.required_payload_bytes <= 3 for field in fields)
        )

    def test_full_classic_can_profile_is_bounded_and_skips_invalid_edges(self):
        fields = list(iter_signal_fields(8))

        self.assertEqual(len(fields), 5632)
        self.assertNotIn(SignalField(63, 2, "little"), fields)
        self.assertNotIn(SignalField(56, 2, "big"), fields)
        self.assertIn(SignalField(63, 1, "little"), fields)
        self.assertIn(SignalField(56, 1, "little"), fields)
        self.assertNotIn(SignalField(56, 1, "big"), fields)

    def test_enumerator_handles_the_64_byte_constructor_boundary(self):
        fields = list(
            iter_signal_fields(
                64,
                lengths=(32,),
                byte_orders=("little", "big"),
                signedness=(False,),
            )
        )

        self.assertIn(SignalField(480, 32, "little"), fields)
        self.assertIn(SignalField(487, 32, "big"), fields)
        self.assertTrue(
            all(field.required_payload_bytes <= 64 for field in fields)
        )


@unittest.skipUnless(cantools is not None, "cantools is an optional test dependency")
class CantoolsCompatibilityTests(unittest.TestCase):
    def test_every_width_order_and_signed_boundary_values(self):
        checked = 0
        for width in range(1, 33):
            for order in ("little", "big"):
                for signed in (False, True):
                    candidates = list(
                        iter_signal_fields(
                            8,
                            lengths=(width,),
                            byte_orders=(order,),
                            signedness=(signed,),
                        )
                    )
                    field = candidates[(width * 7) % len(candidates)]
                    signal = Signal(
                        name="value",
                        start=field.dbc_start_bit,
                        length=field.length_bits,
                        byte_order=(
                            "little_endian"
                            if order == "little"
                            else "big_endian"
                        ),
                        is_signed=signed,
                    )
                    message = Message(
                        frame_id=0x123,
                        name="Boundary",
                        length=8,
                        signals=[signal],
                        strict=True,
                    )
                    values = {
                        field.raw_minimum,
                        field.raw_maximum,
                        0,
                    }
                    if signed:
                        values.add(-1)
                    for value in sorted(values):
                        encoded = message.encode(
                            {"value": value}, scaling=False
                        )
                        self.assertEqual(field.extract(encoded), value)
                        inserted = field.insert(bytes(8), value)
                        self.assertEqual(
                            message.decode(
                                inserted,
                                decode_choices=False,
                                scaling=False,
                            )["value"],
                            value,
                        )
                        checked += 1

        self.assertEqual(checked, 380)

    def test_deterministic_randomized_two_way_compatibility(self):
        rng = random.Random(0x20220728)
        widths = (1, 2, 7, 8, 9, 13, 16, 17, 24, 31, 32)
        checked = 0

        for payload_length in range(1, 9):
            candidates = list(
                iter_signal_fields(
                    payload_length,
                    lengths=(
                        width
                        for width in widths
                        if width <= min(32, payload_length * 8)
                    ),
                )
            )
            for _ in range(625):
                field = rng.choice(candidates)
                signal = Signal(
                    name="value",
                    start=field.dbc_start_bit,
                    length=field.length_bits,
                    byte_order=(
                        "little_endian"
                        if field.byte_order == "little"
                        else "big_endian"
                    ),
                    is_signed=field.signed,
                )
                message = Message(
                    frame_id=0x123,
                    name="Synthetic",
                    length=payload_length,
                    signals=[signal],
                    strict=True,
                )
                value = rng.randint(field.raw_minimum, field.raw_maximum)

                cantools_payload = message.encode(
                    {"value": value}, scaling=False
                )
                self.assertEqual(field.extract(cantools_payload), value)

                background = bytes(
                    rng.randrange(256) for _ in range(payload_length)
                )
                inserted = field.insert(background, value)
                decoded = message.decode(
                    inserted, decode_choices=False, scaling=False
                )["value"]
                self.assertEqual(decoded, value)
                checked += 1

        self.assertEqual(checked, 5000)

    def test_stellantis_fields_serialize_to_expected_dbc_shapes(self):
        messages = []
        for frame_id, field in (
            (0x100, SignalField(4, 13, "big")),
            (0x101, SignalField(0, 17, "big")),
        ):
            signal = Signal(
                name="value",
                start=field.dbc_start_bit,
                length=field.length_bits,
                byte_order="big_endian",
                is_signed=False,
            )
            messages.append(
                Message(
                    frame_id=frame_id,
                    name=f"Frame_{frame_id:03X}",
                    length=8,
                    signals=[signal],
                    strict=True,
                )
            )
        dbc = cantools.database.Database(messages=messages).as_dbc_string()

        self.assertIn("SG_ value : 4|13@0+", dbc)
        self.assertIn("SG_ value : 0|17@0+", dbc)


if __name__ == "__main__":
    unittest.main()
