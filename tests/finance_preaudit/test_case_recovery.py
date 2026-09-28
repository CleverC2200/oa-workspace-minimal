from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import case_recovery, flow_engine, reconciliation


def _material(request_id: str) -> dict:
    return {"schema_version": 1, "kind": "preaudit_material", "request_id": request_id,
            "workflow_key": "travel_expense", "workflow_name": "差旅费报销流程",
            "declared_fields": [], "resources": [], "prepared": {
                "images": [], "structured_contents": [], "manifest": [],
            }, "authority_facts": [], "warnings": []}


def test_authorized_selection_is_once_per_identity_and_never_replays_unknown(tmp_path: Path):
    root = tmp_path / "flows"
    rows = [{"request_id": "pending-case", "received_at": "2026-09-01 09:00:00",
             "workflow_key": "travel_expense"}]
    engine = flow_engine.FlowEngine(
        root=root, discover_candidates=lambda _: rows, discover_handled=lambda: [],
        collect_material=lambda request_id, _: _material(request_id),
    )
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    engine._persist_attempt_needs_admin(path, "codex_decision_invalid")
    report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    before = path.read_bytes(), path.with_name("state.json").read_bytes()
    selected = case_recovery.select_once(root, report, frozenset({"travel_expense"}))
    assert selected == (path,)
    assert before == (path.read_bytes(), path.with_name("state.json").read_bytes())
    assert case_recovery.select_once(root, report, frozenset({"general_expense"})) == ()
    next_action = engine.recover_confirmed_no_write(path, confirmed=True)
    assert next_action["attempt"] == 2
    next_path = Path(next_action["action_file"])
    engine._persist_attempt_needs_admin(next_path, "codex_decision_invalid")
    next_report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    assert case_recovery.select_once(root, next_report, frozenset({"travel_expense"})) == ()
    row = next_report["pending_dispositions"]["items"][0]
    assert row["recovery_limit_reached"] is True and row["next_step"] == "manual_takeover"
    engine._persist_attempt_needs_admin(next_path, "writer_result_unknown")
    unknown_report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    with pytest.raises(ValueError, match="production_rolling_reconciliation_invalid"):
        case_recovery.select_once(root, unknown_report, frozenset({"travel_expense"}))


def test_selector_rechecks_changed_attempt_instead_of_trusting_old_report(tmp_path: Path):
    root = tmp_path / "flows"
    rows = [{"request_id": "pending-case", "received_at": "2026-09-01 09:00:00",
             "workflow_key": "travel_expense"}]
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda _: rows,
        discover_handled=lambda: [], collect_material=lambda request_id, _: _material(request_id))
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    engine._persist_attempt_needs_admin(path, "codex_decision_invalid")
    report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    path.with_name("writer-boundary.json").write_text("{}")
    assert case_recovery.select_once(root, report, frozenset({"travel_expense"})) == ()


def test_pending_age_uses_current_received_time_not_prior_attempt_creation(tmp_path: Path):
    now = datetime(2026, 9, 28, 7, tzinfo=timezone.utc)
    state = {"request_id": "same-case", "received_at": "2026-09-23 09:00:00",
             "created_at": "2026-09-23T01:00:00+00:00", "case_key": "old", "attempt": 1}
    result = case_recovery.pending_dispositions(tmp_path, [state], [{
        "request_id": "same-case", "received_at": "2026-09-28 14:00:00",
        "workflow_key": "travel_expense",
    }], [], now)
    row = result["items"][0]
    assert row["age_seconds"] == 3600 and row["execution_state"] == "not_claimed"
    assert row["attempt_created_at"] is None and row["overdue"] is False


def test_summary_preserves_all_safety_gates_and_removes_large_history():
    payload = {"schema_version": 1, "kind": "preaudit_status", "generated_at": "now",
        "live_check": {"status": "ok"}, "controller_handoff": {"ready_for_start": True},
        "case_metrics": ["large-history"], "reconciliation": {
            "continuation": {"status": "blocked", "blocking_reasons": {"identity_conflict": 1}},
            "manual_review": {"items": [{"category": "identity_conflict", "item_key": "case"}]},
            "findings": ["large-history"],
        }}
    result = case_recovery.summary(payload)
    assert "case_metrics" not in result and "findings" not in result["reconciliation"]
    assert result["reconciliation"]["continuation"] == payload["reconciliation"]["continuation"]
    assert len(result["reconciliation"]["manual_review"]["items"]) == 1


def test_takeover_records_responsibility_without_touching_frozen_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OA_FEISHU_ALERT_CHAT_ID", "configured-person")
    root = tmp_path / "flows"
    rows = [{"request_id": "case", "received_at": "2026-09-01 09:00:00", "workflow_key": "travel_expense"}]
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda _: rows,
        collect_material=lambda request_id, _: _material(request_id))
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    engine._persist_attempt_needs_admin(path, "codex_decision_invalid")
    before = path.read_bytes(), path.with_name("state.json").read_bytes()
    with pytest.raises(ValueError): case_recovery.takeover(path, confirmed=False)
    first = case_recovery.takeover(path, confirmed=True)
    assert case_recovery.takeover(path, confirmed=True) == first
    assert before == (path.read_bytes(), path.with_name("state.json").read_bytes())
    report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    assert report["pending_dispositions"]["items"][0]["takeover_status"] == "acknowledged"
    assert report["pending_dispositions"]["items"][0]["next_step"] == "manual_processing"
    assert len(first["owner_key"]) == 64 and "configured-person" not in json.dumps(first)
    monkeypatch.setenv("OA_FEISHU_ALERT_CHAT_ID", "different-person")
    changed = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
    assert changed["pending_dispositions"]["items"][0]["owner_key"] == first["owner_key"]
    assert changed["pending_dispositions"]["items"][0]["owner_configuration_changed"] is True


def test_unknown_write_gets_two_hour_escalation_and_real_recipient_binding(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OA_FEISHU_ALERT_CHAT_ID", "configured-person")
    state = {"request_id": "case", "received_at": "2026-09-28 12:00:00", "case_key": "case",
             "attempt": 1, "write_state": "unknown", "execution_state": "needs_admin"}
    now = datetime(2026, 9, 28, 7, tzinfo=timezone.utc)
    result = case_recovery.pending_dispositions(tmp_path, [state], [{
        "request_id": "case", "received_at": state["received_at"], "workflow_key": "travel_expense",
    }], [], now)
    row = result["items"][0]
    assert row["escalation_due_at"] == "2026-09-28T06:00:00+00:00"
    assert row["escalation_overdue"] is True and row["owner_status"] == "bound"


def test_verified_deferred_history_keeps_fresh_round_in_fifo(tmp_path: Path):
    old = {"request_id": "same", "received_at": "2026-09-23 09:00:00", "case_key": "old", "attempt": 1}
    result = case_recovery.pending_dispositions(tmp_path, [old], [{
        "request_id": "same", "received_at": "2026-09-28 14:00:00", "workflow_key": "travel_expense",
    }], [], datetime(2026, 9, 28, 7, tzinfo=timezone.utc), findings=[{
        "item_key": "old", "category": "identity_conflict", "recommendation": "deferred_historical_cleanup",
    }])
    row = result["items"][0]
    assert row["next_step"] == "continue_fifo" and row["owner"] == "rolling_controller"
