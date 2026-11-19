"""Pure model of the fuel gauge that keeps counting below the van's own 0 %.

The van's tank level stops at 0 with fuel still in the tank. A below-empty episode opens when
the van reads 0 and dead-reckons the gallons burned since; the lowest point it reaches becomes
the gauge's new zero (the floor) when the episode ends. Nothing here reads a clock, a lock, a
file or the broker: callers pass monotonic times (for durations) and ISO strings (for records).
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from lib.timeutil import finite_number
from projects.vehicle_data import fuel_floor_contract as C

VERSION = 1


def rescale(raw: float, floor: float) -> float:
    """The displayed percent: ``floor`` becomes 0 and the van's 100 stays 100."""
    return (raw - floor) / (100.0 - floor) * 100.0


def _default_floor() -> dict:
    return {"value": 0.0, "committed_at": None, "source": C.DEFAULT, "episode_id": None}


def _num(value: object) -> bool:
    """A bounded real: a huge or non-finite number in a saved file must not reach arithmetic."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and -C.NUMBER_BOUND <= value <= C.NUMBER_BOUND)


def _text(value: object, nullable: bool = False) -> bool:
    return (value is None and nullable) or isinstance(value, str) and bool(value)


def _valid_floor(row: object) -> dict:
    if (not isinstance(row, dict) or set(row) != {"value", "committed_at", "source", "episode_id"}
            or not _num(row["value"]) or not C.FLOOR_SET_MINIMUM <= row["value"] <= 0
            or row["source"] not in C.FLOOR_SOURCES or not _text(row["committed_at"], True)
            or not _text(row["episode_id"], True)):
        raise ValueError("saved fuel floor is invalid")
    return {**row, "value": float(row["value"])}


def _valid_open(row: object) -> dict:
    if (not isinstance(row, dict) or set(row) != {"id", "started_at", "gallons", "deepest_raw", "implausible"}
            or not _text(row["id"]) or not _text(row["started_at"]) or type(row["implausible"]) is not bool
            or not _num(row["gallons"]) or row["gallons"] < 0
            or not _num(row["deepest_raw"]) or not C.IMPLAUSIBLE_RAW <= row["deepest_raw"] <= 0):
        raise ValueError("saved open fuel episode is invalid")
    return dict(row)


_EPISODE_KEYS = {"id", "started_at", "ended_at", "deepest_raw", "gallons", "end_reason",
                 "van_reading_at_end", "implausible", "floor_committed"}


def _valid_episode(row: object) -> dict:
    if (not isinstance(row, dict) or set(row) != _EPISODE_KEYS
            or not _text(row["id"]) or not _text(row["started_at"]) or not _text(row["ended_at"])
            or row["end_reason"] not in C.END_REASONS
            or not _num(row["deepest_raw"]) or not _num(row["gallons"])
            or not isinstance(row["van_reading_at_end"], int) or isinstance(row["van_reading_at_end"], bool)
            or abs(row["van_reading_at_end"]) > C.NUMBER_BOUND
            or type(row["implausible"]) is not bool or type(row["floor_committed"]) is not bool):
        raise ValueError("saved fuel episode is invalid")
    return dict(row)


@dataclass
class _Episode:
    id: str
    started_at: str
    gallons: float = 0.0
    deepest_raw: float = 0.0
    implausible: bool = False

    @property
    def raw(self) -> float:
        """Van-gauge points below the van's 0 (never below the sanity bound)."""
        return max(-self.gallons / C.GAL_PER_POINT, float(C.IMPLAUSIBLE_RAW))


class FuelFloorModel:
    """The floor, its history, and at most one open below-empty episode."""

    def __init__(self, record: dict | None = None):
        self.floor = _default_floor()
        self.earlier_floors: list[dict] = []
        self.episodes: list[dict] = []
        self.episode: _Episode | None = None
        # Run-time only, never saved or shown: a 0 waiting to prove it is not a sentinel.
        self.pending: _Episode | None = None
        self._pending_since = 0.0
        self._last_nonzero: tuple[int, float] | None = None
        self._engine_positive_mono: float | None = None
        # Run-time only: monotonic evidence that a restart rightly forgets.
        self._above_since: float | None = None
        self._last_van: float | None = None
        self._last_rate: tuple[float, float] | None = None
        self._frozen: tuple[float, float] | None = None  # (rate, monotonic it was first seen)
        self.dirty = False
        if record is not None:
            self._load(record)

    # -- persistence shape ---------------------------------------------------

    def _load(self, record: object) -> None:
        if (not isinstance(record, dict) or record.get("version") != VERSION
                or not isinstance(record.get("earlier_floors"), list)
                or not isinstance(record.get("episodes"), list)):
            raise ValueError("fuel floor file schema is invalid")
        floor = _valid_floor(record["floor"])
        earlier = [_valid_floor(row) for row in record["earlier_floors"]][:C.MAX_EARLIER_FLOORS]
        episodes = [_valid_episode(row) for row in record["episodes"]][:C.MAX_EPISODES]
        open_row = record.get("open_episode")
        episode = None if open_row is None else _Episode(**_valid_open(open_row))
        self.floor, self.earlier_floors, self.episodes, self.episode = floor, earlier, episodes, episode

    def to_record(self) -> dict:
        episode = self.episode
        return {
            "version": VERSION,
            "floor": dict(self.floor),
            "earlier_floors": [dict(row) for row in self.earlier_floors],
            "episodes": [dict(row) for row in self.episodes],
            "open_episode": None if episode is None else {
                "id": episode.id, "started_at": episode.started_at,
                "gallons": round(episode.gallons, 4), "deepest_raw": round(episode.deepest_raw, 3),
                "implausible": episode.implausible},
        }

    # -- the van's own reading -----------------------------------------------

    def observe_engine(self, rpm_positive: bool, mono: float) -> None:
        """Whether a fresh engine speed above zero was just seen (from the same snapshot as a rate)."""
        self._engine_positive_mono = mono if rpm_positive else None

    def _engine_fresh(self, mono: float) -> bool:
        seen = self._engine_positive_mono
        return seen is not None and mono - seen <= C.ENGINE_FRESH_SECONDS

    def observe_van_level(self, value: int, mono: float, wall: str) -> bool:
        """One fresh van reading; True when the record changed in a way that must be saved now.

        A missing or stale reading is never fed in, so it neither starts nor ends an episode.
        A 0 only opens a candidate (0x356 sends 0 as "no value" at wake and key-off); it becomes
        an episode after ``CANDIDATE_HOLD_SECONDS`` of unbroken 0 with the engine running.
        """
        previous, self._last_van = self._last_van, mono
        if value != C.START_READING:
            self.pending = None
            self._last_nonzero = (value, mono)
        if self.episode is None:
            return value == C.START_READING and self._observe_zero(mono, wall, previous)
        if value < C.RECOVERY_READING:
            self._above_since = None
            return False
        if value >= C.REFUEL_READING:
            return self._end(C.REFUEL, value, wall)
        if (self._above_since is None or previous is None
                or mono - previous > C.VAN_READING_MAX_GAP_SECONDS):
            self._above_since = mono  # slosh back to 0, or a silent receiver, restarts the hold
        elif mono - self._above_since >= C.RECOVERY_HOLD_SECONDS:
            return self._end(C.RECOVERED, value, wall)
        return False

    def _observe_zero(self, mono: float, wall: str, previous: float | None) -> bool:
        pending = self.pending
        if pending is not None and (previous is None or mono - previous > C.VAN_READING_MAX_GAP_SECONDS):
            pending = self.pending = None  # a silent receiver breaks "no nonzero in between"
        if not self._engine_fresh(mono):
            self.pending = None
            return False
        if pending is None:
            prior = self._last_nonzero
            if (prior is None or prior[0] > C.CANDIDATE_MAX_PRIOR_READING
                    or mono - prior[1] > C.CANDIDATE_MAX_PRIOR_AGE_SECONDS):
                return False  # not a gauge running down: a wake or key-off sentinel
            self.pending = _Episode(uuid.uuid4().hex, wall)
            self._pending_since = mono
            self._reset_run_state()
            return False
        if mono - self._pending_since < C.CANDIDATE_HOLD_SECONDS:
            return False
        self.episode, self.pending = pending, None
        self.dirty = True
        return True

    def _reset_run_state(self) -> None:
        self._above_since = None
        self._last_rate = None
        self._frozen = None

    def _end(self, reason: str, van_reading: int, wall: str) -> bool:
        episode, self.episode = self.episode, None
        self._reset_run_state()
        self.dirty = True
        if episode.gallons < C.MIN_LOGGED_GALLONS and not episode.implausible:
            return True  # a 0 blip at key-off or wake: nothing was burned, nothing to record
        commit = not episode.implausible and episode.deepest_raw < self.floor["value"]
        if commit:
            self._replace_floor({"value": round(episode.deepest_raw, 2), "committed_at": wall,
                                 "source": C.EPISODE, "episode_id": episode.id})
        self.episodes.insert(0, {
            "id": episode.id, "started_at": episode.started_at, "ended_at": wall,
            "deepest_raw": round(episode.deepest_raw, 3), "gallons": round(episode.gallons, 4),
            "end_reason": reason, "van_reading_at_end": van_reading,
            "implausible": episode.implausible, "floor_committed": commit})
        del self.episodes[C.MAX_EPISODES:]
        return True

    def _replace_floor(self, new: dict) -> None:
        self.earlier_floors.insert(0, self.floor)
        del self.earlier_floors[C.MAX_EARLIER_FLOORS:]
        self.floor = new

    # -- fuel burned ---------------------------------------------------------

    def observe_fuel_rate(self, rate_gph: float, sample_mono: float, engine_running: bool) -> None:
        """One fuel-rate sample stamped when it was received; only a fresh running one adds fuel.

        Fuel burned while a candidate is pending is kept in memory, so a confirmed episode counts
        from its first 0.
        """
        target = self.episode or self.pending
        if target is None:
            return
        if not engine_running or not finite_number(rate_gph) or rate_gph < 0:
            self._last_rate = None  # the span across an engine-off or bad sample is not burned fuel
            self._frozen = None
            return
        previous = self._last_rate
        if previous is not None and sample_mono <= previous[0]:
            return  # the same or an older observation read again adds nothing
        broken = previous is None or sample_mono - previous[0] > C.FUEL_SAMPLE_MAX_GAP_SECONDS
        if broken or self._frozen is None or self._frozen[0] != rate_gph:
            self._frozen = (rate_gph, sample_mono)  # continuity broke: the freeze timer restarts
        frozen = sample_mono - self._frozen[1] > C.FROZEN_AFTER_SECONDS
        self._last_rate = (sample_mono, rate_gph)
        if broken or frozen:
            return
        target.gallons += (previous[1] + rate_gph) / 2.0 * (sample_mono - previous[0]) / 3600.0
        self.dirty = self.dirty or target is self.episode
        floor_gallons = -C.IMPLAUSIBLE_RAW * C.GAL_PER_POINT
        if target.gallons >= floor_gallons:
            target.gallons, target.implausible = floor_gallons, True
        target.deepest_raw = min(target.deepest_raw, target.raw)

    # -- owner changes -------------------------------------------------------

    def set_floor(self, points: float, wall: str) -> None:
        if (not finite_number(points) or isinstance(points, bool)
                or not C.FLOOR_SET_MINIMUM <= points <= 0):
            raise ValueError(f"floor must be between {C.FLOOR_SET_MINIMUM} and 0 points")
        self._replace_floor({"value": round(float(points), 2), "committed_at": wall,
                             "source": C.SET, "episode_id": None})
        self.dirty = True

    def restore_floor(self, index: int, wall: str) -> None:
        """Make earlier floor number ``index`` (1 = the most recently replaced) the floor again."""
        if type(index) is not int or not 1 <= index <= len(self.earlier_floors):
            raise ValueError("no earlier floor has that number")
        chosen = self.earlier_floors.pop(index - 1)
        self.earlier_floors.insert(0, self.floor)
        del self.earlier_floors[C.MAX_EARLIER_FLOORS:]
        self.floor = {**chosen, "source": C.RESTORE, "committed_at": wall}
        self.dirty = True

    # -- exposure ------------------------------------------------------------

    def open_view(self) -> dict | None:
        """The open episode as the dashboard needs it, with its displayed percent."""
        episode = self.episode
        if episode is None:
            return None
        raw = episode.raw
        return {"id": episode.id, "started_at": episode.started_at,
                "gallons": round(episode.gallons, 2), "raw": round(raw, 2),
                "adjusted": round(rescale(raw, self.floor["value"]), 2),
                "implausible": episode.implausible}

    def status_block(self) -> dict:
        return {"floor": self.floor["value"], "gal_per_point": round(C.GAL_PER_POINT, 6),
                "episode": self.open_view()}

    def listing(self) -> dict:
        return {**self.status_block(), "floor_record": dict(self.floor),
                "earlier_floors": [dict(row) for row in self.earlier_floors],
                "episodes": [dict(row) for row in self.episodes]}
