from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import (
    cli,
    observability,
    retention,
    review_failure_evidence,
    runtime_maintenance,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _attempt(
    root: Path,
    *,
    case_key: str,
    attempt: int,
    age_days: int,
    workflow_key: str = "travel_expense",
    execution_state: str = "closed",
    business_state: str = "pass",
    write_state: str = "not_requested",
    reason: str | None = None,
    failure_detail_code: str | None = None,
    archived: bool = False,
    received_at: str | None = None,
) -> Path:
    created_at = (NOW - timedelta(days=age_days)).isoformat()
    request_id = f"request-{case_key}"
    received_at = received_at or created_at
    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    prefix = root / ".archive" if archived else root
    attempt_root = (
        prefix
        / workflow_key
        / "cases"
        / case_key
        / f"attempt-{attempt}"
    )
    attempt_root.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "workflow_key": workflow_key,
        "case_key": case_key,
        "attempt": attempt,
        "request_id": request_id,
        "received_at": received_at,
        "created_at": created_at,
        "execution_state": execution_state,
        "business_state": business_state,
        "write_state": write_state,
    }
    if reason is not None:
        state["reason"] = reason
    if failure_detail_code is not None:
        state["failure_detail_code"] = failure_detail_code
    if reason == "codex_review_failed":
        action_file = attempt_root / "action.json"
        state_file = attempt_root / "state.json"
        receipt_file = attempt_root / "review-failure.json"
        json_artifact.write_json_atomic(
            receipt_file,
            review_failure_evidence.receipt_payload(
                action_token="test-action-token",
                workflow_key=workflow_key,
                failure_code="codex_process_failed",
                attempt_error_codes=[
                    "codex_process_failed",
                    "codex_process_failed",
                ],
            ),
        )
        state.update(
            {
                "action": "needs_admin",
                "action_token": "test-action-token",
                "mode": "preview",
                "write_authorized": False,
                "action_file": str(action_file),
                "state_file": str(state_file),
                "review_failure_fingerprint": (
                    review_failure_evidence.file_fingerprint(receipt_file)
                ),
            }
        )
        json_artifact.write_json_atomic(
            action_file,
            {
                key: state[key]
                for key in (
                    "schema_version",
                    "kind",
                    "action",
                    "execution_state",
                    "business_state",
                    "write_state",
                    "workflow_key",
                    "mode",
                    "write_authorized",
                    "reason",
                    "action_file",
                    "state_file",
                    "case_key",
                    "attempt",
                    "review_failure_fingerprint",
                )
            },
        )
    json_artifact.write_json_atomic(attempt_root / "state.json", state)
    (attempt_root / "material.json").write_text(
        '{"sensitive":"must-not-appear"}',
        encoding="utf-8",
    )
    return attempt_root


def _empty_daily_rollup() -> dict:
    outcomes = runtime_maintenance._empty_outcome_metrics()
    return {
        "observability": observability.empty_daily_metrics(),
        "outcomes": {"overall": outcomes, "by_flow": {}},
    }


def test_status_is_versioned_and_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    first_attempt = _attempt(
        root,
        case_key="a" * 64,
        attempt=1,
        age_days=2,
        execution_state="needs_admin",
        business_state="not_available",
        reason="observability_incomplete",
    )
    _attempt(
        root,
        case_key="b" * 64,
        attempt=1,
        age_days=1,
        execution_state="review_required",
        business_state="not_available",
    )
    archived_attempt = _attempt(
        root,
        case_key="c" * 64,
        attempt=1,
        age_days=10,
        archived=True,
    )
    boundary_attempt = _attempt(
        root,
        case_key="d" * 64,
        attempt=1,
        age_days=30,
        archived=True,
    )
    expired_attempt = _attempt(
        root,
        case_key="e" * 64,
        attempt=1,
        age_days=31,
        archived=True,
    )
    observability.record_attempt_phase(
        {
            "request_root": str(first_attempt),
            "request_id": "recorded-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "attempt": 1,
            "flow_key": "travel_expense",
        },
        event_id="review",
        phase="review",
        wall_seconds=5,
        active_seconds=5,
        result="success",
    )
    observability.record_attempt_phase(
        {
            "request_root": str(archived_attempt),
            "request_id": "archived-request",
            "received_at": "2026-08-12T09:00:00+08:00",
            "attempt": 1,
            "flow_key": "travel_expense",
        },
        event_id="close",
        phase="close",
        wall_seconds=2,
        active_seconds=2,
        result="success",
    )
    for attempt_root, request_id, occurred_at, wall_seconds in (
        (boundary_attempt, "boundary-request", "2026-07-23T12:00:00Z", 3),
        (expired_attempt, "expired-request", "2026-07-22T12:00:00Z", 100),
    ):
        observability.record_event(
            attempt_root / "observability.json",
            {
                "event_id": "close",
                "item_key": observability.attempt_key(
                    request_id,
                    "2026-07-23T20:00:00+08:00",
                    1,
                ),
                "workflow_key": "travel_expense",
                "occurred_at": occurred_at,
                "phase": "close",
                "wall_seconds": wall_seconds,
                "active_seconds": wall_seconds,
                "queue_wait_seconds": 0,
                "result": "success",
                "model": None,
                "token_usage": {"status": "unknown"},
            },
        )

    status = runtime_maintenance.status(root, now=NOW)
    encoded = json.dumps(status, ensure_ascii=False)

    assert status["schema_version"] == 1
    assert status["kind"] == "preaudit_runtime_status"
    flow = status["workflows"][0]
    assert flow["workflow_key"] == "travel_expense"
    assert flow["case_count"] == 2
    assert flow["attempt_count"] == 2
    assert flow["backlog_count"] == 2
    assert flow["oldest_backlog_age_seconds"] == 2 * 86400
    assert flow["execution_states"] == {
        "needs_admin": 1,
        "review_required": 1,
    }
    assert flow["failure_counts"] == {"observability_incomplete": 1}
    assert status["observability"]["event_count"] == 3
    assert status["observability"]["totals"]["approval"]["wall_seconds"] == 10
    assert status["observability"]["totals"]["approval"]["usage_unknown_events"] == 3
    assert "sensitive" not in encoded
    assert str(root) not in encoded
    assert "request_id" not in encoded


def test_status_projects_operational_summary_and_exact_stage_timeline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    closed = _attempt(
        root,
        case_key="closed",
        attempt=1,
        age_days=2,
        execution_state="closed",
        business_state="needs_human",
        write_state="submitted",
    )
    _attempt(
        root,
        case_key="isolated",
        attempt=1,
        age_days=1,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="material_collection_failed",
        failure_detail_code="attachment_download_failed",
    )
    _attempt(
        root,
        case_key="unknown",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="unknown",
        reason="writer_result_unknown",
    )
    _attempt(
        root,
        case_key="reviewing",
        attempt=1,
        age_days=0,
        execution_state="review_required",
        business_state="not_available",
        write_state="authorized",
    )
    state = json.loads((closed / "state.json").read_text(encoding="utf-8"))
    item_key = observability.attempt_key(
        state["request_id"],
        state["received_at"],
        state["attempt"],
    )
    for index, phase in enumerate(
        ("discover", "collect", "review", "submit", "postcheck", "close"),
        start=1,
    ):
        observability.record_event(
            closed / "observability.json",
            {
                "event_id": f"{index:02d}-{phase}",
                "item_key": item_key,
                "workflow_key": "travel_expense",
                "occurred_at": f"2026-08-20T12:00:{index:02d}Z",
                "phase": phase,
                "wall_seconds": 1,
                "active_seconds": 1,
                "queue_wait_seconds": 0,
                "result": "success",
                "model": None,
                "token_usage": {"status": "unknown"},
            },
        )

    status = runtime_maintenance.status(root, now=NOW)
    encoded = json.dumps(status, ensure_ascii=False)

    assert status["operations"] == {
        "remaining": 3,
        "completed": 1,
        "needs_human": 1,
        "item_local_quarantined": 1,
        "technical_failures": 0,
        "unknown": 1,
        "oldest_wait_seconds": 86400,
        "backlog_wait": {
            "evidence_status": "exact",
            "open_cases": 3,
            "oldest_seconds": 86400,
        },
        "failure_attention": {
            "scope": "local_open_history",
            "status": "attention_required",
            "affected_cases": 1,
            "repeated_failure_cases": 0,
            "long_wait_failure_cases": 1,
            "long_wait_threshold_seconds": 86400,
            "max_failure_attempts": 1,
            "oldest_wait_seconds": 86400,
            "failure_counts": {"material_collection_failed": 1},
        },
        "current_bottleneck": {
            "evidence_status": "exact",
            "kind": "unknown_write_outcome",
            "affected_cases": 1,
        },
        "current_stages": {"needs_admin": 2, "review_required": 1},
        "next_step": "reconcile_unknown",
    }
    timeline = status["case_metrics"][0]["stage_timestamps"]
    assert status["case_metrics"][0]["waiting_seconds"] == "unknown"
    assert status["case_metrics"][0]["timing_status"] == "unknown"
    assert status["case_metrics"][0]["phase_seconds"] == {
        "discover": 1,
        "collect": 1,
        "review": 1,
        "wait": "unknown",
        "submit": 1,
        "postcheck": 1,
        "close": 1,
    }
    assert timeline["claimed"]["status"] == "exact"
    assert timeline["discovered"] == {
        "status": "exact",
        "occurred_at": "2026-08-20T12:00:01Z",
    }
    assert timeline["prepared"] == {
        "status": "exact",
        "occurred_at": "2026-08-20T12:00:02Z",
    }
    assert timeline["write_ready"] == {"status": "unknown"}
    assert timeline["writer"] == {
        "status": "exact",
        "occurred_at": "2026-08-20T12:00:04Z",
    }
    assert timeline["postcheck"] == {
        "status": "exact",
        "occurred_at": "2026-08-20T12:00:05Z",
    }
    assert timeline["closed"] == {
        "status": "exact",
        "occurred_at": "2026-08-20T12:00:06Z",
    }
    assert "request-" not in encoded
    assert str(root) not in encoded


def test_status_projects_wait_evidence_without_guessing_legacy_wait(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    closed = _attempt(
        root,
        case_key="wait-evidence",
        attempt=1,
        age_days=0,
        execution_state="closed",
        business_state="pass",
        write_state="submitted",
    )
    state = json.loads((closed / "state.json").read_text(encoding="utf-8"))
    item_key = observability.attempt_key(
        state["request_id"],
        state["received_at"],
        state["attempt"],
    )
    for event_id, occurred_at, wall_seconds in (
        ("wait-ready", "2026-08-22T12:00:00Z", 0),
        ("wait-duration", "2026-08-22T12:00:12Z", 12),
    ):
        observability.record_event(
            closed / "observability.json",
            {
            "event_id": event_id,
            "item_key": item_key,
            "workflow_key": "travel_expense",
            "occurred_at": occurred_at,
            "phase": "wait",
            "wall_seconds": wall_seconds,
            "active_seconds": 0,
            "queue_wait_seconds": wall_seconds,
            "result": "success",
            "model": None,
            "token_usage": {"status": "unknown"},
            },
        )
    json_artifact.write_json_atomic(
        closed / "pipeline-metrics.json",
        {
            "schema_version": 1,
            "kind": "preaudit_pipeline_metrics",
            "status": "exact",
            "item_key": item_key,
            "workflow_key": "travel_expense",
            "attempt": 1,
            "event_suffix": 1,
            "write_ready_at": "2026-08-22T12:00:00+00:00",
            "write_start_at": "2026-08-22T12:00:12+00:00",
            "write_queue_depth_at_enqueue": 2,
            "write_wait_attribution": {
                "status": "exact",
                "total_seconds": 12.0,
                "writer_busy_seconds": 5.0,
                "fifo_head_of_line_seconds": 4.0,
                "controller_scheduling_seconds": 3.0,
            },
        },
    )

    status = runtime_maintenance.status(root, now=NOW)
    metric = status["case_metrics"][0]

    assert metric["waiting_seconds"] == 12
    assert metric["timing_status"] == "exact"
    assert metric["stage_timestamps"]["write_ready"] == {
        "status": "exact",
        "occurred_at": "2026-08-22T12:00:00Z",
    }
    assert metric["pipeline_evidence"]["write_queue_depth_at_enqueue"] == 2
    assert metric["pipeline_evidence"]["write_wait_attribution"] == {
        "status": "exact",
        "total_seconds": 12.0,
        "writer_busy_seconds": 5.0,
        "fifo_head_of_line_seconds": 4.0,
        "controller_scheduling_seconds": 3.0,
    }


def test_status_keeps_each_missing_phase_unknown(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    closed = _attempt(
        root,
        case_key="missing-phases",
        attempt=1,
        age_days=0,
        execution_state="closed",
        business_state="pass",
        write_state="submitted",
    )
    state = json.loads((closed / "state.json").read_text(encoding="utf-8"))
    item_key = observability.attempt_key(
        state["request_id"],
        state["received_at"],
        state["attempt"],
    )
    observability.record_event(
        closed / "observability.json",
        {
            "event_id": "01-discover",
            "item_key": item_key,
            "workflow_key": "travel_expense",
            "occurred_at": "2026-08-22T12:00:00Z",
            "phase": "discover",
            "wall_seconds": 1,
            "active_seconds": 1,
            "queue_wait_seconds": 0,
            "result": "success",
            "model": None,
            "token_usage": {"status": "unknown"},
        },
    )

    phases = runtime_maintenance.status(root, now=NOW)["case_metrics"][0][
        "phase_seconds"
    ]

    assert phases == {
        "discover": 1,
        "collect": "unknown",
        "review": "unknown",
        "wait": "unknown",
        "submit": "unknown",
        "postcheck": "unknown",
        "close": "unknown",
    }


def test_status_classifies_allowlisted_item_local_quarantines(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    _attempt(
        root,
        case_key="stable-material-failure",
        attempt=1,
        age_days=1,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="material_collection_failed",
        failure_detail_code="attachment_download_failed",
    )
    _attempt(
        root,
        case_key="unsafe-material-failure",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="material_collection_failed",
        failure_detail_code="frozen_material_changed",
    )
    _attempt(
        root,
        case_key="invalid-codex-decision",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="codex_decision_invalid",
    )
    _attempt(
        root,
        case_key="failed-codex-review",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="codex_review_failed",
    )

    status = runtime_maintenance.status(root, now=NOW)

    assert status["operations"]["item_local_quarantined"] == 3
    assert status["operations"]["technical_failures"] == 1
    assert status["operations"]["current_bottleneck"] == {
        "evidence_status": "exact",
        "kind": "technical_failure",
        "affected_cases": 1,
    }
    assert status["workflows"][0]["failure_counts"] == {
        "codex_decision_invalid": 1,
        "codex_review_failed": 1,
        "material_collection_failed": 2,
    }
    assert "frozen_material_changed" not in json.dumps(status)


def test_status_treats_tampered_review_quarantine_as_technical(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    attempt_root = _attempt(
        root,
        case_key="tampered-codex-review",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="codex_review_failed",
    )
    (attempt_root / "review-failure.json").write_text("{}", encoding="utf-8")

    status = runtime_maintenance.status(root, now=NOW)

    assert status["operations"]["item_local_quarantined"] == 0
    assert status["operations"]["technical_failures"] == 1
    assert status["workflows"][0]["failure_counts"] == {
        "review_quarantine_invalid": 1,
    }


def test_status_highlights_repeated_and_long_lived_failed_backlog(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    repeated_received_at = NOW.isoformat()
    _attempt(
        root,
        case_key="repeated-failure",
        attempt=1,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="writer_preflight_failed",
        received_at=repeated_received_at,
    )
    _attempt(
        root,
        case_key="repeated-failure",
        attempt=2,
        age_days=0,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="material_collection_failed",
        failure_detail_code="attachment_download_failed",
        received_at=repeated_received_at,
    )
    _attempt(
        root,
        case_key="long-lived-failure",
        attempt=1,
        age_days=2,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="not_requested",
        reason="write_authorization_invalid",
    )
    _attempt(
        root,
        case_key="active-review",
        attempt=1,
        age_days=2,
        execution_state="review_required",
        business_state="not_available",
        write_state="authorized",
    )

    status = runtime_maintenance.status(root, now=NOW)

    assert status["operations"]["failure_attention"] == {
        "scope": "local_open_history",
        "status": "attention_required",
        "affected_cases": 2,
        "repeated_failure_cases": 1,
        "long_wait_failure_cases": 1,
        "long_wait_threshold_seconds": 86400,
        "max_failure_attempts": 2,
        "oldest_wait_seconds": 172800,
        "failure_counts": {
            "material_collection_failed": 1,
            "write_authorization_invalid": 1,
        },
    }


@pytest.mark.parametrize(
    "attribution",
    (
        {
            "status": "exact",
            "total_seconds": 10,
            "writer_busy_seconds": 8,
            "fifo_head_of_line_seconds": 8,
            "controller_scheduling_seconds": 0,
        },
        {
            "status": "exact",
            "total_seconds": -1,
            "writer_busy_seconds": 0,
            "fifo_head_of_line_seconds": 0,
            "controller_scheduling_seconds": 0,
        },
        {"status": "unknown", "secret": "must-not-pass"},
    ),
)
def test_pipeline_evidence_rejects_untrusted_attribution(
    tmp_path: Path,
    attribution: dict,
) -> None:
    root = tmp_path / "runtime"
    closed = _attempt(
        root,
        case_key="pipeline-untrusted",
        attempt=1,
        age_days=0,
        execution_state="closed",
        business_state="pass",
        write_state="submitted",
    )
    state = json.loads((closed / "state.json").read_text(encoding="utf-8"))
    item_key = observability.attempt_key(
        state["request_id"],
        state["received_at"],
        state["attempt"],
    )
    json_artifact.write_json_atomic(
        closed / "pipeline-metrics.json",
        {
            "schema_version": 1,
            "kind": "preaudit_pipeline_metrics",
            "status": "exact",
            "item_key": item_key,
            "workflow_key": "travel_expense",
            "attempt": 1,
            "event_suffix": 1,
            "write_ready_at": "2026-08-22T12:00:00+00:00",
            "write_start_at": "2026-08-22T12:00:12+00:00",
            "write_queue_depth_at_enqueue": 1,
            "write_wait_attribution": attribution,
        },
    )

    metric = runtime_maintenance.status(root, now=NOW)["case_metrics"][0]

    assert metric["pipeline_evidence"] == {"status": "unknown"}
    assert "must-not-pass" not in json.dumps(metric)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("attempt", True),
        ("event_suffix", True),
        ("schema_version", 1.0),
        ("attempt", 1.0),
    ),
)
def test_pipeline_evidence_rejects_boolean_integer_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root = tmp_path / "runtime"
    closed = _attempt(
        root,
        case_key="pipeline-boolean",
        attempt=1,
        age_days=0,
        execution_state="closed",
        business_state="pass",
        write_state="submitted",
    )
    state = json.loads((closed / "state.json").read_text(encoding="utf-8"))
    item_key = observability.attempt_key(
        state["request_id"],
        state["received_at"],
        state["attempt"],
    )
    payload = {
        "schema_version": 1,
        "kind": "preaudit_pipeline_metrics",
        "status": "exact",
        "item_key": item_key,
        "workflow_key": "travel_expense",
        "attempt": 1,
        "event_suffix": 1,
        "write_ready_at": "2026-08-22T12:00:00+00:00",
        "write_start_at": "2026-08-22T12:00:12+00:00",
        "write_queue_depth_at_enqueue": 1,
        "write_wait_attribution": {"status": "unknown"},
    }
    payload[field] = value
    json_artifact.write_json_atomic(
        closed / "pipeline-metrics.json",
        payload,
    )

    metric = runtime_maintenance.status(root, now=NOW)["case_metrics"][0]

    assert metric["pipeline_evidence"] == {"status": "unknown"}


def test_status_and_cli_never_echo_unknown_failure_text(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _attempt(
        root,
        case_key="a" * 64,
        attempt=1,
        age_days=1,
        execution_state="needs_admin",
        business_state="not_available",
        reason="secret_token_abc",
    )

    status = runtime_maintenance.status(root, now=NOW)
    encoded = json.dumps(status)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(
            ["status", "--root", str(root)],
            runtime_status=lambda _root: (_ for _ in ()).throw(
                ValueError("secret_token_abc")
            ),
        )

    assert status["workflows"][0]["failure_counts"] == {"unknown_failure": 1}
    assert "secret_token_abc" not in encoded
    assert exit_code == 1
    assert json.loads(stdout.getvalue())["failure"]["code"] == "runtime_failed"
    assert stderr.getvalue().strip() == "runtime_failed"


def test_status_cli_reports_stable_execution_login_failure(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(
            ["status", "--root", str(tmp_path), "--check-live"],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
            live_status=lambda: (_ for _ in ()).throw(
                RuntimeError("oa_execution_identity_login_failed")
            ),
        )

    assert exit_code == 1
    assert (
        json.loads(stdout.getvalue())["failure"]["code"]
        == "oa_execution_identity_login_failed"
    )
    assert stderr.getvalue().strip() == "oa_execution_identity_login_failed"


@pytest.mark.parametrize(
    "error_code",
    (
        "oa_session_expired",
        "rpa_cdp_timeout",
        "rpa_cdp_unavailable",
        "rpa_session_invalid",
        "rpa_session_missing",
    ),
)
def test_status_cli_preserves_web_session_failure_codes(
    tmp_path: Path,
    error_code: str,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(
            ["status", "--root", str(tmp_path), "--check-live"],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
            live_status=lambda: (_ for _ in ()).throw(
                RuntimeError(error_code)
            ),
        )

    assert exit_code == 1
    assert json.loads(stdout.getvalue())["failure"]["code"] == error_code
    assert stderr.getvalue().strip() == error_code


def test_status_counts_only_the_latest_attempt_per_case(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _attempt(
        root,
        case_key="f" * 64,
        attempt=1,
        age_days=2,
        execution_state="needs_admin",
        business_state="not_available",
        reason="writer_submit_rejected",
        received_at="2026-08-20T12:00:00+00:00",
    )
    _attempt(
        root,
        case_key="f" * 64,
        attempt=2,
        age_days=1,
        execution_state="review_required",
        business_state="not_available",
        write_state="authorized",
        received_at="2026-08-20T12:00:00+00:00",
    )

    flow = runtime_maintenance.status(root, now=NOW)["workflows"][0]

    assert flow["case_count"] == 1
    assert flow["attempt_count"] == 2
    assert flow["backlog_count"] == 1
    assert flow["execution_states"] == {"review_required": 1}
    assert flow["write_states"] == {"authorized": 1}
    assert flow["failure_counts"] == {}


@pytest.mark.parametrize(
    "reason",
    ("writer_preflight_failed", "submission_target_changed"),
)
def test_status_keeps_sanitized_preflight_failure_codes(
    tmp_path: Path,
    reason: str,
) -> None:
    root = tmp_path / "runtime"
    _attempt(
        root,
        case_key="e" * 64,
        attempt=1,
        age_days=1,
        execution_state="needs_admin",
        business_state="not_available",
        reason=reason,
    )

    flow = runtime_maintenance.status(root, now=NOW)["workflows"][0]

    assert flow["failure_counts"] == {reason: 1}


def test_status_fails_closed_when_state_identity_does_not_match_case(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    attempt_root = _attempt(
        root,
        case_key="identity-source",
        attempt=1,
        age_days=1,
    )
    state_file = attempt_root / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["request_id"] = "different-request"
    json_artifact.write_json_atomic(state_file, state)

    with pytest.raises(ValueError, match="runtime_state_invalid"):
        runtime_maintenance.status(root, now=NOW)


def test_prune_archives_deletes_and_preserves_protected_attempts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    previous = _attempt(root, case_key="a" * 64, attempt=1, age_days=10)
    expired = _attempt(root, case_key="b" * 64, attempt=1, age_days=31)
    latest = _attempt(root, case_key="c" * 64, attempt=1, age_days=1)
    open_attempt = _attempt(
        root,
        case_key="d" * 64,
        attempt=1,
        age_days=90,
        execution_state="review_required",
        business_state="not_available",
    )
    unknown = _attempt(
        root,
        case_key="e" * 64,
        attempt=1,
        age_days=90,
        execution_state="needs_admin",
        business_state="not_available",
        write_state="unknown",
    )

    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    first = runtime_maintenance.apply_prune_plan(root, plan, now=NOW)
    second = runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert first == {
        "schema_version": 1,
        "kind": "preaudit_prune_receipt",
        "archived": 1,
        "deleted": 1,
        "missing": 0,
        "failed": 0,
        "status": "ok",
    }
    assert second["missing"] == 2
    assert not previous.exists()
    assert (root / ".archive" / previous.relative_to(root)).is_dir()
    assert not expired.exists()
    assert latest.is_dir()
    assert open_attempt.is_dir()
    assert unknown.is_dir()
    assert "targets" not in first


def test_prune_compacts_raw_metrics_before_delete_and_keeps_daily_180_days(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    item_key = observability.attempt_key(
        "expired-request",
        "2026-07-22T09:00:00+08:00",
        1,
    )
    observability.record_event(
        expired / "observability.json",
        {
            "event_id": "close",
            "item_key": item_key,
            "workflow_key": "travel_expense",
            "occurred_at": "2026-07-22T04:00:00Z",
            "phase": "close",
            "wall_seconds": 1,
            "active_seconds": 1,
            "queue_wait_seconds": 0,
            "result": "success",
            "model": None,
            "token_usage": {"status": "unknown"},
        },
    )
    metrics_root = root / ".metrics" / "daily"
    metrics_root.mkdir(parents=True)
    for age_days in (180, 181):
        day = (NOW - timedelta(days=age_days)).date().isoformat()
        json_artifact.write_json_atomic(
            metrics_root / f"{day}.json",
            {
                "schema_version": 1,
                "kind": "preaudit_daily_metric",
                "day": day,
                "generated_at": NOW.isoformat(),
                    "metrics": _empty_daily_rollup(),
            },
        )

    receipt = runtime_maintenance.prune(root, now=NOW)
    status = runtime_maintenance.status(root, now=NOW)

    compacted = metrics_root / "2026-07-22.json"
    boundary = metrics_root / f"{(NOW - timedelta(days=180)).date()}.json"
    expired_daily = metrics_root / f"{(NOW - timedelta(days=181)).date()}.json"
    assert receipt["deleted"] == 2
    assert not expired.exists()
    assert compacted.is_file()
    assert boundary.is_file()
    assert not expired_daily.exists()
    assert item_key not in compacted.read_text(encoding="utf-8")
    assert {item["day"] for item in status["daily_history"]} == {
        "2026-07-22",
        "2026-08-21",
        boundary.stem,
    }
    compacted_payload = json.loads(compacted.read_text(encoding="utf-8"))
    assert set(compacted_payload["metrics"]) == {"observability", "outcomes"}
    assert compacted_payload["metrics"]["outcomes"]["overall"][
        "evidence_status"
    ] in {"exact", "unknown"}


def test_prune_refreshes_a_late_unknown_outcome_without_changing_its_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    old_generated_at = NOW - timedelta(minutes=10)
    for index in range(6):
        attempt_root = _attempt(
            root, case_key=f"original-{index}", attempt=1, age_days=1,
        )
        state_path = attempt_root / "state.json"
        earlier = (old_generated_at - timedelta(minutes=1)).timestamp()
        os.utime(state_path, (earlier, earlier))
    old_payloads = runtime_maintenance._daily_payloads(
        root, now=old_generated_at,
    )
    assert len(old_payloads) == 1
    daily_path, old_payload = next(iter(old_payloads.items()))
    assert old_payload["metrics"]["outcomes"]["overall"]["attempt_count"] == 6
    daily_path.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(daily_path, old_payload)

    late = _attempt(
        root, case_key="late-unknown", attempt=1, age_days=1,
        workflow_key="business_entertainment", execution_state="needs_admin",
        business_state="not_available", write_state="unknown",
        reason="writer_result_unknown",
    )
    late_state = late / "state.json"
    later = (old_generated_at + timedelta(minutes=1)).timestamp()
    os.utime(late_state, (later, later))
    frozen_state = late_state.read_bytes()
    old_fingerprint = f"sha256:{hashlib.sha256(daily_path.read_bytes()).hexdigest()}"

    receipt = runtime_maintenance.prune(root, now=NOW)
    refreshed = runtime_maintenance._read_daily_metric(daily_path)

    assert receipt["status"] == "ok"
    assert refreshed["metrics"]["outcomes"]["overall"]["attempt_count"] == 7
    assert refreshed["metrics"]["outcomes"]["overall"]["write_unknown"] == 1
    assert refreshed["metrics"]["observability"] == old_payload["metrics"]["observability"]
    assert refreshed["prior_fingerprints"] == [old_fingerprint]
    assert late_state.read_bytes() == frozen_state


def test_prune_rejects_rewritten_prior_outcome(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    old_generated_at = NOW - timedelta(minutes=10)
    attempt_root = _attempt(root, case_key="prior", attempt=1, age_days=1)
    daily_path, old_payload = next(iter(runtime_maintenance._daily_payloads(
        root, now=old_generated_at,
    ).items()))
    daily_path.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(daily_path, old_payload)
    old_daily = daily_path.read_bytes()
    state_path = attempt_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "unknown",
        "reason": "writer_result_unknown",
    })
    json_artifact.write_json_atomic(state_path, state)

    with pytest.raises(ValueError, match="retention_plan_invalid") as failure:
        runtime_maintenance.prune(root, now=NOW)

    assert str(failure.value.__cause__) == "daily_metric_conflict"
    assert cli._failure_payload(failure.value, command="prune")["failure"] == {
        "code": "retention_plan_invalid",
        "detail_code": "daily_metric_conflict",
        "stage": "prune",
    }
    assert daily_path.read_bytes() == old_daily


def test_prune_rejects_changed_observability_with_equal_event_count(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    old_generated_at = NOW - timedelta(minutes=10)
    prior = _attempt(root, case_key="prior", attempt=1, age_days=1)
    state_path = prior / "state.json"
    earlier = (old_generated_at - timedelta(minutes=1)).timestamp()
    os.utime(state_path, (earlier, earlier))
    event_path = prior / "observability.json"
    observability.record_event(event_path, {
        "event_id": "review",
        "item_key": observability.attempt_key(
            "request-prior", (NOW - timedelta(days=1)).isoformat(), 1,
        ),
        "workflow_key": "travel_expense",
        "occurred_at": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "phase": "review",
        "wall_seconds": 1,
        "active_seconds": 1,
        "queue_wait_seconds": 0,
        "result": "success",
        "model": None,
        "token_usage": {"status": "unknown"},
    })
    daily_path, old_payload = next(iter(runtime_maintenance._daily_payloads(
        root, now=old_generated_at,
    ).items()))
    daily_path.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(daily_path, old_payload)
    old_daily = daily_path.read_bytes()

    late = _attempt(
        root, case_key="late", attempt=1, age_days=1,
        workflow_key="business_entertainment", execution_state="needs_admin",
        business_state="not_available", write_state="unknown",
        reason="writer_result_unknown",
    )
    later = (old_generated_at + timedelta(minutes=1)).timestamp()
    os.utime(late / "state.json", (later, later))
    event_log = json.loads(event_path.read_text(encoding="utf-8"))
    event_log["events"][0]["wall_seconds"] = 2
    json_artifact.write_json_atomic(event_path, event_log)

    with pytest.raises(ValueError, match="retention_plan_invalid") as failure:
        runtime_maintenance.prune(root, now=NOW)

    assert str(failure.value.__cause__) == "daily_metric_conflict"
    assert daily_path.read_bytes() == old_daily


def test_prune_rejects_prior_outcome_rewrite_when_event_count_grows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    prior = _attempt(root, case_key="prior", attempt=1, age_days=1)
    daily_path, old_payload = next(iter(runtime_maintenance._daily_payloads(
        root, now=NOW - timedelta(minutes=10),
    ).items()))
    daily_path.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(daily_path, old_payload)
    old_daily = daily_path.read_bytes()
    state_path = prior / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "unknown",
        "reason": "writer_result_unknown",
    })
    json_artifact.write_json_atomic(state_path, state)
    observability.record_event(prior / "observability.json", {
        "event_id": "late-event",
        "item_key": observability.attempt_key(
            "request-prior", (NOW - timedelta(days=1)).isoformat(), 1,
        ),
        "workflow_key": "travel_expense",
        "occurred_at": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "phase": "review",
        "wall_seconds": 1,
        "active_seconds": 1,
        "queue_wait_seconds": 0,
        "result": "success",
        "model": None,
        "token_usage": {"status": "unknown"},
    })

    with pytest.raises(ValueError, match="retention_plan_invalid") as failure:
        runtime_maintenance.prune(root, now=NOW)

    assert str(failure.value.__cause__) == "daily_metric_conflict"
    assert daily_path.read_bytes() == old_daily


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("request_id", "different-request"),
        ("workflow_key", "packaging_layout_approval"),
        ("workflow_key", "unsupported_workflow"),
        ("case_key", "b" * 64),
        ("case_key", None),
        ("attempt", 2),
        ("attempt", "1"),
    ],
)
def test_prune_rejects_invalid_attempt_identity(
    tmp_path: Path, field: str, replacement: object,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    attempt_root = _attempt(root, case_key="invalid", attempt=1, age_days=1)
    state_path = attempt_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = replacement
    json_artifact.write_json_atomic(state_path, state)

    with pytest.raises(ValueError, match="retention_plan_invalid") as failure:
        runtime_maintenance.prune(root, now=NOW)

    assert str(failure.value.__cause__) == "runtime_state_identity_invalid"
    assert attempt_root.is_dir()


def test_daily_metric_rejects_null_fingerprint_history(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    _attempt(root, case_key="prior", attempt=1, age_days=1)
    daily_path, payload = next(iter(runtime_maintenance._daily_payloads(
        root, now=NOW,
    ).items()))
    daily_path.parent.mkdir(parents=True)
    payload["prior_fingerprints"] = None
    json_artifact.write_json_atomic(daily_path, payload)

    with pytest.raises(ValueError, match="runtime_state_invalid"):
        runtime_maintenance._read_daily_metric(daily_path)
    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.prune(root, now=NOW)


def test_daily_metric_rejects_nested_unapproved_fields(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    metrics = _empty_daily_rollup()
    metrics["outcomes"]["overall"]["request_id"] = "must-not-appear"
    daily = root / ".metrics" / "daily" / "2026-08-22.json"
    daily.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(
        daily,
        {
            "schema_version": 1,
            "kind": "preaudit_daily_metric",
            "day": "2026-08-22",
            "generated_at": NOW.isoformat(),
            "metrics": metrics,
        },
    )

    with pytest.raises(ValueError, match="runtime_state_invalid"):
        runtime_maintenance.status(root, now=NOW)


def test_daily_metric_rejects_impossible_outcome_totals(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    metrics = _empty_daily_rollup()
    metrics["outcomes"]["overall"]["quality"]["item_covered"] = 1
    daily = root / ".metrics" / "daily" / "2026-08-22.json"
    daily.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(
        daily,
        {
            "schema_version": 1,
            "kind": "preaudit_daily_metric",
            "day": "2026-08-22",
            "generated_at": NOW.isoformat(),
            "metrics": metrics,
        },
    )

    with pytest.raises(ValueError, match="runtime_state_invalid"):
        runtime_maintenance.status(root, now=NOW)


def test_daily_metric_rejects_flow_overall_mismatch() -> None:
    metrics = _empty_daily_rollup()
    flow = runtime_maintenance._empty_outcome_metrics()
    flow["attempt_count"] = 1
    flow["closed_count"] = 1
    flow["business_results"]["pass"] = 1
    flow["quality"]["format_total"] = 1
    metrics["outcomes"]["by_flow"]["travel_expense"] = flow

    with pytest.raises(ValueError, match="runtime_state_invalid"):
        runtime_maintenance._validate_daily_rollup(metrics)


def test_prune_never_follows_daily_target_symlink(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    observability.record_event(
        expired / "observability.json",
        {
            "event_id": "close",
            "item_key": observability.attempt_key("request", "received", 1),
            "workflow_key": "travel_expense",
            "occurred_at": "2026-07-22T04:00:00Z",
            "phase": "close",
            "wall_seconds": 1,
            "active_seconds": 1,
            "queue_wait_seconds": 0,
            "result": "success",
            "model": None,
            "token_usage": {"status": "unknown"},
        },
    )
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    target = root / ".metrics" / "daily" / "2026-07-22.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    with pytest.raises(ValueError):
        runtime_maintenance.prune(root, now=NOW)

    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert expired.is_dir()


def test_recent_close_prevents_old_attempt_from_immediate_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    old = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    observability.record_attempt_phase(
        {
            "request_root": str(old),
            "request_id": "long-running-request",
            "received_at": "2026-07-22T09:00:00+08:00",
            "attempt": 1,
            "flow_key": "travel_expense",
        },
        event_id="close",
        phase="close",
        wall_seconds=1,
        active_seconds=1,
        result="success",
    )

    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    by_path = {item["relative_path"]: item for item in plan["actions"]}

    assert by_path[str(old.relative_to(root))]["action"] == "archive_path"


def test_prune_waits_for_flow_state_transition_lock(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    attempt_root = _attempt(
        root,
        case_key="a" * 64,
        attempt=1,
        age_days=1,
    )
    descriptor = os.open(attempt_root / "complete.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    results: list[object] = []

    def run_prune() -> None:
        try:
            results.append(runtime_maintenance.prune(root, now=NOW))
        except Exception as error:
            results.append(error)

    worker = threading.Thread(target=run_prune)
    worker.start()
    try:
        worker.join(timeout=0.1)
        assert worker.is_alive()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(results) == 1
    assert isinstance(results[0], dict)


def test_prune_plan_ignores_prepare_failure_receipts(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    current = _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    control = root / "control"
    control.mkdir()
    control_receipt = control / f"prepare-failure-{'0' * 64}.json"
    control_receipt.write_text(
        json.dumps({"kind": "preaudit_prepare_failure_diagnostic"})
    )
    attempt_receipt = expired / f"prepare-failure-{'1' * 64}.json"
    attempt_receipt.write_text(
        json.dumps({"kind": "preaudit_prepare_failure_diagnostic"})
    )

    plan = runtime_maintenance.build_prune_plan(root, now=NOW)

    planned = {
        action["relative_path"]: action["action"] for action in plan["actions"]
    }
    assert set(planned) == {
        str(expired.relative_to(root)),
        str(current.relative_to(root)),
    }
    assert planned[str(expired.relative_to(root))] == "delete_path"
    assert not any(path.startswith("control/") for path in planned)
    result = runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert result["deleted"] == 1
    assert not expired.exists()
    assert control_receipt.exists()


def test_prune_rejects_entire_plan_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    plan["actions"] = plan["actions"][:-1]
    plan["fingerprint"] = runtime_maintenance._plan_fingerprint(plan)

    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert expired.is_dir()


def test_prune_accepts_general_expense_managed_paths(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(
        root,
        case_key="general-expired",
        attempt=1,
        age_days=31,
        workflow_key="general_expense",
    )
    current = _attempt(
        root,
        case_key="general-current",
        attempt=1,
        age_days=1,
        workflow_key="general_expense",
    )
    unknown = _attempt(
        root,
        case_key="general-unknown",
        attempt=1,
        age_days=31,
        workflow_key="general_expense",
        write_state="unknown",
        archived=True,
    )

    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    receipt = runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert receipt["deleted"] == 1
    assert not expired.exists()
    assert current.is_dir()
    assert unknown.is_dir()


def test_prune_rejects_plan_when_attempt_appears_after_planning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    expired = _attempt(root, case_key="a" * 64, attempt=1, age_days=31)
    _attempt(root, case_key="b" * 64, attempt=1, age_days=1)
    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    _attempt(root, case_key="c" * 64, attempt=1, age_days=0)

    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert expired.is_dir()


def test_prune_rejects_resigned_extra_target(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    _attempt(root, case_key="a" * 64, attempt=1, age_days=1)
    extra = (
        root
        / "travel_expense"
        / "cases"
        / ("b" * 64)
        / "attempt-99"
    )
    extra.parent.mkdir(parents=True)
    extra.write_text("do-not-delete", encoding="utf-8")
    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    relative_path = str(extra.relative_to(root))
    plan["actions"].append(
        {
            "key": hashlib.sha256(relative_path.encode()).hexdigest(),
            "action": "delete_path",
            "reason": "expired",
            "relative_path": relative_path,
        }
    )
    plan["fingerprint"] = runtime_maintenance._plan_fingerprint(plan)

    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.apply_prune_plan(root, plan, now=NOW)

    assert extra.read_text(encoding="utf-8") == "do-not-delete"


def test_path_identity_mismatch_is_invalid_and_never_deleted(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    mismatched = _attempt(root, case_key="a" * 64, attempt=1, age_days=90)
    state_file = mismatched / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["workflow_key"] = "packaging_layout_approval"
    json_artifact.write_json_atomic(state_file, state)

    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    receipt = runtime_maintenance.apply_prune_plan(root, plan, now=NOW)
    runtime_status = runtime_maintenance.status(root, now=NOW)

    assert receipt["deleted"] == 0
    assert mismatched.is_dir()
    assert plan["actions"][0]["reason"] == "state_invalid"
    assert runtime_status["workflows"][0]["failure_counts"] == {
        "state_invalid": 1
    }


def test_retention_policy_is_fixed_and_plan_must_be_fresh(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    target = _attempt(root, case_key="a" * 64, attempt=1, age_days=1)

    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.build_prune_plan(root, now=NOW, retention_days=1)
    plan = runtime_maintenance.build_prune_plan(root, now=NOW)
    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.apply_prune_plan(
            root,
            plan,
            now=NOW + timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="retention_plan_invalid"):
        runtime_maintenance.apply_prune_plan(
            root,
            plan,
            now=NOW - timedelta(seconds=1),
        )

    assert target.is_dir()


@pytest.mark.parametrize("symlink_kind", ["marker", "cases", "attempt"])
def test_prune_rejects_relevant_symlinks_before_mutation(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    if symlink_kind == "marker":
        marker = root / retention.MARKER_NAME
        marker.unlink()
        outside_marker = outside / "marker"
        outside_marker.write_text(retention.MARKER_VALUE, encoding="utf-8")
        marker.symlink_to(outside_marker)
    elif symlink_kind == "cases":
        cases = root / "travel_expense" / "cases"
        cases.parent.mkdir(parents=True)
        cases.symlink_to(outside, target_is_directory=True)
    else:
        cases = root / "travel_expense" / "cases" / ("a" * 64)
        cases.mkdir(parents=True)
        (cases / "attempt-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        runtime_maintenance.prune(root, now=NOW)


def test_archive_collision_is_rejected_before_any_mutation(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    delete_target = root / "logs" / "expired.json"
    delete_target.parent.mkdir()
    delete_target.write_text("{}", encoding="utf-8")
    archive_source = root / "items" / "previous"
    archive_source.mkdir(parents=True)
    archive_destination = root / ".archive" / "items" / "previous"
    archive_destination.mkdir(parents=True)
    actions = [
        retention.RetentionAction(
            "delete",
            "delete_path",
            "expired",
            "logs/expired.json",
        ),
        retention.RetentionAction(
            "archive",
            "archive_path",
            "previous_task",
            "items/previous",
        ),
    ]

    with pytest.raises(ValueError, match="retention_archive_conflict"):
        retention.apply_file_actions(root, actions, now=NOW)

    assert delete_target.is_file()
    assert archive_source.is_dir()


def test_cli_status_and_prune_share_runtime_contract(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    retention.initialize_root(root)
    _attempt(root, case_key="a" * 64, attempt=1, age_days=1)
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(
            ["status", "--root", str(root)],
            runtime_status=lambda current_root: runtime_maintenance.status(
                current_root,
                now=NOW,
            ),
        )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["kind"] == "preaudit_runtime_status"
    assert stderr.getvalue() == ""

    prune_stdout = io.StringIO()
    with redirect_stdout(prune_stdout):
        prune_exit = cli.main(
            ["prune", "--root", str(root)],
            runtime_prune=lambda current_root: runtime_maintenance.prune(
                current_root,
                now=NOW,
            ),
        )

    assert prune_exit == 0
    assert json.loads(prune_stdout.getvalue())["kind"] == "preaudit_prune_receipt"
