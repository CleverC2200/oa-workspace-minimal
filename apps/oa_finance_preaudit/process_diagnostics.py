from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TextIO

from apps import json_artifact

STAGES = frozenset({"thread.started", "turn.started", "item.started", "item.completed", "turn.completed"})


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
        for code, terms in {
            "transport_signal": ("connection", "tls", "dns", "stream disconnected"),
            "rate_limit_signal": ("rate limit", "429"),
            "authentication_signal": ("unauthorized", "401", "authentication"),
            "retry_signal": ("retry", "reconnect"),
        }.items():
            if any(term in lower for term in terms):
                flags.add(code)
        suffix = lower[-64:]
    return {"stderr_bytes": size, "stderr_signals": sorted(flags)}


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
        "stderr_bytes": max(0, stderr["stderr_bytes"]) if type(stderr.get("stderr_bytes")) is int else 0,
        "stderr_signals": sorted({code for code in stderr.get("stderr_signals", [])
                                  if isinstance(code, str) and code in {"transport_signal", "rate_limit_signal", "authentication_signal", "retry_signal"}}),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    target = path.with_name(f"review-process-{digest}.json")
    if target.is_symlink():
        raise ValueError("process_diagnostic_invalid")
    if target.exists():
        if json.loads(target.read_text()) != payload:
            raise ValueError("process_diagnostic_invalid")
        return
    json_artifact.write_json_atomic(target, payload, indent=2)
