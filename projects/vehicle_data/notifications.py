"""Explicit advisory notification sinks for the telemetry historian.

This module has no CAN access.  The ntfy sink invokes the host's existing
queue-aware ``ntfy-send`` helper with a fixed argv (never a shell), so an
offline notification server results in a durable local queue rather than a
lost advisory.
"""

from __future__ import annotations

import re
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


NTFY_SEND = Path("/usr/local/bin/ntfy-send")
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_MESSAGE_CHARS = 1500


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
        assessment = payload.get("assessment")
        assessment = assessment if isinstance(assessment, Mapping) else {}
        severity = assessment.get("severity")
        if severity == "critical":
            return "max"
        if severity == "warning" or payload.get("state") == "warning":
            return "high"
        return "default"

    @staticmethod
    def _tags(payload: Mapping[str, object]) -> str:
        category = payload.get("category")
        return (
            "warning,computer"
            if category == "can_infrastructure"
            else "warning,car"
        )

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
        if payload.get("advisory") is not True or payload.get("state") != "warning":
            raise ValueError("only explicit warning advisory payloads may be delivered")
        title = self._text(payload.get("title"), "Van telemetry advisory")
        environment = os.environ.copy()
        environment["NTFY_TIMEOUT"] = str(self.helper_network_timeout_seconds)
        completed = self.run(
            [
                str(self.executable),
                "--title",
                title[:120],
                "--priority",
                self._priority(payload),
                "--tags",
                self._tags(payload),
                self.topic,
                self._message(payload),
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


__all__ = ["NtfyAdvisoryNotificationSink"]
