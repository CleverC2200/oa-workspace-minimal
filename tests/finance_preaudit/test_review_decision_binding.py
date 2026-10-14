import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_engine, review_decision


def _action(tmp_path: Path):
    engine = flow_engine.FlowEngine(root=tmp_path / "flows",
        discover_candidates=lambda _: [{"request_id": "case", "received_at": "2026-09-01 09:00:00",
                                        "workflow_key": "travel_expense"}],
        collect_material=lambda request, _: {
            "schema_version": 1, "kind": "preaudit_material", "request_id": request,
            "workflow_key": "travel_expense", "workflow_name": "差旅费报销流程",
            "declared_fields": [{"field_name": "period", "value": "2026-08"}], "resources": [], "prepared": {
                "images": [], "structured_contents": [], "manifest": [],
            }, "authority_facts": [], "warnings": [],
        })
    return engine.next_action("travel_expense")


def test_binding_fills_protocol_only_and_preserves_supplied_conflicts(tmp_path: Path):
    action = _action(tmp_path)
    body = {"overall": "needs_human", "items": [{"status": "uncertain"}]}
    result = review_decision.bind_frozen_fields(Path(action["action_file"]), body)
    assert result["action_token"] == action["action_token"]
    assert result["items"] == body["items"] and "evidence_refs" not in result["items"][0]
    wrong = review_decision.bind_frozen_fields(Path(action["action_file"]), {**body, "action_token": "wrong"})
    assert wrong["action_token"] == "wrong"
    assert "action_token" not in body


def test_binding_rejects_changed_frozen_inputs_and_reserved_output(tmp_path: Path):
    action = _action(tmp_path)
    path = Path(action["action_file"])
    draft = tmp_path / "draft.json"; draft.write_text("{}")
    with pytest.raises(ValueError, match="review_decision_output_unsafe"):
        review_decision.main(["--action-file", str(path), "--input", str(draft),
                              "--output", str(path.with_name("review-decision.json"))])
    path.with_name("material.json").write_text("{}")
    with pytest.raises(ValueError): review_decision.bind_frozen_fields(path, {"overall": "pass", "items": []})


def test_output_cannot_enter_attempt_through_parent_alias(tmp_path: Path):
    action = _action(tmp_path)
    path = Path(action["action_file"])
    draft = tmp_path / "draft.json"; draft.write_text("{}")
    alias = tmp_path / "alias"; alias.symlink_to(path.parent, target_is_directory=True)
    for output in (alias / "new-decision.json", tmp_path / "unused" / ".." / "alias" / "new-decision.json"):
        with pytest.raises(ValueError, match="review_decision_output_unsafe"):
            review_decision.main(["--action-file", str(path), "--input", str(draft), "--output", str(output)])
    assert not path.with_name("new-decision.json").exists()
