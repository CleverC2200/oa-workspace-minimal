from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from scripts import quality


@pytest.mark.parametrize('source,scope', [
    ('def test_stop(): pass\ndef test_stop(): pass\n', 'test_stop'),
    ('class TestStop:\n def test_stop(self): pass\n def test_stop(self): pass\n', 'TestStop.test_stop'),
])
def test_test_names_reject_shadowed_discovered_tests(tmp_path, monkeypatch, capsys, source, scope):
    path = tmp_path / 'test_stop.py'
    path.write_text(source)
    monkeypatch.setattr(quality, 'tracked_files', lambda: [path])
    assert quality.main(['test-names']) == 1
    assert f'duplicate_test_name:test_stop.py:{scope}' in capsys.readouterr().out


def test_test_names_accept_fixture_imports_and_separate_scopes(tmp_path):
    path = tmp_path / 'test_stop.py'
    path.write_text('from shared import exchange\n'
                    'def test_stop(exchange): pass\n'
                    'class TestA:\n def test_stop(self): pass\n'
                    'class TestB:\n def test_stop(self): pass\n')
    assert quality.check_test_names([path]) == []


def test_test_names_reject_definition_hidden_inside_control_block(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'test_stop.py'
    path.write_text('def test_stop(): pass\nif True:\n    def test_stop(): pass\n')
    monkeypatch.setattr(quality, 'tracked_files', lambda: [path])
    assert quality.main(['test-names']) == 1
    assert 'duplicate_test_name:test_stop.py:test_stop' in capsys.readouterr().out


def test_test_names_reject_invalid_source_without_importing_it(tmp_path):
    path = tmp_path / 'test_stop.py'
    path.write_text('def test_stop(:\n')
    assert quality.check_test_names([path]) == ['test_source_invalid:test_stop.py']


def test_payment_rule_ownership_rejects_real_pr405_worker_instruction(tmp_path):
    # Verbatim instruction retained from ec9c4a1, before the #424 repair.
    worker = tmp_path / 'codex_review_worker.py'
    worker.write_text('_GENERAL_EXPENSE_REQUIREMENT = (\n'
                      '    "确认名称不一致后按规则核验人员关系和补充材料；"\n)\n')
    findings = quality.check_payment_rule_ownership(worker)
    assert len(findings) == 1
    assert 'payment_relationship_rule_in_worker' in findings[0]
    assert 'payment-screenshot-exemption.md' in findings[0]


def test_payment_rule_ownership_reads_current_worker_without_importing_it():
    assert quality.check_payment_rule_ownership() == []


def test_payment_rule_ownership_detects_delegation_strings_and_reports_syntax(tmp_path):
    worker = tmp_path / 'codex_review_worker.py'
    worker.write_text('prompt = "需要" + "委托收款材料"\n')
    assert 'payment_relationship_rule_in_worker' in quality.check_payment_rule_ownership(worker)[0]
    worker.write_text('prompt = (\n')
    assert 'payment_rule_source_invalid' in quality.check_payment_rule_ownership(worker)[0]


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
