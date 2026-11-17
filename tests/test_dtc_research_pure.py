"""DTC research's pure parts: the contract, the report reader, the context and the ledger."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from projects.vehicle_data import dtc_research_context as context
from projects.vehicle_data.dtc_research_contract import (
    CONTRACT, ResearchError, encoded_len, relay_allowed, truncate_encoded, validate_post,
)
from projects.vehicle_data.dtc_research_ledger import Ledger
from projects.vehicle_data.dtc_research_prompt import (
    REPORT_SECTIONS, VIN_MASK, first_prompt, follow_up_prompt, mask_vin, parse_report, replay_prompt,
)

REQUEST_ID = "a" * 32
VIN_LIKE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


class ContractTests(unittest.TestCase):
    def test_vocabulary_matches_the_dashboard_block_names(self):
        # The dashboard's RESEARCH block reads these keys (dashboard/src/dtcResearch.helpers.js).
        self.assertEqual(set(CONTRACT["routes"]) >= {"models", "list", "codePrefix", "runPrefix", "post"}, True)
        for key in ("actions", "states", "modes", "summaryNotes", "progressPollMs", "modelsPollMs",
                    "modelsPollMax", "questionMax", "summaryMax", "patterns", "errorReasons", "turnReasons"):
            self.assertIn(key, CONTRACT)
        self.assertEqual(CONTRACT["errorReasons"]["busy"], 429)
        self.assertIn("memory_exhausted", CONTRACT["turnReasons"])

    def test_exact_bodies(self):
        start = {"action": "start", "module_key": "radar_acc", "fca_display": "C1418-78", "provider": "claude",
                 "model": "opus", "effort": "high", "request_id": REQUEST_ID}
        self.assertEqual(validate_post(start)[0], "start")
        for bad in ({**start, "extra": 1}, {**start, "fca_display": "c1418-78"}, {**start, "provider": "x"},
                    {**start, "effort": "huge"}, {"action": "ask", "run_id": "b" * 32, "question": " ",
                                                  "request_id": REQUEST_ID},
                    {"action": "ask", "run_id": "b" * 32, "question": "x" * 2001, "request_id": REQUEST_ID},
                    {"action": "launch"}):
            with self.subTest(bad=bad), self.assertRaises(ResearchError) as caught:
                validate_post(bad)
            self.assertEqual(caught.exception.reason, "invalid_request")
        action, fields = validate_post({"action": "ask", "run_id": "b" * 32, "question": " why?\x07 ",
                                        "request_id": REQUEST_ID})
        self.assertEqual(fields["question"], "why?")

    def test_question_caps_count_characters_and_encoded_bytes(self):
        ask = {"action": "ask", "run_id": "b" * 32, "request_id": REQUEST_ID}
        self.assertEqual(validate_post({**ask, "question": "é" * 2000})[1]["question"], "é" * 2000)  # 12,000 B
        with self.assertRaises(ResearchError):  # 1,500 characters but 18,000 encoded bytes
            validate_post({**ask, "question": "\U0001F600" * 1500})

    def test_every_error_reason_the_backend_raises_is_in_the_contract(self):
        import ast
        from projects.vehicle_data import dtc_research
        root = Path(__file__).resolve().parents[1] / "projects" / "vehicle_data"
        raised = set()
        for name in ("dtc_research.py", "dtc_research_contract.py", "dtc_research_service.py", "dtc_research_store.py"):
            for node in ast.walk(ast.parse((root / name).read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ResearchError" \
                        and node.args and isinstance(node.args[0], ast.Constant):
                    raised.add(node.args[0].value)
        self.assertTrue(raised)
        self.assertLessEqual(raised | set(dtc_research.REFUSAL_TO_ERROR.values()), set(CONTRACT["errorReasons"]))

    def test_relay_forwards_exactly_the_research_routes(self):
        allowed = [("GET", "/v1/assistant/models"), ("GET", "/v1/assistant/dtc-research"),
                   ("POST", "/v1/assistant/dtc-research"), ("GET", "/v1/assistant/dtc-research/code/radar_acc/C1418-78"),
                   ("GET", "/v1/assistant/dtc-research/run/" + "c" * 32),
                   ("GET", "/v1/assistant/dtc-research/run/" + "c" * 32 + "/progress")]
        refused = [("POST", "/v1/assistant/models"), ("GET", "/v1/assistant/dtc-research/other"),
                   ("POST", "/v1/assistant/dtc-research/run/" + "c" * 32), ("DELETE", "/v1/assistant/dtc-research")]
        for method, path in allowed:
            self.assertTrue(relay_allowed(method, path), path)
        for method, path in refused:
            self.assertFalse(relay_allowed(method, path), path)

    def test_truncation_is_by_encoded_size(self):
        text = "\U0001F600" * 5000  # 12 encoded bytes each
        cut, truncated = truncate_encoded(text, 4096)
        self.assertTrue(truncated)
        self.assertLessEqual(encoded_len(cut), 4096)
        self.assertEqual(truncate_encoded("short", 4096), ("short", False))


class ReportTests(unittest.TestCase):
    def test_summary_forms(self):
        body = "\n".join(f"## {name}\ntext" for name in REPORT_SECTIONS)
        good = parse_report("SUMMARY: The radar needs alignment.\n" + body)
        self.assertEqual((good["summary"], good["summary_note"], good["format_warnings"]),
                         ("The radar needs alignment.", None, []))
        self.assertNotIn("SUMMARY", good["body"])
        bold = parse_report("**SUMMARY:** *Loose* connector.\n## What it means\nx")
        self.assertEqual(bold["summary"], "Loose connector.")
        self.assertIn("Sources", bold["format_warnings"])
        long = parse_report("SUMMARY: " + "word " * 60 + "\n" + body)
        self.assertEqual(long["summary_note"], "truncated")
        self.assertLessEqual(len(long["summary"]), 140)
        missing = parse_report("No summary here.\n" + body)
        self.assertEqual((missing["summary"], missing["summary_note"]), (None, "missing"))
        self.assertTrue(missing["body"].startswith("No summary"))

    def test_replay_prompt_drops_oldest_follow_ups_but_keeps_the_report(self):
        exchanges = [(f"q{n}", "a" * 30000) for n in range(10)]
        prompt = replay_prompt({"code": {}}, "REPORT TEXT", exchanges, "latest?")
        self.assertLessEqual(len(prompt.encode()), CONTRACT["sizes"]["replayPromptBytes"])
        self.assertIn("REPORT TEXT", prompt)
        self.assertIn("latest?", prompt)
        self.assertNotIn("OWNER ASKED\nq0", prompt)
        self.assertIn("omitted", prompt)


class Broker:
    def __init__(self, dtcs, ignores=None):
        self.dtcs, self.ignores = dtcs, ignores

    def request(self, method, path):
        if path == "/v1/diagnostics/dtcs":
            return 200, self.dtcs
        if path == "/v1/diagnostics/dtc-ignores" and self.ignores is not None:
            return 200, self.ignores
        raise OSError("down")


class VinMaskTests(unittest.TestCase):
    VIN = "1C6TRVDG0NE123456"  # synthetic

    def prompts(self, vin):
        context = {"owner_code_note": f"van {vin}"}
        return [first_prompt(context), follow_up_prompt(f"is {vin} recalled?"),
                replay_prompt(context, f"report {vin}", [(f"ask {vin}", f"answer {vin}")], f"now {vin}?")]

    def test_a_vin_in_any_case_never_leaves_the_pi_in_any_prompt(self):
        for vin in (self.VIN, self.VIN.lower(), "1c6TRVdg0nE123456"):
            for prompt in self.prompts(vin):
                self.assertNotIn(vin.lower(), prompt.lower())
                self.assertIn(VIN_MASK, prompt)
        self.assertNotIn(self.VIN.lower(), json.dumps(mask_vin({"a": [self.VIN.lower()]})))

    def test_the_vans_own_vin_is_masked_with_separators_and_any_case(self):
        with mock.patch.dict(os.environ, {"OBD_VIN": self.VIN}):
            for typed in ("1c6 trvdg 0ne 123456", "1C6-TRVDG-0NE-123456", "1c6trvdg0ne123456", "1C6TRVDG0NE 123456"):
                self.assertEqual(follow_up_prompt(f"my van is {typed}."), f"my van is {VIN_MASK}.")
            self.assertEqual(follow_up_prompt("1C6TRVDG0NE1234567"), "1C6TRVDG0NE1234567")  # a longer token

    def test_ordinary_text_is_left_alone(self):
        with mock.patch.dict(os.environ, {"OBD_VIN": self.VIN}):
            for text in ("part 1C6TRVDG0NE1234Q6 and 68XIO1234567890AB", "see https://example.com/tsb/radar-alignment-2021/18-045-21.pdf?x=1c6-trvdg-0ne",
                         "C1418-78 and B1040-64 then C1512 88 with U0100 and nothing else",
                         "abcdefghjklmnprst is a lower-case run without a digit"):
                self.assertEqual(follow_up_prompt(text), text)

    def test_a_spaced_vin_is_not_guessed_at_when_the_vans_vin_is_unknown(self):
        text = "1C6 TRVDG 0NE 123456 typed with spaces"  # any 17 letters and digits in groups would match prose
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(follow_up_prompt(text), text)


class ContextTests(unittest.TestCase):
    def test_record_found_in_each_source_in_order(self):
        scan = {"in_vehicle_scan": {"modules": [{"module_key": "bcm_ccan", "dtcs": [
            {"fca_display": "B1632-15", "status": "09", "observed_at": "2026-11-01T00:00:00Z"}]}]}}
        self.assertEqual(context.find_record(scan, "bcm_ccan", "B1632-15")[0], "in_vehicle_scan")
        both = {**scan, "groups": {"current": [{"module_key": "bcm_ccan", "fca_display": "B1632-15"}]}}
        self.assertEqual(context.find_record(both, "bcm_ccan", "B1632-15")[0], "pi_scan")
        self.assertEqual(context.find_record(both, "bcm_ccan", "B9999-15"), (None, None))

    def test_gathered_context_masks_vins_and_takes_no_browser_text(self):
        vin = "1C6TRVDG0NE123456"
        broker = Broker({"groups": {"current": [{"module_key": "bcm_ccan", "fca_display": "B1632-15",
                                                 "module_name": f"BCM {vin}"}]}},
                        {"available": True, "ignored": [{"module_key": "bcm_ccan", "fca_display": "B1632-15",
                                                         "note": f"LED lamps {vin}"}]})
        gathered = context.gather(broker, "bcm_ccan", "B1632-15")
        text = json.dumps(gathered)
        self.assertNotIn(vin, text)
        self.assertEqual(gathered["owner_ignore_note"], "LED lamps [vehicle identifier omitted]")
        self.assertIn("LED", gathered["owner_code_note"])
        self.assertEqual(gathered["vehicle"]["model"], "2022 Ram ProMaster 2500")
        down = context.gather(Broker({}), "radar_acc", "C1418-78")
        self.assertIsNone(down["record"])
        self.assertIsNotNone(down["record_note"])

    def test_facts_file_is_hand_reviewed_shape_only(self):
        facts = context.FACTS
        self.assertFalse(VIN_LIKE.search(json.dumps(facts)))
        self.assertFalse(re.search(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}", json.dumps(facts)))
        for key in facts["code_notes"]:
            module_key, code = key.split(":")
            self.assertRegex(module_key, CONTRACT["patterns"]["moduleKey"])
            self.assertRegex(code, CONTRACT["patterns"]["code"])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "admissions.json"
        self.now = datetime(2026, 11, 16, 23, 59, 59, 999999, tzinfo=timezone.utc)  # longest timestamps

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self):
        return Ledger(self.path, clock=lambda: self.now)

    def test_counted_launches_survive_restarts_and_refusals_never_count(self):
        for n in range(59):
            self.ledger().admit("r" * 32, f"{n:032x}", "initial")
        for n in range(500):
            ledger = self.ledger()
            ledger.admit("r" * 32, f"{n + 1000:032x}", "initial")
            ledger.refused(f"{n + 1000:032x}")
        self.assertEqual(len(json.loads(self.path.read_text())["launches"]), 59)
        self.ledger().admit("r" * 32, "f" * 32, "replay")
        with self.assertRaises(ResearchError) as limit:
            self.ledger().admit("r" * 32, "e" * 32, "initial")
        self.assertEqual(limit.exception.reason, "daily_limit")
        self.assertLessEqual(self.path.stat().st_size, 12 * 1024)  # the documented bound (worst case ~9.1 KiB)
        self.now += timedelta(hours=24, seconds=1)
        self.ledger().admit("r" * 32, "d" * 32, "initial")
        self.assertEqual(self.ledger().count(), 1)

    def test_unreadable_or_unwritable_ledger_refuses(self):
        self.path.write_text("not json")
        with self.assertRaises(ResearchError) as unreadable:
            self.ledger().admit("r" * 32, "a" * 32, "initial")
        self.assertEqual(unreadable.exception.reason, "storage_error")
        self.assertEqual(self.path.read_text(), "not json")
        self.path.unlink()
        with mock.patch("projects.vehicle_data.dtc_research_ledger._atomic_json", side_effect=OSError("full")):
            with self.assertRaises(ResearchError) as unwritable:
                self.ledger().admit("r" * 32, "a" * 32, "initial")
        self.assertEqual(unwritable.exception.reason, "storage_error")


if __name__ == "__main__":
    unittest.main()
