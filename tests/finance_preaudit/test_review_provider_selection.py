import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from apps.oa_finance_preaudit import production_config
from scripts import finance_preaudit_run
from tests.finance_preaudit.test_execution_readback import recorded_execution, readback


@pytest.mark.parametrize('provider,model,effort', [
    ('deepseek', 'deepseek-flash', 'high'), ('openai', 'gpt-6.1-sol', 'medium'),
])
def test_fresh_review_process_uses_only_its_selected_provider(provider, model, effort, tmp_path):
    code = '''import json
from pathlib import Path
from apps.oa_finance_preaudit import codex_review_worker as worker
print(json.dumps({'settings':worker.REVIEW_SETTINGS,
 'command':worker._codex_command(Path.cwd(),Path('schema'),Path('decision')),
 'environment':{key:value for key,value in worker._reviewer_environment().items()
                if key in {'CODEX_HOME','OA_REVIEW_API_KEY','OPENAI_API_KEY','CODEX_API_KEY'}}}))'''
    environment = dict(os.environ, OA_REVIEW_PROVIDER=provider, OA_REVIEW_API_KEY='offline-fake-key',
                       OPENAI_API_KEY='unusable-native-key', CODEX_API_KEY='unusable-native-key',
                       CODEX_HOME=str(tmp_path / 'native-auth'))
    result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    assert actual['settings'] == production_config.review_settings(provider)
    command = actual['command']
    assert command[command.index('--model') + 1] == model
    assert f'model_reasoning_effort="{effort}"' in command
    assert 'OPENAI_API_KEY' not in actual['environment'] and 'CODEX_API_KEY' not in actual['environment']
    if provider == 'deepseek':
        assert actual['environment'] == {'OA_REVIEW_API_KEY': 'offline-fake-key'}
        assert 'model_providers.oa_deepseek.requires_openai_auth=false' in command
        assert any(value.startswith('model_catalog_json=') for value in command)
    else:
        assert actual['environment'] == {'CODEX_HOME': str(tmp_path / 'native-auth')}
        assert 'model_provider="openai"' in command
        assert not any('oa_deepseek' in value or 'model_catalog_json=' in value for value in command)


def test_successive_rounds_select_provider_before_startup_and_pass_it_to_supervisor(monkeypatch, capsys):
    observed = []
    monkeypatch.setenv(production_config.REVIEW_PROVIDER_ENV, 'deepseek')
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: None)
    def startup(_root):
        observed.append(('startup', production_config.review_settings()))
        return {'status': 'ready'}
    def supervise(command, **options):
        observed.append(('supervise', production_config.review_settings()))
        assert options['detached'] is True
        assert command[1:3] == ['-m', 'scripts.finance_preaudit_concurrency_probe']
        return {'result': 'completed', 'review_configuration': production_config.review_settings()}
    monkeypatch.setattr(finance_preaudit_run.approval_lifecycle, 'startup', startup)
    monkeypatch.setattr(finance_preaudit_run, 'supervise', supervise)
    for provider in ('deepseek', 'openai', 'deepseek'):
        assert finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--review-provider', provider,
                                         '--', '--rolling']) == 0
        assert json.loads(capsys.readouterr().out)['review_configuration'] == production_config.review_settings(provider)
    assert [settings['provider'] for _, settings in observed] == ['deepseek'] * 2 + ['openai'] * 2 + ['deepseek'] * 2


def test_readback_preserves_original_provider_and_rejects_mixed_round_configuration(tmp_path, monkeypatch):
    path, owner = recorded_execution(tmp_path, monkeypatch)
    receipt = json.loads(path.read_text())
    manifest_path = path.with_name('controller-manifest.json')
    manifest = json.loads(manifest_path.read_text())
    manifest['review_configuration'] = receipt['review_configuration']
    manifest_path.write_text(json.dumps(manifest))
    import hashlib
    receipt['manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(receipt))
    monkeypatch.setenv(production_config.REVIEW_PROVIDER_ENV, 'openai')
    result = readback(path, owner, '--progress')
    assert result.returncode == 0
    assert json.loads(result.stdout)['review_configuration']['provider'] == 'deepseek'
    receipt['review_configuration'] = production_config.review_settings('openai')
    path.write_text(json.dumps(receipt))
    assert readback(path, owner, '--progress').returncode == 1


def test_unknown_provider_has_no_fallback(monkeypatch):
    monkeypatch.setenv(production_config.REVIEW_PROVIDER_ENV, 'unknown')
    with pytest.raises(ValueError, match='approval_review_provider_invalid'):
        production_config.review_settings()
