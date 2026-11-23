import fcntl
from types import SimpleNamespace
from unittest import mock

from projects.tpms import tpms_logger


def test_auto_mode_honors_fallback_restoration_inhibit_after_restart(tmp_path, monkeypatch):
    state = tpms_logger.can_operation_state
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(state, "current_boot_id", lambda: "12345678-1234-abcd-1234-123456789abc")
    monkeypatch.setattr(state, "WRITER_LOCK_WAIT_SECONDS", 0.05)
    with (tmp_path / ".inhibit-writers.lock").open("wb+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = state.begin_inhibit(tpms_logger.RESTORATION_INHIBIT_NAME, channel="*", reason="test")
    assert record["name"].startswith("nolock.")
    with (
        mock.patch.object(tpms_logger, "_restoration_failed", False),
        mock.patch.object(tpms_logger, "TelemetryClient"),
        mock.patch.object(tpms_logger, "broker_absence_proven", return_value=True),
        mock.patch.object(tpms_logger, "_resolved_rfh_route", return_value=SimpleNamespace(
            module=SimpleNamespace(channel="can9"),
        )),
        mock.patch.object(tpms_logger, "_passive_running_ready") as passive_gate,
        mock.patch.object(tpms_logger, "log_session") as session,
        mock.patch.object(tpms_logger.time, "sleep", side_effect=KeyboardInterrupt) as sleep,
    ):
        tpms_logger.main(["--auto"])
    sleep.assert_called_once_with(tpms_logger.IDLE_SLEEP_S)
    passive_gate.assert_not_called()
    session.assert_not_called()
