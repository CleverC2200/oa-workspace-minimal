from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import re
import json


SCHEMA_VERSION = 1
WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "tpm_small_marketing_fee",
    "general_expense",
    "packaging_layout_approval",
}


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



def read_state(path: Path, *, strict_path_identity: bool = False) -> dict:
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
    identity_error = (
        "runtime_state_identity_invalid"
        if strict_path_identity else "runtime_state_invalid"
    )
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != SCHEMA_VERSION
        or state.get("kind") != "preaudit_action"
        or state.get("execution_state")
        not in {"collecting", "review_required", "needs_admin", "closed"}
        or state.get("business_state")
        not in {"not_available", "pass", "needs_human"}
        or state.get("write_state")
        not in {"not_requested", "authorized", "submitting", "submitted", "unknown"}
    ):
        raise ValueError("runtime_state_invalid")
    if (
        state.get("workflow_key") not in WORKFLOW_KEYS
        or not isinstance(state.get("case_key"), str)
        or not isinstance(state.get("attempt"), int)
        or isinstance(state.get("attempt"), bool)
        or state["attempt"] < 1
        or not re.fullmatch(r"[0-9a-f]{64}", expected_case)
    ):
        raise ValueError(identity_error)
    if (
        state["workflow_key"] != expected_workflow
        or state["case_key"] != expected_case
        or state["attempt"] != expected_attempt
    ):
        raise ValueError(identity_error)
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
