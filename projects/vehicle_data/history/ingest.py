"""Transactional snapshot ingestion, trip segmentation and gap tracking."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Mapping

from .models import (
    IngestResult,
    MICROSECONDS,
    OutOfOrderSnapshotError,
    SnapshotValidationError,
    _MetricDefinition,
    _MetricSample,
)
from .validation import (
    _bool_db,
    _is_placeholder_interface_role,
    _iso,
    _iso_from_us,
    _required_text,
    _to_us,
    _utc_datetime,
)
from .validation import ValidationMixin


class IngestMixin(ValidationMixin):
    """Transactional snapshot ingestion, trip segmentation and gap tracking."""

    def _store_catalog(
        self,
        definitions: Mapping[str, _MetricDefinition],
        captured_us: int,
    ) -> None:
        for metric in definitions.values():
            for source in metric.sources.values():
                exact = {
                    "metric": metric.name,
                    "unit": metric.unit,
                    "value_type": metric.value_type,
                    "stale_after_ms": metric.stale_after_ms,
                    "source": source.name,
                    "bus": source.bus,
                    "quality": source.quality,
                    "provenance": source.provenance,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(exact, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                self._conn.execute(
                    """
                    INSERT INTO catalog_sources(
                        fingerprint,metric,unit,value_type,stale_after_ms,
                        source,bus,quality,provenance,first_seen_us,last_seen_us
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fingerprint) DO UPDATE SET last_seen_us=excluded.last_seen_us
                    """,
                    (
                        fingerprint,
                        metric.name,
                        metric.unit,
                        metric.value_type,
                        metric.stale_after_ms,
                        source.name,
                        source.bus,
                        source.quality,
                        source.provenance,
                        captured_us,
                        captured_us,
                    ),
                )

    def _activity_basis(
        self,
        samples: Mapping[str, _MetricSample],
        vehicle: Mapping[str, object],
    ) -> tuple[bool, str]:
        rpm = samples.get("engine.rpm")
        if (
            rpm is not None
            and rpm.freshness == "fresh"
            and rpm.value_num is not None
            and rpm.value_num >= self.config.running_rpm_threshold
        ):
            return True, "fresh_engine_rpm"
        speed = samples.get("vehicle.speed")
        if (
            speed is not None
            and speed.freshness == "fresh"
            and speed.value_num is not None
            and speed.value_num > self.config.moving_speed_threshold_mph
        ):
            return True, "fresh_vehicle_speed"
        if (
            vehicle.get("running") == 1
            and isinstance(vehicle.get("age_ms"), int)
            and vehicle["age_ms"] <= self.config.vehicle_state_max_age_seconds * 1000
            and vehicle.get("confidence") not in (None, "unknown", "stale")
        ):
            return True, "fresh_vehicle_running_state"
        return False, "no_fresh_running_or_moving_evidence"

    def _classify_regime(self, samples: Mapping[str, _MetricSample]) -> str:
        def numeric(name: str) -> float | None:
            sample = samples.get(name)
            if sample is None or sample.freshness != "fresh":
                return None
            return sample.value_num

        rpm = numeric("engine.rpm")
        speed = numeric("vehicle.speed")
        coolant = numeric("engine.coolant_temperature")
        if rpm is None:
            engine = "engine_unknown"
            rpm_band = "rpm_unknown"
        elif rpm < self.config.running_rpm_threshold:
            engine, rpm_band = "engine_off", "rpm_off"
        elif rpm < 1_000:
            engine, rpm_band = "engine_running", "rpm_idle"
        elif rpm < 2_200:
            engine, rpm_band = "engine_running", "rpm_low"
        elif rpm < 3_500:
            engine, rpm_band = "engine_running", "rpm_mid"
        else:
            engine, rpm_band = "engine_running", "rpm_high"
        if speed is None:
            motion = "speed_unknown"
        elif speed <= self.config.moving_speed_threshold_mph:
            motion = "stationary"
        elif speed < 35:
            motion = "urban"
        elif speed < 65:
            motion = "road"
        else:
            motion = "highway"
        if coolant is None:
            thermal = "thermal_unknown"
        elif coolant < 160:
            thermal = "cold"
        elif coolant <= 220:
            thermal = "warm"
        else:
            thermal = "hot"
        return ":".join((engine, motion, rpm_band, thermal))

    def _resolve_trip(
        self,
        captured_us: int,
        active: bool,
        basis: str,
    ) -> int | None:
        row = self._conn.execute(
            "SELECT * FROM trips WHERE ended_us IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        idle_us = int(round(self.config.trip_idle_timeout_seconds * MICROSECONDS))
        if row is not None and captured_us - row["last_active_us"] >= idle_us:
            self._conn.execute(
                """
                UPDATE trips
                SET ended_us=last_active_us, ended_at=last_active_at,
                    end_reason='activity_timeout'
                WHERE id=?
                """,
                (row["id"],),
            )
            row = None
        if active and row is None:
            cursor = self._conn.execute(
                """
                INSERT INTO trips(
                    started_us,started_at,last_active_us,last_active_at,start_basis
                ) VALUES(?,?,?,?,?)
                """,
                (captured_us, _iso_from_us(captured_us), captured_us, _iso_from_us(captured_us), basis),
            )
            return int(cursor.lastrowid)
        if row is None:
            return None
        if active:
            self._conn.execute(
                """
                UPDATE trips SET last_active_us=?,last_active_at=? WHERE id=?
                """,
                (captured_us, _iso_from_us(captured_us), row["id"]),
            )
        return int(row["id"])

    def _snapshot_ingest_context(
        self,
        snapshot: Mapping[str, object],
        captured_at: datetime | str | None,
        ingest_key: str | None,
    ) -> tuple[str | None, int | None, int, str, str]:
        if not isinstance(snapshot, Mapping):
            raise SnapshotValidationError("snapshot must be an object")
        instance, sequence, generated_ms = self._delivery(snapshot)
        if captured_at is None:
            if generated_ms is not None:
                captured = datetime.fromtimestamp(generated_ms / 1000, timezone.utc)
            else:
                captured = datetime.now(timezone.utc)
        else:
            captured = _utc_datetime(captured_at, "captured_at")
        captured_us = _to_us(captured)
        captured_iso = _iso(captured)
        if ingest_key is None:
            if instance is not None and sequence is not None:
                ingest_key = f"web:{instance}:{sequence}"
            else:
                ingest_key = f"captured:{captured_us}"
        if not isinstance(ingest_key, str) or not ingest_key.strip():
            raise SnapshotValidationError("ingest_key must be a nonempty string")
        return instance, sequence, captured_us, captured_iso, ingest_key

    def _parse_snapshot_metrics(
        self,
        snapshot: Mapping[str, object],
        definitions: Mapping[str, _MetricDefinition],
    ) -> tuple[
        dict[str, _MetricSample],
        dict[str, tuple[str, str, str] | None],
    ]:
        metrics_payload = snapshot.get("metrics")
        if not isinstance(metrics_payload, Mapping):
            raise SnapshotValidationError("snapshot.metrics must be an object")
        unknown = set(metrics_payload) - set(definitions)
        if unknown:
            raise SnapshotValidationError(
                f"metrics absent from catalog: {', '.join(sorted(map(str, unknown)))}"
            )
        samples: dict[str, _MetricSample] = {}
        gap_states: dict[str, tuple[str, str, str] | None] = {}
        for name, definition in definitions.items():
            payload = metrics_payload.get(name)
            if payload is None:
                gap_states[name] = (
                    "missing",
                    "metric_absent",
                    "catalog metric is absent from this snapshot",
                )
                continue
            if not isinstance(payload, Mapping):
                raise SnapshotValidationError(f"metric {name!r} must be an object")
            parsed = self._parse_metric(name, payload, definition)
            if isinstance(parsed, tuple):
                gap_states[name] = parsed
                continue
            samples[name] = parsed
            if parsed.freshness == "fresh":
                gap_states[name] = None
            elif parsed.freshness == "stale":
                gap_states[name] = (
                    "stale",
                    "stale_observation",
                    "the value is retained with its age but is not current evidence",
                )
            else:
                gap_states[name] = (
                    "undated",
                    "observation_time_unavailable",
                    "the value lacks a valid observation time or age",
                )
        return samples, gap_states

    def _insert_snapshot(
        self,
        *,
        captured_us: int,
        captured_iso: str,
        ingest_key: str,
        instance: str | None,
        sequence: int | None,
        definitions: Mapping[str, _MetricDefinition],
        active: bool,
        activity_basis: str,
        vehicle: Mapping[str, object],
        regime: str,
    ) -> tuple[int | None, int]:
        latest = self._conn.execute(
            "SELECT captured_us FROM snapshots ORDER BY captured_us DESC LIMIT 1"
        ).fetchone()
        if latest is not None and captured_us <= latest["captured_us"]:
            raise OutOfOrderSnapshotError(
                "snapshot time must be newer than the latest successful ingest"
            )
        self._store_catalog(definitions, captured_us)
        trip_id = self._resolve_trip(captured_us, active, activity_basis)
        cursor = self._conn.execute(
            """
            INSERT INTO snapshots(
                ingest_key,captured_us,captured_at,source_instance,source_sequence,
                vehicle_state,vehicle_running,vehicle_confidence,vehicle_basis,
                vehicle_observed_at,vehicle_age_ms,regime,trip_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ingest_key,
                captured_us,
                captured_iso,
                instance,
                sequence,
                vehicle["state"],
                vehicle["running"],
                vehicle["confidence"],
                vehicle["basis"],
                vehicle["observed_at"],
                vehicle["age_ms"],
                regime,
                trip_id,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        return trip_id, snapshot_id

    def _store_metric_samples(
        self,
        *,
        snapshot_id: int,
        trip_id: int | None,
        captured_us: int,
        samples: Mapping[str, _MetricSample],
        regime: str,
    ) -> None:
        for sample in samples.values():
            self._conn.execute(
                """
                INSERT INTO metric_samples(
                    snapshot_id,trip_id,captured_us,metric,value_kind,value_num,
                    value_text,value_bool,unit,source,bus,acquisition,interface_mode,
                    quality,provenance,observed_us,observed_at,source_age_ms,
                    reported_stale,freshness,regime
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    trip_id,
                    captured_us,
                    sample.metric,
                    sample.value_kind,
                    sample.value_num,
                    sample.value_text,
                    sample.value_bool,
                    sample.unit,
                    sample.source,
                    sample.bus,
                    sample.acquisition,
                    sample.interface_mode,
                    sample.quality,
                    sample.provenance,
                    sample.observed_us,
                    sample.observed_at,
                    sample.source_age_ms,
                    sample.reported_stale,
                    sample.freshness,
                    regime,
                ),
            )

    def _update_metric_gaps(
        self,
        gap_states: Mapping[str, tuple[str, str, str] | None],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        for metric, gap in gap_states.items():
            self._update_gap(
                table="metric_gaps",
                key_column="metric",
                key=metric,
                gap=gap,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )

    def ingest_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        captured_at: datetime | str | None = None,
        ingest_key: str | None = None,
    ) -> IngestResult:
        """Validate and atomically ingest one broker snapshot.

        ``captured_at`` is the historian receipt/generation time, never a
        substitute for each metric's own ``observed_at``.  When omitted, the
        web-delivery wall timestamp is used; a direct broker snapshot falls
        back to the current UTC time.
        """

        instance, sequence, captured_us, captured_iso, ingest_key = (
            self._snapshot_ingest_context(snapshot, captured_at, ingest_key)
        )
        definitions = self._parse_catalog(snapshot)
        samples, gap_states = self._parse_snapshot_metrics(snapshot, definitions)
        vehicle = self._vehicle_fields(snapshot)
        active, activity_basis = self._activity_basis(samples, vehicle)
        regime = self._classify_regime(samples)

        with self._lock, self._conn:
            duplicate = self._conn.execute(
                "SELECT * FROM snapshots WHERE ingest_key=?", (ingest_key,)
            ).fetchone()
            if duplicate is not None:
                return IngestResult(
                    snapshot_id=int(duplicate["id"]),
                    captured_at=duplicate["captured_at"],
                    duplicate=True,
                    trip_id=duplicate["trip_id"],
                    regime=duplicate["regime"],
                    stored_samples=0,
                    metric_gap_count=self._active_gap_count("metric_gaps"),
                    interface_gap_count=self._active_gap_count("interface_gaps"),
                )
            trip_id, snapshot_id = self._insert_snapshot(
                captured_us=captured_us,
                captured_iso=captured_iso,
                ingest_key=ingest_key,
                instance=instance,
                sequence=sequence,
                definitions=definitions,
                active=active,
                activity_basis=activity_basis,
                vehicle=vehicle,
                regime=regime,
            )
            self._store_metric_samples(
                snapshot_id=snapshot_id,
                trip_id=trip_id,
                captured_us=captured_us,
                samples=samples,
                regime=regime,
            )
            self._update_metric_gaps(gap_states, captured_us, snapshot_id)
            self._ingest_interfaces(snapshot, captured_us, snapshot_id)
            self._ingest_system_health(snapshot, captured_us, snapshot_id)
            self._ingest_usb_can_monitor(snapshot, captured_us, snapshot_id)
            self._ingest_data_quality(snapshot, captured_us, snapshot_id)
            if trip_id is not None:
                self._conn.execute(
                    "UPDATE trips SET snapshot_count=snapshot_count+1 WHERE id=?",
                    (trip_id,),
                )
            return IngestResult(
                snapshot_id=snapshot_id,
                captured_at=captured_iso,
                duplicate=False,
                trip_id=trip_id,
                regime=regime,
                stored_samples=len(samples),
                metric_gap_count=self._active_gap_count("metric_gaps"),
                interface_gap_count=self._active_gap_count("interface_gaps"),
            )

    def _active_gap_count(self, table: str) -> int:
        if table not in ("metric_gaps", "interface_gaps"):
            raise ValueError("unknown gap table")
        return int(
            self._conn.execute(
                f"SELECT count(*) FROM {table} WHERE ended_us IS NULL"
            ).fetchone()[0]
        )

    def _update_gap(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        gap: tuple[str, str, str] | tuple[str, str] | None,
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        if (table, key_column) not in (
            ("metric_gaps", "metric"),
            ("interface_gaps", "role"),
        ):
            raise ValueError("invalid gap table")
        current = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_column}=? AND ended_us IS NULL",
            (key,),
        ).fetchone()
        if gap is None:
            if current is not None:
                self._conn.execute(
                    f"UPDATE {table} SET ended_us=?,ended_at=? WHERE id=?",
                    (captured_us, _iso_from_us(captured_us), current["id"]),
                )
            return
        state, reason = gap[0], gap[1]
        detail = gap[2] if len(gap) == 3 else ""
        same = current is not None and (
            current["state"] == state
            and current["reason"] == reason
            and (table != "metric_gaps" or current["detail"] == detail)
        )
        if same:
            self._conn.execute(
                f"""
                UPDATE {table}
                SET last_seen_us=?,last_seen_at=?,last_snapshot_id=?,
                    observation_count=observation_count+1
                WHERE id=?
                """,
                (captured_us, _iso_from_us(captured_us), snapshot_id, current["id"]),
            )
            return
        if current is not None:
            self._conn.execute(
                f"UPDATE {table} SET ended_us=?,ended_at=? WHERE id=?",
                (captured_us, _iso_from_us(captured_us), current["id"]),
            )
        if table == "metric_gaps":
            columns = (
                "metric,state,reason,detail,started_us,started_at,last_seen_us,"
                "last_seen_at,first_snapshot_id,last_snapshot_id"
            )
            values: list[object] = [
                key,
                state,
                reason,
                detail,
                captured_us,
                _iso_from_us(captured_us),
                captured_us,
                _iso_from_us(captured_us),
                snapshot_id,
                snapshot_id,
            ]
        else:
            columns = (
                "role,state,reason,started_us,started_at,last_seen_us,"
                "last_seen_at,first_snapshot_id,last_snapshot_id"
            )
            values = [
                key,
                state,
                reason,
                captured_us,
                _iso_from_us(captured_us),
                captured_us,
                _iso_from_us(captured_us),
                snapshot_id,
                snapshot_id,
            ]
        placeholders = ",".join("?" for _ in values)
        self._conn.execute(
            f"INSERT INTO {table}({columns}) VALUES({placeholders})",
            values,
        )

    def _retire_placeholder_interface_roles(
        self,
        *,
        captured_us: int,
        snapshot_id: int,
    ) -> set[str]:
        """Close legacy channel-keyed gaps and return durable seen roles.

        Early role-aware startup snapshots could be stored under an ephemeral
        ``canN`` key before reconciliation supplied the logical roles.  Keep
        those rows as provenance, but never use them to create an endless
        ``interface_role_absent`` interval.  Including open gaps in the repair
        set also heals databases whose old raw samples were already pruned.
        """

        previously_seen = {
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT role FROM interface_samples"
            )
        }
        open_gap_roles = {
            row[0]
            for row in self._conn.execute(
                "SELECT role FROM interface_gaps WHERE ended_us IS NULL"
            )
        }
        placeholders = {
            role
            for role in previously_seen | open_gap_roles
            if _is_placeholder_interface_role(role)
        }
        for role in sorted(placeholders):
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=None,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
        return previously_seen - placeholders

    def _ingest_interface(
        self,
        *,
        role: str,
        payload: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        topology = payload.get("topology")
        topology = topology if isinstance(topology, Mapping) else {}
        health, reason = self._interface_health(payload)
        bitrate = payload.get("bitrate")
        if not isinstance(bitrate, int) or isinstance(bitrate, bool) or bitrate <= 0:
            bitrate = None
        self._conn.execute(
            """
            INSERT INTO interface_samples(
                snapshot_id,role,captured_us,channel,usb_serial,bus,
                adapter_present,up,bitrate,listen_only,controller_state,
                topology_usable,health,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                role,
                captured_us,
                payload.get("channel") if isinstance(payload.get("channel"), str) else None,
                payload.get("usb_serial") if isinstance(payload.get("usb_serial"), str) else None,
                topology.get("bus") if isinstance(topology.get("bus"), str) else None,
                _bool_db(payload.get("adapter_present")),
                _bool_db(payload.get("up")),
                bitrate,
                _bool_db(payload.get("listen_only")),
                (
                    payload.get("controller_state")
                    if isinstance(payload.get("controller_state"), str)
                    else None
                ),
                _bool_db(topology.get("usable")),
                health,
                reason,
            ),
        )
        dev_id = payload.get("usb_dev_id")
        if not isinstance(dev_id, int) or isinstance(dev_id, bool) or dev_id < 0:
            dev_id = None
        self._conn.execute(
            """
            INSERT INTO interface_role_details(
                snapshot_id,role,resolution,role_reason,detail,usb_dev_id,
                topology_generation
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                role,
                (
                    payload.get("resolution")
                    if isinstance(payload.get("resolution"), str)
                    else None
                ),
                (
                    payload.get("role_reason")
                    if isinstance(payload.get("role_reason"), str)
                    else None
                ),
                (
                    payload.get("detail")
                    if isinstance(payload.get("detail"), str)
                    else None
                ),
                dev_id,
                (
                    payload.get("topology_generation")
                    if isinstance(payload.get("topology_generation"), str)
                    else None
                ),
            ),
        )
        gap = None if health == "healthy" else (health, reason)
        self._update_gap(
            table="interface_gaps",
            key_column="role",
            key=role,
            gap=gap,
            captured_us=captured_us,
            snapshot_id=snapshot_id,
        )

    def _ingest_interfaces(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        interfaces = self._interface_payloads(snapshot)
        previously_seen = self._retire_placeholder_interface_roles(
            captured_us=captured_us,
            snapshot_id=snapshot_id,
        )
        if not interfaces:
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key="interface-status",
                gap=("missing", "interface_status_missing"),
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
            for role in previously_seen:
                self._update_gap(
                    table="interface_gaps",
                    key_column="role",
                    key=role,
                    gap=("missing", "interface_role_absent"),
                    captured_us=captured_us,
                    snapshot_id=snapshot_id,
                )
            return
        self._update_gap(
            table="interface_gaps",
            key_column="role",
            key="interface-status",
            gap=None,
            captured_us=captured_us,
            snapshot_id=snapshot_id,
        )
        for role in sorted(previously_seen - set(interfaces)):
            self._update_gap(
                table="interface_gaps",
                key_column="role",
                key=role,
                gap=("missing", "interface_role_absent"),
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
        for role, payload in interfaces.items():
            self._ingest_interface(
                role=role,
                payload=payload,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )

    def _ingest_system_health(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        probe = status.get("interface_probe")
        if isinstance(probe, Mapping):
            safe_probe = {key:probe.get(key) for key in ("state","elapsed_seconds","startup_grace_seconds","producer_instance")}
            self._conn.execute("INSERT INTO interface_probe_samples(snapshot_id,probe_json) VALUES(?,?)",
                               (snapshot_id,json.dumps(safe_probe,sort_keys=True)))
        interface = status.get("interface")
        interface = interface if isinstance(interface, Mapping) else {}
        role_snapshot = interface.get("role_interfaces")
        role_snapshot = role_snapshot if isinstance(role_snapshot, Mapping) else {}
        generation = role_snapshot.get("generation")
        generation = generation if isinstance(generation, str) and generation else None
        issues = self._safe_json_list(role_snapshot.get("issues"))
        inhibits = self._safe_json_list(interface.get("active_inhibits"))
        active_drive = status.get("active_drive")
        active_drive = active_drive if isinstance(active_drive, Mapping) else {}
        restoration_failed = active_drive.get("restoration_failed")
        self._conn.execute(
            """
            INSERT INTO system_health_samples(
                snapshot_id,captured_us,topology_generation,issues_json,
                active_inhibits_json,restoration_failed
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                captured_us,
                generation,
                json.dumps(issues, sort_keys=True, separators=(",", ":")),
                json.dumps(inhibits, sort_keys=True, separators=(",", ":")),
                _bool_db(restoration_failed),
            ),
        )

    def _record_usb_can_recovery_event(
        self,
        *,
        row: Mapping[str, object],
        incident_id: str,
        affected: list[str],
        producer_instance: str,
        boot_id: str,
        generation: str,
        resolved_at: str,
        captured_us: int,
        snapshot_id: int,
    ) -> str:
        event_identity = {
            "incident_id": incident_id,
            "producer_instance": producer_instance,
            "topology_generation": generation,
            "resolution": "authoritative_healthy_exact_roles_after_monitor_restart",
        }
        event_id = "usb-can-event-v1:" + hashlib.sha256(
            json.dumps(
                event_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        recovery = {
            "schema_version": 1,
            "event_id": event_id,
            "boot_id": boot_id,
            "kernel_seqnum": None,
            "kind": "usb_can_recovered",
            "action": "reconcile",
            "scope": row["scope"],
            "devpath": row["scope"],
            "usb_vid": "1d50",
            "usb_pid": "606f",
            "usb_serial": None,
            "affected_serials": affected,
            "occurred_at": resolved_at,
            "observed_monotonic": 0.0,
            "monotonic_timestamp_available": False,
            "source": "serial_role_reconciliation",
            "receive_only": True,
            "hardware_action": False,
            "recovery_basis": (
                "authoritative healthy exact-role snapshot after monitor restart"
            ),
            "producer_instance": producer_instance,
        }
        recovery_json = json.dumps(
            recovery,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self._conn.execute(
            """
            INSERT INTO usb_can_events(
                event_id,occurred_us,occurred_at,boot_id,kernel_seqnum,kind,
                action,scope,devpath,usb_vid,usb_pid,usb_serial,
                affected_serials_json,source,payload_json,
                first_snapshot_id,last_snapshot_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                last_snapshot_id=excluded.last_snapshot_id
            """,
            (
                event_id,
                captured_us,
                resolved_at,
                boot_id,
                None,
                "usb_can_recovered",
                "reconcile",
                row["scope"],
                row["scope"],
                "1d50",
                "606f",
                None,
                json.dumps(affected, separators=(",", ":")),
                "serial_role_reconciliation",
                recovery_json,
                snapshot_id,
                snapshot_id,
            ),
        )
        return event_id

    def _resolve_usb_can_incident(
        self,
        *,
        row: Mapping[str, object],
        incident_id: str,
        event_id: str,
        resolved_at: str,
        captured_us: int,
        producer_instance: str,
        snapshot_id: int,
    ) -> None:
        incident = self._usb_can_payload(row)
        incident.update(
            {
                "state": "resolved",
                "last_event_id": event_id,
                "last_seen_at": resolved_at,
                "resolved_event_id": event_id,
                "resolved_at": resolved_at,
                "resolution": (
                    "authoritative_healthy_exact_roles_after_monitor_restart"
                ),
                "notification_eligible": False,
                "event_count": int(row["event_count"]) + 1,
                "resolved_by_producer_instance": producer_instance,
            }
        )
        self._conn.execute(
            """
            UPDATE usb_can_incidents
            SET state='resolved',last_seen_us=?,last_seen_at=?,resolved_us=?,
                resolved_at=?,resolution=?,event_count=event_count+1,
                last_event_id=?,resolved_event_id=?,payload_json=?,
                last_snapshot_id=?
            WHERE incident_id=? AND state='active'
            """,
            (
                captured_us,
                resolved_at,
                captured_us,
                resolved_at,
                "authoritative_healthy_exact_roles_after_monitor_restart",
                event_id,
                event_id,
                json.dumps(
                    incident,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                snapshot_id,
                incident_id,
            ),
        )

    def _resolve_previous_usb_can_incidents(
        self,
        snapshot: Mapping[str, object],
        *,
        captured_us: int,
        snapshot_id: int,
        producer_instance: str,
        boot_id: str,
        current_incident_ids: set[str],
    ) -> None:
        """Retire prior-process incidents from authoritative healthy roles.

        A broker restart necessarily empties the monitor's in-memory active
        map.  Absence from a new process is not recovery evidence by itself;
        only a running receive-only monitor plus a fresh, exact, all-role-safe
        broker snapshot may close the prior incident.  Its original removal
        evidence and incident row remain durable.
        """

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        monitor_status = status.get("usb_can_monitor")
        monitor_status = (
            monitor_status if isinstance(monitor_status, Mapping) else {}
        )
        if not (
            monitor_status.get("state") == "running"
            and monitor_status.get("receive_only") is True
            and monitor_status.get("hardware_actions") is False
            and monitor_status.get("producer_instance") == producer_instance
            and monitor_status.get("boot_id") == boot_id
        ):
            return
        role_snapshot = status.get("interface")
        role_snapshot = (
            role_snapshot.get("role_interfaces")
            if isinstance(role_snapshot, Mapping)
            else None
        )
        generation = (
            role_snapshot.get("generation")
            if isinstance(role_snapshot, Mapping)
            and isinstance(role_snapshot.get("generation"), str)
            else "generation-unavailable"
        )
        rows = self._conn.execute(
            """
            SELECT * FROM usb_can_incidents
            WHERE state='active' AND producer_instance<>?
            ORDER BY opened_us,incident_id
            """,
            (producer_instance,),
        ).fetchall()
        resolved_at = _iso_from_us(captured_us)
        for row in rows:
            incident_id = str(row["incident_id"])
            if incident_id in current_incident_ids:
                continue
            try:
                affected = json.loads(row["affected_serials_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(affected, list) or not affected or not all(
                isinstance(serial, str)
                and self._usb_can_serial_health_proven(snapshot, serial)
                for serial in affected
            ):
                continue
            event_id = self._record_usb_can_recovery_event(
                row=row,
                incident_id=incident_id,
                affected=affected,
                producer_instance=producer_instance,
                boot_id=boot_id,
                generation=generation,
                resolved_at=resolved_at,
                captured_us=captured_us,
                snapshot_id=snapshot_id,
            )
            self._resolve_usb_can_incident(
                row=row,
                incident_id=incident_id,
                event_id=event_id,
                resolved_at=resolved_at,
                captured_us=captured_us,
                producer_instance=producer_instance,
                snapshot_id=snapshot_id,
            )

    def _ingest_usb_can_monitor_batch(
        self,
        snapshot: Mapping[str, object],
        monitor: object,
        captured_us: int,
        snapshot_id: int,
    ) -> tuple[str, str, list[object], list[object]]:
        if not isinstance(monitor, Mapping):
            raise SnapshotValidationError("_usb_can_monitor must be an object")
        if monitor.get("schema_version") != 1:
            raise SnapshotValidationError("unsupported USB CAN monitor schema")
        source = _required_text(monitor.get("source"), "_usb_can_monitor.source")
        if len(source) > 128:
            raise SnapshotValidationError("_usb_can_monitor.source is oversized")
        producer_instance = _required_text(
            monitor.get("producer_instance"),
            "_usb_can_monitor.producer_instance",
        )
        boot_id = _required_text(monitor.get("boot_id"), "_usb_can_monitor.boot_id")
        if len(producer_instance) > 128 or len(boot_id) > 128:
            raise SnapshotValidationError(
                "_usb_can_monitor producer or boot identity is oversized"
            )
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        monitor_status = status.get("usb_can_monitor")
        if not isinstance(monitor_status, Mapping):
            raise SnapshotValidationError(
                "status.usb_can_monitor must accompany its persistence batch"
            )
        if (
            monitor_status.get("producer_instance") != producer_instance
            or monitor_status.get("boot_id") != boot_id
            or monitor_status.get("receive_only") is not True
            or monitor_status.get("hardware_actions") is not False
        ):
            raise SnapshotValidationError(
                "status.usb_can_monitor does not match its safe producer batch"
            )
        dropped = monitor.get("dropped_event_count", 0)
        if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
            raise SnapshotValidationError(
                "_usb_can_monitor.dropped_event_count must be non-negative"
            )
        events = monitor.get("events", [])
        incidents = monitor.get("incidents", [])
        if not isinstance(events, list) or len(events) > 512:
            raise SnapshotValidationError(
                "_usb_can_monitor.events must contain at most 512 events"
            )
        if not isinstance(incidents, list) or len(incidents) > 128:
            raise SnapshotValidationError(
                "_usb_can_monitor.incidents must contain at most 128 incidents"
            )
        self._conn.execute(
            """
            INSERT INTO usb_can_monitor_samples(
                snapshot_id,captured_us,source,producer_instance,
                dropped_event_count,pending_event_count
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                captured_us,
                source,
                producer_instance,
                dropped,
                len(events),
            ),
        )
        return producer_instance, boot_id, events, incidents

    def _usb_can_event_fields(
        self,
        item: object,
        index: int,
        boot_id: str,
        seen_event_ids: set[str],
        allowed_event_kinds: set[str],
    ) -> tuple[
        str, str, str, str, str, str, str, str, int, str, list[str], str | None
    ]:
        prefix = f"_usb_can_monitor.events[{index}]"
        if not isinstance(item, Mapping):
            raise SnapshotValidationError(f"{prefix} must be an object")
        if item.get("schema_version") != 1:
            raise SnapshotValidationError(f"{prefix} has unsupported schema")
        if item.get("receive_only") is not True or item.get("hardware_action") is not False:
            raise SnapshotValidationError(
                f"{prefix} does not prove receive-only/no-action semantics"
            )
        event_id = _required_text(item.get("event_id"), f"{prefix}.event_id")
        if (
            not event_id.startswith("usb-can-event-v1:")
            or len(event_id) > 128
            or event_id in seen_event_ids
        ):
            raise SnapshotValidationError(f"{prefix}.event_id is invalid or repeated")
        seen_event_ids.add(event_id)
        kind = _required_text(item.get("kind"), f"{prefix}.kind")
        if kind not in allowed_event_kinds:
            raise SnapshotValidationError(f"{prefix}.kind is not allowlisted")
        action = _required_text(item.get("action"), f"{prefix}.action")
        if action not in ("add", "remove", "reconcile"):
            raise SnapshotValidationError(f"{prefix}.action is invalid")
        event_boot_id = _required_text(
            item.get("boot_id"), f"{prefix}.boot_id"
        )
        if event_boot_id != boot_id:
            raise SnapshotValidationError(
                f"{prefix}.boot_id does not match its producer batch"
            )
        scope = _required_text(item.get("scope"), f"{prefix}.scope")
        devpath = _required_text(item.get("devpath"), f"{prefix}.devpath")
        event_source = _required_text(item.get("source"), f"{prefix}.source")
        if event_source not in (
            "kernel_kobject_uevent",
            "serial_role_reconciliation",
        ):
            raise SnapshotValidationError(f"{prefix}.source is not allowlisted")
        if any(
            len(value) > maximum
            for value, maximum in (
                (event_boot_id, 128),
                (scope, 320),
                (devpath, 4096),
                (event_source, 128),
            )
        ):
            raise SnapshotValidationError(f"{prefix} contains oversized text")
        occurred_us, occurred_at = self._usb_can_time(
            item.get("occurred_at"), f"{prefix}.occurred_at"
        )
        affected = self._usb_can_serials(
            item.get("affected_serials", []), f"{prefix}.affected_serials"
        )
        observed_monotonic = item.get("observed_monotonic")
        if (
            not isinstance(observed_monotonic, (int, float))
            or isinstance(observed_monotonic, bool)
            or not math.isfinite(float(observed_monotonic))
            or float(observed_monotonic) < 0
        ):
            raise SnapshotValidationError(
                f"{prefix}.observed_monotonic must be finite and non-negative"
            )
        kernel_seqnum = item.get("kernel_seqnum")
        if kernel_seqnum is not None:
            kernel_seqnum = _required_text(
                kernel_seqnum, f"{prefix}.kernel_seqnum"
            )
            if len(kernel_seqnum) > 64:
                raise SnapshotValidationError(
                    f"{prefix}.kernel_seqnum is oversized"
                )
        return (
            prefix, event_id, kind, action, event_boot_id, scope, devpath,
            event_source, occurred_us, occurred_at, affected, kernel_seqnum,
        )

    def _usb_can_event_payload(
        self,
        item: Mapping[str, object],
        prefix: str,
    ) -> tuple[dict[str, str | None], str]:
        optional: dict[str, str | None] = {}
        for field, maximum in (
            ("usb_vid", 4),
            ("usb_pid", 4),
            ("usb_serial", 256),
        ):
            value = item.get(field)
            if value is not None:
                value = _required_text(value, f"{prefix}.{field}")
                if len(value) > maximum:
                    raise SnapshotValidationError(f"{prefix}.{field} is oversized")
            optional[field] = value
        try:
            payload_json = json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"{prefix} is not bounded JSON data: {exc}"
            ) from None
        if len(payload_json.encode("utf-8")) > 16 * 1024:
            raise SnapshotValidationError(f"{prefix} JSON payload is oversized")
        return optional, payload_json

    def _store_usb_can_event(
        self,
        *,
        event_id: str,
        occurred_us: int,
        occurred_at: str,
        event_boot_id: str,
        kernel_seqnum: str | None,
        kind: str,
        action: str,
        scope: str,
        devpath: str,
        optional: Mapping[str, str | None],
        affected: list[str],
        event_source: str,
        payload_json: str,
        snapshot_id: int,
    ) -> None:
        existing = self._conn.execute(
            "SELECT payload_json FROM usb_can_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None and existing["payload_json"] != payload_json:
            raise SnapshotValidationError(
                f"event identity collision for {event_id!r}"
            )
        self._conn.execute(
            """
            INSERT INTO usb_can_events(
                event_id,occurred_us,occurred_at,boot_id,kernel_seqnum,kind,
                action,scope,devpath,usb_vid,usb_pid,usb_serial,
                affected_serials_json,source,payload_json,
                first_snapshot_id,last_snapshot_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                last_snapshot_id=excluded.last_snapshot_id
            """,
            (
                event_id,
                occurred_us,
                occurred_at,
                event_boot_id,
                kernel_seqnum,
                kind,
                action,
                scope,
                devpath,
                optional["usb_vid"],
                optional["usb_pid"],
                optional["usb_serial"],
                json.dumps(affected, separators=(",", ":")),
                event_source,
                payload_json,
                snapshot_id,
                snapshot_id,
            ),
        )

    def _ingest_usb_can_events(
        self,
        events: list[object],
        boot_id: str,
        snapshot_id: int,
    ) -> None:
        allowed_event_kinds = {
            "usb_parent_hub_removed",
            "usb_parent_hub_added",
            "usb_can_adapter_removed",
            "usb_can_adapter_added",
            "usb_can_netdev_removed",
            "usb_can_netdev_added",
            "usb_can_recovered",
        }
        seen_event_ids: set[str] = set()
        for index, item in enumerate(events):
            (
                prefix, event_id, kind, action, event_boot_id, scope, devpath,
                event_source, occurred_us, occurred_at, affected, kernel_seqnum,
            ) = self._usb_can_event_fields(
                item, index, boot_id, seen_event_ids, allowed_event_kinds
            )
            optional, payload_json = self._usb_can_event_payload(item, prefix)
            self._store_usb_can_event(
                event_id=event_id,
                occurred_us=occurred_us,
                occurred_at=occurred_at,
                event_boot_id=event_boot_id,
                kernel_seqnum=kernel_seqnum,
                kind=kind,
                action=action,
                scope=scope,
                devpath=devpath,
                optional=optional,
                affected=affected,
                event_source=event_source,
                payload_json=payload_json,
                snapshot_id=snapshot_id,
            )

    def _usb_can_incident_identity(
        self,
        item: object,
        index: int,
        producer_instance: str,
        seen_incident_ids: set[str],
        current_active_incident_ids: set[str],
    ) -> tuple[
        str, str, str, str, str, str, str, str, str, list[str], int, str, int, str
    ]:
        prefix = f"_usb_can_monitor.incidents[{index}]"
        if not isinstance(item, Mapping):
            raise SnapshotValidationError(f"{prefix} must be an object")
        if item.get("schema_version") != 1:
            raise SnapshotValidationError(f"{prefix} has unsupported schema")
        incident_id = _required_text(
            item.get("incident_id"), f"{prefix}.incident_id"
        )
        if (
            not incident_id.startswith("usb-can-incident-v1:")
            or len(incident_id) > 128
            or incident_id in seen_incident_ids
        ):
            raise SnapshotValidationError(
                f"{prefix}.incident_id is invalid or repeated"
            )
        seen_incident_ids.add(incident_id)
        state = _required_text(item.get("state"), f"{prefix}.state")
        if state not in ("active", "resolved"):
            raise SnapshotValidationError(f"{prefix}.state is invalid")
        if state == "active":
            current_active_incident_ids.add(incident_id)
        kind = _required_text(item.get("kind"), f"{prefix}.kind")
        if kind not in (
            "usb_parent_hub_removed",
            "usb_can_adapter_removed",
            "usb_can_netdev_removed",
        ):
            raise SnapshotValidationError(f"{prefix}.kind is invalid")
        scope = _required_text(item.get("scope"), f"{prefix}.scope")
        incident_source = _required_text(
            item.get("source"), f"{prefix}.source"
        )
        if incident_source != "kernel_kobject_uevent":
            raise SnapshotValidationError(f"{prefix}.source is invalid")
        incident_producer = _required_text(
            item.get("producer_instance"), f"{prefix}.producer_instance"
        )
        if incident_producer != producer_instance:
            raise SnapshotValidationError(
                f"{prefix}.producer_instance does not match its snapshot"
            )
        opened_event_id = _required_text(
            item.get("opened_event_id"), f"{prefix}.opened_event_id"
        )
        last_event_id = _required_text(
            item.get("last_event_id"), f"{prefix}.last_event_id"
        )
        affected = self._usb_can_serials(
            item.get("affected_serials", []), f"{prefix}.affected_serials"
        )
        opened_us, opened_at = self._usb_can_time(
            item.get("opened_at"), f"{prefix}.opened_at"
        )
        last_seen_us, last_seen_at = self._usb_can_time(
            item.get("last_seen_at"), f"{prefix}.last_seen_at"
        )
        return (
            prefix, incident_id, state, kind, scope, incident_source,
            incident_producer, opened_event_id, last_event_id, affected,
            opened_us, opened_at, last_seen_us, last_seen_at,
        )

    def _usb_can_incident_state(
        self,
        item: Mapping[str, object],
        prefix: str,
        state: str,
        opened_us: int,
        last_seen_us: int,
    ) -> tuple[int | None, str | None, str | None, str | None, int, int, str]:
        if last_seen_us < opened_us:
            raise SnapshotValidationError(f"{prefix} predates its opening")
        resolved_at_value = item.get("resolved_at")
        if resolved_at_value is None:
            resolved_us = None
            resolved_at = None
        else:
            resolved_us, resolved_at = self._usb_can_time(
                resolved_at_value, f"{prefix}.resolved_at"
            )
        resolution = item.get("resolution")
        resolved_event_id = item.get("resolved_event_id")
        if state == "resolved":
            resolution = _required_text(resolution, f"{prefix}.resolution")
            resolved_event_id = _required_text(
                resolved_event_id, f"{prefix}.resolved_event_id"
            )
            if resolved_us is None or resolved_us < opened_us:
                raise SnapshotValidationError(
                    f"{prefix}.resolved_at is invalid"
                )
        elif any(
            value is not None
            for value in (resolved_us, resolution, resolved_event_id)
        ):
            raise SnapshotValidationError(
                f"{prefix} active incident carries resolution fields"
            )
        notification_eligible = item.get("notification_eligible")
        if type(notification_eligible) is not bool or notification_eligible != (
            state == "active"
        ):
            raise SnapshotValidationError(
                f"{prefix}.notification_eligible is inconsistent"
            )
        event_count = item.get("event_count")
        reappearance_count = item.get("reappearance_count", 0)
        if (
            not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count < 1
            or not isinstance(reappearance_count, int)
            or isinstance(reappearance_count, bool)
            or reappearance_count < 0
        ):
            raise SnapshotValidationError(f"{prefix} counters are invalid")
        try:
            payload_json = json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"{prefix} is not bounded JSON data: {exc}"
            ) from None
        if len(payload_json.encode("utf-8")) > 16 * 1024:
            raise SnapshotValidationError(f"{prefix} JSON payload is oversized")
        return (
            resolved_us, resolved_at, resolution, resolved_event_id,
            event_count, reappearance_count, payload_json,
        )

    def _store_usb_can_incident(
        self,
        *,
        incident_id: str, state: str, kind: str, scope: str,
        opened_us: int, opened_at: str,
        last_seen_us: int, last_seen_at: str,
        resolved_us: int | None, resolved_at: str | None,
        resolution: str | None, affected: list[str],
        event_count: int, reappearance_count: int,
        opened_event_id: str, last_event_id: str,
        resolved_event_id: str | None,
        incident_source: str, incident_producer: str,
        payload_json: str, snapshot_id: int, prefix: str,
    ) -> None:
        existing = self._conn.execute(
            """
            SELECT scope,opened_us,opened_event_id,affected_serials_json
            FROM usb_can_incidents WHERE incident_id=?
            """,
            (incident_id,),
        ).fetchone()
        if existing is not None and (
            existing["scope"] != scope
            or int(existing["opened_us"]) != opened_us
            or existing["opened_event_id"] != opened_event_id
            or existing["affected_serials_json"]
            != json.dumps(affected, separators=(",", ":"))
        ):
            raise SnapshotValidationError(
                f"incident identity collision for {incident_id!r}"
            )
        self._conn.execute(
            """
            INSERT INTO usb_can_incidents(
                incident_id,state,kind,scope,opened_us,opened_at,last_seen_us,
                last_seen_at,resolved_us,resolved_at,resolution,
                affected_serials_json,event_count,reappearance_count,
                opened_event_id,last_event_id,resolved_event_id,source,
                producer_instance,payload_json,first_snapshot_id,last_snapshot_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_id) DO UPDATE SET
                state=CASE
                    WHEN usb_can_incidents.state='resolved' THEN 'resolved'
                    ELSE excluded.state
                END,
                last_seen_us=MAX(usb_can_incidents.last_seen_us,excluded.last_seen_us),
                last_seen_at=CASE
                    WHEN excluded.last_seen_us >= usb_can_incidents.last_seen_us
                    THEN excluded.last_seen_at ELSE usb_can_incidents.last_seen_at
                END,
                resolved_us=COALESCE(usb_can_incidents.resolved_us,excluded.resolved_us),
                resolved_at=COALESCE(usb_can_incidents.resolved_at,excluded.resolved_at),
                resolution=COALESCE(usb_can_incidents.resolution,excluded.resolution),
                event_count=MAX(usb_can_incidents.event_count,excluded.event_count),
                reappearance_count=MAX(
                    usb_can_incidents.reappearance_count,
                    excluded.reappearance_count
                ),
                last_event_id=excluded.last_event_id,
                resolved_event_id=COALESCE(
                    usb_can_incidents.resolved_event_id,
                    excluded.resolved_event_id
                ),
                payload_json=CASE
                    WHEN usb_can_incidents.state='resolved'
                    THEN usb_can_incidents.payload_json ELSE excluded.payload_json
                END,
                last_snapshot_id=excluded.last_snapshot_id
            """,
            (
                incident_id,
                state,
                kind,
                scope,
                opened_us,
                opened_at,
                last_seen_us,
                last_seen_at,
                resolved_us,
                resolved_at,
                resolution,
                json.dumps(affected, separators=(",", ":")),
                event_count,
                reappearance_count,
                opened_event_id,
                last_event_id,
                resolved_event_id,
                incident_source,
                incident_producer,
                payload_json,
                snapshot_id,
                snapshot_id,
            ),
        )

    def _ingest_usb_can_incidents(
        self,
        incidents: list[object],
        producer_instance: str,
        snapshot_id: int,
    ) -> set[str]:
        seen_incident_ids: set[str] = set()
        current_active_incident_ids: set[str] = set()
        for index, item in enumerate(incidents):
            (
                prefix, incident_id, state, kind, scope, incident_source,
                incident_producer, opened_event_id, last_event_id, affected,
                opened_us, opened_at, last_seen_us, last_seen_at,
            ) = self._usb_can_incident_identity(
                item,
                index,
                producer_instance,
                seen_incident_ids,
                current_active_incident_ids,
            )
            (
                resolved_us, resolved_at, resolution, resolved_event_id,
                event_count, reappearance_count, payload_json,
            ) = self._usb_can_incident_state(
                item, prefix, state, opened_us, last_seen_us
            )
            self._store_usb_can_incident(
                incident_id=incident_id,
                state=state,
                kind=kind,
                scope=scope,
                opened_us=opened_us,
                opened_at=opened_at,
                last_seen_us=last_seen_us,
                last_seen_at=last_seen_at,
                resolved_us=resolved_us,
                resolved_at=resolved_at,
                resolution=resolution,
                affected=affected,
                event_count=event_count,
                reappearance_count=reappearance_count,
                opened_event_id=opened_event_id,
                last_event_id=last_event_id,
                resolved_event_id=resolved_event_id,
                incident_source=incident_source,
                incident_producer=incident_producer,
                payload_json=payload_json,
                snapshot_id=snapshot_id,
                prefix=prefix,
            )
        return current_active_incident_ids

    def _ingest_usb_can_monitor(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        """Persist sub-snapshot kernel edges by stable identity.

        The broker acknowledges its bounded in-memory queue only after this
        surrounding snapshot transaction commits.  Replayed kernel events are
        therefore harmless: ``event_id`` is immutable and the first snapshot
        remains the authority for whether an advisory has already observed it.
        """

        monitor = snapshot.get("_usb_can_monitor")
        if monitor is None:
            return
        producer_instance, boot_id, events, incidents = (
            self._ingest_usb_can_monitor_batch(
                snapshot, monitor, captured_us, snapshot_id
            )
        )
        self._ingest_usb_can_events(events, boot_id, snapshot_id)
        current_active_incident_ids = self._ingest_usb_can_incidents(
            incidents, producer_instance, snapshot_id
        )
        self._resolve_previous_usb_can_incidents(
            snapshot,
            captured_us=captured_us,
            snapshot_id=snapshot_id,
            producer_instance=producer_instance,
            boot_id=boot_id,
            current_incident_ids=current_active_incident_ids,
        )

    def _ingest_data_quality(
        self,
        snapshot: Mapping[str, object],
        captured_us: int,
        snapshot_id: int,
    ) -> None:
        """Upsert the broker's bounded recent quality incidents by stable id."""

        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        quality_status = status.get("data_quality")
        if quality_status is None:
            return
        if not isinstance(quality_status, Mapping):
            raise SnapshotValidationError("status.data_quality must be an object")
        producer_instance = _required_text(
            quality_status.get("producer_instance"),
            "status.data_quality.producer_instance",
        )
        if len(producer_instance) > 300:
            raise SnapshotValidationError(
                "status.data_quality.producer_instance is oversized"
            )
        recent = quality_status.get("recent", [])
        if not isinstance(recent, list) or len(recent) > 32:
            raise SnapshotValidationError(
                "status.data_quality.recent must contain at most 32 events"
            )
        seen_ids: set[str] = set()
        for index, item in enumerate(recent):
            prefix = f"status.data_quality.recent[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            incident_id = _required_text(
                item.get("incident_id"), f"{prefix}.incident_id"
            )
            event_producer = _required_text(
                item.get("producer_instance"), f"{prefix}.producer_instance"
            )
            if event_producer != producer_instance:
                raise SnapshotValidationError(
                    f"{prefix}.producer_instance does not match its snapshot"
                )
            metric = _required_text(item.get("metric"), f"{prefix}.metric")
            source = _required_text(item.get("source"), f"{prefix}.source")
            bus = _required_text(item.get("bus"), f"{prefix}.bus")
            quality = _required_text(item.get("quality"), f"{prefix}.quality")
            reason = _required_text(item.get("reason"), f"{prefix}.reason")
            detail = _required_text(item.get("detail"), f"{prefix}.detail")
            if incident_id in seen_ids:
                raise SnapshotValidationError(
                    f"status.data_quality repeats incident {incident_id!r}"
                )
            seen_ids.add(incident_id)
            if len(incident_id) > 300 or any(
                len(value) > 4000
                for value in (metric, source, bus, quality, reason, detail)
            ):
                raise SnapshotValidationError(f"{prefix} contains oversized text")
            if reason != "implausible_transition":
                raise SnapshotValidationError(
                    f"{prefix}.reason is not an admitted quality event"
                )
            event_status = item.get("status")
            if event_status not in ("active", "resolved"):
                raise SnapshotValidationError(
                    f"{prefix}.status must be active or resolved"
                )
            interface_mode = item.get("interface_mode")
            if interface_mode not in ("listen_only", "armed_diagnostic"):
                raise SnapshotValidationError(
                    f"{prefix}.interface_mode is invalid"
                )
            rejection_count = item.get("rejection_count")
            if (
                not isinstance(rejection_count, int)
                or isinstance(rejection_count, bool)
                or not 1 <= rejection_count <= 1_000_000_000
            ):
                raise SnapshotValidationError(
                    f"{prefix}.rejection_count must be a positive integer"
                )
            if item.get("notification_eligible") is not False:
                raise SnapshotValidationError(
                    f"{prefix} must be explicitly ineligible for notifications"
                )
            first_dt = _utc_datetime(
                item.get("first_seen_at"), f"{prefix}.first_seen_at"
            )
            last_dt = _utc_datetime(
                item.get("last_seen_at"), f"{prefix}.last_seen_at"
            )
            first_us = _to_us(first_dt)
            last_us = _to_us(last_dt)
            if last_us < first_us:
                raise SnapshotValidationError(
                    f"{prefix}.last_seen_at predates first_seen_at"
                )
            resolved_at = item.get("resolved_at")
            resolved_dt = (
                None
                if resolved_at is None
                else _utc_datetime(resolved_at, f"{prefix}.resolved_at")
            )
            resolved_us = None if resolved_dt is None else _to_us(resolved_dt)
            if event_status == "active" and resolved_dt is not None:
                raise SnapshotValidationError(
                    f"{prefix} active incident cannot have resolved_at"
                )
            if event_status == "resolved" and (
                resolved_dt is None or resolved_us < last_us
            ):
                raise SnapshotValidationError(
                    f"{prefix} resolved incident requires a valid resolved_at"
                )
            resolution_reason = item.get("resolution_reason")
            if resolution_reason is not None and (
                not isinstance(resolution_reason, str)
                or not resolution_reason
                or len(resolution_reason) > 500
            ):
                raise SnapshotValidationError(
                    f"{prefix}.resolution_reason must be bounded text or null"
                )
            if event_status == "active" and resolution_reason is not None:
                raise SnapshotValidationError(
                    f"{prefix} active incident cannot have a resolution reason"
                )
            evidence = item.get("evidence")
            if not isinstance(evidence, Mapping):
                raise SnapshotValidationError(f"{prefix}.evidence must be an object")
            try:
                evidence_json = json.dumps(
                    dict(evidence),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotValidationError(
                    f"{prefix}.evidence must be finite JSON data"
                ) from exc
            if len(evidence_json.encode("utf-8")) > 16_384:
                raise SnapshotValidationError(f"{prefix}.evidence is oversized")

            existing = self._conn.execute(
                "SELECT * FROM data_quality_events WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if existing is not None and any(
                existing[field] != expected
                for field, expected in (
                    ("producer_instance", producer_instance),
                    ("metric", metric),
                    ("source", source),
                    ("bus", bus),
                    ("quality", quality),
                    ("reason", reason),
                    ("first_seen_us", first_us),
                )
            ):
                raise SnapshotValidationError(
                    f"{prefix} changes immutable incident identity"
                )
            self._conn.execute(
                """
                INSERT INTO data_quality_events(
                    incident_id,producer_instance,metric,source,bus,quality,reason,status,
                    first_seen_us,first_seen_at,last_seen_us,last_seen_at,
                    resolved_us,resolved_at,resolution_reason,rejection_count,detail,
                    interface_mode,evidence_json,first_snapshot_id,
                    last_snapshot_id,notification_eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status=excluded.status,
                    last_seen_us=max(data_quality_events.last_seen_us,excluded.last_seen_us),
                    last_seen_at=CASE
                        WHEN excluded.last_seen_us >= data_quality_events.last_seen_us
                        THEN excluded.last_seen_at ELSE data_quality_events.last_seen_at END,
                    resolved_us=excluded.resolved_us,
                    resolved_at=excluded.resolved_at,
                    resolution_reason=excluded.resolution_reason,
                    rejection_count=max(
                        data_quality_events.rejection_count,
                        excluded.rejection_count
                    ),
                    detail=excluded.detail,
                    interface_mode=excluded.interface_mode,
                    evidence_json=excluded.evidence_json,
                    last_snapshot_id=excluded.last_snapshot_id
                """,
                (
                    incident_id,
                    producer_instance,
                    metric,
                    source,
                    bus,
                    quality,
                    reason,
                    event_status,
                    first_us,
                    _iso(first_dt),
                    last_us,
                    _iso(last_dt),
                    resolved_us,
                    None if resolved_dt is None else _iso(resolved_dt),
                    resolution_reason,
                    rejection_count,
                    detail,
                    interface_mode,
                    evidence_json,
                    snapshot_id,
                    snapshot_id,
                ),
            )

        authoritative_good = quality_status.get("authoritative_good", [])
        if (
            not isinstance(authoritative_good, list)
            or len(authoritative_good) > 32
        ):
            raise SnapshotValidationError(
                "status.data_quality.authoritative_good must contain at most 32 rows"
            )
        active_current = {
            (item.get("metric"), item.get("source"))
            for item in recent
            if isinstance(item, Mapping) and item.get("status") == "active"
        }
        seen_good: set[tuple[str, str]] = set()
        for index, item in enumerate(authoritative_good):
            prefix = f"status.data_quality.authoritative_good[{index}]"
            if not isinstance(item, Mapping):
                raise SnapshotValidationError(f"{prefix} must be an object")
            metric = _required_text(item.get("metric"), f"{prefix}.metric")
            source = _required_text(item.get("source"), f"{prefix}.source")
            key = (metric, source)
            if key in seen_good:
                raise SnapshotValidationError(
                    f"status.data_quality.authoritative_good repeats {metric!r}"
                )
            seen_good.add(key)
            if key in active_current:
                raise SnapshotValidationError(
                    f"{prefix} conflicts with an active current-process incident"
                )
            observed_dt = _utc_datetime(
                item.get("observed_at"), f"{prefix}.observed_at"
            )
            observed_us = _to_us(observed_dt)
            prior_rows = self._conn.execute(
                """
                SELECT incident_id,last_seen_us FROM data_quality_events
                WHERE status='active' AND metric=? AND source=?
                  AND producer_instance!=?
                """,
                (metric, source, producer_instance),
            ).fetchall()
            for row in prior_rows:
                resolved_us = max(
                    int(row["last_seen_us"]),
                    observed_us,
                    captured_us,
                )
                self._conn.execute(
                    """
                    UPDATE data_quality_events
                    SET status='resolved',resolved_us=?,resolved_at=?,
                        resolution_reason=?,last_snapshot_id=?
                    WHERE incident_id=? AND status='active'
                    """,
                    (
                        resolved_us,
                        _iso_from_us(resolved_us),
                        "producer_restarted_then_authoritative_good_sample",
                        snapshot_id,
                        row["incident_id"],
                    ),
                )
