from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from apps.oa_approval import writer_outcomes


_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "general_expense",
    "packaging_layout_approval",
}
QUARANTINED_REASONS = frozenset(
    {
        "material_collection_failed",
        "write_authorization_invalid",
        "writer_result_unknown",
        *writer_outcomes.FAILURE_CODES,
    }
)
_CASE_KEY = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^attempt-([1-9][0-9]*)$")
_OA_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _identity(row: dict) -> tuple[str, str]:
    request_id = str(row.get("request_id") or row.get("requestId") or "").strip()
    received_at = str(row.get("received_at") or "").strip()
    return request_id, received_at


def _item_key(request_id: str, received_at: str) -> str:
    return hashlib.sha256(f"{request_id}\x1f{received_at}".encode("utf-8")).hexdigest()


def _attempt_roots(case_root: Path) -> list[tuple[int, Path]]:
    attempts: list[tuple[int, Path]] = []
    for attempt_root in case_root.iterdir():
        match = _ATTEMPT.fullmatch(attempt_root.name)
        if match is None:
            continue
        if attempt_root.is_symlink() or not attempt_root.is_dir():
            raise ValueError("runtime_reconciliation_invalid")
        attempts.append((int(match.group(1)), attempt_root))
    return attempts


def _validated_state(
    state_file: Path,
    *,
    workflow_key: str,
    case_key: str,
    attempt: int,
) -> dict:
    if state_file.is_symlink() or not state_file.is_file():
        raise ValueError("runtime_reconciliation_invalid")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        created_at = state.get("created_at") if isinstance(state, dict) else None
        parsed_created_at = datetime.fromisoformat(
            str(created_at or "").replace("Z", "+00:00")
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("runtime_reconciliation_invalid") from error
    request_id = state.get("request_id") if isinstance(state, dict) else None
    received_at = state.get("received_at") if isinstance(state, dict) else None
    if isinstance(request_id, str) and isinstance(received_at, str):
        identity = request_id.strip(), received_at.strip()
    else:
        identity = "", ""
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or state.get("kind") != "preaudit_action"
        or state.get("workflow_key") != workflow_key
        or not isinstance(state.get("attempt"), int)
        or isinstance(state.get("attempt"), bool)
        or state.get("attempt") != attempt
        or state.get("case_key") != case_key
        or not identity[0]
        or not identity[1]
        or _item_key(*identity) != case_key
        or state.get("execution_state")
        not in {"collecting", "review_required", "needs_admin", "closed"}
        or state.get("business_state")
        not in {"not_available", "pass", "needs_human"}
        or state.get("write_state")
        not in {
            "not_requested",
            "authorized",
            "submitting",
            "submitted",
            "unknown",
        }
        or parsed_created_at.tzinfo is None
    ):
        raise ValueError("runtime_reconciliation_invalid")
    return state


def latest_states(root: Path) -> list[dict]:
    runtime_root = Path(root).expanduser()
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ValueError("runtime_reconciliation_invalid")
    latest: list[dict] = []
    try:
        workflow_roots = sorted(runtime_root.iterdir(), key=lambda path: path.name)
        for workflow_root in workflow_roots:
            if workflow_root.name not in _WORKFLOW_KEYS:
                continue
            if workflow_root.is_symlink() or not workflow_root.is_dir():
                raise ValueError("runtime_reconciliation_invalid")
            cases_root = workflow_root / "cases"
            if not cases_root.exists():
                continue
            if cases_root.is_symlink() or not cases_root.is_dir():
                raise ValueError("runtime_reconciliation_invalid")
            for case_root in sorted(cases_root.iterdir(), key=lambda path: path.name):
                if case_root.is_symlink():
                    raise ValueError("runtime_reconciliation_invalid")
                if not case_root.is_dir() or not _CASE_KEY.fullmatch(case_root.name):
                    continue
                attempts = _attempt_roots(case_root)
                if not attempts:
                    continue
                attempt, attempt_root = max(attempts, key=lambda item: item[0])
                state = _validated_state(
                    attempt_root / "state.json",
                    workflow_key=workflow_root.name,
                    case_key=case_root.name,
                    attempt=attempt,
                )
                latest.append(state)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime_reconciliation_invalid") from error
    return latest


def _failure_attempt_count(root: Path, state: dict) -> int:
    workflow_key = str(state["workflow_key"])
    case_key = str(state["case_key"])
    case_root = Path(root).expanduser() / workflow_key / "cases" / case_key
    if case_root.is_symlink() or not case_root.is_dir():
        raise ValueError("runtime_reconciliation_invalid")
    try:
        attempts = _attempt_roots(case_root)
        return sum(
            _validated_state(
                attempt_root / "state.json",
                workflow_key=workflow_key,
                case_key=case_key,
                attempt=attempt,
            ).get("execution_state")
            == "needs_admin"
            for attempt, attempt_root in attempts
        )
    except OSError as error:
        raise ValueError("runtime_reconciliation_invalid") from error


def _received_at_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("runtime_reconciliation_invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_OA_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _quarantine_summary(
    states: Iterable[dict],
    pending_rows: Iterable[dict],
) -> dict[str, int]:
    pending = {_identity(row) for row in pending_rows if isinstance(row, dict)}
    pending = {identity for identity in pending if identity[0] and identity[1]}
    quarantined = {
        _identity(state)
        for state in states
        if state.get("execution_state") == "needs_admin"
        and state.get("reason") in QUARANTINED_REASONS
    }
    quarantined.discard(("", ""))
    quarantined_pending = len(quarantined & pending)
    return {
        "local_quarantined": len(quarantined),
        "pending_quarantined": quarantined_pending,
        "actionable_pending": len(pending) - quarantined_pending,
    }


def quarantine_summary(
    root: Path,
    *,
    pending_rows: Iterable[dict],
) -> dict[str, int]:
    return _quarantine_summary(latest_states(root), pending_rows)


def reconcile(
    root: Path,
    *,
    pending_rows: Iterable[dict],
    handled_rows: Iterable[dict],
    workflow_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    if workflow_key is not None and workflow_key not in _WORKFLOW_KEYS:
        raise ValueError("runtime_reconciliation_invalid")
    pending_input = [row for row in pending_rows if isinstance(row, dict)]
    pending = {_identity(row) for row in pending_input}
    pending = {identity for identity in pending if identity[0]}
    pending_ids = {request_id for request_id, _received_at in pending}
    handled_ids = {
        request_id
        for row in handled_rows
        if isinstance(row, dict)
        and (request_id := _identity(row)[0])
    }
    findings: list[dict] = []
    counts: Counter[str] = Counter()
    recommendations: Counter[str] = Counter()
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("runtime_reconciliation_invalid")
    live_failed: list[dict] = []

    states = [
        state
        for state in latest_states(root)
        if workflow_key is None or state.get("workflow_key") == workflow_key
    ]
    for state in states:
        request_id = str(state.get("request_id") or "").strip()
        received_at = str(state.get("received_at") or "").strip()
        if not request_id or not received_at:
            continue
        exact_pending = (request_id, received_at) in pending
        request_pending = request_id in pending_ids
        handled = request_id in handled_ids
        if request_pending and not exact_pending:
            category = "identity_conflict"
        elif exact_pending and handled:
            category = "pending_handled_conflict"
        elif handled:
            category = "handled_not_pending"
        elif exact_pending:
            category = "pending_not_handled"
        else:
            category = "invisible"

        if state.get("write_state") == "unknown":
            recommendation = "manual_write_state_verification"
        elif (
            category == "pending_not_handled"
            and state.get("execution_state") == "needs_admin"
            and state.get("reason") in {
                "material_collection_failed",
                *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
            }
            and state.get("write_state") == "not_requested"
        ):
            recommendation = "confirmed_no_write_recovery_candidate"
        elif (
            state.get("write_state") == "submitted"
            and category == "handled_not_pending"
        ):
            recommendation = "none"
        else:
            recommendation = "manual_state_review"

        counts[category] += 1
        recommendations[recommendation] += 1
        if exact_pending and state.get("execution_state") == "needs_admin":
            failure_attempts = _failure_attempt_count(root, state)
            received_at_utc = _received_at_utc(received_at)
            live_failed.append(
                {
                    "failure_attempts": failure_attempts,
                    "age_seconds": max(
                        0,
                        int((current_time - received_at_utc).total_seconds()),
                    ),
                    "failure_code": (
                        str(state.get("reason"))
                        if state.get("reason") in QUARANTINED_REASONS
                        else "unknown_failure"
                    ),
                }
            )
        findings.append(
            {
                "item_key": _item_key(request_id, received_at),
                "workflow_key": state.get("workflow_key"),
                "attempt": state.get("attempt"),
                "local_execution_state": state.get("execution_state"),
                "local_write_state": state.get("write_state"),
                "category": category,
                "recommendation": recommendation,
            }
        )

    long_wait_threshold_seconds = 24 * 60 * 60
    attention = [
        item
        for item in live_failed
        if item["failure_attempts"] >= 2
        or item["age_seconds"] >= long_wait_threshold_seconds
    ]
    failure_counts = Counter(item["failure_code"] for item in attention)
    return {
        "schema_version": 1,
        "kind": "preaudit_reconciliation",
        "read_only": True,
        "local_cases": len(findings),
        "quarantine": _quarantine_summary(states, pending_input),
        "counts": dict(sorted(counts.items())),
        "recommendations": dict(sorted(recommendations.items())),
        "failure_attention": {
            "scope": "live_pending",
            "status": "attention_required" if attention else "clear",
            "affected_cases": len(attention),
            "repeated_failure_cases": sum(
                item["failure_attempts"] >= 2 for item in live_failed
            ),
            "long_wait_failure_cases": sum(
                item["age_seconds"] >= long_wait_threshold_seconds
                for item in live_failed
            ),
            "long_wait_threshold_seconds": long_wait_threshold_seconds,
            "max_failure_attempts": max(
                (item["failure_attempts"] for item in attention),
                default=0,
            ),
            "oldest_wait_seconds": max(
                (item["age_seconds"] for item in attention),
                default=0,
            ),
            "failure_counts": dict(sorted(failure_counts.items())),
        },
        "findings": sorted(findings, key=lambda item: item["item_key"]),
    }
