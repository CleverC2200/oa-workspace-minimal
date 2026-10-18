from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from apps.oa_approval import writer_outcomes

from . import case_recovery, item_failure, recovery_receipt


_WORKFLOW_KEYS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "tpm_small_marketing_fee",
    "general_expense",
    "packaging_layout_approval",
}
QUARANTINED_REASONS = frozenset(
    {
        "codex_decision_invalid",
        "codex_review_failed",
        "codex_review_interrupted",
        "material_collection_failed",
        "write_authorization_invalid",
        "writer_result_unknown",
        *writer_outcomes.FAILURE_CODES,
    }
)
_CASE_KEY = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^attempt-([1-9][0-9]*)$")
_OA_TIMEZONE = ZoneInfo("Asia/Shanghai")
_PREWRITE_ABSENT_ARTIFACTS = (
    "writer-boundary.json",
    "execution-receipt.json",
    "review-decision.json",
    "remark.txt",
    "submission-envelope.json",
)


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
    root: Path,
    states: Iterable[dict],
    pending_rows: Iterable[dict],
) -> dict[str, int]:
    pending = {_identity(row) for row in pending_rows if isinstance(row, dict)}
    pending = {identity for identity in pending if identity[0] and identity[1]}
    quarantined = {
        _identity(state) for state in states if _trusted_quarantine(root, state)
    }
    quarantined.discard(("", ""))
    quarantined_pending = len(quarantined & pending)
    return {
        "local_quarantined": len(quarantined),
        "pending_quarantined": quarantined_pending,
        "actionable_pending": len(pending) - quarantined_pending,
    }


def _trusted_quarantine(root: Path, state: dict) -> bool:
    if (
        state.get("execution_state") != "needs_admin"
        or state.get("reason") not in QUARANTINED_REASONS
    ):
        return False
    if state.get("reason") == "codex_review_interrupted":
        return item_failure.isolated(
            Path(root) / str(state["workflow_key"]) / "cases"
            / str(state["case_key"]) / f"attempt-{state['attempt']}",
            state,
        )
    if state.get("reason") != "codex_review_failed":
        return True
    return item_failure.isolated(
        Path(root)
        / str(state["workflow_key"])
        / "cases"
        / str(state["case_key"])
        / f"attempt-{state['attempt']}",
        state,
    )


def quarantine_summary(
    root: Path,
    *,
    pending_rows: Iterable[dict],
) -> dict[str, int]:
    return _quarantine_summary(root, latest_states(root), pending_rows)


def _confirmed_prior_round(root: Path, state: dict, rows: list[dict], now: datetime) -> bool:
    from .flow_engine import FlowEngine

    if len(rows) != 1 or rows[0].get("workflow_key") != state.get("workflow_key"):
        return False
    row = rows[0]
    if row.get("request_id") and row.get("requestId") and row["request_id"] != row["requestId"]:
        return False
    old_time, new_time = str(state.get("received_at") or ""), _identity(row)[1]
    timestamp = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
    if re.fullmatch(timestamp, old_time) is None or re.fullmatch(timestamp, new_time) is None:
        return False
    try:
        previous, current = _received_at_utc(old_time), _received_at_utc(new_time)
        created = datetime.fromisoformat(str(state["created_at"]).replace("Z", "+00:00"))
        if not previous < current <= now or not created < current:
            return False
    except (ValueError, TypeError):
        return False
    return FlowEngine.confirmed_submission(root, state)


def _deferred_historical_no_write_round(
    root: Path, state: dict, rows: list[dict], now: datetime,
) -> bool:
    """Defer a superseded failed round only when its no-write proof is complete."""
    if (
        state.get("execution_state") != "needs_admin"
        or state.get("write_state") != "not_requested"
        or not _trusted_quarantine(root, state)
        or len(rows) != 1
        or rows[0].get("workflow_key") != state.get("workflow_key")
    ):
        return False
    row = rows[0]
    if row.get("request_id") and row.get("requestId") and row["request_id"] != row["requestId"]:
        return False
    old_time, new_time = str(state.get("received_at") or ""), _identity(row)[1]
    timestamp = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
    if re.fullmatch(timestamp, old_time) is None or re.fullmatch(timestamp, new_time) is None:
        return False
    try:
        previous, current = _received_at_utc(old_time), _received_at_utc(new_time)
        created = datetime.fromisoformat(str(state["created_at"]).replace("Z", "+00:00"))
        if not previous < current <= now or not created < current:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return True


def _superseded_unsubmitted_round(
    root: Path, state: dict, attempt_root: Path, rows: list[dict], now: datetime,
) -> bool:
    """Defer one opened round that a strictly newer round replaced before the Writer.

    The old round is obsolete: its frozen evidence describes a version the live
    queue no longer carries. Only a round that provably never reached the Writer
    may leave the blocking set, so any Writer boundary, execution receipt,
    decision, remark or submission envelope keeps the identity conflict.
    """
    from .flow_engine import FlowEngine

    if (
        state.get("mode") != "controlled_write"
        or state.get("write_authorized") is not True
        or state.get("execution_state") != "review_required"
        or state.get("write_state") != "authorized"
        or len(rows) != 1
        or rows[0].get("workflow_key") != state.get("workflow_key")
    ):
        return False
    row = rows[0]
    if row.get("request_id") and row.get("requestId") and row["request_id"] != row["requestId"]:
        return False
    old_time, new_time = str(state.get("received_at") or ""), _identity(row)[1]
    timestamp = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
    if re.fullmatch(timestamp, old_time) is None or re.fullmatch(timestamp, new_time) is None:
        return False
    try:
        previous, current = _received_at_utc(old_time), _received_at_utc(new_time)
        created = datetime.fromisoformat(str(state["created_at"]).replace("Z", "+00:00"))
        if not previous < current <= now or not created < current:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    if any(
        os.path.lexists(Path(attempt_root) / name)
        for name in _PREWRITE_ABSENT_ARTIFACTS
    ):
        return False
    return FlowEngine.resumable_review(root, state)


def reconcile(
    root: Path,
    *,
    pending_rows: Iterable[dict],
    handled_rows: Iterable[dict],
    workflow_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    from .flow_engine import FlowEngine

    if workflow_key is not None and workflow_key not in _WORKFLOW_KEYS:
        raise ValueError("runtime_reconciliation_invalid")
    pending_input = [row for row in pending_rows if isinstance(row, dict)]
    pending = {_identity(row) for row in pending_input}
    pending = {identity for identity in pending if identity[0]}
    pending_ids = {request_id for request_id, _received_at in pending}
    handled_input = [row for row in handled_rows if isinstance(row, dict)]
    handled_ids = {
        request_id
        for row in handled_input
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
    manual_items: list[dict] = []
    blocking_reasons: Counter[str] = Counter()
    isolated_pending: set[tuple[str, str]] = set()

    states = [
        state
        for state in latest_states(root)
        if workflow_key is None or state.get("workflow_key") == workflow_key
    ]
    for state in states:
        attempt_root = Path(root) / str(state["workflow_key"]) / "cases" / str(state["case_key"]) / f"attempt-{state['attempt']}"
        request_id = str(state.get("request_id") or "").strip()
        received_at = str(state.get("received_at") or "").strip()
        if not request_id or not received_at:
            continue
        exact_pending = (request_id, received_at) in pending
        request_pending = request_id in pending_ids
        handled = request_id in handled_ids
        current_rows = [row for row in pending_input if _identity(row)[0] == request_id]
        if request_pending and not exact_pending:
            if not handled and _confirmed_prior_round(
                root, state, current_rows, current_time
            ):
                category = "prior_round_submitted"
            elif not handled and _superseded_unsubmitted_round(
                root, state, attempt_root, current_rows, current_time
            ):
                category = "prior_round_unsubmitted"
            else:
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
            if (category in {"handled_not_pending", "invisible"}
                    and not FlowEngine.historical_unknown_safe(root, state)):
                blocking_reasons["historical_unknown_failure_invalid"] += 1
        elif (
            category == "pending_not_handled"
            and state.get("execution_state") == "needs_admin"
            and state.get("write_state") == "not_requested"
            and len(current_rows) == 1
            and current_rows[0].get("workflow_key") == state["workflow_key"]
            and not (
                current_rows[0].get("request_id") and current_rows[0].get("requestId")
                and current_rows[0]["request_id"] != current_rows[0]["requestId"]
            )
            and FlowEngine.recoverable_no_write(root, attempt_root / "action.json")
        ):
            recommendation = "confirmed_no_write_recovery_candidate"
        elif (
            state.get("write_state") == "submitted"
            and category in {"handled_not_pending", "prior_round_submitted"}
        ):
            recommendation = "none"
        elif category == "prior_round_unsubmitted":
            recommendation = "deferred_unsubmitted_round"
        elif (
            category == "identity_conflict"
            and _deferred_historical_no_write_round(root, state, current_rows, current_time)
        ):
            recommendation = "deferred_historical_cleanup"
        else:
            recommendation = "manual_state_review"

        if category in {"identity_conflict", "pending_handled_conflict"}:
            if recommendation != "deferred_historical_cleanup":
                blocking_reasons[category] += 1
        elif exact_pending and state.get("write_state") in {
            "unknown", "submitting", "authorized", "submitted"
        }:
            if not (state.get("write_state") == "authorized"
                    and FlowEngine.resumable_review(root, state)):
                blocking_reasons["pending_write_state_unresolved"] += 1
        elif exact_pending and state.get("execution_state") == "needs_admin":
            if (
                state.get("write_state") == "not_requested"
                and (
                    (
                        state.get("reason") == "codex_review_failed"
                        and _trusted_quarantine(root, state)
                    )
                    or item_failure.isolated(
                        Path(root) / str(state["workflow_key"]) / "cases"
                        / str(state["case_key"]) / f"attempt-{state['attempt']}",
                        state,
                    )
                )
            ):
                isolated_pending.add((request_id, received_at))
            else:
                blocking_reasons["pending_failure_not_isolated"] += 1

        if state.get("execution_state") == "needs_admin":
            receipt = item_failure.verified_receipt(attempt_root, state)
            manual_items.append({
                "item_key": state["case_key"], "workflow_key": state["workflow_key"],
                "attempt": state["attempt"], "category": category,
                "failure_code": state["reason"] if state.get("reason") in QUARANTINED_REASONS else "unknown_failure",
                "write_state": state["write_state"], "automatic_retry": False,
                "scope": receipt["scope"] if receipt else "unverified",
                "receipt_status": "verified" if receipt else "missing_or_invalid",
                "recommendation": recommendation,
            })

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
                **({"handled_version_verified": any(
                    _identity(row) == (request_id, received_at)
                    and bool(received_at)
                    and not (row.get("request_id") and row.get("requestId")
                             and row["request_id"] != row["requestId"])
                    and row.get("workflow_key", state["workflow_key"]) == state["workflow_key"]
                    for row in handled_input
                )} if category == "handled_not_pending" else {}),
                **({"current_item_key": _item_key(*_identity(current_rows[0]))}
                   if category in {"prior_round_submitted", "prior_round_unsubmitted"}
                   else {}),
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
        "quarantine": _quarantine_summary(root, states, pending_input),
        "counts": dict(sorted(counts.items())),
        "recommendations": dict(sorted(recommendations.items())),
        "continuation": {
            "status": "blocked" if blocking_reasons else "ready",
            "scope": "controller" if blocking_reasons else (
                "item" if isolated_pending else "none"
            ),
            "isolated_pending": len(isolated_pending),
            "blocking_reasons": dict(sorted(blocking_reasons.items())),
        },
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
        "recoveries": {
            "read_only": True,
            "items": [recovery_receipt.projection(
                Path(root) / str(state["workflow_key"]) / "cases" / str(state["case_key"])
                / f"attempt-{state['attempt']}", state,
            ) for state in states if "recovered_from_attempt" in state],
        },
        "manual_review": {
            "read_only": True, "total": len(manual_items),
            "items": sorted(manual_items, key=lambda item: item["item_key"]),
        },
        "pending_dispositions": case_recovery.pending_dispositions(
            root, states, pending_input, manual_items, current_time, findings=findings,
        ),
        "findings": sorted(findings, key=lambda item: item["item_key"]),
    }
