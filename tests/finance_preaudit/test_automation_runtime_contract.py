from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class AutomationRuntimeContractTests(unittest.TestCase):
    def test_project_automation_uses_noninteractive_full_access(self) -> None:
        config_path = ROOT / ".codex" / "config.toml"
        config = config_path.read_text(encoding="utf-8")

        self.assertIn('default_permissions = ":danger-full-access"', config)
        self.assertIn('approval_policy = "never"', config)
        self.assertNotIn("sandbox_mode", config)
        self.assertNotIn("[sandbox_workspace_write]", config)

    def test_batch_contract_separates_coordination_from_oa_writes(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("控制与写入分离", contract)
        self.assertIn("窄 `frozen_writer submit` 是唯一 OA 写入入口", contract)
        self.assertIn("审核 Agent 可以并行", contract)
        self.assertIn("不同 requestId 可进入有界提交池", contract)
        self.assertIn("同一 requestId 始终单飞", contract)
        self.assertIn("不新增逐命令授权", contract)
        self.assertIn("不得反复弹出授权", contract)
        self.assertIn("在领取新租约前报告", contract)
        self.assertNotIn("唯一调度/写入任务", contract)

    def test_hourly_cron_uses_one_visible_launcher_per_run_and_exact_registry(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("每小时一个独立可见调度任务", contract)
        self.assertIn("THREAD_REGISTER|role=hourly_launcher", contract)
        self.assertIn("HOURLY_DISPATCH_RECEIPT|", contract)
        self.assertIn("THREAD_REGISTER|", contract)
        self.assertIn("精确 `thread_id`", contract)
        self.assertIn("`set_thread_archived(archived=true)`", contract)
        self.assertIn("`OA财务预审控制台 YYYY-MM-DD`", contract)
        self.assertIn("不受日期或最近 50 条窗口限制", contract)
        self.assertNotIn("一个长期固定调度入口", contract)
        self.assertNotIn("固定调度入口为长期任务", contract)

    def test_hourly_launcher_archives_all_registered_closed_launchers(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "dispatcher-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("全部未配对 `ARCHIVE_PENDING`", contract)
        self.assertIn("按登记时间从旧到新", contract)
        self.assertIn("`HOURLY_DISPATCH_RECEIPT`", contract)
        self.assertIn("没有活动写入", contract)
        self.assertIn("`set_thread_archived(archived=true)`", contract)
        self.assertIn("当前小时调度任务", contract)

    def test_hourly_launcher_failure_still_has_an_archivable_terminal_receipt(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "dispatcher-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`dispatch=not_sent`", contract)
        self.assertIn("`write_state=confirmed_no_write`", contract)
        self.assertIn("仍须生成 `HOURLY_DISPATCH_RECEIPT`", contract)
        self.assertIn("随后登记 `ARCHIVE_PENDING`", contract)
        self.assertIn("标题设置失败不得阻断", contract)

    def test_dispatcher_can_deliver_to_the_registered_exact_controller_pointer(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "dispatcher-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("已登记的当日精确控制台指针", contract)
        self.assertIn("直接调用 `send_message_to_thread`", contract)
        self.assertIn("返回的 `threadId`", contract)
        self.assertIn("不得重复创建控制台", contract)

    def test_hourly_dispatcher_starts_an_acked_but_not_started_controller(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "dispatcher-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`DAILY_HANDOFF_ACK`", contract)
        self.assertIn("`CONTROL_START_REQUIRED`", contract)
        self.assertIn("只发送一次 `CONTROL_START`", contract)
        self.assertIn("本次不得改发 `HOURLY_WAKEUP`", contract)
        self.assertIn("不得等待下一小时", contract)
        self.assertIn("顶层状态为 `active`", contract)
        self.assertIn("当前存在 `inProgress` 回合", contract)
        self.assertIn("不得再次发送 `CONTROL_START`", contract)
        self.assertIn("`ROLE_RELEASE` 回合只返回 `ROLE_RELEASE_ACK`", contract)
        self.assertIn("不得顺带执行调度动作", contract)

    def test_dispatcher_uses_exact_top_level_identity_when_turn_read_is_unavailable(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "dispatcher-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("精确登记 `thread_id`", contract)
        self.assertIn("顶层任务状态足以发送 `HOURLY_WAKEUP`", contract)
        self.assertIn("`CONTROL_START_REQUIRED` 后立即补发一次 `CONTROL_START`", contract)
        self.assertIn("不得记录 `controller_discovery_unavailable`", contract)

    def test_exact_controller_identity_accepts_idle_and_not_loaded_statuses(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("顶层 `thread.id`", contract)
        self.assertIn("与登记的精确 `thread_id` 完全一致", contract)
        self.assertIn("`idle` 和 `notLoaded` 都是有效状态", contract)
        self.assertIn("不得据此记录 `controller_discovery_unavailable`", contract)

    def test_controller_read_failure_has_one_exact_id_recovery_probe(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("按精确 `thread_id` 匹配", contract)
        self.assertIn("一次 `list_threads(limit=50)`", contract)
        self.assertIn("不得按标题或新旧顺序替换指针", contract)
        self.assertIn("匹配成功后立即发送 `CONTROL_START` 或 `HOURLY_WAKEUP`", contract)

    def test_archive_reconciliation_is_durable_and_independent(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("ARCHIVE_PENDING|", contract)
        self.assertIn("ARCHIVE_RECEIPT|", contract)
        self.assertIn("OPEN_OP|", contract)
        self.assertIn("归档补偿独立于控制台发现", contract)
        self.assertIn("不以 `idle` 或 `notLoaded`", contract)
        self.assertIn("归档失败不阻断控制台发现", contract)

    def test_native_reviewer_agents_are_not_treated_as_archivable_app_threads(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "automation-batch-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("AGENT_REGISTER|", contract)
        self.assertIn("canonical agent path", contract)
        self.assertIn("不得调用 `set_thread_archived`", contract)
        self.assertIn("只有桌面可见任务", contract)

    def test_controller_uses_native_subagents_for_finance_reviewers_and_writer(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "controller-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("普通财务 Reviewer 和 Writer 都使用原生子 Agent", contract)
        self.assertIn("`spawn_agent`", contract)
        self.assertIn("`wait_agent`", contract)
        self.assertIn("包装关键文字通道例外使用独立可见任务", contract)
        self.assertIn("只有命中风险条件时才用原生子 Agent 做一次独立复核", contract)
        self.assertIn("不得用 `create_thread` 创建普通财务 Reviewer 或 Writer", contract)

    def test_controller_refills_all_required_lanes_before_waiting(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "controller-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("进入第一次等待前补满", contract)
        self.assertIn("`active_workers < desired_workers`", contract)
        self.assertIn("不得在只领取一条后提前等待", contract)
        self.assertIn("连续两个不同快照", contract)

    def test_controller_checkpoint_is_gated_by_runtime_snapshot(self) -> None:
        contract = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "controller-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`refresh_checkpoint.due=true`", contract)
        self.assertIn("下一次 claim 前", contract)
        self.assertIn("完整刷新双源", contract)

    def test_controller_reports_blocked_refreshed_intersection_without_calling_it_empty(self) -> None:
        controller = (
            ROOT
            / ".agents"
            / "skills"
            / "oa-finance-preaudit"
            / "references"
            / "controller-contract.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`last_refreshed_intersection`", controller)
        self.assertIn("紧跟本轮双源成功刷新", controller)
        self.assertIn("`actionable`", controller)
        self.assertIn("`blocked`", controller)
        self.assertIn("`candidate_state`", controller)
        self.assertIn("`candidate_state=all_candidates_blocked`", controller)
        self.assertIn("不得表述为“实时双源交集为空”", controller)


if __name__ == "__main__":
    unittest.main()
