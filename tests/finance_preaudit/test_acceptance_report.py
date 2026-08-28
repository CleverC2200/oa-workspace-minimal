from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from apps.oa_finance_preaudit import flow_evals
from apps.oa_finance_preaudit.flow_engine import WORKFLOW_KEYS


START = datetime(2026, 8, 1, tzinfo=timezone.utc)
REPLAY_OK = {
    "accuracy": 1.0,
    "format_compliance": 1.0,
    "differences": [],
}
SETTINGS = {
    "frequency_seconds": 3600,
    "review_concurrency": 1,
    "submit_concurrency": 1,
    "review_packet_profile_sha256": "a" * 64,
}


def _coverage(applicable: bool = True) -> dict:
    return {
        "applicable": applicable,
        "covered": 1 if applicable else 0,
        "total": 1 if applicable else 0,
    }


def _samples(
    count: int = 20,
    *,
    end_to_end: int = 600,
    waiting: int = 60,
    exact_tokens: bool = True,
    technical_failures: int = 0,
) -> list[dict]:
    samples = []
    for workflow_index, workflow_key in enumerate(WORKFLOW_KEYS):
        for index in range(count):
            closed_at = START + timedelta(days=index, hours=workflow_index)
            raw_key = f"{workflow_key}:{index}".encode()
            samples.append(
                {
                    "schema_version": 1,
                    "kind": "preaudit_case_metric",
                    "item_key": hashlib.sha256(raw_key).hexdigest(),
                    "workflow_key": workflow_key,
                    "closed_at": closed_at.isoformat(),
                    "execution_state": (
                        "needs_admin" if index < technical_failures else "closed"
                    ),
                    "backlog_age_seconds": waiting,
                    "end_to_end_seconds": end_to_end,
                    "active_seconds": end_to_end - waiting,
                    "waiting_seconds": waiting,
                    "timing_status": "exact",
                    "phase_seconds": {
                        "discover": 5,
                        "collect": 20,
                        "review": 100,
                        "wait": waiting,
                        "submit": 0,
                        "postcheck": 0,
                        "close": 1,
                    },
                    "stage_timestamps": {},
                    "pipeline_evidence": {"status": "unknown"},
                    "token_usage": (
                        {
                            "status": "exact",
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                        }
                        if exact_tokens
                        else {"status": "unknown"}
                    ),
                    "business_state": (
                        "not_available" if index < technical_failures else "pass"
                    ),
                    "write_state": "not_requested",
                    "quality": {
                        "item_coverage": _coverage(),
                        "resource_coverage": _coverage(),
                        "page_coverage": _coverage(
                            workflow_key == "packaging_layout_approval"
                        ),
                        "format_ok": True,
                    },
                    "duplicate_submissions": 0,
                    "cross_case_mix": 0,
                    "unknown_auto_retries": 0,
                    "agent_messages": 0,
                    "coordination_seconds": 0,
                    "coordination_tokens": 0,
                }
            )
    return samples


def _cli_samples(outcome: str = "success") -> list[dict]:
    return [
        {
            "workflow_key": workflow_key,
            "command": "complete-review",
            "occurred_at": (START + timedelta(days=10)).isoformat(),
            "outcome": outcome,
            "human_reproduction": (
                "not_attempted" if outcome == "success" else "succeeded"
            ),
        }
        for workflow_key in WORKFLOW_KEYS
        for _index in range(20)
    ]


def _report(samples: list[dict], *, cli: list[dict] | None = None) -> dict:
    return flow_evals.build_acceptance_report(
        samples,
        replay_report=REPLAY_OK,
        cli_samples=cli or _cli_samples(),
    )


def test_report_has_case_flow_day_metrics_and_fixed_gates() -> None:
    report = _report(_samples())

    assert len(report["cases"]) == 20 * len(WORKFLOW_KEYS)
    assert set(report["by_flow"]) == set(WORKFLOW_KEYS)
    assert report["by_flow"]["travel_expense"]["closed_samples"] == 20
    assert report["by_flow"]["travel_expense"]["end_to_end_seconds"] == {
        "p50": 600,
        "p95": 600,
    }
    assert report["by_flow"]["travel_expense"]["waiting_ratio"] == 0.1
    assert report["quality_gate"]["passed"] is True
    assert report["target_gate"] == {
        "p95_end_to_end_below_900": True,
        "waiting_ratio_below_0_20": True,
        "technical_error_rate_at_most_0_02": True,
    }
    assert report["sample_eligible"] is True
    assert report["token_tuning"] == {"eligible": True, "reason": "exact"}
    assert report["mcp_admission"]["decision"] == "do_not_introduce_mcp"
    assert all("request_id" not in str(case) for case in report["cases"])


def test_19_samples_or_unknown_tokens_hold_only_affected_optimization() -> None:
    samples = _samples(20, exact_tokens=False)
    samples = [
        sample
        for sample in samples
        if not (
            sample["workflow_key"] == "travel_expense"
            and sample["item_key"]
            == hashlib.sha256(b"travel_expense:19").hexdigest()
        )
    ]

    report = _report(samples)

    assert report["sample_eligible"] is False
    assert report["by_flow"]["travel_expense"]["closed_samples"] == 19
    assert report["token_tuning"] == {
        "eligible": False,
        "reason": "exact_usage_missing",
    }
    assert report["mcp_admission"]["decision"] == "do_not_introduce_mcp"


def test_unknown_runtime_safety_evidence_blocks_quality_gate() -> None:
    samples = _samples()
    samples[0]["unknown_auto_retries"] = "unknown"
    samples[0]["agent_messages"] = "unknown"

    report = _report(samples)

    assert report["quality_gate"]["unknown_auto_retry_0"] is False
    assert report["quality_gate"]["agent_messages_0"] is False
    assert report["quality_gate"]["passed"] is False
    assert report["mcp_admission"]["decision"] == "do_not_introduce_mcp"


def test_cli_metrics_are_independent_and_only_strict_majority_proposes_mcp() -> None:
    cli = _cli_samples()
    for index in range(3):
        cli[index] = {
            **cli[index],
            "outcome": "json_contract_error",
            "human_reproduction": "succeeded",
        }
    report = _report(_samples(), cli=cli)

    assert report["cli_quality"]["failure_counts"]["json_contract_error"] == 3
    assert report["cli_quality"]["manual_reproduction_rate"] == 1.0
    assert report["mcp_admission"]["decision"] == (
        "propose_separate_stdio_mcp_issue"
    )

    cli[0]["human_reproduction"] = "not_attempted"
    incomplete = _report(_samples(), cli=cli)
    assert incomplete["cli_quality"]["reproduction_complete"] is False
    assert incomplete["mcp_admission"]["decision"] == "do_not_introduce_mcp"

    cli[0]["human_reproduction"] = "failed"
    reproduced_failure = _report(_samples(), cli=cli)
    assert reproduced_failure["cli_quality"]["reproduction_complete"] is True
    assert reproduced_failure["cli_quality"]["manual_reproduction_rate"] < 1.0
    assert reproduced_failure["mcp_admission"]["decision"] == "do_not_introduce_mcp"


def test_global_cli_calls_project_to_flows_without_inflating_overall() -> None:
    cli = [
        {
            "workflow_key": "global",
            "command": "status",
            "occurred_at": (START + timedelta(days=10)).isoformat(),
            "outcome": "json_contract_error" if index == 0 else "success",
            "human_reproduction": "succeeded" if index == 0 else "not_attempted",
        }
        for index in range(20)
    ]

    report = _report(_samples(), cli=cli)

    assert report["cli_quality"]["sample_count"] == 20
    assert report["cli_quality"]["failure_count"] == 1
    assert all(
        metrics["sample_count"] == 20
        for metrics in report["cli_quality"]["by_flow"].values()
    )


def test_global_cache_clean_invocation_is_included_in_cli_quality() -> None:
    report = _report(
        _samples(),
        cli=[
            {
                "workflow_key": "global",
                "command": "clean",
                "occurred_at": (START + timedelta(days=10)).isoformat(),
                "outcome": "success",
                "human_reproduction": "not_attempted",
            }
        ],
    )

    assert report["cli_quality"]["sample_count"] == 1
    assert all(
        metrics["sample_count"] == 1
        for metrics in report["cli_quality"]["by_flow"].values()
    )


def test_empty_window_is_reportable_and_runtime_evidence_is_window_bound() -> None:
    empty = flow_evals.build_acceptance_report(
        [],
        replay_report=REPLAY_OK,
        cli_samples=[],
    )
    assert empty["sample_eligible"] is False
    assert empty["cli_quality"]["sample_count"] == 0
    assert empty["token_tuning"] == {
        "eligible": False,
        "reason": "exact_usage_missing",
    }

    samples = _samples()
    evidence = {
        "schema_version": 1,
        "kind": "preaudit_runtime_evidence",
        "window_start": (START - timedelta(seconds=1)).isoformat(),
        "window_end": max(sample["closed_at"] for sample in samples),
        "by_flow": {
            workflow_key: {
                "duplicate_submissions": 0,
                "cross_case_mix": 0,
                "unknown_auto_retries": 0,
                "agent_messages": 0,
            }
            for workflow_key in WORKFLOW_KEYS
        },
    }
    with pytest.raises(ValueError, match="runtime_evidence_invalid"):
        flow_evals.build_acceptance_report(
            samples,
            replay_report=REPLAY_OK,
            cli_samples=_cli_samples(),
            runtime_evidence=evidence,
        )


def test_single_variable_trial_can_be_kept_without_applying_changes() -> None:
    before = _report(_samples(end_to_end=1000, waiting=300))
    after = _report(_samples(end_to_end=700, waiting=100))
    after_settings = {**SETTINGS, "review_concurrency": 2}

    result = flow_evals.evaluate_experiment(
        before,
        after,
        before_settings=SETTINGS,
        after_settings=after_settings,
    )

    assert result["changed_variable"] == "review_concurrency"
    assert result["decision"] == "keep"
    assert result["applied"] is False
    assert result["token_tuning"]["eligible"] is True
    assert result["performance"] == {
        "before": {
            "end_to_end_seconds": {"p50": 1000, "p95": 1000},
            "token_usage": {
                "status": "exact",
                "exact_samples": 20 * len(WORKFLOW_KEYS),
                "unknown_samples": 0,
                "input_tokens": 2000 * len(WORKFLOW_KEYS),
                "output_tokens": 400 * len(WORKFLOW_KEYS),
                "total_tokens": 2400 * len(WORKFLOW_KEYS),
            },
        },
        "after": {
            "end_to_end_seconds": {"p50": 700, "p95": 700},
            "token_usage": {
                "status": "exact",
                "exact_samples": 20 * len(WORKFLOW_KEYS),
                "unknown_samples": 0,
                "input_tokens": 2000 * len(WORKFLOW_KEYS),
                "output_tokens": 400 * len(WORKFLOW_KEYS),
                "total_tokens": 2400 * len(WORKFLOW_KEYS),
            },
        },
    }


def test_experiment_does_not_estimate_incomplete_token_usage() -> None:
    before = _report(_samples())
    after = _report(_samples(exact_tokens=False))

    result = flow_evals.evaluate_experiment(
        before,
        after,
        before_settings=SETTINGS,
        after_settings={**SETTINGS, "review_packet_profile_sha256": "b" * 64},
    )

    assert result["performance"]["after"]["token_usage"] == {
        "status": "unknown",
        "exact_samples": 0,
        "unknown_samples": 20 * len(WORKFLOW_KEYS),
    }


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("quality", "quality_regression"),
        ("duplicate", "submission_safety_regression"),
        ("errors", "technical_error_rate_regression"),
        ("latency", "p95_end_to_end_regression"),
        ("unknown", "unknown_increase"),
    ],
)
def test_trial_rollback_conditions_are_fixed(mutation: str, reason: str) -> None:
    before = _report(_samples(end_to_end=600))
    after_samples = _samples(end_to_end=600)
    after_replay = REPLAY_OK
    if mutation == "quality":
        after_replay = {"accuracy": 0.99, "format_compliance": 1.0, "differences": [{}]}
    elif mutation == "duplicate":
        after_samples[0]["duplicate_submissions"] = 1
    elif mutation == "errors":
        for sample in after_samples[:10]:
            sample["execution_state"] = "needs_admin"
            sample["business_state"] = "not_available"
    elif mutation == "latency":
        for sample in after_samples:
            sample["end_to_end_seconds"] = 721
            sample["active_seconds"] = 661
    else:
        after_samples[0]["write_state"] = "unknown"
    after = flow_evals.build_acceptance_report(
        after_samples,
        replay_report=after_replay,
        cli_samples=_cli_samples(),
    )

    result = flow_evals.evaluate_experiment(
        before,
        after,
        before_settings=SETTINGS,
        after_settings={**SETTINGS, "frequency_seconds": 1800},
    )

    assert result["decision"] == "rollback"
    assert reason in result["reasons"]
    assert result["applied"] is False


def test_experiment_rejects_zero_or_two_changed_variables() -> None:
    report = _report(_samples())
    with pytest.raises(ValueError, match="experiment_variable_invalid"):
        flow_evals.evaluate_experiment(
            report,
            report,
            before_settings=SETTINGS,
            after_settings=SETTINGS,
        )
    with pytest.raises(ValueError, match="experiment_variable_invalid"):
        flow_evals.evaluate_experiment(
            report,
            report,
            before_settings=SETTINGS,
            after_settings={
                **SETTINGS,
                "frequency_seconds": 1800,
                "review_concurrency": 2,
            },
        )


def test_sensitive_or_nonzero_coordination_sample_is_rejected() -> None:
    invalid = deepcopy(_samples())
    invalid[0]["request_id"] = "must-not-appear"
    with pytest.raises(ValueError, match="acceptance_sample_invalid"):
        _report(invalid)
    invalid = _samples()
    invalid[0]["coordination_seconds"] = 1
    with pytest.raises(ValueError, match="architecture_regression"):
        _report(invalid)
