"""Pairing PINs (``assistant_pairing.PairingPins``) through their public methods, with a fake clock
and fake randomness; no server, no network. Every code and PIN here is made up."""

import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
import unittest
from unittest import mock

from projects.vehicle_data import assistant_pairing as pairing
from projects.vehicle_data.assistant_pairing import PairingPins

CODE = "c" * 43
CONTRACT = json.loads((Path(pairing.__file__).with_name("assistant_pairing.json")).read_text())


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Pins:
    """Deterministic PINs: each create takes the next value."""

    def __init__(self, *values):
        self.values = list(values)
        self.limits = []

    def __call__(self, limit):
        self.limits.append(limit)
        return self.values.pop(0)


def pin_ids():
    counter = iter(range(1, 1000))
    return lambda nbytes: f"{next(counter):0{nbytes * 2}x}"


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pins = Pins(42917, 123456, 7, 654321, 111111)
        self.logs = []
        self.p = PairingPins(CODE, clock=self.clock, randbelow=self.pins, token_hex=pin_ids(), log=self.logs.append)

    def create(self):
        status, reply = self.p.create()
        self.assertEqual(status, 200, reply)
        return reply

    def redeem(self, pin):
        return self.p.redeem({"pin": pin})

    def state(self):
        return self.p.status()[1]

    def test_pin_is_six_digits_from_secrets_and_one_active_at_a_time(self):
        first = self.create()
        self.assertEqual(first["pin"], "042917")  # leading zeros kept
        self.assertEqual(self.pins.limits, [10 ** 6])
        self.assertRegex(first["pin_id"], CONTRACT["pinIdPattern"])
        self.assertEqual(first["expires_in_s"], 300)
        second = self.create()
        self.assertNotEqual(first["pin_id"], second["pin_id"])
        self.assertEqual(self.redeem(first["pin"])[0], 403)  # replaced
        self.assertEqual(self.redeem(second["pin"]), (200, {"available": True, "access_code": CODE}))
        self.assertIs(PairingPins.__init__.__kwdefaults__["randbelow"], secrets.randbelow)

    def test_expiry_uses_the_monotonic_clock(self):
        self.assertIs(PairingPins.__init__.__kwdefaults__["clock"], time.monotonic)
        reply = self.create()
        self.clock.now += 299.9
        with mock.patch("time.time", return_value=time.time() + 10 ** 6):
            self.assertEqual(self.state()["state"], "active")
            self.assertEqual(self.redeem(reply["pin"])[0], 200)
        reply = self.create()
        self.clock.now += 300
        self.assertEqual(self.redeem(reply["pin"])[0], 403)
        self.assertEqual(self.state()["state"], "expired")

    def test_pin_is_single_use(self):
        reply = self.create()
        self.assertEqual(self.redeem(reply["pin"])[0], 200)
        self.assertEqual(self.redeem(reply["pin"]), (403, pairing.REJECTED))
        self.assertEqual(self.state()["state"], "redeemed")
        self.assertEqual(self.state()["pin_id"], reply["pin_id"])

    def test_concurrent_redeems_yield_exactly_one_code(self):
        reply = self.create()
        barrier = threading.Barrier(16)
        results = []

        def attempt():
            barrier.wait()
            results.append(self.redeem(reply["pin"]))

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(status for status, _ in results), [200] + [403] * 10 + [429] * 5)

    def test_five_wrong_tries_burn_the_pin(self):
        reply = self.create()
        for _ in range(4):
            self.assertEqual(self.redeem("999999")[0], 403)
        self.assertEqual(self.state()["state"], "active")
        self.assertEqual(self.redeem("999999")[0], 403)
        self.assertEqual(self.state()["state"], "wrong_tries")
        self.assertEqual(self.redeem(reply["pin"])[0], 403)

    def test_window_limit_spans_pins_clients_and_empty_attempts(self):
        for _ in range(3):
            self.assertEqual(self.redeem("999999")[0], 403)  # no PIN active: still counted
        self.create()
        for _ in range(5):
            self.redeem("999999")  # burns the first PIN at five
        self.clock.now += 100
        reply = self.create()
        self.assertEqual(self.redeem("999998")[0], 403)
        status, body = self.redeem("999998")  # the tenth failure locks and burns the active PIN
        self.assertEqual(status, 403)
        self.assertEqual(self.state()["state"], "wrong_tries")
        status, body = self.redeem(reply["pin"])
        self.assertEqual(status, 429)
        self.assertEqual(body["reason"], "pairing_locked")
        self.assertEqual(body["retry_after_s"], 800)  # the oldest failure is 100 s old
        # Refusals while locked are not counted, so they do not extend the lock.
        for _ in range(50):
            self.assertEqual(self.redeem("999997")[0], 429)
        self.clock.now += 799.5
        self.assertEqual(self.state()["locked_for_s"], 1)
        self.clock.now += .5
        self.assertEqual(self.state()["locked_for_s"], 0)
        reply = self.create()
        self.assertEqual(self.redeem(reply["pin"])[0], 200)

    def test_lockout_refuses_create_and_burns_the_active_pin(self):
        for _ in range(9):
            self.redeem("999999")
        reply = self.create()
        self.assertEqual(self.redeem("999999")[0], 403)
        self.assertEqual(self.state()["state"], "wrong_tries")
        status, body = self.p.create()
        self.assertEqual(status, 429)
        self.assertNotIn("pin", body)
        self.assertGreater(self.state()["locked_for_s"], 0)
        self.assertEqual(self.redeem(reply["pin"])[0], 429)

    def test_rejections_are_identical_for_wrong_none_expired_and_burned(self):
        bodies = []
        bodies.append(self.redeem("123456"))                 # none active
        reply = self.create()
        bodies.append(self.redeem("000001"))                 # wrong
        self.clock.now += 301
        bodies.append(self.redeem(reply["pin"]))             # expired
        reply = self.create()
        for _ in range(4):
            self.redeem("000002")
        bodies.append(self.redeem(reply["pin"][::-1]))       # the fifth, burning try
        bodies.append(self.redeem(reply["pin"]))             # burned
        encoded = {json.dumps(body, sort_keys=True) for body in bodies}
        self.assertEqual({status for status, _ in bodies}, {403})
        self.assertEqual(len(encoded), 1)
        self.assertNotIn("attempt", next(iter(encoded)))

    def test_malformed_bodies_are_400_and_not_counted(self):
        reply = self.create()
        for payload in (None, [], "042917", {}, {"pin": 42917}, {"pin": "42917"}, {"pin": "0429170"},
                        {"pin": "04291a"}, {"pin": "٠" * 6}, {"pin": "042917\n"},
                        {"pin": reply["pin"], "extra": 1}) * 3:
            with self.subTest(payload=payload):
                status, body = self.p.redeem(payload)
                self.assertEqual(status, 400)
                self.assertEqual(body["reason"], "invalid_request")
        self.assertEqual(self.state()["state"], "active")
        self.assertEqual(self.redeem(reply["pin"])[0], 200)
        self.assertTrue(all(line["failures_in_window"] == 0 for line in self.logs))

    def test_comparison_is_constant_time_and_done_whether_or_not_a_pin_is_active(self):
        # This proves which comparison decides and that both cases make it; it does not measure
        # whole-request timing (lock waits and bookkeeping are not constant).
        calls = []
        real = hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        with mock.patch("projects.vehicle_data.assistant_pairing.hmac.compare_digest", side_effect=spy):
            self.redeem("123456")
            reply = self.create()
            self.redeem("000001")
            self.assertEqual(self.redeem(reply["pin"])[0], 200)
            self.p.redeem({"pin": "12"})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(isinstance(a, bytes) and len(a) == len(b) == 6 for a, b in calls))
        self.assertEqual(calls[-1], (reply["pin"].encode(), reply["pin"].encode()))

    def test_owner_route_bodies_are_exact(self):
        self.assertEqual(self.p.handle("GET", None)[1]["state"], "none")
        for payload in ({"action": "create", "x": 1}, {"action": "start"}, {"action": "cancel"},
                        {"action": "cancel", "pin_id": "XYZ"}, {"action": "cancel", "pin_id": "0" * 15}, None, []):
            with self.subTest(payload=payload):
                self.assertEqual(self.p.handle("POST", payload), (400, pairing.INVALID))
        status, reply = self.p.handle("POST", {"action": "create"})
        self.assertEqual(status, 200)
        other = "f" * 16
        self.assertEqual(self.p.handle("POST", {"action": "cancel", "pin_id": other})[1]["state"], "active")
        for _ in range(2):  # idempotent
            status, body = self.p.handle("POST", {"action": "cancel", "pin_id": reply["pin_id"]})
            self.assertEqual((status, body["state"]), (200, "cancelled"))
        self.assertEqual(self.redeem(reply["pin"])[0], 403)
        self.assertEqual(set(body), {"available", "pin_id", "state", "expires_in_s", "locked_for_s"})

    def test_logs_never_carry_pin_pin_id_or_code(self):
        reply = self.create()
        self.redeem("999999")
        self.assertEqual(self.redeem(reply["pin"])[0], 200)
        second = self.create()
        self.p.cancel(second["pin_id"])
        third = self.create()
        self.clock.now += 400
        self.state()
        for _ in range(10):
            self.redeem("999999")
        self.redeem("999999")
        self.clock.now += 1000
        self.state()
        text = "\n".join(json.dumps(line) for line in self.logs)
        for secret in (CODE, reply["pin"], second["pin"], third["pin"], reply["pin_id"], second["pin_id"]):
            self.assertNotIn(secret, text)
        self.assertIsNone(re.search(r"\d{6}", text))
        self.assertTrue(all(set(line) == {"event", "action", "failures_in_window"} for line in self.logs))
        actions = [line["action"] for line in self.logs]
        for action in ("created", "redeemed", "rejected", "locked", "unlocked", "cancelled", "expired"):
            self.assertIn(action, actions)
        self.assertEqual(actions.count("rejected"), 10)  # refusals while locked are not logged

    def test_default_log_is_one_json_line_on_stderr(self):
        with mock.patch("sys.stderr") as stderr:
            PairingPins(CODE).status()
            PairingPins(CODE).create()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertEqual(json.loads(written.strip())["action"], "created")

    def test_contract_matches_module_constants(self):
        self.assertEqual(pairing.OWNER_PATH, CONTRACT["routes"]["owner"])
        self.assertEqual(pairing.REDEEM_PATH, CONTRACT["routes"]["redeem"])
        self.assertEqual(pairing.REDEEM_BODY_MAX, CONTRACT["redeemBodyMaxBytes"])
        self.assertGreaterEqual(pairing.REDEEM_BODY_MAX, len(json.dumps({"pin": "0" * CONTRACT["pinDigits"]})))
        self.assertEqual((pairing.PIN_DIGITS, pairing.PIN_LIFETIME, pairing.MAX_ATTEMPTS, pairing.WINDOW_LIMIT,
                          pairing.WINDOW_SECONDS), (6, 300, 5, 10, 900))
        self.assertEqual(set(CONTRACT["states"].values()),
                         {"none", "active", "redeemed", "expired", "wrong_tries", "cancelled"})
        self.assertTrue(pairing.relay_allowed("GET", pairing.OWNER_PATH))
        self.assertTrue(pairing.relay_allowed("POST", pairing.OWNER_PATH))
        for method, path in (("PUT", pairing.OWNER_PATH), ("GET", pairing.OWNER_PATH + "/"),
                             ("POST", pairing.REDEEM_PATH), ("GET", pairing.OWNER_PATH + "x")):
            self.assertFalse(pairing.relay_allowed(method, path))


if __name__ == "__main__":
    unittest.main()
