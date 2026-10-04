"""Wire-shape and import-boundary regressions for the HTTP refactor."""

import io
import json
import types
import unittest
from unittest import mock

from lib import dtc_web
from projects.vehicle_data import api, api_client, api_schema, api_server, http_common, web
from projects.vehicle_data.metrics import METRICS
from tests.test_vehicle_data import FakeAcquirer, FakeClock, TelemetryBroker


class HttpContractTests(unittest.TestCase):
    def test_compatibility_exports_are_the_implementations(self):
        for name in ("TelemetryClient", "UnixHTTPConnection"):
            self.assertIs(getattr(api, name), getattr(api_client, name))
        for name in ("TelemetryApiHandler", "UnixHTTPServer", "serve_unix", "_prepare_socket_path"):
            self.assertIs(getattr(api, name), getattr(api_server, name))
        for name in ("MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "OBSERVATION_DEADLINE_HEADER", "MAX_OBSERVATION_QUEUE_SECONDS"):
            self.assertEqual(getattr(api, name), getattr(http_common, name))
        self.assertIs(web.DtcWebController, dtc_web.DtcWebController)

    def server(self):
        return types.SimpleNamespace(
            warning_chat_socket=None,
            allow_acquisitions=False,
            dtc_controller=None,
            server_address=("127.0.0.1", 8765),
            build_id=lambda: None,
            stream_max_seconds=1,
            stream_interval_seconds=.25,
            next_snapshot_delivery=lambda: {
                "instance_id": "instance", "sequence": 1,
                "generated_at_ms": 1000, "generated_monotonic_ms": 0,
            },
        )

    def test_flags_preserve_all_three_shapes_and_insertion_orders(self):
        server = self.server()
        full = http_common.web_flags(server)
        self.assertEqual(list(full), [
            "warning_chat_enabled", "active_acquisition_enabled", "dtc_jobs_enabled",
            "dtc_jobs_require_local_one_use_arm", "bind",
        ])
        self.assertEqual(full["bind"], "127.0.0.1:8765")
        stream = http_common.web_flags(server, include_bind=False)
        self.assertEqual(list(stream), list(full)[:-1])
        dashboard = http_common.web_flags(server, include_build=True)
        self.assertEqual(list(dashboard), sorted([*full, "build"]))
        self.assertIsNone(dashboard["build"])
        self.assertFalse(dashboard["dtc_jobs_require_local_one_use_arm"])

    def test_schema_names_actual_broker_and_catalog_keys(self):
        broker = TelemetryBroker(acquirer=FakeAcquirer(), monotonic=FakeClock())
        status = broker.status_response()
        self.assertEqual(set(status), api_schema.StatusResponse.__required_keys__)
        snapshot = broker.snapshot_response()
        self.assertEqual(set(snapshot), api_schema.SnapshotResponse.__required_keys__)
        for definition in METRICS.values():
            record = definition.public_dict()
            self.assertLessEqual(api_schema.MetricDefinition.__required_keys__, record.keys())
            self.assertLessEqual(record.keys(), api_schema.MetricDefinition.__annotations__.keys())
            for source in record["sources"]:
                self.assertEqual(set(source), api_schema.MetricSource.__required_keys__)

    def test_schema_distinguishes_absent_flags_and_delivery_overlays(self):
        self.assertEqual(api_schema.WebFlags.__optional_keys__, {"bind", "build"})
        self.assertEqual(api_schema.SnapshotResponse.__optional_keys__, {
            "web", "web_delivery", "catalog_hash", "status_code",
        })
        self.assertEqual(set(self.server().next_snapshot_delivery()), api_schema.SnapshotDelivery.__required_keys__)

    def test_broker_and_web_empty_metric_routes_remain_distinct(self):
        self.assertEqual(http_common.route_name("/v1/metrics/", api_server.TelemetryApiHandler.GET_ROUTES), "_metric_get")
        self.assertIsNone(http_common.route_name("/v1/metrics/", web.TelemetryWebHandler.GET_ROUTES))
        for path in ("/v1/metrics/a/b", "/v1/metrics//"):
            self.assertIsNone(http_common.route_name(path, api_server.TelemetryApiHandler.GET_ROUTES))
            self.assertIsNone(http_common.route_name(path, web.TelemetryWebHandler.GET_ROUTES))
        self.assertIsNone(http_common.route_name("/v1/acquisitions/batteryXvoltage", web.TelemetryWebHandler.POST_ROUTES))

    def handler(self):
        handler = mock.Mock()
        handler.server = self.server()
        handler.wfile = io.BytesIO()
        return handler

    def run_window(self, handler, prepare):
        with mock.patch.object(http_common, "time") as clock:
            clock.monotonic.side_effect = (0, .5, 2)
            http_common.stream_snapshots(handler, prepare)
            clock.sleep.assert_called_once_with(.25)

    def test_full_stream_keeps_header_and_payload_injection_order(self):
        handler = self.handler()
        handler.client.request.return_value = (200, {"status": {}, "catalog": [], "metrics": {}})
        self.run_window(handler, lambda code, payload: web.TelemetryWebHandler._stream_event(handler, code, payload))
        self.assertEqual(handler.method_calls[:6], [
            mock.call.send_response(200),
            mock.call.send_header("Content-Type", "text/event-stream"),
            mock.call.send_header("Connection", "keep-alive"),
            mock.call.send_header("X-Accel-Buffering", "no"),
            mock.call._common_headers(), mock.call.end_headers(),
        ])
        raw = handler.wfile.getvalue()
        self.assertTrue(raw.startswith(b"id: instance:1\nevent: snapshot\ndata: "))
        self.assertTrue(raw.endswith(b"\n\n"))
        event = json.loads(raw.split(b"data: ", 1)[1])
        self.assertEqual(list(event), ["status", "catalog", "metrics", "status_code", "web", "web_delivery"])
        self.assertNotIn("bind", event["web"])
        self.assertEqual(event["status"]["web"], event["web"])

    def test_stream_error_is_not_the_http_503_envelope(self):
        handler = self.handler()
        handler.client.request.side_effect = OSError("offline")
        prepare = mock.Mock()
        self.run_window(handler, prepare)
        prepare.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b'event: error\ndata: {"reason":"broker_unavailable","detail":"offline"}\n\n')
        self.assertEqual(http_common.broker_unavailable(OSError("offline")), {
            "available": False, "reason": "broker_unavailable", "detail": "offline",
        })


if __name__ == "__main__":
    unittest.main()
