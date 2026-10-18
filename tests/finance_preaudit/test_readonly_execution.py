from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from functools import partial
import multiprocessing
import json
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker
from apps.oa_approval import auth
from apps.oa_finance_preaudit import approval_lifecycle, controller_handoff
from apps.oa_approval import runtime_control
from scripts import finance_preaudit_concurrency_probe as probe
from tests.finance_preaudit.review_profiles import retained_deepseek_cli
from tests.finance_preaudit.test_codex_review_worker import _task
from tests.finance_preaudit.process_helpers import (
    _install_fake_codex_tree,
    _codex_task,
    _flow_action_worker,
    _slotted_worker,
    _interval_peak,
    _assert_fake_codex_tree_reaped,
)


@pytest.fixture(autouse=True)
def reject_live_dependencies(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("readonly_test_live_dependency_forbidden")

    monkeypatch.setattr(probe.cli, "_live_flow_engine", reject)
    monkeypatch.setattr(auth, "load_local_env", reject)
    monkeypatch.setattr(probe.session_alert, "send_message", reject)


def test_readonly_probe_preexisting_stop_does_not_start_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: [tmp_path / "action.json"])
    monkeypatch.setattr(probe, "_process_executor", lambda _: pytest.fail("started_after_stop"))
    result = probe.run_read_only_probe(
        capacity=1,
        sample_size=1,
        project_root=tmp_path,
        run_reviews=partial(probe._run_read_only_reviews, stop_requested=lambda: True),
    )
    assert result["completed"] == 0
    assert result["stopped"] == 1
    assert result["peak_active"] == 0
    assert result["processes_reaped"] is True


def _registered_crash_or_healthy_review(task):
    from apps.oa_finance_preaudit import approval_guard as guard
    from apps.oa_approval import runtime_control
    root, action = Path(task["project_root"]), Path(task["action_file"])
    healthy = "healthy" in action.parts
    command = [sys.executable, "-c", "import time; time.sleep(2)"] if healthy else ["codex"]
    with guard.model_slot(action, root, 30) as slot:
        with runtime_control.stop_control_lease():
            process = subprocess.Popen(command, start_new_session=True)
            guard.register_model(slot, process.pid, command)
        if healthy:
            (root / "healthy-started").write_text("ready")
            process.wait(timeout=15)
        else:
            deadline = time.monotonic() + 10
            while not (root / "healthy-started").exists() or not (root / "codex-tree-pids.txt").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture_model_not_started")
                time.sleep(.01)
            os._exit(17)
    return {"status": "completed", "business_state": "pass", "write_state": "not_requested"}


def test_readonly_crashed_worker_reaps_own_model_and_keeps_registered_sibling(tmp_path, monkeypatch):
    actions = [_task(tmp_path / name)[1] for name in ("crash", "healthy")]
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: actions)
    monkeypatch.setattr(probe.codex_review_worker, "run_read_only", _registered_crash_or_healthy_review)
    pid_file, fake_codex, marker = _install_fake_codex_tree(tmp_path, monkeypatch, ignore_child_sigterm=False)
    result = probe.run_read_only_probe(capacity=2, sample_size=2, project_root=tmp_path)
    _assert_fake_codex_tree_reaped(pid_file, fake_codex, marker)
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["error_counts"] == {"worker_process_failed": 1}
    assert result["processes_reaped"] is True


@pytest.mark.parametrize("mode,options,expected", [
    ("task", {"task_timeout_sec": 3}, "worker_task_timeout"),
    ("batch", {"batch_timeout_sec": 3}, "worker_timeout"),
    ("stop", {}, "stop_requested"),
])
def test_readonly_timeout_reaps_registered_real_model_tree(tmp_path, monkeypatch, mode, options, expected):
    task, action = _task(tmp_path)
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: [action])
    pid_file, fake_codex, marker = _install_fake_codex_tree(tmp_path, monkeypatch, ignore_child_sigterm=True)
    if mode == "stop":
        options["stop_requested"] = pid_file.exists
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with approval_lifecycle.run_scope(manifest, Path(task["project_root"])):
            result = probe.run_read_only_probe(
                capacity=1, sample_size=1, project_root=Path(task["project_root"]),
                run_reviews=partial(probe._run_read_only_reviews, **options),
            )
        assert approval_lifecycle.readonly_reaped(manifest)
    assert result["stopped" if mode == "stop" else "timed_out"] == 1
    assert result["error_counts"] == {expected: 1}
    assert result["processes_reaped"] is True
    assert not action.with_name("execution-receipt.json").exists()
    _assert_fake_codex_tree_reaped(pid_file, fake_codex, marker)


def _recorded_readonly_worker(task):
    time.sleep(task["delay"])
    if task.get("fail"):
        raise RuntimeError("private_worker_detail")
    return {"status": "completed", "business_state": "pass", "write_state": "not_requested"}


@pytest.mark.parametrize("failure", ["timeout", "exception"])
def test_readonly_failed_task_keeps_healthy_siblings_and_accepts_next(tmp_path, monkeypatch, failure):
    actions = [tmp_path / name / "action.json" for name in ("bad", "healthy", "next")]
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: actions)
    monkeypatch.setattr(probe.codex_review_worker, "run_read_only", _recorded_readonly_worker)
    def run(tasks, capacity):
        for index, task in enumerate(tasks):
            task.update(delay=5 if index == 0 and failure == "timeout" else .01,
                        fail=index == 0 and failure == "exception")
        return probe._run_read_only_reviews(tasks, capacity, task_timeout_sec=2)
    result = probe.run_read_only_probe(capacity=2, sample_size=3, project_root=tmp_path, run_reviews=run)
    assert result["completed"] == 2
    assert result["timed_out" if failure == "timeout" else "failed"] == 1
    assert result["business_results"] == {"pass": 2}
    assert result["peak_active"] == 2
    assert result["processes_reaped"] is True
    assert result["token_usage"]["status"] == "unknown"
    assert result["watchdog"]["runtime_samples"] > 0
    assert "private_worker_detail" not in str(result)


def test_readonly_start_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: [tmp_path / "action.json"])
    def fail(_capacity):
        raise RuntimeError("private_start_detail")
    monkeypatch.setattr(probe, "_process_executor", fail)
    result = probe.run_read_only_probe(capacity=1, sample_size=1, project_root=tmp_path)
    assert result["failed"] == 1
    assert result["error_counts"] == {"worker_start_failed": 1}
    assert result["processes_reaped"] is True
    assert "private_start_detail" not in str(result)


@pytest.mark.parametrize("trigger", ["task", "batch", "stop"])
def test_readonly_unconfirmed_cleanup_stops_admission_and_stays_failed(tmp_path, monkeypatch, trigger):
    actions = [tmp_path / name / "action.json" for name in ("slow", "next")]
    monkeypatch.setattr(probe, "_reviewable_action_files", lambda root: actions)
    monkeypatch.setattr(probe.codex_review_worker, "run_read_only", _recorded_readonly_worker)
    terminate = probe._terminate_executor
    def unconfirmed(executor, **options):
        terminate(executor, **options)
        return False
    monkeypatch.setattr(probe, "_terminate_executor", unconfirmed)
    started = []
    stop_at = time.monotonic() + .5
    def run(tasks, capacity):
        for task in tasks:
            task["delay"] = 5
        options = ({"task_timeout_sec": 2} if trigger == "task"
                   else {"batch_timeout_sec": 2} if trigger == "batch"
                   else {"stop_requested": lambda: time.monotonic() >= stop_at})
        result = probe._run_read_only_reviews(tasks, capacity, **options)
        started.append(result.started)
        return result
    result = probe.run_read_only_probe(capacity=1, sample_size=2, project_root=tmp_path, run_reviews=run)
    assert result["completed"] == 0
    assert result["failed"] == 1
    assert result["timed_out" if trigger == "batch" else "stopped"] == 1
    assert result["processes_reaped"] is False
    assert started == [1]


def test_same_candidate_has_one_attempt_across_stdlib_workers(tmp_path):
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        actions = list(executor.map(_flow_action_worker, [str(tmp_path), str(tmp_path)]))
    assert {action["action"] for action in actions} == {"review_required"}
    assert len({action["case_key"] for action in actions}) == 1
    assert len({action["action_file"] for action in actions}) == 1
    assert len(list((tmp_path / "travel_expense" / "cases").glob("*/attempt-*"))) == 1


def test_stdlib_workers_respect_shared_resource_capacity(tmp_path):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as executor:
        intervals = list(executor.map(_slotted_worker, [(str(tmp_path), .08, 0)] * 4, timeout=10))
    assert _interval_peak([(value["started_at"], value["ended_at"]) for value in intervals]) == 2


def test_inner_codex_timeout_reaps_process_group_and_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    task, action_file = _codex_task(tmp_path)
    json_artifact.write_json_atomic(
        action_file.with_name("state.json"),
        json.loads(action_file.read_text(encoding="utf-8")),
    )
    pid_file, fake_codex, marker = _install_fake_codex_tree(
        tmp_path,
        monkeypatch,
        ignore_child_sigterm=False,
    )

    result = codex_review_worker._execute_codex(
        Path(task["project_root"]),
        action_file,
        1,
    )

    assert result.error_code == "codex_process_timeout"
    assert len(list(action_file.parent.glob("review-process-*.json"))) == 1
    _assert_fake_codex_tree_reaped(pid_file, fake_codex, marker)
