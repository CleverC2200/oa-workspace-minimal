from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


MODEL = "gpt-5.6-terra"
REASONING_EFFORT = "high"
PROCESS_FAILURE_CODES = frozenset(
    {"codex_process_failed", "codex_process_incomplete"}
)


def receipt_payload(
    *,
    action_token: str,
    workflow_key: str,
    failure_code: str,
    attempt_error_codes: list[str],
) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_review_failure",
        "action_token": action_token,
        "workflow_key": workflow_key,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "process_attempts": 2,
        "process_retries": 1,
        "failure_code": failure_code,
        "attempt_error_codes": attempt_error_codes,
    }


def receipt_matches(
    value: object,
    *,
    action_token: str,
    workflow_key: str,
    failure_code: str | None = None,
    attempt_error_codes: list[str] | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    actual_attempt_codes = value.get("attempt_error_codes")
    actual_failure_code = value.get("failure_code")
    if (
        not isinstance(actual_failure_code, str)
        or actual_failure_code not in PROCESS_FAILURE_CODES
        or not isinstance(actual_attempt_codes, list)
        or len(actual_attempt_codes) != 2
        or any(code not in PROCESS_FAILURE_CODES for code in actual_attempt_codes)
        or actual_attempt_codes[-1] != actual_failure_code
        or (failure_code is not None and actual_failure_code != failure_code)
        or (
            attempt_error_codes is not None
            and actual_attempt_codes != attempt_error_codes
        )
    ):
        return False
    return value == receipt_payload(
        action_token=action_token,
        workflow_key=workflow_key,
        failure_code=actual_failure_code,
        attempt_error_codes=actual_attempt_codes,
    )


def read_receipt(path: Path) -> dict | None:
    receipt_path = Path(path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return None
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def file_fingerprint(path: Path) -> str | None:
    artifact = Path(path)
    if artifact.is_symlink() or not artifact.is_file():
        return None
    try:
        raw = artifact.read_bytes()
    except OSError:
        return None
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def trusted_quarantine(attempt_root: Path, state: dict) -> bool:
    root = Path(attempt_root)
    action_path = root / "action.json"
    state_path = root / "state.json"
    receipt_path = root / "review-failure.json"
    if any(
        os.path.lexists(root / name)
        for name in (
            "writer-boundary.json",
            "execution-receipt.json",
            "review-decision.json",
            "remark.txt",
            "submission-envelope.json",
        )
    ):
        return False
    if action_path.is_symlink() or not action_path.is_file():
        return False
    try:
        action = json.loads(action_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(action, dict):
        return False
    required = {
        "schema_version": 1,
        "kind": "preaudit_action",
        "action": "needs_admin",
        "execution_state": "needs_admin",
        "business_state": "not_available",
        "write_state": "not_requested",
        "mode": "preview",
        "write_authorized": False,
        "reason": "codex_review_failed",
    }
    shared = (
        "workflow_key",
        "case_key",
        "attempt",
        "action_file",
        "state_file",
        "review_failure_fingerprint",
    )
    action_token = state.get("action_token")
    request_id = state.get("request_id")
    received_at = state.get("received_at")
    case_key = state.get("case_key")
    fingerprint = file_fingerprint(receipt_path)
    receipt = read_receipt(receipt_path)
    return (
        isinstance(action_token, str)
        and bool(action_token)
        and isinstance(request_id, str)
        and bool(request_id)
        and isinstance(received_at, str)
        and bool(received_at)
        and isinstance(case_key, str)
        and hashlib.sha256(
            f"{request_id}\x1f{received_at}".encode("utf-8")
        ).hexdigest()
        == case_key
        and root.name == f"attempt-{state.get('attempt')}"
        and root.parent.name == case_key
        and root.parent.parent.name == "cases"
        and root.parent.parent.parent.name == state.get("workflow_key")
        and all(action.get(key) == value for key, value in required.items())
        and all(state.get(key) == value for key, value in required.items())
        and all(action.get(key) == state.get(key) for key in shared)
        and action.get("action_file") == str(action_path)
        and action.get("state_file") == str(state_path)
        and isinstance(fingerprint, str)
        and action.get("review_failure_fingerprint") == fingerprint
        and receipt_matches(
            receipt,
            action_token=action_token,
            workflow_key=str(state.get("workflow_key") or ""),
        )
    )
