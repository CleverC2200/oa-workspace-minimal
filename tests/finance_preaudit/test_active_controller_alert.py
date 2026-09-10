from __future__ import annotations

import json

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import controller_handoff
from scripts import finance_preaudit_concurrency_probe as probe


@pytest.fixture
def notifications(monkeypatch):
    alerts, clears = [], []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    monkeypatch.setattr(probe.session_alert, "clear_active_alert", lambda **kw: clears.append(kw))
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("a" * 40, False))
    monkeypatch.setattr(probe, "_run_rolling_drain_locked", lambda **kw: pytest.fail("duplicate must not start work"))
    return alerts, clears


def _call(capsys):
    result = probe.main(["--rolling", "--adaptive", "--capacity", "6", "--confirm-write", "YES"])
    return result, json.loads(capsys.readouterr().out)


def test_verified_active_controller_skips_without_notifying_or_clearing(notifications, capsys):
    with runtime_control.global_controller_lease():
        controller_handoff.begin({"capacity": 6})
        before = controller_handoff.manifest_file().read_bytes()
        result, payload = _call(capsys)
        assert notifications == ([], [])
        assert result == 0
        assert payload["status"] == "skipped"
        assert payload["reason"] == "production_rolling_controller_active"
        assert payload["controller_handoff"]["lock_owner_consistent"] is True
        assert "notification" not in payload
        assert "error_code" not in payload
        assert controller_handoff.manifest_file().read_bytes() == before
        assert runtime_control.controller_active()


@pytest.mark.parametrize("state", ["missing", "invalid", "wrong_owner", "terminal", "code_changed", "stopped", "status_error"])
def test_untrusted_or_stopping_active_controller_retains_failure(state, notifications, monkeypatch, capsys):
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 6})
        path = controller_handoff.manifest_file()
        if state == "missing":
            path.unlink()
        elif state == "invalid":
            path.write_text("{}")
        elif state == "wrong_owner":
            controller_handoff._write_manifest({**manifest, "controller_pid": manifest["controller_pid"] + 1})
        elif state == "terminal":
            controller_handoff.finish(manifest, status="failed", children_reaped=True, health="degraded")
        elif state == "code_changed":
            monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("b" * 40, False))
        elif state == "stopped":
            runtime_control.request_stop()
        elif state == "status_error":
            def fail():
                raise OSError("redacted status unavailable")
            monkeypatch.setattr(controller_handoff, "status", fail)
        result, payload = _call(capsys)
        assert result == 1
        assert payload["error_code"] == (
            "controller_handoff_untrusted" if state == "invalid"
            else "production_probe_stopped" if state == "stopped"
            else "production_rolling_controller_active"
        )
        assert notifications == (["controller_failed"], [])


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
        assert payload["error_code"] == "production_rolling_controller_active"
        assert notifications == (["controller_failed"], [])
