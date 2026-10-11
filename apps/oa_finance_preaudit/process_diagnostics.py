from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from typing import Callable, TextIO

from apps import json_artifact
from apps.oa_approval import runtime_control
from . import approval_guard

STAGES = frozenset({"thread.started", "turn.started", "item.started", "item.completed", "turn.completed"})
_SIGNAL_TERMS = {
    "transport_signal": ("connection", "tls", "dns", "stream disconnected", "websocket closed"),
    "rate_limit_signal": ("rate limit", "429"),
    "capacity_signal": ("at capacity", "capacity limit", "overloaded"),
    "authentication_signal": ("unauthorized", "401", "authentication"),
    "retry_signal": ("retry", "reconnect"),
    "payload_too_large_signal": ("status 413", "413 request", "payload too large", "request entity too large"),
    "usage_limit_signal": ("you've hit your usage limit", "usage limit exceeded", "usage_limit_exceeded", "usagelimitexceeded", "insufficient_quota"),
    "model_access_signal": ("model is not available", "model unavailable", "model_not_found"),
}
_NOTICE_ROLES = frozenset({"first_notice", "first_blocking_notice", "first_official_notice"})
_NOTICE_CLASSES = approval_guard.NOTICE_CODES | {"network_reconnect", "terminal_failure", "unknown_error", "invalid_event"}
_NATIVE_ERROR_CODES = frozenset({"usageLimitExceeded", "serverOverloaded", "modelNotFound", "modelUnavailable",
                                "rateLimitExceeded", "unauthorized", "httpConnectionFailed"})
_NOTICE_EVENTS = frozenset({"error", "turn.failed", "thread.failed", "item.started", "item.completed"})
_NOTICE_FIELDS = frozenset({"schema_version", "kind", "role", "run_id_sha256", "controller_instance_id_sha256", "launch_id_sha256",
                            "action_sha256", "classification", "blocking", "source", "native_event_type", "native_error_code", "http_status", "observed_at"})


def _timestamp(value: object) -> bool:
    return type(value) in {int, float} and 0 <= value <= time.time() + approval_guard.OBSERVATION_SECONDS and math.isfinite(value)


def _valid_notice(payload: object, action_hash: str) -> bool:
    return (isinstance(payload, dict) and set(payload) == _NOTICE_FIELDS
            and payload.get("schema_version") == 1 and payload.get("kind") == "preaudit_native_notice"
            and isinstance(payload.get("role"), str) and payload["role"] in _NOTICE_ROLES and payload.get("action_sha256") == action_hash
            and isinstance(payload.get("classification"), str) and payload["classification"] in _NOTICE_CLASSES and type(payload.get("blocking")) is bool
            and payload["blocking"] == (payload["classification"] != "network_reconnect")
            and (payload["role"] != "first_blocking_notice" or payload["blocking"])
            and (payload["role"] != "first_official_notice" or payload["classification"] in approval_guard.NOTICE_CODES)
            and isinstance(payload.get("source"), str) and payload["source"] in {"stdout", "stderr"}
            and (payload.get("native_event_type") is None or isinstance(payload["native_event_type"], str) and payload["native_event_type"] in _NOTICE_EVENTS)
            and (payload.get("native_error_code") is None or isinstance(payload["native_error_code"], str) and payload["native_error_code"] in _NATIVE_ERROR_CODES)
            and (payload.get("http_status") is None or type(payload["http_status"]) is int and payload["http_status"] in {401, 403, 413, 429, 503})
            and _timestamp(payload.get("observed_at"))
            and all(value is None or isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in (payload.get(key) for key in ("run_id_sha256", "controller_instance_id_sha256", "launch_id_sha256")))
            and payload["launch_id_sha256"] is not None
            and (payload["run_id_sha256"] is None) == (payload["controller_instance_id_sha256"] is None))


def last_stage(stream: TextIO) -> str:
    stream.seek(0)
    stage = "no_event"
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if isinstance(kind, str) and kind in STAGES:
            stage = kind
    return stage


def stderr_summary(stream: TextIO, *, on_signals: Callable[[set[str]], None] | None = None,
                   on_chunk: Callable[[str], None] | None = None) -> dict:
    # Keep only fixed flags; never persist an error line or its contents.
    flags: set[str] = set()
    size = 0
    suffix = ""
    while chunk := stream.read(4096):
        size += len(chunk.encode("utf-8"))
        if on_chunk is not None:
            on_chunk(chunk)
        lower = suffix + chunk.lower().replace("\u2019", "'").replace("\u2018", "'")
        for code, terms in _SIGNAL_TERMS.items():
            if any(term in lower for term in terms):
                flags.add(code)
        if on_signals is not None:
            on_signals(flags)
        suffix = lower[-64:]
    return {"stderr_bytes": size, "stderr_signals": sorted(flags)}


def error_envelope(event: dict) -> dict | None:
    """Exclude normal messages and command output from error classification."""
    if event.get("type") == "error":
        return event
    if isinstance(event.get("type"), str) and event["type"] in {"turn.failed", "thread.failed"}:
        error = event.get("error")
        return error if isinstance(error, dict) else {}
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "error":
        return item
    return None


def classify_error(event: dict) -> dict | None:
    if event.get("type") == "invalid_event":
        return {"classification": "invalid_event", "blocking": True}
    error = error_envelope(event)
    if error is None:
        return None
    official = approval_guard.notice_code(error)
    if official:
        return {"classification": official, "blocking": True}
    if isinstance(event.get("type"), str) and event["type"] in {"turn.failed", "thread.failed"}:
        return {"classification": "terminal_failure", "blocking": True}
    message = error.get("message")
    info = error.get("codexErrorInfo")
    details = info.get("httpConnectionFailed") if isinstance(info, dict) and set(info) == {"httpConnectionFailed"} else None
    transport_code = (info == "httpConnectionFailed" or isinstance(details, dict)
                      and (details.get("httpStatusCode") is None or type(details["httpStatusCode"]) is int
                           and details["httpStatusCode"] in {408, 500, 502, 504}))
    if isinstance(message, str) and ("codexErrorInfo" not in error or transport_code):
        signals = stderr_summary(io.StringIO(message))["stderr_signals"]
        lower = message.lower()
        if ("transport_signal" in signals and any(term in lower for term in ("reconnecting", "retrying", "will retry"))
                and not any(term in lower for term in ("not reconnecting", "not retrying", "will not retry"))):
            return {"classification": "network_reconnect", "blocking": False}
    return {"classification": "unknown_error", "blocking": True}


def notice_payload(event: dict, *, source: str, observed_at: float, plain_stderr: bool = False) -> dict | None:
    notice = classify_error(event)
    if notice is None:
        return None
    error = error_envelope(event) or {}
    info = error.get("codexErrorInfo")
    code = info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) and len(info) == 1 else None
    details = info.get(code) if isinstance(info, dict) and isinstance(code, str) else None
    status = details.get("httpStatusCode") if isinstance(details, dict) else None
    return {**notice, "source": source,
            "native_event_type": event["type"] if not plain_stderr and isinstance(event.get("type"), str) and event["type"] in _NOTICE_EVENTS else None,
            "native_error_code": code if code in _NATIVE_ERROR_CODES else None,
            "http_status": status if type(status) is int and status in {401, 403, 413, 429, 503} else None,
            "observed_at": observed_at}


def record_notice(action_file: Path, *, slot: dict, role: str, notice: dict) -> str:
    """Write the first observation for this launch without replacing evidence."""
    if role not in _NOTICE_ROLES or hashlib.sha256(action_file.read_bytes()).hexdigest() != slot["action_sha256"]:
        raise ValueError("process_diagnostic_invalid")
    identities = {key + "_sha256": hashlib.sha256(slot[key].encode()).hexdigest() if slot.get(key) is not None else None
                  for key in ("run_id", "controller_instance_id", "launch_id")}
    payload = {"schema_version": 1, "kind": "preaudit_native_notice", "role": role,
               **identities, "action_sha256": slot["action_sha256"], **notice}
    if not _valid_notice(payload, slot["action_sha256"]):
        raise ValueError("process_diagnostic_invalid")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    prefix = f"review-notice-{identities['launch_id_sha256']}-{role}-"
    target = action_file.with_name(prefix + digest + ".json")
    with runtime_control.stop_control_lease():
        existing = list(action_file.parent.glob(prefix + "*.json"))
        if existing:
            if existing != [target] or approval_guard.read(target) != payload:
                raise ValueError("process_diagnostic_invalid")
            return digest
        json_artifact.write_json_atomic(target, payload, indent=2)
    return digest


def read_notices(action_file: Path, *, launch_id_sha256: str | None = None) -> dict:
    """Read the original launch's bounded notice evidence and verify its binding."""
    action_hash = hashlib.sha256(action_file.read_bytes()).hexdigest()
    result = {}
    launch = None
    if launch_id_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", launch_id_sha256):
        raise ValueError("process_diagnostic_invalid")
    pattern = "review-notice-*.json" if launch_id_sha256 is None else f"review-notice-{launch_id_sha256}-*.json"
    for path in action_file.parent.glob(pattern):
        if path.is_symlink():
            raise ValueError("process_diagnostic_invalid")
        payload = approval_guard.read(path)
        if (not _valid_notice(payload, action_hash) or payload["role"] in result
                or (launch is not None and launch != payload["launch_id_sha256"])):
            raise ValueError("process_diagnostic_invalid")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if path.name != f"review-notice-{payload['launch_id_sha256']}-{payload['role']}-{digest}.json":
            raise ValueError("process_diagnostic_invalid")
        result[payload["role"]] = payload
        launch = payload["launch_id_sha256"]
    return result


def event_error_signals(stream: TextIO) -> list[str]:
    """Classify CLI error events without keeping messages or tool output."""
    stream.seek(0)
    flags: set[str] = set()
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        error = error_envelope(event)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            flags.update(stderr_summary(io.StringIO(error["message"]))["stderr_signals"])
    return sorted(flags)


def record(action_file: Path, *, execution, elapsed_seconds: float, stderr: dict) -> None:
    path = Path(action_file)
    state_path = path.with_name("state.json")
    if path.is_symlink() or state_path.is_symlink():
        raise ValueError("process_diagnostic_invalid")
    action = json.loads(path.read_text())
    state = json.loads(state_path.read_text())
    token = action.get("action_token") if isinstance(action, dict) else None
    if not isinstance(token, str) or not token or not isinstance(state, dict) or state.get("action_token") != token:
        raise ValueError("process_diagnostic_invalid")
    events = {key: value for key, value in execution.event_counts.items()
              if key in {"thread.started", "turn.started", "item.started", "item.completed", "turn.completed", "error", "other"}
              and type(value) is int and value >= 0}
    stage = stderr.get("last_observed_stage", "no_event")
    if not isinstance(stage, str) or stage not in STAGES:
        stage = "no_event"
    payload = {
        "schema_version": 1, "kind": "preaudit_process_diagnostic",
        "action_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "last_observed_stage": stage, "event_counts": events,
        "elapsed_seconds": max(0, int(elapsed_seconds)),
        "process_state": execution.process_state if execution.process_state in {"exited", "timed_out", "start_failed"} else "unknown",
        "exit_code": execution.exit_code if type(execution.exit_code) is int else None,
        "event_signals": sorted({code for code in stderr.get("event_signals", [])
                                 if isinstance(code, str) and code in _SIGNAL_TERMS}),
        "stderr_bytes": max(0, stderr["stderr_bytes"]) if type(stderr.get("stderr_bytes")) is int else 0,
        "stderr_signals": sorted({code for code in stderr.get("stderr_signals", [])
                                  if isinstance(code, str) and code in _SIGNAL_TERMS}),
    }
    if "notice_timing" in stderr:
        timing = stderr["notice_timing"]
        if (not isinstance(timing, dict) or set(timing) != {"stop_requested_at", "process_exit_observed_at"}
                or any(stamp is not None and not _timestamp(stamp) for stamp in timing.values())):
            raise ValueError("process_diagnostic_invalid")
        payload["notice_timing"] = dict(timing)
    if "notice_launch_id_sha256" in stderr:
        launch = stderr["notice_launch_id_sha256"]
        if not isinstance(launch, str) or not re.fullmatch(r"[0-9a-f]{64}", launch):
            raise ValueError("process_diagnostic_invalid")
        notices = read_notices(action_file, launch_id_sha256=launch)
        artifacts = stderr.get("notice_artifacts")
        if (not isinstance(artifacts, dict) or set(notices) != set(artifacts)
                or any(role not in _NOTICE_ROLES or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                       or hashlib.sha256(json.dumps(notices[role], sort_keys=True).encode()).hexdigest() != digest
                       for role, digest in artifacts.items())):
            raise ValueError("process_diagnostic_invalid")
        payload["notice_launch_id_sha256"] = launch
        payload["notice_artifacts"] = dict(artifacts)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    target = path.with_name(f"review-process-{digest}.json")
    if target.is_symlink():
        raise ValueError("process_diagnostic_invalid")
    if target.exists():
        if json.loads(target.read_text()) != payload:
            raise ValueError("process_diagnostic_invalid")
        return
    json_artifact.write_json_atomic(target, payload, indent=2)
