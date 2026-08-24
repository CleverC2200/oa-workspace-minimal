from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import concurrency_control, process_pool
from scripts import finance_preaudit_concurrency_probe


def _slow_failed_prepare(_workflow: str) -> dict:
    time.sleep(0.05)
    return {"action": "failed", "error_code": "late_prepare_result"}


def _successful_prepare(_workflow: str) -> dict:
    return {
        "action": "review_required",
        "action_file": os.environ["OA_TEST_PREPARED_ACTION_FILE"],
    }


def _spawn_ignoring_term_child() -> subprocess.Popen:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(os.environ['OA_TEST_PREPARE_CHILD_PID_FILE'])"
                ".write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            ),
        ],
    )
    marker = Path(os.environ["OA_TEST_PREPARE_CHILD_PID_FILE"])
    deadline = time.monotonic() + 2
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    return child


def _prepare_with_owned_child(_workflow: str) -> dict:
    _spawn_ignoring_term_child()
    time.sleep(30)
    return {"action": "failed", "error_code": "late_prepare_result"}


def _failed_prepare_with_owned_child(_workflow: str) -> dict:
    _spawn_ignoring_term_child()
    return {"action": "failed", "error_code": "material_collection_failed"}


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


def test_prepare_skips_quarantined_item_without_global_stop(
    monkeypatch,
) -> None:
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
        lambda _reason=None: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one("travel_expense") == {
        "action": "review_required",
        "action_file": "/safe/action.json",
    }
    assert stop_requests == []


def test_prepare_stops_on_new_write_authorization_failure(monkeypatch) -> None:
    stop_requests: list[bool] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            return {
                "action": "needs_admin",
                "reason": "write_authorization_invalid",
                "write_state": "not_requested",
                "action_file": "/safe/new-action.json",
            }

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
        lambda _reason=None: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        lambda: frozenset({Path("/safe/old-action.json")}),
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "needs_admin",
        "error_code": "write_authorization_invalid",
    }
    assert stop_requests == [True]


def test_prepare_skips_exact_historical_write_authorization_failure(
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    calls: list[frozenset[Path]] = []

    class Engine:
        def next_action(
            self,
            *_args,
            skip_write_authorization_failures: frozenset[Path],
            **_kwargs,
        ) -> dict:
            calls.append(skip_write_authorization_failures)
            return {
                "action": "review_required",
                "action_file": "/safe/new-action.json",
            }

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
        lambda _reason=None: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        lambda: frozenset({Path("/safe/old-action.json")}),
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "review_required",
        "action_file": "/safe/new-action.json",
    }
    assert stop_requests == []
    assert calls == [frozenset({Path("/safe/old-action.json")})]


def test_prepare_requests_stop_when_next_action_raises(monkeypatch) -> None:
    stop_requests: list[bool] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            raise RuntimeError("boom")

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
        lambda _reason=None: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "failed",
        "error_code": "production_probe_prepare_failed",
    }
    assert stop_requests == [True]


def test_prepare_waits_for_inflight_completion_without_requesting_stop(
    monkeypatch,
) -> None:
    calls = 0
    stop_requests: list[bool] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("completion_in_progress")
            return {
                "action": "review_required",
                "action_file": "/safe/action.json",
            }

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
        lambda _reason=None: stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.time,
        "sleep",
        lambda _seconds: None,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "review_required",
        "action_file": "/safe/action.json",
    }
    assert calls == 2
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
                        "codex_events": {
                            "status": "recorded",
                            "attempts": 1,
                            "recorded_attempts": 1,
                            "unknown_attempts": 0,
                            "counts": {"turn.completed": 1},
                        },
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
    assert [health["status"] for health in result["wave_runtime_health"]] == [
        "healthy",
        "healthy",
        "healthy",
    ]


def test_rolling_controller_holds_unique_runtime_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    pending = iter((1, 0))
    current_pending = [0]

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
        assert finance_preaudit_concurrency_probe.runtime_control.controller_active()
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=1.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "needs_human",
                        "token_usage": {"status": "unknown"},
                    },
                )
                for index in range(count)
            ),
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live_status,
        pipeline_runner=pipeline,
        reconcile_status=lambda: _reconciliation_snapshot(current_pending[0]),
    )

    assert result["completed"] == 1
    assert not finance_preaudit_concurrency_probe.runtime_control.controller_active()

    with finance_preaudit_concurrency_probe.runtime_control.global_controller_lease():
        with pytest.raises(
            ValueError,
            match="production_rolling_controller_active",
        ):
            finance_preaudit_concurrency_probe.run_rolling_drain(capacity=6)


@pytest.mark.parametrize(
    ("host_status", "expected_reason"),
    (("critical", "host_critical"), ("healthy", "technical_failure")),
)
def test_rolling_failure_retains_health_and_stable_reason(
    host_status: str,
    expected_reason: str,
) -> None:
    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        adaptive=True,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": 12},
            },
        },
        reconcile_status=lambda: _reconciliation_snapshot(12),
        pipeline_runner=lambda count, capacity: process_pool.PoolResult(
            requested=count,
            started=1,
            completed=0,
            failed=0,
            timed_out=0,
            stopped=count,
            peak_active=capacity,
            elapsed_seconds=10,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="stopped",
                    value=None,
                    error_code="stop_requested",
                )
                for index in range(count)
            ),
            runtime_samples=(
                {
                    "active_workers": 6,
                    "oldest_task_seconds": 8,
                    "host": {
                        "status": host_status,
                        "memory_free_percent": 11,
                        "load_per_cpu": 0.5,
                        "active_threads": 1,
                        "cpu_count": 10,
                    },
                },
            ),
            max_task_elapsed_seconds=8,
        ),
    )

    assert result["status"] == "failed"
    assert result["stop_reason"] == expected_reason
    assert result["last_capacity_decision"]["reason"] == expected_reason
    assert result["wave_runtime_health"] == [
        {
            "status": host_status,
            "sample_count": 1,
            "minimum_memory_free_percent": 11,
            "maximum_load_per_cpu": 0.5,
            "maximum_active_threads": 1,
            "maximum_active_workers": 6,
            "maximum_oldest_task_seconds": 8.0,
        }
    ]


def test_rolling_controller_holds_unique_runtime_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    pending = iter((1, 0))
    current_pending = [0]

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
        assert finance_preaudit_concurrency_probe.runtime_control.controller_active()
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=1.0,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "needs_human",
                        "token_usage": {"status": "unknown"},
                    },
                )
                for index in range(count)
            ),
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live_status,
        pipeline_runner=pipeline,
        reconcile_status=lambda: _reconciliation_snapshot(current_pending[0]),
    )

    assert result["completed"] == 1
    assert not finance_preaudit_concurrency_probe.runtime_control.controller_active()

    with finance_preaudit_concurrency_probe.runtime_control.global_controller_lease():
        with pytest.raises(
            ValueError,
            match="production_rolling_controller_active",
        ):
            finance_preaudit_concurrency_probe.run_rolling_drain(capacity=6)


def test_rolling_failure_retains_host_critical_health_reason() -> None:
    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        adaptive=True,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": 12},
            },
        },
        reconcile_status=lambda: _reconciliation_snapshot(12),
        pipeline_runner=lambda count, capacity: process_pool.PoolResult(
            requested=count,
            started=1,
            completed=0,
            failed=0,
            timed_out=0,
            stopped=count,
            peak_active=capacity,
            elapsed_seconds=10,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="stopped",
                    value=None,
                    error_code="stop_requested",
                )
                for index in range(count)
            ),
            runtime_samples=(
                {
                    "active_workers": 6,
                    "oldest_task_seconds": 8,
                    "host": {
                        "status": "critical",
                        "memory_free_percent": 11,
                        "load_per_cpu": 0.5,
                        "active_threads": 1,
                        "cpu_count": 10,
                    },
                },
            ),
            max_task_elapsed_seconds=8,
        ),
    )

    assert result["status"] == "failed"
    assert result["stop_reason"] == "host_critical"
    assert result["last_capacity_decision"]["reason"] == "host_critical"
    assert result["wave_runtime_health"] == [
        {
            "status": "critical",
            "sample_count": 1,
            "minimum_memory_free_percent": 11,
            "maximum_load_per_cpu": 0.5,
            "maximum_active_threads": 1,
            "maximum_active_workers": 6,
            "maximum_oldest_task_seconds": 8.0,
        }
    ]


def test_rolling_wave_preserves_transient_codex_error_counts() -> None:
    metrics = finance_preaudit_concurrency_probe._rolling_wave_metrics(
        process_pool.PoolResult(
            requested=1,
            started=1,
            completed=1,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=1,
            elapsed_seconds=1.0,
            outcomes=(
                process_pool.TaskOutcome(
                    index=0,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "pass",
                        "process_attempts": 2,
                        "process_retries": 1,
                        "prepare_elapsed_seconds": 0.1,
                        "token_usage": {"status": "unknown"},
                        "codex_events": {
                            "status": "recorded",
                            "attempts": 2,
                            "recorded_attempts": 2,
                            "unknown_attempts": 0,
                            "counts": {
                                "error": 1,
                                "turn.completed": 2,
                            },
                        },
                    },
                ),
            ),
        )
    )

    assert metrics["transient_error_events"] == 1
    assert metrics["codex_events"]["counts"] == {
        "error": 1,
        "turn.completed": 2,
    }


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
    assert result["codex_events"] == {
        "status": "unknown",
        "workers": 0,
        "recorded_workers": 0,
        "unknown_workers": 0,
        "attempts": 0,
        "recorded_attempts": 0,
        "unknown_attempts": 0,
        "counts": {},
    }
    assert result["transient_error_events"] is None


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
        runtime_status=lambda: {"status": "healthy"},
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


def test_dispatcher_times_out_stuck_preparation_before_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    reviews: list[dict] = []
    completions: list[Path] = []
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
        "_prepare_one",
        _slow_failed_prepare,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        review_worker=lambda task: reviews.append(task) or {"status": "reviewed"},
        complete_review=lambda action, _reviewed: completions.append(action)
        or {"status": "completed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        prepare_timeout_sec=0.01,
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.timed_out == 1
    assert result.outcomes[0].error_code == "production_probe_prepare_timeout"
    assert reviews == []
    assert completions == []
    assert result.runtime_samples
    assert result.max_task_elapsed_seconds >= 0.01
    assert stop_requests == [True]


def test_dispatcher_dispatches_successful_preparation_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared_action = (
        tmp_path
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / "prepared"
        / "attempt-1"
        / "action.json"
    )
    prepared_action.parent.mkdir(parents=True)
    prepared_action.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OA_TEST_PREPARED_ACTION_FILE", str(prepared_action))
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    reviews: list[dict] = []

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=_successful_prepare,
        review_worker=lambda task: reviews.append(task)
        or {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        },
        complete_review=lambda _action, _reviewed: {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "error_code": None,
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.timed_out == 0
    assert [task["action_file"] for task in reviews] == [str(prepared_action)]


def test_dispatcher_harvests_completed_preparation_before_timeout_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class CompletedExecutor:
        _processes: dict = {}

        def submit(self, action, *args) -> Future:
            future = Future()
            future.set_result(action(*args))
            return future

        def shutdown(self, **_kwargs) -> None:
            return None

    prepared_action = (
        tmp_path
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / "prepared-at-boundary"
        / "attempt-1"
        / "action.json"
    )
    prepared_action.parent.mkdir(parents=True)
    prepared_action.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OA_TEST_PREPARED_ACTION_FILE", str(prepared_action))
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        CompletedExecutor,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=_successful_prepare,
        review_worker=lambda _task: {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        },
        complete_review=lambda _action, _reviewed: {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        prepare_timeout_sec=1e-9,
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.timed_out == 0
    assert result.outcomes[0].error_code is None


def test_dispatcher_harvests_review_while_preparation_is_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    completions: list[Path] = []
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
    action_file = _open_action(
        flow_root,
        case_key="f" * 64,
        created_at="2026-08-24T08:00:00+00:00",
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=_slow_failed_prepare,
        review_worker=lambda _task: {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
        },
        complete_review=lambda action, _reviewed: completions.append(action)
        or {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        prepare_timeout_sec=0.02,
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.timed_out == 1
    assert completions == [action_file]
    assert result.outcomes[1].error_code == "production_probe_prepare_timeout"
    assert stop_requests == [True]


def test_dispatcher_keeps_only_one_outstanding_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="c" * 64,
        created_at="2026-08-24T08:00:00+00:00",
    )
    first_finished = threading.Event()
    submitted_while_first_running: list[bool] = []
    preparation_calls: list[int] = []
    preparation_guard = threading.Lock()

    class TrackingExecutor:
        _processes: dict = {}

        def __init__(self) -> None:
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._submitted = 0

        def submit(self, action, *args) -> Future:
            self._submitted += 1
            ordinal = self._submitted
            if ordinal > 1 and not first_finished.is_set():
                submitted_while_first_running.append(True)

            def run() -> dict:
                try:
                    return action(*args)
                finally:
                    if ordinal == 1:
                        first_finished.set()

            return self._executor.submit(run)

        def shutdown(self, **kwargs) -> None:
            self._executor.shutdown(**kwargs)

    def prepare(_workflow: str) -> dict:
        with preparation_guard:
            ordinal = len(preparation_calls) + 1
            preparation_calls.append(ordinal)
        time.sleep(0.05)
        action_file = _open_action(
            flow_root,
            case_key=str(ordinal) * 64,
            created_at=f"2026-08-24T08:00:0{ordinal}+00:00",
        )
        return {
            "action": "review_required",
            "action_file": str(action_file),
        }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        TrackingExecutor,
    )
    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        3,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=prepare,
        review_worker=lambda _task: {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        },
        complete_review=lambda _action, _reviewed: {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert preparation_calls == [1, 2]
    assert submitted_while_first_running == []
    assert result.completed == 3
    assert result.stopped == 0


def test_dispatcher_stops_after_preparation_technical_error_without_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    reviews: list[dict] = []
    completions: list[Path] = []
    marker = tmp_path / "child.pid"
    monkeypatch.setenv("OA_TEST_PREPARE_CHILD_PID_FILE", str(marker))
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

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=_failed_prepare_with_owned_child,
        review_worker=lambda task: reviews.append(task) or {"status": "reviewed"},
        complete_review=lambda action, _reviewed: completions.append(action)
        or {"status": "completed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.outcomes[0].value["status"] == "failed"
    assert result.outcomes[0].value["error_code"] == "material_collection_failed"
    assert reviews == []
    assert completions == []
    assert stop_requests == [True]
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("preparation child process was not reclaimed")


def test_dispatcher_harvests_prepare_error_that_requested_global_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = False
    reviews: list[dict] = []

    class CompletedExecutor:
        _processes: dict = {}

        def submit(self, action, *args) -> Future:
            future = Future()
            future.set_result(action(*args))
            return future

        def shutdown(self, **_kwargs) -> None:
            return None

    def prepare(_workflow: str) -> dict:
        nonlocal stopped
        stopped = True
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }

    def request_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        CompletedExecutor,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: stopped,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        request_stop,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=prepare,
        review_worker=lambda task: reviews.append(task) or {"status": "reviewed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.stopped == 0
    assert result.outcomes[0].value["status"] == "failed"
    assert (
        result.outcomes[0].value["error_code"]
        == "production_probe_prepare_failed"
    )
    assert reviews == []


def test_dispatcher_bounded_drains_prepare_error_after_stop_signal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = False
    reviews: list[dict] = []

    class DelayedExecutor:
        _processes: dict = {}

        def __init__(self) -> None:
            self._executor = ThreadPoolExecutor(max_workers=1)

        def submit(self, action, *args) -> Future:
            return self._executor.submit(action, *args)

        def shutdown(self, **kwargs) -> None:
            self._executor.shutdown(**kwargs)

    def prepare(_workflow: str) -> dict:
        nonlocal stopped
        stopped = True
        time.sleep(0.05)
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }

    def request_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        DelayedExecutor,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: stopped,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        request_stop,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=prepare,
        review_worker=lambda task: reviews.append(task) or {"status": "reviewed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 1
    assert result.stopped == 0
    assert result.outcomes[0].value["status"] == "failed"
    assert (
        result.outcomes[0].value["error_code"]
        == "production_probe_prepare_failed"
    )
    assert reviews == []


def test_dispatcher_prepare_timeout_reclaims_owned_child_process_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    marker = tmp_path / "child.pid"
    stop_requests: list[bool] = []
    monkeypatch.setenv("OA_TEST_PREPARE_CHILD_PID_FILE", str(marker))
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

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=_prepare_with_owned_child,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        prepare_timeout_sec=1,
        heartbeat_interval_sec=0.01,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.timed_out == 1
    assert marker.is_file()
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("preparation child process was not reclaimed")
    assert stop_requests == [True]


def test_dispatcher_retries_one_prewrite_invalid_decision_with_merged_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    review_calls: list[int] = []
    completion_attempts: list[int] = []
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
        case_key="9" * 64,
        created_at="2026-08-24T12:00:00+00:00",
    )

    def review(_task: dict) -> dict:
        current = len(review_calls) + 1
        review_calls.append(current)
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {
                "status": "recorded",
                "input_tokens": current * 10,
                "cached_input_tokens": current,
                "output_tokens": current * 2,
                "total_tokens": current * 12,
            },
            "codex_events": {
                "status": "recorded",
                "attempts": 1,
                "recorded_attempts": 1,
                "unknown_attempts": 0,
                "counts": {"turn.completed": 1},
            },
            "agent_wall_seconds": current,
            "decision": {"overall": "needs_human"},
        }

    def complete(_action: Path, reviewed: dict) -> dict:
        attempts = reviewed["process_attempts"]
        completion_attempts.append(attempts)
        if attempts == 1:
            return {
                **reviewed,
                "status": "retry_required",
                "error_code": "codex_decision_invalid",
            }
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert result.completed == 1
    assert review_calls == [1, 2]
    assert completion_attempts == [1, 2]
    assert value["process_attempts"] == 2
    assert value["process_retries"] == 1
    assert value["token_usage"] == {
        "status": "recorded",
        "input_tokens": 30,
        "cached_input_tokens": 3,
        "output_tokens": 6,
        "total_tokens": 36,
    }
    assert value["codex_events"] == {
        "status": "recorded",
        "attempts": 2,
        "recorded_attempts": 2,
        "unknown_attempts": 0,
        "counts": {"turn.completed": 2},
    }
    assert stop_requests == []


def test_dispatcher_stops_after_second_prewrite_invalid_decision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    review_calls: list[bool] = []
    completion_calls: list[bool] = []
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
        case_key="8" * 64,
        created_at="2026-08-24T12:00:00+00:00",
    )

    def review(_task: dict) -> dict:
        review_calls.append(True)
        return {
            "status": "reviewed",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "codex_events": {
                "status": "unknown",
                "attempts": 1,
                "recorded_attempts": 0,
                "unknown_attempts": 1,
                "counts": {},
            },
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        }

    def complete(_action: Path, reviewed: dict) -> dict:
        completion_calls.append(True)
        return {
            **reviewed,
            "status": "retry_required",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_decision_invalid",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert review_calls == [True, True]
    assert completion_calls == [True, True]
    assert value["status"] == "failed"
    assert value["write_state"] == "not_requested"
    assert value["error_code"] == "codex_decision_invalid"
    assert value["process_attempts"] == 2
    assert value["process_retries"] == 1
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
        runtime_status=lambda: {"status": "healthy"},
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
        runtime_status=lambda: {"status": "healthy"},
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
    assert result["codex_events"] == {
        "status": "recorded",
        "workers": 12,
        "recorded_workers": 12,
        "unknown_workers": 0,
        "attempts": 12,
        "recorded_attempts": 12,
        "unknown_attempts": 0,
        "counts": {"turn.completed": 12},
    }
    assert result["token_usage"]["total_tokens"] == 1320
    assert result["runtime_health"]["status"] == "healthy"
    assert result["watchdog"] == {
        "runtime_samples": 1,
        "maximum_task_seconds": 4.0,
        "task_timeout_seconds": 960,
    }
    encoded = json.dumps(result)
    assert str(flow_root) not in encoded


def test_event_aggregation_keeps_invalid_worker_attempts_unknown() -> None:
    result = finance_preaudit_concurrency_probe._aggregate_codex_events(
        [
            {
                "process_attempts": 2,
                "codex_events": {"status": "unknown"},
            }
        ]
    )

    assert result == {
        "status": "unknown",
        "workers": 1,
        "recorded_workers": 0,
        "unknown_workers": 1,
        "attempts": 2,
        "recorded_attempts": 0,
        "unknown_attempts": 2,
        "counts": {},
    }


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
            "codex_events": {
                "status": "recorded",
                "workers": sample_size,
                "recorded_workers": sample_size,
                "unknown_workers": 0,
                "attempts": sample_size,
                "recorded_attempts": sample_size,
                "unknown_attempts": 0,
                "counts": {"turn.completed": sample_size},
            },
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
    assert result["codex_events"]["counts"] == {"turn.completed": 120}
    assert result["transient_error_events"] == 0
    assert result["last_decision"] == {
        "action": "increase",
        "next_capacity": 12,
        "healthy_windows": 0,
        "reason": "healthy_window_threshold",
    }
