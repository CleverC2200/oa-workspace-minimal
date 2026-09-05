from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import reconciliation, review_failure_evidence


def _state(
    root: Path,
    request_id: str,
    received_at: str,
    *,
    attempt: int = 1,
    execution_state: str = "needs_admin",
    write_state: str = "not_requested",
    reason: str = "material_collection_failed",
    created_at: str = "2026-08-24T00:00:00+00:00",
    include_review_failure_receipt: bool = True,
) -> None:
    import hashlib

    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    attempt_root = root / "travel_expense" / "cases" / case_key / f"attempt-{attempt}"
    attempt_root.mkdir(parents=True)
    action_file = attempt_root / "action.json"
    state_file = attempt_root / "state.json"
    state = {
                "schema_version": 1,
                "kind": "preaudit_action",
                "workflow_key": "travel_expense",
                "case_key": case_key,
                "attempt": attempt,
                "request_id": request_id,
                "received_at": received_at,
                "created_at": created_at,
                "execution_state": execution_state,
                "business_state": "not_available",
                "write_state": write_state,
                "reason": reason,
    }
    if reason == "codex_review_failed" and include_review_failure_receipt:
        receipt_file = attempt_root / "review-failure.json"
        receipt = review_failure_evidence.receipt_payload(
            action_token="test-action-token",
            workflow_key="travel_expense",
            failure_code="codex_process_failed",
            attempt_error_codes=["codex_process_failed", "codex_process_failed"],
        )
        receipt_file.write_text(json.dumps(receipt), encoding="utf-8")
        state.update(
            {
                "action": "needs_admin",
                "action_token": "test-action-token",
                "mode": "preview",
                "write_authorized": False,
                "action_file": str(action_file),
                "state_file": str(state_file),
                "review_failure_fingerprint": (
                    review_failure_evidence.file_fingerprint(receipt_file)
                ),
            }
        )
        action_file.write_text(
            json.dumps(
                {
                    key: state[key]
                    for key in (
                        "schema_version",
                        "kind",
                        "action",
                        "execution_state",
                        "business_state",
                        "write_state",
                        "workflow_key",
                        "mode",
                        "write_authorized",
                        "reason",
                        "action_file",
                        "state_file",
                        "case_key",
                        "attempt",
                        "review_failure_fingerprint",
                    )
                }
            ),
            encoding="utf-8",
        )
    state_file.write_text(json.dumps(state), encoding="utf-8")


def test_reconciliation_classifies_live_state_without_exposing_request_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    request_ids = (
        "sensitive-request-alpha",
        "sensitive-request-beta",
        "sensitive-request-gamma",
    )
    _state(root, request_ids[0], "2026-08-24 09:00:00")
    _state(
        root,
        request_ids[1],
        "2026-08-24 09:01:00",
        write_state="unknown",
        reason="writer_transport_unknown",
    )
    _state(
        root,
        request_ids[2],
        "2026-08-24 09:02:00",
        execution_state="closed",
        write_state="submitted",
        reason="",
    )

    result = reconciliation.reconcile(
        root,
        pending_rows=[
            {"request_id": request_ids[0], "received_at": "2026-08-24 09:00:00"},
            {"request_id": request_ids[1], "received_at": "2026-08-24 09:01:00"},
        ],
        handled_rows=[{"request_id": request_ids[2]}],
    )

    assert result["counts"] == {
        "handled_not_pending": 1,
        "pending_not_handled": 2,
    }
    assert result["recommendations"] == {
        "confirmed_no_write_recovery_candidate": 1,
        "manual_write_state_verification": 1,
        "none": 1,
    }
    def string_values(value: object) -> list[str]:
        if isinstance(value, dict):
            return [
                text
                for key, nested in value.items()
                for text in [str(key), *string_values(nested)]
            ]
        if isinstance(value, list):
            return [text for nested in value for text in string_values(nested)]
        return [value] if isinstance(value, str) else []

    assert all(
        request_id not in text
        for text in string_values(result)
        for request_id in request_ids
    )


def test_reconciliation_highlights_only_failures_still_live_pending(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    repeated_received_at = "2026-08-26T11:00:00+00:00"
    _state(
        root,
        "live-repeated",
        repeated_received_at,
        attempt=1,
        reason="writer_preflight_failed",
        created_at="2026-08-26T11:00:00+00:00",
    )
    _state(
        root,
        "live-repeated",
        repeated_received_at,
        attempt=2,
        reason="material_collection_failed",
        created_at="2026-08-26T11:30:00+00:00",
    )
    _state(
        root,
        "live-long",
        "2026-08-24T00:00:00+00:00",
        reason="write_authorization_invalid",
        created_at="2026-08-26T11:55:00+00:00",
    )
    _state(
        root,
        "first-failure-on-attempt-two",
        "2026-08-26T11:45:00+00:00",
        attempt=1,
        execution_state="closed",
        write_state="submitted",
        reason="",
        created_at="2026-08-26T11:45:00+00:00",
    )
    _state(
        root,
        "first-failure-on-attempt-two",
        "2026-08-26T11:45:00+00:00",
        attempt=2,
        reason="writer_preflight_failed",
        created_at="2026-08-26T11:50:00+00:00",
    )
    _state(
        root,
        "handled-old",
        "2026-08-23T00:00:00+00:00",
        attempt=4,
        reason="writer_result_unknown",
        write_state="unknown",
        created_at="2026-08-23T00:00:00+00:00",
    )

    result = reconciliation.reconcile(
        root,
        pending_rows=[
            {
                "request_id": "live-repeated",
                "received_at": repeated_received_at,
            },
            {
                "request_id": "live-long",
                "received_at": "2026-08-24T00:00:00+00:00",
            },
            {
                "request_id": "first-failure-on-attempt-two",
                "received_at": "2026-08-26T11:45:00+00:00",
            },
        ],
        handled_rows=[{"request_id": "handled-old"}],
        now=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )

    assert result["failure_attention"] == {
        "scope": "live_pending",
        "status": "attention_required",
        "affected_cases": 2,
        "repeated_failure_cases": 1,
        "long_wait_failure_cases": 1,
        "long_wait_threshold_seconds": 86400,
        "max_failure_attempts": 2,
        "oldest_wait_seconds": 216000,
        "failure_counts": {
            "material_collection_failed": 1,
            "write_authorization_invalid": 1,
        },
    }


@pytest.mark.parametrize(
    "reason",
    ("writer_preflight_failed", "submission_target_changed"),
)
def test_reconciliation_marks_preflight_no_write_as_recoverable(
    tmp_path: Path,
    reason: str,
) -> None:
    root = tmp_path / "flows"
    _state(root, "1001", "2026-08-24 09:00:00", reason=reason)

    result = reconciliation.reconcile(
        root,
        pending_rows=[
            {"request_id": "1001", "received_at": "2026-08-24 09:00:00"}
        ],
        handled_rows=[],
    )

    assert result["recommendations"] == {
        "confirmed_no_write_recovery_candidate": 1
    }


def test_reconciliation_detects_pending_handled_and_identity_conflicts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    _state(root, "2001", "2026-08-24 10:00:00")
    _state(root, "2002", "2026-08-24 10:01:00")
    _state(root, "2003", "2026-08-24 10:02:00")

    result = reconciliation.reconcile(
        root,
        pending_rows=[
            {"request_id": "2001", "received_at": "2026-08-24 10:00:00"},
            {"request_id": "2002", "received_at": "changed"},
        ],
        handled_rows=[{"request_id": "2001"}],
    )

    assert result["counts"] == {
        "identity_conflict": 1,
        "invisible": 1,
        "pending_handled_conflict": 1,
    }


def test_latest_attempt_alone_controls_quarantine_and_actionable_count(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    received_at = "2026-08-24 11:00:00"
    _state(root, "3001", received_at, attempt=1)
    old_state = next(root.glob("*/cases/*/attempt-1/state.json"))
    old_bytes = old_state.read_bytes()
    _state(
        root,
        "3001",
        received_at,
        attempt=2,
        execution_state="review_required",
        write_state="authorized",
        reason="",
    )

    summary = reconciliation.quarantine_summary(
        root,
        pending_rows=[{"request_id": "3001", "received_at": received_at}],
    )
    reconciled = reconciliation.reconcile(
        root,
        pending_rows=[{"request_id": "3001", "received_at": received_at}],
        handled_rows=[],
    )

    assert summary == {
        "local_quarantined": 0,
        "pending_quarantined": 0,
        "actionable_pending": 1,
    }
    assert reconciled["local_cases"] == 1
    assert reconciled["findings"][0]["attempt"] == 2
    assert old_state.read_bytes() == old_bytes


def test_quarantine_counts_each_latest_case_identity_once(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    received_at = "2026-08-24 11:01:00"
    _state(root, "3002", received_at, attempt=1)
    _state(root, "3002", received_at, attempt=2)

    summary = reconciliation.quarantine_summary(
        root,
        pending_rows=[
            {"request_id": "3002", "received_at": received_at},
            {"request_id": "3003", "received_at": "2026-08-24 11:02:00"},
        ],
    )

    assert summary == {
        "local_quarantined": 1,
        "pending_quarantined": 1,
        "actionable_pending": 1,
    }


def test_quarantine_counts_confirmed_prewrite_review_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    received_at = "2026-08-24 11:03:00"
    _state(
        root,
        "3004",
        received_at,
        reason="codex_review_failed",
    )

    summary = reconciliation.quarantine_summary(
        root,
        pending_rows=[{"request_id": "3004", "received_at": received_at}],
    )

    assert summary == {
        "local_quarantined": 1,
        "pending_quarantined": 1,
        "actionable_pending": 0,
    }


@pytest.mark.parametrize("tamper", ("missing", "changed", "action_kind"))
def test_quarantine_rejects_untrusted_prewrite_review_failure(
    tmp_path: Path,
    tamper: str,
) -> None:
    root = tmp_path / "flows"
    received_at = "2026-08-24 11:04:00"
    _state(
        root,
        "3005",
        received_at,
        reason="codex_review_failed",
        include_review_failure_receipt=tamper != "missing",
    )
    if tamper == "changed":
        receipt_file = next(root.glob("*/cases/*/attempt-1/review-failure.json"))
        receipt_file.write_text("{}", encoding="utf-8")
    elif tamper == "action_kind":
        action_file = next(root.glob("*/cases/*/attempt-1/action.json"))
        action = json.loads(action_file.read_text(encoding="utf-8"))
        action["kind"] = "invalid"
        action_file.write_text(json.dumps(action), encoding="utf-8")

    summary = reconciliation.quarantine_summary(
        root,
        pending_rows=[{"request_id": "3005", "received_at": received_at}],
    )

    assert summary == {
        "local_quarantined": 0,
        "pending_quarantined": 0,
        "actionable_pending": 1,
    }


def test_invalid_highest_attempt_never_falls_back_to_old_state(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    _state(root, "4001", "2026-08-24 12:00:00", attempt=1)
    latest = next(root.glob("*/cases/*")) / "attempt-2"
    latest.mkdir()
    (latest / "state.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        reconciliation.latest_states(root)


@pytest.mark.parametrize("missing_field", ("request_id", "received_at"))
def test_latest_state_requires_identity_bound_to_case_hash(
    tmp_path: Path,
    missing_field: str,
) -> None:
    root = tmp_path / "flows"
    _state(root, "4002", "2026-08-24 12:01:00")
    state_file = next(root.glob("*/cases/*/attempt-1/state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.pop(missing_field)
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        reconciliation.latest_states(root)


def test_latest_state_rejects_identity_that_does_not_match_case_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    _state(root, "4003", "2026-08-24 12:02:00")
    state_file = next(root.glob("*/cases/*/attempt-1/state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["request_id"] = "different-request"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        reconciliation.latest_states(root)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempt", True),
        ("schema_version", None),
        ("kind", "partial_state"),
        ("request_id", 4004),
        ("received_at", 20260824),
        ("execution_state", "invalid"),
        ("write_state", "invalid"),
    ),
)
def test_latest_state_requires_the_complete_runtime_contract(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root = tmp_path / "flows"
    _state(root, "4004", "2026-08-24 12:03:00")
    state_file = next(root.glob("*/cases/*/attempt-1/state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state[field] = value
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        reconciliation.latest_states(root)


@pytest.mark.parametrize("linked_level", ("workflow", "case", "attempt"))
def test_reconciliation_rejects_managed_symlink_components(
    tmp_path: Path,
    linked_level: str,
) -> None:
    root = tmp_path / "flows"
    outside = tmp_path / "outside"
    outside.mkdir()
    workflow = root / "travel_expense"
    cases = workflow / "cases"
    case = cases / ("a" * 64)
    attempt = case / "attempt-1"
    if linked_level == "workflow":
        root.mkdir()
        workflow.symlink_to(outside, target_is_directory=True)
    elif linked_level == "case":
        cases.mkdir(parents=True)
        case.symlink_to(outside, target_is_directory=True)
    else:
        case.mkdir(parents=True)
        attempt.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
        reconciliation.latest_states(root)
