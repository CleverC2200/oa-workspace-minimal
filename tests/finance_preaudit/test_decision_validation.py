
# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, codex_review_worker, decision_validation, flow_engine


def _action(tmp_path: Path) -> Path:
    path = tmp_path / "action.json"
    payload = {"schema_version": 1, "kind": "preaudit_action", "action_token": "private-token", "workflow_key": "small_marketing_fee"}
    json_artifact.write_json_atomic(path, payload)
    json_artifact.write_json_atomic(path.with_name("state.json"), payload)
    return path


def test_rejection_retains_only_sanitized_bound_diagnostics(tmp_path: Path, monkeypatch) -> None:
    path = _action(tmp_path)

    def reject(*_args):
        raise decision_validation.InvalidDecision("item_values")

    monkeypatch.setattr(cli, "_live_flow_engine", lambda **_: SimpleNamespace(complete_review=reject))
    monkeypatch.setattr(codex_review_worker.runtime_control, "stop_requested", lambda: False)
    for value in ("private-one", "private-two", "private-two"):
        assert codex_review_worker._complete_decision(path, {"secret": value}) == "codex_decision_invalid"
    files = list(tmp_path.glob("review-rejection-*.json"))
    assert len(files) == 2
    for file in files:
        text = file.read_text()
        assert "private" not in text
        receipt = json.loads(text)
        assert receipt["validation_code"] == "item_values"
        assert set(receipt) == {"schema_version", "kind", "action_token_sha256", "decision_sha256", "validation_code"}
    assert not (tmp_path / "review-decision.json").exists()


@pytest.mark.parametrize("damage", ["symlink", "content"])
def test_rejection_cannot_overwrite_unsafe_diagnostic(tmp_path: Path, damage: str) -> None:
    path = _action(tmp_path)
    error = decision_validation.InvalidDecision("item_values")
    decision_validation.record_rejection(path, {}, error)
    receipt = next(tmp_path.glob("review-rejection-*.json"))
    if damage == "symlink":
        receipt.unlink()
        receipt.symlink_to(tmp_path / "missing")
    else:
        json_artifact.write_json_atomic(receipt, {"private": "value"})
    with pytest.raises(ValueError, match="decision_diagnostic_invalid"):
        decision_validation.record_rejection(path, {}, error)


def test_diagnostic_failure_stops_completion(tmp_path: Path, monkeypatch) -> None:
    path = _action(tmp_path)

    def reject(*_args):
        raise ValueError("review_decision_invalid")

    def fail(*_args):
        raise OSError("private diagnostic failure")

    monkeypatch.setattr(cli, "_live_flow_engine", lambda **_: SimpleNamespace(complete_review=reject))
    monkeypatch.setattr(codex_review_worker.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(decision_validation, "record_rejection", fail)
    assert codex_review_worker._complete_decision(path, {}) == "codex_decision_diagnostic_failed"


def test_unknown_exception_detail_is_not_persisted(tmp_path: Path) -> None:
    path = _action(tmp_path)
    error = ValueError("private exception")
    error.validation_code = "private-code"
    decision_validation.record_rejection(path, {}, error)
    receipt = next(tmp_path.glob("review-rejection-*.json")).read_text()
    assert "private" not in receipt
    assert json.loads(receipt)["validation_code"] == "unspecified"


def test_parent_does_not_retry_diagnostic_failure(tmp_path: Path, monkeypatch) -> None:
    path = _action(tmp_path)
    stopped = []
    monkeypatch.setattr(codex_review_worker.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(codex_review_worker, "_write_agent_timing", lambda *_a, **_kw: True)
    monkeypatch.setattr(codex_review_worker, "_read_completed", lambda _: None)
    result = codex_review_worker.complete_review_result(
        path,
        {"status": "reviewed", "decision": {}, "process_attempts": 1,
         "model": "gpt-6.1-sol", "reasoning_effort": "medium",
         "model_provider": "openai", "requested_service_tier": "default",
         "agent_wall_seconds": 1, "token_usage": {"status": "unknown"},
         "codex_events": {"status": "recorded", "attempts": 1,
                          "recorded_attempts": 1, "unknown_attempts": 0,
                          "counts": {"turn.completed": 1}}},
        completer=lambda *_: "codex_decision_diagnostic_failed",
        request_stop=lambda: stopped.append(True),
        defer_invalid_stop=True,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_decision_diagnostic_failed"
    assert result["write_state"] == "not_requested"
    assert stopped == [True]


@pytest.mark.parametrize("mutation,code", [
    ("token", "decision_action_token"), ("count", "item_count"),
    ("top_fields", "decision_fields"),
    ("missing_field", "decision_fields"), ("multiple", "decision_fields"),
    ("version", "decision_schema_version"), ("kind", "decision_kind"),
    ("workflow", "decision_workflow"), ("rule", "decision_rule_version"),
    ("overall_invalid", "decision_overall"), ("items_type", "decision_items_type"),
    ("fields", "item_fields"), ("values", "item_values"),
    ("overall", "overall_mismatch"),
])
def test_real_completion_validator_keeps_rejections_classified(
    tmp_path: Path, mutation: str, code: str,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path / "flows",
        discover_candidates=lambda workflow: [{
            "request_id": "test-request", "received_at": "2026-09-05T10:00:00+08:00",
            "workflow_key": workflow,
        }],
        collect_material=lambda request_id, _root: {
            "schema_version": 1, "kind": "preaudit_material",
            "request_id": request_id, "workflow_key": "small_marketing_fee",
            "workflow_name": "小额市场费报销流程", "declared_fields": [{"field_name": "period", "value": "2026-08"}],
            "resources": [], "prepared": {"images": [], "structured_contents": [], "manifest": []},
            "authority_facts": [], "warnings": [],
        },
    )
    action = engine.next_action("small_marketing_fee")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = {
        "schema_version": 1, "kind": "preaudit_review_decision",
        "action_token": action["action_token"], "workflow_key": "small_marketing_fee",
        "rule_version": packet["rule_version"], "overall": "needs_human",
        "items": [{"item_key": item["item_key"], "status": "uncertain",
                   "conclusion": "待确认", "evidence_refs": [], "missing_evidence": ["缺少证据"]}
                  for item in packet["items"]],
    }
    if mutation == "token":
        decision["action_token"] = "wrong-token"
    elif mutation == "top_fields":
        decision["private-field"] = "private-value"
    elif mutation == "missing_field":
        del decision["rule_version"]
    elif mutation == "multiple":
        decision["private-field"] = "private-value"
        decision["action_token"] = "wrong-token"
        decision["items"] = None
    elif mutation == "version":
        decision["schema_version"] = 99
    elif mutation == "kind":
        decision["kind"] = "private-kind"
    elif mutation == "workflow":
        decision["workflow_key"] = "private-workflow"
    elif mutation == "rule":
        decision["rule_version"] = "private-rule"
    elif mutation == "overall_invalid":
        decision["overall"] = "private-overall"
    elif mutation == "items_type":
        decision["items"] = {"private-item": {}}
    elif mutation == "count":
        decision["items"].pop()
    elif mutation == "fields":
        decision["items"][0]["unexpected"] = "private"
    elif mutation == "values":
        decision["items"][0]["missing_evidence"] = []
    else:
        decision["overall"] = "pass"
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision)
    with pytest.raises(decision_validation.InvalidDecision) as rejected:
        engine.complete_review(Path(action["action_file"]), decision_file)
    assert str(rejected.value) == "review_decision_invalid"
    assert rejected.value.validation_code == code
    assert not Path(action["action_file"]).with_name("writer-boundary.json").exists()
    decision_validation.record_rejection(Path(action["action_file"]), decision, rejected.value)
    receipt_path = next(Path(action["action_file"]).parent.glob("review-rejection-*.json"))
    receipt_text = receipt_path.read_text()
    assert json.loads(receipt_text)["validation_code"] == code
    assert "private" not in receipt_text
    assert "wrong-token" not in receipt_text
    assert action["action_token"] not in receipt_text
