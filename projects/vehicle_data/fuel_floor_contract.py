"""Names, limits and constants of the below-empty fuel gauge, shared with the dashboard.

The numbers live in ``fuel_floor.json`` so the dashboard imports the same copy. Only the
conversion factor is derived here: it is tied to the fuel-rate scale it was measured under.
"""

import json
from pathlib import Path

from lib.broadcast_signals import FUEL_RATE_CLUSTER_FACTOR
from projects.vehicle_data.active_session_gates import RUNNING_RPM

CONTRACT = json.loads(Path(__file__).with_name("fuel_floor.json").read_text())
API_PATH = CONTRACT["path"]
STATUS_KEY = CONTRACT["status_key"]
FILE_NAME = CONTRACT["file_name"]
# Gallons of engine.fuel_rate fuel per van-gauge point, measured over the 40 -> 10 % band
# with the cluster factor at the reference value; a later factor change leaves points unchanged.
GAL_PER_POINT = (CONTRACT["gal_per_point_reference"] * FUEL_RATE_CLUSTER_FACTOR
                 / CONTRACT["reference_cluster_factor"])
START_READING = CONTRACT["start_reading"]
# 0x356 sends 0 as a no-value sentinel at wake and key-off, so a 0 only opens a candidate.
CANDIDATE_HOLD_SECONDS = CONTRACT["candidate_hold_seconds"]
CANDIDATE_MAX_PRIOR_READING = CONTRACT["candidate_max_prior_reading"]
CANDIDATE_MAX_PRIOR_AGE_SECONDS = CONTRACT["candidate_max_prior_age_seconds"]
ENGINE_FRESH_SECONDS = CONTRACT["engine_fresh_seconds"]
NUMBER_BOUND = CONTRACT["number_bound"]
QUEUE_MAX = CONTRACT["queue_max"]
RECOVERY_READING = CONTRACT["recovery_reading"]
RECOVERY_HOLD_SECONDS = CONTRACT["recovery_hold_seconds"]
REFUEL_READING = CONTRACT["refuel_reading"]
VAN_READING_MAX_GAP_SECONDS = CONTRACT["van_reading_max_gap_seconds"]
FUEL_SAMPLE_MAX_GAP_SECONDS = CONTRACT["fuel_sample_max_gap_seconds"]
FROZEN_AFTER_SECONDS = CONTRACT["frozen_after_seconds"]
IMPLAUSIBLE_RAW = CONTRACT["implausible_raw"]
FLOOR_SET_MINIMUM = CONTRACT["floor_set_minimum"]
MIN_LOGGED_GALLONS = CONTRACT["min_logged_gallons"]
MAX_EARLIER_FLOORS = CONTRACT["max_earlier_floors"]
MAX_EPISODES = CONTRACT["max_episodes"]
WRITE_INTERVAL_SECONDS = CONTRACT["write_interval_seconds"]
RECOVERED, REFUEL = CONTRACT["end_reasons"]["recovered"], CONTRACT["end_reasons"]["refuel"]
END_REASONS = frozenset((RECOVERED, REFUEL))
DEFAULT, EPISODE, SET, RESTORE = (CONTRACT["floor_sources"][name]
                                  for name in ("default", "episode", "set", "restore"))
FLOOR_SOURCES = frozenset((DEFAULT, EPISODE, SET, RESTORE))
SET_ACTION, RESTORE_ACTION = CONTRACT["actions"]["set"], CONTRACT["actions"]["restore"]
