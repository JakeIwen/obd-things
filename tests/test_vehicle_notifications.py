import subprocess
import unittest
from types import SimpleNamespace

from projects.vehicle_data.notifications import NtfyAdvisoryNotificationSink


class NtfyAdvisoryNotificationSinkTests(unittest.TestCase):
    def payload(self, **updates):
        payload = {
            "advisory": True,
            "episode_id": 42,
            "category": "vehicle_health",
            "title": "Oil pressure below operating minimum",
            "state": "warning",
            "reason": "Two fresh post-grace observations were below 12 psi.",
            "evaluated_at": "2026-08-22T02:00:00+00:00",
            "assessment": {"severity": "critical"},
        }
        payload.update(updates)
        return payload

    def test_uses_fixed_argv_and_queue_aware_helper(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stderr="")

        sink = NtfyAdvisoryNotificationSink(
            "van-telemetry",
            executable="/safe/ntfy-send",
            run=run,
        )
        sink.deliver(self.payload())

        argv, kwargs = calls[0]
        self.assertEqual(argv[0], "/safe/ntfy-send")
        self.assertIn("van-telemetry", argv)
        self.assertIn("max", argv)
        self.assertNotIn("shell", kwargs)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["env"]["NTFY_TIMEOUT"], "2")

    def test_rejects_nonwarning_payload_and_unsafe_topic(self):
        with self.assertRaises(ValueError):
            NtfyAdvisoryNotificationSink("../topic")
        sink = NtfyAdvisoryNotificationSink("van-telemetry", run=lambda *_a, **_k: None)
        with self.assertRaisesRegex(ValueError, "warning advisory"):
            sink.deliver(self.payload(state="normal"))

    def test_nonzero_helper_exit_is_a_dispatch_failure(self):
        sink = NtfyAdvisoryNotificationSink(
            "van-telemetry",
            run=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=2,
                stderr="configuration unavailable",
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "exited 2"):
            sink.deliver(self.payload())


if __name__ == "__main__":
    unittest.main()
