"""Local administrative receipts; never generate a business review or retry a write."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from apps import json_artifact
from apps.oa_approval import writer_outcomes

from . import review_failure_evidence as evidence

RECEIPT_FILE = "manual-review-receipt.json"
ITEM_LOCAL_MATERIAL_FAILURE_DETAILS = frozenset({
    "attachment_download_capacity_timeout",
    "attachment_download_failed",
    "attachment_download_incomplete",
    "attachment_download_launch_timeout",
    "attachment_download_timeout",
    "invoice_buyer_read_failed",
    "travel_attachment_discovery_failed",
    "travel_attachment_failed",
    "travel_attachment_resources_incomplete",
})
_ARTIFACTS = (
    "action.json", "state.json", "review-failure.json", "writer-boundary.json",
    "execution-receipt.json", "review-decision.json", "remark.txt",
    "submission-envelope.json",
)
_FAILURE_CODES = frozenset({
    "codex_decision_invalid", "codex_review_failed", "codex_review_interrupted", "material_collection_failed",
    "write_authorization_invalid", "writer_result_unknown", *writer_outcomes.FAILURE_CODES,
})


def _bound_state(root: Path, state: dict) -> bool:
    request_id, received_at = state.get("request_id"), state.get("received_at")
    attempt = state.get("attempt")
    if not isinstance(request_id, str) or not request_id or not isinstance(received_at, str) or not received_at:
        return False
    key = hashlib.sha256(f"{request_id}\x1f{received_at}".encode()).hexdigest()
    action = evidence.read_receipt(root / "action.json")
    required = {
        "schema_version": 1, "kind": "preaudit_action", "action": "needs_admin",
        "execution_state": "needs_admin", "business_state": "not_available",
        "mode": "preview", "write_authorized": False,
        "action_file": str(root / "action.json"), "state_file": str(root / "state.json"),
    }
    return (
        isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0
        and root.name == f"attempt-{attempt}" and root.parent.name == key
        and root.parent.parent.name == "cases"
        and root.parent.parent.parent.name == state.get("workflow_key")
        and state.get("case_key") == key and isinstance(action, dict)
        and not any(path.is_symlink() for path in (root, *root.parents))
        and all(action.get(k) == v == state.get(k) for k, v in required.items())
        and all(action.get(k) == state.get(k) for k in (
            "reason", "write_state", "workflow_key", "case_key", "attempt", "failure_detail_code",
        ))
    )


def _scope(root: Path, state: dict, *, confirmed_rejection: bool) -> str:
    if not _bound_state(root, state) or state.get("write_state") != "not_requested":
        return "controller"
    reason = state.get("reason")
    if reason == writer_outcomes.SUBMIT_REJECTED:
        return "item" if confirmed_rejection else "controller"
    if reason == writer_outcomes.PREFLIGHT_FAILED:
        from . import production_write

        return "item" if production_write.verified_cycle_stop_no_write_failure(root, state) else "controller"
    if any(os.path.lexists(root / name) for name in _ARTIFACTS[3:]):
        return "controller"
    if reason == "codex_review_failed":
        return "item" if evidence.trusted_quarantine(root, state) else "controller"
    if reason == "codex_review_interrupted":
        from . import review_interruption
        return "item" if review_interruption.trusted(root, state) else "controller"
    if reason == "codex_decision_invalid":
        return "item"
    if reason == "material_collection_failed" and state.get("failure_detail_code") in ITEM_LOCAL_MATERIAL_FAILURE_DETAILS:
        return "item"
    return "controller"


def material_failure_without_write(root: Path, state: dict) -> bool:
    """Validate an unwritten material failure, including legacy attempts without receipts."""
    return (
        state.get("reason") == "material_collection_failed"
        and state.get("write_state") == "not_requested"
        and evidence.read_receipt(root / "state.json") == state
        and _bound_state(root, state)
        and not any(os.path.lexists(root / name) for name in _ARTIFACTS[3:])
    )


def _payload(root: Path, state: dict, *, scope: str) -> dict:
    code = state.get("reason")
    return {
        "schema_version": 1, "kind": "preaudit_manual_review_receipt",
        "item_key": state.get("case_key"), "workflow_key": state.get("workflow_key"),
        "attempt": state.get("attempt"),
        "failure_code": code if code in _FAILURE_CODES else "unknown_failure",
        "scope": scope, "write_state": state.get("write_state"),
        "automatic_retry": False, "business_review": "not_available",
        "evidence_fingerprints": {name: evidence.file_fingerprint(root / name) for name in _ARTIFACTS},
    }


def persist(root: Path, state: dict, *, confirmed_rejection: bool = False) -> None:
    if not _bound_state(root, state):
        return
    target = root / RECEIPT_FILE
    if target.is_symlink():
        raise ValueError("persistent_state_invalid")
    json_artifact.write_json_atomic(target, _payload(
        root, state, scope=_scope(root, state, confirmed_rejection=confirmed_rejection),
    ), indent=2)


def verified_receipt(root: Path, state: dict) -> dict | None:
    receipt = evidence.read_receipt(root / RECEIPT_FILE)
    if receipt is None or not _bound_state(root, state):
        return None
    scope = receipt.get("scope")
    if scope not in {"item", "controller"}:
        return None
    if scope == "item" and _scope(root, state, confirmed_rejection=True) != "item":
        return None
    if any((root / name).is_symlink() for name in _ARTIFACTS):
        return None
    if receipt != _payload(root, state, scope=scope):
        return None
    if state.get("reason") == writer_outcomes.SUBMIT_REJECTED and scope == "item":
        from . import production_write

        if not production_write.verified_submit_rejection(root, state):
            return None
    return receipt


def isolated(root: Path, state: dict) -> bool:
    receipt = verified_receipt(root, state)
    return receipt is not None and receipt["scope"] == "item"
