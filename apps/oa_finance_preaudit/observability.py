from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from apps.json_artifact import write_json_atomic


_PHASE_CATEGORY = {
    "discover": "mechanical",
    "collect": "mechanical",
    "review": "approval",
    "coordination": "coordination",
    "wait": "waiting",
    "submit": "submission",
    "postcheck": "submission",
    "close": "approval",
}
_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "packaging_layout_approval",
}
_RESULTS = {"success", "business_warning", "technical_failure", "unknown"}
_EVENT_FIELDS = {
    "event_id",
    "item_key",
    "workflow_key",
    "occurred_at",
    "phase",
    "wall_seconds",
    "active_seconds",
    "queue_wait_seconds",
    "result",
    "model",
    "token_usage",
}
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_CLI_COMMANDS = {"next-action", "complete-review", "status", "prune", "clean"}
_CLI_OUTCOMES = {
    "success",
    "argument_error",
    "json_contract_error",
    "file_transfer_error",
    "process_start_error",
    "other",
}
_CLI_FIELDS = {
    "workflow_key",
    "command",
    "occurred_at",
    "outcome",
    "human_reproduction",
}
_RAW_RETENTION = timedelta(days=30)


def attempt_key(request_id: str, received_at: str, attempt: int) -> str:
    if not request_id or not received_at or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("observability_identity_invalid")
    value = f"{request_id}\x1f{received_at}\x1f{attempt}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("observability_event_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("observability_event_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("observability_event_invalid")
    return parsed


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validated_usage(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("observability_event_invalid")
    if value == {"status": "unknown"}:
        return dict(value)
    if set(value) != {"status", "input_tokens", "output_tokens", "total_tokens"}:
        raise ValueError("observability_event_invalid")
    if value.get("status") != "exact":
        raise ValueError("observability_event_invalid")
    counts = [value.get("input_tokens"), value.get("output_tokens"), value.get("total_tokens")]
    if not all(_nonnegative_integer(count) for count in counts):
        raise ValueError("observability_event_invalid")
    if counts[0] + counts[1] != counts[2]:
        raise ValueError("observability_event_invalid")
    return dict(value)


def validate_event(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        raise ValueError("observability_event_invalid")
    event = dict(value)
    if not _SAFE_TEXT.fullmatch(str(event["event_id"])):
        raise ValueError("observability_event_invalid")
    if not _SHA256.fullmatch(str(event["item_key"])):
        raise ValueError("observability_event_invalid")
    if event["workflow_key"] not in _WORKFLOW_KEYS:
        raise ValueError("observability_event_invalid")
    if event["phase"] not in _PHASE_CATEGORY or event["result"] not in _RESULTS:
        raise ValueError("observability_event_invalid")
    _timestamp(event["occurred_at"])
    for field in ("wall_seconds", "active_seconds", "queue_wait_seconds"):
        if not _nonnegative_integer(event[field]):
            raise ValueError("observability_event_invalid")
    if event["active_seconds"] > event["wall_seconds"]:
        raise ValueError("observability_event_invalid")
    if event["queue_wait_seconds"] > event["wall_seconds"]:
        raise ValueError("observability_event_invalid")
    model = event["model"]
    if model is not None and not _SAFE_TEXT.fullmatch(str(model)):
        raise ValueError("observability_event_invalid")
    event["token_usage"] = _validated_usage(event["token_usage"])
    return event


def validate_cli_invocation(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _CLI_FIELDS:
        raise ValueError("cli_invocation_invalid")
    receipt = dict(value)
    try:
        occurred_at = datetime.fromisoformat(str(receipt["occurred_at"]))
    except ValueError as error:
        raise ValueError("cli_invocation_invalid") from error
    if (
        receipt["workflow_key"] not in _WORKFLOW_KEYS | {"global"}
        or receipt["command"] not in _CLI_COMMANDS
        or receipt["outcome"] not in _CLI_OUTCOMES
        or occurred_at.tzinfo is None
        or receipt["human_reproduction"]
        not in {"not_attempted", "succeeded", "failed"}
        or (
            receipt["outcome"] == "success"
            and receipt["human_reproduction"] != "not_attempted"
        )
    ):
        raise ValueError("cli_invocation_invalid")
    return receipt


@contextmanager
def _exclusive_lock(path: Path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_log(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "kind": "preaudit_observability", "events": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("observability_log_unreadable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "preaudit_observability"
        or not isinstance(payload.get("events"), list)
    ):
        raise ValueError("observability_log_invalid")
    payload["events"] = [validate_event(event) for event in payload["events"]]
    return payload


def _read_cli_log(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "kind": "preaudit_cli_invocations", "events": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("cli_invocation_log_unreadable") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "kind", "events"}
        or payload.get("schema_version") != 1
        or payload.get("kind") != "preaudit_cli_invocations"
        or not isinstance(payload.get("events"), list)
    ):
        raise ValueError("cli_invocation_log_invalid")
    payload["events"] = [
        validate_cli_invocation(event) for event in payload["events"]
    ]
    return payload


def _recent_cli_events(events: list[dict], now: datetime) -> list[dict]:
    if now.tzinfo is None:
        raise ValueError("cli_invocation_now_invalid")
    cutoff = now - _RAW_RETENTION
    return [
        event
        for event in events
        if cutoff
        <= datetime.fromisoformat(event["occurred_at"])
        <= now
    ]


def record_cli_invocation(
    path: Path,
    receipt: object,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or datetime.now(timezone.utc)
    target = Path(path).expanduser().absolute()
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("cli_invocation_path_invalid")
    normalized = validate_cli_invocation(receipt)
    with _exclusive_lock(target):
        payload = _read_cli_log(target)
        payload["events"] = _recent_cli_events(
            [*payload["events"], normalized],
            current_time,
        )
        payload["events"].sort(
            key=lambda item: (
                item["occurred_at"],
                item["workflow_key"],
                item["command"],
            )
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(target, payload)


def cli_invocations(
    path: Path,
    *,
    now: datetime | None = None,
) -> list[dict]:
    current_time = now or datetime.now(timezone.utc)
    target = Path(path).expanduser().absolute()
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("cli_invocation_path_invalid")
    return _recent_cli_events(_read_cli_log(target)["events"], current_time)


def compact_cli_invocations(
    path: Path,
    *,
    now: datetime,
) -> None:
    target = Path(path).expanduser().absolute()
    if not target.exists():
        return
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("cli_invocation_path_invalid")
    with _exclusive_lock(target):
        payload = _read_cli_log(target)
        recent = _recent_cli_events(payload["events"], now)
        if recent != payload["events"]:
            write_json_atomic(target, {**payload, "events": recent})


def record_event(path: Path, event: object) -> dict:
    target = Path(path).expanduser().resolve()
    normalized = validate_event(event)
    with _exclusive_lock(target):
        payload = _read_log(target)
        identity = (normalized["item_key"], normalized["event_id"])
        for existing in payload["events"]:
            if (existing["item_key"], existing["event_id"]) != identity:
                continue
            if existing != normalized:
                raise ValueError("observability_event_conflict")
            return {"event_count": len(payload["events"]), "replayed": True}
        payload["events"].append(normalized)
        payload["events"].sort(key=lambda item: (item["occurred_at"], item["item_key"], item["event_id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(target, payload)
        return {"event_count": len(payload["events"]), "replayed": False}


def record_attempt_phase(
    assignment: dict,
    *,
    event_id: str,
    phase: str,
    wall_seconds: int,
    active_seconds: int,
    queue_wait_seconds: int = 0,
    result: str,
    model: str | None = None,
    token_usage: dict | None = None,
) -> dict:
    request_root = Path(str(assignment["request_root"])).expanduser().resolve()
    return record_event(
        request_root / "observability.json",
        {
            "event_id": event_id,
            "item_key": attempt_key(
                str(assignment["request_id"]),
                str(assignment["received_at"]),
                int(assignment["attempt"]),
            ),
            "workflow_key": str(assignment["flow_key"]),
            "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phase": phase,
            "wall_seconds": wall_seconds,
            "active_seconds": active_seconds,
            "queue_wait_seconds": queue_wait_seconds,
            "result": result,
            "model": model,
            "token_usage": token_usage or {"status": "unknown"},
        },
    )


def attempt_has_phases(
    path: Path,
    *,
    request_id: str,
    received_at: str,
    attempt: int,
    phases: Iterable[str],
) -> bool:
    payload = _read_log(Path(path).expanduser().resolve())
    item_key = attempt_key(request_id, received_at, attempt)
    present = {
        event["phase"]
        for event in payload["events"]
        if event["item_key"] == item_key
    }
    required = set(phases)
    if not required.issubset(_PHASE_CATEGORY):
        raise ValueError("observability_event_invalid")
    return required.issubset(present)


def latest_event_at(path: Path) -> datetime | None:
    events = _read_log(Path(path).expanduser().resolve())["events"]
    if not events:
        return None
    return max(_timestamp(event["occurred_at"]) for event in events)


def attempt_stage_timestamps(path: Path, *, item_key: str) -> dict:
    if _SHA256.fullmatch(item_key) is None:
        raise ValueError("observability_identity_invalid")
    stages = {
        "discovered": "discover",
        "prepared": "collect",
        "review_done": "review",
        "write_ready": "wait",
        "writer": "submit",
        "postcheck": "postcheck",
        "closed": "close",
    }
    events = [
        event
        for event in _read_log(Path(path).expanduser().resolve())["events"]
        if event["item_key"] == item_key
    ]
    projected: dict[str, dict] = {}
    for stage, phase in stages.items():
        timestamps = [
            event["occurred_at"]
            for event in events
            if event["phase"] == phase
        ]
        projected[stage] = (
            {
                "status": "exact",
                "occurred_at": (
                    min(timestamps) if stage == "write_ready" else max(timestamps)
                ),
            }
            if timestamps
            else {"status": "unknown"}
        )
    return projected


def _empty_metrics() -> dict:
    return {
        "event_count": 0,
        "wall_seconds": 0,
        "active_seconds": 0,
        "queue_wait_seconds": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "usage_unknown_events": 0,
    }


def _add(metrics: dict, event: dict) -> None:
    metrics["event_count"] += 1
    for field in ("wall_seconds", "active_seconds", "queue_wait_seconds"):
        metrics[field] += event[field]
    usage = event["token_usage"]
    if usage["status"] == "unknown":
        metrics["usage_unknown_events"] += 1
    else:
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            metrics[field] += usage[field]


def _percentiles(values: Iterable[int]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"p50": "unknown", "p95": "unknown"}

    def nearest_rank(percentile: float) -> int:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    return {"p50": nearest_rank(0.50), "p95": nearest_rank(0.95)}


def _item_metrics(events: list[dict]) -> dict:
    ordered = sorted(
        events,
        key=lambda item: (item["occurred_at"], item["event_id"]),
    )
    workflow_keys = {event["workflow_key"] for event in ordered}
    if len(workflow_keys) != 1:
        raise ValueError("observability_event_conflict")
    starts = [
        _timestamp(event["occurred_at"])
        - timedelta(seconds=event["wall_seconds"])
        for event in ordered
    ]
    ends = [_timestamp(event["occurred_at"]) for event in ordered]
    end_to_end = max(0, int((max(ends) - min(starts)).total_seconds()))
    active_seconds = sum(event["active_seconds"] for event in ordered)
    waiting_seconds = min(
        end_to_end,
        sum(event["queue_wait_seconds"] for event in ordered),
    )
    exact_usage = [
        event["token_usage"]
        for event in ordered
        if event["token_usage"]["status"] == "exact"
    ]
    token_usage = (
        {
            "status": "exact",
            "input_tokens": sum(item["input_tokens"] for item in exact_usage),
            "output_tokens": sum(item["output_tokens"] for item in exact_usage),
            "total_tokens": sum(item["total_tokens"] for item in exact_usage),
        }
        if len(exact_usage) == len(ordered)
        else {"status": "unknown"}
    )
    phases: dict[str, dict] = {}
    for event in ordered:
        _add(phases.setdefault(event["phase"], _empty_metrics()), event)
    return {
        "workflow_key": next(iter(workflow_keys)),
        "local_day": max(ends).astimezone(_LOCAL_TIMEZONE).strftime("%Y-%m-%d"),
        "event_count": len(ordered),
        "end_to_end_seconds": end_to_end,
        "active_seconds": active_seconds,
        "waiting_seconds": waiting_seconds,
        "waiting_ratio": (
            round(waiting_seconds / end_to_end, 4) if end_to_end else 0.0
        ),
        "token_usage": token_usage,
        "phases": dict(sorted(phases.items())),
    }


def _finish_bucket(metrics: dict, cases: list[dict]) -> dict:
    end_to_end = [case["end_to_end_seconds"] for case in cases]
    waiting_seconds = sum(case["waiting_seconds"] for case in cases)
    total_seconds = sum(end_to_end)
    phase_values: dict[str, list[int]] = {}
    for case in cases:
        for phase, phase_metrics in case["phases"].items():
            phase_values.setdefault(phase, []).append(phase_metrics["wall_seconds"])
    return {
        **metrics,
        "case_count": len(cases),
        "end_to_end_seconds": _percentiles(end_to_end),
        "phase_wall_seconds": {
            phase: _percentiles(values)
            for phase, values in sorted(phase_values.items())
        },
        "waiting_ratio": (
            round(waiting_seconds / total_seconds, 4) if total_seconds else 0.0
        ),
    }


def _aggregate_events(events: Iterable[dict]) -> dict:
    metrics = _empty_metrics()
    for event in events:
        _add(metrics, event)
    return metrics


def _validate_percentiles(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"p50", "p95"}:
        raise ValueError("observability_daily_invalid")
    values = (value["p50"], value["p95"])
    if values == ("unknown", "unknown"):
        return dict(value)
    if not all(_nonnegative_integer(item) for item in values):
        raise ValueError("observability_daily_invalid")
    if values[0] > values[1]:
        raise ValueError("observability_daily_invalid")
    return dict(value)


def _validate_finished_metrics(value: object) -> dict:
    expected = set(_empty_metrics()) | {
        "case_count",
        "end_to_end_seconds",
        "phase_wall_seconds",
        "waiting_ratio",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("observability_daily_invalid")
    for field in set(_empty_metrics()) | {"case_count"}:
        if not _nonnegative_integer(value[field]):
            raise ValueError("observability_daily_invalid")
    if (
        value["active_seconds"] > value["wall_seconds"]
        or value["queue_wait_seconds"] > value["wall_seconds"]
        or value["input_tokens"] + value["output_tokens"]
        != value["total_tokens"]
        or value["usage_unknown_events"] > value["event_count"]
        or value["case_count"] > value["event_count"]
    ):
        raise ValueError("observability_daily_invalid")
    end_to_end = _validate_percentiles(value["end_to_end_seconds"])
    phases = value["phase_wall_seconds"]
    if (
        not isinstance(phases, dict)
        or not set(phases).issubset(_PHASE_CATEGORY)
    ):
        raise ValueError("observability_daily_invalid")
    normalized_phases = {
        phase: _validate_percentiles(percentiles)
        for phase, percentiles in phases.items()
    }
    waiting_ratio = value["waiting_ratio"]
    if (
        isinstance(waiting_ratio, bool)
        or not isinstance(waiting_ratio, (int, float))
        or not 0 <= waiting_ratio <= 1
    ):
        raise ValueError("observability_daily_invalid")
    return {
        **{field: value[field] for field in _empty_metrics()},
        "case_count": value["case_count"],
        "end_to_end_seconds": end_to_end,
        "phase_wall_seconds": dict(sorted(normalized_phases.items())),
        "waiting_ratio": waiting_ratio,
    }


def validate_daily_metrics(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"overall", "by_flow"}:
        raise ValueError("observability_daily_invalid")
    by_flow = value["by_flow"]
    if not isinstance(by_flow, dict) or not set(by_flow).issubset(_WORKFLOW_KEYS):
        raise ValueError("observability_daily_invalid")
    overall = _validate_finished_metrics(value["overall"])
    normalized_by_flow = {
        workflow_key: _validate_finished_metrics(metrics)
        for workflow_key, metrics in sorted(by_flow.items())
    }
    for field in set(_empty_metrics()) | {"case_count"}:
        if sum(metrics[field] for metrics in normalized_by_flow.values()) != overall[field]:
            raise ValueError("observability_daily_invalid")
    return {"overall": overall, "by_flow": normalized_by_flow}


def empty_daily_metrics() -> dict:
    return {
        "overall": _finish_bucket(_empty_metrics(), []),
        "by_flow": {},
    }


def summarize_logs(paths: Iterable[Path]) -> dict:
    events: dict[tuple[str, str], dict] = {}
    for path in paths:
        for event in _read_log(Path(path).expanduser().resolve())["events"]:
            identity = (event["item_key"], event["event_id"])
            previous = events.get(identity)
            if previous is not None and previous != event:
                raise ValueError("observability_event_conflict")
            events[identity] = event

    totals = {category: _empty_metrics() for category in _PHASE_CATEGORY.values()}
    by_flow_raw: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    results = {result: 0 for result in sorted(_RESULTS)}
    for event in events.values():
        occurred_at = _timestamp(event["occurred_at"])
        hour_key = occurred_at.strftime("%Y-%m-%dT%H:00:00Z")
        _add(totals[_PHASE_CATEGORY[event["phase"]]], event)
        for bucket, key in (
            (by_flow_raw, event["workflow_key"]),
            (by_hour, hour_key),
        ):
            _add(bucket.setdefault(key, _empty_metrics()), event)
        results[event["result"]] += 1
    events_by_item: dict[str, list[dict]] = {}
    for event in events.values():
        events_by_item.setdefault(event["item_key"], []).append(event)
    by_item = {
        item_key: _item_metrics(item_events)
        for item_key, item_events in sorted(events_by_item.items())
    }
    cases_by_flow: dict[str, list[dict]] = {}
    cases_by_date: dict[str, list[dict]] = {}
    cases_by_date_flow: dict[str, dict[str, list[dict]]] = {}
    by_date_raw: dict[str, dict] = {}
    by_date_flow_raw: dict[str, dict[str, dict]] = {}
    for item_key, case in by_item.items():
        cases_by_flow.setdefault(case["workflow_key"], []).append(case)
        cases_by_date.setdefault(case["local_day"], []).append(case)
        cases_by_date_flow.setdefault(case["local_day"], {}).setdefault(
            case["workflow_key"], []
        ).append(case)
        case_metrics = _aggregate_events(events_by_item[item_key])
        day_metrics = by_date_raw.setdefault(case["local_day"], _empty_metrics())
        flow_metrics = by_date_flow_raw.setdefault(
            case["local_day"], {}
        ).setdefault(case["workflow_key"], _empty_metrics())
        for field, amount in case_metrics.items():
            day_metrics[field] += amount
            flow_metrics[field] += amount
    by_flow = {
        key: _finish_bucket(metrics, cases_by_flow.get(key, []))
        for key, metrics in sorted(by_flow_raw.items())
    }
    by_date = {
        key: _finish_bucket(metrics, cases_by_date.get(key, []))
        for key, metrics in sorted(by_date_raw.items())
    }
    daily = {
        day: {
            "overall": by_date[day],
            "by_flow": {
                workflow_key: _finish_bucket(
                    metrics,
                    cases_by_date_flow.get(day, {}).get(workflow_key, []),
                )
                for workflow_key, metrics in sorted(flow_metrics.items())
            },
        }
        for day, flow_metrics in sorted(by_date_flow_raw.items())
    }
    return {
        "schema_version": 1,
        "event_count": len(events),
        "totals": totals,
        "by_item": by_item,
        "by_flow": dict(sorted(by_flow.items())),
        "by_date": dict(sorted(by_date.items())),
        "daily": daily,
        "by_hour": dict(sorted(by_hour.items())),
        "results": results,
    }
