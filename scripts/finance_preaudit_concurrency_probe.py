from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
from typing import Callable, Sequence

from apps.oa_approval import runtime_control, writer_outcomes
from apps.oa_finance_preaudit import (
    cli,
    codex_review_worker,
    concurrency_control,
    flow_engine,
    process_pool,
    reconciliation,
)
from apps.oa_finance_preaudit import runtime_doctor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RenewActions = Callable[[list[Path]], list[Path]]
ReviewRunner = Callable[[list[dict], int], process_pool.PoolResult[dict]]
PrepareRunner = Callable[[int], process_pool.PoolResult[dict]]
PrepareAction = Callable[[str], dict]
BatchRunner = Callable[..., dict]
LiveStatus = Callable[[], dict]
PipelineRunner = Callable[[int, int], process_pool.PoolResult[dict]]
ReviewWorker = Callable[[dict], dict]
ReviewCompleter = Callable[[Path, dict], dict]
ExecutorFactory = Callable[[int], object]
QUARANTINED_REASONS = {
    "material_collection_failed",
    "writer_result_unknown",
    *writer_outcomes.FAILURE_CODES,
}


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("production_probe_state_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("production_probe_state_invalid")
    return value


def _open_action_files(project_root: Path) -> list[Path]:
    flow_root = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    if not flow_root.parent.exists():
        return []
    opened: list[tuple[str, Path]] = []
    for state in reconciliation.latest_states(flow_root.parent):
        if state.get("workflow_key") != "travel_expense":
            continue
        action_file = (
            flow_root
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        if (
            state.get("execution_state") == "review_required"
            and state.get("write_state") == "authorized"
            and state.get("mode") == "controlled_write"
            and state.get("write_authorized") is True
            and state.get("workflow_key") == "travel_expense"
            and state.get("action_file") == str(action_file)
            and action_file.is_file()
            and not action_file.is_symlink()
        ):
            opened.append((str(state.get("created_at") or ""), action_file))
    return [path for _created_at, path in sorted(opened)]


def _renew_actions(action_files: list[Path]) -> list[Path]:
    engine = cli._live_flow_engine(controlled=True)
    workflow_root = (
        PROJECT_ROOT
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    renewed: list[Path] = []
    for action_file in action_files:
        action = _read_object(action_file)
        case_key = str(action.get("case_key") or "")
        try:
            with flow_engine._exclusive_lock(workflow_root / "flow.lock"):
                current = engine._resume_active_action(
                    workflow_root,
                    case_key=case_key,
                )
        except Exception as error:
            runtime_control.request_stop()
            raise ValueError("production_probe_renew_failed") from error
        if (
            current is None
            or current.get("action") != "review_required"
            or current.get("action_file") != str(action_file)
            or current.get("write_state") != "authorized"
        ):
            runtime_control.request_stop()
            raise ValueError("production_probe_renew_failed")
        renewed.append(action_file)
    return renewed


def _historical_write_authorization_failures() -> frozenset[Path]:
    flow_root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    historical: set[Path] = set()
    for state in reconciliation.latest_states(flow_root):
        if (
            state.get("workflow_key") != "travel_expense"
            or state.get("execution_state") != "needs_admin"
            or state.get("reason") != "write_authorization_invalid"
            or state.get("write_state") != "not_requested"
        ):
            continue
        action_file = (
            flow_root
            / "travel_expense"
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        if state.get("action_file") != str(action_file):
            raise ValueError("production_probe_state_invalid")
        historical.add(action_file)
    return frozenset(historical)


def _prepare_one(_workflow_key: str) -> dict:
    if runtime_control.stop_requested():
        return {"action": "failed", "error_code": "production_probe_stopped"}
    try:
        historical_authorization_failures = (
            _historical_write_authorization_failures()
        )
    except ValueError:
        runtime_control.request_stop()
        return {
            "action": "failed",
            "error_code": "production_probe_prepare_failed",
        }
    engine = cli._live_flow_engine(controlled=True)
    for _attempt in range(_local_quarantine_count() + 1):
        if runtime_control.stop_requested():
            return {
                "action": "failed",
                "error_code": "production_probe_stopped",
            }
        try:
            action = engine.next_action(
                "travel_expense",
                mode="controlled_write",
                skip_write_authorization_failures=(
                    historical_authorization_failures
                ),
            )
        except Exception:
            runtime_control.request_stop()
            return {
                "action": "failed",
                "error_code": "production_probe_prepare_failed",
            }
        if action.get("action") == "review_required":
            return {
                "action": "review_required",
                "action_file": action["action_file"],
            }
        if (
            action.get("action") == "needs_admin"
            and (
                action.get("reason") in QUARANTINED_REASONS
            )
        ):
            continue
        runtime_control.request_stop()
        return {
            "action": str(action.get("action") or "failed"),
            "error_code": str(
                action.get("reason") or "production_probe_prepare_failed"
            ),
        }
    return {
        "action": "failed",
        "error_code": "production_probe_quarantine_exhausted",
    }


def _prepare_actions(count: int) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        ["travel_expense"] * count,
        _prepare_one,
        max_workers=count,
        batch_timeout_sec=600,
        task_timeout_sec=300,
        heartbeat_interval_sec=10,
        runtime_status=concurrency_control.sample_local_health,
    )


def _run_reviews(
    tasks: list[dict],
    capacity: int,
) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        tasks,
        codex_review_worker.review,
        max_workers=capacity,
        batch_timeout_sec=1200,
        task_timeout_sec=960,
        heartbeat_interval_sec=15,
        runtime_status=concurrency_control.sample_local_health,
    )


def _reviewable_action_files(project_root: Path) -> list[Path]:
    flow_root = (
        project_root
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    if not flow_root.parent.exists():
        return []
    selected: list[Path] = []
    for state in reconciliation.latest_states(flow_root.parent):
        if (
            state.get("workflow_key") != "travel_expense"
            or state.get("execution_state") != "closed"
            or state.get("write_state") != "submitted"
            or state.get("business_state") not in {"pass", "needs_human"}
        ):
            continue
        action_file = (
            flow_root
            / "cases"
            / str(state["case_key"])
            / f"attempt-{state['attempt']}"
            / "action.json"
        )
        try:
            action = _read_object(action_file)
            review_packet_file = Path(
                str(action["review_packet_file"])
            ).resolve(strict=True)
            review_packet = _read_object(review_packet_file)
            material_file = Path(
                str(review_packet["material_file"])
            ).resolve(strict=True)
        except (KeyError, OSError, ValueError):
            continue
        attempt_root = action_file.parent.resolve()
        if (
            action.get("workflow_key") == "travel_expense"
            and action.get("action") == "closed"
            and action.get("execution_state") == "closed"
            and action.get("write_state") == "submitted"
            and action.get("action_file") == str(action_file)
            and codex_review_worker._read_completed(action_file)
            == state.get("business_state")
            and review_packet.get("workflow_key") == "travel_expense"
            and review_packet_file.is_relative_to(attempt_root)
            and material_file.is_relative_to(attempt_root)
            and not action_file.is_symlink()
            and not review_packet_file.is_symlink()
            and not material_file.is_symlink()
        ):
            selected.append(action_file.resolve())
    return sorted(selected)


def _run_read_only_reviews(
    tasks: list[dict],
    capacity: int,
) -> process_pool.PoolResult[dict]:
    return process_pool.run(
        tasks,
        codex_review_worker.run_read_only,
        max_workers=capacity,
        batch_timeout_sec=1800,
        task_timeout_sec=960,
        heartbeat_interval_sec=15,
        runtime_status=concurrency_control.sample_local_health,
    )


def _aggregate_codex_events(values: list[dict]) -> dict:
    counts: Counter[str] = Counter()
    attempts = 0
    attempts_known = True
    recorded_attempts = 0
    unknown_attempts = 0
    recorded_workers = 0
    for value in values:
        events = value.get("codex_events") if isinstance(value, dict) else None
        current_process_attempts = (
            value.get("process_attempts") if isinstance(value, dict) else None
        )
        if (
            not isinstance(current_process_attempts, int)
            or isinstance(current_process_attempts, bool)
            or current_process_attempts < 0
        ):
            attempts_known = False
            continue
        attempts += current_process_attempts
        if not codex_review_worker._valid_events(
            events,
            attempts=current_process_attempts,
        ):
            unknown_attempts += current_process_attempts
            continue
        current_recorded = events.get("recorded_attempts")
        current_unknown = events.get("unknown_attempts")
        current_counts = events.get("counts")
        if (
            isinstance(current_recorded, int)
            and not isinstance(current_recorded, bool)
            and current_recorded >= 0
        ):
            recorded_attempts += current_recorded
        if (
            isinstance(current_unknown, int)
            and not isinstance(current_unknown, bool)
            and current_unknown >= 0
        ):
            unknown_attempts += current_unknown
        if events.get("status") == "recorded":
            recorded_workers += 1
        if isinstance(current_counts, dict):
            for event_type, count in current_counts.items():
                if (
                    isinstance(event_type, str)
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                ):
                    counts[event_type] += count
    workers = len(values)
    status = (
        "recorded"
        if workers > 0 and recorded_workers == workers
        else "unknown"
    )
    return {
        "status": status,
        "workers": workers,
        "recorded_workers": recorded_workers,
        "unknown_workers": workers - recorded_workers,
        "attempts": attempts if attempts_known else None,
        "recorded_attempts": recorded_attempts,
        "unknown_attempts": unknown_attempts if attempts_known else None,
        "counts": dict(sorted(counts.items())),
    }


def _merge_codex_event_summaries(summaries: list[dict]) -> dict:
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    count_fields = (
        "workers",
        "recorded_workers",
        "unknown_workers",
        "recorded_attempts",
    )
    attempts_known = True
    for summary in summaries:
        for field in count_fields:
            value = summary.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
        for field in ("attempts", "unknown_attempts"):
            value = summary.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[field] += value
            else:
                attempts_known = False
        current_counts = summary.get("counts")
        if isinstance(current_counts, dict):
            for event_type, count in current_counts.items():
                if isinstance(event_type, str) and isinstance(count, int):
                    counts[event_type] += count
    return {
        "status": (
            "recorded"
            if totals["workers"] > 0
            and totals["recorded_workers"] == totals["workers"]
            else "unknown"
        ),
        **{field: totals[field] for field in count_fields},
        "attempts": totals["attempts"] if attempts_known else None,
        "unknown_attempts": (
            totals["unknown_attempts"] if attempts_known else None
        ),
        "counts": dict(sorted(counts.items())),
    }


def run_read_only_probe(
    *,
    capacity: int,
    sample_size: int = 20,
    project_root: Path = PROJECT_ROOT,
    run_reviews: ReviewRunner = _run_read_only_reviews,
) -> dict:
    if (
        not isinstance(capacity, int)
        or isinstance(capacity, bool)
        or not 1 <= capacity <= 12
    ):
        raise ValueError("read_only_probe_capacity_invalid")
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
        or not capacity <= sample_size <= 100
    ):
        raise ValueError("read_only_probe_sample_size_invalid")
    root = Path(project_root).expanduser().resolve()
    selected = _reviewable_action_files(root)[:sample_size]
    if len(selected) != sample_size:
        raise ValueError("read_only_probe_insufficient_samples")
    tasks = [
        {
            "project_root": str(root),
            "action_file": str(action_file),
            "timeout_seconds": 900,
        }
        for action_file in selected
    ]
    result = run_reviews(tasks, capacity)
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    process_states: Counter[str] = Counter()
    decision_states: Counter[str] = Counter()
    exit_codes: Counter[str] = Counter()
    usage_statuses: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    completed = 0
    failed = result.failed
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        if outcome.status == "completed" and value.get("status") == "completed":
            completed += 1
            business_state = value.get("business_state")
            if isinstance(business_state, str):
                business_results[business_state] += 1
        elif outcome.status == "completed":
            failed += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_states[str(value.get("process_state") or "unknown")] += 1
        decision_states[str(value.get("decision_state") or "unknown")] += 1
        exit_code = value.get("exit_code")
        exit_codes[str(exit_code) if isinstance(exit_code, int) else "none"] += 1
        usage = value.get("token_usage")
        usage_status = usage.get("status") if isinstance(usage, dict) else None
        if usage_status not in {"recorded", "partial"}:
            usage_status = "unknown"
        usage_statuses[usage_status] += 1
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
    codex_events = _aggregate_codex_events(
        [
            outcome.value if isinstance(outcome.value, dict) else {}
            for outcome in result.outcomes
        ]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "schema_version": 1,
        "kind": "preaudit_read_only_concurrency_probe",
        "capacity": capacity,
        "sample_size": sample_size,
        "completed": completed,
        "failed": failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "process_states": dict(sorted(process_states.items())),
        "decision_states": dict(sorted(decision_states.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "exit_codes": dict(sorted(exit_codes.items())),
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "production_write": False,
        "production_stop_preserved": runtime_control.stop_requested(),
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "token_usage": {
            "status": (
                "recorded"
                if usage_statuses["recorded"] == len(result.outcomes)
                else "partial"
                if usage_statuses["recorded"] or usage_statuses["partial"]
                else "unknown"
            ),
            "recorded_workers": usage_statuses["recorded"],
            "partial_workers": usage_statuses["partial"],
            "unknown_workers": usage_statuses["unknown"],
            **{
                field: token_totals[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            },
        },
    }


def run_adaptive_read_only_probe(
    *,
    start_capacity: int = 6,
    sample_size: int = 20,
    max_waves: int = 6,
    probe_runner: Callable[..., dict] = run_read_only_probe,
) -> dict:
    if (
        start_capacity not in concurrency_control.CAPACITY_STEPS
        or not isinstance(max_waves, int)
        or isinstance(max_waves, bool)
        or not 1 <= max_waves <= 20
    ):
        raise ValueError("adaptive_probe_settings_invalid")
    capacity = start_capacity
    healthy_windows = 0
    waves: list[dict] = []
    decisions: list[dict] = []
    token_totals: Counter[str] = Counter()
    status = "completed"
    for _index in range(max_waves):
        wave = probe_runner(capacity=capacity, sample_size=sample_size)
        waves.append(wave)
        usage = wave.get("token_usage")
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
        decision = concurrency_control.decide_capacity(
            current_capacity=capacity,
            healthy_windows=healthy_windows,
            wave={
                "requested": int(wave.get("sample_size") or 0),
                "completed": int(wave.get("completed") or 0),
                "failed": int(wave.get("failed") or 0),
                "timed_out": int(wave.get("timed_out") or 0),
                "stopped": int(wave.get("stopped") or 0),
                "transient_error_events": wave.get("transient_error_events"),
                "codex_event_status": (
                    wave.get("codex_events", {}).get("status")
                    if isinstance(wave.get("codex_events"), dict)
                    else "unknown"
                ),
            },
            host=(
                wave.get("runtime_health")
                if isinstance(wave.get("runtime_health"), dict)
                else {"status": "unknown"}
            ),
        )
        decisions.append(decision)
        capacity = int(decision["next_capacity"])
        healthy_windows = int(decision["healthy_windows"])
        if decision["action"] == "stop":
            status = "stopped"
            break
    codex_events = _merge_codex_event_summaries(
        [
            wave["codex_events"]
            for wave in waves
            if isinstance(wave.get("codex_events"), dict)
        ]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "schema_version": 1,
        "kind": "preaudit_adaptive_read_only_probe",
        "status": status,
        "start_capacity": start_capacity,
        "recommended_capacity": capacity,
        "waves": len(waves),
        "last_decision": decisions[-1],
        "elapsed_seconds": sum(
            float(wave.get("elapsed_seconds") or 0) for wave in waves
        ),
        "completed": sum(int(wave.get("completed") or 0) for wave in waves),
        "failed": sum(int(wave.get("failed") or 0) for wave in waves),
        "token_usage": dict(token_totals),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "production_write": False,
    }


def _review_only(task: dict) -> dict:
    return codex_review_worker.review(task)


def _process_executor(capacity: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=capacity,
        mp_context=multiprocessing.get_context("spawn"),
    )


def _preparation_executor() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=os.setsid,
    )


def _merge_review_attempts(attempts: list[dict]) -> dict:
    current = dict(attempts[-1])
    process_attempts = sum(int(item.get("process_attempts") or 0) for item in attempts)
    usages = [
        item.get("token_usage")
        for item in attempts
        if isinstance(item.get("token_usage"), dict)
    ]
    recorded_usages = [
        usage for usage in usages if usage.get("status") in {"recorded", "partial"}
    ]
    token_usage = {
        "status": (
            "recorded"
            if len(usages) == len(attempts)
            and usages
            and all(usage.get("status") == "recorded" for usage in usages)
            else "partial"
            if recorded_usages
            else "unknown"
        ),
        **{
            field: sum(int(usage.get(field) or 0) for usage in recorded_usages)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            )
        },
    }
    event_values = [
        item.get("codex_events")
        for item in attempts
        if isinstance(item.get("codex_events"), dict)
    ]
    event_counts: Counter[str] = Counter()
    recorded_events = 0
    for events in event_values:
        recorded_events += int(events.get("recorded_attempts") or 0)
        counts = events.get("counts")
        if isinstance(counts, dict):
            event_counts.update(
                {
                    str(key): int(value)
                    for key, value in counts.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
            )
    codex_events = {
        "status": (
            "recorded"
            if process_attempts > 0 and recorded_events == process_attempts
            else "unknown"
        ),
        "attempts": process_attempts,
        "recorded_attempts": recorded_events,
        "unknown_attempts": max(0, process_attempts - recorded_events),
        "counts": dict(sorted(event_counts.items())),
    }
    return {
        **current,
        "process_attempts": process_attempts,
        "process_retries": max(0, process_attempts - 1),
        "token_usage": token_usage,
        "codex_events": codex_events,
        "agent_wall_seconds": sum(
            int(item.get("agent_wall_seconds") or 0) for item in attempts
        ),
    }


def _terminate_executor(
    executor: object,
    *,
    owned_process_groups: bool = False,
) -> None:
    processes = list((getattr(executor, "_processes", None) or {}).values())
    owned_groups: set[int] = set()
    if owned_process_groups:
        for process in processes:
            if not process.is_alive() or process.pid is None:
                continue
            try:
                if os.getpgid(process.pid) == process.pid:
                    owned_groups.add(process.pid)
            except OSError:
                continue
    for process in processes:
        if process.pid in owned_groups:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        elif process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=1)
    for group in owned_groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    for process in processes:
        if process.is_alive() and process.pid not in owned_groups:
            process.kill()
        process.join(timeout=1)
    executor.shutdown(wait=False, cancel_futures=True)


def _run_dispatched_tasks(
    count: int,
    capacity: int,
    *,
    project_root: Path = PROJECT_ROOT,
    renew_actions: RenewActions = _renew_actions,
    prepare_action: PrepareAction | None = None,
    review_worker: ReviewWorker = _review_only,
    complete_review: ReviewCompleter = codex_review_worker.complete_review_attempt,
    executor_factory: ExecutorFactory = _process_executor,
    task_timeout_sec: float = 960,
    prepare_timeout_sec: float = 300,
    heartbeat_interval_sec: float = 15,
    runtime_status: Callable[[], dict] = concurrency_control.sample_local_health,
) -> process_pool.PoolResult[dict]:
    if (
        task_timeout_sec <= 0
        or prepare_timeout_sec <= 0
        or heartbeat_interval_sec <= 0
    ):
        raise ValueError("production_dispatcher_watchdog_invalid")
    started_at = time.monotonic()
    root = Path(project_root).expanduser().resolve()
    outcomes: list[process_pool.TaskOutcome[dict] | None] = [None] * count
    assigned: set[Path] = set()
    futures: dict[object, tuple[int, float, float, Path]] = {}
    review_histories: dict[int, list[dict]] = {}
    decision_retries: set[int] = set()
    next_index = 0
    started = 0
    peak_active = 0
    halted = False
    force_terminated = False
    runtime_samples: list[dict] = []
    max_task_elapsed_seconds = 0.0
    next_heartbeat = time.monotonic()
    preparation: tuple[Future[dict], int, float] | None = None
    prepare_action = prepare_action or _prepare_one
    prepare_executor = _preparation_executor()
    prepare_executor_terminated = False
    executor = executor_factory(capacity)

    def stop_reviews() -> None:
        nonlocal force_terminated
        for _future, (
            index,
            _prepare_elapsed,
            _task_started,
            _action_file,
        ) in futures.items():
            outcomes[index] = process_pool.TaskOutcome(
                index=index,
                status="stopped",
                value=None,
                error_code="stop_requested",
            )
        futures.clear()
        _terminate_executor(executor)
        force_terminated = True

    try:
        while futures or preparation is not None or (
            next_index < count and not halted
        ):
            if runtime_control.stop_requested():
                halted = True
            now = time.monotonic()
            review_ages = {
                index: max(0.0, now - task_started)
                for (
                    index,
                    _prepare_elapsed,
                    task_started,
                    _action_file,
                ) in futures.values()
            }
            active_ages = dict(review_ages)
            prepare_age = 0.0
            if preparation is not None:
                _prepare_future, prepare_index, prepare_started_at = preparation
                prepare_age = max(
                    0.0,
                    now - prepare_started_at,
                )
                active_ages[prepare_index] = prepare_age
            max_task_elapsed_seconds = max(
                max_task_elapsed_seconds,
                max(active_ages.values(), default=0.0),
            )
            timed_out_indices = {
                index
                for future, (
                    index,
                    _prepare_elapsed,
                    _task_started,
                    _action_file,
                ) in futures.items()
                if not future.done()
                and review_ages[index] >= task_timeout_sec
            }
            prepare_timed_out = (
                preparation is not None
                and not preparation[0].done()
                and prepare_age >= prepare_timeout_sec
            )
            if timed_out_indices or prepare_timed_out:
                for _future, (
                    index,
                    _prepare_elapsed,
                    _started_at,
                    _action_file,
                ) in futures.items():
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="timed_out",
                        value=None,
                        error_code=(
                            "worker_task_timeout"
                            if index in timed_out_indices
                            else "worker_cancelled_after_timeout"
                        ),
                    )
                if preparation is not None:
                    _prepare_future, prepare_index, _prepare_started_at = preparation
                    outcomes[prepare_index] = process_pool.TaskOutcome(
                        index=prepare_index,
                        status="timed_out",
                        value=None,
                        error_code=(
                            "production_probe_prepare_timeout"
                            if prepare_timed_out
                            else "worker_cancelled_after_timeout"
                        ),
                    )
                    _terminate_executor(
                        prepare_executor,
                        owned_process_groups=True,
                    )
                    prepare_executor_terminated = True
                    preparation = None
                futures.clear()
                halted = True
                runtime_control.request_stop()
                _terminate_executor(executor)
                force_terminated = True
                break
            if now >= next_heartbeat:
                try:
                    host = runtime_status()
                except Exception:
                    host = {"status": "unknown"}
                runtime_samples.append(
                    {
                        "elapsed_seconds": round(now - started_at, 3),
                        "pending_tasks": max(0, count - next_index),
                        "active_workers": len(futures)
                        + int(preparation is not None),
                        "completed_tasks": sum(
                            outcome is not None for outcome in outcomes
                        ),
                        "oldest_task_seconds": round(
                            max(active_ages.values(), default=0.0), 3
                        ),
                        "host": host,
                    }
                )
                next_heartbeat = now + heartbeat_interval_sec
                if host.get("status") == "critical":
                    halted = True
                    runtime_control.request_stop()
            if (
                halted
                and preparation is not None
                and not preparation[0].done()
            ):
                wait(
                    (preparation[0],),
                    timeout=min(0.25, prepare_timeout_sec),
                    return_when=FIRST_COMPLETED,
                )
            if (
                halted
                and preparation is not None
                and not preparation[0].done()
            ):
                _prepare_future, prepare_index, _prepare_started_at = preparation
                outcomes[prepare_index] = process_pool.TaskOutcome(
                    index=prepare_index,
                    status="stopped",
                    value=None,
                    error_code="stop_requested",
                )
                _terminate_executor(
                    prepare_executor,
                    owned_process_groups=True,
                )
                prepare_executor_terminated = True
                preparation = None
            if preparation is not None:
                prepare_future, index, prepare_started_at = preparation
                if prepare_future.done():
                    prepare_elapsed = time.monotonic() - prepare_started_at
                    try:
                        prepared = prepare_future.result()
                    except Exception:
                        prepared = {
                            "action": "failed",
                            "error_code": "production_probe_prepare_failed",
                        }
                    preparation = None
                    if prepared.get("action") != "review_required":
                        outcomes[index] = process_pool.TaskOutcome(
                            index=index,
                            status="completed",
                            value={
                                "status": "failed",
                                "business_state": "not_available",
                                "error_code": str(
                                    prepared.get("error_code")
                                    or "production_probe_prepare_failed"
                                ),
                                "prepare_elapsed_seconds": prepare_elapsed,
                                "process_attempts": 0,
                                "process_retries": 0,
                                "token_usage": {"status": "unknown"},
                            },
                        )
                        halted = True
                        runtime_control.request_stop()
                        _terminate_executor(
                            prepare_executor,
                            owned_process_groups=True,
                        )
                        prepare_executor_terminated = True
                        stop_reviews()
                        break
                    else:
                        action_file = Path(str(prepared["action_file"])).resolve()
                        if (
                            action_file in assigned
                            or runtime_control.stop_requested()
                        ):
                            outcomes[index] = process_pool.TaskOutcome(
                                index=index,
                                status="stopped",
                                value=None,
                                error_code="stop_requested",
                            )
                            halted = True
                            runtime_control.request_stop()
                        else:
                            assigned.add(action_file)
                            task = {
                                "project_root": str(root),
                                "action_file": str(action_file),
                                "timeout_seconds": 900,
                            }
                            future = executor.submit(review_worker, task)
                            futures[future] = (
                                index,
                                prepare_elapsed,
                                time.monotonic(),
                                action_file,
                            )
                            started += 1
                            peak_active = max(peak_active, len(futures))
            while (
                not halted
                and preparation is None
                and next_index < count
                and len(futures) < capacity
            ):
                prepare_started = time.monotonic()
                available = [
                    path
                    for path in _open_action_files(root)
                    if path not in assigned
                ]
                if available:
                    renewed = renew_actions([available[0]])
                    prepared = (
                        {
                            "action": "review_required",
                            "action_file": str(renewed[0]),
                        }
                        if renewed == [available[0]]
                        else {
                            "action": "failed",
                            "error_code": "production_probe_renew_failed",
                        }
                    )
                else:
                    preparation = (
                        prepare_executor.submit(
                            prepare_action,
                            "travel_expense",
                        ),
                        next_index,
                        time.monotonic(),
                    )
                    next_index += 1
                    break
                prepare_elapsed = time.monotonic() - prepare_started
                if prepared.get("action") != "review_required":
                    outcomes[next_index] = process_pool.TaskOutcome(
                        index=next_index,
                        status="completed",
                        value={
                            "status": "failed",
                            "business_state": "not_available",
                            "error_code": str(
                                prepared.get("error_code")
                                or "production_probe_prepare_failed"
                            ),
                            "prepare_elapsed_seconds": prepare_elapsed,
                            "process_attempts": 0,
                            "process_retries": 0,
                            "token_usage": {"status": "unknown"},
                        },
                    )
                    next_index += 1
                    halted = True
                    runtime_control.request_stop()
                    stop_reviews()
                    break
                action_file = Path(str(prepared["action_file"])).resolve()
                if action_file in assigned or runtime_control.stop_requested():
                    halted = True
                    break
                assigned.add(action_file)
                task = {
                    "project_root": str(root),
                    "action_file": str(action_file),
                    "timeout_seconds": 900,
                }
                future = executor.submit(review_worker, task)
                futures[future] = (
                    next_index,
                    prepare_elapsed,
                    time.monotonic(),
                    action_file,
                )
                next_index += 1
                started += 1
                peak_active = max(peak_active, len(futures))

            if not futures:
                if preparation is not None:
                    time.sleep(
                        min(
                            0.25,
                            heartbeat_interval_sec,
                            prepare_timeout_sec / 4,
                        )
                    )
                    continue
                break
            done, _pending = wait(
                tuple(futures),
                timeout=min(1.0, heartbeat_interval_sec, task_timeout_sec / 4),
                return_when=FIRST_COMPLETED,
            )
            failed_now = False
            reviewed_now: list[tuple[int, float, Path, dict]] = []
            for future in done:
                index, prepare_elapsed, _task_started, action_file = futures.pop(future)
                try:
                    value = future.result()
                except Exception:
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="failed",
                        value=None,
                        error_code="worker_process_failed",
                    )
                    halted = True
                    failed_now = True
                    continue
                if value.get("status") != "reviewed":
                    outcomes[index] = process_pool.TaskOutcome(
                        index=index,
                        status="completed",
                        value={
                            **value,
                            "prepare_elapsed_seconds": prepare_elapsed,
                        },
                    )
                    halted = True
                    failed_now = True
                    continue
                reviewed_now.append((index, prepare_elapsed, action_file, value))
            if failed_now:
                runtime_control.request_stop()
                stop_reviews()
                break
            for index, prepare_elapsed, action_file, reviewed in reviewed_now:
                merged_review = _merge_review_attempts(
                    [*review_histories.get(index, []), reviewed]
                )
                value = {
                    **complete_review(action_file, merged_review),
                    "prepare_elapsed_seconds": prepare_elapsed,
                }
                if (
                    value.get("status") == "retry_required"
                    and value.get("error_code") == "codex_decision_invalid"
                    and index not in decision_retries
                ):
                    decision_retries.add(index)
                    review_histories[index] = [merged_review]
                    future = executor.submit(
                        review_worker,
                        {
                            "project_root": str(root),
                            "action_file": str(action_file),
                            "timeout_seconds": 900,
                        },
                    )
                    futures[future] = (
                        index,
                        prepare_elapsed,
                        time.monotonic(),
                        action_file,
                    )
                    peak_active = max(peak_active, len(futures))
                    continue
                if value.get("status") == "retry_required":
                    value = {**value, "status": "failed"}
                outcomes[index] = process_pool.TaskOutcome(
                    index=index,
                    status="completed",
                    value=value,
                )
                review_histories.pop(index, None)
                if value.get("status") != "completed":
                    halted = True
                    failed_now = True
                    break
            if failed_now:
                runtime_control.request_stop()
                stop_reviews()
                break
    finally:
        if not prepare_executor_terminated:
            if preparation is not None:
                _terminate_executor(
                    prepare_executor,
                    owned_process_groups=True,
                )
            else:
                prepare_executor.shutdown(wait=True)
        if not force_terminated:
            executor.shutdown(wait=True)

    for index, outcome in enumerate(outcomes):
        if outcome is None:
            outcomes[index] = process_pool.TaskOutcome(
                index=index,
                status="stopped",
                value=None,
                error_code="stop_requested",
            )
    completed_outcomes = tuple(
        outcome for outcome in outcomes if outcome is not None
    )
    if halted and not force_terminated and not runtime_control.stop_requested():
        runtime_control.request_stop()
    return process_pool.PoolResult(
        requested=count,
        started=started,
        completed=sum(
            outcome.status == "completed" for outcome in completed_outcomes
        ),
        failed=sum(outcome.status == "failed" for outcome in completed_outcomes),
        timed_out=sum(
            outcome.status == "timed_out" for outcome in completed_outcomes
        ),
        stopped=sum(
            outcome.status == "stopped" for outcome in completed_outcomes
        ),
        peak_active=peak_active,
        elapsed_seconds=time.monotonic() - started_at,
        outcomes=completed_outcomes,
        runtime_samples=tuple(runtime_samples),
        max_task_elapsed_seconds=round(max_task_elapsed_seconds, 3),
    )


def _legacy_run_existing_batch(
    *,
    capacity: int,
    project_root: Path = PROJECT_ROOT,
    prepare_actions: PrepareRunner = _prepare_actions,
    renew_actions: RenewActions = _renew_actions,
    run_reviews: ReviewRunner = _run_reviews,
) -> dict:
    if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 8:
        raise ValueError("production_probe_capacity_invalid")
    root = Path(project_root).expanduser().resolve()
    if runtime_control.stop_requested():
        raise ValueError("production_probe_stopped")
    selected = _open_action_files(root)[:capacity]
    prepared_new = 0
    prepare_peak_active = 0
    prepare_elapsed_seconds = 0.0
    missing = capacity - len(selected)
    if missing:
        preparation = prepare_actions(missing)
        prepared_new = preparation.completed
        prepare_peak_active = preparation.peak_active
        prepare_elapsed_seconds = preparation.elapsed_seconds
        if (
            preparation.completed != missing
            or preparation.failed
            or preparation.timed_out
            or preparation.stopped
            or any(
                not isinstance(outcome.value, dict)
                or outcome.value.get("action") != "review_required"
                for outcome in preparation.outcomes
            )
        ):
            runtime_control.request_stop()
            raise ValueError("production_probe_prepare_failed")
        selected = _open_action_files(root)[:capacity]
    if len(selected) != capacity:
        raise ValueError("production_probe_insufficient_open_actions")
    renewed = renew_actions(selected)
    if renewed != selected:
        raise ValueError("production_probe_renew_failed")
    tasks = [
        {
            "project_root": str(root),
            "action_file": str(action_file),
            "timeout_seconds": 900,
        }
        for action_file in renewed
    ]
    result = run_reviews(tasks, capacity)
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    completed = 0
    failed = 0
    process_attempts = 0
    process_retries = 0
    usage_statuses: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        business_state = value.get("business_state")
        worker_status = value.get("status")
        if outcome.status == "completed" and worker_status == "completed":
            completed += 1
        elif outcome.status not in {"timed_out", "stopped"}:
            failed += 1
        if worker_status == "completed" and isinstance(business_state, str):
            business_results[business_state] += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_attempts += int(value.get("process_attempts") or 0)
        process_retries += int(value.get("process_retries") or 0)
        token_usage = value.get("token_usage")
        if not isinstance(token_usage, dict):
            usage_statuses["unknown"] += 1
            continue
        usage_status = str(token_usage.get("status") or "unknown")
        if usage_status not in {"recorded", "partial"}:
            usage_status = "unknown"
        usage_statuses[usage_status] += 1
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            value_count = token_usage.get(field)
            if isinstance(value_count, int) and not isinstance(value_count, bool):
                token_totals[field] += value_count
    if failed or result.timed_out or result.stopped:
        runtime_control.request_stop()
    return {
        "schema_version": 1,
        "kind": "preaudit_production_concurrency_probe",
        "capacity": capacity,
        "selected": len(selected),
        "prepared_new": prepared_new,
        "prepare_peak_active": prepare_peak_active,
        "prepare_elapsed_seconds": prepare_elapsed_seconds,
        "completed": completed,
        "failed": failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "process_attempts": process_attempts,
        "process_retries": process_retries,
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "token_usage": {
            "status": (
                "recorded"
                if usage_statuses["recorded"] == len(result.outcomes)
                else "partial"
                if usage_statuses["recorded"] or usage_statuses["partial"]
                else "unknown"
            ),
            "recorded_workers": usage_statuses["recorded"],
            "partial_workers": usage_statuses["partial"],
            "unknown_workers": usage_statuses["unknown"],
            **{
                field: token_totals[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            },
        },
    }


def _travel_pending(live: dict) -> int:
    credentials = live.get("credentials")
    oa = live.get("oa")
    if (
        not isinstance(credentials, dict)
        or credentials.get("status") != "ok"
        or not isinstance(oa, dict)
        or oa.get("execution_identity") not in {"verified", "unknown"}
    ):
        raise ValueError("production_drain_live_status_invalid")
    by_workflow = oa.get("by_workflow")
    if not isinstance(by_workflow, dict):
        raise ValueError("production_drain_live_status_invalid")
    pending = by_workflow.get("travel_expense", 0)
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise ValueError("production_drain_live_status_invalid")
    if pending and oa.get("execution_identity") != "verified":
        raise ValueError("production_drain_live_status_invalid")
    return pending


def _production_reconciliation() -> dict:
    root = PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    return cli._live_reconciliation(root, workflow_key="travel_expense")


def _rolling_snapshot(
    live_status: LiveStatus,
    reconcile_status: Callable[[], dict],
) -> tuple[int, int, int]:
    pending = _travel_pending(live_status())
    try:
        reconciled = reconcile_status()
        quarantine = reconciled["quarantine"]
        quarantined = quarantine["pending_quarantined"]
        actionable = quarantine["actionable_pending"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("production_rolling_reconciliation_invalid") from error
    if (
        reconciled.get("schema_version") != 1
        or reconciled.get("kind") != "preaudit_reconciliation"
        or reconciled.get("read_only") is not True
        or not isinstance(quarantined, int)
        or isinstance(quarantined, bool)
        or not isinstance(actionable, int)
        or isinstance(actionable, bool)
        or quarantined < 0
        or actionable < 0
        or quarantined + actionable != pending
    ):
        raise ValueError("production_rolling_reconciliation_invalid")
    return pending, quarantined, actionable


def _quarantined_pending_count() -> int:
    return int(_production_reconciliation()["quarantine"]["pending_quarantined"])


def _local_quarantine_count() -> int:
    flow_root = (
        PROJECT_ROOT
        / "runtime"
        / "finance_preaudit_flows"
        / "travel_expense"
    )
    return reconciliation.quarantine_summary(
        flow_root.parent,
        pending_rows=[],
    )["local_quarantined"]


def _legacy_run_drain(
    *,
    max_capacity: int,
    live_status: LiveStatus = runtime_doctor.status,
    batch_runner: BatchRunner = _legacy_run_existing_batch,
    on_batch: Callable[[dict], None] | None = None,
    max_batches: int = 100,
) -> dict:
    if not 1 <= max_capacity <= 6:
        raise ValueError("production_drain_capacity_invalid")
    started_at = time.monotonic()
    initial_pending = _travel_pending(live_status())
    pending = initial_pending
    batches: list[dict] = []
    while pending:
        if len(batches) >= max_batches:
            runtime_control.request_stop()
            raise ValueError("production_drain_batch_limit_reached")
        batch = batch_runner(capacity=min(max_capacity, pending))
        batches.append(batch)
        if on_batch is not None:
            on_batch(batch)
        if (
            batch.get("failed")
            or batch.get("timed_out")
            or batch.get("stopped")
            or batch.get("completed") != batch.get("selected")
        ):
            runtime_control.request_stop()
            raise ValueError("production_drain_batch_failed")
        pending = _travel_pending(live_status())

    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    for batch in batches:
        business_results.update(batch.get("business_results") or {})
        error_counts.update(batch.get("error_counts") or {})
        usage = batch.get("token_usage") or {}
        for field in ("recorded_workers", "partial_workers", "unknown_workers"):
            usage_workers[field] += int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            token_totals[field] += int(usage.get(field) or 0)
    return {
        "schema_version": 1,
        "kind": "preaudit_production_drain",
        "status": "completed",
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "max_capacity": max_capacity,
        "initial_pending": initial_pending,
        "final_pending": pending,
        "batches": len(batches),
        "selected": sum(int(batch.get("selected") or 0) for batch in batches),
        "prepared_new": sum(int(batch.get("prepared_new") or 0) for batch in batches),
        "completed": sum(int(batch.get("completed") or 0) for batch in batches),
        "process_attempts": sum(int(batch.get("process_attempts") or 0) for batch in batches),
        "process_retries": sum(int(batch.get("process_retries") or 0) for batch in batches),
        "batch_elapsed_seconds": sum(float(batch.get("elapsed_seconds") or 0) for batch in batches),
        "elapsed_seconds": time.monotonic() - started_at,
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == sum(
                    int(batch.get("selected") or 0) for batch in batches
                )
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def run_existing_batch(*, capacity: int, **_legacy_options: object) -> dict:
    del capacity, _legacy_options
    raise ValueError("production_legacy_scheduler_removed")


def run_drain(*, max_capacity: int, **_legacy_options: object) -> dict:
    del max_capacity, _legacy_options
    raise ValueError("production_legacy_scheduler_removed")


def _rolling_wave_metrics(result: process_pool.PoolResult[dict]) -> dict:
    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    completed = 0
    failed = 0
    process_attempts = 0
    process_retries = 0
    prepare_active_seconds = 0.0
    worker_values: list[dict] = []
    for outcome in result.outcomes:
        value = outcome.value if isinstance(outcome.value, dict) else {}
        worker_values.append(value)
        worker_status = value.get("status")
        if outcome.status == "completed" and worker_status == "completed":
            completed += 1
            business_state = value.get("business_state")
            if isinstance(business_state, str):
                business_results[business_state] += 1
        elif outcome.status not in {"timed_out", "stopped"}:
            failed += 1
        error_code = value.get("error_code") or outcome.error_code
        if isinstance(error_code, str):
            error_counts[error_code] += 1
        process_attempts += int(value.get("process_attempts") or 0)
        process_retries += int(value.get("process_retries") or 0)
        prepare_active_seconds += float(value.get("prepare_elapsed_seconds") or 0)
        usage = value.get("token_usage")
        status = usage.get("status") if isinstance(usage, dict) else "unknown"
        if status not in {"recorded", "partial"}:
            status = "unknown"
        usage_workers[f"{status}_workers"] += 1
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                count = usage.get(field)
                if isinstance(count, int) and not isinstance(count, bool):
                    token_totals[field] += count
    codex_events = _aggregate_codex_events(worker_values)
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "requested": result.requested,
        "started": result.started,
        "completed": completed,
        "failed": failed + result.failed,
        "timed_out": result.timed_out,
        "stopped": result.stopped,
        "peak_active": result.peak_active,
        "elapsed_seconds": result.elapsed_seconds,
        "prepare_active_seconds": prepare_active_seconds,
        "process_attempts": process_attempts,
        "process_retries": process_retries,
        "runtime_health": concurrency_control.summarize_runtime(
            result.runtime_samples
        ),
        "watchdog": {
            "runtime_samples": len(result.runtime_samples),
            "maximum_task_seconds": result.max_task_elapsed_seconds,
            "task_timeout_seconds": 960,
        },
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == len(result.outcomes)
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def run_rolling_drain(
    *,
    capacity: int,
    adaptive: bool = False,
    max_capacity: int = 12,
    live_status: LiveStatus = runtime_doctor.status,
    pipeline_runner: PipelineRunner = _run_dispatched_tasks,
    reconcile_status: Callable[[], dict] = _production_reconciliation,
    max_waves: int = 10,
) -> dict:
    try:
        with runtime_control.global_controller_lease():
            return _run_rolling_drain_locked(
                capacity=capacity,
                adaptive=adaptive,
                max_capacity=max_capacity,
                live_status=live_status,
                pipeline_runner=pipeline_runner,
                reconcile_status=reconcile_status,
                max_waves=max_waves,
            )
    except RuntimeError as error:
        if str(error) == "controller_already_active":
            raise ValueError("production_rolling_controller_active") from error
        raise


def _run_rolling_drain_locked(
    *,
    capacity: int,
    adaptive: bool = False,
    max_capacity: int = 12,
    live_status: LiveStatus = runtime_doctor.status,
    pipeline_runner: PipelineRunner = _run_dispatched_tasks,
    reconcile_status: Callable[[], dict] = _production_reconciliation,
    max_waves: int = 10,
) -> dict:
    if not 1 <= capacity <= 6:
        raise ValueError("production_rolling_capacity_invalid")
    if adaptive and (
        capacity not in concurrency_control.CAPACITY_STEPS
        or max_capacity not in concurrency_control.CAPACITY_STEPS
        or capacity > max_capacity
    ):
        raise ValueError("production_rolling_capacity_invalid")
    if runtime_control.stop_requested():
        raise ValueError("production_probe_stopped")
    started_at = time.monotonic()
    try:
        initial_pending, quarantined_pending, initial_actionable = (
            _rolling_snapshot(live_status, reconcile_status)
        )
    except ValueError as error:
        if str(error) == "production_rolling_reconciliation_invalid":
            raise
        raise ValueError("production_rolling_reconciliation_invalid") from error
    pending = initial_pending
    actionable = initial_actionable
    waves: list[dict] = []
    status = "completed"
    stop_reason: str | None = None
    healthy_windows = 0
    current_capacity = capacity
    last_decision = {
        "action": "hold",
        "next_capacity": current_capacity,
        "healthy_windows": 0,
        "reason": "fixed_capacity",
    }
    initial_quarantined = quarantined_pending
    while actionable:
        if len(waves) >= max_waves:
            runtime_control.request_stop()
            raise ValueError("production_rolling_wave_limit_reached")
        wave_size = (
            min(actionable, current_capacity * 2) if adaptive else actionable
        )
        wave = _rolling_wave_metrics(
            pipeline_runner(wave_size, current_capacity)
        )
        wave["capacity"] = current_capacity
        waves.append(wave)
        if (
            wave["failed"]
            or wave["timed_out"]
            or wave["stopped"]
            or wave["completed"] != wave["requested"]
        ):
            runtime_control.request_stop()
            status = "failed"
            stop_reason = (
                "host_critical"
                if wave["runtime_health"].get("status") == "critical"
                else "technical_failure"
            )
            last_decision = {
                "action": "stop",
                "next_capacity": current_capacity,
                "healthy_windows": 0,
                "reason": stop_reason,
            }
            break
        try:
            pending, quarantined_pending, actionable = _rolling_snapshot(
                live_status,
                reconcile_status,
            )
        except ValueError:
            runtime_control.request_stop()
            raise
        if quarantined_pending > initial_quarantined:
            runtime_control.request_stop()
            status = "failed"
            stop_reason = "quarantine_increased"
            break
        if adaptive:
            last_decision = concurrency_control.decide_capacity(
                current_capacity=current_capacity,
                healthy_windows=healthy_windows,
                wave={
                    "requested": wave["requested"],
                    "completed": wave["completed"],
                    "failed": wave["failed"],
                    "timed_out": wave["timed_out"],
                    "stopped": wave["stopped"],
                    "transient_error_events": wave["transient_error_events"],
                    "codex_event_status": wave["codex_events"]["status"],
                },
                host=wave["runtime_health"],
            )
            healthy_windows = int(last_decision["healthy_windows"])
            if last_decision["action"] == "stop":
                runtime_control.request_stop()
                status = "failed"
                stop_reason = str(last_decision["reason"])
                break
            current_capacity = min(
                int(last_decision["next_capacity"]),
                max_capacity,
            )

    if status == "completed" and quarantined_pending:
        status = "completed_with_quarantine"

    business_results: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    usage_workers: Counter[str] = Counter()
    token_totals: Counter[str] = Counter()
    for wave in waves:
        business_results.update(wave["business_results"])
        error_counts.update(wave["error_counts"])
        usage = wave["token_usage"]
        for field in ("recorded_workers", "partial_workers", "unknown_workers"):
            usage_workers[field] += int(usage.get(field) or 0)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            token_totals[field] += int(usage.get(field) or 0)
    completed = sum(wave["completed"] for wave in waves)
    codex_events = _merge_codex_event_summaries(
        [wave["codex_events"] for wave in waves]
    )
    transient_error_events = codex_events["counts"].get("error")
    if transient_error_events is None and codex_events["status"] == "recorded":
        transient_error_events = 0
    return {
        "schema_version": 1,
        "kind": "preaudit_production_rolling_drain",
        "status": status,
        "scheduler": "rolling",
        "elastic": adaptive,
        "capacity": capacity,
        "model": codex_review_worker.MODEL,
        "reasoning_effort": codex_review_worker.REASONING_EFFORT,
        "start_capacity": capacity,
        "recommended_capacity": current_capacity,
        "last_capacity_decision": last_decision,
        "stop_reason": stop_reason,
        "wave_runtime_health": [wave["runtime_health"] for wave in waves],
        "initial_pending": initial_pending,
        "initial_actionable": initial_actionable,
        "final_pending": (
            pending
            if status in {"completed", "completed_with_quarantine"}
            else "unknown"
        ),
        "quarantined_pending": quarantined_pending,
        "waves": len(waves),
        "completed": completed,
        "failed": sum(wave["failed"] for wave in waves),
        "timed_out": sum(wave["timed_out"] for wave in waves),
        "stopped": sum(wave["stopped"] for wave in waves),
        "peak_active": max((wave["peak_active"] for wave in waves), default=0),
        "elapsed_seconds": max(
            time.monotonic() - started_at,
            sum(wave["elapsed_seconds"] for wave in waves),
        ),
        "prepare_active_seconds": sum(
            wave["prepare_active_seconds"] for wave in waves
        ),
        "process_attempts": sum(wave["process_attempts"] for wave in waves),
        "process_retries": sum(wave["process_retries"] for wave in waves),
        "business_results": dict(sorted(business_results.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "codex_events": codex_events,
        "transient_error_events": transient_error_events,
        "token_usage": {
            "status": (
                "recorded"
                if usage_workers["recorded_workers"] == completed
                else "partial"
                if usage_workers["recorded_workers"] or usage_workers["partial_workers"]
                else "unknown"
            ),
            **usage_workers,
            **token_totals,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance-preaudit-concurrency-probe")
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--confirm-write", choices=("YES",))
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--adaptive", action="store_true")
    parser.add_argument("--waves", type=int, default=6)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--rolling", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.read_only:
            if args.drain or args.rolling:
                raise ValueError("read_only_probe_scheduler_invalid")
            payload = (
                run_adaptive_read_only_probe(
                    start_capacity=args.capacity,
                    sample_size=args.samples,
                    max_waves=args.waves,
                )
                if args.adaptive
                else run_read_only_probe(
                    capacity=args.capacity,
                    sample_size=args.samples,
                )
            )
        elif args.confirm_write != "YES":
            raise ValueError("production_probe_write_confirmation_required")
        elif args.drain:
            raise ValueError("production_legacy_scheduler_removed")
        else:
            payload = run_rolling_drain(
                capacity=args.capacity,
                adaptive=args.adaptive,
            )
    except ValueError as error:
        payload = {
            "schema_version": 1,
            "kind": (
                "preaudit_read_only_concurrency_probe_failed"
                if args.read_only
                else "preaudit_production_concurrency_probe_failed"
            ),
            "error_code": str(error),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
