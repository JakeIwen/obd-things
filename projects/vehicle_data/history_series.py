"""``GET /v1/history/series?metric=<name>&span=<key>``: one metric over 1 h to 7 d.

The dashboard's tap-to-open chart draws its 15-minute span from the browser's own ring; longer
spans come from here as a downsampled, read-only history product. Spans, buckets and the refresh
cadence come from ``history_series.json`` (the dashboard reads it too). The accepted metrics are
``history_recent.METRICS`` plus the broker's own history trend metrics (``insights.history_metrics``,
the History view's trend cards and the Parked battery trend), handed in by the broker so no list is
written twice.

Like every broker GET the route is cache-only: ``SeriesService.refresh`` runs inside the broker's
60-second supplemental refresh and fills one product per (metric, span); ``respond`` only reads
memory under a short lock. A key is recomputed when its span's bucket has advanced since its last
product, so the 7-day span is queried about every 42 minutes, not every minute. A failed
recomputation leaves the previous product served for two of that span's buckets, then the route
answers ``history_unavailable``. Products are bounded at metrics x 4 spans.

The query ends at the last whole minute, never later than now, for the reasons in
``history_recent``: ``at`` is then the true bucket start and the minute in progress is not frozen
before its samples arrive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qsl

from projects.vehicle_data import history_recent

VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
API_PATH = VOCABULARY["path"]
QUERY_KEYS = tuple(VOCABULARY["query_keys"])
REFRESH_SECONDS = VOCABULARY["refresh_seconds"]
SPANS = {span["key"]: span for span in VOCABULARY["spans"] if span["source"] == "history"}
LIVE_SPANS = frozenset(span["key"] for span in VOCABULARY["spans"] if span["source"] == "live")
# The historian's rollups are one minute wide; ending on that boundary keeps buckets aligned to them.
ALIGN_SECONDS = 60


class InvalidRequest(ValueError):
    """The query string is not one known metric and one served span."""


def accepted_metrics(history_metrics=()) -> tuple[str, ...]:
    """The route's metric allowlist: the sparkline metrics plus the broker's trend metrics."""

    return tuple(dict.fromkeys((*history_recent.METRICS, *history_metrics)))


def parse_query(query_string: str, metrics=history_recent.METRICS) -> tuple[str, str]:
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
    if values["metric"] not in metrics:
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
    """Validation, the per-(metric, span) product cache and its due-time bookkeeping."""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        metrics: Callable[[], Sequence[str]] = lambda: history_recent.METRICS,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._metrics = metrics
        self._lock = threading.Lock()
        self._products: dict[tuple[str, str], dict[str, object]] = {}
        self._computed_at: dict[tuple[str, str], datetime] = {}
        self._failed: set[tuple[str, str]] = set()

    def refresh(self, historian) -> dict[str, float]:
        """Recompute every due product off the request path; return per-span seconds spent.

        Each key is queried outside the lock and swapped in under it, so a slow query never
        delays a GET. A failure is recorded per key and retried on the next pass.
        """

        now = self._clock()
        metrics = tuple(self._metrics())
        wanted = {(metric, span) for metric in metrics for span in SPANS}
        spent = {span: 0.0 for span in SPANS}
        with self._lock:
            for key in set(self._products) - wanted:  # a metric left the allowlist
                self._products.pop(key, None)
                self._computed_at.pop(key, None)
                self._failed.discard(key)
            due = [key for key in sorted(wanted) if self._is_due(key, now)]
        for key in due:
            started = time.monotonic()
            try:
                product = query(historian, key[0], key[1], now)
            except Exception:  # a busy or broken database must not take the refresh down
                with self._lock:
                    self._failed.add(key)
            else:
                with self._lock:
                    self._products[key] = product
                    self._computed_at[key] = now
                    self._failed.discard(key)
            spent[key[1]] += time.monotonic() - started
        return spent

    def _is_due(self, key: tuple[str, str], now: datetime) -> bool:
        computed = self._computed_at.get(key)
        if computed is None:
            return True
        bucket = SPANS[key[1]]["bucket_seconds"]
        return history_recent.aligned_end(now, bucket) > history_recent.aligned_end(computed, bucket)

    def respond(self, enabled: bool, query_string: str) -> tuple[int, Mapping[str, object]]:
        """``(status, payload)`` from memory; ``enabled`` is whether a historian exists."""

        try:
            metric, span = parse_query(query_string, tuple(self._metrics()))
        except InvalidRequest as exc:
            return 400, {"available": False, "reason": "invalid_request", "detail": str(exc)}
        if not enabled:
            return 200, {
                "available": False,
                "reason": "history_disabled",
                "detail": "telemetry history is disabled by broker configuration",
            }
        key = (metric, span)
        now = self._clock()
        with self._lock:
            product = self._products.get(key)
            computed = self._computed_at.get(key)
            failed = key in self._failed
        stale_after = timedelta(seconds=2 * SPANS[span]["bucket_seconds"])
        if product is not None and not (failed and now - computed >= stale_after):
            return 200, product
        if failed:
            return 503, {"available": False, "reason": "history_unavailable",
                         "detail": "the last refresh of this series failed"}
        return 200, {"available": False, "reason": "pending",
                     "detail": "the background refresh has not produced this series yet"}
