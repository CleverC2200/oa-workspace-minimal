from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re

import pytest

from apps import json_artifact
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import cli, controller_handoff, process_pool
from scripts import finance_preaudit_concurrency_probe


def _live_status(pending: int = 0) -> dict:
    return {
        "credentials": {"status": "ok"},
        "oa": {
            "execution_identity": "verified",
            "by_workflow": {"travel_expense": pending},
        },
    }


def _reconciliation(pending: int = 0) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_reconciliation",
        "read_only": True,
        "quarantine": {
            "pending_quarantined": 0,
            "actionable_pending": pending,
        },
    }


def test_completed_controller_run_publishes_sanitized_handoff_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "control" / "rolling-controller.lock"),
    )
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", "0")

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=_live_status,
        reconcile_status=_reconciliation,
    )

    handoff = result["controller_handoff"]
    assert handoff["status"] == "completed"
    assert handoff["restart_mode"] == "fresh_process"
    assert handoff["controller_lock_verified"] is True
    assert handoff["writer_inactive_at_start"] is True
    assert handoff["children_reaped"] is True
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", handoff["config_fingerprint"])
    assert handoff["code_revision"] == "unknown" or re.fullmatch(
        r"[0-9a-f]{40}", handoff["code_revision"]
    )

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["status", "--root", str(runtime_root)],
            runtime_status=lambda _root: {
                "schema_version": 1,
                "kind": "preaudit_runtime_status",
            },
        )

    assert exit_code == 0
    public_handoff = json.loads(output.getvalue())["controller_handoff"]
    assert public_handoff == {
        "active_request_writers": 0,
        "automation_state": "not_checked",
        "children_reaped": True,
        "code_matches_current": True,
        "code_revision": handoff["code_revision"],
        "config_fingerprint": handoff["config_fingerprint"],
        "controller_active": False,
        "kind": "preaudit_controller_handoff_status",
        "health": "healthy",
        "lock_owner_consistent": True,
        "manifest_state": "completed",
        "ready_for_start": False,
        "restart_mode": "fresh_process",
        "restart_verified": True,
        "schema_version": 1,
        "status": "blocked",
        "writer_active": False,
    }


def test_stale_running_manifest_blocks_a_new_controller(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    manifest_file = lock_file.with_name("rolling-controller-manifest.json")
    manifest_file.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(
        manifest_file,
        {
            "schema_version": 1,
            "kind": "preaudit_controller_run_manifest",
            "state": "running",
            "run_id": "0" * 32,
            "started_at": "2026-08-25T00:00:00+00:00",
            "completed_at": None,
            "controller_pid": 999999,
            "controller_instance_id": "1" * 32,
            "previous_run_id": None,
            "previous_controller_instance_id": None,
            "code_revision": "0" * 40,
            "code_dirty": False,
            "config_fingerprint": "sha256:" + "0" * 64,
            "restart_mode": "fresh_process",
            "controller_lock_verified": True,
            "writer_inactive_at_start": True,
            "children_reaped": False,
            "restart_verified": True,
            "health": "unknown",
        },
    )

    with pytest.raises(ValueError, match="controller_handoff_untrusted"):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=_live_status,
            reconcile_status=_reconciliation,
        )

    assert runtime_control.controller_active() is False
    assert runtime_control.writer_active() is False


def test_unreaped_pipeline_blocks_terminal_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "control" / "rolling-controller.lock"),
    )
    pending = iter((1, 0, 0))
    current = [0]

    def live() -> dict:
        current[0] = next(pending)
        return _live_status(current[0])

    with pytest.raises(
        ValueError,
        match="production_controller_children_active",
    ):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=live,
            reconcile_status=lambda: _reconciliation(current[0]),
            pipeline_runner=lambda count, capacity: process_pool.PoolResult(
                requested=count,
                started=count,
                completed=count,
                failed=0,
                timed_out=0,
                stopped=0,
                peak_active=capacity,
                elapsed_seconds=1,
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
                processes_reaped=False,
            ),
        )

    assert runtime_control.stop_requested() is True


def test_terminal_handoff_requires_a_fresh_controller_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "control" / "rolling-controller.lock"),
    )
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", "0")
    finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=_live_status,
        reconcile_status=_reconciliation,
    )

    with pytest.raises(ValueError, match="controller_handoff_untrusted"):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=_live_status,
            reconcile_status=_reconciliation,
        )

    monkeypatch.setattr(controller_handoff, "CONTROLLER_INSTANCE_ID", "f" * 32)
    resumed = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        live_status=_live_status,
        reconcile_status=_reconciliation,
    )
    assert resumed["controller_handoff"]["restart_verified"] is True


def test_manifest_write_rejects_a_symlink_inserted_after_parent_pin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    manifest_file = lock_file.with_name("rolling-controller-manifest.json")
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text('{"sentinel":true}', encoding="utf-8")
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    real_open_directory = runtime_control._open_directory_nofollow
    inserted = False

    def pin_then_insert(path: Path, *, create: bool = True) -> int:
        nonlocal inserted
        descriptor = real_open_directory(path, create=create)
        if Path(path) == manifest_file.parent and not inserted:
            inserted = True
            manifest_file.symlink_to(sentinel)
        return descriptor

    monkeypatch.setattr(
        runtime_control,
        "_open_directory_nofollow",
        pin_then_insert,
    )
    with runtime_control.global_controller_lease():
        with pytest.raises(ValueError, match="controller_handoff_untrusted"):
            controller_handoff.begin({"capacity": 6})

    assert inserted is True
    assert sentinel.read_text(encoding="utf-8") == '{"sentinel":true}'
