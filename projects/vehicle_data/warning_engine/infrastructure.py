"""Read-only infrastructure-health assessment from persisted host evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from lib.vehicle_can_roles import CAN_BUS_ROLES
from projects.vehicle_data.historian import TelemetryHistorian

from .rules import ROLE_USB_SERIALS, WARNING_SCHEMA_VERSION
from .samples import _utc


class InfrastructureHealthEvaluator:
    """Translate persisted USB/interface facts into tier-3 system assessments.

    This evaluator never resets, rebinds, or reconfigures hardware.  Nothing
    here is notification-eligible: adapter, bus and data-quality items are
    shown in the dashboard's System notes only (owner decision 2026-09-24).
    Severity still ranks them there: a controller error or ambiguous identity
    (immediately), a missing adapter or down link after about five minutes
    (60 historian samples), a failed status probe and a latched restoration
    inhibit are the hard faults.  A role missing or failing on its first
    observation reports ``normal`` with ``pending`` counts, so no System event
    opens until the second observation confirms it.  USB topology changes and
    transient disconnects are information unless they persist.
    """

    MISSING_NOTIFY_OBSERVATIONS = 60
    TRANSIENT_WARNING_SECONDS = 5 * 60
    TRANSIENT_WARNING_REMOVALS_24H = 3
    ADAPTER_ACTION = "Check the CAN adapter cable."
    USB_ACTION = "Check the USB hub and adapter."
    INHIBIT_ACTION = "Restart the telemetry service when parked."
    DEAF_ACTION = "Unplug the CAN adapter's USB cable for 5 s, or reboot the Pi."

    def __init__(self, historian: TelemetryHistorian):
        self.historian = historian

    @staticmethod
    def _base(
        *,
        rule: str,
        title: str,
        severity: str = "warning",
        rate_limit_seconds: float = 30 * 60,
        action: str = "Check the USB hub and adapter.",
    ) -> dict[str, object]:
        return {
            "rule": rule,
            "title": title,
            "metric": None,
            "category": "can_infrastructure",
            "severity": severity,
            "advisory": True,
            "notification_eligible": False,
            "notification_rate_limit_seconds": rate_limit_seconds,
            "interpretation": (
                "host/interface evidence only; no hardware reset, USB power "
                "cycle, CAN reconfiguration, or component diagnosis is implied"
            ),
            "tier": 3,
            "group": "system",
            "action": action,
            "confidence": None,
        }

    @staticmethod
    def _inhibit_name(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("name", "reason", "kind"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
        return ""

    def _infrastructure_usb_context(
        self,
        evaluated: datetime,
        usb_context: Mapping[str, object],
    ) -> dict[str, object]:
        removals = usb_context.get("new_removal_events")
        removals = removals if isinstance(removals, list) else []
        active_usb = usb_context.get("active_incidents")
        active_usb = active_usb if isinstance(active_usb, list) else []
        usb_history_available = usb_context.get("available") is True
        transient_detected = bool(removals or active_usb)
        affected_serials = sorted(
            {
                serial
                for item in [*removals, *active_usb]
                if isinstance(item, Mapping)
                for serial in item.get("affected_serials", [])
                if isinstance(serial, str)
            }
        )
        removal_count_24h = usb_context.get("removal_event_count_24h", 0)
        removal_count_24h = (
            removal_count_24h
            if isinstance(removal_count_24h, int) and not isinstance(removal_count_24h, bool)
            else 0
        )
        oldest_active_seconds = None
        for item in active_usb:
            if not isinstance(item, Mapping) or not isinstance(item.get("opened_at"), str):
                continue
            try:
                opened = _utc(str(item["opened_at"]))
            except ValueError:
                continue
            elapsed = (evaluated - opened).total_seconds()
            if oldest_active_seconds is None or elapsed > oldest_active_seconds:
                oldest_active_seconds = elapsed
        long_incident = (
            oldest_active_seconds is not None
            and oldest_active_seconds >= self.TRANSIENT_WARNING_SECONDS
        )
        repeated_removals = removal_count_24h >= self.TRANSIENT_WARNING_REMOVALS_24H
        if removals:
            usb_reason = (
                f"{len(removals)} new receive-only kernel removal edge(s) "
                "were durably recorded"
            )
        elif active_usb:
            usb_reason = (
                f"{len(active_usb)} USB CAN incident(s) remain active pending "
                "healthy exact-role re-resolution"
            )
        elif usb_history_available:
            usb_reason = "no new or unresolved USB CAN removal incident is present"
        else:
            usb_reason = "kernel USB CAN incident history is not available for this snapshot"
        if long_incident or repeated_removals:
            usb_state = "warning"
            usb_reason = (
                "a USB CAN incident has stayed unresolved for more than five minutes"
                if long_incident
                else f"{removal_count_24h} USB CAN removals in the last 24 hours"
            )
        elif transient_detected or usb_history_available:
            # Short, self-resolving hub re-enumerations are information only.
            usb_state = "normal"
            if transient_detected:
                usb_reason += "; short-lived, recorded as information"
        else:
            # Lost incident history is not affirmative recovery evidence.  The
            # advisory lifecycle treats unavailable as inconclusive and keeps
            # any already-open episode intact without notification eligibility.
            usb_state = "unavailable"
        return {
            "context": usb_context,
            "removals": removals,
            "active": active_usb,
            "transient_detected": transient_detected,
            "affected_serials": affected_serials,
            "removal_count_24h": removal_count_24h,
            "oldest_active_seconds": oldest_active_seconds,
            "state": usb_state,
            "reason": usb_reason,
        }

    def _infrastructure_role_status(
        self,
        role: str,
        base: dict[str, object],
        current: Mapping[str, object] | None,
        role_gap: Mapping[str, object],
        count: int,
        waiting_probe: bool,
        failed_probe: bool,
    ) -> dict[str, object]:
        status_unproven = current is None or current.get("health") == "unknown"
        immediate = False
        missing_or_down = False
        if status_unproven and (waiting_probe or failed_probe):
            state = "unavailable"
            reason = ("awaiting the broker's first interface status probe" if waiting_probe else "broker interface status probe failed; device health is unknown")
            current_payload = {"role":role,"health":"unknown","reason":"status_probe_pending" if waiting_probe else "status_probe_failed"}
            base["title"] = f"{role} interface status pending"
        elif current is None:
            base["title"] = f"{role} interface status is missing"
            state = "warning" if count >= 2 else "normal"
            reason = "logical role is absent from current interface status"
            missing_or_down = True
            current_payload = {
                "role": role,
                "health": "missing",
                "reason": role_gap.get("reason", "interface_role_absent"),
                "gap_observation_count": count,
            }
            if state != "warning":
                reason += "; first observation, confirming"
                current_payload["pending"] = {"observed": count, "required": 2}
        else:
            current_payload = dict(current)
            health = current.get("health")
            resolution = current.get("resolution")
            cause = current.get("reason")
            if health == "healthy":
                state = "normal"
                reason = "role is resolved and its controller is healthy"
            elif health == "unknown":
                state = "normal"
                reason = "role health evidence is incomplete"
                current_payload["pending"] = {"observed": count, "required": 2}
                base["title"] = f"{role} interface status is incomplete"
            else:
                causes = set(str(cause).split(","))
                # receive_silent already needed two minutes of evidence.
                immediate = (
                    resolution == "ambiguous"
                    or "controller_unhealthy" in causes
                    or "receive_silent" in causes
                )
                state = "warning" if immediate or count >= 2 else "normal"
                if resolution in ("missing", "ambiguous"):
                    reason = f"logical USB role resolution is {resolution}"
                    base["title"] = f"{role} adapter {'identity is ambiguous' if resolution == 'ambiguous' else 'is missing'}"
                    missing_or_down = resolution == "missing"
                elif "controller_unhealthy" in causes:
                    reason = "SocketCAN controller is not ERROR-ACTIVE"
                    base["title"] = f"{role} controller error"
                elif "receive_silent" in causes:
                    reason = (
                        "no frames received for two minutes or more while "
                        "CAN-CH was continuously busy; the link reports "
                        "normal, so the adapter itself has stopped receiving"
                    )
                    base["title"] = f"{role} adapter hears nothing while CAN-CH is active"
                    base["action"] = self.DEAF_ACTION
                else:
                    reason = f"interface health check failed: {cause}"
                    if "interface_down" in causes:
                        base["title"] = f"{role} interface is down"
                        missing_or_down = True
                    elif "adapter_missing" in causes:
                        base["title"] = f"{role} adapter is missing"
                        missing_or_down = True
                    else:
                        base["title"] = f"{role} interface configuration mismatch"
                        if current.get("role_reason"):
                            reason += f" ({current['role_reason']})"
            current_payload["gap_observation_count"] = count
            if health not in ("healthy", "unknown") and state != "warning":
                reason += "; first observation, confirming"
                current_payload["pending"] = {"observed": count, "required": 2}
        return {
            "base": base,
            "state": state,
            "reason": reason,
            "current": current,
            "current_payload": current_payload,
            "immediate": immediate,
            "missing_or_down": missing_or_down,
        }

    def _infrastructure_role_assessment(
        self,
        role: str,
        roles: Mapping[str, object],
        gaps: Mapping[str, object],
        global_gap: Mapping[str, object],
        *,
        waiting_probe: bool,
        failed_probe: bool,
        usb: Mapping[str, object],
    ) -> dict[str, object]:
        normalized_rule = role.replace("-", "_")
        base = self._base(
            rule=f"can_interface_role_{normalized_rule}",
            title=f"{role} interface status",
            action=self.ADAPTER_ACTION,
        )
        current = roles.get(role)
        current = current if isinstance(current, Mapping) else None
        role_gap = current.get("active_gap") if isinstance(current, Mapping) else gaps.get(role)
        role_gap = role_gap if isinstance(role_gap, Mapping) else global_gap
        count = role_gap.get("observation_count", 0)
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        status = self._infrastructure_role_status(
            role, base, current, role_gap, count, waiting_probe, failed_probe
        )
        state = status["state"]
        immediate = status["immediate"]
        missing_or_down = status["missing_or_down"]
        hard = state == "warning" and (
            immediate
            or (missing_or_down and count >= self.MISSING_NOTIFY_OBSERVATIONS)
        )
        role_serial = (
            current.get("usb_serial")
            if isinstance(current, Mapping)
            and isinstance(current.get("usb_serial"), str)
            else ROLE_USB_SERIALS.get(role)
        )
        usb_covers_role = bool(
            usb["transient_detected"]
            and role_serial in usb["affected_serials"]
        )
        return {
            **base,
            "state": state,
            "reason": status["reason"],
            "severity": "warning" if hard else "info",
            "notification_eligible": False,
            "notification_suppressed_by": (
                "usb_can_transient_disconnect"
                if state == "warning" and usb_covers_role
                else None
            ),
            "current": status["current_payload"],
            "persistence": {
                "required": 1 if (
                    current is not None
                    and (
                        current.get("resolution") == "ambiguous"
                        or "controller_unhealthy" in str(current.get("reason")).split(",")
                    )
                ) else 2,
                "observed": count,
                "notification_required": (
                    1 if immediate else self.MISSING_NOTIFY_OBSERVATIONS
                ),
            },
        }

    def _infrastructure_probe_assessment(
        self,
        probe: Mapping[str, object],
        *,
        waiting_probe: bool,
        failed_probe: bool,
        overdue: bool,
    ) -> dict[str, object] | None:
        if not probe:
            return None
        discovery_state = "warning" if overdue or failed_probe else "unavailable" if waiting_probe else "normal"
        return {
            **self._base(rule="can_interface_status_probe", title="Interface status probe failed" if failed_probe else "Interface discovery delayed" if overdue else "Interface status initialization", severity="warning" if failed_probe else "info", action=self.USB_ACTION),
            "state":discovery_state,
            "reason":"interface status probe failed; adapter health cannot be established" if failed_probe else "first interface status probe has not completed within 30 seconds" if overdue else "awaiting initial interface discovery" if waiting_probe else "interface status probe completed",
            "notification_eligible": False,
            "current":dict(probe),
        }

    def _infrastructure_topology_assessment(
        self,
        context: Mapping[str, object],
        *,
        hard_role: bool,
    ) -> dict[str, object]:
        topology_changed = context.get("topology_changed") is True
        topology_warning = topology_changed and hard_role
        return {
            **self._base(
                rule="usb_can_topology_generation_changed",
                title="USB CAN topology generation changed",
                severity="warning" if topology_warning else "info",
                rate_limit_seconds=10 * 60,
                action=self.USB_ACTION,
            ),
            "state": "warning" if topology_warning else "normal",
            "reason": (
                "serial/dev_id to netdev topology changed while an adapter "
                "role is failing"
                if topology_warning
                else "serial/dev_id to netdev topology changed since the prior "
                "sample; recorded as information"
                if topology_changed
                else "USB CAN topology generation is stable or establishing its baseline"
            ),
            "notification_eligible": False,
            "notification_suppressed_by": None,
            "current": {
                "topology_changed": topology_changed,
                "topology_generation": context.get("topology_generation"),
                "previous_topology_generation": context.get(
                    "previous_topology_generation"
                ),
                "issues": context.get("issues"),
            },
        }

    def _infrastructure_usb_assessment(
        self,
        usb: Mapping[str, object],
    ) -> dict[str, object]:
        removals = usb["removals"]
        active_usb = usb["active"]
        usb_context = usb["context"]
        return {
            **self._base(
                rule="usb_can_transient_disconnect",
                title="USB CAN branch transiently disconnected",
                severity="warning" if usb["state"] == "warning" else "info",
                rate_limit_seconds=10 * 60,
                action=self.USB_ACTION,
            ),
            "state": usb["state"],
            "reason": usb["reason"],
            "notification_eligible": False,
            "current": {
                "new_removal_event_count": len(removals),
                "active_incident_count": len(active_usb),
                "oldest_active_incident_seconds": usb["oldest_active_seconds"],
                "affected_serials": usb["affected_serials"],
                "event_ids": [
                    item.get("event_id")
                    for item in removals
                    if isinstance(item, Mapping)
                    and isinstance(item.get("event_id"), str)
                ],
                "incident_ids": [
                    item.get("incident_id")
                    for item in active_usb
                    if isinstance(item, Mapping)
                    and isinstance(item.get("incident_id"), str)
                ],
                "dropped_event_count": usb_context.get("dropped_event_count", 0),
                "removal_event_count_24h": usb["removal_count_24h"],
                "source": usb_context.get("source"),
            },
            "persistence": {
                "required": 1,
                "observed": len(removals) or len(active_usb),
                "warning_after_seconds": self.TRANSIENT_WARNING_SECONDS,
                "warning_after_removals_24h": self.TRANSIENT_WARNING_REMOVALS_24H,
            },
        }

    def _infrastructure_restoration_assessment(
        self,
        context: Mapping[str, object],
    ) -> dict[str, object]:
        inhibits = context.get("active_inhibits")
        inhibits = inhibits if isinstance(inhibits, list) else []
        restoration_inhibits = [
            value
            for value in inhibits
            if "restoration" in self._inhibit_name(value).lower()
        ]
        restoration_failed = context.get("restoration_failed") is True
        inhibited = restoration_failed or bool(restoration_inhibits)
        return {
            **self._base(
                rule="can_restoration_inhibit",
                title="CAN restoration inhibit is active",
                severity="critical",
                rate_limit_seconds=10 * 60,
                action=self.INHIBIT_ACTION,
            ),
            "state": "warning" if inhibited else "normal",
            "reason": (
                "active-drive restoration failed or a restoration inhibit is latched"
                if inhibited
                else "no restoration failure or restoration inhibit is present"
            ),
            "notification_eligible": False,
            "current": {
                "restoration_failed": restoration_failed,
                "active_inhibits": restoration_inhibits,
            },
        }

    def evaluate(
        self,
        snapshot_id: int,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, object]:
        evaluated = _utc(at)
        context = self.historian.system_health_context(snapshot_id)
        roles = context.get("roles")
        roles = roles if isinstance(roles, Mapping) else {}
        gaps = context.get("active_interface_gaps")
        gaps = gaps if isinstance(gaps, Mapping) else {}
        global_gap = gaps.get("interface-status")
        global_gap = global_gap if isinstance(global_gap, Mapping) else {}
        try:
            usb_context = self.historian.usb_can_health_context(snapshot_id)
        except (AttributeError, KeyError, RuntimeError, ValueError):
            usb_context = {"available": False}
        usb = self._infrastructure_usb_context(evaluated, usb_context)
        probe = context.get("interface_probe") or {}
        probe_state = probe.get("state")
        waiting_probe = probe_state == "awaiting_first_probe"
        failed_probe = probe_state == "failed"
        elapsed = probe.get("elapsed_seconds")
        overdue = waiting_probe and isinstance(elapsed,(int,float)) and not isinstance(elapsed,bool) and elapsed >= 30
        assessments: list[dict[str, object]] = []
        for role in CAN_BUS_ROLES:
            assessments.append(self._infrastructure_role_assessment(
                role,
                roles,
                gaps,
                global_gap,
                waiting_probe=waiting_probe,
                failed_probe=failed_probe,
                usb=usb,
            ))
        hard_role = any(
            item["severity"] == "warning" and item["state"] == "warning"
            for item in assessments
        )
        probe_assessment = self._infrastructure_probe_assessment(
            probe,
            waiting_probe=waiting_probe,
            failed_probe=failed_probe,
            overdue=overdue,
        )
        if probe_assessment is not None:
            assessments.append(probe_assessment)
        assessments.append(
            self._infrastructure_topology_assessment(context, hard_role=hard_role)
        )
        assessments.append(self._infrastructure_usb_assessment(usb))
        assessments.append(self._infrastructure_restoration_assessment(context))
        return {
            "schema_version": WARNING_SCHEMA_VERSION,
            "generated_at": evaluated.isoformat(),
            "method": {
                "source": "persisted serial-role/interface snapshots",
                "automatic_hardware_reset": False,
                "opaque_health_score": False,
            },
            "active": [
                item
                for item in assessments
                if item["state"] in ("watch", "warning")
            ],
            "assessments": assessments,
        }
