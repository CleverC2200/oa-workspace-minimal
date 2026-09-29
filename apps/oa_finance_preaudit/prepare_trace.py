"""Best-effort snapshots of the serial preparation worker, never business data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable

from apps import json_artifact


STAGES = frozenset({
    "selection_contract", "quarantine_probe", "live_pending_read",
    "identity_contract", "recovery_candidates", "engine_build", "next_action",
    "completion_wait", "action_validation",
})
_ROOT = Path(__file__).resolve().parents[2]
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_local = threading.local()
_sampler_lock = threading.Lock()
_sampler: threading.Thread | None = None
_active: Trace | None = None
_wake_sampler = threading.Event()


@dataclass
class Trace:
    directory: Path
    context: dict
    thread_id: int
    started: float = field(default_factory=time.monotonic)
    phase: tuple = field(default_factory=lambda: (
        "selection_contract", time.monotonic(), {},
    ))

    def snapshot(self) -> dict:
        now = time.monotonic()
        name, started, completed = self.phase
        durations = {**completed}
        durations[name] = durations.get(name, 0.0) + max(0.0, now - started)
        stack = []
        frame = sys._current_frames().get(self.thread_id)
        # Only repository code locations; no source text, locals or external paths.
        for _ in range(64):
            if frame is None or len(stack) >= 16:
                break
            try:
                source = Path(frame.f_code.co_filename).relative_to(_ROOT)
            except ValueError:
                source = None
            if source and source.parts[0] in {"apps", "scripts"}:
                function = frame.f_code.co_name
                if _IDENTIFIER.fullmatch(function):
                    stack.append({"file": source.as_posix(), "function": function,
                                  "line": frame.f_lineno})
            frame = frame.f_back
        return {
            **self.context,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "sampled_monotonic": now,
            "elapsed_seconds": max(0.0, now - self.started),
            "stage": name,
            "stage_seconds": max(0.0, now - started),
            "stage_totals_seconds": durations,
            "stack": stack,
        }


def stage(name: str) -> None:
    trace = getattr(_local, "trace", None)
    if trace is None or name not in STAGES:
        return
    previous, started, completed = trace.phase
    now = time.monotonic()
    totals = {**completed}
    totals[previous] = totals.get(previous, 0.0) + max(0.0, now - started)
    trace.phase = (name, now, totals)


def _write(directory: Path, name: str, payload: dict) -> None:
    # Do not follow runtime/control or target symlinks into unrelated evidence.
    if directory.resolve() != directory or (directory / name).is_symlink():
        raise ValueError("prepare_trace_target_invalid")
    directory.mkdir(parents=True, exist_ok=True)
    json_artifact.write_json_atomic(directory / name, payload)


def _sample_forever() -> None:
    while True:
        _wake_sampler.wait(1.0)
        _wake_sampler.clear()
        trace = _active
        if trace is not None:
            try:
                _write(trace.directory, f"prepare-progress-{trace.context['trace_id']}.json",
                       trace.snapshot())
            except Exception:
                # Diagnostics cannot change preparation, timeout or Writer behavior.
                pass


def run(prepare: Callable, selection: object, directory: Path, context: dict) -> dict:
    global _active, _sampler
    trace = Trace(directory, context, threading.get_ident())
    try:
        with _sampler_lock:
            if _sampler is None or not _sampler.is_alive():
                _sampler = threading.Thread(target=_sample_forever,
                                            name="oa-prepare-sampler", daemon=True)
                _sampler.start()
        _local.trace = trace
        _active = trace
        _wake_sampler.set()
    except Exception:
        # Thread creation failure must not suppress or repeat the actual operation.
        _local.trace = None
    try:
        return prepare(selection)
    finally:
        if _active is trace:
            _active = None
        _local.trace = None


def _duration(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _validated_sample(path: Path, context: dict) -> dict | None:
    if path.is_symlink():
        return None
    with path.open() as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        return None
    sample = json.loads(raw)
    if not isinstance(sample, dict) or any(sample.get(k) != v for k, v in context.items()):
        return None
    if sample.get("stage") not in STAGES:
        return None
    if not all(_duration(sample.get(k)) for k in (
        "sampled_monotonic", "elapsed_seconds", "stage_seconds",
    )):
        return None
    stamp = datetime.fromisoformat(sample["sampled_at"])
    totals, stack = sample["stage_totals_seconds"], sample["stack"]
    if not isinstance(totals, dict) or not all(
        k in STAGES and _duration(v) for k, v in totals.items()
    ) or not isinstance(stack, list) or len(stack) > 16:
        return None
    for frame in stack:
        if not isinstance(frame, dict) or set(frame) != {"file", "function", "line"}:
            return None
        source = Path(frame["file"])
        if (source.is_absolute() or ".." in source.parts or not source.parts
                or source.parts[0] not in {"apps", "scripts"}
                or source.suffix != ".py" or not (_ROOT / source).is_file()
                or not _IDENTIFIER.fullmatch(frame["function"])
                or type(frame["line"]) is not int or frame["line"] <= 0):
            return None
    return {
        **context, "sampled_at": stamp.isoformat(),
        **{k: sample[k] for k in (
            "sampled_monotonic", "elapsed_seconds", "stage", "stage_seconds",
            "stage_totals_seconds", "stack",
        )},
    }


def record_timeout(directory: Path, context: dict) -> None:
    """Called after process-group reclamation; stale or malformed samples stay unknown."""
    try:
        sample = _validated_sample(
            directory / f"prepare-progress-{context['trace_id']}.json", context,
        )
    except Exception:
        sample = None
    now = time.monotonic()
    if sample is not None and sample["sampled_monotonic"] > now:
        sample = None
    payload = {
        "schema_version": 1, "kind": "preaudit_prepare_timeout_diagnostic",
        **context, "error_code": "production_probe_prepare_timeout",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sample_status": "recorded" if sample is not None else "unavailable",
        "sample_age_seconds": now - sample["sampled_monotonic"] if sample else None,
        "sample": sample,
    }
    try:
        _write(directory, f"prepare-timeout-{context['trace_id']}.json", payload)
    except Exception:
        pass
