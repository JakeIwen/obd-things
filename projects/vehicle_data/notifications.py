"""Explicit advisory notification sinks for the telemetry historian.

This module has no CAN access.  The ntfy sink invokes the host's existing
queue-aware ``ntfy-send`` helper with a fixed argv (never a shell), so an
offline notification server results in a durable local queue rather than a
lost advisory.

It also owns the pure delivery-time helpers used by the historian's tiered
notification policy: quiet-hours deferral in the van's local zone and the
owner-facing message template.  Nothing here imports other vehicle_data
modules, so the historian may import it lazily without a cycle.
"""

from __future__ import annotations

import re
import os
import subprocess
import zoneinfo
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.timeutil import finite_number as _finite


NTFY_SEND = Path("/usr/local/bin/ntfy-send")
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_MESSAGE_CHARS = 1500
MAX_TITLE_CHARS = 120

# Quiet hours (spec section 6): 22:00-07:00 in the van's zone.  Tier 1/2 items
# (and non-critical tier-3 hard faults) raised from 22:00 until the 07:30
# morning digest are held until 07:30 local; tier 0 always sends immediately.
QUIET_HOURS_ZONE = os.environ.get("VAN_TELEMETRY_QUIET_ZONE", "US/Mountain")
QUIET_HOURS_START = 22
QUIET_HOURS_END = 7
DIGEST_HOUR, DIGEST_MINUTE = 7, 30
# True after local_zone() had to fall back to UTC because the zone key (or the
# system tzdata) was unavailable.  Quiet hours are then evaluated in UTC.
QUIET_HOURS_ZONE_FALLBACK = False

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)

# Evaluator vocabulary that must never reach a phone notification (the
# dashboard applies the same list to card lines).
FORBIDDEN_WORDS_RE = re.compile(
    r"\b(regime|mad|deviation|persisten\w*|episode|advisory|unavailable)\b",
    re.IGNORECASE,
)
_RECOVERY_TITLE_SUFFIXES = frozenset(
    ("critical", "low", "high", "hot", "problem", "warning")
)
_GROUP_TAGS = {
    "tires": "warning,car",
    "oil": "warning,car",
    "cooling": "warning,car",
    "transmission": "warning,car",
    "charging": "warning,battery",
    "battery": "warning,battery",
    "system": "warning,computer",
}


def local_zone() -> zoneinfo.ZoneInfo | timezone:
    """Return the configured quiet-hours zone, or UTC when it cannot load."""

    global QUIET_HOURS_ZONE_FALLBACK
    try:
        return zoneinfo.ZoneInfo(QUIET_HOURS_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        QUIET_HOURS_ZONE_FALLBACK = True
        return timezone.utc


def _datetime_from_us(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def quiet_hours_deferral(event_us: int) -> int | None:
    """Return the next 07:30 local time (µs) for an event in quiet hours.

    The window is ``[22:00, 07:30)`` local: quiet hours proper run 22:00-07:00
    and anything raised before the 07:30 digest waits for it.  Outside that
    window the result is ``None``.  Pure function of ``event_us`` and the
    module constants; DST transitions are handled by constructing the target
    wall time in the zone rather than adding a fixed offset.
    """

    if isinstance(event_us, bool) or not isinstance(event_us, int):
        raise TypeError("event_us must be integer microseconds")
    zone = local_zone()
    local = _datetime_from_us(event_us).astimezone(zone)
    minutes = local.hour * 60 + local.minute
    start = QUIET_HOURS_START * 60
    end = DIGEST_HOUR * 60 + DIGEST_MINUTE
    if start > end:
        quiet = minutes >= start or minutes < end
    else:
        quiet = start <= minutes < end
    if not quiet:
        return None
    day = local.date()
    if start > end and minutes >= start:
        day += timedelta(days=1)
    target = datetime(
        day.year, day.month, day.day, DIGEST_HOUR, DIGEST_MINUTE, tzinfo=zone
    )
    return (target - _EPOCH) // _MICROSECOND


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _tier(payload: Mapping[str, object]) -> int | None:
    tier = payload.get("tier")
    if tier is None:
        tier = _mapping(payload.get("assessment")).get("tier")
    if isinstance(tier, int) and not isinstance(tier, bool) and 0 <= tier <= 3:
        return tier
    return None


def _severity(payload: Mapping[str, object]) -> object:
    severity = payload.get("severity")
    if severity is None:
        severity = _mapping(payload.get("assessment")).get("severity")
    return severity


def _format_number(value: float) -> str:
    value = float(value)
    if abs(value) >= 100 or abs(value - round(value)) < 0.05:
        return f"{int(round(value)):,}"
    return f"{value:.1f}"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(1, int(round(seconds)))} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(round(minutes))} min"
    hours = minutes / 60
    return f"{hours:.1f}".rstrip("0").rstrip(".") + " h"


def _clean(text: object) -> str | None:
    """Return owner-safe text, or None when it carries evaluator vocabulary."""

    if not isinstance(text, str):
        return None
    text = " ".join(text.split()).strip()
    if not text or FORBIDDEN_WORDS_RE.search(text):
        return None
    return text


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _local_clock(payload: Mapping[str, object]) -> str | None:
    assessment = _mapping(payload.get("assessment"))
    moment = _parse_iso(assessment.get("evaluated_at")) or _parse_iso(
        payload.get("evaluated_at")
    )
    if moment is None:
        return None
    return moment.astimezone(local_zone()).strftime("%H:%M")


def _value_text(assessment: Mapping[str, object]) -> str | None:
    current = _mapping(assessment.get("current"))
    value = current.get("value")
    if not _finite(value):
        return None
    unit = current.get("unit")
    text = _format_number(float(value))
    return f"{text} {unit}" if isinstance(unit, str) and unit else text


def _comparison(assessment: Mapping[str, object], tier: int) -> str | None:
    current = _mapping(assessment.get("current"))
    value = current.get("value")
    unit = current.get("unit") if isinstance(current.get("unit"), str) else ""
    if tier >= 2:
        return _clean(assessment.get("reason"))
    absolute = _mapping(assessment.get("absolute_threshold"))
    if _finite(absolute.get("value")):
        limit_unit = absolute.get("unit") if isinstance(absolute.get("unit"), str) else unit
        limit = " ".join(filter(None, (_format_number(float(absolute["value"])), limit_unit)))
        if absolute.get("operator") == "above" or (
            absolute.get("operator") is None and assessment.get("direction") == "high"
        ):
            return f"above your {limit} limit"
        return f"below the {limit} minimum"
    baseline = _mapping(assessment.get("baseline"))
    if _finite(baseline.get("median")) and _finite(value):
        median = float(baseline["median"])
        delta = abs(float(value) - median)
        side = "under" if float(value) < median else "over"
        delta_text = " ".join(filter(None, (_format_number(delta), unit)))
        return f"{delta_text} {side} its usual {_format_number(median)}"
    custom = _mapping(assessment.get("custom_rule"))
    if _finite(custom.get("threshold")):
        limit = " ".join(filter(None, (_format_number(float(custom["threshold"])), unit)))
        side = "below" if custom.get("operator") == "below" else "above"
        return f"{side} your {limit} threshold"
    return _clean(assessment.get("reason"))


def _duration_text(payload: Mapping[str, object], assessment: Mapping[str, object]) -> str | None:
    persistence = _mapping(assessment.get("persistence"))
    window = persistence.get("window_seconds")
    if not _finite(window) or float(window) <= 0:
        return None
    observed, required = persistence.get("observed"), persistence.get("required")
    if _finite(observed) and _finite(required) and float(observed) < float(required):
        return "just now"
    seconds = float(window)
    if payload.get("notification_kind") == "repeat":
        opened = _parse_iso(payload.get("opened_at"))
        evaluated = _parse_iso(payload.get("evaluated_at"))
        if opened is not None and evaluated is not None and evaluated > opened:
            seconds += (evaluated - opened).total_seconds()
    return _format_duration(seconds)


def _recovery_title(title: str) -> str:
    words = title.split()
    if len(words) > 1 and words[-1].lower() in _RECOVERY_TITLE_SUFFIXES:
        words = words[:-1]
    return " ".join(words + ["normal"])


def render_message(payload: Mapping[str, object]) -> tuple[str, str]:
    """Owner-facing ntfy title and body for a tiered payload (spec section 7).

    Body line 1 is ``<value> <unit>[ @ <rpm> rpm], <comparison>, <duration>.``
    and line 2 is ``<Action> <HH:MM>`` in the van's local zone.  Recovery
    payloads render ``<title> normal`` / ``<value> <unit> at <HH:MM>.``.
    Evaluator vocabulary (regime, MAD, deviation, persistence, episode,
    advisory) never appears: free text containing it is dropped.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("notification payload must be a mapping")
    assessment = _mapping(payload.get("assessment"))
    raw_title = payload.get("title") or assessment.get("title")
    title = _clean(raw_title) or "Van telemetry"
    clock = _local_clock(payload)
    value = _value_text(assessment)
    if payload.get("notification_kind") == "recovery":
        title = _recovery_title(title)
        if value:
            body = f"{value} at {clock}." if clock else f"{value}."
        else:
            body = f"Back to normal at {clock}." if clock else "Back to normal."
        return title[:MAX_TITLE_CHARS], body[:MAX_MESSAGE_CHARS]
    tier = _tier(payload)
    tier = 1 if tier is None else tier
    lead = value
    rpm = _mapping(_mapping(assessment.get("running_evidence")).get("rpm")).get("value")
    if lead and _finite(rpm):
        lead += f" @ {int(round(float(rpm))):,} rpm"
    parts = [
        part
        for part in (lead, _comparison(assessment, tier), _duration_text(payload, assessment))
        if part
    ]
    line_one = _sentence(", ".join(parts)) if parts else _sentence(title)
    action = _clean(assessment.get("action") or payload.get("action"))
    action = _sentence(action) if action else "Check it when safe."
    line_two = f"{action} {clock}" if clock else action
    body = f"{line_one[:MAX_MESSAGE_CHARS // 2]}\n{line_two[:MAX_MESSAGE_CHARS // 2 - 1]}"
    return title[:MAX_TITLE_CHARS], body[:MAX_MESSAGE_CHARS]


class NtfyAdvisoryNotificationSink:
    """Deliver one bounded advisory through the installed queue-aware helper."""

    enabled = True

    def __init__(
        self,
        topic: str,
        *,
        executable: str | Path = NTFY_SEND,
        timeout_seconds: float = 5.0,
        helper_network_timeout_seconds: int = 2,
        run=subprocess.run,
    ) -> None:
        if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
            raise ValueError(
                "ntfy topic must contain 1-64 letters, digits, underscores, or hyphens"
            )
        if timeout_seconds <= 0:
            raise ValueError("notification timeout must be positive")
        if (
            not isinstance(helper_network_timeout_seconds, int)
            or not 1 <= helper_network_timeout_seconds <= 4
        ):
            raise ValueError("notification helper network timeout must be 1..4 seconds")
        self.topic = topic
        self.executable = Path(executable)
        self.timeout_seconds = float(timeout_seconds)
        self.helper_network_timeout_seconds = helper_network_timeout_seconds
        self.run = run

    @staticmethod
    def _text(value: object, fallback: str) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else fallback

    @staticmethod
    def _priority(payload: Mapping[str, object]) -> str:
        if payload.get("notification_kind") == "recovery":
            return "default"
        tier = _tier(payload)
        severity = _severity(payload)
        if tier is None:
            # Legacy (pre-tier) payloads keep their original mapping.
            if severity == "critical":
                return "max"
            if severity == "warning" or payload.get("state") == "warning":
                return "high"
            return "default"
        if tier == 0:
            return "urgent" if severity == "critical" else "high"
        if tier == 1:
            return "high"
        return "default"

    @staticmethod
    def _tags(payload: Mapping[str, object]) -> str:
        if payload.get("notification_kind") == "recovery":
            return "white_check_mark,car"
        category = payload.get("category")
        legacy = "warning,computer" if category == "can_infrastructure" else "warning,car"
        if _tier(payload) is None:
            return legacy
        group = payload.get("group")
        if group is None:
            group = _mapping(payload.get("assessment")).get("group")
        return _GROUP_TAGS.get(group, legacy) if isinstance(group, str) else legacy

    @classmethod
    def _message(cls, payload: Mapping[str, object]) -> str:
        reason = cls._text(payload.get("reason"), "Telemetry evidence needs review.")
        evaluated = cls._text(payload.get("evaluated_at"), "time unavailable")
        episode = payload.get("episode_id")
        episode_text = (
            str(episode)
            if isinstance(episode, int) and not isinstance(episode, bool)
            else "unknown"
        )
        message = (
            f"{reason}\nObserved: {evaluated}\nEpisode: {episode_text}\n"
            "Advisory evidence only; inspect current conditions before acting."
        )
        return message[:MAX_MESSAGE_CHARS]

    def deliver(self, payload: Mapping[str, object]) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("notification payload must be a mapping")
        recovery = payload.get("notification_kind") == "recovery"
        if payload.get("advisory") is not True or (
            payload.get("state") != "warning" and not recovery
        ):
            raise ValueError("only explicit warning advisory payloads may be delivered")
        if recovery or _tier(payload) is not None:
            title, message = render_message(payload)
        else:
            title = self._text(payload.get("title"), "Van telemetry advisory")
            message = self._message(payload)
        environment = os.environ.copy()
        environment["NTFY_TIMEOUT"] = str(self.helper_network_timeout_seconds)
        completed = self.run(
            [
                str(self.executable),
                "--title",
                title[:MAX_TITLE_CHARS],
                "--priority",
                self._priority(payload),
                "--tags",
                self._tags(payload),
                self.topic,
                message,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            if len(detail) > 300:
                detail = detail[:300] + "…"
            raise RuntimeError(
                f"ntfy-send exited {completed.returncode}"
                + (f": {detail}" if detail else "")
            )


__all__ = [
    "DIGEST_HOUR",
    "DIGEST_MINUTE",
    "NtfyAdvisoryNotificationSink",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "QUIET_HOURS_ZONE",
    "local_zone",
    "quiet_hours_deferral",
    "render_message",
]
