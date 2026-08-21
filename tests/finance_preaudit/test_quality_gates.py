from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts import quality


class QualityGateTests(unittest.TestCase):
    def test_current_skill_format_links_and_hygiene_pass(self) -> None:
        files = quality.tracked_files()
        self.assertEqual(quality.check_skill(), [])
        self.assertEqual(quality.check_format(files), [])
        self.assertEqual(quality.check_markdown_links(files), [])
        self.assertEqual(quality.check_hygiene(files), [])

    def test_fake_secret_and_tracked_database_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "settings.txt"
            secret.write_text("GITHUB_TOKEN=" + "ghp_" + "A" * 40 + "\n", encoding="utf-8")
            database = root / "runtime.sqlite3"
            database.write_bytes(b"SQLite format 3\x00")

            findings = quality.check_hygiene([secret, database])

        self.assertTrue(any(item.startswith("github_token:") for item in findings))
        self.assertTrue(any(item.startswith("tracked_database:") for item in findings))

    def test_pr_requires_one_issue_validation_and_rollback(self) -> None:
        valid = "## 验证\n全部通过\n\n## 风险与回退\n回退提交\n\nCloses #8\n"
        self.assertEqual(quality.check_pr_body(valid), [])
        self.assertEqual(
            quality.check_pr_body(valid.replace("Closes #8", "Refs #8")),
            [],
        )
        self.assertIn("pr_requires_one_issue", quality.check_pr_body(valid + "Closes #9\n"))
        self.assertIn("pr_risk_missing", quality.check_pr_body("## 验证\n失败\nCloses #8\n"))

    def test_broken_local_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "README.md"
            document.write_text("[missing](docs/missing.md)\n", encoding="utf-8")
            findings = quality.check_markdown_links([document], root)
        self.assertEqual(findings, ["missing_link:README.md:docs/missing.md"])


if __name__ == "__main__":
    unittest.main()
