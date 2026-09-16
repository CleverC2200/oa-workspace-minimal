from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_approval import flow
from apps.oa_finance_preaudit import (
    codex_review_worker,
    flow_engine,
    manual_handoff,
    observability,
    production_pending,
    reconciliation,
    runtime_maintenance,
)


WORKFLOW_KEY = "tpm_small_marketing_fee"
RULE_VERSION = "tpm_small_marketing_fee:1"
ITEM_KEYS = (
    "expense_subject",
    "attachment_completeness",
    "linked_invoice_receipt",
    "payment_evidence",
    "reimbursable_minimum",
    "entity_consistency",
)


def _engine(tmp_path: Path) -> flow_engine.FlowEngine:
    return flow_engine.FlowEngine(
        root=tmp_path / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "tpm-marketing-request",
                "received_at": "2026-09-16T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": WORKFLOW_KEY,
            "workflow_name": "TPM小额市场费用报销申请流程",
            "declared_fields": {},
            "resources": [],
            "prepared": {
                "images": [],
                "structured_contents": [],
                "manifest": [],
            },
            "authority_facts": [],
            "warnings": [],
        },
    )


def _decision(action: dict, *, conclusion: str) -> dict:
    items = []
    for item_key in ITEM_KEYS:
        status = "uncertain" if item_key == "attachment_completeness" else "pass"
        items.append(
            {
                "item_key": item_key,
                "status": status,
                "conclusion": conclusion if status == "uncertain" else "已核对",
                "evidence_refs": (
                    []
                    if status == "uncertain"
                    else ["material:declared_fields.expense_subject"]
                ),
                "missing_evidence": (
                    ["缺少具体附件"] if status == "uncertain" else []
                ),
            }
        )
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": RULE_VERSION,
        "overall": "needs_human",
        "items": items,
    }


def test_tpm_and_ordinary_marketing_use_distinct_exact_identities() -> None:
    configs = flow.load_all_configs()
    rows = [
        {
            "request_id": "tpm-request",
            "workflow_id": "195039",
            "workflow_name": "TPM小额市场费用报销申请流程",
            "received_at": "2026-09-16T09:00:00+08:00",
        },
        {
            "request_id": "ordinary-request",
            "workflow_id": "125026",
            "workflow_name": "小额市场费用报销申请流程",
            "received_at": "2026-09-16T09:01:00+08:00",
        },
    ]

    identified = production_pending.identify_pending_rows(rows, configs)

    assert [item["flow_key"] for item in identified] == [
        WORKFLOW_KEY,
        "small_marketing_fee",
    ]


def test_tpm_is_registered_across_every_runtime_boundary() -> None:
    assert WORKFLOW_KEY in flow_engine.WORKFLOW_KEYS
    assert WORKFLOW_KEY in codex_review_worker._FINANCE_RULES
    assert WORKFLOW_KEY in manual_handoff.WORKFLOWS
    assert WORKFLOW_KEY in observability._WORKFLOW_KEYS
    assert WORKFLOW_KEY in reconciliation._WORKFLOW_KEYS
    assert WORKFLOW_KEY in runtime_maintenance._WORKFLOW_KEYS


def test_tpm_flow_freezes_an_independent_six_item_contract(tmp_path: Path) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )

    assert packet["workflow_key"] == WORKFLOW_KEY
    assert packet["rule_version"] == RULE_VERSION
    assert tuple(item["item_key"] for item in packet["items"]) == ITEM_KEYS
    assert flow_engine._FLOW_SPECS["small_marketing_fee"]["rule_version"] == (
        "small_marketing_fee:2"
    )


def test_tpm_worker_uses_only_the_tpm_rule_pack(tmp_path: Path) -> None:
    action = _engine(tmp_path).next_action(WORKFLOW_KEY)
    action_file = Path(action["action_file"])

    schema = codex_review_worker._decision_schema(action_file)
    prompt = codex_review_worker._prompt(action_file)

    assert schema["properties"]["workflow_key"]["const"] == WORKFLOW_KEY
    assert schema["properties"]["rule_version"]["const"] == RULE_VERSION
    assert "TPM小额市场费生产预审" in prompt
    assert "tpm-small-marketing-fee.md" in prompt
    assert "10 类费用科目" in prompt
    assert "AI不得退回" in prompt
    assert "`references/flows/small-marketing-fee.md`" not in prompt


@pytest.mark.parametrize(
    "vague",
    (
        "异常",
        "缺少",
        "异常缺少",
        "附件不完整，缺少",
        "异常发票",
        "发票冲突",
        "异常，请财务核对",
        "无法识别",
        "无法识别发票",
        "无法识别，请人工判断",
        "需补充",
        "需补充相关材料",
        "缺少资料",
        "需补充证明",
        "无法识别票据",
        "凭证异常",
        "信息不清",
        "附件不完整",
        "附件不完整，请补充附件",
    ),
)
def test_tpm_rejects_vague_issue_conclusions(tmp_path: Path, vague: str) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(_decision(action, conclusion=vague), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


@pytest.mark.parametrize(
    "specific",
    (
        "缺少执行后的现场定位照片 1 张",
        "需补充关联发票",
        "无法识别发票销售方",
        "发票金额与支付金额相差 100 元",
        "费用科目原值“渠道促销费”不在 10 类规则表",
    ),
)
def test_tpm_accepts_a_specific_actionable_issue(
    tmp_path: Path,
    specific: str,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            _decision(action, conclusion=specific),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"
