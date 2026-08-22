import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from lib.dtc_batch import JobStore, atomic_json
from lib.dtc_web import build_request, queue_cancel_request, queue_request
from tools import dtc_batch_request


class DtcBatchRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = self.root / "request.json"
        self.current = self.root / "current.json"
        self.cancel_dir = self.root / "cancel"
        self.cancel_dir.mkdir(mode=0o700)
        self.jobs = self.root / "jobs"

    def test_consumes_closed_request_and_calls_only_fixed_batch(self):
        queue_request(
            self.request,
            build_request("dtc-web-test", now=100.0),
            now=100.0,
        )
        with (
            mock.patch("lib.dtc_web.time.time", return_value=101.0),
            mock.patch("tools.dtc_batch_request.dtc_batch.main", return_value=0) as run,
        ):
            code = dtc_batch_request.main(
                [
                    "--request-file",
                    str(self.request),
                    "--current-file",
                    str(self.current),
                    "--job-root",
                    str(self.jobs),
                    "--cancel-dir",
                    str(self.cancel_dir),
                ]
            )
        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "--execute",
                "--confirm-parked",
                "--confirm-park-gear",
                "--confirm-ignition-on-engine-off",
                "--job-id",
                "dtc-web-test",
                "--job-root",
                str(self.jobs),
                "--json",
            ],
        )
        current = json.loads(self.current.read_text())
        self.assertEqual(current["job_id"], "dtc-web-test")
        self.assertEqual(current["state"], "failed")
        self.assertFalse(self.request.exists())
        self.assertTrue((self.jobs / "dtc-web-test" / "web-request.json").is_file())

    def test_invalid_request_never_calls_batch(self):
        self.request.write_text("{}")
        self.request.chmod(0o600)
        with mock.patch("tools.dtc_batch_request.dtc_batch.main") as run:
            code = dtc_batch_request.main(
                [
                    "--request-file",
                    str(self.request),
                    "--current-file",
                    str(self.current),
                    "--cancel-dir",
                    str(self.cancel_dir),
                ]
            )
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertFalse(self.request.exists())
        self.assertEqual(
            len(list(self.root.glob(".request.json.claiming-*"))),
            1,
        )

    def test_stale_request_is_quarantined_off_watched_path(self):
        queue_request(
            self.request,
            build_request("dtc-web-stale", now=100.0),
            now=100.0,
        )
        with (
            mock.patch("lib.dtc_web.time.time", return_value=401.0),
            mock.patch("tools.dtc_batch_request.dtc_batch.main") as run,
        ):
            code = dtc_batch_request.main(
                [
                    "--request-file", str(self.request),
                    "--current-file", str(self.current),
                    "--cancel-dir", str(self.cancel_dir),
                ]
            )
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertFalse(self.request.exists())
        self.assertEqual(
            len(list(self.root.glob(".request.json.claiming-*"))),
            1,
        )

    def test_runtime_cancel_is_bridged_only_after_job_ledger_exists(self):
        queue_request(
            self.request,
            build_request("dtc-web-cancel", now=time.time()),
        )
        ready = threading.Event()

        def fake_main(argv):
            job_id = argv[argv.index("--job-id") + 1]
            root = Path(argv[argv.index("--job-root") + 1])
            store = JobStore(root, job_id)
            store.directory.mkdir(parents=True)
            atomic_json(
                store.record_path,
                {
                    "job_id": job_id,
                    "state": "running",
                    "modules": [],
                    "progress": {},
                    "cancel_requested": False,
                },
            )
            ready.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not store.cancellation_requested():
                time.sleep(0.02)
            return 1

        result = []
        with mock.patch(
            "tools.dtc_batch_request.dtc_batch.main",
            side_effect=fake_main,
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    dtc_batch_request.main(
                        [
                            "--request-file", str(self.request),
                            "--current-file", str(self.current),
                            "--cancel-dir", str(self.cancel_dir),
                            "--job-root", str(self.jobs),
                        ]
                    )
                )
            )
            thread.start()
            self.assertTrue(ready.wait(1))
            cancel = self.cancel_dir / "dtc-web-cancel.json"
            queue_cancel_request(cancel, "dtc-web-cancel")
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [1])
        self.assertTrue(
            JobStore(self.jobs, "dtc-web-cancel").cancellation_requested()
        )
        self.assertFalse(cancel.exists())


if __name__ == "__main__":
    unittest.main()
