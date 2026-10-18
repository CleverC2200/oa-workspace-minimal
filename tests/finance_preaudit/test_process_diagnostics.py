
# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401
import io
import json
import sys
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker as worker, process_diagnostics


def _action(tmp_path: Path) -> Path:
    path = tmp_path / "action.json"
    payload = {"schema_version": 1, "kind": "preaudit_action", "action_token": "private-token", "workflow_key": "travel_expense"}
    json_artifact.write_json_atomic(path, payload)
    json_artifact.write_json_atomic(path.with_name("state.json"), payload)
    return path


def test_diagnostic_retains_only_fixed_signals(tmp_path: Path) -> None:
    path = _action(tmp_path)
    stderr = process_diagnostics.stderr_summary(io.StringIO("private-account retry connection private-secret\n"))
    stderr["last_observed_stage"] = "turn.started"
    process_diagnostics.record(path, execution=worker.CodexExecution(
        "codex_process_timeout", {"status": "unknown"}, process_state="timed_out",
        event_counts={"turn.started": 1, "private-tool": 4}, exit_code=-15,
    ), elapsed_seconds=1, stderr=stderr)
    text = next(tmp_path.glob("review-process-*.json")).read_text()
    assert "private" not in text
    d = json.loads(text)
    assert d["last_observed_stage"] == "turn.started"
    assert d["stderr_signals"] == ["retry_signal", "transport_signal"]
    assert set(d) == {"schema_version", "kind", "action_token_sha256", "last_observed_stage",
                      "event_counts", "elapsed_seconds", "process_state", "exit_code",
                      "stderr_bytes", "stderr_signals", "event_signals"}


def test_real_child_timeout_records_stage_and_reaps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    path = _action(tmp_path)
    monkeypatch.setattr(worker, "_decision_schema", lambda _, **_kwargs: {})
    monkeypatch.setattr(worker, "_codex_command", lambda *_, visual_aux=None: [sys.executable, "-c",
        'import sys,time; print(\'{"type":"turn.started"}\',flush=True); '
        'print("private retry connection",file=sys.stderr,flush=True); time.sleep(30)'])
    result = worker._execute_codex(tmp_path, path, 0.2, prompt_builder=lambda _: "synthetic")
    assert result.error_code == "codex_process_timeout"
    assert result.exit_code is not None
    diagnostic = json.loads(next(tmp_path.glob("review-process-*.json")).read_text())
    assert diagnostic["process_state"] == "timed_out"
    assert diagnostic["last_observed_stage"] == "turn.started"
    assert diagnostic["stderr_bytes"] > 0


def test_last_stage_uses_event_order() -> None:
    assert process_diagnostics.last_stage(io.StringIO(
        '{"type":"item.completed"}\n{"type":"item.started"}\n'
    )) == "item.started"


def test_stderr_signals_cross_chunk_boundary() -> None:
    result = process_diagnostics.stderr_summary(io.StringIO("x" * 4094 + "reconnect"))
    assert result["stderr_signals"] == ["retry_signal"]


@pytest.mark.parametrize("damage", ["symlink", "content"])
def test_diagnostic_conflict_is_not_overwritten(tmp_path: Path, damage: str) -> None:
    path = _action(tmp_path)
    execution = worker.CodexExecution(None, {}, process_state="exited")
    kwargs = dict(execution=execution, elapsed_seconds=1, stderr={})
    process_diagnostics.record(path, **kwargs)
    target = next(tmp_path.glob("review-process-*.json"))
    if damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "missing")
    else:
        json_artifact.write_json_atomic(target, {})
    with pytest.raises(ValueError, match="process_diagnostic_invalid"):
        process_diagnostics.record(path, **kwargs)


def test_stdout_disconnect_survives_timeout_without_raw_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    path = _action(tmp_path)
    monkeypatch.setattr(worker, "_decision_schema", lambda _, **_kwargs: {})
    event = {"type": "error", "message": "Reconnecting... 2/5 (stream disconnected before completion: websocket closed by server before response.completed) private-secret"}
    script = f"import time; print({json.dumps(event)!r}, flush=True); time.sleep(30)"
    monkeypatch.setattr(worker, "_codex_command", lambda *_, visual_aux=None: [sys.executable, "-c", script])
    result = worker._execute_codex(tmp_path, path, 0.2, prompt_builder=lambda _: "synthetic")
    assert result.error_code == "codex_process_timeout"
    text = next(tmp_path.glob("review-process-*.json")).read_text()
    assert "private-secret" not in text
    diagnostic = json.loads(text)
    assert diagnostic["event_signals"] == ["retry_signal", "transport_signal"]
    assert diagnostic["stderr_signals"] == []


def test_error_signals_ignore_evidence_and_handle_fallback_event():
    events = [
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "401 private"}},
        {"type": "item.completed", "item": {"type": "error", "message": "stream disconnected"}},
        {"type": "error", "message": 42},
        [],
    ]
    stream = io.StringIO("\n".join(json.dumps(event) for event in events))
    assert process_diagnostics.event_error_signals(stream) == ["transport_signal"]


def test_websocket_closed_signal_without_disconnect_prefix():
    event = {"type": "error", "message": "websocket closed before response.completed"}
    assert process_diagnostics.event_error_signals(io.StringIO(json.dumps(event))) == ["transport_signal"]


@pytest.mark.parametrize("event", [
    {"type": "error", "message": "unexpected status 413 Payload Too Large private-secret"},
    {"type": "turn.failed", "error": {"message": "413 Request Entity Too Large private-secret"}},
])
def test_payload_too_large_signal_is_recorded_without_raw_error(tmp_path, event):
    path = _action(tmp_path)
    signals = process_diagnostics.event_error_signals(io.StringIO(json.dumps(event)))
    assert signals == ["payload_too_large_signal"]
    process_diagnostics.record(path, execution=worker.CodexExecution(
        "codex_process_failed", {}, process_state="exited", exit_code=1,
    ), elapsed_seconds=1, stderr={"event_signals": signals})
    text = next(tmp_path.glob("review-process-*.json")).read_text()
    assert "private-secret" not in text
    assert json.loads(text)["event_signals"] == signals


@pytest.mark.parametrize("timing", [
    {"stop_requested_at": 1.0, "process_exit_observed_at": 2.0, "message": "private-secret"},
    {"stop_requested_at": "private-secret", "process_exit_observed_at": None},
    {"stop_requested_at": float("inf"), "process_exit_observed_at": None},
])
def test_notice_timing_rejects_non_allowlisted_evidence(tmp_path, timing):
    path = _action(tmp_path)
    with pytest.raises(ValueError, match="process_diagnostic_invalid"):
        process_diagnostics.record(path, execution=worker.CodexExecution(None, {}),
                                   elapsed_seconds=1, stderr={"notice_timing": timing})
    assert not list(tmp_path.glob("review-process-*.json"))
