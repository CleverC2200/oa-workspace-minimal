from __future__ import annotations

from scripts.finance_preaudit_concurrency_probe import PoolResult, TaskOutcome

import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import cli, controller_handoff, reconciliation, session_alert
from scripts import finance_preaudit_concurrency_probe as probe, finance_preaudit_run


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


def stopped_pipeline(count: int, capacity: int) -> PoolResult[dict]:
    runtime_control.request_stop("production_reconciliation_blocked")
    return PoolResult(
        requested=count, started=1, completed=0, failed=0, timed_out=0,
        stopped=1, peak_active=capacity, elapsed_seconds=0.01,
        outcomes=(TaskOutcome(
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
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code, **_kwargs: (
        notifications.append(code)
        or {"error_code": code, "delivery_state": "sent"}
    ))
    exit_code = probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["primary_error_code"] == "production_reconciliation_blocked"
    assert payload["notification"]["error_code"] == "production_reconciliation_blocked"
    assert notifications == ["production_reconciliation_blocked"]


@pytest.mark.parametrize("write_state,handled,reason", [
    ("submitted", False, "pending_write_state_unresolved"),
    ("submitting", False, "pending_write_state_unresolved"),
    ("unknown", False, "pending_write_state_unresolved"),
    ("submitted", True, "pending_handled_conflict"),
])
def test_running_status_identifies_gate_scope_without_relaxing_the_conflict(
    tmp_path, capsys, write_state, handled, reason,
):
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
        "write_state": write_state, "reason": "",
    }))
    before = state_file.read_bytes()
    pending = [{"request_id": "synthetic-request", "received_at": received_at}]
    handled_rows = [{"request_id": "synthetic-request"}] if handled else []

    def query():
        status_file = tmp_path / f"status-{uuid4().hex}.json"
        assert cli.main(
            ["status", "--root", str(root), "--reconcile", "--summary", "--output-file", str(status_file)],
            runtime_status=lambda _root: {"schema_version": 1, "kind": "preaudit_runtime_status"},
            reconcile_status=lambda current: reconciliation.reconcile(
                current, pending_rows=pending, handled_rows=handled_rows,
            ),
        ) == 0
        output = json.loads(capsys.readouterr().out)
        raw = status_file.read_bytes()
        assert output["status_file"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert json.loads(raw)["reconciliation"]["continuation"] == output["reconciliation"]["continuation"]
        return output

    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        output = query()
        assert output["reconciliation"]["usage"] == {
            "gate_scope": "startup_or_terminal",
            "snapshot_consistency": "non_atomic",
            "running_monitor": "original_execution_progress",
        }
        assert output["reconciliation"]["continuation"]["blocking_reasons"] == {
            reason: 1,
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


@pytest.mark.parametrize("prior,expected_code,expected_preserved", [
    ("plain", None, True),
    ("invalid", None, True),
    ("cycle", None, False),
    ("write_authorization_invalid", "write_authorization_invalid", True),
])
def test_prior_stops_are_not_replaced_by_reconciliation(prior, expected_code, expected_preserved):
    def pipeline(count, capacity):
        if prior == "plain":
            runtime_control.request_stop()
        elif prior == "invalid":
            runtime_control.stop_file().parent.mkdir(parents=True, exist_ok=True)
            runtime_control.stop_file().write_bytes(b"stop_requested:unrecognized_reason\n")
            runtime_control.stop_file().chmod(0o600)
        elif prior == "cycle":
            runtime_control.request_cycle_stop()
        else:
            runtime_control.request_stop(prior)
        before = runtime_control.stop_file().read_bytes()
        result = stopped_pipeline(count, capacity)
        assert runtime_control.stop_file().read_bytes() == before
        return result

    result = probe.run_rolling_drain(
        capacity=3, live_status=live_status, reconcile_status=reconciled,
        pipeline_runner=pipeline,
    )
    assert result["primary_error_code"] == expected_code
    assert runtime_control.stop_requested() is expected_preserved


def test_first_writer_failure_is_not_replaced_by_a_later_stop(monkeypatch, capsys):
    def pipeline(count, capacity):
        runtime_control.request_stop("production_reconciliation_blocked")
        return PoolResult(
            requested=count, started=1, completed=0, failed=1, timed_out=0,
            stopped=0, peak_active=capacity, elapsed_seconds=0.01,
            primary_error_code="writer_result_unknown",
            outcomes=(TaskOutcome(
                index=0, status="failed", value=None, error_code="writer_result_unknown",
            ),),
        )

    rolling = probe.run_rolling_drain
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kwargs: rolling(
        **kwargs, live_status=live_status, reconcile_status=reconciled, pipeline_runner=pipeline,
    ))
    alerts = []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code, **_kwargs: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    assert probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary_error_code"] == "writer_result_unknown"
    assert alerts == ["writer_result_unknown"]


def test_original_execution_retrieves_the_specific_terminal_result(tmp_path, capsys):
    owner = str(uuid4())
    directory = runtime_control.controller_lock_file().parent / "executions" / uuid4().hex
    # The real controller and supervisor run in a child, with only external IO
    # adapters replaced. Manifest, PID, exit status and hashes stay real.
    code = f'''import runpy
from scripts import finance_preaudit_concurrency_probe as probe
helpers = runpy.run_path({str(Path(__file__).resolve())!r})
rolling = probe.run_rolling_drain
probe.run_rolling_drain = lambda **kw: rolling(**kw,
    live_status=helpers["live_status"], reconcile_status=helpers["reconciled"],
    pipeline_runner=helpers["stopped_pipeline"])
probe.session_alert.alert_from_environment = lambda code, **_kwargs: {{"error_code": code, "delivery_state": "sent"}}
raise SystemExit(probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"]))
'''
    receipt = finance_preaudit_run.supervise(
        [sys.executable, "-B", "-c", code], directory=directory, owner_task_id=owner,
    )
    assert receipt["exit_code"] == 1
    assert receipt["controller_state"] == "failed"
    assert receipt["children_reaped"] is True
    capsys.readouterr()
    before = {path: path.read_bytes() for path in directory.iterdir()}
    assert finance_preaudit_run.main([
        "--owner-task-id", owner, "--receipt-file", receipt["receipt_file"],
        "--invocation-id", directory.name, "--retrieve-result",
    ]) == 1
    retrieved = json.loads(capsys.readouterr().out)
    assert retrieved["terminal"] is True
    output = json.loads(retrieved["controller_stdout"])
    assert output["primary_error_code"] == "production_reconciliation_blocked"
    assert output["notification"]["error_code"] == "production_reconciliation_blocked"
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_original_progress_waits_while_writer_is_active_without_oa_or_stop(tmp_path, monkeypatch, capsys):
    owner = str(uuid4())
    directory = runtime_control.controller_lock_file().parent / "executions" / uuid4().hex
    entered = tmp_path / "writer-entered"
    release = tmp_path / "release-writer"
    child = f'''import runpy,time
from pathlib import Path
from apps.oa_approval import runtime_control
from scripts import finance_preaudit_concurrency_probe as probe
helpers = runpy.run_path({str(Path(__file__).resolve())!r})
rolling = probe.run_rolling_drain
def pipeline(count, capacity):
    with runtime_control.request_writer_lease("synthetic-request"):
        Path({str(entered)!r}).touch()
        deadline = time.monotonic() + 10
        while not Path({str(release)!r}).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test release timeout")
            time.sleep(.01)
        return helpers["stopped_pipeline"](count, capacity)
probe.run_rolling_drain = lambda **kw: rolling(**kw,
    live_status=helpers["live_status"], reconcile_status=helpers["reconciled"], pipeline_runner=pipeline)
probe.session_alert.alert_from_environment = lambda code, **_kwargs: {{"error_code": code, "delivery_state": "sent"}}
raise SystemExit(probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"]))
'''
    supervisor = (
        "from pathlib import Path; from scripts.finance_preaudit_run import supervise; import sys; "
        f"supervise([sys.executable, '-B', '-c', {child!r}], directory=Path({str(directory)!r}), owner_task_id={owner!r})"
    )
    with subprocess.Popen(
        [sys.executable, "-B", "-c", supervisor], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=dict(os.environ),
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(process.communicate()[1])
                time.sleep(.01)
            assert entered.exists()
            assert runtime_control.active_request_writers() == 1
            receipt_file = directory / "execution.json"
            before = receipt_file.read_bytes()

            def forbidden(*_args, **_kwargs):
                pytest.fail("read-only progress attempted auth, OA, notification or stop")

            monkeypatch.setattr(finance_preaudit_run.auth, "load_local_env", forbidden)
            monkeypatch.setattr(probe.runtime_doctor, "status", forbidden)
            monkeypatch.setattr(probe.session_alert, "alert_from_environment", forbidden)
            monkeypatch.setattr(runtime_control, "request_stop", forbidden)
            assert finance_preaudit_run.main([
                "--owner-task-id", owner, "--receipt-file", str(receipt_file),
                "--invocation-id", directory.name, "--progress",
            ]) == 2
            progress = json.loads(capsys.readouterr().out)
            assert progress["status"] == "waiting"
            assert progress["terminal"] is False
            assert receipt_file.read_bytes() == before
            assert runtime_control.stop_requested() is False
        finally:
            release.touch()
            process.communicate(timeout=5)
    assert process.returncode == 0


@pytest.mark.parametrize("first_error,expected", [
    (None, "production_reconciliation_blocked"),
    ("writer_result_unknown", "writer_result_unknown"),
])
def test_unreaped_children_remain_blocked_and_keep_the_specific_alert(first_error, expected, monkeypatch, capsys):
    def pipeline(count, capacity):
        return replace(stopped_pipeline(count, capacity),
                       processes_reaped=False, primary_error_code=first_error)

    rolling = probe.run_rolling_drain
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kwargs: rolling(
        **kwargs, live_status=live_status, reconcile_status=reconciled, pipeline_runner=pipeline,
    ))
    alerts = []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code, **_kwargs: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    assert probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "production_controller_children_active"
    assert payload["primary_error_code"] == expected
    assert alerts == [expected]
    handoff = controller_handoff.status()
    assert handoff["children_reaped"] is False
    assert handoff["ready_for_start"] is False
    assert runtime_control.stop_requested() is True


@pytest.mark.parametrize("stop_reason,expected_status,expected_primary,stop_preserved", [
    ("production_reconciliation_blocked", "failed", "production_reconciliation_blocked", True),
    (None, "completed", None, True),
    ("controller_cycle_stop", "completed", None, False),
])
def test_specific_stop_is_retained_when_the_last_writer_finishes(
    stop_reason, expected_status, expected_primary, stop_preserved,
):
    pending = [1]

    def pipeline(count, capacity):
        runtime_control.request_stop(stop_reason)
        pending[0] = 0
        return PoolResult(
            requested=count, started=1, completed=1, failed=0, timed_out=0,
            stopped=0, peak_active=capacity, elapsed_seconds=0.01,
            outcomes=(TaskOutcome(
                index=0, status="completed", value={
                    "status": "completed", "business_state": "needs_human", "write_state": "submitted",
                },
            ),),
        )

    def current_live():
        payload = live_status()
        payload["oa"]["by_workflow"]["travel_expense"] = pending[0]
        return payload

    def current_reconciliation():
        payload = reconciled()
        payload["quarantine"]["actionable_pending"] = pending[0]
        return payload

    result = probe.run_rolling_drain(
        capacity=3, live_status=current_live, reconcile_status=current_reconciliation,
        pipeline_runner=pipeline,
    )
    assert result["completed"] == 1
    assert result["stopped"] == 0
    assert result["status"] == expected_status
    assert result["primary_error_code"] == expected_primary
    assert runtime_control.stop_requested() is stop_preserved


@pytest.mark.parametrize("stop_reason,expected_exit,expected_alert", [
    ("production_reconciliation_blocked", 1, "production_reconciliation_blocked"),
    (None, 0, None),
    ("controller_cycle_stop", 0, None),
])
def test_retry_pending_does_not_hide_a_specific_stop(stop_reason, expected_exit, expected_alert, monkeypatch, capsys):
    def pipeline(count, capacity):
        runtime_control.request_stop(stop_reason)
        return PoolResult(
            requested=count, started=1, completed=1, failed=0, timed_out=0,
            stopped=0, peak_active=capacity, elapsed_seconds=0.01,
            outcomes=(TaskOutcome(
                index=0, status="completed", value={
                    "status": "retry_pending", "error_code": "codex_review_retry_pending",
                    "write_state": "not_requested", "process_attempts": 1,
                },
            ),),
        )

    rolling = probe.run_rolling_drain
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kwargs: rolling(
        **kwargs, live_status=live_status, reconcile_status=reconciled, pipeline_runner=pipeline,
    ))
    alerts, clears = [], []
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code, **_kwargs: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    monkeypatch.setattr(probe.session_alert, "clear_active_alert", lambda **kwargs: clears.append(kwargs))
    assert probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES", "--confirm-alerts", "YES"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["completed"] == 1
    assert payload["failed"] == payload["stopped"] == 0
    assert payload["error_counts"] == {"codex_review_retry_pending": 1}
    assert payload["primary_error_code"] == expected_alert
    assert alerts == ([expected_alert] if expected_alert else [])
    assert clears == ([] if expected_alert else [{"session_only": True}])
