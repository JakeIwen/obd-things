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
product, so the 7-day span is queried about every 42 minutes, not every minute. A product
is served only while its build time is within the greater of two span buckets and three refresh
periods (and not in the future), whether or not a refresh failed; after that the route answers
``history_unavailable``. A pass stops at its first failed
query and handles at most ``MAX_KEYS_PER_PASS`` keys, so a busy database costs the recorder one
timeout, not one per key. Products are bounded at metrics x 4 spans.

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
# Why a cap: the pass runs on the recorder thread, and a busy database holds each query for up to
# SQLite's 10 s timeout. The steady state needs about 22 keys a pass (13 metrics x (1 + 1/2 + 1/6 +
# 1/42)), so 32 leaves headroom while the every-42-minute all-due pass (52 keys) spreads over two.
MAX_KEYS_PER_PASS = VOCABULARY["max_keys_per_pass"]
SPANS = {span["key"]: span for span in VOCABULARY["spans"] if span["source"] == "history"}
LIVE_SPANS = frozenset(span["key"] for span in VOCABULARY["spans"] if span["source"] == "live")
# The historian's rollups are one minute wide; ending on that boundary keeps buckets aligned to them.
ALIGN_SECONDS = 60
_NEVER = datetime.min.replace(tzinfo=timezone.utc)


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
        # Whether the last pass hit a failing query. One flag, not one per key: the pass stops at
        # its first failure, so keys it never reached are just as unavailable as the one that failed.
        self._last_pass_failed = False

    def refresh(self, historian) -> dict[str, float]:
        """Recompute due products off the request path; return per-span seconds spent.

        Each key is queried outside the lock and swapped in under it, so a slow query never
        delays a GET. The pass stops at the first failed query (the rest stay due for the next
        pass) and handles at most ``MAX_KEYS_PER_PASS`` keys, finest span first and oldest first
        within a span: short spans go stale soonest, and a deferred long span is harmless.
        """

        now = self._clock()
        metrics = tuple(self._metrics())
        wanted = {(metric, span) for metric in metrics for span in SPANS}
        spent = {span: 0.0 for span in SPANS}
        with self._lock:
            for key in set(self._products) - wanted:  # a metric left the allowlist
                self._products.pop(key, None)
                self._computed_at.pop(key, None)
            due = [key for key in wanted if self._is_due(key, now)]
            due.sort(key=lambda key: (SPANS[key[1]]["bucket_seconds"], self._computed_at.get(key, _NEVER), key))
        for key in due[:MAX_KEYS_PER_PASS]:
            started = time.monotonic()
            try:
                product = query(historian, key[0], key[1], now)
            except Exception:  # a busy or broken database must not take the refresh down
                with self._lock:
                    self._last_pass_failed = True
                spent[key[1]] += time.monotonic() - started
                return spent  # every later key would wait out the same busy timeout
            with self._lock:
                self._products[key] = product
                self._computed_at[key] = now
            spent[key[1]] += time.monotonic() - started
        with self._lock:
            self._last_pass_failed = False
        return spent

    def _is_due(self, key: tuple[str, str], now: datetime) -> bool:
        computed = self._computed_at.get(key)
        if computed is None or computed > now:  # never built, or the wall clock stepped back
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
            failed = self._last_pass_failed
        # Two buckets, but never under three refresh periods: the 1 h span's bucket equals the
        # refresh period, so one late or failed pass must not flip a healthy chart to unavailable.
        stale_after = timedelta(seconds=max(2 * SPANS[span]["bucket_seconds"], 3 * REFRESH_SECONDS))
        if product is not None:
            # Always enforced: a producer that stalled or died fails nothing, so age alone must
            # end service. A product dated ahead of now (the clock stepped back) is not trusted.
            if timedelta(0) <= now - computed < stale_after:
                return 200, product
            return 503, {"available": False, "reason": "history_unavailable",
                         "detail": "this series is out of date; the background refresh is not keeping up"}
        if failed:
            return 503, {"available": False, "reason": "history_unavailable",
                         "detail": "the last refresh of this series failed"}
        return 200, {"available": False, "reason": "pending",
                     "detail": "the background refresh has not produced this series yet"}
