from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time
import threading

import pytest

from apps.oa_finance_preaudit import concurrency_control as control
from scripts import finance_preaudit_concurrency_probe as probe


def test_cpu_pressure_is_not_a_critical_memory_failure():
    assert control.sample_local_health(memory_reader=lambda: 44,
        load_reader=lambda: (27.41, 0, 0), cpu_reader=lambda: 10)["status"] == "pressure"
    assert control.sample_local_health(memory_reader=lambda: 11,
        load_reader=lambda: (0, 0, 0), cpu_reader=lambda: 10)["status"] == "critical"


def test_pressure_can_reduce_to_one_and_recover_in_steps():
    wave = dict(requested=2, completed=2, failed=0, timed_out=0, stopped=0,
                codex_event_status="recorded", transient_error_events=0)
    capacity = 6
    for expected in (2, 1, 1):
        result = control.decide_capacity(current_capacity=capacity, healthy_windows=0,
            wave=wave, host={"status": "pressure"})
        assert result["next_capacity"] == expected
        assert result["action"] != "stop"
        capacity = expected
    first = control.decide_capacity(current_capacity=1, healthy_windows=0,
        wave=wave, host={"status": "healthy"})
    second = control.decide_capacity(current_capacity=1, healthy_windows=first["healthy_windows"],
        wave=wave, host={"status": "healthy"})
    assert second["next_capacity"] == 2


def test_sustained_cpu_pressure_keeps_fifo_and_does_not_handoff(tmp_path: Path, monkeypatch):
    from tests.finance_preaudit.test_concurrency_probe import _open_action
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    paths = [_open_action(tmp_path / "runtime" / "finance_preaudit_flows",
        case_key=f"pressure-{i}", created_at=f"2026-08-25T02:01:0{i}+00:00") for i in range(4)]
    completed = []
    def review(task):
        time.sleep(0.04)
        return {"status": "reviewed", "business_state": "not_available", "write_state": "not_requested",
                "process_attempts": 1, "process_retries": 0, "token_usage": {"status": "unknown"},
                "agent_wall_seconds": 0, "decision": {"kind": "preaudit_review_decision", "overall": "pass"}}
    def complete(path, reviewed):
        assert reviewed["decision"]["kind"] == "preaudit_review_decision"
        completed.append(path)
        return {**reviewed, "status": "completed", "business_state": "pass", "write_state": "submitted", "error_code": None}
    result = probe._run_dispatched_tasks(4, 6, project_root=tmp_path, adaptive=True,
        heartbeat_interval_sec=0.01, renew_actions=lambda p: p, review_worker=review,
        complete_review=complete, executor_factory=lambda n: ThreadPoolExecutor(max_workers=n),
        runtime_status=lambda: control.sample_local_health(memory_reader=lambda: 44,
            load_reader=lambda: (27.41, 0, 0), cpu_reader=lambda: 10))
    assert result.completed == 4
    assert result.failed == result.stopped == result.timed_out == 0
    assert completed == paths
    assert result.recommended_capacity == 1
    assert result.processes_reaped


@pytest.mark.parametrize("stop_during_wait", [False, True])
def test_unknown_health_waits_without_claim_or_write_and_can_resume(tmp_path: Path, monkeypatch, stop_during_wait):
    from tests.finance_preaudit.test_concurrency_probe import _open_action
    path = _open_action(tmp_path / "runtime" / "finance_preaudit_flows",
        case_key="resource-wait", created_at="2026-08-25T02:01:00+00:00")
    samples = 0
    calls = []
    def health():
        nonlocal samples
        samples += 1
        return {"status": "unknown" if samples < 4 or stop_during_wait else "healthy"}
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: stop_during_wait and samples >= 4)
    def renew(paths):
        assert samples >= 4
        calls.append("renew")
        return paths
    def review(task):
        calls.append("review")
        return {"status": "reviewed", "decision": {"kind": "preaudit_review_decision"},
                "process_attempts": 1, "agent_wall_seconds": 0, "token_usage": {"status": "unknown"}}
    def complete(action, value):
        assert action == path
        calls.append("write")
        return {**value, "status": "completed", "business_state": "pass", "write_state": "submitted"}
    result = probe._run_dispatched_tasks(1, 6, project_root=tmp_path, adaptive=True,
        heartbeat_interval_sec=0.01, runtime_status=health, renew_actions=renew,
        review_worker=review, complete_review=complete,
        executor_factory=lambda n: ThreadPoolExecutor(max_workers=n))
    assert any(s["resource_paused"] for s in result.runtime_samples)
    assert result.processes_reaped
    if stop_during_wait:
        assert not calls
        assert result.stopped == 1
    else:
        assert calls == ["renew", "review", "write"]
        assert result.completed == 1


def test_severe_memory_still_stops_even_if_cpu_sample_unavailable():
    assert control.sample_local_health(memory_reader=lambda: 11,
        cpu_reader=lambda: 0)["status"] == "critical"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_cpu_measurements_are_not_treated_as_healthy(value):
    assert control.sample_local_health(memory_reader=lambda: 44,
        load_reader=lambda: (value, 0, 0), cpu_reader=lambda: 10)["status"] == "unknown"


def test_retry_waits_for_resource_recovery(tmp_path: Path, monkeypatch):
    from tests.finance_preaudit.test_concurrency_probe import _open_action
    _open_action(tmp_path / "runtime" / "finance_preaudit_flows", case_key="retry-wait",
        created_at="2026-08-25T02:01:00+00:00")
    phase = "initial"
    wait_samples = 0
    review_count = 0
    write_count = 0
    unknown_seen = threading.Event()
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    def health():
        nonlocal phase, wait_samples
        if phase in {"writer", "waiting"}:
            unknown_seen.set()
            if phase == "waiting":
                wait_samples += 1
                if wait_samples >= 3:
                    phase = "recovered"
            return {"status": "healthy" if phase == "recovered" else "unknown"}
        return {"status": "healthy"}
    def review(task):
        nonlocal review_count
        review_count += 1
        if review_count == 2:
            assert phase == "recovered"
        return {"status": "reviewed", "decision": {}, "process_attempts": 1,
                "agent_wall_seconds": 0, "token_usage": {"status": "unknown"}}
    def complete(action, value):
        nonlocal phase, write_count
        write_count += 1
        if write_count == 1:
            phase = "writer"
            assert unknown_seen.wait(1)
            phase = "waiting"
            return {**value, "status": "retry_required", "error_code": "codex_decision_invalid"}
        return {**value, "status": "completed", "business_state": "pass", "write_state": "submitted"}
    result = probe._run_dispatched_tasks(1, 6, project_root=tmp_path, adaptive=True,
        heartbeat_interval_sec=0.01, runtime_status=health, renew_actions=lambda p: p,
        review_worker=review, complete_review=complete,
        executor_factory=lambda n: ThreadPoolExecutor(max_workers=n))
    assert result.completed == 1
    assert review_count == write_count == 2


def test_prepared_work_waits_until_existing_reviews_drain_after_decrease(tmp_path: Path, monkeypatch):
    from tests.finance_preaudit.test_concurrency_probe import _open_action
    monkeypatch.setattr(probe.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(probe, "_preparation_executor", lambda: ThreadPoolExecutor(max_workers=1))
    prepare_count = 0
    pressure_samples = 0
    prepared_order = {}
    active = 0
    sixth_started = False
    release_preparation = threading.Event()
    release_reviews = threading.Event()
    guard = threading.Lock()
    def prepare(selection):
        nonlocal prepare_count
        prepare_count += 1
        index = prepare_count
        if index == 6:
            assert release_preparation.wait(2)
        path = _open_action(tmp_path / "runtime" / "finance_preaudit_flows",
            case_key=f"prepared-{index}", created_at=f"2026-08-25T02:01:0{index}+00:00")
        prepared_order[path] = index
        return {"action": "review_required", "action_file": str(path)}
    def health():
        nonlocal pressure_samples
        if prepare_count >= 6:
            pressure_samples += 1
            release_preparation.set()
            if pressure_samples >= 5:
                release_reviews.set()
            return {"status": "pressure"}
        return {"status": "healthy"}
    def review(task):
        nonlocal active, sixth_started
        with guard:
            active += 1
            if prepared_order[Path(task["action_file"])] == 6:
                assert release_reviews.is_set()
                assert active == 1
                sixth_started = True
        assert release_reviews.wait(2)
        with guard:
            active -= 1
        return {"status": "reviewed", "decision": {}, "process_attempts": 1,
                "agent_wall_seconds": 0, "token_usage": {"status": "unknown"}}
    result = probe._run_dispatched_tasks(6, 6, project_root=tmp_path, adaptive=True,
        heartbeat_interval_sec=0.01, runtime_status=health, prepare_action=prepare,
        renew_actions=lambda p: p, review_worker=review,
        complete_review=lambda p,v: {**v, "status": "completed", "business_state": "pass", "write_state": "submitted"},
        executor_factory=lambda n: ThreadPoolExecutor(max_workers=n))
    assert result.completed == 6
    assert sixth_started
    assert result.recommended_capacity == 1
