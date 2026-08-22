import json
import re
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import elastic_queue


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_ROOT = ROOT / ".agents" / "skills" / "oa-finance-preaudit" / "references"
RESULT_CONTRACT = REFERENCE_ROOT / "result-contract.md"
SKILL = REFERENCE_ROOT.parent / "SKILL.md"
AUTOMATION_BATCH_CONTRACT = REFERENCE_ROOT / "automation-batch-contract.md"
CONTROLLER_CONTRACT = REFERENCE_ROOT / "controller-contract.md"
DISPATCHER_CONTRACT = REFERENCE_ROOT / "dispatcher-contract.md"
REVIEWER_CONTRACT = REFERENCE_ROOT / "reviewer-contract.md"
WRITER_CONTRACT = REFERENCE_ROOT / "writer-contract.md"
CONFIRMED_FEEDBACK = REFERENCE_ROOT / "confirmed-feedback.md"
REGRESSION_CASES = REFERENCE_ROOT / "regression-cases.json"
PACKAGING_SKILL_ROOT = ROOT / ".agents" / "skills" / "audit-chinese-image-text"

FLOW_RULE_PACKS = {
    "travel_expense": REFERENCE_ROOT / "flows" / "travel-expense.md",
    "business_entertainment": REFERENCE_ROOT / "flows" / "business-entertainment.md",
    "business_support_fee": REFERENCE_ROOT / "flows" / "business-support-fee.md",
    "small_marketing_fee": REFERENCE_ROOT / "flows" / "small-marketing-fee.md",
    "packaging_layout_approval": REFERENCE_ROOT
    / "flows"
    / "packaging-layout-approval.md",
}
FLOW_ITEM_NAMES = {
    "travel_expense": (
        "报销周期",
        "出差与住宿天数",
        "公共交通",
        "住宿与考勤",
        "跨区域带车",
        "油补",
        "个人垫付车票",
        "发票购买方",
    ),
    "business_entertainment": (
        "附件完整性/支付证据",
        "发票关联",
        "预算",
        "金额孰低核销",
    ),
    "business_support_fee": (
        "附件完整性",
        "业务事项合规性",
        "预算校验",
        "金额孰低核销",
    ),
    "small_marketing_fee": (
        "预算科目",
        "附件完整性",
        "费用承担主体",
        "SAP 客户编码匹配",
        "金额孰低核销",
    ),
    "packaging_layout_approval": (
        "配料信息",
        "过敏原信息",
        "营养成分表信息",
        "地址信息",
    ),
}

ROLE_REQUIREMENTS = [
    (
        "dispatcher",
        DISPATCHER_CONTRACT,
        ("不得 claim", "不得读取附件", "HOURLY_DISPATCH_RECEIPT", "CONTROL_START"),
        ("collect-assignment", "submit-frozen-once"),
    ),
    (
        "controller",
        CONTROLLER_CONTRACT,
        ("结束门禁", "actionable=0", "滚动检查点", "补槽 → 再次等待"),
        (),
    ),
    (
        "reviewer",
        REVIEWER_CONTRACT,
        ("collect-assignment", "collection-status", "seal-review", "绝不重新采集"),
        ("create_thread", "set_thread_archived", "run-once"),
    ),
    (
        "writer",
        WRITER_CONTRACT,
        ("按请求隔离", "不得执行审核判断", "submit-frozen-once", "current proof"),
        ("collect-assignment",),
    ),
]

RUNTIME_REQUIREMENTS = {
    "queue_and_identity": (
        "submitted_at 从早到晚",
        "requestId + received_at + worker_id + lease_token",
        "同一 requestId 不能同时租给两个任务",
        "跨单据不建立写入依赖",
    ),
    "frozen_submission": (
        "四个文件位于同一 attempt",
        "四份 SHA-256",
        "Writer 不读取附件正文",
        "窄 `frozen_writer submit` 是唯一 OA 写入入口",
        "request_id、submission_token、run_id",
    ),
    "control_plane": (
        "每个自然日只有一个当天控制台",
        "每小时创建一个独立可见调度任务",
        "队列状态机是唯一运行事实",
        "用户指定的 Admin",
        "不得硬编码 Admin threadId",
    ),
    "lifecycle_and_ops": (
        "PENDING_SKILL_RELEASE",
        "继续按已加载指纹自然闭合",
        "OPS_EVENT|",
        "OPS_RESOLUTION|",
        "closure=awaiting_ops_resolution",
        "不得自动修改财务规则",
    ),
}

FINANCE_REQUIREMENTS = [
    (
        "travel_expense",
        (
            "实际行程日期",
            "常驻地集合",
            "酒店详情页完整地址",
            "正负金额绝对值相等且净额为 0",
            "起始读数、结束读数、单位和计算差值",
            "无法稳定辨认时使用 `⚠️`",
        ),
    ),
    (
        "business_entertainment",
        (
            "已支付金额－已退款金额",
            "稳定订单号、交易号或支付平台关联记录",
            "同一支付事实覆盖多条拆分明细",
            "该支付事实只计一次",
        ),
    ),
    ("small_marketing_fee", ("SAP 客户编码匹配", "金额孰低核销")),
]


@pytest.mark.parametrize(
    "workflow_key,path,item_names",
    [(key, FLOW_RULE_PACKS[key], FLOW_ITEM_NAMES[key]) for key in FLOW_RULE_PACKS],
)
def test_each_flow_rule_pack_has_only_its_fixed_items(
    workflow_key: str, path: Path, item_names: tuple[str, ...]
) -> None:
    contract = path.read_text(encoding="utf-8")
    assert f"`workflow_key={workflow_key}`" in contract
    assert "## 固定审核项" in contract
    assert (
        tuple(
            re.findall(
                r"^[1-9][0-9]*\. ([^：；。\n]+)[：；。].*$", contract, re.MULTILINE
            )
        )
        == item_names
    )


@pytest.mark.parametrize("workflow_key,item_names", FLOW_ITEM_NAMES.items())
def test_each_flow_fixed_items_match_the_runtime_remark_contract(
    workflow_key: str, item_names: tuple[str, ...], tmp_path: Path
) -> None:
    remark = tmp_path / "remark.txt"
    lines = ["AI审核结论：可提交 ✅"] + [
        f"✅ {index}、{item_name}：符合。"
        for index, item_name in enumerate(item_names, start=1)
    ]
    if workflow_key == "packaging_layout_approval":
        lines.append("Excel 对照：本次未提供 Excel，未执行 Excel 对照。")
    remark.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = elastic_queue.validate_remark_file(
        remark, expected_path=remark, expected_flow_key=workflow_key
    )

    assert result["remark_fingerprint"].startswith("sha256:")


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


@pytest.mark.parametrize(
    "role,path,required,forbidden",
    ROLE_REQUIREMENTS,
    ids=[item[0] for item in ROLE_REQUIREMENTS],
)
def test_runtime_role_contracts_keep_narrow_authority(
    role: str, path: Path, required: tuple[str, ...], forbidden: tuple[str, ...]
) -> None:
    contract = path.read_text(encoding="utf-8")
    assert all(marker in contract for marker in required), role
    assert all(marker not in contract for marker in forbidden), role


@pytest.mark.parametrize("area,markers", RUNTIME_REQUIREMENTS.items())
def test_automation_contract_preserves_critical_runtime_boundaries(
    area: str, markers: tuple[str, ...]
) -> None:
    contract = AUTOMATION_BATCH_CONTRACT.read_text(encoding="utf-8")
    assert all(marker in contract for marker in markers), area


@pytest.mark.parametrize("workflow_key,markers", FINANCE_REQUIREMENTS)
def test_flow_rule_packs_preserve_high_impact_finance_judgments(
    workflow_key: str, markers: tuple[str, ...]
) -> None:
    contract = FLOW_RULE_PACKS[workflow_key].read_text(encoding="utf-8")
    assert all(marker in contract for marker in markers), workflow_key


def test_packaging_contract_stays_text_only_and_has_finite_escalation() -> None:
    audit_skill = (PACKAGING_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    output_contract = (
        PACKAGING_SKILL_ROOT / "references" / "output-contract.md"
    ).read_text(encoding="utf-8")
    flow_contract = FLOW_RULE_PACKS["packaging_layout_approval"].read_text(
        encoding="utf-8"
    )
    for marker in (
        "关键文字范围",
        "最多增加一次独立复核",
        "最多执行一次定向重切",
    ):
        assert marker in audit_skill
    assert "默认快速路径" in AUTOMATION_BATCH_CONTRACT.read_text(encoding="utf-8")
    assert "不得无限重切或无限等待" in flow_contract
    assert "非编号的 `补充校对`" in flow_contract
    assert "不是第五审核项" in output_contract
    assert "三路盲审齐全数" not in output_contract


def test_confirmed_feedback_and_regressions_are_sanitized_and_structured() -> None:
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


def test_skill_keeps_throughput_and_feedback_as_explicit_boundaries() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    for marker in (
        "快速、准确、持续排空",
        "不能把控制面动作变成审批路障",
        "已确认反馈清单",
        "错误回归案例",
        "不得要求重复确认",
    ):
        assert marker in skill
