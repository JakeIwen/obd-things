"""Advisory episode persistence and notification outbox policy."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .models import (
    ADVISORY_ACTIVE_STATES,
    ADVISORY_INCONCLUSIVE_STATES,
    ADVISORY_RESOLVING_STATES,
    ADVISORY_SCHEMA_VERSION,
    AdvisoryPersistenceResult,
    MAX_NOTIFICATION_RATE_LIMIT_SECONDS,
    MICROSECONDS,
    NOTIFICATION_RANK_SQL,
    SYSTEM_NOTE_CATEGORIES,
    TRIP_NONCRITICAL_PUSH_CAP,
)
from .validation import (
    _iso,
    _iso_from_us,
    _to_us,
    _utc_datetime,
)


class AdvisoryMixin:
    """Advisory episode persistence and notification outbox policy."""

    @staticmethod
    def _advisory_json(value: Mapping[str, object]) -> str:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("advisory assessment must be JSON serializable") from exc

    @staticmethod
    def _advisory_context(assessment: Mapping[str, object]) -> dict[str, object]:
        """Select material context without volatile values or timestamps."""

        current = assessment.get("current")
        current = current if isinstance(current, Mapping) else {}
        baseline = assessment.get("baseline")
        baseline = baseline if isinstance(baseline, Mapping) else {}
        deviation = assessment.get("deviation")
        deviation = deviation if isinstance(deviation, Mapping) else {}
        plausibility = assessment.get("plausibility")
        plausibility = plausibility if isinstance(plausibility, Mapping) else {}
        return {
            "rule": assessment.get("rule"),
            "state": assessment.get("state"),
            "reason": assessment.get("reason"),
            "category": assessment.get("category"),
            "severity": assessment.get("severity"),
            "regime": assessment.get("regime"),
            "baseline_regime": assessment.get("baseline_regime"),
            "direction": assessment.get("direction"),
            "notification_eligible": assessment.get("notification_eligible"),
            "current": {
                key: current.get(key)
                for key in (
                    "metric",
                    "unit",
                    "source",
                    "bus",
                    "quality",
                    "provenance",
                    "role",
                    "resolution",
                    "role_reason",
                    "health",
                    "reason",
                    "controller_state",
                    "usb_serial",
                    "usb_dev_id",
                    "topology_generation",
                )
                if key in current
            },
            "baseline": {
                key: baseline.get(key)
                for key in (
                    "unit",
                    "quality",
                    "source",
                    "provenance",
                    "median",
                    "mad",
                    "robust_sigma",
                )
                if key in baseline
            },
            "threshold": deviation.get("threshold"),
            "plausibility_limit": {
                "maximum_delta_c": plausibility.get("maximum_delta_c"),
                "maximum_delta_window_seconds": plausibility.get(
                    "maximum_delta_window_seconds"
                ),
            },
            "absolute_threshold": assessment.get("absolute_threshold"),
            "reference_provenance": assessment.get("reference_provenance"),
        }

    @classmethod
    def _advisory_context_fingerprint(
        cls, assessment: Mapping[str, object]
    ) -> str:
        encoded = json.dumps(
            cls._advisory_context(assessment),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _insert_advisory_event_locked(
        self,
        *,
        episode_id: int,
        event_us: int,
        event_type: str,
        previous_state: str | None,
        new_state: str | None,
        context_fingerprint: str,
        assessment_json: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO advisory_episode_events(
                episode_id,event_us,event_at,event_type,previous_state,
                new_state,context_fingerprint,assessment_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                episode_id,
                event_us,
                _iso_from_us(event_us),
                event_type,
                previous_state,
                new_state,
                context_fingerprint,
                assessment_json,
            ),
        )
        return int(cursor.lastrowid)

    def _notification_due_locked(
        self,
        *,
        episode_id: int,
        rule_key: str,
        event_us: int,
        rate_limit_seconds: float,
        group_key: str | None = None,
        rank: int | None = None,
        critical: bool = False,
    ) -> bool:
        if critical:
            # A critical push is owned only by an undelivered critical row of
            # the same episode.  A pending lower-severity row (superseded by
            # the caller) or a terminally failed row must never silence it.
            pending = self._conn.execute(
                """
                SELECT 1 FROM advisory_notification_outbox
                WHERE episode_id=? AND status='pending'
                  AND json_extract(payload_json,'$.severity')='critical' LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
        else:
            pending = self._conn.execute(
                """
                SELECT 1 FROM advisory_notification_outbox
                WHERE episode_id=? AND status IN ('pending','failed') LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
        if pending is not None:
            # One durable delivery owner per episode.  A periodic reminder
            # must not overtake a pending retry or resurrect a row that already
            # reached its terminal failed state.  Resolution/reopening creates
            # a new episode and therefore a new bounded owner.
            return False
        if group_key is None:
            last = self._conn.execute(
                """
                SELECT max(coalesce(delivered_us,last_attempt_us,created_us))
                FROM advisory_notification_outbox
                WHERE rule_key=? AND status!='cancelled'
                """,
                (rule_key,),
            ).fetchone()[0]
        else:
            # Tiered cooldown per group across rules and episodes.  The outbox
            # is tiny and the lower bound (the longest cooldown) keeps the
            # json_extract scan bounded; recovery rows are not warning pushes.
            # Only same-or-higher urgency pushes count (``rank``).
            lower_us = event_us - MAX_NOTIFICATION_RATE_LIMIT_SECONDS * MICROSECONDS
            last = self._conn.execute(
                f"""
                SELECT max(coalesce(delivered_us,last_attempt_us,created_us))
                FROM advisory_notification_outbox
                WHERE status!='cancelled' AND created_us>=?
                  AND json_extract(payload_json,'$.group')=?
                  AND coalesce(json_extract(payload_json,'$.notification_kind'),'')
                      !='recovery'
                  AND ({NOTIFICATION_RANK_SQL})>=?
                """,
                (lower_us, group_key, 0 if rank is None else int(rank)),
            ).fetchone()[0]
            if last is not None:
                # A later "back to normal" push in the group ends the cooldown:
                # a recurrence after the owner was told it recovered must push.
                recovered = self._conn.execute(
                    """
                    SELECT max(created_us) FROM advisory_notification_outbox
                    WHERE status!='cancelled' AND created_us>=?
                      AND json_extract(payload_json,'$.group')=?
                      AND json_extract(payload_json,'$.notification_kind')='recovery'
                    """,
                    (lower_us, group_key),
                ).fetchone()[0]
                if recovered is not None and int(recovered) > int(last):
                    return True
        return last is None or event_us - int(last) >= int(
            round(rate_limit_seconds * MICROSECONDS)
        )

    @staticmethod
    def _notification_rate_limit(
        assessment: Mapping[str, object],
    ) -> float:
        rate_limit = assessment.get("notification_rate_limit_seconds", 30 * 60)
        if (
            isinstance(rate_limit, bool)
            or not isinstance(rate_limit, (int, float))
            or not math.isfinite(float(rate_limit))
            or not 60 <= float(rate_limit) <= MAX_NOTIFICATION_RATE_LIMIT_SECONDS
        ):
            raise ValueError(
                "notification_rate_limit_seconds must be between 60 and "
                f"{MAX_NOTIFICATION_RATE_LIMIT_SECONDS}"
            )
        return float(rate_limit)

    @staticmethod
    def _notification_policy(
        assessment: Mapping[str, object],
    ) -> dict[str, object]:
        """Delivery policy for one assessment; ``legacy`` without a tier.

        Tier 0 critical: 5-minute repeats, never deferred.  Tier 0 warning:
        once per episode, 6 h group cooldown, never deferred.  Tier 1: once,
        6 h, quiet-hours deferral.  Tier 2: once, 7 d, deferral.  Tier 3: once,
        6 h, deferral except critical; unreachable, because
        ``_record_tiered_notification_locked`` never enqueues tier 3 or a
        System-note category (owner decision 2026-09-24).
        """

        tier = assessment.get("tier")
        if (
            not isinstance(tier, int)
            or isinstance(tier, bool)
            or not 0 <= tier <= 3
        ):
            return {"legacy": True}
        severity = assessment.get("severity")
        if tier == 0 and severity == "critical":
            cooldown, repeat, defer, once = 300, True, False, False
        elif tier == 0:
            cooldown, repeat, defer, once = 21600, False, False, True
        elif tier == 1:
            cooldown, repeat, defer, once = 21600, False, True, True
        elif tier == 2:
            cooldown, repeat, defer, once = 604800, False, True, True
        else:
            cooldown, repeat, defer, once = 21600, False, severity != "critical", True
        group = assessment.get("group")
        group = group if isinstance(group, str) and group else None
        return {
            "legacy": False,
            "tier": tier,
            "cooldown_seconds": float(cooldown),
            "repeat": repeat,
            "defer": defer,
            "once_per_episode": once,
            "group": group,
            "dedupe_key": group or assessment.get("rule"),
        }

    @staticmethod
    def _notification_rank(tier: object, severity: object) -> int:
        """Urgency rank of a tiered push; mirrors ``NOTIFICATION_RANK_SQL``."""

        if severity == "critical":
            return 4
        if tier == 0:
            return 3
        if tier == 2:
            return 1
        return 2

    def _trip_push_count_locked(self, event_us: int) -> int:
        """Non-critical tiered warning rows created since the open trip began."""

        trip = self._conn.execute(
            "SELECT started_us FROM trips WHERE ended_us IS NULL LIMIT 1"
        ).fetchone()
        if trip is None:
            return 0
        return int(
            self._conn.execute(
                """
                SELECT count(*) FROM advisory_notification_outbox
                WHERE created_us>=? AND created_us<=? AND status!='cancelled'
                  AND json_extract(payload_json,'$.tier') IS NOT NULL
                  AND coalesce(json_extract(payload_json,'$.severity'),'')!='critical'
                  AND coalesce(json_extract(payload_json,'$.notification_kind'),'')
                      !='recovery'
                """,
                (int(trip["started_us"]), event_us),
            ).fetchone()[0]
        )

    def _enqueue_advisory_notification_locked(
        self,
        *,
        episode: sqlite3.Row,
        event_id: int,
        event_us: int,
        context_fingerprint: str,
        assessment: Mapping[str, object],
        assessment_json: str,
        rate_limit_seconds: float | None = None,
        eligible_after_us: int | None = None,
        notification_kind: str = "warning",
        payload_extra: Mapping[str, object] | None = None,
        dedupe_source: str | None = None,
        skip_due_check: bool = False,
    ) -> bool:
        """Insert one pending outbox row.  Keyword defaults are the legacy path.

        Tiered callers pass the policy cooldown, the quiet-hours eligibility,
        the C3 payload additions and ``skip_due_check`` (they already ran the
        group-keyed due check and the per-trip cap).
        """

        rule_key = str(episode["rule_key"])
        rate_limit = (
            self._notification_rate_limit(assessment)
            if rate_limit_seconds is None
            else float(rate_limit_seconds)
        )
        if not skip_due_check and not self._notification_due_locked(
            episode_id=int(episode["id"]),
            rule_key=rule_key,
            event_us=event_us,
            rate_limit_seconds=float(rate_limit),
        ):
            return False
        payload = {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "advisory": True,
            "episode_id": int(episode["id"]),
            "rule": rule_key,
            "category": episode["category"],
            "title": episode["title"],
            "state": assessment["state"],
            "reason": assessment.get("reason"),
            "opened_at": episode["opened_at"],
            "evaluated_at": _iso_from_us(event_us),
            "assessment": json.loads(assessment_json),
        }
        if payload_extra is not None or notification_kind != "warning":
            payload.update(dict(payload_extra or {}))
            payload["notification_kind"] = notification_kind
        if dedupe_source is None:
            bucket_us = max(
                MICROSECONDS,
                int(round(float(rate_limit) * MICROSECONDS)),
            )
            dedupe_source = (
                f"{rule_key}:{episode['id']}:{event_us // bucket_us}:"
                f"{context_fingerprint}"
            )
        dedupe_key = hashlib.sha256(dedupe_source.encode()).hexdigest()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO advisory_notification_outbox(
                episode_id,event_id,rule_key,dedupe_key,created_us,created_at,
                eligible_after_us,status,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                episode["id"],
                event_id,
                rule_key,
                dedupe_key,
                event_us,
                _iso_from_us(event_us),
                event_us if eligible_after_us is None else int(eligible_after_us),
                "pending",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        if cursor.rowcount:
            self._conn.execute(
                "UPDATE advisory_episodes "
                "SET last_notification_enqueued_us=? WHERE id=?",
                (event_us, episode["id"]),
            )
            return True
        return False

    @staticmethod
    def _tiered_payload_extra(
        assessment: Mapping[str, object],
        *,
        tier: int,
        group: str | None,
        event_us: int,
        eligible_after_us: int,
    ) -> dict[str, object]:
        """Contract C3 additions carried by every tiered outbox row."""

        current = assessment.get("current")
        current = current if isinstance(current, Mapping) else {}
        trip_id = current.get("trip_id")
        return {
            "tier": tier,
            "group": group,
            "severity": assessment.get("severity"),
            "action": assessment.get("action"),
            "confidence": assessment.get("confidence"),
            "deferred_until": (
                _iso_from_us(eligible_after_us)
                if eligible_after_us > event_us
                else None
            ),
            "trip_id": (
                trip_id
                if isinstance(trip_id, int) and not isinstance(trip_id, bool)
                else None
            ),
        }

    def _record_tiered_notification_locked(
        self,
        *,
        episode: sqlite3.Row,
        event_id: int | None,
        event_us: int,
        fingerprint: str,
        assessment: Mapping[str, object],
        assessment_json: str,
        policy: Mapping[str, object],
        counters: dict[str, int],
    ) -> None:
        """Group cooldown, once-per-episode/repeat, trip cap, quiet hours."""

        if not (
            assessment.get("state") == "warning"
            and assessment.get("notification_eligible") is True
            and episode["acknowledged_us"] is None
        ):
            return
        if policy["tier"] == 3 or assessment.get("category") in SYSTEM_NOTE_CATEGORIES:
            # Owner decision 2026-09-24: adapter, bus and data-quality items
            # are dashboard System notes only and never notify, even if a
            # producer marks one eligible by mistake.
            return
        episode_id = int(episode["id"])
        rule_key = str(episode["rule_key"])
        cooldown = float(policy["cooldown_seconds"])
        critical = assessment.get("severity") == "critical"
        prior_row = self._conn.execute(
            """
            SELECT 1 FROM advisory_notification_outbox
            WHERE episode_id=? AND status!='cancelled' LIMIT 1
            """,
            (episode_id,),
        ).fetchone()
        if policy["once_per_episode"] and prior_row is not None:
            return
        group = policy.get("group")
        if not self._notification_due_locked(
            episode_id=episode_id,
            rule_key=rule_key,
            event_us=event_us,
            rate_limit_seconds=cooldown,
            group_key=group if isinstance(group, str) else None,
            rank=self._notification_rank(policy["tier"], assessment.get("severity")),
            critical=critical,
        ):
            return
        # Tier 0 is never capped: an absolute-limit warning (oil below its
        # band, coolant or transmission hot, charging failure, tire low) must
        # not be dropped because lower tiers already pushed this trip.
        if (
            not critical
            and policy["tier"] != 0
            and self._trip_push_count_locked(event_us) >= TRIP_NONCRITICAL_PUSH_CAP
        ):
            counters["notifications_capped"] += 1
            return
        eligible_after_us = event_us
        # A tier-1 warning raised while a trip is open is not deferred: the
        # owner is driving (trips 50-55 ran until 23:27 local), so holding it
        # to 07:30 would deliver it after the drive, or cancel it unseen if
        # the episode resolves overnight.  Parked tier-1 and tier-2 still wait.
        in_trip_tier1 = policy["tier"] == 1 and self._conn.execute(
            "SELECT 1 FROM trips WHERE ended_us IS NULL AND started_us<=? LIMIT 1",
            (event_us,),
        ).fetchone() is not None
        if policy["defer"] and not in_trip_tier1:
            from projects.vehicle_data.notifications import quiet_hours_deferral

            eligible_after_us = quiet_hours_deferral(event_us) or event_us
        prior_critical = None
        if critical:
            # An escalation supersedes the episode's undelivered lower-severity
            # row: the critical row goes out now at urgent priority instead.
            self._conn.execute(
                """
                UPDATE advisory_notification_outbox
                SET status='cancelled',last_error='superseded by a critical notification'
                WHERE episode_id=? AND status='pending'
                  AND coalesce(json_extract(payload_json,'$.severity'),'')!='critical'
                """,
                (episode_id,),
            )
            prior_critical = self._conn.execute(
                """
                SELECT 1 FROM advisory_notification_outbox
                WHERE episode_id=? AND status!='cancelled'
                  AND json_extract(payload_json,'$.severity')='critical' LIMIT 1
                """,
                (episode_id,),
            ).fetchone()
        kind = "repeat" if policy["repeat"] and prior_critical is not None else "warning"
        if event_id is None:
            event_id = self._insert_advisory_event_locked(
                episode_id=episode_id,
                event_us=event_us,
                event_type=(
                    "notification_repeat_due" if kind == "repeat" else "notification_due"
                ),
                previous_state=str(assessment["state"]),
                new_state=str(assessment["state"]),
                context_fingerprint=fingerprint,
                assessment_json=assessment_json,
            )
        if self._enqueue_advisory_notification_locked(
            episode=episode,
            event_id=event_id,
            event_us=event_us,
            context_fingerprint=fingerprint,
            assessment=assessment,
            assessment_json=assessment_json,
            rate_limit_seconds=cooldown,
            eligible_after_us=eligible_after_us,
            notification_kind=kind,
            payload_extra=self._tiered_payload_extra(
                assessment,
                tier=int(policy["tier"]),
                group=group if isinstance(group, str) else None,
                event_us=event_us,
                eligible_after_us=eligible_after_us,
            ),
            skip_due_check=True,
        ):
            counters["notifications_enqueued"] += 1
            if eligible_after_us > event_us:
                counters["notifications_deferred"] += 1

    def _enqueue_recovery_notification_locked(
        self,
        *,
        episode: sqlite3.Row,
        event_id: int,
        event_us: int,
        fingerprint: str,
        assessment: Mapping[str, object],
        assessment_json: str,
    ) -> bool:
        """One recovery row for a delivered tier-0 episode that returned normal.

        Not subject to cooldown or the trip cap.  The caller has already
        cancelled the episode's pending warning rows.
        """

        if assessment.get("state") != "normal" or episode["acknowledged_us"] is not None:
            return False
        stored: dict[str, object] = {}
        tier = assessment.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool):
            try:
                loaded = json.loads(episode["latest_assessment_json"])
            except (TypeError, json.JSONDecodeError):
                loaded = {}
            stored = loaded if isinstance(loaded, dict) else {}
            tier = stored.get("tier")
        if not isinstance(tier, int) or isinstance(tier, bool) or tier != 0:
            return False
        delivered = self._conn.execute(
            """
            SELECT 1 FROM advisory_notification_outbox
            WHERE episode_id=? AND status='delivered'
              AND coalesce(json_extract(payload_json,'$.notification_kind'),'')
                  !='recovery'
            LIMIT 1
            """,
            (int(episode["id"]),),
        ).fetchone()
        if delivered is None:
            return False
        group = assessment.get("group") or stored.get("group")
        return self._enqueue_advisory_notification_locked(
            episode=episode,
            event_id=event_id,
            event_us=event_us,
            context_fingerprint=fingerprint,
            assessment=assessment,
            assessment_json=assessment_json,
            rate_limit_seconds=300.0,
            eligible_after_us=event_us,
            notification_kind="recovery",
            payload_extra=self._tiered_payload_extra(
                assessment,
                tier=0,
                group=group if isinstance(group, str) and group else None,
                event_us=event_us,
                eligible_after_us=event_us,
            ),
            dedupe_source=f"recovery:{int(episode['id'])}",
            skip_due_check=True,
        )

    def record_advisory_assessments(
        self,
        assessments: Sequence[Mapping[str, object]],
        *,
        evaluated_at: datetime | str,
        authoritative_rule_keys: Sequence[str] | None = None,
    ) -> AdvisoryPersistenceResult:
        """Atomically open, update, or resolve advisory episodes.

        ``normal`` and ``suppressed`` are affirmative recovery states and may
        resolve an episode.  Missing history, stale evidence, and plausibility
        rejection are inconclusive. Confirmed warnings retain their history;
        interrupted never-warning vehicle-health watches archive unconfirmed
        after their quiet window. Coverage changes are audit notes, not alerts.  Only an unacknowledged ``warning`` with
        ``notification_eligible=true`` enters the rate-limited outbox.

        Assessments carrying an integer ``tier`` follow the tiered policy
        (``_notification_policy``): group cooldowns, once-per-episode pushes,
        tier-0 critical repeats, a per-trip cap, quiet-hours deferral, a
        tier-0 recovery row, any-regime timed recovery and the 24 h parked
        closure.  Assessments without ``tier`` keep the legacy behaviour.
        """

        from projects.vehicle_data.event_history import stamp, recovery_gate, checkpoint, archive_baselines, finish_windows, archive_interrupted_watch, archive_parked_episode

        moment = _utc_datetime(evaluated_at, "evaluated_at")
        event_us = _to_us(moment)
        authoritative_rules: set[str] | None = None
        if authoritative_rule_keys is not None:
            if isinstance(authoritative_rule_keys, (str, bytes, bytearray)):
                raise ValueError("authoritative_rule_keys must be a sequence of rule keys")
            normalized_rules = tuple(authoritative_rule_keys)
            if any(
                not isinstance(rule, str) or not rule or len(rule) > 500
                for rule in normalized_rules
            ):
                raise ValueError(
                    "authoritative_rule_keys must contain bounded nonempty text"
                )
            if len(normalized_rules) != len(set(normalized_rules)):
                raise ValueError("authoritative_rule_keys must be unique")
            authoritative_rules = set(normalized_rules)
        counters = {
            "opened": 0,
            "updated": 0,
            "resolved": 0,
            "inconclusive": 0,
            "notifications_enqueued": 0,
            "notifications_capped": 0,
            "notifications_deferred": 0,
        }
        seen: set[str] = set()
        with self._lock, self._conn:
            for index, assessment in enumerate(assessments):
                if not isinstance(assessment, Mapping):
                    raise ValueError(f"assessment[{index}] must be an object")
                rule_key = assessment.get("rule")
                title = assessment.get("title")
                state = assessment.get("state")
                if not isinstance(rule_key, str) or not rule_key:
                    raise ValueError(f"assessment[{index}].rule must be text")
                if rule_key in seen:
                    raise ValueError(f"duplicate advisory rule {rule_key!r}")
                seen.add(rule_key)
                if not isinstance(title, str) or not title:
                    raise ValueError(f"assessment[{index}].title must be text")
                allowed_states = (
                    ADVISORY_ACTIVE_STATES
                    | ADVISORY_RESOLVING_STATES
                    | ADVISORY_INCONCLUSIVE_STATES
                )
                if state not in allowed_states:
                    raise ValueError(
                        f"assessment[{index}].state {state!r} is unsupported"
                    )
                category = assessment.get("category", "vehicle_health")
                if not isinstance(category, str) or not category:
                    raise ValueError(f"assessment[{index}].category must be text")
                if assessment.get("advisory") is not True:
                    raise ValueError(f"assessment[{index}] must remain advisory")
                episode = self._conn.execute(
                    """
                    SELECT * FROM advisory_episodes
                    WHERE rule_key=? AND status='open'
                    """,
                    (rule_key,),
                ).fetchone()

                # Ignore duplicated or late evaluations before any counter/outbox mutation.
                if episode is not None and event_us <= episode["last_evaluated_us"]:
                    continue
                if episode is not None and assessment.get("rule_revision"):
                    opening = json.loads(episode["first_assessment_json"])
                    prior_revision = opening.get("rule_revision")
                    if prior_revision and prior_revision != assessment["rule_revision"]:
                        retired = json.loads(episode["latest_assessment_json"])
                        retired.update(state="suppressed", notification_eligible=False,
                                       reason="rule revision replaced; administrative closure, recovery not established")
                        self._conn.execute("UPDATE advisory_episodes SET status='resolved',current_state='suppressed',evidence_state='suppressed',resolved_us=?,resolved_at=?,resolution_reason=? WHERE id=?",
                                           (event_us,_iso_from_us(event_us),retired["reason"],episode["id"]))
                        self._insert_advisory_event_locked(episode_id=episode["id"],event_us=event_us,event_type="rule_replaced",previous_state=episode["current_state"],new_state="suppressed",context_fingerprint=self._advisory_context_fingerprint(retired),assessment_json=self._advisory_json(retired))
                        self._conn.execute("UPDATE advisory_notification_outbox SET status='cancelled',last_error='rule replaced before delivery' WHERE episode_id=? AND status='pending'",(episode["id"],))
                        counters["resolved"] += 1
                        episode = None
                if archive_interrupted_watch(self._conn, episode, assessment, event_us):
                    counters["resolved"] += 1
                    episode = None
                if archive_parked_episode(self._conn, episode, assessment, event_us):
                    counters["resolved"] += 1
                    episode = None
                assessment = recovery_gate(self._conn, episode, assessment, event_us)
                if episode is not None or state in ADVISORY_ACTIVE_STATES:
                    archive_baselines(self._conn, assessment, self._baseline_inputs)
                assessment = stamp(assessment, _iso_from_us(event_us))
                state = assessment["state"]
                assessment_json = self._advisory_json(assessment)
                fingerprint = self._advisory_context_fingerprint(assessment)
                if episode is not None:
                    checkpoint(self._conn, episode, assessment, event_us)

                if state in ADVISORY_INCONCLUSIVE_STATES:
                    counters["inconclusive"] += 1
                    if episode is None:
                        continue
                    prior_evidence = str(episode["evidence_state"])
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET evidence_state=?,last_evaluated_us=?,
                            last_evaluated_at=?,latest_assessment_json=?,
                            latest_context_fingerprint=?,update_count=update_count+1
                        WHERE id=?
                        """,
                        (
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    if prior_evidence != state:
                        self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type="evidence_inconclusive",
                            previous_state=prior_evidence,
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    counters["updated"] += 1
                    continue

                if state in ADVISORY_RESOLVING_STATES:
                    if episode is None:
                        continue
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET status='resolved',current_state=?,evidence_state=?,
                            last_evaluated_us=?,
                            last_evaluated_at=?,resolved_us=?,resolved_at=?,
                            resolution_reason=?,latest_assessment_json=?,
                            latest_context_fingerprint=?,update_count=update_count+1,
                            transition_count=transition_count+1
                        WHERE id=?
                        """,
                        (
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            assessment.get("reason") or f"assessment became {state}",
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    resolved_event_id = self._insert_advisory_event_locked(
                        episode_id=int(episode["id"]),
                        event_us=event_us,
                        event_type="resolved",
                        previous_state=str(episode["current_state"]),
                        new_state=str(state),
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_notification_outbox
                        SET status='cancelled',last_error='episode resolved before delivery'
                        WHERE episode_id=? AND status='pending'
                        """,
                        (episode["id"],),
                    )
                    # After the cancel above, so the new row survives it.
                    if self._enqueue_recovery_notification_locked(
                        episode=episode,
                        event_id=resolved_event_id,
                        event_us=event_us,
                        fingerprint=fingerprint,
                        assessment=assessment,
                        assessment_json=assessment_json,
                    ):
                        counters["notifications_enqueued"] += 1
                    counters["resolved"] += 1
                    continue

                assert state in ADVISORY_ACTIVE_STATES
                event_id: int | None = None
                if episode is None:
                    cursor = self._conn.execute(
                        """
                        INSERT INTO advisory_episodes(
                            rule_key,category,title,advisory,status,current_state,
                            evidence_state,opened_us,opened_at,last_evaluated_us,
                            last_evaluated_at,last_observed_us,last_observed_at,
                            first_assessment_json,latest_assessment_json,
                            latest_context_fingerprint
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            rule_key,
                            category,
                            title,
                            1,
                            "open",
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            assessment_json,
                            assessment_json,
                            fingerprint,
                        ),
                    )
                    episode_id = int(cursor.lastrowid)
                    event_id = self._insert_advisory_event_locked(
                        episode_id=episode_id,
                        event_us=event_us,
                        event_type="opened",
                        previous_state=None,
                        new_state=str(state),
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    episode = self._conn.execute(
                        "SELECT * FROM advisory_episodes WHERE id=?",
                        (episode_id,),
                    ).fetchone()
                    checkpoint(self._conn, episode, assessment, event_us)
                    counters["opened"] += 1
                else:
                    previous_state = str(episode["current_state"])
                    prior_evidence = str(episode["evidence_state"])
                    previous_fingerprint = str(episode["latest_context_fingerprint"])
                    transitions = int(previous_state != state)
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET category=?,title=?,current_state=?,evidence_state=?,
                            last_evaluated_us=?,last_evaluated_at=?,
                            last_observed_us=?,last_observed_at=?,
                            observation_count=observation_count+1,
                            update_count=update_count+1,
                            transition_count=transition_count+?,
                            latest_assessment_json=?,latest_context_fingerprint=?
                        WHERE id=?
                        """,
                        (
                            category,
                            title,
                            state,
                            state,
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            transitions,
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    if previous_state != state:
                        event_type = (
                            "escalated" if state == "warning" else "deescalated"
                        )
                    elif prior_evidence in ADVISORY_INCONCLUSIVE_STATES:
                        event_type = "evidence_restored"
                    elif previous_fingerprint != fingerprint:
                        event_type = "context_updated"
                    else:
                        event_type = ""
                    if event_type:
                        event_id = self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type=event_type,
                            previous_state=previous_state,
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    episode = self._conn.execute(
                        "SELECT * FROM advisory_episodes WHERE id=?",
                        (episode["id"],),
                    ).fetchone()
                    counters["updated"] += 1

                policy = self._notification_policy(assessment)
                if not policy.get("legacy"):
                    self._record_tiered_notification_locked(
                        episode=episode,
                        event_id=event_id,
                        event_us=event_us,
                        fingerprint=fingerprint,
                        assessment=assessment,
                        assessment_json=assessment_json,
                        policy=policy,
                        counters=counters,
                    )
                    continue
                notification_eligible = (
                    state == "warning"
                    and assessment.get("notification_eligible") is True
                    and episode["acknowledged_us"] is None
                    and assessment.get("category") not in SYSTEM_NOTE_CATEGORIES
                )
                if notification_eligible:
                    rate_limit = self._notification_rate_limit(assessment)
                    if not self._notification_due_locked(
                        episode_id=int(episode["id"]),
                        rule_key=rule_key,
                        event_us=event_us,
                        rate_limit_seconds=rate_limit,
                    ):
                        continue
                    if event_id is None:
                        event_id = self._insert_advisory_event_locked(
                            episode_id=int(episode["id"]),
                            event_us=event_us,
                            event_type="notification_repeat_due",
                            previous_state=str(state),
                            new_state=str(state),
                            context_fingerprint=fingerprint,
                            assessment_json=assessment_json,
                        )
                    if self._enqueue_advisory_notification_locked(
                        episode=episode,
                        event_id=event_id,
                        event_us=event_us,
                        context_fingerprint=fingerprint,
                        assessment=assessment,
                        assessment_json=assessment_json,
                    ):
                        counters["notifications_enqueued"] += 1
            if authoritative_rules is not None:
                if seen != authoritative_rules:
                    raise ValueError(
                        "authoritative_rule_keys must exactly match the evaluated assessments"
                    )
                open_rows = self._conn.execute(
                    "SELECT * FROM advisory_episodes WHERE status='open'"
                ).fetchall()
                for episode in open_rows:
                    if str(episode["rule_key"]) in authoritative_rules:
                        continue
                    reason = "rule retired from authoritative evaluator catalog"
                    try:
                        retired_assessment = json.loads(
                            episode["latest_assessment_json"]
                        )
                    except (TypeError, json.JSONDecodeError):
                        retired_assessment = {}
                    if not isinstance(retired_assessment, dict):
                        retired_assessment = {}
                    retired_assessment.update(
                        {
                            "rule": episode["rule_key"],
                            "title": episode["title"],
                            "category": episode["category"],
                            "advisory": True,
                            "state": "suppressed",
                            "reason": reason,
                            "notification_eligible": False,
                        }
                    )
                    assessment_json = self._advisory_json(retired_assessment)
                    fingerprint = self._advisory_context_fingerprint(
                        retired_assessment
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_episodes
                        SET status='resolved',current_state='suppressed',
                            evidence_state='suppressed',
                            last_evaluated_us=?,last_evaluated_at=?,
                            resolved_us=?,resolved_at=?,resolution_reason=?,
                            latest_assessment_json=?,latest_context_fingerprint=?,
                            update_count=update_count+1,
                            transition_count=transition_count+1
                        WHERE id=? AND status='open'
                        """,
                        (
                            event_us,
                            _iso_from_us(event_us),
                            event_us,
                            _iso_from_us(event_us),
                            reason,
                            assessment_json,
                            fingerprint,
                            episode["id"],
                        ),
                    )
                    self._insert_advisory_event_locked(
                        episode_id=int(episode["id"]),
                        event_us=event_us,
                        event_type="rule_retired",
                        previous_state=str(episode["current_state"]),
                        new_state="suppressed",
                        context_fingerprint=fingerprint,
                        assessment_json=assessment_json,
                    )
                    self._conn.execute(
                        """
                        UPDATE advisory_notification_outbox
                        SET status='cancelled',
                            last_error='rule retired before delivery'
                        WHERE episode_id=? AND status='pending'
                        """,
                        (episode["id"],),
                    )
                    counters["resolved"] += 1
            finish_windows(self._conn, event_us)
        return AdvisoryPersistenceResult(
            evaluated_at=_iso(moment),
            **counters,
        )

    @staticmethod
    def _advisory_episode_dict(row: sqlite3.Row, now_us: int) -> dict[str, object]:
        from projects.vehicle_data.event_history import episode_outcome
        end_us = row["resolved_us"] if row["resolved_us"] is not None else now_us
        return {
            "id": row["id"],
            "rule": row["rule_key"],
            "category": row["category"],
            "title": row["title"],
            "advisory": bool(row["advisory"]),
            "status": row["status"],
            "state": row["current_state"],
            "evidence_state": row["evidence_state"],
            "opened_at": row["opened_at"],
            "last_evaluated_at": row["last_evaluated_at"],
            "last_observed_at": row["last_observed_at"],
            "resolved_at": row["resolved_at"],
            "resolution_reason": row["resolution_reason"],
            "outcome": episode_outcome(row),
            "duration_seconds": max(0.0, (end_us - row["opened_us"]) / MICROSECONDS),
            "observation_count": row["observation_count"],
            "update_count": row["update_count"],
            "transition_count": row["transition_count"],
            "acknowledged": row["acknowledged_us"] is not None,
            "acknowledged_at": row["acknowledged_at"],
            "acknowledgment_note": row["acknowledgment_note"],
            "first_assessment": json.loads(row["first_assessment_json"]),
            "latest_assessment": json.loads(row["latest_assessment_json"]),
        }

    def list_advisory_episodes(
        self,
        *,
        active_only: bool = False,
        limit: int = 100,
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        where = "WHERE status='open'" if active_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM advisory_episodes {where} "
                "ORDER BY opened_us DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        now_us = _to_us(moment)
        return [self._advisory_episode_dict(row, now_us) for row in rows]

    def list_advisory_events(
        self,
        episode_id: int,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        if not isinstance(episode_id, int) or isinstance(episode_id, bool) or episode_id < 1:
            raise ValueError("episode_id must be a positive integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM advisory_episode_events WHERE episode_id=?
                ORDER BY event_us DESC,id DESC LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "episode_id": row["episode_id"],
                "at": row["event_at"],
                "type": row["event_type"],
                "previous_state": row["previous_state"],
                "new_state": row["new_state"],
                "assessment": json.loads(row["assessment_json"]),
            }
            for row in rows
        ]

    def acknowledge_advisory_episode(
        self,
        episode_id: int,
        *,
        acknowledged_at: datetime | str | None = None,
        note: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(episode_id, int) or isinstance(episode_id, bool) or episode_id < 1:
            raise ValueError("episode_id must be a positive integer")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise ValueError("acknowledgment note must be text of at most 500 characters")
        moment = (
            datetime.now(timezone.utc)
            if acknowledged_at is None
            else _utc_datetime(acknowledged_at, "acknowledged_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM advisory_episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown advisory episode {episode_id}")
            if row["acknowledged_us"] is None:
                self._conn.execute(
                    """
                    UPDATE advisory_episodes
                    SET acknowledged_us=?,acknowledged_at=?,acknowledgment_note=?
                    WHERE id=?
                    """,
                    (at_us, _iso(moment), note, episode_id),
                )
                self._insert_advisory_event_locked(
                    episode_id=episode_id,
                    event_us=at_us,
                    event_type="acknowledged",
                    previous_state=row["current_state"],
                    new_state=row["current_state"],
                    context_fingerprint=row["latest_context_fingerprint"],
                    assessment_json=row["latest_assessment_json"],
                )
                self._conn.execute(
                    """
                    UPDATE advisory_notification_outbox
                    SET status='cancelled',last_error='episode acknowledged before delivery'
                    WHERE episode_id=? AND status='pending'
                    """,
                    (episode_id,),
                )
            updated = self._conn.execute(
                "SELECT * FROM advisory_episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
        return self._advisory_episode_dict(updated, at_us)

    def pending_advisory_notifications(
        self,
        *,
        at: datetime | str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        moment = datetime.now(timezone.utc) if at is None else _utc_datetime(at, "at")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT outbox.* FROM advisory_notification_outbox AS outbox
                JOIN advisory_episodes AS episode ON episode.id=outbox.episode_id
                WHERE outbox.status='pending' AND outbox.eligible_after_us<=?
                  AND (episode.status='open'
                       OR json_extract(outbox.payload_json,'$.notification_kind')='recovery')
                  AND episode.acknowledged_us IS NULL
                ORDER BY outbox.created_us,outbox.id LIMIT ?
                """,
                (_to_us(moment), limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "episode_id": row["episode_id"],
                "rule": row["rule_key"],
                "created_at": row["created_at"],
                "attempt_count": row["attempt_count"],
                "last_error": row["last_error"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def mark_advisory_notification_delivered(
        self,
        notification_id: int,
        *,
        delivered_at: datetime | str | None = None,
    ) -> None:
        moment = (
            datetime.now(timezone.utc)
            if delivered_at is None
            else _utc_datetime(delivered_at, "delivered_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE advisory_notification_outbox
                SET status='delivered',attempt_count=attempt_count+1,
                    last_attempt_us=?,last_attempt_at=?,delivered_us=?,
                    delivered_at=?,last_error=NULL
                WHERE id=? AND status='pending'
                """,
                (at_us, _iso(moment), at_us, _iso(moment), notification_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"pending advisory notification {notification_id} not found")

    def mark_advisory_notification_failed(
        self,
        notification_id: int,
        *,
        error: str,
        attempted_at: datetime | str | None = None,
        retry_after_seconds: float = 300,
        max_attempts: int = 3,
    ) -> None:
        if not isinstance(error, str) or not error or len(error) > 1000:
            raise ValueError("notification error must be nonempty text of at most 1000 characters")
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(float(retry_after_seconds))
            or not 1 <= retry_after_seconds <= 24 * 60 * 60
        ):
            raise ValueError("retry_after_seconds must be between 1 and 86400")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        moment = (
            datetime.now(timezone.utc)
            if attempted_at is None
            else _utc_datetime(attempted_at, "attempted_at")
        )
        at_us = _to_us(moment)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM advisory_notification_outbox WHERE id=? AND status='pending'",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"pending advisory notification {notification_id} not found")
            attempts = int(row["attempt_count"]) + 1
            terminal = attempts >= max_attempts
            self._conn.execute(
                """
                UPDATE advisory_notification_outbox
                SET status=?,attempt_count=?,last_attempt_us=?,last_attempt_at=?,
                    eligible_after_us=?,last_error=? WHERE id=?
                """,
                (
                    "failed" if terminal else "pending",
                    attempts,
                    at_us,
                    _iso(moment),
                    at_us + int(round(retry_after_seconds * MICROSECONDS)),
                    error,
                    notification_id,
                ),
            )

    def advisory_summary(
        self,
        *,
        now: datetime | str | None = None,
        recent_limit: int = 25,
    ) -> dict[str, object]:
        moment = datetime.now(timezone.utc) if now is None else _utc_datetime(now, "now")
        with self._lock:
            counts = {
                row["status"]: row["count"]
                for row in self._conn.execute(
                    """
                    SELECT status,count(*) AS count
                    FROM advisory_notification_outbox GROUP BY status
                    """
                )
            }
        return {
            "schema_version": ADVISORY_SCHEMA_VERSION,
            "generated_at": _iso(moment),
            "active": self.list_advisory_episodes(
                active_only=True,
                limit=recent_limit,
                now=moment,
            ),
            "recent": self.list_advisory_episodes(
                active_only=False,
                limit=recent_limit,
                now=moment,
            ),
            "notification_outbox": {
                "pending": counts.get("pending", 0),
                "delivered": counts.get("delivered", 0),
                "failed": counts.get("failed", 0),
                "cancelled": counts.get("cancelled", 0),
            },
        }
