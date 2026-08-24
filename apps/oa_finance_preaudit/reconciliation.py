from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
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


def _identity(row: dict) -> tuple[str, str]:
    request_id = str(row.get("request_id") or row.get("requestId") or "").strip()
    received_at = str(row.get("received_at") or "").strip()
    return request_id, received_at


def _item_key(request_id: str, received_at: str) -> str:
    return hashlib.sha256(f"{request_id}\x1f{received_at}".encode("utf-8")).hexdigest()


def latest_states(root: Path) -> list[dict]:
    latest: dict[Path, tuple[int, dict]] = {}
    for state_file in Path(root).glob("*/cases/*/attempt-*/state.json"):
        try:
            attempt = int(state_file.parent.name.removeprefix("attempt-"))
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        workflow_key = state_file.parents[3].name
        case_root = state_file.parent.parent
        if (
            not isinstance(state, dict)
            or workflow_key not in _WORKFLOW_KEYS
            or state.get("workflow_key") != workflow_key
            or state.get("attempt") != attempt
            or state.get("case_key") != case_root.name
        ):
            continue
        current = latest.get(case_root)
        if current is None or attempt > current[0]:
            latest[case_root] = (attempt, state)
    return [value[1] for value in latest.values()]


def quarantine_summary(
    root: Path,
    *,
    pending_rows: Iterable[dict],
) -> dict[str, int]:
    pending = {_identity(row) for row in pending_rows if isinstance(row, dict)}
    pending = {identity for identity in pending if identity[0] and identity[1]}
    quarantined = {
        _identity(state)
        for state in latest_states(root)
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


def reconcile(
    root: Path,
    *,
    pending_rows: Iterable[dict],
    handled_rows: Iterable[dict],
) -> dict:
    pending = {_identity(row) for row in pending_rows if isinstance(row, dict)}
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

    for state in latest_states(root):
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
        "counts": dict(sorted(counts.items())),
        "recommendations": dict(sorted(recommendations.items())),
        "findings": sorted(findings, key=lambda item: item["item_key"]),
    }
