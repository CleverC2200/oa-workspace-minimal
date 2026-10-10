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


def test_yaml_rejects_duplicate_keys_and_keeps_actions_on(tmp_path):
    valid = tmp_path / 'quality.yml'
    valid.write_text('name: checks\non: [push]\njobs: {}\n')
    assert quality.check_yaml([valid]) == []
    valid.write_text('name: first\nname: second\n')
    assert quality.check_yaml([valid]) == ['yaml_invalid:quality.yml']
    valid.write_text('name: [broken\n')
    assert quality.check_yaml([valid]) == ['yaml_invalid:quality.yml']


def test_issue_form_fixed_contract_and_unique_ids(tmp_path):
    import yaml
    path = tmp_path / 'ISSUE_TEMPLATE' / 'bug.yml'; path.parent.mkdir()
    form = {'name': 'Bug', 'description': 'Record a bug', 'body': [
        {'type': 'textarea', 'id': 'impact', 'attributes': {'label': 'Impact'}, 'validations': {'required': True}},
        {'type': 'checkboxes', 'id': 'privacy', 'attributes': {'label': 'Privacy',
         'options': [{'label': 'Redacted', 'required': True}]}},
    ]}
    path.write_text(yaml.safe_dump(form))
    assert quality.check_yaml([path]) == []
    form['body'][1]['id'] = 'impact'
    path.write_text(yaml.safe_dump(form))
    assert quality.check_yaml([path]) == ['issue_form_invalid:bug.yml']
    form['body'][1]['id'] = 'privacy'
    form['body'][0]['validations']['required'] = 'true'
    path.write_text(yaml.safe_dump(form))
    assert quality.check_yaml([path]) == ['issue_form_invalid:bug.yml']


def test_current_yaml_and_generated_review_schemas_pass():
    assert quality.check_yaml(quality.tracked_files()) == []
    assert quality.check_review_schemas() == []


def test_bad_generated_schema_reports_each_workflow(monkeypatch):
    from apps.oa_finance_preaudit import codex_review_worker
    monkeypatch.setattr(codex_review_worker, '_decision_schema', lambda *_args, **_kwargs: {'type': 'bad-type'})
    assert quality.check_review_schemas() == [f'review_schema_invalid:{key}' for key in codex_review_worker._FINANCE_RULES]


def test_issue_form_unhashable_type_is_a_finding():
    for kind in ([], {}):
        assert quality.check_issue_form({'name': 'Bug', 'description': 'Bug', 'body': [
            {'type': kind, 'attributes': {'label': 'Impact'}}]}, 'bug.yml') == ['issue_form_invalid:bug.yml']


def test_review_process_rejects_the_real_past_bypasses(tmp_path):
    # Offending expressions copied unchanged from 43a551e6c3dafc5d2161b400db0dab58d37425ba.
    # Keep the regression runnable after a squash or a checkout without that old Git object.
    past = {
        "apps/oa_finance_preaudit/review_comparison.py": """
class _ComparisonCaller:
    def _side_expectations(self, root):
        report = batch.build_report(root)
    def _candidate_sources(self, plan, candidate):
        _terminal(plan, 'candidate', batch.build_report(candidate))
    def _baseline_quality(row, path, report):
        rebuilt = flow_evals.build_financial_quality_report([{
            'item_key': report['financial_binding']['item_key'],
            'archive': _ref(Path(_read(row['plan'])['arms']['baseline']) / 'manifest.json'),
            'checklist': refs['checklist'], 'assessment': refs['assessment'],
            'expectations': refs['expectations']}])
    def final_financial_qualification(self, reviews):
        twenty = flow_evals.build_financial_quality_report(list(reviews.values()))
        cohort = flow_evals.build_financial_quality_report(list(reviews.values()))
""",
        "apps/oa_finance_preaudit/review_references.py": """
def original_record(refs):
    rebuilt = batch.build_report(root)
    rebuilt = flow_evals.build_financial_quality_report([{
        "item_key": case["item_key"], "archive": manifest_ref,
        "checklist": refs["checklist"], "assessment": refs["assessment"],
        "expectations": refs["freeze"],
    }])
""",
        "scripts/review_experiment_host.py": """
result = {"output_files": {str(p): digest(p.read_bytes()) for p in audit.rglob("*") if p.is_file()}}
""",
    }
    for name, original in past.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original)

    findings = quality.check_review_process(tmp_path)

    assert len(findings) == 8
    assert sum(item.startswith("review_report_bypass:") for item in findings) == 3
    assert sum(item.startswith("review_quality_reader_missing:") for item in findings) == 4
    assert sum(item.startswith("review_output_recursive:") for item in findings) == 1
    assert all("use " in item or "pass " in item for item in findings)


def test_review_process_current_sources_and_ci_command_pass():
    assert quality.check_review_process() == []
    workflow = (quality.PROJECT_ROOT / ".github/workflows/quality.yml").read_text()
    assert "run: python -m scripts.quality review-process" in workflow
    assert quality.main(["review-process"]) == 0
