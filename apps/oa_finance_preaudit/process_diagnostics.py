from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import TextIO

from apps import json_artifact

STAGES = frozenset({"thread.started", "turn.started", "item.started", "item.completed", "turn.completed"})
_SIGNAL_TERMS = {
    "transport_signal": ("connection", "tls", "dns", "stream disconnected", "websocket closed"),
    "rate_limit_signal": ("rate limit", "429"),
    "capacity_signal": ("at capacity", "capacity limit", "overloaded"),
    "authentication_signal": ("unauthorized", "401", "authentication"),
    "retry_signal": ("retry", "reconnect"),
    "payload_too_large_signal": ("status 413", "413 request", "payload too large", "request entity too large"),
}


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


def stderr_summary(stream: TextIO) -> dict:
    # Keep only fixed flags; never persist an error line or its contents.
    flags: set[str] = set()
    size = 0
    suffix = ""
    while chunk := stream.read(4096):
        size += len(chunk.encode("utf-8"))
        lower = suffix + chunk.lower()
        for code, terms in _SIGNAL_TERMS.items():
            if any(term in lower for term in terms):
                flags.add(code)
        suffix = lower[-64:]
    return {"stderr_bytes": size, "stderr_signals": sorted(flags)}


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
        item = event.get("item")
        error = event if event.get("type") == "error" else (
            item if isinstance(item, dict) and item.get("type") == "error" else None
        )
        if event.get("type") == "turn.failed":
            error = event.get("error")
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
    delivery = stderr.get("input_delivery")
    if isinstance(delivery, dict):
        modes = {"existing_reading_path", "complete_initial_input"}
        reasons = {None, "text_limit", "image_count_limit", "image_encoding_not_preserved",
                   "image_preparation_differs", "image_bytes_limit", "request_bytes_limit", "encoded_size_not_verified"}
        if (delivery.get("mode") not in modes or delivery.get("fallback_reason") not in reasons
                or type(delivery.get("images_supplied")) is not int
                or delivery["images_supplied"] < 0):
            raise ValueError("process_diagnostic_invalid")
        payload["input_delivery"] = {key: delivery[key] for key in ("mode", "fallback_reason", "images_supplied")}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    target = path.with_name(f"review-process-{digest}.json")
    if target.is_symlink():
        raise ValueError("process_diagnostic_invalid")
    if target.exists():
        if json.loads(target.read_text()) != payload:
            raise ValueError("process_diagnostic_invalid")
        return
    json_artifact.write_json_atomic(target, payload, indent=2)
