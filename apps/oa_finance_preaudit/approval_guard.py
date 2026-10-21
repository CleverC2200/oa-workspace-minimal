"""Local admission and ownership proof for approval model processes only."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
from pathlib import Path
import signal
import sqlite3
import subprocess
from subprocess import Popen as _identity_popen
import time
from typing import Iterator
from uuid import uuid4

from apps.oa_approval import runtime_control
from . import production_config

MAX_MODEL_WORKERS = 3
COOLDOWN_SECONDS = 3 * 60 * 60
OBSERVATION_SECONDS = 5
STOP_REASON = "codex_official_error_cooldown"
RUN_ENV = "OA_APPROVAL_RUN_ID"
INSTANCE_ENV = "OA_APPROVAL_INSTANCE_ID"
OWNER_ENV = "OA_APPROVAL_OWNER_TASK_ID"
COOLDOWN_OVERRIDE_ENV = "OA_APPROVAL_COOLDOWN_OVERRIDE"
EXECUTION_OWNERS_ENV = "OA_APPROVAL_EXECUTION_OWNERS"
NOTICE_CODES = frozenset({"usage_limit", "model_unavailable", "rate_limit", "server_overloaded", "authentication"})
RETRYABLE_NOTICE_CODES = frozenset({"rate_limit", "server_overloaded"})
RETRY_STOP_REASON = "codex_transient_retry_exhausted"
MAX_TRANSIENT_RETRIES = 2
MODEL_STOP_REASONS = frozenset({STOP_REASON, RETRY_STOP_REASON})


def directory() -> Path:
    base = runtime_control.controller_lock_file().expanduser()
    if not base.is_absolute():
        base = runtime_control.PROJECT_ROOT / base
    return Path(os.path.abspath(base)).parent / "approval-guard"


def read(path: Path) -> dict | None:
    raw = runtime_control.read_control_bytes(path)
    if raw is None:
        return None
    if len(raw) > 1024 * 1024:
        raise ValueError("approval_guard_untrusted")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("approval_guard_untrusted")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("approval_guard_untrusted")
    return value


def write(path: Path, value: dict) -> None:
    runtime_control.write_control_bytes_atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def _timestamp(value: object) -> bool:
    return type(value) in {float, int} and math.isfinite(value) and value >= 0


def notice_code(error: object) -> str | None:
    """Only call on a native error envelope, never a message or tool result."""
    if not isinstance(error, dict):
        return None
    info = error.get("codexErrorInfo")
    code = info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) and len(info) == 1 else None
    codes = {"usageLimitExceeded": "usage_limit", "serverOverloaded": "server_overloaded",
             "modelNotFound": "model_unavailable", "modelUnavailable": "model_unavailable",
             "rateLimitExceeded": "rate_limit", "unauthorized": "authentication"}
    recognized = set()
    for native_code in (code, error.get("code")):
        if isinstance(native_code, str) and native_code in codes:
            recognized.add(codes[native_code])
    details = info.get("httpConnectionFailed") if code == "httpConnectionFailed" and isinstance(info, dict) else None
    for status in (details.get("httpStatusCode") if isinstance(details, dict) else None, error.get("httpStatusCode")):
        if type(status) is int and status in {401, 403, 429, 503}:
            recognized.add({401: "authentication", 403: "authentication", 429: "rate_limit", 503: "server_overloaded"}[status])
    message = error.get("message")
    if not isinstance(message, str):
        message = ''
    lower = message.lower().replace("\u2019", "'").replace("\u2018", "'")
    terms = {
        "usage_limit": ("you've hit your usage limit", "usage limit exceeded", "usage_limit_exceeded", "insufficient_quota"),
        "model_unavailable": ("model unavailable", "model is not available", "model_not_found", "does not exist or you do not have access"),
        "rate_limit": ("rate limit", "too many requests", "status 429"),
        "server_overloaded": ("server overloaded", "server is overloaded", "selected model is at capacity"),
        "authentication": ("account has been deactivated", "account is deactivated", "account suspended", "status 401", "status 403",
                           "unauthorized", "authentication failed", "invalid authentication credentials", "invalid api key", "incorrect api key"),
    }
    recognized.update(name for name, phrases in terms.items() if any(term in lower for term in phrases))
    return next((name for name in ('authentication', 'usage_limit', 'model_unavailable', 'rate_limit', 'server_overloaded')
                 if name in recognized), None)


def cooldown() -> dict | None:
    value = read(directory() / "cooldown.json")
    if value is None:
        return None
    if (set(value) != {"schema_version", "kind", "triggered_at", "blocked_until", "notice_code", "sources", "run_id"}
            or value.get("schema_version") != 1 or value.get("kind") != "approval_model_cooldown"
            or value.get("notice_code") not in NOTICE_CODES
            or not _timestamp(value.get("triggered_at")) or not _timestamp(value.get("blocked_until"))
            or value["blocked_until"] - value["triggered_at"] != COOLDOWN_SECONDS
            or not isinstance(value.get("sources"), list) or not value["sources"]
            or len(value["sources"]) > 1000
            or any(not isinstance(key, str) or len(key) != 64 for key in value["sources"])):
        raise ValueError("approval_guard_untrusted")
    return value


def retry_budget_file(run_id: str) -> Path:
    from . import approval_lifecycle
    return approval_lifecycle.run_directory(run_id) / "transient-retries.json"


def _retry_budget_record(run_id: str) -> dict | None:
    value = read(retry_budget_file(run_id))
    if value is None:
        return None
    if (set(value) != {"schema_version", "kind", "run_id", "controller_instance_id", "owner_task_id", "started_at", "events"}
            or value.get("schema_version") != 1 or value.get("kind") != "approval_transient_retry_budget"
            or value.get("run_id") != run_id or not _timestamp(value.get("started_at"))
            or not isinstance(value.get("events"), list) or not 1 <= len(value["events"]) <= MAX_TRANSIENT_RETRIES + 1
            or re.fullmatch(r"[0-9a-f]{32}", str(value.get("controller_instance_id"))) is None
            or not isinstance(value.get("owner_task_id"), str)):
        raise ValueError("approval_retry_budget_untrusted")
    keys = set()
    for event in value["events"]:
        if (not isinstance(event, dict) or set(event) != {"source_sha256", "notice_code", "occurred_at"}
                or event.get("notice_code") not in RETRYABLE_NOTICE_CODES
                or re.fullmatch(r"[0-9a-f]{64}", str(event.get("source_sha256"))) is None
                or event["source_sha256"] in keys or not _timestamp(event.get("occurred_at"))
                or not value["started_at"] <= event["occurred_at"] <= time.time() + OBSERVATION_SECONDS):
            raise ValueError("approval_retry_budget_untrusted")
        keys.add(event["source_sha256"])
    return value


def retry_budget(run_id: str) -> dict | None:
    value = _retry_budget_record(run_id)
    if value is None:
        return None
    return {**value, "failures": len(value["events"]),
            "retry_allowed": len(value["events"]) <= MAX_TRANSIENT_RETRIES}


def _record_transient_notice(code: str, source: str, occurred_at: float, project_root: Path,
                             *, owner_task_id: str | None = None) -> dict:
    from datetime import datetime
    from . import approval_lifecycle, controller_handoff
    manifest = controller_handoff._read_manifest()
    if manifest is None or manifest["state"] not in {"running", "completed"}:
        return {"status": "ignored", "reason": "no_active_round"}
    started = datetime.fromisoformat(manifest["started_at"]).timestamp()
    if occurred_at < started:
        return {"status": "ignored", "reason": "earlier_round"}
    from . import host_chat_review
    owner = owner_task_id or os.environ.get(OWNER_ENV)
    if owner is None:
        return {"status": "ignored", "reason": "no_active_observer"}
    if (manifest.get("owner_task_id") != owner or not owner
            or manifest.get("review_configuration", {}).get("transport") != "host_chat"):
        raise ValueError("approval_retry_round_untrusted")
    registry_path = host_chat_review.registry_file(owner, manifest["run_id"])
    registry = host_chat_review._registry(owner, run_id=manifest["run_id"], live=False) if os.path.lexists(registry_path) else None
    reviewers = {manifest["owner_task_id"]}
    if registry is not None:
        if any(registry.get(field) != manifest[field] for field in ("owner_task_id", "run_id", "controller_instance_id")):
            raise ValueError("approval_retry_round_untrusted")
        reviewers.update(item["reviewer_task_id"] for item in registry["reviewers"])
    if manifest['state'] == 'completed':
        if registry is None or registry['phase'] != 'packaging' or registry['state'] != 'open' or registry.get('shutdown') is not None:
            return {"status": "ignored", "reason": "no_active_round"}
        from scripts import finance_preaudit_run
        binding = registry['original_execution']
        report = finance_preaudit_run.read_execution(**binding)
        progress = approval_lifecycle._progress(manifest)
        if (manifest['children_reaped'] is not True or report.get('terminal') is not True
                or progress is None or Path(progress['project_root']).resolve() != Path(project_root).resolve()
                or report.get('status') != 'completed' or report.get('children_reaped') is not True
                or any(report.get(key) != manifest[key] for key in ('run_id', 'controller_instance_id'))
                or hashlib.sha256(host_chat_review._read(Path(binding['receipt_file']))[0]).hexdigest() != registry.get('finance_receipt_sha256')
                or not _timestamp(registry.get('heartbeat_at'))
                or not 0 <= time.time() - registry['heartbeat_at'] < host_chat_review.PARENT_HEARTBEAT_MAX_AGE
                or registry['execution_owners']):
            raise ValueError("approval_retry_round_untrusted")
    else:
        progress = approval_lifecycle._progress(manifest)
        if (runtime_control.controller_owner_pid() != manifest["controller_pid"]
                or progress is None or process_identity(manifest["controller_pid"]) != progress["controller"]
                or Path(progress['project_root']).resolve() != Path(project_root).resolve()
                or not execution_owners_alive()):
            raise ValueError("approval_retry_round_untrusted")
    fields = source.split(":", 2)
    if len(fields) != 3 or fields[0] != "main" or fields[1] not in reviewers:
        return {"status": "ignored", "reason": "other_round_source"}
    key = hashlib.sha256(source.encode()).hexdigest()
    with runtime_control.stop_control_lease():
        if controller_handoff._read_manifest() != manifest:
            return {"status": "ignored", "reason": "round_changed"}
        previous = _retry_budget_record(manifest["run_id"])
        if previous is not None and (previous["started_at"] != started or any(previous[field] != manifest[field] for field in
                                        ("run_id", "controller_instance_id", "owner_task_id"))):
            raise ValueError("approval_retry_budget_untrusted")
        events = previous["events"] if previous else []
        matched = next((event for event in events if event["source_sha256"] == key), None)
        if matched is not None and matched["notice_code"] != code:
            raise ValueError("approval_retry_budget_untrusted")
        if matched is None and len(events) <= MAX_TRANSIENT_RETRIES:
            value = {"schema_version": 1, "kind": "approval_transient_retry_budget", "run_id": manifest["run_id"],
                     "controller_instance_id": manifest["controller_instance_id"], "owner_task_id": manifest["owner_task_id"],
                     "started_at": started, "events": [*events, {"source_sha256": key, "notice_code": code, "occurred_at": occurred_at}]}
            write(retry_budget_file(manifest["run_id"]), value)
        result = retry_budget(manifest["run_id"])
        if not result["retry_allowed"]:
            runtime_control._write_stop_locked(RETRY_STOP_REASON)
        return result


def record_notice(code: str, source: str, *, occurred_at: float | None = None,
                  technical_stop_identity: tuple[int, int, int] | None = None, append_only: bool = False) -> dict | None:
    now = time.time()
    occurred_at = now if occurred_at is None else occurred_at
    if code not in NOTICE_CODES or not _timestamp(occurred_at) or occurred_at > now + OBSERVATION_SECONDS:
        raise ValueError("approval_notice_invalid")
    key = hashlib.sha256(source.encode()).hexdigest()
    with runtime_control.stop_control_lease():
        publish = True
        try:
            previous = cooldown()
            overridden = cooldown_was_overridden(previous) if previous else False
            if append_only and (previous is None or overridden or time.time() >= previous['blocked_until']
                                or not previous['triggered_at'] <= occurred_at <= previous['blocked_until']):
                publish = False
                return previous
            if previous and key in previous["sources"]:
                publish = now < previous["blocked_until"] and not overridden
                return previous
            if previous and now < previous["blocked_until"] and not overridden:
                # Other workers reporting this incident do not restart its clock.
                if len(previous["sources"]) >= 1000:
                    raise ValueError("approval_notice_capacity_exceeded")
                value = {**previous, "sources": [*previous["sources"], key]}
            else:
                value = {"schema_version": 1, "kind": "approval_model_cooldown",
                         "triggered_at": occurred_at, "blocked_until": occurred_at + COOLDOWN_SECONDS,
                         "notice_code": code, "sources": [key], "run_id": os.environ.get(RUN_ENV)}
            write(directory() / "cooldown.json", value)
            publish = now < value["blocked_until"]
            return value
        finally:
            if publish:
                if technical_stop_identity is not None and runtime_control.read_control_bytes(runtime_control.stop_file()) == b"stop_requested:codex_review_technical_failure\n":
                    metadata = runtime_control.stop_file().stat(follow_symlinks=False)
                    if (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns) == technical_stop_identity:
                        runtime_control.write_control_bytes_atomic(runtime_control.stop_file(), f"stop_requested:{STOP_REASON}\n".encode())
                runtime_control._write_stop_locked(STOP_REASON)


def project_official_errors(project_root: Path) -> Iterator[tuple[str, str, float]]:
    """Read recognized official terminal notices without recording or stopping.

    Cwd is an error-scope filter, not proof that a process belongs to approvals.
    No prompts, titles, credentials or generic log bodies are read.
    """
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    state, history = home / "state_5.sqlite", home / "thread_history_1.sqlite"
    try:
        with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=1) as database:
            database.execute("PRAGMA query_only=ON")
            database.execute("ATTACH DATABASE ? AS history", (history.as_uri() + "?mode=ro",))
            rows = database.execute(
                "SELECT t.id, e.turn_id, e.error_json, e.completed_at "
                "FROM threads t JOIN history.thread_turns e ON e.thread_id=t.id "
                "WHERE t.cwd=? AND t.model_provider='openai' AND e.status='failed' "
                "AND e.completed_at>=? AND e.error_json IS NOT NULL ORDER BY e.completed_at",
                (str(Path(project_root).resolve()), time.time() - COOLDOWN_SECONDS),
            ).fetchall()
        for thread, turn, raw, stamp in rows:
            error = json.loads(raw)
            code = notice_code(error)
            if code:
                yield code, f"main:{thread}:{turn}", stamp
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise ValueError("approval_official_history_unavailable") from error


def observe_project_errors(project_root: Path, *, owner_task_id: str | None = None) -> None:
    # OpenAI Desktop errors do not describe the isolated DeepSeek API reviewer.
    # Its own CLI notices still pass through record_notice and the same cooldown.
    if production_config.review_settings()["provider"] == "deepseek":
        return
    try:
        for code, source, stamp in project_official_errors(project_root):
            if code in RETRYABLE_NOTICE_CODES and production_config.review_settings()["transport"] == "host_chat":
                if runtime_control.stop_reason() == STOP_REASON:
                    record_notice(code, source, occurred_at=stamp, append_only=True)
                else:
                    _record_transient_notice(code, source, stamp, project_root, owner_task_id=owner_task_id)
            else:
                record_notice(code, source, occurred_at=stamp)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        raise ValueError("approval_official_history_unavailable") from error


def process_identity(pid: int) -> dict | None:
    if type(pid) is not int or pid <= 0:
        raise ValueError("approval_process_untrusted")
    # Preserve a real local ps even when tests replace model Popen with a double.
    try:
        with _identity_popen(["ps", "-p", str(pid), "-o", "lstart=", "-o", "pgid=", "-o", "uid=", "-o", "stat="],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             env={**os.environ, "LC_ALL": "C"}) as process:
            try:
                text, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)
                raise
        fields = text.split()
        if process.returncode == 1 and not fields:
            return None
        if process.returncode != 0 or len(fields) != 8:
            raise ValueError("approval_process_untrusted")
        if fields[7].startswith("Z"):
            return None
        return {"pid": pid, "birth": " ".join(fields[:5]), "pgid": int(fields[5]), "uid": int(fields[6])}
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("approval_process_untrusted") from error


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _valid_identity(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == {"pid", "birth", "pgid", "uid"}
            and type(value["pid"]) is int and value["pid"] > 0
            and type(value["pgid"]) is int and value["pgid"] > 0
            and value["uid"] == os.getuid() and isinstance(value["birth"], str) and bool(value["birth"]))


def _slot_path(index: int) -> Path:
    return directory() / f"model-slot-{index}.json"


def _slot(index: int) -> dict | None:
    value = read(_slot_path(index))
    if value is None:
        return None
    if (value.get("kind") != "approval_model_slot" or value.get("schema_version") != 1
            or value.get("slot") != index or value.get("state") not in {"launching", "running", "closed"}
            or not isinstance(value.get("launch_id"), str) or re.fullmatch(r"[0-9a-f]{32}", value["launch_id"]) is None
            or not _valid_identity(value.get("worker")) or not _timestamp(value.get("deadline"))
            or not _timestamp(value.get("created_at")) or value["deadline"] < value["created_at"]
            or any(value.get(key) is not None and (not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{32}", value[key]) is None)
                   for key in ("run_id", "controller_instance_id"))
            or (value.get("run_id") is None) != (value.get("controller_instance_id") is None)
            or value.get("model") not in {production_config.MODEL, "gpt-6.1-sol"}
            or not isinstance(value.get("project_root"), str) or not Path(value["project_root"]).is_absolute()
            or not isinstance(value.get("action_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", value["action_sha256"]) is None
            or not isinstance(value.get("action_file"), str) or not Path(value["action_file"]).is_absolute()
            or (value.get("pid") is not None and (type(value["pid"]) is not int or value["pid"] <= 0))
            or (value.get("process") is not None and (not _valid_identity(value["process"])
                or value["process"]["pid"] != value.get("pid") or value["process"]["pgid"] != value.get("pid")))):
        raise ValueError("approval_slot_untrusted")
    return value


def _slot_gone(value: dict) -> bool:
    if value["state"] == "closed":
        return True
    pid = value.get("pid")
    # An unregistered launch intent may represent a process started just before
    # its owner crashed. Never guess a PID or free that slot on owner death.
    if type(pid) is not int or pid <= 0:
        return False
    return not group_alive(pid)


def _close_slot(value: dict) -> None:
    if not _slot_gone(value):
        raise ValueError("approval_model_children_unknown")
    path = _slot_path(value["slot"])
    with runtime_control.stop_control_lease():
        current = _slot(value["slot"])
        if current and current["launch_id"] == value["launch_id"] and current["state"] == "closed":
            value.update(current)
            return
        if current != value:
            raise ValueError("approval_slot_changed")
        closed = {**value, "state": "closed", "closed_at": time.time()}
        write(directory() / "launches" / f"{value['launch_id']}.json", closed)
        write(path, closed)
        value.update(closed)


def cooldown_ignored(value: dict) -> bool:
    if COOLDOWN_OVERRIDE_ENV not in os.environ:
        return False
    from .cooldown_override import matches
    return matches(value)


def cooldown_was_overridden(value: dict) -> bool:
    if COOLDOWN_OVERRIDE_ENV not in os.environ:
        return False
    from .cooldown_override import recording_matches
    try:
        return recording_matches(value)
    except (OSError, RuntimeError, ValueError, TypeError):
        # Invalid waiver proof cannot relax admission or suppress an official stop.
        return False


def assert_admission() -> None:
    with runtime_control.stop_control_lease():
        _assert_admission_locked()


def _assert_admission_locked() -> None:
    value = cooldown()
    ignored = cooldown_ignored(value)
    if value and time.time() < value["blocked_until"] and not ignored:
        raise ValueError("approval_model_cooldown")
    if runtime_control.stop_requested():
        raise ValueError("stop_requested")
    if not execution_owners_alive():
        runtime_control._write_stop_locked("codex_review_technical_failure")
        raise ValueError("approval_execution_owner_lost")


def execution_owners_alive() -> bool:
    raw = os.environ.get(EXECUTION_OWNERS_ENV)
    if raw is None:
        return True
    try:
        if len(raw) > 4096:
            return False
        owners = json.loads(raw)
        return (isinstance(owners, list) and 1 <= len(owners) <= 3
                and all(_valid_identity(owner) and process_identity(owner["pid"]) == owner for owner in owners))
    except (ValueError, TypeError, OSError):
        return False


@contextmanager
def model_slot(action_file: Path, project_root: Path, timeout_seconds: float) -> Iterator[dict]:
    worker = process_identity(os.getpid())
    if worker is None:
        raise ValueError("approval_process_untrusted")
    value = None
    with runtime_control.stop_control_lease():
        _assert_admission_locked()
        for index in range(MAX_MODEL_WORKERS):
            previous = _slot(index)
            if previous is None or previous["state"] == "closed":
                value = {"schema_version": 1, "kind": "approval_model_slot", "slot": index,
                         "launch_id": uuid4().hex, "state": "launching", "pid": None, "process": None,
                         "worker": worker, "run_id": os.environ.get(RUN_ENV),
                         "controller_instance_id": os.environ.get(INSTANCE_ENV),
                         "project_root": str(Path(project_root).resolve()), "action_file": str(action_file),
                         "action_sha256": hashlib.sha256(action_file.read_bytes()).hexdigest(),
                         "model": production_config.review_settings()["model"], "created_at": time.time(),
                         "deadline": time.time() + timeout_seconds}
                write(_slot_path(index), value)
                break
        if value is None:
            raise ValueError("approval_model_capacity_full")
    try:
        yield value
    finally:
        if value["state"] != "closed" and value["pid"] is not None and _slot_gone(value):
            current = _slot(value["slot"])
            if current and current["launch_id"] == value["launch_id"] and current["state"] != "launching":
                _close_slot(current)


def cancel_unlaunched(value: dict) -> None:
    if value["state"] != "launching" or value["pid"] is not None:
        raise ValueError("approval_slot_untrusted")
    with runtime_control.stop_control_lease():
        cancel_unlaunched_locked(value)


def cancel_unlaunched_locked(value: dict) -> None:
    if _slot(value["slot"]) != value or value["state"] != "launching" or value["pid"] is not None:
        raise ValueError("approval_slot_changed")
    value.update(state="closed", closed_at=time.time(), cancel_reason="model_not_started")
    write(_slot_path(value["slot"]), value)


def register_model(value: dict, pid: int, command: list[str]) -> None:
    if _slot(value["slot"]) != value:
        raise ValueError("approval_slot_changed")
    identity = process_identity(pid)
    if identity is not None and (identity["pgid"] != pid or identity["uid"] != os.getuid()):
        raise ValueError("approval_process_untrusted")
    value.update(state="running", pid=pid, process=identity,
                 command_sha256=hashlib.sha256(json.dumps(command).encode()).hexdigest())
    write(_slot_path(value["slot"]), value)


def models_reaped(run_id: str | None = None, *, worker: dict | None = None) -> bool:
    for index in range(MAX_MODEL_WORKERS):
        value = _slot(index)
        if (value and (run_id is None or value["run_id"] == run_id)
                and (worker is None or value["worker"] == worker) and not _slot_gone(value)):
            return False
    return True


def _terminate_registered(identity: dict) -> bool:
    if not _valid_identity(identity) or identity["pgid"] != identity["pid"]:
        raise ValueError("approval_process_untrusted")
    actual = process_identity(identity["pid"])
    if actual is None:
        return not group_alive(identity["pgid"])
    if actual != identity:
        raise ValueError("approval_process_identity_changed")
    try:
        os.killpg(identity["pgid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 1
    while group_alive(identity["pgid"]) and time.monotonic() < deadline:
        time.sleep(.05)
    if group_alive(identity["pgid"]):
        if process_identity(identity["pid"]) != identity:
            return False
        try:
            os.killpg(identity["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + 1
    while group_alive(identity["pgid"]) and time.monotonic() < deadline:
        time.sleep(.05)
    return not group_alive(identity["pgid"])


def reap_models(*, run_id: str | None = None, worker: dict | None = None, orphaned: bool = False) -> bool:
    """Only registered approval CLI groups, never a controller or Writer group."""
    for index in range(MAX_MODEL_WORKERS):
        value = _slot(index)
        if value is None or value["state"] == "closed":
            continue
        if run_id is not None and value["run_id"] != run_id:
            continue
        if worker is not None and value["worker"] != worker:
            continue
        if _slot_gone(value):
            _close_slot(value)
            continue
        owner = process_identity(value["worker"]["pid"])
        dead_owner = owner != value["worker"]
        if not (dead_owner or orphaned or time.time() >= value["deadline"]):
            continue
        identity = value["process"]
        if identity is None:
            return False
        if not _terminate_registered(identity):
            return False
        _close_slot(value)
    return models_reaped(run_id, worker=worker)


def maintenance_writer_idle() -> bool:
    path = runtime_control.request_writer_lock_file(runtime_control.GLOBAL_WRITER_KEY, runtime_control.writer_lock_file())
    return (not runtime_control.writer_active() and runtime_control.active_request_writers() == 1
            and runtime_control.writer_active(path)
            and runtime_control.read_control_bytes(path) == str(os.getpid()).encode())


def clear_expired_cooldown() -> bool:
    """Caller must hold the controller lease and prove all readonly work gone."""
    with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY, runtime_control.writer_lock_file(), blocking=False), runtime_control.stop_control_lease():
        value = cooldown()
        if (value is None or time.time() < value["blocked_until"]
                or runtime_control.controller_owner_pid() != os.getpid()
                or not maintenance_writer_idle() or not models_reaped()):
            return False
        return runtime_control._clear_stop_locked(f"stop_requested:{STOP_REASON}\n".encode())
