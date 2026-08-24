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
from apps.oa_finance_preaudit import cli, observability, retention, runtime_maintenance


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _attempt(
    root: Path,
    *,
    case_key: str,
    attempt: int,
    age_days: int,
    execution_state: str = "closed",
    business_state: str = "pass",
    write_state: str = "not_requested",
    reason: str | None = None,
    archived: bool = False,
) -> Path:
    prefix = root / ".archive" if archived else root
    attempt_root = (
        prefix
        / "travel_expense"
        / "cases"
        / case_key
        / f"attempt-{attempt}"
    )
    attempt_root.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "workflow_key": "travel_expense",
        "case_key": case_key,
        "attempt": attempt,
        "created_at": (NOW - timedelta(days=age_days)).isoformat(),
        "execution_state": execution_state,
        "business_state": business_state,
        "write_state": write_state,
    }
    if reason is not None:
        state["reason"] = reason
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
    )
    _attempt(
        root,
        case_key="f" * 64,
        attempt=2,
        age_days=1,
        execution_state="review_required",
        business_state="not_available",
        write_state="authorized",
    )

    flow = runtime_maintenance.status(root, now=NOW)["workflows"][0]

    assert flow["case_count"] == 1
    assert flow["attempt_count"] == 2
    assert flow["backlog_count"] == 1
    assert flow["execution_states"] == {"review_required": 1}
    assert flow["write_states"] == {"authorized": 1}
    assert flow["failure_counts"] == {}


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
        boundary.stem,
    }
    compacted_payload = json.loads(compacted.read_text(encoding="utf-8"))
    assert set(compacted_payload["metrics"]) == {"observability", "outcomes"}
    assert compacted_payload["metrics"]["outcomes"]["overall"][
        "evidence_status"
    ] in {"exact", "unknown"}


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
