"""Evidence for a revoked, unwritten review; not a fabricated model failure."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import attempt_state, approval_guard as guard, approval_lifecycle, controller_handoff, review_failure_evidence as evidence

REASON = "codex_review_interrupted"
FORBIDDEN_WRITES = ("writer-boundary.json", "execution-receipt.json", "review-decision.json", "remark.txt", "submission-envelope.json")


def closed_run(run_id: str, action_file: Path) -> dict:
    path = approval_lifecycle.run_directory(run_id) / "closed-readonly.json"
    proof = guard.read(path)
    fields = {"schema_version", "kind", "run_id", "controller_instance_id", "controller", "project_root",
              "actions", "closed_at", "readonly_reaped", "writer_inactive", "cause", "cooldown"}
    if proof is not None and proof.get("schema_version") == 2:
        fields.add("retry_budget_fingerprint")
    if (proof is None or proof.get("schema_version") not in {1, 2} or proof.get("kind") != "approval_closed_readonly_run"
            or set(proof) != fields
            or proof.get("run_id") != run_id or proof.get("readonly_reaped") is not True
            or not isinstance(proof.get("controller_instance_id"), str)
            or controller_handoff._RUN_ID.fullmatch(proof["controller_instance_id"]) is None
            or not guard._valid_identity(proof.get("controller"))
            or not isinstance(proof.get("actions"), list) or any(not isinstance(action, str) for action in proof["actions"])
            or not isinstance(proof.get("project_root"), str) or not Path(proof["project_root"]).is_absolute()
            or proof.get("writer_inactive") is not True or not guard._timestamp(proof.get("closed_at"))
            or proof.get("cause") not in guard.MODEL_STOP_REASONS | {"controller_orphaned", "readonly_deadline_exceeded"}
            or str(action_file) not in proof.get("actions", [])
            or not action_file.is_relative_to(Path(proof["project_root"]) / "runtime" / "finance_preaudit_flows")):
        raise ValueError("review_interruption_untrusted")
    if proof["cause"] == guard.STOP_REASON:
        cooldown = proof["cooldown"]
        if (not isinstance(cooldown, dict) or cooldown.get("kind") != "approval_model_cooldown"
                or not guard._timestamp(cooldown.get("triggered_at")) or not guard._timestamp(cooldown.get("blocked_until"))
                or cooldown["blocked_until"] - cooldown["triggered_at"] != guard.COOLDOWN_SECONDS):
            raise ValueError("review_interruption_untrusted")
    if proof['cause'] == guard.RETRY_STOP_REASON:
        budget = guard.retry_budget(run_id)
        if (proof['schema_version'] != 2 or proof['cooldown'] is not None or budget is None or budget['retry_allowed']
                or budget['controller_instance_id'] != proof['controller_instance_id']
                or proof['retry_budget_fingerprint'] != evidence.file_fingerprint(guard.retry_budget_file(run_id))):
            raise ValueError('review_interruption_untrusted')
    elif proof['schema_version'] != 1:
        raise ValueError('review_interruption_untrusted')
    return proof


def record(action_file: Path, state: dict, *, run_id: str) -> str:
    proof = closed_run(run_id, action_file)
    if any(os.path.lexists(action_file.with_name(name)) for name in FORBIDDEN_WRITES):
        raise ValueError("review_interruption_written_or_unknown")
    for name in ("action.json", "state.json"):
        original = action_file.with_name(name)
        archived = action_file.with_name("interrupted-" + name)
        raw = original.read_bytes()
        previous = guard.read(archived)
        if previous is not None and previous != json.loads(raw):
            raise ValueError("review_interruption_untrusted")
        if previous is None:
            from apps.oa_approval import runtime_control
            runtime_control.write_control_bytes_atomic(archived, raw)
    payload = {"schema_version": 1, "kind": "preaudit_review_interruption", "run_id": run_id,
               "controller_instance_id": proof["controller_instance_id"], "action_file": str(action_file),
               "workflow_key": state["workflow_key"], "case_key": state["case_key"], "attempt": state["attempt"],
               "action_token_sha256": hashlib.sha256(state["action_token"].encode()).hexdigest(),
               "original_action_fingerprint": evidence.file_fingerprint(action_file.with_name("interrupted-action.json")),
               "original_state_fingerprint": evidence.file_fingerprint(action_file.with_name("interrupted-state.json")),
               "closed_run_fingerprint": evidence.file_fingerprint(approval_lifecycle.run_directory(run_id) / "closed-readonly.json"),
               "write_state": "not_requested", "next_step": (
                   "fresh_prepare_next_round" if proof['cause'] == guard.RETRY_STOP_REASON else "fresh_prepare_after_cooldown")}
    target = action_file.with_name("review-interruption.json")
    previous = guard.read(target)
    if previous is not None and previous != payload:
        raise ValueError("review_interruption_untrusted")
    guard.write(target, payload)
    return evidence.file_fingerprint(target)


def trusted(attempt_root: Path, state: dict) -> bool:
    try:
        action = attempt_state.read_action(attempt_root / "action.json", state=state)
        receipt = guard.read(attempt_root / "review-interruption.json")
        if not isinstance(action, dict) or receipt is None:
            return False
        proof = closed_run(receipt["run_id"], attempt_root / "action.json")
        required = {"action": "needs_admin", "execution_state": "needs_admin", "write_state": "not_requested",
                    "business_state": "not_available", "mode": "preview", "write_authorized": False, "reason": REASON}
        if (any(action.get(key) != value or state.get(key) != value for key, value in required.items())
                or any(os.path.lexists(attempt_root / name) for name in FORBIDDEN_WRITES)
                or receipt.get("schema_version") != 1 or receipt.get("kind") != "preaudit_review_interruption"
                or receipt.get("controller_instance_id") != proof["controller_instance_id"]
                or receipt.get("action_file") != str(attempt_root / "action.json")
                or any(receipt.get(key) != state.get(key) for key in ("workflow_key", "case_key", "attempt"))
                or receipt.get("write_state") != "not_requested" or receipt.get("next_step") != (
                    "fresh_prepare_next_round" if proof['cause'] == guard.RETRY_STOP_REASON else "fresh_prepare_after_cooldown")
                or receipt.get("action_token_sha256") != hashlib.sha256(state["action_token"].encode()).hexdigest()
                or state.get("review_interruption_fingerprint") != evidence.file_fingerprint(attempt_root / "review-interruption.json")
                or action.get("review_interruption_fingerprint") != state["review_interruption_fingerprint"]
                or receipt.get("closed_run_fingerprint") != evidence.file_fingerprint(approval_lifecycle.run_directory(receipt["run_id"]) / "closed-readonly.json")):
            return False
        original_state = guard.read(attempt_root / "interrupted-state.json")
        for name in ("action", "state"):
            path = attempt_root / f"interrupted-{name}.json"
            old = guard.read(path)
            if name == "action" and old is not None:
                old = attempt_state.view(attempt_root / "action.json", old, raw=path.read_bytes(), state=original_state)
            if (receipt[f"original_{name}_fingerprint"] != evidence.file_fingerprint(path) or old is None
                    or old.get("action") != "review_required" or old.get("write_state") not in {"authorized", "not_requested"}
                    or old.get("action_token") != state["action_token"]):
                return False
        return True
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return False


def select(root: Path, report: dict, allowed_workflows: frozenset[str]) -> tuple[Path, ...]:
    from . import reconciliation
    from .flow_engine import FlowEngine
    if report.get("continuation", {}).get("status") != "ready" or report["continuation"].get("blocking_reasons") != {}:
        raise ValueError("production_rolling_reconciliation_invalid")
    items = {row["item_key"]: row for row in report.get("manual_review", {}).get("items", [])}
    selected = []
    for state in reconciliation.latest_states(root):
        if state.get("workflow_key") not in allowed_workflows or state.get("reason") != REASON:
            continue
        action = Path(state["action_file"])
        row = items.get(state["case_key"], {})
        if row.get("category") in {"handled_not_pending", "invisible", "prior_round_submitted"}:
            continue
        if (row.get("category") != "pending_not_handled" or row.get("recommendation") != "confirmed_no_write_recovery_candidate"
                or not trusted(action.parent, state) or not FlowEngine.recoverable_no_write(root, action)):
            raise ValueError("review_interruption_recovery_blocked")
        selected.append((state["received_at"], state["request_id"], action))
    return tuple(item[2] for item in sorted(selected))
