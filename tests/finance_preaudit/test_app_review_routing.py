from pathlib import Path

import pytest

from apps.oa_finance_preaudit import production_config, controller_handoff, codex_review_worker
from apps.oa_approval import runtime_control


def test_default_openai_round_uses_app_transport_without_claiming_model(monkeypatch):
    monkeypatch.delenv('OA_REVIEW_PROVIDER', raising=False)
    monkeypatch.delenv('OA_REVIEW_TRANSPORT', raising=False)
    assert production_config.review_settings() == {
        'provider': 'openai', 'model': 'host-current-chat', 'reasoning_effort': 'unknown',
        'model_provider': 'openai', 'requested_service_tier': 'unknown', 'transport': 'host_chat',
    }


def test_openai_cli_selection_is_rejected_and_never_falls_back(monkeypatch):
    monkeypatch.setenv('OA_REVIEW_PROVIDER', 'openai')
    monkeypatch.setenv('OA_REVIEW_TRANSPORT', 'cli')
    with pytest.raises(ValueError, match='openai_review_cli_forbidden'):
        production_config.review_settings()


def test_direct_review_execution_cannot_launch_openai_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_review_worker.subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('OpenAI CLI launched'))
    result = codex_review_worker._execute_codex_raw(
        tmp_path, tmp_path / 'missing-action.json', 60, read_only=True, stderr_summary={})
    assert result.error_code == 'openai_review_cli_forbidden'
    assert result.process_state == 'not_started'


def test_real_controller_manifest_accepts_app_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'controller.lock'))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'review_configuration': production_config.review_settings('openai', transport='host_chat')})
        assert controller_handoff.status()['status'] == 'active'
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
        assert controller_handoff.status()['manifest_state'] == 'completed'
