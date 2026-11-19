"""Below-empty fuel gauge: episodes, the floor, the owner record, the CLI and the broker wiring."""

import importlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.broadcast_signals import FUEL_RATE_CLUSTER_FACTOR
from projects.vehicle_data import bcan_companion, compass_receiver as cr, display_receiver as dr
from projects.vehicle_data import fuel_floor_model
from projects.vehicle_data.models import success
from projects.vehicle_data import fuel_floor, fuel_floor_contract as C, fuel_floor_store, fuel_level
from projects.vehicle_data.broker import TelemetryBroker
from projects.vehicle_data.fuel_floor_model import FuelFloorModel, rescale
from projects.vehicle_data.fuel_floor_store import FuelFloorStore
from tests.test_vehicle_data import FakeAcquirer, FakeClock

WALL = "2026-11-19T10:00:00+00:00"
GPP = C.GAL_PER_POINT


def burn(model, gallons, start=1000.0, rate=3.5):
    """Feed fresh 1 Hz running samples until about ``gallons`` have burned; the next mono time."""
    mono = start
    model.observe_fuel_rate(rate, mono, True)
    while model.episode is not None and model.episode.gallons < gallons - 1e-9 and mono - start < 4000:
        mono += 1.0
        # A real rate is never bit-identical for half a minute (that is what "frozen" means).
        model.observe_fuel_rate(rate + 0.001 * (int(mono) % 2), mono, True)
    return mono


def open_episode(model, start=0.0, hold=None):
    """A genuine approach (engine running, 3 -> 2 -> 1 -> 0) held until the candidate confirms.

    Returns the monotonic time of the first 0. ``hold`` overrides the seconds of 0 fed in.
    """
    for step, level in enumerate((3, 2, 1)):
        model.observe_engine(True, start + step)
        model.observe_van_level(level, start + step, WALL)
    zero = start + 3.0
    for second in range(int(C.CANDIDATE_HOLD_SECONDS if hold is None else hold) + 1):
        model.observe_engine(True, zero + second)
        model.observe_van_level(0, zero + second, f"zero+{second}" if second else "zero-first")
    return zero


def store_episode(store, start=0.0):
    """Open an episode through a store the way the receiver and broker would feed it; the end time."""
    for step, level in enumerate((3, 2, 1)):
        store.observe_engine(780, start + step)
        store.observe_van_level(level, start + step)
    for second in range(int(C.CANDIDATE_HOLD_SECONDS) + 1):
        store.observe_engine(780, start + 3 + second)
        store.observe_van_level(0, start + 3 + second)
    return start + 3 + C.CANDIDATE_HOLD_SECONDS


class ContractTests(unittest.TestCase):
    def test_points_are_tied_to_the_fuel_rate_scale(self):
        self.assertAlmostEqual(GPP, 0.175 * FUEL_RATE_CLUSTER_FACTOR / 1.131)
        try:
            with mock.patch("lib.broadcast_signals.FUEL_RATE_CLUSTER_FACTOR", 1.2):
                importlib.reload(C)
                self.assertAlmostEqual(C.GAL_PER_POINT, 0.175 * 1.2 / 1.131)
        finally:
            importlib.reload(C)
        self.assertAlmostEqual(C.GAL_PER_POINT, GPP)

    def test_rescale_makes_the_floor_zero_and_keeps_one_hundred(self):
        self.assertEqual(rescale(40, 0), 40)
        self.assertEqual(rescale(-6, -6), 0)
        self.assertEqual(rescale(100, -6), 100)
        self.assertAlmostEqual(rescale(10, -10), 20 / 110 * 100)
        self.assertLess(rescale(-8, -6), 0)


class EpisodeTests(unittest.TestCase):
    def test_a_genuine_approach_opens_the_episode_only_after_the_hold(self):
        model = FuelFloorModel()
        for step, level in enumerate((3, 2, 1)):
            model.observe_engine(True, float(step))
            self.assertFalse(model.observe_van_level(level, float(step), WALL))
        for second in range(0, 30):
            model.observe_engine(True, 3.0 + second)
            self.assertFalse(model.observe_van_level(0, 3.0 + second, f"w{second}"))
            if second:  # a candidate: burned fuel is held in memory, nothing shows or saves
                model.observe_fuel_rate(3.6 + 0.001 * (second % 2), 3.0 + second, True)
            self.assertIsNone(model.episode)
            self.assertIsNone(model.open_view())
            self.assertFalse(model.dirty)
            self.assertIsNone(model.to_record()["open_episode"])
        model.observe_engine(True, 33.0)
        self.assertTrue(model.observe_van_level(0, 33.0, "w30"))
        self.assertEqual(model.episode.started_at, "w0")  # the first 0's receipt time
        self.assertGreater(model.episode.gallons, 0.0)    # fuel burned since the first 0 counts
        self.assertEqual(model.open_view()["raw"], round(-model.episode.gallons / GPP, 2))

    def test_the_wake_sentinel_60_then_0_then_99_leaves_no_trace(self):
        model = FuelFloorModel()
        model.observe_engine(True, 0.0)
        model.observe_van_level(60, 0.0, WALL)
        self.assertFalse(model.observe_van_level(0, 1.0, WALL))
        self.assertFalse(model.observe_van_level(99, 3.2, WALL))
        self.assertEqual((model.episode, model.pending, model.dirty, model.episodes), (None, None, False, []))
        # Even a long 0 with the engine running is not an episode after a reading of 60.
        model.observe_van_level(60, 10.0, WALL)
        for second in range(0, 90):
            model.observe_engine(True, 11.0 + second)
            model.observe_van_level(0, 11.0 + second, WALL)
        self.assertEqual((model.episode, model.floor["value"], model.episodes), (None, 0, []))

    def test_the_key_off_sentinel_with_the_engine_stopping_leaves_no_trace(self):
        model = FuelFloorModel()
        model.observe_engine(True, 0.0)
        model.observe_van_level(3, 0.0, WALL)      # worst case: a low reading just before
        model.observe_van_level(0, 1.0, WALL)
        model.observe_van_level(0, 2.0, WALL)
        model.observe_engine(False, 3.0)           # the engine stops
        for second in range(4, 100):
            model.observe_van_level(0, float(second), WALL)
        self.assertEqual((model.episode, model.dirty, model.episodes), (None, False, []))

    def test_a_candidate_needs_a_recent_low_reading_and_unbroken_zeros(self):
        model = FuelFloorModel()
        model.observe_engine(True, 0.0)
        model.observe_van_level(1, 0.0, WALL)
        model.observe_engine(True, 1000.0)
        for second in range(0, 40):               # the last reading above 0 was 1000 s ago
            model.observe_van_level(0, 1000.0 + second, WALL)
        self.assertIsNone(model.episode)
        model.observe_van_level(2, 2000.0, WALL)
        for second in range(1, 20):               # a silent receiver breaks "no reading in between"
            model.observe_engine(True, 2000.0 + second)
            model.observe_van_level(0, 2000.0 + second, WALL)
        model.observe_engine(True, 2040.0)
        model.observe_van_level(0, 2040.0, WALL)
        model.observe_engine(True, 2050.0)
        self.assertFalse(model.observe_van_level(0, 2050.0, WALL))
        self.assertIsNone(model.episode)
        model.observe_van_level(1, 2051.0, WALL)  # any reading above 0 cancels a candidate
        self.assertIsNone(model.pending)

    def test_a_zero_with_no_engine_evidence_confirms_nothing(self):
        model = FuelFloorModel()
        model.observe_van_level(2, 0.0, WALL)     # no engine evidence at all
        for second in range(1, 60):
            model.observe_van_level(0, float(second), WALL)
        self.assertEqual((model.episode, model.pending), (None, None))

    def test_gallons_become_negative_points_and_the_floor_waits_for_the_end(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 0.7)
        view = model.open_view()
        self.assertAlmostEqual(view["raw"], -0.7 / GPP, delta=0.1)
        self.assertLess(view["adjusted"], 0)
        self.assertEqual(model.floor["value"], 0)  # the floor in force is the one from before
        deepest = model.episode.deepest_raw
        self.assertTrue(model.observe_van_level(30, 5000.0, "2026-11-19T11:00:00+00:00"))
        self.assertIsNone(model.episode)
        self.assertEqual(model.floor["value"], round(deepest, 2))
        self.assertEqual(model.floor["source"], C.EPISODE)
        row, = model.episodes
        self.assertEqual((row["end_reason"], row["van_reading_at_end"], row["floor_committed"]),
                         (C.REFUEL, 30, True))
        self.assertEqual(model.earlier_floors[0]["value"], 0)

    def test_a_shallower_episode_never_raises_the_floor(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 1.0)
        model.observe_van_level(50, 5000.0, WALL)
        floor = model.floor["value"]
        open_episode(model, 6000.0)
        burn(model, 0.4, start=7000.0)
        model.observe_van_level(50, 9000.0, WALL)
        self.assertEqual(model.floor["value"], floor)
        self.assertEqual([row["floor_committed"] for row in model.episodes], [False, True])

    def test_the_gauge_deepens_the_floor_across_episodes_and_displays_from_it(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 0.35)
        model.observe_van_level(50, 5000.0, WALL)
        first = model.floor["value"]
        self.assertAlmostEqual(first, -2, delta=0.2)
        open_episode(model, 6000.0)
        self.assertEqual(model.open_view()["adjusted"], round(rescale(0, first), 2))  # van zero is above the floor
        burn(model, 0.7, start=7000.0)
        self.assertLess(model.open_view()["raw"], first)
        self.assertLess(model.open_view()["adjusted"], 0)

    def test_recovery_needs_a_continuous_minute_at_one_or_more_and_slosh_does_not_end_it(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 0.2)
        for second in range(0, 59):  # readings of 1 and 2 for 58 s
            model.observe_van_level(1 + second % 2, 100.0 + second, WALL)
        self.assertIsNotNone(model.episode)
        model.observe_van_level(0, 160.0, WALL)  # slosh back to 0 restarts the hold
        for second in range(0, 60):
            self.assertFalse(model.observe_van_level(2, 161.0 + second, WALL))
        self.assertIsNotNone(model.episode)
        self.assertTrue(model.observe_van_level(2, 221.0, WALL))
        self.assertEqual(model.episodes[0]["end_reason"], C.RECOVERED)

    def test_a_silent_receiver_restarts_the_hold_but_does_not_end_or_extend_it(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 0.2)
        model.observe_van_level(2, 100.0, WALL)
        self.assertFalse(model.observe_van_level(2, 170.0, WALL))  # 70 s apart: not continuous
        self.assertIsNotNone(model.episode)
        self.assertFalse(model.observe_van_level(2, 230.0, WALL))   # 60 s since the restart, but a gap hid it
        for second in range(231, 289):
            self.assertFalse(model.observe_van_level(2, float(second), WALL))
        self.assertTrue(model.observe_van_level(2, 290.0, WALL))   # a full minute of readings since 230

    def test_dead_reckoning_goes_on_with_no_van_readings_at_all(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 0.5)
        self.assertIsNotNone(model.episode)
        self.assertGreater(model.episode.gallons, 0.49)

    def test_the_sanity_bound_holds_and_commits_no_floor(self):
        model = FuelFloorModel()
        open_episode(model)
        burn(model, 6.0, rate=20.0)
        view = model.open_view()
        self.assertEqual(view["raw"], -30)
        self.assertTrue(view["implausible"])
        burn(model, 9.0, start=9000.0, rate=20.0)
        self.assertEqual(model.open_view()["raw"], -30)
        model.observe_van_level(40, 20000.0, WALL)
        self.assertEqual(model.floor["value"], 0)
        self.assertTrue(model.episodes[0]["implausible"])
        self.assertFalse(model.episodes[0]["floor_committed"])

    def test_fuel_adds_only_for_fresh_running_samples_within_the_gap_rule(self):
        model = FuelFloorModel()
        open_episode(model)
        model.observe_fuel_rate(3.6, 10.0, True)
        model.observe_fuel_rate(3.6, 10.0, True)       # the same observation read again
        model.observe_fuel_rate(3.6, 9.0, True)        # an older one
        self.assertEqual(model.episode.gallons, 0)
        model.observe_fuel_rate(3.6, 11.0, True)       # 1 s at 3.6 gal/h = 0.001 gal
        self.assertAlmostEqual(model.episode.gallons, 0.001)
        model.observe_fuel_rate(3.6, 30.0, True)       # a 19 s gap contributes nothing
        self.assertAlmostEqual(model.episode.gallons, 0.001)
        model.observe_fuel_rate(3.6, 31.0, False)      # engine off: no fuel, and the chain breaks
        model.observe_fuel_rate(3.6, 32.0, True)
        self.assertAlmostEqual(model.episode.gallons, 0.001)
        model.observe_fuel_rate(float("nan"), 33.0, True)
        model.observe_fuel_rate(-1.0, 34.0, True)
        self.assertAlmostEqual(model.episode.gallons, 0.001)

    def test_a_frozen_value_with_the_engine_reported_running_adds_nothing(self):
        model = FuelFloorModel()
        open_episode(model)
        for second in range(0, 600):  # the 2026-11-18 evening history: 0.7248 gal/h for ten minutes
            model.observe_fuel_rate(0.7248, 100.0 + second, True)
        self.assertLess(model.episode.gallons, 0.7248 * (C.FROZEN_AFTER_SECONDS + 2) / 3600)
        before = model.episode.gallons
        model.observe_fuel_rate(1.5, 701.0, True)       # it moves again: counting resumes
        model.observe_fuel_rate(1.6, 702.0, True)
        self.assertGreater(model.episode.gallons, before)

    def test_the_freeze_timer_restarts_after_a_gap_or_an_engine_off_sample(self):
        model = FuelFloorModel()
        open_episode(model)
        for second in range(0, 5):
            model.observe_fuel_rate(0.7248, 100.0 + second, True)
        before = model.episode.gallons
        model.observe_fuel_rate(0.7248, 204.0, True)    # a 100 s gap adds nothing, restarts the timer
        self.assertEqual(model.episode.gallons, before)
        model.observe_fuel_rate(0.7248, 205.0, True)    # two equal fresh samples one second apart
        self.assertGreater(model.episode.gallons, before)
        model.observe_fuel_rate(0.7248, 206.0, False)   # engine off breaks the chain too
        model.observe_fuel_rate(0.7248, 300.0, True)
        again = model.episode.gallons
        model.observe_fuel_rate(0.7248, 301.0, True)
        self.assertGreater(model.episode.gallons, again)

    def test_a_zero_blip_that_burned_nothing_leaves_no_trace(self):
        model = FuelFloorModel()
        open_episode(model)                            # confirmed, yet nothing was burned
        self.assertTrue(model.observe_van_level(45, 100.0, WALL))
        self.assertIsNone(model.episode)
        self.assertEqual((model.episodes, model.earlier_floors, model.floor["value"]), ([], [], 0))

    def test_history_lists_are_capped(self):
        model = FuelFloorModel()
        for number in range(C.MAX_EPISODES + 5):
            open_episode(model, 20000.0 * number)
            burn(model, 0.1 + number * 0.01, start=20000.0 * number + 1000.0)
            model.observe_van_level(50, 20000.0 * number + 9000.0, WALL)
            model.set_floor(-1, WALL)
        self.assertEqual(len(model.episodes), C.MAX_EPISODES)
        self.assertEqual(len(model.earlier_floors), C.MAX_EARLIER_FLOORS)


class OwnerChangeTests(unittest.TestCase):
    def test_set_and_restore_keep_what_they_replace(self):
        model = FuelFloorModel()
        model.set_floor(-4.5, WALL)
        model.set_floor(-2, WALL)
        self.assertEqual([row["value"] for row in model.earlier_floors], [-4.5, 0])
        model.restore_floor(1, WALL)
        self.assertEqual(model.floor["value"], -4.5)
        self.assertEqual(model.floor["source"], C.RESTORE)
        self.assertEqual([row["value"] for row in model.earlier_floors], [-2, 0])

    def test_bad_values_are_refused(self):
        model = FuelFloorModel()
        for bad in (1, -31, float("nan"), "2", True, None):
            with self.assertRaises(ValueError):
                model.set_floor(bad, WALL)
        for bad in (0, 1, 1.0, True):
            with self.assertRaises(ValueError):
                model.restore_floor(bad, WALL)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "fuel-floor.json"

    def test_a_restart_mid_episode_keeps_anchor_gallons_and_deepest_point(self):
        store = FuelFloorStore(self.path)
        store_episode(store)
        self.assertTrue(self.path.exists())  # the start is written at once, when it is confirmed
        for second in range(0, 70):
            store.observe_fuel_rate(3.6 + 0.001 * (second % 2), 100.0 + second, 780, 100.0 + second)
        store.flush(500.0)
        reborn = FuelFloorStore(self.path)
        episode = reborn.model.episode
        saved_gallons = episode.gallons
        self.assertEqual(episode.id, store.model.episode.id)
        self.assertEqual(episode.started_at, store.model.episode.started_at)
        self.assertAlmostEqual(episode.gallons, store.model.episode.gallons, places=3)
        self.assertAlmostEqual(episode.deepest_raw, store.model.episode.deepest_raw, places=2)
        reborn.observe_fuel_rate(3.6, 900.0, 780, 900.0)   # a new run chain: the downtime adds nothing
        reborn.observe_fuel_rate(3.6, 901.0, 780, 901.0)
        self.assertAlmostEqual(reborn.model.episode.gallons, saved_gallons + 0.001, places=3)

    def test_writes_during_an_episode_are_at_most_once_a_minute_plus_start_and_end(self):
        store = FuelFloorStore(self.path)
        with mock.patch.object(fuel_floor_store, "_atomic_json", wraps=fuel_floor_store._atomic_json) as write:
            store_episode(store)                      # confirmed at t=33: the start is written at once
            for second in range(1, 181):
                store.observe_fuel_rate(3.6 + 0.001 * (second % 2), 100.0 + second, 780, 100.0 + second)
            self.assertEqual(write.call_count, 1 + 3)  # start, then 160, 220, 280 (due 60 s after the last)
            store.observe_van_level(50, 282.0)
            self.assertEqual(write.call_count, 1 + 3 + 1)

    def test_a_missing_file_is_floor_zero_and_an_unreadable_one_is_moved_aside(self):
        self.assertEqual(FuelFloorStore(self.path).model.floor["value"], 0)
        self.path.write_text("{not json")
        store = FuelFloorStore(self.path)
        self.assertEqual((store.model.floor["value"], store.model.episode), (0, None))
        self.assertIn("could not be read", store.storage_error)
        self.assertEqual((self.path.parent / "fuel-floor.json.unreadable").read_text(), "{not json")
        store_episode(store)                                   # and it still works
        self.assertTrue(json.loads(self.path.read_text())["open_episode"])
        for text in ('{"version":2}', '{"version":1,"floor":{"value":-99}}', "[]"):
            self.path.write_text(text)
            self.assertEqual(FuelFloorStore(self.path).model.floor["value"], 0)

    def test_a_write_failure_is_reported_never_raised_and_set_is_not_adopted(self):
        store = FuelFloorStore(self.path)
        with mock.patch.object(fuel_floor_store, "_atomic_json", side_effect=OSError("disk full")):
            store_episode(store)
            store.observe_fuel_rate(3.6, 100.0, 780, 100.0)
            self.assertIn("not saved", store.storage_error)
            status, body = store.request({"action": "set", "points": -3}, 5.0)
        self.assertEqual(status, 503)
        self.assertEqual(body["floor"], 0)
        self.assertEqual(store.model.floor["value"], 0)

    def test_requests_validate_and_persist(self):
        store = FuelFloorStore(self.path)
        status, body = store.request({"action": "set", "points": -4.5}, 1.0)
        self.assertEqual((status, body["floor"]), (200, -4.5))
        self.assertEqual(json.loads(self.path.read_text())["floor"]["value"], -4.5)
        for bad in ({"action": "set"}, {"action": "set", "points": 5}, {"action": "restore", "index": 9},
                    {"action": "other"}, [], {"action": "set", "points": -1, "x": 1}):
            self.assertEqual(store.request(bad, 2.0)[0], 400)
        self.assertEqual(store.request({"action": "restore", "index": 1}, 3.0)[1]["floor"], 0)
        self.assertEqual(FuelFloorStore(self.path).model.floor["value"], 0)

    def test_non_integer_readings_are_ignored(self):
        store = FuelFloorStore(self.path)
        for junk in (None, 0.0, True, "0"):
            store.observe_van_level(junk, 1.0)
        self.assertIsNone(store.model.episode)


class CliTests(unittest.TestCase):
    class Client:
        def __init__(self, store):
            self.store = store

        def __call__(self, socket_path, timeout):
            return self

        def request(self, method, path, body=None):
            assert path == C.API_PATH
            return (200, self.store.response()) if method == "GET" else self.store.request(body, 1.0)

    def run_cli(self, store, *argv):
        out = io.StringIO()
        code = fuel_floor.run(fuel_floor.parse(list(argv)), client_factory=self.Client(store), out=out)
        return code, out.getvalue()

    def test_set_restore_and_list_through_the_broker_contract(self):
        store = FuelFloorStore()
        self.assertEqual(self.run_cli(store, "set", "-4.5")[0], 0)
        self.assertEqual(self.run_cli(store, "set", "-2")[0], 0)
        code, text = self.run_cli(store, "list")
        self.assertEqual(code, 0)
        self.assertIn("-2 points", text)
        self.assertIn("1. -4.5 points", text)
        self.assertEqual(self.run_cli(store, "restore", "1")[0], 0)
        self.assertEqual(store.model.floor["value"], -4.5)
        self.assertEqual(self.run_cli(store, "restore", "9")[0], 1)
        self.assertEqual(json.loads(self.run_cli(store, "list", "--json")[1])["floor"], -4.5)

    def test_an_unreachable_broker_exits_two(self):
        def refuse(*_args, **_kw):
            raise OSError("no socket")
        args = fuel_floor.parse(["list", "--socket", "/nonexistent"])
        self.assertEqual(fuel_floor.run(args, client_factory=refuse), 2)


class BrokerWiringTests(unittest.TestCase):
    def rig(self):
        clock = FakeClock(100.0)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = FuelFloorStore(Path(directory.name) / "fuel-floor.json")
        broker = TelemetryBroker(acquirer=FakeAcquirer(), monotonic=clock, fuel_floor_store=store)
        return broker, clock, store

    def publish_running(self, broker, rate, rpm=780):
        broker.publish_observation("engine.rpm", value=rpm, unit="rpm", source="ccan.broadcast.0x0fc",
                                   bus="c-can", quality="observed_alfa_scale")
        broker.publish_observation("engine.fuel_rate", value=rate, unit="gal/h", source="ccan.broadcast.0x2ed",
                                   bus="c-can", quality="candidate")

    def approach(self, broker, clock, seconds=34):
        """3 -> 2 -> 1 -> 0 with the engine running and fuel flowing, as the receiver and snapshots feed it."""
        for level in (3, 2, 1):
            clock.value += 1.0
            broker.observe_fuel_tank_reading(level, clock())
            self.publish_running(broker, 3.6)
        for second in range(seconds):
            clock.value += 1.0
            broker.observe_fuel_tank_reading(0, clock())
            self.publish_running(broker, 3.6 + 0.001 * (second % 2))

    def test_a_van_zero_then_published_fuel_shows_in_status_and_leaves_the_tank_metric_alone(self):
        broker, clock, store = self.rig()
        self.assertEqual(broker.status_response()["fuel_floor"]["episode"], None)
        self.approach(broker, clock)
        store.process(clock())  # the worker drains what reception and publication queued
        block = broker.status_response()["fuel_floor"]
        self.assertEqual(block["floor"], 0)
        self.assertGreater(block["episode"]["gallons"], 0.015)
        self.assertLess(block["episode"]["adjusted"], 0)
        self.assertEqual(broker.metric_response("fuel.tank_level")["reason"], "stale")  # nothing published

    def test_reception_and_publication_only_queue(self):
        broker, clock, store = self.rig()
        broker.observe_fuel_tank_reading(0, clock())
        self.publish_running(broker, 3.6)
        self.assertEqual(len(store._queue), 2)       # a van reading and a rate
        self.assertIsNone(store.model.pending)        # nothing was applied on those threads

    def test_fuel_with_the_engine_off_or_unknown_adds_nothing(self):
        broker, clock, store = self.rig()
        store.model.episode = fuel_floor_model.FuelFloorModel(
            {"version": 1, "floor": {"value": 0.0, "committed_at": None, "source": C.DEFAULT, "episode_id": None},
             "earlier_floors": [], "episodes": [],
             "open_episode": {"id": "x", "started_at": WALL, "gallons": 0, "deepest_raw": 0,
                              "implausible": False}}).episode
        for _ in range(5):
            clock.value += 1.0
            broker.publish_observation("engine.fuel_rate", value=3.6, unit="gal/h",
                                       source="ccan.broadcast.0x2ed", bus="c-can", quality="candidate")
        store.process(clock())
        self.assertEqual(store.model.episode.gallons, 0)

    def snapshot_broker(self, snapshots):
        """A broker whose passive powertrain reader returns ``snapshots`` in turn, metrics in name order."""
        from types import SimpleNamespace
        clock = FakeClock(100.0)
        queue = list(snapshots)

        class Reader:
            def read(self):
                rate, rpm = queue.pop(0)
                rows = [("engine.fuel_rate", rate, "gal/h", "ccan.broadcast.0x2ed", "candidate"),
                        ("engine.rpm", rpm, "rpm", "ccan.broadcast.0x0fc", "observed_alfa_scale")]
                return tuple(SimpleNamespace(metric=m, value=v, unit=u, source=src, quality=q)
                             for m, v, u, src, q in sorted(rows))

        store = FuelFloorStore()
        broker = TelemetryBroker(acquirer=FakeAcquirer(), monotonic=clock, passive_powertrain_reader=Reader(),
                                 fuel_floor_store=store)
        bus = success(metric="battery.voltage", unit="V", value=14.0, source="ccan.broadcast.0x41a",
                      bus="c-can", acquisition="passive", quality="verified", observed_monotonic=100.0)
        return broker, clock, store, bus

    def test_each_rate_is_paired_with_the_engine_speed_of_its_own_snapshot(self):
        # Production publication order is by metric name: engine.fuel_rate is stored before engine.rpm.
        broker, clock, store, bus = self.snapshot_broker([(3.6, 780), (3.6, 780), (3.7, 0), (3.6, 0)])
        store.model.episode = fuel_floor_model.FuelFloorModel(
            {"version": 1, "floor": {"value": 0.0, "committed_at": None, "source": C.DEFAULT, "episode_id": None},
             "earlier_floors": [], "episodes": [],
             "open_episode": {"id": "x", "started_at": WALL, "gallons": 0, "deepest_raw": 0,
                              "implausible": False}}).episode
        for _ in range(2):                    # engine running in both snapshots: fuel counts
            clock.value += 1.0
            broker._collect_passive_powertrain(bus)
        store.process(clock())
        counted = store.model.episode.gallons
        self.assertGreater(counted, 0)
        for _ in range(2):                    # the engine stopped: the rate, read before the rpm, must not count
            clock.value += 1.0
            broker._collect_passive_powertrain(bus)
        store.process(clock())
        self.assertEqual(store.model.episode.gallons, counted)

    def test_requests_and_listing_go_through_the_broker(self):
        broker, _, _ = self.rig()
        self.assertEqual(broker.fuel_floor_request({"action": "set", "points": -3})[0], 200)
        self.assertEqual(broker.fuel_floor_response()["floor"], -3)

    def test_the_route_exists_on_the_broker_socket_only(self):
        from projects.vehicle_data import api_server, web
        self.assertIn(C.API_PATH, api_server.TelemetryApiHandler.GET_PRODUCTS)
        self.assertNotIn("fuel-floor", Path(web.__file__).read_text())

    def test_the_worker_starts_applies_and_stops_with_the_broker(self):
        broker, clock, store = self.rig()
        store.start(clock)
        try:
            self.approach(broker, clock)
            deadline = time.monotonic() + 5
            while store.model.episode is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(store.model.episode)
        finally:
            store.stop(clock)
        self.assertIsNone(store._thread)
        self.assertTrue(json.loads(store.path.read_text())["open_episode"])


class ReceiverTapTests(unittest.TestCase):
    TAIL = bytes.fromhex("0B800000000000")

    def run_receive(self, valid_result=True):
        seen, status_reads = [], []
        worker = cr.CompassReceiver(None, status_reader=lambda: status_reads.append(1) or {}, publish=lambda *a: None,
                                    invalidate=lambda: None, tank_tap=lambda *a: seen.append(a))
        captured = {}

        def receiver(channel, **kwargs):
            captured.update(kwargs)

        with mock.patch.object(worker, "receiver", receiver):
            worker._receive("can9", "passive", lambda **kw: valid_result)
        return captured, seen

    def test_the_tap_sees_zero_with_the_receipt_time_which_the_metric_never_carries(self):
        captured, seen = self.run_receive()
        observe, decode = captured["observe"], captured["decode"]
        self.assertIsNone(decode(0x356, bytes([0]) + self.TAIL))        # the metric still has no value for 0
        observe(0x356, bytes([0]) + self.TAIL, None, 500.0)
        observe(0x356, bytes([42]) + self.TAIL, None, 500.5)            # inside the one-a-second sampling
        observe(0x356, bytes([42]) + self.TAIL, None, 501.0)
        observe(0x356, bytes([101]) + self.TAIL, None, 503.0)           # out of range: not a reading
        observe(0x355, bytes([7]) + self.TAIL, None, 504.0)             # another frame
        self.assertEqual(seen, [(0, 500.0), (42, 501.0)])
        self.assertIsNone(fuel_level.raw_reading(0x355, bytes([5]) + self.TAIL))

    def test_a_reading_the_route_check_refuses_is_never_fed(self):
        captured, seen = self.run_receive(valid_result=False)
        captured["observe"](0x356, bytes([0]) + self.TAIL, None, 500.0)
        self.assertEqual(seen, [])

    def test_the_real_receive_loop_hands_every_fresh_frame_to_observe_with_its_receipt_time(self):
        from tests.test_display_frames import raw_frame
        from tests.test_display_receiver import Clock, TimedSocket
        clock = Clock()
        frames = [(1, raw_frame(0x356, bytes([0]) + self.TAIL)), (2, raw_frame(0x356, bytes([50]) + self.TAIL))]
        seen, published = [], []
        dr.receive_display_frames(
            "can9", valid=lambda: True, stopped=lambda: clock.now >= 4,
            publish=lambda obs, at, mono: published.append((obs[0].value, mono)),
            observe=lambda can_id, data, at, mono: seen.append((can_id, data[0], mono)),
            frame_dlcs=bcan_companion.FRAME_DLCS, decode=bcan_companion.decode_bcan_companion,
            socket_factory=lambda *_: TimedSocket(clock, frames),
            monotonic=clock.monotonic, wall_time=clock.wall_time)
        self.assertEqual([(c, level) for c, level, _ in seen], [(0x356, 0), (0x356, 50)])
        self.assertEqual([value for value, _ in published], [50])         # the metric still skips 0
        self.assertEqual(seen[1][2], published[0][1])                       # the same receipt time the metric uses


class StoreRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "fuel-floor.json"

    def test_hostile_files_never_stop_construction(self):
        nested = "[" * 100000 + "]" * 100000                           # RecursionError in json
        record = {"version": 1, "floor": {"value": 10 ** 400, "committed_at": None, "source": "default",
                                          "episode_id": None}, "earlier_floors": [], "episodes": []}
        cases = {"nested": nested, "huge int": json.dumps(record),
                 "huge episode": json.dumps({**record, "floor": {**record["floor"], "value": 0},
                                             "episodes": [{"id": "a", "started_at": "t", "ended_at": "t",
                                                           "deepest_raw": -1, "gallons": 10 ** 400,
                                                           "end_reason": "recovered", "van_reading_at_end": 5,
                                                           "implausible": False, "floor_committed": False}]}),
                 "bad utf8": b"\xff\xfe\x00{"}
        for label, content in cases.items():
            with self.subTest(label):
                if isinstance(content, bytes):
                    self.path.write_bytes(content)
                else:
                    self.path.write_text(content)
                store = FuelFloorStore(self.path)
                self.assertEqual((store.model.floor["value"], store.model.episode), (0, None))
                self.assertIn("could not be read", store.storage_error)
                store.status_block()                                      # and it still answers

    def test_an_unwritable_directory_still_constructs_and_runs(self):
        blocker = Path(self.dir.name) / "a-file"
        blocker.write_text("x")                                           # a file where the directory should be
        store = FuelFloorStore(blocker / "fuel-floor.json")
        store_episode(store)                                              # the write fails, tracking goes on
        self.assertIsNotNone(store.model.episode)
        self.assertIn("not saved", store.storage_error)
        self.assertEqual(store.status_block()["episode"]["gallons"], 0)

    def test_a_stalled_writer_never_holds_up_reception_or_status(self):
        store = FuelFloorStore(self.path)
        release, entered = threading.Event(), threading.Event()

        def stalled(path, record):
            entered.set()
            release.wait(10)

        with mock.patch.object(fuel_floor_store, "_atomic_json", side_effect=stalled):
            writer = threading.Thread(target=store.request, args=({"action": "set", "points": -3}, 1.0))
            writer.start()
            self.assertTrue(entered.wait(5))                              # the disk write is now stuck
            started = time.monotonic()
            for second in range(200):
                store.enqueue_van_level(0, float(second))                 # the receive thread's whole job
                store.enqueue_fuel_rate(3.6, float(second), 780, float(second))
            store.status_block()
            store.response()
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            writer.join(5)
        self.assertEqual(store.status_block()["floor"], -3)


if __name__ == "__main__":
    unittest.main()
