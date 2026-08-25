from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import time
from typing import Callable, Generic, Iterable, TypeVar


Input = TypeVar("Input")
Output = TypeVar("Output")
MAX_PROCESS_POOL_CAPACITY = 12


@dataclass(frozen=True)
class TaskOutcome(Generic[Output]):
    index: int
    status: str
    value: Output | None
    error_code: str | None = None


@dataclass(frozen=True)
class PoolResult(Generic[Output]):
    requested: int
    started: int
    completed: int
    failed: int
    timed_out: int
    stopped: int
    peak_active: int
    elapsed_seconds: float
    outcomes: tuple[TaskOutcome[Output], ...]
    runtime_samples: tuple[dict, ...] = ()
    max_task_elapsed_seconds: float = 0.0
    processes_reaped: bool = True
    recommended_capacity: int | None = None
    capacity_decisions: tuple[dict, ...] = ()


@dataclass
class _Worker:
    process: BaseProcess
    connection: Connection
    task_index: int | None = None
    started_at: float | None = None


def _worker_main(
    connection: Connection,
    worker: Callable[[Input], Output],
) -> None:
    try:
        while True:
            task = connection.recv()
            if task is None:
                return
            index, item = task
            started_at = time.monotonic()
            connection.send(("started", index, started_at))
            try:
                value = worker(item)
            except Exception:
                status = "failed"
                value = None
                error_code = "worker_failed"
            else:
                status = "completed"
                error_code = None
            connection.send(
                (
                    "finished",
                    index,
                    started_at,
                    time.monotonic(),
                    status,
                    value,
                    error_code,
                )
            )
    finally:
        connection.close()


def _start_worker(
    context: multiprocessing.context.BaseContext,
    worker: Callable[[Input], Output],
) -> _Worker:
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_worker_main, args=(child, worker))
    try:
        process.start()
    except Exception:
        parent.close()
        child.close()
        raise RuntimeError("process_pool_start_failed") from None
    child.close()
    return _Worker(process=process, connection=parent)


def _close_worker(slot: _Worker, *, terminate: bool) -> None:
    if terminate and slot.process.is_alive():
        slot.process.terminate()
    slot.process.join(timeout=1)
    if slot.process.is_alive():
        slot.process.kill()
        slot.process.join(timeout=1)
    slot.connection.close()


def _stopped_outcome(index: int) -> TaskOutcome[object]:
    return TaskOutcome(
        index=index,
        status="stopped",
        value=None,
        error_code="stop_requested",
    )


def run(
    items: Iterable[Input],
    worker: Callable[[Input], Output],
    *,
    max_workers: int,
    batch_timeout_sec: float | None = None,
    task_timeout_sec: float | None = None,
    heartbeat_interval_sec: float = 30.0,
    runtime_status: Callable[[], dict] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    drain_on_stop: bool = False,
) -> PoolResult[Output]:
    tasks = tuple(items)
    if (
        not isinstance(max_workers, int)
        or isinstance(max_workers, bool)
        or not 1 <= max_workers <= MAX_PROCESS_POOL_CAPACITY
    ):
        raise ValueError("process_pool_capacity_invalid")
    if batch_timeout_sec is not None and (
        isinstance(batch_timeout_sec, bool) or float(batch_timeout_sec) <= 0
    ):
        raise ValueError("process_pool_timeout_invalid")
    if task_timeout_sec is not None and (
        isinstance(task_timeout_sec, bool) or float(task_timeout_sec) <= 0
    ):
        raise ValueError("process_pool_task_timeout_invalid")
    if (
        isinstance(heartbeat_interval_sec, bool)
        or float(heartbeat_interval_sec) <= 0
    ):
        raise ValueError("process_pool_heartbeat_invalid")
    if not isinstance(drain_on_stop, bool):
        raise ValueError("process_pool_drain_mode_invalid")
    run_started_at = time.monotonic()
    if not tasks:
        return PoolResult(
            requested=0,
            started=0,
            completed=0,
            failed=0,
            timed_out=0,
            stopped=0,
            peak_active=0,
            elapsed_seconds=round(time.monotonic() - run_started_at, 6),
            outcomes=(),
        )
    if stop_requested is not None and stop_requested():
        outcomes = tuple(_stopped_outcome(index) for index in range(len(tasks)))
        return PoolResult(
            requested=len(tasks),
            started=0,
            completed=0,
            failed=0,
            timed_out=0,
            stopped=len(tasks),
            peak_active=0,
            elapsed_seconds=round(time.monotonic() - run_started_at, 6),
            outcomes=outcomes,
        )

    context = multiprocessing.get_context("spawn")
    pending = deque(enumerate(tasks))
    outcomes: list[TaskOutcome[Output] | None] = [None] * len(tasks)
    slots: list[_Worker] = []
    try:
        for _index in range(min(max_workers, len(tasks))):
            slots.append(_start_worker(context, worker))
    except Exception:
        for slot in slots:
            _close_worker(slot, terminate=True)
        raise RuntimeError("process_pool_start_failed") from None
    started = 0
    peak_active = 0
    stop_reason: str | None = None
    task_timeout_indices: set[int] = set()
    draining = False
    runtime_samples: list[dict] = []
    max_task_elapsed_seconds = 0.0
    next_heartbeat = time.monotonic()
    deadline = (
        time.monotonic() + float(batch_timeout_sec)
        if batch_timeout_sec is not None
        else None
    )

    try:
        while pending or any(slot.task_index is not None for slot in slots):
            if (
                not draining
                and stop_requested is not None
                and stop_requested()
            ):
                if not drain_on_stop:
                    stop_reason = "stopped"
                    break
                draining = True
                while pending:
                    index, _item = pending.popleft()
                    outcomes[index] = _stopped_outcome(index)
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "timed_out"
                break

            now = time.monotonic()
            active_ages = {
                slot.task_index: max(0.0, now - slot.started_at)
                for slot in slots
                if slot.task_index is not None and slot.started_at is not None
            }
            max_task_elapsed_seconds = max(
                max_task_elapsed_seconds,
                max(active_ages.values(), default=0.0),
            )
            if (
                task_timeout_sec is not None
                and any(
                    age >= float(task_timeout_sec)
                    for age in active_ages.values()
                )
            ):
                task_timeout_indices = {
                    index
                    for index, age in active_ages.items()
                    if age >= float(task_timeout_sec)
                }
                stop_reason = "task_timed_out"
                break
            if now >= next_heartbeat:
                try:
                    host = runtime_status() if runtime_status is not None else None
                except Exception:
                    host = {"status": "unknown"}
                runtime_samples.append(
                    {
                        "elapsed_seconds": round(now - run_started_at, 3),
                        "pending_tasks": len(pending),
                        "active_workers": len(active_ages),
                        "completed_tasks": sum(
                            outcome is not None for outcome in outcomes
                        ),
                        "oldest_task_seconds": round(
                            max(active_ages.values(), default=0.0),
                            3,
                        ),
                        "host": host,
                    }
                )
                next_heartbeat = now + float(heartbeat_interval_sec)

            for slot in slots:
                if slot.task_index is not None or not pending:
                    continue
                index, item = pending.popleft()
                try:
                    slot.connection.send((index, item))
                except (BrokenPipeError, EOFError, OSError):
                    outcomes[index] = TaskOutcome(
                        index=index,
                        status="failed",
                        value=None,
                        error_code="worker_process_failed",
                    )
                    continue
                slot.task_index = index
                slot.started_at = time.monotonic()
                started += 1
            peak_active = max(
                peak_active,
                sum(slot.task_index is not None for slot in slots),
            )

            progressed = False
            for slot_index, slot in tuple(enumerate(slots)):
                while slot.connection.poll():
                    progressed = True
                    try:
                        message = slot.connection.recv()
                    except (EOFError, OSError):
                        break
                    if message[0] == "started":
                        if slot.task_index == message[1]:
                            slot.started_at = message[2]
                        continue
                    (
                        _kind,
                        index,
                        _started_at,
                        _ended_at,
                        status,
                        value,
                        error_code,
                    ) = message
                    if slot.task_index != index:
                        continue
                    outcomes[index] = TaskOutcome(
                        index=index,
                        status=status,
                        value=value,
                        error_code=error_code,
                    )
                    slot.task_index = None
                    slot.started_at = None

                if slot.process.is_alive():
                    continue
                progressed = True
                failed_index = slot.task_index
                _close_worker(slot, terminate=False)
                if failed_index is not None and outcomes[failed_index] is None:
                    outcomes[failed_index] = TaskOutcome(
                        index=failed_index,
                        status="failed",
                        value=None,
                        error_code="worker_process_failed",
                    )
                slots[slot_index] = _start_worker(context, worker)

            if not progressed:
                time.sleep(0.005)
    finally:
        if stop_reason is None:
            for slot in slots:
                if slot.process.is_alive():
                    try:
                        slot.connection.send(None)
                    except (BrokenPipeError, EOFError, OSError):
                        pass
                _close_worker(slot, terminate=False)
        else:
            terminal_status = (
                "timed_out"
                if stop_reason in {"timed_out", "task_timed_out"}
                else "stopped"
            )
            error_code = (
                "worker_timeout"
                if stop_reason == "timed_out"
                else "worker_cancelled_after_timeout"
                if stop_reason == "task_timed_out"
                else "stop_requested"
            )
            for slot in slots:
                if slot.task_index is not None and outcomes[slot.task_index] is None:
                    outcomes[slot.task_index] = TaskOutcome(
                        index=slot.task_index,
                        status=terminal_status,
                        value=None,
                        error_code=(
                            "worker_task_timeout"
                            if slot.task_index in task_timeout_indices
                            else error_code
                        ),
                    )
                _close_worker(slot, terminate=True)
            while pending:
                index, _item = pending.popleft()
                outcomes[index] = TaskOutcome(
                    index=index,
                    status=terminal_status,
                    value=None,
                    error_code=error_code,
                )

    completed_outcomes = tuple(
        outcome for outcome in outcomes if outcome is not None
    )
    return PoolResult(
        requested=len(tasks),
        started=started,
        completed=sum(
            outcome.status == "completed" for outcome in completed_outcomes
        ),
        failed=sum(outcome.status == "failed" for outcome in completed_outcomes),
        timed_out=sum(
            outcome.status == "timed_out" for outcome in completed_outcomes
        ),
        stopped=sum(outcome.status == "stopped" for outcome in completed_outcomes),
        peak_active=peak_active,
        elapsed_seconds=round(time.monotonic() - run_started_at, 6),
        outcomes=completed_outcomes,
        runtime_samples=tuple(runtime_samples),
        max_task_elapsed_seconds=round(max_task_elapsed_seconds, 3),
    )


def summarize(result: PoolResult[object]) -> dict:
    error_counts: dict[str, int] = {}
    for outcome in result.outcomes:
        if outcome.error_code is None:
            continue
        error_counts[outcome.error_code] = error_counts.get(outcome.error_code, 0) + 1
    return {
        "schema_version": 1,
        "kind": "preaudit_process_pool_summary",
        "requested": result.requested,
        "started": result.started,
        "completed": result.completed,
        "failed": result.failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "error_counts": dict(sorted(error_counts.items())),
    }
