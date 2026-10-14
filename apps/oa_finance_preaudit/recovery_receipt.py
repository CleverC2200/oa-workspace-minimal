"""Recovery audit receipts; reading them never authorizes a write or retry."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

from apps import json_artifact
from apps.oa_approval import writer_outcomes
from . import review_failure_evidence as evidence

NAME = "recovery-receipt.json"
_CODES = {"material_collection_failed", "write_authorization_invalid", "codex_decision_invalid",
          "codex_review_failed", "writer_result_unknown", *writer_outcomes.CONFIRMED_NO_WRITE_CODES}


def _fingerprints(source: Path) -> dict:
    values = {}
    for name in ("action.json", "state.json", "writer-boundary.json", "execution-receipt.json"):
        path = source / name
        value = evidence.file_fingerprint(path)
        if os.path.lexists(path) and (path.is_symlink() or not path.is_file() or value is None):
            raise ValueError("recovery_receipt_invalid")
        values[name] = value
    return values


def _write(target: Path, receipt: dict) -> None:
    json_artifact.write_json_atomic(target / NAME, receipt, indent=2)
    state = evidence.read_receipt(target / "state.json")
    if state is None:
        raise ValueError("recovery_receipt_invalid")
    state["recovery_receipt_fingerprint"] = evidence.file_fingerprint(target / NAME)
    json_artifact.write_json_atomic(target / "state.json", state, indent=2)
    if state.get("execution_state") == "needs_admin":
        from . import item_failure
        item_failure.persist(target, state)


def begin(target: Path, source: Path, state: dict, *, human_confirmed: bool) -> None:
    payload = {
        "schema_version": 1, "kind": "preaudit_recovery_receipt",
        "item_key": state["case_key"], "workflow_key": state["workflow_key"],
        "from_attempt": state["attempt"], "to_attempt": state["attempt"] + 1,
        "failure_code": state["reason"],
        "no_write_evidence": "human_confirmed_no_write" if human_confirmed else "confirmed_no_write",
        "source_fingerprints": _fingerprints(source),
        "status": "started", "started_at": datetime.now(timezone.utc).isoformat(),
        "result_action": "not_available",
    }
    _write(target, payload)


def finish(target: Path, *, result_action: str) -> None:
    receipt = evidence.read_receipt(target / NAME)
    if receipt is None:
        raise ValueError("recovery_receipt_invalid")
    receipt.update(
        status="prepared" if result_action == "review_required" else "preparation_failed",
        result_action=result_action if result_action in {"review_required", "needs_admin"} else "not_available",
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    _write(target, receipt)


def begin_withdrawal(target: Path, source: Path, state: dict, observation: dict) -> None:
    _write(target, {
        "schema_version": 2, "kind": "preaudit_recovery_receipt",
        "item_key": state["case_key"], "workflow_key": state["workflow_key"],
        "from_attempt": state["attempt"], "to_attempt": state["attempt"] + 1,
        "recovery_reason": "user_confirmed_withdrawal",
        "source_outcome": "submitted_then_withdrawn", "withdrawal_confirmed": True,
        "source_fingerprints": _fingerprints(source), "observation": observation,
        "status": "started", "started_at": datetime.now(timezone.utc).isoformat(),
        "result_action": "not_available",
    })


def projection(target: Path, state: dict) -> dict:
    base = {"item_key": state["case_key"], "workflow_key": state["workflow_key"],
            "attempt": state["attempt"], "automatic_retry": False}
    invalid = {**base, "receipt_status": "missing_or_invalid"}
    receipt = evidence.read_receipt(target / NAME)
    previous = state.get("recovered_from_attempt")
    if receipt is None or type(previous) is not int or previous < 1:
        return invalid
    source = target.with_name(f"attempt-{previous}")
    if state.get("recovery_receipt_fingerprint") != evidence.file_fingerprint(target / NAME):
        return invalid
    try:
        fingerprints = _fingerprints(source)
    except (OSError, ValueError):
        return invalid
    withdrawn = receipt.get("schema_version") == 2
    if any(not isinstance(receipt.get(name), str) for name in (
        "status", "result_action", "started_at",
    )):
        return invalid
    expected = {
        "schema_version": 1, "kind": "preaudit_recovery_receipt",
        "item_key": state["case_key"], "workflow_key": state["workflow_key"],
        "from_attempt": previous, "to_attempt": state["attempt"],
        "failure_code": state.get("recovery_reason"),
        "no_write_evidence": state.get("recovery_no_write_evidence", "confirmed_no_write"),
        "source_fingerprints": fingerprints,
    }
    if withdrawn:
        from . import flow_engine, withdrawn_submission
        source_state = evidence.read_receipt(source / "state.json")
        if (source_state is None or receipt.get("withdrawal_confirmed") is not True
                or not flow_engine.FlowEngine.confirmed_submission(target.parents[3], source_state)
                or not withdrawn_submission.valid_observation(source / "action.json", source_state,
                    receipt.get("observation"), fresh=False)):
            return invalid
        expected = {key: value for key, value in expected.items() if key not in {"failure_code", "no_write_evidence"}}
        expected.update(schema_version=2, recovery_reason=state.get("recovery_reason"),
            source_outcome="submitted_then_withdrawn", withdrawal_confirmed=True, observation=receipt["observation"])
    completed = receipt.get("status") in {"prepared", "preparation_failed"}
    required_fields = set(expected) | {"status", "result_action", "started_at"}
    if completed:
        required_fields.add("finished_at")
    if (set(receipt) != required_fields
            or (withdrawn and expected["recovery_reason"] != "user_confirmed_withdrawal")
            or (not withdrawn and (expected["failure_code"] not in _CODES
                or expected["no_write_evidence"] not in {"confirmed_no_write", "human_confirmed_no_write"}))
            or (receipt.get("status") == "prepared") != (receipt.get("result_action") == "review_required")
            or any(path.is_symlink() for path in (target, source, *target.parents))
            or previous + 1 != state["attempt"]
            or any(receipt.get(k) != v for k, v in expected.items())
            or any(expected["source_fingerprints"][name] is None for name in ("action.json", "state.json"))
            or receipt.get("status") not in {"started", "prepared", "preparation_failed"}
            or receipt.get("result_action") not in {"not_available", "review_required", "needs_admin"}):
        return invalid
    times = {}
    try:
        for name in ("started_at", "finished_at"):
            if name in receipt:
                value = datetime.fromisoformat(receipt[name])
                if value.tzinfo is None:
                    return invalid
                times[name] = value.isoformat()
    except (TypeError, ValueError):
        return invalid
    fields = ("recovery_reason", "source_outcome") if withdrawn else ("failure_code", "no_write_evidence")
    return {**base, "receipt_status": "verified", **{k: receipt[k] for k in (
        "from_attempt", "to_attempt", "status", "result_action", *fields,
    )}, **times}
