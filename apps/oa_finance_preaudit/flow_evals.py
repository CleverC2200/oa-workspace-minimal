from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
from statistics import mean
from typing import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .flow_engine import WORKFLOW_KEYS
from apps.oa_approval import flow as approval_flow


SCENARIOS = (
    "normal",
    "business_warning",
    "evidence_insufficient",
    "technical_failure",
)
_OUTCOMES = {"pass", "business_warning", "evidence_insufficient", "technical_failure"}
_STATUSES = {"✅", "❌", "⚠️"}
_FORBIDDEN_KEYS = {"request_id", "attachment_body", "person_name", "company_name"}


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return bool(_FORBIDDEN_KEYS.intersection(value)) or any(
            _has_forbidden_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _validate_suite(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("eval_suite_invalid")
    flows = payload.get("flows")
    cases = payload.get("cases")
    if (
        not isinstance(payload.get("suite_version"), str)
        or not payload["suite_version"]
        or not isinstance(flows, dict)
        or set(flows) != set(WORKFLOW_KEYS)
        or not isinstance(cases, list)
        or _has_forbidden_key(payload)
    ):
        raise ValueError("eval_suite_invalid")
    for names in flows.values():
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError("eval_suite_invalid")

    case_ids: set[str] = set()
    coverage = {workflow_key: set() for workflow_key in WORKFLOW_KEYS}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("eval_case_invalid")
        case_id = case.get("case_id")
        workflow_key = case.get("workflow_key")
        scenario = case.get("scenario")
        source = case.get("source")
        expected = case.get("expected")
        baseline = case.get("baseline")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in case_ids
            or workflow_key not in flows
            or scenario not in SCENARIOS
            or not isinstance(source, dict)
            or not all(
                isinstance(source.get(field), str) and source[field]
                for field in ("kind", "reference", "version")
            )
            or not isinstance(expected, dict)
            or expected.get("outcome") not in _OUTCOMES
            or not isinstance(expected.get("statuses"), list)
            or not isinstance(baseline, dict)
            or isinstance(baseline.get("duration_ms"), bool)
            or not isinstance(baseline.get("duration_ms"), (int, float))
            or baseline["duration_ms"] < 0
            or isinstance(baseline.get("tokens"), bool)
            or not isinstance(baseline.get("tokens"), int)
            or baseline["tokens"] < 0
        ):
            raise ValueError("eval_case_invalid")
        statuses = expected["statuses"]
        if scenario == "technical_failure":
            if statuses or not isinstance(expected.get("stop_code"), str):
                raise ValueError("eval_case_invalid")
        elif (
            len(statuses) != len(flows[workflow_key])
            or not all(status in _STATUSES for status in statuses)
            or expected.get("stop_code") is not None
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "normal" and not (
            (expected["outcome"] == "pass" and set(statuses) == {"✅"})
            or (
                expected["outcome"] == "evidence_insufficient"
                and "⚠️" in statuses
                and "❌" not in statuses
            )
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "business_warning" and (
            expected["outcome"] != "business_warning"
            or not {"❌", "⚠️"}.intersection(statuses)
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "evidence_insufficient" and (
            expected["outcome"] != "evidence_insufficient" or "⚠️" not in statuses
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "technical_failure" and expected["outcome"] != "technical_failure":
            raise ValueError("eval_case_invalid")
        case_ids.add(case_id)
        coverage[workflow_key].add(scenario)
    if any(values != set(SCENARIOS) for values in coverage.values()):
        raise ValueError("eval_suite_incomplete")
    return payload


def load_suite(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("eval_suite_unreadable") from error
    return _validate_suite(payload)


def expected_actuals(suite: dict) -> dict[str, dict]:
    suite = _validate_suite(suite)
    actuals = {}
    for case in suite["cases"]:
        names = (
            []
            if case["scenario"] == "technical_failure"
            else suite["flows"][case["workflow_key"]]
        )
        actuals[case["case_id"]] = {
            "outcome": case["expected"]["outcome"],
            "items": [
                {"name": name, "status": status}
                for name, status in zip(names, case["expected"]["statuses"])
            ],
            "stop_code": case["expected"]["stop_code"],
            "duration_ms": case["baseline"]["duration_ms"],
            "tokens": case["baseline"]["tokens"],
        }
    return actuals


def _performance(values: list[tuple[dict, dict]]) -> dict:
    durations = [
        actual.get("duration_ms")
        for _case, actual in values
        if isinstance(actual.get("duration_ms"), (int, float))
        and not isinstance(actual.get("duration_ms"), bool)
    ]
    tokens = [
        actual.get("tokens")
        for _case, actual in values
        if isinstance(actual.get("tokens"), int)
        and not isinstance(actual.get("tokens"), bool)
    ]
    baseline_durations = [case["baseline"]["duration_ms"] for case, _actual in values]
    baseline_tokens = [case["baseline"]["tokens"] for case, _actual in values]
    duration = round(mean(durations), 2) if durations else "unknown"
    token_value = round(mean(tokens), 2) if tokens else "unknown"
    return {
        "duration_ms": duration,
        "duration_baseline_ms": round(mean(baseline_durations), 2),
        "duration_delta_ms": (
            round(duration - mean(baseline_durations), 2)
            if isinstance(duration, float)
            else "unknown"
        ),
        "tokens": token_value,
        "token_baseline": round(mean(baseline_tokens), 2),
        "token_delta": (
            round(token_value - mean(baseline_tokens), 2)
            if isinstance(token_value, float)
            else "unknown"
        ),
        "token_unknown": len(values) - len(tokens),
    }


def run_replay(suite: dict, actuals: Mapping[str, dict]) -> dict:
    suite = _validate_suite(suite)
    differences: list[dict] = []
    matched = 0
    format_matches = 0
    performance_by_flow: dict[str, list[tuple[dict, dict]]] = {
        workflow_key: [] for workflow_key in WORKFLOW_KEYS
    }
    for case in suite["cases"]:
        case_id = case["case_id"]
        workflow_key = case["workflow_key"]
        actual = actuals.get(case_id)
        if not isinstance(actual, dict):
            differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "missing"}
            )
            continue
        performance_by_flow[workflow_key].append((case, actual))
        case_differences = []
        if actual.get("outcome") != case["expected"]["outcome"]:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "outcome"}
            )
        if actual.get("stop_code") != case["expected"]["stop_code"]:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "stop_code"}
            )
        expected_names = (
            []
            if case["scenario"] == "technical_failure"
            else suite["flows"][workflow_key]
        )
        items = actual.get("items")
        format_ok = isinstance(items, list) and len(items) == len(expected_names)
        if format_ok:
            for index, (name, status) in enumerate(
                zip(expected_names, case["expected"]["statuses"])
            ):
                item = items[index]
                if (
                    not isinstance(item, dict)
                    or item.get("name") != name
                    or item.get("status") not in _STATUSES
                ):
                    format_ok = False
                    case_differences.append(
                        {"case_id": case_id, "workflow_key": workflow_key, "field": "format", "item": name}
                    )
                    continue
                if item["status"] != status:
                    case_differences.append(
                        {"case_id": case_id, "workflow_key": workflow_key, "field": "status", "item": name}
                    )
        else:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "format"}
            )
        if format_ok:
            format_matches += 1
        if not case_differences:
            matched += 1
        differences.extend(case_differences)
    case_count = len(suite["cases"])
    return {
        "schema_version": 1,
        "suite_version": suite["suite_version"],
        "case_count": case_count,
        "matched": matched,
        "accuracy": round(matched / case_count, 4),
        "format_compliance": round(format_matches / case_count, 4),
        "by_flow": {
            workflow_key: _performance(values)
            for workflow_key, values in performance_by_flow.items()
        },
        "differences": differences,
    }


_CASE_METRIC_FIELDS = {
    "schema_version",
    "kind",
    "item_key",
    "workflow_key",
    "closed_at",
    "execution_state",
    "backlog_age_seconds",
    "end_to_end_seconds",
    "active_seconds",
    "waiting_seconds",
    "timing_status",
    "phase_seconds",
    "stage_timestamps",
    "pipeline_evidence",
    "token_usage",
    "business_state",
    "write_state",
    "quality",
    "duplicate_submissions",
    "cross_case_mix",
    "unknown_auto_retries",
    "agent_messages",
    "coordination_seconds",
    "coordination_tokens",
}
_PHASES = {"discover", "collect", "review", "wait", "submit", "postcheck", "close"}
_CLI_OUTCOMES = {
    "success",
    "argument_error",
    "json_contract_error",
    "file_transfer_error",
    "process_start_error",
    "other",
}
_CLI_COMMANDS = {"next-action", "complete-review", "status", "prune", "clean"}
_SETTINGS = {
    "frequency_seconds",
    "review_concurrency",
    "submit_concurrency",
    "review_packet_profile_sha256",
}
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_pipeline_evidence(value: object) -> bool:
    if value == {"status": "unknown"}:
        return True
    fields = {
        "status",
        "write_ready_at",
        "write_start_at",
        "write_queue_depth_at_enqueue",
        "write_wait_attribution",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("status") != "exact"
        or not _nonnegative_integer(value.get("write_queue_depth_at_enqueue"))
    ):
        return False
    try:
        ready_at = datetime.fromisoformat(str(value["write_ready_at"]))
        start_at = datetime.fromisoformat(str(value["write_start_at"]))
    except ValueError:
        return False
    if ready_at.tzinfo is None or start_at.tzinfo is None or ready_at > start_at:
        return False
    attribution = value.get("write_wait_attribution")
    if attribution == {"status": "unknown"}:
        return True
    attribution_fields = {
        "status",
        "total_seconds",
        "writer_busy_seconds",
        "fifo_head_of_line_seconds",
        "controller_scheduling_seconds",
    }
    if not isinstance(attribution, dict) or set(attribution) != attribution_fields:
        return False
    numbers = [
        attribution.get(field)
        for field in attribution_fields - {"status"}
    ]
    return (
        attribution.get("status") == "exact"
        and all(
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            and number >= 0
            for number in numbers
        )
        and math.isclose(
            float(attribution["total_seconds"]),
            float(attribution["writer_busy_seconds"])
            + float(attribution["fifo_head_of_line_seconds"])
            + float(attribution["controller_scheduling_seconds"]),
            abs_tol=1e-6,
        )
    )


def _validate_coverage(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "applicable",
        "covered",
        "total",
    }:
        raise ValueError("acceptance_sample_invalid")
    applicable = value["applicable"]
    covered = value["covered"]
    total = value["total"]
    if (
        not isinstance(applicable, bool)
        or not _nonnegative_integer(covered)
        or not _nonnegative_integer(total)
        or covered > total
        or (applicable and total < 1)
        or (not applicable and (covered != 0 or total != 0))
    ):
        raise ValueError("acceptance_sample_invalid")
    return dict(value)


def _validate_case_metric(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != _CASE_METRIC_FIELDS:
        raise ValueError("acceptance_sample_invalid")
    sample = dict(raw)
    if (
        sample.get("schema_version") != 1
        or sample.get("kind") != "preaudit_case_metric"
        or not isinstance(sample.get("item_key"), str)
        or len(sample["item_key"]) != 64
        or any(character not in "0123456789abcdef" for character in sample["item_key"])
        or sample.get("workflow_key") not in WORKFLOW_KEYS
        or sample.get("execution_state") not in {"closed", "needs_admin"}
        or sample.get("business_state") not in {"pass", "needs_human", "not_available"}
        or sample.get("write_state")
        not in {"not_requested", "submitted", "unknown"}
    ):
        raise ValueError("acceptance_sample_invalid")
    if (
        sample["execution_state"] == "closed"
        and sample["business_state"] not in {"pass", "needs_human"}
    ) or (
        sample["execution_state"] == "needs_admin"
        and sample["business_state"] != "not_available"
    ):
        raise ValueError("acceptance_sample_invalid")
    try:
        closed_at = datetime.fromisoformat(str(sample.get("closed_at") or ""))
    except ValueError as error:
        raise ValueError("acceptance_sample_invalid") from error
    if closed_at.tzinfo is None:
        raise ValueError("acceptance_sample_invalid")
    for field in (
        "end_to_end_seconds",
        "active_seconds",
        "duplicate_submissions",
        "cross_case_mix",
        "unknown_auto_retries",
        "agent_messages",
        "coordination_seconds",
        "coordination_tokens",
    ):
        if field in {
            "duplicate_submissions",
            "cross_case_mix",
            "unknown_auto_retries",
            "agent_messages",
        } and sample.get(field) == "unknown":
            continue
        if not _nonnegative_integer(sample.get(field)):
            raise ValueError("acceptance_sample_invalid")
    if sample.get("backlog_age_seconds") != "unknown" and not _nonnegative_integer(
        sample.get("backlog_age_seconds")
    ):
        raise ValueError("acceptance_sample_invalid")
    waiting_seconds = sample.get("waiting_seconds")
    timing_status = sample.get("timing_status")
    if timing_status not in {"exact", "unknown"} or (
        timing_status == "exact" and not _nonnegative_integer(waiting_seconds)
    ) or (timing_status == "unknown" and waiting_seconds != "unknown"):
        raise ValueError("acceptance_sample_invalid")
    if (
        (
            timing_status == "exact"
            and sample["active_seconds"] + waiting_seconds
            > sample["end_to_end_seconds"]
        )
        or sample["coordination_seconds"] != 0
        or sample["coordination_tokens"] != 0
    ):
        code = (
            "architecture_regression"
            if sample["coordination_seconds"] or sample["coordination_tokens"]
            else "acceptance_sample_invalid"
        )
        raise ValueError(code)
    phases = sample.get("phase_seconds")
    if (
        not isinstance(phases, dict)
        or set(phases) != _PHASES
        or not all(
            value == "unknown" or _nonnegative_integer(value)
            for value in phases.values()
        )
        or (
            sample["timing_status"] == "exact"
            and phases["wait"] != sample["waiting_seconds"]
        )
    ):
        raise ValueError("acceptance_sample_invalid")
    stage_timestamps = sample.get("stage_timestamps")
    if not isinstance(stage_timestamps, dict) or any(
        not isinstance(stage, str)
        or not isinstance(value, dict)
        or value.get("status") not in {"exact", "unknown"}
        or (
            value.get("status") == "exact"
            and (
                set(value) != {"status", "occurred_at"}
                or not isinstance(value.get("occurred_at"), str)
            )
        )
        or (value.get("status") == "unknown" and set(value) != {"status"})
        for stage, value in stage_timestamps.items()
    ):
        raise ValueError("acceptance_sample_invalid")
    if not _valid_pipeline_evidence(sample.get("pipeline_evidence")):
        raise ValueError("acceptance_sample_invalid")
    usage = sample.get("token_usage")
    if usage == {"status": "unknown"}:
        pass
    elif (
        not isinstance(usage, dict)
        or set(usage) != {"status", "input_tokens", "output_tokens", "total_tokens"}
        or usage.get("status") != "exact"
        or not all(
            _nonnegative_integer(usage.get(field))
            for field in ("input_tokens", "output_tokens", "total_tokens")
        )
        or usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]
    ):
        raise ValueError("acceptance_sample_invalid")
    quality = sample.get("quality")
    if not isinstance(quality, dict) or set(quality) != {
        "item_coverage",
        "resource_coverage",
        "page_coverage",
        "format_ok",
    }:
        raise ValueError("acceptance_sample_invalid")
    sample["quality"] = {
        "item_coverage": _validate_coverage(quality["item_coverage"]),
        "resource_coverage": _validate_coverage(quality["resource_coverage"]),
        "page_coverage": _validate_coverage(quality["page_coverage"]),
        "format_ok": quality.get("format_ok"),
    }
    if not isinstance(sample["quality"]["format_ok"], bool):
        raise ValueError("acceptance_sample_invalid")
    return sample


def _nearest_rank(values: list[int], percentile: float) -> int | str:
    if not values:
        return "unknown"
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _coverage_ratio(samples: list[dict], field: str) -> float:
    applicable = [
        sample["quality"][field]
        for sample in samples
        if sample["quality"][field]["applicable"]
    ]
    if not applicable:
        return 1.0
    total = sum(item["total"] for item in applicable)
    covered = sum(item["covered"] for item in applicable)
    return round(covered / total, 4)


def _sum_evidence(samples: list[dict], field: str) -> int | str:
    values = [sample[field] for sample in samples]
    if any(value == "unknown" for value in values):
        return "unknown"
    return sum(values)


def _aggregate_samples(samples: list[dict]) -> dict:
    closed = [sample for sample in samples if sample["execution_state"] == "closed"]
    e2e = [sample["end_to_end_seconds"] for sample in closed]
    backlog = [
        sample["backlog_age_seconds"]
        for sample in closed
        if isinstance(sample["backlog_age_seconds"], int)
    ]
    total_seconds = sum(e2e)
    timing_exact = bool(closed) and all(
        sample["timing_status"] == "exact" for sample in closed
    )
    waiting_seconds = sum(
        sample["waiting_seconds"]
        for sample in closed
        if isinstance(sample["waiting_seconds"], int)
    )
    exact = [
        sample["token_usage"]
        for sample in closed
        if sample["token_usage"]["status"] == "exact"
    ]
    technical_failures = sum(
        sample["execution_state"] == "needs_admin" for sample in samples
    )
    phase_seconds = {
        phase: {
            "p50": _nearest_rank(
                [
                    sample["phase_seconds"][phase]
                    for sample in closed
                    if isinstance(sample["phase_seconds"][phase], int)
                ],
                0.50,
            ),
            "p95": _nearest_rank(
                [
                    sample["phase_seconds"][phase]
                    for sample in closed
                    if isinstance(sample["phase_seconds"][phase], int)
                ],
                0.95,
            ),
        }
        for phase in sorted(_PHASES)
    }
    return {
        "attempt_samples": len(samples),
        "closed_samples": len(closed),
        "sample_eligible": len(closed) >= 20,
        "backlog_age_seconds": {
            "p50": _nearest_rank(backlog, 0.50),
            "p95": _nearest_rank(backlog, 0.95),
        },
        "end_to_end_seconds": {
            "p50": _nearest_rank(e2e, 0.50),
            "p95": _nearest_rank(e2e, 0.95),
        },
        "phase_seconds": phase_seconds,
        "active_seconds": (
            sum(sample["active_seconds"] for sample in closed)
            if timing_exact
            else "unknown"
        ),
        "waiting_seconds": waiting_seconds if timing_exact else "unknown",
        "waiting_ratio": (
            round(waiting_seconds / total_seconds, 4)
            if timing_exact and total_seconds
            else "unknown"
        ),
        "token_usage": {
            "exact_samples": len(exact),
            "unknown_samples": len(closed) - len(exact),
            "input_tokens": sum(item["input_tokens"] for item in exact),
            "output_tokens": sum(item["output_tokens"] for item in exact),
            "total_tokens": sum(item["total_tokens"] for item in exact),
        },
        "business_results": {
            state: sum(sample["business_state"] == state for sample in closed)
            for state in ("pass", "needs_human", "not_available")
        },
        "technical_failures": technical_failures,
        "technical_error_rate": (
            round(technical_failures / len(samples), 4) if samples else 0.0
        ),
        "write_unknown": sum(
            sample["write_state"] == "unknown" for sample in samples
        ),
        "duplicate_submissions": _sum_evidence(closed, "duplicate_submissions"),
        "cross_case_mix": _sum_evidence(closed, "cross_case_mix"),
        "unknown_auto_retries": _sum_evidence(closed, "unknown_auto_retries"),
        "agent_messages": _sum_evidence(closed, "agent_messages"),
        "coordination_seconds": 0,
        "coordination_tokens": 0,
        "item_coverage": _coverage_ratio(closed, "item_coverage"),
        "resource_coverage": _coverage_ratio(closed, "resource_coverage"),
        "page_coverage": _coverage_ratio(closed, "page_coverage"),
        "format_compliance": round(
            sum(sample["quality"]["format_ok"] for sample in closed)
            / len(closed),
            4,
        )
        if closed
        else 0.0,
    }


def _cli_quality(values: object, samples: list[dict]) -> dict:
    if not isinstance(values, list):
        raise ValueError("cli_sample_invalid")
    failure_counts = {
        outcome: 0 for outcome in sorted(_CLI_OUTCOMES - {"success"})
    }
    reproduced: list[bool] = []
    successes = 0
    by_flow = {
        workflow_key: {"sample_count": 0, "failure_count": 0}
        for workflow_key in WORKFLOW_KEYS
    }
    if not samples:
        if values:
            raise ValueError("cli_sample_invalid")
        return {
            "sample_count": 0,
            "sample_eligible": False,
            "successes": 0,
            "failure_counts": failure_counts,
            "failure_count": 0,
            "failure_rate": 0.0,
            "by_flow": by_flow,
            "reproduction_complete": True,
            "manual_reproduction_rate": "unknown",
        }
    window_start = min(
        datetime.fromisoformat(sample["closed_at"]) for sample in samples
    )
    window_end = max(
        datetime.fromisoformat(sample["closed_at"]) for sample in samples
    )
    for raw in values:
        if not isinstance(raw, dict) or set(raw) != {
            "workflow_key",
            "command",
            "occurred_at",
            "outcome",
            "human_reproduction",
        }:
            raise ValueError("cli_sample_invalid")
        try:
            occurred_at = datetime.fromisoformat(str(raw["occurred_at"] or ""))
        except ValueError as error:
            raise ValueError("cli_sample_invalid") from error
        reproduction = raw["human_reproduction"]
        if (
            raw["workflow_key"] not in set(WORKFLOW_KEYS) | {"global"}
            or raw["command"] not in _CLI_COMMANDS
            or raw["outcome"] not in _CLI_OUTCOMES
            or occurred_at.tzinfo is None
            or not window_start <= occurred_at <= window_end
            or reproduction not in {"not_attempted", "succeeded", "failed"}
            or (raw["outcome"] == "success" and reproduction != "not_attempted")
        ):
            raise ValueError("cli_sample_invalid")
        affected_flows = (
            WORKFLOW_KEYS
            if raw["workflow_key"] == "global"
            else {raw["workflow_key"]}
        )
        for workflow_key in affected_flows:
            by_flow[workflow_key]["sample_count"] += 1
        if raw["outcome"] == "success":
            successes += 1
        else:
            failure_counts[raw["outcome"]] += 1
            for workflow_key in affected_flows:
                by_flow[workflow_key]["failure_count"] += 1
            if reproduction in {"succeeded", "failed"}:
                reproduced.append(reproduction == "succeeded")
    failure_count = len(values) - successes
    return {
        "sample_count": len(values),
        "sample_eligible": all(
            metrics["sample_count"] >= 20 for metrics in by_flow.values()
        ),
        "successes": successes,
        "failure_counts": failure_counts,
        "failure_count": failure_count,
        "failure_rate": round(failure_count / len(values), 4) if values else 0.0,
        "by_flow": by_flow,
        "reproduction_complete": len(reproduced) == failure_count,
        "manual_reproduction_rate": (
            round(sum(reproduced) / len(reproduced), 4)
            if reproduced
            else "unknown"
        ),
    }


def _apply_review_timings(samples: list[dict], receipts: object) -> list[dict]:
    if not isinstance(receipts, list):
        raise ValueError("review_timing_invalid")
    by_item = {sample["item_key"]: dict(sample) for sample in samples}
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "item_key",
            "workflow_key",
            "active_seconds",
            "waiting_seconds",
            "source",
        }:
            raise ValueError("review_timing_invalid")
        item_key = receipt["item_key"]
        sample = by_item.get(item_key)
        if (
            sample is None
            or item_key in seen
            or receipt["workflow_key"] != sample["workflow_key"]
            or receipt["source"] != "host_exact"
            or not _nonnegative_integer(receipt["active_seconds"])
            or not _nonnegative_integer(receipt["waiting_seconds"])
            or receipt["active_seconds"] + receipt["waiting_seconds"]
            > sample["end_to_end_seconds"]
        ):
            raise ValueError("review_timing_invalid")
        seen.add(item_key)
        phases = dict(sample["phase_seconds"])
        phases["wait"] = receipt["waiting_seconds"]
        by_item[item_key] = {
            **sample,
            "active_seconds": receipt["active_seconds"],
            "waiting_seconds": receipt["waiting_seconds"],
            "timing_status": "exact",
            "phase_seconds": phases,
        }
    return [by_item[sample["item_key"]] for sample in samples]


def _apply_runtime_evidence(
    by_flow: dict,
    overall: dict,
    evidence: object,
    samples: list[dict],
) -> None:
    if evidence is None:
        return
    fields = {
        "duplicate_submissions",
        "cross_case_mix",
        "unknown_auto_retries",
        "agent_messages",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {"schema_version", "kind", "window_start", "window_end", "by_flow"}
        or evidence.get("schema_version") != 1
        or evidence.get("kind") != "preaudit_runtime_evidence"
        or not isinstance(evidence.get("by_flow"), dict)
        or set(evidence["by_flow"]) != set(WORKFLOW_KEYS)
    ):
        raise ValueError("runtime_evidence_invalid")
    if not samples:
        raise ValueError("runtime_evidence_invalid")
    expected_start = min(
        datetime.fromisoformat(sample["closed_at"]) for sample in samples
    )
    expected_end = max(
        datetime.fromisoformat(sample["closed_at"]) for sample in samples
    )
    try:
        evidence_start = datetime.fromisoformat(str(evidence["window_start"]))
        evidence_end = datetime.fromisoformat(str(evidence["window_end"]))
    except ValueError as error:
        raise ValueError("runtime_evidence_invalid") from error
    if evidence_start != expected_start or evidence_end != expected_end:
        raise ValueError("runtime_evidence_invalid")
    for workflow_key, values in evidence["by_flow"].items():
        if (
            not isinstance(values, dict)
            or set(values) != fields
            or not all(_nonnegative_integer(value) for value in values.values())
        ):
            raise ValueError("runtime_evidence_invalid")
        by_flow[workflow_key].update(values)
    for field in fields:
        overall[field] = sum(by_flow[key][field] for key in WORKFLOW_KEYS)


_FINANCIAL_SCENARIOS = {
    "ordinary_payment", "multiple_payment_refund_overclaim", "relationship_unknown",
    "shared_payment", "confirmed_exception_with_unknown",
}


def _read_bound_artifact(reference: object, *, document: bool = True) -> object:
    if not isinstance(reference, dict) or set(reference) != {"file", "sha256"}:
        raise ValueError("financial_review_invalid")
    path, digest = reference["file"], reference["sha256"]
    if (not isinstance(path, str) or not Path(path).is_absolute()
            or not isinstance(digest, str) or len(digest) != 64):
        raise ValueError("financial_review_invalid")
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("financial_review_evidence_changed")
        return json.loads(raw) if document else raw
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("financial_review_invalid") from error


def _historical_crosscheck(checklist: dict) -> str:
    refs = checklist.get("history_refs")
    if not isinstance(refs, list) or not refs:
        return "missing"
    materials = [_read_bound_artifact(ref) for ref in checklist["source_refs"]
                 if Path(ref["file"]).name == "material.json"]
    if len(materials) != 1 or not isinstance(materials[0], dict) or not materials[0].get("request_id"):
        raise ValueError("financial_history_binding_invalid")
    request = str(materials[0]["request_id"])
    for ref in refs:
        context = _read_bound_artifact(ref)
        workflow = context.get("workflow") if isinstance(context, dict) else None
        handled = context.get("handled_row") if isinstance(context, dict) else None
        if (not isinstance(workflow, dict) or not isinstance(handled, dict)
                or str(workflow.get("requestId")) != request
                or str(handled.get("requestId")) != request
                or not isinstance(workflow.get("workflowRequestLogs"), list)
                or not workflow["workflowRequestLogs"]
                or not all(isinstance(log, dict) for log in workflow["workflowRequestLogs"])):
            raise ValueError("financial_history_binding_invalid")
        try:
            actual = approval_flow.workflow_identity(workflow)
            handled_actual = approval_flow.workflow_identity(handled)
            if (not actual["workflow_id"] or not actual["workflow_name"]
                    or actual != handled_actual):
                raise ValueError("financial_history_binding_invalid")
            configs = approval_flow.load_all_configs()
            selected = approval_flow.select_config_for_workflow(request, workflow, configs)
            selected_handled = approval_flow.select_config_for_workflow(request, handled, configs)
            selected_material = approval_flow.select_config_for_workflow(
                request, {"workflowName": materials[0].get("workflow_name")}, configs)
            if not (selected["flow_key"] == selected_handled["flow_key"]
                    == selected_material["flow_key"] == materials[0].get("workflow_key")
                    == checklist.get("workflow_key")):
                raise ValueError("financial_history_binding_invalid")
        except (RuntimeError, SystemExit, OSError, ValueError, KeyError, TypeError):
            # Original IDs/names must remain in private artifacts, not exceptions.
            raise ValueError("financial_history_binding_invalid") from None
    checks = checklist.get("historical_checks")
    expected_ids = {check["id"] for check in checklist["checks"]}
    if (not isinstance(checks, list) or len(checks) != len(expected_ids)
            or any(not isinstance(check, dict) for check in checks)
            or {check.get("id") for check in checks} != expected_ids
            or any(check.get("outcome") not in {"consistent", "conflict", "uninformative"}
                   or check.get("basis") != "original_material_and_rules"
                   or not isinstance(check.get("reason"), str) or not check["reason"] for check in checks)):
        raise ValueError("financial_history_binding_invalid")
    return "conflict" if any(check["outcome"] == "conflict" for check in checks) else "complete"


def _flow_config_pins() -> dict:
    return {path.relative_to(approval_flow.CONFIG_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(approval_flow.CONFIG_DIR.glob("*.json"))}


def prepare_financial_checklist(checklist_reference: dict, destination: Path) -> dict:
    """Create a new preparation with exact rule bytes; never alter old references."""
    import os
    checklist = _read_bound_artifact(checklist_reference)
    rules = [(ref, _read_bound_artifact(ref, document=False)) for ref in checklist["rule_refs"]]
    path = Path(destination).absolute()
    snapshot = path.with_name(path.stem + "-rules")
    snapshot.mkdir(mode=0o700)
    references = []
    for index, (reference, raw) in enumerate(rules):
        rule = snapshot / (str(index) + "-" + Path(reference["file"]).name)
        with rule.open("xb") as stream:
            os.chmod(rule, 0o400)
            stream.write(raw)
        references.append({"file": str(rule), "sha256": reference["sha256"]})
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump({**checklist, "rule_refs": references}, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def freeze_financial_expectations(checklist_reference: dict, destination: Path) -> dict:
    """Freeze source-based historical crosschecks now; no caller-provided time/signature."""
    checklist = _read_bound_artifact(checklist_reference)
    for ref in checklist["source_refs"] + checklist["rule_refs"]:
        _read_bound_artifact(ref, document=False)
    status = _historical_crosscheck(checklist)
    path = Path(destination).absolute()
    payload = {"schema_version": 1, "kind": "preaudit_expectations_freeze",
               "checklist_sha256": checklist_reference["sha256"],
               "historical_crosscheck": status, "frozen_at": datetime.now().astimezone().isoformat(),
               "flow_configs": _flow_config_pins()}
    with path.open("x", encoding="utf-8") as stream:
        import os
        os.chmod(path, 0o600)
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _financial_replay_binding(review: dict, checklist: dict, assessment: dict) -> dict:
    """Consume the archive reporter's validated machine facts, never a caller label."""
    unknown = {"status": "not_established", "start": {"status": "not_established"}}
    reference = review.get("archive")
    if reference is None:
        return unknown
    manifest = _read_bound_artifact(reference)
    path = Path(reference["file"])
    if path.name != "manifest.json" or not isinstance(manifest, dict):
        raise ValueError("financial_review_binding_invalid")
    from .review_batch_report import build_report

    report = build_report(path.parent)
    binding = report["financial_binding"]
    fingerprint = report["archive_fingerprint"]
    if (fingerprint != reference["sha256"]
            or (checklist.get("archive_manifest_sha256") is not None
                and checklist["archive_manifest_sha256"] != fingerprint)
            or assessment.get("archive_manifest_sha256") != fingerprint
            or review["item_key"] != binding.get("item_key")
            or assessment.get("item_key") != binding.get("item_key")
            or checklist["workflow_key"] != report["scope"]["workflow"]
            or assessment.get("result_sha256") != binding.get("result_sha256")
            or sorted(ref["sha256"] for ref in checklist["source_refs"])
            != binding.get("original_file_sha256")
            or sorted(ref["sha256"] for ref in checklist["rule_refs"])
            != binding.get("required_rule_file_sha256")):
        raise ValueError("financial_review_binding_invalid")
    required = {binding["result_sha256"]}
    if binding["status"] == "verified_consumed_correspondence":
        required.update(binding[field] for field in (
            "raw_decision_sha256", "consumed_decision_sha256", "remark_sha256", "receipt_sha256",
        ))
    if not required.issubset({ref["sha256"] for ref in assessment["output_refs"]}):
        raise ValueError("financial_review_binding_invalid")
    start = report["timing"]["start_receipt"]
    if start["status"] == "bound_source_host_receipt" and review.get("replay_started_at") is not None:
        try:
            same = datetime.fromisoformat(review["replay_started_at"]) == datetime.fromisoformat(start["replay_started_at"])
        except (TypeError, ValueError) as error:
            raise ValueError("financial_review_binding_invalid") from error
        if not same:
            raise ValueError("financial_review_binding_invalid")
    return {**binding, "start": start, "final_contract": report["acceptance"]["final_contract"]}


def build_financial_quality_report(reviews: object) -> dict:
    """Bind independent manual claim checks to original evidence and consumed output.

    This reads assessments; it does not infer financial truth from model text.
    Source, rules, checklist, output and confirmation stay in private artifacts.
    """
    if not isinstance(reviews, list):
        raise ValueError("financial_review_invalid")
    cases, keys, covered = [], set(), set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("financial_review_invalid")
        key = review.get("item_key")
        if (not isinstance(key, str) or len(key) != 64
                or any(char not in "0123456789abcdef" for char in key) or key in keys):
            raise ValueError("financial_review_invalid")
        keys.add(key)
        checklist = _read_bound_artifact(review.get("checklist"))
        assessment = _read_bound_artifact(review.get("assessment"))
        if (not isinstance(checklist, dict) or checklist.get("schema_version") != 1
                or checklist.get("workflow_key") not in WORKFLOW_KEYS
                or checklist.get("source_kind") not in {"real_archive", "synthetic"}
                or not isinstance(checklist.get("scenario"), str)
                or not isinstance(assessment, dict) or assessment.get("schema_version") != 1
                or assessment.get("method") != "independent_manual"
                or assessment.get("reviewer_role") not in {"engineering_draft", "finance", "historical_crosscheck"}
                or not isinstance(assessment.get("reviewer_record"), str)
                or not assessment["reviewer_record"]
                or assessment.get("checklist_sha256") != review["checklist"]["sha256"]):
            raise ValueError("financial_review_invalid")
        for owner, field in ((checklist, "source_refs"), (checklist, "rule_refs"),
                             (assessment, "output_refs")):
            references = owner.get(field)
            if not isinstance(references, list) or not references:
                raise ValueError("financial_review_invalid")
            for reference in references:
                _read_bound_artifact(reference, document=False)
        expected, checked = checklist.get("checks"), assessment.get("checks")
        if not isinstance(expected, list) or not expected or not isinstance(checked, list):
            raise ValueError("financial_review_invalid")
        for check in expected:
            if (not isinstance(check, dict) or not isinstance(check.get("id"), str)
                    or not check["id"] or not isinstance(check.get("expected"), str)
                    or not check["expected"]):
                raise ValueError("financial_review_invalid")
        expected_ids = [check["id"] for check in expected]
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("financial_review_invalid")
        for check in checked:
            if (not isinstance(check, dict) or check.get("outcome") not in {"pass", "fail", "unverified"}
                    or not isinstance(check.get("reason"), str) or not check["reason"]):
                raise ValueError("financial_review_invalid")
        checked_ids = [check.get("id") for check in checked]
        if len(checked_ids) != len(expected_ids) or set(checked_ids) != set(expected_ids):
            raise ValueError("financial_review_invalid")
        failed = [check["id"] for check in checked if check["outcome"] == "fail"]
        unknown = [check["id"] for check in checked if check["outcome"] == "unverified"]
        binding = _financial_replay_binding(review, checklist, assessment)
        confirmed = False
        if review.get("confirmation") is not None:
            record = _read_bound_artifact(review["confirmation"])
            try:
                approved = datetime.fromisoformat(record["approved_at"])
                trusted_start = binding["start"].get("replay_started_at")
                started = datetime.fromisoformat(trusted_start) if trusted_start else None
                confirmed = (binding["start"]["status"] == "bound_source_host_receipt"
                             and started is not None
                             and record.get("role") == "finance"
                             and bool(record.get("reviewer_record"))
                             and record.get("checklist_sha256") == review["checklist"]["sha256"]
                             and approved.tzinfo is not None and started.tzinfo is not None
                             and approved <= started)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("financial_review_invalid") from error
        result_reviewed_by_finance = False
        result_audit_complete = False
        if assessment["reviewer_role"] in {"finance", "historical_crosscheck"}:
            try:
                reviewed_at = datetime.fromisoformat(assessment["reviewed_at"])
                closed_at = binding.get("closed_at")
                closed = datetime.fromisoformat(closed_at) if closed_at and closed_at != "not_established" else None
                result_audit_complete = (binding["status"] == "verified_consumed_correspondence"
                                             and reviewed_at.tzinfo is not None
                                             and closed is not None and closed.tzinfo is not None
                                             and closed <= reviewed_at <= datetime.now().astimezone())
                result_reviewed_by_finance = result_audit_complete and assessment["reviewer_role"] == "finance"
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("financial_review_invalid") from error
        historical = _historical_crosscheck(checklist)
        frozen_before = False
        if review.get("expectations") is not None:
            freeze = _read_bound_artifact(review["expectations"])
            try:
                frozen = datetime.fromisoformat(freeze["frozen_at"])
                started_value = binding["start"].get("replay_started_at")
                started = datetime.fromisoformat(started_value) if started_value else None
                frozen_before = (freeze.get("kind") == "preaudit_expectations_freeze"
                                 and freeze.get("checklist_sha256") == review["checklist"]["sha256"]
                                 and freeze.get("historical_crosscheck") == historical == "complete"
                                 and freeze.get("flow_configs") == _flow_config_pins()
                                 and frozen.tzinfo is not None and started is not None
                                 and frozen <= started)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("financial_review_invalid") from error
        workflow, scenario = checklist["workflow_key"], checklist["scenario"]
        if workflow == "business_entertainment" and checklist["source_kind"] == "real_archive":
            covered.add(scenario)
        required_items = binding.get("required_review_item_keys", [])
        covered_items = {check.get("item_key") for check in expected if check.get("item_key") in required_items}
        complete_scope = (checklist.get("review_scope") == "complete_financial_review"
                          and bool(required_items) and covered_items == set(required_items))
        metric = binding.get("case_metric")
        window_binding = {field: metric[field] for field in (
            "closed_at", "stage_timestamps", "execution_state", "business_state",
            "write_state", "quality", "pipeline_evidence",
        )} if isinstance(metric, dict) else None
        cases.append({"item_key": key, "workflow_key": workflow, "scenario": scenario,
                      "source_kind": checklist["source_kind"],
                      "complete_review_scope": complete_scope,
                      "missing_review_items": sorted(set(required_items) - covered_items),
                      "historical_crosscheck": historical,
                      "expectations_frozen_before_replay": frozen_before,
                      "result_audit_complete": result_audit_complete,
                      "binding_status": binding["status"],
                      "original_final_contract": binding.get("final_contract", "not_established"),
                      "window_binding": window_binding,
                      "replay_start_status": binding["start"]["status"],
                      "checklist_sha256": review["checklist"]["sha256"],
                      "assessment_sha256": review["assessment"]["sha256"],
                      "verdict": "fail" if failed else "unverified" if unknown else "pass",
                      "check_outcomes": {check["id"]: check["outcome"] for check in checked},
                      "failed_checks": failed, "unverified_checks": unknown,
                      "financial_confirmed_before_replay": confirmed,
                      "result_reviewed_by_finance": result_reviewed_by_finance})
    confirmed = bool(cases) and all(case["financial_confirmed_before_replay"] for case in cases)
    missing = sorted(_FINANCIAL_SCENARIOS - covered)
    return {"schema_version": 1, "kind": "preaudit_financial_quality_report",
            "cases": cases, "financial_confirmation": "confirmed" if confirmed else "pending",
            "missing_entertainment_scenarios": missing,
            "eligible": bool(cases) and not missing and all(
                case["source_kind"] == "real_archive"
                and case["complete_review_scope"]
                and case["binding_status"] == "verified_consumed_correspondence"
                and case["original_final_contract"] == "verified_snapshot_schema"
                and case["window_binding"] is not None
                and case["verdict"] == "pass" and case["result_audit_complete"]
                and case["historical_crosscheck"] == "complete"
                and case["expectations_frozen_before_replay"] for case in cases)}


def build_acceptance_report(
    attempt_samples: object,
    *,
    replay_report: object,
    cli_samples: object,
    review_timing_receipts: object = (),
    runtime_evidence: object = None,
    financial_reviews: object = None,
) -> dict:
    if not isinstance(attempt_samples, list) or not isinstance(replay_report, dict):
        raise ValueError("acceptance_input_invalid")
    samples = [_validate_case_metric(sample) for sample in attempt_samples]
    samples = _apply_review_timings(samples, list(review_timing_receipts))
    item_keys = [sample["item_key"] for sample in samples]
    if len(item_keys) != len(set(item_keys)):
        raise ValueError("acceptance_sample_duplicate")
    by_flow_samples = {
        workflow_key: [
            sample for sample in samples if sample["workflow_key"] == workflow_key
        ]
        for workflow_key in WORKFLOW_KEYS
    }
    by_day_samples: dict[str, list[dict]] = {}
    for sample in samples:
        day = datetime.fromisoformat(sample["closed_at"]).astimezone(
            _LOCAL_TIMEZONE
        ).strftime("%Y-%m-%d")
        by_day_samples.setdefault(day, []).append(sample)
    by_flow = {
        workflow_key: _aggregate_samples(values)
        for workflow_key, values in by_flow_samples.items()
    }
    by_day = {
        day: _aggregate_samples(values)
        for day, values in sorted(by_day_samples.items())
    }
    overall = _aggregate_samples(samples)
    _apply_runtime_evidence(by_flow, overall, runtime_evidence, samples)
    replay_ok = (
        replay_report.get("accuracy") == 1.0
        and replay_report.get("format_compliance") == 1.0
        and replay_report.get("differences") == []
    )
    quality_checks = {
        "item_coverage_100": overall["item_coverage"] == 1.0,
        "resource_coverage_100": overall["resource_coverage"] == 1.0,
        "page_coverage_100": overall["page_coverage"] == 1.0,
        "format_100": overall["format_compliance"] == 1.0,
        "golden_replay_100": replay_ok,
        "duplicate_submission_0": overall["duplicate_submissions"] == 0,
        "cross_case_mix_0": overall["cross_case_mix"] == 0,
        "unknown_auto_retry_0": overall["unknown_auto_retries"] == 0,
        "agent_messages_0": overall["agent_messages"] == 0,
        "coordination_seconds_0": True,
        "coordination_tokens_0": True,
    }
    financial_quality = None
    if financial_reviews is not None:
        financial_quality = build_financial_quality_report(financial_reviews)
        assessed = {case["item_key"]: case for case in financial_quality["cases"]}
        sample_by_key = {sample["item_key"]: sample for sample in samples}
        if any(key not in sample_by_key or sample_by_key[key]["workflow_key"] != case["workflow_key"]
               for key, case in assessed.items()):
            raise ValueError("financial_review_sample_mismatch")
        for key, case in assessed.items():
            protected = case.get("window_binding")
            if protected is not None and any(sample_by_key[key].get(field) != value for field, value in protected.items()):
                raise ValueError("financial_review_sample_mismatch")
        closed_keys = {sample["item_key"] for sample in samples if sample["execution_state"] == "closed"}
        quality_checks["financial_quality_100"] = (
            financial_quality["eligible"] and closed_keys == set(assessed)
        )
    quality_gate = {**quality_checks, "passed": all(quality_checks.values())}
    target_gate = {
        "p95_end_to_end_below_900": all(
            value["end_to_end_seconds"]["p95"] != "unknown"
            and value["end_to_end_seconds"]["p95"] < 900
            for value in by_flow.values()
        ),
        "waiting_ratio_below_0_20": all(
            value["waiting_ratio"] != "unknown"
            and value["waiting_ratio"] < 0.20
            for value in by_flow.values()
        ),
        "technical_error_rate_at_most_0_02": all(
            value["technical_error_rate"] <= 0.02 for value in by_flow.values()
        ),
    }
    sample_eligible = all(value["sample_eligible"] for value in by_flow.values())
    token_eligible = (
        sample_eligible
        and overall["closed_samples"] > 0
        and overall["token_usage"]["unknown_samples"] == 0
    )
    cli_quality = _cli_quality(cli_samples, samples)
    cli_primary = cli_quality["failure_count"] > overall["technical_failures"]
    mcp_eligible = (
        sample_eligible
        and quality_gate["passed"]
        and cli_quality["sample_eligible"]
        and cli_quality["reproduction_complete"]
        and cli_quality["manual_reproduction_rate"] == 1.0
        and cli_quality["failure_rate"] > 0.02
        and cli_primary
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_acceptance_report",
        "cases": samples,
        "overall": overall,
        "by_flow": by_flow,
        "by_day": by_day,
        "quality_gate": quality_gate,
        "target_gate": target_gate,
        "sample_eligible": sample_eligible,
        "token_tuning": {
            "eligible": token_eligible,
            "reason": "exact" if token_eligible else "exact_usage_missing",
        },
        "cli_quality": cli_quality,
        "mcp_admission": {
            "decision": (
                "propose_separate_stdio_mcp_issue"
                if mcp_eligible
                else "do_not_introduce_mcp"
            ),
            "cli_is_primary_failure_source": cli_primary,
        },
        "applied": False,
        **({"financial_quality": financial_quality} if financial_quality is not None else {}),
    }


def _validate_settings(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _SETTINGS:
        raise ValueError("experiment_settings_invalid")
    if not all(
        isinstance(value[field], int)
        and not isinstance(value[field], bool)
        and value[field] >= 1
        for field in ("frequency_seconds", "review_concurrency", "submit_concurrency")
    ):
        raise ValueError("experiment_settings_invalid")
    profile = value["review_packet_profile_sha256"]
    if (
        not isinstance(profile, str)
        or len(profile) != 64
        or any(character not in "0123456789abcdef" for character in profile)
    ):
        raise ValueError("experiment_settings_invalid")
    return dict(value)


def _experiment_performance(report: dict) -> dict:
    overall = report["overall"]
    usage = overall["token_usage"]
    token_usage = {
        "status": (
            "exact"
            if usage["exact_samples"] > 0 and usage["unknown_samples"] == 0
            else "unknown"
        ),
        "exact_samples": usage["exact_samples"],
        "unknown_samples": usage["unknown_samples"],
    }
    if token_usage["status"] == "exact":
        token_usage.update(
            {
                field: usage[field]
                for field in ("input_tokens", "output_tokens", "total_tokens")
            }
        )
    return {
        "end_to_end_seconds": dict(overall["end_to_end_seconds"]),
        "token_usage": token_usage,
    }


def evaluate_experiment(
    before: object,
    after: object,
    *,
    before_settings: object,
    after_settings: object,
) -> dict:
    if (
        not isinstance(before, dict)
        or before.get("kind") != "preaudit_acceptance_report"
        or not isinstance(after, dict)
        or after.get("kind") != "preaudit_acceptance_report"
    ):
        raise ValueError("experiment_report_invalid")
    baseline_settings = _validate_settings(before_settings)
    candidate_settings = _validate_settings(after_settings)
    changed = [
        field
        for field in sorted(_SETTINGS)
        if baseline_settings[field] != candidate_settings[field]
    ]
    if len(changed) != 1:
        raise ValueError("experiment_variable_invalid")
    changed_variable = changed[0]
    reasons: list[str] = []
    if not after.get("quality_gate", {}).get("passed"):
        reasons.append("quality_regression")
    after_overall = after["overall"]
    before_overall = before["overall"]
    safety_fields = (
        "duplicate_submissions",
        "cross_case_mix",
        "unknown_auto_retries",
        "agent_messages",
    )
    if any(
        isinstance(after_overall[field], int) and after_overall[field] > 0
        for field in safety_fields
    ):
        reasons.append("submission_safety_regression")
    if (
        after_overall["technical_error_rate"]
        - before_overall["technical_error_rate"]
        >= 0.02
    ):
        reasons.append("technical_error_rate_regression")
    before_p95 = before_overall["end_to_end_seconds"]["p95"]
    after_p95 = after_overall["end_to_end_seconds"]["p95"]
    if (
        isinstance(before_p95, int)
        and isinstance(after_p95, int)
        and after_p95 > before_p95 * 1.20
    ):
        reasons.append("p95_end_to_end_regression")
    before_unknown = (
        before_overall["write_unknown"]
        + before_overall["token_usage"]["unknown_samples"]
        + sum(before_overall[field] == "unknown" for field in safety_fields)
    )
    after_unknown = (
        after_overall["write_unknown"]
        + after_overall["token_usage"]["unknown_samples"]
        + sum(after_overall[field] == "unknown" for field in safety_fields)
    )
    if after_unknown > before_unknown:
        reasons.append("unknown_increase")
    if reasons:
        decision = "rollback"
    elif not before.get("sample_eligible") or not after.get("sample_eligible"):
        decision = "insufficient_samples"
        reasons.append("insufficient_samples")
    else:
        decision = "keep"
    token_eligible = (
        before["token_tuning"]["eligible"]
        and after["token_tuning"]["eligible"]
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_experiment_report",
        "changed_variable": changed_variable,
        "before_value": baseline_settings[changed_variable],
        "after_value": candidate_settings[changed_variable],
        "decision": decision,
        "reasons": reasons,
        "token_tuning": {
            "eligible": token_eligible,
            "reason": "exact" if token_eligible else "exact_usage_missing",
        },
        "performance": {
            "before": _experiment_performance(before),
            "after": _experiment_performance(after),
        },
        "mcp_admission": after["mcp_admission"],
        "applied": False,
    }
