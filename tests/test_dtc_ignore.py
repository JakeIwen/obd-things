"""Ignored diagnostic codes: the store, the broker route, the web relay and the summary slice."""

import http.client
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.modules import MODULES
from projects.vehicle_data import dtc_ignore, web_v2
from projects.vehicle_data.dtc_ignore import DtcIgnoreStore
from tests import test_vehicle_data as api_fixtures

MODULE = next(iter(MODULES))
OTHER_MODULE = list(MODULES)[1]


def ignore(code="B1632-15", note="LED headlights", request_id="ign_test_000001", module=MODULE):
    return {"action": "ignore", "module_key": module, "fca_display": code,
            "note": note, "request_id": request_id}


def restore(code="B1632-15", request_id="res_test_000001", module=MODULE):
    return {"action": "restore", "module_key": module, "fca_display": code,
            "request_id": request_id}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / dtc_ignore.FILE_NAME
        self.now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        self.store = DtcIgnoreStore(self.path, now=lambda: self.now)

    def test_ignore_lists_newest_first_and_survives_restart(self):
        self.assertEqual(self.store.snapshot(), {
            "available": True, "persistent": True, "storage_error": None, "limit": 500, "ignored": []})
        self.assertEqual(self.store.request(ignore())[0], 200)
        self.now += timedelta(minutes=1)
        code, reply = self.store.request(ignore("U0001-88", "", "ign_test_000002", OTHER_MODULE))
        self.assertEqual(code, 200)
        self.assertEqual([row["fca_display"] for row in reply["ignored"]], ["U0001-88", "B1632-15"])
        self.assertEqual(reply["ignored"][1], {
            "module_key": MODULE, "fca_display": "B1632-15", "note": "LED headlights",
            "ignored_at": "2026-11-16T12:00:00+00:00"})
        self.assertEqual(DtcIgnoreStore(self.path).snapshot(), reply)

    def test_reignoring_changes_nothing_and_succeeds(self):
        self.store.request(ignore())
        before = self.path.read_bytes()
        self.now += timedelta(hours=1)
        code, reply = self.store.request(ignore(note="a different note", request_id="ign_test_000002"))
        self.assertEqual((code, reply["ignored"][0]["note"]), (200, "LED headlights"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_restore_removes_and_restoring_an_unknown_code_succeeds(self):
        self.store.request(ignore())
        code, reply = self.store.request(restore())
        self.assertEqual((code, reply["ignored"]), (200, []))
        code, reply = self.store.request(restore("P0420-00", "res_test_000002"))
        self.assertEqual((code, reply["ignored"]), (200, []))
        self.assertEqual(DtcIgnoreStore(self.path).snapshot()["ignored"], [])

    def test_the_same_code_under_two_modules_is_two_entries(self):
        self.store.request(ignore())
        self.store.request(ignore(module=OTHER_MODULE, request_id="ign_test_000002"))
        self.assertEqual(len(self.store.snapshot()["ignored"]), 2)
        self.store.request(restore(request_id="res_test_000002"))
        self.assertEqual([row["module_key"] for row in self.store.snapshot()["ignored"]], [OTHER_MODULE])

    def test_the_501st_code_is_refused_with_a_clear_error(self):
        for index in range(dtc_ignore.MAX_IGNORED):
            code, _ = self.store.request(ignore(f"B{index:04d}-00", "", f"ign_test_{index:06d}"))
            self.assertEqual(code, 200)
        code, reply = self.store.request(ignore("C9999-00", "", "ign_test_999999"))
        self.assertEqual((code, reply["reason"]), (409, "limit_reached"))
        self.assertIn("500", reply["detail"])
        self.assertEqual(len(reply["ignored"]), 500)
        self.assertEqual(self.store.request(restore("B0000-00"))[0], 200)
        self.assertEqual(self.store.request(ignore("C9999-00", "", "ign_test_999998"))[0], 200)

    def test_the_audit_log_keeps_only_the_last_200_actions_and_is_not_served(self):
        for index in range(150):
            self.store.request(ignore(f"B{index:04d}-00", "", f"ign_test_{index:06d}"))
            self.store.request(restore(f"B{index:04d}-00", f"res_test_{index:06d}"))
        saved = json.loads(self.path.read_text())
        self.assertEqual(len(saved["log"]), dtc_ignore.LOG_LIMIT)
        self.assertEqual(saved["log"][-1]["request_id"], "res_test_000149")
        self.assertNotIn("log", self.store.snapshot())
        self.assertEqual(DtcIgnoreStore(self.path).error, None)

    def test_bad_requests_are_refused_and_change_nothing(self):
        bad = [
            ignore(module="not_a_module"), ignore(module=7), ignore(code=""), ignore(code="B1632 15"),
            ignore(code="x" * 33), ignore(code=5), ignore(note="n" * 201), ignore(note=None),
            ignore(note="line\nbreak"), ignore(request_id="short"), {**ignore(), "extra": 1},
            {**restore(), "note": ""}, {"action": "clear"}, [ignore()], "ignore",
        ]
        bad.append({key: value for key, value in ignore().items() if key != "note"})
        for payload in bad:
            with self.subTest(payload=payload):
                code, reply = self.store.request(payload)
                self.assertEqual((code, reply["reason"]), (400, "invalid_request"))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.request(ignore(note="n" * 200))[0], 200)

    def test_request_id_replay_is_idempotent_but_reuse_for_another_change_is_refused(self):
        self.store.request(ignore())
        self.store.request(restore())
        # The retry of an already applied ignore must not undo the later restore.
        code, reply = self.store.request(ignore())
        self.assertEqual((code, reply["ignored"]), (200, []))
        code, reply = self.store.request(ignore("U0001-88"))
        self.assertEqual((code, reply["reason"]), (400, "invalid_request"))
        self.assertIn("Request ID", reply["detail"])

    def test_an_unreadable_file_is_preserved_and_reported(self):
        for content in ("{not json", json.dumps({"version": 2, "ignored": [], "log": []}),
                        json.dumps({"version": 1, "ignored": [{"module_key": "bogus"}], "log": []})):
            with self.subTest(content=content):
                self.path.write_text(content)
                store = DtcIgnoreStore(self.path)
                snapshot = store.snapshot()
                self.assertFalse(snapshot["available"])
                self.assertIn("could not be read", snapshot["storage_error"])
                code, reply = store.request(ignore())
                self.assertEqual((code, reply["reason"]), (503, "storage_unavailable"))
                self.assertEqual(self.path.read_text(), content)

    def test_a_failed_write_keeps_memory_and_reports_it(self):
        with mock.patch.object(dtc_ignore, "_atomic_json", side_effect=OSError("disk full")):
            code, reply = self.store.request(ignore())
        self.assertEqual((code, reply["reason"], reply["ignored"]), (503, "storage_unavailable", []))
        self.assertEqual(self.store.request(ignore())[0], 200)
        self.assertEqual(len(DtcIgnoreStore(self.path).snapshot()["ignored"]), 1)

    def test_without_a_path_the_list_works_but_is_not_persistent(self):
        store = DtcIgnoreStore()
        code, reply = store.request(ignore())
        self.assertEqual((code, reply["persistent"], len(reply["ignored"])), (200, False, 1))


class BrokerRouteTests(unittest.TestCase):
    setUp = api_fixtures.ApiTests.setUp
    tearDown = api_fixtures.ApiTests.tearDown

    def test_get_and_post_reach_the_store_and_never_touch_the_dtc_cache(self):
        self.broker.dtc_ignores = DtcIgnoreStore(Path(self.tmp.name) / dtc_ignore.FILE_NAME)
        path = dtc_ignore.API_PATH
        dtcs_before = self.client.request("GET", "/v1/diagnostics/dtcs")
        self.assertEqual(self.client.request("GET", path)[1]["ignored"], [])
        code, reply = self.client.request("POST", path, ignore())
        self.assertEqual((code, [row["fca_display"] for row in reply["ignored"]]), (200, ["B1632-15"]))
        self.assertEqual(self.client.request("GET", path)[1], reply)
        self.assertEqual(self.client.request("POST", path, {"action": "ignore"})[0], 400)
        self.assertEqual(self.client.request("GET", "/v1/diagnostics/dtcs"), dtcs_before)
        self.assertEqual(self.acquirer.calls, [])


class WebRouteTests(unittest.TestCase):
    tearDown = api_fixtures.WebTests.tearDown
    request = api_fixtures.WebTests.request

    def setUp(self):
        api_fixtures.WebTests.setUp(self)
        self.api.broker.dtc_ignores = DtcIgnoreStore(Path(self.tmp.name) / dtc_ignore.FILE_NAME)
        self.origin = f"http://127.0.0.1:{self.web.server_port}"

    def post(self, body, headers=None, raw=None):
        headers = {"Origin": self.origin, **(headers or {})}
        if raw is not None:
            connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=2)
            connection.request("POST", dtc_ignore.API_PATH, body=raw,
                               headers={"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, json.loads(data)
        status, data = self.request("POST", dtc_ignore.API_PATH, body, headers)
        return status, json.loads(data)

    def test_same_origin_json_only_and_the_list_is_visible_on_reload(self):
        self.assertEqual(self.request("POST", dtc_ignore.API_PATH, ignore())[0], 403)
        self.assertEqual(self.post(ignore(), {"Origin": "http://unrelated.example"})[0], 403)
        self.assertEqual(self.post(ignore(), {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post(ignore(), {"Content-Type": "text/plain"}, raw=b"{}")[0], 415)
        status, body = self.post(None, raw=b"{oops")
        self.assertEqual((status, body["reason"]), (400, "invalid_request"))
        status, body = self.post(None, raw=b"x" * 5000)
        self.assertEqual(status, 400)
        status, body = self.post({**ignore(), "extra": True})
        self.assertEqual((status, body["reason"]), (400, "invalid_request"))
        status, body = self.post(ignore())
        self.assertEqual((status, body["ignored"][0]["note"]), (200, "LED headlights"))
        status, raw = self.request("GET", dtc_ignore.API_PATH)
        self.assertEqual((status, json.loads(raw)), (200, body))
        self.assertEqual(self.acquirer.calls, [])


class SummarySliceTests(unittest.TestCase):
    def fetch_with(self, ignores):
        def fetch(path):
            if path == dtc_ignore.API_PATH:
                if isinstance(ignores, Exception):
                    raise ignores
                return ignores
            return 200, {"available": True}
        return fetch

    def test_the_list_rides_the_summary_beside_the_dtcs(self):
        reply = DtcIgnoreStore()
        reply.request(ignore())
        status, summary = web_v2.build_summary(self.fetch_with((200, reply.snapshot())), {})
        self.assertEqual(status, 200)
        self.assertEqual(summary["dtc_ignores"], reply.snapshot())
        self.assertIn("dtcs", summary)

    def test_an_older_broker_or_an_unreachable_one_yields_unavailable_without_failing_the_rest(self):
        older = (404, {"available": False, "reason": "not_found", "detail": dtc_ignore.API_PATH})
        for ignores in (older, OSError("socket gone"), (200, ["not", "a", "dict"])):
            with self.subTest(ignores=ignores):
                status, summary = web_v2.build_summary(self.fetch_with(ignores), {})
                self.assertEqual(status, 200)
                self.assertEqual(summary["dtc_ignores"], {"available": False})
                self.assertTrue(summary["maintenance"]["available"])

    def test_an_unreadable_file_is_passed_on_so_the_dashboard_can_say_why(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / dtc_ignore.FILE_NAME
        path.write_text("{oops")
        _, summary = web_v2.build_summary(self.fetch_with((200, DtcIgnoreStore(path).snapshot())), {})
        self.assertFalse(summary["dtc_ignores"]["available"])
        self.assertIn("could not be read", summary["dtc_ignores"]["storage_error"])


if __name__ == "__main__":
    unittest.main()
