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
        "不能建立",
        "不能建立对应关系",
        "无法建立金额",
        "未能建立对应关系，请财务确认",
        "待财务确认",
        "对应关系待确认",
        "对应关系不明",
        "对应关系未确认",
        "对应关系不可推定",
        "事项未确认",
        "事项1未确认",
        "事项不符合要求",
        "金额不满足规则",
        "金额1未确认",
        "对应关系1未确认",
        "原值未确认",
        "“异常”未确认",
        "“未知”无法判断",
        "制作清单已提供，但缺少资料",
        "制作清单已提供且缺少资料",
        "关联发票正常，需补充证明",
        "关联发票正常并需补充证明",
        "支付截图已齐全，仍缺少相关材料",
        "发票金额已确认且事项不明",
        "制作清单缺失，但其他资料异常",
        "制作清单缺失，需补充证明",
        "制作清单缺失，事项无法判断",
        "制作清单缺失，无法确认对应关系",
        "制作清单缺失，不能确定金额",
        "制作清单缺失，已核对其他附件，无法判断",
        "发票金额缺失，其他材料已齐全，对应关系不能建立",
        "印章和印章冲突",
        "制作清单与制作清单不一致",
        "定位照片与现场定位照片冲突",
        "关联发票与纸质发票不一致",
        "制作清单与广告公司盖章版制作清单不一致",
        "关联发票购买方与购买方冲突",
        "关联发票销售方与销售方不一致",
        "制作清单印章与印章冲突",
        "申请签报及附件显示定位照片与现场定位照片冲突",
        "申请签报及附件显示发票金额与发票金额冲突",
        "发票金额记录显示定位照片与现场定位照片冲突",
        "购买方资料中的关联发票与纸质发票不一致",
        "费用科目说明制作清单与广告公司盖章版制作清单不一致",
        "购买方与关联发票购买方冲突",
        "销售方与关联发票销售方不一致",
        "印章与制作清单印章冲突",
        "定位照片未记录",
        "费用科目未确认",
        "关联发票无法判断",
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
        "支付截图仅能证明一笔支付，尚不能建立其与关联发票的直接对应关系",
        "关联发票与支付事实的对应关系尚不能建立，孰低核销金额待财务确认",
        "申请签报与费用科目不一致",
        "支付日期与关联发票不一致",
        "支付对应关系与收款关系冲突",
        "签到表与制作清单不一致",
        "发票金额、支付金额不一致",
        "发票金额同支付金额不一致",
        "发票金额跟支付金额不一致",
        "发票金额及支付金额不一致",
        "合同显示发票金额与支付金额不一致",
        "同一发票金额与申报金额不一致",
        "本次涉及发票金额与支付金额不一致",
        "需跟踪发票金额与支付金额不一致",
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


@pytest.mark.parametrize(
    "explicit_missing",
    (
        "申请签报缺少",
        "明细归属缺失",
        "收款关系缺失",
        "发票金额缺失",
        "制作清单印章缺失",
        "定位信息缺失",
        "发票金额仍未确认",
        "付款状态仍然不明",
        "发票金额却为空",
        "缺失字段：发票金额",
        "需补充：申请签报",
        "缺少材料：广告公司盖章版制作清单",
        "缺失的字段：发票金额",
        "缺少的材料：申请签报",
        "需补充材料：申请签报",
        "支付截图未见重复、发票金额缺失",
    ),
)
def test_tpm_accepts_an_explicitly_missing_material_or_field(
    tmp_path: Path,
    explicit_missing: str,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            _decision(action, conclusion=explicit_missing),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"


@pytest.mark.parametrize(
    "natural_specific",
    (
        "未取得当前费用明细的费用科目原值，无法逐字匹配十类费用科目。",
        "定位照片所在资源未记录与该费用明细的归属关系，不能据照片数量确认现场执行附件已满足要求。",
        "实际支付状态、支付去重及收款方与发票销售方对应关系未确认，不能按孰低原则确定核销金额。",
        "关联发票为电子发票，非纸质发票。",
        "第2至4条关联发票字段未解析，第4条另缺广告公司盖章版制作清单。",
        "关联发票金额未取得，支付事实与关联发票的去重对应关系未建立。",
        "未取得费用明细及附件清单，无法按十类费用科目的必需材料逐项核对。",
        "关联发票金额和转账金额合计一致，但支付收款关系不明，不能按四条明细推定分配。",
        "第4条无实际支付金额，第5、6条关联发票和附件字段未解析，申报总额无法完成核销。",
        "关联发票引用尚未解析，且支付截图准备页不可读取，附件完整性不能确认。",
        "未能从OA明细关联发票确认纸质发票，购买方、销售方、金额及明细归属均未核验。",
        "申报金额为356元，但关联发票金额未取得，支付事实与关联发票的对应关系未建立，不能按孰低原则确定核销金额。",
        "第2至第4笔发票归属未确认，且付款状态和流水号为空，无法按孰低原则核销。",
        "关联发票金额与支付截图金额均为198元，但支付对应关系未确认，无法按孰低原则确定核销金额。",
    ),
)
def test_tpm_accepts_specific_natural_failure_phrasing(
    tmp_path: Path,
    natural_specific: str,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            _decision(action, conclusion=natural_specific),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"
