import os
import json
from pathlib import Path
import tempfile
import unittest

from lib.dtc_web import (
    ArmTokenStore,
    DtcWebAuthorizationError,
    DtcWebRequestError,
    build_request,
    claim_request,
    queue_cancel_request,
    queue_request,
    read_cancel_request,
    validate_request,
)
from projects.vehicle_data.web import (
    DtcWebController,
    validate_dtc_job_bind,
    validate_dtc_origin,
)


class DtcWebBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_arm_token_is_hashed_private_expiring_and_one_use(self):
        path = self.root / "arm.json"
        store = ArmTokenStore(path)
        issued = store.issue(ttl_seconds=60, now=100.0)
        raw = path.read_text()
        self.assertNotIn(str(issued["token"]), raw)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

        store.consume(str(issued["token"]), now=159.0)
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(DtcWebAuthorizationError, "no local"):
            store.consume(str(issued["token"]), now=159.0)

    def test_bad_token_does_not_consume_but_expired_token_does(self):
        path = self.root / "arm.json"
        store = ArmTokenStore(path)
        issued = store.issue(ttl_seconds=60, now=100.0)
        with self.assertRaisesRegex(DtcWebAuthorizationError, "invalid"):
            store.consume("x" * 43, now=110.0)
        self.assertTrue(path.exists())
        with self.assertRaisesRegex(DtcWebAuthorizationError, "expired"):
            store.consume(str(issued["token"]), now=160.0)
        self.assertFalse(path.exists())

    def test_fixed_request_is_exclusive_private_and_claimed(self):
        path = self.root / "request.json"
        request = build_request("dtc-web-123", now=100.0)
        queue_request(path, request, now=100.0)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(DtcWebRequestError, "already queued"):
            queue_request(
                path,
                build_request("dtc-web-456", now=100.0),
                now=100.0,
            )

        claimed = claim_request(path, now=101.0)
        self.assertEqual(claimed["job_id"], "dtc-web-123")
        self.assertFalse(path.exists())
        self.assertTrue(Path(str(claimed["claimed_path"])).is_file())

    def test_request_schema_payload_and_freshness_are_closed(self):
        request = build_request("dtc-web-123", now=100.0)
        self.assertEqual(validate_request(request, now=100.0)["request_hex"], "19 02 FF")
        for update in (
            {"clear_requested": True},
            {"request_hex": "14 FF FF FF"},
            {"action": "arbitrary"},
            {"confirm_parked": False},
        ):
            with self.subTest(update=update):
                with self.assertRaises(DtcWebRequestError):
                    validate_request({**request, **update}, now=100.0)
        with self.assertRaisesRegex(DtcWebRequestError, "schema"):
            validate_request({**request, "module": "pcm"}, now=100.0)
        with self.assertRaisesRegex(DtcWebRequestError, "expired"):
            validate_request(request, now=401.0)
        with self.assertRaises(DtcWebRequestError):
            validate_request(
                {**request, "created_at_epoch": float("nan")},
                now=100.0,
            )

    def test_cancel_request_is_fixed_private_and_job_scoped(self):
        path = self.root / "cancel.json"
        queue_cancel_request(path, "dtc-web-test", now=100.0)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(
            read_cancel_request(
                path,
                expected_job_id="dtc-web-test",
                now=100.0,
            )["action"],
            "cancel_dtc_batch",
        )
        with self.assertRaises(DtcWebRequestError):
            read_cancel_request(
                path,
                expected_job_id="different-job",
                now=100.0,
            )
        with self.assertRaises(DtcWebRequestError):
            queue_cancel_request(path.with_name("nan.json"), "dtc-web-test", now=float("nan"))

    def test_controller_consumes_arm_and_queues_single_job(self):
        arm = self.root / "arm.json"
        request = self.root / "request.json"
        current = self.root / "current.json"
        controller = DtcWebController(
            arm_path=arm,
            request_path=request,
            current_path=current,
            cancel_dir=self.root / "cancel",
            job_root=self.root / "jobs",
        )
        issued = controller.arm_store.issue(ttl_seconds=60)
        queued = controller.start(str(issued["token"]))
        self.assertEqual(queued["state"], "queued")
        self.assertTrue(request.is_file())
        self.assertFalse(arm.exists())
        self.assertEqual(controller.status()["state"], "queued")
        with self.assertRaisesRegex(DtcWebRequestError, "already"):
            controller.start(str(issued["token"]))

        cancelled = controller.cancel()
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse(request.exists())
        self.assertTrue(
            next(self.root.glob("request.json.cancelled-dtc-web-*"), None)
        )

    def test_trusted_origin_must_exactly_match_listener(self):
        self.assertEqual(
            validate_dtc_origin(
                "http://100.64.0.10:8765",
                bind="100.64.0.10",
                port=8765,
            ),
            "http://100.64.0.10:8765",
        )
        for origin in (
            None,
            "http://100.64.0.10:8766",
            "http://192.168.6.103:8765",
            "http://100.64.0.10:8765/path",
            "http://user@100.64.0.10:8765",
            "http://100.64.0.10:not-a-port",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(SystemExit):
                    validate_dtc_origin(
                        origin,
                        bind="100.64.0.10",
                        port=8765,
                    )

    def test_dtc_jobs_reject_lan_and_accept_only_loopback_or_tailscale(self):
        for bind in ("127.0.0.1", "::1", "100.64.0.10", "fd7a:115c:a1e0::10"):
            with self.subTest(bind=bind):
                self.assertIsNone(validate_dtc_job_bind(bind))
        for bind in ("192.168.6.103", "0.0.0.0", "vanpi", "2001:db8::1"):
            with self.subTest(bind=bind):
                with self.assertRaises(SystemExit):
                    validate_dtc_job_bind(bind)

    def test_restoration_failure_is_sticky_at_web_boundary(self):
        current = self.root / "current.json"
        jobs = self.root / "jobs"
        controller = DtcWebController(
            arm_path=self.root / "arm.json",
            request_path=self.root / "request.json",
            current_path=current,
            cancel_dir=self.root / "cancel",
            job_root=jobs,
        )
        job_id = "dtc-web-20260822T010203Z-deadbeef"
        job = jobs / job_id
        job.mkdir(parents=True)
        (job / "job.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "state": "restoration_failed",
                    "modules": [],
                    "progress": {},
                    "cancel_requested": False,
                }
            )
        )
        (job / "job.json").chmod(0o600)
        current.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "state": "restoration_failed",
                }
            )
        )
        current.chmod(0o600)
        issued = controller.arm_store.issue(ttl_seconds=60)
        with self.assertRaisesRegex(DtcWebRequestError, "unverified restoration"):
            controller.start(str(issued["token"]))
        self.assertTrue((self.root / "arm.json").is_file())


if __name__ == "__main__":
    unittest.main()
