from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import concurrency_control, process_pool
from scripts import finance_preaudit_concurrency_probe


def _open_action(root: Path, *, case_key: str, created_at: str) -> Path:
    request_id = f"request-{case_key}"
    received_at = "2026-08-23T08:00:00+08:00"
    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    attempt_root = (
        root
        / "travel_expense"
        / "cases"
        / case_key
        / "attempt-1"
    )
    action_file = attempt_root / "action.json"
    state_file = attempt_root / "state.json"
    payload = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "action": "review_required",
        "execution_state": "review_required",
        "business_state": "not_available",
        "write_state": "authorized",
        "workflow_key": "travel_expense",
        "mode": "controlled_write",
        "write_authorized": True,
        "created_at": created_at,
        "case_key": case_key,
        "attempt": 1,
        "request_id": request_id,
        "received_at": received_at,
        "action_file": str(action_file),
        "state_file": str(state_file),
    }
    attempt_root.mkdir(parents=True)
    json_artifact.write_json_atomic(action_file, payload)
    json_artifact.write_json_atomic(state_file, payload)
    return action_file


def _reconciliation_snapshot(pending: int, quarantined: int = 0) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_reconciliation",
        "read_only": True,
        "quarantine": {
            "local_quarantined": quarantined,
            "pending_quarantined": quarantined,
            "actionable_pending": pending - quarantined,
        },
    }


def _reviewable_action(root: Path, *, case_key: str) -> Path:
    action_file = _open_action(
        root,
        case_key=case_key,
        created_at="2026-08-24T00:00:00+00:00",
    )
    review_packet = action_file.with_name("review-packet.json")
    material_file = action_file.with_name("material.json")
    json_artifact.write_json_atomic(
        review_packet,
        {
            "workflow_key": "travel_expense",
            "material_file": str(material_file),
        },
    )
    json_artifact.write_json_atomic(material_file, {"workflow_key": "travel_expense"})
    action = json.loads(action_file.read_text(encoding="utf-8"))
    receipt_file = action_file.with_name("execution-receipt.json")
    closed = {
        **action,
        "action": "closed",
        "execution_state": "closed",
        "business_state": "needs_human",
        "write_state": "submitted",
        "receipt_file": str(receipt_file),
    }
    json_artifact.write_json_atomic(
        action_file,
        {
            **closed,
            "review_packet_file": str(review_packet),
        },
    )
    json_artifact.write_json_atomic(action_file.with_name("state.json"), closed)
    json_artifact.write_json_atomic(
        receipt_file,
        {
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        },
    )
    return action_file


def test_legacy_module_production_entries_are_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="^production_legacy_scheduler_removed$",
    ):
        finance_preaudit_concurrency_probe.run_existing_batch(capacity=1)
    with pytest.raises(
        ValueError,
        match="^production_legacy_scheduler_removed$",
    ):
        finance_preaudit_concurrency_probe.run_drain(max_capacity=1)


def test_read_only_probe_excludes_older_closed_attempt_when_latest_is_active(
    tmp_path: Path,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    older = _reviewable_action(flow_root, case_key="older")
    latest_root = older.parent.parent / "attempt-2"
    latest_root.mkdir()
    latest = json.loads(older.read_text(encoding="utf-8"))
    latest.update(
        {
            "attempt": 2,
            "action": "review_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": "authorized",
            "action_file": str(latest_root / "action.json"),
            "state_file": str(latest_root / "state.json"),
            "receipt_file": None,
        }
    )
    json_artifact.write_json_atomic(latest_root / "action.json", latest)
    json_artifact.write_json_atomic(latest_root / "state.json", latest)

    assert finance_preaudit_concurrency_probe._reviewable_action_files(tmp_path) == []


def test_probe_renews_oldest_open_actions_and_returns_only_sanitized_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    newer = _open_action(
        flow_root,
        case_key="b" * 64,
        created_at="2026-08-23T12:00:00+00:00",
    )
    older = _open_action(
        flow_root,
        case_key="a" * 64,
        created_at="2026-08-23T11:00:00+00:00",
    )
    renewed: list[Path] = []

    def renew(paths: list[Path]) -> list[Path]:
        renewed.extend(paths)
        return paths

    def review(tasks: list[dict], capacity: int) -> process_pool.PoolResult[dict]:
        assert capacity == 1
        assert [Path(task["action_file"]) for task in tasks] == [older]
        outcome = process_pool.TaskOutcome(
            index=0,
            status="completed",
            value={
                "status": "completed",
                "business_state": "needs_human",
                "process_attempts": 1,
                "process_retries": 0,
                "token_usage": {
                    "status": "recorded",
                    "input_tokens": 100,
                    "cached_input_tokens": 10,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )
        return process_pool.PoolResult(
            requested=1,
            started=1,
            completed=1,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=1,
            elapsed_seconds=1.25,
            outcomes=(outcome,),
        )

    result = finance_preaudit_concurrency_probe._legacy_run_existing_batch(
        capacity=1,
        project_root=tmp_path,
        renew_actions=renew,
        run_reviews=review,
    )

    assert renewed == [older]
    assert result == {
        "schema_version": 1,
        "kind": "preaudit_production_concurrency_probe",
        "capacity": 1,
        "selected": 1,
        "prepared_new": 0,
        "prepare_peak_active": 0,
        "prepare_elapsed_seconds": 0.0,
        "completed": 1,
        "failed": 0,
        "timed_out": 0,
        "stopped": 0,
        "peak_active": 1,
        "elapsed_seconds": 1.25,
        "business_results": {"needs_human": 1},
        "error_counts": {},
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "process_attempts": 1,
        "process_retries": 0,
        "runtime_health": {
            "status": "unknown",
                "sample_count": 0,
            "minimum_memory_free_percent": None,
                "maximum_load_per_cpu": None,
                "maximum_active_threads": None,
                "maximum_active_workers": 0,
                "maximum_oldest_task_seconds": 0.0,
            },
        "watchdog": {
            "runtime_samples": 0,
            "maximum_task_seconds": 0.0,
            "task_timeout_seconds": 960,
        },
        "token_usage": {
            "status": "recorded",
            "recorded_workers": 1,
            "partial_workers": 0,
            "unknown_workers": 0,
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    }
    encoded = json.dumps(result)
    assert str(older) not in encoded
    assert str(newer) not in encoded


def test_probe_prepares_missing_actions_before_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    prepared_file = flow_root / "travel_expense" / "pending-action.json"

    def prepare(count: int) -> process_pool.PoolResult[dict]:
        assert count == 1
        action_file = _open_action(
            flow_root,
            case_key="c" * 64,
            created_at="2026-08-23T13:00:00+00:00",
        )
        outcome = process_pool.TaskOutcome(
            index=0,
            status="completed",
            value={"action": "review_required", "action_file": str(action_file)},
        )
        return process_pool.PoolResult(
            requested=1,
            started=1,
            completed=1,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=1,
            elapsed_seconds=2.5,
            outcomes=(outcome,),
        )

    def review(tasks: list[dict], capacity: int) -> process_pool.PoolResult[dict]:
        del tasks, capacity
        outcome = process_pool.TaskOutcome(
            index=0,
            status="completed",
            value={"status": "completed", "business_state": "pass"},
        )
        return process_pool.PoolResult(
            requested=1,
            started=1,
            completed=1,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=1,
            elapsed_seconds=3.0,
            outcomes=(outcome,),
        )

    result = finance_preaudit_concurrency_probe._legacy_run_existing_batch(
        capacity=1,
        project_root=tmp_path,
        prepare_actions=prepare,
        renew_actions=lambda paths: paths,
        run_reviews=review,
    )

    assert result["prepared_new"] == 1
    assert result["prepare_peak_active"] == 1
    assert result["prepare_elapsed_seconds"] == 2.5
    assert result["completed"] == 1
    assert not prepared_file.exists()


def test_prepare_skips_quarantined_item_without_global_stop(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "needs_admin",
                "reason": "writer_result_unknown",
                "write_state": "unknown",
            },
            {
                "action": "review_required",
                "action_file": "/safe/action.json",
            },
        ]
    )
    stop_requests: list[bool] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            return next(actions)

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )

    assert finance_preaudit_concurrency_probe._prepare_one("travel_expense") == {
        "action": "review_required",
        "action_file": "/safe/action.json",
    }
    assert stop_requests == []


def test_prepare_stops_before_collecting_another_item_after_stop(monkeypatch) -> None:
    stopped = False
    calls = 0

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            nonlocal stopped, calls
            calls += 1
            stopped = True
            return {
                "action": "needs_admin",
                "reason": "writer_result_unknown",
                "write_state": "unknown",
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: stopped,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 5,
    )

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")

    assert result == {
        "action": "failed",
        "error_code": "production_probe_stopped",
    }
    assert calls == 1


def test_probe_accepts_partial_final_capacity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(3):
        _open_action(
            flow_root,
            case_key=str(index) * 64,
            created_at=f"2026-08-23T15:00:0{index}+00:00",
        )

    result = finance_preaudit_concurrency_probe._legacy_run_existing_batch(
        capacity=3,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        run_reviews=lambda tasks, capacity: process_pool.PoolResult(
            requested=3,
            started=3,
            completed=3,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=4.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={"status": "completed", "business_state": "pass"},
                )
                for index, _task in enumerate(tasks)
            ),
        ),
    )

    assert result["capacity"] == 3
    assert result["completed"] == 3


def test_drain_uses_six_then_partial_capacity_and_aggregates() -> None:
    pending = iter([8, 2, 0])
    capacities: list[int] = []

    def live_status() -> dict:
        count = next(pending)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": count},
            },
        }

    def batch_runner(*, capacity: int) -> dict:
        capacities.append(capacity)
        return {
            "capacity": capacity,
            "selected": capacity,
            "prepared_new": capacity,
            "completed": capacity,
            "failed": 0,
            "timed_out": 0,
            "stopped": 0,
            "elapsed_seconds": 10.0,
            "business_results": {"needs_human": capacity},
            "error_counts": {},
            "process_attempts": capacity,
            "process_retries": 0,
            "token_usage": {
                "status": "recorded",
                "recorded_workers": capacity,
                "partial_workers": 0,
                "unknown_workers": 0,
                "input_tokens": capacity * 100,
                "cached_input_tokens": 0,
                "output_tokens": capacity * 10,
                "total_tokens": capacity * 110,
            },
        }

    result = finance_preaudit_concurrency_probe._legacy_run_drain(
        max_capacity=6,
        live_status=live_status,
        batch_runner=batch_runner,
    )

    assert capacities == [6, 2]
    assert result["status"] == "completed"
    assert result["initial_pending"] == 8
    assert result["final_pending"] == 0
    assert result["batches"] == 2
    assert result["completed"] == 8
    assert result["token_usage"]["total_tokens"] == 880


def test_rolling_drain_runs_full_pipeline_without_batch_barriers() -> None:
    pending = iter([8, 0])
    current_pending = [0]
    calls: list[tuple[int, int]] = []

    def live_status() -> dict:
        count = next(pending)
        current_pending[0] = count
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": count},
            },
        }

    def pipeline_runner(count: int, capacity: int) -> process_pool.PoolResult[dict]:
        calls.append((count, capacity))
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=20.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "needs_human",
                        "process_attempts": 1,
                        "process_retries": 0,
                        "prepare_elapsed_seconds": 2.0,
                        "token_usage": {
                            "status": "recorded",
                            "input_tokens": 100,
                            "cached_input_tokens": 50,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    },
                )
                for index in range(count)
            ),
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live_status,
        pipeline_runner=pipeline_runner,
        reconcile_status=lambda: _reconciliation_snapshot(current_pending[0]),
    )

    assert calls == [(8, 6)]
    assert result["scheduler"] == "rolling"
    assert result["elastic"] is False
    assert result["start_capacity"] == 6
    assert result["recommended_capacity"] == 6
    assert result["completed"] == 8
    assert result["final_pending"] == 0
    assert result["elapsed_seconds"] >= 20.0
    assert result["token_usage"]["total_tokens"] == 880


def test_elastic_rolling_requires_two_healthy_waves_before_upgrade() -> None:
    pending = iter([36, 24, 12, 0])
    current_pending = [0]
    calls: list[tuple[int, int]] = []

    def live_status() -> dict:
        current_pending[0] = next(pending)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": current_pending[0]},
            },
        }

    def pipeline(count: int, capacity: int) -> process_pool.PoolResult[dict]:
        calls.append((count, capacity))
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=10.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "needs_human",
                        "process_attempts": 1,
                        "process_retries": 0,
                        "token_usage": {"status": "unknown"},
                    },
                )
                for index in range(count)
            ),
            runtime_samples=(
                {
                    "active_workers": capacity,
                    "oldest_task_seconds": 9.0,
                    "host": {
                        "status": "healthy",
                        "memory_free_percent": 40,
                        "load_per_cpu": 0.5,
                        "active_threads": 3,
                        "cpu_count": 10,
                    },
                },
            ),
            max_task_elapsed_seconds=9.0,
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        adaptive=True,
        live_status=live_status,
        pipeline_runner=pipeline,
        reconcile_status=lambda: _reconciliation_snapshot(current_pending[0]),
    )

    assert calls == [(12, 6), (12, 6), (12, 8)]
    assert result["elastic"] is True
    assert result["recommended_capacity"] == 8
    assert result["status"] == "completed"


def test_rolling_drain_excludes_quarantined_unknown_writes() -> None:
    calls: list[tuple[int, int]] = []

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": 3},
            },
        },
        pipeline_runner=lambda count, capacity: calls.append((count, capacity)),
        reconcile_status=lambda: _reconciliation_snapshot(3, 3),
    )

    assert calls == []
    assert result["status"] == "completed_with_quarantine"
    assert result["initial_pending"] == 3
    assert result["initial_actionable"] == 0
    assert result["quarantined_pending"] == 3
    assert result["final_pending"] == 3


def test_rolling_controller_reconciles_before_any_pipeline_work() -> None:
    calls: list[tuple[int, int]] = []

    def fail_reconciliation() -> dict:
        raise ValueError("runtime_reconciliation_invalid")

    try:
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=lambda: {
                "credentials": {"status": "ok"},
                "oa": {
                    "execution_identity": "verified",
                    "by_workflow": {"travel_expense": 2},
                },
            },
            reconcile_status=fail_reconciliation,
            pipeline_runner=lambda count, capacity: calls.append((count, capacity)),
        )
    except ValueError as error:
        assert str(error) == "production_rolling_reconciliation_invalid"
    else:  # pragma: no cover - failure assertion
        raise AssertionError("invalid reconciliation unexpectedly accepted")

    assert calls == []


def test_rolling_controller_rejects_untrusted_live_state_before_reconciliation(
    monkeypatch,
) -> None:
    reconciliations: list[bool] = []
    pipelines: list[tuple[int, int]] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    for live in (
        {
            "credentials": {"status": "expired"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": 2},
            },
        },
        {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "unknown",
                "by_workflow": {"travel_expense": 2},
            },
        },
    ):
        try:
            finance_preaudit_concurrency_probe.run_rolling_drain(
                capacity=6,
                live_status=lambda live=live: live,
                reconcile_status=lambda: reconciliations.append(True)
                or _reconciliation_snapshot(2),
                pipeline_runner=lambda count, capacity: pipelines.append(
                    (count, capacity)
                ),
            )
        except ValueError as error:
            assert str(error) == "production_rolling_reconciliation_invalid"
        else:  # pragma: no cover - failure assertion
            raise AssertionError("untrusted live state unexpectedly accepted")

    assert reconciliations == []
    assert pipelines == []


def test_production_cli_defaults_to_rolling_and_rejects_legacy_drain(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda *, capacity, adaptive: calls.append((capacity, adaptive))
        or {"status": "completed"},
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "6", "--confirm-write", "YES"]
    ) == 0
    assert calls == [(6, False)]
    capsys.readouterr()

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "6", "--confirm-write", "YES", "--drain"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "production_legacy_scheduler_removed"
    assert calls == [(6, False)]


def test_dispatcher_assigns_each_open_action_once_with_rolling_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(3):
        _open_action(
            flow_root,
            case_key=str(index) * 64,
            created_at=f"2026-08-23T16:00:0{index}+00:00",
        )
    assigned: list[str] = []
    guard = threading.Lock()

    def review(task: dict) -> dict:
        with guard:
            assigned.append(task["action_file"])
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        }

    def complete(_action_file: Path, _reviewed: dict) -> dict:
        return {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        3,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
    )

    assert result.completed == 3
    assert result.failed == 0
    assert result.peak_active == 2
    assert len(assigned) == 3
    assert len(set(assigned)) == 3


def test_dispatcher_times_out_stuck_task_and_stops_new_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda: stop_requests.append(True),
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="e" * 64,
        created_at="2026-08-23T16:00:00+00:00",
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=lambda _task: time.sleep(0.05) or {"status": "completed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        task_timeout_sec=0.01,
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.timed_out == 1
    assert result.outcomes[0].error_code == "worker_task_timeout"
    assert result.runtime_samples
    assert stop_requests == [True]


def test_dispatcher_requests_stop_before_failed_sibling_can_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop = threading.Event()
    sibling_started = threading.Event()
    writes: list[str] = []
    worker_number = 0
    guard = threading.Lock()
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        stop.is_set,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        stop.set,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(2):
        _open_action(
            flow_root,
            case_key=str(index),
            created_at=f"2026-08-23T17:00:0{index}+00:00",
        )

    def review(_task: dict) -> dict:
        nonlocal worker_number
        with guard:
            worker_number += 1
            current = worker_number
        if current == 1:
            assert sibling_started.wait(timeout=1)
            return {
                "status": "failed",
                "error_code": "codex_process_incomplete",
            }
        sibling_started.set()
        assert stop.wait(timeout=1)
        if not stop.is_set():
            writes.append("unexpected")
        return {"status": "failed", "error_code": "stop_requested"}

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
    )

    assert stop.is_set()
    assert writes == []
    assert result.outcomes[0].value["error_code"] == "codex_process_incomplete"
    assert result.outcomes[1].error_code == "stop_requested"


def test_dispatcher_lets_parent_writer_finish_before_stopping_failed_sibling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop = threading.Event()
    completion_started = threading.Event()
    sibling_failed = threading.Event()
    completion_finished = threading.Event()
    worker_number = 0
    guard = threading.Lock()
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        stop.is_set,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        stop.set,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(2):
        _open_action(
            flow_root,
            case_key=f"writer-{index}",
            created_at=f"2026-08-23T18:00:0{index}+00:00",
        )

    def review(_task: dict) -> dict:
        nonlocal worker_number
        with guard:
            worker_number += 1
            current = worker_number
        if current == 1:
            return {
                "status": "reviewed",
                "business_state": "not_available",
                "write_state": "not_requested",
                "process_attempts": 1,
                "process_retries": 0,
                "token_usage": {"status": "unknown"},
                "agent_wall_seconds": 1,
                "decision": {"overall": "pass"},
            }
        assert completion_started.wait(timeout=1)
        sibling_failed.set()
        return {
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_process_incomplete",
        }

    def complete(_action_file: Path, _reviewed: dict) -> dict:
        completion_started.set()
        assert sibling_failed.wait(timeout=1)
        completion_finished.set()
        return {
            "status": "completed",
            "business_state": "pass",
            "write_state": "submitted",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
    )

    assert completion_finished.is_set()
    assert stop.is_set()
    assert result.outcomes[0].value["write_state"] == "submitted"
    assert result.outcomes[1].value["error_code"] == "codex_process_incomplete"


def test_probe_counts_returned_worker_failure_as_batch_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="d" * 64,
        created_at="2026-08-23T14:00:00+00:00",
    )
    stop_requests: list[bool] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    def review(tasks: list[dict], capacity: int) -> process_pool.PoolResult[dict]:
        del tasks, capacity
        outcome = process_pool.TaskOutcome(
            index=0,
            status="completed",
            value={
                "status": "failed",
                "business_state": "not_available",
                "error_code": "codex_process_incomplete",
            },
        )
        return process_pool.PoolResult(
            requested=1,
            started=1,
            completed=1,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=1,
            elapsed_seconds=2.0,
            outcomes=(outcome,),
        )

    result = finance_preaudit_concurrency_probe._legacy_run_existing_batch(
        capacity=1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        run_reviews=review,
    )

    assert result["completed"] == 0
    assert result["failed"] == 1
    assert result["business_results"] == {}
    assert result["error_counts"] == {"codex_process_incomplete": 1}
    assert stop_requests == [True]


def test_read_only_probe_allows_twelve_slots_and_returns_sanitized_summary(
    tmp_path: Path,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(12):
        _reviewable_action(flow_root, case_key=f"{index:064x}")

    def review(tasks: list[dict], capacity: int) -> process_pool.PoolResult[dict]:
        assert len(tasks) == 12
        assert capacity == 12
        return process_pool.PoolResult(
            requested=12,
            started=12,
            completed=12,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=12,
            elapsed_seconds=5.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "needs_human",
                        "error_code": None,
                        "process_state": "exited",
                        "decision_state": "valid_object",
                        "event_counts": {"turn.completed": 1},
                        "exit_code": 0,
                        "token_usage": {
                            "status": "recorded",
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    },
                )
                for index in range(12)
            ),
            runtime_samples=(
                {
                    "active_workers": 12,
                    "oldest_task_seconds": 4.0,
                    "host": {
                        "status": "healthy",
                        "memory_free_percent": 40,
                        "load_per_cpu": 0.5,
                        "active_threads": 2,
                        "cpu_count": 10,
                    },
                },
            ),
            max_task_elapsed_seconds=4.0,
        )

    result = finance_preaudit_concurrency_probe.run_read_only_probe(
        capacity=12,
        sample_size=12,
        project_root=tmp_path,
        run_reviews=review,
    )

    assert result["kind"] == "preaudit_read_only_concurrency_probe"
    assert result["capacity"] == 12
    assert result["sample_size"] == 12
    assert result["completed"] == 12
    assert result["failed"] == 0
    assert result["peak_active"] == 12
    assert result["process_states"] == {"exited": 12}
    assert result["decision_states"] == {"valid_object": 12}
    assert result["event_counts"] == {"turn.completed": 12}
    assert result["token_usage"]["total_tokens"] == 1320
    assert result["runtime_health"]["status"] == "healthy"
    assert result["watchdog"] == {
        "runtime_samples": 1,
        "maximum_task_seconds": 4.0,
        "task_timeout_seconds": 960,
    }
    encoded = json.dumps(result)
    assert str(flow_root) not in encoded


def test_adaptive_read_only_probe_requires_two_healthy_waves_per_upgrade() -> None:
    capacities: list[int] = []

    def probe(*, capacity: int, sample_size: int) -> dict:
        capacities.append(capacity)
        return {
            "capacity": capacity,
            "sample_size": sample_size,
            "completed": sample_size,
            "failed": 0,
            "timed_out": 0,
            "stopped": 0,
            "transient_error_events": 0,
            "elapsed_seconds": 10.0,
            "runtime_health": {
                "status": "healthy",
                "minimum_memory_free_percent": 40,
            },
            "token_usage": {"total_tokens": 100},
        }

    result = finance_preaudit_concurrency_probe.run_adaptive_read_only_probe(
        start_capacity=6,
        sample_size=20,
        max_waves=6,
        probe_runner=probe,
    )

    assert capacities == [6, 6, 8, 8, 10, 10]
    assert result["status"] == "completed"
    assert result["recommended_capacity"] == 12
    assert result["waves"] == 6
    assert result["token_usage"]["total_tokens"] == 600
    assert result["last_decision"] == {
        "action": "increase",
        "next_capacity": 12,
        "healthy_windows": 0,
        "reason": "healthy_window_threshold",
    }
