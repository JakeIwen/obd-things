"""Offline cgroup inspection against synthetic kernel files and read races."""

from pathlib import Path

import pytest

from projects.vehicle_data.service_cgroup import inspect
from projects.vehicle_data.usb_recovery_contract import HELPER_MODULES, PROCESS_CHECK_ROUNDS
from projects.vehicle_data.usb_recovery_model import ProcessCheck

_SELF = 42
_PI = (1000,) * 4
_GROUP = "/system.slice/van-telemetry.service"


def stat_line(pid, start, *, ppid, comm, state="S"):
    # Start time is field 22, after the state, PPID and the next 17 fields.
    return f"{pid} ({comm}) {state} {ppid}{' 0' * 17} {start} 0\n".encode()


def status_line(uids):
    return b"Name:\tfixture\nUid:\t" + b"\t".join(str(uid).encode() for uid in uids) + b"\n"


class FakeCgroup:
    def __init__(self, tmp_path):
        self.proc = tmp_path / "proc"
        self.cgroup = tmp_path / "cgroup"
        self.group = self.proc / "self/cgroup"
        self.group.parent.mkdir(parents=True)
        self.group.write_text(f"0::{_GROUP}\n")
        self.membership = self.cgroup / _GROUP[1:] / "cgroup.procs"
        self.membership.parent.mkdir(parents=True)
        self.list()

    def list(self, *pids):
        self.membership.write_bytes(self.listing(*pids))

    def listing(self, *pids):
        return "".join(f"{pid}\n" for pid in (_SELF, *pids)).encode()

    def process(self, pid, *argv, uids=_PI, start=100, ppid=_SELF, comm="fixture ) comm", state="S"):
        path = self.proc / str(pid)
        path.mkdir(exist_ok=True)
        (path / "stat").write_bytes(stat_line(pid, start, ppid=ppid, comm=comm, state=state))
        (path / "status").write_bytes(status_line(uids))
        (path / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
        return path

    def inspect(self, **kwargs):
        return inspect(proc_root=self.proc, cgroup_root=self.cgroup, self_pid=_SELF, **kwargs)


@pytest.fixture
def group(tmp_path):
    return FakeCgroup(tmp_path)


def script_reads(monkeypatch, path, *contents):
    """Inject different kernel bytes on successive reads of just one file."""
    read_bytes = Path.read_bytes
    values = iter(contents)
    seen = []

    def read(selected):
        if selected != path:
            return read_bytes(selected)
        value = next(values)
        seen.append(value)
        return value

    monkeypatch.setattr(Path, "read_bytes", read)
    return seen


def test_only_self_is_quiet(group):
    assert group.inspect() == ProcessCheck(quiet=True)


def test_unprivileged_children_and_orphans_are_quiet(group):
    group.process(51, "/usr/sbin/ip", "-details", "link", "show")
    group.process(52, "ntfy-send", "example notification arguments")
    group.process(53, "ntfy-flush", ppid=52)
    group.process(54, "curl", "Bearer private", ppid=1, comm="newline\n ) comm")
    group.list(51, 52, 53, 54, 54)  # cgroup.procs can repeat a PID.
    assert group.inspect() == ProcessCheck(quiet=True)


@pytest.mark.parametrize("ppid", [_SELF, 1])
def test_sudo_is_offender_even_with_only_unprivileged_uids(group, ppid):
    group.process(51, "/usr/bin/sudo", "-n", "ip", "do not log this", ppid=ppid)
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "sudo", "sudo"),))


@pytest.mark.parametrize("uids", [(0, 0, 0, 0), (0, 1000, 1000, 1000), (1000, 0, 1000, 1000),
                                  (1000, 1000, 0, 1000), (1000, 1000, 1000, 0)])
def test_root_ip_is_offender_without_sudo(group, uids):
    group.process(51, "/usr/sbin/ip", "link", "set", uids=uids, ppid=1)
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "root", "ip"),))


@pytest.mark.parametrize("module", HELPER_MODULES)
def test_live_helper_module_is_offender_without_exposing_argv(group, module):
    name = "python" + "x" * 40
    group.process(51, f"/path/to/{name}", "-m", module, "secret")
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "helper", name[:32]),))


@pytest.mark.parametrize("membership", ["0::/system.slice/not-the-broker.service\n", "2:cpu:/x\n",
                                        f"0::{_GROUP}\n0::{_GROUP}\n",
                                        f"0::/../{_GROUP[1:]}\n"])
def test_wrong_or_malformed_cgroup_is_unverifiable(group, membership):
    group.group.write_text(membership)
    assert group.inspect() == ProcessCheck(quiet=False, unverifiable=True)


@pytest.mark.parametrize("listing", [b"", b"51\n", b"42\n0\n", b"42\n51 52\n"])
def test_malformed_or_inconsistent_listing_is_unverifiable(group, listing):
    group.membership.write_bytes(listing)
    assert group.inspect() == ProcessCheck(quiet=False, unverifiable=True)


@pytest.mark.parametrize("file, bad", [("stat", b"51 (too few fields) S 42"),
                                       ("status", b"Uid: 1000 1000 bad 1000\n"),
                                       ("cmdline", b"unterminated")])
def test_unparseable_process_file_is_unverifiable(group, file, bad):
    proc = group.process(51, "curl")
    (proc / file).write_bytes(bad)
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=False, unverifiable=True)


def test_missing_file_in_existing_process_directory_is_unverifiable(group):
    proc = group.process(51, "curl")
    (proc / "status").unlink()
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=False, unverifiable=True)


def test_vanished_uninspected_pid_does_not_hide_its_child(group, monkeypatch):
    group.process(50, "curl")
    group.process(52, "/usr/sbin/ip", uids=(0,) * 4, ppid=1)
    # The missing PID first appears on what could otherwise be the convergence round.
    listings = [group.listing(50), group.listing(50, 51), group.listing(50, 52), group.listing(50, 52)]
    seen = script_reads(monkeypatch, group.membership, *listings)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((52, "root", "ip"),))
    assert seen == listings


def test_pid_disappearing_mid_inspection_forces_relist(group, monkeypatch):
    process = group.process(51, "curl")
    group.process(52, "/usr/bin/sudo", ppid=1)
    group.list(51)
    original = Path.read_bytes

    def read(path):
        if path == process / "cmdline":
            process.rename(group.proc / "exited")
            group.list(52)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((52, "sudo", "sudo"),))


def test_exec_between_status_reads_retries_and_catches_root(group, monkeypatch):
    process = group.process(51, "/usr/sbin/ip", ppid=1)
    group.list(51)
    pi, root = status_line(_PI), status_line((0,) * 4)
    reads = script_reads(monkeypatch, process / "status", pi, root, root, root)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "root", "ip"),))
    assert reads == [pi, root, root, root]


def test_three_inconsistent_reads_mark_a_changing_offender(group, monkeypatch):
    process = group.process(51, "/usr/sbin/ip", ppid=1)
    group.list(51)
    pi, root = status_line(_PI), status_line((0,) * 4)
    reads = script_reads(monkeypatch, process / "status", pi, root, pi, root, pi, root)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "changing", "ip"),))
    assert len(reads) == 6


def test_new_identity_for_same_pid_is_inspected(group, monkeypatch):
    process = group.process(51, "curl", start=200)
    group.list(51)
    orig = Path.read_bytes
    calls = 0

    def read(path):
        nonlocal calls
        if path == group.membership:
            calls += 1
            if calls == 2:
                group.process(51, "/usr/bin/sudo", start=201)
        return orig(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "sudo", "sudo"),))
    assert process.is_dir() and calls == 3


def test_identity_change_mid_inspection_discards_the_mixed_sample(group, monkeypatch):
    process = group.process(51, "curl", start=200)
    group.list(51)
    original = Path.read_bytes
    reads = 0

    def read(path):
        nonlocal reads
        if path == process / "stat":
            reads += 1
            if reads == 2:
                group.process(51, "/usr/bin/sudo", start=201)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "sudo", "sudo"),))
    assert reads == 5


def test_new_pid_in_every_round_is_unstable(group, monkeypatch):
    pids = range(51, 51 + PROCESS_CHECK_ROUNDS)
    for pid in pids:
        group.process(pid, "curl")
    seen = script_reads(monkeypatch, group.membership, *(group.listing(pid) for pid in pids))
    assert group.inspect() == ProcessCheck(quiet=False, unstable=True)
    assert len(seen) == PROCESS_CHECK_ROUNDS


def test_benign_child_coming_and_going_still_converges(group, monkeypatch):
    group.process(51, "curl")
    script_reads(monkeypatch, group.membership, group.listing(51), group.listing())
    assert group.inspect() == ProcessCheck(quiet=True)


def test_no_argv_only_counts_as_inspected_if_the_process_is_dead(group):
    group.process(51, state="Z")
    group.list(51)
    assert group.inspect() == ProcessCheck(quiet=True)
    group.process(51)
    assert group.inspect() == ProcessCheck(quiet=False, offenders=((51, "changing", ""),))
