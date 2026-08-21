from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
from statistics import mean
from typing import Iterable, Mapping

from .flow_runner import FlowRunnerConfig, WORKFLOW_KEYS, load_configs


MIN_CLOSED_SAMPLES = 20
MAX_FLOW_REVIEWERS = 3
MAX_FLOW_SUBMITTERS = 3
MAX_ERROR_RATE = 0.02


def _number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(code)
    return float(value)


def _validated_samples(values: Iterable[dict]) -> list[dict]:
    samples = []
    for raw in values:
        if not isinstance(raw, dict) or raw.get("closed") is not True:
            raise ValueError("tuning_sample_invalid")
        try:
            closed_at = datetime.fromisoformat(str(raw.get("closed_at") or ""))
        except ValueError as error:
            raise ValueError("tuning_sample_invalid") from error
        if closed_at.tzinfo is None:
            raise ValueError("tuning_sample_invalid")
        total = _number(raw.get("total_seconds"), "tuning_sample_invalid")
        review = _number(raw.get("review_seconds"), "tuning_sample_invalid")
        review_wait = _number(raw.get("review_wait_seconds"), "tuning_sample_invalid")
        submit_wait = _number(raw.get("submit_wait_seconds"), "tuning_sample_invalid")
        if total <= 0 or review > total or review_wait + submit_wait > total:
            raise ValueError("tuning_sample_invalid")
        tokens = raw.get("tokens")
        if tokens != "unknown" and (
            isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0
        ):
            raise ValueError("tuning_sample_invalid")
        if not isinstance(raw.get("error"), bool) or not isinstance(
            raw.get("duplicate_submission"), bool
        ):
            raise ValueError("tuning_sample_invalid")
        samples.append(
            {
                "closed_at": closed_at,
                "total_seconds": total,
                "review_seconds": review,
                "review_wait_seconds": review_wait,
                "submit_wait_seconds": submit_wait,
                "tokens": tokens,
                "error": raw["error"],
                "duplicate_submission": raw["duplicate_submission"],
            }
        )
    return samples


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def _quality_passes(report: object) -> bool:
    return (
        isinstance(report, dict)
        and report.get("accuracy") == 1.0
        and report.get("format_compliance") == 1.0
        and report.get("differences") == []
    )


def _unknown_metrics(sample_count: int) -> dict:
    return {
        "sample_count": sample_count,
        "p50_total_seconds": "unknown",
        "p95_total_seconds": "unknown",
        "p50_review_seconds": "unknown",
        "p95_review_seconds": "unknown",
        "waiting_ratio": "unknown",
        "review_waiting_ratio": "unknown",
        "submit_waiting_ratio": "unknown",
        "tokens_per_case": "unknown",
        "error_rate": "unknown",
        "duplicate_submission_rate": "unknown",
    }


def _metrics(samples: list[dict]) -> dict:
    total = [item["total_seconds"] for item in samples]
    review = [item["review_seconds"] for item in samples]
    total_sum = sum(total)
    exact_tokens = [item["tokens"] for item in samples if item["tokens"] != "unknown"]
    review_wait = sum(item["review_wait_seconds"] for item in samples)
    submit_wait = sum(item["submit_wait_seconds"] for item in samples)
    return {
        "sample_count": len(samples),
        "p50_total_seconds": _percentile(total, 0.50),
        "p95_total_seconds": _percentile(total, 0.95),
        "p50_review_seconds": _percentile(review, 0.50),
        "p95_review_seconds": _percentile(review, 0.95),
        "waiting_ratio": round((review_wait + submit_wait) / total_sum, 4),
        "review_waiting_ratio": round(review_wait / total_sum, 4),
        "submit_waiting_ratio": round(submit_wait / total_sum, 4),
        "tokens_per_case": (
            round(mean(exact_tokens), 2)
            if len(exact_tokens) == len(samples)
            else "unknown"
        ),
        "error_rate": round(sum(item["error"] for item in samples) / len(samples), 4),
        "duplicate_submission_rate": round(
            sum(item["duplicate_submission"] for item in samples) / len(samples),
            4,
        ),
    }


def _hold(
    *,
    workflow_key: str,
    reason: str,
    current_reviewers: int,
    current_submitters: int,
    metrics: dict,
    window: dict | None,
) -> dict:
    return {
        "workflow_key": workflow_key,
        "decision": "hold",
        "reason": reason,
        "current_reviewers": current_reviewers,
        "current_submitters": current_submitters,
        "recommended_reviewers": current_reviewers,
        "recommended_submitters": current_submitters,
        "window": window or "unknown",
        "metrics": metrics,
        "hypothesis": "insufficient_or_unsafe_evidence",
        "expected_effect": "keep_current_behavior",
        "rollback": {
            "reviewers": current_reviewers,
            "submitters": current_submitters,
            "conditions": [],
        },
        "applied": False,
    }


def recommend_flow(
    *,
    workflow_key: str,
    samples: Iterable[dict],
    current_reviewers: int,
    current_submitters: int,
    shared_reviewer_slots: int,
    shared_submit_slots: int,
    quality_report: dict,
) -> dict:
    if workflow_key not in WORKFLOW_KEYS:
        raise ValueError("tuning_workflow_invalid")
    capacities = (
        current_reviewers,
        current_submitters,
        shared_reviewer_slots,
        shared_submit_slots,
    )
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 1
        for value in capacities
    ):
        raise ValueError("tuning_capacity_invalid")
    history = _validated_samples(samples)
    if len(history) < MIN_CLOSED_SAMPLES:
        return _hold(
            workflow_key=workflow_key,
            reason="insufficient_samples",
            current_reviewers=current_reviewers,
            current_submitters=current_submitters,
            metrics=_unknown_metrics(len(history)),
            window=None,
        )
    metrics = _metrics(history)
    window = {
        "closed_count": len(history),
        "start": min(item["closed_at"] for item in history).isoformat(),
        "end": max(item["closed_at"] for item in history).isoformat(),
    }
    hold_reason = None
    if metrics["tokens_per_case"] == "unknown":
        hold_reason = "token_usage_incomplete"
    elif not _quality_passes(quality_report):
        hold_reason = "quality_regression"
    elif metrics["duplicate_submission_rate"] > 0:
        hold_reason = "submission_safety_regression"
    elif metrics["error_rate"] > MAX_ERROR_RATE:
        hold_reason = "error_rate_high"
    if hold_reason:
        return _hold(
            workflow_key=workflow_key,
            reason=hold_reason,
            current_reviewers=current_reviewers,
            current_submitters=current_submitters,
            metrics=metrics,
            window=window,
        )

    reviewer_cap = max(1, min(MAX_FLOW_REVIEWERS, shared_reviewer_slots - 1))
    submitter_cap = max(1, min(MAX_FLOW_SUBMITTERS, shared_submit_slots - 1))
    reviewers = current_reviewers
    submitters = current_submitters
    hypotheses = []
    if metrics["p95_total_seconds"] >= 900 and metrics["review_waiting_ratio"] >= 0.20:
        reviewers = min(current_reviewers + 1, reviewer_cap)
        if reviewers > current_reviewers:
            hypotheses.append("review_wait_is_primary_latency_source")
    if metrics["p95_total_seconds"] >= 900 and metrics["submit_waiting_ratio"] >= 0.20:
        submitters = min(current_submitters + 1, submitter_cap)
        if submitters > current_submitters:
            hypotheses.append("submit_wait_is_primary_latency_source")
    if not hypotheses:
        return _hold(
            workflow_key=workflow_key,
            reason="no_measured_pressure",
            current_reviewers=current_reviewers,
            current_submitters=current_submitters,
            metrics=metrics,
            window=window,
        )
    return {
        "workflow_key": workflow_key,
        "decision": "recommend_change",
        "reason": "measured_pressure",
        "current_reviewers": current_reviewers,
        "current_submitters": current_submitters,
        "recommended_reviewers": reviewers,
        "recommended_submitters": submitters,
        "window": window,
        "metrics": metrics,
        "hypothesis": hypotheses,
        "expected_effect": "reduce_p95_total_and_waiting_ratio_without_quality_change",
        "rollback": {
            "reviewers": current_reviewers,
            "submitters": current_submitters,
            "conditions": [
                "quality_accuracy_or_format_below_1",
                "duplicate_submission_rate_above_0",
                "error_rate_increase_above_0.02",
                "p95_total_seconds_worsens_above_20_percent",
            ],
        },
        "applied": False,
    }


def recommend_all(
    histories: Mapping[str, Iterable[dict]],
    *,
    configs: Mapping[str, FlowRunnerConfig],
    shared_reviewer_slots: int,
    shared_submit_slots: int,
    quality_report: dict,
) -> dict:
    if set(histories) != set(WORKFLOW_KEYS) or set(configs) != set(WORKFLOW_KEYS):
        raise ValueError("tuning_flows_incomplete")
    return {
        "schema_version": 1,
        "flows": {
            workflow_key: recommend_flow(
                workflow_key=workflow_key,
                samples=histories[workflow_key],
                current_reviewers=configs[workflow_key].reviewer_limit,
                current_submitters=configs[workflow_key].submit_limit,
                shared_reviewer_slots=shared_reviewer_slots,
                shared_submit_slots=shared_submit_slots,
                quality_report=quality_report,
            )
            for workflow_key in WORKFLOW_KEYS
        },
        "applied": False,
    }


def evaluate_trial(
    recommendation: dict,
    *,
    observed_samples: Iterable[dict],
    quality_report: dict,
) -> dict:
    if (
        not isinstance(recommendation, dict)
        or recommendation.get("decision") != "recommend_change"
        or not isinstance(recommendation.get("metrics"), dict)
        or not isinstance(recommendation.get("rollback"), dict)
    ):
        raise ValueError("tuning_recommendation_invalid")
    observed = _validated_samples(observed_samples)
    rollback = recommendation["rollback"]
    if len(observed) < MIN_CLOSED_SAMPLES:
        reason = "trial_samples_incomplete"
        observed_metrics = _unknown_metrics(len(observed))
    else:
        observed_metrics = _metrics(observed)
        baseline = recommendation["metrics"]
        if not _quality_passes(quality_report):
            reason = "quality_regression"
        elif observed_metrics["tokens_per_case"] == "unknown":
            reason = "token_usage_incomplete"
        elif observed_metrics["duplicate_submission_rate"] > 0:
            reason = "submission_safety_regression"
        elif observed_metrics["error_rate"] - baseline["error_rate"] > 0.02:
            reason = "error_rate_regression"
        elif observed_metrics["p95_total_seconds"] > baseline["p95_total_seconds"] * 1.20:
            reason = "p95_total_regression"
        else:
            reason = "trial_passed"
    if reason != "trial_passed":
        return {
            "decision": "rollback",
            "reason": reason,
            "reviewers": rollback["reviewers"],
            "submitters": rollback["submitters"],
            "metrics": observed_metrics,
            "applied": False,
        }
    return {
        "decision": "accept_for_manual_application",
        "reason": reason,
        "reviewers": recommendation["recommended_reviewers"],
        "submitters": recommendation["recommended_submitters"],
        "metrics": observed_metrics,
        "applied": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oa-finance-tuning")
    parser.add_argument("--histories", type=Path, required=True)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--shared-reviewer-slots", type=int, required=True)
    parser.add_argument("--shared-submit-slots", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        histories = json.loads(args.histories.read_text(encoding="utf-8"))
        quality = json.loads(args.quality_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit("tuning_input_unreadable") from error
    if not isinstance(histories, dict) or not isinstance(quality, dict):
        raise SystemExit("tuning_input_invalid")
    report = recommend_all(
        histories,
        configs=load_configs(),
        shared_reviewer_slots=args.shared_reviewer_slots,
        shared_submit_slots=args.shared_submit_slots,
        quality_report=quality,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
