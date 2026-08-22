from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import FlowEngine
from apps.oa_finance_preaudit.flow_engine import FlowEngine as DefinedFlowEngine

ROOT = Path(__file__).resolve().parents[1]
FINANCE_SKILL = ROOT / ".agents" / "skills" / "oa-finance-preaudit"
SYNC_SKILL = ROOT / ".agents" / "skills" / "lark-sync-oa-approval-status"
APPROVAL_SKILLS = {"oa-finance-preaudit": FINANCE_SKILL, "lark-sync": SYNC_SKILL}
PHASES = [
    "## 0. 目标门禁",
    "## 1. 输入与证据",
    "## 2. 计划或判断",
    "## 3. 执行效果",
    "## 4. 后置验证与回执",
    "## 5. 反馈沉淀",
    "## 6. 错误回归",
    "## 7. 技术失败",
]
REFERENCE_FILES = {
    "result-contract.md",
    "error-handling.md",
    "confirmed-feedback.md",
    "regression-cases.json",
}


class TestApprovalSkillStructure:
    def test_finance_preaudit_exports_only_the_deep_module(self) -> None:
        assert FlowEngine is DefinedFlowEngine

    def test_sync_skill_keeps_the_generic_closed_loop_phases(self) -> None:
        content = (SYNC_SKILL / "SKILL.md").read_text(encoding="utf-8")
        positions = [content.index(phase) for phase in PHASES]
        assert positions == sorted(positions)

    def test_finance_skill_keeps_only_the_four_step_deep_module_loop(self) -> None:
        content = (FINANCE_SKILL / "SKILL.md").read_text(encoding="utf-8")
        phases = (
            "## 1. 取得当前动作",
            "## 2. 读取证据",
            "## 3. 形成结构化决定",
            "## 4. 完成审核",
        )
        positions = [content.index(phase) for phase in phases]
        assert positions == sorted(positions)

    @pytest.mark.parametrize("name,skill_dir", APPROVAL_SKILLS.items())
    def test_all_approval_skills_have_closed_loop_references(
        self, name: str, skill_dir: Path
    ) -> None:
        references = skill_dir / "references"
        assert REFERENCE_FILES.issubset({path.name for path in references.iterdir()}), (
            name
        )
        cases = json.loads(
            (references / "regression-cases.json").read_text(encoding="utf-8")
        )
        assert cases["schema_version"] == 1
        assert isinstance(cases["cases"], list)

    def test_effect_and_postcheck_are_distinct(self) -> None:
        finance = (FINANCE_SKILL / "SKILL.md").read_text(encoding="utf-8")
        sync = (SYNC_SKILL / "SKILL.md").read_text(encoding="utf-8")
        assert "preview-only" in finance
        assert "external-write" in sync
        assert "external_instances/check" in sync
