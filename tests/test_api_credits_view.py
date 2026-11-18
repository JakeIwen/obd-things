"""The Mac's credit snapshot validator and the credit view the advisor computes from it."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from projects.vehicle_data.api_credits_contract import GRANTS, SNAPSHOT
from projects.vehicle_data.api_credits_snapshot import UNKNOWN, SnapshotStore, validate_snapshot
from projects.vehicle_data.api_credits_view import build_view
from tests.dtc_research_scripted_worker import FAKE_KEY

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
FETCHED = NOW - timedelta(minutes=10)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(grants=(("Nov 25", "75.00"), ("Dec 8", "143.68")), fetched=FETCHED):
    total = sum(float(balance) for _label, balance in grants)
    rows = [{"balance_usd": balance, "expiration": label} for label, balance in grants]
    text = (f"${total:,.2f} left · Expires {rows[0]['expiration']} (UTC)" if len(rows) == 1 else
            f"${total:,.2f} left in {len(rows)} grants: " + ", ".join(
                f"${float(g['balance_usd']):,.2f} expires {g['expiration']} (UTC)" for g in rows))
    return {"version": 2, "fetched_at": iso(fetched), "promo_balance_usd": f"{total:.2f}", "grants": rows,
            "partial": False, "display": text}


def entry(started, settled, known, unknown=False, state=None):
    return {"attempt_id": "a" * 32, "model": "opus", "started_at": iso(started),
            "settled_at": iso(settled) if settled else None, "known_usd": known, "unknown": unknown,
            "state": state or ("settled" if settled else "open")}


def worker_credits(entries=(), **extra):
    base = {"key_installed": True, "enabled": True, "until": iso(GRANTS[-1][1]), "entries": list(entries),
            "latches": {"account": None, "models": {}}, "last_fallback": None, "ledger_full": False}
    return {**base, **extra}


def view(entries=(), snapshot=None, now=NOW, **extra):
    snap = snapshot if snapshot is not None else validate_snapshot(record(), NOW)
    return build_view(worker_credits(entries, **extra), snap, now)


class ValidatorTests(unittest.TestCase):
    def test_a_v2_record_is_accepted_with_grants_dated_from_the_contract_table(self):
        snapshot = validate_snapshot(record(), NOW)
        self.assertEqual([(g.label, g.expires) for g in snapshot.grants], list(GRANTS))
        self.assertEqual(snapshot.fetched_at, FETCHED)

    def test_rejected_records(self):
        good = record()
        bad = [None, [], {}, {**good, "version": 1}, {**good, "display": "untrusted"}, {**good, "promo_balance_usd": "1.00"},
               {**good, "grants": []}, {**good, "partial": 0},
               record(grants=(("Nov 26", "75.00"),)),  # a label the contract does not know
               record(grants=(("Jan 8, 2027", "75.00"),)),
               record(grants=(("Nov 25", "75.00"), ("Nov 25", "75.00"))),
               record(fetched=NOW + timedelta(hours=1)),
               record(grants=(("Nov 25", "75.00"),), fetched=datetime(2026, 11, 25, 6, tzinfo=timezone.utc))]
        for candidate in bad:
            with self.subTest(candidate=str(candidate)[:60]), self.assertRaises(ValueError):
                validate_snapshot(candidate, NOW)

    def test_too_many_grants_is_rejected(self):
        many = {**record(), "grants": [{"balance_usd": "1.00", "expiration": "Nov 25"}] * (SNAPSHOT["maxGrants"] + 1)}
        with self.assertRaises(ValueError):
            validate_snapshot(many, NOW)

    def test_a_stale_snapshot_is_kept_and_flagged_not_rejected(self):
        stale = validate_snapshot(record(fetched=NOW - timedelta(hours=5)), NOW)
        self.assertTrue(view(snapshot=stale)["balance"]["stale"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file, self.cache = Path(self.tmp.name, "snap.json"), Path(self.tmp.name, "cache.json")
        self.store = SnapshotStore(self.file, self.cache, clock=lambda: NOW)

    def test_a_v1_error_record_means_unknown_and_is_not_papered_over_by_the_last_good_one(self):
        self.file.write_text(json.dumps(record()))
        self.assertEqual(len(self.store.current().grants), 2)
        self.file.write_text(json.dumps({"version": 1, "fetched_at": None, "error": "promo lookup unavailable"}))
        self.assertEqual(self.store.current(), UNKNOWN)
        self.assertEqual(view(snapshot=UNKNOWN)["balance"]["state"], "unknown")

    def test_oversize_or_unusable_files_keep_the_last_valid_snapshot_and_a_restart_reads_the_cache(self):
        self.file.write_text(json.dumps(record()))
        first = self.store.current()
        self.assertTrue(self.cache.exists())
        self.file.write_text(json.dumps(record()) + " " * SNAPSHOT["maxBytes"])  # over the bound
        self.assertEqual(self.store.current(), first)
        self.file.unlink()
        restarted = SnapshotStore(self.file, self.cache, clock=lambda: NOW)
        self.assertEqual(restarted.current().fetched_at, first.fetched_at)

    def test_no_file_and_no_cache_is_none_and_a_cache_write_failure_only_loses_the_cache(self):
        self.assertIsNone(self.store.current())
        self.file.write_text(json.dumps(record()))
        Path(self.tmp.name, "blocker").write_text("a file where the cache directory should be")
        blocked = SnapshotStore(self.file, Path(self.tmp.name, "blocker", "c.json"), clock=lambda: NOW)
        self.assertEqual(len(blocked.current().grants), 2)

    def test_a_snapshot_with_unknown_labels_is_rejected(self):
        self.file.write_text(json.dumps(record(grants=(("Nov 26", "75.00"),))))
        self.assertIsNone(self.store.current())


class ViewTests(unittest.TestCase):
    def test_a_fresh_snapshot_with_no_spend(self):
        reply = view()
        self.assertEqual(reply["balance"], {"state": "known", "total_usd": 218.68, "as_of": iso(FETCHED), "stale": False,
                                            "grants": [{"label": "Nov 25", "expires": "2026-11-25T00:00:00Z", "remaining_usd": 75.0},
                                                       {"label": "Dec 8", "expires": "2026-12-08T00:00:00Z", "remaining_usd": 143.68}]})
        self.assertEqual((reply["spent_since_snapshot_usd"], reply["uncertain_count"], reply["unknown_spend_count"]), (0.0, 0, 0))

    def test_spend_after_the_fetch_draws_on_the_earliest_expiring_live_grant(self):
        after = FETCHED + timedelta(minutes=1)
        reply = view([entry(after, after + timedelta(seconds=30), 1.5)])
        self.assertEqual([g["remaining_usd"] for g in reply["balance"]["grants"]], [73.5, 143.68])
        self.assertEqual(reply["spent_since_snapshot_usd"], 1.5)
        self.assertEqual(reply["uncertain_count"], 0)

    def test_an_entry_started_after_the_first_grant_expired_draws_on_the_next(self):
        later = datetime(2026, 11, 26, tzinfo=timezone.utc)
        reply = view([entry(later, later + timedelta(seconds=30), 2.0)], now=later + timedelta(hours=1))
        self.assertEqual(reply["balance"]["grants"], [{"label": "Dec 8", "expires": "2026-12-08T00:00:00Z", "remaining_usd": 141.68}])
        self.assertEqual(reply["balance"]["total_usd"], 141.68)  # the expired grant's remainder is lost

    def test_a_spend_larger_than_a_grant_spills_to_the_next_live_one(self):
        after = FETCHED + timedelta(minutes=1)
        reply = view([entry(after, after + timedelta(seconds=30), 80.0)])
        self.assertEqual([g["remaining_usd"] for g in reply["balance"]["grants"]], [0.0, 138.68])

    def test_straddling_and_open_entries_are_subtracted_in_full_and_uncertain(self):
        straddling = entry(FETCHED - timedelta(seconds=20), FETCHED + timedelta(seconds=20), 0.4)
        open_entry = entry(FETCHED - timedelta(minutes=2), None, 0.1)
        reply = view([straddling, open_entry])
        self.assertEqual(reply["uncertain_count"], 2)
        self.assertEqual(reply["spent_since_snapshot_usd"], 0.5)
        self.assertEqual(reply["balance"]["grants"][0]["remaining_usd"], 74.5)

    def test_an_entry_settled_before_the_fetch_is_already_reflected(self):
        reply = view([entry(FETCHED - timedelta(minutes=5), FETCHED - timedelta(minutes=4), 9.0)])
        self.assertEqual((reply["balance"]["total_usd"], reply["spent_since_snapshot_usd"]), (218.68, 0.0))

    def test_unknown_spend_is_counted_and_stale_and_expired_are_flagged(self):
        after = FETCHED + timedelta(minutes=1)
        reply = view([entry(after, after + timedelta(seconds=5), 0.2, unknown=True)], now=NOW + timedelta(hours=3))
        self.assertEqual(reply["unknown_spend_count"], 1)
        self.assertTrue(reply["balance"]["stale"])
        expired = view(now=GRANTS[-1][1] + timedelta(seconds=1))
        self.assertTrue(expired["expired"])
        self.assertEqual(expired["balance"]["grants"], [])
        self.assertEqual(expired["new_research"], {"billing": "subscription", "reason": "expired", "latched_models": []})

    def test_new_research_billing_reasons_and_model_latches(self):
        self.assertEqual(view()["new_research"], {"billing": "api_credits", "reason": None, "latched_models": []})
        latched = view(latches={"account": None, "models": {"claude-opus-x": "model_unavailable"}})["new_research"]
        self.assertEqual(latched, {"billing": "api_credits", "reason": None, "latched_models": ["claude-opus-x"]})
        for extra, reason in (({"enabled": False}, "disabled"), ({"key_installed": False}, "no_key"),
                              ({"latches": {"account": "exhausted", "models": {}}}, "unavailable"),
                              ({"ledger_full": True}, "ledger_full")):
            with self.subTest(reason=reason):
                self.assertEqual(view(**extra)["new_research"], {"billing": "subscription", "reason": reason, "latched_models": []})
        # an expired view outranks a latch
        both = view(now=GRANTS[-1][1], latches={"account": "exhausted", "models": {}})
        self.assertEqual(both["new_research"]["reason"], "expired")

    def test_no_snapshot_is_balance_unknown(self):
        reply = build_view(worker_credits(), None, NOW)
        self.assertEqual(reply["balance"], {"state": "unknown", "total_usd": None, "as_of": None, "stale": False, "grants": []})

    def test_last_fallback_passes_through_when_valid(self):
        last = {"at": iso(NOW), "code": "exhausted", "model": "opus"}
        self.assertEqual(view(last_fallback=last)["last_fallback"], last)
        self.assertIsNone(view(last_fallback={"at": "no", "code": "x"})["last_fallback"])

    def test_the_view_never_carries_anything_the_worker_did_not_whitelist_and_never_a_key(self):
        hostile = worker_credits(extra_field=FAKE_KEY, key=FAKE_KEY, key_path="/var/lib/vanresearch/anthropic-api-key")
        hostile["entries"].append({**entry(FETCHED, None, 0.0), "note": FAKE_KEY})
        hostile["latches"]["models"] = {FAKE_KEY: "model_unavailable"}
        text = json.dumps(build_view(hostile, validate_snapshot(record(), NOW), NOW))
        self.assertNotIn("sk-ant", text)
        self.assertNotIn("anthropic-api-key", text)


if __name__ == "__main__":
    unittest.main()
