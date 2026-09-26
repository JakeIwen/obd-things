"""Dashboard v2 listener (``web_v2.py``) contract tests.

A canned Unix-socket broker stands in for the real one so the tests cover the
listener's own behaviour: static serving from the built dashboard root, gzip
and cache headers, the lite stream, the trimmed summary bundle, and the fact
that every ``/v1`` proxy route of ``web.py`` still works unchanged.
"""

import collections
import contextlib
import gzip
import http.client
import http.server
import io
import json
import os
import pathlib
import re
import shlex
import socketserver
import tempfile
import threading
import types
import unittest
import unittest.mock

from projects.vehicle_data import web_v2
from projects.vehicle_data.web_v2 import DashboardServer


CATALOG = [
    {
        "name": "engine.rpm",
        "unit": "rpm",
        "value_type": "number",
        "stale_after_seconds": 5.0,
        "minimum": 0,
        "maximum": 9000,
        "allowed_acquisition_modes": [],
        "sources": [
            {
                "name": "ccan.broadcast.0x0fc",
                "bus": "c-can",
                "bitrate": 500000,
                "acquisition_class": "passive_broadcast",
                "quality": "observed_alfa_scale",
                "provenance": "test",
                "side_effects": "none",
                "publisher_allowed": False,
            }
        ],
    },
    {
        "name": "battery.voltage",
        "unit": "V",
        "value_type": "number",
        "stale_after_seconds": 30.0,
        "minimum": 0,
        "maximum": 32,
        "allowed_acquisition_modes": ["passive"],
        "sources": [
            {
                "name": "ccan.broadcast.0x41a",
                "bus": "c-can",
                "bitrate": 500000,
                "acquisition_class": "passive_broadcast",
                "quality": "verified",
                "provenance": "test",
                "side_effects": "none",
                "publisher_allowed": False,
            }
        ],
    },
]

STATUS = {
    "service": "van-telemetry",
    "started_at": "2026-09-22T15:17:40+00:00",
    "vehicle_state": {
        "state": "asleep",
        "running": False,
        "confidence": "inferred",
        "basis": "passive_bus_silence",
        "detail": "no frames",
        "observed_at": "2026-09-22T18:58:21+00:00",
        "age_ms": 619,
    },
    "collector": {"state": "running", "cycles": 10, "last_cycle_at": "x", "interval_seconds": 1.0},
    "active_drive": {"enabled": True, "state": "idle"},
    "auxiliary_drive": {"enabled": True, "state": "idle", "metric": "vehicle.odometer"},
    "cached_metrics": {"engine.rpm": {"available": True, "value": 799.0}},
    "interface": {
        "mode": "listen_only",
        "active_inhibits": [],
        "role_interfaces": {
            "ready": True,
            "passive_ready": True,
            "resolved": True,
            "vehicle_buses_ready": True,
            "mode": "passive",
            "issues": [],
            "generation": 3,
            "inventory": [{"channel": "can0", "usb_serial": "SECRET"}],
            "roles": {
                "c-can": {
                    "channel": "can0",
                    "resolution": "resolved",
                    "reason": "ok",
                    "detail": "long detail",
                    "safe": True,
                    "passive_ready": True,
                    "expected": {"bitrate": 500000},
                    "actual": {"bitrate": 500000},
                }
            },
        },
    },
    "usb_can_monitor": {"enabled": True, "state": "running", "active_count": 0, "recent_events": [1, 2, 3]},
    "data_quality": {"active_count": 0, "recent": [{"x": 1}], "active": []},
    "radar_alignment": {},
    "last_readings": {"persistent": True, "storage_error": None},
    "supplemental_cache": {"state": "ready"},
    "current_owner": None,
    "interface_probe": {"state": "complete"},
    "history_recorder": {"enabled": True},
    "inflight": [],
    "last_acquisition_errors": {},
    "active_acquisition_permitted": True,
    "engine_off_voltage": {"enabled": True},
    "radar_alignment_polling": {"commissioned": True},
}

METRICS = {
    "engine.rpm": {
        "metric": "engine.rpm",
        "available": True,
        "unit": "rpm",
        "value": 799.0,
        "source": "ccan.broadcast.0x0fc",
        "bus": "c-can",
        "acquisition": "passive_broadcast",
        "interface_mode": "armed_diagnostic",
        "quality": "observed_alfa_scale",
        "observed_at": "2026-09-22T16:25:20+00:00",
        "age_ms": 9181866,
        "stale": True,
    },
    "battery.voltage": {
        "metric": "battery.voltage",
        "available": True,
        "unit": "V",
        "value": 12.6,
        "source": "ccan.broadcast.0x41a",
        "bus": "c-can",
        "acquisition": "passive",
        "interface_mode": None,
        "quality": "verified",
        "observed_at": "2026-09-22T18:00:17+00:00",
        "age_ms": 3485192,
        "stale": True,
    },
}

HEALTH = {
    "available": True,
    "schema_version": 1,
    "generated_at": "2026-09-22T18:57:18+00:00",
    "detail": "d",
    "assessments": [
        {
            "rule": "engine_coolant_temperature_relative_high",
            "metric": "engine.coolant_temperature",
            "state": "unavailable",
            "severity": "warning",
            "title": "Coolant temperature above its comparable-history band",
            "input_buckets": list(range(200)),
        }
    ],
    "active": [],
    "episodes": {
        "generated_at": "x",
        "schema_version": 1,
        "notification_outbox": {"pending": 0, "failed": 0, "delivered": 128},
        "active": [
            {
                "id": 614,
                "rule": "tire_pressure_rr_relative_low",
                "title": "RR tire",
                "status": "open",
                "state": "warning",
                "first_assessment_json": "{...}",
                "latest_assessment": {"state": "unavailable", "samples": list(range(500))},
            }
        ],
        "recent": [
            {
                "id": 600 + i,
                "rule": "battery_voltage_relative_low",
                "title": "Battery voltage below its comparable-history band",
                "status": "resolved",
                "state": "normal",
                "opened_at": "2026-09-21T00:00:00+00:00",
                "resolved_at": "2026-09-21T00:01:00+00:00",
                "resolution_reason": "current value is inside the learned relative-deviation band",
                "first_assessment": {"samples": list(range(300))},
                "latest_assessment": {"samples": list(range(300))},
            }
            for i in range(25)
        ],
    },
    "notification_delivery": {"enabled": True, "sink": "ntfy"},
    "custom_rules": {"count": 0, "enabled": True, "error": None, "rule_ids": []},
    "data_quality": {"active": [], "active_count": 0, "recent": [], "detail": "x"},
    "usb_can_incidents": {"active": [], "active_count": 0, "recent_events": list(range(100)), "recent_incidents": []},
    "overview_limits": {"byte_budget": 524288},
    "event_detail_template": "/v1/events/{id}",
    "evaluation_hook": {"mode": "on_ingest"},
    "evidence_scope": "overview",
    "method": {"center": "median"},
    "broker_cache": {"state": "ready"},
}

DTCS = {
    "available": True,
    "acquisition": "cache_only",
    "compact": True,
    "schema_version": 2,
    "generated_at": "2026-08-20T08:07:53Z",
    "detail": "d",
    "coverage": {"total_modules": 16},
    "group_counts": {"current": 1, "confirmed_history": 1},
    "group_returned_counts": {"current": 1, "confirmed_history": 1},
    "groups": {
        "current": [
            {
                "raw_dtc": "B163215",
                "fca_display": "B1632-15",
                "module_key": "bcm",
                "module_name": "Body Control Module",
                "description": "Left high-beam circuit — Short to battery or open",
                "status": "0x2F",
                "status_flags": ["test_failed"],
                "last_seen_at": "2026-07-24T20:31:34+00:00",
                "episode_count": 3,
            }
        ],
        "confirmed_history": [
            {
                "raw_dtc": "U000188",
                "fca_display": "U0001-88",
                "module_key": "rf_hub",
                "module_name": "RF Hub",
                "description": "CAN bus off",
                "status": "0x08",
                "status_flags": ["confirmed"],
                "last_seen_at": "2026-07-24T20:31:34+00:00",
                "episode_count": 1,
                "observation_count": 9,
            }
        ],
    },
    "groups_truncated": False,
    "per_group_limit": 25,
    "modules": [{"module_key": "bcm"}],
    "description_catalog": {"scope": "x"},
    "broker_cache": {"state": "ready"},
}


def find_sentinels(value, path="$"):
    """Paths of legacy ``{"omitted_items": n}`` sentinels left inside lists."""

    found = []
    if isinstance(value, dict):
        if "omitted_items" in value:
            found.append(path)
        for key, item in value.items():
            found.extend(find_sentinels(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_sentinels(item, f"{path}[{index}]"))
    return found


class CannedBrokerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/v1/status": STATUS,
            "/v1/snapshot": {"status": STATUS, "catalog": CATALOG, "metrics": METRICS},
            "/v1/history": {"available": True, "schema_version": 1, "recent_trips": []},
            "/v1/health": HEALTH,
            "/v1/diagnostics/dtcs": DTCS,
            "/v1/maintenance": {"available": True, "oil_changes": []},
            "/v1/metrics": {"metrics": CATALOG},
        }
        if path in routes:
            payload = json.loads(json.dumps(routes[path]))
            status = 200
        else:
            payload = {"available": False, "reason": "not_found", "detail": path}
            status = 404
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CannedBroker(socketserver.UnixStreamServer):
    pass


class WebV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vt-web-v2-", dir="/tmp")
        root = pathlib.Path(self.tmp.name)
        socket_path = str(root / "api.sock")
        self.static_root = root / "dist"
        (self.static_root / "assets").mkdir(parents=True)
        (self.static_root / "docs").mkdir()
        (self.static_root / "index.html").write_text("<!doctype html><title>Van</title>" + "x" * 2000)
        (self.static_root / "assets" / "app.deadbeef01.js").write_text("console.log(1);" + "/* pad */" * 300)
        (self.static_root / "assets" / "app.deadbeef01.css").write_text("body{margin:0}")
        (self.static_root / "docs" / "caveats.html").write_text("<h1>Caveats</h1>")
        (self.static_root / "manifest.webmanifest").write_text("{}")
        (self.static_root / "build.json").write_text(json.dumps({"build": "b1d2c3e4f5a6", "builtAt": "x"}))
        (root / "secret.txt").write_text("nope")
        self.api = CannedBroker(socket_path, CannedBrokerHandler)
        self.api_thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.api_thread.start()
        self.web = DashboardServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            socket_path=socket_path,
            allow_acquisitions=False,
            stream_interval_seconds=0.05,
            # Long enough that a slow first broker round trip on a busy worker
            # still leaves room for the two events the stream tests read.
            stream_max_seconds=2.0,
        )
        self.web_thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.web_thread.start()

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        self.web_thread.join()
        self.api.shutdown()
        self.api.server_close()
        self.api_thread.join()
        self.tmp.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        head = {k.lower(): v for k, v in response.getheaders()}
        connection.close()
        return response.status, head, raw

    def test_index_and_hashed_assets_have_the_intended_cache_policy(self):
        status, head, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(head["content-type"], "text/html; charset=utf-8")
        self.assertEqual(head["cache-control"], "no-cache")
        self.assertIn("etag", head)
        self.assertIn("content-security-policy", head)
        self.assertIn("style-src 'self'", head["content-security-policy"])
        self.assertTrue(raw.startswith(b"<!doctype html>"))

        status, head, _ = self.request("GET", "/", {"If-None-Match": head["etag"]})
        self.assertEqual(status, 304)

        status, head, raw = self.request("GET", "/assets/app.deadbeef01.js")
        self.assertEqual(status, 200)
        self.assertEqual(head["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(head["cache-control"], "public, max-age=31536000, immutable")
        self.assertNotIn("content-encoding", head)

        status, head, _ = self.request("GET", "/assets/app.deadbeef01.css")
        self.assertEqual(head["content-type"], "text/css; charset=utf-8")

        status, head, _ = self.request("GET", "/docs/caveats.html")
        self.assertEqual(status, 200)
        self.assertEqual(head["cache-control"], "no-cache")

        status, head, _ = self.request("GET", "/manifest.webmanifest")
        self.assertEqual(head["content-type"], "application/manifest+json")

    def test_gzip_is_used_only_when_accepted_and_worthwhile(self):
        status, head, raw = self.request("GET", "/assets/app.deadbeef01.js", {"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(status, 200)
        self.assertEqual(head["content-encoding"], "gzip")
        self.assertEqual(head["vary"], "Accept-Encoding")
        self.assertIn(b"console.log(1)", gzip.decompress(raw))
        self.assertEqual(int(head["content-length"]), len(raw))

        status, head, raw = self.request("GET", "/assets/app.deadbeef01.css", {"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", head)  # below the 1 KiB threshold
        self.assertEqual(raw, b"body{margin:0}")

        status, head, raw = self.request("GET", "/v1/snapshot", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(head.get("content-encoding"), "gzip")
        self.assertEqual(head["cache-control"], "no-store")
        payload = json.loads(gzip.decompress(raw))
        self.assertEqual(payload["web"]["active_acquisition_enabled"], False)
        self.assertIn("web_delivery", payload)
        self.assertEqual(len(payload["catalog"]), 2)
        self.assertEqual(payload["catalog_hash"], web_v2.catalog_hash(CATALOG))

    def test_static_root_is_sandboxed_and_unknown_paths_use_the_json_envelope(self):
        for path in ("/../secret.txt", "/assets/../../secret.txt", "/nope.js", "/assets/"):
            status, head, raw = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(head["content-type"], "application/json")
            self.assertEqual(json.loads(raw)["reason"], "not_found")
        status, _, raw = self.request("GET", "/v1/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw)["reason"], "not_found")
        status, _, raw = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")

    def test_v1_proxies_and_status_flags_are_unchanged(self):
        status, _, raw = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["web"]["active_acquisition_enabled"], False)
        self.assertEqual(payload["web"]["dtc_jobs_enabled"], False)
        self.assertTrue(payload["web"]["bind"].startswith("127.0.0.1:"))
        status, _, raw = self.request("GET", "/v1/metrics/engine.rpm")
        self.assertEqual(status, 404)  # canned broker has no per-metric route; proxied verbatim
        status, _, raw = self.request("GET", "/v1/diagnostics/dtc-jobs/current")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["reason"], "dtc_jobs_disabled")

    def read_events(self, path, count):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=3)
        connection.request("GET", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        events, current = [], []
        while len(events) < count:
            line = response.fp.readline()
            if not line:
                break
            if line in (b"\n", b"\r\n"):
                if current:
                    events.append(current)
                    current = []
                continue
            current.append(line.decode().rstrip())
        connection.close()
        return events

    def test_v1_snapshot_carries_catalog_hash_and_the_stream_web_flags(self):
        status, head, raw = self.request("GET", "/v1/snapshot?fresh=1-1")
        self.assertEqual(status, 200)
        self.assertEqual(head["cache-control"], "no-store")
        baseline = json.loads(raw)
        self.assertEqual(baseline["catalog_hash"], web_v2.catalog_hash(CATALOG))
        self.assertEqual(len(baseline["catalog"]), 2)
        self.assertEqual(baseline["metrics"]["engine.rpm"]["value"], 799.0)
        self.assertIn("cached_metrics", baseline["status"])  # full status, as web.py serves it
        web = baseline["web"]
        self.assertEqual(web["bind"], f"127.0.0.1:{self.web.server_port}")
        self.assertEqual(web["build"], "b1d2c3e4f5a6")
        self.assertEqual(web["dtc_jobs_enabled"], False)
        self.assertEqual(web["active_acquisition_enabled"], False)
        self.assertEqual(baseline["status"]["web"], web)
        delivery = baseline["web_delivery"]
        self.assertEqual(len(delivery["instance_id"]), 32)
        self.assertIsInstance(delivery["sequence"], int)
        # The baseline and the lite stream agree on the hash and on the exact
        # serialisation of the flags (key order included).
        event = json.loads(self.read_events("/v2/stream", 1)[0][2][len("data: "):])
        self.assertEqual(event["catalog_hash"], baseline["catalog_hash"])
        self.assertEqual(json.dumps(event["web"]), json.dumps(web))
        self.assertEqual(event["web_delivery"]["instance_id"], delivery["instance_id"])
        self.assertGreater(event["web_delivery"]["sequence"], delivery["sequence"])

    def test_build_id_is_read_once_per_build_and_tolerates_a_missing_file(self):
        build_json = self.static_root / "build.json"
        self.assertEqual(self.web.build_id(), "b1d2c3e4f5a6")
        with unittest.mock.patch.object(pathlib.Path, "read_text", side_effect=AssertionError("re-read")):
            self.assertEqual(self.web.build_id(), "b1d2c3e4f5a6")  # cached while unchanged
        build_json.write_text(json.dumps({"build": "0123456789ab", "builtAt": "later"}))
        os.utime(build_json, ns=(1, 2_000_000_000))
        self.assertEqual(self.web.build_id(), "0123456789ab")
        build_json.write_text("not json")
        self.assertIsNone(self.web.build_id())
        build_json.unlink()
        self.assertIsNone(self.web.build_id())
        status, _, raw = self.request("GET", "/v2/summary")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(raw)["web"]["build"])

    def test_user_agent_is_logged_once_per_client_ip(self):
        agent = "Mozilla/5.0 (Linux; Android 11; SM-T500) Chrome/120 Safari/537.36"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            for path in ("/", "/assets/app.deadbeef01.js", "/v1/status"):
                self.assertEqual(self.request("GET", path, {"User-Agent": agent})[0], 200)
            self.request("HEAD", "/", {"User-Agent": "other/1"})
        lines = [line for line in captured.getvalue().splitlines() if "user-agent" in line]
        self.assertEqual(lines, [f"127.0.0.1 user-agent {json.dumps(agent)}"])

    def test_lite_stream_carries_reduced_status_and_catalog_hash(self):
        events = self.read_events("/v2/stream", 2)
        self.assertEqual(len(events), 2)
        first = events[0]
        self.assertTrue(first[0].startswith("id: "))
        self.assertEqual(first[1], "event: snapshot")
        event = json.loads(first[2][len("data: "):])
        self.assertEqual(event["catalog_hash"], web_v2.catalog_hash(CATALOG))
        self.assertEqual(event["catalog_count"], 2)
        self.assertNotIn("catalog", event)
        self.assertEqual(event["metrics"]["engine.rpm"]["value"], 799.0)
        self.assertEqual(event["status"]["vehicle_state"]["state"], "asleep")
        self.assertNotIn("cached_metrics", event["status"])
        roles = event["status"]["interface"]["role_interfaces"]
        self.assertNotIn("inventory", roles)
        self.assertEqual(roles["roles"]["c-can"]["resolution"], "resolved")
        self.assertNotIn("detail", roles["roles"]["c-can"])
        self.assertNotIn("recent_events", event["status"]["usb_can_monitor"])
        self.assertEqual(event["web"]["dtc_jobs_enabled"], False)
        self.assertEqual(event["status"]["web"]["dtc_jobs_enabled"], False)
        self.assertEqual(event["status_code"], 200)
        delivery = event["web_delivery"]
        self.assertEqual(f"{delivery['instance_id']}:{delivery['sequence']}", first[0][len("id: "):])
        second = json.loads(events[1][2][len("data: "):])
        self.assertGreater(second["web_delivery"]["sequence"], delivery["sequence"])
        self.assertEqual(second["web_delivery"]["instance_id"], delivery["instance_id"])
        # The full /v1 stream is still available unchanged on this listener.
        full = json.loads(self.read_events("/v1/stream", 1)[0][2][len("data: "):])
        self.assertEqual(len(full["catalog"]), 2)
        self.assertIn("cached_metrics", full["status"])

    def test_summary_trims_evidence_arrays_and_keeps_headlines(self):
        status, head, raw = self.request("GET", "/v2/summary", {"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(head["content-encoding"], "gzip")
        summary = json.loads(gzip.decompress(raw))
        self.assertTrue(summary["available"])
        self.assertEqual(summary["web"]["dtc_jobs_enabled"], False)
        self.assertEqual(summary["status_full"]["vehicle_state"]["state"], "asleep")
        self.assertIn("cached_metrics", summary["status_full"])  # full status is intentional here
        health = summary["health"]
        self.assertEqual(health["assessments"][0]["input_buckets_omitted"], True)
        self.assertNotIn("input_buckets", health["assessments"][0])
        active = health["episodes"]["active"][0]
        self.assertEqual(active["first_assessment_json_omitted"], True)
        self.assertEqual(active["latest_assessment"]["samples_omitted"], True)
        recent = health["episodes"]["recent"]
        self.assertEqual(len(recent), 12)
        self.assertEqual(health["episodes"]["recent_total"], 25)
        self.assertEqual(health["episodes"]["recent_omitted_count"], 13)
        self.assertEqual(set(recent[0]) <= set(web_v2.EPISODE_HEADLINE_KEYS), True)
        self.assertEqual(recent[0]["title"], "Battery voltage below its comparable-history band")
        self.assertNotIn("recent_events", health["usb_can_incidents"])
        self.assertEqual(health["usb_can_incidents"]["recent_incidents"], [])
        self.assertNotIn("recent_incidents_omitted_count", health["usb_can_incidents"])
        self.assertEqual(summary["web"]["build"], "b1d2c3e4f5a6")
        self.assertEqual(summary["web"]["bind"], f"127.0.0.1:{self.web.server_port}")
        self.assertEqual(summary["status_full"]["web"], summary["web"])
        self.assertEqual(find_sentinels(summary), [])
        dtcs = summary["dtcs"]
        self.assertEqual(dtcs["groups"]["current"][0]["status_flags"], ["test_failed"])
        self.assertNotIn("status_flags", dtcs["groups"]["confirmed_history"][0])
        self.assertEqual(dtcs["groups"]["confirmed_history"][0]["fca_display"], "U0001-88")
        self.assertEqual(summary["maintenance"]["oil_changes"], [])
        self.assertEqual(summary["history"]["status_code"], 200)

    def test_summary_reports_broker_outage_per_section(self):
        def fetch(path):
            if path == "/v1/health":
                raise OSError("socket gone")
            return 200, {"available": True, "path": path}

        status, summary = web_v2.build_summary(fetch, {"dtc_jobs_enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(summary["health"]["reason"], "broker_unavailable")
        self.assertEqual(summary["history"]["path"], "/v1/history")

        def fetch_down(_path):
            raise OSError("down")

        status, summary = web_v2.build_summary(fetch_down, {})
        self.assertEqual(status, 503)
        self.assertEqual(summary["reason"], "broker_unavailable")


class HelperTests(unittest.TestCase):
    def test_warning_rows_are_never_capped_and_capped_lists_count_beside(self):
        rules = [f"custom_rule_{index:02d}" for index in range(20)]
        health = {
            "available": True,
            "assessments": [
                {"rule": rule, "state": "watch", "samples": list(range(50))} for rule in rules
            ],
            "active": [{"rule": rule, "state": "warning"} for rule in rules],
            "episodes": {
                "active": [{"id": index, "rule": rule, "status": "open"} for index, rule in enumerate(rules)],
                "recent": [{"id": 1000 + index, "rule": "x"} for index in range(5)],
                "notification_outbox": {"pending": 0},
            },
            "usb_can_incidents": {"active": [], "recent_incidents": [{"n": index} for index in range(32)]},
            "data_quality": {"active": [], "recent": [{"n": index} for index in range(3)]},
        }

        def fetch(path):
            payload = health if path == "/v1/health" else {"available": True}
            return 200, json.loads(json.dumps(payload))

        status, summary = web_v2.build_summary(fetch, {})
        self.assertEqual(status, 200)
        lite = json.loads(json.dumps(summary))["health"]
        self.assertEqual([row["rule"] for row in lite["assessments"]], rules)
        self.assertTrue(all(row["samples_omitted"] for row in lite["assessments"]))
        self.assertEqual([row["rule"] for row in lite["active"]], rules)
        self.assertEqual([row["rule"] for row in lite["episodes"]["active"]], rules)
        for key in ("assessments_omitted_count", "active_omitted_count"):
            self.assertNotIn(key, lite)
        self.assertNotIn("active_omitted_count", lite["episodes"])
        self.assertNotIn("recent_omitted_count", lite["episodes"])
        self.assertEqual(lite["episodes"]["recent_total"], 5)
        usb = lite["usb_can_incidents"]
        self.assertEqual(len(usb["recent_incidents"]), web_v2.SUMMARY_MAX_LIST)
        self.assertEqual(usb["recent_incidents_omitted_count"], 32 - web_v2.SUMMARY_MAX_LIST)
        self.assertEqual(len(lite["data_quality"]["recent"]), 3)
        self.assertEqual(find_sentinels(lite), [])

    def test_user_agent_memory_is_bounded_and_least_recently_seen_is_evicted(self):
        state = types.SimpleNamespace(
            _agents_lock=threading.Lock(), _agents_seen=collections.OrderedDict()
        )
        note = web_v2.DashboardServer.note_user_agent
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertTrue(note(state, "10.0.0.1", "first"))
            self.assertFalse(note(state, "10.0.0.1", "first again"))
            for index in range(2, web_v2.USER_AGENT_MEMORY + 1):
                note(state, f"10.0.0.{index}", "ua")
            self.assertFalse(note(state, "10.0.0.1", "refresh"))  # now most recent
            self.assertTrue(note(state, "10.0.1.1", "one more"))  # evicts 10.0.0.2
            self.assertEqual(len(state._agents_seen), web_v2.USER_AGENT_MEMORY)
            self.assertTrue(note(state, "10.0.0.2", "back"))
            self.assertFalse(note(state, "10.0.0.1", "still remembered"))
            self.assertTrue(note(state, "10.0.2.1", "x" * 5000))
        self.assertEqual(len(state._agents_seen), web_v2.USER_AGENT_MEMORY)
        last = captured.getvalue().splitlines()[-1]
        self.assertLess(len(last), web_v2.USER_AGENT_MAX_CHARS + 40)

    def test_catalog_hash_is_order_insensitive_for_keys_and_stable(self):
        a = web_v2.catalog_hash([{"b": 1, "a": 2}])
        b = web_v2.catalog_hash([{"a": 2, "b": 1}])
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, web_v2.catalog_hash([{"a": 3, "b": 1}]))
        self.assertIsNone(web_v2.catalog_hash(None))

    def test_status_lite_tolerates_missing_sections(self):
        self.assertEqual(web_v2.status_lite(None), None)
        lite = web_v2.status_lite({"vehicle_state": {"state": "running"}, "interface": "odd"})
        self.assertEqual(lite["vehicle_state"]["state"], "running")
        self.assertNotIn("interface", lite)

    def test_shrink_bounds_lists_depth_and_strings(self):
        nested = {"a": [{"b": [{"c": [{"d": [{"e": [{"f": [{"g": 1}]}]}]}]}]}]}
        shrunk = web_v2.shrink(nested)
        self.assertIsInstance(shrunk["a"], list)
        long = web_v2.shrink({"s": "x" * 2000})["s"]
        self.assertEqual(len(long), web_v2.SUMMARY_MAX_STRING + 1)
        many = web_v2.shrink(list(range(40)))
        self.assertEqual(many, list(range(web_v2.SUMMARY_MAX_LIST)))
        wrapped = web_v2.shrink({"recent": list(range(40)), "few": [1, 2]})
        self.assertEqual(len(wrapped["recent"]), web_v2.SUMMARY_MAX_LIST)
        self.assertEqual(wrapped["recent_omitted_count"], 40 - web_v2.SUMMARY_MAX_LIST)
        self.assertNotIn("few_omitted_count", wrapped)
        self.assertEqual(find_sentinels(wrapped), [])
        exact = web_v2.shrink({"recent": list(range(web_v2.SUMMARY_MAX_LIST))})
        self.assertNotIn("recent_omitted_count", exact)

    def test_tailscale_unit_arguments_validate_with_only_the_documented_variables(self):
        systemd = pathlib.Path(web_v2.__file__).with_name("systemd")
        unit = (systemd / "van-telemetry-web-v2-tailscale.service").read_text()
        example = (systemd / "tailscale-web.env.example").read_text()
        documented = set(re.findall(r"^([A-Z_]+)=", example, re.MULTILINE))
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        used = set(re.findall(r"\$\{([A-Z_]+)\}", exec_line))
        self.assertEqual(used - documented, set())  # an undefined variable crash-loops the unit
        self.assertNotIn("VAN_TELEMETRY_DTC_ORIGIN_V2", unit)
        env = {"VAN_TELEMETRY_TAILSCALE_BIND": "100.82.91.76"}
        words = [
            re.sub(r"\$\{([A-Z_]+)\}", lambda match: env[match.group(1)], word)
            for word in shlex.split(exec_line[len("ExecStart="):])
        ]
        self.assertTrue(words[1].endswith("projects/vehicle_data/web_v2.py"))
        args = web_v2.build_parser().parse_args(words[2:])
        self.assertTrue(args.enable_dtc_jobs)
        self.assertEqual(args.port, 8766)
        web_v2.base.validate_dtc_job_bind(args.bind)
        self.assertEqual(
            web_v2.base.validate_dtc_origin(args.dtc_trusted_origin, bind=args.bind, port=args.port),
            "http://100.82.91.76:8766",
        )

    def test_parser_defaults_to_port_8766_and_requires_a_built_root(self):
        parser = web_v2.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.port, 8766)
        self.assertTrue(args.static_root.endswith("dashboard/dist"))
        with self.assertRaises(SystemExit):
            web_v2.main(["--static-root", "/nonexistent/dist"])


if __name__ == "__main__":
    unittest.main()
