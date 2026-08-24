from __future__ import annotations

import json
import io
from pathlib import Path
import signal
import threading
import time

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker
from apps.oa_approval import runtime_control


def _task(tmp_path: Path) -> tuple[dict, Path]:
    project_root = tmp_path / "project"
    attempt_root = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / ("a" * 64)
        / "attempt-1"
    )
    action_file = attempt_root / "action.json"
    attempt_root.mkdir(parents=True)
    json_artifact.write_json_atomic(
        action_file,
        {
            "schema_version": 1,
            "kind": "preaudit_action",
            "action": "review_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": "authorized",
            "workflow_key": "travel_expense",
            "mode": "controlled_write",
            "write_authorized": True,
            "action_token": "test-action-token",
        },
    )
    json_artifact.write_json_atomic(
        action_file.with_name("state.json"),
        json.loads(action_file.read_text(encoding="utf-8")),
    )
    return {
        "project_root": str(project_root),
        "action_file": str(action_file),
        "timeout_seconds": 900,
    }, action_file


def test_worker_returns_sanitized_success_only_after_bound_receipt(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    stopped: list[bool] = []

    def execute(
        _project_root: Path,
        current_action: Path,
        _timeout: int,
    ) -> codex_review_worker.CodexExecution:
        receipt_file = current_action.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        payload = json.loads(current_action.read_text(encoding="utf-8"))
        json_artifact.write_json_atomic(
            current_action,
            {
                **payload,
                "action": "closed",
                "execution_state": "closed",
                "business_state": "needs_human",
                "write_state": "submitted",
                "receipt_file": str(receipt_file),
            },
        )
        return codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "total_tokens": 150,
            },
        )

    result = codex_review_worker._legacy_run_with_completion(
        task,
        executor=execute,
        request_stop=lambda: stopped.append(True),
    )

    assert result == {
        "schema_version": 1,
        "kind": "preaudit_review_worker_result",
        "status": "completed",
        "execution_state": "closed",
        "business_state": "needs_human",
        "write_state": "submitted",
        "error_code": None,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "process_attempts": 1,
        "process_retries": 0,
        "token_usage": {
            "status": "recorded",
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "total_tokens": 150,
        },
    }
    assert str(action_file) not in repr(result)
    assert stopped == []


def test_worker_requests_global_stop_on_codex_process_failure(tmp_path: Path) -> None:
    task, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker._legacy_run_with_completion(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code="codex_process_failed",
            token_usage={"status": "unknown"},
        ),
        request_stop=lambda: stopped.append(True),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "codex_process_failed"
    assert str(action_file) not in repr(result)
    assert stopped == [True]


def test_worker_retries_once_only_when_first_process_left_action_untouched(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    attempts: list[int] = []
    stopped: list[bool] = []

    def execute(
        _project_root: Path,
        current_action: Path,
        _timeout: int,
    ) -> codex_review_worker.CodexExecution:
        attempts.append(1)
        if len(attempts) == 1:
            return codex_review_worker.CodexExecution(
                error_code="codex_process_failed",
                token_usage={
                    "status": "recorded",
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
            )
        receipt_file = current_action.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        payload = json.loads(current_action.read_text(encoding="utf-8"))
        json_artifact.write_json_atomic(
            current_action,
            {
                **payload,
                "action": "closed",
                "execution_state": "closed",
                "business_state": "pass",
                "write_state": "submitted",
                "receipt_file": str(receipt_file),
            },
        )
        return codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 20,
                "cached_input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 23,
            },
        )

    result = codex_review_worker._legacy_run_with_completion(
        task,
        executor=execute,
        request_stop=lambda: stopped.append(True),
    )

    assert result["status"] == "completed"
    assert result["process_attempts"] == 2
    assert result["process_retries"] == 1
    assert result["token_usage"] == {
        "status": "recorded",
        "input_tokens": 30,
        "cached_input_tokens": 5,
        "output_tokens": 5,
        "total_tokens": 35,
    }
    assert stopped == []


def test_worker_does_not_retry_timeout_or_changed_action(tmp_path: Path) -> None:
    task, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker._legacy_run_with_completion(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code="codex_process_timeout",
            token_usage={"status": "unknown"},
        ),
        request_stop=lambda: stopped.append(True),
    )

    assert result["process_attempts"] == 1
    assert result["process_retries"] == 0
    assert stopped == [True]
    assert action_file.exists()


def test_codex_command_pins_terra_high_and_json_output(tmp_path: Path) -> None:
    command = codex_review_worker._codex_command(
        tmp_path,
        tmp_path / "schema.json",
        tmp_path / "decision.json",
    )

    assert command[command.index("--model") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in command
    assert "--json" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--output-schema") + 1].endswith("schema.json")
    assert command[command.index("--output-last-message") + 1].endswith(
        "decision.json"
    )


def test_prompt_exposes_flow_engine_fallback_routing_for_legacy_packet(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _task(tmp_path)
    json_artifact.write_json_atomic(
        action_file.with_name("review-packet.json"),
        {
            "schema_version": 1,
            "kind": "preaudit_review_packet",
            "workflow_key": "travel_expense",
        },
    )
    json_artifact.write_json_atomic(
        action_file.with_name("material.json"),
        {
            "prepared": {
                "images": [
                    {"parent_resource_id": "resource-a"},
                    {"parent_resource_id": "resource-a"},
                ]
            }
        },
    )

    prompt = codex_review_worker._prompt(action_file)

    assert '"strategy":"full_page_fallback"' in prompt
    assert '"review_page_count":2' in prompt
    assert "material:prepared.images.resource-a.1" in prompt
    assert "material:prepared.images.resource-a.2" in prompt
    assert "逐项覆盖以下宿主 fallback 路由" in prompt
    assert '"strategy":"full_page_fallback"' in (
        codex_review_worker._read_only_prompt(action_file)
    )


def test_prompt_does_not_replace_explicit_or_unreadable_page_routing(
    tmp_path: Path,
) -> None:
    _task_payload, action_file = _task(tmp_path)
    packet_file = action_file.with_name("review-packet.json")
    material_file = action_file.with_name("material.json")
    json_artifact.write_json_atomic(
        material_file,
        {"prepared": {"images": [{"parent_resource_id": "resource-a"}]}},
    )
    json_artifact.write_json_atomic(
        packet_file,
        {
            "page_routing": {
                "strategy": "exact_page_duplicate_only",
                "review_page_refs": ["packet-owned-ref"],
            }
        },
    )

    assert "full_page_fallback" not in codex_review_worker._prompt(action_file)

    packet_file.write_text("not-json", encoding="utf-8")
    assert "full_page_fallback" not in codex_review_worker._prompt(action_file)


def test_codex_start_blocks_sigterm_until_process_group_is_registered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task, action_file = _task(tmp_path)
    calls: list[tuple[str, object]] = []

    def mask(how: object, values: object) -> set[signal.Signals]:
        calls.append(("mask", how))
        return set()

    def install(signum: int, handler: object) -> object:
        calls.append(("signal", signum))
        return signal.SIG_DFL

    class Process:
        pid = 12345
        returncode = 1

        def communicate(self, *, input: str, timeout: int) -> None:
            del input, timeout
            calls.append(("communicate", self.pid))

        def poll(self) -> int:
            return self.returncode

    def popen(*_args, **_kwargs) -> Process:
        calls.append(("popen", "registered"))
        return Process()

    monkeypatch.setattr(codex_review_worker.signal, "pthread_sigmask", mask)
    monkeypatch.setattr(codex_review_worker.signal, "signal", install)
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", popen)

    result = codex_review_worker._execute_codex(
        Path(task["project_root"]),
        action_file,
        60,
    )

    assert result.error_code == "codex_process_failed"
    assert [name for name, _value in calls[:5]] == [
        "mask",
        "signal",
        "popen",
        "mask",
        "communicate",
    ]
    assert calls[0][1] == signal.SIG_BLOCK
    assert calls[3][1] == signal.SIG_SETMASK


def test_worker_submits_structured_agent_decision_through_host(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    decision = {"kind": "preaudit_review_decision", "items": []}
    completed: list[dict] = []

    def complete(current_action: Path, current_decision: dict) -> str | None:
        assert current_decision == decision
        completed.append(current_decision)
        receipt_file = current_action.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        payload = json.loads(current_action.read_text(encoding="utf-8"))
        closed = {
            **payload,
            "action": "closed",
            "execution_state": "closed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "receipt_file": str(receipt_file),
        }
        json_artifact.write_json_atomic(current_action, closed)
        return None

    result = codex_review_worker._legacy_run_with_completion(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={"status": "unknown"},
            decision=decision,
        ),
        completer=complete,
        request_stop=lambda: None,
    )

    assert result["status"] == "completed"
    assert result["business_state"] == "needs_human"
    assert completed == [decision]
    timing = json.loads(
        action_file.with_name("agent-timing.json").read_text(encoding="utf-8")
    )
    assert timing["action_token"] == "test-action-token"
    assert timing["model"] == "gpt-5.6-terra"
    assert timing["process_attempts"] == 1
    assert "request_id" not in timing


def test_production_review_phase_returns_decision_without_completing_action(
    tmp_path: Path,
) -> None:
    task, action_file = _task(tmp_path)
    decision = {"kind": "preaudit_review_decision", "items": []}

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
                "total_tokens": 12,
            },
            decision=decision,
        ),
    )

    assert result["status"] == "reviewed"
    assert result["write_state"] == "not_requested"
    assert result["decision"] == decision
    assert result["codex_events"] == {
        "status": "unknown",
        "attempts": 1,
        "recorded_attempts": 0,
        "unknown_attempts": 1,
        "counts": {},
    }
    assert json.loads(action_file.read_text(encoding="utf-8"))["action"] == (
        "review_required"
    )
    assert not action_file.with_name("agent-timing.json").exists()
    assert not action_file.with_name("execution-receipt.json").exists()


def test_production_review_accumulates_retry_events_and_tokens(
    tmp_path: Path,
) -> None:
    task, _action_file = _task(tmp_path)
    executions = iter(
        [
            codex_review_worker.CodexExecution(
                error_code="codex_process_failed",
                token_usage={
                    "status": "recorded",
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 11,
                },
                event_counts={"error": 1, "turn.completed": 1},
                event_status="recorded",
            ),
            codex_review_worker.CodexExecution(
                error_code=None,
                token_usage={
                    "status": "recorded",
                    "input_tokens": 20,
                    "cached_input_tokens": 5,
                    "output_tokens": 3,
                    "total_tokens": 23,
                },
                decision={"overall": "pass"},
                event_counts={"turn.completed": 1},
                event_status="recorded",
            ),
        ]
    )

    result = codex_review_worker.review(
        task,
        executor=lambda *_args: next(executions),
    )

    assert result["status"] == "reviewed"
    assert result["process_attempts"] == 2
    assert result["token_usage"] == {
        "status": "recorded",
        "input_tokens": 30,
        "cached_input_tokens": 7,
        "output_tokens": 4,
        "total_tokens": 34,
    }
    assert result["codex_events"] == {
        "status": "recorded",
        "attempts": 2,
        "recorded_attempts": 2,
        "unknown_attempts": 0,
        "counts": {"error": 1, "turn.completed": 2},
    }


def test_parent_completion_persists_timing_and_bound_receipt(
    tmp_path: Path,
) -> None:
    _task_value, action_file = _task(tmp_path)
    decision = {"kind": "preaudit_review_decision", "items": []}

    def complete(current_action: Path, current_decision: dict) -> str | None:
        assert current_decision == decision
        receipt_file = current_action.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        action = json.loads(current_action.read_text(encoding="utf-8"))
        json_artifact.write_json_atomic(
            current_action,
            {
                **action,
                "action": "closed",
                "execution_state": "closed",
                "business_state": "pass",
                "write_state": "submitted",
                "receipt_file": str(receipt_file),
            },
        )
        return None

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "process_attempts": 1,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {
                "status": "recorded",
                "attempts": 1,
                "recorded_attempts": 1,
                "unknown_attempts": 0,
                "counts": {"turn.completed": 1},
            },
            "decision": decision,
        },
        completer=complete,
        request_stop=lambda: None,
    )

    assert result["status"] == "completed"
    assert result["write_state"] == "submitted"
    assert result["codex_events"]["counts"] == {"turn.completed": 1}
    timing = json.loads(
        action_file.with_name("agent-timing.json").read_text(encoding="utf-8")
    )
    assert timing["wall_seconds"] == 12


def test_usage_parser_reads_only_turn_completed_usage() -> None:
    stream = io.StringIO(
        '\n'.join(
            [
                '{"type":"item.completed","usage":{"input_tokens":999}}',
                '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":20,"output_tokens":30}}',
                "not-json",
            ]
        )
    )

    assert codex_review_worker._parse_token_usage(stream) == {
        "status": "recorded",
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "total_tokens": 150,
    }


def test_event_status_rejects_missing_or_malformed_jsonl() -> None:
    complete = io.StringIO('{"type":"turn.completed"}\n')
    missing = io.StringIO('{"type":"error"}\n')
    malformed = io.StringIO('{"type":"turn.completed"}\nnot-json\n')

    assert codex_review_worker._event_status(
        complete,
        {"turn.completed": 1},
    ) == "recorded"
    assert codex_review_worker._event_status(missing, {"error": 1}) == "unknown"
    assert codex_review_worker._event_status(
        malformed,
        {"turn.completed": 1},
    ) == "unknown"


def test_event_parser_sanitizes_unknown_event_types() -> None:
    sensitive_type = "request-123-/internal/path"
    stream = io.StringIO(
        f'{{"type":"{sensitive_type}"}}\n{{"type":"turn.completed"}}\n'
    )

    counts = codex_review_worker._parse_event_counts(stream)

    assert counts == {"other": 1, "turn.completed": 1}
    assert sensitive_type not in repr(counts)
    assert codex_review_worker._event_status(stream, counts) == "unknown"


def test_parent_invalid_review_preserves_known_attempts_as_unknown_events(
    tmp_path: Path,
) -> None:
    _task_value, action_file = _task(tmp_path)
    stopped: list[bool] = []

    result = codex_review_worker.complete_review_result(
        action_file,
        {
            "status": "reviewed",
            "process_attempts": 2,
            "agent_wall_seconds": 12,
            "token_usage": {"status": "unknown"},
            "codex_events": {"status": "unknown"},
            "decision": {"overall": "pass"},
        },
        request_stop=lambda: stopped.append(True),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "codex_review_result_invalid"
    assert result["process_attempts"] == 2
    assert result["process_retries"] == 1
    assert result["codex_events"] == {
        "status": "unknown",
        "attempts": 2,
        "recorded_attempts": 0,
        "unknown_attempts": 2,
        "counts": {},
    }
    assert stopped == [True]


def test_event_contract_rejects_recorded_attempt_without_completed_event() -> None:
    assert not codex_review_worker._valid_events(
        {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": {},
        },
        attempts=1,
    )


def test_host_completion_does_not_add_a_second_writer_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FROZEN_WRITER_LOCK_FILE",
        str(tmp_path / "control" / "writer.lock"),
    )
    active = 0
    peak = 0
    guard = threading.Lock()

    class Engine:
        def complete_review(self, _action_file: Path, _decision_file: Path) -> dict:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return {
                "action": "closed",
                "execution_state": "closed",
                "write_state": "submitted",
            }

    monkeypatch.setattr(
        "apps.oa_finance_preaudit.cli._live_flow_engine",
        lambda *, controlled: Engine(),
    )
    results: list[str | None] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                codex_review_worker._complete_decision(
                    tmp_path / "action.json",
                    {"kind": "preaudit_review_decision"},
                )
            )
        )
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [None, None]
    assert peak == 2


def test_global_stop_request_is_visible_to_all_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_file = tmp_path / "control" / "stop.flag"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))

    runtime_control.request_stop()

    assert runtime_control.stop_requested() is True
    assert stop_file.read_text(encoding="utf-8") == "stop_requested\n"
    assert stop_file.stat().st_mode & 0o777 == 0o600


def test_read_only_worker_returns_structured_decision_without_completion(
    tmp_path: Path,
) -> None:
    task, _action_file = _task(tmp_path)
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "overall": "needs_human",
    }

    result = codex_review_worker.run_read_only(
        task,
        executor=lambda *_args: codex_review_worker.CodexExecution(
            error_code=None,
            token_usage={
                "status": "recorded",
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "total_tokens": 110,
            },
            decision=decision,
            process_state="exited",
            decision_state="valid_object",
            event_counts={"turn.completed": 1},
            event_status="recorded",
            exit_code=0,
        ),
    )

    assert result == {
        "schema_version": 1,
        "kind": "preaudit_read_only_review_result",
        "status": "completed",
        "business_state": "needs_human",
        "write_state": "not_requested",
        "error_code": None,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "process_attempts": 1,
        "process_retries": 0,
        "process_state": "exited",
        "decision_state": "valid_object",
        "codex_events": {
            "status": "recorded",
            "attempts": 1,
            "recorded_attempts": 1,
            "unknown_attempts": 0,
            "counts": {"turn.completed": 1},
        },
        "exit_code": 0,
        "token_usage": {
            "status": "recorded",
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 10,
            "total_tokens": 110,
        },
    }


def test_read_only_worker_classifies_zero_exit_without_decision() -> None:
    result = codex_review_worker.run_read_only(
        {
            "project_root": "/missing",
            "action_file": "/missing/action.json",
            "timeout_seconds": 900,
        },
    )

    assert result["status"] == "failed"
    assert result["write_state"] == "not_requested"
    assert result["error_code"] == "codex_review_task_invalid"
