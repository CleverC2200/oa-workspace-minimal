from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import reconciliation


def _state(
    root: Path,
    request_id: str,
    received_at: str,
    *,
    attempt: int = 1,
    execution_state: str = "needs_admin",
    write_state: str = "not_requested",
    reason: str = "material_collection_failed",
) -> None:
    import hashlib

    case_key = hashlib.sha256(
        f"{request_id}\x1f{received_at}".encode("utf-8")
    ).hexdigest()
    attempt_root = root / "travel_expense" / "cases" / case_key / f"attempt-{attempt}"
    attempt_root.mkdir(parents=True)
    (attempt_root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "preaudit_action",
                "workflow_key": "travel_expense",
                "case_key": case_key,
                "attempt": attempt,
                "request_id": request_id,
                "received_at": received_at,
                "created_at": "2026-08-24T00:00:00+00:00",
                "execution_state": execution_state,
                "business_state": "not_available",
                "write_state": write_state,
                "reason": reason,
            }
        ),
        encoding="utf-8",
    )


def test_reconciliation_classifies_live_state_without_exposing_request_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    _state(root, "1001", "2026-08-24 09:00:00")
    _state(
        root,
        "1002",
        "2026-08-24 09:01:00",
        write_state="unknown",
        reason="writer_transport_unknown",
    )
    _state(
        root,
        "1003",
        "2026-08-24 09:02:00",
        execution_state="closed",
        write_state="submitted",
        reason="",
    )

    result = reconciliation.reconcile(
        root,
        pending_rows=[
            {"request_id": "1001", "received_at": "2026-08-24 09:00:00"},
            {"request_id": "1002", "received_at": "2026-08-24 09:01:00"},
        ],
        handled_rows=[{"request_id": "1003"}],
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
    serialized = json.dumps(result)
    assert "1001" not in serialized
    assert "1002" not in serialized
    assert "1003" not in serialized


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
