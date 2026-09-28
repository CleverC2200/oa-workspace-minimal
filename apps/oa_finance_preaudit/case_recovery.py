"""Bounded, explicitly authorized recovery and read-only pending dispositions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from apps.oa_approval import auth, writer_outcomes
from apps import json_artifact
from . import review_failure_evidence as evidence

OWNER = "configured_alert_recipient"
ALLOWED_REASONS = frozenset({
    "codex_review_failed", "codex_decision_invalid", "material_collection_failed",
    *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
})


def _owner_key() -> str | None:
    target = str(auth.env("OA_FEISHU_ALERT_CHAT_ID") or "").strip()
    return hashlib.sha256(("oa-preaudit-owner-v1\0" + target).encode()).hexdigest() if target else None


def takeover(action_file: Path, *, confirmed: bool) -> dict:
    """Acknowledge local responsibility only; no OA reads, writes or retries."""
    from . import item_failure, reconciliation
    if not confirmed:
        raise ValueError("takeover_confirmation_required")
    path = Path(action_file).absolute()
    state = evidence.read_receipt(path.with_name("state.json"))
    if (path.is_symlink() or state is None or len(path.parents) < 5
            or not item_failure._bound_state(path.parent, state)
            or state not in reconciliation.latest_states(path.parents[4])):
        raise ValueError("takeover_identity_invalid")
    receipt = {"schema_version": 1, "kind": "preaudit_takeover", "item_key": state["case_key"],
               "workflow_key": state["workflow_key"], "attempt": state["attempt"], "owner": OWNER,
               "owner_key": _owner_key(),
               "state_fingerprint": evidence.file_fingerprint(path.with_name("state.json"))}
    if receipt["owner_key"] is None:
        raise ValueError("takeover_owner_unconfigured")
    target = path.with_name("takeover-receipt.json")
    if target.exists() or target.is_symlink():
        existing = evidence.read_receipt(target)
        if existing is None or any(existing.get(k) != v for k, v in receipt.items()):
            raise ValueError("takeover_receipt_changed")
        return existing
    now = datetime.now(timezone.utc)
    receipt.update(acknowledged_at=now.isoformat(), resolution_due_at=(now + timedelta(hours=24)).isoformat())
    json_artifact.write_json_atomic(target, receipt, indent=2)
    return receipt


def _takeover_receipt(state: dict) -> dict | None:
    path = Path(str(state.get("state_file") or ""))
    if not path.is_absolute() or not path.name:
        return None
    receipt = evidence.read_receipt(path.with_name("takeover-receipt.json"))
    if (receipt is None or receipt.get("schema_version") != 1 or receipt.get("kind") != "preaudit_takeover"
            or receipt.get("workflow_key") != state.get("workflow_key")
            or receipt.get("item_key") != state.get("case_key")
            or receipt.get("attempt") != state.get("attempt") or receipt.get("owner") != OWNER
            or not isinstance(receipt.get("owner_key"), str) or len(receipt["owner_key"]) != 64
            or receipt.get("state_fingerprint") != evidence.file_fingerprint(path)):
        return None
    try:
        due = _time(receipt["resolution_due_at"])
        acknowledged = _time(receipt["acknowledged_at"])
        if due - acknowledged != timedelta(hours=24) or acknowledged > datetime.now(timezone.utc): return None
    except (ValueError, TypeError, KeyError):
        return None
    return receipt


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if parsed.tzinfo is None else parsed


def select_once(root: Path, report: dict, allowed_workflows: frozenset[str]) -> tuple[Path, ...]:
    """Select only verified no-write items; the existing recovery port rechecks live state."""
    from . import reconciliation
    from .flow_engine import FlowEngine

    continuation = report.get("continuation", {})
    quarantine = report.get("quarantine", {})
    isolated = continuation.get("isolated_pending")
    if (report.get("read_only") is not True or report.get("kind") != "preaudit_reconciliation"
            or continuation.get("status") != "ready" or continuation.get("blocking_reasons") != {}
            or type(isolated) is not int or isolated < 0
            or isolated != quarantine.get("pending_quarantined")
            or continuation.get("scope") != ("item" if isolated else "none")):
        raise ValueError("production_rolling_reconciliation_invalid")
    states = {state["case_key"]: state for state in reconciliation.latest_states(root)}
    selected = []
    for item in report.get("manual_review", {}).get("items", []):
        if (item.get("workflow_key") not in allowed_workflows
                or item.get("category") != "pending_not_handled"
                or item.get("recommendation") != "confirmed_no_write_recovery_candidate"
                or item.get("write_state") != "not_requested" or item.get("scope") != "item"
                or item.get("receipt_status") != "verified"
                or item.get("failure_code") not in ALLOWED_REASONS):
            continue
        state = states.get(item["item_key"], {})
        if (state.get("workflow_key") != item["workflow_key"]
                or state.get("attempt") != item["attempt"] or state.get("attempt") != 1
                or "recovered_from_attempt" in state):
            continue
        action = Path(str(state.get("action_file") or ""))
        if FlowEngine.recoverable_no_write(root, action):
            selected.append((str(state["received_at"]), str(state["request_id"]), action))
    return tuple(row[2] for row in sorted(selected))


def pending_dispositions(root: Path, states: list[dict], pending: list[dict],
                         manual: list[dict], now: datetime, findings: list[dict] | None = None) -> dict:
    """Every current pending identity gets a bounded, non-authorizing next step."""
    by_identity = {(s["request_id"], s["received_at"]): s for s in states}
    by_case = {item["item_key"]: item for item in manual}
    by_finding = {item["item_key"]: item for item in (findings or [])}
    owner_key = _owner_key()
    rows = []
    for row in pending:
        request = str(row.get("request_id") or row.get("requestId") or "")
        received = str(row.get("received_at") or "")
        if not request or not received:
            continue
        from .reconciliation import _item_key
        state = by_identity.get((request, received), {})
        item = by_case.get(state.get("case_key"), {})
        try:
            started = _time(received).astimezone(timezone.utc)
        except (ValueError, TypeError):
            started = None
        age = max(0, int((now - started).total_seconds())) if started else "unknown"
        attempted = "recovered_from_attempt" in state or state.get("attempt", 1) > 1
        next_step, owner = "continue_fifo", "rolling_controller"
        related = [by_finding.get(s["case_key"], {}) for s in states if s["request_id"] == request]
        security_fault = any(
            x.get("category") in {"identity_conflict", "pending_handled_conflict"}
            and x.get("recommendation") != "deferred_historical_cleanup" for x in related
        )
        if state.get("write_state") in {"unknown", "submitting"}:
            next_step, owner = "verify_original_write", OWNER
            security_fault = True
        elif state.get("execution_state") == "needs_admin":
            next_step, owner = "investigate_failure", OWNER
            if item.get("recommendation") == "confirmed_no_write_recovery_candidate":
                next_step = "manual_takeover" if attempted else "authorized_recovery_once"
        if state.get("reason") == "write_authorization_invalid":
            security_fault = True
        if security_fault and state.get("write_state") not in {"unknown", "submitting"}:
            next_step, owner = "verify_identity_or_authorization", OWNER
        if started is None:
            next_step, owner = "verify_received_at", OWNER
        due = started + timedelta(hours=24 if owner == OWNER else 2) if started else None
        escalation = started + timedelta(hours=2) if started else None
        acknowledged = _takeover_receipt(state) if state else None
        if acknowledged:
            due = _time(acknowledged["resolution_due_at"])
            if next_step in {"manual_takeover", "investigate_failure", "authorized_recovery_once"}:
                next_step = "manual_processing"
        rows.append({
            "item_key": _item_key(request, received), "workflow_key": row.get("workflow_key"),
            "received_at": received, "age_seconds": age,
            "attempt_created_at": state.get("created_at"),
            "first_failure_at": state.get("failed_at"),
            "execution_state": state.get("execution_state", "not_claimed"),
            "write_state": state.get("write_state", "not_requested"),
            "failure_code": item.get("failure_code"), "owner": owner,
            "owner_key": acknowledged["owner_key"] if acknowledged else (owner_key if owner == OWNER else None),
            "owner_status": "bound" if acknowledged or owner != OWNER or owner_key else "unconfigured",
            "owner_configuration_changed": bool(acknowledged and acknowledged["owner_key"] != owner_key),
            "next_step": next_step, "recovery_limit_reached": attempted,
            "takeover_status": "acknowledged" if acknowledged else "not_acknowledged",
            "takeover_due_at": (started + timedelta(hours=4)).isoformat() if owner == OWNER and started else None,
            "resolution_due_at": due.isoformat() if due else None, "overdue": now > due if due else False,
            "escalation_due_at": escalation.isoformat() if escalation else None,
            "escalation_overdue": now > escalation if escalation else False,
        })
    return {"read_only": True, "items": sorted(rows, key=lambda x: (x["received_at"], x["item_key"])),
            "overdue": sum(row["overdue"] for row in rows), "total": len(rows)}


def summary(payload: dict) -> dict:
    """Keep all live safety gates and actionable dispositions, omit historical metrics."""
    result = {key: payload[key] for key in (
        "schema_version", "kind", "generated_at", "live_check", "controller_handoff", "notification",
    ) if key in payload}
    if "reconciliation" in payload:
        report = payload["reconciliation"]
        result["reconciliation"] = {key: value for key, value in report.items()
                                    if key not in {"findings", "recoveries", "manual_review"}}
        manual = report.get("manual_review", {})
        items = [item for item in manual.get("items", [])
                 if item.get("category") in {"pending_not_handled", "identity_conflict", "pending_handled_conflict"}
                 or item.get("write_state") == "unknown" or item.get("receipt_status") != "verified"]
        result["reconciliation"]["manual_review"] = {**manual, "items": items, "visible": len(items)}
        if len(items) > 200:
            result["reconciliation"]["manual_review"].update(items=items[:200], visible=200, display_truncated=True)
        pending = report.get("pending_dispositions", {})
        if len(pending.get("items", [])) > 200:
            result["reconciliation"]["pending_dispositions"] = {
                **pending, "items": pending["items"][:200], "visible": 200, "display_truncated": True,
            }
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-file", type=Path, required=True)
    parser.add_argument("--confirm-takeover", choices=("YES",), required=True)
    args = parser.parse_args(argv)
    result = takeover(args.action_file, confirmed=args.confirm_takeover == "YES")
    print(json.dumps(result, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
