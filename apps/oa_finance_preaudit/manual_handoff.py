"""Bound administrative handoff notices, never synthetic business decisions."""
from __future__ import annotations

from pathlib import Path

from . import attempt_state, review_failure_evidence

KIND = "preaudit_manual_handoff"
WORKFLOWS = frozenset({"travel_expense", "business_entertainment", "business_support_fee",
                       "small_marketing_fee", "tpm_small_marketing_fee",
                       "general_expense"})
REASONS = {"codex_process_timeout": "审核超时"}
FIELDS = {"schema_version", "kind", "action_token", "workflow_key", "rule_version",
          "overall", "failure_code", "failure_receipt", "failure_fingerprint"}


def _read(path: Path) -> dict:
    value = review_failure_evidence.read_receipt(path)
    if value is None:
        raise ValueError("manual_handoff_invalid")
    return value


def _proof(root: Path, document: dict) -> bool:
    name = document.get("failure_receipt")
    code = document.get("failure_code")
    token = document.get("action_token")
    if not isinstance(name, str) or not isinstance(token, str) or not token:
        return False
    if code != "codex_process_timeout" or name != "review-failure.json":
        return False
    path = root / name
    receipt = review_failure_evidence.read_receipt(path)
    fingerprint = review_failure_evidence.file_fingerprint(path)
    if receipt is None or fingerprint is None or fingerprint != document.get("failure_fingerprint"):
        return False
    return review_failure_evidence.receipt_matches(
        receipt,
        action_token=token,
        workflow_key=document["workflow_key"],
        failure_code=code,
    )


def validate(document: dict, root: Path, action: dict) -> None:
    if (
        not isinstance(document, dict)
        or set(document) != FIELDS
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("kind") != KIND
        or document.get("overall") != "needs_human"
        or not isinstance(document.get("workflow_key"), str)
        or document.get("workflow_key") not in WORKFLOWS
        or document.get("workflow_key") != action.get("workflow_key")
        or document.get("action_token") != action.get("action_token", document.get("action_token"))
        or not isinstance(document.get("rule_version"), str)
        or document.get("rule_version") != _read(root / "review-packet.json").get("rule_version")
        or not _proof(root, document)
    ):
        raise ValueError("manual_handoff_invalid")


def require_unwritten(action: dict, state: dict, root: Path) -> None:
    required = {
        "action": "review_required", "execution_state": "review_required",
        "business_state": "not_available", "write_state": "authorized",
        "mode": "controlled_write", "write_authorized": True,
    }
    if any(action.get(key) != value or state.get(key) != value for key, value in required.items()):
        raise ValueError("manual_handoff_invalid")
    for name in ("writer-boundary.json", "execution-receipt.json", "review-decision.json",
                 "remark.txt", "submission-envelope.json"):
        path = root / name
        if path.is_symlink() or path.exists():
            raise ValueError("manual_handoff_invalid")


def build(action_file: Path, failure_code: str) -> dict:
    if failure_code not in REASONS:
        raise ValueError("manual_handoff_invalid")
    root = action_file.parent
    action = attempt_state.read_action(action_file)
    state = _read(root / "state.json")
    if not action.get("action_token") or state.get("action_token") != action.get("action_token"):
        raise ValueError("manual_handoff_invalid")
    paths = [root / "review-failure.json"]
    for path in paths:
        document = {
            "schema_version": 1, "kind": KIND, "overall": "needs_human",
            "workflow_key": action.get("workflow_key"), "action_token": action["action_token"],
            "rule_version": _read(root / "review-packet.json").get("rule_version"),
            "failure_code": failure_code, "failure_receipt": path.name,
            "failure_fingerprint": review_failure_evidence.file_fingerprint(path),
        }
        try:
            validate(document, root, action)
        except ValueError:
            continue
        return document
    raise ValueError("manual_handoff_invalid")


def remark(document: dict) -> str:
    return (
        "AI审核结论：需人工审核 ⚠️\n"
        f"特别说明：本次 AI 预审因{REASONS[document['failure_code']]}未完成，"
        "未形成有效业务审核结论。请财务人员人工核对材料并审核。"
    )
