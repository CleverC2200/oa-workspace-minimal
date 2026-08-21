from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

from .json_artifact import write_json_atomic


_PHASE_CATEGORY = {
    "collect": "approval",
    "review": "approval",
    "coordination": "coordination",
    "wait": "waiting",
    "submit": "submission",
    "postcheck": "submission",
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


def attach_exact_usage(
    path: Path,
    *,
    item_key: str,
    event_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> dict:
    target = Path(path).expanduser().resolve()
    usage = _validated_usage(
        {
            "status": "exact",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    )
    if not _SHA256.fullmatch(item_key) or not _SAFE_TEXT.fullmatch(event_id):
        raise ValueError("observability_event_invalid")
    if not _SAFE_TEXT.fullmatch(model):
        raise ValueError("observability_event_invalid")
    with _exclusive_lock(target):
        payload = _read_log(target)
        for event in payload["events"]:
            if event["item_key"] != item_key or event["event_id"] != event_id:
                continue
            if event["token_usage"] == usage and event["model"] == model:
                return {"event_count": len(payload["events"]), "replayed": True}
            if event["token_usage"] != {"status": "unknown"}:
                raise ValueError("observability_usage_conflict")
            event["token_usage"] = usage
            event["model"] = model
            write_json_atomic(target, payload)
            return {"event_count": len(payload["events"]), "replayed": False}
    raise ValueError("observability_event_not_found")


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
    by_flow: dict[str, dict] = {}
    by_date: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    results = {result: 0 for result in sorted(_RESULTS)}
    for event in events.values():
        occurred_at = _timestamp(event["occurred_at"])
        date_key = occurred_at.strftime("%Y-%m-%d")
        hour_key = occurred_at.strftime("%Y-%m-%dT%H:00:00Z")
        _add(totals[_PHASE_CATEGORY[event["phase"]]], event)
        for bucket, key in (
            (by_flow, event["workflow_key"]),
            (by_date, date_key),
            (by_hour, hour_key),
        ):
            _add(bucket.setdefault(key, _empty_metrics()), event)
        results[event["result"]] += 1
    return {
        "schema_version": 1,
        "event_count": len(events),
        "totals": totals,
        "by_flow": dict(sorted(by_flow.items())),
        "by_date": dict(sorted(by_date.items())),
        "by_hour": dict(sorted(by_hour.items())),
        "results": results,
    }


def _read_event_file(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("observability_event_file_unreadable") from error
    return validate_event(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-finance-preaudit-observability")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--log", type=Path, required=True)
    record.add_argument("--event-file", type=Path, required=True)
    usage = commands.add_parser("usage")
    usage.add_argument("--log", type=Path, required=True)
    usage.add_argument("--item-key", required=True)
    usage.add_argument("--event-id", required=True)
    usage.add_argument("--model", required=True)
    usage.add_argument("--input-tokens", type=int, required=True)
    usage.add_argument("--output-tokens", type=int, required=True)
    usage.add_argument("--total-tokens", type=int, required=True)
    summary = commands.add_parser("summary")
    summary.add_argument("--log", type=Path, action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record":
        result = record_event(args.log, _read_event_file(args.event_file))
    elif args.command == "usage":
        result = attach_exact_usage(
            args.log,
            item_key=args.item_key,
            event_id=args.event_id,
            model=args.model,
            input_tokens=args.input_tokens,
            output_tokens=args.output_tokens,
            total_tokens=args.total_tokens,
        )
    else:
        result = summarize_logs(args.log)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
