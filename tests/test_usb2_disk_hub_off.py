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

    def attach_disk(
        self, name: str, sectors: int = 1000, node: bool = True, device_number: object = "0:0"
    ) -> None:
        """A block device whose real sysfs path runs through the hub's port 2.

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

    def attach_storage_interface(self) -> None:
        """The USB mass-storage interface attach_disk's disks sit behind."""
        interface = self.sysfs / "1-1.2.1:1.0"
        interface.mkdir()
        (interface / "bInterfaceClass").write_text("08\n")

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
    assert result.returncode == 0
    assert tree.disable.read_text().strip() == "1"
    assert "powering it off, attempt 1 of 3" in result.stderr
    assert "off again" in result.stderr


def test_a_device_still_enumerated_behind_the_port_fails_the_check(tree):
    tree.disable.write_text("1\n")
    tree.attach_child()
    result = tree.run("check")
    assert result.returncode == 1
    assert "still on after 3 attempts" in result.stderr
    status = tree.run("status")
    assert (status.returncode, status.stdout) == (1, "open disable=1 child=present\n")


def test_a_write_the_port_refuses_fails_the_check(tree):
    tree.disable.chmod(0o444)
    if os.access(tree.disable, os.W_OK):
        pytest.skip("this user can write a read-only file, so a refused write cannot be staged")
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert "writing 1 to" in result.stderr


def test_a_different_hub_at_the_path_is_never_written(tree, tmp_path):
    other = Tree(tmp_path / "other", vendor="0bda", product="5411")
    result = other.run("check")
    assert result.returncode == 1
    assert other.disable.read_text().strip() == "0"
    assert "not the Pi's onboard hub (foreign 0bda:5411)" in result.stderr


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


def test_a_disk_behind_the_port_that_cannot_be_claimed_is_never_disconnected(tree):
    # A mounted disk refuses the exclusive claim with EBUSY; that needs a real block device, so
    # the same refusal is staged with a device node that cannot be opened at all.
    tree.attach_child()
    tree.attach_disk("sdz", node=False)
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert "sdz behind it cannot be claimed" in result.stderr
    assert "not disconnecting it" in result.stderr


def test_a_disk_behind_the_port_that_cannot_be_inspected_is_never_disconnected(tree):
    tree.attach_disk("sdz", device_number=None)
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert "sdz behind it cannot be inspected" in result.stderr


def test_an_unused_disk_behind_the_port_does_not_stop_the_power_off(tree):
    tree.attach_disk("sdz")
    result = tree.run("check")
    assert result.returncode == 0
    assert tree.disable.read_text().strip() == "1"


def test_an_empty_card_reader_is_claimed_and_does_not_stop_the_power_off(tree):
    tree.attach_disk("sdz", sectors=0)
    result = tree.run("check")
    assert result.returncode == 0
    assert tree.disable.read_text().strip() == "1"


def test_a_disk_reporting_no_capacity_is_still_claimed(tree):
    # The kernel shows a failed capacity read as 0 sectors, so 0 is not proof of an empty reader.
    tree.attach_disk("sdz", sectors=0, node=False)
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert "sdz behind it cannot be claimed" in result.stderr


def test_a_node_that_is_not_the_inspected_device_is_never_trusted(tree):
    tree.attach_disk("sdz", device_number="8:16")
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert "sdz behind it is not the device sysfs describes" in result.stderr


def test_a_disk_that_appears_while_disks_are_claimed_is_never_cut_unclaimed(tree, monkeypatch, capsys):
    # Staged inside one process: the second disk is plugged in at the first claim.
    tree.attach_disk("sda")
    for key, value in tree.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("usb2_block_program", str(PROGRAM))
    program = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(program)
    real_open = os.open

    def open_then_plug(path, flags, *more):
        descriptor = real_open(path, flags, *more)
        if not (tree.sys_block / "sdb").exists():
            tree.attach_disk("sdb", node=False)
        return descriptor

    monkeypatch.setattr(os, "open", open_then_plug)
    assert program.main(["vanpi-usb2-disk-hub-off", "check"]) == 1
    monkeypatch.undo()
    stderr = capsys.readouterr().err
    assert tree.disable.read_text().strip() == "0"
    assert "changed while its disks were being claimed; nothing written on attempt 1" in stderr
    assert "sdb behind it cannot be claimed" in stderr


def test_a_storage_interface_whose_disk_has_not_appeared_stops_the_power_off(tree):
    # The kernel registers a disk a second or more after its USB interface is probed.
    tree.attach_storage_interface()
    result = tree.run("check")
    assert result.returncode == 1
    assert tree.disable.read_text().strip() == "0"
    assert result.stderr.count("storage interface 1-1.2.1:1.0 behind it has no disk yet; nothing written") == 3
    assert "still on after 3 attempts" in result.stderr


def test_a_storage_interface_with_its_disk_claimed_does_not_stop_the_power_off(tree):
    tree.attach_storage_interface()
    tree.attach_disk("sdz")
    result = tree.run("check")
    assert result.returncode == 0
    assert tree.disable.read_text().strip() == "1"


def test_a_disk_elsewhere_is_not_claimed(tree):
    elsewhere = tree.root / "real" / "usb2" / "2-2" / "block" / "sdy"
    elsewhere.mkdir(parents=True)
    (tree.sys_block / "sdy").symlink_to(elsewhere)
    result = tree.run("check")
    assert result.returncode == 0
    assert tree.disable.read_text().strip() == "1"


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
    assert stderr.count("still on after 3 attempts") == 1
    assert "holds again" in stderr


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
