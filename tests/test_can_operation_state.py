from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import pytest

from lib import can_operation_state as state
from tools import can_operation_state as cli


class OperationStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.directory.name)
        self.state_patch = mock.patch.object(state, "STATE_DIR", self.state_dir)
        self.boot_patch = mock.patch.object(
            state, "current_boot_id", return_value="boot-current"
        )
        self.state_patch.start()
        self.boot_patch.start()

    def tearDown(self):
        self.boot_patch.stop()
        self.state_patch.stop()
        self.directory.cleanup()

    def test_same_boot_explicit_topology_is_usable(self):
        written = state.set_topology(
            "can0",
            "c-can",
            pair="6/14",
            source="test",
        )
        self.assertTrue(written.usable)
        self.assertEqual(written.bus, "c-can")
        self.assertEqual(state.load_topology("can0").pair, "6/14")

    def test_topology_from_another_boot_fails_closed(self):
        state.set_topology("can0", "b-can", pair="3/11", source="test")
        with mock.patch.object(
            state, "current_boot_id", return_value="boot-next"
        ):
            loaded = state.load_topology("can0")
        self.assertFalse(loaded.usable)
        self.assertEqual(loaded.bus, "unknown")
        self.assertIn("another boot", loaded.reason)

    def test_missing_or_malformed_topology_fails_closed(self):
        self.assertFalse(state.load_topology("can0").usable)
        path = self.state_dir / "topology-can0.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        loaded = state.load_topology("can0")
        self.assertFalse(loaded.usable)
        self.assertIn("malformed", loaded.reason)

    def test_same_boot_inhibit_blocks_until_explicit_end(self):
        state.begin_inhibit(
            "alfaobd", channel="can0", reason="test campaign"
        )
        active = state.active_inhibits("can0")
        self.assertEqual([item["name"] for item in active], ["alfaobd"])
        self.assertTrue(state.end_inhibit("alfaobd"))
        self.assertEqual(state.active_inhibits("can0"), ())

    def test_stale_boot_inhibit_does_not_survive_reboot(self):
        state.begin_inhibit(
            "alfaobd", channel="can0", reason="test campaign"
        )
        with mock.patch.object(
            state, "current_boot_id", return_value="boot-next"
        ):
            self.assertEqual(state.active_inhibits("can0"), ())

    def test_global_inhibit_survives_same_boot_channel_renumber(self):
        written = state.begin_inhibit(
            "restoration-failed",
            channel="*",
            reason="exact passive restoration was not verified",
        )

        self.assertEqual(written["channel"], "*")
        self.assertEqual(
            [item["name"] for item in state.active_inhibits("can0")],
            ["restoration-failed"],
        )
        self.assertEqual(
            [item["name"] for item in state.active_inhibits("can7")],
            ["restoration-failed"],
        )
        with mock.patch.object(
            state, "current_boot_id", return_value="boot-next"
        ):
            self.assertEqual(state.active_inhibits("can7"), ())

    def test_malformed_inhibit_blocks_wake(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "inhibit-broken.json").write_text(
            "{bad", encoding="utf-8"
        )
        active = state.active_inhibits("can0")
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["invalid"])


_BOOT = "12345678-1234-abcd-1234-123456789abc"
_OLD_BOOT = "87654321-4321-dcba-4321-cba987654321"
_LOCK_FILE = ".inhibit-writers.lock"
_FIRST, _SECOND = "z-first", "a-second"


@pytest.fixture
def inhibit_store(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(state, "current_boot_id", lambda: _BOOT)
    return tmp_path


def _begin(name, channel="*"):
    return state.begin_inhibit(name, channel=channel, reason="test inhibit")


def _path(directory, name):
    return directory / f"inhibit-{name}.json"


def _inhibit_files(directory):
    return {path.name: path.read_bytes() for path in directory.glob("inhibit-*.json")}


def _hold_lock(handle):
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.fixture
def expected(inhibit_store):
    return {name: _begin(name) for name in (_FIRST, _SECOND)}


def _commit(expected, *, monotonic=time.monotonic, timeout=4.0):
    return state.end_inhibits_if_exactly(
        expected, deadline_monotonic=monotonic() + timeout, monotonic=monotonic,
    )


def test_conditional_end_removes_exact_records_in_caller_order(inhibit_store, expected):
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE, *(_path(inhibit_store, n).name for n in expected)}
    assert _commit(expected) == state.EndResult("removed", (_FIRST, _SECOND))
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE}


@pytest.mark.parametrize("case", ["changed", "missing", "foreign", "malformed", "encoding", "alias"])
def test_conditional_end_mismatch_leaves_every_record_byte_identical(inhibit_store, expected, case):
    second = _path(inhibit_store, _SECOND)
    differs = second
    if case == "changed":
        state.begin_inhibit(_SECOND, channel="*", reason="newer reason")
    elif case == "missing":
        second.unlink()
    elif case == "foreign":
        _begin("different", channel="can9")
        differs = _path(inhibit_store, "different")
    elif case == "alias":
        differs = _path(inhibit_store, "aliased-record")
        differs.write_bytes(second.read_bytes())
    else:
        second.write_bytes(b"{" if case == "malformed" else b"\xff")
    before = _inhibit_files(inhibit_store)
    assert _commit(expected) == state.EndResult("mismatch", detail=differs.name)
    assert _inhibit_files(inhibit_store) == before


def test_conditional_end_ignores_other_boots(inhibit_store, expected, monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(state, "current_boot_id", lambda: _OLD_BOOT)
        _begin("previous")
    stale = _path(inhibit_store, "previous")
    before = stale.read_bytes()
    assert _commit(expected) == state.EndResult("removed", (_FIRST, _SECOND))
    assert _inhibit_files(inhibit_store) == {stale.name: before}


def test_conditional_end_expires_while_another_file_description_holds_lock(inhibit_store, expected):
    before = _inhibit_files(inhibit_store)
    with (inhibit_store / _LOCK_FILE).open("rb+") as holder:
        _hold_lock(holder)
        assert _commit(expected, timeout=0.05) == state.EndResult("expired")
    assert _inhibit_files(inhibit_store) == before


def test_conditional_end_refuses_unopenable_writer_lock(inhibit_store, expected):
    lock = inhibit_store / _LOCK_FILE
    lock.unlink()
    lock.mkdir()
    before = _inhibit_files(inhibit_store)
    assert _commit(expected) == state.EndResult("unavailable")
    assert _inhibit_files(inhibit_store) == before


@pytest.mark.parametrize("error,outcome", [(FileNotFoundError, "mismatch"), (OSError, "unavailable")])
def test_first_unlink_failure_removes_nothing(inhibit_store, expected, monkeypatch, error, outcome):
    before = _inhibit_files(inhibit_store)
    remove = mock.Mock(side_effect=error("injected unlink error"))
    monkeypatch.setattr(os, "unlink", remove)
    assert _commit(expected) == state.EndResult(outcome, detail=_FIRST)
    remove.assert_called_once_with(_path(inhibit_store, _FIRST))
    assert _inhibit_files(inhibit_store) == before


def test_second_unlink_failure_is_partial(inhibit_store, expected, monkeypatch):
    unlink = os.unlink

    def fail_second(path):
        if path == _path(inhibit_store, _SECOND):
            raise OSError("injected unlink error")
        unlink(path)

    before = _inhibit_files(inhibit_store)
    monkeypatch.setattr(os, "unlink", fail_second)
    assert _commit(expected) == state.EndResult("partial", (_FIRST,), detail=_SECOND)
    assert _inhibit_files(inhibit_store) == {k: v for k, v in before.items() if k.endswith(f"{_SECOND}.json")}


@pytest.mark.parametrize("crash_on", [_FIRST, _SECOND])
@pytest.mark.parametrize("second_state", ["matches", "changed", "malformed"])
def test_crash_cannot_hide_or_unlink_unverified_records(
    inhibit_store, expected, monkeypatch, crash_on, second_state,
):
    second = _path(inhibit_store, _SECOND)
    if second_state == "changed":
        state.begin_inhibit(_SECOND, channel="*", reason="replacement")
    elif second_state == "malformed":
        second.write_text("{", encoding="utf-8")
    before = _inhibit_files(inhibit_store)
    unlink = os.unlink

    def crash_at_unlink(path):
        if path == _path(inhibit_store, crash_on):
            raise SystemExit("injected crash")
        unlink(path)

    remove = mock.Mock(side_effect=crash_at_unlink)
    monkeypatch.setattr(os, "unlink", remove)
    if second_state == "matches":
        with pytest.raises(SystemExit, match="injected crash"):
            _commit(expected)
        if crash_on == _SECOND:
            del before[_path(inhibit_store, _FIRST).name]
    else:
        assert _commit(expected) == state.EndResult("mismatch", detail=second.name)
        remove.assert_not_called()
    assert _inhibit_files(inhibit_store) == before
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE, *before}


@pytest.mark.parametrize("after_first", [False, True])
def test_deadline_checked_after_scan_and_after_each_fsync(inhibit_store, expected, monkeypatch, after_first):
    clock = mock.Mock(return_value=0.0)
    before = _inhibit_files(inhibit_store)
    if after_first:
        fsync = os.fsync

        def slow_sync(fd):
            fsync(fd)
            clock.return_value = 3.0

        monkeypatch.setattr(os, "fsync", slow_sync)
        assert _commit(expected, monotonic=clock, timeout=2.0) == state.EndResult(
            "partial", (_FIRST,), detail=_SECOND,
        )
        del before[_path(inhibit_store, _FIRST).name]
    else:
        read_text = Path.read_text

        def slow_read(path, *args, **kwargs):
            result = read_text(path, *args, **kwargs)
            clock.return_value = 3.0
            return result

        monkeypatch.setattr(Path, "read_text", slow_read)
        assert _commit(expected, monotonic=clock, timeout=2.0) == state.EndResult("expired", detail=_FIRST)
    assert _inhibit_files(inhibit_store) == before


@pytest.mark.parametrize("singleton", [False, True])
def test_fsync_error_keeps_successful_unlink_in_result(inhibit_store, monkeypatch, singleton):
    names = (_FIRST,) if singleton else (_FIRST, _SECOND)
    expected = {name: _begin(name) for name in names}
    monkeypatch.setattr(os, "fsync", mock.Mock(side_effect=OSError("directory sync failed")))
    result = _commit(expected)
    assert result == state.EndResult(
        "removed" if singleton else "partial", (_FIRST,), durable=False, detail="directory sync failed",
    )
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE, *(_path(inhibit_store, n).name for n in names[1:])}


@pytest.mark.parametrize("unopenable", [False, True])
def test_fallback_publishes_a_single_wildcard_without_replacing_canonical(inhibit_store, monkeypatch, unopenable):
    name = "x" * 200
    canonical = _begin(name, "can0")
    before = _inhibit_files(inhibit_store)
    lock = inhibit_store / _LOCK_FILE
    if unopenable:
        lock.unlink()
        lock.mkdir()
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    boot = mock.Mock(side_effect=[_BOOT, _BOOT])
    with (nullcontext() if unopenable else lock.open("rb+")) as holder:
        if holder is not None:
            _hold_lock(holder)
        with monkeypatch.context() as once:
            once.setattr(state, "current_boot_id", boot)
            first = _begin(name, "can1")
            second = _begin(name, "can2")
    assert boot.call_count == 2
    fallback_name = f"nolock.{_BOOT.replace('-', '')}.{hashlib.sha256(name.encode()).hexdigest()}"
    assert first["name"] == second["name"] == fallback_name
    assert first["channel"] == second["channel"] == "*"
    assert second["boot_id"] == _BOOT
    assert second["stands_for"] == name
    assert second["reason"] == "test inhibit [written without the inhibit writer lock; requested channel can2]"
    fallback = _path(inhibit_store, fallback_name)
    assert len(fallback.name) == 117
    assert json.loads(fallback.read_text()) == second
    assert _inhibit_files(inhibit_store) == {**before, fallback.name: fallback.read_bytes()}
    assert state.active_inhibits("can9") == (second,)
    assert state.all_active_inhibits() == (second, canonical)
    assert state.inhibit_is_for(canonical, name)
    assert state.inhibit_is_for(second, name)
    assert not state.inhibit_is_for(second, "other")
    assert not state.inhibit_is_for({}, name)
    assert state.inhibit_is_for({"stands_for": name}, name)


def test_fallback_already_on_disk_blocks_conditional_end(inhibit_store, expected, monkeypatch):
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    with (inhibit_store / _LOCK_FILE).open("rb+") as holder:
        _hold_lock(holder)
        fallback = _begin(_SECOND)
    before = _inhibit_files(inhibit_store)
    assert _commit(expected) == state.EndResult("mismatch", detail=_path(inhibit_store, fallback["name"]).name)
    assert _inhibit_files(inhibit_store) == before


def test_reserved_fallback_names_cannot_be_written_or_conditionally_ended(inhibit_store):
    with pytest.raises(ValueError, match="reserved inhibit name"):
        _begin("nolock.x")
    with pytest.raises(ValueError, match="reserved inhibit name"):
        _commit({"nolock.x": {}})
    assert not list(inhibit_store.iterdir())


@pytest.mark.parametrize("held", [False, True])
def test_writer_with_no_boot_id_refuses_before_waiting_or_writing(inhibit_store, monkeypatch, held):
    lock = inhibit_store / _LOCK_FILE
    now = mock.Mock(side_effect=AssertionError("must not start waiting"))
    with (lock.open("wb+") if held else nullcontext()) as holder:
        if holder is not None:
            _hold_lock(holder)
        with monkeypatch.context() as failure:
            failure.setattr(state, "current_boot_id", lambda: "")
            failure.setattr(state.time, "monotonic", now)
            with pytest.raises(RuntimeError, match="^cannot establish current boot id$"):
                _begin("alfaobd")
    assert set(os.listdir(inhibit_store)) == ({_LOCK_FILE} if held else set())
    assert state.all_active_inhibits() == ()


def test_pruning_never_unlinks_concurrently_published_current_boot_fallback(inhibit_store, monkeypatch):
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    lock = inhibit_store / _LOCK_FILE
    with lock.open("wb+") as holder, monkeypatch.context() as old:
        _hold_lock(holder)
        old.setattr(state, "current_boot_id", lambda: _OLD_BOOT)
        stale = _begin("shared", "can0")
    stale_path = _path(inhibit_store, stale["name"])
    stale_path.write_text("{", encoding="utf-8")  # Filename, not the payload, proves the boot.
    malformed_name = _path(inhibit_store, "nolock.not-a-boot-id")
    malformed_name.write_text("{", encoding="utf-8")
    entered, resume = threading.Event(), threading.Event()
    unlink = Path.unlink

    def paused_prune(path, *args, **kwargs):
        if path == stale_path:
            entered.set()
            assert resume.wait(5)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", paused_prune)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pruner = executor.submit(_begin, "unrelated")
        try:
            assert entered.wait(5)
            current = _begin("shared", "can1")
            current_path = _path(inhibit_store, current["name"])
            before = current_path.read_bytes()
        finally:
            resume.set()
        pruner.result(timeout=5)
    assert current_path != stale_path
    assert current_path.read_bytes() == before
    assert not stale_path.exists()
    assert malformed_name.exists()
    assert state.end_inhibit("unrelated")
    assert current_path.read_bytes() == before


def test_rollback_reader_and_unlink_can_manage_fallback_only_wildcard(inhibit_store, monkeypatch):
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    with (inhibit_store / _LOCK_FILE).open("wb+") as holder:
        _hold_lock(holder)
        fallback = _begin("alfaobd", "can0")

    def pre_change_active(channel):
        active = []
        for path in sorted(inhibit_store.glob("inhibit-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                active.append({"name": path.name, "channel": channel, "invalid": True})
            elif payload.get("boot_id") == _BOOT and payload.get("channel") in (channel, "*"):
                active.append(payload)
        return tuple(active)

    def pre_change_end(name):
        try:
            _path(inhibit_store, name).unlink()
        except FileNotFoundError:
            return False
        return True

    assert pre_change_active("can9") == (fallback,)
    assert not pre_change_end("alfaobd")
    assert pre_change_end(fallback["name"])
    assert pre_change_active("can9") == ()
    assert state.all_active_inhibits() == ()


def test_fallback_writer_between_unlinks_never_loses_its_record(inhibit_store, expected, monkeypatch):
    fsync = os.fsync
    submitted = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        def delayed_first_fsync(fd):
            fsync(fd)
            if not submitted and not _path(inhibit_store, _FIRST).exists() and _path(inhibit_store, _SECOND).exists():
                submitted.append(executor.submit(_begin, _SECOND, "can7").result(timeout=5))
                assert state.inhibit_is_for(submitted[0], _SECOND)
                assert _path(inhibit_store, _SECOND).exists()

        # The worker uses the production 2 s wait. Its temp-file fsync is not a directory fsync.
        def sync_if_directory(fd):
            if os.path.samestat(os.fstat(fd), inhibit_store.stat()):
                delayed_first_fsync(fd)
            else:
                fsync(fd)

        monkeypatch.setattr(os, "fsync", sync_if_directory)
        assert _commit(expected, timeout=8) == state.EndResult("removed", (_FIRST, _SECOND))
    assert len(submitted) == 1
    assert state.active_inhibits("can0") == (submitted[0],)
    assert state.all_active_inhibits() == (submitted[0],)
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE, _path(inhibit_store, submitted[0]["name"]).name}


def test_manual_end_refuses_when_busy_and_then_removes_both_names(inhibit_store, monkeypatch):
    _begin("alfaobd")
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(state, "WRITER_LOCK_END_WAIT_SECONDS", 0.05)
    with (inhibit_store / _LOCK_FILE).open("rb+") as holder:
        _hold_lock(holder)
        fallback = _begin("alfaobd", "can7")
        before = _inhibit_files(inhibit_store)
        with pytest.raises(state.InhibitStateBusy, match="no inhibits ended"):
            state.end_inhibit("alfaobd")
        assert _inhibit_files(inhibit_store) == before
    assert _path(inhibit_store, fallback["name"]).exists()
    assert state.end_inhibit("alfaobd")
    assert not state.end_inhibit("alfaobd")
    assert set(os.listdir(inhibit_store)) == {_LOCK_FILE}


def test_end_without_boot_id_does_not_prune_fallbacks(inhibit_store, monkeypatch):
    _begin("alfaobd")
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    with (inhibit_store / _LOCK_FILE).open("rb+") as holder:
        _hold_lock(holder)
        fallback = _begin("alfaobd", "can7")
        with monkeypatch.context() as old:
            old.setattr(state, "current_boot_id", lambda: _OLD_BOOT)
            stale = _begin("alfaobd", "can9")
    stale_path = _path(inhibit_store, stale["name"])
    with monkeypatch.context() as failure:
        failure.setattr(state, "current_boot_id", lambda: "")
        assert state.end_inhibit("alfaobd")
    assert stale_path.exists()
    assert state.all_active_inhibits() == (fallback,)
    assert not state.end_inhibit("unrelated")
    assert not stale_path.exists()
    assert state.all_active_inhibits() == (fallback,)


def test_cli_reports_busy_and_ends_fallback_by_its_printed_name(inhibit_store, monkeypatch, capsys):
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(state, "WRITER_LOCK_END_WAIT_SECONDS", 0.05)
    with (inhibit_store / _LOCK_FILE).open("wb+") as holder:
        _hold_lock(holder)
        assert cli.main(["inhibit-begin", "user", "--channel", "can0", "--reason", "test"]) == 0
        record = json.loads(capsys.readouterr().out)
        assert record["stands_for"] == "user"
        assert cli.main(["inhibit-end", "user"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "ERROR: cannot acquire inhibit writer lock; no inhibits ended\n"
    assert cli.main(["status", "--channel", "can9"]) == 0
    assert json.loads(capsys.readouterr().out)["active_inhibits"] == [record]
    assert cli.main(["inhibit-end", record["name"]]) == 0
    assert json.loads(capsys.readouterr().out) == {"name": record["name"], "removed": True}
    assert state.all_active_inhibits() == ()
