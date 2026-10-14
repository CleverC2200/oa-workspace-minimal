from __future__ import annotations

import json

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import controller_handoff, approval_lifecycle, approval_guard
from scripts import finance_preaudit_concurrency_probe as probe


@pytest.fixture
def notifications(monkeypatch):
    alerts, clears = [], []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code, **_kwargs: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    monkeypatch.setattr(probe.session_alert, "clear_active_alert", lambda **kw: clears.append(kw))
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("a" * 40, False))
    monkeypatch.setattr(probe, "_run_rolling_drain_locked", lambda **kw: pytest.fail("duplicate must not start work"))
    monkeypatch.setattr(approval_guard, "observe_project_errors", lambda _: None)
    return alerts, clears


def _call(capsys):
    result = probe.main(["--rolling", "--fixed-capacity", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"])
    return result, json.loads(capsys.readouterr().out)


def test_verified_active_controller_skips_without_notifying_or_clearing(notifications, capsys):
    with runtime_control.global_controller_lease(), approval_lifecycle.run_scope(controller_handoff.begin({"capacity": 3}), probe.PROJECT_ROOT):
        before = controller_handoff.manifest_file().read_bytes()
        result, payload = _call(capsys)
        assert notifications == ([], [])
        assert result == 0
        assert payload["status"] == "skipped"
        assert payload["reason"] == "previous_run_busy"
        assert "notification" not in payload
        assert "error_code" not in payload
        assert controller_handoff.manifest_file().read_bytes() == before
        assert runtime_control.controller_active()


@pytest.mark.parametrize("state", ["missing", "invalid", "wrong_owner", "terminal", "stale_progress", "stopped", "status_error"])
def test_untrusted_or_stopping_active_controller_retains_failure(state, notifications, monkeypatch, capsys):
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 6})
        with approval_lifecycle.run_scope(manifest, probe.PROJECT_ROOT):
            pass
        path = controller_handoff.manifest_file()
        if state == "missing":
            path.unlink()
        elif state == "invalid":
            path.write_text("{}")
        elif state == "wrong_owner":
            controller_handoff._write_manifest({**manifest, "controller_pid": manifest["controller_pid"] + 1})
        elif state == "terminal":
            controller_handoff.finish(manifest, status="failed", children_reaped=True, health="degraded")
        elif state == "stale_progress":
            progress_path = approval_lifecycle.run_directory(manifest["run_id"]) / "progress.json"
            approval_guard.write(progress_path, {**approval_guard.read(progress_path), "heartbeat_at": 1})
        elif state == "stopped":
            runtime_control.request_stop()
        elif state == "status_error":
            def fail():
                raise OSError("redacted status unavailable")
            monkeypatch.setattr(approval_lifecycle, "_progress", lambda _: fail())
        result, payload = _call(capsys)
        assert result == 1
        if state in {"invalid", "status_error", "wrong_owner"}:
            assert payload["error_code"] == {"invalid": "controller_handoff_untrusted", "status_error": "controller_failed",
                                             "wrong_owner": "approval_run_untrusted"}[state]
            assert notifications == (["controller_failed"], [])
        else:
            assert payload["status"] == "blocked"
            assert payload["reason"] == {"missing": "previous_run_ownership_unknown",
                "terminal": "previous_run_ownership_unknown", "stale_progress": "previous_run_progress_unknown", "stopped": "existing_stop_preserved"}.get(state, payload.get("reason"))
            assert notifications == ([], [])


def test_active_error_without_current_lock_is_not_silenced(notifications, monkeypatch, capsys):
    def fail(**kw):
        raise ValueError("production_rolling_controller_active")
    monkeypatch.setattr(probe, "run_rolling_drain", fail)
    result, payload = _call(capsys)
    assert result == 1
    assert payload["error_code"] == "production_rolling_controller_active"
    assert notifications == (["controller_failed"], [])


@pytest.mark.parametrize("code_state", [("unknown", "unknown"), ("a" * 40, "unknown")])
def test_unknown_code_state_cannot_verify_active_controller(code_state, notifications, monkeypatch, capsys):
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: code_state)
    with runtime_control.global_controller_lease():
        controller_handoff.begin({"capacity": 6})
        result, payload = _call(capsys)
        assert result == 1
        assert payload["status"] == "blocked" and payload["reason"] == "previous_run_ownership_unknown"
        assert notifications == ([], [])
