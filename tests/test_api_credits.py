"""Promotional API-credit rules: key reader, billing choice, failure classifier, spend tally, ledger."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from projects.vehicle_data.api_credits import (
    CreditEnd, Billing, UsageTally, choose_billing, classify_credit_failure, read_key_file, redact, scrub,
)
from projects.vehicle_data.api_credits_contract import BILLING_REASONS, UNTIL
from projects.vehicle_data.api_credits_ledger import CreditLedger, LedgerError
from projects.vehicle_data.research_runner import TurnRequest
from tests.research_support import FAKE_KEY, INSIDE_GRANTS


def request(**kwargs):
    fields = dict(run_id="r" * 32, provider="claude", model="sonnet", effort=None, kind="initial",
                  turn_kind="report", prompt="p", timeout_s=60, billing_capable=True)
    fields.update(kwargs)
    return TurnRequest(**fields)


def choose(req=None, record=None, now=INSIDE_GRANTS, **state):
    state = {"credits_allowed": True, "enabled": True, "key_installed": True, **state}
    return choose_billing(req or request(), record, now, **state)


class KeyReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, mode=0o600, name="key"):
        path = self.dir / name
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def test_accepts_the_bare_key_and_the_assignment_forms(self):
        for text in (FAKE_KEY + "\n", f"export ANTHROPIC_PROMO_API_KEY={FAKE_KEY}\n",
                     f"ANTHROPIC_PROMO_API_KEY='{FAKE_KEY}'\n"):
            with self.subTest(text[:20]):
                self.assertEqual(read_key_file(self.write(text)), (FAKE_KEY, None))

    def test_rejects_bad_modes_links_oversize_garbage_admin_and_oauth_keys_without_echoing_them(self):
        good = self.write(FAKE_KEY)
        link = self.dir / "link"
        link.symlink_to(good)
        cases = {"mode": self.write(FAKE_KEY, mode=0o644, name="open"), "link": link,
                 "big": self.write(FAKE_KEY + "\n" + "x" * 2000, name="big"),
                 "garbage": self.write("hello world", name="garbage"),
                 "two": self.write(FAKE_KEY + "\n" + FAKE_KEY, name="two"),
                 "admin": self.write("sk-ant-admin01-" + "A" * 40, name="admin"),
                 "oauth": self.write("sk-ant-oat01-" + "A" * 40, name="oauth"),
                 "short": self.write("sk-ant-api03-short", name="short"),
                 "directory": self.dir, "missing": self.dir / "absent"}
        for name, path in cases.items():
            with self.subTest(name):
                key, problem = read_key_file(path)
                self.assertIsNone(key)
                self.assertTrue(problem)
                self.assertNotIn("sk-ant", problem)

    def test_both_model_key_prefixes_are_accepted(self):
        # The owner's Console issues sk-ant-usr- model keys (verified on the API, 2026-11-19).
        for key in ("sk-ant-api03-" + "A1_b-" * 8, "sk-ant-usr-" + "A1_b-" * 19):
            with self.subTest(key[:11]):
                path = self.write(key, name=key[7:10])
                self.assertEqual(read_key_file(path), (key, None))
        for bad in ("sk-ant-usr01-" + "A" * 40, "sk-ant-usr-short", "sk-ant-user-" + "A" * 40):
            with self.subTest(bad[:12]):
                self.assertIsNone(read_key_file(self.write(bad, name="bad"))[0])

    def test_a_source_file_need_not_be_private_but_the_installed_copy_must_be(self):
        path = self.write(FAKE_KEY, mode=0o644)
        self.assertEqual(read_key_file(path, require_private=False)[0], FAKE_KEY)
        self.assertIsNone(read_key_file(path)[0])


class ChooseBillingTests(unittest.TestCase):
    def test_every_billing_reason(self):
        cases = [
            (dict(req=request(billing_capable=False)), "legacy"),
            (dict(req=request(forced_billing="subscription")), "forced"),
            (dict(credits_allowed=False), "ineligible"),
            (dict(enabled=False), "disabled"),
            (dict(key_installed=False), "no_key"),
            (dict(now=UNTIL), "expired"),
            (dict(latches={"account": "exhausted", "models": {}}), "unavailable"),
            (dict(latches={"account": None, "models": {"sonnet": "model_unavailable"}}), "unavailable"),
            (dict(ledger_reason="ledger_unavailable"), "ledger_unavailable"),
            (dict(ledger_reason="ledger_full"), "ledger_full"),
        ]
        for state, reason in cases:
            with self.subTest(reason):
                req = state.pop("req", None)
                self.assertEqual(choose(req, None, **state), Billing("subscription", reason))
        self.assertEqual({reason for _, reason in cases}, set(BILLING_REASONS))
        self.assertEqual(choose(), Billing("api_credits"))

    def test_expired_outranks_a_latch_and_a_model_latch_is_per_model(self):
        latched = {"account": "exhausted", "models": {}}
        self.assertEqual(choose(now=UNTIL + timedelta(hours=1), latches=latched).reason, "expired")
        models = {"account": None, "models": {"opus": "model_unavailable"}}
        self.assertEqual(choose(request(model="sonnet"), latches=models).choice, "api_credits")
        self.assertEqual(choose(request(model="opus"), latches=models).reason, "unavailable")

    def test_forced_and_legacy_beat_disabled_and_ineligible_reasons(self):
        self.assertEqual(choose(request(forced_billing="subscription"), enabled=False).reason, "forced")
        self.assertEqual(choose(request(billing_capable=False), enabled=False).reason, "legacy")

    def test_resume_follows_its_sessions_origin(self):
        resume = request(kind="resume", session_id="s")
        # A subscription-created session (or a record with no billing) stays on the subscription.
        self.assertEqual(choose(resume, {"billing": "subscription"}), Billing("subscription", "ineligible"))
        self.assertEqual(choose(resume, {}), Billing("subscription", "ineligible"))
        # An API-created session resumes only on credits.
        self.assertEqual(choose(resume, {"billing": "api_credits"}), Billing("api_credits"))
        for state, reason in (({"enabled": False}, "disabled"), ({"key_installed": False}, "no_key"),
                              ({"now": UNTIL}, "expired"), ({"ledger_reason": "ledger_full"}, "ledger_full"),
                              ({"latches": {"account": "invalid_key", "models": {}}}, "unavailable")):
            with self.subTest(reason):
                got = choose(resume, {"billing": "api_credits"}, **state)
                self.assertEqual(got, Billing("replay_required", reason))
        forced = request(kind="resume", session_id="s", forced_billing="subscription")
        self.assertEqual(choose(forced, {"billing": "api_credits"}).choice, "replay_required")

    def test_old_protocol_never_gets_replay_required(self):
        old = request(kind="resume", session_id="s", billing_capable=False)
        self.assertEqual(choose(old, {"billing": "api_credits"}).choice, "session_missing")
        self.assertEqual(choose(old, {"billing": "subscription"}), Billing("subscription", "legacy"))


def end(**kwargs):
    fields = dict(stop_reason=None, returncode=1, complete=False, result=None, typed_error=None, init_seen=True,
                  stderr_tail="")
    fields.update(kwargs)
    return CreditEnd(**fields)


def failed(text=None, errors=None, subtype="error_during_execution"):
    result = {"type": "result", "subtype": subtype, "is_error": True}
    if text is not None:
        result["result"] = text
    if errors is not None:
        result["errors"] = errors
    return result


class ClassifierTests(unittest.TestCase):
    def test_positive_cases_by_typed_error_text_and_errors_array(self):
        cases = [
            (end(typed_error="billing_error", result=failed("x")), "exhausted"),
            (end(typed_error="authentication_failed", result=failed("x")), "invalid_key"),
            (end(typed_error="model_not_found", result=failed("x")), "model_unavailable"),
            (end(result=failed("Your credit balance is too low to access the API")), "exhausted"),
            (end(result=failed("401 invalid x-api-key")), "invalid_key"),
            (end(result=failed(errors=["authentication_error: bad key"])), "invalid_key"),
            (end(result=failed(errors=["model: sonnet-9 does not exist"])), "model_unavailable"),
            (end(result=failed(errors=["Purchase more credits at Plans & Billing"])), "exhausted"),
        ]
        for case, code in cases:
            with self.subTest(code=code, case=str(case.result)[:50]):
                self.assertEqual(classify_credit_failure(case), code)

    def test_negative_cases_are_not_credit_failures(self):
        cases = {
            "success": end(returncode=0, complete=True, result={"subtype": "success", "is_error": False}),
            "unrelated error": end(result=failed("the tool crashed")),
            "bare 403 and billing in text": end(result=failed("fetched 403 page about billing and permission_error")),
            "budget": end(typed_error="billing_error", result=failed("x", subtype="error_max_budget_usd")),
            "cancelled": end(stop_reason="cancelled", typed_error="billing_error", result=failed("credit balance is too low")),
            "timeout": end(stop_reason="timeout", result=failed("credit balance is too low")),
            "policy": end(stop_reason="policy_violation", result=failed("credit balance is too low")),
            "protocol": end(stop_reason="protocol_error"),
            "typed error but no failed end": end(typed_error="billing_error", returncode=0, result={
                "subtype": "success", "is_error": False}),
            "recovered": end(typed_error="billing_error", returncode=0, complete=True,
                             result={"subtype": "success", "is_error": False, "result": "fine"}),
            "stderr after init is not read": end(stderr_tail="credit balance is too low", result=failed("x")),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertIsNone(classify_credit_failure(case))

    def test_terminal_overload_and_rate_limit_outrank_an_earlier_assistant_error(self):
        for text in ("API Error: overloaded_error", "rate_limit_error: 429", "Overloaded (529)"):
            with self.subTest(text):
                self.assertIsNone(classify_credit_failure(end(typed_error="billing_error", result=failed(text))))

    def test_the_workers_own_key_ignored_stop_is_a_credit_failure(self):
        self.assertEqual(classify_credit_failure(end(stop_reason="key_ignored", returncode=-9, init_seen=True)),
                         "key_ignored")

    def test_pre_init_failures(self):
        silent = end(init_seen=False, returncode=1, stderr_tail="", result=failed())
        self.assertEqual(classify_credit_failure(silent), "invalid_key")
        self.assertEqual(classify_credit_failure(end(init_seen=False, stderr_tail="Invalid API key provided")),
                         "invalid_key")
        self.assertEqual(classify_credit_failure(end(init_seen=False, stderr_tail="Credit balance is too low")),
                         "exhausted")
        self.assertIsNone(classify_credit_failure(end(init_seen=False, stderr_tail="getaddrinfo ENOTFOUND")))
        # A clean pre-init exit is not a failure at all.
        self.assertIsNone(classify_credit_failure(end(init_seen=False, returncode=0)))
        # With the failure after init, the stderr tail is ignored.
        self.assertIsNone(classify_credit_failure(end(init_seen=True, stderr_tail="", returncode=1)))

    def test_pre_init_deaths_and_malformed_results_never_latch_a_healthy_key(self):
        cases = {"no result, exit 1": end(init_seen=False, returncode=1),
                 "signal death": end(init_seen=False, returncode=-9),
                 "bare result": end(init_seen=False, returncode=1, result={"type": "result"}),
                 "result with a null subtype": end(init_seen=False, returncode=1,
                                                   result={"type": "result", "subtype": None}),
                 "explicit success": end(init_seen=False, returncode=0, result={
                     "type": "result", "subtype": "success", "is_error": False})}
        for name, case in cases.items():
            with self.subTest(name):
                self.assertIsNone(classify_credit_failure(case))
        for result in (failed(), {"type": "result", "is_error": True}, {"type": "result", "subtype": "error_x"}):
            self.assertEqual(classify_credit_failure(end(init_seen=False, result=result)), "invalid_key")

    def test_terminal_key_refusal_codes_are_invalid_key_by_structure_or_exact_text(self):
        for field in ("api_error_code", "api_error"):
            for code in ("api_key_auth_disabled", "org_disabled_credential"):
                with self.subTest(field=field, code=code):
                    result = {**failed("x"), field: code, "api_error_status": 403}
                    self.assertEqual(classify_credit_failure(end(result=result)), "invalid_key")
        nested = {**failed("x"), "error": {"api_error": "org_disabled_credential"}}
        self.assertEqual(classify_credit_failure(end(result=nested)), "invalid_key")
        for text in ("Your organization has disabled API key authentication",
                     "Your ANTHROPIC_API_KEY belongs to a disabled organization"):
            self.assertEqual(classify_credit_failure(end(result=failed(text))), "invalid_key")
        self.assertIsNone(classify_credit_failure(end(result={**failed("forbidden"), "api_error_status": 403})))

    def test_structural_rate_limit_and_overload_outrank_a_remembered_assistant_error(self):
        for extra in ({"api_error_type": "rate_limit_error"}, {"api_error_type": "overloaded_error"},
                      {"api_error_status": 429}, {"api_error_status": 529}):
            with self.subTest(extra):
                result = {**failed("an opaque message"), **extra}
                self.assertIsNone(classify_credit_failure(end(typed_error="billing_error", result=result)))

    def test_redact_removes_the_exact_key_and_anything_shaped_like_one(self):
        self.assertEqual(redact("odd-secret-value here", "odd-secret-value"), "[key] here")
        self.assertEqual(scrub({"a": ["x odd-secret", {"b": "sk-ant-api03-zzzz"}], "n": 3}, "odd-secret"),
                         {"a": ["x [key]", {"b": "[key]"}], "n": 3})

    def test_redact_removes_anything_shaped_like_a_key(self):
        self.assertEqual(redact(f"boom {FAKE_KEY} and sk-ant-oat01-abc"), "boom [key] and [key]")


class UsageTallyTests(unittest.TestCase):
    def result(self, index, **costs):
        event = {"type": "result", "result_index": index}
        if costs is not None:
            event["model_usage_since_last_result"] = {m: {"costUSD": c} for m, c in costs.items()}
        return event

    def test_sums_the_deltas_and_never_reads_the_cumulative_total(self):
        tally = UsageTally()
        event = self.result(0, sonnet=0.25)
        event["total_cost_usd"] = 99.0  # restored from the transcript on resume; never charged
        tally.add(event)
        self.assertEqual(tally.settled(clean_exit=True), (0.25, False))

    def test_a_duplicate_index_adds_nothing_and_a_gap_is_unknown(self):
        tally = UsageTally()
        tally.add(self.result(0, sonnet=0.1))
        tally.add(self.result(0, sonnet=0.1))
        self.assertEqual(tally.settled(clean_exit=True), (0.1, False))
        tally.add(self.result(2, sonnet=0.2))
        self.assertEqual(tally.settled(clean_exit=True), (0.3, True))

    def test_first_result_not_zero_is_a_gap(self):
        tally = UsageTally()
        tally.add(self.result(1, sonnet=0.1))
        self.assertTrue(tally.settled(clean_exit=True)[1])

    def test_invalid_or_missing_delta_no_result_or_an_interrupted_process_is_unknown(self):
        for event in ({"result_index": 0}, {"result_index": 0, "model_usage_since_last_result": {"a": {}}},
                      {"result_index": 0, "model_usage_since_last_result": {"a": {"costUSD": -1}}},
                      {"result_index": 0, "model_usage_since_last_result": {"a": {"costUSD": True}}},
                      {"result_index": 0, "model_usage_since_last_result": []},
                      {"model_usage_since_last_result": {"a": {"costUSD": 1}}}):
            with self.subTest(event):
                tally = UsageTally()
                tally.add(event)
                self.assertTrue(tally.settled(clean_exit=True)[1])
        self.assertTrue(UsageTally().settled(clean_exit=True)[1])
        tally = UsageTally()
        tally.add(self.result(0, sonnet=0.4))
        self.assertEqual(tally.settled(clean_exit=False), (0.4, True))  # killed after an earlier result


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "api-credits.json"
        self.now = INSIDE_GRANTS
        self.writes = []

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self, **kwargs):
        ledger = CreditLedger(self.path, now=lambda: self.now, **kwargs)
        ledger.start()
        return ledger

    def test_reserve_settle_withdraw_round_trip_through_the_file(self):
        ledger = self.ledger()
        ledger.reserve("a" * 32, "sonnet")
        self.assertEqual(json.loads(self.path.read_text())["entries"][0]["state"], "open")
        ledger.settle("a" * 32, 0.5, False)
        entry = ledger.snapshot()["entries"][0]
        self.assertEqual((entry["state"], entry["known_usd"], entry["unknown"]), ("settled", 0.5, False))
        ledger.reserve("b" * 32, "opus")
        ledger.withdraw("b" * 32)
        self.assertEqual([e["attempt_id"] for e in ledger.snapshot()["entries"]], ["a" * 32])

    def test_the_durable_write_fsyncs_the_directory(self):
        from unittest import mock
        with mock.patch("lib.reports._fsync_directory") as synced:
            self.ledger().reserve("a" * 32, "sonnet")
        synced.assert_called_with(self.path.parent)

    def test_a_failed_reservation_write_raises_and_leaves_no_entry(self):
        ledger = self.ledger()
        ledger._write = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(LedgerError) as failed:
            ledger.reserve("a" * 32, "sonnet")
        self.assertEqual(failed.exception.reason, "ledger_unavailable")
        self.assertEqual(ledger.snapshot()["entries"], [])

    def test_a_failed_settlement_leaves_the_entry_open_and_the_next_start_settles_it_unknown(self):
        ledger = self.ledger()
        ledger.reserve("a" * 32, "sonnet")
        ledger._write = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(LedgerError):
            ledger.settle("a" * 32, 0.5, False)
        self.assertEqual(ledger.snapshot()["entries"][0]["state"], "open")
        restarted = self.ledger()
        entry = restarted.snapshot()["entries"][0]
        self.assertEqual((entry["state"], entry["unknown"]), ("settled", True))

    def test_the_bound_refuses_new_launches_and_discards_nothing(self):
        record = {"schema_version": 1, "latches": {"account": None, "models": {}}, "last_fallback": None,
                  "entries": [{"attempt_id": f"{n:032x}", "model": "m", "started_at": "2026-11-20T00:00:00Z",
                               "settled_at": "2026-11-20T00:00:01Z", "known_usd": 0.1, "unknown": False,
                               "state": "settled"} for n in range(2000)]}
        self.path.write_text(json.dumps(record))
        ledger = self.ledger()
        self.assertTrue(ledger.full)
        with self.assertRaises(LedgerError) as full:
            ledger.reserve("f" * 32, "sonnet")
        self.assertEqual(full.exception.reason, "ledger_full")
        self.assertEqual(len(ledger.snapshot()["entries"]), 2000)
        self.assertTrue(ledger.snapshot()["ledger_full"])

    def test_latches_clear_only_on_restart_and_the_last_fallback_survives(self):
        ledger = self.ledger()
        ledger.latch("model_unavailable", "opus")
        ledger.latch("exhausted", "sonnet")
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["latches"], {"account": "exhausted", "models": {"opus": "model_unavailable"}})
        self.assertEqual(snapshot["last_fallback"]["code"], "exhausted")
        restarted = self.ledger().snapshot()
        self.assertEqual(restarted["latches"], {"account": None, "models": {}})
        self.assertEqual(restarted["last_fallback"]["model"], "sonnet")

    def test_entries_are_deleted_only_after_until_plus_thirty_days(self):
        ledger = self.ledger()
        ledger.reserve("a" * 32, "sonnet")
        ledger.settle("a" * 32, 0.1, False)
        self.now = UNTIL + timedelta(days=29)
        self.assertEqual(len(self.ledger().snapshot()["entries"]), 1)
        self.now = UNTIL + timedelta(days=31)
        self.assertEqual(self.ledger().snapshot()["entries"], [])

    def test_a_corrupt_file_makes_the_ledger_unavailable_and_is_not_overwritten(self):
        self.path.write_text("{not json")
        ledger = self.ledger()
        self.assertFalse(ledger.available)
        with self.assertRaises(LedgerError):
            ledger.reserve("a" * 32, "sonnet")
        self.assertEqual(self.path.read_text(), "{not json")

    def good_record(self, **changes):
        record = {"schema_version": 1, "latches": {"account": None, "models": {}}, "last_fallback": None,
                  "entries": [{"attempt_id": "a" * 32, "model": "sonnet", "started_at": "2026-11-20T00:00:00Z",
                               "settled_at": "2026-11-20T00:00:01Z", "known_usd": 0.1, "unknown": False,
                               "state": "settled"}]}
        record.update(changes)
        return record

    def test_any_schema_violation_leaves_the_file_untouched_and_the_ledger_unavailable(self):
        entry = self.good_record()["entries"][0]
        bad = {
            "entry that is not an object": self.good_record(entries=["x"]),
            "entry without its state": self.good_record(entries=[{k: v for k, v in entry.items() if k != "state"}]),
            "negative spend": self.good_record(entries=[{**entry, "known_usd": -1}]),
            "boolean spend": self.good_record(entries=[{**entry, "known_usd": True}]),
            "spend as text": self.good_record(entries=[{**entry, "known_usd": "0.1"}]),
            "unknown as a number": self.good_record(entries=[{**entry, "unknown": 1}]),
            "bad timestamp": self.good_record(entries=[{**entry, "started_at": "yesterday"}]),
            "open entry that has a settled time": self.good_record(entries=[{**entry, "state": "open"}]),
            "no last_fallback": {k: v for k, v in self.good_record().items() if k != "last_fallback"},
            "last_fallback that is not an object": self.good_record(last_fallback="x"),
            "last_fallback with an unknown code": self.good_record(
                last_fallback={"at": "2026-11-20T00:00:00Z", "code": "boom", "model": "m"}),
            "account latch with a model code": self.good_record(
                latches={"account": "model_unavailable", "models": {}}),
            "model latch with an unknown code": self.good_record(latches={"account": None, "models": {"m": "x"}}),
            "too many entries": self.good_record(entries=[entry] * 2001),
        }
        for name, record in bad.items():
            with self.subTest(name):
                text = json.dumps(record)
                self.path.write_text(text)
                ledger = self.ledger()
                self.assertFalse(ledger.available)
                self.assertEqual(self.path.read_text(), text)
                with self.assertRaises(LedgerError):
                    ledger.reserve("b" * 32, "sonnet")
                self.assertEqual(ledger.snapshot()["last_fallback"], None)  # a usable reply, never a crash

    def test_a_well_formed_file_with_every_field_loads(self):
        record = self.good_record(last_fallback={"at": "2026-11-20T00:00:00Z", "code": "exhausted", "model": "m"},
                                  latches={"account": "invalid_key", "models": {"m": "model_unavailable"}})
        self.path.write_text(json.dumps(record))
        self.assertTrue(self.ledger().available)


if __name__ == "__main__":
    unittest.main()
