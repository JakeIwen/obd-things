"""Bounded last accepted observations, separate from the live metric cache.

No CAN access and no file/SQL work on GETs. Presentation decides which dated
values are useful to show; historical samples never become live observations.
"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading

from projects.vehicle_data.engine_off_voltage import _atomic_json


class LastReadings:
    def __init__(self, definitions, path=None):
        self.definitions = definitions
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self.readings = {}
        self.dirty = False
        self.storage_error = None
        if self.path is not None:
            try:
                if self.path.stat().st_size > 256 * 1024:
                    raise ValueError("last-readings file exceeds size limit")
                data = json.loads(self.path.read_text())
                if data.get("version") != 1 or not isinstance(data.get("readings"), dict):
                    raise ValueError("invalid last-readings schema")
                for name, sample in data["readings"].items():
                    self.remember(name, sample)
                self.dirty = False
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self.storage_error = f"Last readings could not be restored: {exc}"

    def remember(self, name, sample):
        definition = self.definitions.get(name)
        if definition is None or not isinstance(sample, dict):
            return False
        source = next((s for s in definition.sources if s.name == sample.get("source")), None)
        if (source is None or sample.get("unit") != definition.unit
                or sample.get("quality") != source.quality):
            return False
        value = sample.get("value")
        if definition.value_type in ("number", "integer"):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or (definition.value_type == "integer" and value != int(value))
                    or (definition.minimum is not None and value < definition.minimum)
                    or (definition.maximum is not None and value > definition.maximum)):
                return False
            if definition.value_type == "integer":
                value = int(value)
        elif definition.value_type == "boolean":
            if type(value) is not bool:
                return False
        else:
            return False
        if source.publisher_values is not None and value not in source.publisher_values:
            return False
        try:
            observed = datetime.fromisoformat(sample["observed_at"])
            if observed.tzinfo is None or observed > datetime.now(timezone.utc):
                return False
        except (KeyError, ValueError, TypeError):
            return False
        row = {"value": value, "unit": definition.unit, "source": source.name,
               "bus": source.bus, "quality": source.quality,
               "acquisition": source.acquisition_class,
               "observed_at": observed.isoformat()}
        with self.lock:
            previous = self.readings.get(name)
            if previous and datetime.fromisoformat(previous["observed_at"]) >= observed:
                return False
            self.readings[name] = row
            self.dirty = True
        return True

    def observe(self, result):
        if result.available and result.observed_at is not None:
            self.remember(result.metric, {"value": result.value, "unit": result.unit,
                "source": result.source, "quality": result.quality,
                "observed_at": result.observed_at.isoformat()})

    def get(self, name):
        with self.lock:
            row = self.readings.get(name)
            return dict(row) if row is not None else None

    def restore_from_historian(self, historian):
        # One bounded indexed lookup per registry metric, at startup only.
        try:
            for name in self.definitions:
                self.remember(name, historian.latest_sample(name, fresh_only=True))
        except Exception as exc:
            # A presentation/history failure must not stop live collection.
            self.storage_error = f"Last-reading history recovery failed: {exc}"
            return
        self.flush()

    def flush(self):
        with self.lock:
            if not self.dirty or self.path is None:
                return
            try:
                _atomic_json(self.path, {"version": 1, "readings": self.readings})
            except OSError as exc:
                self.storage_error = f"Last readings could not be saved: {exc}"
            else:
                self.storage_error = None
                self.dirty = False
