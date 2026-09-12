from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
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
            "continuation": {"status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {}},
        "read_only": True,
        "quarantine": {
            "pending_quarantined": 0,
            "actionable_pending": pending,
        },
    }


def _clear_reconciliation(pending: int = 0) -> dict:
    return {
        **_reconciliation(pending),
        "continuation": {
            "status": "ready", "scope": "none", "isolated_pending": 0,
            "blocking_reasons": {},
        },
        "failure_attention": {
            "scope": "live_pending",
            "status": "clear",
            "affected_cases": 0,
            "repeated_failure_cases": 0,
            "long_wait_failure_cases": 0,
            "long_wait_threshold_seconds": 86400,
            "max_failure_attempts": 0,
            "oldest_wait_seconds": 0,
            "failure_counts": {},
        },
    }


def test_completed_controller_run_publishes_sanitized_handoff_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
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
        "health_scope": "run_result",
        "monitoring_action": "investigate_handoff",
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


def _write_failed_unreaped_manifest(
    path: Path,
    *,
    controller_pid: int = 999999,
) -> None:
    path.parent.mkdir(parents=True)
    json_artifact.write_json_atomic(
        path,
        {
            "schema_version": 1,
            "kind": "preaudit_controller_run_manifest",
            "state": "failed",
            "run_id": "0" * 32,
            "started_at": "2026-08-25T00:00:00+00:00",
            "completed_at": "2026-08-25T00:10:00+00:00",
            "controller_pid": controller_pid,
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
            "health": "degraded",
        },
    )


def test_confirmed_stale_handoff_recovery_runs_only_after_live_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    stop_file = tmp_path / "stop-preaudit.flag"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", "0")
    monkeypatch.setattr(
        controller_handoff,
        "_code_state",
        lambda: ("f" * 40, False),
    )
    _write_failed_unreaped_manifest(
        lock_file.with_name("rolling-controller-manifest.json")
    )
    runtime_control.request_cycle_stop()

    result = finance_preaudit_concurrency_probe.run_rolling_drain(
        capacity=6,
        confirm_stale_children_reaped=True,
        live_status=_live_status,
        reconcile_status=_clear_reconciliation,
    )

    assert result["status"] == "completed"
    assert result["recovered_cycle_stop"] is True
    assert runtime_control.stop_requested() is False
    assert controller_handoff.status()["children_reaped"] is True


def test_unconfirmed_stale_handoff_recovery_remains_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    stop_file = tmp_path / "stop-preaudit.flag"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_FILE", str(stop_file))
    monkeypatch.setattr(
        controller_handoff,
        "_code_state",
        lambda: ("f" * 40, False),
    )
    _write_failed_unreaped_manifest(
        lock_file.with_name("rolling-controller-manifest.json")
    )
    runtime_control.request_cycle_stop()

    with pytest.raises(
        ValueError,
        match="production_cycle_stop_recovery_blocked",
    ):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            live_status=_live_status,
            reconcile_status=_reconciliation,
        )

    assert runtime_control.stop_requested() is True
    assert controller_handoff.status()["children_reaped"] is False


def test_confirmed_stale_handoff_recovery_rejects_when_not_required(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(tmp_path / "control" / "rolling-controller.lock"),
    )
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        str(tmp_path / "stop-preaudit.flag"),
    )

    with pytest.raises(
        ValueError,
        match="production_cycle_stop_recovery_not_required",
    ):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=6,
            confirm_stale_children_reaped=True,
            live_status=_live_status,
            reconcile_status=_clear_reconciliation,
        )


def test_confirmed_stale_handoff_recovery_blocks_a_live_controller_pid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        str(tmp_path / "stop-preaudit.flag"),
    )
    monkeypatch.setattr(
        controller_handoff,
        "_code_state",
        lambda: ("f" * 40, False),
    )
    _write_failed_unreaped_manifest(
        lock_file.with_name("rolling-controller-manifest.json"),
        controller_pid=os.getpid(),
    )
    runtime_control.request_cycle_stop()

    with pytest.raises(
        ValueError,
        match="controller_handoff_recovery_blocked",
    ):
        controller_handoff.recover_confirmed_children_reaped(confirmed=True)

    assert controller_handoff.status()["children_reaped"] is False


def test_confirmed_stale_handoff_recovery_rejects_a_different_stop_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_file = tmp_path / "control" / "rolling-controller.lock"
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        str(lock_file),
    )
    monkeypatch.setenv(
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        str(tmp_path / "stop-preaudit.flag"),
    )
    monkeypatch.setattr(
        controller_handoff,
        "_code_state",
        lambda: ("f" * 40, False),
    )
    _write_failed_unreaped_manifest(
        lock_file.with_name("rolling-controller-manifest.json")
    )
    runtime_control.request_stop("writer_result_unknown")

    with pytest.raises(
        ValueError,
        match="controller_handoff_recovery_blocked",
    ):
        controller_handoff.recover_confirmed_children_reaped(confirmed=True)

    assert controller_handoff.status()["children_reaped"] is False


def test_terminal_handoff_requires_a_fresh_controller_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        finance_preaudit_concurrency_probe,
        "PROJECT_ROOT",
        tmp_path,
    )
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


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_pipeline_closes_manifest_without_claiming_reaped(monkeypatch, interrupt):
    def interrupted(count, capacity):
        raise interrupt()

    with pytest.raises(interrupt):
        finance_preaudit_concurrency_probe.run_rolling_drain(
            capacity=1,
            live_status=lambda: _live_status(1),
            reconcile_status=lambda: _reconciliation(1),
            pipeline_runner=interrupted,
        )
    state = controller_handoff.status()
    assert state["manifest_state"] == "failed"
    assert state["children_reaped"] is False
    assert state["ready_for_start"] is False
    assert runtime_control.controller_active() is False
    assert runtime_control.stop_reason() == "controller_cycle_stop"


def _orphan(monkeypatch):
    path = controller_handoff.manifest_file()
    _write_failed_unreaped_manifest(path)
    value = json.loads(path.read_text())
    value.update(state="running", completed_at=None, health="unknown")
    json_artifact.write_json_atomic(path, value)
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("0" * 40, False))
    monkeypatch.setattr(controller_handoff, "_process_alive", lambda pid: False)
    return value


def test_orphan_recovery_preserves_original_and_requires_locked_live_gates(monkeypatch):
    original = _orphan(monkeypatch)
    def check():
        assert runtime_control.controller_active()
        assert runtime_control.active_request_writers() == 1
        return {"live_check": _live_status(), "reconciliation": _clear_reconciliation()}
    result = controller_handoff.recover_orphaned_run(
        confirmed_children_reaped=True, check_live=check,
    )
    assert result["status"] == "recovered"
    assert json.loads(Path(result["evidence_file"]).read_text()) == original
    manifest = json.loads(controller_handoff.manifest_file().read_text())
    assert manifest["state"] == "failed"
    assert manifest["children_reaped"] is True
    assert manifest["health"] == "degraded"


@pytest.mark.parametrize("gate", ["confirmation", "pid", "identity", "reconciliation", "dirty"])
def test_orphan_recovery_keeps_manifest_when_gate_fails(monkeypatch, gate):
    original = _orphan(monkeypatch)
    if gate == "pid": monkeypatch.setattr(controller_handoff, "_process_alive", lambda pid: True)
    if gate == "dirty": monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("0" * 40, True))
    live = _live_status()
    reconciled = _clear_reconciliation()
    if gate == "identity": live["oa"]["execution_identity"] = "unknown"
    if gate == "reconciliation": reconciled["continuation"]["status"] = "blocked"
    with pytest.raises(ValueError, match="controller_handoff_recovery_blocked"):
        controller_handoff.recover_orphaned_run(
            confirmed_children_reaped=gate != "confirmation",
            check_live=lambda: {"live_check": live, "reconciliation": reconciled},
        )
    assert json.loads(controller_handoff.manifest_file().read_text()) == original


def test_orphan_recovery_rechecks_stop_after_live_read(monkeypatch):
    original = _orphan(monkeypatch)
    def check():
        runtime_control.request_cycle_stop()
        return {"live_check": _live_status(), "reconciliation": _clear_reconciliation()}
    with pytest.raises(ValueError, match="controller_handoff_recovery_blocked"):
        controller_handoff.recover_orphaned_run(confirmed_children_reaped=True, check_live=check)
    assert json.loads(controller_handoff.manifest_file().read_text()) == original


@pytest.mark.parametrize("owner", ["controller", "global", "request"])
def test_orphan_recovery_rejects_active_leases(monkeypatch, owner):
    original = _orphan(monkeypatch)
    lease = (runtime_control.global_controller_lease() if owner == "controller" else
             runtime_control.request_writer_lease(
                 runtime_control.GLOBAL_WRITER_KEY if owner == "global" else "example-request",
                 runtime_control.writer_lock_file(), blocking=False))
    with lease, pytest.raises(ValueError, match="controller_handoff_recovery_blocked"):
        controller_handoff.recover_orphaned_run(
            confirmed_children_reaped=True,
            check_live=lambda: {"live_check": _live_status(), "reconciliation": _clear_reconciliation()},
        )
    assert json.loads(controller_handoff.manifest_file().read_text()) == original


def test_recovery_cli_uses_live_sources_and_does_not_start_controller(monkeypatch, capsys):
    from apps.oa_finance_preaudit import controller_recovery
    _orphan(monkeypatch)
    monkeypatch.setattr(controller_recovery.runtime_doctor, "status", _live_status)
    monkeypatch.setattr(cli, "_live_reconciliation", lambda root: _clear_reconciliation())
    assert controller_recovery.main(["--confirm-children-reaped", "YES"]) == 0
    assert json.loads(capsys.readouterr().out)["write_state"] == "not_requested"
    assert not runtime_control.controller_active()
    assert controller_handoff.status()["manifest_state"] == "failed"


def test_running_unknown_result_instructs_wait_without_claiming_host_failure(monkeypatch):
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("0" * 40, False))
    with runtime_control.global_controller_lease():
        controller_handoff.begin({"capacity": 1})
        result = controller_handoff.status()
        assert result["health"] == "unknown"
        assert result["health_scope"] == "run_result"
        assert result["monitoring_action"] == "wait_for_current_run"
        assert not result["ready_for_start"]
        monkeypatch.setattr(runtime_control, "controller_owner_pid", lambda: -1)
        result = controller_handoff.status()
        assert result["status"] == "blocked"
        assert result["monitoring_action"] == "investigate_handoff"


def test_failed_recovery_cli_preserves_evidence_without_starting_approval(monkeypatch, capsys):
    from apps.oa_finance_preaudit import controller_recovery
    value = _orphan(monkeypatch)
    value.update(state="failed", completed_at="2026-08-25T00:01:00+00:00", health="degraded")
    json_artifact.write_json_atomic(controller_handoff.manifest_file(), value)
    original = controller_handoff.manifest_file().read_bytes()
    runtime_control.request_cycle_stop()
    monkeypatch.setattr(controller_recovery.runtime_doctor, "status", _live_status)
    monkeypatch.setattr(cli, "_live_reconciliation", lambda root: _clear_reconciliation())
    assert controller_recovery.main([
        "--confirm-children-reaped", "YES", "--mode", "failed",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["write_state"] == "not_requested"
    assert json.loads(Path(result["evidence_file"]).read_bytes()) == json.loads(original)
    assert Path(result["stop_evidence_file"]).read_bytes() == b"stop_requested:controller_cycle_stop\n"
    assert controller_handoff.status()["ready_for_start"]
    assert not runtime_control.stop_requested()


@pytest.mark.parametrize("gate", [
    "confirmation", "pid", "dirty", "identity", "reconciliation", "quarantine",
    "missing_stop", "manual_stop", "environment_stop", "changed_stop", "changed_manifest",
    "recent", "controller", "global", "request",
])
def test_failed_recovery_rejects_untrusted_gates(monkeypatch, gate):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    value = _orphan(monkeypatch)
    value.update(state="failed", completed_at="2026-08-25T00:01:00+00:00", health="degraded")
    if gate == "recent":
        value["completed_at"] = datetime.now(timezone.utc).isoformat()
    json_artifact.write_json_atomic(controller_handoff.manifest_file(), value)
    runtime_control.request_cycle_stop()
    if gate == "missing_stop": runtime_control.stop_file().unlink()
    if gate == "manual_stop": runtime_control.stop_file().write_text("stop_requested\n")
    if gate == "environment_stop": monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "YES")
    if gate == "pid": monkeypatch.setattr(controller_handoff, "_process_alive", lambda pid: True)
    if gate == "dirty": monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("0" * 40, True))
    live, reconciled = _live_status(), _clear_reconciliation()
    if gate == "identity": live["oa"]["execution_identity"] = "unknown"
    if gate == "reconciliation": reconciled["continuation"]["status"] = "blocked"
    if gate == "quarantine": reconciled["quarantine"]["pending_quarantined"] = 1
    def check():
        if gate == "changed_stop": runtime_control.stop_file().write_text("stop_requested\n")
        if gate == "changed_manifest":
            value["run_id"] = "2" * 32
            json_artifact.write_json_atomic(controller_handoff.manifest_file(), value)
        return {"live_check": live, "reconciliation": reconciled}
    lease = nullcontext()
    if gate == "controller": lease = runtime_control.global_controller_lease()
    if gate in {"global", "request"}:
        lease = runtime_control.request_writer_lease(
            runtime_control.GLOBAL_WRITER_KEY if gate == "global" else "test-request",
            runtime_control.writer_lock_file(), blocking=False,
        )
    with lease, pytest.raises(ValueError, match="controller_handoff_recovery_blocked"):
        controller_handoff.recover_orphaned_run(
            confirmed_children_reaped=gate != "confirmation", mode="failed", check_live=check,
        )
    assert json.loads(controller_handoff.manifest_file().read_bytes()) == value
    assert runtime_control.stop_requested() is (gate != "missing_stop")
