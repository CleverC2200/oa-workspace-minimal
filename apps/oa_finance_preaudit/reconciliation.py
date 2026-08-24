from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable


_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "packaging_layout_approval",
}
QUARANTINED_REASONS = frozenset(
    {
        "material_collection_failed",
        "writer_pending_conflict",
        "writer_postcheck_timeout",
        "writer_result_unknown",
        "writer_submit_rejected",
        "writer_transport_unknown",
    }
)
_CASE_KEY = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^attempt-([1-9][0-9]*)$")


def _identity(row: dict) -> tuple[str, str]:
    request_id = str(row.get("request_id") or row.get("requestId") or "").strip()
    received_at = str(row.get("received_at") or "").strip()
    return request_id, received_at


def _item_key(request_id: str, received_at: str) -> str:
    return hashlib.sha256(f"{request_id}\x1f{received_at}".encode("utf-8")).hexdigest()


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
                attempts: list[tuple[int, Path]] = []
                for attempt_root in case_root.iterdir():
                    match = _ATTEMPT.fullmatch(attempt_root.name)
                    if match is None:
                        continue
                    if attempt_root.is_symlink() or not attempt_root.is_dir():
                        raise ValueError("runtime_reconciliation_invalid")
                    attempts.append((int(match.group(1)), attempt_root))
                if not attempts:
                    continue
                attempt, attempt_root = max(attempts, key=lambda item: item[0])
                state_file = attempt_root / "state.json"
                if state_file.is_symlink() or not state_file.is_file():
                    raise ValueError("runtime_reconciliation_invalid")
                state = json.loads(state_file.read_text(encoding="utf-8"))
                created_at = state.get("created_at") if isinstance(state, dict) else None
                try:
                    parsed_created_at = datetime.fromisoformat(
                        str(created_at or "").replace("Z", "+00:00")
                    )
                except ValueError as error:
                    raise ValueError("runtime_reconciliation_invalid") from error
                if (
                    not isinstance(state, dict)
                    or state.get("schema_version") != 1
                    or state.get("kind") != "preaudit_action"
                    or state.get("workflow_key") != workflow_root.name
                    or not isinstance(state.get("attempt"), int)
                    or isinstance(state.get("attempt"), bool)
                    or state.get("attempt") != attempt
                    or state.get("case_key") != case_root.name
                    or not _identity(state)[0]
                    or not _identity(state)[1]
                    or _item_key(*_identity(state)) != case_root.name
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
                latest.append(state)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime_reconciliation_invalid") from error
    return latest


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
) -> dict:
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

    states = latest_states(root)
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
                "writer_submit_rejected",
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

    return {
        "schema_version": 1,
        "kind": "preaudit_reconciliation",
        "read_only": True,
        "local_cases": len(findings),
        "quarantine": _quarantine_summary(states, pending_input),
        "counts": dict(sorted(counts.items())),
        "recommendations": dict(sorted(recommendations.items())),
        "findings": sorted(findings, key=lambda item: item["item_key"]),
    }
