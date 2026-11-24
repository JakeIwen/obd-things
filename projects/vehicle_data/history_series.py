"""``GET /v1/history/series?metric=<name>&span=<key>``: one sparkline metric over 1 h to 7 d.

The dashboard's tap-to-open chart draws its 15-minute span from the browser's own ring; longer
spans come from here as a downsampled, read-only history product. Spans, buckets and the cache
lifetime come from ``history_series.json`` (the dashboard reads it too) and the metric list is
``history_recent.METRICS``, so neither is written twice.

Unlike ``history_recent.py`` this queries on the request path: the broker's bounded GET workers
call ``SeriesService.respond``, and a memo keeps each (metric, span) product for
``cache_seconds``, so at most one rollup-backed query of at most 240 buckets runs per key per
minute. Concurrent requests wait on the memo lock and reuse the first product.

The query ends at the last whole minute, never later than now, for the reasons in
``history_recent``: ``at`` is then the true bucket start and the minute in progress is not frozen
before its samples arrive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Callable, Mapping
from urllib.parse import parse_qsl

from projects.vehicle_data import history_recent

VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
API_PATH = VOCABULARY["path"]
QUERY_KEYS = tuple(VOCABULARY["query_keys"])
CACHE_SECONDS = VOCABULARY["cache_seconds"]
SPANS = {span["key"]: span for span in VOCABULARY["spans"] if span["source"] == "history"}
LIVE_SPANS = frozenset(span["key"] for span in VOCABULARY["spans"] if span["source"] == "live")
# The historian's rollups are one minute wide; ending on that boundary keeps buckets aligned to them.
ALIGN_SECONDS = 60


class InvalidRequest(ValueError):
    """The query string is not one known metric and one served span."""


def parse_query(query_string: str) -> tuple[str, str]:
    """Return ``(metric, span_key)`` or raise ``InvalidRequest``; no other key is accepted."""

    try:
        pairs = parse_qsl(query_string, keep_blank_values=True, strict_parsing=bool(query_string))
    except ValueError as exc:
        raise InvalidRequest("malformed query string") from exc
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise InvalidRequest("a query key was repeated")
    if set(keys) != set(QUERY_KEYS):
        raise InvalidRequest("the query must be exactly " + " and ".join(QUERY_KEYS))
    values = dict(pairs)
    if values["metric"] not in history_recent.METRICS:
        raise InvalidRequest("unknown metric")
    span = values["span"]
    if span in LIVE_SPANS:
        raise InvalidRequest(f"span {span} is drawn from the browser's live ring, not served")
    if span not in SPANS:
        raise InvalidRequest("unknown span")
    return values["metric"], span


def query(historian, metric: str, span_key: str, now: datetime) -> dict[str, object]:
    """Run one bounded ``metric_series`` query and return the response product."""

    span = SPANS[span_key]
    seconds, bucket = span["seconds"], span["bucket_seconds"]
    end = history_recent.aligned_end(now, ALIGN_SECONDS)
    series = historian.metric_series(
        metric,
        end=end,
        window_seconds=seconds,
        bucket_seconds=bucket,
        max_points=seconds // bucket,
    )
    points = []
    for point in series.get("points", []):
        start = history_recent.parse_at(point.get("at"))
        if start is None or start >= end or not isinstance(point.get("value"), (int, float)):
            continue
        points.append({"at": point["at"], "value": point["value"],
                       "min": point.get("minimum"), "max": point.get("maximum")})
    return {
        "available": True,
        "metric": metric,
        "span": span_key,
        "window_seconds": seconds,
        "bucket_seconds": bucket,
        "start_at": (end - timedelta(seconds=seconds)).isoformat(),
        "end_at": end.isoformat(),
        "queried_at": now.isoformat(),
        "points": points,
    }


class SeriesService:
    """Validation plus the per-(metric, span) memo; one lock serialises queries and memo access."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._memo: dict[tuple[str, str], tuple[datetime, dict[str, object]]] = {}

    def respond(self, historian, query_string: str) -> tuple[int, Mapping[str, object]]:
        try:
            metric, span = parse_query(query_string)
        except InvalidRequest as exc:
            return 400, {"available": False, "reason": "invalid_request", "detail": str(exc)}
        if historian is None:
            return 200, {
                "available": False,
                "reason": "history_disabled",
                "detail": "telemetry history is disabled by broker configuration",
            }
        with self._lock:
            now = self._clock()
            held = self._memo.get((metric, span))
            if held is not None and timedelta(0) <= now - held[0] < timedelta(seconds=CACHE_SECONDS):
                return 200, held[1]
            try:
                product = query(historian, metric, span, now)
            except Exception as exc:  # a busy or broken database must not take the route down
                return 503, {"available": False, "reason": "history_unavailable", "detail": type(exc).__name__}
            self._memo[(metric, span)] = (now, product)
            return 200, product
