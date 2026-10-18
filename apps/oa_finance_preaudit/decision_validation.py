from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from apps import json_artifact


CODES = frozenset({
    "decision_contract", "item_count", "item_fields", "item_values",
    "decision_fields", "decision_schema_version", "decision_kind",
    "decision_action_token", "decision_workflow", "decision_rule_version",
    "decision_overall", "decision_items_type", "decision_json", "decision_object", "decision_binding",
    "required_uncertain", "payment_company_conclusion", "payment_attachment_terms",
    "budget_conclusion", "deferred_payment_conclusion",
    "overall_mismatch", "fuel_trip_observation", "company_comparison_binding", "company_comparison_observations",
    "company_comparison_result", "company_comparison_missing",
    "packaging_visual_contract",
    "packaging_supplementary_checks",
    "packaging_page_coverage", "packaging_page_reference", "packaging_version_coverage", "packaging_overall_mismatch",
    "unspecified",
})


class InvalidDecision(ValueError):
    def __init__(self, validation_code: str):
        super().__init__("review_decision_invalid")
        self.validation_code = validation_code if validation_code in CODES else "unspecified"


def record_rejection(action_file: Path, decision: dict, error: ValueError, *, context_fingerprint: str | None = None,
                     action_token_sha256: str | None = None) -> None:
    action_path = Path(action_file)
    state_path = action_path.with_name("state.json")
    if action_path.is_symlink() or state_path.is_symlink():
        raise ValueError("decision_diagnostic_invalid")
    if action_token_sha256 is None:
        action = json.loads(action_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(action, dict) or not isinstance(state, dict):
            raise ValueError("decision_diagnostic_invalid")
        token = action.get("action_token")
        if not isinstance(token, str) or not token or state.get("action_token") != token:
            raise ValueError("decision_diagnostic_invalid")
        action_token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    elif not isinstance(action_token_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", action_token_sha256):
        raise ValueError("decision_diagnostic_invalid")
    decision_hash = hashlib.sha256(json.dumps(
        decision, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    code = getattr(error, "validation_code", "unspecified")
    payload = {
        "schema_version": 1,
        "kind": "preaudit_decision_rejection",
        "action_token_sha256": action_token_sha256,
        "decision_sha256": decision_hash,
        "validation_code": code if isinstance(code, str) and code in CODES else "unspecified",
    }
    if context_fingerprint is not None:
        payload["context_fingerprint"] = context_fingerprint
    receipt_file = action_path.with_name(f"review-rejection-{decision_hash}.json")
    if receipt_file.is_symlink():
        raise ValueError("decision_diagnostic_invalid")
    if os.path.lexists(receipt_file):
        if json.loads(receipt_file.read_text(encoding="utf-8")) != payload:
            raise ValueError("decision_diagnostic_invalid")
        return
    json_artifact.write_json_atomic(receipt_file, payload, indent=2)
