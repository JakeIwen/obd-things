"""Below-empty fuel gauge: episodes, the floor, the owner record, the CLI and the broker wiring."""

import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.broadcast_signals import FUEL_RATE_CLUSTER_FACTOR
from projects.vehicle_data import compass_receiver as cr
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
    def test_only_a_van_reading_of_zero_starts_an_episode(self):
        model = FuelFloorModel()
        for level in (1, 2, 10, 100):
            self.assertFalse(model.observe_van_level(level, 1.0, WALL))
        self.assertIsNone(model.episode)
        self.assertTrue(model.observe_van_level(0, 2.0, WALL))
        self.assertEqual(model.episode.started_at, WALL)
        self.assertEqual(model.open_view()["raw"], 0)

    def test_gallons_become_negative_points_and_the_floor_waits_for_the_end(self):
        model = FuelFloorModel()
        model.observe_van_level(0, 0.0, WALL)
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
        model.observe_van_level(0, 0.0, WALL)
        burn(model, 1.0)
        model.observe_van_level(50, 5000.0, WALL)
        floor = model.floor["value"]
        model.observe_van_level(0, 6000.0, WALL)
        burn(model, 0.4, start=7000.0)
        model.observe_van_level(50, 9000.0, WALL)
        self.assertEqual(model.floor["value"], floor)
        self.assertEqual([row["floor_committed"] for row in model.episodes], [False, True])

    def test_the_gauge_deepens_the_floor_across_episodes_and_displays_from_it(self):
        model = FuelFloorModel()
        model.observe_van_level(0, 0.0, WALL)
        burn(model, 0.35)
        model.observe_van_level(50, 5000.0, WALL)
        first = model.floor["value"]
        self.assertAlmostEqual(first, -2, delta=0.2)
        model.observe_van_level(0, 6000.0, WALL)
        self.assertEqual(model.open_view()["adjusted"], round(rescale(0, first), 2))  # van zero is above the floor
        burn(model, 0.7, start=7000.0)
        self.assertLess(model.open_view()["raw"], first)
        self.assertLess(model.open_view()["adjusted"], 0)

    def test_recovery_needs_a_continuous_minute_at_one_or_more_and_slosh_does_not_end_it(self):
        model = FuelFloorModel()
        model.observe_van_level(0, 0.0, WALL)
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
        model.observe_van_level(0, 0.0, WALL)
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
        model.observe_van_level(0, 0.0, WALL)
        burn(model, 0.5)
        self.assertIsNotNone(model.episode)
        self.assertGreater(model.episode.gallons, 0.49)

    def test_the_sanity_bound_holds_and_commits_no_floor(self):
        model = FuelFloorModel()
        model.observe_van_level(0, 0.0, WALL)
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
        model.observe_van_level(0, 0.0, WALL)
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
        model.observe_van_level(0, 0.0, WALL)
        for second in range(0, 600):  # the 2026-11-18 evening history: 0.7248 gal/h for ten minutes
            model.observe_fuel_rate(0.7248, 100.0 + second, True)
        self.assertLess(model.episode.gallons, 0.7248 * (C.FROZEN_AFTER_SECONDS + 2) / 3600)
        before = model.episode.gallons
        model.observe_fuel_rate(1.5, 701.0, True)       # it moves again: counting resumes
        model.observe_fuel_rate(1.6, 702.0, True)
        self.assertGreater(model.episode.gallons, before)

    def test_a_zero_blip_that_burned_nothing_leaves_no_trace(self):
        model = FuelFloorModel()
        model.observe_van_level(0, 0.0, WALL)          # key-off or wake shows raw 0 briefly
        self.assertTrue(model.observe_van_level(45, 3.0, WALL))
        self.assertIsNone(model.episode)
        self.assertEqual((model.episodes, model.earlier_floors, model.floor["value"]), ([], [], 0))

    def test_history_lists_are_capped(self):
        model = FuelFloorModel()
        for number in range(C.MAX_EPISODES + 5):
            model.observe_van_level(0, 0.0, WALL)
            burn(model, 0.1 + number * 0.01)
            model.observe_van_level(50, 9000.0, WALL)
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
        store.observe_van_level(0, 0.0)
        self.assertTrue(self.path.exists())  # the start is written at once
        for second in range(0, 70):
            store.observe_fuel_rate(3.6 + 0.001 * (second % 2), 100.0 + second, True, 100.0 + second)
        store.flush(500.0)
        reborn = FuelFloorStore(self.path)
        episode = reborn.model.episode
        saved_gallons = episode.gallons
        self.assertEqual(episode.id, store.model.episode.id)
        self.assertEqual(episode.started_at, store.model.episode.started_at)
        self.assertAlmostEqual(episode.gallons, store.model.episode.gallons, places=3)
        self.assertAlmostEqual(episode.deepest_raw, store.model.episode.deepest_raw, places=2)
        reborn.observe_fuel_rate(3.6, 900.0, True, 900.0)   # a new run chain: the downtime adds nothing
        reborn.observe_fuel_rate(3.6, 901.0, True, 901.0)
        self.assertAlmostEqual(reborn.model.episode.gallons, saved_gallons + 0.001, places=3)

    def test_writes_during_an_episode_are_at_most_once_a_minute_plus_start_and_end(self):
        store = FuelFloorStore(self.path)
        with mock.patch.object(fuel_floor_store, "_atomic_json", wraps=fuel_floor_store._atomic_json) as write:
            store.observe_van_level(0, 0.0)
            for second in range(1, 181):
                store.observe_fuel_rate(3.6 + 0.001 * (second % 2), float(second), True, float(second))
            self.assertEqual(write.call_count, 1 + 3)  # start, then t=60, 120, 180 (first due at 60 s)
            store.observe_van_level(50, 181.0)
            self.assertEqual(write.call_count, 1 + 3 + 1)

    def test_a_missing_file_is_floor_zero_and_an_unreadable_one_is_moved_aside(self):
        self.assertEqual(FuelFloorStore(self.path).model.floor["value"], 0)
        self.path.write_text("{not json")
        store = FuelFloorStore(self.path)
        self.assertEqual((store.model.floor["value"], store.model.episode), (0, None))
        self.assertIn("could not be read", store.storage_error)
        self.assertEqual((self.path.parent / "fuel-floor.json.unreadable").read_text(), "{not json")
        store.observe_van_level(0, 0.0)                        # and it still works
        self.assertTrue(json.loads(self.path.read_text())["open_episode"])
        for text in ('{"version":2}', '{"version":1,"floor":{"value":-99}}', "[]"):
            self.path.write_text(text)
            self.assertEqual(FuelFloorStore(self.path).model.floor["value"], 0)

    def test_a_write_failure_is_reported_never_raised_and_set_is_not_adopted(self):
        store = FuelFloorStore(self.path)
        with mock.patch.object(fuel_floor_store, "_atomic_json", side_effect=OSError("disk full")):
            store.observe_van_level(0, 0.0)
            store.observe_fuel_rate(3.6, 1.0, True, 1.0)
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

    def publish_running(self, broker, rate):
        broker.publish_observation("engine.rpm", value=780, unit="rpm", source="ccan.broadcast.0x0fc",
                                   bus="c-can", quality="observed_alfa_scale")
        broker.publish_observation("engine.fuel_rate", value=rate, unit="gal/h", source="ccan.broadcast.0x2ed",
                                   bus="c-can", quality="candidate")

    def test_a_van_zero_then_published_fuel_shows_in_status_and_leaves_the_tank_metric_alone(self):
        broker, clock, store = self.rig()
        self.assertEqual(broker.status_response()["fuel_floor"]["episode"], None)
        broker.observe_fuel_tank_reading(0)
        for _ in range(20):
            clock.value += 1.0
            self.publish_running(broker, 3.6)
        block = broker.status_response()["fuel_floor"]
        self.assertEqual(block["floor"], 0)
        self.assertGreater(block["episode"]["gallons"], 0.015)
        self.assertLess(block["episode"]["adjusted"], 0)
        self.assertEqual(broker.metric_response("fuel.tank_level")["reason"], "stale")  # nothing published

    def test_fuel_with_the_engine_off_or_unknown_adds_nothing(self):
        broker, clock, store = self.rig()
        broker.observe_fuel_tank_reading(0)
        for _ in range(5):
            clock.value += 1.0
            broker.publish_observation("engine.fuel_rate", value=3.6, unit="gal/h",
                                       source="ccan.broadcast.0x2ed", bus="c-can", quality="candidate")
        self.assertEqual(store.model.episode.gallons, 0)

    def test_requests_and_listing_go_through_the_broker(self):
        broker, _, _ = self.rig()
        self.assertEqual(broker.fuel_floor_request({"action": "set", "points": -3})[0], 200)
        self.assertEqual(broker.fuel_floor_response()["floor"], -3)

    def test_the_route_exists_on_the_broker_socket_only(self):
        from projects.vehicle_data import api_server, web
        self.assertIn(C.API_PATH, api_server.TelemetryApiHandler.GET_PRODUCTS)
        self.assertNotIn("fuel-floor", Path(web.__file__).read_text())


class ReceiverTapTests(unittest.TestCase):
    def test_the_tap_sees_zero_which_the_metric_never_carries(self):
        seen = []
        worker = cr.CompassReceiver(None, status_reader=lambda: {}, publish=lambda *a: None,
                                    invalidate=lambda: None, tank_tap=seen.append)
        captured = {}

        def receiver(channel, **kwargs):
            captured.update(kwargs)

        with mock.patch.object(worker, "receiver", receiver):
            worker._receive("can9", "passive", lambda **kw: True)
        decode = captured["decode"]
        tail = bytes.fromhex("0B800000000000")
        self.assertIsNone(decode(0x356, bytes([0]) + tail))
        self.assertEqual(len(decode(0x356, bytes([42]) + tail)), 1)
        self.assertEqual(decode(0x356, bytes([101]) + tail), ())
        self.assertEqual(seen, [0, 42])
        self.assertIsNone(fuel_level.raw_reading(0x355, bytes([5]) + tail))


if __name__ == "__main__":
    unittest.main()
