from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "oa-finance-preaudit"
SKILL = SKILL_ROOT / "SKILL.md"
REFERENCE_ROOT = SKILL_ROOT / "references"
RESULT_CONTRACT = REFERENCE_ROOT / "result-contract.md"
OBSERVATION_CONTRACT = REFERENCE_ROOT / "observation-contract.md"
CONFIRMED_FEEDBACK = REFERENCE_ROOT / "confirmed-feedback.md"
REGRESSION_CASES = REFERENCE_ROOT / "regression-cases.json"
GOLDEN_REPLAY = ROOT / "tests" / "finance_preaudit" / "fixtures" / "golden_replay.json"
FLOW_RULE_PACKS = {
    "travel_expense": REFERENCE_ROOT / "flows" / "travel-expense.md",
    "business_entertainment": REFERENCE_ROOT / "flows" / "business-entertainment.md",
    "business_support_fee": REFERENCE_ROOT / "flows" / "business-support-fee.md",
    "small_marketing_fee": REFERENCE_ROOT / "flows" / "small-marketing-fee.md",
    "tpm_small_marketing_fee": REFERENCE_ROOT / "flows" / "tpm-small-marketing-fee.md",
    "general_expense": REFERENCE_ROOT / "flows" / "general-expense.md",
    "packaging_layout_approval": REFERENCE_ROOT / "flows" / "packaging-layout-approval.md",
}


@pytest.mark.parametrize("workflow_key,path", FLOW_RULE_PACKS.items())
def test_each_flow_rule_pack_matches_the_golden_contract(
    workflow_key: str, path: Path
) -> None:
    contract = path.read_text(encoding="utf-8")
    golden_items = json.loads(GOLDEN_REPLAY.read_text(encoding="utf-8"))["flows"]
    documented_names = re.findall(
        r"^[1-9][0-9]*\. ([^：；。\n]+)[：；。].*$",
        contract,
        re.MULTILINE,
    )
    assert f"`workflow_key={workflow_key}`" in contract
    assert documented_names == golden_items[workflow_key]


def test_skill_routes_only_through_the_two_review_actions() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    assert "next-action" in skill
    assert "complete-review" in skill
    for removed_surface in (
        "queue_cli",
        "role_cli",
        "Controller",
        "Reviewer",
        "Writer",
        "Observer",
        "ITEM_RESULT",
        "OPS_EVENT",
        "collect-assignment",
        "seal-review",
        "begin-submit",
        "release manifest",
        "租约",
    ):
        assert removed_surface not in skill


def test_shared_result_contract_routes_without_copying_flow_rules() -> None:
    contract = RESULT_CONTRACT.read_text(encoding="utf-8")
    for path in FLOW_RULE_PACKS.values():
        assert f"({path.relative_to(REFERENCE_ROOT).as_posix()})" in contract
    for flow_specific_rule in (
        "差旅第 1 项判定细则",
        "同一支付事实覆盖多条拆分明细",
        "SAP 主数据接口未接入",
    ):
        assert flow_specific_rule not in contract


def test_travel_page_routing_allows_only_exact_duplicate_reuse() -> None:
    contract = OBSERVATION_CONTRACT.read_text(encoding="utf-8")

    assert "只做页级 SHA-256 精确重复复用" in contract
    assert "声明哈希与冻结页面实际字节复核" in contract
    assert "`review_page_refs` 是必须直接覆盖的非重复页" in contract
    assert "完成动作必须逐项验证所有 `review_page_refs`" in contract
    assert "回退覆盖全部 `prepared.images`" in contract
    assert "不得按文件名、页序、金额、日期或关键词继续裁剪" in contract


def test_confirmed_feedback_and_regressions_stay_sanitized() -> None:
    feedback = CONFIRMED_FEEDBACK.read_text(encoding="utf-8")
    regression_data = json.loads(REGRESSION_CASES.read_text(encoding="utf-8"))
    assert "不得记录流程编号、requestId" in feedback
    assert regression_data["schema_version"] == 1
    required = {
        "id",
        "scope",
        "status",
        "input_facts",
        "incorrect_output",
        "expected_output",
        "audit_item",
        "failure_mode",
        "rule_ref",
    }
    for case in regression_data["cases"]:
        assert set(case) == required
        assert case["status"] == "confirmed"
        assert case["input_facts"]
        assert case["incorrect_output"]
        assert case["expected_output"]
    serialized = json.dumps(regression_data, ensure_ascii=False)
    assert re.search(r"(?:CLBX|YWZDL)\d+", serialized) is None


@pytest.mark.parametrize(
    "workflow_key,markers",
    (
        (
            "travel_expense",
            (
                "实际行程日期",
                "酒店详情页完整地址",
                "正负金额绝对值相等且净额为 0",
                "起始读数、结束读数、单位和计算差值",
            ),
        ),
        (
            "business_entertainment",
            (
                "已支付金额－已退款金额",
                "同一支付事实覆盖多条拆分明细",
                "该支付事实只计一次",
            ),
        ),
        (
            "small_marketing_fee",
            (
                "SAP 客户编码匹配",
                "金额孰低核销",
                "支付截图",
                "发票",
                "报销公司、支付公司、购买方",
                "small_marketing_fee:1",
                "small_marketing_fee:2",
            ),
        ),
        (
            "tpm_small_marketing_fee",
            (
                "10 类费用科目",
                "终端建设费-终端形象制作费",
                "通路促销-订货会会务费",
                "不少于 4 张",
                "核对 OA“关联发票”及发票金额",
                "收款人名称与关联发票销售方一致",
                "排除重复截图",
                "不设置容差",
                "AI 只形成预审意见，不批准、不退回",
            ),
        ),
        (
            "general_expense",
            (
                "实付 0 元",
                "“先乘车后付款”不等于“先用后付”",
                "当前状态明确为“支付成功”",
                "发票+支付截图",
                "当前阶段按既定策略暂不校验部门年度预算",
                "报销公司、支付公司、购买方",
            ),
        ),
    ),
)
def test_high_impact_finance_rules_remain_explicit(
    workflow_key: str,
    markers: tuple[str, ...],
) -> None:
    contract = FLOW_RULE_PACKS[workflow_key].read_text(encoding="utf-8")
    assert all(marker in contract for marker in markers)


def test_general_expense_paid_ride_receipt_regression_is_confirmed() -> None:
    regression_cases = {
        case["id"]: case
        for case in json.loads(REGRESSION_CASES.read_text(encoding="utf-8"))["cases"]
    }
    case = regression_cases["RC-028-paid-ride-receipt-treated-as-deferred"]

    assert case["scope"] == "general-expense"
    assert "同一页面当前状态明确显示支付成功" in case["input_facts"]
    assert "实际扣款金额" in case["expected_output"]
    assert "实付0元" in case["incorrect_output"]


def test_packaging_rule_keeps_text_scope_and_finite_escalation() -> None:
    packaging_skill = (
        ROOT / ".agents" / "skills" / "audit-chinese-image-text" / "SKILL.md"
    ).read_text(encoding="utf-8")
    flow_contract = FLOW_RULE_PACKS["packaging_layout_approval"].read_text(
        encoding="utf-8"
    )
    result_contract = RESULT_CONTRACT.read_text(encoding="utf-8")
    regression_cases = {
        case["id"]: case
        for case in json.loads(REGRESSION_CASES.read_text(encoding="utf-8"))["cases"]
    }
    assert "关键文字范围" in packaging_skill
    assert "最多增加一次独立复核" in packaging_skill
    assert "最多执行一次定向重切" in packaging_skill
    assert "不得无限重切或无限等待" in flow_contract
    assert "只有全部页面均明确为 `non_packaging`" in packaging_skill
    assert "仅含品名、规格、储存条件、运输/堆码信息或生产日期填写位" in packaging_skill
    assert "不得仅因零售包装缺少固定四项" in packaging_skill
    assert "不承载固定四项内容的储运外箱版面使用 `non_packaging`" in flow_contract
    assert "生产/经销单位名称或地址等固定项内容" in flow_contract
    assert "只要存在一页 `packaging_layout`" in flow_contract
    assert "不得跨版本拼接证据" in flow_contract
    assert "全部图片的视觉分类均为 `non_packaging`" in result_contract
    assert {
        "RC-023-packaging-storage-carton-four-item-warning",
        "RC-024-packaging-retail-layout-missing-items-skipped",
        "RC-025-packaging-carton-fixed-item-content-skipped",
    }.issubset(regression_cases)
    assert (
        "页面不承载配料、过敏原、营养成分表、生产经销单位名称或地址等固定项内容"
        in regression_cases["RC-023-packaging-storage-carton-four-item-warning"][
            "input_facts"
        ]
    )
    assert "固定四项存在缺项" in regression_cases[
        "RC-024-packaging-retail-layout-missing-items-skipped"
    ]["input_facts"]
    assert any(
        "至少一项" in fact
        for fact in regression_cases[
            "RC-025-packaging-carton-fixed-item-content-skipped"
        ]["input_facts"]
    )
