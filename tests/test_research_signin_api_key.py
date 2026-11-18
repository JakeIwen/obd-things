"""The research sign-in's API-key installer and the volume mount check, on injected paths and mount facts."""

import contextlib
import getpass
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import warnings

from projects.vehicle_data import dtc_research_signin as signin
from projects.vehicle_data.api_credits import read_key_file
from projects.vehicle_data.research_volume import VOLUME_IMAGE, Volume, mount_problem
from tests.research_support import FAKE_KEY


def mounted(_volume):
    """The tests' volume is a temp directory; the mount check has its own tests below."""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.volume = self.root / "volume"
        self.volume.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv, prompt=None, mount=mounted):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = signin.main([*argv, "--volume", str(self.volume)], prompt=prompt or (lambda _: FAKE_KEY),
                               mounted=mount)
        self.assertNotIn(FAKE_KEY, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def installed(self):
        return Volume(self.volume).api_key

    def test_typed_key_is_installed_mode_0600_and_readable_by_the_workers_reader(self):
        code, out, _ = self.main("api-key")
        self.assertEqual(code, 0)
        path = self.installed()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(read_key_file(path), (FAKE_KEY, None))
        self.assertEqual(sorted(p.name for p in self.volume.iterdir()), ["anthropic-api-key"])  # no temp left behind
        self.assertIn("Saved", out)

    def test_from_file_accepts_the_bare_key_and_the_export_line_and_never_sources_them(self):
        for text in (FAKE_KEY, f"export ANTHROPIC_PROMO_API_KEY={FAKE_KEY}\n"):
            source = self.root / "secret"
            source.write_text(text)
            os.chmod(source, 0o600)
            with self.subTest(text[:12]):
                self.assertEqual(self.main("api-key", "--from-file", str(source), prompt=self.fail)[0], 0)
                self.assertEqual(read_key_file(self.installed())[0], FAKE_KEY)

    def test_from_file_rejects_links_directories_oversize_and_garbage(self):
        real = self.root / "real"
        real.write_text(FAKE_KEY)
        link = self.root / "link"
        link.symlink_to(real)
        big = self.root / "big"
        big.write_text(FAKE_KEY + "\n" + "x" * 4000)
        junk = self.root / "junk"
        junk.write_text("hello")
        for name, source in {"link": link, "dir": self.root, "big": big, "junk": junk, "missing": self.root / "no"}.items():
            with self.subTest(name):
                code, _, err = self.main("api-key", "--from-file", str(source))
                self.assertEqual(code, 1)
                self.assertIn("Not saved", err)
        self.assertFalse(self.installed().exists())

    def test_admin_oauth_and_other_keys_are_refused_without_echoing_them(self):
        for bad in ("sk-ant-admin01-" + "A" * 40, "sk-ant-oat01-" + "A" * 40, "sk-ant-api03-short", "hello",
                    "", "sk-proj-" + "A" * 40):
            with self.subTest(bad[:14]):
                code, out, err = self.main("api-key", prompt=lambda _, bad=bad: bad)
                self.assertEqual(code, 1)
                self.assertNotIn(bad or "\0", out + err)
        self.assertFalse(self.installed().exists())

    def test_an_unmounted_volume_is_refused_for_the_key_and_the_oauth_token(self):
        def unmounted(volume):
            signin.require_mounted(volume)

        for what in ("api-key", "claude"):
            with self.subTest(what):
                code, _, err = self.main(what, mount=unmounted)
                self.assertEqual(code, 1)
                self.assertIn("not a mount point", err)
        self.assertEqual(list(self.volume.iterdir()), [])

    def test_a_terminal_that_cannot_hide_input_is_an_error_not_an_echoing_fallback(self):
        def echoing(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            return FAKE_KEY

        with mock.patch("getpass.getpass", side_effect=echoing):
            with self.assertRaises(ValueError):
                signin.read_hidden("key: ")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass", side_effect=echoing), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = signin.main(["api-key", "--volume", str(self.volume)], mounted=mounted)
        self.assertEqual(code, 1)
        self.assertIn("hide input", err.getvalue())
        self.assertFalse(self.installed().exists())

    def test_remove_deletes_the_key_and_is_idempotent(self):
        self.main("api-key")
        code, out, _ = self.main("api-key", "--remove")
        self.assertEqual((code, self.installed().exists()), (0, False))
        self.assertIn("removed", out)
        self.assertIn("No API key", self.main("api-key", "--remove")[1])

    def test_status_reports_installed_or_not_and_never_the_value(self):
        self.assertIn("API key (promotional credits): not installed", self.main("status")[1])
        self.main("api-key")
        text = self.main("status")[1]
        self.assertIn("API key (promotional credits): installed", text)
        self.assertNotIn(FAKE_KEY, text)
        os.chmod(self.installed(), 0o644)
        self.assertIn("unusable", self.main("status")[1])

    def test_the_key_file_is_replaced_atomically_and_never_follows_a_planted_link(self):
        self.main("api-key")
        other = "sk-ant-api03-" + "B" * 32
        signin.save_api_key(self.volume, other, mounted=mounted)
        self.assertEqual(read_key_file(self.installed())[0], other)
        victim = self.root / "victim"
        victim.write_text("keep")
        self.installed().unlink()
        self.installed().symlink_to(victim)
        signin.save_api_key(self.volume, FAKE_KEY, mounted=mounted)
        self.assertEqual(victim.read_text(), "keep")  # the rename replaced the link itself
        self.assertFalse(self.installed().is_symlink())

    def test_the_directory_is_fsynced_after_the_rename(self):
        events = []
        real_replace = os.replace
        with mock.patch("os.fsync", side_effect=lambda fd: events.append("fsync")), \
                mock.patch("os.replace", side_effect=lambda *a: (events.append("replace"), real_replace(*a))):
            signin.save_api_key(self.volume, FAKE_KEY, mounted=mounted)
        self.assertEqual(events, ["fsync", "replace", "fsync"])


class MountCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mountinfo, self.sys_block = root / "mountinfo", root / "block"

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, source, *, is_mount=True, backing=None, root="/var/lib/vanresearch"):
        self.mountinfo.write_text(
            "20 1 8:1 / / rw - ext4 /dev/mmcblk0p2 rw\n"
            f"45 20 7:3 / {root} rw,nodev - ext4 {source} rw\n")
        if backing is not None:
            loop = self.sys_block / "loop3" / "loop"
            loop.mkdir(parents=True, exist_ok=True)
            (loop / "backing_file").write_text(backing + "\n")
        return mount_problem(root, is_mount=lambda _: is_mount, mountinfo=self.mountinfo, sys_block=self.sys_block)

    def test_the_images_loop_device_is_accepted(self):
        self.assertIsNone(self.check("/dev/loop3", backing=VOLUME_IMAGE))
        self.assertIsNone(self.check("/dev/loop3", backing=VOLUME_IMAGE + " (deleted)"))

    def test_the_image_itself_as_the_source_is_accepted(self):
        self.assertIsNone(self.check(VOLUME_IMAGE))

    def test_not_a_mount_point_a_foreign_loop_device_or_another_source_is_refused(self):
        self.assertIn("not a mount point", self.check("/dev/loop3", is_mount=False, backing=VOLUME_IMAGE))
        self.assertIn("other than the research image", self.check("/dev/loop3", backing="/home/pi/other.img"))
        self.assertIn("other than the research image", self.check("/dev/sda1"))
        self.assertIn("other than the research image", self.check("tmpfs"))

    def test_an_unreadable_loop_device_or_missing_mount_record_is_refused(self):
        self.assertIn("cannot be identified", self.check("/dev/loop9"))
        self.mountinfo.write_text("")
        self.assertIn("no readable mount record", mount_problem(
            "/elsewhere", is_mount=lambda _: True, mountinfo=self.mountinfo, sys_block=self.sys_block))

    def test_the_last_matching_mount_line_wins(self):
        self.mountinfo.write_text(
            f"45 20 7:3 / /var/lib/vanresearch rw - ext4 /dev/sda1 rw\n"
            f"46 20 7:4 / /var/lib/vanresearch rw - ext4 {VOLUME_IMAGE} rw\n")
        self.assertIsNone(mount_problem("/var/lib/vanresearch", is_mount=lambda _: True, mountinfo=self.mountinfo,
                                        sys_block=self.sys_block))


if __name__ == "__main__":
    unittest.main()
