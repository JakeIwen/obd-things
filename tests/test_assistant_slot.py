"""The shared CLI lock between the warning chat and research, and the warning chat's slot."""

from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from projects.vehicle_data.assistant_common import ChatError
from projects.vehicle_data.assistant_slot import RESEARCH_RUNNING, ModelSlot, StartWindow
from projects.vehicle_data.model_cli_lock import ACQUIRED, HELD, MISSING, CliLock


class CliLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "model-cli.lock"
        self.path.write_text("")
        self.path.chmod(0o440)  # read-only is enough, as in the units

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_holder_at_a_time_in_both_race_directions(self):
        for first_is_chat in (True, False):
            with self.subTest(first_is_chat=first_is_chat):
                chat = ModelSlot(cli_lock=CliLock(self.path))
                research = CliLock(self.path)
                barrier, results = threading.Barrier(2), {}

                def take_chat():
                    barrier.wait()
                    if not first_is_chat:
                        time.sleep(.05)
                    try:
                        chat.try_acquire()
                        results["chat"] = "acquired"
                    except ChatError as exc:
                        results["chat"] = str(exc)

                def take_research():
                    barrier.wait()
                    if first_is_chat:
                        time.sleep(.05)
                    results["research"] = research.try_acquire()

                threads = [threading.Thread(target=take_chat), threading.Thread(target=take_research)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                winners = [name for name, value in results.items() if value in ("acquired", ACQUIRED)]
                self.assertEqual(winners, ["chat"] if first_is_chat else ["research"])
                if first_is_chat:
                    self.assertEqual(results["research"], HELD)
                    chat.release()
                else:
                    self.assertEqual(results["chat"], RESEARCH_RUNNING)
                    self.assertFalse(chat.busy)  # the slot was given back
                    research.release()

    def test_a_holder_killed_mid_run_releases_the_lock(self):
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl,os,sys,time\n"
            f"fd=os.open({str(self.path)!r}, os.O_RDONLY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(CliLock(self.path).try_acquire(), HELD)
            holder.send_signal(signal.SIGKILL)
            holder.wait(5)
            lock = CliLock(self.path)
            self.assertEqual(lock.try_acquire(), ACQUIRED)
            lock.release()
        finally:
            holder.kill()
            holder.wait()

    def test_missing_lock_file_is_reported_and_the_warning_chat_carries_on(self):
        self.path.unlink()
        self.assertEqual(CliLock(self.path).try_acquire(), MISSING)
        chat = ModelSlot(cli_lock=CliLock(self.path))
        chat.try_acquire()
        self.assertTrue(chat.busy)
        chat.release()

    def test_window_and_slot_keep_their_old_behaviour(self):
        clock = [0.0]
        slot = ModelSlot(window=StartWindow(limit=2, seconds=60, clock=lambda: clock[0]),
                         cli_lock=CliLock(self.path))
        for _ in range(2):
            slot.try_acquire()
            slot.started()
            slot.release()
        with self.assertRaises(ChatError) as full:
            slot.try_acquire()
        self.assertEqual(full.exception.status, 429)
        clock[0] = 61
        slot.try_acquire()
        with self.assertRaises(ChatError):
            slot.try_acquire()  # one job at a time
        slot.release()

    def test_each_recorded_start_carries_its_own_time_not_another_requests_check(self):
        clock = [0.0]
        window = StartWindow(limit=10, seconds=60, clock=lambda: clock[0])
        window.full()  # request A checks at 0 and starts later
        clock[0] = 50
        window.full()  # request B's check lands in between
        clock[0] = 55
        window.record()  # A's start
        clock[0] = 56
        window.record()  # B's start
        self.assertEqual(list(window.recent), [55, 56])


if __name__ == "__main__":
    unittest.main()
