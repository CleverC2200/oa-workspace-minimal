from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from apps import json_artifact


SCHEMA_VERSION = 1
KIND = "preaudit_prepare_failure_diagnostic"

# Fixed stages: where in the prepare (claim) pipeline the failure was observed.
STAGES = frozenset(
    {
        "selection_contract",
        "quarantine_probe",
        "identity_contract",
        "live_pending_read",
        "engine_build",
        "next_action",
        "unexpected_action",
        "child_exception",
        "identity_validation",
        "late_visible_contract",
    }
)
STOP_ACTIONS = frozenset({"cycle_stop", "stop"})

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CASE_KEY = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[0-9a-f]{8,64}$")
_MAX_PATH = 512


def _code(value: object) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def is_code(value: object) -> bool:
    """Whether ``value`` is a stable lowercase error code the receipt accepts."""
    return _code(value) is not None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def error_class(error: BaseException) -> str | None:
    """Return the exception class name; never its message text."""
    if not isinstance(error, BaseException):
        return None
    return _name(type(error).__name__)


def last_frame(error: BaseException) -> str | None:
    """Return the innermost traceback function name, a code identifier only."""
    if not isinstance(error, BaseException):
        return None
    traceback = getattr(error, "__traceback__", None)
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    if traceback is None:
        return None
    code = getattr(traceback.tb_frame, "f_code", None)
    return _name(getattr(code, "co_name", None))


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def describe(
    stage: object,
    *,
    error_code: object = "production_probe_prepare_failed",
    reason_code: object = None,
    error: BaseException | None = None,
    stop_action: object = None,
    workflow_key: object = None,
    case_key: object = None,
    attempt: object = None,
    action_file: object = None,
) -> dict:
    """Transport-safe failure description a prepare child can hand back.

    Only codes, fixed enumerations and code identifiers travel; the exception
    message is never part of it. ``record`` accepts this mapping as kwargs.
    """
    if stage not in STAGES:
        raise ValueError("prepare_failure_stage_invalid")
    stable_error_code = _code(error_code)
    if stable_error_code is None:
        raise ValueError("prepare_failure_error_code_invalid")
    return {
        "stage": stage,
        "error_code": stable_error_code,
        "reason_code": reason_code or stable_error_code,
        "error_class_name": error_class(error),
        "last_frame_name": last_frame(error),
        "stop_action": stop_action,
        "workflow_key": workflow_key,
        "case_key": case_key,
        "attempt": attempt,
        "action_file": action_file,
    }


def _item(
    *,
    workflow_key: object,
    case_key: object,
    attempt: object,
    action_file: object,
) -> dict | None:
    values: dict[str, object] = {}
    if _code(workflow_key) is not None:
        values["workflow_key"] = workflow_key
    if isinstance(case_key, str) and _CASE_KEY.fullmatch(case_key):
        values["case_key"] = case_key
    if type(attempt) is int and attempt >= 1:
        values["attempt"] = attempt
    if (
        isinstance(action_file, str)
        and action_file.startswith("/")
        and len(action_file) <= _MAX_PATH
        and "\n" not in action_file
        and "\r" not in action_file
    ):
        values["action_file"] = action_file
    return values or None


def build(
    *,
    stage: object,
    error_code: object,
    reason_code: object = None,
    error: BaseException | None = None,
    error_class_name: object = None,
    last_frame_name: object = None,
    stop_action: object = None,
    run_id: object = None,
    controller_instance_id: object = None,
    workflow_key: object = None,
    case_key: object = None,
    attempt: object = None,
    action_file: object = None,
    observed_at: datetime | None = None,
) -> dict:
    """Build one bounded, redacted prepare-failure diagnostic.

    Only stable codes, fixed enumerations, code identifiers and the item
    identity are kept. Exception messages, task text, paths outside the
    attempt tree and any free-form text are dropped on purpose.
    """
    if stage not in STAGES:
        raise ValueError("prepare_failure_stage_invalid")
    stable_error_code = _code(error_code)
    if stable_error_code is None:
        raise ValueError("prepare_failure_error_code_invalid")
    stable_reason_code = _code(reason_code) if reason_code is not None else None
    if reason_code is not None and stable_reason_code is None:
        raise ValueError("prepare_failure_reason_code_invalid")
    if stop_action is not None and stop_action not in STOP_ACTIONS:
        raise ValueError("prepare_failure_stop_action_invalid")
    resolved_error_class = (
        error_class(error) if error is not None else None
    )
    if error_class_name is not None:
        resolved_error_class = _name(error_class_name)
    resolved_last_frame = last_frame(error) if error is not None else None
    if last_frame_name is not None:
        resolved_last_frame = _name(last_frame_name)

    def optional_identifier(value: object) -> str | None:
        if value is None:
            return None
        validated = _identifier(value)
        if validated is None:
            raise ValueError("prepare_failure_identifier_invalid")
        return validated

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "observed_at": _timestamp(observed_at),
        "stage": stage,
        "error_code": stable_error_code,
        "reason_code": stable_reason_code or stable_error_code,
        "error_class": resolved_error_class,
        "last_frame": resolved_last_frame,
        "stop_action": stop_action,
        "run_id": optional_identifier(run_id),
        "controller_instance_id": optional_identifier(controller_instance_id),
        "item": _item(
            workflow_key=workflow_key,
            case_key=case_key,
            attempt=attempt,
            action_file=action_file,
        ),
    }


def _digest(payload: dict) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def record(target_dir: Path, *, create: bool = False, **kwargs) -> Path:
    """Persist one diagnostic into ``target_dir``; identical input is a no-op.

    ``create`` may create the target directory, but only after the diagnostic
    itself validated, so an invalid input never leaves a directory behind.
    """
    directory = Path(target_dir)
    payload = build(**kwargs)
    if create and not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("prepare_failure_target_invalid")
    target = directory / f"prepare-failure-{_digest(payload)}.json"
    if target.is_symlink():
        raise ValueError("prepare_failure_target_invalid")
    if target.exists():
        if json.loads(target.read_text()) != payload:
            raise ValueError("prepare_failure_target_invalid")
        return target
    json_artifact.write_json_atomic(target, payload, indent=2)
    return target
