from __future__ import annotations

from apps.oa_finance_preaudit import concurrency_control


HEALTHY = {
    "status": "healthy",
    "memory_free_percent": 40,
    "load_per_cpu": 0.4,
    "active_threads": 3,
    "cpu_count": 10,
}


def _wave(**changes: int) -> dict:
    wave = {
        "requested": 20,
        "completed": 20,
        "failed": 0,
        "timed_out": 0,
        "stopped": 0,
        "transient_error_events": 0,
    }
    wave.update(changes)
    return wave


def test_two_healthy_windows_upgrade_one_verified_step() -> None:
    first = concurrency_control.decide_capacity(
        current_capacity=6,
        healthy_windows=0,
        wave=_wave(),
        host=HEALTHY,
    )
    second = concurrency_control.decide_capacity(
        current_capacity=first["next_capacity"],
        healthy_windows=first["healthy_windows"],
        wave=_wave(),
        host=HEALTHY,
    )

    assert first == {
        "action": "hold",
        "next_capacity": 6,
        "healthy_windows": 1,
        "reason": "healthy_window_accumulating",
    }
    assert second == {
        "action": "increase",
        "next_capacity": 8,
        "healthy_windows": 0,
        "reason": "healthy_window_threshold",
    }


def test_pressure_decreases_one_step_and_technical_failure_stops() -> None:
    pressure = concurrency_control.decide_capacity(
        current_capacity=12,
        healthy_windows=1,
        wave=_wave(),
        host={**HEALTHY, "status": "pressure", "memory_free_percent": 20},
    )
    failed = concurrency_control.decide_capacity(
        current_capacity=10,
        healthy_windows=1,
        wave=_wave(failed=1, completed=19),
        host=HEALTHY,
    )

    assert pressure == {
        "action": "decrease",
        "next_capacity": 10,
        "healthy_windows": 0,
        "reason": "host_pressure",
    }
    assert failed == {
        "action": "stop",
        "next_capacity": 8,
        "healthy_windows": 0,
        "reason": "technical_failure",
    }


def test_transient_errors_or_unknown_host_hold_without_upgrade() -> None:
    transient = concurrency_control.decide_capacity(
        current_capacity=8,
        healthy_windows=1,
        wave=_wave(transient_error_events=2),
        host=HEALTHY,
    )
    unknown = concurrency_control.decide_capacity(
        current_capacity=8,
        healthy_windows=1,
        wave=_wave(),
        host={**HEALTHY, "status": "unknown", "memory_free_percent": None},
    )

    assert transient["action"] == "hold"
    assert transient["healthy_windows"] == 0
    assert transient["reason"] == "transient_errors_observed"
    assert unknown["action"] == "hold"
    assert unknown["healthy_windows"] == 0
    assert unknown["reason"] == "host_health_unknown"


def test_local_health_is_sanitized_and_classifies_memory_pressure() -> None:
    health = concurrency_control.sample_local_health(
        memory_reader=lambda: 18,
        load_reader=lambda: (2.0, 1.0, 0.5),
        cpu_reader=lambda: 10,
        thread_reader=lambda: 4,
    )

    assert health == {
        "status": "pressure",
        "memory_free_percent": 18,
        "load_per_cpu": 0.2,
        "active_threads": 4,
        "cpu_count": 10,
    }
    assert "pid" not in health
    assert "command" not in health


def test_runtime_summary_uses_worst_observed_host_state() -> None:
    summary = concurrency_control.summarize_runtime(
        (
            {
                "active_workers": 8,
                "oldest_task_seconds": 100.0,
                "host": HEALTHY,
            },
            {
                "active_workers": 10,
                "oldest_task_seconds": 220.0,
                "host": {
                    **HEALTHY,
                    "status": "pressure",
                    "memory_free_percent": 22,
                    "load_per_cpu": 0.8,
                    "active_threads": 5,
                },
            },
        )
    )

    assert summary == {
        "status": "pressure",
        "sample_count": 2,
        "minimum_memory_free_percent": 22,
        "maximum_load_per_cpu": 0.8,
        "maximum_active_threads": 5,
        "maximum_active_workers": 10,
        "maximum_oldest_task_seconds": 220.0,
    }
