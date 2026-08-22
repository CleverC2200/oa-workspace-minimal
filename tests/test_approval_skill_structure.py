from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import collect_case as public_collect_case
from apps.oa_finance_preaudit.case_bundle import collect_case

ROOT = Path(__file__).resolve().parents[1]
APPROVAL_SKILLS = {
    "oa-finance-preaudit": ROOT / ".agents" / "skills" / "oa-finance-preaudit",
    "lark-sync-oa-approval-status": ROOT
    / ".agents"
    / "skills"
    / "lark-sync-oa-approval-status",
}
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
    def test_finance_preaudit_keeps_public_collect_case_import(self) -> None:
        assert public_collect_case is collect_case

    @pytest.mark.parametrize("name,skill_dir", APPROVAL_SKILLS.items())
    def test_all_approval_skills_share_closed_loop_phases(
        self, name: str, skill_dir: Path
    ) -> None:
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        positions = [content.index(phase) for phase in PHASES]
        assert positions == sorted(positions), name

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
        finance = (APPROVAL_SKILLS["oa-finance-preaudit"] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        sync = (APPROVAL_SKILLS["lark-sync-oa-approval-status"] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "preview-only" in finance
        assert "external-write" in sync
        assert "external_instances/check" in sync
