import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import packaging_review
from tests.finance_preaudit.test_flow_engine_packaging import (
    ITEM_NAMES, WORKFLOW_KEY, _decision, _engine, _passing_items,
)


PAGE = "material:prepared.images.page-1.page-1"


def supplementary(status="fail", conclusion="版本 A 左侧储存说明标题用词异常"):
    return {"page_ref": PAGE, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize("excel", [False, True])
@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_supplementary_anomaly_keeps_four_passing_items_and_single_tail_line(tmp_path, excel, status):
    calls = []
    engine = _engine(tmp_path / "flows", excel=excel, calls=calls)
    action = engine.next_action(WORKFLOW_KEY, mode="controlled_write")
    decision = _decision(action, _passing_items(), excel=excel)
    decision["overall"] = "needs_human"
    decision["supplementary_checks"] = [
        supplementary(status),
        supplementary("uncertain", "版本 A 右侧警示语末字需人工核对"),
    ]
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "submitted" and len(calls) == 1
    lines = Path(closed["remark_file"]).read_text().splitlines()
    assert lines[0] == "AI审核结论：需人工判断 ⚠️"
    assert all(lines[index].startswith(f"✅ {index}、{name}：")
               for index, name in enumerate(ITEM_NAMES, 1))
    assert len(lines) == 7 and lines[-1].startswith("Excel 对照：")
    assert lines[-2].startswith("补充校对：")
    assert "左侧储存说明标题用词异常" in lines[-2]
    assert "右侧警示语末字需人工核对" in lines[-2]
    assert lines[-2].count("补充校对：") == 1


def test_draft_schema_carries_business_supplement_without_rewriting_it(tmp_path):
    engine = _engine(tmp_path / "flows")
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, _passing_items())
    decision.update(overall="needs_human", supplementary_checks=[supplementary()])
    body = {key: decision[key] for key in (
        "overall", "items", "excel_comparison", "visual_review", "supplementary_checks",
    )}
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    assert packaging_review.parse_draft(json.dumps(body), packaging_review.draft_schema(packet)) == body


@pytest.mark.parametrize("checks", [
    None, {}, [None],
    [{"page_ref": "material:unrelated", "status": "fail", "conclusion": "未知来源"}],
    [supplementary("pass")], [supplementary(conclusion=" ")],
    [supplementary(conclusion="第一处\n第二处")],
    [supplementary(), supplementary()],
    [{**supplementary(), "extra": "untrusted"}],
])
def test_invalid_supplement_is_rejected_before_writer(tmp_path, checks):
    calls = []
    engine = _engine(tmp_path / "flows", calls=calls)
    action = engine.next_action(WORKFLOW_KEY, mode="controlled_write")
    decision = _decision(action, _passing_items())
    decision.update(overall="needs_human", supplementary_checks=checks)
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), target)
    assert calls == []
    assert not Path(action["action_file"]).with_name("writer-boundary.json").exists()


@pytest.mark.parametrize("include_empty", [False, True])
def test_legacy_and_empty_supplements_preserve_old_opinion(tmp_path, include_empty):
    engine = _engine(tmp_path / "flows")
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, _passing_items())
    if include_empty:
        decision["supplementary_checks"] = []
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    closed = engine.complete_review(Path(action["action_file"]), target)
    assert closed["business_state"] == "pass"
    assert "补充校对" not in Path(closed["remark_file"]).read_text()


@pytest.mark.parametrize("all_non_packaging", [False, True])
def test_non_packaging_pages_cannot_supply_supplementary_anomalies(tmp_path, all_non_packaging):
    engine = _engine(tmp_path / "flows", page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual = {"outcome": "all_non_packaging" if all_non_packaging else "reviewed", "pages": [
        {"page_ref": PAGE, "classification": "non_packaging" if all_non_packaging else "packaging_layout",
         "package_version_id": None if all_non_packaging else "version-1",
         "item_keys": [] if all_non_packaging else ["ingredients", "allergens", "nutrition_facts", "address_information"]},
        {"page_ref": "material:prepared.images.page-1.page-2", "classification": "non_packaging",
         "package_version_id": None, "item_keys": []},
    ]}
    items = _passing_items()
    if all_non_packaging:
        for item in items:
            item["evidence_refs"] = []
    decision = _decision(action, items, visual_review=visual)
    decision["supplementary_checks"] = [{**supplementary(), "page_ref": "material:prepared.images.page-1.page-2"}]
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), target)


@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_uncertain_page_can_only_have_uncertain_supplement(tmp_path, status):
    engine = _engine(tmp_path / "flows")
    action = engine.next_action(WORKFLOW_KEY)
    items = _passing_items()
    for item in items:
        item.update(status="uncertain", evidence_refs=[], missing_evidence=[PAGE, "页面分类关系尚不能确认"])
    visual = {"outcome": "reviewed", "pages": [
        {"page_ref": PAGE, "classification": "uncertain", "package_version_id": None, "item_keys": []},
    ]}
    decision = _decision(action, items, visual_review=visual)
    decision["supplementary_checks"] = [supplementary(status)]
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    if status == "fail":
        with pytest.raises(ValueError, match="review_decision_invalid"):
            engine.complete_review(Path(action["action_file"]), target)
    else:
        assert engine.complete_review(Path(action["action_file"]), target)["business_state"] == "needs_human"


def test_supplementary_anomaly_cannot_claim_overall_pass(tmp_path):
    engine = _engine(tmp_path / "flows")
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action, _passing_items())
    decision["supplementary_checks"] = [supplementary()]
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), target)


def test_financial_workflow_rejects_packaging_supplementary_field(tmp_path):
    from apps.oa_finance_preaudit.flow_engine import FlowEngine
    from tests.finance_preaudit.test_flow_engine import _passing_decision, _travel_material
    engine = FlowEngine(root=tmp_path / "flows", discover_candidates=lambda workflow: [{
        "request_id": "test-travel", "received_at": "2026-08-22T09:00:00+08:00", "workflow_key": workflow,
    }], collect_material=lambda request_id, _root: _travel_material(request_id))
    action = engine.next_action("travel_expense")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = _passing_decision(action, packet)
    decision["supplementary_checks"] = []
    target = tmp_path / "decision.json"
    json_artifact.write_json_atomic(target, decision)
    with pytest.raises(ValueError, match="review_decision_invalid") as error:
        engine.complete_review(Path(action["action_file"]), target)
    assert error.value.validation_code == "decision_fields"
