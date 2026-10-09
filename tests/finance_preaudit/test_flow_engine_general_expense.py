from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine


WORKFLOW_KEY = "general_expense"
ITEM_NAMES = (
    "附件完整性/支付证据",
    "发票关联",
    "预算",
    "金额孰低核销",
    "支付公司一致",
)


def _material(request_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": WORKFLOW_KEY,
        "workflow_name": "通用费用报销申请流程",
        "declared_fields": [{
            "path": "detail[0].row[0].field[17]", "name": "关联发票", "code": "glfp",
            "read_status": "present", "value": "12345678901234567890",
        }],
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def test_general_expense_uses_five_fixed_items_and_renders_them_once(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "general-expense-request",
                "received_at": "2026-08-28T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _material(request_id),
    )

    action = engine.next_action(WORKFLOW_KEY)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )

    assert packet["rule_version"] == "general_expense:1"
    assert tuple(item["name"] for item in packet["items"]) == ITEM_NAMES

    conclusions = (
        "附件及支付证据完整。",
        "发票均已关联。",
        "当前阶段按既定策略暂不校验部门年度预算。",
        "整单核销金额为100.00元。",
        "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。",
    )
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": packet["rule_version"],
        "overall": "pass",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "pass",
                "conclusion": conclusion,
                "evidence_refs": ["material:declared_fields"],
                "missing_evidence": [],
            }
            for item, conclusion in zip(packet["items"], conclusions, strict=True)
        ],
    }
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")

    assert closed["business_state"] == "pass"
    assert remark.startswith("AI审核结论：可提交 ✅")
    remark_lines = remark.splitlines()
    assert [line.split("：", 1)[0] for line in remark_lines[1:]] == [
        f"✅ {index}、{name}" for index, name in enumerate(ITEM_NAMES, start=1)
    ]
    assert "当前阶段按既定策略暂不校验部门年度预算" in remark
    assert "应选择示例有限公司支付" in remark


def test_general_expense_deferred_payment_can_close_as_needs_human(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "deferred-payment-request",
                "received_at": "2026-08-28T09:01:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _material(request_id),
    )
    action = engine.next_action(WORKFLOW_KEY)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision = {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": packet["rule_version"],
        "overall": "needs_human",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "fail" if index in {0, 3} else "pass",
                    "conclusion": (
                        "实付0元，均为收到货后自动付款100.00元。"
                        if item["item_key"] == "attachment_payment_evidence"
                        else "当前阶段按既定策略暂不校验部门年度预算。"
                        if item["item_key"] == "budget"
                        else (
                            "报销公司、支付公司、购买方三者一致，"
                            "应选择示例有限公司支付。"
                        )
                        if item["item_key"] == "payment_company_consistency"
                        else "当前证据符合该审核项。"
                    ),
                "evidence_refs": ["material:prepared.images.payment.1"],
                "missing_evidence": [],
            }
            for index, item in enumerate(packet["items"])
        ],
    }
    decision_file = tmp_path / "deferred-decision.json"
    json_artifact.write_json_atomic(decision_file, decision)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")

    assert closed["business_state"] == "needs_human"
    assert "实付0元，均为收到货后自动付款100.00元" in remark


@pytest.mark.parametrize(
    "item_key,conclusion",
    (
        ("budget", "暂不校验预算。"),
        (
            "payment_company_consistency",
            "报销公司、支付公司与购买方一致。",
        ),
        (
            "attachment_payment_evidence",
            "三笔先用后付页面显示实付0元，未形成已支付证据。",
        ),
    ),
)
def test_general_expense_rejects_confirmed_wording_drift(
    item_key: str,
    conclusion: str,
) -> None:
    items = [
        {
            "item_key": "attachment_payment_evidence",
            "status": "pass",
            "conclusion": "附件完整。",
        },
        {
            "item_key": "budget",
            "status": "pass",
            "conclusion": "当前阶段按既定策略暂不校验部门年度预算。",
        },
        {
            "item_key": "payment_company_consistency",
            "status": "pass",
            "conclusion": (
                "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。"
            ),
        },
    ]
    target = next(item for item in items if item["item_key"] == item_key)
    target["conclusion"] = conclusion
    if item_key == "attachment_payment_evidence":
        target["status"] = "fail"

    with pytest.raises(ValueError, match="review_decision_invalid"):
        flow_engine._validate_general_expense_conclusions(items)
