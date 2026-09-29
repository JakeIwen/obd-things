"""Speed limit (0x0E0) and the ACC display (0x5A0) from real owner-annotated frames.

Frames are copied from the owner-annotated drive captures under
/mnt/EXFAT512/obd-things/tmp/captures/three_bus_drive/broker-drive/:
broker-drive-20260927T212949433889 (set speed, speed limit; see
projects/radar/findings/2026-09-27_acc_speed_limit_owner_reference.md) and
broker-drive-20260928T231134826208 (states, cruise mode, following distance,
lead vehicle; see projects/radar/findings/2026-09-28_acc_owner_reference_drive.md).
"""

import socket
import struct
import unittest
from datetime import datetime, timezone
from itertools import count

from projects.vehicle_data import ccan_powertrain as cp
from projects.vehicle_data.broker import ACTIVE_DRIVE_SOURCES, TelemetryBroker
from projects.vehicle_data.metrics import METRICS
from projects.vehicle_data.models import success


# Owner-noted speed-limit changes, 0x0E0 (DLC 4), first frame after each change.
SPEED_LIMIT_FRAMES = (
    ("21:44:33.123880Z 45->30", "1E440000", 30),
    ("21:45:17.823640Z 30->45", "2D440000", 45),
    ("21:45:50.945761Z 45->55", "37440000", 55),
    ("21:52:44.956126Z 55->45", "2D440000", 45),
)
# Key-off / no limit shown (parked capture broker-drive-20260927T205938144339).
SPEED_LIMIT_NONE = "00440000"

# 0x5A0 frames around the owner's "4:41 set to 61, +1 to 66" (CDT) note.
ACC_OFF = "2D0000003F3C0400"  # 21:29:50.634171Z state 0
ACC_READY = "2D390000033C1440"  # 21:41:28.077076Z ACC on, no set speed yet
ACC_SET_61 = "2D41623D033C3480"  # 21:41:43.357703Z first SET+, engaged 61 mph / 98 km/h
ACC_OVERRIDE_61 = "2D4A623D033C6900"  # 21:41:43.438056Z accelerator override
ACC_SET_66 = "2D006A42033C3480"  # 21:42:22.919304Z fifth +1 press, 66 mph / 106 km/h
ACC_STANDBY_66 = "2D466A42033C4540"  # 21:43:08.599279Z cancelled, 66 in memory

# One contiguous real C-CAN window, 1790545342.801593-.941643 (21:42:22.80-.94Z),
# filtered to the snapshot's kernel allowlist, in arrival order.
REAL_WINDOW = """
100#5582AD5600000A35 101#00CF000000080411 0FC#2264C7E20855BB49 100#5582AD5600000B28
1F7#0188A0F011240650 101#00CF00000008050C 41D#00003500000000FA 0FC#2270C7E20855FC18
100#5582AF5600000CAF 101#00CF00000008062B 2ED#821FC00000D1D000 0FC#226CC7E20855FD1E
100#558AAF5620000D88 1F7#0188A0F0112A07A2 101#00CF2000000807F7 2EF#0F25000000000000
0FC#2278C7E20855FE28 100#558AAF5620000EAF 101#00CF20000008084C 0FC#2260C7E20855FF2B
100#558AB05620000F20 1F7#018860F0112208FF 101#00CF200000080951 0FC#224CC7E2085610D1
100#558AAE5620000063 101#00CF200000080A76 41A#C8000000000000 0FC#226CC7E20855D105
100#5592B056400001C8 1F7#018880F01122099F 101#00CF200000080B6B 2ED#821FC00000D1D000
0FC#2264C7E2085612C9 0E0#37440000 100#5592AF564000027D 101#00CF400000080C66
2EF#0F25000000000000 0FC#2258C7E20855F381 100#5592B056400003F2 1F7#018800F011220A9B
101#00CF400000080D7B 0FC#2270C7E208561496 100#5592B056400004A1 101#00CF400000080E5C
0FC#2270C7E20856158B 100#558AB056200005F2 1F7#018840F011280BDB 101#00CF400000080F41
41D#00003500000001E7 0FC#2268C7E2085616B2 5A0#2D006A42033C3480 100#558AB3562000066B
101#00CF4000000800FA 2ED#821FC00000D92000 0FC#2278C7E20856772F 100#558AB2562000071C
"""


def raw_frame(can_id, data):
    payload = bytes(data)
    return struct.pack("=IB3x8s", can_id, len(payload), payload.ljust(8, b"\0"))


def window_frames(text=REAL_WINDOW):
    frames = []
    for token in text.split():
        can_id, data = token.split("#")
        frames.append(raw_frame(int(can_id, 16), bytes.fromhex(data)))
    return frames


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.filters = None

    def setsockopt(self, _level, _option, value):
        self.filters = value

    def bind(self, _address):
        pass

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if not self.frames:
            raise socket.timeout
        return self.frames.pop(0)

    def close(self):
        pass


def snapshot(frames, *, timeout=0.5, include_battery=False, display_wait=None, start=0.0):
    fake = FakeSocket(frames)
    ticks = count(start=start, step=0.001)
    result = cp.read_broadcast_snapshot(
        "can7",
        timeout=timeout,
        include_battery=include_battery,
        socket_factory=lambda *_args: fake,
        monotonic=lambda: next(ticks),
        display_wait=display_wait,
    )
    return result, fake


def by_metric(observations):
    return {item.metric: item for item in observations}


class SpeedLimitDecodeTests(unittest.TestCase):
    def test_owner_noted_changes_decode_to_the_displayed_limit(self):
        for label, data, mph in SPEED_LIMIT_FRAMES:
            with self.subTest(label):
                (observation,) = cp.decode_frame_observations(0x0E0, bytes.fromhex(data))
                self.assertEqual(observation.metric, "vehicle.speed_limit")
                self.assertEqual(observation.value, mph)
                self.assertIs(type(observation.value), int)
                self.assertEqual(observation.unit, "mph")
                self.assertEqual(observation.source, "ccan.broadcast.0x0e0")
                self.assertEqual(observation.quality, "verified")

    def test_zero_means_no_limit_and_is_never_published(self):
        self.assertEqual(cp.decode_frame_observations(0x0E0, bytes.fromhex(SPEED_LIMIT_NONE)), ())
        self.assertEqual(cp.decode_frame_observations(0x0E0, b""), ())
        self.assertEqual(cp.decode_frame_observations(0x0E0, b"\xff\x44\x00\x00"), ())


class AccDisplayDecodeTests(unittest.TestCase):
    def decode(self, hexdata):
        return by_metric(cp.decode_frame_observations(0x5A0, bytes.fromhex(hexdata)))

    def test_state_enum_on_real_frames(self):
        cases = (
            (ACC_OFF, "off"),
            (ACC_READY, "ready"),
            (ACC_SET_61, "engaged"),
            (ACC_OVERRIDE_61, "override"),
            (ACC_SET_66, "engaged"),
            (ACC_STANDBY_66, "standby"),
        )
        for data, state in cases:
            with self.subTest(data):
                observation = self.decode(data)["acc.state"]
                self.assertEqual(observation.value, state)
                self.assertEqual(observation.quality, "verified")
                self.assertEqual(observation.unit, "state")

    def test_set_speed_matches_owner_display_only_while_ready_engaged_or_standby(self):
        self.assertEqual(self.decode(ACC_SET_61)["acc.set_speed"].value, 61)
        self.assertEqual(self.decode(ACC_OVERRIDE_61)["acc.set_speed"].value, 61)
        self.assertEqual(self.decode(ACC_SET_66)["acc.set_speed"].value, 66)
        self.assertEqual(self.decode(ACC_STANDBY_66)["acc.set_speed"].value, 66)
        set_speed = self.decode(ACC_SET_66)["acc.set_speed"]
        self.assertIs(type(set_speed.value), int)
        self.assertEqual((set_speed.unit, set_speed.quality), ("mph", "verified"))
        # Off, and ready before the first SET (byte 3 = 0), publish no set speed.
        self.assertNotIn("acc.set_speed", self.decode(ACC_OFF))
        self.assertNotIn("acc.set_speed", self.decode(ACC_READY))

    def test_off_state_with_a_remembered_speed_still_publishes_none(self):
        frame = bytearray.fromhex(ACC_SET_66)
        frame[6] &= 0xFE  # state bits -> 0 (off)
        frame[7] &= 0x3F
        decoded = self.decode(frame.hex())
        self.assertEqual(decoded["acc.state"].value, "off")
        self.assertNotIn("acc.set_speed", decoded)

    def test_unmapped_state_and_inconsistent_kmh_are_withheld(self):
        frame = bytearray.fromhex(ACC_SET_66)
        frame[6] = (frame[6] & 0xFE) | 0x00
        frame[7] = (frame[7] & 0x3F) | 0xC0  # raw 3, seen once for 80 ms at CANC
        self.assertEqual(self.decode(frame.hex()), {})
        mismatched = bytearray.fromhex(ACC_SET_66)
        mismatched[2] = 90  # 66 mph is 106 km/h, not 90
        decoded = self.decode(mismatched.hex())
        self.assertEqual(decoded["acc.state"].value, "engaged")
        self.assertNotIn("acc.set_speed", decoded)
        self.assertEqual(cp.decode_frame_observations(0x5A0, bytes.fromhex(ACC_SET_66)[:7]), ())


# Real 0x5A0 frames of the owner-annotated 2026-09-28 drive (UTC), one per
# display the owner called out.  Each row: (note, frame, state, mode, bars,
# lead vehicle, set speed); None means the metric is not published.
CALLOUT_FRAMES = (
    # 7:00 PM CDT: distance stepped one bar to four and back, 5 s apart
    ("00:00:07.916Z distance+ to two bars", "2D3F7549033C2C80", "engaged", "adaptive", 2, False, 73),
    ("00:00:15.036Z distance+ to three bars", "2D407549033C3080", "engaged", "adaptive", 3, False, 73),
    ("00:00:21.395Z distance+ to four bars", "2D417549033C3480", "engaged", "adaptive", 4, False, 73),
    ("00:00:40.315Z distance- back at one bar", "2D007549033C2880", "engaged", "adaptive", 1, False, 73),
    # 7:01 PM: ACC off, then on again
    ("23:11:35.628Z ACC off", "2D0000003F3C0400", "off", None, None, None, None),
    ("00:01:58.754Z ACC on, ready", "2D000000033C0840", "ready", "adaptive", 1, None, None),
    # 7:05 PM: regular cruise on, set 65, cancel, off
    ("00:05:00.959Z fixed cruise on", "2D000000033C7040", "ready", "fixed", None, None, None),
    ("00:05:04.369Z fixed cruise set 65", "2D006941033C7480", "engaged", "fixed", None, None, 65),
    ("00:05:04.319Z fixed cruise, accelerator", "2D546941033C8100", "override", "fixed", None, None, 65),
    ("00:05:19.118Z fixed cruise cancelled", "2D006941033C6D40", "standby", "fixed", None, None, 65),
    ("00:05:27.119Z fixed cruise off", "2D000000033C7C00", "off", None, None, None, None),
    # 7:08 PM: accelerator override, set speed blinking
    ("00:08:11.881Z gas override", "2D005D3A033C5D00", "override", "adaptive", 1, False, 58),
    ("00:19:14.055Z gas override at four bars", "2D4A653F033C6900", "override", "adaptive", 4, False, 63),
    # 7:10 PM: cancelled, then RES back to 61
    ("23:18:05.055Z cancelled at one bar", "2D00643E033C3940", "standby", "adaptive", 1, None, 62),
    ("00:11:56.287Z cancelled at four bars", "2D00623D033C4540", "standby", "adaptive", 4, None, 61),
    # 7:11 PM and 7:44-7:47 PM: vehicle ahead, icon shown
    ("23:30:48.589Z vehicle ahead at one bar", "2D00653F033C1880", "engaged", "adaptive", 1, True, 63),
    ("00:44:56.133Z vehicle ahead at two bars", "2D006A42033C1C80", "engaged", "adaptive", 2, True, 66),
    ("00:11:31.649Z vehicle ahead at three bars", "2D3C623D033C2080", "engaged", "adaptive", 3, True, 61),
    ("00:11:36.249Z vehicle ahead at four bars", "2D00623D033C2480", "engaged", "adaptive", 4, True, 61),
    ("23:30:48.509Z override with a vehicle ahead", "2D4A653F033C9500", "override", "adaptive", 1, True, 63),
    ("00:48:23.979Z override, vehicle ahead, four bars", "2D4A4028033CA100", "override", "adaptive", 4, True, 40),
)
# Raw state 3 for 80-240 ms while cancelling or switching off: nothing is published.
TRANSITIONAL_FRAMES = ("2D350000033C04C0", "2D46623D033C38C0", "2D46643E033C44C0", "2D4F6941033C6CC0")


class AccCalloutDecodeTests(unittest.TestCase):
    def decode(self, hexdata):
        return by_metric(cp.decode_frame_observations(0x5A0, bytes.fromhex(hexdata)))

    def test_every_owner_callout_frame_decodes_to_what_the_cluster_showed(self):
        names = ("acc.state", "acc.mode", "acc.follow_distance", "acc.lead_vehicle", "acc.set_speed")
        for note, frame, *expected in CALLOUT_FRAMES:
            with self.subTest(note):
                decoded = self.decode(frame)
                for name, value in zip(names, expected):
                    if value is None:
                        self.assertNotIn(name, decoded)
                    else:
                        self.assertEqual(decoded[name].value, value)
                        self.assertIs(type(decoded[name].value), type(value))
                        self.assertEqual(decoded[name].quality, "verified")
                        self.assertEqual(decoded[name].source, "ccan.broadcast.0x5a0")

    def test_units_and_registry_values(self):
        decoded = self.decode("2D00623D033C2480")
        self.assertEqual(decoded["acc.mode"].unit, "mode")
        self.assertEqual(decoded["acc.follow_distance"].unit, "bars")
        self.assertEqual(decoded["acc.lead_vehicle"].unit, "boolean")
        for _note, frame, *_expected in CALLOUT_FRAMES:
            for observation in cp.decode_frame_observations(0x5A0, bytes.fromhex(frame)):
                source = METRICS[observation.metric].sources[0]
                self.assertEqual(observation.unit, METRICS[observation.metric].unit)
                if source.publisher_values is not None:
                    self.assertIn(observation.value, source.publisher_values)

    def test_transitional_frames_publish_nothing(self):
        for frame in TRANSITIONAL_FRAMES:
            with self.subTest(frame):
                self.assertEqual(self.decode(frame), {})

    def test_unknown_state_and_index_pairs_publish_only_the_state(self):
        # Index 20 has never been seen; index 10 belongs to engaged, not ready.
        for index, state_bits in ((20, (0, 0x80)), (10, (0, 0x40))):
            frame = bytearray.fromhex("2D00623D033C2880")
            frame[6] = (index << 2) | state_bits[0]
            frame[7] = state_bits[1]
            decoded = self.decode(frame.hex())
            with self.subTest(index=index):
                self.assertIn("acc.state", decoded)
                for name in ("acc.mode", "acc.follow_distance", "acc.lead_vehicle"):
                    self.assertNotIn(name, decoded)

    def test_family_table_has_no_overlap_and_matches_the_observed_indices(self):
        seen = {}
        for state, first, length, mode, _lead in cp.ACC_HUD_FAMILIES:
            self.assertIn(state, cp.ACC_STATE_NAMES.values())
            self.assertIn(mode, cp.ACC_MODE_NAMES)
            for index in range(first, first + length):
                self.assertNotIn(index, seen, f"index {index} listed twice")
                seen[index] = state
        self.assertEqual(sorted(seen), [*range(2, 18), *range(23, 30), 32, *range(37, 41)])
        self.assertEqual(cp.acc_display_family("engaged", 9), ("adaptive", True, 4))
        self.assertEqual(cp.acc_display_family("engaged", 10), ("adaptive", False, 1))
        self.assertEqual(cp.acc_display_family("engaged", 29), ("fixed", None, None))
        self.assertIsNone(cp.acc_display_family("off", 1))
        self.assertIsNone(cp.acc_display_family("off", 31))
        self.assertIsNone(cp.acc_display_family(None, 10))


class DisplaySnapshotTests(unittest.TestCase):
    def test_real_window_without_display_wait_ends_before_the_speed_limit(self):
        # 0x0E0 trails 0x41D and 0x2EF in every 100 ms cycle, so an early-ending
        # snapshot misses it by phase, not by chance.
        result, fake = snapshot(window_frames())
        metrics = by_metric(result.observations)
        self.assertNotIn("vehicle.speed_limit", metrics)
        self.assertNotIn("acc.set_speed", metrics)
        self.assertIn("vehicle.speed", metrics)
        self.assertTrue(fake.frames, "snapshot ended early")

    def test_real_window_with_display_wait_publishes_limit_set_speed_and_state(self):
        wait = cp.LowRateFrameWait()
        result, fake = snapshot(window_frames(), display_wait=wait)
        metrics = by_metric(result.observations)
        self.assertEqual(metrics["vehicle.speed_limit"].value, 55)
        self.assertEqual(metrics["acc.set_speed"].value, 66)
        self.assertEqual(metrics["acc.state"].value, "engaged")
        self.assertEqual(metrics["transmission.gear_estimate"].value, "7")
        self.assertAlmostEqual(metrics["vehicle.speed"].value, 64.35, places=2)
        self.assertIn("latest of 1 frame(s)", metrics["acc.set_speed"].detail)
        # It stopped as soon as both display frames had arrived.
        self.assertEqual(len(fake.frames), 5)
        # Both were just seen, so the next snapshot waits only for 0x0E0.
        self.assertEqual(wait.wanted(result.completed_monotonic + 0.5), frozenset({0x0E0}))

    def test_display_frames_publish_the_latest_frame_not_a_median(self):
        frames = window_frames(REAL_WINDOW.replace("5A0#2D006A42033C3480", ""))
        off = bytearray.fromhex(ACC_SET_66)
        off[6] &= 0xFE
        off[7] &= 0x3F
        # Engaged at 66, then off in the same window: no stale set speed.
        frames.insert(2, raw_frame(0x5A0, bytes.fromhex(ACC_SET_66)))
        frames.insert(10, raw_frame(0x5A0, bytes(off)))
        frames.insert(3, raw_frame(0x0E0, bytes.fromhex("2D440000")))
        frames.insert(6, raw_frame(0x0E0, bytes.fromhex("1E440000")))
        result, _fake = snapshot(frames, display_wait=cp.LowRateFrameWait())
        metrics = by_metric(result.observations)
        self.assertEqual(metrics["acc.state"].value, "off")
        self.assertNotIn("acc.set_speed", metrics)
        # 45 then 30: a two-sample median would report 45.
        self.assertEqual(metrics["vehicle.speed_limit"].value, 30)
        self.assertIn("latest of 2 frame(s)", metrics["vehicle.speed_limit"].detail)

    def test_filters_include_display_frames(self):
        _result, fake = snapshot([])
        ids = [
            struct.unpack("=II", fake.filters[offset : offset + 8])[0]
            for offset in range(0, len(fake.filters), 8)
        ]
        self.assertIn(0x0E0, ids)
        self.assertIn(0x5A0, ids)


class LowRateFrameWaitTests(unittest.TestCase):
    def test_refresh_intervals(self):
        wait = cp.LowRateFrameWait()
        self.assertEqual(wait.wanted(0.0), frozenset({0x0E0, 0x5A0}))
        wait.finish(frozenset({0x0E0, 0x5A0}), {0x0E0: 0.1, 0x5A0: 0.2}, extended_to_deadline=False, now=0.3)
        self.assertEqual(wait.wanted(1.0), frozenset({0x0E0}))
        self.assertEqual(wait.wanted(2.2), frozenset({0x0E0, 0x5A0}))

    def test_absent_frame_backs_off_only_after_misses_and_time(self):
        wait = cp.LowRateFrameWait(refresh_seconds={0x5A0: 2.0})
        now = 0.0
        # Six extended misses in under ten seconds: a 1 Hz frame can do that by chance.
        for _ in range(6):
            wait.finish(frozenset({0x5A0}), {}, extended_to_deadline=True, now=now)
            now += 1.0
        wait.finish(frozenset({0x5A0}), {0x5A0: now}, extended_to_deadline=False, now=now)
        self.assertEqual(wait.wanted(now + 2.0), frozenset({0x5A0}))
        # Then absent for more than ten seconds and six extended waits: back off.
        start = now
        for _ in range(12):
            now += 1.0
            wait.finish(frozenset({0x5A0}), {}, extended_to_deadline=True, now=now)
        self.assertGreaterEqual(now - start, 10.0)
        self.assertEqual(wait.wanted(now + 1.0), frozenset())
        self.assertEqual(wait.wanted(now + 31.0), frozenset({0x5A0}))
        # An incidental sighting during the back-off clears it.
        wait.finish(frozenset(), {0x5A0: now + 2.0}, extended_to_deadline=False, now=now + 2.0)
        self.assertEqual(wait.wanted(now + 4.1), frozenset({0x5A0}))

    def test_snapshot_that_ended_for_other_reasons_is_not_a_miss(self):
        wait = cp.LowRateFrameWait(refresh_seconds={0x5A0: 2.0}, misses_before_backoff=1, absent_seconds=0.0)
        wait.finish(frozenset({0x5A0}), {}, extended_to_deadline=False, now=5.0)
        self.assertEqual(wait.wanted(5.1), frozenset({0x5A0}))
        wait.finish(frozenset({0x5A0}), {}, extended_to_deadline=True, now=6.0)
        self.assertEqual(wait.wanted(6.1), frozenset())

    def test_bus_asleep_snapshot_does_not_count_a_miss(self):
        wait = cp.LowRateFrameWait(misses_before_backoff=1, absent_seconds=0.0)
        for start in (0.0, 1.0, 2.0):
            snapshot([], display_wait=wait, start=start)
        self.assertEqual(wait.wanted(3.0), frozenset({0x0E0, 0x5A0}))


class FakeAcquirer:
    channel = "can7"

    def acquire(self, mode):
        return success(
            metric="battery.voltage",
            unit="V",
            value=12.5,
            source="bcan.broadcast.0x46c",
            bus="b-can",
            acquisition=mode,
            quality="verified",
            observed_monotonic=100.0,
            observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )


class ActiveDriveAllowlistRegressionTests(unittest.TestCase):
    """2026-09-24: a snapshot source missing from ACTIVE_DRIVE_SOURCES made the
    broker reject the helper's forwarded observation and latch restoration_failed."""

    def active_snapshot(self):
        result, _fake = snapshot(
            window_frames(),
            include_battery=True,
            display_wait=cp.LowRateFrameWait(),
        )
        return result

    def test_every_snapshot_source_is_in_the_active_drive_allowlist(self):
        possible = {f"ccan.broadcast.0x{can_id:03x}" for can_id in cp.ACTIVE_FILTER_IDS}
        possible.add(cp.GEAR_ESTIMATE_SOURCE)
        self.assertEqual(possible - ACTIVE_DRIVE_SOURCES, set())
        observations = self.active_snapshot().observations
        emitted = {item.source for item in observations}
        # The real window exercises every kernel-filtered identifier and the gear.
        self.assertEqual(emitted, possible)
        self.assertEqual(emitted - ACTIVE_DRIVE_SOURCES, set())

    def test_broker_accepts_every_forwarded_snapshot_observation(self):
        broker = TelemetryBroker(acquirer=FakeAcquirer())
        for observation in self.active_snapshot().observations:
            with self.subTest(observation.metric):
                # Same fields active_drive._emit_observation sends.
                broker._store_active_observation(
                    {
                        "metric": observation.metric,
                        "value": observation.value,
                        "unit": observation.unit,
                        "source": observation.source,
                        "bus": "c-can",
                        "quality": observation.quality,
                        "detail": observation.detail,
                    }
                )
                live = broker.metric_response(observation.metric)
                self.assertTrue(live["available"], live)
                self.assertEqual(live["value"], observation.value)
        self.assertEqual(broker.metric_response("acc.set_speed")["value"], 66)
        self.assertEqual(broker.metric_response("vehicle.speed_limit")["value"], 55)
        self.assertEqual(broker.metric_response("acc.state")["value"], "engaged")

    def test_broker_passive_publish_accepts_display_observations(self):
        broker = TelemetryBroker(acquirer=FakeAcquirer())
        for observation in self.active_snapshot().observations:
            if observation.source not in ("ccan.broadcast.0x0e0", "ccan.broadcast.0x5a0"):
                continue
            result = broker.publish_observation(
                observation.metric,
                value=observation.value,
                unit=observation.unit,
                source=observation.source,
                bus="c-can",
                quality=observation.quality,
            )
            self.assertTrue(result.available, result)


class RegistryTests(unittest.TestCase):
    def test_display_metrics_are_registered_with_matching_sources(self):
        limit = METRICS["vehicle.speed_limit"]
        self.assertEqual((limit.unit, limit.value_type), ("mph", "integer"))
        self.assertEqual(limit.sources[0].name, "ccan.broadcast.0x0e0")
        self.assertEqual(limit.sources[0].quality, "verified")
        set_speed = METRICS["acc.set_speed"]
        self.assertEqual((set_speed.unit, set_speed.value_type), ("mph", "integer"))
        self.assertEqual(set_speed.sources[0].name, "ccan.broadcast.0x5a0")
        self.assertGreaterEqual(set_speed.stale_after_seconds, 3.0)
        state = METRICS["acc.state"]
        self.assertEqual(state.value_type, "string")
        self.assertEqual(state.sources[0].quality, "verified")
        self.assertEqual(set(state.sources[0].publisher_values), set(cp.ACC_STATE_NAMES.values()))
        mode = METRICS["acc.mode"]
        self.assertEqual((mode.unit, mode.value_type), ("mode", "string"))
        self.assertEqual(mode.sources[0].publisher_values, cp.ACC_MODE_NAMES)
        distance = METRICS["acc.follow_distance"]
        self.assertEqual((distance.unit, distance.value_type), ("bars", "integer"))
        self.assertEqual((distance.minimum, distance.maximum), (1, 4))
        self.assertEqual(distance.sources[0].publisher_values, cp.ACC_FOLLOW_DISTANCE_BARS)
        lead = METRICS["acc.lead_vehicle"]
        self.assertEqual((lead.unit, lead.value_type), ("boolean", "boolean"))
        self.assertEqual(lead.sources[0].publisher_values, (True, False))
        for metric in (mode, distance, lead):
            self.assertEqual(metric.sources[0].name, "ccan.broadcast.0x5a0")
            self.assertEqual(metric.sources[0].quality, "verified")
            self.assertEqual(metric.stale_after_seconds, set_speed.stale_after_seconds)
        for metric in (limit, set_speed, state, mode, distance, lead):
            source = metric.sources[0]
            self.assertEqual((source.bus, source.acquisition_class), ("c-can", "passive_broadcast"))
            self.assertTrue(source.publisher_allowed)


if __name__ == "__main__":
    unittest.main()
