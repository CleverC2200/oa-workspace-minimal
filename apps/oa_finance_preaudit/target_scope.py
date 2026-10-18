from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence

from .flow_engine import FlowEngine, WORKFLOW_KEYS
from . import reconciliation, recovery_receipt


_NO_WRITE_STATES = {("review_required", "authorized", "not_available"),
                    ("needs_admin", "not_requested", "not_available")}


@dataclass(frozen=True)
class Target:
    action_file: Path
    workflow_key: str
    item_key: str
    request_id: str
    received_at: str
    source_attempt: int
    recovery: bool
    material_fingerprint: str | None
    initially_submitted: bool

    def selection(self) -> dict:
        return {"action_file": str(self.action_file), "workflow_key": self.workflow_key,
                "item_key": self.item_key, "request_id": self.request_id,
                "received_at": self.received_at, "source_attempt": self.source_attempt,
                "recovery": self.recovery, "material_fingerprint": self.material_fingerprint}


@dataclass(frozen=True)
class TargetScope:
    root: Path
    targets: tuple[Target, ...]

    @classmethod
    def bind(cls, root: Path, *, resume: Sequence[Path], recover: Sequence[Path], workflows: frozenset[str]):
        resume_paths = {Path(path).expanduser().absolute() for path in resume}
        recovery_paths = {Path(path).expanduser().absolute() for path in recover}
        if resume_paths & recovery_paths:
            raise ValueError("production_target_scope_conflict")
        selected = {}
        for paths, recovery in ((resume, False), (recover, True)):
            for raw in paths:
                state = FlowEngine.inspect_target_action(root, raw, recovery=recovery)
                if state["workflow_key"] not in workflows:
                    raise ValueError("production_target_workflow_invalid")
                target = Target(Path(state["action_file"]), state["workflow_key"], state["case_key"],
                                state["request_id"], state["received_at"], state["attempt"], recovery,
                                state.get("material_fingerprint"), state.get("write_state") == "submitted")
                previous = selected.get(target.item_key)
                if previous is not None and previous != target:
                    raise ValueError("production_target_scope_conflict")
                selected[target.item_key] = target
        if not selected:
            raise ValueError("production_target_scope_empty")
        return cls(Path(root).resolve(), tuple(selected.values()))

    def selections(self) -> list[dict]:
        return [target.selection() for target in self.targets]

    def counts(self, reconciled: dict) -> tuple[int, int, int]:
        findings = reconciled.get("findings")
        if not isinstance(findings, list):
            raise ValueError("production_target_reconciliation_invalid")
        pending = quarantined = 0
        for target in self.targets:
            matched = [row for row in findings if row.get("item_key") == target.item_key
                       and row.get("workflow_key") == target.workflow_key]
            if len(matched) != 1:
                raise ValueError("production_target_reconciliation_invalid")
            row = matched[0]
            if row.get("category") == "pending_not_handled":
                pending += 1
                if row.get("local_execution_state") == "needs_admin":
                    quarantined += 1
        return pending, quarantined, pending - quarantined


    def pending_recovery_actions(self, reconciled: dict) -> tuple[Path, ...]:
        self.counts(reconciled)
        return tuple(target.action_file for target in self.targets if target.recovery and any(
            row.get("item_key") == target.item_key and row.get("workflow_key") == target.workflow_key
            and row.get("category") == "pending_not_handled" for row in reconciled["findings"]
        ))

    def outcome(self, reconciled: dict, *, controller_completed: bool) -> dict:
        findings = reconciled.get("findings")
        trusted = (reconciled.get("schema_version") == 1
                   and reconciled.get("kind") == "preaudit_reconciliation"
                   and reconciled.get("read_only") is True and isinstance(findings, list)
                   and all(isinstance(row, dict) for row in findings)
                   and isinstance(reconciled.get("continuation"), dict))
        try:
            states = {(state["workflow_key"], state["case_key"]): state
                      for state in reconciliation.latest_states(self.root)}
        except (OSError, ValueError):
            states = {}
            trusted = False
        items = []
        blocked = (not controller_completed or not trusted
                   or not isinstance(reconciled.get("continuation"), dict)
                   or reconciled["continuation"].get("status") != "ready")
        for target in self.targets:
            state = states.get((target.workflow_key, target.item_key))
            matches = ([row for row in findings if row.get("item_key") == target.item_key
                        and row.get("workflow_key") == target.workflow_key] if trusted else [])
            category = matches[0].get("category") if len(matches) == 1 else None
            item = {"item_key": target.item_key, "workflow_key": target.workflow_key,
                    "source_attempt": target.source_attempt,
                    "attempt": state.get("attempt") if state else None,
                    "operation": "recover" if target.recovery else "resume",
                    "execution_state": state.get("execution_state") if state else "unknown",
                    "business_state": state.get("business_state") if state else "not_available",
                    "write_state": state.get("write_state") if state else "unknown",
                    "outcome": "unresolved", "reason": "target_evidence_unavailable",
                    "evidence_verified": False}
            if state is None or category is None:
                blocked = True
            else:
                attempt = self.root / target.workflow_key / "cases" / target.item_key / f"attempt-{state['attempt']}"
                related = state["attempt"] == target.source_attempt
                if target.recovery and state["attempt"] == target.source_attempt + 1:
                    proof = recovery_receipt.projection(attempt, state)
                    related = proof.get("receipt_status") == "verified" and proof.get("from_attempt") == target.source_attempt
                if (not related or (not target.recovery
                                    and state.get("material_fingerprint") != target.material_fingerprint)):
                    item["reason"] = "production_target_action_changed"
                    blocked = True
                elif state["write_state"] in {"unknown", "submitting"}:
                    item["reason"] = "target_write_state_unresolved"
                    blocked = True
                elif state["write_state"] == "submitted":
                    if (category == "handled_not_pending"
                            and FlowEngine.confirmed_submission(self.root, state)):
                        item.update(outcome="already_completed" if target.initially_submitted else "submitted_confirmed",
                                    reason="handled_not_pending", evidence_verified=True)
                    else:
                        item["reason"] = "target_submission_unverified"
                        blocked = True
                elif category == "handled_not_pending":
                    no_write_state = (state["execution_state"], state["write_state"], state["business_state"]) in _NO_WRITE_STATES
                    if (no_write_state and (FlowEngine.resumable_review(self.root, state)
                            or FlowEngine.recoverable_no_write(self.root, attempt / "action.json"))):
                        item.update(outcome="confirmed_no_resubmit", reason="handled_not_pending", evidence_verified=True)
                    else:
                        item["reason"] = "target_no_write_unverified"
                        blocked = True
                elif category == "invisible":
                    item["reason"] = "target_not_visible"
                elif category == "pending_not_handled":
                    item["reason"] = ("review_retry_pending" if state.get("production_failure_round", 0)
                                      else "target_not_closed")
                else:
                    item["reason"] = "target_identity_conflict"
                    blocked = True
            items.append(item)
        completed = sum(item["outcome"] != "unresolved" for item in items)
        return {"schema_version": 1, "kind": "preaudit_target_outcome",
                "status": "blocked" if blocked else "complete" if completed == len(items) else "partial",
                "target_count": len(items), "completed": completed, "unresolved": len(items) - completed,
                "items": items}


def validate_outcome(value: object) -> dict:
    fields = {"schema_version", "kind", "status", "target_count", "completed", "unresolved", "items"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema_version"] != 1
            or type(value["schema_version"]) is not int or value["kind"] != "preaudit_target_outcome"
            or value["status"] not in {"complete", "partial", "blocked"}
            or any(type(value[name]) is not int or value[name] < 0
                   for name in ("target_count", "completed", "unresolved"))
            or value["target_count"] < 1 or not isinstance(value["items"], list)
            or len(value["items"]) != value["target_count"]):
        raise ValueError("production_target_outcome_invalid")
    item_fields = {"item_key", "workflow_key", "source_attempt", "attempt", "operation",
                   "execution_state", "business_state", "write_state", "outcome", "reason", "evidence_verified"}
    reasons = {"target_evidence_unavailable", "production_target_action_changed", "target_write_state_unresolved",
               "target_submission_unverified", "target_no_write_unverified", "handled_not_pending", "target_not_visible",
               "review_retry_pending", "target_not_closed", "target_identity_conflict"}
    keys = set()
    completed = 0
    for row in value["items"]:
        if (not isinstance(row, dict) or set(row) != item_fields
                or not isinstance(row["item_key"], str)
                or re.fullmatch(r"[0-9a-f]{64}", row["item_key"]) is None or row["item_key"] in keys
                or row["workflow_key"] not in WORKFLOW_KEYS
                or type(row["source_attempt"]) is not int or row["source_attempt"] < 1
                or (row["attempt"] is not None and (type(row["attempt"]) is not int or row["attempt"] < 1))
                or row["operation"] not in {"resume", "recover"}
                or row["execution_state"] not in {"unknown", "collecting", "review_required", "needs_admin", "closed"}
                or row["business_state"] not in {"not_available", "pass", "needs_human"}
                or row["write_state"] not in {"not_requested", "authorized", "submitting", "submitted", "unknown"}
                or row["outcome"] not in {"submitted_confirmed", "already_completed", "confirmed_no_resubmit", "unresolved"}
                or row["reason"] not in reasons or type(row["evidence_verified"]) is not bool
                or row["evidence_verified"] != (row["outcome"] != "unresolved")):
            raise ValueError("production_target_outcome_invalid")
        if row["outcome"] != "unresolved":
            actual = row["attempt"]
            expected = row["source_attempt"] + (row["operation"] == "recover")
            if (actual is None or row["reason"] != "handled_not_pending"
                    or (row["outcome"] == "confirmed_no_resubmit"
                        and (actual not in {row["source_attempt"], expected}
                             or (row["execution_state"], row["write_state"], row["business_state"]) not in _NO_WRITE_STATES))
                    or (row["outcome"] != "confirmed_no_resubmit"
                        and (row["execution_state"] != "closed" or row["write_state"] != "submitted"
                             or row["business_state"] not in {"pass", "needs_human"}
                             or actual != expected))
                    or (row["outcome"] == "already_completed" and row["operation"] != "resume")):
                raise ValueError("production_target_outcome_invalid")
        keys.add(row["item_key"])
        completed += row["outcome"] != "unresolved"
    if (value["completed"] != completed or value["unresolved"] != len(keys) - completed
            or (value["status"] == "complete" and value["unresolved"] != 0)
            or (value["status"] == "partial" and value["unresolved"] == 0)):
        raise ValueError("production_target_outcome_invalid")
    return {**value, "items": [dict(row) for row in value["items"]]}
