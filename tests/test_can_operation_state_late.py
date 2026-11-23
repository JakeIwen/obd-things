"""Late-removal semantics of the conditional inhibit end used by USB drop recovery."""

import os
import threading
from pathlib import Path
from unittest import mock

import pytest

from lib import can_operation_state as state
from tests.test_can_operation_state import (
    _FIRST, _SECOND, _begin, _commit, _inhibit_files, _path, expected, inhibit_store,
)


def _slow_unlink(monkeypatch, clock, slow_name):
    real = os.unlink

    def unlink(path, *args, **kwargs):
        real(path, *args, **kwargs)
        if Path(path).name == f"inhibit-{slow_name}.json":
            clock.return_value = 3.0

    monkeypatch.setattr(os, "unlink", unlink)


def test_late_first_unlink_of_two_is_partial_and_leaves_second_untouched(inhibit_store, expected, monkeypatch):
    clock = mock.Mock(return_value=0.0)
    _slow_unlink(monkeypatch, clock, _FIRST)
    second = _path(inhibit_store, _SECOND).read_bytes()
    assert _commit(expected, monotonic=clock, timeout=2.0) == state.EndResult(
        "partial", (_FIRST,), detail=_SECOND, late=True,
    )
    assert _inhibit_files(inhibit_store) == {_path(inhibit_store, _SECOND).name: second}
    assert [r["name"] for r in state.active_inhibits("can0")] == [_SECOND]


@pytest.mark.parametrize("singleton", [False, True])
def test_late_last_unlink_is_removed_late_and_never_recreated(inhibit_store, monkeypatch, singleton):
    names = (_FIRST,) if singleton else (_FIRST, _SECOND)
    expected = {name: _begin(name) for name in names}
    clock = mock.Mock(return_value=0.0)
    _slow_unlink(monkeypatch, clock, names[-1])
    result = _commit(expected, monotonic=clock, timeout=2.0)
    assert result == state.EndResult("removed", names, late=True)
    assert _inhibit_files(inhibit_store) == {}
    assert state.active_inhibits("can0") == ()


def test_reader_sees_record_or_absence_while_unlink_stalls(inhibit_store, expected, monkeypatch):
    real = os.unlink
    clock = mock.Mock(return_value=0.0)
    # Hold the syscall on both sides of its observable effect. The reader never
    # takes the writer lock and can already act while the writer is still inside.
    before, proceed, after, finish = (threading.Event() for _ in range(4))

    def stalled(path, *args, **kwargs):
        if Path(path).name != f"inhibit-{_SECOND}.json":
            return real(path, *args, **kwargs)
        before.set()
        assert proceed.wait(5)
        real(path, *args, **kwargs)
        after.set()
        assert finish.wait(5)

    monkeypatch.setattr(os, "unlink", stalled)
    result = []
    worker = threading.Thread(target=lambda: result.append(_commit(expected, monotonic=clock, timeout=2)))
    worker.start()
    try:
        assert before.wait(5)
        assert [r['name'] for r in state.active_inhibits('can0')] == [_SECOND]
        clock.return_value = 3.0
        proceed.set()
        assert after.wait(5)
        assert not state.active_inhibits('can0')
    finally:
        proceed.set()
        finish.set()
        worker.join(5)
    assert result == [state.EndResult("removed", (_FIRST, _SECOND), late=True)]
    assert _inhibit_files(inhibit_store) == {}


@pytest.mark.parametrize("took_effect", [False, True])
def test_process_death_at_final_unlink_leaves_readers_consistent(inhibit_store, expected, monkeypatch, took_effect):
    real = os.unlink

    def dying(path, *args, **kwargs):
        if Path(path).name == f"inhibit-{_SECOND}.json":
            if took_effect:
                real(path, *args, **kwargs)
            raise SystemExit("simulated crash")
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", dying)
    with pytest.raises(SystemExit):
        _commit(expected)
    remaining = [r["name"] for r in state.active_inhibits("can0")]
    assert remaining == ([] if took_effect else [_SECOND])
    assert set(_inhibit_files(inhibit_store)) == (set() if took_effect else {_path(inhibit_store, _SECOND).name})
