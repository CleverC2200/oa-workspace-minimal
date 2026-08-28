from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo

from apps.oa_approval import writer_outcomes

from . import observability, retention


SCHEMA_VERSION = 1
_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "general_expense",
    "packaging_layout_approval",
}
_FAILURE_CODES = {
    "agent_timing_invalid",
    "frozen_material_changed",
    "frozen_packet_changed",
    "material_collection_failed",
    "observability_incomplete",
    "observability_write_failed",
    "persistent_state_invalid",
    "state_invalid",
    "submission_adapter_unavailable",
    "write_authorization_invalid",
    "writer_result_unknown",
    *writer_outcomes.FAILURE_CODES,
}
ITEM_LOCAL_MATERIAL_FAILURE_DETAILS = frozenset(
    {
        "attachment_download_capacity_timeout",
        "attachment_download_failed",
        "attachment_download_incomplete",
        "attachment_download_launch_timeout",
        "attachment_download_timeout",
        "invoice_buyer_read_failed",
        "travel_attachment_discovery_failed",
        "travel_attachment_failed",
        "travel_attachment_resources_incomplete",
    }
)
_MANAGED_PATH = re.compile(
    r"^(?:(?:\.archive/)?(?:travel_expense|business_entertainment|"
    r"business_support_fee|small_marketing_fee|packaging_layout_approval)/"
    r"cases/[0-9a-f]{64}/attempt-[1-9][0-9]*|"
    r"\.metrics/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json)$"
)
_PLAN_MAX_AGE = timedelta(minutes=5)
_DAILY_RETENTION_DAYS = 180
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_CLI_LOG = Path(".metrics/cli-invocations.json")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("runtime_state_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("runtime_state_invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("runtime_state_invalid")
    return parsed


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime_state_invalid") from error
    attempt_root = path.parent
    try:
        expected_attempt = int(attempt_root.name.removeprefix("attempt-"))
    except ValueError as error:
        raise ValueError("runtime_state_invalid") from error
    expected_case = attempt_root.parent.name
    expected_workflow = attempt_root.parent.parent.parent.name
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != SCHEMA_VERSION
        or state.get("kind") != "preaudit_action"
        or state.get("workflow_key") not in _WORKFLOW_KEYS
        or state.get("execution_state")
        not in {"collecting", "review_required", "needs_admin", "closed"}
        or state.get("business_state")
        not in {"not_available", "pass", "needs_human"}
        or state.get("write_state")
        not in {"not_requested", "authorized", "submitting", "submitted", "unknown"}
        or not isinstance(state.get("case_key"), str)
        or not isinstance(state.get("attempt"), int)
        or isinstance(state.get("attempt"), bool)
        or state["attempt"] < 1
        or state.get("workflow_key") != expected_workflow
        or state.get("case_key") != expected_case
        or state.get("attempt") != expected_attempt
        or not re.fullmatch(r"[0-9a-f]{64}", expected_case)
    ):
        raise ValueError("runtime_state_invalid")
    request_id = state.get("request_id")
    received_at = state.get("received_at")
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or not isinstance(received_at, str)
        or not received_at.strip()
        or hashlib.sha256(
            f"{request_id}\x1f{received_at}".encode("utf-8")
        ).hexdigest()
        != expected_case
    ):
        raise ValueError("runtime_state_identity_invalid")
    _timestamp(state.get("created_at"))
    return state


def _item_key(case_key: str, attempt: int) -> str:
    raw = f"{case_key}\x1f{attempt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _attempt_roots(root: Path, *, archived: bool = False) -> list[Path]:
    base = root / ".archive" if archived else root
    if base.is_symlink():
        raise ValueError("runtime_path_symlink")
    paths: list[Path] = []
    for workflow_key in sorted(_WORKFLOW_KEYS):
        workflow_root = base / workflow_key
        cases_root = workflow_root / "cases"
        if workflow_root.is_symlink() or cases_root.is_symlink():
            raise ValueError("runtime_path_symlink")
        if not cases_root.is_dir():
            continue
        for case_root in cases_root.iterdir():
            if case_root.is_symlink():
                raise ValueError("runtime_path_symlink")
            if not case_root.is_dir():
                continue
            for attempt_root in case_root.glob("attempt-*"):
                if attempt_root.is_symlink():
                    raise ValueError("runtime_path_symlink")
                if not attempt_root.is_dir():
                    continue
                if any(path.is_symlink() for path in attempt_root.rglob("*")):
                    raise ValueError("runtime_path_symlink")
                paths.append(attempt_root)
    return sorted(paths, key=str)


def _age_seconds(created_at: datetime, now: datetime) -> int:
    return max(0, int((now - created_at).total_seconds()))


def _received_at(state: dict) -> datetime | None:
    try:
        return _timestamp(state.get("received_at"))
    except ValueError:
        return None


def _activity_at(attempt_root: Path, state: dict) -> datetime:
    created_at = _timestamp(state["created_at"])
    latest = observability.latest_event_at(attempt_root / "observability.json")
    return max(created_at, latest) if latest is not None else created_at


def _coverage_projection(value: object) -> dict:
    if not isinstance(value, dict):
        return {"applicable": False, "covered": 0, "total": 0}
    applicable = value.get("applicable")
    covered = value.get("covered")
    total = value.get("total")
    if (
        not isinstance(applicable, bool)
        or isinstance(covered, bool)
        or not isinstance(covered, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or covered < 0
        or total < 0
        or covered > total
        or (not applicable and (covered != 0 or total != 0))
    ):
        return {"applicable": False, "covered": 0, "total": 0}
    return {"applicable": applicable, "covered": covered, "total": total}


def _quality_from_receipt(attempt_root: Path, *, closed: bool) -> dict:
    fallback = {
        "item_coverage": {"applicable": False, "covered": 0, "total": 0},
        "resource_coverage": {"applicable": False, "covered": 0, "total": 0},
        "page_coverage": {"applicable": False, "covered": 0, "total": 0},
        "format_ok": False,
    }
    if not closed:
        return fallback
    try:
        receipt = json.loads(
            (attempt_root / "execution-receipt.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return fallback
    quality = receipt.get("quality") if isinstance(receipt, dict) else None
    if not isinstance(quality, dict):
        return fallback
    return {
        "item_coverage": _coverage_projection(quality.get("item_coverage")),
        "resource_coverage": _coverage_projection(
            quality.get("resource_coverage")
        ),
        "page_coverage": _coverage_projection(quality.get("page_coverage")),
        "format_ok": quality.get("format_ok") is True,
    }


def _pipeline_evidence(
    attempt_root: Path,
    *,
    item_key: str,
    workflow_key: str,
    attempt: int,
) -> dict:
    path = attempt_root / "pipeline-metrics.json"
    if path.is_symlink() or not path.is_file():
        return {"status": "unknown"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        attribution = payload["write_wait_attribution"]
        expected_fields = {
            "schema_version",
            "kind",
            "status",
            "item_key",
            "workflow_key",
            "attempt",
            "event_suffix",
            "write_ready_at",
            "write_start_at",
            "write_queue_depth_at_enqueue",
            "write_wait_attribution",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or not isinstance(payload.get("schema_version"), int)
            or isinstance(payload.get("schema_version"), bool)
            or payload["schema_version"] != SCHEMA_VERSION
            or payload.get("kind") != "preaudit_pipeline_metrics"
            or payload.get("status") != "exact"
            or payload.get("item_key") != item_key
            or payload.get("workflow_key") != workflow_key
            or not isinstance(payload.get("attempt"), int)
            or isinstance(payload.get("attempt"), bool)
            or payload["attempt"] != attempt
            or not isinstance(payload.get("event_suffix"), int)
            or isinstance(payload.get("event_suffix"), bool)
            or payload["event_suffix"] < 1
            or not isinstance(payload.get("write_queue_depth_at_enqueue"), int)
            or isinstance(payload.get("write_queue_depth_at_enqueue"), bool)
            or payload["write_queue_depth_at_enqueue"] < 0
            or not _valid_write_wait_attribution(attribution)
        ):
            raise ValueError
        write_ready_at = _timestamp(payload["write_ready_at"])
        write_start_at = _timestamp(payload["write_start_at"])
        if write_ready_at > write_start_at:
            raise ValueError
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "unknown"}
    return {
        "status": "exact",
        "write_ready_at": write_ready_at.isoformat(),
        "write_start_at": write_start_at.isoformat(),
        "write_queue_depth_at_enqueue": payload[
            "write_queue_depth_at_enqueue"
        ],
        "write_wait_attribution": attribution,
    }


def _valid_write_wait_attribution(value: object) -> bool:
    if value == {"status": "unknown"}:
        return True
    fields = {
        "status",
        "total_seconds",
        "writer_busy_seconds",
        "fifo_head_of_line_seconds",
        "controller_scheduling_seconds",
    }
    if not isinstance(value, dict) or set(value) != fields:
        return False
    if value.get("status") == "unknown":
        return all(value.get(field) == "unknown" for field in fields - {"status"})
    numbers = [value.get(field) for field in fields - {"status"}]
    if value.get("status") != "exact" or not all(
        isinstance(number, (int, float))
        and not isinstance(number, bool)
        and math.isfinite(float(number))
        and number >= 0
        for number in numbers
    ):
        return False
    return math.isclose(
        float(value["total_seconds"]),
        float(value["writer_busy_seconds"])
        + float(value["fifo_head_of_line_seconds"])
        + float(value["controller_scheduling_seconds"]),
        abs_tol=1e-6,
    )


def _project_case_metric(attempt_root: Path, state: dict) -> dict | None:
    execution_state = state.get("execution_state")
    if execution_state not in {"closed", "needs_admin"}:
        return None
    created_at = _timestamp(state["created_at"])
    received_at = _received_at(state)
    activity_at = _activity_at(attempt_root, state)
    end_to_end = max(0, int((activity_at - created_at).total_seconds()))
    backlog_age = (
        max(0, int((created_at - received_at).total_seconds()))
        if received_at is not None
        else "unknown"
    )
    try:
        request_id = state["request_id"]
        raw_received_at = state["received_at"]
        if not isinstance(request_id, str) or not isinstance(raw_received_at, str):
            return None
        expected_item_key = observability.attempt_key(
            request_id,
            raw_received_at,
            int(state["attempt"]),
        )
        summary = observability.summarize_logs(
            [attempt_root / "observability.json"]
            if (attempt_root / "observability.json").is_file()
            else []
        )
    except (KeyError, TypeError, ValueError):
        return None
    observed = summary["by_item"].get(expected_item_key)
    phases: dict[str, int | str] = {
        phase: "unknown"
        for phase in (
            "discover",
            "collect",
            "review",
            "wait",
            "submit",
            "postcheck",
            "close",
        )
    }
    active_seconds = 0
    waiting_seconds: int | str = "unknown"
    timing_status = "unknown"
    token_usage = {"status": "unknown"}
    if observed is not None:
        for phase, metrics in observed["phases"].items():
            if phase in phases:
                phases[phase] = metrics["wall_seconds"]
        active_seconds = min(end_to_end, observed["active_seconds"])
        token_usage = observed["token_usage"]
        wait_metrics = observed["phases"].get("wait")
        if wait_metrics is not None:
            waiting_seconds = observed["waiting_seconds"]
            timing_status = "exact"
    coordination = summary["totals"]["coordination"]
    submit_events = (
        observed["phases"].get("submit", {}).get("event_count", 0)
        if observed is not None
        else 0
    )
    write_state = state.get("write_state")
    if write_state not in {"not_requested", "submitted", "unknown"}:
        write_state = "unknown"
    stage_timestamps = observability.attempt_stage_timestamps(
        attempt_root / "observability.json",
        item_key=expected_item_key,
    )
    stage_timestamps["claimed"] = {
        "status": "exact",
        "occurred_at": created_at.isoformat(),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_case_metric",
        "item_key": expected_item_key,
        "workflow_key": state["workflow_key"],
        "closed_at": activity_at.isoformat(),
        "execution_state": execution_state,
        "backlog_age_seconds": backlog_age,
        "end_to_end_seconds": end_to_end,
        "active_seconds": active_seconds,
        "waiting_seconds": waiting_seconds,
        "timing_status": timing_status,
        "phase_seconds": phases,
        "stage_timestamps": stage_timestamps,
        "pipeline_evidence": _pipeline_evidence(
            attempt_root,
            item_key=expected_item_key,
            workflow_key=str(state["workflow_key"]),
            attempt=int(state["attempt"]),
        ),
        "token_usage": token_usage,
        "business_state": state["business_state"],
        "write_state": write_state,
        "quality": _quality_from_receipt(
            attempt_root,
            closed=execution_state == "closed",
        ),
        "duplicate_submissions": max(0, submit_events - 1),
        "cross_case_mix": max(0, len(summary["by_item"]) - 1),
        "unknown_auto_retries": "unknown",
        "agent_messages": "unknown",
        "coordination_seconds": coordination["wall_seconds"],
        "coordination_tokens": coordination["total_tokens"],
    }


def _operation_projection(items: list[dict]) -> dict:
    open_items = [item for item in items if item["execution_state"] != "closed"]
    unknown = [item for item in open_items if item["write_state"] == "unknown"]
    isolated = [
        item
        for item in open_items
        if item["execution_state"] == "needs_admin"
        and item["write_state"] == "not_requested"
        and item["failure_code"] == "material_collection_failed"
        and item["failure_detail_code"]
        in ITEM_LOCAL_MATERIAL_FAILURE_DETAILS
    ]
    technical = [
        item
        for item in open_items
        if item["execution_state"] == "needs_admin"
        and item not in isolated
        and item not in unknown
    ]
    stages: dict[str, int] = {}
    for item in open_items:
        stage = str(item["execution_state"])
        stages[stage] = stages.get(stage, 0) + 1
    if unknown:
        next_step = "reconcile_unknown"
        bottleneck_kind = "unknown_write_outcome"
        bottleneck_count = len(unknown)
        bottleneck_evidence = "exact"
    elif technical:
        next_step = "inspect_technical_failure"
        bottleneck_kind = "technical_failure"
        bottleneck_count = len(technical)
        bottleneck_evidence = "exact"
    elif open_items:
        next_step = "continue_fifo"
        bottleneck_kind = "unknown"
        bottleneck_count = len(open_items)
        bottleneck_evidence = "unknown"
    else:
        next_step = "complete"
        bottleneck_kind = "none"
        bottleneck_count = 0
        bottleneck_evidence = "exact"
    oldest_wait_seconds = max(
        (int(item["age_seconds"]) for item in open_items),
        default=0,
    )
    long_wait_threshold_seconds = 24 * 60 * 60
    failed_items = [
        item for item in open_items if item["execution_state"] == "needs_admin"
    ]
    attention_items = [
        item
        for item in failed_items
        if int(item["failure_attempts"]) >= 2
        or int(item["age_seconds"]) >= long_wait_threshold_seconds
    ]
    attention_failure_counts: dict[str, int] = {}
    for item in attention_items:
        code = str(item["failure_code"] or "unknown_failure")
        attention_failure_counts[code] = attention_failure_counts.get(code, 0) + 1
    return {
        "remaining": len(open_items),
        "completed": sum(
            item["execution_state"] == "closed" for item in items
        ),
        "needs_human": sum(
            item["execution_state"] == "closed"
            and item["business_state"] == "needs_human"
            for item in items
        ),
        "item_local_quarantined": len(isolated),
        "technical_failures": len(technical),
        "unknown": len(unknown),
        "oldest_wait_seconds": oldest_wait_seconds,
        "backlog_wait": {
            "evidence_status": "exact",
            "open_cases": len(open_items),
            "oldest_seconds": oldest_wait_seconds,
        },
        "failure_attention": {
            "scope": "local_open_history",
            "status": "attention_required" if attention_items else "clear",
            "affected_cases": len(attention_items),
            "repeated_failure_cases": sum(
                int(item["failure_attempts"]) >= 2 for item in failed_items
            ),
            "long_wait_failure_cases": sum(
                int(item["age_seconds"]) >= long_wait_threshold_seconds
                for item in failed_items
            ),
            "long_wait_threshold_seconds": long_wait_threshold_seconds,
            "max_failure_attempts": max(
                (int(item["failure_attempts"]) for item in attention_items),
                default=0,
            ),
            "oldest_wait_seconds": max(
                (int(item["age_seconds"]) for item in attention_items),
                default=0,
            ),
            "failure_counts": dict(sorted(attention_failure_counts.items())),
        },
        "current_bottleneck": {
            "evidence_status": bottleneck_evidence,
            "kind": bottleneck_kind,
            "affected_cases": bottleneck_count,
        },
        "current_stages": dict(sorted(stages.items())),
        "next_step": next_step,
    }


def _daily_metric_files(root: Path) -> list[Path]:
    metrics_root = root / ".metrics"
    daily_root = metrics_root / "daily"
    if metrics_root.is_symlink() or daily_root.is_symlink():
        raise ValueError("runtime_path_symlink")
    if not daily_root.is_dir():
        return []
    files: list[Path] = []
    for path in daily_root.iterdir():
        if path.is_symlink():
            raise ValueError("runtime_path_symlink")
        if path.is_file():
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.json", path.name):
                raise ValueError("runtime_state_invalid")
            files.append(path)
    return sorted(files, key=str)


def _empty_outcome_metrics() -> dict:
    return {
        "attempt_count": 0,
        "closed_count": 0,
        "business_results": {
            "pass": 0,
            "needs_human": 0,
            "not_available": 0,
        },
        "technical_failures": 0,
        "write_unknown": 0,
        "quality": {
            "item_covered": 0,
            "item_total": 0,
            "resource_covered": 0,
            "resource_total": 0,
            "page_covered": 0,
            "page_total": 0,
            "format_pass": 0,
            "format_total": 0,
        },
        "duplicate_submissions": 0,
        "cross_case_mix": 0,
        "unknown_auto_retries": 0,
        "agent_messages": 0,
        "evidence_status": "exact",
    }


def _aggregate_case_metrics(samples: list[dict]) -> dict:
    metrics = _empty_outcome_metrics()
    unknown_evidence = False
    for sample in samples:
        metrics["attempt_count"] += 1
        closed = sample["execution_state"] == "closed"
        metrics["closed_count"] += int(closed)
        metrics["business_results"][sample["business_state"]] += 1
        metrics["technical_failures"] += int(
            sample["execution_state"] == "needs_admin"
        )
        metrics["write_unknown"] += int(sample["write_state"] == "unknown")
        if closed:
            quality = sample["quality"]
            for prefix, field in (
                ("item", "item_coverage"),
                ("resource", "resource_coverage"),
                ("page", "page_coverage"),
            ):
                coverage = quality[field]
                metrics["quality"][f"{prefix}_covered"] += coverage["covered"]
                metrics["quality"][f"{prefix}_total"] += coverage["total"]
            metrics["quality"]["format_pass"] += int(quality["format_ok"])
            metrics["quality"]["format_total"] += 1
        for field in (
            "duplicate_submissions",
            "cross_case_mix",
            "unknown_auto_retries",
            "agent_messages",
        ):
            value = sample[field]
            if value == "unknown":
                unknown_evidence = True
            else:
                metrics[field] += value
    if unknown_evidence:
        metrics["evidence_status"] = "unknown"
    return metrics


def _validate_outcome_metrics(value: object) -> dict:
    expected = set(_empty_outcome_metrics())
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("runtime_state_invalid")
    integer_fields = expected - {"business_results", "quality", "evidence_status"}
    if not all(
        isinstance(value[field], int)
        and not isinstance(value[field], bool)
        and value[field] >= 0
        for field in integer_fields
    ):
        raise ValueError("runtime_state_invalid")
    business = value["business_results"]
    quality = value["quality"]
    if (
        not isinstance(business, dict)
        or set(business) != {"pass", "needs_human", "not_available"}
        or not all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in business.values()
        )
        or not isinstance(quality, dict)
        or set(quality) != set(_empty_outcome_metrics()["quality"])
        or not all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in quality.values()
        )
        or value["evidence_status"] not in {"exact", "unknown"}
    ):
        raise ValueError("runtime_state_invalid")
    if (
        sum(business.values()) != value["attempt_count"]
        or value["closed_count"] + value["technical_failures"]
        != value["attempt_count"]
        or business["pass"] + business["needs_human"] != value["closed_count"]
        or business["not_available"] != value["technical_failures"]
        or value["write_unknown"] > value["attempt_count"]
        or quality["item_covered"] > quality["item_total"]
        or quality["resource_covered"] > quality["resource_total"]
        or quality["page_covered"] > quality["page_total"]
        or quality["format_pass"] > quality["format_total"]
        or quality["format_total"] != value["closed_count"]
    ):
        raise ValueError("runtime_state_invalid")
    return json.loads(json.dumps(value))


def _validate_daily_rollup(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"observability", "outcomes"}:
        raise ValueError("runtime_state_invalid")
    outcomes = value["outcomes"]
    if (
        not isinstance(outcomes, dict)
        or set(outcomes) != {"overall", "by_flow"}
        or not isinstance(outcomes["by_flow"], dict)
        or not set(outcomes["by_flow"]).issubset(_WORKFLOW_KEYS)
    ):
        raise ValueError("runtime_state_invalid")
    try:
        operational = observability.validate_daily_metrics(
            value["observability"]
        )
    except ValueError as error:
        raise ValueError("runtime_state_invalid") from error
    overall_outcomes = _validate_outcome_metrics(outcomes["overall"])
    by_flow_outcomes = {
        workflow_key: _validate_outcome_metrics(metrics)
        for workflow_key, metrics in sorted(outcomes["by_flow"].items())
    }
    scalar_fields = set(_empty_outcome_metrics()) - {
        "business_results",
        "quality",
        "evidence_status",
    }
    for field in scalar_fields:
        if sum(metrics[field] for metrics in by_flow_outcomes.values()) != overall_outcomes[field]:
            raise ValueError("runtime_state_invalid")
    for nested in ("business_results", "quality"):
        for field in overall_outcomes[nested]:
            if (
                sum(
                    metrics[nested][field]
                    for metrics in by_flow_outcomes.values()
                )
                != overall_outcomes[nested][field]
            ):
                raise ValueError("runtime_state_invalid")
    expected_evidence = (
        "unknown"
        if any(
            metrics["evidence_status"] == "unknown"
            for metrics in by_flow_outcomes.values()
        )
        else "exact"
    )
    if overall_outcomes["evidence_status"] != expected_evidence:
        raise ValueError("runtime_state_invalid")
    return {
        "observability": operational,
        "outcomes": {
            "overall": overall_outcomes,
            "by_flow": by_flow_outcomes,
        },
    }


def _read_daily_metric(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime_state_invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema_version",
            "kind",
            "day",
            "generated_at",
            "metrics",
        }
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != "preaudit_daily_metric"
        or payload.get("day") != path.stem
    ):
        raise ValueError("runtime_state_invalid")
    _timestamp(payload.get("generated_at"))
    payload["metrics"] = _validate_daily_rollup(payload.get("metrics"))
    return payload


def _local_day(value: datetime) -> str:
    return value.astimezone(_LOCAL_TIMEZONE).date().isoformat()


def _daily_payloads(root: Path, *, now: datetime) -> dict[Path, dict]:
    local_today = now.astimezone(_LOCAL_TIMEZONE).date()
    cutoff = local_today - timedelta(days=_DAILY_RETENTION_DAYS)
    attempt_roots = [
        *_attempt_roots(root),
        *_attempt_roots(root, archived=True),
    ]
    summary = observability.summarize_logs(
        attempt_root / "observability.json"
        for attempt_root in attempt_roots
        if (attempt_root / "observability.json").is_file()
    )
    cases_by_day: dict[str, list[dict]] = {}
    for attempt_root in attempt_roots:
        try:
            state = _read_state(attempt_root / "state.json")
            metric = _project_case_metric(attempt_root, state)
        except (OSError, ValueError):
            metric = None
        if metric is not None:
            day = _local_day(_timestamp(metric["closed_at"]))
            cases_by_day.setdefault(day, []).append(metric)
    daily_root = root / ".metrics" / "daily"
    if daily_root.is_symlink() or daily_root.parent.is_symlink():
        raise ValueError("runtime_path_symlink")
    payloads: dict[Path, dict] = {}
    for day in sorted(set(summary["daily"]) | set(cases_by_day)):
        try:
            parsed_day = datetime.fromisoformat(day).date()
            operational = observability.validate_daily_metrics(
                summary["daily"].get(day, observability.empty_daily_metrics())
            )
        except ValueError as error:
            raise ValueError("runtime_state_invalid") from error
        if parsed_day < cutoff or parsed_day > local_today:
            continue
        day_cases = cases_by_day.get(day, [])
        by_flow_cases = {
            workflow_key: [
                sample
                for sample in day_cases
                if sample["workflow_key"] == workflow_key
            ]
            for workflow_key in sorted(_WORKFLOW_KEYS)
        }
        metrics = {
            "observability": operational,
            "outcomes": {
                "overall": _aggregate_case_metrics(day_cases),
                "by_flow": {
                    workflow_key: _aggregate_case_metrics(samples)
                    for workflow_key, samples in by_flow_cases.items()
                    if samples
                },
            },
        }
        target = daily_root / f"{day}.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_daily_metric",
            "day": day,
            "generated_at": now.isoformat(),
            "metrics": metrics,
        }
        if target.is_symlink():
            raise ValueError("runtime_path_symlink")
        if target.exists():
            existing = _read_daily_metric(target)
            existing_count = existing["metrics"]["observability"]["overall"][
                "event_count"
            ]
            current_count = metrics["observability"]["overall"]["event_count"]
            if existing_count > current_count:
                continue
            if existing_count == current_count:
                if existing["metrics"] != metrics:
                    raise ValueError("daily_metric_conflict")
                continue
        payloads[target] = payload
    return payloads


def _write_daily_metric(target: Path, payload: dict) -> None:
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("runtime_path_symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("runtime_path_symlink")
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    serialized = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        if json.loads(temporary.read_text(encoding="utf-8")) != payload:
            raise OSError("daily_metric_readback_mismatch")
        if target.is_symlink():
            raise ValueError("runtime_path_symlink")
        os.replace(temporary, target)
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_locks(paths: list[Path], *, exact: bool = False):
    descriptors: list[int] = []
    try:
        for path in sorted(set(paths), key=str):
            lock = path if exact else path.with_suffix(path.suffix + ".lock")
            if lock.is_symlink():
                raise ValueError("runtime_path_symlink")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock, flags, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def status(root: Path, *, now: datetime | None = None) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_now_invalid")
    runtime_root = Path(root).expanduser()
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ValueError("runtime_root_invalid")

    by_workflow: dict[str, list[dict]] = {}
    attempt_roots = _attempt_roots(runtime_root)
    metric_cutoff = current_time - timedelta(days=retention.RETENTION_DAYS)
    metric_roots: list[Path] = []
    for attempt_root in [
        *attempt_roots,
        *_attempt_roots(runtime_root, archived=True),
    ]:
        try:
            state = _read_state(attempt_root / "state.json")
            activity_at = _activity_at(attempt_root, state)
        except (OSError, ValueError):
            continue
        if activity_at >= metric_cutoff:
            metric_roots.append(attempt_root)
    for attempt_root in attempt_roots:
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError as error:
            if str(error) == "runtime_state_identity_invalid":
                raise ValueError("runtime_state_invalid") from error
            state_path = attempt_root / "state.json"
            created_at = datetime.fromtimestamp(
                state_path.stat().st_mtime if state_path.exists() else 0,
                tz=timezone.utc,
            )
            workflow_key = attempt_root.parent.parent.parent.name
            try:
                attempt = int(attempt_root.name.removeprefix("attempt-"))
            except ValueError:
                attempt = 0
            state = {
                "workflow_key": workflow_key,
                "case_key": attempt_root.parent.name,
                "attempt": attempt,
                "execution_state": "needs_admin",
                "business_state": "not_available",
                "write_state": "unknown",
                "reason": "state_invalid",
            }
        else:
            created_at = _timestamp(state["created_at"])
        received_at = _received_at(state) or created_at
        reason = str(state.get("reason") or "")
        failure_code = (
            reason
            if reason in _FAILURE_CODES
            else ("unknown_failure" if reason else None)
        )
        raw_failure_detail = str(state.get("failure_detail_code") or "")
        failure_detail_code = (
            raw_failure_detail
            if raw_failure_detail in ITEM_LOCAL_MATERIAL_FAILURE_DETAILS
            else ("unclassified" if raw_failure_detail else None)
        )
        by_workflow.setdefault(state["workflow_key"], []).append(
            {
                "item_key": _item_key(state["case_key"], state["attempt"]),
                "case_key": state["case_key"],
                "attempt": state["attempt"],
                "created_at": created_at,
                "age_seconds": _age_seconds(received_at, current_time),
                "execution_state": state["execution_state"],
                "business_state": state["business_state"],
                "write_state": state["write_state"],
                "failure_code": failure_code,
                "failure_detail_code": failure_detail_code,
            }
        )

    workflows: list[dict] = []
    operation_items: list[dict] = []
    for workflow_key, attempts in sorted(by_workflow.items()):
        attempts.sort(key=lambda item: (item["created_at"], item["item_key"]))
        latest_by_case: dict[str, dict] = {}
        for item in attempts:
            current = latest_by_case.get(item["case_key"])
            if current is None or item["attempt"] > current["attempt"]:
                latest_by_case[item["case_key"]] = item
        failure_attempts_by_case: dict[str, int] = {}
        for item in attempts:
            if item["execution_state"] == "needs_admin":
                failure_attempts_by_case[item["case_key"]] = (
                    failure_attempts_by_case.get(item["case_key"], 0) + 1
                )
        for item in latest_by_case.values():
            item["failure_attempts"] = failure_attempts_by_case.get(
                item["case_key"], 0
            )
        current_attempts = sorted(
            latest_by_case.values(),
            key=lambda item: (item["created_at"], item["item_key"]),
        )
        operation_items.extend(current_attempts)
        latest = current_attempts[-1]
        open_attempts = [
            item for item in current_attempts if item["execution_state"] != "closed"
        ]
        execution_states: dict[str, int] = {}
        business_states: dict[str, int] = {}
        write_states: dict[str, int] = {}
        failure_counts: dict[str, int] = {}
        for item in current_attempts:
            for counts, key in (
                (execution_states, item["execution_state"]),
                (business_states, item["business_state"]),
                (write_states, item["write_state"]),
            ):
                counts[key] = counts.get(key, 0) + 1
            if item["failure_code"]:
                code = item["failure_code"]
                failure_counts[code] = failure_counts.get(code, 0) + 1
        workflows.append(
            {
                "workflow_key": workflow_key,
                "latest_round": {
                    key: latest[key]
                    for key in (
                        "item_key",
                        "attempt",
                        "age_seconds",
                        "execution_state",
                        "business_state",
                        "write_state",
                    )
                },
                "case_count": len(current_attempts),
                "attempt_count": len(attempts),
                "backlog_count": len(open_attempts),
                "oldest_backlog_age_seconds": (
                    max(item["age_seconds"] for item in open_attempts)
                    if open_attempts
                    else 0
                ),
                "execution_states": execution_states,
                "business_states": business_states,
                "write_states": write_states,
                "failure_counts": failure_counts,
                "operations": _operation_projection(current_attempts),
            }
        )
    daily_cutoff = current_time.astimezone(_LOCAL_TIMEZONE).date() - timedelta(
        days=_DAILY_RETENTION_DAYS
    )
    daily_history = []
    for path in _daily_metric_files(runtime_root):
        payload = _read_daily_metric(path)
        day = datetime.fromisoformat(payload["day"]).date()
        if day >= daily_cutoff:
            daily_history.append(payload)
    case_metrics = []
    for attempt_root in metric_roots:
        try:
            state = _read_state(attempt_root / "state.json")
            metric = _project_case_metric(attempt_root, state)
        except (OSError, ValueError):
            metric = None
        if metric is not None:
            case_metrics.append(metric)
    case_metrics.sort(
        key=lambda item: (
            item["closed_at"],
            item["workflow_key"],
            item["item_key"],
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_runtime_status",
        "generated_at": current_time.isoformat(),
        "operations": _operation_projection(operation_items),
        "workflows": workflows,
        "observability": observability.summarize_logs(
            path
            for path in (
                attempt_root / "observability.json"
                for attempt_root in metric_roots
            )
            if path.is_file()
        ),
        "case_metrics": case_metrics,
        "cli_invocations": observability.cli_invocations(
            runtime_root / _CLI_LOG,
            now=current_time,
        ),
        "daily_history": daily_history,
    }


def _plan_fingerprint(plan: dict) -> str:
    payload = {key: value for key, value in plan.items() if key != "fingerprint"}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def build_prune_plan(
    root: Path,
    *,
    now: datetime | None = None,
    retention_days: int = retention.RETENTION_DAYS,
) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if (
        current_time.tzinfo is None
        or isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days != retention.RETENTION_DAYS
    ):
        raise ValueError("retention_plan_invalid")
    raw_root = Path(root).expanduser()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("runtime_root_invalid")
    runtime_root = raw_root.resolve(strict=True)
    cutoff = current_time - timedelta(days=retention_days)
    active: list[tuple[Path, dict | None]] = []
    for attempt_root in _attempt_roots(runtime_root):
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError:
            state = None
        active.append((attempt_root, state))
    latest_by_workflow: dict[str, tuple[datetime, Path]] = {}
    for attempt_root, state in active:
        if state is None:
            continue
        created_at = _timestamp(state["created_at"])
        current = latest_by_workflow.get(state["workflow_key"])
        if current is None or (created_at, str(attempt_root)) > (
            current[0],
            str(current[1]),
        ):
            latest_by_workflow[state["workflow_key"]] = (
                created_at,
                attempt_root,
            )

    actions: list[dict] = []
    for attempt_root, state in active:
        relative_path = str(attempt_root.relative_to(runtime_root))
        if state is None:
            action, reason = "keep", "state_invalid"
        else:
            try:
                activity_at = _activity_at(attempt_root, state)
            except (OSError, ValueError):
                action, reason = "keep", "state_invalid"
                activity_at = None
            is_latest = latest_by_workflow.get(state["workflow_key"], (None, None))[1] == attempt_root
            if activity_at is None:
                pass
            elif state["execution_state"] != "closed":
                action, reason = "keep", "not_closed"
            elif state["write_state"] == "unknown":
                action, reason = "keep", "write_state_unknown"
            elif is_latest:
                action, reason = "keep", "latest_task"
            elif activity_at < cutoff:
                action, reason = "delete_path", "expired"
            else:
                action, reason = "archive_path", "previous_task"
        actions.append(
            {
                "key": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "action": action,
                "reason": reason,
                "relative_path": relative_path,
            }
        )

    for attempt_root in _attempt_roots(runtime_root, archived=True):
        relative_from_archive = attempt_root.relative_to(runtime_root / ".archive")
        relative_path = str(Path(".archive") / relative_from_archive)
        try:
            state = _read_state(attempt_root / "state.json")
        except ValueError:
            state = None
        if state is None:
            action, reason = "keep", "state_invalid"
        else:
            try:
                activity_at = _activity_at(attempt_root, state)
            except (OSError, ValueError):
                action, reason = "keep", "state_invalid"
                activity_at = None
            if activity_at is None:
                pass
            elif state["execution_state"] != "closed":
                action, reason = "keep", "not_closed"
            elif state["write_state"] == "unknown":
                action, reason = "keep", "write_state_unknown"
            elif activity_at < cutoff:
                action, reason = "delete_path", "expired"
            else:
                action, reason = "keep", "within_retention"
        actions.append(
            {
                "key": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "action": action,
                "reason": reason,
                "relative_path": relative_path,
            }
        )

    daily_cutoff = current_time.astimezone(_LOCAL_TIMEZONE).date() - timedelta(
        days=_DAILY_RETENTION_DAYS
    )
    for path in _daily_metric_files(runtime_root):
        relative_path = str(path.relative_to(runtime_root))
        try:
            payload = _read_daily_metric(path)
            day = datetime.fromisoformat(payload["day"]).date()
        except ValueError:
            action, reason = "keep", "state_invalid"
        else:
            if day < daily_cutoff:
                action, reason = "delete_path", "expired"
            else:
                action, reason = "keep", "within_retention"
        actions.append(
            {
                "key": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "action": action,
                "reason": reason,
                "relative_path": relative_path,
            }
        )

    actions.sort(key=lambda item: item["relative_path"])
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_retention_plan",
        "generated_at": current_time.isoformat(),
        "retention_days": retention_days,
        "actions": actions,
    }
    return {**plan, "fingerprint": _plan_fingerprint(plan)}


def _validated_plan(plan: object) -> list[retention.RetentionAction]:
    if (
        not isinstance(plan, dict)
        or set(plan)
        != {
            "schema_version",
            "kind",
            "generated_at",
            "retention_days",
            "actions",
            "fingerprint",
        }
        or plan.get("schema_version") != SCHEMA_VERSION
        or plan.get("kind") != "preaudit_retention_plan"
        or not isinstance(plan.get("retention_days"), int)
        or isinstance(plan.get("retention_days"), bool)
        or plan["retention_days"] != retention.RETENTION_DAYS
        or not isinstance(plan.get("actions"), list)
        or plan.get("fingerprint") != _plan_fingerprint(plan)
    ):
        raise ValueError("retention_plan_invalid")
    try:
        _timestamp(plan.get("generated_at"))
    except ValueError as error:
        raise ValueError("retention_plan_invalid") from error
    actions: list[retention.RetentionAction] = []
    keys: set[str] = set()
    paths: set[str] = set()
    for item in plan["actions"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "action", "reason", "relative_path"}
            or item.get("action") not in {"archive_path", "delete_path", "keep"}
            or not isinstance(item.get("key"), str)
            or not isinstance(item.get("reason"), str)
            or not isinstance(item.get("relative_path"), str)
            or not _MANAGED_PATH.fullmatch(item["relative_path"])
            or (
                item.get("action") == "archive_path"
                and item["relative_path"].startswith((".archive/", ".metrics/"))
            )
            or item["key"] in keys
            or item["relative_path"] in paths
            or item["key"]
            != hashlib.sha256(item["relative_path"].encode("utf-8")).hexdigest()
        ):
            raise ValueError("retention_plan_invalid")
        keys.add(item["key"])
        paths.add(item["relative_path"])
        actions.append(retention.RetentionAction(**item))
    return actions


def apply_prune_plan(
    root: Path,
    plan: object,
    *,
    now: datetime | None = None,
) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_now_invalid")
    try:
        with retention.maintenance_lock(root) as locked_root:
            raw = _apply_prune_plan_locked(
                locked_root,
                plan,
                current_time=current_time,
            )
    except ValueError as error:
        raise ValueError("retention_plan_invalid") from error
    return _prune_receipt(raw)


def _apply_prune_plan_locked(
    locked_root: Path,
    plan: object,
    *,
    current_time: datetime,
) -> dict:
    actions = _validated_plan(plan)
    retention.validate_file_actions(locked_root, actions)
    generated_at = _timestamp(plan["generated_at"])
    if generated_at > current_time or current_time - generated_at > _PLAN_MAX_AGE:
        raise ValueError("retention_plan_stale")
    canonical = build_prune_plan(
        locked_root,
        now=generated_at,
        retention_days=plan["retention_days"],
    )
    canonical_by_path = {
        item["relative_path"]: item for item in canonical["actions"]
    }
    planned_by_path = {action.relative_path: action for action in actions}
    archived_destinations = {
        str(Path(".archive") / str(action.relative_path))
        for action in actions
        if action.action == "archive_path"
    }
    for relative_path, expected in canonical_by_path.items():
        planned = planned_by_path.get(relative_path)
        if planned is None:
            if relative_path in archived_destinations:
                continue
            raise ValueError("retention_plan_incomplete")
        if (
            planned.action != expected["action"]
            or planned.reason != expected["reason"]
        ):
            raise ValueError("retention_plan_conflict")
    for relative_path in planned_by_path.keys() - canonical_by_path.keys():
        target = locked_root / str(relative_path)
        if target.exists() or target.is_symlink():
            raise ValueError("retention_plan_extra_target")
    return retention.apply_file_actions_locked(
        locked_root,
        actions,
        now=current_time,
    )


def _prune_receipt(raw: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_prune_receipt",
        "archived": raw["archived"],
        "deleted": raw["deleted"],
        "missing": raw["missing"],
        "failed": raw["failed"],
        "status": raw["status"],
    }


def prune(root: Path, *, now: datetime | None = None) -> dict:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_now_invalid")
    try:
        with retention.maintenance_lock(root) as locked_root:
            attempt_roots = [
                *_attempt_roots(locked_root),
                *_attempt_roots(locked_root, archived=True),
            ]
            log_paths = [
                attempt_root / "observability.json"
                for attempt_root in attempt_roots
            ]
            state_lock_paths = [
                lock_path
                for attempt_root in attempt_roots
                for lock_path in (
                    attempt_root / "prepare.lock",
                    attempt_root / "complete.lock",
                )
            ]
            with _file_locks(state_lock_paths, exact=True), _file_locks(log_paths):
                payloads = _daily_payloads(locked_root, now=current_time)
                initial_plan = build_prune_plan(locked_root, now=current_time)
                prospective = list(initial_plan["actions"])
                known_paths = {
                    item["relative_path"] for item in prospective
                }
                for target in payloads:
                    relative_path = str(target.relative_to(locked_root))
                    if relative_path in known_paths:
                        continue
                    prospective.append(
                        {
                            "key": hashlib.sha256(
                                relative_path.encode("utf-8")
                            ).hexdigest(),
                            "action": "keep",
                            "reason": "within_retention",
                            "relative_path": relative_path,
                        }
                    )
                prospective.sort(key=lambda item: item["relative_path"])
                prospective_plan = {
                    **initial_plan,
                    "actions": prospective,
                }
                prospective_plan["fingerprint"] = _plan_fingerprint(
                    prospective_plan
                )
                prospective_actions = _validated_plan(prospective_plan)
                retention.validate_file_actions(
                    locked_root,
                    prospective_actions,
                )
                observability.compact_cli_invocations(
                    locked_root / _CLI_LOG,
                    now=current_time,
                )
                for target, payload in payloads.items():
                    _write_daily_metric(target, payload)
                final_plan = build_prune_plan(locked_root, now=current_time)
                raw = _apply_prune_plan_locked(
                    locked_root,
                    final_plan,
                    current_time=current_time,
                )
    except ValueError as error:
        raise ValueError("retention_plan_invalid") from error
    return _prune_receipt(raw)
