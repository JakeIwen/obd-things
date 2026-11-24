"""The last 15 minutes of the Drive sparkline metrics, one mean per minute, for page-load seeding.

``GET /v1/history/recent`` takes no parameters. The dashboard's client-side rings start empty
after a page load; this fixed, bounded product lets it fill them at once. The metric list, window
and bucket come from ``history_recent.json``, which the dashboard reads too, so the two cannot
drift. The broker queries on its existing 60-second supplemental refresh and serves the result
from memory (``fresh_response``), so no request touches SQLite.

``at`` is the start of a wall-clock minute and ``value`` the mean of that minute's samples. The
query ends at the last whole minute boundary, so ``metric_series``'s query-relative buckets
coincide with the historian's minute rollups and its bucket-start label is the true minute start
(an unaligned end would label a rollup up to 59 s early). The minute in progress is therefore not
included, and a minute with no samples is simply absent. The end is never moved past ``now``:
``metric_series`` rolls up every minute before its ``end``, so a later end would freeze the
minute in progress before its samples had arrived.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Mapping

VOCABULARY = json.loads(Path(__file__).with_suffix(".json").read_text())
API_PATH = VOCABULARY["path"]
WINDOW_SECONDS = VOCABULARY["window_seconds"]
BUCKET_SECONDS = VOCABULARY["bucket_seconds"]
AT_BASIS = VOCABULARY["at_basis"]
METRICS = tuple(VOCABULARY["metrics"])
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def query(historian, now: datetime | None = None) -> dict[str, object]:
    """Run the bounded series queries (one per metric) and return the cacheable product."""

    now = now or datetime.now(timezone.utc)
    end = aligned_end(now, BUCKET_SECONDS)
    metrics: dict[str, list[dict[str, object]]] = {}
    for name in METRICS:
        series = historian.metric_series(
            name,
            end=end,
            window_seconds=WINDOW_SECONDS,
            bucket_seconds=BUCKET_SECONDS,
            max_points=WINDOW_SECONDS // BUCKET_SECONDS,
        )
        metrics[name] = [
            {"at": point["at"], "value": point["value"]}
            for point in series.get("points", [])
            if isinstance(point.get("value"), (int, float)) and _is_complete_minute(point["at"], end)
        ]
    return {"queried_at": now.isoformat(), "metrics": metrics}


def aligned_end(now: datetime, bucket_seconds: int) -> datetime:
    """``now`` floored to a whole ``bucket_seconds`` boundary (never later than ``now``); shared
    with ``history_series.py`` so both routes label buckets the same way."""

    bucket_us = bucket_seconds * 1_000_000
    now_us = (now - _EPOCH) // timedelta(microseconds=1)
    return _EPOCH + timedelta(microseconds=now_us - now_us % bucket_us)


def _is_complete_minute(at: object, end: datetime) -> bool:
    """A point labelled at or after ``end`` is raw samples exactly on the boundary: a partial minute."""

    start = parse_at(at)
    return start is not None and start < end


def parse_at(at: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def fresh_response(cached: Mapping[str, object] | None, now: datetime | None = None) -> dict[str, object]:
    """The reply for ``cached`` (``query``'s product) as of ``now``, dropping buckets that ended
    before the window opened, so a van parked for an hour answers with empty lists."""

    now = now or datetime.now(timezone.utc)
    if cached is None:
        return {
            "available": False,
            "reason": "history_recent_cache_starting",
            "detail": "the background supplemental cache has not completed its first refresh",
        }
    oldest = now - timedelta(seconds=WINDOW_SECONDS)
    bucket = timedelta(seconds=BUCKET_SECONDS)
    metrics: dict[str, list[dict[str, object]]] = {}
    for name in METRICS:
        points = []
        for point in cached["metrics"].get(name, []):
            start = parse_at(point["at"])
            if start is not None and start + bucket > oldest:
                points.append(dict(point))
        metrics[name] = points
    return {
        "available": True,
        "generated_at": now.isoformat(),
        "window_seconds": WINDOW_SECONDS,
        "bucket_seconds": BUCKET_SECONDS,
        "at_basis": AT_BASIS,
        "metrics": metrics,
    }
