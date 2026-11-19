"""Intake-air (0x417) and A/C high-side (0x41B) broadcasts, end to end."""
import struct
import threading
import unittest
from itertools import count
from unittest import mock

from lib import broadcast_signals as bs
from projects.vehicle_data import active_drive, ccan_powertrain as cp, intake_ac_metrics
from projects.vehicle_data.metrics import METRICS
from tests.test_ccan_powertrain import FakeSocket, TEST_CHANNEL, frame

# Real frames from the 2026-07-27 and 2026-08-19 warm captures (pcm-plots and
# three-bus candump), with the raw field read by hand from the bit layout.
# (frame hex, 8-bit field = byte2 bits 4-0 then byte3 bits 7-5)
IAT_FRAMES = (
    ("b71e07f8e0000000", 63),   # 23 C, August cool-soak low end
    ("b71e07d6e0000000", 62),   # August minimum
    ("991e083d20000000", 65),   # July
    ("b4000c9920000000", 100),  # August maximum, 60 C heat soak
)
# (frame hex, 9-bit field = byte0 then byte1 bit 7)
AC_FRAMES = (
    ("1ca0000000400000", 57),
    ("1fa0000000580000", 63),   # July
    ("4da0000000600000", 155),  # August maximum
    ("0020000000b00000", 0),
)
REQUIRED = (
    (0x0FC, "2fa3"), (0x41D, "000036"), (0x2ED, "7e"),
    (0x100, "4fb9f43ee0000fcf"), (0x101, "0060000000000000"),
    (0x1F7, "002d102005b60000"), (0x2EF, ""), (0x41A, "be"),
)


def f_from_c(c):
    return c * 9 / 5 + 32


class IntakeAcDecodeTests(unittest.TestCase):
    def test_intake_air_real_frames(self):
        for data, field in IAT_FRAMES:
            with self.subTest(data=data):
                value = bs.INTAKE_AIR_TEMPERATURE.decode(bytes.fromhex(data))
                self.assertAlmostEqual(value, f_from_c(field - 40))
        # 63 is 23 C = 73.4 F, 100 is 60 C = 140 F.
        self.assertAlmostEqual(
            bs.INTAKE_AIR_TEMPERATURE.decode(bytes.fromhex(IAT_FRAMES[0][0])), 73.4)

    def test_intake_air_ignores_neighbour_bits(self):
        base = bytearray.fromhex("b71e07f8e0000000")
        want = bs.INTAKE_AIR_TEMPERATURE.decode(base)
        noisy = bytearray(base)
        noisy[3] |= 0x1F      # byte 3 bits 4-0 are another field
        noisy[2] |= 0xE0      # byte 2 bits 7-5 likewise
        noisy[4] = 0xFF
        self.assertEqual(bs.INTAKE_AIR_TEMPERATURE.decode(noisy), want)

    def test_ac_pressure_real_frames(self):
        for data, field in AC_FRAMES:
            with self.subTest(data=data):
                value = bs.AC_HIGH_SIDE_PRESSURE.decode(bytes.fromhex(data))
                self.assertAlmostEqual(value, (9.9 * field + 6) * bs.KPA_TO_PSI)
        self.assertAlmostEqual(
            bs.AC_HIGH_SIDE_PRESSURE.decode(bytes.fromhex("4da0000000600000")),
            1540.5 * bs.KPA_TO_PSI)

    def test_ac_pressure_ignores_neighbour_bits(self):
        base = bytearray.fromhex("1ca0000000400000")
        want = bs.AC_HIGH_SIDE_PRESSURE.decode(base)
        noisy = bytearray(base)
        noisy[1] |= 0x7F      # byte 1 bits 6-0 are another field
        noisy[4] = 0xFF
        noisy[5] = 0xFF
        self.assertEqual(bs.AC_HIGH_SIDE_PRESSURE.decode(noisy), want)

    def test_short_frames_decode_to_nothing(self):
        self.assertIsNone(bs.INTAKE_AIR_TEMPERATURE.decode(b"\xb7\x1e\x07"))
        self.assertIsNone(bs.AC_HIGH_SIDE_PRESSURE.decode(b"\x1c"))

    def test_observations_carry_candidate_quality_and_registry_names(self):
        (iat,) = cp.decode_frame_observations(0x417, bytes.fromhex(IAT_FRAMES[0][0]))
        (ac,) = cp.decode_frame_observations(0x41B, bytes.fromhex(AC_FRAMES[0][0]))
        for obs, name, unit in (
            (iat, "engine.intake_air_temperature", "°F"),
            (ac, "hvac.ac_high_side_pressure", "psi"),
        ):
            definition = METRICS[name]
            self.assertEqual((obs.metric, obs.unit, obs.quality), (name, unit, "candidate"))
            self.assertEqual(definition.unit, unit)
            self.assertEqual(obs.source, definition.sources[0].name)
            self.assertTrue(definition.sources[0].publisher_allowed)
            self.assertTrue(definition.minimum <= obs.value <= definition.maximum)
        self.assertIn("420 kPa", ac.detail)


class IntakeAcSnapshotTests(unittest.TestCase):
    def filters(self, fake):
        return [
            struct.unpack("=II", fake.filters[o:o + 8])[0]
            for o in range(0, len(fake.filters), 8)
        ]

    def read(self, frames, **kwargs):
        fake = FakeSocket(frames)
        ticks = count(start=0.0, step=0.01)
        snapshot = cp.read_broadcast_snapshot(
            TEST_CHANNEL, timeout=0.5, include_battery=True,
            socket_factory=lambda *_a: fake, monotonic=lambda: next(ticks), **kwargs)
        return fake, snapshot

    def test_filters_include_both_ids_by_default_and_drop_them_when_gated(self):
        fake, _ = self.read([])
        self.assertTrue({0x417, 0x41B} <= set(self.filters(fake)))
        self.assertTrue({0x417, 0x41B} <= set(cp.FILTER_IDS))
        fake, _ = self.read([], include_intake_ac=False)
        self.assertFalse({0x417, 0x41B} & set(self.filters(fake)))
        self.assertIn(0x41A, self.filters(fake))

    def required_frames(self):
        return [frame(i, bytes.fromhex(h)) for i, h in REQUIRED]

    def test_snapshot_completes_without_the_new_frames(self):
        sentinel = frame(0x41D, bytes.fromhex("000036"))
        fake, snapshot = self.read(self.required_frames() + [sentinel])
        # Complete as soon as the required set was seen: sentinel unconsumed.
        self.assertEqual(len(fake.frames), 1)
        metrics = {o.metric for o in snapshot.observations}
        self.assertNotIn("engine.intake_air_temperature", metrics)
        self.assertNotIn("hvac.ac_high_side_pressure", metrics)

    def test_new_frames_are_reported_when_present_and_dropped_when_gated(self):
        extra = [frame(0x417, bytes.fromhex(IAT_FRAMES[3][0])),
                 frame(0x41B, bytes.fromhex(AC_FRAMES[2][0]))]
        _, snapshot = self.read(extra + self.required_frames())
        by_metric = {o.metric: o.value for o in snapshot.observations}
        self.assertAlmostEqual(by_metric["engine.intake_air_temperature"], 140.0)
        self.assertAlmostEqual(by_metric["hvac.ac_high_side_pressure"], 1540.5 * bs.KPA_TO_PSI)
        _, snapshot = self.read(extra + self.required_frames(), include_intake_ac=False)
        metrics = {o.metric for o in snapshot.observations}
        self.assertNotIn("engine.intake_air_temperature", metrics)
        self.assertNotIn("hvac.ac_high_side_pressure", metrics)
        self.assertIn("engine.rpm", metrics)


class ImplausibleReadingTests(unittest.TestCase):
    """The broker rejects out-of-registry-range observations by ending the helper."""

    IAT_RAW_255 = "b71e1ff8e0000000"   # byte 2 bits 4-0 and byte 3 bits 7-5 all set
    AC_RAW_511 = "ffa0000000400000"

    def read(self, frames):
        return IntakeAcSnapshotTests.read(self, frames)

    def required_frames(self):
        return [frame(i, bytes.fromhex(h)) for i, h in REQUIRED]

    def test_raw_extremes_decode_as_expected(self):
        # Guards the fixture bytes: 255 is 215 C = 419 F, over the 302 F registry maximum.
        self.assertAlmostEqual(bs.INTAKE_AIR_TEMPERATURE.decode(bytes.fromhex(self.IAT_RAW_255)), 419.0)
        self.assertAlmostEqual(
            bs.AC_HIGH_SIDE_PRESSURE.decode(bytes.fromhex(self.AC_RAW_511)),
            (9.9 * 511 + 6) * bs.KPA_TO_PSI)

    def test_every_raw_value_is_within_registry_bounds_or_dropped(self):
        for raw in range(256):
            data = bytearray.fromhex("b71e07f8e0000000")
            data[2] = (data[2] & 0xE0) | (raw >> 3)
            data[3] = (data[3] & 0x1F) | ((raw & 7) << 5)
            for obs in cp.decode_frame_observations(0x417, bytes(data)):
                self.assertEqual(obs.value, bs.INTAKE_AIR_TEMPERATURE.decode(bytes(data)))
        for raw in range(512):
            data = bytearray.fromhex("1ca0000000400000")
            data[0] = raw >> 1
            data[1] = (data[1] & 0x7F) | ((raw & 1) << 7)
            value = bs.AC_HIGH_SIDE_PRESSURE.decode(bytes(data))
            self.assertTrue(intake_ac_metrics.in_registry_range("hvac.ac_high_side_pressure", value), raw)

    def test_implausible_intake_is_dropped_and_the_rest_of_the_snapshot_arrives(self):
        extra = [frame(0x417, bytes.fromhex(self.IAT_RAW_255)),
                 frame(0x41B, bytes.fromhex(self.AC_RAW_511))]
        _, snapshot = self.read(extra + self.required_frames())
        by_metric = {o.metric: o.value for o in snapshot.observations}
        self.assertNotIn("engine.intake_air_temperature", by_metric)
        self.assertAlmostEqual(
            by_metric["hvac.ac_high_side_pressure"], (9.9 * 511 + 6) * bs.KPA_TO_PSI)
        self.assertIn("engine.rpm", by_metric)
        for obs in snapshot.observations:
            definition = METRICS.get(obs.metric)
            if definition and definition.maximum is not None:
                self.assertLessEqual(obs.value, definition.maximum, obs.metric)

    def test_one_bad_frame_does_not_spoil_the_median_of_good_ones(self):
        good = bytes.fromhex(IAT_FRAMES[0][0])
        extra = [frame(0x417, good), frame(0x417, bytes.fromhex(self.IAT_RAW_255)),
                 frame(0x417, good)]
        _, snapshot = self.read(extra + self.required_frames())
        by_metric = {o.metric: o.value for o in snapshot.observations}
        self.assertAlmostEqual(by_metric["engine.intake_air_temperature"], 73.4)


class RollingDeployTests(unittest.TestCase):
    """Either side can be rolled back alone without the broker killing a helper.

    An old broker rejects the new sources, so a new helper reads them only when
    the broker's environment variable says so; an old helper ignores the
    variable, whereas an unknown flag would fail its parser before ``ready``.
    """

    HELPER_ARGS = ["--channel", "can0", "--expected-usb-serial", "S",
                   "--expected-dev-id", "0", "--expected-parent-pid", "1"]

    def helper_backend_kwargs(self, environ):
        built = []

        def backend(*_args, **kwargs):
            built.append(kwargs)
            raise SystemExit(0)

        with mock.patch.object(active_drive, "_set_parent_death_signal", lambda *_a: None), \
                mock.patch.object(active_drive, "SystemBackend", backend), \
                mock.patch.dict(active_drive.os.environ, environ, clear=True):
            with self.assertRaises(SystemExit):
                active_drive.main(self.HELPER_ARGS)
        return built[0]

    def test_new_helper_under_an_old_broker_reads_neither(self):
        self.assertIs(self.helper_backend_kwargs({})["include_intake_ac"], False)
        backend = active_drive.SystemBackend(
            "can0", expected_usb_serial="S", expected_dev_id=0)
        with mock.patch.object(cp, "read_broadcast_snapshot") as read:
            backend.broadcast_snapshot(0.5)
        self.assertIs(read.call_args.kwargs["include_intake_ac"], False)

    def test_new_helper_under_a_new_broker_reads_both(self):
        kwargs = self.helper_backend_kwargs({cp.INTAKE_AC_ENV: cp.INTAKE_AC_ENV_ON})
        self.assertIs(kwargs["include_intake_ac"], True)
        self.assertFalse(cp.intake_ac_enabled({cp.INTAKE_AC_ENV: "yes"}))

    def test_new_broker_launch_stays_parseable_by_an_old_helper(self):
        from projects.vehicle_data import broker
        launches = []

        class Process:
            stdout = iter(())
            pid = 1

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        supervisor = broker.ActiveDriveSupervisor(
            channel="can7", expected_usb_serial="serial-a", expected_dev_id=0,
            event_handler=lambda _event: None,
            popen_factory=lambda command, **kw: launches.append((command, kw)) or Process(),
        )
        supervisor.run(threading.Event())
        command, kwargs = launches[0]
        # No argument the pre-feature helper parser lacks.
        self.assertFalse([arg for arg in command if "intake" in arg])
        self.assertEqual(kwargs["env"][cp.INTAKE_AC_ENV], cp.INTAKE_AC_ENV_ON)

    def test_broker_allowlists_both_sources(self):
        from projects.vehicle_data import broker
        self.assertTrue(cp.INTAKE_AC_SOURCES <= broker.ACTIVE_DRIVE_SOURCES)


if __name__ == "__main__":
    unittest.main()
