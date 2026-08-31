import socket
import struct
import unittest
from unittest import mock

from projects.ecu_mapping import rke_front_unlock as rke


def frame(can_id, data):
    return struct.pack("=IB3x8s", can_id, len(data), data.ljust(8, b"\0"))


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        if not self.frames:
            raise socket.timeout
        return self.frames.pop(0)

    def send(self, value):
        self.sent.append(value)
        return len(value)

    def close(self):
        pass


class RkeFrontUnlockTests(unittest.TestCase):
    def test_known_payloads_and_crc(self):
        self.assertEqual(rke.build_front_unlock(1).hex(), "4204000011e001b7")
        self.assertEqual(rke.build_front_unlock(14).hex(), "4204000011e00e0c")
        self.assertEqual(rke.build_action("lock_all", 1).hex(), "42040000101001da")
        self.assertEqual(rke.build_action("unlock_cargo", 11).hex(), "4204000011f00bd1")
        with self.assertRaises(ValueError):
            rke.build_action("arbitrary", 1)

    def test_sync_requires_three_sequential_valid_frames_and_sends_once(self):
        ordinary = []
        for counter in (7, 8, 9):
            body = rke.ORDINARY_PREFIX + bytes((counter,))
            ordinary.append(frame(rke.ACTION_ID, body + bytes((rke.crc8_sae_j1850(body),))))
        sock = FakeSocket(ordinary)
        payload, counters = rke.synchronize_and_send(sock, clock=mock.Mock(return_value=0))
        self.assertEqual(counters, (7, 8, 9))
        self.assertEqual(payload, rke.build_front_unlock(10))
        self.assertEqual(len(sock.sent), 1)

    def test_sync_rejects_ignition(self):
        sock = FakeSocket([frame(rke.IGNITION_ID, b"\x00")])
        with self.assertRaisesRegex(rke.ReplayError, "ignition"):
            rke.synchronize_and_send(sock, clock=mock.Mock(return_value=0))

    def test_dry_run_never_calls_execute(self):
        with mock.patch.object(rke, "execute_once") as execute:
            self.assertEqual(rke.main([]), 0)
        execute.assert_not_called()

    def test_execute_requires_every_gate(self):
        with mock.patch.object(rke, "execute_once") as execute:
            self.assertEqual(rke.main(["--execute"]), 2)
        execute.assert_not_called()

    def test_lock_domain_candidate_decode(self):
        sample = {
            "frames": {
                "5E2": {
                    "count": 2,
                    "first_hex": "00 06 00 00 00 00 00 00",
                    "last_hex": "00 06 00 00 00 00 00 00",
                    "distinct_hex": ["00 06 00 00 00 00 00 00"],
                }
            }
        }
        self.assertEqual(
            rke.decode_lock_domains(sample),
            {
                "state": "front_unlocked_cargo_locked",
                "front_locked": False,
                "cargo_locked": True,
                "quality": "verified",
                "source": "b-can.0x5e2.byte1",
            },
        )

    def test_lock_domain_requires_two_consistent_samples(self):
        insufficient = {
            "frames": {
                "5E2": {
                    "count": 1,
                    "last_hex": "00 02 00 00",
                    "distinct_hex": ["00 02 00 00"],
                }
            }
        }
        unstable = {
            "frames": {
                "5E2": {
                    "count": 2,
                    "last_hex": "00 06 00 00",
                    "distinct_hex": ["00 02 00 00", "00 06 00 00"],
                }
            }
        }
        self.assertEqual(
            rke.decode_lock_domains(insufficient)["quality"],
            "insufficient_samples",
        )
        self.assertEqual(
            rke.decode_lock_domains(unstable)["quality"],
            "unstable_sample",
        )

    def test_fixed_bcm_door_read_and_driver_candidate(self):
        sock = FakeSocket([])
        with mock.patch.object(
            rke.uds, "open_module_socket", return_value=sock
        ) as open_socket, mock.patch.object(rke.uds, "drain"), mock.patch.object(
            rke.uds, "request", return_value=(bytes.fromhex("62 01 30 8C"), "positive")
        ) as request:
            sample = rke._read_bcm_door_inputs("can-test")
        open_socket.assert_called_once_with(
            rke.MODULES["bcm_ccan"], timeout=0.75, channel="can-test"
        )
        request.assert_called_once_with(
            sock, bytes.fromhex("22 01 30"), timeout=0.75, retries=0
        )
        self.assertEqual(sample["request_count"], 1)
        self.assertEqual(sample["data_hex"], "8C")
        doors = rke._decode_doors(sample)
        self.assertFalse(doors["driver"]["ajar"])
        self.assertTrue(doors["driver"]["reported_closed"])
        self.assertTrue(doors["driver"]["physical_state_observable"])
        self.assertIsNone(doors["passenger"]["reported_closed"])
        self.assertIsNone(doors["passenger"]["physical_state_observable"])
        self.assertTrue(doors["sliding"]["reported_closed"])
        self.assertFalse(doors["sliding"]["physical_state_observable"])
        self.assertIsNone(doors["rear"]["reported_closed"])
        self.assertIsNone(doors["rear"]["physical_state_observable"])

    def test_access_state_uses_one_wake_and_returns_every_door(self):
        wake = mock.Mock(role="c-can", source="test", detail="woke")
        route = mock.Mock(channel="can-test")
        session = mock.Mock()
        session._ownership.route = route
        session.trigger.return_value = wake
        handoff = mock.MagicMock()
        bcm_inputs = {
            "did_hex": "0130",
            "request_count": 1,
            "response_hex": "62 01 30 88",
            "data_hex": "88",
            "status": "positive",
            "error": None,
        }
        b_sample = {
            "role": "b-can",
            "pair": "3/11",
            "sample_seconds": 1.0,
            "frames": {
                "46C": {"count": 1, "last_hex": "00 20 6F B2 53 00 00 00"},
                "5B2": {"count": 1, "last_hex": "00 00 13 10 01 00 00 00"},
                "5E2": {
                    "count": 2,
                    "first_hex": "00 02 00 00",
                    "last_hex": "00 02 00 00",
                    "distinct_hex": ["00 02 00 00"],
                },
            },
            "error": None,
        }
        c_sample = {
            "role": "c-can",
            "pair": "6/14",
            "sample_seconds": 1.0,
            "frames": {},
            "error": None,
        }
        with mock.patch.object(
            rke.can_handoff, "active_turn", return_value=handoff
        ), mock.patch.object(
            rke.can_wake, "_open_wake_session", return_value=session
        ) as open_session, mock.patch.object(
            rke, "_read_bcm_door_inputs", return_value=bcm_inputs
        ) as read_bcm, mock.patch.object(
            rke, "_sample_active_c_can", return_value=c_sample
        ), mock.patch.object(
            rke, "_sample_passive_role", return_value=b_sample
        ):
            result = rke.read_access_state_once()
        open_session.assert_called_once_with("c-can", prearm_check=rke.prearm_conflicts)
        session.trigger.assert_called_once_with()
        read_bcm.assert_called_once_with("can-test")
        session.close.assert_called_once_with()
        self.assertEqual(result["wake"]["count"], 1)
        self.assertEqual(result["wake"]["additional_tx_after_wake"], 1)
        self.assertEqual(result["lock_domains"]["state"], "locked")
        self.assertEqual(set(result["doors"]), {"driver", "passenger", "sliding", "rear"})
        self.assertTrue(result["doors"]["driver"]["ajar"])
        self.assertFalse(result["doors"]["driver"]["reported_closed"])
        self.assertTrue(result["doors"]["driver"]["physical_state_observable"])
        self.assertFalse(result["doors"]["sliding"]["physical_state_observable"])
        self.assertEqual(
            result["doors"]["sliding"]["ajar_quality"],
            "hardware_bypass_forced_closed",
        )
        self.assertTrue(
            all(
                result["doors"][name]["ajar"] is None
                for name in ("passenger", "sliding", "rear")
            )
        )


if __name__ == "__main__":
    unittest.main()
