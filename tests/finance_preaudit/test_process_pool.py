from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from apps.oa_finance_preaudit import flow_engine, process_pool
from apps.oa_approval import resource_slots


def _timed_worker(task: tuple[str, float]) -> dict:
    name, delay = task
    started_at = time.monotonic()
    time.sleep(delay)
    return {
        "name": name,
        "pid": os.getpid(),
        "started_at": started_at,
        "ended_at": time.monotonic(),
    }


def _sometimes_fails(name: str) -> str:
    if name == "bad":
        raise RuntimeError("sensitive worker detail")
    return name


def _sleep_worker(delay: float) -> float:
    time.sleep(delay)
    return delay


def _must_not_run(_value: str) -> str:
    raise AssertionError("worker must not start")


def _crashing_worker(name: str) -> str:
    if name == "crash":
        os._exit(17)
    return name


def _slotted_worker(task: tuple[str, float]) -> dict:
    root, delay = task
    with resource_slots.acquire(
        "travel-browser",
        capacity=2,
        timeout_sec=2,
        root=Path(root),
    ):
        started_at = time.monotonic()
        time.sleep(delay)
        return {
            "started_at": started_at,
            "ended_at": time.monotonic(),
        }


def _interval_peak(intervals: list[tuple[float, float]]) -> int:
    active = 0
    peak = 0
    events = [
        event
        for started_at, ended_at in intervals
        for event in ((started_at, 1), (ended_at, -1))
    ]
    for _occurred_at, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _flow_action_worker(root: str) -> dict:
    def collect(request_id: str, _attempt_root: Path) -> dict:
        time.sleep(0.08)
        return {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": "travel_expense",
            "workflow_name": "差旅费报销流程",
            "declared_fields": [],
            "resources": [],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [],
            },
            "authority_facts": [],
            "warnings": [],
        }

    engine = flow_engine.FlowEngine(
        root=Path(root),
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-23T12:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )
    action = engine.next_action("travel_expense")
    return {
        "action": action["action"],
        "case_key": action.get("case_key"),
        "action_file": action.get("action_file"),
    }


def test_process_pool_enforces_capacity_and_preserves_input_order() -> None:
    result = process_pool.run(
        [("first", 0.12), ("second", 0.12), ("third", 0.01)],
        _timed_worker,
        max_workers=2,
    )

    assert result.requested == 3
    assert result.started == 3
    assert result.completed == 3
    assert result.failed == 0
    assert result.timed_out == 0
    assert result.peak_active == 2
    assert [outcome.value["name"] for outcome in result.outcomes] == [
        "first",
        "second",
        "third",
    ]
    assert len({outcome.value["pid"] for outcome in result.outcomes}) == 2


def test_worker_failure_is_isolated_and_sanitized() -> None:
    result = process_pool.run(
        ["first", "bad", "third"],
        _sometimes_fails,
        max_workers=2,
    )

    assert result.completed == 2
    assert result.failed == 1
    assert [outcome.status for outcome in result.outcomes] == [
        "completed",
        "failed",
        "completed",
    ]
    assert result.outcomes[1].value is None
    assert result.outcomes[1].error_code == "worker_failed"
    assert "sensitive worker detail" not in repr(result)


def test_pool_summary_contains_only_batch_level_monitoring_facts() -> None:
    result = process_pool.run(
        ["first", "bad", "third"],
        _sometimes_fails,
        max_workers=2,
    )

    summary = process_pool.summarize(result)

    assert summary == {
        "schema_version": 1,
        "kind": "preaudit_process_pool_summary",
        "requested": 3,
        "started": 3,
        "completed": 2,
        "failed": 1,
        "timed_out": 0,
        "stopped": 0,
        "peak_active": 2,
        "elapsed_seconds": result.elapsed_seconds,
        "error_counts": {"worker_failed": 1},
    }
    encoded = json.dumps(summary, ensure_ascii=False)
    assert "first" not in encoded
    assert "third" not in encoded
    assert "sensitive worker detail" not in encoded


def test_batch_timeout_terminates_pending_workers_and_pool_can_restart() -> None:
    started_at = time.monotonic()
    timed_out = process_pool.run(
        [0.4, 0.4],
        _sleep_worker,
        max_workers=2,
        batch_timeout_sec=0.05,
    )

    assert time.monotonic() - started_at < 0.3
    assert timed_out.completed == 0
    assert timed_out.timed_out == 2
    assert [outcome.status for outcome in timed_out.outcomes] == [
        "timed_out",
        "timed_out",
    ]
    assert all(outcome.error_code == "worker_timeout" for outcome in timed_out.outcomes)

    recovered = process_pool.run(
        [0.01],
        _sleep_worker,
        max_workers=1,
        batch_timeout_sec=1,
    )
    assert recovered.completed == 1
    assert recovered.timed_out == 0


def test_task_timeout_terminates_stuck_worker_and_emits_runtime_heartbeat() -> None:
    timed_out = process_pool.run(
        [0.4],
        _sleep_worker,
        max_workers=1,
        task_timeout_sec=0.05,
        heartbeat_interval_sec=0.01,
        runtime_status=lambda: {
            "status": "healthy",
            "memory_free_percent": 40,
        },
    )

    assert timed_out.completed == 0
    assert timed_out.timed_out == 1
    assert timed_out.outcomes[0].error_code == "worker_task_timeout"
    assert timed_out.runtime_samples
    assert timed_out.runtime_samples[-1]["active_workers"] == 1
    assert timed_out.runtime_samples[-1]["host"]["status"] == "healthy"

    recovered = process_pool.run(
        [0.01],
        _sleep_worker,
        max_workers=1,
        task_timeout_sec=1,
    )
    assert recovered.completed == 1


def test_crashed_process_is_classified_and_other_tasks_continue() -> None:
    result = process_pool.run(
        ["first", "crash", "third"],
        _crashing_worker,
        max_workers=2,
        batch_timeout_sec=1,
    )

    assert result.completed == 2
    assert result.failed == 1
    assert result.timed_out == 0
    assert result.outcomes[1].status == "failed"
    assert result.outcomes[1].error_code == "worker_process_failed"
    assert [outcome.value for outcome in result.outcomes] == ["first", None, "third"]


def test_preexisting_stop_request_does_not_start_workers() -> None:
    result = process_pool.run(
        ["first", "second"],
        _must_not_run,
        max_workers=2,
        stop_requested=lambda: True,
    )

    assert result.requested == 2
    assert result.started == 0
    assert result.stopped == 2
    assert result.failed == 0
    assert all(outcome.status == "stopped" for outcome in result.outcomes)
    assert all(outcome.error_code == "stop_requested" for outcome in result.outcomes)


def test_stop_request_terminates_unfinished_workers_and_pool_can_restart() -> None:
    started_at = time.monotonic()
    stopped = process_pool.run(
        [0.4, 0.4],
        _sleep_worker,
        max_workers=2,
        batch_timeout_sec=1,
        stop_requested=lambda: time.monotonic() - started_at >= 0.05,
    )

    assert time.monotonic() - started_at < 0.3
    assert stopped.completed == 0
    assert stopped.stopped == 2
    assert all(outcome.status == "stopped" for outcome in stopped.outcomes)

    recovered = process_pool.run(
        [0.01],
        _sleep_worker,
        max_workers=1,
        batch_timeout_sec=1,
    )
    assert recovered.completed == 1


def test_drain_on_stop_finishes_active_tasks_without_starting_pending() -> None:
    started_at = time.monotonic()
    result = process_pool.run(
        [0.2] * 6,
        _sleep_worker,
        max_workers=2,
        stop_requested=lambda: time.monotonic() - started_at >= 0.05,
        drain_on_stop=True,
    )

    assert result.started == 2
    assert result.completed == 2
    assert result.stopped == 4
    assert [outcome.status for outcome in result.outcomes].count("completed") == 2
    assert [outcome.status for outcome in result.outcomes].count("stopped") == 4


def test_process_pool_respects_cross_process_resource_capacity(
    tmp_path: Path,
) -> None:
    result = process_pool.run(
        [(str(tmp_path), 0.08)] * 4,
        _slotted_worker,
        max_workers=4,
        batch_timeout_sec=2,
    )

    intervals = [
        (outcome.value["started_at"], outcome.value["ended_at"])
        for outcome in result.outcomes
    ]
    assert result.completed == 4
    assert result.peak_active == 4
    assert _interval_peak(intervals) == 2


@pytest.mark.parametrize("capacity", [1, 2, 4, 8, 10, 12])
def test_process_pool_is_stable_across_repeated_capacity_rounds(
    capacity: int,
) -> None:
    for round_index in range(2):
        tasks = [
            (f"{round_index}-{task_index}", 0.02)
            for task_index in range(capacity * 2)
        ]
        result = process_pool.run(
            tasks,
            _timed_worker,
            max_workers=capacity,
            batch_timeout_sec=5,
        )

        assert result.completed == len(tasks)
        assert result.failed == 0
        assert result.timed_out == 0
        assert result.stopped == 0
        assert result.peak_active == capacity
        assert len({outcome.value["pid"] for outcome in result.outcomes}) == capacity


@pytest.mark.parametrize("capacity", [0, 13, True])
def test_process_pool_rejects_capacity_outside_local_safety_limit(
    capacity: int,
) -> None:
    with pytest.raises(ValueError, match="process_pool_capacity_invalid"):
        process_pool.run([], _must_not_run, max_workers=capacity)


def test_partial_pool_start_failure_closes_started_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        alive = True
        terminated = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.alive = False
            self.terminated = True

        def join(self, timeout: float) -> None:
            del timeout

        def kill(self) -> None:
            self.alive = False

    class FakeConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    fake_process = FakeProcess()
    fake_connection = FakeConnection()
    calls = 0

    def fail_second_start(_context: object, _worker: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sensitive process start detail")
        return process_pool._Worker(fake_process, fake_connection)

    monkeypatch.setattr(process_pool, "_start_worker", fail_second_start)

    with pytest.raises(RuntimeError, match="^process_pool_start_failed$") as raised:
        process_pool.run(["first", "second"], _must_not_run, max_workers=2)

    assert "sensitive process start detail" not in str(raised.value)
    assert fake_process.terminated is True
    assert fake_connection.closed is True


def test_same_candidate_has_one_attempt_across_process_workers(
    tmp_path: Path,
) -> None:
    result = process_pool.run(
        [str(tmp_path), str(tmp_path)],
        _flow_action_worker,
        max_workers=2,
        batch_timeout_sec=3,
    )

    actions = [outcome.value for outcome in result.outcomes]
    assert result.completed == 2
    assert {action["action"] for action in actions} == {"review_required"}
    assert len({action["case_key"] for action in actions}) == 1
    assert len({action["action_file"] for action in actions}) == 1
    attempts = list(
        (tmp_path / "travel_expense" / "cases").glob("*/attempt-*")
    )
    assert len(attempts) == 1
