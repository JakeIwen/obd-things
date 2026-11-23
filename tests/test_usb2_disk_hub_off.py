"""The USB 2.0 block watcher, run as a program against a scratch sysfs tree.

No test touches the real /sys, /dev or a notification: every path comes from the environment.
"""

import importlib.machinery
import importlib.util
import os
import pathlib
import stat
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROGRAM = REPO_ROOT / "projects/vehicle_data/usb2_block/vanpi-usb2-disk-hub-off"
# The installed copy runs through its own `python3 -I` line; here the suite's interpreter does.
COMMAND = [sys.executable, "-I", str(PROGRAM)]
KNOWN_READER = ("05e3", "0764")  # registers two disks, one per card slot


class Tree:
    """A scratch /sys/bus/usb/devices holding the onboard hub and its port 2."""

    def __init__(self, root: pathlib.Path, vendor: str = "2109", product: str = "3431"):
        self.root = root
        self.sysfs = root / "devices"
        self.hub = self.sysfs / "1-1"
        self.port = self.hub / "1-1:1.0" / "1-1-port2"
        self.port.mkdir(parents=True)
        (self.hub / "idVendor").write_text(vendor + "\n")
        (self.hub / "idProduct").write_text(product + "\n")
        self.disable = self.port / "disable"
        self.disable.write_text("0\n")
        self.sys_block = root / "block"
        self.sys_block.mkdir()
        self.dev = root / "dev"
        self.dev.mkdir()
        self.alerts = root / "alerts.log"
        notify = root / "notify"
        notify.write_text(f'#!/bin/bash\nprintf "%s|%s\\n" "$1" "$3" >> "{self.alerts}"\n')
        notify.chmod(notify.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": os.environ["PATH"],
            "USB2_BLOCK_SYSFS": str(self.sysfs),
            "USB2_BLOCK_SYS_BLOCK": str(self.sys_block),
            "USB2_BLOCK_DEV": str(self.dev),
            "USB2_BLOCK_NOTIFY": str(notify),
            "USB2_BLOCK_NOTIFY_USER": "",
            "USB2_BLOCK_STATE_DIR": str(root / "state"),
            "USB2_BLOCK_SETTLE_SECONDS": "0",
            "USB2_BLOCK_FAST_INTERVAL_SECONDS": "0.05",
            "USB2_BLOCK_SLOW_INTERVAL_SECONDS": "0.05",
        }

    def attach_child(self) -> pathlib.Path:
        child = self.sysfs / "1-1.2"
        child.mkdir()
        return child

    def attach_storage(self, model: tuple = KNOWN_READER) -> None:
        """The USB storage device at 1-1.2.1 that attach_disk's disks sit behind."""
        device = self.sysfs / "1-1.2.1"
        device.mkdir()
        (device / "idVendor").write_text(model[0] + "\n")
        (device / "idProduct").write_text(model[1] + "\n")
        interface = self.sysfs / "1-1.2.1:1.0"
        interface.mkdir()
        (interface / "bInterfaceClass").write_text("08\n")

    def attach_disk(
        self, name: str, sectors: int = 1000, node: bool = True, device_number: object = "0:0"
    ) -> None:
        """A block device behind that storage device's interface.

        Its node is a regular file, whose device number reads as 0:0.
        """
        real = (
            self.root / "real" / "usb1" / "1-1" / "1-1.2" / "1-1.2.1" / "1-1.2.1:1.0" / "host0"
            / "block" / name
        )
        real.mkdir(parents=True)
        (real / "size").write_text(f"{sectors}\n")
        if device_number is not None:
            (real / "dev").write_text(f"{device_number}\n")
        (self.sys_block / name).symlink_to(real)
        if node:
            (self.dev / name).write_text("")

    def run(self, verb: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            COMMAND + [verb], env=self.env, capture_output=True, text=True, timeout=30
        )

    def watch(self) -> subprocess.Popen:
        return subprocess.Popen(COMMAND + ["watch"], env=self.env, stderr=subprocess.PIPE, text=True)

    def wait_for_alert(self) -> None:
        deadline = time.monotonic() + 20
        while not self.alerts.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def refused(self, result: subprocess.CompletedProcess, why: str) -> None:
        assert result.returncode == 1
        assert self.disable.read_text().strip() == "0"
        assert why in result.stderr

    def powered_off(self, result: subprocess.CompletedProcess) -> None:
        assert result.returncode == 0
        assert self.disable.read_text().strip() == "1"


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path)


def test_a_held_block_is_left_alone_and_silent(tree):
    tree.disable.write_text("1\n")
    result = tree.run("check")
    assert (result.returncode, result.stderr) == (0, "")
    assert tree.run("status").stdout == "held\n"


def test_an_open_port_is_powered_off(tree):
    result = tree.run("check")
    tree.powered_off(result)
    assert "powering it off, attempt 1 of 5" in result.stderr
    assert "off again" in result.stderr


def test_a_device_still_enumerated_behind_the_port_fails_the_check(tree):
    tree.disable.write_text("1\n")
    tree.attach_child()
    result = tree.run("check")
    assert result.returncode == 1
    assert "still on after 5 attempts" in result.stderr
    status = tree.run("status")
    assert (status.returncode, status.stdout) == (1, "open disable=1 child=present\n")


def test_a_write_the_port_refuses_fails_the_check(tree):
    tree.disable.chmod(0o444)
    if os.access(tree.disable, os.W_OK):
        pytest.skip("this user can write a read-only file, so a refused write cannot be staged")
    tree.refused(tree.run("check"), "writing 1 to")


def test_a_different_hub_at_the_path_is_never_written(tree, tmp_path):
    other = Tree(tmp_path / "other", vendor="0bda", product="5411")
    other.refused(other.run("check"), "not the Pi's onboard hub (foreign 0bda:5411)")


def test_no_hub_at_the_path_is_nothing_to_block_and_not_a_held_block(tree, tmp_path):
    empty = Tree(tmp_path / "empty")
    empty.disable.unlink()
    for attribute in ("idVendor", "idProduct"):
        (empty.hub / attribute).unlink()
    empty.port.rmdir()
    empty.port.parent.rmdir()
    empty.hub.rmdir()
    result = empty.run("check")
    assert (result.returncode, result.stderr) == (0, "")
    status = empty.run("status")
    assert (status.returncode, status.stdout) == (1, "no-hub\n")


def test_a_hub_without_its_port_control_is_not_a_held_block(tree):
    tree.disable.unlink()
    result = tree.run("check")
    assert result.returncode == 3
    assert "present without its port 2 control" in result.stderr
    assert "off again" not in result.stderr


def test_a_device_behind_a_port_with_no_control_fails_the_check(tree):
    tree.disable.unlink()
    tree.attach_child()
    result = tree.run("check")
    assert result.returncode == 1
    assert "has no control attribute" in result.stderr


def test_the_known_reader_with_both_disks_unused_is_cut_off(tree):
    tree.attach_storage()
    tree.attach_disk("sdy")
    tree.attach_disk("sdz")
    tree.powered_off(tree.run("check"))


def test_the_known_reader_with_no_cards_is_cut_off(tree):
    tree.attach_storage()
    tree.attach_disk("sdy", sectors=0)
    tree.attach_disk("sdz", sectors=0)
    tree.powered_off(tree.run("check"))


def test_a_disk_that_cannot_be_claimed_is_never_disconnected(tree):
    # A mounted disk refuses the exclusive claim with EBUSY; that needs a real block device, so
    # the same refusal is staged with a device node that cannot be opened at all.
    tree.attach_child()
    tree.attach_storage()
    tree.attach_disk("sdy")
    tree.attach_disk("sdz", node=False)
    result = tree.run("check")
    tree.refused(result, "sdz behind it cannot be claimed")
    assert "not disconnecting it" in result.stderr


def test_a_disk_that_cannot_be_inspected_is_never_disconnected(tree):
    tree.attach_storage()
    tree.attach_disk("sdy")
    tree.attach_disk("sdz", device_number=None)
    tree.refused(tree.run("check"), "sdz behind it cannot be inspected")


def test_a_disk_reporting_no_capacity_is_still_claimed(tree):
    # The kernel shows a failed capacity read as 0 sectors, so 0 is not proof of an empty reader.
    tree.attach_storage()
    tree.attach_disk("sdy")
    tree.attach_disk("sdz", sectors=0, node=False)
    tree.refused(tree.run("check"), "sdz behind it cannot be claimed")


def test_a_node_that_is_not_the_inspected_device_is_never_trusted(tree):
    tree.attach_storage()
    tree.attach_disk("sdy")
    tree.attach_disk("sdz", device_number="8:16")
    tree.refused(tree.run("check"), "sdz behind it is not the device sysfs describes")


def test_storage_this_block_does_not_know_is_never_disconnected(tree):
    # A disk that fell back from USB 3.0: how many disks it will register is not known.
    tree.attach_storage(model=("0bc2", "2344"))
    tree.attach_disk("sdz")
    tree.refused(tree.run("check"), "storage device 1-1.2.1:1.0 (0bc2:2344) behind it is not one this block may disconnect")


def test_a_disk_behind_no_listed_storage_interface_is_never_disconnected(tree):
    tree.attach_disk("sdz")
    tree.refused(tree.run("check"), "sdz behind it belongs to no listed storage interface")


CUT = "has shown {found} of its 2 disks on 3 checks; cutting it without them"


def test_the_known_reader_that_stays_without_disks_is_cut_on_the_third_check(tree):
    # A reader stuck in a kernel reset loop never registers a disk.
    tree.attach_storage()
    result = tree.run("check")
    tree.powered_off(result)
    assert result.stderr.count("shows 0 of its 2 disks; nothing written on attempt") == 2
    assert "nothing written on attempt 1" in result.stderr
    assert "nothing written on attempt 2" in result.stderr
    assert result.stderr.count(CUT.format(found=0)) == 1
    assert "storage interface 1-1.2.1:1.0 behind it " + CUT.format(found=0) in result.stderr
    assert "off again" in result.stderr


def test_the_known_reader_with_one_disk_listed_is_claimed_and_cut_on_the_third_check(tree):
    tree.attach_storage()
    tree.attach_disk("sdy")
    result = tree.run("check")
    tree.powered_off(result)
    assert result.stderr.count("shows 1 of its 2 disks; nothing written") == 2
    assert CUT.format(found=1) in result.stderr


def test_the_listed_disk_of_an_incomplete_reader_that_cannot_be_claimed_is_never_cut(tree):
    tree.attach_storage()
    tree.attach_disk("sdy", node=False)
    result = tree.run("check")
    tree.refused(result, "sdy behind it cannot be claimed")
    assert "not disconnecting it" in result.stderr


class Program:
    """The watcher loaded into this process, so the scratch tree can change between attempts."""

    def __init__(self, tree, monkeypatch):
        for key, value in tree.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
        loader = importlib.machinery.SourceFileLoader("usb2_block_program", str(PROGRAM))
        self.module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.module)
        self.sleeps = 0
        self.then = lambda number: None
        monkeypatch.setattr(self.module.time, "sleep", self.sleep)

    def sleep(self, _seconds):
        self.sleeps += 1
        self.then(self.sleeps)

    def check(self, monkeypatch, capsys):
        code = self.module.main(["vanpi-usb2-disk-hub-off", "check"])
        monkeypatch.undo()
        return code, capsys.readouterr().err


def test_a_listing_that_changes_between_attempts_restarts_the_count(tree, monkeypatch, capsys):
    # 0 disks on attempts 1 and 2, a disk appears for attempt 3 (count restarts), the listing
    # then stands for attempts 3, 4 and 5: the cut comes on attempt 5, not 3.
    tree.attach_storage()
    program = Program(tree, monkeypatch)
    program.then = lambda number: tree.attach_disk("sdy") if number == 2 else None
    code, stderr = program.check(monkeypatch, capsys)
    assert code == 0
    assert tree.disable.read_text().strip() == "1"
    assert "nothing written on attempt 3" in stderr
    assert "nothing written on attempt 4" in stderr
    assert "powering it off, attempt 5 of 5" in stderr
    assert CUT.format(found=1) in stderr
    assert stderr.count("shows 0 of its 2 disks; nothing") == 2


def test_a_reader_that_completes_during_the_pass_takes_the_ordinary_path(tree, monkeypatch, capsys):
    tree.attach_storage()
    tree.attach_disk("sdy")
    program = Program(tree, monkeypatch)
    program.then = lambda number: tree.attach_disk("sdz") if number == 1 else None
    code, stderr = program.check(monkeypatch, capsys)
    assert code == 0
    assert tree.disable.read_text().strip() == "1"
    assert "cutting it without them" not in stderr
    assert "nothing written on attempt 1" in stderr
    assert "nothing written on attempt 2" not in stderr


def test_a_pass_whose_listing_keeps_changing_runs_out_after_five_attempts(tree, monkeypatch, capsys):
    tree.attach_storage()
    program = Program(tree, monkeypatch)

    tree.attach_disk("sdy")
    hidden = tree.root / "hidden-sdy"
    (tree.sys_block / "sdy").rename(hidden)

    def flip(_number):
        # The disk is listed on every other attempt, so no two consecutive listings match.
        if (tree.sys_block / "sdy").is_symlink():
            (tree.sys_block / "sdy").rename(hidden)
        else:
            hidden.rename(tree.sys_block / "sdy")

    program.then = flip
    code, stderr = program.check(monkeypatch, capsys)
    assert code == 1
    assert tree.disable.read_text().strip() == "0"
    assert "cutting it without them" not in stderr
    # Attempts 1, 3 and 5 see no disk; the reason is the last attempt's own.
    assert "still on after 5 attempts (open disable=0 child=absent); storage interface 1-1.2.1:1.0 behind it shows 0 of its 2 disks" in stderr


def test_the_count_does_not_carry_from_one_pass_to_the_next(tree):
    tree.attach_storage()
    tree.powered_off(tree.run("check"))
    tree.disable.write_text("0\n")
    second = tree.run("check")
    tree.powered_off(second)
    assert "nothing written on attempt 2" in second.stderr
    assert "nothing written on attempt 3" not in second.stderr


def test_an_unknown_reader_with_no_disks_is_never_disconnected(tree):
    tree.attach_storage(model=("0bc2", "2344"))
    tree.refused(tree.run("check"), "(0bc2:2344) behind it is not one this block may disconnect")


def test_a_known_reader_beside_an_unknown_one_is_never_disconnected(tree):
    tree.attach_storage()
    other = tree.sysfs / "1-1.2.2"
    other.mkdir()
    (other / "idVendor").write_text("0bc2\n")
    (other / "idProduct").write_text("2344\n")
    interface = tree.sysfs / "1-1.2.2:1.0"
    interface.mkdir()
    (interface / "bInterfaceClass").write_text("08\n")
    tree.refused(tree.run("check"), "(0bc2:2344) behind it is not one this block may disconnect")


def test_more_disks_than_the_known_reader_has_still_refuses(tree):
    tree.attach_storage()
    for name in ("sda", "sdb", "sdc"):
        tree.attach_disk(name)
    tree.refused(tree.run("check"), "shows 3 disks where 2 are known")


def test_a_disk_that_appears_while_disks_are_claimed_is_never_cut_unclaimed(tree, monkeypatch, capsys):
    # Staged inside one process: a third disk is plugged in at the first claim.
    tree.attach_storage()
    tree.attach_disk("sda")
    tree.attach_disk("sdb")
    for key, value in tree.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("usb2_block_program", str(PROGRAM))
    program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(program)
    real_open = os.open

    def open_then_plug(path, flags, *more):
        descriptor = real_open(path, flags, *more)
        if not (tree.sys_block / "sdc").exists():
            tree.attach_disk("sdc")
        return descriptor

    monkeypatch.setattr(os, "open", open_then_plug)
    assert program.main(["vanpi-usb2-disk-hub-off", "check"]) == 1
    monkeypatch.undo()
    stderr = capsys.readouterr().err
    assert tree.disable.read_text().strip() == "0"
    assert "changed while its disks were being claimed; nothing written on attempt 1" in stderr
    assert "shows 3 disks where 2 are known" in stderr


def test_a_disk_elsewhere_is_not_claimed(tree):
    elsewhere = tree.root / "real" / "usb2" / "2-2" / "block" / "sdy"
    elsewhere.mkdir(parents=True)
    (tree.sys_block / "sdy").symlink_to(elsewhere)
    tree.powered_off(tree.run("check"))


def test_watch_alerts_once_while_the_block_keeps_failing_and_reports_recovery(tree):
    tree.disable.write_text("1\n")
    child = tree.attach_child()
    watcher = tree.watch()
    try:
        tree.wait_for_alert()
        time.sleep(0.5)  # several more failing passes
        child.rmdir()
        time.sleep(0.5)
    finally:
        watcher.terminate()
        stderr = watcher.communicate(timeout=10)[1]
    assert tree.alerts.read_text() == "vanpi USB 2.0 block FAILED|high\n"
    assert stderr.count("still on after 5 attempts") == 1
    assert "holds again" in stderr


def test_watch_does_not_alert_for_a_reader_stuck_without_disks_that_is_cut(tree):
    tree.attach_storage()
    watcher = tree.watch()
    try:
        deadline = time.monotonic() + 20
        while tree.disable.read_text().strip() != "1" and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)  # several more passes
    finally:
        watcher.terminate()
        stderr = watcher.communicate(timeout=10)[1]
    assert tree.disable.read_text().strip() == "1"
    assert not tree.alerts.exists()
    assert "cutting it without them" in stderr


def test_watch_alerts_when_the_port_control_stays_missing(tree):
    tree.disable.unlink()
    watcher = tree.watch()
    try:
        tree.wait_for_alert()
    finally:
        watcher.terminate()
        stderr = watcher.communicate(timeout=10)[1]
    assert tree.alerts.read_text() == "vanpi USB 2.0 block FAILED|high\n"
    assert "present without its port 2 control (for 4 passes)" in stderr
    assert "holds again" not in stderr


def test_unknown_verbs_are_usage_errors(tree):
    assert tree.run("reset").returncode == 2
