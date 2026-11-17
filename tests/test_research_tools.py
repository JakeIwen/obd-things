"""Owner-run research helpers: the peak sampler, the sign-in saver and the Codex pure readers."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import dtc_research_signin, measure_assistant_peak
from projects.vehicle_data.cli_process import StopRun
from projects.vehicle_data.research_cli import CodexStream, codex_argv, codex_env, offered_local_tools
from tests.research_support import proc_entry


class PeakSamplerTests(unittest.TestCase):
    def test_peaks_cover_descendants_not_the_advisor_and_the_run_ends_after_the_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, meminfo = Path(tmp) / "proc", Path(tmp) / "meminfo"
            proc_entry(root, 10, children=[11], rss_kib=999999, hwm_kib=999999)  # the advisor itself
            proc_entry(root, 11, children=[12], rss_kib=100 * 1024, hwm_kib=120 * 1024)
            proc_entry(root, 12, rss_kib=50 * 1024, hwm_kib=60 * 1024)
            meminfo.write_text("MemAvailable: 900000 kB\n")
            now, ticks = [0.0], []

            def sleep(seconds):
                now[0] += seconds
                ticks.append(now[0])
                meminfo.write_text(f"MemAvailable: {(900 - len(ticks) * 100) * 1024} kB\n")
                if len(ticks) == 3:  # the child has gone
                    (root / "10" / "task" / "10" / "children").write_text("")

            result = measure_assistant_peak.sample(10, 600, proc_root=root, meminfo=meminfo,
                                                   clock=lambda: now[0], sleep=sleep)
        self.assertTrue(result["child_seen"])
        self.assertEqual((result["peak_tree_rss_mib"], result["peak_tree_hwm_mib"]), (150.0, 180.0))
        self.assertLess(result["lowest_available_mib"], 900000 / 1024)
        self.assertLess(now[0], 20)  # stopped after the settle period, not at 600 s

    def test_no_child_reports_not_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, meminfo = Path(tmp) / "proc", Path(tmp) / "meminfo"
            proc_entry(root, 10)
            meminfo.write_text("MemAvailable: 1024 kB\n")
            now = [0.0]
            result = measure_assistant_peak.sample(10, 1, proc_root=root, meminfo=meminfo, clock=lambda: now[0],
                                                   sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertFalse(result["child_seen"])
        self.assertEqual(result["peak_tree_rss_mib"], 0)


class SigninTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.volume = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_token_writes_mode_0600_and_replaces_atomically(self):
        path = dtc_research_signin.save_token(self.volume, "  sk-ant-oat01_abc.DEF-1\n")
        self.assertEqual(path.read_text(), "sk-ant-oat01_abc.DEF-1\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        dtc_research_signin.save_token(self.volume, "second")
        self.assertEqual(path.read_text(), "second\n")
        self.assertEqual(sorted(p.name for p in self.volume.iterdir()), ["claude-oauth-token"])

    def test_bad_tokens_and_missing_volume_are_refused_without_echoing_the_token(self):
        for bad in ("", "has space", "semi;colon", "x" * 5000):
            with self.subTest(bad[:12]), self.assertRaises(ValueError) as refused:
                dtc_research_signin.save_token(self.volume, bad)
            self.assertNotIn("semi", str(refused.exception))
        with self.assertRaises(ValueError):
            dtc_research_signin.save_token(self.volume / "absent", "token")
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_main_never_prints_the_token_and_status_says_who_is_signed_in(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass", return_value="secret-token-value"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(dtc_research_signin.main(["claude", "--volume", str(self.volume)]), 0)
        self.assertNotIn("secret-token-value", out.getvalue() + err.getvalue())
        with contextlib.redirect_stdout(out):
            dtc_research_signin.main(["status", "--volume", str(self.volume)])
        self.assertIn("Claude: signed in", out.getvalue())
        self.assertIn("Codex: not signed in", out.getvalue())
        with mock.patch("getpass.getpass", return_value="bad token"), contextlib.redirect_stderr(err):
            self.assertEqual(dtc_research_signin.main(["claude", "--volume", str(self.volume)]), 1)


def rollout(*items):
    return [json.dumps({"type": "response_item", "payload": item}) for item in items]


class CodexPureTests(unittest.TestCase):
    def test_argv_for_a_new_thread_and_a_resume(self):
        argv = codex_argv("/opt/codex", workdir="/w", model="gpt-x", effort="high", log_dir="/l")
        self.assertEqual(argv[:2], ["/opt/codex", "exec"])
        self.assertEqual(argv[-1], "-")
        self.assertIn("-C", argv)
        self.assertIn('web_search="live"', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertTrue(any(a.startswith("permissions.dtc-research.filesystem={") for a in argv))
        self.assertIn("--ignore-user-config", argv)
        resumed = codex_argv("/opt/codex", workdir="/w", model="gpt-x", effort=None, log_dir="/l", thread_id="t-1")
        self.assertEqual(resumed[2], "resume")
        self.assertEqual(resumed[-2:], ["t-1", "-"])
        self.assertNotIn("-C", resumed)
        self.assertFalse(any(a.startswith("model_reasoning_effort") for a in resumed))

    def test_env_carries_codex_home_and_no_provider_keys(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "leak"}):
            env = codex_env("/h", "/t", "/c")
        self.assertEqual((env["HOME"], env["TMPDIR"], env["CODEX_HOME"]), ("/h", "/t", "/c"))
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_offered_local_tools_flattens_namespaces_and_never_reads_a_bad_record_as_empty(self):
        good = rollout({"type": "additional_tools", "tools": [
            {"type": "function", "name": "exec"},
            {"type": "namespace", "name": "clock", "tools": [{"type": "function", "name": "sleep"}]}]})
        self.assertEqual(offered_local_tools(good), ["exec", "clock.sleep"])
        for bad in ([], rollout({"type": "other"}), rollout({"type": "additional_tools", "tools": "x"}),
                    rollout({"type": "additional_tools", "tools": [{"type": "function"}]}),
                    rollout({"type": "additional_tools", "tools": [{"type": "namespace", "name": "n"}]})):
            with self.subTest(bad), self.assertRaises(ValueError):
                offered_local_tools(bad)

    def test_stream_collects_answer_and_stops_on_items_outside_policy(self):
        stream = CodexStream("t-1")
        stream({"type": "thread.started", "thread_id": "t-1"})
        stream({"type": "item.started", "item": {"type": "web_search"}})
        stream({"type": "item.completed", "item": {"type": "agent_message", "text": "SUMMARY: x"}})
        stream({"type": "turn.completed"})
        self.assertEqual((stream.searches, stream.answer, stream.completed), (1, "SUMMARY: x", True))
        with self.assertRaises(StopRun) as other:
            CodexStream()({"type": "item.started", "item": {"type": "command_execution"}})
        self.assertEqual(other.exception.reason, "policy_violation")
        with self.assertRaises(StopRun) as wrong:
            CodexStream("t-1")({"type": "thread.started", "thread_id": "t-2"})
        self.assertEqual(wrong.exception.reason, "resume_unverified")


if __name__ == "__main__":
    unittest.main()
