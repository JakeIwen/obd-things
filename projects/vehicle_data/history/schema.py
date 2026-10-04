"""Verbatim historian schema creation and version checks."""

from __future__ import annotations


from .models import (
    ADVISORY_SCHEMA_VERSION,
    DATA_QUALITY_SCHEMA_VERSION,
    HistorianError,
    SCHEMA_VERSION,
)


class SchemaMixin:
    """Verbatim historian schema creation and version checks."""

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS historian_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_active_us INTEGER NOT NULL,
            last_active_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            start_basis TEXT NOT NULL,
            end_reason TEXT,
            snapshot_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_trip
            ON trips((1)) WHERE ended_us IS NULL;

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            ingest_key TEXT NOT NULL UNIQUE,
            captured_us INTEGER NOT NULL UNIQUE,
            captured_at TEXT NOT NULL,
            source_instance TEXT,
            source_sequence INTEGER,
            vehicle_state TEXT,
            vehicle_running INTEGER,
            vehicle_confidence TEXT,
            vehicle_basis TEXT,
            vehicle_observed_at TEXT,
            vehicle_age_ms INTEGER,
            regime TEXT NOT NULL,
            trip_id INTEGER REFERENCES trips(id)
        );
        CREATE INDEX IF NOT EXISTS snapshots_trip_time
            ON snapshots(trip_id, captured_us);

        CREATE TABLE IF NOT EXISTS catalog_sources (
            fingerprint TEXT PRIMARY KEY,
            metric TEXT NOT NULL,
            unit TEXT NOT NULL,
            value_type TEXT NOT NULL,
            stale_after_ms INTEGER NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            first_seen_us INTEGER NOT NULL,
            last_seen_us INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS catalog_metric_source
            ON catalog_sources(metric, source);

        CREATE TABLE IF NOT EXISTS metric_samples (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            trip_id INTEGER REFERENCES trips(id),
            captured_us INTEGER NOT NULL,
            metric TEXT NOT NULL,
            value_kind TEXT NOT NULL CHECK(value_kind IN ('number','boolean','string')),
            value_num REAL,
            value_text TEXT,
            value_bool INTEGER CHECK(value_bool IN (0,1) OR value_bool IS NULL),
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            acquisition TEXT,
            interface_mode TEXT,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            observed_us INTEGER,
            observed_at TEXT,
            source_age_ms INTEGER,
            reported_stale INTEGER CHECK(reported_stale IN (0,1) OR reported_stale IS NULL),
            freshness TEXT NOT NULL CHECK(freshness IN ('fresh','stale','undated')),
            regime TEXT NOT NULL,
            UNIQUE(snapshot_id, metric)
        );
        CREATE INDEX IF NOT EXISTS metric_samples_lookup
            ON metric_samples(metric, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_baseline
            ON metric_samples(metric, regime, freshness, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_trip
            ON metric_samples(trip_id, metric, captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_time
            ON metric_samples(captured_us);
        CREATE INDEX IF NOT EXISTS metric_samples_observation
            ON metric_samples(metric, source, observed_us, captured_us);
        -- Advisory evaluation asks for the newest fresh row for every rule.
        -- Cached observations are still ingested after they become stale so
        -- coverage gaps remain explicit; without this partial index, each
        -- lookup walks every newer stale copy before reaching the last fresh
        -- row.  That cost grows with parked time and used to monopolize one Pi
        -- core during each five-second historian checkpoint.
        CREATE INDEX IF NOT EXISTS metric_samples_fresh_latest
            ON metric_samples(metric, captured_us DESC)
            WHERE freshness='fresh';
        -- Rollups count each source observation once even when several broker
        -- snapshots retain it.  Cover the complete observation identity plus
        -- capture ordering so the correlated earlier-row check is an exact
        -- bounded lookup rather than a scan through every cached duplicate.
        CREATE INDEX IF NOT EXISTS metric_samples_rollup_dedup
            ON metric_samples(
                metric, source, unit, quality, provenance, observed_us,
                captured_us
            )
            WHERE freshness='fresh' AND value_kind='number'
              AND observed_us IS NOT NULL;

        CREATE TABLE IF NOT EXISTS metric_gaps (
            id INTEGER PRIMARY KEY,
            metric TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('missing','stale','undated')),
            reason TEXT NOT NULL,
            detail TEXT NOT NULL,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            first_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            last_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            observation_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_metric_gap
            ON metric_gaps(metric) WHERE ended_us IS NULL;
        CREATE INDEX IF NOT EXISTS metric_gaps_time
            ON metric_gaps(started_us, ended_us);

        CREATE TABLE IF NOT EXISTS interface_samples (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            captured_us INTEGER NOT NULL,
            channel TEXT,
            usb_serial TEXT,
            bus TEXT,
            adapter_present INTEGER,
            up INTEGER,
            bitrate INTEGER,
            listen_only INTEGER,
            controller_state TEXT,
            topology_usable INTEGER,
            health TEXT NOT NULL CHECK(health IN ('healthy','unhealthy','unknown')),
            reason TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, role)
        );
        CREATE INDEX IF NOT EXISTS interface_samples_lookup
            ON interface_samples(role, captured_us);
        CREATE INDEX IF NOT EXISTS interface_samples_time
            ON interface_samples(captured_us);

        CREATE TABLE IF NOT EXISTS interface_gaps (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('unhealthy','unknown','missing')),
            reason TEXT NOT NULL,
            started_us INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_us INTEGER,
            ended_at TEXT,
            first_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            last_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            observation_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_interface_gap
            ON interface_gaps(role) WHERE ended_us IS NULL;
        CREATE INDEX IF NOT EXISTS interface_gaps_time
            ON interface_gaps(started_us, ended_us);

        CREATE TABLE IF NOT EXISTS interface_probe_samples (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            probe_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_health_samples (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            captured_us INTEGER NOT NULL,
            topology_generation TEXT,
            issues_json TEXT NOT NULL,
            active_inhibits_json TEXT NOT NULL,
            restoration_failed INTEGER CHECK(restoration_failed IN (0,1) OR restoration_failed IS NULL)
        );
        CREATE INDEX IF NOT EXISTS system_health_samples_time
            ON system_health_samples(captured_us);

        CREATE TABLE IF NOT EXISTS interface_role_details (
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            resolution TEXT,
            role_reason TEXT,
            detail TEXT,
            usb_dev_id INTEGER,
            topology_generation TEXT,
            PRIMARY KEY(snapshot_id, role)
        );

        CREATE TABLE IF NOT EXISTS usb_can_monitor_samples (
            snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
            captured_us INTEGER NOT NULL,
            source TEXT NOT NULL,
            producer_instance TEXT NOT NULL,
            dropped_event_count INTEGER NOT NULL CHECK(dropped_event_count >= 0),
            pending_event_count INTEGER NOT NULL CHECK(pending_event_count >= 0)
        );
        CREATE INDEX IF NOT EXISTS usb_can_monitor_samples_time
            ON usb_can_monitor_samples(captured_us);

        CREATE TABLE IF NOT EXISTS usb_can_events (
            event_id TEXT PRIMARY KEY,
            occurred_us INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            boot_id TEXT NOT NULL,
            kernel_seqnum TEXT,
            kind TEXT NOT NULL,
            action TEXT NOT NULL,
            scope TEXT NOT NULL,
            devpath TEXT NOT NULL,
            usb_vid TEXT,
            usb_pid TEXT,
            usb_serial TEXT,
            affected_serials_json TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_events_time
            ON usb_can_events(occurred_us DESC);
        CREATE INDEX IF NOT EXISTS usb_can_events_kind_time
            ON usb_can_events(kind, occurred_us DESC);

        CREATE TABLE IF NOT EXISTS usb_can_advisory_consumption (
            event_id TEXT PRIMARY KEY REFERENCES usb_can_events(event_id)
                ON DELETE CASCADE,
            consumed_us INTEGER NOT NULL,
            consumed_at TEXT NOT NULL,
            snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_advisory_consumption_snapshot
            ON usb_can_advisory_consumption(snapshot_id);

        CREATE TABLE IF NOT EXISTS usb_can_incidents (
            incident_id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('active','resolved')),
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            opened_us INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution TEXT,
            affected_serials_json TEXT NOT NULL,
            event_count INTEGER NOT NULL CHECK(event_count > 0),
            reappearance_count INTEGER NOT NULL CHECK(reappearance_count >= 0),
            opened_event_id TEXT NOT NULL,
            last_event_id TEXT NOT NULL,
            resolved_event_id TEXT,
            source TEXT NOT NULL,
            producer_instance TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS usb_can_incidents_state_time
            ON usb_can_incidents(state, last_seen_us DESC);

        CREATE TABLE IF NOT EXISTS data_quality_events (
            incident_id TEXT PRIMARY KEY,
            producer_instance TEXT NOT NULL,
            metric TEXT NOT NULL,
            source TEXT NOT NULL,
            bus TEXT NOT NULL,
            quality TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','resolved')),
            first_seen_us INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_us INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution_reason TEXT,
            rejection_count INTEGER NOT NULL CHECK(rejection_count > 0),
            detail TEXT NOT NULL,
            interface_mode TEXT NOT NULL CHECK(
                interface_mode IN ('listen_only','armed_diagnostic')
            ),
            evidence_json TEXT NOT NULL,
            first_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            last_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
            notification_eligible INTEGER NOT NULL DEFAULT 0 CHECK(
                notification_eligible = 0
            )
        );
        CREATE INDEX IF NOT EXISTS data_quality_events_recent
            ON data_quality_events(last_seen_us DESC);
        CREATE INDEX IF NOT EXISTS data_quality_events_active
            ON data_quality_events(status,last_seen_us DESC);

        CREATE TABLE IF NOT EXISTS advisory_episodes (
            id INTEGER PRIMARY KEY,
            rule_key TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            advisory INTEGER NOT NULL CHECK(advisory IN (0,1)),
            status TEXT NOT NULL CHECK(status IN ('open','resolved')),
            current_state TEXT NOT NULL CHECK(
                current_state IN ('watch','warning','normal','suppressed')
            ),
            evidence_state TEXT NOT NULL,
            opened_us INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            last_evaluated_us INTEGER NOT NULL,
            last_evaluated_at TEXT NOT NULL,
            last_observed_us INTEGER NOT NULL,
            last_observed_at TEXT NOT NULL,
            resolved_us INTEGER,
            resolved_at TEXT,
            resolution_reason TEXT,
            observation_count INTEGER NOT NULL DEFAULT 1,
            update_count INTEGER NOT NULL DEFAULT 0,
            transition_count INTEGER NOT NULL DEFAULT 0,
            acknowledged_us INTEGER,
            acknowledged_at TEXT,
            acknowledgment_note TEXT,
            first_assessment_json TEXT NOT NULL,
            latest_assessment_json TEXT NOT NULL,
            latest_context_fingerprint TEXT NOT NULL,
            last_notification_enqueued_us INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_advisory_episode
            ON advisory_episodes(rule_key) WHERE status='open';
        CREATE INDEX IF NOT EXISTS advisory_episodes_recent
            ON advisory_episodes(opened_us DESC);

        CREATE TABLE IF NOT EXISTS advisory_episode_events (
            id INTEGER PRIMARY KEY,
            episode_id INTEGER NOT NULL REFERENCES advisory_episodes(id) ON DELETE CASCADE,
            event_us INTEGER NOT NULL,
            event_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            previous_state TEXT,
            new_state TEXT,
            context_fingerprint TEXT NOT NULL,
            assessment_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS advisory_episode_events_lookup
            ON advisory_episode_events(episode_id, event_us);

        CREATE TABLE IF NOT EXISTS advisory_notification_outbox (
            id INTEGER PRIMARY KEY,
            episode_id INTEGER NOT NULL REFERENCES advisory_episodes(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES advisory_episode_events(id) ON DELETE CASCADE,
            rule_key TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            created_us INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            eligible_after_us INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed','cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_us INTEGER,
            last_attempt_at TEXT,
            delivered_us INTEGER,
            delivered_at TEXT,
            last_error TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS advisory_notification_outbox_pending
            ON advisory_notification_outbox(status, eligible_after_us, created_us);

        CREATE TABLE IF NOT EXISTS metric_rollups (
            bucket_us INTEGER NOT NULL,
            bucket_seconds INTEGER NOT NULL,
            trip_key INTEGER NOT NULL,
            metric TEXT NOT NULL,
            regime TEXT NOT NULL,
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            quality TEXT NOT NULL,
            provenance TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            minimum REAL NOT NULL,
            maximum REAL NOT NULL,
            mean REAL NOT NULL,
            median REAL NOT NULL,
            mad REAL NOT NULL,
            first_us INTEGER NOT NULL,
            last_us INTEGER NOT NULL,
            PRIMARY KEY(
                bucket_us, bucket_seconds, trip_key, metric, regime,
                unit, source, quality, provenance
            )
        );
        CREATE INDEX IF NOT EXISTS metric_rollups_baseline
            ON metric_rollups(metric, regime, bucket_us);
        """
        with self._lock, self._conn:
            self._conn.executescript(schema)
            existing = self._conn.execute(
                "SELECT value FROM historian_meta WHERE key='schema_version'"
            ).fetchone()
            if existing is not None and int(existing[0]) != SCHEMA_VERSION:
                raise HistorianError(
                    f"database schema {existing[0]} is not supported by schema {SCHEMA_VERSION}"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) "
                "VALUES('advisory_schema_version',?)",
                (str(ADVISORY_SCHEMA_VERSION),),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO historian_meta(key,value) "
                "VALUES('data_quality_schema_version',?)",
                (str(DATA_QUALITY_SCHEMA_VERSION),),
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
