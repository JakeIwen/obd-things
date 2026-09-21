"""Small, durable owner-entered service journal and last-known ICS mileage.

This module has no vehicle transport. Recording a change cannot reset the ECU.
GETs copy memory; mileage persistence is flushed by the background historian.
"""

from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import threading

from projects.vehicle_data.engine_off_voltage import _atomic_json


class MaintenanceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self.data = {"version": 1, "oil_changes": [], "odometer": None}
        self.error = None
        self.write_error = None
        self.dirty = False
        if self.path is not None:
            try:
                if self.path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("maintenance file exceeds size limit")
                loaded = json.loads(self.path.read_text())
                if (not isinstance(loaded, dict) or loaded.get("version") != 1
                        or not isinstance(loaded.get("oil_changes"), list)):
                    raise ValueError("maintenance file schema is invalid")
                for record in loaded["oil_changes"]:
                    self.validate({key: record[key] for key in
                                   ("date", "mileage_mi", "mileage_source", "notes", "request_id")})
                    datetime.fromisoformat(record["recorded_at"])
                odometer = loaded.get("odometer")
                if odometer is not None:
                    if (not isinstance(odometer, dict) or odometer.get("source") != "ics.did.2001"
                            or not self.valid_mileage(odometer.get("value"))):
                        raise ValueError("saved odometer is invalid")
                    datetime.fromisoformat(odometer["observed_at"])
                self.data = loaded
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError) as exc:
                # Never overwrite an unreadable owner record with an empty log.
                self.error = f"Maintenance storage could not be read: {exc}"

    @staticmethod
    def valid_mileage(value):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and 0 <= value <= 2_000_000)

    @classmethod
    def validate(cls, payload):
        keys = {"date", "mileage_mi", "mileage_source", "notes", "request_id"}
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError("Expected date, mileage_mi, mileage_source, notes, and request_id")
        if not isinstance(payload["date"], str):
            raise ValueError("Service date must be YYYY-MM-DD")
        serviced = date.fromisoformat(payload["date"])
        if serviced.isoformat() != payload["date"] or not date(2000, 1, 1) <= serviced <= date.today():
            raise ValueError("Service date must be between 2000-01-01 and today")
        mileage = payload["mileage_mi"]
        source = payload["mileage_source"]
        if mileage is not None and not cls.valid_mileage(mileage):
            raise ValueError("Mileage must be a finite number from 0 to 2,000,000 miles")
        if source not in ("cluster_or_receipt", "ics_estimate", "unknown"):
            raise ValueError("Unknown mileage source")
        if (mileage is None) != (source == "unknown"):
            raise ValueError("Unknown mileage requires an empty mileage field")
        if not isinstance(payload["notes"], str) or len(payload["notes"]) > 800:
            raise ValueError("Notes must be at most 800 characters")
        if (not isinstance(payload["request_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", payload["request_id"])):
            raise ValueError("Invalid record request ID")

    def snapshot(self):
        with self.lock:
            records = sorted(self.data["oil_changes"], key=lambda r: (r["date"], r["recorded_at"]))
            return json.loads(json.dumps({
                "available": self.error is None, "storage_error": self.error or self.write_error,
                "persistent": self.path is not None,
                "last_oil_change": records[-1] if records else None,
                "oil_changes": list(reversed(records[-20:])),
                "record_count": len(records), "last_known_odometer": self.data["odometer"],
            }))

    def add_oil_change(self, payload):
        self.validate(payload)
        with self.lock:
            if self.error:
                raise OSError(self.error)
            for record in self.data["oil_changes"]:
                if record["request_id"] == payload["request_id"]:
                    if any(record[key] != payload[key] for key in payload):
                        raise ValueError("Record ID already exists with different contents")
                    return dict(record)
            if len(self.data["oil_changes"]) >= 1000:
                raise ValueError("Service history is full; archive it before adding records")
            record = {**payload, "recorded_at": datetime.now(timezone.utc).isoformat()}
            updated = {**self.data, "oil_changes": [*self.data["oil_changes"], record]}
            # Commit to disk before acknowledging the user; failed writes retain memory.
            if self.path is not None:
                _atomic_json(self.path, updated)
            self.data = updated
            self.write_error = None
            self.dirty = False
            return dict(record)

    def observe_odometer(self, result):
        if (not result.available or result.source != "ics.did.2001"
                or result.unit != "mi" or not self.valid_mileage(result.value)
                or result.observed_at is None):
            return
        with self.lock:
            self.data["odometer"] = {"value": result.value, "unit": "mi",
                                     "source": result.source, "quality": "candidate",
                                     "observed_at": result.observed_at.isoformat()}
            self.dirty = True

    def flush(self):
        with self.lock:
            if self.path is not None and self.dirty and self.error is None:
                try:
                    _atomic_json(self.path, self.data)
                except OSError as exc:
                    self.write_error = f"Last-known mileage was not persisted: {exc}"
                else:
                    self.write_error = None
                    self.dirty = False

    def restore_odometer(self, sample):
        """Import one dated historian row at startup, never as a live sample."""
        if (not isinstance(sample, dict) or sample.get("source") != "ics.did.2001"
                or sample.get("unit") != "mi" or not self.valid_mileage(sample.get("value"))
                or not isinstance(sample.get("observed_at"), str)):
            return
        timestamp = datetime.fromisoformat(sample["observed_at"])
        with self.lock:
            previous = self.data["odometer"]
            if previous and datetime.fromisoformat(previous["observed_at"]) >= timestamp:
                return
            self.data["odometer"] = {key: sample[key] for key in ("value", "unit", "source", "observed_at")}
            self.data["odometer"]["quality"] = "candidate"
            self.dirty = True
