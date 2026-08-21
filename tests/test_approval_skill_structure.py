from __future__ import annotations

import json
import unittest
from pathlib import Path


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


class ApprovalSkillStructureTest(unittest.TestCase):
    def test_all_approval_skills_share_closed_loop_phases(self) -> None:
        for name, skill_dir in APPROVAL_SKILLS.items():
            with self.subTest(skill=name):
                content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                positions = [content.index(phase) for phase in PHASES]
                self.assertEqual(positions, sorted(positions))

    def test_all_approval_skills_have_closed_loop_references(self) -> None:
        for name, skill_dir in APPROVAL_SKILLS.items():
            with self.subTest(skill=name):
                references = skill_dir / "references"
                self.assertTrue(REFERENCE_FILES.issubset({path.name for path in references.iterdir()}))
                cases = json.loads(
                    (references / "regression-cases.json").read_text(encoding="utf-8")
                )
                self.assertEqual(cases["schema_version"], 1)
                self.assertIsInstance(cases["cases"], list)

    def test_effect_and_postcheck_are_distinct(self) -> None:
        finance = (APPROVAL_SKILLS["oa-finance-preaudit"] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        sync = (
            APPROVAL_SKILLS["lark-sync-oa-approval-status"] / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("preview-only", finance)
        self.assertIn("external-write", sync)
        self.assertIn("external_instances/check", sync)


if __name__ == "__main__":
    unittest.main()
