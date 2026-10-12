from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import (
    codex_review_worker,
    concurrency_control,
    observability,
    process_pool,
)
from scripts import finance_preaudit_concurrency_probe


@pytest.fixture(autouse=True)
def isolate_alert_delivery(tmp_path, monkeypatch):
    alert = finance_preaudit_concurrency_probe.session_alert
    original = alert.alert_once
    def local_alert(code, **kwargs):
        kwargs["control_root"] = tmp_path / "alerts"
        return original(code, **kwargs)
    monkeypatch.setattr(alert, "alert_once", local_alert)
    monkeypatch.setattr(alert, "send_message", lambda *args, **kwargs: None)
    from tests.finance_preaudit.test_approval_guard import official_history
    home = tmp_path / "official"
    official_history(home, finance_preaudit_concurrency_probe.PROJECT_ROOT, [])
    monkeypatch.setenv("CODEX_HOME", str(home))


def _slow_failed_prepare(_workflow: str) -> dict:
    time.sleep(0.05)
    return {"action": "failed", "error_code": "late_prepare_result"}


def _successful_prepare(_workflow: str) -> dict:
    return {
        "action": "review_required",
        "action_file": os.environ["OA_TEST_PREPARED_ACTION_FILE"],
    }


def _persisted_identity_reader(
    action_file: str,
    *,
    request_id: str,
    received_at: str,
    workflow_key: str,
):
    action_path = Path(action_file)
    state_path = action_path.with_name("state.json")

    def read(path: Path) -> dict:
        if path == action_path:
            return {
                "action_file": str(action_path),
                "state_file": str(state_path),
            }
        if path == state_path:
            return {
                "action_file": str(action_path),
                "state_file": str(state_path),
                "request_id": request_id,
                "received_at": received_at,
                "workflow_key": workflow_key,
            }
        return {}

    return read


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


def _open_action(
    root: Path,
    *,
    case_key: str,
    created_at: str,
    workflow_key: str = "travel_expense",
    received_at: str = "2026-08-23T08:00:00+08:00",
) -> Path:
    request_id = f"request-{case_key}"
    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    attempt_root = (
        root
        / workflow_key
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
        "workflow_key": workflow_key,
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


def _rejected_decision_result(reviewed: dict) -> dict:
    return {
        **codex_review_worker._base_result(attempts=reviewed["process_attempts"], usages=[reviewed["token_usage"]]),
        "codex_events": reviewed["codex_events"], "status": "retry_required",
        "execution_state": "review_required", "business_state": "not_available",
        "write_state": "not_requested", "error_code": "codex_decision_invalid",
    }


def _quarantined_action(
    root: Path,
    *,
    request_id: str,
    received_at: str,
    failure_detail_code: str | None = "attachment_download_failed",
    reason: str = "material_collection_failed",
) -> Path:
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
        "action": "needs_admin",
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "not_requested",
        "workflow_key": "travel_expense",
        "reason": reason,
        "request_id": request_id,
        "received_at": received_at,
        "case_key": case_key,
        "attempt": 1,
        "action_file": str(action_file),
        "state_file": str(state_file),
        **(
            {"failure_detail_code": failure_detail_code}
            if failure_detail_code is not None
            else {}
        ),
    }
    attempt_root.mkdir(parents=True)
    if reason == "codex_review_failed":
        receipt_file = attempt_root / "review-failure.json"
        json_artifact.write_json_atomic(
            receipt_file,
            {
                "schema_version": 1,
                "kind": "preaudit_review_failure",
                "action_token": "historical-action-token",
                "workflow_key": "travel_expense",
                "model": codex_review_worker.MODEL,
                "reasoning_effort": codex_review_worker.REASONING_EFFORT,
                "model_provider": "openai",
                "requested_service_tier": "default",
                "process_attempts": 2,
                "process_retries": 1,
                "failure_code": "codex_process_failed",
                "attempt_error_codes": [
                    "codex_process_failed",
                    "codex_process_failed",
                ],
            },
        )
        payload["review_failure_fingerprint"] = (
            finance_preaudit_concurrency_probe._file_fingerprint(receipt_file)
        )
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
        "continuation": {
            "status": "ready", "scope": "item" if quarantined else "none",
            "isolated_pending": quarantined, "blocking_reasons": {},
        },
        "failure_attention": {
            "scope": "live_pending",
            "status": "clear",
            "affected_cases": 0,
            "repeated_failure_cases": 0,
            "long_wait_failure_cases": 0,
            "long_wait_threshold_seconds": 900,
            "max_failure_attempts": 3,
            "oldest_wait_seconds": 0,
            "failure_counts": {},
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
        "model": "gpt-6.1-sol",
        "reasoning_effort": "medium",
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


def test_renew_failure_requests_only_cycle_stop(tmp_path: Path, monkeypatch) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_file = _open_action(
        flow_root,
        case_key="a" * 64,
        created_at="2026-08-31T00:00:00+00:00",
    )
    cycle_stop_requests: list[bool] = []
    persistent_stop_requests: list[str | None] = []

    class Engine:
        def _resume_active_action(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: cycle_stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: persistent_stop_requests.append(reason),
    )

    with pytest.raises(ValueError, match="^production_probe_renew_failed$"):
        finance_preaudit_concurrency_probe._renew_actions([action_file])

    assert cycle_stop_requests == [True]
    assert persistent_stop_requests == []


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
                "reason": "writer_preflight_failed",
                "write_state": "not_requested",
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


def test_development_clone_keeps_isolated_items_in_diagnostic_queue(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path / "oa-workspace-dev",
    )
    assert finance_preaudit_concurrency_probe._include_isolated_development_items()


def test_production_clone_keeps_isolated_items_out_of_queue(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path / "oa-workspace-minimal",
    )
    assert not finance_preaudit_concurrency_probe._include_isolated_development_items()


def test_prepare_global_fifo_selects_oldest_supported_workflow(
    monkeypatch,
) -> None:
    selected = {
        "requestId": "older-request",
        "received_at": "2026-08-25T08:00:00+08:00",
        "flow_key": "business_entertainment",
        "support_status": "supported",
        "can_submit": True,
    }
    calls: list[str] = []
    engine_selections: list[tuple[str, str]] = []

    class Engine:
        def next_action(self, workflow_key: str, **_kwargs) -> dict:
            calls.append(workflow_key)
            return {
                "action": "review_required",
                "action_file": "/safe/older-action.json",
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": "newer-request",
                "received_at": "2026-08-25T09:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            },
            selected,
        ],
        raising=False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: (
            engine_selections.append(selected_identity) or Engine()
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_read_object",
        _persisted_identity_reader(
            "/safe/older-action.json",
            request_id=selected["requestId"],
            received_at=selected["received_at"],
            workflow_key=selected["flow_key"],
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda _reason=None: None,
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
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
        raising=False,
    )

    assert finance_preaudit_concurrency_probe._prepare_one("*") == {
        "action": "review_required",
        "action_file": "/safe/older-action.json",
    }
    assert calls == ["business_entertainment"]
    assert engine_selections == [
        (selected["requestId"], selected["received_at"])
    ]


def test_prepared_action_matches_selected_case_path(tmp_path: Path) -> None:
    selected = {
        "requestId": "selected-request",
        "received_at": "2026-08-25T08:00:00+08:00",
    }
    workflow_key = "travel_expense"
    case_key = hashlib.sha256(
        f"{selected['requestId']}\x1f{selected['received_at']}".encode()
    ).hexdigest()
    action_path = (
        tmp_path
        / "runtime"
        / "finance_preaudit_flows"
        / workflow_key
        / "cases"
        / case_key
        / "attempt-1"
        / "action.json"
    )
    shared = {
        "action": "review_required",
        "execution_state": "review_required",
        "business_state": "not_available",
        "write_state": "authorized",
        "workflow_key": workflow_key,
        "case_key": case_key,
        "attempt": 1,
        "mode": "controlled_write",
        "write_authorized": True,
        "action_file": str(action_path),
        "state_file": str(action_path.with_name("state.json")),
    }
    state = {
        **shared,
        "request_id": selected["requestId"],
        "received_at": selected["received_at"],
    }

    assert finance_preaudit_concurrency_probe._prepared_action_matches_selection(
        action_path,
        shared,
        state,
        selected,
        workflow_key,
        project_root=tmp_path,
    )
    assert not finance_preaudit_concurrency_probe._prepared_action_matches_selection(
        action_path.parent.parent.parent / "different-case" / "attempt-1" / "action.json",
        shared,
        state,
        selected,
        workflow_key,
        project_root=tmp_path,
    )
    assert not finance_preaudit_concurrency_probe._prepared_action_matches_selection(
        action_path,
        {**shared, "workflow_key": "business_support_fee"},
        state,
        selected,
        workflow_key,
        project_root=tmp_path,
    )


def test_prepare_global_fifo_stops_when_claimed_identity_changes(
    monkeypatch,
) -> None:
    stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, _workflow_key: str, **_kwargs) -> dict:
            return {
                "action": "review_required",
                "action_file": "/safe/wrong-action.json",
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": "fifo-head",
                "received_at": "2026-08-25T08:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
        raising=False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_read_object",
        _persisted_identity_reader(
            "/safe/wrong-action.json",
            request_id="different-request",
            received_at="2026-08-25T08:00:00+08:00",
            workflow_key="travel_expense",
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stop_requests.append(reason),
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
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one("*") == {
        "action": "failed",
        "error_code": "production_probe_fifo_mismatch",
    }
    assert stop_requests == ["production_probe_prepare_failed"]


def _assert_prepare_failure(
    result: dict,
    *,
    stage: str,
    error_code: str,
    stop_action: str,
    error_class_name: str | None = None,
) -> dict:
    """Assert the bounded, redacted prepare-failure diagnostic."""
    diagnostic = result["prepare_failure"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["stage"] == stage
    assert diagnostic["error_code"] == error_code
    assert diagnostic["reason_code"] == error_code
    assert diagnostic["stop_action"] == stop_action
    assert diagnostic["error_class_name"] == error_class_name
    return diagnostic


def test_prepare_live_discovery_failure_requests_only_cycle_stop(
    monkeypatch,
) -> None:
    cycle_stop_requests: list[bool] = []
    persistent_stop_requests: list[str | None] = []

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: (_ for _ in ()).throw(
            RuntimeError(
                "oa_execution_identity_login_failed: redacted login detail"
            )
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: cycle_stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: persistent_stop_requests.append(reason),
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
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one("*")
    assert result["action"] == "failed"
    assert result["error_code"] == "oa_execution_identity_login_failed"
    diagnostic = _assert_prepare_failure(
        result,
        stage="live_pending_read",
        error_code="oa_execution_identity_login_failed",
        stop_action="cycle_stop",
        error_class_name="RuntimeError",
    )
    assert "redacted login detail" not in json.dumps(diagnostic)
    assert cycle_stop_requests == [True]
    assert persistent_stop_requests == []


@pytest.mark.parametrize("failure_stage", ("engine", "next_action"))
def test_prepare_propagates_session_failure_from_live_engine(
    failure_stage: str,
    monkeypatch,
) -> None:
    cycle_stop_requests: list[bool] = []
    persistent_stop_requests: list[str | None] = []
    error = RuntimeError("rpa_session_invalid: redacted session detail")

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            raise error

    def live_engine(*, controlled: bool) -> Engine:
        assert controlled is True
        if failure_stage == "engine":
            raise error
        return Engine()

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        live_engine,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: cycle_stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: persistent_stop_requests.append(reason),
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

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")
    assert result["action"] == "failed"
    assert result["error_code"] == "rpa_session_invalid"
    diagnostic = _assert_prepare_failure(
        result,
        stage=("engine_build" if failure_stage == "engine" else "next_action"),
        error_code="rpa_session_invalid",
        stop_action="cycle_stop",
        error_class_name="RuntimeError",
    )
    assert "redacted session detail" not in json.dumps(diagnostic)
    assert cycle_stop_requests == [True]
    assert persistent_stop_requests == []


def test_prepare_global_fifo_skips_actions_already_claimed_by_controller(
    monkeypatch,
) -> None:
    calls: list[str] = []
    claimed = ("claimed-request", "2026-08-25T08:00:00+08:00")

    class Engine:
        def next_action(self, workflow_key: str, **_kwargs) -> dict:
            calls.append(workflow_key)
            return {
                "action": "review_required",
                "action_file": "/safe/next-action.json",
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": claimed[0],
                "received_at": claimed[1],
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            },
            {
                "requestId": "next-request",
                "received_at": "2026-08-25T08:01:00+08:00",
                "flow_key": "business_entertainment",
                "support_status": "supported",
                "can_submit": True,
            },
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_read_object",
        _persisted_identity_reader(
            "/safe/next-action.json",
            request_id="next-request",
            received_at="2026-08-25T08:01:00+08:00",
            workflow_key="business_entertainment",
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda _reason=None: None,
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
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [list(claimed)],
        }
    ) == {
        "action": "review_required",
        "action_file": "/safe/next-action.json",
    }
    assert calls == ["business_entertainment"]


def test_prepare_global_fifo_includes_new_live_item_before_snapshot_finishes(
    monkeypatch,
) -> None:
    first = ("first-request", "2026-08-25T08:00:00+08:00")
    remaining = ("remaining-request", "2026-08-25T08:02:00+08:00")
    arrived = ("arrived-request", "2026-08-25T08:01:00+08:00")
    pending = [first, remaining]
    selected: list[tuple[str, str]] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            return {
                "action": "review_required",
                "action_file": "/safe/next-action.json",
            }

    def engine_factory(
        *, controlled: bool, selected_identity: tuple[str, str]
    ) -> Engine:
        selected.append(selected_identity)
        return Engine()

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": request_id,
                "received_at": received_at,
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
            for request_id, received_at in pending
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        engine_factory,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
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
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_read_object",
        lambda _path: {},
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
        }
    )["action"] == "review_required"
    pending.insert(1, arrived)
    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [list(first)],
            "historical_unknown_identities": [],
            "recover_action_files": [],
        }
    )["action"] == "review_required"

    assert selected == [first, arrived]


def test_prepare_global_fifo_scopes_historical_skips_to_selected_workflow(
    monkeypatch,
) -> None:
    historical = ("historical-unknown", "2026-08-25T08:00:00+08:00")
    historical_action = Path("/safe/historical-action.json")
    skip_calls: list[frozenset[Path]] = []
    authorization_skip_calls: list[frozenset[Path]] = []
    historical_travel_authorization = (
        finance_preaudit_concurrency_probe.PROJECT_ROOT
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / "historical"
        / "attempt-1"
        / "action.json"
    )
    selected = {
        "requestId": "next-request",
        "received_at": "2026-08-25T08:01:00+08:00",
        "flow_key": "business_support_fee",
        "support_status": "supported",
        "can_submit": True,
    }
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": historical[0],
                "received_at": historical[1],
                "flow_key": "business_support_fee",
                "support_status": "supported",
                "can_submit": True,
            },
            selected,
        ],
    )

    class Engine:
        def next_action(
            self,
            _workflow_key: str,
            *,
            skip_write_authorization_failures: frozenset[Path],
            skip_unknown_failures: frozenset[Path],
            **_kwargs,
        ) -> dict:
            authorization_skip_calls.append(skip_write_authorization_failures)
            skip_calls.append(skip_unknown_failures)
            return {
                "action": "review_required",
                "action_file": "/safe/next-action.json",
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_read_object",
        _persisted_identity_reader(
            "/safe/next-action.json",
            request_id=selected["requestId"],
            received_at=selected["received_at"],
            workflow_key=selected["flow_key"],
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda _reason=None: None,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        lambda: frozenset({historical_travel_authorization}),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_unknown_action_files",
        lambda identities, *, workflow_key: (
            frozenset({historical_action})
            if identities == {historical}
            and workflow_key == "business_support_fee"
            else frozenset()
        ),
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [list(historical)],
        }
    ) == {
        "action": "review_required",
        "action_file": "/safe/next-action.json",
    }
    assert authorization_skip_calls == [frozenset()]
    assert skip_calls == [frozenset({historical_action})]


def test_local_quarantine_excludes_historical_no_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    (tmp_path / "runtime" / "finance_preaudit_flows").mkdir(parents=True)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.reconciliation,
        "latest_states",
        lambda _root: [
            {
                "request_id": "historical-request",
                "received_at": "2026-08-25T08:00:00+08:00",
                "execution_state": "needs_admin",
                "write_state": "not_requested",
                "reason": "write_authorization_invalid",
            }
        ],
    )

    assert finance_preaudit_concurrency_probe._local_quarantined_identities() == (
        frozenset(
            {("historical-request", "2026-08-25T08:00:00+08:00")}
        )
    )


def test_missing_local_runtime_is_empty_but_symlink_is_invalid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )

    assert finance_preaudit_concurrency_probe._latest_local_states() == []

    (tmp_path / "runtime").symlink_to(
        tmp_path / "missing-runtime",
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        finance_preaudit_concurrency_probe._latest_local_states()


def test_local_unknown_quarantine_snapshot_is_separate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    (tmp_path / "runtime" / "finance_preaudit_flows").mkdir(parents=True)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.reconciliation,
        "latest_states",
        lambda _root: [
            {
                "request_id": "historical-request",
                "received_at": "2026-08-25T08:00:00+08:00",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        ],
    )

    assert (
        finance_preaudit_concurrency_probe._local_unknown_quarantined_identities()
        == frozenset(
            {("historical-request", "2026-08-25T08:00:00+08:00")}
        )
    )
    assert finance_preaudit_concurrency_probe._local_quarantined_identities() == (
        frozenset()
    )


def test_global_dispatcher_passes_frozen_unknown_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    historical = frozenset(
        {("historical-request", "2026-08-25T08:00:00+08:00")}
    )
    captured: list[dict] = []
    sentinel = object()

    def run(_count: int, _capacity: int, **kwargs):
        captured.append(kwargs)
        return sentinel

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_run_dispatched_tasks",
        run,
    )

    result = finance_preaudit_concurrency_probe._run_global_dispatched_tasks(
        3,
        6,
        historical_unknown_identities=historical,
    )

    assert result is sentinel
    assert captured == [
        {
            "strict_global_fifo": True,
            "adaptive": False,
            "max_capacity": None,
            "historical_unknown_identities": historical,
            "committed_received_at": None,
            "committed_request_id": None,
            "known_late_visible_keys": frozenset(),
                "admission_cutoff": None,
                "continuous_discovery": True,
                "record_failure_round": (
                    finance_preaudit_concurrency_probe._record_failure_round
                ),
                "allowed_workflows": (
                    finance_preaudit_concurrency_probe.ROLLING_WORKFLOW_KEYS
                ),
                "run_id": None,
                "controller_instance_id": None,
            }
        ]


@pytest.mark.parametrize(
    "reason",
    ("material_collection_failed", "write_authorization_invalid"),
)
def test_prepare_one_recovers_an_explicit_confirmed_no_write_action(
    tmp_path: Path,
    monkeypatch,
    reason: str,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_file = _quarantined_action(
        flow_root,
        request_id="recovery-request",
        received_at="2026-08-25T08:00:00+08:00",
        reason=reason,
    )
    calls: list[tuple[Path, str, bool]] = []

    class Engine:
        def recover_confirmed_no_write(
            self,
            path: Path,
            *,
            mode: str,
            confirmed: bool,
        ) -> dict:
            calls.append((path, mode, confirmed))
            return {
                "action": "review_required",
                "action_file": str(tmp_path / "attempt-2" / "action.json"),
            }

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(action_file)],
        }
    )

    assert result == {
        "action": "review_required",
        "action_file": str(tmp_path / "attempt-2" / "action.json"),
    }
    assert calls == [(action_file, "controlled_write", True)]


def test_prepare_one_merges_live_and_recovery_candidates_in_global_fifo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    recovery = _quarantined_action(
        flow_root,
        request_id="newer-recovery",
        received_at="2026-08-25T09:00:00+08:00",
    )
    selected_identities: list[tuple[str, str]] = []

    class Engine:
        def next_action(self, *_args: object, **_kwargs: object) -> dict:
            return {
                "action": "review_required",
                "action_file": str(tmp_path / "live" / "action.json"),
            }

    def engine_factory(
        *,
        controlled: bool,
        selected_identity: tuple[str, str] | None = None,
    ) -> Engine:
        if selected_identity is not None:
            selected_identities.append(selected_identity)
        return Engine()

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        engine_factory,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": "older-live",
                "received_at": "2026-08-25T08:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(recovery)],
        }
    )

    assert result["action"] == "review_required"
    assert selected_identities == [
        ("older-live", "2026-08-25T08:00:00+08:00")
    ]


def test_prepare_one_sorts_recovery_inputs_by_operation_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    newer = _quarantined_action(
        flow_root,
        request_id="newer-recovery",
        received_at="2026-08-25T09:00:00+08:00",
    )
    older = _quarantined_action(
        flow_root,
        request_id="older-recovery",
        received_at="2026-08-25T08:00:00+08:00",
    )
    recovered: list[Path] = []

    class Engine:
        def recover_confirmed_no_write(
            self,
            path: Path,
            **_kwargs: object,
        ) -> dict:
            recovered.append(path)
            return {
                "action": "review_required",
                "action_file": str(tmp_path / "attempt-2" / "action.json"),
            }

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 2,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_prepared_action_matches_selection",
        lambda *_args, **_kwargs: True,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(newer), str(older)],
        }
    )

    assert result["action"] == "review_required"
    assert recovered == [older]


@pytest.mark.parametrize("outcome", ["raises", "needs_admin"])
def test_prepare_one_recovery_failure_requests_a_valid_stop(
    tmp_path: Path,
    monkeypatch,
    outcome: str,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    recovery = _quarantined_action(
        flow_root,
        request_id="failed-recovery",
        received_at="2026-08-25T08:00:00+08:00",
    )
    stops: list[str | None] = []

    class Engine:
        def recover_confirmed_no_write(self, *_args: object, **_kwargs: object) -> dict:
            if outcome == "raises":
                raise ValueError("recovery_not_safe")
            return {"action": "needs_admin"}

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stops.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(recovery)],
        }
    )

    assert result["action"] == "failed"
    assert result["error_code"] == "production_probe_recovery_failed"
    _assert_prepare_failure(
        result,
        stage=("unexpected_action" if outcome == "needs_admin" else "next_action"),
        error_code="production_probe_recovery_failed",
        stop_action="stop",
        error_class_name=(
            None if outcome == "needs_admin" else "ValueError"
        ),
    )
    assert stops == ["production_probe_prepare_failed"]


def test_prepare_one_recovery_preflight_failure_keeps_recovery_error_semantics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_action = (
        tmp_path
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / ("a" * 64)
        / "attempt-1"
        / "action.json"
    )
    stops: list[str | None] = []
    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stops.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 1,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(missing_action)],
        }
    )

    assert result == {
        "action": "failed",
        "error_code": "production_probe_recovery_failed",
    }
    assert stops == ["production_probe_prepare_failed"]


def test_prepare_one_rejects_duplicate_recovery_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    first = _quarantined_action(
        flow_root,
        request_id="duplicate-recovery",
        received_at="2026-08-25T08:00:00+08:00",
    )
    second = first.parent.parent / "attempt-2" / "action.json"
    second_state = second.with_name("state.json")
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload.update(
        attempt=2,
        action_file=str(second),
        state_file=str(second_state),
    )
    second.parent.mkdir()
    json_artifact.write_json_atomic(second, payload)
    json_artifact.write_json_atomic(second_state, payload)
    stops: list[str | None] = []
    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stops.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 2,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [str(first), str(second)],
        }
    )

    assert result == {
        "action": "failed",
        "error_code": "production_probe_recovery_failed",
    }
    assert stops == ["production_probe_prepare_failed"]


def test_unknown_snapshot_failure_happens_before_handoff_begin(monkeypatch) -> None:
    begins: list[dict] = []
    monkeypatch.setattr(finance_preaudit_concurrency_probe.approval_lifecycle, "startup", lambda *a, **kw: {"status": "ready"})

    def fail_snapshot() -> frozenset[tuple[str, str]]:
        raise ValueError("production_probe_state_invalid")

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.controller_handoff,
        "assert_ready_for_start",
        lambda: None,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "global_controller_lease",
        nullcontext,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.attachment_cache,
        "prune_expired",
        lambda: {"status": "ok"},
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_unknown_quarantined_identities",
        fail_snapshot,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.controller_handoff,
        "begin",
        lambda payload: begins.append(payload),
    )

    with pytest.raises(ValueError, match="production_probe_state_invalid"):
        finance_preaudit_concurrency_probe.run_rolling_drain(capacity=3)

    assert begins == []


def test_open_actions_are_global_fifo_across_workflows(tmp_path: Path) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    newer = _open_action(
        flow_root,
        case_key="newer-travel",
        created_at="2026-08-25T09:01:00+08:00",
        workflow_key="travel_expense",
        received_at="2026-08-25T09:00:00+08:00",
    )
    older = _open_action(
        flow_root,
        case_key="older-entertainment",
        created_at="2026-08-25T09:02:00+08:00",
        workflow_key="business_entertainment",
        received_at="2026-08-25T00:30:00Z",
    )

    assert finance_preaudit_concurrency_probe._open_action_files(tmp_path) == [
        older,
        newer,
    ]


def test_prepare_stops_on_unknown_write_result(monkeypatch) -> None:
    calls = 0
    stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            nonlocal calls
            calls += 1
            return {
                "action": "needs_admin",
                "reason": "writer_result_unknown",
                "write_state": "unknown",
            }

    def request_stop(reason=None) -> None:
        stop_requests.append(reason)
        if reason is not None:
            raise ValueError("stop_reason_invalid")

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
        request_stop,
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

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")
    assert result["action"] == "needs_admin"
    assert result["error_code"] == "writer_result_unknown"
    _assert_prepare_failure(
        result,
        stage="unexpected_action",
        error_code="writer_result_unknown",
        stop_action="cycle_stop",
    )
    assert calls == 1
    assert stop_requests == ["writer_result_unknown", None]


def test_prepare_extends_skip_budget_when_new_item_is_quarantined(
    monkeypatch,
) -> None:
    actions = iter(
        [
            {
                "action": "needs_admin",
                "reason": "writer_preflight_failed",
                "write_state": "not_requested",
            },
            {
                "action": "needs_admin",
                "reason": "material_collection_failed",
                "write_state": "not_requested",
            },
            {
                "action": "review_required",
                "action_file": "/safe/action.json",
            },
        ]
    )
    quarantine_counts = iter((1, 1, 2))
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
        lambda: next(quarantine_counts),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "review_required",
        "action_file": "/safe/action.json",
    }
    assert stop_requests == []


@pytest.mark.parametrize(
    ("reason", "failure_detail_code"),
    [
        ("material_collection_failed", "attachment_download_failed"),
        ("codex_decision_invalid", None),
        ("codex_review_failed", None),
    ],
)
def test_prepare_global_fifo_returns_trusted_zero_write_quarantine(
    tmp_path: Path,
    monkeypatch,
    reason: str,
    failure_detail_code: str | None,
) -> None:
    request_id = "quarantined-request"
    received_at = "2026-08-25T08:00:00+08:00"
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            action_file = _quarantined_action(
                flow_root,
                request_id=request_id,
                received_at=received_at,
                reason=reason,
                failure_detail_code=failure_detail_code,
            )
            return json.loads(action_file.read_text(encoding="utf-8"))

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": request_id,
                "received_at": received_at,
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
        }
    ) == {
        "action": "quarantined",
        "error_code": reason,
        "write_state": "not_requested",
    }
    assert stop_requests == []


def test_prepare_global_fifo_stops_on_trusted_session_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_id = "session-failure-request"
    received_at = "2026-08-31T08:00:00+08:00"
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    cycle_stop_requests: list[bool] = []
    persistent_stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            action_file = _quarantined_action(
                flow_root,
                request_id=request_id,
                received_at=received_at,
                failure_detail_code="oa_session_expired",
            )
            return json.loads(action_file.read_text(encoding="utf-8"))

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": request_id,
                "received_at": received_at,
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: cycle_stop_requests.append(True),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: persistent_stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
        }
    ) == {
        "action": "failed",
        "error_code": "oa_session_expired",
    }
    assert cycle_stop_requests == [True]
    assert persistent_stop_requests == []


def test_prepare_global_fifo_stops_on_untrusted_zero_write_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_id = "quarantined-request"
    received_at = "2026-08-25T08:00:00+08:00"
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            action_file = _quarantined_action(
                flow_root,
                request_id=request_id,
                received_at=received_at,
            )
            state_file = action_file.with_name("state.json")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["request_id"] = "different-request"
            json_artifact.write_json_atomic(state_file, state)
            return json.loads(action_file.read_text(encoding="utf-8"))

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": request_id,
                "received_at": received_at,
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda *, controlled, selected_identity: Engine(),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
        }
    ) == {
        "action": "failed",
        "error_code": "production_probe_fifo_mismatch",
    }
    assert stop_requests == ["production_probe_prepare_failed"]


@pytest.mark.parametrize(
    "forbidden_name",
    (
        "writer-boundary.json",
        "execution-receipt.json",
        "review-decision.json",
        "remark.txt",
        "submission-envelope.json",
    ),
)
@pytest.mark.parametrize("dangling_symlink", (False, True))
def test_zero_write_quarantine_rejects_forbidden_artifact(
    tmp_path: Path,
    forbidden_name: str,
    dangling_symlink: bool,
) -> None:
    request_id = "quarantined-request"
    received_at = "2026-08-25T08:00:00+08:00"
    action_file = _quarantined_action(
        tmp_path / "runtime" / "finance_preaudit_flows",
        request_id=request_id,
        received_at=received_at,
    )
    action = json.loads(action_file.read_text(encoding="utf-8"))
    state = json.loads(
        action_file.with_name("state.json").read_text(encoding="utf-8")
    )
    selected = {
        "requestId": request_id,
        "received_at": received_at,
    }

    assert finance_preaudit_concurrency_probe._quarantined_action_matches_selection(
        action_file,
        action,
        state,
        selected,
        "travel_expense",
        project_root=tmp_path,
    )
    forbidden = action_file.with_name(forbidden_name)
    if dangling_symlink:
        forbidden.symlink_to(action_file.with_name("missing-artifact"))
    elif forbidden.suffix == ".json":
        json_artifact.write_json_atomic(forbidden, {"state": "present"})
    else:
        forbidden.write_text("present", encoding="utf-8")
    assert not finance_preaudit_concurrency_probe._quarantined_action_matches_selection(
        action_file,
        action,
        state,
        selected,
        "travel_expense",
        project_root=tmp_path,
    )


@pytest.mark.parametrize(
    "failure_detail_code",
    (
        "oa_run_stopped",
        "frozen_material_changed",
        "material_request_mismatch",
        "resource_request_mismatch",
        "resource_ownership_untrusted",
        "material_collection_failed",
    ),
)
def test_zero_write_quarantine_rejects_system_or_identity_failure_detail(
    tmp_path: Path,
    failure_detail_code: str,
) -> None:
    request_id = "quarantined-request"
    received_at = "2026-08-25T08:00:00+08:00"
    action_file = _quarantined_action(
        tmp_path / "runtime" / "finance_preaudit_flows",
        request_id=request_id,
        received_at=received_at,
        failure_detail_code=failure_detail_code,
    )
    action = json.loads(action_file.read_text(encoding="utf-8"))
    state = json.loads(
        action_file.with_name("state.json").read_text(encoding="utf-8")
    )

    assert not finance_preaudit_concurrency_probe._quarantined_action_matches_selection(
        action_file,
        action,
        state,
        {"requestId": request_id, "received_at": received_at},
        "travel_expense",
        project_root=tmp_path,
    )


def test_prepare_stops_when_dynamic_quarantine_count_is_invalid(
    monkeypatch,
) -> None:
    quarantine_reads = 0
    stop_requests: list[str | None] = []

    class Engine:
        def next_action(self, *_args, **_kwargs) -> dict:
            return {
                "action": "needs_admin",
                "reason": "material_collection_failed",
                "write_state": "not_requested",
            }

    def quarantine_count() -> int:
        nonlocal quarantine_reads
        quarantine_reads += 1
        if quarantine_reads == 1:
            return 1
        raise ValueError("local_quarantine_invalid")

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
        lambda reason=None: stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        quarantine_count,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")
    assert result["action"] == "failed"
    assert result["error_code"] == "production_probe_prepare_failed"
    _assert_prepare_failure(
        result,
        stage="quarantine_probe",
        error_code="production_probe_prepare_failed",
        stop_action="stop",
        error_class_name="ValueError",
    )
    assert stop_requests == ["production_probe_prepare_failed"]


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

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")
    assert result["action"] == "needs_admin"
    assert result["error_code"] == "write_authorization_invalid"
    _assert_prepare_failure(
        result,
        stage="unexpected_action",
        error_code="write_authorization_invalid",
        stop_action="stop",
    )
    assert stop_requests == [True]


def test_prepare_skips_exact_historical_write_authorization_failure(
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    calls: list[frozenset[Path]] = []
    historical_action = (
        finance_preaudit_concurrency_probe.PROJECT_ROOT
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / "historical"
        / "attempt-1"
        / "action.json"
    )

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
        lambda: frozenset({historical_action}),
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        "travel_expense"
    ) == {
        "action": "review_required",
        "action_file": "/safe/new-action.json",
    }
    assert stop_requests == []
    assert calls == [frozenset({historical_action})]


def test_prepare_requests_stop_when_next_action_raises(monkeypatch) -> None:
    cycle_stop_requests: list[bool] = []
    persistent_stop_requests: list[str | None] = []

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
        lambda reason=None: persistent_stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: cycle_stop_requests.append(True),
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

    result = finance_preaudit_concurrency_probe._prepare_one("travel_expense")
    assert result["action"] == "failed"
    assert result["error_code"] == "production_probe_prepare_failed"
    diagnostic = _assert_prepare_failure(
        result,
        stage="next_action",
        error_code="production_probe_prepare_failed",
        stop_action="cycle_stop",
        error_class_name="RuntimeError",
    )
    assert "boom" not in json.dumps(diagnostic)
    assert cycle_stop_requests == [True]
    assert persistent_stop_requests == []


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


def test_prepare_stops_before_collecting_another_item_after_stop(
    tmp_path: Path,
    monkeypatch,
) -> None:
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
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )

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
    pending = iter([8, 0, 0])
    current_pending = [0]
    calls: list[tuple[int, int]] = []

    def live_status() -> dict:
        count = next(pending)
        current_pending[0] = count
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {
                    "travel_expense": min(count, 5),
                    "business_entertainment": max(0, count - 5),
                    "packaging_layout_approval": 4,
                },
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


def test_rolling_drain_routes_recoveries_through_the_isolated_prepare_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    recovery = tmp_path / "attempt-1" / "action.json"
    pending = iter([(3, 3), (2, 2), (2, 2)])
    current = [(0, 0)]
    calls: list[tuple[int, int, tuple[Path, ...]]] = []

    def live_status() -> dict:
        count, quarantined = next(pending)
        current[0] = (count, quarantined)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": count},
            },
        }

    def pipeline(
        count: int,
        capacity: int,
        **kwargs: object,
    ) -> process_pool.PoolResult[dict]:
        calls.append(
            (
                count,
                capacity,
                tuple(kwargs.get("recovery_action_files", ())),
            )
        )
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
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
                        "business_state": "needs_human",
                        "token_usage": {"status": "unknown"},
                    },
                ),
            ),
        )

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_run_global_dispatched_tasks",
        pipeline,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=3,
        recovery_action_files=(recovery,),
        live_status=live_status,
        pipeline_runner=pipeline,
        reconcile_status=lambda: _reconciliation_snapshot(*current[0]),
    )

    assert calls == [(1, 3, (recovery,))]
    assert result["completed"] == 1
    assert result["quarantined_pending"] == 2


def test_hourly_recovery_option_keeps_new_failures_deferred(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import case_recovery, production_write

    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(case_recovery, "select_once", lambda *args: ())
    monkeypatch.setattr(production_write, "_authorized_workflows", lambda *args: probe.ROLLING_WORKFLOW_KEYS)
    pending = [2]
    calls = []

    def pipeline(count, capacity, *, defer_review_failures=True, **kwargs):
        calls.append((count, capacity, defer_review_failures))
        pending[0] = 1
        return process_pool.PoolResult(
            requested=2, started=2, completed=2, failed=0, timed_out=0, stopped=0,
            peak_active=1, elapsed_seconds=1,
            outcomes=(
                process_pool.TaskOutcome(index=0, status="completed", value={
                    "status": "retry_pending", "error_code": "codex_review_retry_pending",
                    "write_state": "authorized", "token_usage": {"status": "unknown"}}),
                process_pool.TaskOutcome(index=1, status="completed", value={
                    "status": "completed", "write_state": "submitted", "token_usage": {"status": "unknown"}}),
            ),
        )

    monkeypatch.setattr(probe, "_run_global_dispatched_tasks", pipeline)
    result = probe.run_rolling_drain(
        capacity=3, max_capacity=3, authorized_recovery_once=True, pipeline_runner=pipeline,
        live_status=lambda: {"credentials": {"status": "ok"}, "oa": {
            "execution_identity": "verified", "by_workflow": {"travel_expense": pending[0]}}},
        reconcile_status=lambda: _reconciliation_snapshot(pending[0]),
    )
    assert calls == [(2, 3, True)]
    assert result["completed"] == 2
    assert result["error_counts"] == {"codex_review_retry_pending": 1}
    assert result["stop_reason"] == "review_retry_pending"


def test_elastic_health_window_does_not_cap_claims() -> None:
    pending = iter([36, 0, 0])
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

    assert calls == [(36, 6)]
    assert result["elastic"] is True
    assert result["recommended_capacity"] == 6
    assert result["last_capacity_decision"]["reason"] == (
        "healthy_window_accumulating"
    )
    assert result["status"] == "completed"
    assert [health["status"] for health in result["wave_runtime_health"]] == [
        "healthy"
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
        result = finance_preaudit_concurrency_probe.run_rolling_drain(capacity=3)
        assert result["status"] == "blocked" and result["reason"] == "previous_run_ownership_unknown"


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
    assert (
        finance_preaudit_concurrency_probe.runtime_control.stop_requested()
        is False
    )
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


def test_rolling_recovers_reaped_cycle_stop_after_live_gates_pass() -> None:
    finance_preaudit_concurrency_probe.runtime_control.request_cycle_stop()

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "unsupported": 0,
                "by_workflow": {},
            },
        },
        reconcile_status=lambda: _reconciliation_snapshot(0),
        pipeline_runner=lambda *_args: pytest.fail("pipeline must remain idle"),
    )

    assert result["status"] == "completed"
    assert result["recovered_cycle_stop"] is True
    assert (
        finance_preaudit_concurrency_probe.runtime_control.stop_requested()
        is False
    )


def test_rolling_preserves_cycle_stop_when_live_gates_are_untrusted() -> None:
    finance_preaudit_concurrency_probe.runtime_control.request_cycle_stop()

    with pytest.raises(
        ValueError,
        match="production_cycle_stop_recovery_blocked",
    ):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=lambda: (_ for _ in ()).throw(
                RuntimeError("pending_queue_fetch_failed")
            ),
            reconcile_status=lambda: _reconciliation_snapshot(0),
            pipeline_runner=lambda *_args: pytest.fail("pipeline must remain idle"),
        )

    assert (
        finance_preaudit_concurrency_probe.runtime_control.stop_requested()
        is True
    )


def test_rolling_scope_is_explicitly_limited_to_finance_worker_routes() -> None:
    assert finance_preaudit_concurrency_probe.ROLLING_WORKFLOW_KEYS == {
        "travel_expense",
        "business_entertainment",
        "business_support_fee",
        "small_marketing_fee",
        "tpm_small_marketing_fee",
        "general_expense",
    }
    assert (
        finance_preaudit_concurrency_probe.ROLLING_WORKFLOW_KEYS
        <= finance_preaudit_concurrency_probe.codex_review_worker._FINANCE_RULES.keys()
    )


def test_rolling_controller_holds_unique_runtime_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    pending = iter((1, 0, 0))
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
        result = finance_preaudit_concurrency_probe.run_rolling_drain(capacity=3)
        assert result["status"] == "blocked" and result["reason"] == "previous_run_ownership_unknown"


def test_rolling_failure_retains_internal_host_critical_decision() -> None:
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
                        "status": "healthy",
                        "memory_free_percent": 40,
                        "load_per_cpu": 0.5,
                        "active_threads": 1,
                        "cpu_count": 10,
                    },
                },
            ),
            max_task_elapsed_seconds=8,
            recommended_capacity=6,
            capacity_decisions=(
                {
                    "action": "stop",
                    "next_capacity": 6,
                    "healthy_windows": 0,
                    "reason": "host_critical",
                },
            ),
        ),
    )

    assert result["status"] == "failed"
    assert result["stop_reason"] == "host_critical"
    assert result["last_capacity_decision"]["reason"] == "host_critical"
    assert result["wave_runtime_health"] == [
        {
            "status": "healthy",
            "sample_count": 1,
            "minimum_memory_free_percent": 40,
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


def test_rolling_wave_preserves_first_non_cancellation_error() -> None:
    metrics = finance_preaudit_concurrency_probe._rolling_wave_metrics(
        process_pool.PoolResult(
            requested=3,
            started=3,
            completed=1,
            failed=1,
            timed_out=0,
            stopped=1,
            peak_active=3,
            elapsed_seconds=1.0,
            outcomes=(
                process_pool.TaskOutcome(
                    index=0,
                    status="stopped",
                    value=None,
                    error_code="stop_requested",
                ),
                process_pool.TaskOutcome(
                    index=1,
                    status="completed",
                    value={
                        "status": "failed",
                        "error_code": "writer_result_unknown",
                        "token_usage": {"status": "unknown"},
                    },
                ),
                process_pool.TaskOutcome(
                    index=2,
                    status="failed",
                    value=None,
                    error_code="codex_decision_invalid",
                ),
            ),
            primary_error_code="codex_decision_invalid",
        )
    )

    assert metrics["primary_error_code"] == "codex_decision_invalid"


def test_rolling_drain_excludes_trusted_isolated_items() -> None:
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


def test_rolling_drain_accounts_for_new_item_local_quarantine() -> None:
    live_counts = iter((2, 1, 1))
    reconciliations = iter(
        (
            _reconciliation_snapshot(2, 0),
            _reconciliation_snapshot(1, 1),
            _reconciliation_snapshot(1, 1),
        )
    )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": next(live_counts)},
            },
        },
        reconcile_status=lambda: next(reconciliations),
        pipeline_runner=lambda count, capacity: process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=1.0,
            outcomes=(
                process_pool.TaskOutcome(
                    index=0,
                    status="completed",
                    value={
                        "status": "quarantined",
                        "business_state": "not_available",
                        "write_state": "not_requested",
                        "error_code": "material_collection_failed",
                        "token_usage": {"status": "unknown"},
                    },
                ),
                process_pool.TaskOutcome(
                    index=1,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "pass",
                        "write_state": "submitted",
                        "token_usage": {"status": "unknown"},
                    },
                ),
            ),
            processes_reaped=True,
        ),
    )

    assert result["status"] == "completed_with_quarantine"
    assert result["stop_reason"] is None
    assert result["completed"] == 2
    assert result["quarantined_pending"] == 1
    assert result["error_counts"] == {"material_collection_failed": 1}


def test_rolling_drain_requires_consecutive_empty_snapshots_and_accepts_arrival(
) -> None:
    pending = iter((1, 0, 1, 0, 0))
    current = [0]
    pipeline_calls: list[int] = []

    def live() -> dict:
        current[0] = next(pending)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": current[0]},
            },
        }

    def pipeline(count: int, capacity: int) -> process_pool.PoolResult[dict]:
        pipeline_calls.append(count)
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=0.1,
            outcomes=tuple(
                process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "pass",
                        "write_state": "submitted",
                        "token_usage": {"status": "unknown"},
                    },
                )
                for index in range(count)
            ),
            processes_reaped=True,
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live,
        reconcile_status=lambda: _reconciliation_snapshot(current[0]),
        pipeline_runner=pipeline,
    )

    assert pipeline_calls == [1, 1]
    assert result["status"] == "completed"
    assert result["empty_snapshots"] == 2
    assert result["discovery_refreshes"] == 5


@pytest.mark.parametrize(
    ("late_pending", "unauthorized", "post_boundary"),
    ((1, 1, 0), (0, 0, 1)),
)
def test_rolling_drain_uses_continuous_dispatcher_completion_boundary(
    late_pending: int,
    unauthorized: int,
    post_boundary: int,
) -> None:
    counts = iter((1, 1))
    current = [0]
    pipeline_calls = 0

    def live() -> dict:
        current[0] = next(counts)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": current[0]},
            },
        }

    def pipeline(count: int, capacity: int) -> process_pool.PoolResult[dict]:
        nonlocal pipeline_calls
        pipeline_calls += 1
        return process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=0.1,
            outcomes=(
                process_pool.TaskOutcome(
                    index=0,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "pass",
                        "write_state": "submitted",
                        "token_usage": {"status": "unknown"},
                    },
                ),
            ),
            late_visible_pending_count=late_pending,
            discovery_exhausted=True,
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live,
        reconcile_status=lambda: _reconciliation_snapshot(current[0]),
        pipeline_runner=pipeline,
    )

    assert pipeline_calls == 1
    assert result["empty_snapshots"] == 2
    assert result["unauthorized_pending"] == unauthorized
    assert result["post_boundary_actionable"] == post_boundary
    assert result["final_pending"] == 1


def test_rolling_drain_stops_at_admission_cutoff_boundary() -> None:
    counts = iter((1, 2))
    current = [0]

    def live() -> dict:
        current[0] = next(counts)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": current[0]},
            },
        }

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live,
        reconcile_status=lambda: _reconciliation_snapshot(current[0]),
        pipeline_runner=lambda count, capacity: process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=0.1,
            outcomes=(
                process_pool.TaskOutcome(
                    index=0,
                    status="completed",
                    value={
                        "status": "completed",
                        "business_state": "pass",
                        "write_state": "submitted",
                        "token_usage": {"status": "unknown"},
                    },
                ),
            ),
            admission_closed=True,
        ),
    )

    assert result["admission_closed"] is True
    assert result["unauthorized_pending"] == 2
    assert result["final_pending"] == 2


def test_rolling_drain_counts_unsupported_items_without_claiming() -> None:
    pipeline_calls: list[int] = []

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {},
                "unsupported": 2,
            },
        },
        reconcile_status=lambda: _reconciliation_snapshot(0),
        pipeline_runner=lambda count, _capacity: pipeline_calls.append(count),
    )

    assert pipeline_calls == []
    assert result["unauthorized_pending"] == 2
    assert result["completed"] == 0


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


@pytest.mark.parametrize(
    "live",
    (
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
    ),
)
def test_rolling_controller_rejects_untrusted_live_state_before_reconciliation(
    live: dict,
    monkeypatch,
) -> None:
    reconciliations: list[bool] = []
    pipelines: list[tuple[int, int]] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    try:
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=lambda: live,
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
    calls: list[tuple[int, bool, tuple[Path, ...], frozenset[str]]] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda *, capacity, adaptive, allowed_workflows, recovery_action_files=(): calls.append(
            (
                capacity,
                adaptive,
                tuple(recovery_action_files),
                allowed_workflows,
            )
        )
        or {"status": "completed"},
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 0
    assert calls == [
        (
            3,
            False,
            (),
            finance_preaudit_concurrency_probe.ROLLING_WORKFLOW_KEYS,
        )
    ]
    capsys.readouterr()

    recovery = Path("/safe/attempt-1/action.json")
    assert finance_preaudit_concurrency_probe.main(
        [
            "--capacity",
            "3",
            "--confirm-write",
            "YES",
            "--recover-action-file",
            str(recovery),
        ]
    ) == 0
    assert calls[-1] == (
        3,
        False,
        (recovery,),
        finance_preaudit_concurrency_probe.ROLLING_WORKFLOW_KEYS,
    )
    capsys.readouterr()

    assert finance_preaudit_concurrency_probe.main(
        [
            "--capacity",
            "3",
            "--confirm-write",
            "YES",
            "--workflow",
            "general_expense",
        ]
    ) == 0
    assert calls[-1] == (3, False, (), frozenset({"general_expense"}))
    capsys.readouterr()

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES", "--drain"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "production_legacy_scheduler_removed"
    assert len(calls) == 3


def test_production_cli_defaults_to_two_fixed_slots(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **kwargs: calls.append(kwargs) or {"status": "completed"},
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--confirm-write", "YES"]
    ) == 0
    assert calls[0]["capacity"] == 2
    assert calls[0]["adaptive"] is False
    capsys.readouterr()


def test_production_cli_passes_three_fixed_slots(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe, "run_rolling_drain",
        lambda **kwargs: calls.append(kwargs) or {"status": "completed"},
    )
    assert finance_preaudit_concurrency_probe.main([
        "--rolling", "--capacity", "3", "--max-capacity", "3", "--confirm-write", "YES",
    ]) == 0
    assert calls[0]["capacity"] == 3
    assert calls[0]["max_capacity"] == 3
    assert calls[0]["adaptive"] is False
    capsys.readouterr()


@pytest.mark.parametrize("capacity,maximum", [(3, 1), (3, 5), (6, 4), (3, 14)])
def test_production_cli_rejects_invalid_ceiling_before_controller(
    monkeypatch, capsys, capacity, maximum,
) -> None:
    calls = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe, "run_rolling_drain",
        lambda **kwargs: calls.append(kwargs) or {"status": "completed"},
    )
    assert finance_preaudit_concurrency_probe.main([
        "--capacity", str(capacity), "--max-capacity", str(maximum), "--confirm-write", "YES",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "production_rolling_capacity_invalid"
    assert calls == []


def test_read_only_cli_rejects_production_ceiling_before_model_probe(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe, "run_adaptive_read_only_probe",
        lambda **kwargs: calls.append(kwargs) or {"status": "completed"},
    )
    assert finance_preaudit_concurrency_probe.main([
        "--read-only", "--capacity", "3", "--max-capacity", "6",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "read_only_probe_scheduler_invalid"
    assert calls == []


def test_rolling_rejects_invalid_ceiling_before_acquiring_controller_lease(tmp_path, monkeypatch) -> None:
    lock_file = tmp_path / "controller.lock"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE", str(lock_file))
    calls = []
    with finance_preaudit_concurrency_probe.runtime_control.global_controller_lease():
        with pytest.raises(ValueError, match="^production_rolling_capacity_invalid$"):
            finance_preaudit_concurrency_probe.run_rolling_drain(
                capacity=3, max_capacity=2, adaptive=True,
                live_status=lambda: calls.append("live_status") or {},
            )
    assert calls == []
    assert not lock_file.exists()


def test_production_cli_alerts_on_session_failure_without_masking_it(
    monkeypatch,
    capsys,
) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("oa_session_expired: redacted")
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "alert_from_environment",
        lambda code: alerts.append(code)
        or {
            "schema_version": 1,
            "kind": "oa_session_alert_receipt",
            "error_code": code,
            "delivery_state": "unknown",
        },
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "oa_session_expired"
    assert payload["notification"]["delivery_state"] == "unknown"
    assert alerts == ["oa_session_expired"]


def test_production_cli_alerts_on_pipeline_session_failure_without_clearing(
    monkeypatch,
    capsys,
) -> None:
    alerts: list[str] = []
    clears: list[bool] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **_kwargs: {
            "status": "failed",
            "error_counts": {"oa_session_expired": 1},
        },
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "alert_from_environment",
        lambda code: alerts.append(code)
        or {
            "schema_version": 1,
            "kind": "oa_session_alert_receipt",
            "error_code": code,
            "delivery_state": "sent",
        },
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "clear_active_alert",
        lambda: clears.append(True),
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_counts"] == {"oa_session_expired": 1}
    assert payload["notification"]["delivery_state"] == "sent"
    assert alerts == ["oa_session_expired"]
    assert clears == []


@pytest.mark.parametrize("status,error_counts,expected_alert", [
    ("completed_with_quarantine", {}, None),
    ("completed_with_quarantine", {"codex_review_failed": 0}, None),
    ("completed_with_quarantine", {"codex_review_failed": 1}, "codex_review_failed"),
    ("completed_with_quarantine", {"unmapped_failure": 1}, "controller_failed"),
    ("completed", {"writer_result_unknown": 1}, "writer_result_unknown"),
    ("failed", {}, "controller_failed"),
    ("unrecognized", {}, "controller_failed"),
])
def test_production_cli_alerts_only_on_current_failures(
    monkeypatch, capsys, status, error_counts, expected_alert,
) -> None:
    probe = finance_preaudit_concurrency_probe
    alerts = []
    clears = []
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kw: {
        "status": status, "error_counts": error_counts, "quarantined_pending": 1,
    })
    monkeypatch.setattr(probe.session_alert, "alert_from_environment", lambda code: (
        alerts.append(code) or {"error_code": code, "delivery_state": "sent"}
    ))
    monkeypatch.setattr(probe.session_alert, "clear_active_alert", lambda **kw: clears.append(kw))

    exit_code = probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == (1 if status == "failed" else 0)
    assert payload["status"] == status
    assert payload["quarantined_pending"] == 1
    assert alerts == ([] if expected_alert is None else [expected_alert])
    assert clears == []
    if expected_alert is None:
        assert "notification" not in payload
        assert "notification_reset" not in payload
    else:
        assert payload["notification"]["error_code"] == expected_alert


def test_production_cli_finds_session_failure_in_exception_chain(
    monkeypatch,
    capsys,
) -> None:
    alerts: list[str] = []

    def fail(**_kwargs) -> dict:
        try:
            raise RuntimeError("rpa_cdp_timeout")
        except RuntimeError as cause:
            raise ValueError("production_cycle_stop_recovery_blocked") from cause

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        fail,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "alert_from_environment",
        lambda code: alerts.append(code)
        or {
            "schema_version": 1,
            "kind": "oa_session_alert_receipt",
            "error_code": code,
            "delivery_state": "sent",
        },
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "rpa_cdp_timeout"
    assert alerts == ["rpa_cdp_timeout"]


def test_production_cli_preserves_primary_error_when_reaping_fails(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **_kwargs: (_ for _ in ()).throw(
            finance_preaudit_concurrency_probe.ControllerChildrenActiveError(
                "codex_decision_invalid"
            )
        ),
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "production_controller_children_active"
    assert payload["primary_error_code"] == "codex_decision_invalid"


def test_production_cli_preserves_known_error_prefix_with_detail(
    monkeypatch,
    capsys,
) -> None:
    alerts: list[str] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("production_reconciliation_blocked: stale handoff")
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "alert_from_environment",
        lambda code: alerts.append(code) or {"error_code": code},
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "production_reconciliation_blocked"
    assert payload["notification"]["error_code"] == "production_reconciliation_blocked"
    assert alerts == ["production_reconciliation_blocked"]


def test_production_cli_does_not_mask_success_when_alert_reset_is_unknown(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "run_rolling_drain",
        lambda **_kwargs: {"status": "completed"},
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.session_alert,
        "clear_active_alert",
        lambda **kwargs: {
            "schema_version": 1,
            "kind": "oa_session_alert_receipt",
            "status": "unknown",
        },
    )

    assert finance_preaudit_concurrency_probe.main(
        ["--capacity", "3", "--confirm-write", "YES"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["notification_reset"]["status"] == "unknown"


def test_supported_pending_can_be_narrowed_to_one_workflow() -> None:
    live = {
        "credentials": {"status": "ok"},
        "oa": {
            "execution_identity": "verified",
            "by_workflow": {
                "travel_expense": 9,
                "general_expense": 8,
            },
        },
    }

    assert finance_preaudit_concurrency_probe._supported_pending(
        live,
        frozenset({"general_expense"}),
    ) == 8


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
    assert result.processes_reaped is True
    assert result.outcomes[0].error_code == "worker_task_timeout"
    assert result.runtime_samples
    assert stop_requests == [True]


def test_dispatcher_uses_final_process_state_after_transient_reaping_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda: None,
    )
    original_terminate = finance_preaudit_concurrency_probe._terminate_executor

    def terminate_but_report_failure(executor, **kwargs) -> bool:
        original_terminate(executor, **kwargs)
        return False

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_terminate_executor",
        terminate_but_report_failure,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="9" * 64,
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

    assert result.processes_reaped is True


def test_dispatcher_times_out_stuck_recovery_before_review(
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
        strict_global_fifo=True,
        recovery_action_files=(tmp_path / "attempt-1" / "action.json",),
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
    preparation_future = Future()
    clock = [0.0]
    wall_deadline = time.monotonic() + 5

    class PendingPreparationExecutor:
        _processes: dict = {}

        def submit(self, _action, *_args) -> Future:
            assert preparation_future.set_running_or_notify_cancel()
            return preparation_future

        def shutdown(self, **_kwargs) -> None:
            return None

    def monotonic() -> float:
        assert time.monotonic() < wall_deadline, "dispatcher did not finish"
        return clock[0]

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        PendingPreparationExecutor,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "time",
        SimpleNamespace(monotonic=monotonic, sleep=time.sleep),
    )
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

    def complete_review(action, _reviewed):
        assert preparation_future.running()
        completions.append(action)
        # Expire preparation only after the independently ready review closes.
        clock[0] = 1.0
        return {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        }

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
        complete_review=complete_review,
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


def test_dispatcher_skips_trusted_zero_write_item_and_continues_fifo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    preparation_calls = 0
    reviews: list[Path] = []
    writes: list[Path] = []
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_file: Path | None = None

    def prepare(_selection: object) -> dict:
        nonlocal action_file, preparation_calls
        preparation_calls += 1
        if preparation_calls == 1:
            return {
                "action": "quarantined",
                "error_code": "material_collection_failed",
                "write_state": "not_requested",
            }
        action_file = _open_action(
            flow_root,
            case_key="after-quarantine",
            created_at="2026-08-25T08:01:00+08:00",
        )
        return {
            "action": "review_required",
            "action_file": str(action_file),
        }

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
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        1,
        project_root=tmp_path,
        prepare_action=prepare,
        review_worker=lambda task: reviews.append(Path(task["action_file"]))
        or {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "decision": {"overall": "needs_human"},
        },
        complete_review=lambda action, _reviewed: writes.append(action)
        or {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 2
    assert result.failed == 0
    assert result.stopped == 0
    assert result.outcomes[0].value["status"] == "quarantined"
    assert result.outcomes[0].value["write_state"] == "not_requested"
    assert result.outcomes[0].value["error_code"] == "material_collection_failed"
    assert action_file is not None
    assert reviews == [action_file]
    assert writes == [action_file]
    assert stop_requests == []


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


def test_dispatcher_preserves_prepare_failure_when_stop_races_review_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = threading.Event()
    completions: list[Path] = []
    heartbeats: list[bool] = []
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="prepare-race",
        created_at="2026-08-25T01:00:00+00:00",
    )

    def prepare(_workflow: str) -> dict:
        stopped.set()
        time.sleep(0.4)
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }

    def review(_task: dict) -> dict:
        assert stopped.wait(timeout=1)
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

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        stopped.is_set,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        stopped.set,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=prepare,
        review_worker=review,
        complete_review=lambda action, _reviewed: completions.append(action) or {
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "stop_requested",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: heartbeats.append(True)
        or {"status": "healthy"},
    )

    assert completions == []
    assert len(heartbeats) >= 2
    assert any(
        outcome.value
        and outcome.value.get("error_code")
        == "production_probe_prepare_failed"
        for outcome in result.outcomes
    )
    harvested = result.outcomes[0]
    assert harvested.status == "stopped"
    assert harvested.value["process_attempts"] == 1


def test_dispatcher_treats_assigned_prepare_result_as_review_backpressure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = False
    duplicate_returned = threading.Event()
    preparation_calls = 0
    completions: list[Path] = []
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    first_action = _open_action(
        flow_root,
        case_key="assigned-first",
        created_at="2026-08-25T01:02:00+00:00",
    )

    def prepare(_workflow: str) -> dict:
        nonlocal preparation_calls
        preparation_calls += 1
        if preparation_calls == 1:
            duplicate_returned.set()
            action_file = first_action
        else:
            action_file = _open_action(
                flow_root,
                case_key="assigned-second",
                created_at="2026-08-25T01:03:00+00:00",
            )
        return {
            "action": "review_required",
            "action_file": str(action_file),
        }

    def review(task: dict) -> dict:
        if Path(task["action_file"]) == first_action:
            assert duplicate_returned.wait(timeout=1)
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

    def request_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
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
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=prepare,
        review_worker=review,
        complete_review=lambda action, _reviewed: completions.append(action)
        or {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert stopped is False
    assert preparation_calls == 2
    assert len(completions) == 2
    assert completions[0] == first_action
    assert completions[1] != first_action
    assert result.completed == 2
    assert result.failed == 0
    assert result.stopped == 0


def test_dispatcher_bounds_repeated_assigned_result_without_active_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = False
    duplicate_returned = threading.Event()
    preparation_calls = 0
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_file = _open_action(
        flow_root,
        case_key="assigned-repeated",
        created_at="2026-08-25T01:04:00+00:00",
    )

    def prepare(_workflow: str) -> dict:
        nonlocal preparation_calls
        preparation_calls += 1
        duplicate_returned.set()
        if preparation_calls >= 4:
            return {
                "action": "failed",
                "error_code": "unbounded_duplicate_test_guard",
            }
        return {
            "action": "review_required",
            "action_file": str(action_file),
        }

    def review(_task: dict) -> dict:
        assert duplicate_returned.wait(timeout=1)
        time.sleep(0.05)
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

    def request_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
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
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=prepare,
        review_worker=review,
        complete_review=lambda _action, _reviewed: {
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert preparation_calls == 3
    assert stopped is True
    assert result.outcomes[1].value["error_code"] == (
        "production_probe_duplicate_action"
    )


def test_dispatcher_harvests_review_done_before_prepare_failure_is_processed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stopped = threading.Event()
    completions: list[Path] = []
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    _open_action(
        flow_root,
        case_key="prepare-first",
        created_at="2026-08-25T01:01:00+00:00",
    )

    def prepare(_workflow: str) -> dict:
        stopped.set()
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }

    def review(_task: dict) -> dict:
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

    real_wait = finance_preaudit_concurrency_probe.wait
    hidden_once = False

    def hide_completed_once(futures, **kwargs):
        nonlocal hidden_once
        if not hidden_once:
            hidden_once = True
            deadline = time.monotonic() + 1
            while not all(future.done() for future in futures):
                assert time.monotonic() < deadline
                time.sleep(0.001)
            return set(), set(futures)
        return real_wait(futures, **kwargs)

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "wait",
        hide_completed_once,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        stopped.is_set,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        stopped.set,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        prepare_action=prepare,
        review_worker=review,
        complete_review=lambda action, _reviewed: completions.append(action)
        or {"status": "completed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert completions == []
    assert result.outcomes[0].status == "stopped"
    assert result.outcomes[0].value["process_attempts"] == 1
    assert result.outcomes[1].value["error_code"] == (
        "production_probe_prepare_failed"
    )


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

    real_record = finance_preaudit_concurrency_probe.prepare_trace.record_timeout
    real_reaped = finance_preaudit_concurrency_probe._processes_reaped
    reaped_checks = []

    def checked_reaped(*args, **kwargs):
        result = real_reaped(*args, **kwargs)
        reaped_checks.append(result)
        return result

    def record_after_cleanup(*args, **kwargs):
        assert stop_requests == [True]
        assert reaped_checks and all(reaped_checks)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "_processes_reaped", checked_reaped)
    monkeypatch.setattr(finance_preaudit_concurrency_probe.prepare_trace,
                        "record_timeout", record_after_cleanup)

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
    assert result.processes_reaped is True
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
    receipts = list((tmp_path / "runtime" / "control").glob("prepare-timeout-*.json"))
    assert len(receipts) == 1
    diagnostic = json.loads(receipts[0].read_text())
    assert diagnostic["error_code"] == "production_probe_prepare_timeout"
    assert diagnostic["task_index"] == 0
    assert diagnostic["sample_status"] == "recorded"
    assert diagnostic["sample"]["stage"] == "selection_contract"
    assert diagnostic["sample_age_seconds"] >= 0
    assert diagnostic["sample"]["stack"]


def test_dispatcher_defers_prewrite_invalid_decision_without_extra_usage(
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
            return _rejected_decision_result(reviewed)
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
        record_failure_round=lambda *_args: {"production_failure_round": 1},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert result.completed == 1
    assert review_calls == [1]
    assert completion_attempts == [1]
    assert value["status"] == "retry_pending"
    assert value["process_attempts"] == 1
    assert value["process_retries"] == 0
    assert value["token_usage"] == {
        "status": "recorded",
        "input_tokens": 10,
        "cached_input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 12,
    }
    assert value["codex_events"] == {
        "status": "recorded",
        "attempts": 1,
        "recorded_attempts": 1,
        "unknown_attempts": 0,
        "counts": {"turn.completed": 1},
    }
    assert stop_requests == []


def test_isolated_timeout_does_not_stop_next_item(tmp_path: Path, monkeypatch) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker

    stopped, quarantined, submitted = [], [], []
    monkeypatch.setattr(finance_preaudit_concurrency_probe.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(finance_preaudit_concurrency_probe.runtime_control, "request_stop", lambda: stopped.append(True))
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    first = _open_action(flow_root, case_key="8" * 64, created_at="2026-08-24T12:00:00+00:00")
    second = _open_action(flow_root, case_key="9" * 64, created_at="2026-08-24T12:00:01+00:00")
    reviews = []
    value = {
        **worker._base_result(attempts=1, usages=[{"status": "unknown"}]),
        "codex_events": worker._unknown_events(1), "status": "failed",
        "business_state": "not_available", "write_state": "not_requested",
        "error_code": "codex_process_timeout",
    }

    def quarantine(path, reason):
        quarantined.append(reason)
        return {"action": "needs_admin", "execution_state": "needs_admin",
                "business_state": "not_available", "write_state": "not_requested",
                "reason": "codex_review_failed"}

    def review(task):
        action = Path(task["action_file"])
        reviews.append(action)
        return value if action == first else {"status": "reviewed", "decision": {"overall": "pass"}}

    def complete(action, reviewed):
        submitted.append(action)
        return {**reviewed, "status": "completed", "write_state": "submitted", "error_code": None}

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2, 1, project_root=tmp_path, renew_actions=lambda paths: paths,
        review_worker=review, complete_review=complete,
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )
    assert quarantined == ["codex_process_timeout"]
    assert stopped == [] and submitted == [second]
    assert reviews == [first, second]
    assert result.outcomes[0].value["execution_state"] == "needs_admin"
    assert result.outcomes[0].value["status"] == "quarantined"
    assert result.outcomes[0].value["write_state"] == "not_requested"
    assert result.outcomes[1].value["status"] == "completed"


def test_rolling_timeout_keeps_token_usage_unknown() -> None:
    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6, adaptive=True,
        live_status=lambda: {"credentials": {"status": "ok"}, "oa": {
            "execution_identity": "verified", "by_workflow": {"travel_expense": 1}}},
        reconcile_status=lambda: _reconciliation_snapshot(1),
        pipeline_runner=lambda count, capacity: process_pool.PoolResult(
            requested=1, started=1, completed=1, failed=0, timed_out=0, stopped=0,
            peak_active=1, elapsed_seconds=1,
            outcomes=(process_pool.TaskOutcome(index=0, status="completed", value={
                "status": "failed", "error_code": "codex_process_timeout",
                "business_state": "not_available", "write_state": "not_requested",
                "process_attempts": 1, "process_retries": 0,
                "token_usage": {"status": "unknown"},
            }),),
        ),
    )
    assert result["failed"] == 1
    assert result["token_usage"]["unknown_workers"] == 1
    assert result["token_usage"]["status"] == "unknown"


def test_dispatcher_isolates_invalid_decision_without_failure_recorder(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    review_calls: list[bool] = []
    completion_calls: list[bool] = []
    quarantine_calls: list[Path] = []
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
        return _rejected_decision_result(reviewed)

    def quarantine(action_file: Path, reason: str) -> dict:
        assert reason == "codex_decision_invalid"
        quarantine_calls.append(action_file)
        return {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "not_requested",
            "reason": "codex_decision_invalid",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert review_calls == [True]
    assert completion_calls == [True]
    assert len(quarantine_calls) == 1
    assert value["status"] == "quarantined"
    assert value["write_state"] == "not_requested"
    assert value["error_code"] == "codex_decision_invalid"
    assert value["process_attempts"] == 1
    assert value["process_retries"] == 0
    assert stop_requests == []


def test_dispatcher_quarantines_malformed_review_without_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker

    stop_requests: list[bool] = []
    quarantine_calls: list[str] = []
    submitted: list[bool] = []
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
    value = {
        **worker._base_result(
            attempts=1,
            usages=[{"status": "unknown"}],
        ),
        "codex_events": worker._unknown_events(1),
        "status": "failed",
        "business_state": "not_available",
        "write_state": "not_requested",
        "error_code": "codex_decision_invalid",
    }

    def quarantine(_action_file: Path, reason: str) -> dict:
        quarantine_calls.append(reason)
        return {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "not_requested",
            "reason": "codex_decision_invalid",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=lambda _task: value,
        complete_review=lambda *_args: submitted.append(True),
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    assert quarantine_calls == ["codex_decision_invalid"]
    assert submitted == []
    assert result.outcomes[0].value["status"] == "quarantined"
    assert result.outcomes[0].value["write_state"] == "not_requested"
    assert stop_requests == []


def test_dispatcher_never_calls_second_review_after_writer_rejection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker

    stop_requests: list[bool] = []
    quarantine_calls: list[str] = []
    completion_calls: list[bool] = []
    review_calls: list[bool] = []
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
    reviewed = {
        **worker._base_result(attempts=1, usages=[{"status": "unknown"}]),
        "codex_events": worker._unknown_events(1),
        "status": "reviewed",
        "business_state": "not_available",
        "write_state": "not_requested",
        "error_code": None,
        "agent_wall_seconds": 1,
        "decision": {"overall": "needs_human"},
    }
    malformed = {
        **worker._base_result(attempts=1, usages=[{"status": "unknown"}]),
        "codex_events": worker._unknown_events(1),
        "status": "failed",
        "business_state": "not_available",
        "write_state": "not_requested",
        "error_code": "codex_decision_invalid",
    }

    def review(_task: dict) -> dict:
        review_calls.append(True)
        return reviewed if len(review_calls) == 1 else malformed

    def complete(_action_file: Path, value: dict) -> dict:
        completion_calls.append(True)
        return _rejected_decision_result(value)

    def quarantine(_action_file: Path, reason: str) -> dict:
        quarantine_calls.append(reason)
        return {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "not_requested",
            "reason": "codex_decision_invalid",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert review_calls == [True]
    assert completion_calls == [True]
    assert quarantine_calls == ["codex_decision_invalid"], (
        value,
        result.primary_error_code,
        stop_requests,
    )
    assert value["status"] == "quarantined"
    assert value["process_attempts"] == 1
    assert value["process_retries"] == 0
    assert value["write_state"] == "not_requested"
    assert stop_requests == []


def test_dispatcher_continues_fifo_after_invalid_decision_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    submitted: list[str] = []
    quarantined: list[str] = []
    blocked_key: list[str] = []
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
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    project_root = tmp_path.resolve()
    flow_root = project_root / "runtime" / "finance_preaudit_flows"
    first_action = _open_action(
        flow_root,
        case_key="a" * 64,
        created_at="2026-08-24T12:00:00+00:00",
    )
    second_action = _open_action(
        flow_root,
        case_key="b" * 64,
        created_at="2026-08-24T12:00:01+00:00",
    )
    first_key = first_action.parent.parent.name
    second_key = second_action.parent.parent.name
    prepared_actions = iter((first_action, second_action))

    def prepare(_selection: dict) -> dict:
        return {
            "action": "review_required",
            "action_file": str(next(prepared_actions)),
        }

    def review(_task: dict) -> dict:
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

    def complete(action_file: Path, reviewed: dict) -> dict:
        case_key = action_file.parent.parent.name
        if not blocked_key:
            blocked_key.append(case_key)
        if case_key == blocked_key[0]:
            return _rejected_decision_result(reviewed)
        submitted.append(case_key)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    def quarantine(action_file: Path, reason: str) -> dict:
        assert reason == "codex_decision_invalid"
        quarantined.append(action_file.parent.parent.name)
        return {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "not_requested",
            "reason": "codex_decision_invalid",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=project_root,
        prepare_action=prepare,
        review_worker=review,
        complete_review=complete,
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
        strict_global_fifo=True,
    )

    expected_submitted = second_key if blocked_key == [first_key] else first_key
    assert quarantined == blocked_key
    assert submitted == [expected_submitted], (
        submitted,
        quarantined,
        [(outcome.status, outcome.value) for outcome in result.outcomes],
        stop_requests,
        result.primary_error_code,
    )
    assert result.outcomes[0].value["status"] == "quarantined"
    assert result.outcomes[1].value["status"] == "completed"
    assert stop_requests == []


def test_dispatcher_continues_fifo_after_bounded_prewrite_review_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_requests: list[bool] = []
    submitted: list[str] = []
    quarantined: list[tuple[str, str]] = []
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
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    project_root = tmp_path.resolve()
    flow_root = project_root / "runtime" / "finance_preaudit_flows"
    first_action = _open_action(
        flow_root,
        case_key="c" * 64,
        created_at="2026-08-24T12:00:00+00:00",
    )
    second_action = _open_action(
        flow_root,
        case_key="d" * 64,
        created_at="2026-08-24T12:00:01+00:00",
    )
    prepared_actions = iter((first_action, second_action))

    def prepare(_selection: dict) -> dict:
        return {
            "action": "review_required",
            "action_file": str(next(prepared_actions)),
        }

    def review(task: dict) -> dict:
        action_file = Path(task["action_file"])
        if action_file == first_action:
            return {
                "schema_version": 1,
                "kind": "preaudit_review_worker_result",
                "model": codex_review_worker.MODEL,
                "reasoning_effort": codex_review_worker.REASONING_EFFORT,
                "model_provider": "openai",
                "requested_service_tier": "default",
                "status": "failed",
                "business_state": "not_available",
                "write_state": "not_requested",
                "error_code": "codex_process_failed",
                "process_attempts": 2,
                "process_retries": 1,
                "token_usage": {"status": "unknown"},
                "codex_events": {
                    "status": "unknown",
                    "attempts": 2,
                    "recorded_attempts": 0,
                    "unknown_attempts": 2,
                    "counts": {},
                },
            }
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

    def complete(action_file: Path, reviewed: dict) -> dict:
        submitted.append(action_file.parent.parent.name)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    def quarantine(action_file: Path, reason: str = "") -> dict:
        quarantined.append((action_file.parent.parent.name, reason))
        return {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "not_requested",
            "reason": "codex_review_failed",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        2,
        2,
        project_root=project_root,
        prepare_action=prepare,
        review_worker=review,
        complete_review=complete,
        quarantine_review=quarantine,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
        strict_global_fifo=True,
    )

    assert quarantined == [
        (first_action.parent.parent.name, "codex_process_failed")
    ]
    assert submitted == [second_action.parent.parent.name]
    assert result.outcomes[0].value["status"] == "quarantined"
    assert result.outcomes[1].value["status"] == "completed"
    assert stop_requests == []


def test_dispatcher_stops_when_invalid_decision_quarantine_is_untrusted(
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
        case_key="a" * 64,
        created_at="2026-08-24T12:00:00+00:00",
    )

    def review(_task: dict) -> dict:
        return {
            "status": "reviewed",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "codex_events": codex_review_worker._unknown_events(1),
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        }

    def complete(_action: Path, reviewed: dict) -> dict:
        return _rejected_decision_result(reviewed)

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        quarantine_review=lambda _path, _reason: {
            "action": "needs_admin",
            "execution_state": "needs_admin",
            "business_state": "not_available",
            "write_state": "unknown",
            "reason": "codex_decision_invalid",
        },
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    value = result.outcomes[0].value
    assert value["status"] == "failed"
    assert value["error_code"] == "codex_review_quarantine_failed"
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


def test_dispatcher_refills_reviews_while_single_writer_is_slow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_files = [
        _open_action(
            flow_root,
            case_key=f"stream-{index}",
            created_at=f"2026-08-25T02:00:0{index}+00:00",
        )
        for index in range(3)
    ]
    writer_started = threading.Event()
    third_review_started = threading.Event()
    writer_saw_refill: list[bool] = []

    def review(task: dict) -> dict:
        action_file = Path(task["action_file"])
        if action_file == action_files[1]:
            assert writer_started.wait(timeout=1)
        if action_file == action_files[2]:
            third_review_started.set()
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

    def complete(_action: Path, reviewed: dict) -> dict:
        if not writer_started.is_set():
            writer_started.set()
            writer_saw_refill.append(third_review_started.wait(timeout=0.2))
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
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
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert writer_saw_refill == [True]
    assert result.completed == 3
    assert result.failed == 0


def test_write_wait_attribution_separates_writer_busy_and_fifo_head() -> None:
    blocked = finance_preaudit_concurrency_probe._write_wait_attribution(
        ready_at=10.0,
        write_started_at=20.0,
        writer_busy_intervals=((8.0, 12.0), (15.0, 18.0)),
        became_write_head_at=18.0,
    )
    head = finance_preaudit_concurrency_probe._write_wait_attribution(
        ready_at=10.0,
        write_started_at=20.0,
        writer_busy_intervals=((8.0, 12.0), (15.0, 18.0)),
        became_write_head_at=10.0,
    )

    assert blocked == {
        "status": "exact",
        "total_seconds": 10.0,
        "writer_busy_seconds": 5.0,
        "fifo_head_of_line_seconds": 3.0,
        "controller_scheduling_seconds": 2.0,
    }
    assert head == {
        "status": "exact",
        "total_seconds": 10.0,
        "writer_busy_seconds": 5.0,
        "fifo_head_of_line_seconds": 0.0,
        "controller_scheduling_seconds": 5.0,
    }


def test_pipeline_percentiles_keep_insufficient_samples_unknown() -> None:
    assert finance_preaudit_concurrency_probe._pipeline_percentiles(
        [1.0] * 4
    ) == {
        "samples": 4,
        "p50": "unknown",
        "p95": "unknown",
        "p99": "unknown",
    }
    assert finance_preaudit_concurrency_probe._pipeline_percentiles(
        [float(value) for value in range(1, 101)]
    ) == {
        "samples": 100,
        "p50": 50.0,
        "p95": 95.0,
        "p99": 99.0,
    }


def test_global_dispatcher_validates_reviewer_environment_before_prepare(
    monkeypatch,
) -> None:
    dispatched: list[bool] = []

    def invalid_environment() -> dict[str, str]:
        raise ValueError("codex_reviewer_environment_invalid")

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.codex_review_worker,
        "_reviewer_environment",
        invalid_environment,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_run_dispatched_tasks",
        lambda *_args, **_kwargs: dispatched.append(True),
    )

    with pytest.raises(ValueError, match="codex_reviewer_environment_invalid"):
        finance_preaudit_concurrency_probe._run_global_dispatched_tasks(1, 1)

    assert dispatched == []


@pytest.mark.parametrize("handoff", [False, True])
def test_dispatcher_single_writer_is_fifo_and_never_concurrent(
    tmp_path: Path,
    monkeypatch,
    handoff: bool,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_files = [
        _open_action(
            flow_root,
            case_key=f"fifo-{index}",
            created_at=f"2026-08-25T02:01:0{index}+00:00",
        )
        for index in range(3)
    ]
    release_reviews = threading.Event()
    writer_active = 0
    writer_peak = 0
    writer_order: list[Path] = []
    reviewed_order: list[Path] = []
    guard = threading.Lock()
    def review(task: dict) -> dict:
        action = Path(task["action_file"])
        index = action_files.index(action)
        reviewed_order.append(action)
        if index == 0:
            assert release_reviews.wait(timeout=1)
        elif index == 2:
            release_reviews.set()
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human", "kind": "preaudit_manual_handoff" if handoff and index == 0 else "preaudit_review_decision"},
        }

    def complete(action: Path, reviewed: dict) -> dict:
        nonlocal writer_active, writer_peak
        with guard:
            writer_active += 1
            writer_peak = max(writer_peak, writer_active)
            writer_order.append(action)
        time.sleep(0.01)
        with guard:
            writer_active -= 1
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        3,
        3,
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
    assert writer_peak == 1
    assert writer_order == action_files
    assert sorted(reviewed_order) == sorted(action_files)


def test_prepare_excludes_late_visible_item_before_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine_calls = 0
    stop_requests: list[str | None] = []

    def engine(**_kwargs: object) -> object:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("late-visible item must not open an attempt")

    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: stop_requests.append(reason),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": "late-request",
                "received_at": "2026-08-25T09:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        engine,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
            "committed_received_at": "2026-08-25T10:00:00+08:00",
            "committed_request_id": "committed-request",
        }
    )

    assert result["action"] == "late_visible"
    assert len(result["late_visible_keys"]) == 1
    assert engine_calls == 0
    assert stop_requests == []


def test_prepare_excludes_same_time_lower_request_id_before_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: [
            {
                "requestId": "a-request",
                "received_at": "2026-08-25T09:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ],
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("late-visible tie must not claim")
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
            "committed_received_at": "2026-08-25T09:00:00+08:00",
            "committed_request_id": "z-request",
        }
    )

    assert result["action"] == "late_visible"


def test_prepare_closes_admission_without_reading_or_claiming(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        lambda: (_ for _ in ()).throw(AssertionError("must not discover")),
    )

    assert finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
            "admission_cutoff": "2020-01-01T00:00:00+00:00",
        }
    ) == {"action": "admission_closed"}


def test_prepare_rechecks_admission_after_slow_live_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(finance_preaudit_concurrency_probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )

    def slow_pending() -> list[dict]:
        time.sleep(0.03)
        return [
            {
                "requestId": "after-cutoff",
                "received_at": "2026-08-25T09:00:00+08:00",
                "flow_key": "travel_expense",
                "support_status": "supported",
                "can_submit": True,
            }
        ]

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_pending_identities",
        slow_pending,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.cli,
        "_live_flow_engine",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("cutoff must stop before claim")
        ),
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_historical_write_authorization_failures",
        frozenset,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantine_count",
        lambda: 0,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_local_quarantined_identities",
        frozenset,
    )

    result = finance_preaudit_concurrency_probe._prepare_one(
        {
            "workflow_key": "*",
            "assigned_identities": [],
            "historical_unknown_identities": [],
            "recover_action_files": [],
            "admission_cutoff": (
                datetime.now(timezone.utc) + timedelta(milliseconds=10)
            ).isoformat(),
        }
    )

    assert result == {"action": "admission_closed"}


def test_continuous_dispatcher_admits_new_item_while_writer_is_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    first = _open_action(
        flow_root,
        case_key="continuous-first",
        created_at="2026-08-25T02:00:00+00:00",
        received_at="2026-08-25T10:00:00+08:00",
    )
    arrived = _open_action(
        flow_root,
        case_key="continuous-arrived",
        created_at="2026-08-25T02:01:00+00:00",
        received_at="2026-08-25T10:01:00+08:00",
    )
    writer_started = threading.Event()
    arrived_reviewed = threading.Event()
    prepare_calls = 0
    writes: list[Path] = []

    def prepare(_selection: object) -> dict:
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            return {"action": "review_required", "action_file": str(first)}
        if prepare_calls == 2:
            assert writer_started.wait(timeout=1)
            return {"action": "review_required", "action_file": str(arrived)}
        return {"action": "no_action"}

    def review(task: dict) -> dict:
        action = Path(task["action_file"])
        if action == arrived:
            arrived_reviewed.set()
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "decision": {"overall": "needs_human"},
        }

    def complete(action: Path, reviewed: dict) -> dict:
        writes.append(action)
        if action == first:
            writer_started.set()
            assert arrived_reviewed.wait(timeout=1)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        strict_global_fifo=True,
        continuous_discovery=True,
        prepare_action=prepare,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.requested == 2
    assert result.completed == 2
    assert writes == [first, arrived]
    assert prepare_calls >= 4
    metrics = json.loads(
        arrived.with_name("pipeline-metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["status"] == "exact"
    assert metrics["write_wait_attribution"]["status"] == "exact"
    assert observability.attempt_has_phases(
        arrived.with_name("observability.json"),
        request_id="request-continuous-arrived",
        received_at="2026-08-25T10:01:00+08:00",
        attempt=1,
        phases=("wait",),
    )


def test_strict_fifo_inserts_newly_discovered_older_uncommitted_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    newer = _open_action(
        flow_root,
        case_key="reorder-newer",
        created_at="2026-08-25T02:01:00+00:00",
        received_at="2026-08-25T10:01:00+08:00",
    )
    older = _open_action(
        flow_root,
        case_key="reorder-older",
        created_at="2026-08-25T02:00:00+00:00",
        received_at="2026-08-25T10:00:00+08:00",
    )
    older_reviewed = threading.Event()
    prepare_calls = 0
    writes: list[Path] = []

    def prepare(_selection: object) -> dict:
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            return {"action": "review_required", "action_file": str(newer)}
        if prepare_calls == 2:
            return {"action": "review_required", "action_file": str(older)}
        return {"action": "no_action"}

    def review(task: dict) -> dict:
        action = Path(task["action_file"])
        if action == newer:
            assert older_reviewed.wait(timeout=1)
        else:
            older_reviewed.set()
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "decision": {"overall": "needs_human"},
        }

    def complete(action: Path, reviewed: dict) -> dict:
        writes.append(action)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        2,
        project_root=tmp_path,
        strict_global_fifo=True,
        continuous_discovery=True,
        prepare_action=prepare,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: {"status": "healthy"},
    )

    assert result.completed == 2
    assert writes == [older, newer]


def test_pipeline_evidence_failure_stops_before_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action = _open_action(
        flow_root,
        case_key="evidence-failure",
        created_at="2026-08-25T02:00:00+00:00",
    )
    writes: list[Path] = []
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_persist_write_wait_evidence",
        lambda *_args, **_kwargs: False,
    )

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=lambda _task: {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "process_attempts": 1,
            "process_retries": 0,
            "token_usage": {"status": "unknown"},
            "decision": {"overall": "needs_human"},
        },
        complete_review=lambda path, reviewed: writes.append(path) or reviewed,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )

    assert writes == []
    assert result.outcomes[0].value["error_code"] == "observability_write_failed"
    assert result.outcomes[0].value["write_state"] == "not_requested"


@pytest.mark.parametrize(
    ("initial_suffix", "retry_suffix"),
    ((1, 1), (2, 1)),
)
def test_pipeline_evidence_retries_partial_observability_write(
    tmp_path: Path,
    monkeypatch,
    initial_suffix: int,
    retry_suffix: int,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action = _open_action(
        flow_root,
        case_key="evidence-retry",
        created_at="2026-08-25T02:00:00+00:00",
    )
    original_record_event = observability.record_event
    calls = 0

    def fail_second_event(path: Path, event: object) -> dict:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected_observability_failure")
        return original_record_event(path, event)

    monkeypatch.setattr(observability, "record_event", fail_second_event)
    attribution = {
        "status": "exact",
        "total_seconds": 12.0,
        "writer_busy_seconds": 5.0,
        "fifo_head_of_line_seconds": 4.0,
        "controller_scheduling_seconds": 3.0,
    }
    first = finance_preaudit_concurrency_probe._persist_write_wait_evidence(
        action,
        ready_at=10.0,
        write_started_at=22.0,
        queue_depth=2,
        attribution=attribution,
        event_suffix=initial_suffix,
    )
    persisted_before_retry = action.with_name(
        "pipeline-metrics.json"
    ).read_bytes()

    second = finance_preaudit_concurrency_probe._persist_write_wait_evidence(
        action,
        ready_at=100.0,
        write_started_at=200.0,
        queue_depth=99,
        attribution={"status": "unknown"},
        event_suffix=retry_suffix,
    )

    assert first is False
    assert second is True
    assert action.with_name("pipeline-metrics.json").read_bytes() == (
        persisted_before_retry
    )
    assert calls == 4
    summary = observability.summarize_logs(
        [action.with_name("observability.json")]
    )
    item = next(iter(summary["by_item"].values()))
    assert item["phases"]["wait"]["wall_seconds"] == 12


def test_pipeline_evidence_suffix_advances_across_controller_runs(
    tmp_path: Path,
) -> None:
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action = _open_action(
        flow_root,
        case_key="evidence-cross-run",
        created_at="2026-08-25T02:00:00+00:00",
    )

    assert finance_preaudit_concurrency_probe._persist_write_wait_evidence(
        action,
        ready_at=10.0,
        write_started_at=12.0,
        queue_depth=1,
        attribution={
            "status": "exact",
            "total_seconds": 2.0,
            "writer_busy_seconds": 0.0,
            "fifo_head_of_line_seconds": 1.0,
            "controller_scheduling_seconds": 1.0,
        },
        event_suffix=2,
    )
    assert finance_preaudit_concurrency_probe._persist_write_wait_evidence(
        action,
        ready_at=20.0,
        write_started_at=23.0,
        queue_depth=2,
        attribution={
            "status": "exact",
            "total_seconds": 3.0,
            "writer_busy_seconds": 1.0,
            "fifo_head_of_line_seconds": 1.0,
            "controller_scheduling_seconds": 1.0,
        },
        event_suffix=1,
    )

    metrics = json.loads(
        action.with_name("pipeline-metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["event_suffix"] == 3
    assert metrics["write_queue_depth_at_enqueue"] == 2
    event_ids = {
        event["event_id"]
        for event in json.loads(
            action.with_name("observability.json").read_text(encoding="utf-8")
        )["events"]
    }
    assert event_ids == {
        "04-wait-ready-2",
        "04-wait-duration-2",
        "04-wait-ready-3",
        "04-wait-duration-3",
    }


@pytest.mark.parametrize("start,maximum", [(6, 10), (3, 6)])
def test_dispatcher_adaptive_capacity_increases_inside_continuous_fifo(
    tmp_path: Path,
    monkeypatch,
    start,
    maximum,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    action_files = [
        _open_action(
            flow_root,
            case_key=f"adaptive-{index}",
            created_at=(
                f"2026-08-25T{2 + index // 60:02d}:"
                f"{index % 60:02d}:00+00:00"
            ),
            received_at=(
                f"2026-08-25T{10 + index // 60:02d}:"
                f"{index % 60:02d}:00+08:00"
            ),
        )
        for index in range(100)
    ]
    writes: list[Path] = []
    writer_lock = threading.Lock()
    active_writers = 0
    maximum_writers = 0
    active_reviews = 0
    maximum_reviews = 0
    review_entries = 0
    first_cohort = threading.Barrier(start)

    def review(_task: dict) -> dict:
        nonlocal active_reviews, maximum_reviews, review_entries
        with writer_lock:
            review_entries += 1
            entry = review_entries
            active_reviews += 1
            maximum_reviews = max(maximum_reviews, active_reviews)
        if entry <= start:
            first_cohort.wait(timeout=10)
        with writer_lock:
            active_reviews -= 1
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
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
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        }

    def complete(action: Path, reviewed: dict) -> dict:
        nonlocal active_writers, maximum_writers
        with writer_lock:
            active_writers += 1
            maximum_writers = max(maximum_writers, active_writers)
        time.sleep(0.001)
        writes.append(action)
        with writer_lock:
            active_writers -= 1
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        100,
        start,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        runtime_status=lambda: {
            "status": "healthy",
            "memory_free_percent": 40,
            "load_per_cpu": 0.5,
            "active_threads": 3,
            "cpu_count": 10,
        },
        adaptive=True,
        max_capacity=maximum,
    )

    assert result.completed == 100
    assert result.recommended_capacity == maximum
    assert result.capacity_decisions[0]["next_capacity"] == start
    assert [item["next_capacity"] for item in result.capacity_decisions
            if item["action"] == "increase"] == ([8, 10] if start == 6 else [4, 6])
    assert all(item["next_capacity"] <= maximum for item in result.capacity_decisions)
    assert max(item["active_workers"] for item in result.runtime_samples) <= maximum
    assert result.capacity_decisions[-1] == {
        "action": "hold",
        "next_capacity": maximum,
        "healthy_windows": 1,
        "reason": "configured_maximum",
    }
    assert writes == action_files
    assert maximum_writers == 1
    assert start <= maximum_reviews <= maximum


def test_dispatcher_adaptive_window_keeps_worst_heartbeat_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(12):
        _open_action(
            flow_root,
            case_key=f"pressure-{index}",
            created_at=f"2026-08-25T03:{index:02d}:00+00:00",
            received_at=f"2026-08-25T11:{index:02d}:00+08:00",
        )
    health_reads = 0

    def runtime_status() -> dict:
        nonlocal health_reads
        health_reads += 1
        status = (
            "pressure"
            if health_reads != 2
            else "unknown"
        )
        return {
            "status": status,
            "memory_free_percent": 25 if health_reads == 1 else 40,
            "load_per_cpu": 0.5,
            "active_threads": 3,
            "cpu_count": 10,
        }

    def review(_task: dict) -> dict:
        return {
            "status": "reviewed",
            "business_state": "not_available",
            "write_state": "not_requested",
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
            "agent_wall_seconds": 1,
            "decision": {"overall": "needs_human"},
        }

    def complete(_action: Path, reviewed: dict) -> dict:
        time.sleep(0.002)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        12,
        8,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.001,
        runtime_status=runtime_status,
        adaptive=True,
        max_capacity=10,
    )

    assert result.completed == 12
    assert result.capacity_decisions[0]["action"] == "decrease"
    assert result.capacity_decisions[0]["next_capacity"] == 6
    assert result.capacity_decisions[0]["reason"] == "host_pressure"
    assert [
        decision["next_capacity"]
        for decision in result.capacity_decisions
        if decision["reason"] == "host_pressure"
    ] == [6, 4, 2]
    assert result.capacity_decisions[-1]["reason"] == "host_pressure_active"
    assert result.recommended_capacity == 2


def test_strict_global_fifo_merges_recovered_open_and_live_claim_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "_preparation_executor",
        lambda: ThreadPoolExecutor(max_workers=1),
    )

    class ImmediateWriterExecutor:
        def submit(self, function, *args) -> Future:
            future: Future = Future()
            future.set_result(function(*args))
            return future

        def shutdown(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "ThreadPoolExecutor",
        lambda **_kwargs: ImmediateWriterExecutor(),
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    newer_open = _open_action(
        flow_root,
        case_key="recovered-newer",
        created_at="2026-08-25T10:01:00+08:00",
        received_at="2026-08-25T10:00:00+08:00",
    )
    newest_open = _open_action(
        flow_root,
        case_key="recovered-newest",
        created_at="2026-08-25T11:01:00+08:00",
        received_at="2026-08-25T11:00:00+08:00",
    )
    older_live = _open_action(
        flow_root,
        case_key="older-live",
        created_at="2026-08-25T09:01:00+08:00",
        received_at="2026-08-25T09:00:00+08:00",
        workflow_key="business_entertainment",
    )
    older_live_action = json.loads(older_live.read_text(encoding="utf-8"))
    older_live_action.pop("request_id")
    older_live_action.pop("received_at")
    json_artifact.write_json_atomic(older_live, older_live_action)
    selections: list[dict] = []
    writes: list[Path] = []

    def prepare(
        selection: finance_preaudit_concurrency_probe.PrepareSelection,
    ) -> dict:
        assert isinstance(selection, dict)
        selections.append(selection)
        assigned = {
            tuple(identity)
            for identity in selection["assigned_identities"]
        }
        if ("request-older-live", "2026-08-25T09:00:00+08:00") not in assigned:
            next_action = older_live
        elif (
            "request-recovered-newer",
            "2026-08-25T10:00:00+08:00",
        ) not in assigned:
            next_action = newer_open
        else:
            next_action = newest_open
        return {
            "action": "review_required",
            "action_file": str(next_action),
        }

    def review(_task: dict) -> dict:
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

    def complete(action: Path, reviewed: dict) -> dict:
        writes.append(action)
        if action == older_live:
            for artifact in (action, action.with_name("state.json")):
                persisted = json.loads(artifact.read_text(encoding="utf-8"))
                json_artifact.write_json_atomic(
                    artifact,
                    {
                        **persisted,
                        "action": "closed",
                        "execution_state": "closed",
                        "business_state": "needs_human",
                        "write_state": "submitted",
                    },
                )
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        3,
        2,
        project_root=tmp_path,
        renew_actions=lambda _paths: (_ for _ in ()).throw(
            AssertionError("strict FIFO must not consume open actions directly")
        ),
        prepare_action=prepare,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        runtime_status=lambda: {"status": "healthy"},
        strict_global_fifo=True,
    )

    assert result.completed == 3
    assert writes == [older_live, newer_open, newest_open]
    assert selections[0]["assigned_identities"] == []
    assert selections[1]["assigned_identities"] == [
        ["request-older-live", "2026-08-25T09:00:00+08:00"]
    ]
    assert selections[2]["assigned_identities"] == [
        ["request-older-live", "2026-08-25T09:00:00+08:00"],
        ["request-recovered-newer", "2026-08-25T10:00:00+08:00"],
    ]


def test_dispatcher_backpressures_claims_but_keeps_harvesting_and_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    for index in range(7):
        _open_action(
            flow_root,
            case_key=f"backpressure-{index}",
            created_at=f"2026-08-25T02:02:0{index}+00:00",
        )
    writer_started = threading.Event()
    release_writer = threading.Event()
    five_reviews_started = threading.Event()
    review_calls: list[bool] = []
    heartbeat_calls: list[int] = []
    checkpoint: list[int] = []
    guard = threading.Lock()

    def review(_task: dict) -> dict:
        with guard:
            review_calls.append(True)
            if len(review_calls) == 5:
                five_reviews_started.set()
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

    def complete(_action: Path, reviewed: dict) -> dict:
        if not writer_started.is_set():
            writer_started.set()
            assert release_writer.wait(timeout=1)
        return {
            **reviewed,
            "status": "completed",
            "business_state": "needs_human",
            "write_state": "submitted",
            "error_code": None,
        }

    def check_backpressure() -> None:
        assert writer_started.wait(timeout=1)
        assert five_reviews_started.wait(timeout=1)
        time.sleep(0.05)
        with guard:
            checkpoint.append(len(review_calls))
        release_writer.set()

    checker = threading.Thread(target=check_backpressure)
    checker.start()
    result = finance_preaudit_concurrency_probe._run_dispatched_tasks(
        7,
        2,
        project_root=tmp_path,
        renew_actions=lambda paths: paths,
        review_worker=review,
        complete_review=complete,
        executor_factory=lambda capacity: ThreadPoolExecutor(
            max_workers=capacity
        ),
        heartbeat_interval_sec=0.005,
        runtime_status=lambda: heartbeat_calls.append(len(review_calls))
        or {"status": "healthy"},
    )
    checker.join(timeout=1)

    assert checkpoint == [5]
    assert heartbeat_calls
    assert result.completed == 7
    assert max(
        sample["max_write_queue_depth"] for sample in result.runtime_samples
    ) <= 4
    pipeline = finance_preaudit_concurrency_probe._rolling_wave_metrics(result)[
        "pipeline"
    ]
    assert pipeline["review_done_to_write_start_seconds"]["samples"] == 7
    assert pipeline["writer"]["max_concurrency"] == 1
    assert 0 < pipeline["writer"]["busy_ratio"] <= 1
    assert pipeline["review_slots"]["samples"] == len(
        result.runtime_samples
    )
    assert pipeline["write_queue"] == {
        "capacity": 4,
        "max_depth": 4,
    }


def test_dispatcher_unknown_writer_stops_queue_without_second_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop = threading.Event()
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
    for index in range(3):
        _open_action(
            flow_root,
            case_key=f"writer-unknown-{index}",
            created_at=f"2026-08-25T02:03:0{index}+00:00",
        )
    writes: list[Path] = []

    def review(_task: dict) -> dict:
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

    def complete(action: Path, _reviewed: dict) -> dict:
        writes.append(action)
        return {
            "status": "failed",
            "business_state": "not_available",
            "write_state": "unknown",
            "error_code": "writer_result_unknown",
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

    assert stop.is_set()
    assert len(writes) == 1
    assert any(
        outcome.value
        and outcome.value.get("write_state") == "unknown"
        for outcome in result.outcomes
    )


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


@pytest.mark.parametrize("outcome", [
    {"status": "failed", "error_counts": {"codex_decision_invalid": 1}},
    {"status": "failed", "error_counts": {}, "stop_reason": "host_critical"},
    {"status": "completed_with_quarantine", "error_counts": {"codex_decision_invalid": 1}},
    RuntimeError("production_controller_children_active"),
    OSError("private path failure"),
    {"status": "failed", "error_counts": {"oa_session_expired": 1, "writer_result_unknown": 1}},
])
def test_controller_current_failures_notify_without_session_failure(tmp_path, monkeypatch, capsys, outcome):
    from apps.oa_finance_preaudit import session_alert
    monkeypatch.chdir(tmp_path)
    def run(**kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    sent = []
    monkeypatch.setattr(finance_preaudit_concurrency_probe, "run_rolling_drain", run)
    monkeypatch.setattr(session_alert.auth, "load_local_env", lambda: None)
    monkeypatch.setenv("OA_FEISHU_ALERT_CHAT_ID", "oc_recorded")
    monkeypatch.setattr(session_alert, "_live_status", lambda: {"status": "ok"})
    monkeypatch.setattr(session_alert, "send_message", lambda *args, **kwargs: sent.append(args))
    finance_preaudit_concurrency_probe.main(["--capacity", "3", "--confirm-write", "YES"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["notification"]["delivery_state"] == "sent"
    assert len(sent) == 1


def test_rolling_recovers_cycle_stop_with_trusted_review_isolation() -> None:
    finance_preaudit_concurrency_probe.runtime_control.request_cycle_stop()
    pending = [2]
    calls = []

    def reconcile_status() -> dict:
        snapshot = _reconciliation_snapshot(pending[0], 1)
        snapshot["continuation"] = {
            "status": "ready", "scope": "item", "isolated_pending": 1,
            "blocking_reasons": {},
        }
        snapshot["failure_attention"]["status"] = "attention_required"
        return snapshot

    def pipeline(count, capacity):
        calls.append((count, capacity))
        pending[0] = 1
        return process_pool.PoolResult(
            requested=1, started=1, completed=1, failed=0, timed_out=0,
            stopped=0, peak_active=1, elapsed_seconds=1,
            outcomes=(process_pool.TaskOutcome(
                index=0, status="completed",
                value={"status": "completed", "business_state": "needs_human",
                       "token_usage": {"status": "unknown"}},
            ),),
        )

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {"execution_identity": "verified", "by_workflow": {"travel_expense": pending[0]}},
        },
        reconcile_status=reconcile_status,
        pipeline_runner=pipeline,
    )
    assert calls == [(1, 6)]
    assert result["status"] == "completed_with_quarantine"
    assert result["recovered_cycle_stop"] is True
    assert result["final_pending"] == 1
    assert not finance_preaudit_concurrency_probe.runtime_control.stop_requested()


@pytest.mark.parametrize("continuation", [
    None,
    {"status": "blocked", "scope": "controller", "isolated_pending": 0,
     "blocking_reasons": {"pending_write_state_unresolved": 1}},
    {"status": "ready", "scope": "none", "isolated_pending": 0,
     "blocking_reasons": {}},
    {"status": "ready", "scope": "item", "isolated_pending": True,
     "blocking_reasons": {}},
])
def test_cycle_recovery_preserves_stop_for_unresolved_or_invalid_isolation(continuation) -> None:
    finance_preaudit_concurrency_probe.runtime_control.request_cycle_stop()
    snapshot = _reconciliation_snapshot(1, 1)
    snapshot["continuation"] = continuation
    with pytest.raises(ValueError, match="production_cycle_stop_recovery_blocked"):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=lambda: {
                "credentials": {"status": "ok"},
                "oa": {"execution_identity": "verified", "by_workflow": {"travel_expense": 1}},
            },
            reconcile_status=lambda: snapshot,
            pipeline_runner=lambda *_args: pytest.fail("blocked round must remain idle"),
        )
    assert finance_preaudit_concurrency_probe.runtime_control.stop_requested()


@pytest.mark.parametrize("blocker", [
    "pending_write_state_unresolved", "identity_conflict", "pending_failure_not_isolated",
])
def test_rolling_snapshot_rejects_blocker_without_existing_stop(blocker) -> None:
    snapshot = _reconciliation_snapshot(1)
    snapshot["continuation"] = {
        "status": "blocked", "scope": "controller", "isolated_pending": 0,
        "blocking_reasons": {blocker: 1},
    }
    with pytest.raises(ValueError, match="production_rolling_reconciliation_invalid"):
        finance_preaudit_concurrency_probe._rolling_snapshot(
            lambda: {"credentials": {"status": "ok"}, "oa": {
                "execution_identity": "verified", "by_workflow": {"travel_expense": 1},
            }}, lambda: snapshot,
        )


@pytest.mark.parametrize("extra_blocker", [False, True])
@pytest.mark.parametrize("artifact", [None, "execution-receipt.json", "writer-boundary.json"])
def test_explicit_recovery_gate_uses_real_reconciliation(tmp_path, monkeypatch, extra_blocker, artifact):
    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "runtime" / "finance_preaudit_flows"
    received = "2026-09-09T09:00:00+08:00"
    action = _quarantined_action(root, request_id="recover", received_at=received,
                                 failure_detail_code=None)
    for name in ("action.json", "state.json"):
        path = action.with_name(name)
        payload = json.loads(path.read_text())
        payload["created_at"] = received
        path.write_text(json.dumps(payload))
    if artifact:
        action.with_name(artifact).write_text("{}")
    rows = [{"request_id": "recover", "received_at": received},
            {"request_id": "earlier-fresh", "received_at": "2026-09-09T08:00:00+08:00"}]
    if extra_blocker:
        blocked = _quarantined_action(root, request_id="blocked", received_at=received)
        for name in ("action.json", "state.json"):
            path = blocked.with_name(name)
            payload = json.loads(path.read_text())
            payload.update(created_at=received, write_state="unknown", reason="writer_result_unknown")
            path.write_text(json.dumps(payload))
        rows.append({"request_id": "blocked", "received_at": received})
    report = probe.reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    assert not probe._continuation_is_ready(report)
    assert probe._continuation_is_ready(report, recovery_action_files=(action,)) is (not extra_blocker and artifact is None)
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}


def test_finance_success_does_not_clear_whole_task_production_fault(tmp_path, monkeypatch, capsys):
    probe = finance_preaudit_concurrency_probe
    alert = probe.session_alert
    alert.alert_once("controller_failed", chat_id="oc_recorded", control_root=tmp_path,
                     sender=lambda *args: None)
    original_clear = alert.clear_active_alert
    monkeypatch.setattr(alert, "clear_active_alert", lambda **kw: original_clear(control_root=tmp_path / "alerts", **kw))
    monkeypatch.setattr(probe, "run_rolling_drain", lambda **kw: {"status": "completed"})
    assert probe.main(["--rolling", "--capacity", "3", "--confirm-write", "YES"]) == 0
    capsys.readouterr()
    state = alert.status(control_root=tmp_path / "alerts")
    assert state["status"] == "active"
    assert state["attempt_count"] == 1


def _run_once_with_prepare(
    tmp_path: Path,
    monkeypatch,
    prepare,
    *,
    run_id: str | None = None,
    controller_instance_id: str | None = None,
):
    class CompletedExecutor:
        _processes: dict = {}

        def submit(self, action, *args) -> Future:
            future = Future()
            try:
                future.set_result(action(*args))
            except Exception as error:  # noqa: BLE001 - propagate like a pool
                future.set_exception(error)
            return future

        def shutdown(self, **_kwargs) -> None:
            return None

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
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_cycle_stop",
        lambda: None,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe.runtime_control,
        "request_stop",
        lambda reason=None: None,
    )
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    return finance_preaudit_concurrency_probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        prepare_action=prepare,
        review_worker=lambda task: {"status": "reviewed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
        run_id=run_id,
        controller_instance_id=controller_instance_id,
    )


def _control_receipts(tmp_path: Path) -> list[dict]:
    return [
        json.loads(path.read_text())
        for path in sorted((tmp_path / "runtime" / "control").glob(
            "prepare-failure-*.json"
        ))
    ]


def test_dispatcher_records_prepare_failure_when_child_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def prepare(_workflow: str) -> dict:
        raise RuntimeError("boom: private detail")

    result = _run_once_with_prepare(
        tmp_path,
        monkeypatch,
        prepare,
        run_id="0123456789abcdef0123456789abcdef",
        controller_instance_id="fedcba9876543210fedcba9876543210",
    )

    assert result.outcomes[0].value["status"] == "failed"
    assert (
        result.outcomes[0].value["error_code"]
        == "production_probe_prepare_failed"
    )
    receipts = _control_receipts(tmp_path)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["kind"] == "preaudit_prepare_failure_diagnostic"
    assert receipt["stage"] == "child_exception"
    assert receipt["error_code"] == "production_probe_prepare_failed"
    assert receipt["error_class"] == "RuntimeError"
    assert receipt["last_frame"] == "prepare"
    assert receipt["stop_action"] == "cycle_stop"
    assert receipt["run_id"] == "0123456789abcdef0123456789abcdef"
    assert receipt["controller_instance_id"] == "fedcba9876543210fedcba9876543210"
    written = (tmp_path / "runtime" / "control").glob("prepare-failure-*.json")
    assert "private detail" not in next(written).read_text()


def test_dispatcher_records_child_diagnostic_for_failed_prepare(
    tmp_path: Path,
    monkeypatch,
) -> None:
    probe = finance_preaudit_concurrency_probe

    def prepare(_selection) -> dict:
        # The dispatcher passes the global "*" selection here; a real
        # post-selection failure carries the resolved workflow instead.
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
            "prepare_failure": probe.prepare_failure.describe(
                "live_pending_read",
                error=RuntimeError("oa_read_failed: private detail"),
                stop_action="cycle_stop",
                workflow_key="travel_expense",
            ),
        }

    result = _run_once_with_prepare(tmp_path, monkeypatch, prepare)

    assert result.outcomes[0].value["status"] == "failed"
    receipts = _control_receipts(tmp_path)
    assert len(receipts) == 1
    assert receipts[0]["stage"] == "live_pending_read"
    assert receipts[0]["item"] == {"workflow_key": "travel_expense"}
    assert receipts[0]["error_class"] == "RuntimeError"


def test_prepare_failure_helpers_resolve_receipt_location(tmp_path: Path, monkeypatch) -> None:
    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    attempt = (
        flow_root
        / "travel_expense"
        / "cases"
        / ("a" * 64)
        / "attempt-2"
    )
    attempt.mkdir(parents=True)
    action_file = attempt / "action.json"

    assert probe._prepare_failure_item(action_file) == {
        "workflow_key": "travel_expense",
        "case_key": "a" * 64,
        "attempt": 2,
    }
    assert probe._prepare_failure_item(tmp_path / "elsewhere" / "action.json") == {}
    assert (
        probe._prepare_failure_item(flow_root / "travel_expense" / "action.json")
        == {}
    )
    assert probe._prepare_failure_directory(action_file) == attempt
    assert (
        probe._prepare_failure_directory(tmp_path / "elsewhere" / "action.json")
        == tmp_path / "runtime" / "control"
    )
    assert probe._prepare_failure_directory(None) == tmp_path / "runtime" / "control"


def test_prepare_failure_recorder_ignores_unknown_shapes(tmp_path: Path, monkeypatch) -> None:
    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)

    assert probe._record_prepare_failure(None) is None
    assert probe._record_prepare_failure("not-a-diagnostic") is None
    assert probe._record_prepare_failure({"stage": "not_a_stage"}) is None
    assert not (tmp_path / "runtime" / "control").exists()


def test_dispatcher_records_identity_validation_failure_next_to_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    probe = finance_preaudit_concurrency_probe
    case_key = "b" * 64
    attempt_root = (
        tmp_path
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
        / "cases"
        / case_key
        / "attempt-1"
    )
    attempt_root.mkdir(parents=True)
    missing_action = attempt_root / "action.json"

    def prepare(_selection) -> dict:
        return {
            "action": "review_required",
            "action_file": str(missing_action),
        }

    class CompletedExecutor:
        _processes: dict = {}

        def submit(self, action, *args) -> Future:
            future = Future()
            future.set_result(action(*args))
            return future

        def shutdown(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(probe, "_preparation_executor", CompletedExecutor)
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        probe.runtime_control,
        "stop_requested",
        lambda: False,
    )
    monkeypatch.setattr(
        probe.runtime_control,
        "request_cycle_stop",
        lambda: None,
    )

    result = probe._run_dispatched_tasks(
        1,
        1,
        project_root=tmp_path,
        strict_global_fifo=True,
        prepare_action=prepare,
        review_worker=lambda task: {"status": "reviewed"},
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
        run_id="0123456789abcdef0123456789abcdef",
    )

    assert result.outcomes[0].value["status"] == "failed"
    assert (
        result.outcomes[0].value["error_code"]
        == "production_probe_prepare_failed"
    )
    receipts = sorted(attempt_root.glob("prepare-failure-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["stage"] == "identity_validation"
    assert receipt["reason_code"] == "production_probe_state_invalid"
    assert receipt["item"] == {
        "workflow_key": "travel_expense",
        "case_key": case_key,
        "attempt": 1,
        "action_file": str(missing_action),
    }


def test_dispatcher_keeps_failure_when_diagnostic_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    probe = finance_preaudit_concurrency_probe

    def prepare(_selection) -> dict:
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
            "prepare_failure": probe.prepare_failure.describe(
                "live_pending_read",
                stop_action="cycle_stop",
            ),
        }

    def failing_record(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(probe.prepare_failure, "record", failing_record)

    result = _run_once_with_prepare(tmp_path, monkeypatch, prepare)

    assert result.outcomes[0].value["status"] == "failed"
    assert (
        result.outcomes[0].value["error_code"]
        == "production_probe_prepare_failed"
    )


def test_failure_round_recorder_does_not_build_a_live_oa_engine(tmp_path, monkeypatch) -> None:
    """失败轮次记录器只写单据文件,不得构建 OA/RPA 会话。"""
    probe = finance_preaudit_concurrency_probe
    flow_root = tmp_path / "runtime" / "finance_preaudit_flows"
    case = flow_root / "travel_expense" / "cases" / ("a" * 64) / "attempt-1"
    case.mkdir(parents=True)
    action = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "action": "review_required",
        "execution_state": "review_required",
        "business_state": "not_available",
        "write_state": "authorized",
        "workflow_key": "travel_expense",
        "case_key": "a" * 64,
        "attempt": 1,
        "mode": "preview",
        "write_authorized": False,
        "action_token": "t",
    }
    action_file = case / "action.json"
    (case / "state.json").write_text(json.dumps(action))
    action_file.write_text(json.dumps(action))

    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)
    called = {"live": 0}

    def forbidden(*_args, **_kwargs):
        called["live"] += 1
        raise AssertionError("must not build a live flow engine")

    monkeypatch.setattr(probe.cli, "_live_flow_engine", forbidden)

    recorded = probe._record_failure_round(action_file, "codex_process_failed")

    assert recorded.get("production_failure_round") == 1
    assert called["live"] == 0
    assert json.loads((case / "state.json").read_text())["production_failure_round"] == 1


def test_bounded_policy_isolates_exhausted_review_without_extra_production_rounds(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import codex_review_worker as worker
    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    _open_action(tmp_path / "runtime" / "finance_preaudit_flows", case_key="8" * 64,
                 created_at="2026-08-24T12:00:00+00:00")
    value = {**worker._base_result(attempts=2, usages=[{"status": "unknown"}] * 2),
             "codex_events": worker._unknown_events(2), "status": "failed",
             "business_state": "not_available", "write_state": "not_requested",
             "error_code": "codex_process_failed"}
    quarantines = []
    def quarantine(path, code):
        quarantines.append(code)
        return {"action": "needs_admin", "execution_state": "needs_admin", "business_state": "not_available",
                "write_state": "not_requested", "reason": "codex_review_failed"}
    result = probe._run_dispatched_tasks(
        1, 1, project_root=tmp_path, renew_actions=lambda paths: paths,
        review_worker=lambda _: value, quarantine_review=quarantine,
        record_failure_round=lambda *_: {"production_failure_round": 1}, defer_review_failures=False,
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"},
    )
    assert quarantines == ["codex_process_failed"]
    assert result.outcomes[0].value["status"] == "quarantined"


def test_retry_pending_does_not_hide_prepare_timeout(monkeypatch):
    probe = finance_preaudit_concurrency_probe
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(probe.runtime_control, "request_cycle_stop", lambda: None)
    result = process_pool.PoolResult(requested=2, started=2, completed=1, failed=0, timed_out=1,
        stopped=0, peak_active=1, elapsed_seconds=1.0, outcomes=(
            process_pool.TaskOutcome(index=0, status="completed", value={"status": "retry_pending",
                "error_code": "codex_review_retry_pending", "token_usage": {"status": "unknown"}}),
            process_pool.TaskOutcome(index=1, status="timed_out", value=None,
                error_code="production_probe_prepare_timeout"),
        ))
    payload = probe._run_rolling_drain_locked(
        capacity=2, live_status=lambda: {"credentials": {"status": "ok"}, "oa": {
            "execution_identity": "verified", "by_workflow": {"travel_expense": 2}}},
        reconcile_status=lambda: _reconciliation_snapshot(2, 0),
        pipeline_runner=lambda *_: result,
    )
    assert payload["status"] == "failed"
    assert payload["primary_error_code"] == "production_probe_prepare_timeout"


@pytest.mark.parametrize("notice_code", [None, *sorted(finance_preaudit_concurrency_probe.approval_guard.NOTICE_CODES)])
def test_official_first_stop_survives_secondary_prepare_failure(tmp_path, monkeypatch, notice_code):
    probe = finance_preaudit_concurrency_probe
    guard = probe.approval_guard
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)

    def pipeline(count, capacity, **kwargs):
        if notice_code is not None:
            guard.record_notice(notice_code, "cli:offline-first")
        return process_pool.PoolResult(
            requested=count, started=1, completed=0, failed=1, timed_out=0,
            stopped=count - 1, peak_active=1, elapsed_seconds=1,
            primary_error_code="production_probe_prepare_failed",
            outcomes=(process_pool.TaskOutcome(index=0, status="failed", value={
                "status": "failed", "error_code": "production_probe_prepare_failed",
                "token_usage": {"status": "unknown"},
            }),),
        )

    monkeypatch.setattr(probe, "_run_global_dispatched_tasks", pipeline)
    result = probe.run_rolling_drain(
        capacity=3, max_capacity=3, pipeline_runner=pipeline,
        live_status=lambda: {"credentials": {"status": "ok"}, "oa": {
            "execution_identity": "verified", "by_workflow": {"travel_expense": 2}}},
        reconcile_status=lambda: _reconciliation_snapshot(2),
    )
    assert result["status"] == "failed"
    if notice_code is None:
        assert result["stop_reason"] == "technical_failure"
        assert result["primary_error_code"] == "production_probe_prepare_failed"
        assert "official_notice" not in result
        return
    assert result["stop_reason"] == guard.STOP_REASON
    assert result["primary_error_code"] == guard.STOP_REASON
    assert result["official_notice"] == {
        "status": "recorded", "notice_code": notice_code,
        "triggered_at": guard.cooldown()["triggered_at"],
        "blocked_until": guard.cooldown()["blocked_until"],
    }
    assert result["last_capacity_decision"]["reason"] == guard.STOP_REASON
    assert result["error_counts"] == {"production_probe_prepare_failed": 1}
    assert result["processes_reaped"] is True
    assert result["controller_handoff"]["children_reaped"] is True
    assert probe.runtime_control.stop_reason() == guard.STOP_REASON


@pytest.mark.parametrize("evidence", ["missing", "other_run", "invalid"])
def test_official_stop_does_not_invent_classification_from_unbound_evidence(tmp_path, monkeypatch, evidence):
    probe = finance_preaudit_concurrency_probe
    guard = probe.approval_guard
    monkeypatch.setattr(probe, "PROJECT_ROOT", tmp_path)

    def pipeline(count, capacity, **kwargs):
        if evidence == "other_run":
            with monkeypatch.context() as context:
                context.setenv(guard.RUN_ENV, "a" * 32)
                guard.record_notice("usage_limit", "cli:old")
        else:
            probe.runtime_control.request_stop(guard.STOP_REASON)
            if evidence == "invalid":
                guard.write(guard.directory() / "cooldown.json", {"notice_code": "usage_limit"})
        return process_pool.PoolResult(requested=count, started=0, completed=0, failed=0,
            timed_out=0, stopped=count, peak_active=0, elapsed_seconds=1, outcomes=())

    monkeypatch.setattr(probe, "_run_global_dispatched_tasks", pipeline)
    kwargs = dict(capacity=3, max_capacity=3, pipeline_runner=pipeline,
        live_status=lambda: {"credentials": {"status": "ok"}, "oa": {
            "execution_identity": "verified", "by_workflow": {"travel_expense": 1}}},
        reconcile_status=lambda: _reconciliation_snapshot(1))
    if evidence == "invalid":
        with pytest.raises(ValueError, match="approval_guard_untrusted"):
            probe.run_rolling_drain(**kwargs)
    else:
        result = probe.run_rolling_drain(**kwargs)
        assert result["status"] == "failed"
        assert result["official_notice"] == {"status": "unknown"}
    assert probe.runtime_control.stop_reason() == guard.STOP_REASON
