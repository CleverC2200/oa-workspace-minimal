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
        "scripts/finance_preaudit_concurrency_probe.py", "config/deepseek-models.json",
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
        'MODEL = REVIEW_SETTINGS["model"]', 'MODEL = "deepseek-v4-flash"',
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


@pytest.mark.parametrize("relative,before,after,code", [
    ("config/automations/oa-skill-prompt.txt", "--capacity 3", "--capacity 4", "production_config_capacity_drift:template"),
    ("config/automations/oa-skill-prompt.txt", "--max-capacity 3", "--max-capacity 12", "production_config_capacity_drift:template"),
    ("docs/runbooks/hourly-production.md", "service_tier=unknown", "service_tier=priority", "production_config_service_tier_drift:runbook"),
    ("config/automations/oa-skill-prompt.txt", "--workflow general_expense", "--workflow packaging_layout_approval", "production_config_workflows_drift:template"),
    ("config/automations/oa-skill-prompt.txt", "--confirm-write YES", "--read-only", "production_config_command_invalid:template"),
    ("apps/oa_finance_preaudit/codex_review_worker.py", 'f\'model_reasoning_effort="{REASONING_EFFORT}"\'', '\'model_reasoning_effort="medium"\'', "production_config_native_settings_drift"),
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
    path.write_text(path.read_text() + "\n审核默认使用 DeepSeek。\n")
    assert "production_config_provider_instruction_drift:runbook" in production_config.repository_findings(repository)


def test_consistent_deepseek_template_cannot_override_the_codex_default(repository):
    path = repository / 'config/automations/oa-skill-prompt.txt'
    text = path.read_text().replace('--review-provider openai --', '--review-provider deepseek --')
    text = text.replace('model=gpt-6.1-sol reasoning_effort=medium model_provider=openai',
                        'model=deepseek-flash reasoning_effort=high model_provider=oa_deepseek')
    path.write_text(text)
    assert 'production_config_provider_selection_drift:template' in production_config.repository_findings(repository)


def test_public_snapshot_matches_authorized_production_settings():
    assert production_config.snapshot() == {
        "provider": "openai", "transport": "host_chat",
        "model": "host-current-chat", "reasoning_effort": "unknown", "model_provider": "openai",
        "requested_service_tier": "unknown", "initial_capacity": 3, "max_capacity": 3,
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


@pytest.mark.parametrize("field,value", [
    ("input_modalities", ["text"]), ("slug", "gpt-6.1-sol"),
    ("default_reasoning_level", "low"), ("prefer_websockets", True),
])
def test_catalog_drift_blocks_production_configuration(repository, field, value):
    import json
    path = repository / production_config.MODEL_CATALOG
    data = json.loads(path.read_text())
    data["models"][0][field] = value
    path.write_text(json.dumps(data))
    assert "production_config_catalog_drift" in production_config.repository_findings(repository)


def test_openai_cli_guard_cannot_be_replaced_with_a_different_failure(repository):
    path = repository / 'apps/oa_finance_preaudit/codex_review_worker.py'
    text = path.read_text().replace('openai_review_cli_forbidden', 'codex_process_failed')
    path.write_text(text)
    assert 'production_config_openai_cli_guard_drift' in production_config.repository_findings(repository)
