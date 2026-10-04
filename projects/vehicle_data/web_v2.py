#!/usr/bin/env python3
"""Telemetry dashboard listener (port 8765).

This is the only dashboard web server; both systemd web units run this file.
The filename keeps its historical ``_v2`` suffix because the installed units
execute it by path. It has the same broker proxy, POST gates, origin checks and
server-sent-event contract as ``web.py``, which it subclasses for the ``/v1``
API, and adds:

- the built frontend from ``dashboard/dist``, with MIME by extension, gzip for
  compressible bodies, immutable caching for content-hashed assets and ETag
  revalidation for everything else;
- ``GET /v2/stream``: the ``/v1/stream`` loop with a reduced ``status`` and a
  catalog hash instead of the 16 KB catalog, for a slow tablet;
- ``GET /v2/summary``: one trimmed, compressible bundle of the broker's cached
  history, health, DTC, maintenance and status products. Warning rows
  (``health.assessments``, ``health.active``, ``health.episodes.active``) are
  never capped; other capped lists report ``<key>_omitted_count`` beside the
  list;
- ``GET /v1/snapshot`` on this listener also carries ``catalog_hash`` (the same
  hash as the lite stream), and every ``web`` flags object here adds ``bind``
  and the dashboard ``build`` id from ``<static_root>/build.json``;
- each client IP's User-Agent is logged once (bounded memory), to record which
  browser the tablet runs.

GETs remain cache-only and never touch CAN. Every ``/v1`` route of ``web.py``
stays available; the MacBook-managed Van Dashboard reads ``/v1/snapshot``.
"""

from __future__ import annotations

import collections
import gzip
import hashlib
import json
import mimetypes
import pathlib
import re
import sys
import threading
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from projects.vehicle_data import web as base  # noqa: E402
from projects.vehicle_data.http_common import broker_unavailable, route_name, stream_snapshots, web_flags

DEFAULT_STATIC_ROOT = pathlib.Path(__file__).with_name("dashboard") / "dist"
DEFAULT_PORT = 8765

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
COMPRESSIBLE_PREFIXES = (
    "text/",
    "application/json",
    "application/javascript",
    "image/svg+xml",
    "application/manifest+json",
)
GZIP_MIN_BYTES = 1024
# Client IPs whose User-Agent has been logged (critique M2: record the tablet's
# browser once instead of on every request).
USER_AGENT_MEMORY = 64
USER_AGENT_MAX_CHARS = 400
HASHED_ASSET = re.compile(
    r"^/assets/[A-Za-z0-9_.-]+\.[0-9A-Za-z]{8,}\.(?:js|mjs|css|map|svg|woff2|png)$"
)

# Top-level ``status`` keys forwarded unchanged on the lite stream. Everything
# not listed (adapter inventories, USB event lists, cached_metrics, reconcile
# dumps) is available from ``/v2/summary.status_full`` once a minute instead.
STATUS_LITE_KEYS = (
    "service",
    "started_at",
    "vehicle_state",
    "collector",
    "active_drive",
    "auxiliary_drive",
    "engine_off_voltage",
    "radar_alignment",
    "radar_alignment_polling",
    "last_readings",
    "supplemental_cache",
    "current_owner",
    "interface_probe",
    "history_recorder",
    "inflight",
    "last_acquisition_errors",
    "active_acquisition_permitted",
)
ROLE_LITE_KEYS = ("channel", "resolution", "reason", "safe", "passive_ready")
ROLE_INTERFACES_LITE_KEYS = (
    "ready",
    "passive_ready",
    "resolved",
    "vehicle_buses_ready",
    "mode",
    "issues",
    "generation",
)
SUMMARY_MAX_LIST = 16
EPISODE_RECENT_LIMIT = 12
SUMMARY_MAX_DEPTH = 6
SUMMARY_MAX_STRING = 800
SUMMARY_DROP_KEYS = frozenset(
    (
        "first_assessment_json",
        "latest_assessment_json",
        "input_buckets",
        "inputs",
        "samples",
        "sample_window",
        "observations",
        "recent_events",
        "baseline_archives",
    )
)
EPISODE_HEADLINE_KEYS = (
    "id",
    "rule",
    "title",
    "category",
    "advisory",
    "status",
    "state",
    "evidence_state",
    "outcome",
    "opened_at",
    "resolved_at",
    "resolution_reason",
    "last_observed_at",
    "duration_seconds",
    "observation_count",
    "acknowledged",
)
DTC_RECORD_LITE_KEYS = (
    "raw_dtc",
    "fca_display",
    "module_key",
    "module_name",
    "logical_bus",
    "status",
    "display_group",
    "description",
    "description_reviewed",
    "description_source",
    "first_seen_at",
    "last_seen_at",
    "observation_state",
    "present",
    "current",
    "pending",
    "confirmed",
    "incomplete_only",
    "warning_indicator_requested",
)
DTC_FULL_GROUPS = ("current", "pending")


def status_lite(status: Any) -> Any:
    """Reduce the broker status to what the live views read every second."""

    if not isinstance(status, dict):
        return status
    lite: dict[str, Any] = {}
    for key in STATUS_LITE_KEYS:
        if key in status:
            lite[key] = status[key]
    interface = status.get("interface")
    if isinstance(interface, dict):
        lite_interface: dict[str, Any] = {}
        for key in ("mode", "active_inhibits", "adapter_present", "up"):
            if key in interface:
                lite_interface[key] = interface[key]
        roles = interface.get("role_interfaces")
        if isinstance(roles, dict):
            lite_roles: dict[str, Any] = {
                key: roles[key] for key in ROLE_INTERFACES_LITE_KEYS if key in roles
            }
            role_map = roles.get("roles")
            if isinstance(role_map, dict):
                lite_roles["roles"] = {
                    name: {
                        key: role[key] for key in ROLE_LITE_KEYS if key in role
                    }
                    if isinstance(role, dict)
                    else role
                    for name, role in role_map.items()
                }
            lite_interface["role_interfaces"] = lite_roles
        lite["interface"] = lite_interface
    for key in ("data_quality", "usb_can_monitor"):
        section = status.get(key)
        if isinstance(section, dict):
            lite[key] = {
                sub: section[sub]
                for sub in ("state", "active_count", "enabled")
                if sub in section
            }
    if "web" in status:
        lite["web"] = status["web"]
    return lite


def catalog_hash(catalog: Any) -> str | None:
    if not isinstance(catalog, list):
        return None
    canonical = json.dumps(catalog, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def lite_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the ``/v2/stream`` event body from a full broker snapshot."""

    lite: dict[str, Any] = {
        "status": status_lite(payload.get("status")),
        "metrics": payload.get("metrics"),
        "catalog_hash": catalog_hash(payload.get("catalog")),
    }
    catalog = payload.get("catalog")
    lite["catalog_count"] = len(catalog) if isinstance(catalog, list) else None
    return lite


def omitted_count_key(key: str) -> str:
    """Name of the sibling field that counts items cut from a capped list."""

    return f"{key}_omitted_count"


def shrink(value: Any, depth: int = 0) -> Any:
    """Bounded copy: drop evidence arrays, cap list lengths, depth and strings.

    A list longer than ``SUMMARY_MAX_LIST`` keeps its first items only. When the
    list is a dict value, the number cut is reported **beside** it as
    ``<key>_omitted_count`` (never as a sentinel element inside the list, which
    a client would render or silently drop). Lists that must never be capped
    (warning rows) go through :func:`shrink_items` instead.
    """

    if depth > SUMMARY_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in SUMMARY_DROP_KEYS:
                out[f"{key}_omitted"] = True
                continue
            out[key] = shrink(item, depth + 1)
            if isinstance(item, list) and len(item) > SUMMARY_MAX_LIST:
                out[omitted_count_key(key)] = len(item) - SUMMARY_MAX_LIST
        return out
    if isinstance(value, list):
        return [shrink(item, depth + 1) for item in value[:SUMMARY_MAX_LIST]]
    if isinstance(value, str) and len(value) > SUMMARY_MAX_STRING:
        return value[:SUMMARY_MAX_STRING] + "…"
    return value


def shrink_items(value: Any) -> Any:
    """Uncapped list: every element is kept, each element is bounded by :func:`shrink`.

    Used for ``health.assessments``, ``health.active`` and
    ``health.episodes.active``: each owner-added early warning adds a row, and a
    dropped row would hide a live warning.
    """

    if not isinstance(value, list):
        return shrink(value)
    return [shrink(item, 1) for item in value]


def dtc_lite(dtcs: Any) -> Any:
    if not isinstance(dtcs, dict) or not dtcs.get("available"):
        return dtcs
    lite = {
        key: dtcs[key]
        for key in (
            "available",
            "acquisition",
            "compact",
            "schema_version",
            "generated_at",
            "detail",
            "coverage",
            "group_counts",
            "group_returned_counts",
            "groups_truncated",
            "per_group_limit",
            "modules",
            "description_catalog",
            "broker_cache",
            # The van's own health-check results (passive, additive; see van_scan_harvest.py).
            "in_vehicle_scan",
        )
        if key in dtcs
    }
    groups = dtcs.get("groups")
    if isinstance(groups, dict):
        lite_groups: dict[str, Any] = {}
        for name, records in groups.items():
            if not isinstance(records, list):
                lite_groups[name] = records
            elif name in DTC_FULL_GROUPS:
                lite_groups[name] = records
            else:
                lite_groups[name] = [
                    {key: record[key] for key in DTC_RECORD_LITE_KEYS if key in record}
                    if isinstance(record, dict)
                    else record
                    for record in records
                ]
        lite["groups"] = lite_groups
    return lite


def health_lite(health: Any) -> Any:
    if not isinstance(health, dict) or not health.get("available"):
        return health
    lite = {
        key: health[key]
        for key in (
            "available",
            "schema_version",
            "generated_at",
            "detail",
            "evidence_scope",
            "evaluation_hook",
            "notification_delivery",
            "custom_rules",
            "overview_limits",
            "event_detail_template",
            "broker_cache",
        )
        if key in health
    }
    # Warning rows are never capped (critique M6): each element is bounded, the
    # list is not.
    lite["assessments"] = shrink_items(health.get("assessments", []))
    lite["active"] = shrink_items(health.get("active", []))
    episodes = health.get("episodes")
    if isinstance(episodes, dict):
        recent = episodes.get("recent", [])
        recent = recent if isinstance(recent, list) else []
        lite["episodes"] = {
            "generated_at": episodes.get("generated_at"),
            "schema_version": episodes.get("schema_version"),
            "notification_outbox": shrink(episodes.get("notification_outbox")),
            "active": shrink_items(episodes.get("active", [])),
            # Closed episodes only need their headline on the cards; the event
            # dialog reads full evidence from /v1/events on demand.
            "recent": [
                {
                    key: episode[key]
                    for key in EPISODE_HEADLINE_KEYS
                    if key in episode
                }
                if isinstance(episode, dict)
                else episode
                for episode in recent[:EPISODE_RECENT_LIMIT]
            ],
            "recent_total": len(recent),
        }
        if len(recent) > EPISODE_RECENT_LIMIT:
            lite["episodes"][omitted_count_key("recent")] = (
                len(recent) - EPISODE_RECENT_LIMIT
            )
    for key in ("data_quality", "usb_can_incidents"):
        section = health.get(key)
        if isinstance(section, dict):
            lite[key] = shrink(
                {
                    sub: section[sub]
                    for sub in (
                        "active",
                        "active_count",
                        "counts",
                        "incident_count",
                        "event_count",
                        "recent",
                        "recent_incidents",
                        "detail",
                        "notification_delivery",
                        "generated_at",
                    )
                    if sub in section
                }
            )
    return lite


def build_summary(fetch, web_status: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Assemble ``/v2/summary`` from cache-only broker GETs.

    ``fetch(path)`` returns ``(status_code, payload)`` or raises. A failing
    section is reported inline; only an unreachable status endpoint makes the
    whole response 503, matching ``web.py`` semantics for ``/v1/status``.
    """

    summary: dict[str, Any] = {
        "available": True,
        "schema_version": 1,
        "generated_at_ms": time.time_ns() // 1_000_000,
        "web": web_status,
    }
    try:
        status_code, status = fetch("/v1/status")
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        return 503, broker_unavailable(exc)
    summary["status_full"] = status if status_code == 200 else {
        "available": False,
        "reason": "status_unavailable",
        "status_code": status_code,
    }
    if isinstance(summary["status_full"], dict):
        summary["status_full"]["web"] = web_status
    sections = (
        ("history", "/v1/history", lambda payload: payload),
        ("health", "/v1/health", health_lite),
        ("dtcs", "/v1/diagnostics/dtcs", dtc_lite),
        ("maintenance", "/v1/maintenance", lambda payload: payload),
    )
    for key, path, transform in sections:
        try:
            status_code, payload = fetch(path)
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            summary[key] = broker_unavailable(exc)
            continue
        if not isinstance(payload, dict):
            summary[key] = {"available": False, "reason": "malformed_response"}
            continue
        payload.setdefault("status_code", status_code)
        summary[key] = transform(payload)
    return 200, summary


class DashboardHandler(base.TelemetryWebHandler):
    server_version = "VanTelemetryWebV2/1"

    # -- helpers ---------------------------------------------------------

    def _accepts_gzip(self) -> bool:
        accept = self.headers.get("Accept-Encoding", "")
        return any(token.strip().split(";")[0] == "gzip" for token in accept.split(","))

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        etag: str | None = None,
        head_only: bool = False,
    ) -> None:
        encoded = body
        encoding = None
        if (
            len(body) >= GZIP_MIN_BYTES
            and content_type.startswith(COMPRESSIBLE_PREFIXES)
            and self._accepts_gzip()
        ):
            encoded = self.server.gzip_cached(body)
            encoding = "gzip"
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", cache_control)
            if etag:
                self.send_header("ETag", etag)
            self._security_headers()
            self.end_headers()
            if not head_only:
                self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self._send_body(status, body, "application/json")

    def _web_status(self) -> dict[str, Any]:
        """Listener flags for ``/v2/*`` and this listener's ``/v1/snapshot``.

        ``web.py``'s fields (``bind`` in its ``addr:port`` form) plus the
        dashboard ``build`` id. Keys are inserted in sorted order so the stream
        (``json.dumps`` without ``sort_keys``) and the HTTP baseline
        (``sort_keys=True``) serialise the object identically and the client's
        string comparison does not see a change on every resync.
        """

        return web_flags(self.server, include_build=True)

    def _note_user_agent(self) -> None:
        self.server.note_user_agent(
            self.client_address[0], self.headers.get("User-Agent")
        )

    # -- static ----------------------------------------------------------

    def _serve_static(self, path: str, *, head_only: bool = False) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.endswith("/"):
            rel += "index.html"
        root = self.server.static_root
        try:
            target = (root / rel).resolve()
        except (OSError, RuntimeError):
            return self._json(404, {"available": False, "reason": "not_found", "detail": path})
        if not target.is_relative_to(root) or not target.is_file():
            return self._json(404, {"available": False, "reason": "not_found", "detail": path})
        try:
            body, etag = self.server.static_bytes(target)
        except OSError:
            return self._json(
                500,
                {"available": False, "reason": "static_asset_unavailable", "detail": rel},
            )
        if self.headers.get("If-None-Match") == etag:
            try:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self._security_headers()
                self.end_headers()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return
        content_type = (
            MIME_TYPES.get(target.suffix.lower())
            or mimetypes.guess_type(target.name)[0]
            or "application/octet-stream"
        )
        cache_control = (
            "public, max-age=31536000, immutable"
            if HASHED_ASSET.match(path)
            else "no-cache"
        )
        self._send_body(
            200,
            body,
            content_type,
            cache_control=cache_control,
            etag=etag,
            head_only=head_only,
        )

    # -- routes ----------------------------------------------------------

    DASHBOARD_GET_ROUTES = (
        (r"/v2/stream", "_lite_stream_get"),
        (r"/v2/summary", "_summary_get"),
        (r"/v1/snapshot", "_snapshot_get"),
        (r"/v[12]/.*", "_base_get"),
    )

    def do_GET(self):
        self._note_user_agent()
        path = self.path.split("?", 1)[0]
        name = route_name(path, self.DASHBOARD_GET_ROUTES)
        if name is None:
            return self._serve_static(path)
        return getattr(self, name)(path)

    def _lite_stream_get(self, path: str) -> None:
        return self._stream_lite()

    def _summary_get(self, path: str) -> None:
        return self._summary()

    def _snapshot_get(self, path: str) -> None:
        return self._snapshot()

    def _base_get(self, path: str) -> None:
        return super().do_GET()

    def do_HEAD(self):
        self._note_user_agent()
        path = self.path.split("?", 1)[0]
        if path.startswith("/v1/") or path.startswith("/v2/"):
            return self._json(405, {"available": False, "reason": "method_not_allowed"})
        return self._serve_static(path, head_only=True)

    def do_POST(self):
        self._note_user_agent()
        return super().do_POST()

    def _snapshot(self) -> None:
        """``/v1/snapshot`` as ``web.py`` serves it, plus ``catalog_hash``.

        The client's HTTP baseline then carries the same hash as the lite
        stream (freshness rule 19), so the first stream event can be compared
        instead of adopted blindly.
        """

        try:
            status, response = self.client.request("GET", "/v1/snapshot")
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            return self._json(503, broker_unavailable(exc))
        if not isinstance(response, dict):
            return self._json(502, {"available": False, "reason": "malformed_response"})
        web_status = self._web_status()
        response["web"] = web_status
        response["web_delivery"] = self.server.next_snapshot_delivery()
        if isinstance(response.get("status"), dict):
            response["status"]["web"] = web_status
        digest = catalog_hash(response.get("catalog"))
        if digest is not None:
            response["catalog_hash"] = digest
        return self._json(status, response)

    def _summary(self) -> None:
        status, payload = build_summary(
            lambda path: self.client.request("GET", path),
            self._web_status(),
        )
        return self._json(status, payload)

    def _stream_lite(self) -> None:
        return stream_snapshots(self, self._stream_lite_event)

    def _stream_lite_event(self, status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
        web_status = self._web_status()
        event = lite_snapshot(payload if isinstance(payload, dict) else {})
        event["status_code"] = status_code
        event["web"] = web_status
        delivery = self.server.next_snapshot_delivery()
        event["web_delivery"] = delivery
        if isinstance(event.get("status"), dict):
            event["status"]["web"] = web_status
        return event


class DashboardServer(base.TelemetryWebServer):
    def __init__(self, address, *, static_root: pathlib.Path | str, **kwargs):
        super().__init__(address, **kwargs)
        self.static_root = pathlib.Path(static_root).resolve()
        self.RequestHandlerClass = DashboardHandler
        self._static_lock = threading.Lock()
        self._static_cache: dict[pathlib.Path, tuple[float, int, bytes, str]] = {}
        self._gzip_cache: dict[bytes, bytes] = {}
        self._build_lock = threading.Lock()
        self._build_key: tuple[int, int] | None = None
        self._build_id: str | None = None
        self._agents_lock = threading.Lock()
        self._agents_seen: collections.OrderedDict[str, None] = collections.OrderedDict()

    def build_id(self) -> str | None:
        """The dashboard build id from ``<static_root>/build.json`` (field ``build``).

        Read once per build: the file is parsed only when its mtime or size
        changes, so a rebuild in place is reported without a restart. A missing
        or malformed file yields ``None``.
        """

        path = self.static_root / "build.json"
        try:
            stat = path.stat()
        except OSError:
            with self._build_lock:
                self._build_key, self._build_id = None, None
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        with self._build_lock:
            if key == self._build_key:
                return self._build_id
        try:
            build = json.loads(path.read_text(encoding="utf-8")).get("build")
        except (OSError, ValueError, AttributeError):
            build = None
        build = build if isinstance(build, str) and build else None
        with self._build_lock:
            self._build_key, self._build_id = key, build
        return build

    def note_user_agent(self, client_ip: str, user_agent: str | None) -> bool:
        """Log a client's User-Agent the first time its IP is seen.

        The set of remembered IPs is bounded (``USER_AGENT_MEMORY``, least
        recently seen evicted). Returns whether a line was logged.
        """

        with self._agents_lock:
            if client_ip in self._agents_seen:
                self._agents_seen.move_to_end(client_ip)
                return False
            self._agents_seen[client_ip] = None
            while len(self._agents_seen) > USER_AGENT_MEMORY:
                self._agents_seen.popitem(last=False)
        agent = (user_agent or "")[:USER_AGENT_MAX_CHARS]
        print(f"{client_ip} user-agent {json.dumps(agent)}", flush=True)
        return True

    def static_bytes(self, target: pathlib.Path) -> tuple[bytes, str]:
        stat = target.stat()
        with self._static_lock:
            cached = self._static_cache.get(target)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                return cached[2], cached[3]
        body = target.read_bytes()
        etag = f'"{hashlib.sha256(body).hexdigest()[:20]}"'
        with self._static_lock:
            if len(self._static_cache) > 256:
                self._static_cache.clear()
            self._static_cache[target] = (stat.st_mtime, stat.st_size, body, etag)
        return body, etag

    def gzip_cached(self, body: bytes) -> bytes:
        key = hashlib.sha256(body).digest()
        with self._static_lock:
            cached = self._gzip_cache.get(key)
        if cached is not None:
            return cached
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        with self._static_lock:
            if len(self._gzip_cache) > 64:
                self._gzip_cache.clear()
            self._gzip_cache[key] = compressed
        return compressed


def build_parser():
    parser = base.build_parser()
    parser.description = "Telemetry dashboard listener (port 8765 by default)"
    parser.add_argument(
        "--static-root",
        default=str(DEFAULT_STATIC_ROOT),
        help="directory holding the built dashboard (index.html, assets/, docs/)",
    )
    parser.set_defaults(port=DEFAULT_PORT)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    base.validate_bind(args.bind, allow_remote_bind=args.allow_remote_bind)
    if not 0 < args.port < 65536:
        raise SystemExit("--port must be between 1 and 65535")
    if args.stream_interval <= 0 or args.stream_max_seconds <= 0:
        raise SystemExit("stream intervals must be positive")
    static_root = pathlib.Path(args.static_root).resolve()
    if not (static_root / "index.html").is_file():
        raise SystemExit(
            f"--static-root {static_root} has no index.html; run "
            "'npm run build' in projects/vehicle_data/dashboard first"
        )
    dtc_controller = None
    dtc_origin = None
    if args.enable_dtc_jobs:
        base.validate_dtc_job_bind(args.bind)
        dtc_origin = base.validate_dtc_origin(
            args.dtc_trusted_origin, bind=args.bind, port=args.port
        )
        dtc_controller = base.DtcWebController(
            request_path=args.dtc_request_file,
            current_path=args.dtc_current_file,
            cancel_dir=args.dtc_cancel_dir,
            job_root=args.dtc_job_root,
        )
    elif args.dtc_trusted_origin:
        raise SystemExit("--dtc-trusted-origin requires --enable-dtc-jobs")
    server = DashboardServer(
        (args.bind, args.port),
        static_root=static_root,
        socket_path=args.socket,
        allow_acquisitions=args.allow_acquisitions,
        stream_interval_seconds=args.stream_interval,
        stream_max_seconds=args.stream_max_seconds,
        dtc_controller=dtc_controller,
        dtc_trusted_origin=dtc_origin,
        warning_chat_socket=None if args.no_warning_chat else args.warning_chat_socket,
        warning_chat_origins=args.warning_chat_origin,
    )
    print(
        f"dashboard v2 listening on {args.bind}:{args.port} "
        f"static={static_root} broker={args.socket}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
