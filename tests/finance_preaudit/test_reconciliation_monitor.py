from __future__ import annotations

import hashlib
import json

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import cli, controller_handoff, process_pool, reconciliation, session_alert
from scripts import finance_preaudit_concurrency_probe as probe


def live_status() -> dict:
    return {
        "credentials": {"status": "ok"},
        "oa": {
            "execution_identity": "verified",
            "by_workflow": {"travel_expense": 1},
        },
    }


def reconciled() -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_reconciliation",
        "read_only": True,
        "quarantine": {"pending_quarantined": 0, "actionable_pending": 1},
        "continuation": {
            "status": "ready", "scope": "none",
            "isolated_pending": 0, "blocking_reasons": {},
        },
    }


def stopped_pipeline(count: int, capacity: int) -> process_pool.PoolResult[dict]:
    runtime_control.request_stop("production_reconciliation_blocked")
    return process_pool.PoolResult(
        requested=count, started=1, completed=0, failed=0, timed_out=0,
        stopped=1, peak_active=capacity, elapsed_seconds=0.01,
        outcomes=(process_pool.TaskOutcome(
            index=0, status="stopped", value=None, error_code="stop_requested",
        ),),
    )


def test_specific_reconciliation_stop_survives_controller_exit():
    result = probe.run_rolling_drain(
        capacity=3, live_status=live_status, reconcile_status=reconciled,
        pipeline_runner=stopped_pipeline,
    )
    assert result["status"] == "failed"
    assert result["primary_error_code"] == "production_reconciliation_blocked"
    assert result["controller_handoff"]["status"] == "failed"
    assert result["processes_reaped"] is True
    assert runtime_control.stop_file().read_bytes() == (
        b"stop_requested:production_reconciliation_blocked\n"
    )


def test_terminal_cli_sends_the_specific_stop_once(monkeypatch, capsys):
    # main has no adapter arguments; route its call to the real public runner
    # with the existing offline IO seams instead of invoking production IO.
    rolling = probe.run_rolling_drain
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kwargs: rolling(
        **kwargs, live_status=live_status, reconcile_status=reconciled,
        pipeline_runner=stopped_pipeline,
    ))
    notifications = []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code: (
        notifications.append(code)
        or {"error_code": code, "delivery_state": "sent"}
    ))
    exit_code = probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["primary_error_code"] == "production_reconciliation_blocked"
    assert payload["notification"]["error_code"] == "production_reconciliation_blocked"
    assert notifications == ["production_reconciliation_blocked"]


def test_running_status_identifies_gate_scope_without_relaxing_the_conflict(tmp_path, capsys):
    root = tmp_path / "flows"
    received_at = "2026-10-09T08:00:00+00:00"
    case_key = hashlib.sha256(f"synthetic-request\x1f{received_at}".encode()).hexdigest()
    state_file = root / "travel_expense" / "cases" / case_key / "attempt-1" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "schema_version": 1, "kind": "preaudit_action", "workflow_key": "travel_expense",
        "case_key": case_key, "attempt": 1, "request_id": "synthetic-request",
        "received_at": received_at, "created_at": "2026-10-09T08:00:01+00:00",
        "execution_state": "closed", "business_state": "needs_human",
        "write_state": "submitted", "reason": "",
    }))
    before = state_file.read_bytes()
    pending = [{"request_id": "synthetic-request", "received_at": received_at}]

    def query():
        assert cli.main(
            ["status", "--root", str(root), "--reconcile", "--summary"],
            runtime_status=lambda _root: {"schema_version": 1, "kind": "preaudit_runtime_status"},
            reconcile_status=lambda current: reconciliation.reconcile(
                current, pending_rows=pending, handled_rows=[],
            ),
        ) == 0
        return json.loads(capsys.readouterr().out)

    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        output = query()
        assert output["reconciliation"]["usage"] == {
            "gate_scope": "startup_or_terminal",
            "snapshot_consistency": "non_atomic",
            "running_monitor": "original_execution_progress",
        }
        assert output["reconciliation"]["continuation"]["blocking_reasons"] == {
            "pending_write_state_unresolved": 1,
        }
        assert output["reconciliation"]["continuation"]["status"] == "blocked"
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
    assert query()["reconciliation"]["continuation"]["status"] == "blocked"
    assert state_file.read_bytes() == before
    assert runtime_control.stop_requested() is False


def test_specific_alert_describes_reconciliation_instead_of_only_startup(tmp_path):
    messages = []
    receipt = session_alert.alert_once(
        "production_reconciliation_blocked", chat_id="synthetic-chat",
        control_root=tmp_path,
        sender=lambda _chat, text, _key: messages.append(text),
    )
    assert receipt["delivery_state"] == "sent"
    assert len(messages) == 1
    assert "生产对账门禁未通过" in messages[0]
    assert "本地对账与控制器回执" in messages[0]
    assert "生产启动门禁未通过" not in messages[0]
