from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Callable


CAPACITY_STEPS = (6, 8, 10, 12)
_MEMORY_PERCENT = re.compile(r"free percentage:\s*([0-9]{1,3})%")


def _memory_free_percent() -> int | None:
    try:
        result = subprocess.run(
            ["memory_pressure", "-Q"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _MEMORY_PERCENT.search(result.stdout)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def sample_local_health(
    *,
    memory_reader: Callable[[], int | None] = _memory_free_percent,
    load_reader: Callable[[], tuple[float, float, float]] = os.getloadavg,
    cpu_reader: Callable[[], int | None] = os.cpu_count,
    thread_reader: Callable[[], int] = threading.active_count,
) -> dict:
    try:
        memory_free = memory_reader()
    except Exception:
        memory_free = None
    try:
        cpu_count = int(cpu_reader() or 0)
        one_minute_load = float(load_reader()[0])
    except Exception:
        cpu_count = 0
        one_minute_load = 0.0
    try:
        active_threads = max(0, int(thread_reader()))
    except Exception:
        active_threads = 0
    load_per_cpu = round(one_minute_load / cpu_count, 3) if cpu_count else None
    if memory_free is None or load_per_cpu is None:
        status = "unknown"
    elif memory_free < 12 or load_per_cpu >= 2.0:
        status = "critical"
    elif memory_free < 25 or load_per_cpu >= 1.25:
        status = "pressure"
    elif memory_free >= 30 and load_per_cpu < 1.0:
        status = "healthy"
    else:
        status = "unknown"
    return {
        "status": status,
        "memory_free_percent": memory_free,
        "load_per_cpu": load_per_cpu,
        "active_threads": active_threads,
        "cpu_count": cpu_count,
    }


def _lower_capacity(current_capacity: int) -> int:
    index = CAPACITY_STEPS.index(current_capacity)
    return CAPACITY_STEPS[max(0, index - 1)]


def decide_capacity(
    *,
    current_capacity: int,
    healthy_windows: int,
    wave: dict,
    host: dict,
) -> dict:
    if current_capacity not in CAPACITY_STEPS or healthy_windows not in {0, 1}:
        raise ValueError("concurrency_state_invalid")
    required = {"requested", "completed", "failed", "timed_out", "stopped"}
    if not isinstance(wave, dict) or not required.issubset(wave):
        raise ValueError("concurrency_window_invalid")
    technical_failure = (
        int(wave.get("failed") or 0) > 0
        or int(wave.get("timed_out") or 0) > 0
        or int(wave.get("stopped") or 0) > 0
        or int(wave.get("completed") or 0) != int(wave.get("requested") or 0)
    )
    if technical_failure:
        return {
            "action": "stop",
            "next_capacity": _lower_capacity(current_capacity),
            "healthy_windows": 0,
            "reason": "technical_failure",
        }
    host_status = host.get("status") if isinstance(host, dict) else "unknown"
    if host_status == "critical":
        return {
            "action": "stop",
            "next_capacity": _lower_capacity(current_capacity),
            "healthy_windows": 0,
            "reason": "host_critical",
        }
    if host_status == "pressure":
        return {
            "action": "decrease",
            "next_capacity": _lower_capacity(current_capacity),
            "healthy_windows": 0,
            "reason": "host_pressure",
        }
    if host_status != "healthy":
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": 0,
            "reason": "host_health_unknown",
        }
    if wave.get("codex_event_status") != "recorded":
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": 0,
            "reason": "codex_events_unknown",
        }
    transient_errors = wave.get("transient_error_events")
    if (
        not isinstance(transient_errors, int)
        or isinstance(transient_errors, bool)
        or transient_errors < 0
    ):
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": 0,
            "reason": "codex_events_unknown",
        }
    if transient_errors > 0:
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": 0,
            "reason": "transient_errors_observed",
        }
    next_windows = healthy_windows + 1
    if next_windows < 2:
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": next_windows,
            "reason": "healthy_window_accumulating",
        }
    index = CAPACITY_STEPS.index(current_capacity)
    if index == len(CAPACITY_STEPS) - 1:
        return {
            "action": "hold",
            "next_capacity": current_capacity,
            "healthy_windows": 1,
            "reason": "verified_maximum",
        }
    return {
        "action": "increase",
        "next_capacity": CAPACITY_STEPS[index + 1],
        "healthy_windows": 0,
        "reason": "healthy_window_threshold",
    }


def summarize_runtime(samples: tuple[dict, ...]) -> dict:
    statuses = []
    memory_values: list[int] = []
    load_values: list[float] = []
    thread_values: list[int] = []
    worker_values: list[int] = []
    task_ages: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        host = sample.get("host")
        if isinstance(host, dict):
            statuses.append(str(host.get("status") or "unknown"))
            memory = host.get("memory_free_percent")
            load = host.get("load_per_cpu")
            threads = host.get("active_threads")
            if isinstance(memory, int) and not isinstance(memory, bool):
                memory_values.append(memory)
            if isinstance(load, (int, float)) and not isinstance(load, bool):
                load_values.append(float(load))
            if isinstance(threads, int) and not isinstance(threads, bool):
                thread_values.append(threads)
        workers = sample.get("active_workers")
        age = sample.get("oldest_task_seconds")
        if isinstance(workers, int) and not isinstance(workers, bool):
            worker_values.append(workers)
        if isinstance(age, (int, float)) and not isinstance(age, bool):
            task_ages.append(float(age))
    severity = {"healthy": 0, "unknown": 1, "pressure": 2, "critical": 3}
    status = max(statuses, key=lambda value: severity.get(value, 1)) if statuses else "unknown"
    return {
        "status": status,
        "sample_count": len(samples),
        "minimum_memory_free_percent": min(memory_values) if memory_values else None,
        "maximum_load_per_cpu": max(load_values) if load_values else None,
        "maximum_active_threads": max(thread_values) if thread_values else None,
        "maximum_active_workers": max(worker_values) if worker_values else 0,
        "maximum_oldest_task_seconds": max(task_ages) if task_ages else 0.0,
    }
