from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3

import pytest

from apps.oa_approval import attachment_cache, runtime_control
from apps.oa_finance_preaudit import cli
from scripts import finance_preaudit_concurrency_probe


def _record(cache_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(cache_path))
    result = attachment_cache.record_attachment_file(
        request_id="request-a",
        flow_key="travel_expense",
        resource_key="resource-a",
        entry_json={"status": "cached"},
    )
    assert result["ok"] is True


def test_clean_all_cache_is_idempotent_and_preserves_business_attempts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    attempt_file = tmp_path / "runtime" / "attempt-1" / "execution-receipt.json"
    attempt_file.parent.mkdir(parents=True)
    attempt_file.write_text('{"write_state":"submitted"}', encoding="utf-8")
    _record(cache_path, monkeypatch)

    first_output = io.StringIO()
    with redirect_stdout(first_output):
        first_code = cli.main(["clean", "--all-cache"])

    assert first_code == 0
    assert json.loads(first_output.getvalue()) == {
        "deleted_entries": 1,
        "kind": "preaudit_cache_clean_receipt",
        "schema_version": 1,
        "status": "ok",
    }
    assert attachment_cache.latest_attachment_file(
        "request-a",
        "travel_expense",
        "resource-a",
    ) is None
    assert attempt_file.read_text(encoding="utf-8") == (
        '{"write_state":"submitted"}'
    )

    second_output = io.StringIO()
    with redirect_stdout(second_output):
        second_code = cli.main(["clean", "--all-cache"])

    assert second_code == 0
    assert json.loads(second_output.getvalue())["deleted_entries"] == 0


def test_clean_all_cache_persists_one_sanitized_global_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_FLOW_ROOT", str(runtime_root))
    _record(cache_path, monkeypatch)

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["clean", "--all-cache"],
            persist_invocations=True,
        )

    assert exit_code == 0
    payload = json.loads(
        (runtime_root / ".metrics" / "cli-invocations.json").read_text(
            encoding="utf-8"
        )
    )
    assert [
        (event["workflow_key"], event["command"], event["outcome"])
        for event in payload["events"]
    ] == [("global", "clean", "success")]


def test_clean_all_cache_refuses_an_active_controller(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    controller_lock = tmp_path / "rolling-controller.lock"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(controller_lock),
    )
    _record(cache_path, monkeypatch)

    output = io.StringIO()
    with runtime_control.global_controller_lease(), redirect_stdout(output):
        exit_code = cli.main(["clean", "--all-cache"])

    assert exit_code == 1
    assert json.loads(output.getvalue())["failure"] == {
        "code": "cache_clean_runtime_active",
        "stage": "clean",
    }
    assert attachment_cache.latest_attachment_file(
        "request-a",
        "travel_expense",
        "resource-a",
    ) == {"status": "cached"}


def test_clean_all_cache_refuses_an_active_global_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    _record(cache_path, monkeypatch)

    output = io.StringIO()
    with runtime_control.global_writer_lease(), redirect_stdout(output):
        exit_code = cli.main(["clean", "--all-cache"])

    assert exit_code == 1
    assert json.loads(output.getvalue())["failure"]["code"] == (
        "cache_clean_runtime_active"
    )
    assert attachment_cache.latest_attachment_file(
        "request-a", "travel_expense", "resource-a"
    ) == {"status": "cached"}


def test_clean_all_cache_does_not_follow_a_database_replaced_during_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    held_path = tmp_path / "held.sqlite3"
    sentinel_path = tmp_path / "sentinel.sqlite3"
    _record(cache_path, monkeypatch)
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(sentinel_path))
    _record(sentinel_path, monkeypatch)
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(cache_path))
    real_connect = sqlite3.connect
    swapped = False

    def replace_before_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            cache_path.rename(held_path)
            cache_path.symlink_to(sentinel_path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_before_connect)

    with pytest.raises(RuntimeError, match="cache_clean_failed"):
        attachment_cache.clean_all_cache()

    with real_connect(sentinel_path) as connection:
        assert connection.execute(
            "select count(*) from attachment_files"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "table_definition",
    (
        """
        create table attachment_files (
            request_id integer not null,
            flow_key text not null,
            resource_key text not null,
            entry_json text not null,
            updated_at text not null,
            primary key (request_id, flow_key, resource_key)
        )
        """,
        """
        create table attachment_files (
            request_id text,
            flow_key text not null,
            resource_key text not null,
            entry_json text not null,
            updated_at text not null,
            primary key (request_id, flow_key, resource_key)
        )
        """,
        """
        create table attachment_files (
            request_id text not null,
            flow_key text not null,
            resource_key text not null,
            entry_json text not null,
            updated_at text not null,
            primary key (flow_key, request_id, resource_key)
        )
        """,
    ),
    ids=("wrong-type", "missing-not-null", "wrong-primary-key"),
)
def test_clean_all_cache_refuses_a_lookalike_database_schema(
    tmp_path: Path,
    monkeypatch,
    table_definition: str,
) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(cache_path))
    with sqlite3.connect(cache_path) as connection:
        connection.execute(table_definition)
        connection.execute(
            """
            insert into attachment_files (
                request_id, flow_key, resource_key, entry_json, updated_at
            ) values (?, ?, ?, ?, ?)
            """,
            (
                "request-a",
                "travel_expense",
                "resource-a",
                '{"status":"must-survive"}',
                "2026-08-25T00:00:00+00:00",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="cache_clean_failed"):
        attachment_cache.clean_all_cache()

    with sqlite3.connect(cache_path) as connection:
        assert connection.execute(
            "select count(*) from attachment_files"
        ).fetchone() == (1,)


def test_rolling_controller_prunes_only_expired_cache_entries_at_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
    cache_path = tmp_path / "cache.sqlite3"
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(cache_path))
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    for resource_key in ("expired", "current"):
        attachment_cache.record_attachment_file(
            request_id="request-a",
            flow_key="travel_expense",
            resource_key=resource_key,
            entry_json={"status": resource_key},
        )
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "update attachment_files set updated_at = ? where resource_key = ?",
            ("2026-01-01T00:00:00+00:00", "expired"),
        )
        connection.commit()

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=lambda: {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": 0},
            },
        },
        reconcile_status=lambda: {
            "schema_version": 1,
            "kind": "preaudit_reconciliation",
            "continuation": {"status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {}},
            "read_only": True,
            "quarantine": {
                "pending_quarantined": 0,
                "actionable_pending": 0,
            },
        },
    )

    assert result["cache_maintenance"] == {
        "deleted_entries": 1,
        "kind": "preaudit_cache_maintenance_receipt",
        "schema_version": 1,
        "status": "ok",
    }
    assert attachment_cache.latest_attachment_file(
        "request-a", "travel_expense", "expired"
    ) is None
    assert attachment_cache.latest_attachment_file(
        "request-a", "travel_expense", "current"
    ) == {"status": "current"}


def test_rolling_controller_runs_cache_maintenance_at_startup_and_each_wave(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "rolling-controller.lock"),
    )
    maintenance_calls: list[int] = []
    pending_values = iter((2, 1, 0, 0))
    current_pending = [0]

    def maintain() -> dict:
        maintenance_calls.append(len(maintenance_calls) + 1)
        return {
            "schema_version": 1,
            "kind": "preaudit_cache_maintenance_receipt",
            "status": "ok",
            "deleted_entries": 0,
        }

    def live_status() -> dict:
        current_pending[0] = next(pending_values)
        return {
            "credentials": {"status": "ok"},
            "oa": {
                "execution_identity": "verified",
                "by_workflow": {"travel_expense": current_pending[0]},
            },
        }

    def pipeline_runner(count: int, capacity: int):
        return finance_preaudit_concurrency_probe.process_pool.PoolResult(
            requested=count,
            started=count,
            completed=count,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=capacity,
            elapsed_seconds=0,
            outcomes=tuple(
                finance_preaudit_concurrency_probe.process_pool.TaskOutcome(
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

    monkeypatch.setattr(attachment_cache, "prune_expired", maintain)
    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=live_status,
        reconcile_status=lambda: {
            "schema_version": 1,
            "kind": "preaudit_reconciliation",
            "continuation": {"status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {}},
            "read_only": True,
            "quarantine": {
                "pending_quarantined": 0,
                "actionable_pending": current_pending[0],
            },
        },
        pipeline_runner=pipeline_runner,
    )

    assert maintenance_calls == [1, 2, 3]
    assert len(result["cache_maintenance_waves"]) == 2
