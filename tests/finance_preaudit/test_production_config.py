from __future__ import annotations

from scripts import quality
from apps.oa_finance_preaudit import production_config
import pytest


@pytest.fixture
def repository(tmp_path):
    for relative in (
        "config/automations/oa-skill-prompt.txt", "docs/runbooks/hourly-production.md",
        "apps/oa_finance_preaudit/production_config.py",
        "apps/oa_finance_preaudit/review_failure_evidence.py",
        "apps/oa_finance_preaudit/codex_review_worker.py",
        "scripts/finance_preaudit_concurrency_probe.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((quality.PROJECT_ROOT / relative).read_bytes())
    return tmp_path


def test_production_configuration_quality_cli_accepts_current_repository(capsys):
    assert quality.main(["production-config"]) == 0
    assert capsys.readouterr().out == "production-config: ok\n"


def test_source_default_model_drift_is_rejected_without_execution(repository):
    path = repository / "apps/oa_finance_preaudit/codex_review_worker.py"
    path.write_text(path.read_text().replace(
        "MODEL = review_failure_evidence.MODEL", 'MODEL = "deepseek-v4-flash"',
    ) + '\nraise RuntimeError("must never execute target source")\n')
    assert "production_config_reviewer_drift" in production_config.repository_findings(repository)


def test_native_automation_shape_preserves_project_and_schedule():
    prompt = (quality.PROJECT_ROOT / "config/automations/oa-skill-prompt.txt").read_text()
    config = {"model": "gpt-6.1-sol", "reasoning_effort": "medium", "prompt": prompt,
              "rrule": "original-schedule", "kind": "cron", "execution_environment": "local",
              "target": {"type": "project", "project_id": "production-project"}}
    assert production_config.automation_findings(
        config, production_project_id="production-project", expected_schedule="original-schedule",
    ) == []


def test_original_native_prompt_wording_is_supported():
    prompt = (quality.PROJECT_ROOT / "config/automations/oa-skill-prompt.txt").read_text().replace(
        "审核默认 model=gpt-6.1-sol reasoning_effort=medium model_provider=openai service_tier=default。",
        "审核默认使用已发布代码中的原生 OpenAI gpt-6.1-sol，推理 medium，service_tier=default，不开 Fast/priority 加速。",
    )
    config = {"model": "gpt-6.1-sol", "reasoning_effort": "medium", "prompt": prompt,
              "rrule": "original-schedule", "kind": "cron", "execution_environment": "local",
              "target": {"type": "project", "project_id": "production-project"}}
    assert production_config.automation_findings(
        config, production_project_id="production-project", expected_schedule="original-schedule",
    ) == []


@pytest.mark.parametrize("relative,before,after,code", [
    ("config/automations/oa-skill-prompt.txt", "--capacity 3", "--capacity 4", "production_config_capacity_drift:template"),
    ("config/automations/oa-skill-prompt.txt", "--max-capacity 6", "--max-capacity 12", "production_config_capacity_drift:template"),
    ("docs/runbooks/hourly-production.md", "service_tier=default", "service_tier=priority", "production_config_service_tier_drift:runbook"),
    ("config/automations/oa-skill-prompt.txt", "--workflow general_expense", "--workflow packaging_layout_approval", "production_config_workflows_drift:template"),
    ("config/automations/oa-skill-prompt.txt", "--confirm-write YES", "--read-only", "production_config_command_invalid:template"),
    ("apps/oa_finance_preaudit/codex_review_worker.py", 'f\'model_reasoning_effort="{REASONING_EFFORT}"\'', '\'model_reasoning_effort="high"\'', "production_config_native_settings_drift"),
])
def test_repository_drift_is_rejected_at_quality_seam(repository, monkeypatch, capsys, relative, before, after, code):
    path = repository / relative
    original = path.read_text()
    assert before in original
    path.write_text(original.replace(before, after))
    monkeypatch.setattr(quality, "PROJECT_ROOT", repository)
    assert quality.main(["production-config"]) == 1
    assert code in capsys.readouterr().out.splitlines()


def test_obsolete_active_provider_instruction_is_rejected(repository):
    path = repository / "docs/runbooks/hourly-production.md"
    path.write_text(path.read_text() + "\n业务审核仍使用独立 DeepSeek。\n")
    assert "production_config_provider_instruction_drift:runbook" in production_config.repository_findings(repository)


def test_public_snapshot_matches_authorized_production_settings():
    assert production_config.snapshot() == {
        "model": "gpt-6.1-sol", "reasoning_effort": "medium", "model_provider": "openai",
        "requested_service_tier": "default", "initial_capacity": 3, "max_capacity": 6,
        "workflows": ["travel_expense", "business_entertainment", "business_support_fee",
                      "small_marketing_fee", "tpm_small_marketing_fee", "general_expense"],
    }


@pytest.mark.parametrize("key,value,code", [
    ("model", "deepseek-v4-flash", "production_config_automation_model_drift"),
    ("reasoning_effort", "high", "production_config_automation_model_drift"),
    ("rrule", "changed-schedule", "production_config_automation_schedule_drift"),
    ("target", {"type": "project", "project_id": "development-project"}, "production_config_automation_target_drift"),
    ("execution_environment", "cloud", "production_config_automation_target_drift"),
])
def test_automation_drift_returns_only_stable_codes(key, value, code):
    config = {"model": "gpt-6.1-sol", "reasoning_effort": "medium",
              "prompt": (quality.PROJECT_ROOT / "config/automations/oa-skill-prompt.txt").read_text(),
              "rrule": "original-schedule", "kind": "cron", "execution_environment": "local",
              "target": {"type": "project", "project_id": "production-project"}}
    config[key] = value
    assert production_config.automation_findings(
        config, production_project_id="production-project", expected_schedule="original-schedule",
    ) == [code]


def test_unused_default_tier_string_cannot_hide_actual_priority_setting(repository):
    path = repository / "apps/oa_finance_preaudit/codex_review_worker.py"
    text = path.read_text().replace("'service_tier=\"default\"',", "'service_tier=\"priority\"',")
    text = text.replace("    native_home = Path", "    unused = 'service_tier=\"default\"'\n    native_home = Path", 1)
    path.write_text(text)
    assert "production_config_native_settings_drift" in production_config.repository_findings(repository)


@pytest.mark.parametrize('construction', ['direct', 'variable', 'optional'])
def test_repository_accepts_review_command_construction(repository, construction):
    path = repository / "apps/oa_finance_preaudit/codex_review_worker.py"
    source = path.read_text()
    start = source.index("\ndef _codex_command(")
    end = source.index("\ndef _decision_schema(", start)
    function = source[start:end]
    optional = function.index('    if tool_output_token_limit is not None:')
    if construction == 'direct':
        function = function[:optional].replace('    command = [\n', '    return [\n', 1)
    elif construction == 'variable':
        function = function[:optional] + '    return command\n\n'
    path.write_text(source[:start] + function + source[end:])
    assert production_config.repository_findings(repository) == []


@pytest.mark.parametrize('before,after', [
    ('        MODEL,', '        "other-model",'),
    ('\'model_provider="openai"\'', '\'model_provider="other"\''),
    ('\'service_tier="default"\'', '\'service_tier="priority"\''),
    ('f\'model_reasoning_effort="{REASONING_EFFORT}"\'', '\'model_reasoning_effort="high"\''),
    ('f"tool_output_token_limit={tool_output_token_limit}"', '\'service_tier="priority"\''),
    ('    return command\n', '    command.extend(["--model", "other-model"])\n    return command\n'),
    ('    return command\n', '    return tuple(command)\n'),
    ('    return command\n', '    if project_root:\n        return []\n    return command\n'),
])
def test_assembled_native_command_drift_and_unknown_paths_are_rejected(repository, before, after):
    path = repository / 'apps/oa_finance_preaudit/codex_review_worker.py'
    source = path.read_text()
    assert before in source
    path.write_text(source.replace(before, after, 1))
    assert 'production_config_native_settings_drift' in production_config.repository_findings(repository)


def test_native_command_cannot_shadow_the_authorized_model(repository):
    path = repository / 'apps/oa_finance_preaudit/codex_review_worker.py'
    source = path.read_text().replace('    command = [\n', '    MODEL = "other-model"\n    command = [\n', 1)
    path.write_text(source)
    assert 'production_config_native_settings_drift' in production_config.repository_findings(repository)
