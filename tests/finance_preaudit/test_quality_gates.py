from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from scripts import quality


class TestQualityGate:
    def test_tracked_files_include_new_files_and_skip_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=root,
                check=True,
            )
            deleted = root / "deleted.txt"
            deleted.write_text("tracked\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "deleted.txt"],
                cwd=root,
                check=True,
            )
            deleted.unlink()
            added = root / "added.txt"
            added.write_text("new\n", encoding="utf-8")

            files = quality.tracked_files(root)

        assert files == [added]

    def test_current_skill_format_links_and_hygiene_pass(self) -> None:
        files = quality.tracked_files()
        assert quality.check_skill() == []
        assert quality.check_format(files) == []
        assert quality.check_markdown_links(files) == []
        assert quality.check_hygiene(files) == []

    def test_fake_secret_and_tracked_database_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "settings.txt"
            secret.write_text(
                "GITHUB_TOKEN=" + "ghp_" + "A" * 40 + "\n", encoding="utf-8"
            )
            database = root / "runtime.sqlite3"
            database.write_bytes(b"SQLite format 3\x00")

            findings = quality.check_hygiene([secret, database])

        assert any(item.startswith("github_token:") for item in findings)
        assert any(item.startswith("tracked_database:") for item in findings)

    def test_pr_requires_one_issue_validation_and_rollback(self) -> None:
        valid = "## 验证\n全部通过\n\n## 风险与回退\n回退提交\n\nCloses #8\n"
        assert quality.check_pr_body(valid) == []
        assert quality.check_pr_body(valid.replace("Closes #8", "Refs #8")) == []
        assert "pr_requires_one_issue" in quality.check_pr_body(valid + "Closes #9\n")
        assert "pr_risk_missing" in quality.check_pr_body("## 验证\n失败\nCloses #8\n")

    def test_broken_local_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "README.md"
            document.write_text("[missing](docs/missing.md)\n", encoding="utf-8")
            findings = quality.check_markdown_links([document], root)
        assert findings == ["missing_link:README.md:docs/missing.md"]
