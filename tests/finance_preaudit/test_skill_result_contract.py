import json
import re
import tempfile
import unittest
from pathlib import Path

from apps.oa_finance_preaudit import elastic_queue


RESULT_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "oa-finance-preaudit"
    / "references"
    / "result-contract.md"
)
SKILL = RESULT_CONTRACT.parents[1] / "SKILL.md"
OBSERVATION_CONTRACT = RESULT_CONTRACT.parent / "observation-contract.md"
AUTOMATION_BATCH_CONTRACT = RESULT_CONTRACT.parent / "automation-batch-contract.md"
DISPATCHER_CONTRACT = RESULT_CONTRACT.parent / "dispatcher-contract.md"
CONTROLLER_CONTRACT = RESULT_CONTRACT.parent / "controller-contract.md"
REVIEWER_CONTRACT = RESULT_CONTRACT.parent / "reviewer-contract.md"
WRITER_CONTRACT = RESULT_CONTRACT.parent / "writer-contract.md"
CONFIRMED_FEEDBACK = RESULT_CONTRACT.parent / "confirmed-feedback.md"
REGRESSION_CASES = RESULT_CONTRACT.parent / "regression-cases.json"
PACKAGING_AUDIT_SKILL = (
    RESULT_CONTRACT.parents[2] / "audit-chinese-image-text" / "SKILL.md"
)
PACKAGING_OUTPUT_CONTRACT = (
    RESULT_CONTRACT.parents[2]
    / "audit-chinese-image-text"
    / "references"
    / "output-contract.md"
)
FLOW_RULE_PACKS = {
    "travel_expense": RESULT_CONTRACT.parent / "flows" / "travel-expense.md",
    "business_entertainment": (
        RESULT_CONTRACT.parent / "flows" / "business-entertainment.md"
    ),
    "business_support_fee": (
        RESULT_CONTRACT.parent / "flows" / "business-support-fee.md"
    ),
    "small_marketing_fee": (
        RESULT_CONTRACT.parent / "flows" / "small-marketing-fee.md"
    ),
    "packaging_layout_approval": (
        RESULT_CONTRACT.parent / "flows" / "packaging-layout-approval.md"
    ),
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


class SkillResultContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = RESULT_CONTRACT.read_text(encoding="utf-8")
        cls.observation_contract = OBSERVATION_CONTRACT.read_text(encoding="utf-8")
        cls.automation_batch_contract = AUTOMATION_BATCH_CONTRACT.read_text(
            encoding="utf-8"
        )
        cls.dispatcher_contract = DISPATCHER_CONTRACT.read_text(encoding="utf-8")
        cls.controller_contract = CONTROLLER_CONTRACT.read_text(encoding="utf-8")
        cls.reviewer_contract = REVIEWER_CONTRACT.read_text(encoding="utf-8")
        cls.writer_contract = WRITER_CONTRACT.read_text(encoding="utf-8")
        cls.skill = SKILL.read_text(encoding="utf-8")
        cls.feedback = CONFIRMED_FEEDBACK.read_text(encoding="utf-8")
        cls.regression_data = json.loads(REGRESSION_CASES.read_text(encoding="utf-8"))
        cls.packaging_audit_skill = PACKAGING_AUDIT_SKILL.read_text(encoding="utf-8")
        cls.packaging_output_contract = PACKAGING_OUTPUT_CONTRACT.read_text(
            encoding="utf-8"
        )
        cls.flow_contracts = {
            workflow_key: path.read_text(encoding="utf-8")
            for workflow_key, path in FLOW_RULE_PACKS.items()
        }
        cls.travel_contract = cls.flow_contracts["travel_expense"]
        cls.entertainment_contract = cls.flow_contracts["business_entertainment"]
        cls.small_marketing_contract = cls.flow_contracts["small_marketing_fee"]
        cls.packaging_contract = cls.flow_contracts["packaging_layout_approval"]

    def test_each_workflow_has_one_isolated_rule_pack(self):
        self.assertEqual(set(FLOW_RULE_PACKS), set(FLOW_ITEM_NAMES))
        unique_markers = {
            "travel_expense": "报销周期",
            "business_entertainment": "附件完整性/支付证据",
            "business_support_fee": "业务事项合规性",
            "small_marketing_fee": "SAP 客户编码匹配",
            "packaging_layout_approval": "配料信息",
        }
        for workflow_key, path in FLOW_RULE_PACKS.items():
            with self.subTest(workflow_key=workflow_key):
                self.assertTrue(path.is_file(), path)
                contract = path.read_text(encoding="utf-8")
                self.assertIn(f"`workflow_key={workflow_key}`", contract)
                self.assertIn("## 固定审核项", contract)
                item_names = tuple(
                    re.findall(
                        r"^[1-9][0-9]*\. ([^：；。\n]+)[：；。].*$",
                        contract,
                        re.MULTILINE,
                    )
                )
                self.assertEqual(item_names, FLOW_ITEM_NAMES[workflow_key])
                for other_key, marker in unique_markers.items():
                    if other_key != workflow_key:
                        self.assertNotIn(marker, contract)

    def test_shared_result_contract_is_only_the_common_interface_and_router(self):
        for path in FLOW_RULE_PACKS.values():
            relative = path.relative_to(RESULT_CONTRACT.parent).as_posix()
            self.assertIn(f"({relative})", self.contract)
        for flow_specific_rule in (
            "差旅第 1 项判定细则",
            "同一支付事实覆盖多条拆分明细",
            "SAP 主数据接口未接入",
            "每一版包装图片都必须输出上述四项",
        ):
            self.assertNotIn(flow_specific_rule, self.contract)

    def test_reviewer_loads_only_the_material_workflow_rule_pack(self):
        self.assertIn("材料中的 `workflow_key`", self.reviewer_contract)
        self.assertIn("只读取该 `workflow_key` 对应的一个流程规则包", self.reviewer_contract)
        self.assertIn("不得读取其他流程规则包", self.reviewer_contract)

    def test_each_flow_rule_pack_matches_the_runtime_output_contract(self):
        for workflow_key, item_names in FLOW_ITEM_NAMES.items():
            with self.subTest(workflow_key=workflow_key), tempfile.TemporaryDirectory() as temporary:
                remark = Path(temporary) / "remark.txt"
                lines = ["AI审核结论：可提交 ✅"]
                lines.extend(
                    f"✅ {index}、{item_name}：符合。"
                    for index, item_name in enumerate(item_names, start=1)
                )
                if workflow_key == "packaging_layout_approval":
                    lines.append("Excel 对照：本次未提供 Excel，未执行 Excel 对照。")
                remark.write_text("\n".join(lines) + "\n", encoding="utf-8")

                result = elastic_queue.validate_remark_file(
                    remark,
                    expected_path=remark,
                    expected_flow_key=workflow_key,
                )

                self.assertTrue(result["remark_fingerprint"].startswith("sha256:"))

    def test_travel_period_uses_actual_travel_date_not_transaction_date(self):
        self.assertIn("实际行程日期", self.travel_contract)
        self.assertIn("消费日期、下单日期、支付日期和开票日期", self.travel_contract)
        self.assertIn("不得仅因缺少结构化“报销期间”降为 `⚠️`", self.travel_contract)

    def test_cross_region_candidates_exclude_resident_locations(self):
        self.assertIn("常驻地集合", self.travel_contract)
        self.assertIn("跨区域候选集合", self.travel_contract)
        self.assertIn("不得进入跨区域候选集合", self.travel_contract)

    def test_fully_reversed_hotel_booking_is_not_stay_evidence(self):
        self.assertIn("正负金额绝对值相等且净额为 0", self.travel_contract)
        self.assertIn("不计入住宿夜数", self.travel_contract)
        self.assertIn("不得参与住宿与考勤地点比较", self.travel_contract)
        self.assertIn("部分退款", self.travel_contract)

    def test_hotel_location_uses_detail_address_not_booking_summary(self):
        self.assertIn("酒店详情页完整地址", self.travel_contract)
        self.assertIn("费用说明中的城市、入住区间和夜数只是预订摘要", self.travel_contract)
        self.assertIn("酒店名称括号内的商圈、门店或区域字样", self.travel_contract)

    def test_refund_reduces_actual_payment_only_when_relation_is_stable(self):
        self.assertIn("已支付金额－已退款金额", self.entertainment_contract)
        self.assertIn("稳定订单号、交易号或支付平台关联记录", self.entertainment_contract)
        self.assertIn("不得按相似金额或相邻截图强行抵减", self.entertainment_contract)

    def test_multiple_payment_vouchers_are_summed_per_owned_detail(self):
        self.assertIn("同一费用明细稳定绑定多张支付凭证", self.entertainment_contract)
        self.assertIn("逐张读取交易状态和实际支出", self.entertainment_contract)
        self.assertIn("不能仅因凭证超过一张就停止在“无法汇总”", self.entertainment_contract)
        self.assertIn("不得按文件名、相邻顺序或凑申报金额强行相加", self.entertainment_contract)

    def test_one_payment_fact_can_cover_stably_bound_split_details(self):
        self.assertIn("同一支付事实覆盖多条拆分明细", self.entertainment_contract)
        self.assertIn("拆分明细申报金额之和等于凭证实付金额", self.entertainment_contract)
        self.assertIn("该支付事实只计一次", self.entertainment_contract)
        self.assertIn("不得表述为每条明细各有一笔独立支付", self.entertainment_contract)
        self.assertIn("交易号或订单号可见时必须一致", self.entertainment_contract)
        self.assertIn("不可见时不能单独作为否定分组的理由", self.entertainment_contract)

    def test_payment_status_uses_business_semantics_not_one_literal(self):
        self.assertIn("有效支付证据不要求画面逐字出现“支付成功”", self.entertainment_contract)
        self.assertIn("订单已完成", self.entertainment_contract)
        self.assertIn("扫码支付", self.entertainment_contract)
        self.assertIn("支付合计", self.entertainment_contract)
        self.assertIn("取消、关闭、失败、退款或待支付", self.entertainment_contract)

    def test_pass_remarks_collapse_amounts_into_final_reconciliation_item(self):
        self.assertIn("可提交金额收敛", self.contract)
        self.assertIn("前置审核项只说明证据状态和关联结果", self.contract)
        self.assertIn("只展示一次最终核销合计", self.contract)
        self.assertIn("不逐笔重复支付、发票和申报金额", self.contract)

    def test_visual_queue_only_contains_unique_pages_that_need_vision(self):
        self.assertIn("视觉队列", self.observation_contract)
        self.assertIn("结构化内容已足够", self.observation_contract)
        self.assertIn("页级精确重复", self.observation_contract)
        self.assertIn("1–3 个视觉工作单元", self.observation_contract)
        self.assertIn("4–12 个视觉工作单元", self.observation_contract)
        self.assertIn("超过 12 个视觉工作单元", self.observation_contract)

    def test_visual_observation_is_reusable_without_reopening_clear_pages(self):
        for field in (
            "resource_id",
            "owner_detail_id",
            "page",
            "visible_fields",
            "uncertainties",
        ):
            self.assertIn(field, self.observation_contract)
        self.assertIn("先按高细节读取", self.observation_contract)
        self.assertIn("升级为原始分辨率", self.observation_contract)
        self.assertIn("不得重复打开", self.observation_contract)

    def test_single_flow_runner_continues_after_item_level_failures(self):
        flow_runner_contract = (RESULT_CONTRACT.parent / "flow-runner-contract.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("单流程定时运行合同", self.skill)
        self.assertIn("只闭合当前 requestId", flow_runner_contract)
        self.assertIn("同一流程内其他安全单据继续", flow_runner_contract)
        self.assertIn("不得暂停、重启或改写其他四个入口", flow_runner_contract)

    def test_automation_batch_drains_actionable_queue(self):
        self.assertIn("每小时完整拉取一次", self.automation_batch_contract)
        self.assertIn("刷新并合并清单", self.automation_batch_contract)
        self.assertIn("本轮已尝试 requestId 集合", self.automation_batch_contract)

    def test_backlog_catchup_keeps_one_rolling_controller_until_actionable_zero(self):
        self.assertIn("连续排空模式", self.controller_contract)
        self.assertIn("20 条", self.automation_batch_contract)
        self.assertIn("不等待下一小时", self.automation_batch_contract)
        self.assertIn("再次完整拉取两个实时来源", self.automation_batch_contract)
        self.assertIn("积压未清空时不得因固定时长停止新增 claim", self.controller_contract)
        self.assertIn("滚动检查点", self.controller_contract)

    def test_backlog_mode_uses_bounded_review_and_submit_pools(self):
        self.assertIn("普通池最多 3 个单据审核 Agent", self.automation_batch_contract)
        self.assertIn("包装准确性协调位独立计算", self.automation_batch_contract)
        self.assertIn("实际可用槽位", self.automation_batch_contract)
        self.assertIn("唯一 OA 写入入口", self.automation_batch_contract)
        self.assertIn("等待下一小时刷新", self.automation_batch_contract)
        self.assertIn("低待办时不启动十分钟心跳", self.automation_batch_contract)
        self.assertIn("不同 requestId 可以并发", self.automation_batch_contract)
        self.assertIn("同一 requestId 使用 requestId 级锁", self.automation_batch_contract)
        self.assertIn("剩余候选", self.automation_batch_contract)

    def test_frozen_writer_rechecks_attempt_identity_without_reading_attachments(self):
        self.assertIn("apps.oa_approval.frozen_writer submit", self.automation_batch_contract)
        self.assertIn("四个文件位于同一 attempt", self.automation_batch_contract)
        self.assertIn("四份 SHA-256", self.automation_batch_contract)
        self.assertIn("Writer 不读取附件正文", self.automation_batch_contract)

    def test_request_identity_is_rechecked_before_any_submission_token(self):
        self.assertIn("版本、待办身份或冻结指纹变化", self.automation_batch_contract)
        self.assertIn("只保持该项 prepared", self.automation_batch_contract)
        self.assertIn("不影响池内其他 requestId", self.automation_batch_contract)

    def test_begin_submit_handoff_cannot_yield_before_writer_launch(self):
        self.assertIn(
            "`role_cli writer submit-frozen-once`",
            self.automation_batch_contract,
        )
        self.assertIn("同一个前台进程", self.automation_batch_contract)
        self.assertIn("不得手工拼接这些步骤", self.automation_batch_contract)

    def test_current_proof_uses_exact_source_schemas_without_terminal_scanning(self):
        self.assertIn("`.items[].requestId`", self.automation_batch_contract)
        self.assertIn("`.cases[].request_id`", self.automation_batch_contract)
        self.assertIn("各自唯一命中一条", self.automation_batch_contract)
        self.assertIn("终端展示、截断片段或肉眼搜索", self.automation_batch_contract)
        self.assertIn(
            "`role_cli writer submit-frozen-once",
            self.automation_batch_contract,
        )
        self.assertIn("写临时 JSON", self.automation_batch_contract)
        self.assertIn("原子替换 final", self.automation_batch_contract)
        self.assertIn("current proof 必须不存在", self.automation_batch_contract)
        self.assertIn("不得手工重定向", self.automation_batch_contract)

    def test_live_sources_are_atomically_materialized_before_writer_launch(self):
        self.assertIn(
            "role_cli controller capture-live-inputs",
            self.automation_batch_contract,
        )
        self.assertIn(
            "role_cli controller list-production-pending",
            self.automation_batch_contract,
        )
        self.assertIn("`live-inputs.json`", self.automation_batch_contract)
        self.assertIn(
            "preaudit_production_identities_written",
            self.automation_batch_contract,
        )
        self.assertIn("preaudit_case_index_written", self.automation_batch_contract)
        self.assertNotIn("apps.oa_approval.cli", self.automation_batch_contract)
        self.assertNotIn("oa_todo_list_written", self.automation_batch_contract)
        self.assertIn("manifest 路径、两个 final schema、计数与 SHA-256", self.writer_contract)

    def test_runtime_contract_does_not_publish_low_level_queue_commands(self):
        for command in (
            "queue_cli status",
            "queue_cli claim",
            "queue_cli collect-assignment",
            "queue_cli seal-review",
            "queue_cli recover-stale",
            "queue_cli capture-live-inputs",
            "queue_cli next-submit",
            "queue_cli submit-frozen-once",
            "queue_cli resolve-submitting-external",
            "queue_cli mark-submit-failed",
            "queue_cli requeue-blocked",
            "queue_cli requeue-blocked-external-manual",
            "queue_cli block-prepared",
        ):
            self.assertNotIn(command, self.automation_batch_contract)
        self.assertNotIn(
            "后续关于手工 `collect-case`、手工封套",
            self.automation_batch_contract,
        )

    def test_controller_waits_for_workers_until_the_epoch_is_closed(self):
        self.assertIn("发送审核任务后不得结束当前 epoch", self.automation_batch_contract)
        self.assertIn("wait_threads", self.automation_batch_contract)
        self.assertIn("任一审核 Agent 完成", self.automation_batch_contract)
        self.assertIn("提交当前全部可执行 prepared 单据", self.automation_batch_contract)

    def test_controller_uses_rolling_checkpoints_without_stopping_backlog(self):
        self.assertIn("滚动调度 Goal", self.automation_batch_contract)
        self.assertIn("滚动检查点", self.automation_batch_contract)
        self.assertIn("create_goal", self.automation_batch_contract)
        self.assertIn("get_goal", self.automation_batch_contract)
        self.assertIn("积压未清空时不得因固定时长停止新增 claim", self.automation_batch_contract)
        self.assertIn("update_goal(status=complete)", self.automation_batch_contract)
        self.assertIn("单次不超过 60 秒", self.automation_batch_contract)

    def test_automation_batch_orders_claims_without_cross_request_submit_dependency(self):
        self.assertIn("list-pending", self.automation_batch_contract)
        self.assertIn("submitted_at 从早到晚", self.automation_batch_contract)
        self.assertIn("received_at", self.automation_batch_contract)
        self.assertIn("requestId 升序", self.automation_batch_contract)
        self.assertIn("缺失提交时间的候选优先", self.automation_batch_contract)
        self.assertIn("跨单据不建立写入依赖", self.automation_batch_contract)

    def test_automation_batch_deduplicates_unchanged_blocks_across_goals(self):
        self.assertIn("跨轮阻断索引", self.automation_batch_contract)
        self.assertIn("requestId + received_at", self.automation_batch_contract)
        self.assertIn("reviewer release manifest 的 SHA-256", self.automation_batch_contract)
        self.assertIn("24 小时冷却", self.automation_batch_contract)
        self.assertIn("不得重新分配", self.automation_batch_contract)
        self.assertIn("received_at 变化", self.automation_batch_contract)

    def test_elastic_preparation_and_submission_are_request_isolated(self):
        self.assertIn("不得生成、修正或推断财务结论", self.automation_batch_contract)
        self.assertIn("全新原生审核 Agent 按 Skill 完成", self.automation_batch_contract)
        self.assertIn("当天控制台", self.automation_batch_contract)
        self.assertIn("requestId + received_at + worker_id + lease_token", self.automation_batch_contract)
        self.assertIn(
            "requests/{requestId}/{isolation_key}/attempt-{preparation_epoch}/",
            self.automation_batch_contract,
        )
        self.assertIn("同一 requestId 不能同时租给两个任务", self.automation_batch_contract)
        self.assertIn("material_fingerprint", self.automation_batch_contract)
        self.assertIn("差旅保留通道", self.automation_batch_contract)
        self.assertIn("非差旅财务保留通道", self.automation_batch_contract)
        self.assertIn("2–19 条保持 2 个", self.automation_batch_contract)
        self.assertIn("prepared 缓冲不足", self.automation_batch_contract)
        self.assertIn("第 3 槽", self.automation_batch_contract)
        self.assertIn("实际可用槽位", self.automation_batch_contract)
        self.assertIn("小时调度任务只等待控制台反馈", self.automation_batch_contract)
        self.assertIn("禁止无上限创建任务", self.automation_batch_contract)
        self.assertIn("当天控制台", self.automation_batch_contract)
        self.assertIn("兼任跨线程只读监控", self.automation_batch_contract)
        self.assertIn("不启动十分钟模型心跳或独立常驻监控任务", self.automation_batch_contract)
        self.assertIn("跨单据不建立写入依赖", self.automation_batch_contract)
        self.assertIn("窄 `frozen_writer submit` 是唯一 OA 写入入口", self.automation_batch_contract)
        self.assertIn("mark-submit-failed --confirm-no-write YES", self.automation_batch_contract)
        self.assertIn("active_run=false", self.automation_batch_contract)
        self.assertIn("--no-write-proof-file", self.automation_batch_contract)
        self.assertIn("request_id、submission_token、run_id", self.automation_batch_contract)
        self.assertIn("--current-proof-file", self.automation_batch_contract)
        self.assertIn("requeue-blocked --confirm-repair YES", self.automation_batch_contract)
        self.assertIn("requestId + received_at + failure_code", self.automation_batch_contract)
        self.assertIn("共享下载通道", self.automation_batch_contract)

    def test_worker_assignment_forwards_the_complete_claim_packet(self):
        self.assertIn("不可变 assignment JSON", self.automation_batch_contract)
        self.assertIn("role_cli controller claim", self.automation_batch_contract)
        self.assertIn("原样转发", self.automation_batch_contract)
        self.assertIn("不得从队列摘要手工重建", self.automation_batch_contract)
        self.assertIn("received_at", self.automation_batch_contract)
        self.assertIn("material_file", self.automation_batch_contract)
        self.assertIn("assignment_payload_incomplete", self.automation_batch_contract)

    def test_archived_claim_copy_is_never_used_as_canonical_assignment_input(self):
        for contract in (
            self.automation_batch_contract,
            self.controller_contract,
            self.reviewer_contract,
        ):
            self.assertIn("归档副本只用于审计记录", contract)
            self.assertIn("内嵌的绝对 `assignment_file`", contract)
            self.assertIn("不得传入 epoch 归档副本", contract)
        self.assertIn(
            "`collect-assignment`、`collection-status` 和 `seal-review`",
            self.reviewer_contract,
        )

    def test_claim_must_close_its_dispatch_handshake(self):
        self.assertIn("领取-派发握手", self.automation_batch_contract)
        self.assertIn("任务系统的创建成功回执", self.automation_batch_contract)
        self.assertIn("不得把 `leased` 等同于任务正在运行", self.automation_batch_contract)
        self.assertIn("role_cli ops recover-stale", self.automation_batch_contract)

    def test_dispatch_slot_limit_recovers_one_lease_before_another_claim(self):
        self.assertIn("每次只允许一个未派发租约", self.automation_batch_contract)
        self.assertIn("agent_thread_limit_reached", self.automation_batch_contract)
        self.assertIn("不得领取第二条", self.automation_batch_contract)
        self.assertIn("不等于平台实际可用槽位", self.automation_batch_contract)
        self.assertIn("容量变化后继续", self.automation_batch_contract)
        self.assertIn("不等待下一小时", self.automation_batch_contract)
        self.assertIn("同一容量快照不得重复探测", self.automation_batch_contract)

    def test_missing_assignment_has_an_exact_recovery_proof_contract(self):
        self.assertIn("assignment_missing", self.automation_batch_contract)
        self.assertIn('`worker_inactive=true`', self.automation_batch_contract)
        self.assertIn('`related_process_inactive=true`', self.automation_batch_contract)
        self.assertIn("完整 assignment JSON", self.automation_batch_contract)
        self.assertIn("不发送缺少 assignment JSON 的空任务", self.automation_batch_contract)

    def test_automation_has_one_operational_control_plane(self):
        self.assertIn("闭环控制面", self.automation_batch_contract)
        self.assertIn("每个自然日只有一个当天控制台", self.automation_batch_contract)
        self.assertIn("每小时创建一个独立可见调度任务", self.automation_batch_contract)
        self.assertIn("当天控制台直接协调", self.automation_batch_contract)
        self.assertIn("不再为每个流程创建可见批次任务", self.automation_batch_contract)
        self.assertIn("用户指定的 Admin", self.automation_batch_contract)
        self.assertIn("队列状态机是唯一运行事实", self.automation_batch_contract)
        self.assertIn("automation memory 只记录", self.automation_batch_contract)

    def test_monitor_events_have_a_resolution_receipt(self):
        self.assertIn("OPS_EVENT|", self.automation_batch_contract)
        self.assertIn("OPS_RESOLUTION|", self.automation_batch_contract)
        self.assertIn("send_message_to_thread", self.automation_batch_contract)
        self.assertIn("恢复后进度验证", self.automation_batch_contract)
        self.assertIn("事件驱动监控", self.automation_batch_contract)

    def test_repair_authority_has_an_explicit_human_gate(self):
        self.assertIn("可自动修复", self.automation_batch_contract)
        self.assertIn("必须人工确认", self.automation_batch_contract)
        self.assertIn("恢复 automation", self.automation_batch_contract)
        self.assertIn("不得自动修改财务规则", self.automation_batch_contract)

    def test_finance_rule_changes_use_a_controlled_cutover(self):
        self.assertIn("逻辑切换门禁", self.automation_batch_contract)
        self.assertIn("policy_change_pending", self.automation_batch_contract)
        self.assertIn("旧冻结意见", self.automation_batch_contract)
        self.assertIn("重新取证", self.automation_batch_contract)

    def test_hourly_trigger_uses_one_daily_controller_and_fresh_epoch(self):
        self.assertIn("项目级 hourly cron", self.automation_batch_contract)
        self.assertIn("每小时一个独立可见调度任务", self.automation_batch_contract)
        self.assertIn("当天控制台", self.automation_batch_contract)
        self.assertIn("不得继续复用前一日控制台", self.automation_batch_contract)
        self.assertIn("同一控制台内建立或合并 fresh epoch", self.automation_batch_contract)

    def test_hourly_trigger_refreshes_live_candidates_before_empty_decision(self):
        self.assertIn("每次小时触发都必须先刷新实时候选交集", self.automation_batch_contract)
        self.assertIn("旧 `actionable=0` 不能作为跳过刷新", self.automation_batch_contract)
        self.assertIn("controller_discovery_unavailable", self.automation_batch_contract)
        self.assertIn("不得报告“队列为空”", self.automation_batch_contract)

    def test_fresh_agent_gets_contract_hashes_and_assignment_atomically(self):
        self.assertIn("角色级 release manifest", self.automation_batch_contract)
        self.assertIn("完整 assignment JSON", self.automation_batch_contract)
        self.assertIn("最终 `ITEM_RESULT` 回传", self.automation_batch_contract)
        self.assertIn("dispatcher/controller 文档变更不得使已派发审核 assignment 失效", self.automation_batch_contract)
        self.assertIn("取消旧常驻 worker 的 `CONTROL/CONTROL_ACK` 往返", self.automation_batch_contract)

    def test_each_item_uses_a_fresh_isolated_reviewer(self):
        self.assertIn('fork_turns="none"', self.automation_batch_contract)
        self.assertIn("同一审核对话不得处理两个 requestId", self.automation_batch_contract)
        self.assertIn("不得在完成一单后领取第二单", self.automation_batch_contract)
        self.assertIn("观察子 Agent 不得接触其他单据", self.automation_batch_contract)

    def test_model_escalation_is_bounded_and_reuses_frozen_material(self):
        self.assertIn("`gpt-5.6-terra`", self.skill)
        self.assertIn("`reasoning_effort=high`", self.skill)
        self.assertIn("升级一次 `gpt-5.6-sol`", self.skill)
        self.assertIn("同一份冻结材料", self.skill)
        self.assertIn("不得重新下载、重新采集", self.skill)

    def test_admin_is_runtime_selected_without_new_code_acl(self):
        self.assertIn("Admin 由用户", self.automation_batch_contract)
        self.assertIn("不得硬编码 Admin threadId", self.automation_batch_contract)
        self.assertIn("不新增 Admin 授权数据库", self.automation_batch_contract)
        self.assertIn("超时、权限未知、查询失败", self.automation_batch_contract)
        self.assertIn("不得触发替换", self.automation_batch_contract)

    def test_daily_handoff_is_sanitized_and_acknowledged_before_archive(self):
        self.assertIn("DAILY_HANDOFF|", self.automation_batch_contract)
        self.assertIn("DAILY_HANDOFF_ACK", self.automation_batch_contract)
        self.assertIn("才归档前一日控制台", self.automation_batch_contract)
        self.assertIn("不得依赖压缩后的旧控制台对话", self.automation_batch_contract)
        self.assertIn("不含人员、公司、附件、意见正文", self.automation_batch_contract)

    def test_next_hourly_task_reconciles_registered_threads_without_hiding_events(self):
        self.assertIn("ARCHIVE_PENDING|", self.automation_batch_contract)
        self.assertIn("`set_thread_archived(archived=true)`", self.automation_batch_contract)
        self.assertIn("OPEN_OP|", self.automation_batch_contract)
        self.assertIn("异常持久化先于归档", self.automation_batch_contract)

    def test_archive_registry_does_not_depend_on_ui_discovery_window(self):
        self.assertIn("THREAD_REGISTER|", self.automation_batch_contract)
        self.assertIn("精确 `thread_id`", self.automation_batch_contract)
        self.assertIn("不受日期或最近 50 条窗口限制", self.automation_batch_contract)
        self.assertIn("不以 `idle` 或 `notLoaded`", self.automation_batch_contract)

    def test_non_emergency_skill_release_waits_for_a_fresh_epoch(self):
        self.assertIn("PENDING_SKILL_RELEASE", self.automation_batch_contract)
        self.assertIn("继续按已加载指纹自然闭合", self.automation_batch_contract)
        self.assertIn("确认后停止新增 claim", self.automation_batch_contract)
        self.assertIn("立即创建后继 fresh epoch", self.automation_batch_contract)
        self.assertIn("EMERGENCY_SKILL_RELEASE", self.automation_batch_contract)

    def test_epoch_receipt_exposes_productive_and_control_plane_time(self):
        for field in (
            "productive_submit_window_seconds=",
            "control_pause_seconds=",
            "reviewer_slots_peak=",
            "reviewer_slots_available=",
        ):
            self.assertIn(field, self.automation_batch_contract)

    def test_each_item_and_epoch_expose_sanitized_latency_metrics(self):
        for field in (
            "collection_seconds=",
            "observation_seconds=",
            "judgment_seconds=",
            "preparation_total_seconds=",
            "prepared_wait_seconds=",
            "writer_total_seconds=",
            "attempt_end_to_end_seconds=",
        ):
            self.assertIn(field, self.automation_batch_contract)
        for field in (
            "epoch_elapsed_seconds=",
            "items_timed=",
            "avg_attempt_seconds=",
            "p95_attempt_seconds=",
            "flow_timing=",
            "capacity_probe_attempts=",
            "capacity_rejections=",
            "observer_slots_peak=",
        ):
            self.assertIn(field, self.automation_batch_contract)
        self.assertIn("ITEM_TIMING|", self.automation_batch_contract)
        self.assertIn("使用单调时钟", self.automation_batch_contract)
        self.assertIn("oldest_actionable_age_seconds", self.controller_contract)
        self.assertIn("queue_age_p95_seconds", self.controller_contract)
        self.assertIn("contiguous_prepared_depth", self.controller_contract)

    def test_backlog_reserves_reviewers_before_visual_observers(self):
        self.assertIn("积压达到 20 条", self.automation_batch_contract)
        self.assertIn("差旅和非差旅财务两个保留槽", self.automation_batch_contract)
        self.assertIn("等待最久且缓冲不足的通道", self.automation_batch_contract)
        self.assertIn("只有命中风险条件的独立复核", self.automation_batch_contract)
        self.assertIn("任一审核或观察 Agent 状态变化", self.automation_batch_contract)
        self.assertIn("重新计算三类通道", self.automation_batch_contract)
        self.assertIn("不得跨快照沿用单槽结论", self.automation_batch_contract)

    def test_reviewer_capacity_is_anchored_to_actually_running_agents(self):
        self.assertIn("实际处于 running", self.automation_batch_contract)
        self.assertIn("completed、idle", self.automation_batch_contract)
        self.assertIn("未创建成功的 leased", self.automation_batch_contract)
        self.assertIn("均不得计入活动审核槽", self.automation_batch_contract)
        self.assertIn("持续补池到 3 个", self.automation_batch_contract)
        self.assertIn("只终止当前一次创建", self.automation_batch_contract)
        self.assertIn("不得冻结本 epoch 的后续补池", self.automation_batch_contract)

    def test_throughput_is_a_primary_skill_goal_not_a_control_plane_side_effect(self):
        self.assertIn("快速、准确、持续排空", self.skill)
        self.assertIn("不能把控制面动作变成审批路障", self.skill)
        self.assertIn("时效指标缺失不得阻断", self.automation_batch_contract)
        self.assertIn("归档失败不得阻断", self.automation_batch_contract)
        self.assertIn("观察子 Agent 不可用", self.automation_batch_contract)
        self.assertIn("当前审核 Agent 直接完成", self.automation_batch_contract)

    def test_controller_rolls_over_when_its_native_agent_roster_is_exhausted(self):
        self.assertIn("同一时刻只有一个活动控制台", self.automation_batch_contract)
        self.assertIn("controller_roster_exhausted", self.automation_batch_contract)
        self.assertIn("pending_init", self.automation_batch_contract)
        self.assertIn("DAILY_CONTROLLER_ROLLOVER", self.automation_batch_contract)
        self.assertIn("DAILY_CONTROLLER_ROLLOVER_ACK", self.automation_batch_contract)
        self.assertIn("先更新精确控制台指针", self.automation_batch_contract)
        self.assertIn("不得同时 claim", self.automation_batch_contract)

    def test_write_reconciliation_uses_the_exact_receipt_run_not_a_recent_row_window(self):
        self.assertIn("`request_id + submission_token + run_id`", self.automation_batch_contract)
        self.assertIn("最近 N 条", self.automation_batch_contract)
        self.assertIn("不得据此声明历史写入为 0", self.automation_batch_contract)

    def test_writer_reconciliation_binds_one_process_and_receipt_to_one_token(self):
        self.assertIn("每个 submission token 只能对应一个", self.automation_batch_contract)
        self.assertIn("一个终态回执", self.automation_batch_contract)
        self.assertIn("冲突终态", self.automation_batch_contract)

    def test_postcheck_requires_submit_handled_hit_and_pending_absence(self):
        self.assertIn("提交接口成功", self.automation_batch_contract)
        self.assertIn("已办索引命中当前 requestId", self.automation_batch_contract)
        self.assertIn("当前待办索引不再包含", self.automation_batch_contract)

    def test_actual_write_postcheck_gap_has_exact_external_closure_only(self):
        self.assertIn("resolve-submitting-external", self.automation_batch_contract)
        self.assertIn("actual_write_proof", self.automation_batch_contract)
        self.assertIn("blocked_external", self.automation_batch_contract)
        self.assertIn("不得重试、补写或标记 submitted", self.automation_batch_contract)

    def test_controller_discovery_prefers_exact_pointer_over_listing_or_timeout_guess(self):
        self.assertIn("`list_threads` 的成功空结果", self.automation_batch_contract)
        self.assertIn("`OA财务预审控制台 YYYY-MM-DD`", self.automation_batch_contract)
        self.assertIn("不得用固定 55/60 秒等待代替", self.automation_batch_contract)
        self.assertIn("不得对控制台发现调用 `wait_threads`", self.automation_batch_contract)
        self.assertIn("精确指针", self.automation_batch_contract)
        self.assertIn("一次有界恢复探针", self.automation_batch_contract)
        self.assertIn("`controller_discovery_unavailable`", self.automation_batch_contract)

    def test_controller_creation_preserves_the_top_level_thread_result(self):
        self.assertIn("`create_thread` 的完整顶层返回对象", self.automation_batch_contract)
        self.assertIn("`threadId` 或 `clientThreadId`", self.automation_batch_contract)
        self.assertIn("`result.structuredContent ?? result.content`", self.automation_batch_contract)
        self.assertIn("`set_thread_title`", self.automation_batch_contract)
        self.assertIn("不得更新控制台指针", self.automation_batch_contract)

    def test_preflight_retries_only_the_unknown_control_probe(self):
        self.assertIn("不得调用系统 `timeout` 命令", self.automation_batch_contract)
        self.assertIn("已成功的队列和生产状态探针不得重跑", self.automation_batch_contract)
        self.assertIn("只单独重试未知的控制面探针", self.automation_batch_contract)

    def test_batch_turn_cannot_end_while_actionable_fifo_remains(self):
        self.assertIn("结束门禁", self.controller_contract)
        self.assertIn("`actionable=0`", self.controller_contract)
        self.assertIn("未完成 Goal 自动续回合", self.controller_contract)
        self.assertIn("阶段回执", self.controller_contract)
        self.assertIn("不是 Goal 完成条件", self.controller_contract)

    def test_blocked_controller_goal_requires_sequential_same_day_rollover(self):
        self.assertIn("`get_goal.status=blocked` 不是 unfinished Goal", self.controller_contract)
        self.assertIn(
            "CONTROLLER_ROLLOVER_REQUIRED|failure_code=controller_goal_blocked",
            self.controller_contract,
        )
        self.assertIn("停止新增 claim", self.controller_contract)
        self.assertIn("自然闭合", self.controller_contract)
        self.assertIn("同日控制台顺序换班", self.dispatcher_contract)
        self.assertIn("ACK 前不创建 Goal", self.dispatcher_contract)
        self.assertIn("DAILY_CONTROLLER_ROLLOVER_ACK", self.dispatcher_contract)
        self.assertIn("更新精确控制台指针", self.dispatcher_contract)
        self.assertIn("CONTROL_START", self.dispatcher_contract)
        self.assertIn("第一步创建 fresh unfinished Goal", self.controller_contract)
        self.assertIn("不得先发送 final", self.controller_contract)
        self.assertIn("旧控制台进入归档补偿", self.dispatcher_contract)
        self.assertIn("failure_code=controller_goal_blocked", self.automation_batch_contract)
        self.assertIn("不得在 blocked Goal 下继续 claim", self.automation_batch_contract)
        self.assertIn("ACK 前不得创建 Goal", self.automation_batch_contract)
        self.assertIn("不等待下一小时调度", self.automation_batch_contract)

    def test_skill_routes_each_runtime_role_to_a_small_contract(self):
        for name in (
            "flow-runner-contract.md",
            "reviewer-contract.md",
            "writer-contract.md",
        ):
            self.assertIn(name, self.skill)
        self.assertIn("只读取本角色", self.skill)
        self.assertIn("不进入新自动化上下文", self.skill)

    def test_reviewer_contract_excludes_controller_and_writer_operations(self):
        self.assertIn("collect-assignment", self.reviewer_contract)
        self.assertIn("collection-status", self.reviewer_contract)
        self.assertIn("绝不重新采集", self.reviewer_contract)
        self.assertIn("session_id", self.reviewer_contract)
        self.assertIn("write_stdin", self.reviewer_contract)
        self.assertIn("exit_code", self.reviewer_contract)
        self.assertIn("seal-review", self.reviewer_contract)

        self.assertIn("本回合就禁止发送 final", self.controller_contract)
        self.assertIn("补槽 → 再次等待", self.controller_contract)
        self.assertIn("不得依赖下一小时调度任务", self.controller_contract)
        self.assertIn("role_cli reviewer release-manifest", self.reviewer_contract)
        self.assertIn("不得对单个文件运行 `shasum`", self.reviewer_contract)
        self.assertIn("不手工计算或填写", self.reviewer_contract)
        self.assertIn("--preparation-total-seconds", self.reviewer_contract)
        self.assertIn("不得另设整进程超时", self.reviewer_contract)
        self.assertIn("禁止在项目根执行宽泛 `rg`", self.reviewer_contract)
        self.assertIn("一个 assignment", self.reviewer_contract)
        self.assertNotIn("finish-review --result-bundle", self.reviewer_contract)
        self.assertNotIn("create_thread", self.reviewer_contract)
        self.assertNotIn("set_thread_archived", self.reviewer_contract)
        self.assertNotIn("run-once", self.reviewer_contract)

    def test_dispatcher_and_writer_contracts_have_narrow_authority(self):
        self.assertIn("不得 claim", self.dispatcher_contract)
        self.assertIn("不得读取附件", self.dispatcher_contract)
        self.assertIn("按请求隔离", self.writer_contract)
        self.assertIn("不得执行审核判断", self.writer_contract)
        self.assertIn("role_cli writer release-manifest", self.writer_contract)
        self.assertIn("--project-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal", self.writer_contract)
        self.assertIn("--writer-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal", self.writer_contract)
        self.assertIn("--production-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal", self.writer_contract)
        self.assertNotIn("/Users/synear/.codex/worktrees/c8c1", self.writer_contract)
        self.assertIn("不得从生产 cwd 推导", self.writer_contract)
        self.assertIn("role_cli writer submit-frozen-once", self.writer_contract)
        self.assertNotIn("begin-submit", self.writer_contract.split("canonical 单入口")[0])
        self.assertIn("取得 submission token 后不会返回模型控制权", self.writer_contract)
        self.assertIn("submit-frozen-once", self.automation_batch_contract)

    def test_epoch_receipt_keeps_open_ops_out_of_closed_status(self):
        self.assertIn("`actionable=0` 可以结束调度 Goal", self.automation_batch_contract)
        self.assertIn("`closure=awaiting_ops_resolution`", self.automation_batch_contract)
        self.assertIn("不得声明 `closure=closed`", self.automation_batch_contract)
        self.assertIn("所有 `OPS_EVENT` 都已配对 `OPS_RESOLUTION`", self.automation_batch_contract)
        self.assertIn("恢复后进度验证完成", self.automation_batch_contract)

    def test_epoch_receipt_separates_current_and_carried_queue_metrics(self):
        for field in (
            "blocked_new=",
            "blocked_carried_forward=",
            "requeued_after_repair=",
            "attempts_started=",
        ):
            self.assertIn(field, self.automation_batch_contract)

    def test_generation_transition_is_observable_in_all_runtime_receipts(self):
        for receipt in ("ITEM_RESULT", "submission_receipt", "EPOCH_RECEIPT"):
            self.assertIn(receipt, self.automation_batch_contract)
        self.assertIn("loaded_generation=...", self.automation_batch_contract)
        self.assertIn("latest_generation=...", self.automation_batch_contract)
        self.assertIn("自然闭合旧代际", self.automation_batch_contract)

    def test_generation_change_after_assignment_is_audit_only_not_a_failure(self):
        self.assertIn("只接收 `reviewer` 角色指纹", self.automation_batch_contract)
        self.assertIn("处理中才发现代际变化", self.automation_batch_contract)
        self.assertIn("不得重取证", self.automation_batch_contract)
        self.assertIn("`generation_transition=natural_close_old`", self.automation_batch_contract)

    def test_old_epoch_stops_new_claims_after_a_skill_release(self):
        self.assertIn("每次 claim 前", self.automation_batch_contract)
        self.assertIn("reviewer 指纹或 latest_generation 已变", self.automation_batch_contract)
        self.assertIn("停止新 claim", self.automation_batch_contract)
        self.assertIn("载入新 release 继续", self.automation_batch_contract)
        self.assertIn("不得把新指纹与旧 assignment 混用", self.automation_batch_contract)

    def test_valid_collected_material_reaches_the_canonical_seal_interface(self):
        self.assertIn("preaudit_assignment_material_written", self.reviewer_contract)
        self.assertIn("role_cli reviewer seal-review", self.reviewer_contract)
        self.assertIn("封套、指纹、身份字段和结果 JSON 均由 canonical 闭合命令生成", self.reviewer_contract)
        self.assertIn("不得退回旧的手工结果包闭合路径", self.reviewer_contract)

    def test_failure_matrix_keeps_diagnostic_retry_out_of_one_attempt(self):
        self.assertIn("输出文件缺失", self.automation_batch_contract)
        self.assertIn("合法脱敏 failure.code", self.automation_batch_contract)
        self.assertIn("无合法 code", self.automation_batch_contract)
        self.assertIn("同一 attempt 禁止为补错误码重跑", self.automation_batch_contract)

    def test_collection_waits_for_the_nested_exec_session_to_exit(self):
        self.assertIn("`session_id`", self.reviewer_contract)
        self.assertIn("write_stdin", self.reviewer_contract)
        self.assertIn("明确 `exit_code`", self.reviewer_contract)
        self.assertIn(
            "在此之前不得调用 `collection-status`",
            self.reviewer_contract,
        )

    def test_collection_does_not_publish_the_low_level_collector_interface(self):
        self.assertIn("role_cli reviewer collect-assignment", self.reviewer_contract)
        self.assertIn("不得自行拼接 `collect-case`", self.reviewer_contract)
        self.assertIn("不得手工改队列", self.reviewer_contract)
        self.assertNotIn("queue_cli collect-assignment", self.automation_batch_contract)

    def test_outer_exec_cell_is_resumed_before_collection_is_classified(self):
        self.assertIn("`Script running with cell ID ...`", self.automation_batch_contract)
        self.assertIn("`functions.wait(cell_id=...)`", self.automation_batch_contract)
        self.assertIn("直到包装层明确完成", self.automation_batch_contract)
        self.assertIn("不得把外层 yield 当成采集失败", self.automation_batch_contract)

    def test_collection_writes_the_frozen_material_to_the_assignment_path(self):
        self.assertIn("assignment 指定的 `material_file`", self.reviewer_contract)
        self.assertIn("不得自行拼接 `collect-case`", self.reviewer_contract)
        self.assertIn("或复制 stdout", self.reviewer_contract)

    def test_collection_output_validation_uses_machine_readable_evidence(self):
        self.assertIn("preaudit_assignment_material_written", self.reviewer_contract)
        self.assertIn("同一 attempt 的 `material_file`", self.reviewer_contract)
        self.assertIn("绝不重新采集", self.reviewer_contract)

    def test_small_marketing_remark_uses_the_exact_fixed_item_name(self):
        self.assertIn("固定项目名和顺序必须逐字采用", self.contract)
        self.assertIn("SAP 客户编码匹配", self.small_marketing_contract)

    def test_packaging_flow_uses_four_content_sections_without_regulatory_expansion(self):
        self.assertIn("packaging_layout_approval", self.skill)
        self.assertIn("audit-chinese-image-text", self.packaging_contract)
        self.assertIn("固定四项，顺序不可改变", self.packaging_contract)
        for item_name in (
            "配料信息",
            "过敏原信息",
            "营养成分表信息",
            "地址信息",
        ):
            self.assertIn(item_name, self.packaging_contract)
        self.assertIn("每一版包装图片都必须输出上述四项", self.packaging_contract)
        self.assertIn("与 Excel 不一致", self.packaging_contract)
        self.assertIn("关键文字", self.packaging_contract)
        self.assertIn("默认不纳入全面逐字、语病或文风检查", self.packaging_contract)
        self.assertIn("没有 Excel 时", self.packaging_contract)
        self.assertIn("不判断许可证真伪", self.packaging_contract)
        self.assertIn("错别字、漏字、多字", self.packaging_audit_skill)
        self.assertIn("四块内容", self.packaging_audit_skill)

    def test_packaging_without_excel_still_requires_original_image_visual_closure(self):
        self.assertIn(
            "逐个下载 `review_role=packaging_image` 的原始附件",
            self.packaging_audit_skill,
        )
        self.assertIn("旋转 90°、180° 或 270°", self.packaging_audit_skill)
        self.assertIn("关键文字区域清单", self.packaging_audit_skill)
        self.assertIn(
            "简单清晰版面由包装协调者直接完成覆盖自检",
            self.packaging_audit_skill,
        )
        self.assertIn(
            "不得仅因未提供 Excel 将四项统一标为 `⚠️`",
            self.packaging_contract,
        )
        self.assertIn("关键文字审核路径已闭合", self.packaging_contract)

    def test_packaging_defaults_to_key_text_fast_path(self):
        self.assertIn("关键文字范围", self.packaging_audit_skill)
        self.assertIn("装饰性文字、营销口号", self.packaging_audit_skill)
        self.assertIn("默认快速路径", self.automation_batch_contract)
        self.assertIn("不要求 A/B/C 三路齐全", self.packaging_contract)
        self.assertNotIn("三路盲审齐全数", self.packaging_output_contract)

    def test_packaging_escalates_only_risky_or_uncertain_text(self):
        self.assertIn("低置信、高影响数字或专名、Excel 差异或内部冲突", self.packaging_audit_skill)
        self.assertIn("最多增加一次独立复核", self.packaging_audit_skill)
        self.assertIn("用户明确要求全面逐字校对", self.packaging_audit_skill)
        self.assertIn("才升级为三路盲审", self.packaging_audit_skill)

    def test_packaging_g1_has_a_finite_convergence_rule(self):
        self.assertIn("最多执行一次定向重切", self.packaging_audit_skill)
        self.assertIn("受影响的具体审核项标为 `⚠️`", self.packaging_audit_skill)
        self.assertNotIn("不得因固定尝试次数停止", self.packaging_audit_skill)
        self.assertIn("不得无限重切或无限等待", self.packaging_contract)

    def test_packaging_g1_technical_block_uses_an_exact_reason_code(self):
        for code in (
            "packaging_g1_source_image_incomplete",
            "packaging_g1_lossless_crop_unavailable",
            "packaging_g1_coverage_auditor_unavailable",
        ):
            self.assertIn(code, self.packaging_audit_skill)
            self.assertIn(code, self.packaging_contract)
        self.assertIn(
            "泛化的 `packaging_g1_coverage_failed` 不再允许封存新 attempt",
            self.packaging_contract,
        )

    def test_packaging_output_contract_has_canonical_no_excel_signing_template(self):
        for text in (
            "OA 无 Excel 标准签字意见",
            "包装图片关键文字校对未见错别字、漏字、多字、异常字符或重复印刷",
            "表头、项目、数值、单位及 NRV% 目视校对未见异常",
            "本次未提供 Excel，未执行 Excel 对照",
        ):
            self.assertIn(text, self.packaging_output_contract)

        case_ids = {case["id"] for case in self.regression_data["cases"]}
        self.assertIn("RC-013-packaging-no-excel-blanket-warning", case_ids)
        self.assertIn("RC-015-packaging-full-text-overcontrol", case_ids)
        self.assertIn("F-016", self.feedback)
        self.assertIn("F-018", self.feedback)

    def test_packaging_excel_availability_notice_is_emitted_once_after_four_items(self):
        no_excel_template = self.packaging_output_contract.split(
            "### OA 无 Excel 标准签字意见", 1
        )[1].split("### OA 有 Excel 标准签字意见", 1)[0]

        self.assertEqual(
            no_excel_template.count("本次未提供 Excel，未执行 Excel 对照"),
            1,
        )
        self.assertIn(
            "Excel 对照：本次未提供 Excel，未执行 Excel 对照。",
            no_excel_template,
        )
        self.assertLess(
            no_excel_template.index("✅ 4、地址信息"),
            no_excel_template.index("Excel 对照："),
        )
        self.assertIn("只在四项之后统一说明一次", self.packaging_contract)

        case_ids = {case["id"] for case in self.regression_data["cases"]}
        self.assertIn("RC-014-packaging-excel-notice-repeated", case_ids)
        self.assertIn("F-017", self.feedback)

    def test_packaging_non_domain_text_uses_an_unnumbered_supplement(self):
        self.assertIn("非编号的 `补充校对`", self.packaging_contract)
        self.assertIn("合并到唯一一行非编号的 `补充校对`", self.packaging_contract)
        self.assertIn("全部非四项关键异常合并到唯一一行", self.packaging_contract)
        self.assertIn("多个异常必须合并到这一行", self.packaging_output_contract)
        self.assertIn("不是第五审核项", self.packaging_output_contract)
        self.assertIn("不得漏报或硬塞进错误项目", self.packaging_output_contract)

    def test_packaging_accuracy_contract_overrides_ordinary_slot_target(self):
        for text in (
            "包装关键文字优先",
            "每个包装单据使用一个独立包装审核对话",
            "默认快速路径由协调者直接完成关键文字覆盖自检和主审",
            "不计入普通报销审核槽位上限",
            "平台物理槽位不足时只等待必需复核",
        ):
            self.assertIn(text, self.automation_batch_contract)

    def test_travel_and_nontravel_finance_have_reserved_preparation_lanes(self):
        for text in (
            "差旅保留通道",
            "非差旅财务保留通道",
            "不得被差旅积压占用",
            "包装准确性通道",
            "不同 requestId 可由有界提交池并发",
        ):
            self.assertIn(text, self.automation_batch_contract)
        self.assertIn("--lane", self.controller_contract)
        self.assertIn("packaging_accuracy", self.controller_contract)

    def test_collection_preserves_the_safe_failure_code(self):
        self.assertIn("不得将 stdout 或 stderr 重定向到 `/dev/null`", self.automation_batch_contract)
        self.assertIn("`failure.code`", self.automation_batch_contract)
        self.assertIn("只读取 assignment 绑定的脱敏失败回执", self.automation_batch_contract)
        self.assertIn("同一 attempt 禁止为补错误码重跑", self.automation_batch_contract)

    def test_travel_attachment_count_failure_has_only_safe_diagnostics(self):
        self.assertIn("travel_attachment_resources_incomplete", self.automation_batch_contract)
        for field in (
            "declared_attachment_count",
            "discovered_resource_count",
            "form_resource_count",
            "travel_resource_count",
            "trusted_source_sighting_count",
        ):
            self.assertIn(field, self.automation_batch_contract)
        self.assertIn("五个整数计数", self.automation_batch_contract)

    def test_travel_resource_dedup_uses_stable_attachment_identity(self):
        self.assertIn("稳定附件 ID", self.automation_batch_contract)
        self.assertIn("不同下载 URL", self.automation_batch_contract)
        self.assertIn("trusted_source_sighting_count", self.automation_batch_contract)
        self.assertIn("可信来源记录数少于声明数", self.automation_batch_contract)
        self.assertIn("达到声明数后", self.automation_batch_contract)
        self.assertIn("下载全部唯一资源", self.automation_batch_contract)

    def test_collection_failure_uses_a_separate_safe_receipt_file(self):
        self.assertIn("preaudit_assignment_collection_failed", self.reviewer_contract)
        self.assertIn("collection-failure.json", self.reviewer_contract)
        self.assertIn("绝不重新采集", self.reviewer_contract)

    def test_item_ops_events_preserve_item_identity(self):
        self.assertIn("单据级事件必须包含 request_id", self.automation_batch_contract)
        self.assertIn("不得在转发时删除字段", self.automation_batch_contract)
        self.assertIn("发送前从当前队列项补齐", self.automation_batch_contract)

    def test_historical_block_reconciliation_never_substitutes_an_aggregate_event(self):
        self.assertIn("禁止用 `blocked_count` 的 aggregate 事件代替", self.automation_batch_contract)
        self.assertIn("逐条读取窄队列字段", self.automation_batch_contract)
        self.assertIn("`ops_event_identity_missing` 只表示当前一条", self.automation_batch_contract)

    def test_carried_technical_blocks_are_reconciled_with_admin(self):
        self.assertIn("历史技术阻断重对账", self.automation_batch_contract)
        self.assertIn("仍在实时交集", self.automation_batch_contract)
        self.assertIn("该版本未配对", self.automation_batch_contract)
        self.assertIn("每个 `requestId + received_at + failure_code` 至多补发一次", self.automation_batch_contract)
        self.assertIn("不得自动 requeue", self.automation_batch_contract)

    def test_runtime_errors_do_not_mutate_finance_rules(self):
        self.assertIn("技术超时、登录失败或任务卡住本身不能自动改财务判断规则", self.automation_batch_contract)
        self.assertIn("错误回归", self.automation_batch_contract)
        self.assertIn("用户纠错或明确财务反馈", self.skill)

    def test_mileage_result_requires_raw_readings_before_comparison(self):
        self.assertIn("起始读数、结束读数、单位和计算差值", self.travel_contract)
        self.assertIn("不得拼接不同显示区域中的数字", self.travel_contract)
        self.assertIn("不得直接采用旧 RPA 或 OCR 给出的差值", self.travel_contract)
        self.assertIn("无法稳定辨认时使用 `⚠️`", self.travel_contract)

    def test_flow_number_is_a_first_class_collection_entry(self):
        self.assertIn("--flow-number FLOW_NUMBER", self.skill)
        self.assertIn("精确查询唯一 requestId", self.skill)
        self.assertIn("零条、多条或数据库查询失败", self.skill)
        self.assertIn("不得改用标题、浏览器搜索或候选遍历猜测", self.skill)

    def test_confirmed_conversation_feedback_is_routed_and_recorded(self):
        self.assertIn("已确认反馈清单", self.skill)
        self.assertIn("错误回归案例", self.skill)
        self.assertIn("不得要求重复确认", self.skill)
        self.assertIn("F-004", self.feedback)
        self.assertIn("F-011", self.feedback)
        self.assertIn("F-013", self.feedback)
        self.assertIn("F-014", self.feedback)
        self.assertIn("F-015", self.feedback)
        self.assertIn("不得记录流程编号、requestId", self.feedback)

    def test_regression_cases_preserve_wrong_and_expected_outputs(self):
        self.assertEqual(self.regression_data["schema_version"], 1)
        cases = self.regression_data["cases"]
        self.assertGreaterEqual(len(cases), 10)
        required = {
            "id",
            "scope",
            "audit_item",
            "status",
            "input_facts",
            "incorrect_output",
            "expected_output",
            "failure_mode",
            "rule_ref",
        }
        for case in cases:
            self.assertEqual(set(case), required)
            self.assertEqual(case["status"], "confirmed")
            self.assertTrue(case["input_facts"])
            self.assertTrue(case["incorrect_output"])
            self.assertTrue(case["expected_output"])

        expected_outputs = "\n".join(str(case["expected_output"]) for case in cases)
        self.assertNotIn("OA暂未提供预算数据", expected_outputs)

        case_ids = {case["id"] for case in cases}
        self.assertIn("RC-004-refund-not-netted-from-payment", case_ids)
        self.assertIn("RC-009-multiple-payments-not-summed", case_ids)
        self.assertIn("RC-011-split-details-double-counted-one-payment", case_ids)
        self.assertIn("RC-012-packaging-generic-three-item-template", case_ids)
        serialized = json.dumps(self.regression_data, ensure_ascii=False)
        self.assertIsNone(re.search(r"(?:CLBX|YWZDL)\\d+", serialized))


if __name__ == "__main__":
    unittest.main()
